"""View-layer normalization applied when a query snapshot is assembled.

Faithful port of midplatform/ontology_query/normalize.py. This module
deliberately does not touch Oxigraph or call a model. It fixes deterministic,
repeatable problems that are cheaper to correct in the read model than to
re-extract:

* alias nodes created by raw-text URIs (``《X》`` vs ``X``, whitespace variants,
  issuer prefixes, long/short titles);
* label/role errors that can be decided from the node name plus existing edges
  (inspection topics, generic mechanisms/groups/concepts, official posts).

Every change is auditable: merged nodes keep ``mergedUris``/``aliases``, and
relabelled nodes keep ``labelOriginal``/``labelNormalized``.
"""
from __future__ import annotations

from collections import defaultdict
import re
from typing import TYPE_CHECKING

from .read_model import (LABEL_RANK, POLICY_LABELS, REGION_LABELS,
                         is_substantive_policy_name)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .read_model import Snapshot

ORG_LABELS = {
    '机构组织', '政府机构（含党组织）', '国家级机构', '省级机构', '市级机构',
    '区级机构', '街镇机构', '社区自治组织', '社会组织', '企业', '境外组织',
    '境外国家/地区', '境外政府机构', '国际组织', '金融机构', '交易场所',
}
GOV_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构',
              '政府机构（含党组织）'}
PERSON_LABELS = {'人物', '领导干部'}
EVENT_LABELS = {'会议论坛', '事件', '行动事项', '市级行动', '区级行动',
                '街镇行动', '社区行动', '居民活动', '调研视察'}

# Conservative, high-confidence generic hubs. They are not useful query
# targets and inflate relation counts / hotspot breadth.
GENERIC_MECHANISMS = {
    '党建引领', '志愿服务', '联合巡查', '联合处置', '联合会商', '吹哨',
    '吹哨报到', '共建共治共享', '网格化管理', '精细化治理', '多元共治',
    '三治融合', '自治法治德治', '党建引领基层治理', '党建引领社会治理',
    '基层治理', '社会治理', '社区治理', '协同治理', '治理现代化',
    '现代化治理', '智慧治理', '数字治理', '共建共享', '共治共享',
}
GENERIC_GROUPS = {
    '群众', '群众们', '居民', '村民', '市民', '社区居民', '志愿者', '商户',
    '党员', '社会力量',
}
GENERIC_CONCEPTS = {
    '基层治理', '社区治理', '社会治理', '基层党建', '协同治理',
    '治理现代化', '共建共治共享', '高质量发展',
}

INSPECTION_VERB_RE = re.compile(
    r'(赴|到|前往|深入|调研|考察|走访|慰问|检查|督导|接访)')
LEADER_CORE_RE = re.compile(
    r'(书记|主任|主席|部长|局长|厅长|处长|科长|区长|市长|省长|镇长|院长|'
    r'校长|所长|站长|队长|秘书长)$')
PLACE_RE = re.compile(
    r'[一-龥]{2,8}(?:省|市|区|县|镇|街道|自治区|自治州)(?!长)')
GOV_NATIONAL_RE = re.compile(
    r'^(?:中央|国务院|国家|中国证监会|中国人民银行|国家金融监督管理总局|'
    r'金融监管总局|证监会|国家统计局)')
NON_GOV_ORG_RE = re.compile(
    r'(公司|集团|银行|研究院|大学|学院|中心|医院|学校|协会|学会|联合会|事务所)')
NON_ADMIN_AREA_SUFFIXES = (
    '社区', '小区', '园区', '校区', '景区', '厂区', '片区', '街区', '居民区',
    '生活区', '作业区', '示范区', '试验区', '保护区', '风景区', '工业区',
    '商业区', '居住区', '营区',
)
_ISSUER_PREFIX_RE = re.compile(
    r'^(?:中办国办|中共中央办公厅国务院办公厅|中共中央办公厅|国务院办公厅|'
    r'中办|国办)')
_VERSION_MARKER_RE = re.compile(r'[（(][^（）()]{0,30}[）)]')
_YEAR_RE = re.compile(r'(?:19|20)\d{2}')
_BOOK_MARK_RE = re.compile(r'[《》]')
_SPACE_RE = re.compile(r'\s+')


