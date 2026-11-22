"""Deterministic per-board hotspot selection for the shadow weekly.

Ported from midplatform/workflows/ontology_weekly_select.py:

- 基层党建/社区治理/数字社区: node-level action focus (signal predicates,
  units/streets/policies/actions/groups, leader boost 300 / non-target -300,
  proximity, cross-week fresh/progress/stale, subset dedupe);
- 经济动态/政策速递: chain-level policy focus (proximity>=2, richness>=2,
  lineage/implementer/prior/leader gate, two-hop reach, merge by implementer);
- 经济动态 fallback: article-level street relevance (signal-noise hints,
  local +16 / other-region -8, score>=18, signal>=3, title dedupe 0.6).

Read-only and model-free; selection outcomes are logged, never printed.
"""
from __future__ import annotations

from collections import defaultdict
import logging
import re

from .leader_rules import (LEADER_ACTIVITY_RE, is_national_leader,
                           is_target_jurisdiction, is_target_leader_sentence)
from .shadow_relations import (
    ACTION_LABELS, ORG_LABELS, POLICY_LABELS, SHANGHAI_HINTS,
    _action_implementers, _display_name, _norm, _similarity,
    _window_article_ids, build_relation_dossier,
)
from .shadow_snapshot import article_view
from .weekly_history import COMMENTARY_FLAGS, classify_repeat

logger = logging.getLogger(__name__)

HOTSPOT_LABELS = {'行动事项', '市级行动', '区级行动', '街镇行动', '社区行动',
                  '居民活动', '调研视察', '治理机制', '数字平台系统'}
STREET_ORG_LABELS = {'街镇机构', '社区自治组织'}
STREET_ACTION_LABELS = {'街镇行动', '社区行动', '居民活动'}
UNIT_PREDICATES = {'参与', '实施主体', '主办', '落实', '服务', '采用',
                   '建设运营', '前往', '考察'}
POLICY_PREDICATES = {'落实', '依据', '响应', '提及', '涉及', '发布'}
ACTION_PREDICATES = {'参与', '涉及', '响应', '落实', '服务', '实施主体'}
SIGNAL_PREDICATES = UNIT_PREDICATES | POLICY_PREDICATES | ACTION_PREDICATES
REACH_PREDICATES = {'落实', '依据', '响应', '实施主体', '参与', '主办',
                    '服务', '采用', '建设运营', '隶属'}
NOISE_NODE_LABELS = {'概念议题', '通用名词', '职务', '人物'}
LEADER_EXTRA_LABELS = {'会议论坛', '事件'}
LEADER_TEXT_LABELS = {'调研视察', '会议论坛', '事件', '行动事项', '市级行动',
                      '区级行动', '街镇行动', '社区行动'}
LEADER_SCORE_BOOST = 300
NON_TARGET_LEADER_PENALTY = 300
LEADER_EDGE_PREDICATES = {'参与', '前往', '考察', '强调'}
PROVINCE_LABELS = {
    '北京': '北京市', '天津': '天津市', '重庆': '重庆市', '河北': '河北省',
    '山西': '山西省', '内蒙古': '内蒙古自治区', '辽宁': '辽宁省', '吉林': '吉林省',
    '黑龙江': '黑龙江省', '江苏': '江苏省', '浙江': '浙江省', '安徽': '安徽省',
    '福建': '福建省', '江西': '江西省', '山东': '山东省', '河南': '河南省',
    '湖北': '湖北省', '湖南': '湖南省', '广东': '广东省', '广西': '广西壮族自治区',
    '海南': '海南省', '四川': '四川省', '贵州': '贵州省', '云南': '云南省',
    '西藏': '西藏自治区', '陕西': '陕西省', '甘肃': '甘肃省', '青海': '青海省',
    '宁夏': '宁夏回族自治区', '新疆': '新疆维吾尔自治区',
}
ACTION_BOARDS = {'基层党建', '社区治理', '数字社区'}
POLICY_BOARDS = {'经济动态', '政策速递'}
ACTION_MIN_EDGES = 4
FOCUS_TOP_N = 2
ECONOMY_MIN_SCORE = 18
ECONOMY_MIN_SIGNAL = 3
ECONOMY_LOCAL_BOOST = 16
ECONOMY_OTHER_REGION_PENALTY = 8
ECONOMY_TITLE_SIMILARITY = 0.6
ECONOMY_RELEVANCE_HINTS = {
    '街镇': 6, '街道': 5, '社区': 5, '居民': 4, '基层': 4,
    '就业': 4, '稳就业': 6, '就业服务': 6, '消费': 3, '消费券': 6,
    '以旧换新': 6, '补贴': 4, '贴息': 4, '购房': 3, '刚需': 3,
    '民生': 3, '培训': 3, '中小企业': 5, '小微企业': 5, '民营': 4,
    '个体工商户': 6, '便民': 5, '生活圈': 5, '菜市场': 6,
    '养老': 4, '托育': 4, '商圈': 4, '商业': 2, '专项资金': 4,
    '贷款': 2, '融资': 2, '创业': 4, '营商环境': 3, '为企服务': 5,
    '招商引资': 2, '智慧社区': 5, '数字社会': 4, '数字经济': 2,
    '数据要素': 2,
}
ECONOMY_NOISE_HINTS = {
    'A股': 8, '港股': 8, '美股': 8, '日股': 8, '韩股': 8, '个股': 6,
    '股票': 6, '分红': 7, '回购': 6, '增持': 6, 'ETF': 6, '债券': 5,
    '证券投资基金': 5, '公募基金': 5, '基金净值': 6, '汇率': 5,
    '外储': 6, '央行': 4, '证监会': 6,
    '资本市场': 7, '券商': 6, '市值': 6, '涨停': 6, '复牌': 6,
    '重组': 4, '股价': 6, '净值': 5, '减持': 6, 'IPO': 7, '信披': 7,
    '大牛股': 8,
}


