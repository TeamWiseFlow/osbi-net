"""Graph-first candidate builders for the shadow topnews / weekly pipelines.

Ported from midplatform/workflows/ontology_candidates.py. Turns evidence-built
snapshots into evidence-bearing candidate lists: title dedupe -> event dedupe
-> leader locality hard filter -> quality gate -> full scoring -> per-board
candidate pool. Constants and thresholds mirror production exactly.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from difflib import SequenceMatcher
import re

from .leader_rules import (KNOWN_NATIONAL_LEADERS, is_national_leader,
                           is_non_target_leader, is_target_leader,
                           locality_tier, target_leader_snippet)
from .publisher_penalty import publisher_key
from .shadow_snapshot import (ShadowSnapshot, SnapshotService, article_view,
                              dedupe_articles, is_policy_node,
                              strip_publish_date_suffix)

SECTIONS = ['基层党建', '经济动态', '社区治理', '数字社区', '政策速递']
BOARD_LIBRARY = {'经济动态': 'economy'}
DEFAULT_LIBRARIES = ('policy', 'economy')
POLICY_LABELS = {'政策文件', '国家级政策', '省级政策', '市级政策', '区级政策',
                 '街镇级政策', '政策征求意见稿'}
ACTION_LABELS = {'行动事项', '市级行动', '区级行动', '街镇行动', '社区行动',
                 '事件', '会议论坛', '调研视察'}
LEADER_LABELS = {'调研视察', '领导干部'}
GOV_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构',
              '政府机构（含党组织）'}
LEADER_SCORE_BOOST = 500
NON_TARGET_LEADER_PENALTY = 300
LEADER_POST = re.compile(r'(中国|中共|国务院|中央|省|市|区|县|镇|街道|党委|政府|部|局|委|厅|办|署)')
LEADER_ORG_EXCLUDE = re.compile(r'(学院|党校|社科院|科学院|研究院|研究所|大学|学校|中心|医院|日报|电视台)')
REGION_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构'}
# commentary alone is not fatal; only commentary + low_density hard-excludes.
DEFAULT_EXCLUDE_QUALITY = ['zero_relation', 'report']
COMMENTARY_HARD_EXCLUDE = {'commentary', 'low_density'}
EVENT_LABELS = {'会议论坛', '事件', '调研视察', '行动事项', '市级行动',
                '区级行动', '街镇行动', '社区行动', '居民活动'}
GENERIC_EVENT_NAMES = {'会议', '活动', '工作', '调研', '考察', '座谈会',
                       '推进会', '发布会', '启动仪式', '揭牌仪式', '论坛',
                       '大会', '培训', '竞赛', '评选', '总结', '部署'}
GENERIC_ENTITY_NAMES = {'上海市', '静安区', '中国', '国家', '政府', '社区',
                        '街道', '居民', '基层治理', '社会治理', '基层党建',
                        '社区治理', '经济动态', '数字社区', '政策速递',
                        '高质量发展', '新质生产力', '工作', '建设', '发展',
                        '服务', '管理', '治理', '通知', '意见', '方案',
                        '办法', '规划', '政策', '活动', '会议'}
NATIONAL_EVENT_MARKERS = ('全国', '中国', '国家', '中央', '国务院',
                          '中共中央', '党的二十届', '全会', '两会',
                          '十五五', '十四五')
LOCALITY_SCORE = {'target': 120, 'shanghai': 60, 'other_district': 10,
                  'other_province': -40, 'national': 0, 'unknown': 0}


def _service_for(services, board: str) -> SnapshotService:
    if isinstance(services, SnapshotService):
        return services
    library = BOARD_LIBRARY.get(board, 'policy')
    if library in services:
        return services[library]
    return next(iter(services.values()))


def _all_services(services) -> list:
    return [services] if isinstance(services, SnapshotService) else list(services.values())


def _item_raw_text(snapshot: ShadowSnapshot, item: dict, limit: int = 1200) -> str:
    for block_id in item.get('blockIds', []):
        raw = snapshot.blocks.get(block_id, {}).get('rawText', '')
        if raw:
            return raw[:limit]
    return ''


def _leader_text(item: dict) -> str:
    entities = item.get('keyEntities', [])
    return ' '.join([item.get('title', ''), item.get('publisher', ''),
                     item.get('leaderText', '')]
                    + [e.get('name', '') for e in entities])


def _leader_snippet(item: dict, limit: int = 160) -> str:
    return target_leader_snippet(item.get('leaderText', ''), limit)


def _is_leader_item(item: dict) -> bool:
    """Shanghai city / Jing'an district leader visit or speech signal."""
    return is_target_leader(_leader_text(item))