def _norm(value: str) -> str:
    return re.sub(r'[\s\W_《》]+', '', value or '')


def _unique(values) -> list:
    """Deduplicate names/URIs and dict-shaped tags without hashing them."""
    result = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _node_names(node: dict) -> list[str]:
    return _unique([node.get('name')] + list(node.get('names') or []))


def _node_dates(snapshot: 'Snapshot', node: dict) -> tuple:
    dates = set()
    for article_id in node.get('articleIds', []):
        article = snapshot.articles.get(article_id)
        if article and article.get('publishedDate'):
            dates.add(article['publishedDate'])
    return tuple(sorted(dates))


def _normalize_marker(marker: str) -> str:
    value = marker.strip('（）()')
    value = _SPACE_RE.sub('', value)
    for dash in ('—', '－', '–', '至'):
        value = value.replace(dash, '-')
    return value


def _version_key(node: dict) -> tuple:
    """Keep policy versions separate while merging punctuation variants.

    Use the raw primary name: ``canonicalName`` intentionally strips trailing
    parentheses, so ``（征求意见稿）`` must be read from ``name``.
    """
    name = node.get('name') or ''
    markers = tuple(sorted({_normalize_marker(marker)
                            for marker in _VERSION_MARKER_RE.findall(name)}))
    years = tuple(sorted(set(_YEAR_RE.findall(name))))
    return markers, years


def _is_year_only_version(version: tuple) -> bool:
    markers, years = version
    if not markers and not years:
        return False
    return all(re.fullmatch(r'\d{4}(?:-\d{4})?年?', marker) for marker in markers)


def _policy_core(name: str) -> str:
    value = _norm(name)
    return _ISSUER_PREFIX_RE.sub('', value)


def _alias_key(snapshot: 'Snapshot', node: dict):
    label = node.get('label') or ''
    name = node.get('canonicalName') or node.get('name') or ''
    if label in POLICY_LABELS:
        core = _policy_core(name)
        if len(core) < 4:
            return None
        return ('policy', core, _version_key(node))
    if label in ORG_LABELS and label not in REGION_LABELS:
        core = _norm(name)
        if len(core) < 2:
            return None
        return ('org', label, core)
    if label in REGION_LABELS:
        core = _norm(name)
        if len(core) < 2:
            return None
        return ('region', core)
    if label in EVENT_LABELS:
        core = _norm(node.get('name') or '')
        if not core:
            return None
        years = set(_YEAR_RE.findall(node.get('name') or ''))
        years.update(date[:4] for date in _node_dates(snapshot, node)
                     if date[:4].isdigit())
        return ('event', label, core, tuple(sorted(years)))
    return None


def _rep_score(node: dict) -> tuple:
    name = node.get('name') or ''
    odd = len(_BOOK_MARK_RE.findall(name)) + len(_SPACE_RE.findall(name))
    versioned = 0
    if node.get('label') in POLICY_LABELS:
        markers, years = _version_key(node)
        versioned = 1 if markers or years else 0
    return (1 if node.get('seed') else 0,
            versioned,
            len(node.get('articleIds') or []),
            len(node.get('blockIds') or []),
            LABEL_RANK.get(node.get('label'), -1),
            -odd,
            node.get('degree', 0),
            len(name))


def _display_name(names: list[str]) -> str:
    cleaned = [name.strip('《》').strip() for name in names if name]
    cleaned = [name for name in cleaned if name]
    no_space = [name for name in cleaned if not _SPACE_RE.search(name)]
    pool = no_space or cleaned
    return max(pool, key=len) if pool else ''