def _service_for(services, board: str):
    library = 'economy' if board == '经济动态' else 'policy'
    if library in services:
        return services[library]
    return next(iter(services.values()))


def _board_articles(snapshot, board: str, window_ids: set) -> list:
    return [article for article in snapshot.articles.values()
            if board in article.get('boards', [])
            and article['articleId'] in window_ids]


def _signal(snapshot, uri: str, window_ids: set) -> dict:
    """Structural relationships around one node, ignoring location/bare mentions."""
    units, policies, actions, streets, groups, predicates = {}, {}, {}, {}, {}, set()
    edges = {}
    for edge in snapshot.edges.values():
        predicate = edge['predicateLabel']
        if predicate not in SIGNAL_PREDICATES:
            continue
        if uri not in (edge['subject'], edge['object']):
            continue
        if not any(article_id in window_ids for article_id in edge['articleIds']):
            continue
        other_uri = edge['object'] if edge['subject'] == uri else edge['subject']
        other = snapshot.nodes.get(other_uri, {})
        label = other.get('label', '')
        if label in NOISE_NODE_LABELS:
            continue
        edges[edge['key']] = edge
        predicates.add(predicate)
        if label in ORG_LABELS and predicate in UNIT_PREDICATES:
            units[other_uri] = other.get('name', '')
            if label in STREET_ORG_LABELS:
                streets[other_uri] = other.get('name', '')
        if label in POLICY_LABELS and predicate in POLICY_PREDICATES:
            policies[other_uri] = other.get('name', '')
        if label in ACTION_LABELS and predicate in ACTION_PREDICATES:
            actions[other_uri] = other.get('name', '')
        if label == '社会群体':
            groups[other_uri] = other.get('name', '')
    return dict(edgeCount=len(edges), predicates=sorted(predicates),
                units=list(units.values()), policies=list(policies.values()),
                actions=list(actions.values()), streets=list(streets.values()),
                groups=list(groups.values()))


def _proximity(snapshot, node: dict, article_ids: set) -> int:
    texts = [node.get('name', '')]
    for article_id in article_ids:
        article = snapshot.articles.get(article_id)
        if article:
            texts.extend([article.get('title', ''), article.get('publisher', '')])
    return 2 if any(hint in ' '.join(texts) for hint in SHANGHAI_HINTS) else 0