def _is_non_target_leader_item(item: dict) -> bool:
    """Leader-like item from another province/district or unclear jurisdiction.

    National / central leader activity is neutral: neither boosted nor excluded.
    """
    text = _leader_text(item)
    if is_target_leader(text) or is_national_leader(text):
        return False
    if any(e.get('label') == '调研视察' for e in item.get('keyEntities', [])):
        return True
    return is_non_target_leader(text)


def _quality_excluded(item: dict, excluded: set) -> bool:
    flags = set(item.get('qualityFlags', []))
    if flags & excluded:
        return True
    return COMMENTARY_HARD_EXCLUDE <= flags


def _norm_dedupe_text(value: str) -> str:
    value = strip_publish_date_suffix(str(value or ''))
    return re.sub(r'[^一-鿿A-Za-z0-9]+', '', value)


def _event_names(item: dict) -> set:
    names = set()
    for entity in item.get('keyEntities', []):
        if entity.get('label') not in EVENT_LABELS:
            continue
        name = _norm_dedupe_text(entity.get('name'))
        if len(name) >= 5 and name not in GENERIC_EVENT_NAMES:
            names.add(name)
    return names


def _specific_entities(item: dict) -> set:
    names = set()
    for entity in item.get('keyEntities', []):
        name = _norm_dedupe_text(entity.get('name'))
        if len(name) >= 4 and name not in GENERIC_ENTITY_NAMES:
            names.add(name)
    for relation in item.get('relations', []):
        for key in ('subjectText', 'objectText'):
            name = _norm_dedupe_text(relation.get(key))
            if len(name) >= 4 and name not in GENERIC_ENTITY_NAMES:
                names.add(name)
    return names


def _title_ratio(a: dict, b: dict) -> float:
    left = _norm_dedupe_text(a.get('title'))
    right = _norm_dedupe_text(b.get('title'))
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio() * 100


def _is_national_event(name: str) -> bool:
    return any(marker in name for marker in NATIONAL_EVENT_MARKERS)


def _same_event(a: dict, b: dict, title_threshold: int = 90) -> bool:
    if a.get('publishedDate') != b.get('publishedDate'):
        return False
    if not set(a.get('boards', [])) & set(b.get('boards', [])):
        return False
    shared_events = {name for name in _event_names(a) & _event_names(b)
                     if not _is_national_event(name)}
    if shared_events:
        return True
    ratio = _title_ratio(a, b)
    if ratio >= title_threshold:
        return True
    shared = _specific_entities(a) & _specific_entities(b)
    return len(shared) >= 3 and ratio >= 55


def dedupe_events(items: list, title_threshold: int = 90) -> list:
    """Merge cross-publisher stories that describe the same graph event."""
    groups = []
    for item in items:
        placed = False
        for group in groups:
            representative = group['representative']
            if not _same_event(representative, item, title_threshold):
                continue
            if (item.get('relationCount', 0), item.get('entityCount', 0)) > (
                    representative.get('relationCount', 0),
                    representative.get('entityCount', 0)):
                group['duplicates'].append(representative)
                group['representative'] = item
            else:
                group['duplicates'].append(item)
            placed = True
            break
        if not placed:
            groups.append(dict(representative=item, duplicates=[]))
    result = []
    for group in groups:
        representative = dict(group['representative'])
        duplicates = group['duplicates']
        if duplicates:
            representative['eventDuplicateCount'] = len(duplicates)
            representative['eventDuplicatePublishers'] = sorted({
                item.get('publisher', '') for item in duplicates
                if item.get('publisher')})
            urls = list(representative.get('duplicateUrls', []))
            for item in duplicates:
                url = item.get('url', '')
                if url and url not in urls:
                    urls.append(url)
            representative['duplicateCount'] = (
                representative.get('duplicateCount', 0) + len(duplicates))
            representative['duplicateUrls'] = urls
        result.append(representative)
    return result


