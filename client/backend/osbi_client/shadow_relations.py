"""Deterministic policy/action relation context for the shadow weekly.

Ported from midplatform/workflows/ontology_relations.py. Builds evidence-
bearing policy chains: 落实/响应/实施主体 direct implementers, 依据 lineage,
发布 issuers, 提及 leads, 强调 emphasis, plus prior-policy matching (0.62).
Only direct edges may be described as implementation; co-occurrence is a lead.

Adaptation: prior policies come from the client weekly coverage ledger
(pre-loaded by the caller) instead of the production weekly_events.db.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import re

from .leader_rules import is_non_target_leader, is_target_jurisdiction
from .shadow_snapshot import is_policy_node

POLICY_LABELS = {'政策文件', '国家级政策', '省级政策', '市级政策', '区级政策',
                 '街镇级政策', '政策征求意见稿'}
ACTION_LABELS = {'行动事项', '市级行动', '区级行动', '街镇行动', '社区行动',
                 '事件', '会议论坛', '调研视察', '居民活动'}
ORG_LABELS = {'机构组织', '政府机构（含党组织）', '国家级机构', '省级机构',
              '市级机构', '区级机构', '街镇机构', '社区自治组织', '社会组织',
              '企业', '金融机构', '交易场所'}
REGION_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构'}
STREET_LABELS = {'街镇机构', '社区自治组织', '社会组织', '街镇行动', '社区行动',
                 '居民活动'}
ACTION_ACTOR_LABELS = STREET_LABELS | ACTION_LABELS
DIRECT_IMPL_PREDICATES = {'落实', '响应', '实施主体'}
LINEAGE_PREDICATE = '依据'
ISSUE_PREDICATE = '发布'
MENTION_PREDICATE = '提及'
EMPHASIS_PREDICATE = '强调'
LEADER_LIKE_LABELS = {'调研视察'}
RELEVANT_BOARDS = {'社区治理', '基层党建', '政策速递', '数字社区'}
SHANGHAI_HINTS = ('上海', '静安', '黄浦', '长宁', '虹口', '杨浦', '普陀',
                  '徐汇', '浦东', '闵行', '宝山', '嘉定', '松江', '青浦',
                  '奉贤', '金山', '崇明')
PRIOR_SIMILARITY_THRESHOLD = 0.62
MAX_PRIORS_PER_POLICY = 2
NOISE_IMPLEMENTERS = {'党中央', '国务院', '中共中央', '上海市', '上海市人民政府',
                      '中共上海市委', '中共上海市委办公厅', '上海市人民政府办公厅'}
GENERIC_POLICY_TERMS = (
    '上海市人民政府办公厅', '上海市人民政府', '中共上海市委办公厅', '中共上海市委',
    '上海市', '关于', '的通知', '通知', '若干', '规定', '办法', '意见', '实施方案',
    '行动计划', '规划', '措施', '试行', '修订', '草案', '征求意见稿', '十五五',
    '十四五', '发展', '建设', '管理', '工作', '推动', '加强', '进一步', '持续',
    '实施', '促进', '完善', '提升', '支持', '加快', '深化', '全面', '高质量',
    '若干重大问题的决定', '决定',
)


def _norm(value: str) -> str:
    return re.sub(r'[\s\W_《》]+', '', value or '')


def _display_name(value: str) -> str:
    return re.sub(r'[《》]', '', value or '').strip()


def _policy_core(value: str) -> str:
    text = _norm(value)
    for term in GENERIC_POLICY_TERMS:
        text = text.replace(_norm(term), '')
    return text


def _bigrams(value: str) -> set:
    text = _norm(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _similarity(left: str, right: str) -> float:
    """Character-level similarity that works without a tokenizer."""
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    containment = (min(len(a), len(b)) / max(len(a), len(b))
                   if a in b or b in a else 0.0)
    grams_a, grams_b = _bigrams(a), _bigrams(b)
    jaccard = (len(grams_a & grams_b) / len(grams_a | grams_b)
               if grams_a | grams_b else 0.0)
    return max(ratio, containment * 0.95, jaccard * 0.9)


def _same_policy(left: str, right: str) -> bool:
    a, b = _norm(left), _norm(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 8 and len(b) >= 8 and (a in b or b in a) and abs(len(a) - len(b)) <= 24:
        return True
    return _similarity(a, b) >= 0.94


def _evidence(snapshot, article_id: str) -> dict | None:
    article = snapshot.articles.get(article_id)
    if not article:
        return None
    return dict(articleId=article['articleId'], url=article['url'],
                title=article['title'], publisher=article['publisher'],
                publishedDate=article['publishedDate'],
                boards=list(article['boards']))


def _edge_evidence(snapshot, edge: dict, window_ids: set) -> list:
    result = []
    for article_id in edge['articleIds']:
        if article_id not in window_ids:
            continue
        item = _evidence(snapshot, article_id)
        if item and item not in result:
            result.append(item)
    return result


def _edge_date(snapshot, edge: dict, window_ids: set) -> str:
    dates = sorted({item['publishedDate'] for item in _edge_evidence(snapshot, edge, window_ids)
                    if item.get('publishedDate')})
    return dates[-1] if dates else ''


def _window_article_ids(snapshot, from_date: str, to_date: str) -> set:
    return {article['articleId'] for article in snapshot.articles.values()
            if article.get('publishedDate')
            and (not from_date or article['publishedDate'] >= from_date)
            and (not to_date or article['publishedDate'] <= to_date)}


def _region_chain(snapshot, uri: str) -> str:
    """Administrative region situating an actor (parent edge, else own name)."""
    node = snapshot.nodes.get(uri)
    label = node.get('label', '') if node else ''
    if label in REGION_LABELS - {'街镇机构'}:
        return node.get('name', '')
    for edge in snapshot.edges.values():
        if edge['subject'] != uri or edge['predicateLabel'] not in ('位于', '隶属'):
            continue
        target = snapshot.nodes.get(edge['object'])
        if target and target.get('label') in REGION_LABELS:
            return target.get('name', '')
    if label == '街镇机构':
        return node.get('name', '')
    return ''


def _relation_actor(snapshot, uri: str) -> dict:
    node = snapshot.nodes.get(uri, {})
    return dict(uri=uri, name=node.get('name') or node.get('canonicalName') or uri,
                label=node.get('label', ''),
                degree=node.get('degree', 0),
                region=_region_chain(snapshot, uri))


def _policy_groups(snapshot, window_ids: set) -> list:
    window_edge_uris = set()
    for edge in snapshot.edges.values():
        if not any(article_id in window_ids for article_id in edge['articleIds']):
            continue
        window_edge_uris.add(edge['subject'])
        window_edge_uris.add(edge['object'])
    candidates = []
    for node in snapshot.nodes.values():
        if not is_policy_node(node):
            continue
        in_window = bool(set(node['articleIds']) & window_ids) or node['uri'] in window_edge_uris
        if in_window:
            candidates.append(node)
    groups = []
    for node in sorted(candidates, key=lambda item: (len(item.get('name', '')),
                                                      item.get('degree', 0)), reverse=True):
        target = None
        for group in groups:
            if _same_policy(group['name'], node.get('name', '')):
                target = group
                break
        if target is None:
            groups.append(dict(name=node.get('name', ''), label=node.get('label', ''),
                               uris={node['uri']}, aliases=[], boards=set(),
                               dates=set(), articleIds=set()))
            target = groups[-1]
        target['uris'].add(node['uri'])
        if node.get('name') and node['name'] != target['name']:
            target['aliases'].append(node['name'])
        if node.get('label') in POLICY_LABELS and node.get('label'):
            target['label'] = node.get('label')
        for article_id in node['articleIds']:
            article = snapshot.articles.get(article_id)
            if not article:
                continue
            if article_id in window_ids and article.get('publishedDate'):
                target['dates'].add(article['publishedDate'])
            if article_id in window_ids:
                target['boards'].update(article['boards'])
                target['articleIds'].add(article_id)
    for group in groups:
        group['uris'] = set(group['uris'])
        group['boards'] = set(group['boards'])
        group['dates'] = sorted(group['dates'])
        group['articleIds'] = set(group['articleIds'])
    return groups


def _relation_evidence(snapshot, edge: dict, window_ids: set) -> dict:
    evidence = _edge_evidence(snapshot, edge, window_ids)
    boards = sorted({board for item in evidence for board in item['boards']})
    return dict(date=_edge_date(snapshot, edge, window_ids), boards=boards,
                articles=evidence[:3])


def _classify_edges(snapshot, uris: set, window_ids: set) -> dict:
    result = dict(lineage=[], implementers=[], issuers=[], mentions=[], emphasis=[])
    for edge in snapshot.edges.values():
        predicate = edge['predicateLabel']
        if predicate not in (DIRECT_IMPL_PREDICATES | {LINEAGE_PREDICATE,
                                                       ISSUE_PREDICATE,
                                                       MENTION_PREDICATE,
                                                       EMPHASIS_PREDICATE}):
            continue
        if not ({edge['subject'], edge['object']} & uris):
            continue
        evidence = _relation_evidence(snapshot, edge, window_ids)
        if not evidence['date']:
            continue
        subject_in = edge['subject'] in uris
        other_uri = edge['object'] if subject_in else edge['subject']
        other = _relation_actor(snapshot, other_uri)
        item = dict(relation=predicate, direction='out' if subject_in else 'in',
                    entity=other, evidence=evidence)
        if predicate == LINEAGE_PREDICATE:
            # 依据 is child->upper; a reverse edge is not lineage.
            if subject_in and other['label'] in POLICY_LABELS:
                result['lineage'].append(item)
        elif predicate in DIRECT_IMPL_PREDICATES:
            result['implementers'].append(item)
        elif predicate == ISSUE_PREDICATE:
            if other['label'] in ORG_LABELS or other['label'] in {'会议论坛'}:
                result['issuers'].append(item)
        elif predicate == MENTION_PREDICATE:
            result['mentions'].append(item)
        elif predicate == EMPHASIS_PREDICATE:
            result['emphasis'].append(item)
    for key, items in result.items():
        deduped = {}
        for item in items:
            dedupe_key = (item['relation'], item['entity']['uri'])
            previous = deduped.get(dedupe_key)
            if previous is None or item['evidence']['date'] > previous['evidence']['date']:
                deduped[dedupe_key] = item
        result[key] = list(deduped.values())
    return result


def match_prior_policies(policy_name: str, priors: list) -> list:
    core = _policy_core(policy_name)
    if len(core) < 4:
        return []
    scored = []
    for prior in priors:
        if len(_policy_core(prior['topic'])) < 4:
            continue
        score = _similarity(core, _policy_core(prior['topic']))
        if score >= PRIOR_SIMILARITY_THRESHOLD:
            scored.append(dict(prior, score=round(score, 3)))
    scored.sort(key=lambda item: (item['score'], item['weekEnd']), reverse=True)
    return scored[:MAX_PRIORS_PER_POLICY]


def _is_noise_implementer(policy_name: str, policy_label: str, item: dict,
                          issuers: list) -> bool:
    """Drop preamble noise such as “上海市 落实 上海市某方案”."""
    entity = item['entity']
    name = entity.get('name', '')
    if not name:
        return True
    if any(name == issuer['entity'].get('name') for issuer in issuers):
        return True
    if entity.get('label') in REGION_LABELS and _norm(name) and _norm(name) in _norm(policy_name):
        return True
    if (name in NOISE_IMPLEMENTERS
            and policy_label in {'市级政策', '区级政策', '街镇级政策'}):
        return True
    return False


def _score_chain(policy: dict, classified: dict) -> float:
    implementers = classified['implementers']
    street = [item for item in implementers if item['entity']['label'] in STREET_LABELS]
    actions = [item for item in implementers if item['entity']['label'] in ACTION_LABELS]
    shanghai = [item for item in implementers
                if any(hint in (item['entity'].get('region') or '')
                       or hint in item['entity'].get('name', '')
                       for hint in SHANGHAI_HINTS)]
    score = 6.0 * min(len(street), 2)
    score += 3.0 * min(len(actions), 2)
    score += 3.0 * min(len(shanghai), 2)
    score += 3.0 if classified['lineage'] else 0.0
    score += 2.0 if classified['emphasis'] else 0.0
    score += 2.0 if policy.get('prior') else 0.0
    score += 2.0 if RELEVANT_BOARDS & set(policy['boards']) else 0.0
    score += 2.0 if policy['label'] in {'市级政策', '区级政策', '街镇级政策'} else 0.0
    score += min(len(implementers), 3)
    score += min(len(policy['dates']), 2) * 0.5
    return score


def build_relation_dossier(services, *, from_date: str, to_date: str,
                           priors: list | None = None,
                           max_chains: int = 24) -> dict:
    """Build evidence-bearing policy chains for one weekly window."""
    service_list = ([services] if not isinstance(services, dict)
                    else list(services.values()))
    priors = priors or []
    chains = []
    for service in service_list:
        window_ids = _window_article_ids(service.snapshot, from_date, to_date)
        if not window_ids:
            continue
        for group in _policy_groups(service.snapshot, window_ids):
            classified = _classify_edges(service.snapshot, group['uris'], window_ids)
            classified['lineage'] = [
                item for item in classified['lineage']
                if not _same_policy(group['name'], item['entity']['name'])]
            classified['implementers'] = [
                item for item in classified['implementers']
                if not _is_noise_implementer(group['name'], group['label'], item,
                                             classified['issuers'])]
            if not (classified['implementers'] or classified['lineage']
                    or classified['mentions'] or classified['issuers']
                    or classified['emphasis']):
                continue
            policy = dict(name=group['name'], label=group['label'],
                          aliases=group['aliases'], dates=group['dates'],
                          boards=sorted(group['boards']),
                          uris=sorted(group['uris']),
                          articleIds=sorted(group['articleIds']),
                          prior=match_prior_policies(group['name'], priors))
            boards = set(group['boards'])
            for key in ('implementers', 'lineage', 'issuers', 'mentions', 'emphasis'):
                for item in classified[key]:
                    boards.update(item['evidence']['boards'])
            chain = dict(policy=policy, boards=sorted(boards),
                         libraryId=service.snapshot.library_id,
                         issuers=classified['issuers'],
                         lineage=classified['lineage'],
                         implementers=classified['implementers'],
                         mentions=classified['mentions'],
                         emphasis=classified['emphasis'])
            chain['score'] = _score_chain(policy, classified)
            chains.append(chain)
    chains.sort(key=lambda item: (item['score'], item['policy']['dates']),
                reverse=True)
    return dict(fromDate=from_date, toDate=to_date,
                chains=chains[:max_chains],
                priorPool=len(priors),
                snapshots={lib: service.snapshot.stats
                           for lib, service in (
                               services.items() if isinstance(services, dict)
                               else [('policy', services)])})


def _format_entity(item: dict) -> str:
    entity = item['entity']
    region = f"，{entity['region']}" if entity.get('region') else ''
    return f"{_display_name(entity['name'])}（{entity['label'] or '未分类'}{region}）"


def _leader_text(item: dict) -> str:
    text = item['entity'].get('name', '')
    for article in item.get('evidence', {}).get('articles', []):
        text += ' ' + article.get('title', '') + ' ' + article.get('publisher', '')
    return text


def _is_non_target_leader_item(item: dict) -> bool:
    if item['entity'].get('label') not in LEADER_LIKE_LABELS:
        return False
    return is_non_target_leader(_leader_text(item))


def _is_target_leader_entity_item(item: dict) -> bool:
    label = item['entity'].get('label')
    if label not in {'调研视察', '人物', '领导干部'}:
        return False
    if label == '调研视察':
        return not _is_non_target_leader_item(item)
    return is_target_jurisdiction(_leader_text(item))


def _action_implementers(chain: dict) -> list:
    return [item for item in chain.get('implementers', [])
            if item['entity'].get('label') in ACTION_ACTOR_LABELS
            and not _is_non_target_leader_item(item)]


def _render_chain(index: int, chain: dict, board: str | None) -> list:
    policy = chain['policy']
    dates = '、'.join(policy['dates']) or '无日期'
    lines = [f"{index}. 《{_display_name(policy['name'])}》"
             f"（{policy['label']}；材料日期 {dates}）"]
    if policy.get('prior'):
        prior = policy['prior'][0]
        lines.append(f"   此前相关政策（{prior['weekEnd']}）：{prior['topic']}")
    if chain['lineage']:
        for item in chain['lineage'][:3]:
            lines.append(f"   上位/依据政策：{_format_entity(item)}"
                         f"（{item['evidence']['date']}）")
    else:
        lines.append("   上位/依据政策：图谱未提供"
                     "（本条政策本身不是它的上位政策）")
    emphasis = [item for item in chain.get('emphasis', [])
                if _is_target_leader_entity_item(item)]
    for item in emphasis[:2]:
        lines.append(f"   领导讲话强调：{_format_entity(item)}")
    for item in chain['issuers'][:3]:
        lines.append(f"   发布机关：{_format_entity(item)}")
    action_impls = [item for item in _action_implementers(chain)
                    if not _is_non_target_leader_item(item)]
    org_impls = [item for item in chain['implementers']
                 if item not in action_impls and not _is_non_target_leader_item(item)]
    for item in action_impls[:4]:
        lines.append(f"   落实/响应（直接边，街道/行动）：{_format_entity(item)}"
                     f"（材料日期 {item['evidence']['date']}）")
    if not board:
        for item in org_impls[:2]:
            lines.append(f"   机构层面实施（非街道行动）：{_format_entity(item)}"
                         f"（材料日期 {item['evidence']['date']}）")
    mentions = [item for item in chain['mentions']
                if (item['entity'].get('label') not in {'人物', '领导干部', '调研视察'}
                    or _is_target_leader_entity_item(item))]
    for item in mentions[:2]:
        lines.append(f"   同篇提及（非落实）：{_format_entity(item)}")
    return lines


def format_relation_brief(dossier: dict, *, board: str | None = None,
                          max_chains: int = 8) -> str:
    """Compact prompt block; empty string when the window has no relations."""
    chains = dossier.get('chains') or []
    if board:
        chains = [chain for chain in chains
                  if (board in chain['boards'] or board in chain['policy']['boards'])
                  and (_action_implementers(chain) or chain['lineage'])]
    chains = chains[:max_chains]
    if not chains:
        return ''
    lines = ['【图谱关系线索】',
             '说明：标注“落实/响应（直接边）”的是图直接边，可作为落实判断；'
             '“同篇提及”只是线索，不能写成已落实。日期为材料日期，不是政策发布日期。'
             '不同政策的关系不得混用：每条热点只写该政策自己的上位/此前政策。']
    direct = [chain for chain in chains if (board and _action_implementers(chain))
              or (not board and chain['implementers'])]
    if board and direct:
        preferred = []
        for chain in direct[:2]:
            for item in _action_implementers(chain)[:1]:
                preferred.append(f"{_format_entity(item)}落实《"
                                 f"{_display_name(chain['policy']['name'])}》")
        if preferred:
            lines.append('【本方向必须围绕的直接链路】' + '；'.join(preferred))
    for index, chain in enumerate(chains, 1):
        lines.extend(_render_chain(index, chain, board))
    return '\n'.join(lines)


def format_relation_markdown(dossier: dict, *, max_chains: int = 6) -> str:
    """Deterministic human-readable fallback for the 关联解读 section."""
    chains = (dossier.get('chains') or [])[:max_chains]
    if not chains:
        return ''
    lines = []
    for chain in chains:
        policy = chain['policy']
        dates = '、'.join(policy['dates']) or '日期未知'
        head = f"- **《{_display_name(policy['name'])}》**（{policy['label']}，{dates}）"
        impls = [item for item in (_action_implementers(chain)
                                   or chain['implementers'])
                 if not _is_non_target_leader_item(item)]
        actions = '、'.join(_format_entity(item) for item in impls[:3])
        lineage = '、'.join(f"《{_display_name(item['entity']['name'])}》"
                            for item in chain['lineage'][:3])
        extras = []
        if actions:
            extras.append(f"落实/响应：{actions}")
        if lineage:
            extras.append(f"上位依据：{lineage}")
        if policy.get('prior'):
            prior = policy['prior'][0]
            extras.append(f"此前政策：{prior['topic']}（{prior['weekEnd']}）")
        lines.append(head + ('；' + '；'.join(extras) if extras else ''))
    return '\n'.join(lines)


def configure(*, territory_hints=None, relevantBoards=None,
              noiseImplementers=None, genericPolicyTerms=None):
    """Override node territory hints used by policy/action relevance scoring."""
    global SHANGHAI_HINTS, RELEVANT_BOARDS, NOISE_IMPLEMENTERS, GENERIC_POLICY_TERMS
    if territory_hints:
        SHANGHAI_HINTS = tuple(str(value) for value in territory_hints)
    if relevantBoards:
        RELEVANT_BOARDS = set(relevantBoards)
    if noiseImplementers:
        NOISE_IMPLEMENTERS = set(noiseImplementers)
    if genericPolicyTerms:
        GENERIC_POLICY_TERMS = tuple(str(value) for value in genericPolicyTerms)