def merge_alias_nodes(snapshot: 'Snapshot') -> dict:
    """Merge nodes whose raw-text URIs differ only by presentation."""
    groups = defaultdict(list)
    for uri, node in list(snapshot.nodes.items()):
        key = _alias_key(snapshot, node)
        if key:
            groups[key].append(uri)

    # A short policy reference ("X") and the same document with a year range
    # ("X（2024—2026年）") are the same node. Status markers such as
    # （征求意见稿） / （讨论稿） stay separate on purpose.
    policy_by_core = defaultdict(list)
    for key, uris in list(groups.items()):
        if key[0] == 'policy':
            policy_by_core[key[1]].append((key, uris))
    for core, entries in policy_by_core.items():
        non_empty = [(key, uris) for key, uris in entries
                     if key[2][0] or key[2][1]]
        empty = [(key, uris) for key, uris in entries
                 if not (key[2][0] or key[2][1])]
        if len(non_empty) != 1 or not empty:
            continue
        version_key, version_uris = non_empty[0]
        if not _is_year_only_version(version_key[2]):
            continue
        for empty_key, empty_uris in empty:
            groups[version_key].extend(empty_uris)
            groups.pop(empty_key, None)

    uri_map = {}
    samples = []
    merged_groups = 0
    merged_nodes = 0
    for key, uris in groups.items():
        if len(uris) < 2:
            continue
        representative = max(
            uris, key=lambda uri: (_rep_score(snapshot.nodes[uri]), uri))
        rep = snapshot.nodes[representative]
        names = list(rep.get('names') or [])
        labels = list(rep.get('labels') or [])
        tags = list(rep.get('tags') or [])
        block_ids = list(rep.get('blockIds') or [])
        article_ids = list(rep.get('articleIds') or [])
        merged_uris = []
        seeds = bool(rep.get('seed'))
        region_level = rep.get('regionLevel')
        for uri in uris:
            if uri == representative:
                continue
            alias = snapshot.nodes[uri]
            names.extend(alias.get('names') or [])
            labels.extend(alias.get('labels') or [])
            tags.extend(alias.get('tags') or [])
            block_ids.extend(alias.get('blockIds') or [])
            article_ids.extend(alias.get('articleIds') or [])
            merged_uris.append(uri)
            seeds = seeds or bool(alias.get('seed'))
            if region_level is None:
                region_level = alias.get('regionLevel')
            uri_map[uri] = representative
            merged_nodes += 1
        display = _display_name(names) or rep.get('name') or ''
        rep['name'] = display
        rep['canonicalName'] = rep.get('canonicalName') or display
        rep['names'] = _unique([display] + names)
        rep['aliases'] = [name for name in rep['names'] if name != display]
        rep['labels'] = _unique(labels)
        rep['label'] = max(rep['labels'],
                           key=lambda label: LABEL_RANK.get(label, -1)) if rep['labels'] else rep.get('label')
        rep['tags'] = _unique(tags)
        rep['blockIds'] = _unique(block_ids)
        rep['articleIds'] = _unique(article_ids)
        rep['mergedUris'] = sorted(merged_uris)
        rep['seed'] = seeds
        if region_level is not None:
            rep['regionLevel'] = region_level
        if rep['label'] in POLICY_LABELS:
            rep['policySubstantive'] = is_substantive_policy_name(rep['name'])
        merged_groups += 1
        if len(samples) < 12:
            samples.append(dict(representative=representative,
                                mergedUris=sorted(merged_uris),
                                names=_unique(names)[:6]))

    for uri in uri_map:
        snapshot.nodes.pop(uri, None)
    key_map = _remap_edges(snapshot, uri_map)
    _rewrite_references(snapshot, uri_map, key_map)
    return dict(mergedGroups=merged_groups, mergedNodes=merged_nodes,
                samples=samples)


def _edge_lists(value) -> list:
    return list(value or [])


