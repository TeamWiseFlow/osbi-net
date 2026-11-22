"""Deterministic "加油站" fact candidates from policy source text.

Faithful port of midplatform/ontology_query/facts.py. The graph stores
concrete entities/relations but not clause-level compliance facts. This
module scans the original block text already present in the snapshot and
extracts short normative sentences (不得/禁止/无需/允许/应当…, threshold
conditions, exemptions, penalties) with a policy context.

Two rules keep the output trustworthy:

* every fact is copied verbatim from the source block (only the amendment
  wrapper such as ``第二十七条修改为`` is stripped for presentation);
* the regulation named in the fact is resolved from the text itself, not from
  the longest policy node in the block. This matters for omnibus amendment
  decisions that touch dozens of regulations at once.

Selection favours facts with specificity and contrast (numbers, conditions,
exceptions, responsibility, penalties) over generic prohibitions. The shadow
topnews pipeline may rerank the leading candidates with the local model for
"反差感"; the model only picks an index and never rewrites the fact.

The FactLedger lives on the client report side (production keeps it in the
workflows work dir); callers pass the explicit ledger path.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re

POLICY_LABELS = {'政策文件', '国家级政策', '省级政策', '市级政策', '区级政策',
                 '街镇级政策', '政策征求意见稿'}
ISSUER_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构',
                 '政府机构（含党组织）', '机构组织'}

TITLE_RE = re.compile(r'《([^》]{2,60})》')
ARTICLE_TAIL_RE = re.compile(
    r'^\s*(?:第[一二三四五六七八九十百千零〇两\d]+条'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+款|第[一二三四五六七八九十百千零〇两\d]+项)?'
    r'|第[一二三四五六七八九十百千零〇两\d]+款)')
ITEM_START_RE = re.compile(r'\n(?=[一二三四五六七八九十百千]+、)')
BULLET_START_RE = re.compile(r'\n(?=[■●◆▪▫◦•])')
SENTENCE_SPLIT_RE = re.compile(r'[。！？\n]')

ENUM_PREFIX_RE = re.compile(
    r'^(?:[一二三四五六七八九十百千]+[、.．]|（[一二三四五六七八九十百千]+）|'
    r'\d+[.、．]|[■●◆▪▫◦•]|◎|★)\s*')
LEAD_RE = re.compile(
    r'^(?:通知|决定|意见|方案|规定|办法|规划|细则|条例|《[^》]{2,40}》)'
    r'(?:明确|规定|提出|要求|指出|强调)[，,:：\s]*')
AMEND_RE = re.compile(
    r'^(?:将)?\s*《([^》]{2,60})》\s*'
    r'(第[一二三四五六七八九十百千零〇两\d]+条'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+款)?'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+项)?)\s*'
    r'(?:修改为|改为|增加一款[，,]?作为(第[一二三四五六七八九十百千零〇两\d]+款)'
    r'|增加(?:一款)?[，,]?作为(第[一二三四五六七八九十百千零〇两\d]+款))?[：:]?')
BARE_ARTICLE_RE = re.compile(
    r'^(第[一二三四五六七八九十百千零〇两\d]+条'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+款)?'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+项)?)\s*'
    r'(?:修改为|改为|增加|删去)?[：:]?\s*')
ARTICLE_WRAPPER_RE = re.compile(
    r'^(第[一二三四五六七八九十百千零〇两\d]+条'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+款)?'
    r'(?:第[一二三四五六七八九十百千零〇两\d]+项)?)[，,]?\s*'
    r'(?:修改为|改为)[：:]?\s*')
DELETION_RE = re.compile(r'^(?:删去|删除)第[一二三四五六七八九十百千零〇两\d]+条')
CITATION_ONLY_RE = re.compile(
    r'(中的[“"][^”"]{0,40}[”"]修改为|^删去|'
    r'将[^。]{0,40}修改为[“"][^”"]{0,40}[”"]$)')
POST_RELEASE_RE = re.compile(r'发布《[^》]{2,60}》')
QA_TITLE_RE = re.compile(r'(答记者问|政策解读|解读|问答)')

NORM_STRONG_RE = re.compile(r'(不得|禁止|严禁|无需|免于|不再|取消|不允许|不能)')
NORM_MEDIUM_RE = re.compile(r'(可以|允许|应当|必须)')
EXCEPT_RE = re.compile(
    r'(除外|例外|但是|但书|除[^，。；]{0,12}外|无需|免于|不再|取消|放宽|简化|'
    r'豁免|免试|免检|不再要求|改为备案|一次性|首次|最多|最少|不适用|'
    r'不受[^，。；]{0,10}限制|国家负责|国家不负责|不予|不承担|'
    r'按照[^。；]{0,12}给予)')
PENALTY_RE = re.compile(r'(罚款|处罚|责令|吊销|追责|问责|处分|没收|'
                        r'列入[^，。；]{0,6}名单|惩戒|通报|约谈)')
THRESHOLD_RE = re.compile(
    r'(\d+(?:\.\d+)?[%％]|\d+(?:\.\d+)?(?:元|万元|亿元|倍|人|平方米|公里|米|'
    r'小时|个工作日|个百分点|岁|周岁)|不超过|不少于|不得少于|不得超过|'
    r'至少|至多)')
PRINCIPLE_RE = re.compile(
    r'(坚持|加强|推进|提升|完善|健全|落实|统筹|深化|着力|全面|持续|高质量|'
    r'积极|大力|切实|进一步)')
POLICY_WORD_RE = re.compile(
    r'(条例|规定|办法|意见|通知|方案|法律|政策|制度|标准|指南|细则|规划|'
    r'决定|规则|指引|清单|目录|纲要|批复|令)')
ORG_RE = re.compile(r'(部|委|局|厅|政府|银行|公司|中心|协会|街道|镇|办|院|社|'
                    r'会|署|行|台|集团|大学|学校|医院)')
MEDIA_RE = re.compile(r'(日报|晚报|广播|电视台|出版社|杂志|传媒|报社)')
DATE_RE = re.compile(r'(?<!\d)(19|20)\d{2}年(?:\d{1,2}月)?(?:\d{1,2}日)?')
BAD_RE = re.compile(
    r'(我们|我|你|记者|小编|点击|关注|请|欢迎|扫描|二维码|投稿|编辑|责编|'
    r'转载|原创稿件|版权|早安|晚安|例如|比如|首先|其次|总之|基于此|换言之|'
    r'这意味着|可以说|一体推进|不敢腐|不能腐|不想腐|^[一二三四五六七八九十]+是|'
    r'^答[：:])')
GENERIC_POLICY_NAMES = {
    '规划', '纲要', '报告', '方案', '通知', '规定', '办法', '条例', '意见',
    '决定', '规则', '指引', '清单', '目录', '批复', '令', '政策', '制度',
}
MIN_SCORE = 2
# One policy document / source may not supply a second 加油站 fact within N
# days: a single file often yields several high-score facts, and factId-only
# dedupe would make consecutive days look identical.
SOURCE_COOLDOWN_DAYS = 7
SOURCE_TITLE_MIN = 6


def _split_sentences(text: str):
    position = 0
    for match in SENTENCE_SPLIT_RE.finditer(text):
        yield position, match.start(), text[position:match.start()]
        position = match.end()
    yield position, len(text), text[position:]


def _quote_state(text: str, opened: bool) -> bool:
    """Track Chinese double quotes across sentence boundaries."""
    for char in text:
        if char == '“':
            opened = True
        elif char == '”':
            opened = False
    return opened


def _scope_start(text: str, position: int) -> int:
    """Start of the current law item or paragraph.

    Omnibus amendment decisions repeat a regulation title once per item, then
    list several ``第X条`` paragraphs. Scoping the title to that item (rather
    than the whole block) prevents facts from being attributed to the wrong
    regulation.
    """
    last_item = last_bullet = last_blank = -1
    for match in ITEM_START_RE.finditer(text[:position]):
        last_item = match.start()
    for match in BULLET_START_RE.finditer(text[:position]):
        last_bullet = match.start()
    for match in re.finditer(r'\n\s*\n', text[:position]):
        last_blank = match.end()
    if last_item >= 0:
        return max(last_item, last_bullet)
    return max(last_blank, last_bullet, 0)


def _policy_names(snapshot, block: dict) -> list[str]:
    names = []
    for uri in block.get('nodeUris', []):
        node = snapshot.nodes.get(uri)
        if not node or node.get('label') not in POLICY_LABELS:
            continue
        name = node.get('canonicalName') or node.get('name') or ''
        if name and name not in names:
            names.append(name)
    return names


def _usable_names(names: list[str]) -> list[str]:
    return [name for name in names
            if len(name) >= 4 and name not in GENERIC_POLICY_NAMES]


def _match_policy(names: list[str], raw_title: str) -> str:
    for name in names:
        if raw_title in name or name in raw_title:
            return name
    return ''


def _compact_text(value: str) -> str:
    """Normalize whitespace/punctuation for title and source matching."""
    return re.sub(r'[\s\W_]+', '', str(value or ''))


def _clean_title(title: str) -> str:
    title = re.sub(r'（内容发布日期[^）]*）$', '', title or '').strip()
    return title.split('_')[0].strip()


def _title_overlap(name: str, title: str, size: int = 8) -> bool:
    """Whether a policy name shares a distinctive phrase with the title."""
    compact_name = re.sub(r'[\s\W_]+', '', name)
    compact_title = re.sub(r'[\s\W_]+', '', title)
    if len(compact_name) < size:
        return compact_name in compact_title
    return any(compact_name[index:index + size] in compact_title
               for index in range(len(compact_name) - size + 1))


def _anchor(text: str, start: int, end: int, scope: int,
            names: list[str]) -> tuple[str, bool]:
    """Regulation title that governs the sentence, restricted to its item.

    Returns ``(name, is_direct)``. ``is_direct`` is true when the title is
    followed by 第X条/修改为/增加, i.e. the sentence is amending that
    regulation. A loose citation (只是“依据《XX法》”) must not override the
    current policy document when the block is itself a policy.
    """
    anchored = []
    loose = []
    for match in TITLE_RE.finditer(text, scope, end + 16):
        raw_title = match.group(1)
        name = _match_policy(names, raw_title) if names else raw_title
        if not name or name in GENERIC_POLICY_NAMES:
            continue
        tail = text[match.end():match.end() + 16]
        if tail.startswith(('的', '等', '、')):
            continue
        if (ARTICLE_TAIL_RE.match(tail) or '修改为' in tail[:12]
                or '增加' in tail[:10]):
            anchored.append((match.start(), name))
        else:
            loose.append((match.start(), name))
    for is_direct, group in ((True, anchored), (False, loose)):
        before = [entry for entry in group if entry[0] <= end]
        if before:
            return before[-1][1], is_direct
    return '', False


def _issuer(snapshot, regulation: str, doc_title: str, block: dict) -> str:
    regulation_uri = doc_uri = ''
    for uri, node in snapshot.nodes.items():
        if node.get('label') not in POLICY_LABELS:
            continue
        name = node.get('canonicalName') or node.get('name') or ''
        if not regulation_uri and regulation and (
                name == regulation or regulation in name or name in regulation):
            regulation_uri = uri
        if not doc_uri and doc_title and (
                name == doc_title or doc_title in name or name in doc_title):
            doc_uri = uri

    issuers = []

    def add(uri: str) -> None:
        if not uri:
            return
        for edge in snapshot.edges.values():
            if edge.get('predicateLabel') != '发布':
                continue
            if uri not in (edge.get('subject'), edge.get('object')):
                continue
            other_uri = (edge['object'] if edge.get('subject') == uri
                         else edge['subject'])
            other = snapshot.nodes.get(other_uri, {})
            name = other.get('canonicalName') or other.get('name') or ''
            if (other.get('label') in ISSUER_LABELS and name
                    and name not in issuers and not MEDIA_RE.search(name)):
                issuers.append(name)

    add(regulation_uri)
    add(doc_uri)
    if not issuers:
        publisher = block.get('publisher') or ''
        if publisher and ORG_RE.search(publisher) and not MEDIA_RE.search(publisher):
            issuers.append(publisher)
    return '、'.join(issuers[:3])


def _clean_fact(text: str) -> str:
    text = text.strip(' \t\r\n“”"\'：:，, ')
    text = ENUM_PREFIX_RE.sub('', text)
    text = LEAD_RE.sub('', text)
    return text.strip(' \t\r\n“”"\'：:，, ')


def _has_policy_context(clean: str) -> bool:
    """A substantive rule, threshold, or a long-context obligation."""
    has_number = bool(THRESHOLD_RE.search(clean))
    has_rule = bool(NORM_STRONG_RE.search(clean) or EXCEPT_RE.search(clean)
                    or PENALTY_RE.search(clean))
    has_medium = bool(NORM_MEDIUM_RE.search(clean))
    return has_rule or has_number or (has_medium and len(clean) >= 25)


def _score(clean: str) -> int:
    score = 0
    if THRESHOLD_RE.search(clean):
        score += 4
    if EXCEPT_RE.search(clean):
        score += 5
    if PENALTY_RE.search(clean):
        score += 2
    if NORM_STRONG_RE.search(clean):
        score += 1
        if len(clean) >= 35:
            score += 2
    if NORM_MEDIUM_RE.search(clean):
        score += 1
    if len(clean) >= 90:
        score -= 1
    specific = (THRESHOLD_RE.search(clean) or EXCEPT_RE.search(clean)
                or PENALTY_RE.search(clean))
    if (PRINCIPLE_RE.search(clean) and not specific
            and not NORM_STRONG_RE.search(clean)):
        score -= 2
    if (NORM_STRONG_RE.search(clean) and not specific
            and len(clean) < 30):
        score -= 2
    return score


def extract_facts(snapshot, *, from_date: str = '', to_date: str = '',
                  limit: int = 20) -> list[dict]:
    facts = []
    seen = set()
    for block in snapshot.blocks.values():
        date = block.get('publishedDate') or ''
        if from_date and date and date < from_date:
            continue
        if to_date and date and date > to_date:
            continue
        text = block.get('rawText') or ''
        if not text:
            continue
        title = block.get('title') or ''
        clean_title = _clean_title(title)
        content_type = block.get('contentType') or ''
        names = _usable_names(_policy_names(snapshot, block))
        is_policy = content_type == 'policy' and not QA_TITLE_RE.search(clean_title)
        is_news = content_type == 'news'
        if not (is_policy or is_news):
            continue
        if not names and not POLICY_WORD_RE.search(clean_title):
            continue
        compact_title = _compact_text(clean_title)
        compact_body = _compact_text(text)
        doc_title = ''
        for name in names:
            compact_name = _compact_text(name)
            if compact_name and (compact_name in compact_title
                                 or compact_title in compact_name):
                doc_title = name
                break
        if not doc_title and is_policy:
            doc_title = names[0] if len(names) == 1 else clean_title
        document_policy = ''
        if is_news and POLICY_WORD_RE.search(clean_title):
            title_policies = [name for name in names
                              if _title_overlap(name, clean_title)]
            if title_policies:
                document_policy = title_policies[0]
        if is_news and document_policy:
            doc_title = document_policy
        elif is_policy and _compact_text(doc_title) not in {
                _compact_text(name) for name in names}:
            body_names = [name for name in names
                          if _compact_text(name) in compact_body]
            if body_names:
                doc_title = max(body_names, key=len)

        current_scope = -1
        current_reg = ''
        current_article = ''
        quote_open = False
        for start, end, raw_text in _split_sentences(text):
            scope = _scope_start(text, start)
            if scope != current_scope:
                current_scope = scope
                current_article = ''
                quote_open = False
            quote_before = quote_open
            quote_open = _quote_state(raw_text, quote_before)

            raw = raw_text.strip(' \t\r\n“”"\'：:，, ')
            if len(raw) < 10 or BAD_RE.search(raw):
                continue
            raw = ENUM_PREFIX_RE.sub('', raw).strip()
            if CITATION_ONLY_RE.search(raw) or DELETION_RE.match(raw):
                continue

            article = ''
            regulation = ''
            amend = AMEND_RE.match(raw)
            if amend:
                regulation = _match_policy(names, amend.group(1))
                if names and not regulation:
                    continue
                article = amend.group(2) or ''
                if amend.group(3):
                    article += '第' + amend.group(3) + '款'
                elif amend.group(4):
                    article += '第' + amend.group(4) + '款'
                clean = raw[amend.end():].strip(' \t\r\n“”"\'：:，, ')
                wrapper = ARTICLE_WRAPPER_RE.match(clean)
                if wrapper:
                    article = wrapper.group(1)
                    clean = clean[wrapper.end():].strip()
                clean = clean.strip(' \t\r\n“”"\'：:，, ')
            else:
                bare = BARE_ARTICLE_RE.match(raw)
                if bare and (regulation or current_reg or names or doc_title):
                    article = bare.group(1)
                    clean = raw[bare.end():].strip()
                else:
                    clean = _clean_fact(raw)
                    if quote_before:
                        article = current_article
                clean = clean.strip(' \t\r\n“”"\'：:，, ')
            if article:
                current_article = article

            anchor = ''
            if not regulation:
                anchor, anchor_direct = _anchor(text, start, end, scope, names)
                # A loose "依据《XX法》" inside a policy text is only a
                # citation; it must not attribute this document's clauses to
                # the cited regulation. Only direct anchors (第X条修改为/增加)
                # may override the current document.
                if anchor and (anchor_direct or not (is_policy and doc_title)):
                    regulation = anchor
                elif quote_before and current_reg:
                    regulation = current_reg
                elif document_policy:
                    regulation = document_policy
                elif len(names) == 1:
                    regulation = names[0]
                elif is_policy and doc_title:
                    regulation = doc_title
            if not regulation:
                continue
            current_reg = regulation

            if not (is_policy or anchor or document_policy or len(names) == 1):
                continue
            strong_rule = bool(NORM_STRONG_RE.search(clean)
                               or PENALTY_RE.search(clean))
            if is_news and not (
                    LEAD_RE.search(raw) or POST_RELEASE_RE.search(raw)
                    or ((document_policy or anchor) and strong_rule)):
                continue
            if len(clean) < 10 or len(clean) > 180:
                continue
            if (BAD_RE.search(clean) or CITATION_ONLY_RE.search(clean)
                    or DELETION_RE.match(clean)):
                continue
            if not _has_policy_context(clean):
                continue

            has_rule = bool(NORM_STRONG_RE.search(clean) or EXCEPT_RE.search(clean)
                            or PENALTY_RE.search(clean))
            if POST_RELEASE_RE.search(clean) and not has_rule:
                continue
            if (THRESHOLD_RE.search(clean) and not has_rule
                    and not THRESHOLD_RE.search(DATE_RE.sub('', clean))):
                continue
            if re.fullmatch(r'.{0,60}（(?:19|20)\d{2}年.{0,60}）', clean):
                continue

            score = _score(clean)
            if score < MIN_SCORE:
                continue

            key = re.sub(r'[\s\W_]+', '', clean)
            if not key or key in seen:
                continue
            seen.add(key)
            facts.append(dict(
                factId=hashlib.sha1(key.encode()).hexdigest()[:16],
                text=clean,
                article=article,
                score=score,
                policy=regulation,
                policyUri='',
                docTitle=doc_title or regulation,
                issuer=_issuer(snapshot, regulation, doc_title or regulation,
                               block),
                articleId=block.get('articleId', ''),
                blockId=block.get('extractionId', ''),
                sourceTitle=title,
                sourceUrl=block.get('url', ''),
                publisher=block.get('publisher', ''),
                publishedDate=date,
                board=block.get('board', '')))

    facts.sort(key=lambda item: (item['score'], item['publishedDate'],
                                 len(item['text'])), reverse=True)
    return facts[:limit]


def _source_key(fact: dict) -> str:
    """Stable identity of the document a fact came from.

    ``docTitle`` is the actual document name resolved from the block graph;
    ``policy`` and ``sourceTitle`` are fallbacks when the graph is thin.
    """
    return _compact_text(fact.get('docTitle') or fact.get('policy')
                         or fact.get('sourceTitle') or '')


def _same_source(left: str, right: str) -> bool:
    """Treat short/long variants of one document title as the same source."""
    left = _compact_text(left)
    right = _compact_text(right)
    if not left or not right:
        return False
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    return (len(short) >= SOURCE_TITLE_MIN and short in long
            and len(long) - len(short) <= 24)


def _distinct_sources(facts: list[dict], limit: int) -> list[dict]:
    """Keep at most one candidate per document so the reranker sees variety."""
    picked = []
    keys = []
    for fact in facts:
        key = _source_key(fact)
        if key and any(_same_source(key, other) for other in keys):
            continue
        picked.append(fact)
        if key:
            keys.append(key)
        if len(picked) >= limit:
            break
    return picked or facts[:limit]


def leading_facts(snapshots, *, from_date: str = '', to_date: str = '',
                  ledger: 'FactLedger | None' = None, limit: int = 8,
                  source_cooldown_days: int = SOURCE_COOLDOWN_DAYS) -> list[dict]:
    """Merge candidates from all libraries, keep the best for a reranker."""
    candidates = []
    seen = set()
    for snapshot in snapshots:
        for fact in extract_facts(snapshot, from_date=from_date,
                                  to_date=to_date, limit=max(limit * 3, 24)):
            if fact['factId'] in seen:
                continue
            seen.add(fact['factId'])
            candidates.append(fact)
    candidates.sort(key=lambda item: (item['score'], item['publishedDate'],
                                      len(item['text'])), reverse=True)
    if ledger is not None:
        visible = ledger.unseen(candidates)
        # factId dedupe first, then source cooldown; if the cooldown leaves
        # nothing, fall back to the factId-deduped list so the 加油站 column
        # never disappears because dedupe was too strict.
        recent = ledger.recent_sources(
            fact_lookup={fact['factId']: fact for fact in candidates},
            days=source_cooldown_days,
            today=to_date)
        fresh = [fact for fact in visible
                 if not any(_same_source(_source_key(fact), key)
                            for key in recent)]
        candidates = fresh or visible
    return _distinct_sources(candidates, limit)[:limit]


class FactLedger:
    """Remember facts already sent so daily topnews does not repeat them.

    Besides exact ``factId`` dedupe, records carry the source document and
    send date so the same policy file cannot supply 加油站 several days in a
    row. The old ``{"factIds": [...]}`` format is still readable.
    """

    RECORD_LIMIT = 120

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        fact_ids = {str(value) for value in (data.get('factIds') or []) if value}
        records = [dict(record) for record in (data.get('records') or [])
                   if isinstance(record, dict) and record.get('factId')]
        for record in records:
            fact_ids.add(str(record['factId']))
        return dict(factIds=sorted(fact_ids), records=records)

    def unseen(self, facts: list[dict]) -> list[dict]:
        sent = set(self._load()['factIds'])
        return [fact for fact in facts if fact.get('factId') not in sent]

    def recent_sources(self, *, fact_lookup: dict | None = None,
                       days: int = SOURCE_COOLDOWN_DAYS,
                       today: str = '') -> list[str]:
        """Source keys sent within the cooldown window."""
        data = self._load()
        cutoff = ''
        today = today or datetime.now().strftime('%Y-%m-%d')
        try:
            cutoff = (datetime.strptime(today, '%Y-%m-%d')
                      - timedelta(days=days)).strftime('%Y-%m-%d')
        except ValueError:
            cutoff = ''
        sources = []
        recorded = {record.get('factId') for record in data['records']}
        for record in data['records']:
            key = record.get('sourceKey') or ''
            if not key and fact_lookup:
                fact = fact_lookup.get(record.get('factId'))
                if fact:
                    key = _source_key(fact)
            if not key:
                continue
            sent_date = str(record.get('sentDate') or '')
            if cutoff and sent_date and sent_date < cutoff:
                continue
            sources.append(key)
        # Old ledgers only stored factIds; when the corresponding fact is
        # still in the current candidate window, infer its source so the
        # first run after the upgrade already benefits from the cooldown.
        if fact_lookup:
            for fact_id in data['factIds']:
                if fact_id in recorded:
                    continue
                fact = fact_lookup.get(fact_id)
                if fact:
                    sources.append(_source_key(fact))
        return list(dict.fromkeys(sources))

    def mark(self, facts: list[dict], sent_date: str = '') -> None:
        if not facts:
            return
        sent_date = sent_date or datetime.now().strftime('%Y-%m-%d')
        data = self._load()
        sent = set(data['factIds'])
        records = data['records']
        by_id = {record['factId']: record for record in records}
        for fact in facts:
            fact_id = fact.get('factId')
            if not fact_id:
                continue
            sent.add(fact_id)
            record = by_id.get(fact_id)
            if record is None:
                record = dict(factId=fact_id)
                records.append(record)
                by_id[fact_id] = record
            record.update(
                sourceKey=_source_key(fact),
                policy=fact.get('policy') or '',
                docTitle=fact.get('docTitle') or '',
                articleId=fact.get('articleId') or '',
                sourceUrl=fact.get('sourceUrl') or '',
                publishedDate=fact.get('publishedDate') or '',
                sentDate=sent_date)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(dict(factIds=sorted(sent),
                            records=records[-self.RECORD_LIMIT:]),
                       ensure_ascii=False, indent=2), encoding='utf-8')


def format_fact(fact: dict) -> str:
    policy = fact.get('policy') or fact.get('sourceTitle') or ''
    article = fact.get('article') or ''
    body = (fact.get('text') or '').strip().rstrip('。；')
    if policy:
        lead = f'《{policy}》' + article
        body = f'{lead}规定，{body}。'
    else:
        body = body + '。'
    line = f'#加油站\n{body}'
    doc_title = fact.get('docTitle') or policy
    publisher = fact.get('issuer') or ''
    meta = ' '.join(part for part in (publisher, fact.get('publishedDate'))
                    if part)
    source = f'《{doc_title}》' if doc_title else ''
    if source or meta:
        line += f'\n依据：{source}' + (f'（{meta}）' if meta else '')
    return line


def top_fact(snapshots, *, from_date: str = '', to_date: str = '',
             ledger: FactLedger | None = None) -> dict | None:
    candidates = leading_facts(snapshots, from_date=from_date, to_date=to_date,
                               ledger=ledger, limit=1)
    return candidates[0] if candidates else None