def _locality(item: dict) -> str:
    return locality_tier(item.get('leaderText', ''),
                         item.get('publisher', ''))


def _score(item: dict, publisher_penalties: dict | None = None) -> float:
    score = item.get('relationCount', 0) * 2 + item.get('entityCount', 0) * 0.2
    score += LOCALITY_SCORE.get(_locality(item), 0)
    key = item.get('publisherKey') or ''
    score += float((publisher_penalties or {}).get(key, 0))
    if _is_leader_item(item):
        score += LEADER_SCORE_BOOST
    elif _is_non_target_leader_item(item):
        score -= NON_TARGET_LEADER_PENALTY
    flags = set(item.get('qualityFlags', []))
    if 'low_density' in flags:
        score -= 2
    if 'digest' in flags:
        score -= 3
    if 'no_date' in flags:
        score -= 1
    types = set(item.get('contentTypes', []))
    if 'policy' in types:
        score += 3
    if 'activity' in types:
        score += 1
    for entity in item.get('keyEntities', []):
        if entity.get('label') in POLICY_LABELS:
            score += 2
        elif entity.get('label') in ACTION_LABELS:
            score += 1
    return score


def topnews_candidates(services, *, from_date: str, to_date: str,
                       boards=None, per_board: int = 3,
                       exclude_quality=None, dedupe_threshold: int = 90,
                       publisher_penalties: dict | None = None) -> dict:
    boards = boards or SECTIONS
    exclude_quality = DEFAULT_EXCLUDE_QUALITY if exclude_quality is None else exclude_quality
    result = {}
    candidate_publishers = set()
    for board in boards:
        service = _service_for(services, board)
        raw = service.list_articles(
            from_date=from_date, to_date=to_date, boards=[board],
            exclude_quality=[], limit=80, with_relations=True)
        items = dedupe_articles(raw['items'], threshold=dedupe_threshold)
        items = dedupe_events(items)
        for item in items:
            item['leaderText'] = _item_raw_text(service.snapshot, item)
            item['publisherKey'] = publisher_key(
                item.get('url', ''), item.get('publisher', ''),
                item.get('leaderText', ''))
            candidate_publishers.add(item['publisherKey'])
        # 基层党建 sources are client-fixed: non-local items are downweighted
        # by _score, never hard-excluded, so the board can still fill.
        items = [item for item in items
                 if board == '基层党建'
                 or not _is_non_target_leader_item(item)]
        excluded = set(exclude_quality)
        # Target leader activities bypass the quality gate.
        items = [item for item in items
                 if not _quality_excluded(item, excluded)
                 or _is_leader_item(item)]
        if board == '基层党建':
            items.sort(key=lambda item: (LOCALITY_SCORE.get(_locality(item), 0),
                                         _score(item, publisher_penalties)),
                       reverse=True)
        else:
            items.sort(key=lambda item: _score(item, publisher_penalties),
                       reverse=True)
        for item in items:
            item.setdefault('section', board)
        result[board] = items[:per_board]
    snapshots = {lib: svc.snapshot.stats for lib, svc in (
        services.items() if isinstance(services, dict) else [('policy', services)])}
    return dict(fromDate=from_date, toDate=to_date, boards=result,
                candidatePublishers=sorted(candidate_publishers),
                snapshots=snapshots)