def _remap_edges(snapshot: 'Snapshot', uri_map: dict,
                 dropped: set | None = None) -> dict:
    """Rewrite edge endpoints and merge duplicate edges.

    ``uri_map[old] = None`` (or membership in ``dropped``) removes the node and
    every incident edge; otherwise it maps an alias URI to its canonical URI.
    """
    dropped = dropped or set()
    new_edges = {}
    key_map = {}
    for old_key, edge in list(snapshot.edges.items()):
        subject = uri_map.get(edge['subject'], edge['subject'])
        obj = uri_map.get(edge['object'], edge['object'])
        if subject is None or obj is None or subject in dropped or obj in dropped:
            key_map[old_key] = None
            continue
        new_key = f'{subject}|{edge["predicate"]}|{obj}'
        key_map[old_key] = new_key
        existing = new_edges.get(new_key)
        if existing is not None:
            existing['blockIds'] = _unique(
                _edge_lists(existing.get('blockIds')) + _edge_lists(edge.get('blockIds')))
            existing['articleIds'] = _unique(
                _edge_lists(existing.get('articleIds')) + _edge_lists(edge.get('articleIds')))
            existing['seed'] = bool(existing.get('seed') or edge.get('seed'))
            continue
        item = dict(edge)
        item['subject'] = subject
        item['object'] = obj
        item['key'] = new_key
        subject_node = snapshot.nodes.get(subject, {})
        object_node = snapshot.nodes.get(obj, {})
        item['subjectText'] = subject_node.get('name') or item.get('subjectText', '')
        item['objectText'] = object_node.get('name') or item.get('objectText', '')
        new_edges[new_key] = item
    snapshot.edges = new_edges
    return key_map


def _rewrite_references(snapshot: 'Snapshot', uri_map: dict, key_map: dict) -> None:
    def map_uri(value):
        return uri_map.get(value, value)

    def map_key(value):
        return key_map.get(value, value)

    for block in snapshot.blocks.values():
        block['nodeUris'] = _unique(
            [map_uri(uri) for uri in block.get('nodeUris', [])
             if map_uri(uri) is not None])
        block['edgeKeys'] = _unique(
            [map_key(key) for key in block.get('edgeKeys', [])
             if map_key(key) is not None])
        block['entityCount'] = len(block['nodeUris'])
        block['relationCount'] = len(block['edgeKeys'])
    for article in snapshot.articles.values():
        article['nodeUris'] = _unique(
            [map_uri(uri) for uri in article.get('nodeUris', [])
             if map_uri(uri) is not None])
        article['edgeKeys'] = _unique(
            [map_key(key) for key in article.get('edgeKeys', [])
             if map_key(key) is not None])


def _set_label(node: dict, label: str, reason: str) -> None:
    original = node.get('labelOriginal') or node.get('label')
    node['labelOriginal'] = original
    node['labelNormalized'] = label
    node['normalization'] = reason
    labels = [value for value in node.get('labels', [])
              if value not in {original, label}]
    node['labels'] = _unique([label] + labels)
    node['label'] = label


def _has_place(name: str) -> bool:
    for match in PLACE_RE.finditer(name):
        value = match.group(0)
        if value in ('自治区', '自治州'):
            continue
        if value.endswith(NON_ADMIN_AREA_SUFFIXES):
            continue
        return True
    return False


def _qualified_post(name: str) -> bool:
    """A post is queryable as a leader post only with a real jurisdiction."""
    value = (name or '').strip()
    if not value or not LEADER_CORE_RE.search(value):
        return False
    # Suffixes like 中心/公司/研究院 usually denote public institutions or
    # companies; a name that merely contains "深圳市宝安区" must not be
    # treated as a government leader post.
    if NON_GOV_ORG_RE.search(value):
        return False
    if _has_place(value):
        return True
    return bool(GOV_NATIONAL_RE.match(value))