def _clean_raw(text: str) -> str:
    text = re.sub(r'（空白图像[^）]*）', '', text or '')
    text = re.sub(r'<\|[^|]*\|>', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _leader_text(snapshot, article_ids, limit_per_article: int = 1200) -> str:
    texts = []
    for article_id in list(article_ids)[:3]:
        article = snapshot.articles.get(article_id)
        if not article:
            continue
        texts.extend([article.get('title', ''), article.get('publisher', '')])
        for block_id in article.get('blockIds', []):
            raw = _clean_raw(snapshot.blocks.get(block_id, {}).get('rawText', ''))
            if raw:
                texts.append(raw[:limit_per_article])
                break
    return ' '.join(texts)


def _location_from_text(text: str) -> str:
    if '静安' in text:
        return '上海市静安区'
    for name, label in PROVINCE_LABELS.items():
        if name in text:
            return label
    if '上海' in text:
        return '上海市'
    return ''


def _detect_location(snapshot, node: dict, article_ids) -> str:
    """标题/来源优先，正文兜底；避免外地稿因正文提到上海而误标上海。"""
    primary = [node.get('name', '')]
    raw = []
    for article_id in list(article_ids)[:2]:
        article = snapshot.articles.get(article_id)
        if not article:
            continue
        primary.extend([article.get('title', ''), article.get('publisher', '')])
        for block_id in article.get('blockIds', []):
            text = _clean_raw(snapshot.blocks.get(block_id, {}).get('rawText', ''))
            if text:
                raw.append(text[:800])
                break
    location = _location_from_text(' '.join(primary))
    return location or _location_from_text(' '.join(raw))


def _leader_person_uris(snapshot) -> set:
    result = set()
    for edge in snapshot.edges.values():
        if edge['predicateLabel'] not in ('现任', '任职'):
            continue
        post = snapshot.nodes.get(edge['object'], {})
        person = snapshot.nodes.get(edge['subject'], {})
        if (post.get('label') == '职务' and is_target_jurisdiction(post.get('name', ''))
                and person.get('label') in ('人物', '领导干部')):
            result.add(edge['subject'])
    return result


def _leader_detail(snapshot, node: dict, article_ids, leader_persons: set) -> dict:
    """Concrete Shanghai/Jing'an leader evidence for one hotspot node."""
    text = node.get('name', '') + ' ' + _leader_text(snapshot, article_ids)
    snippets = []
    if node.get('label') in LEADER_TEXT_LABELS:
        for sentence in re.split(r'[。；！？\n]', text):
            if is_target_leader_sentence(sentence, text):
                snippets.append(sentence.strip()[:180])
    names = []
    if leader_persons:
        for edge in snapshot.edges.values():
            if edge['predicateLabel'] not in LEADER_EDGE_PREDICATES:
                continue
            if node['uri'] not in (edge['subject'], edge['object']):
                continue
            other = (edge['object'] if edge['subject'] == node['uri']
                     else edge['subject'])
            if other in leader_persons:
                person = snapshot.nodes.get(other, {})
                if person.get('name') and person['name'] not in names:
                    names.append(person['name'])
    return dict(snippets=list(dict.fromkeys(snippets))[:2], names=names[:3])


def _is_non_target_leader(snapshot, node: dict, article_ids,
                          leader_persons: set) -> bool:
    """Leader-like activity that is not clearly Shanghai city / Jing'an."""
    detail = _leader_detail(snapshot, node, article_ids, leader_persons)
    if detail['snippets'] or detail['names']:
        return False
    text = node.get('name', '') + ' ' + _leader_text(snapshot, article_ids)
    if is_national_leader(text):
        return False
    label = node.get('label', '')
    if label == '调研视察':
        return True
    if label not in LEADER_TEXT_LABELS:
        return False
    return any(LEADER_ACTIVITY_RE.search(sentence)
               for sentence in re.split(r'[。；！？\n]', text))


def _excerpt(snapshot, article_ids, limit: int = 1400) -> str:
    pieces = []
    for article_id in list(article_ids)[:2]:
        article = snapshot.articles.get(article_id)
        if not article:
            continue
        for block_id in article.get('blockIds', []):
            raw = _clean_raw(snapshot.blocks.get(block_id, {}).get('rawText', ''))
            if raw:
                pieces.append(raw[:800])
                break
    return '\n'.join(pieces)[:limit]


def _focus_items(snapshot, article_ids, limit: int = 3) -> list:
    items = []
    for article_id in article_ids:
        article = snapshot.articles.get(article_id)
        if not article:
            continue
        item = article_view(snapshot, article, with_relations=True,
                            relation_limit=10)
        item['excerpt'] = _excerpt(snapshot, [article_id])
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _hint_score(text: str, hints: dict) -> int:
    return sum(weight for hint, weight in hints.items() if hint in text)


def _economy_relevance(item: dict, snapshot=None) -> tuple[int, list[str]]:
    """Deterministic street-relevance score for one economy article.

    Accepts an article_view item; when the item carries ``leaderText``/raw
    body via its blocks the snapshot is used, otherwise the item's excerpt.
    """
    title = item.get('title') or ''
    publisher = item.get('publisher') or ''
    entities = ' '.join(entity.get('name') or ''
                        for entity in item.get('keyEntities') or [])
    body = ''
    if snapshot is not None:
        for block_id in item.get('blockIds') or []:
            raw = _clean_raw(snapshot.blocks.get(block_id, {}).get('rawText', ''))
            if raw:
                body = raw[:1200]
                break
    if not body:
        body = _clean_raw(item.get('excerpt') or item.get('rawText') or '')[:1200]

    signal = (2 * _hint_score(title, ECONOMY_RELEVANCE_HINTS)
              + _hint_score(publisher, ECONOMY_RELEVANCE_HINTS)
              + _hint_score(entities, ECONOMY_RELEVANCE_HINTS)
              + _hint_score(body, ECONOMY_RELEVANCE_HINTS))
    noise = (3 * _hint_score(title, ECONOMY_NOISE_HINTS)
             + _hint_score(publisher, ECONOMY_NOISE_HINTS)
             + _hint_score(entities, ECONOMY_NOISE_HINTS)
             + _hint_score(body, ECONOMY_NOISE_HINTS))
    primary = f'{title} {publisher} {entities}'
    local = (ECONOMY_LOCAL_BOOST
             if any(hint in primary for hint in SHANGHAI_HINTS)
             else (2 if any(hint in body for hint in SHANGHAI_HINTS) else 0))
    other_region = (ECONOMY_OTHER_REGION_PENALTY
                    if any(region in primary for region in PROVINCE_LABELS)
                    else (2 if any(region in body for region in PROVINCE_LABELS)
                          else 0))
    flags = set(item.get('qualityFlags') or [])
    quality_penalty = (3 if 'report' in flags else 0)
    quality_penalty += (2 if 'commentary' in flags else 0)
    quality_penalty += (1 if 'low_density' in flags else 0)
    quality_penalty += (1 if 'zero_relation' in flags else 0)
    structure = min(item.get('relationCount', 0), 3)
    score = signal - noise + local - other_region + structure - quality_penalty
    matched = sorted(hint for hint in ECONOMY_RELEVANCE_HINTS
                     if hint in f'{title} {entities} {body}')
    return score, matched


def _dedupe_economy_rows(rows: list, top_n: int) -> list:
    selected = []
    for row in rows:
        title = row['item'].get('title', '')
        if any(_similarity(title, kept['item'].get('title', ''))
               >= ECONOMY_TITLE_SIMILARITY for kept in selected):
            continue
        selected.append(row)
        if len(selected) >= top_n:
            break
    return selected


def _format_economy_brief(rows: list) -> str:
    lines = ['【图谱选出的本板块经济信息（必须围绕以下内容写，不要另选）】']
    for index, row in enumerate(rows, 1):
        item = row['item']
        lines.append(f"{index}. {item.get('title', '')}"
                     f"（{item.get('publisher', '')}，"
                     f"{item.get('publishedDate', '')}）")
        if row.get('matched'):
            lines.append('   街道相关信号：' + '、'.join(row['matched'][:6]))
        if item.get('excerpt'):
            lines.append('   摘要：' + item['excerpt'][:600])
    lines.append(
        '写作要求：只写与居民生活、就业、消费、社区商业、中小微企业和基层经济工作'
        '相关的内容；不要写A股/港股行情、个股分红、回购、基金净值等资本市场内容；'
        '非上海/全国性政策第一次出现时写清适用范围；每个小节最后落到临汾路街道的启示。'
        '不要使用字段式小标题或表格，不要写“材料未提供……”之类占位说明。')
    return '\n'.join(lines)


def _economy_focus(service, board: str, from_date: str, to_date: str,
                   top_n: int) -> dict:
    """Article-level fallback for 经济动态 when no policy chain qualifies."""
    snapshot = service.snapshot
    window_ids = _window_article_ids(snapshot, from_date, to_date)
    rows = []
    low_score = 0
    low_signal = 0
    for article in _board_articles(snapshot, board, window_ids):
        if 'digest' in (article.get('qualityFlags') or []):
            continue
        item = article_view(snapshot, article, with_relations=True,
                            relation_limit=10)
        item['excerpt'] = _excerpt(snapshot, [article['articleId']], limit=800)
        score, matched = _economy_relevance(item, snapshot)
        if score < ECONOMY_MIN_SCORE:
            low_score += 1
            continue
        signal = sum(ECONOMY_RELEVANCE_HINTS[hint] for hint in matched)
        if not matched or signal < ECONOMY_MIN_SIGNAL:
            low_signal += 1
            continue
        rows.append(dict(item=item, score=score, matched=matched,
                         articleIds=[article['articleId']]))
    rows.sort(key=lambda row: (row['score'], row['item'].get('relationCount', 0)),
              reverse=True)
    selected = _dedupe_economy_rows(rows, top_n)
    logger.info('weekly economy fallback: board=%s candidates=%s selected=%s '
                'rejected_low_score=%s rejected_low_signal=%s',
                board, len(rows), len(selected), low_score, low_signal)
    return dict(type='economy', board=board,
                items=[row['item'] for row in selected],
                brief=_format_economy_brief(selected))


def _dedupe_hotspots(rows: list) -> list:
    selected = []
    for row in rows:
        duplicate = False
        for kept in selected:
            same_article = bool(set(row['articleIds']) & set(kept['articleIds']))
            if same_article and _similarity(row['name'], kept['name']) >= 0.45:
                duplicate = True
                break
        if not duplicate:
            selected.append(row)
    return selected


def _localize_name(name: str, location: str) -> str:
    """把外地材料里的“全市/我市/全区”补成明确省市，避免误读为上海。"""
    if not location or location in ('上海市', '上海市静安区'):
        return name
    for prefix in ('全市', '我市', '全区', '我区'):
        if name.startswith(prefix):
            return location + name[len(prefix):]
    return name


def _format_action_brief(hotspots: list) -> str:
    lines = ['【图谱选出的本板块热点（必须围绕以下主体写，不要另选）】']
    for index, hotspot in enumerate(hotspots, 1):
        lines.append(
            f"{index}. {hotspot['name']}（{hotspot['label']}）："
            f"{hotspot['edgeCount']} 条关系边，涉及 {len(hotspot['units'])} 家单位、"
            f"{len(hotspot['groups'])} 类群体、{len(hotspot['policies'])} 项政策、"
            f"{len(hotspot['actions'])} 个关联行动")
        if hotspot.get('location'):
            lines.append('   地域：' + hotspot['location'])
        if hotspot.get('priorReported'):
            prior = hotspot['priorReported']
            lines.append(
                f"   跨周提示：该热点上周（{prior['weekEnd']}）已报道过"
                f"（当时主题：{prior['topic']}）。本周新增 {prior['newCount']} 篇材料，"
                '必须聚焦本周相对上周的新进展、新做法或动态变化，开头点明“上周报道的…本周…”，'
                '不要重复上周已报的会议/事件本身；若只是旧事重述则不写。')
        if hotspot.get('leader'):
            detail = hotspot.get('leaderDetail') or {}
            if detail.get('names'):
                lines.append('   领导：' + '、'.join(detail['names']))
            for snippet in detail.get('snippets', []):
                lines.append('   领导活动原文：' + snippet)
            lines.append('   权重：上海市/静安区领导调研、考察或讲话类信息，权重最高。')
        elif hotspot.get('nonTarget'):
            lines.append('   降权：非上海市/静安区领导活动，或领导属地不明确。')
        if hotspot['units']:
            lines.append('   实施/参与单位：' + '、'.join(hotspot['units'][:6]))
        if hotspot['groups']:
            lines.append('   涉及群体：' + '、'.join(hotspot['groups'][:8]))
        if hotspot['policies']:
            lines.append('   对接政策：' + '、'.join(hotspot['policies'][:4]))
        if hotspot['actions']:
            location = hotspot.get('location', '')
            actions = [_localize_name(name, location)
                       for name in hotspot['actions'][:4]]
            lines.append('   关联行动：' + '、'.join(actions))
    lines.append(
        '写作要求：用连贯的段落写这些热点，重点表现其关系网络的广度（谁在实施、'
        '对接了什么政策和行动），不必追溯政策沿革；不要使用字段式小标题或表格，'
        '不要写“材料未提供……”之类占位说明。'
        '如果地域不是上海市/静安区，正文第一次出现时必须写清省市全称（如“重庆市”），'
        '不能只写“全市/全区/我市/我区”；引用原文“全市”时补全为“重庆市全市”'
        '或改写为“重庆市”。')
    return '\n'.join(lines)


def _action_focus(service, board: str, from_date: str, to_date: str,
                  min_edges: int, top_n: int, priors: list | None = None) -> dict:
    snapshot = service.snapshot
    window_ids = _window_article_ids(snapshot, from_date, to_date)
    node_articles = defaultdict(set)
    for article in _board_articles(snapshot, board, window_ids):
        for uri in article['nodeUris']:
            node_articles[uri].add(article['articleId'])
    leader_persons = _leader_person_uris(snapshot)
    rows = []
    for uri, article_ids in node_articles.items():
        node = snapshot.nodes.get(uri)
        if not node:
            continue
        leader_detail = _leader_detail(snapshot, node, article_ids, leader_persons)
        leader = bool(leader_detail['snippets'] or leader_detail['names'])
        allowed = node.get('label') in HOTSPOT_LABELS or (
            leader and node.get('label') in LEADER_EXTRA_LABELS)
        if not allowed:
            continue
        signal = _signal(snapshot, uri, window_ids)
        if signal['edgeCount'] < (1 if leader else min_edges):
            continue
        non_target = (not leader
                      and _is_non_target_leader(snapshot, node, article_ids,
                                                leader_persons))
        proximity = _proximity(snapshot, node, article_ids)
        score = (signal['edgeCount'] + len(signal['units']) + len(signal['groups'])
                 + 2 * len(signal['policies']) + 2 * len(signal['actions'])
                 + 2 * len(signal['streets']) + proximity
                 + (LEADER_SCORE_BOOST if leader else 0)
                 - (NON_TARGET_LEADER_PENALTY if non_target else 0))
        rows.append(dict(uri=uri, name=node.get('name', ''),
                         label=node.get('label', ''), score=score,
                         leader=leader, nonTarget=non_target,
                         leaderDetail=leader_detail,
                         location=_detect_location(snapshot, node, article_ids),
                         articleIds=sorted(article_ids), **signal))
    # Cross-week dedupe: stale repeats dropped, progress kept after fresh ones.
    priors = priors or []
    kept_rows = []
    stale_count = 0
    progress_count = 0
    for row in rows:
        status, info = classify_repeat(snapshot.articles, row, priors)
        if status == 'stale':
            stale_count += 1
            continue
        if status == 'progress':
            row['priorReported'] = info
            progress_count += 1
        kept_rows.append(row)
    rows = kept_rows
    rows.sort(key=lambda item: (bool(item.get('priorReported')),
                                -item['score'], -item['edgeCount']))
    selected = []
    for row in _dedupe_hotspots(rows):
        if any(set(row['articleIds']) <= set(kept['articleIds'])
               for kept in selected):
            continue
        selected.append(row)
        if len(selected) >= top_n:
            break
    article_ids = []
    for hotspot in selected:
        article_ids.extend(hotspot['articleIds'])
    items = _focus_items(snapshot, list(dict.fromkeys(article_ids)))
    logger.info('weekly action focus: board=%s candidates=%s selected=%s '
                'min_edges=%s dropped_repeat=%s repeat_with_progress=%s',
                board, len(rows), len(selected), min_edges, stale_count,
                progress_count)
    return dict(type='action', board=board, hotspots=selected,
                items=items, brief=_format_action_brief(selected))


def _reach(snapshot, start_uris, window_ids: set, *, limit: int = 40) -> dict:
    """Two-hop context: nodes reachable via direct neighbors, excluding hubs."""
    adjacency = defaultdict(set)
    for edge in snapshot.edges.values():
        if edge['predicateLabel'] not in REACH_PREDICATES:
            continue
        if not any(article_id in window_ids for article_id in edge['articleIds']):
            continue
        adjacency[edge['subject']].add(edge['object'])
        adjacency[edge['object']].add(edge['subject'])
    starts = {uri for uri in start_uris if uri}
    direct = set()
    for uri in starts:
        direct |= adjacency.get(uri, set())
    second = set()
    for uri in list(direct - starts)[:limit]:
        second |= adjacency.get(uri, set())
    second -= starts | direct
    result = defaultdict(set)
    for uri in list(second)[:limit * 4]:
        node = snapshot.nodes.get(uri, {})
        if node.get('degree', 0) > 200:
            continue
        label = node.get('label', '')
        if label in STREET_ORG_LABELS | STREET_ACTION_LABELS:
            result['streets'].add(node.get('name', ''))
        elif label in POLICY_LABELS:
            result['policies'].add(node.get('name', ''))
        elif label in ACTION_LABELS:
            result['actions'].add(node.get('name', ''))
    return {key: sorted(values)[:5] for key, values in result.items()}


def _chain_proximity(snapshot, chain: dict) -> int:
    texts = [chain['policy']['name']]
    for article_id in chain['policy'].get('articleIds', []):
        article = snapshot.articles.get(article_id)
        if article:
            texts.extend([article.get('title', ''), article.get('publisher', '')])
    for key in ('issuers', 'implementers', 'lineage'):
        for item in chain.get(key, []):
            entity = item['entity']
            texts.extend([entity.get('name', ''), entity.get('region') or ''])
            for article in item['evidence'].get('articles', []):
                texts.extend([article.get('title', ''), article.get('publisher', '')])
    score = 3 if any(hint in ' '.join(texts) for hint in SHANGHAI_HINTS) else 0
    if chain['policy']['label'] in {'市级政策', '区级政策', '街镇级政策'}:
        score += 2
    return score


def _chain_richness(chain: dict) -> int:
    predicates = set()
    for key in ('issuers', 'implementers', 'lineage', 'mentions', 'emphasis'):
        for item in chain.get(key, []):
            predicates.add(item['relation'])
    return (len(chain.get('lineage', [])) + len(chain.get('implementers', []))
            + len(chain.get('issuers', [])) + len(chain.get('emphasis', []))
            + len(predicates) + (1 if chain['policy'].get('prior') else 0))


def _chain_leader_emphasis(snapshot, chain: dict, leader_persons: set) -> bool:
    for item in chain.get('emphasis', []):
        entity = item['entity']
        if entity.get('uri') in leader_persons:
            return True
        article_ids = {article['articleId']
                       for article in item['evidence'].get('articles', [])}
        text = entity.get('name', '') + ' ' + _leader_text(snapshot, article_ids)
        if any(is_target_leader_sentence(sentence, text)
               for sentence in re.split(r'[。；！？\n]', text)):
            return True
    return False


def _merge_policy_focuses(ranked: list) -> list:
    groups = {}
    for row in ranked:
        action_impls = _action_implementers(row['chain'])
        if action_impls:
            key = ('action', action_impls[0]['entity']['uri'])
        else:
            key = ('policy', _norm(row['chain']['policy']['name']))
        groups.setdefault(key, []).append(row)
    focuses = []
    for key, rows in groups.items():
        rows.sort(key=lambda item: item['score'], reverse=True)
        first = rows[0]
        chains = [item['chain'] for item in rows]
        article_ids = set()
        for chain in chains:
            article_ids.update(chain['policy'].get('articleIds') or [])
            for item in chain.get('implementers', []):
                for article in item['evidence'].get('articles', []):
                    article_ids.add(article['articleId'])
        leader = any(item.get('leader') for item in rows)
        if key[0] == 'action':
            action = _action_implementers(chains[0])[0]['entity']
            focuses.append(dict(
                type='action', title=action['name'], label=action['label'],
                uri=action['uri'], policies=[chain['policy'] for chain in chains],
                chains=chains, proximity=first['proximity'], leader=leader,
                richness=max(item['richness'] for item in rows),
                reach=first['reach'], score=first['score'] + 4,
                articleIds=sorted(article_ids)))
        else:
            policy = chains[0]['policy']
            focuses.append(dict(
                type='policy', title=policy['name'], label=policy['label'],
                uri=(policy.get('uris') or [''])[0], policies=[policy],
                chains=[chains[0]], proximity=first['proximity'], leader=leader,
                richness=first['richness'], reach=first['reach'],
                score=first['score'], articleIds=sorted(article_ids)))
    focuses.sort(key=lambda item: item['score'], reverse=True)
    return focuses


def _format_policy_brief(focuses: list) -> str:
    lines = ['【图谱选出的本板块政策链（必须围绕以下内容写，不要另选）】']
    for index, focus in enumerate(focuses, 1):
        if focus['type'] == 'action':
            policies = '、'.join(f"《{_display_name(p['name'])}》"
                                 for p in focus['policies'])
            lines.append(f"{index}. 行动“{focus['title']}”（{focus['label']}）："
                         f"落实/响应 {len(focus['policies'])} 项政策。")
            lines.append(f"   对应政策：{policies}")
        else:
            policy = focus['policies'][0]
            lines.append(f"{index}. 政策《{_display_name(policy['name'])}》"
                         f"（{policy['label']}）")
            chain = focus['chains'][0]
            if chain['lineage']:
                lineage = '、'.join(f"《{_display_name(item['entity']['name'])}》"
                                    for item in chain['lineage'][:4])
                lines.append(f"   上位/依据政策：{lineage}")
            if policy.get('prior'):
                prior = policy['prior'][0]
                lines.append(f"   此前相关政策（{prior['weekEnd']}）：{prior['topic']}")
            actions = _action_implementers(chain)
            if actions:
                lines.append('   已落实的街道/行动：' +
                             '、'.join(_display_name(item['entity']['name'])
                                       for item in actions[:4]))
        if focus.get('leader'):
            lines.append('   权重：上海市/静安区领导讲话/强调类信息，权重最高。')
        emphases = []
        for chain in focus.get('chains', []):
            for item in chain.get('emphasis', [])[:2]:
                emphases.append(_display_name(item['entity']['name']))
        if emphases:
            lines.append('   领导讲话/强调：' + '、'.join(dict.fromkeys(emphases)))
        reach = focus.get('reach') or {}
        if reach.get('streets'):
            lines.append('   二跳可及街道：' + '、'.join(reach['streets']))
        if reach.get('policies'):
            lines.append('   二跳相关政策：' + '、'.join(reach['policies']))
        if reach.get('actions'):
            lines.append('   二跳相关行动：' + '、'.join(reach['actions']))
    lines.append(
        '写作要求：用连贯的段落讲清政策脉络和落实链条，尤其是上位/此前政策、'
        '哪些街道或单位已经在落实、以及二跳三跳关系；不要使用字段式小标题或表格，'
        '不要写“材料未提供……”之类占位说明。')
    return '\n'.join(lines)


def _policy_focus(services, relation_dossier: dict, board: str,
                  from_date: str, to_date: str, top_n: int) -> dict:
    service = _service_for(services, board)
    snapshot = service.snapshot
    window_ids = _window_article_ids(snapshot, from_date, to_date)
    leader_persons = _leader_person_uris(snapshot)
    ranked = []
    matching_chains = 0
    for chain in relation_dossier.get('chains', []):
        if board not in chain['boards'] and board not in chain['policy']['boards']:
            continue
        matching_chains += 1
        if chain.get('libraryId') and chain['libraryId'] != snapshot.library_id:
            continue
        proximity = _chain_proximity(snapshot, chain)
        if proximity < 2:
            continue
        leader = _chain_leader_emphasis(snapshot, chain, leader_persons)
        # Issuer-only chains are not a policy story.
        if not (chain['lineage'] or _action_implementers(chain)
                or chain['policy'].get('prior') or leader):
            continue
        richness = _chain_richness(chain)
        if richness < 2:
            continue
        reach = _reach(snapshot, chain['policy'].get('uris', []), window_ids)
        score = (3 * proximity + 2 * richness + 2 * len(reach.get('streets', []))
                 + len(reach.get('policies', [])) + len(reach.get('actions', []))
                 + (LEADER_SCORE_BOOST if leader else 0))
        ranked.append(dict(chain=chain, proximity=proximity, richness=richness,
                           leader=leader, reach=reach, score=score))
    ranked.sort(key=lambda item: item['score'], reverse=True)
    focuses = _merge_policy_focuses(ranked)[:top_n]
    article_ids = []
    for focus in focuses:
        if focus['type'] == 'action':
            focus['reach'] = _reach(snapshot, [focus['uri']], window_ids)
        article_ids.extend(focus['articleIds'])
    items = _focus_items(snapshot, list(dict.fromkeys(article_ids)))
    logger.info('weekly policy focus: board=%s matching_chains=%s '
                'qualified=%s selected=%s', board, matching_chains,
                len(ranked), len(focuses))
    return dict(type='policy', board=board, focuses=focuses,
                items=items, brief=_format_policy_brief(focuses))


def select_weekly_focus(services, *, from_date: str, to_date: str,
                        boards=None, priors: list | None = None,
                        relation_dossier: dict | None = None,
                        action_min_edges: int = ACTION_MIN_EDGES,
                        top_n: int = FOCUS_TOP_N) -> dict:
    """Return ``{board: focus}`` for boards that have a qualifying focus."""
    if not isinstance(services, dict):
        services = {'policy': services}
    boards = boards or ['基层党建', '经济动态', '社区治理', '数字社区', '政策速递']
    relation_dossier = relation_dossier or build_relation_dossier(
        services, from_date=from_date, to_date=to_date, priors=priors,
        max_chains=60)
    result = {}
    for board in boards:
        if board in ACTION_BOARDS:
            focus = _action_focus(_service_for(services, board), board,
                                  from_date, to_date, action_min_edges, top_n,
                                  priors=priors)
        elif board in POLICY_BOARDS:
            focus = _policy_focus(services, relation_dossier, board,
                                  from_date, to_date, top_n)
            if board == '经济动态' and not focus.get('items'):
                focus = _economy_focus(_service_for(services, board), board,
                                       from_date, to_date, top_n)
        else:
            continue
        if focus.get('items'):
            result[board] = focus
        else:
            logger.info('weekly board omitted: board=%s reason=no qualifying '
                        'focus', board)
    return result