def format_markdown(candidates: dict) -> tuple:
    """Topnews-style markdown + link_dict for an LLM editor stage."""
    markdown = ''
    link_dict = {}
    for board, items in candidates.get('boards', {}).items():
        markdown += f'## {board}\n'
        for item in items:
            key = f'[{len(link_dict) + 1}]'
            link_dict[key] = item
            relations = '；'.join(
                f"{r['subjectText']} {r['predicateLabel']} {r['objectText']}"
                for r in (item.get('relations') or [])[:3])
            leader_snippet = (_leader_snippet(item)
                              if _is_leader_item(item) else '')
            events = '、'.join(dict.fromkeys(
                e.get('name', '') for e in item.get('keyEntities', [])
                if e.get('label') in EVENT_LABELS and e.get('name')))
            markdown += (f"* {key}{item.get('title', '')}\n"
                         f"链接：{item.get('url', '')}\n"
                         f"来源：{item.get('publisher', '')} {item.get('publishedDate', '')}\n"
                         + (f"领导活动：{leader_snippet}\n" if leader_snippet else '')
                         + (f"事件/活动：{events}\n" if events else '')
                         + f"图关系：{relations}\n\n")
    return markdown, link_dict


# -- weekly dossier ----------------------------------------------------------

def _window_articles(service: SnapshotService, from_date: str, to_date: str) -> list:
    return [article for article in service.snapshot.articles.values()
            if article.get('publishedDate')
            and (not from_date or article['publishedDate'] >= from_date)
            and (not to_date or article['publishedDate'] <= to_date)]


def _merged_nodes(service_list: list) -> dict:
    merged = {}
    for service in service_list:
        for uri, node in service.snapshot.nodes.items():
            current = merged.get(uri)
            if current is None:
                merged[uri] = dict(node, labels=list(node.get('labels', [])),
                                   articleIds=list(node.get('articleIds', [])),
                                   blockIds=list(node.get('blockIds', [])))
                continue
            current['degree'] = current.get('degree', 0) + node.get('degree', 0)
            current['labels'] = list(dict.fromkeys(current.get('labels', []) + node.get('labels', [])))
            current['articleIds'] = list(dict.fromkeys(current.get('articleIds', []) + node.get('articleIds', [])))
            current['blockIds'] = list(dict.fromkeys(current.get('blockIds', []) + node.get('blockIds', [])))
            if not current.get('name'):
                current['name'] = node.get('name', '')
            if not current.get('canonicalName'):
                current['canonicalName'] = node.get('canonicalName', '')
            if current.get('regionLevel') is None:
                current['regionLevel'] = node.get('regionLevel')
    return merged


def _leader_uris(service_list: list, nodes: dict) -> set:
    result = {uri for uri, node in nodes.items()
              if node.get('label') in LEADER_LABELS
              or (node.get('label') == '人物' and node.get('name') in KNOWN_NATIONAL_LEADERS)}
    for service in service_list:
        for edge in service.snapshot.edges.values():
            if edge['predicateLabel'] not in {'任职', '现任'}:
                continue
            for person_uri, org_uri in ((edge['subject'], edge['object']),
                                        (edge['object'], edge['subject'])):
                person = nodes.get(person_uri)
                org = nodes.get(org_uri)
                if not person or person.get('label') != '人物' or not org:
                    continue
                if (edge['predicateLabel'] == '任职'
                        and org.get('label') in GOV_LABELS
                        and not LEADER_ORG_EXCLUDE.search(org.get('name', ''))):
                    result.add(person_uri)
                elif (edge['predicateLabel'] == '现任' and org.get('label') == '职务'
                      and LEADER_POST.search(org.get('name', ''))):
                    result.add(person_uri)
    return result


def _norm_policy(value: str) -> str:
    return re.sub(r'[\s\W_《》]+', '', value or '')