def relabel_nodes(snapshot: 'Snapshot') -> dict:
    """Deterministic label/role fixes that need no model judgement."""
    stats = dict(inspectionTopics=0, officials=0, qualifiedPosts=0,
                 ambiguousPosts=0, samples=[])

    # Inspection topics: a 调研视察 entity must contain a visit verb. The
    # model sometimes labels the subject of an inspection ("...专项整顿工作")
    # as the inspection itself, which corrupts 前往/考察 subjects.
    for node in snapshot.nodes.values():
        if node.get('label') != '调研视察':
            continue
        names = _node_names(node)
        if any(INSPECTION_VERB_RE.search(name) for name in names):
            continue
        _set_label(node, '行动事项', 'inspection_topic')
        stats['inspectionTopics'] += 1
        if len(stats['samples']) < 10:
            stats['samples'].append(dict(kind='inspection', name=node.get('name')))

    # Post qualification. Generic "省委...部长" without a province name stays
    # marked, so leader selection cannot treat it as a locatable target.
    qualified_posts = set()
    for uri, node in snapshot.nodes.items():
        if node.get('label') != '职务':
            continue
        qualified = _qualified_post(node.get('name') or '')
        node['qualified'] = qualified
        if qualified:
            qualified_posts.add(uri)
            stats['qualifiedPosts'] += 1
        else:
            stats['ambiguousPosts'] += 1

    # Person -> Official only when a qualified government post is explicitly
    # linked by 现任. 任职 to a research institute / university is not enough.
    official_person = set()
    for edge in snapshot.edges.values():
        if edge.get('predicateLabel') != '现任':
            continue
        if edge.get('subject') in snapshot.nodes and edge.get('object') in qualified_posts:
            official_person.add(edge['subject'])
    for uri in official_person:
        node = snapshot.nodes.get(uri, {})
        if node.get('label') == '领导干部':
            continue
        if '人物' not in (node.get('labels') or []):
            continue
        _set_label(node, '领导干部', 'official_from_post')
        post = next((snapshot.nodes[edge['object']].get('name')
                     for edge in snapshot.edges.values()
                     if edge.get('subject') == uri and edge.get('predicateLabel') == '现任'
                     and edge.get('object') in qualified_posts), '')
        node['officialEvidence'] = post
        stats['officials'] += 1
        if len(stats['samples']) < 10:
            stats['samples'].append(dict(kind='official', name=node.get('name'),
                                         post=post))
    return stats


def drop_generic_nodes(snapshot: 'Snapshot') -> dict:
    """Remove high-confidence generic hubs that only inflate relation counts."""
    dropped = set()
    samples = []
    for uri, node in snapshot.nodes.items():
        if node.get('seed'):
            continue
        name = _norm(node.get('name') or '')
        labels = set(node.get('labels') or [])
        reason = ''
        if name in GENERIC_MECHANISMS and '治理机制' in labels:
            reason = 'generic_mechanism'
        elif name in GENERIC_GROUPS and '社会群体' in labels:
            reason = 'generic_group'
        elif name in GENERIC_CONCEPTS and '概念议题' in labels:
            reason = 'generic_concept'
        if not reason:
            continue
        dropped.add(uri)
        if len(samples) < 12:
            samples.append(dict(kind=reason, name=node.get('name')))
    if not dropped:
        return dict(droppedNodes=0, samples=[])
    key_map = _remap_edges(snapshot, {}, dropped=dropped)
    uri_map = {uri: None for uri in dropped}
    _rewrite_references(snapshot, uri_map, key_map)
    for uri in dropped:
        snapshot.nodes.pop(uri, None)
    return dict(droppedNodes=len(dropped), samples=samples)


def _recompute_degrees(snapshot: 'Snapshot') -> None:
    for node in snapshot.nodes.values():
        node['degree'] = 0
    for edge in snapshot.edges.values():
        if edge['subject'] in snapshot.nodes:
            snapshot.nodes[edge['subject']]['degree'] += 1
        if edge['object'] in snapshot.nodes:
            snapshot.nodes[edge['object']]['degree'] += 1


def normalize_snapshot(snapshot: 'Snapshot') -> dict:
    """Apply all view-layer fixes and return an auditable summary."""
    merged = merge_alias_nodes(snapshot)
    labels = relabel_nodes(snapshot)
    dropped = drop_generic_nodes(snapshot)
    _recompute_degrees(snapshot)
    stats = dict(mergedGroups=merged['mergedGroups'],
                 mergedNodes=merged['mergedNodes'],
                 relabeledInspectionTopics=labels['inspectionTopics'],
                 relabeledOfficials=labels['officials'],
                 qualifiedPosts=labels['qualifiedPosts'],
                 ambiguousPosts=labels['ambiguousPosts'],
                 droppedGenericNodes=dropped['droppedNodes'],
                 samples=merged['samples'][:4] + labels['samples'][:4] + dropped['samples'][:4])
    snapshot.normalization = stats
    return stats