def _merge_policies(rows: list) -> list:
    rows = sorted(rows, key=lambda row: (row['count'], row['degree']), reverse=True)
    merged = []
    for row in rows:
        target = None
        for group in merged:
            left = _norm_policy(group.get('canonicalName') or group.get('name'))
            right = _norm_policy(row.get('canonicalName') or row.get('name'))
            if left and right and (left == right or (
                    len(left) >= 8 and len(right) >= 8
                    and (left in right or right in left)
                    and abs(len(left) - len(right)) <= 24)):
                target = group
                break
        if target is None:
            merged.append(dict(row, aliases=list(row.get('aliases') or [])))
            continue
        target['count'] += row['count']
        target['degree'] = max(target['degree'], row['degree'])
        target['articleIds'] = sorted(set(target.get('articleIds', [])) | set(row.get('articleIds', [])))
        if row['name'] not in target['aliases'] and row['name'] != target['name']:
            target['aliases'].append(row['name'])
        if len(row['name']) > len(target['name']):
            target['name'] = row['name']
            target['canonicalName'] = row.get('canonicalName')
            target['uri'] = row['uri']
    return merged


def _node_row(node: dict, count: int, article_ids) -> dict:
    return dict(uri=node['uri'], name=node['name'],
                canonicalName=node.get('canonicalName'),
                label=node.get('label'), count=count,
                degree=node.get('degree', 0),
                regionLevel=node.get('regionLevel'),
                articleIds=sorted(article_ids))


def weekly_dossier(services, *, from_date: str, to_date: str,
                   boards=None, max_policies: int = 8,
                   dedupe_threshold: int = 90) -> dict:
    """Window dossier for fallback digest/analysis.

    Simplification vs production: ``policyLinks`` (QueryService.policy_links
    two/three-tier expansion) is omitted; it never feeds selection or prompts,
    only the unused dossier field.
    """
    boards = boards or SECTIONS
    service_list = _all_services(services)
    articles = []
    for service in service_list:
        articles.extend(_window_articles(service, from_date, to_date))
    by_board = defaultdict(list)
    for article in articles:
        for board in article['boards']:
            by_board[board].append(article)
    node_counts = Counter()
    node_articles = defaultdict(set)
    for service in service_list:
        for article in _window_articles(service, from_date, to_date):
            for uri in article['nodeUris']:
                node_counts[uri] += 1
                node_articles[uri].add(article['articleId'])
    nodes = _merged_nodes(service_list)
    leader_uris = _leader_uris(service_list, nodes)
    policies, actions, leaders, regions = [], [], [], []
    for uri, count in node_counts.items():
        node = nodes.get(uri)
        if not node:
            continue
        label = node.get('label')
        if is_policy_node(node):
            policies.append(_node_row(node, count, node_articles[uri]))
        elif uri in leader_uris:
            leaders.append(_node_row(node, count, node_articles[uri]))
        elif label in ACTION_LABELS:
            actions.append(_node_row(node, count, node_articles[uri]))
        elif label in REGION_LABELS and node.get('regionLevel') is not None:
            regions.append(_node_row(node, count, node_articles[uri]))
    policies = _merge_policies(policies)
    policies.sort(key=lambda row: (row['count'], row['degree']), reverse=True)
    actions.sort(key=lambda row: (row['count'], row['degree']), reverse=True)
    leaders.sort(key=lambda row: (row['count'], row['degree']), reverse=True)
    regions.sort(key=lambda row: (row['count'], row['degree']), reverse=True)
    sections = {}
    for board in boards:
        board_service = _service_for(services, board)
        board_articles = by_board.get(board, [])
        items = [article_view(board_service.snapshot, article, with_relations=True,
                              relation_limit=12) for article in board_articles]
        items = dedupe_articles(items, threshold=dedupe_threshold)
        items.sort(key=lambda item: (item.get('relationCount', 0),
                                     item.get('entityCount', 0)), reverse=True)
        sections[board] = dict(articles=items,
                               articleCount=len(board_articles),
                               entityCount=sum(len(a['nodeUris']) for a in board_articles),
                               relationCount=sum(len(a['edgeKeys']) for a in board_articles))
    return dict(fromDate=from_date, toDate=to_date,
                sections=sections,
                topPolicies=policies[:max_policies],
                topActions=actions[:20],
                topLeaders=leaders[:20],
                topRegions=regions[:20],
                policyLinks={},
                snapshots={lib: svc.snapshot.stats for lib, svc in (
                    services.items() if isinstance(services, dict) else [('policy', services)])})
