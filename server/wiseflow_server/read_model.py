"""Read-only normalized snapshot of active ontology blocks (server query side).

Faithful port of midplatform/ontology_query/snapshot.py (graph-building core).
The extraction store owns Oxigraph; the query path builds a compact snapshot
from the durable block results, merges the deterministic Shanghai admin
skeleton (seeds_shanghai.ttl) and applies the view-layer normalization from
read_normalize.py. Nothing here writes back to the store: rebuilding the
snapshot is enough (production semantics).

Snapshot layout (all ids are stable strings):

- ``blocks[extraction_id]``  one selected original block (provenance + counts)
- ``articles[article_id]``   blocks grouped by ``material_id`` (or URL)
- ``nodes[uri]``             merged entity, including all block/article sources
- ``edges["s|p|o"]``         merged relation, including all block/article sources

The midplatform-specific receipt/HTTP refresh plumbing (read_receipts,
fetch_blocks, refresh_snapshot, work_dir paths) is intentionally not ported:
the osbi server already owns receipts and the store handle (see app.py).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

BOARD_BY_FOCUS = {2: '经济动态', 8: '社区治理', 9: '经济动态',
                  10: '基层党建', 11: '政策速递', 12: '数字社区'}
REGION_LABELS = {'国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构'}
POLICY_LABELS = {'政策文件', '国家级政策', '省级政策', '市级政策', '区级政策',
                 '街镇级政策', '政策征求意见稿'}
GENERIC_REGION_NAMES = {
    '全国', '我国', '城镇', '乡村', '东部地区', '西部地区', '中部地区', '东北地区',
    '东部', '西部', '中部', '东北', '城区', '郊区', '开发区', '园区', '景区',
    '校区', '厂区', '社区', '小区',
}
NON_ADMIN_AREA_SUFFIXES = (
    '社区', '小区', '园区', '校区', '景区', '厂区', '摊区', '片区', '街区',
    '居民区', '生活区', '作业区', '示范区', '试验区', '保护区', '风景区',
    '工业区', '商业区', '居住区', '营区',
)
PUBLIC_INSTITUTION_SUFFIXES = (
    '医院', '中医院', '大学', '学院', '学校', '广播电视台', '电视台', '电台', '总台',
    '日报', '晚报', '报社', '出版社', '研究院', '研究所', '科学院', '图书馆',
    '博物馆', '文化馆', '体育馆', '剧院', '歌舞团', '报',
)
REGION_SUFFIXES = (
    ('特别行政区', '市级机构'), ('自治区', '省级机构'), ('自治州', '市级机构'),
    ('街道', '街镇机构'), ('地区', '市级机构'), ('省', '省级机构'),
    ('市', '市级机构'), ('区', '区级机构'), ('县', '区级机构'),
    ('镇', '街镇机构'), ('乡', '街镇机构'),
)
REGION_LEVEL = {'国家级机构': 0, '省级机构': 1, '市级机构': 2,
                '区级机构': 3, '街镇机构': 4}
# Presentation priority for one-off class conflicts. Mirrors the extraction
# freeze priority, but is intentionally local: querying must not import the
# extraction runtime or change its frozen fingerprint.
LABEL_PRIORITY = (
    '国家级政策', '省级政策', '市级政策', '区级政策', '街镇级政策', '政策征求意见稿',
    '国家级机构', '省级机构', '市级机构', '区级机构', '街镇机构',
    '领导干部', '人物',
    '金融机构', '金融产品', '交易场所', '经济指标',
    '境外国家/地区', '境外政府机构', '国际组织', '境外组织',
    '企业', '社区自治组织', '社会组织', '政府机构（含党组织）', '机构组织',
    '会议论坛', '调研视察', '居民活动',
    '街镇行动', '社区行动', '区级行动', '市级行动', '行动事项', '事件',
    '政策文件', '治理机制', '数字平台系统', '社会群体', '概念议题', '通用名词', '职务',
    '招聘公告', '认定仲裁公告',
)
LABEL_RANK = {label: len(LABEL_PRIORITY) - index
              for index, label in enumerate(LABEL_PRIORITY)}
DATE_PATTERN = re.compile(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})')
NON_POLICY_TITLE = re.compile(
    r'(基金份额发售公告|份额发售公告|招募说明书|基金合同|托管协议|分红公告|'
    r'申购赎回|上市交易公告|净值公告|基金产品资料概要|定期报告|季度报告|年度报告)')
HIGH_VALUE_PREDICATES = {
    '落实', '依据', '提及', '响应', '发布', '实施主体', '主办', '涉及', '参与',
    '前往', '考察', '强调', '服务', '采用', '隶属', '位于', '现任', '任职',
    '建设运营', '上市', '跟踪', '发布数据',
}


def parse_date(value: Any) -> str:
    """Return YYYY-MM-DD for ISO/Chinese date strings; empty when unparseable."""
    text = str(value or '').strip()
    if not text:
        return ''
    match = DATE_PATTERN.search(text)
    if not match:
        return ''
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3])).strftime('%Y-%m-%d')
    except ValueError:
        return ''


def article_key(provenance: dict, extraction_id: str) -> str:
    """Group blocks of one article by material id; fall back to URL/block id."""
    material = str(provenance.get('material_id') or '').strip()
    if material:
        return 'm_' + material
    meta = provenance.get('source_meta') or {}
    url = str(provenance.get('source_url') or meta.get('canonicalUrl')
              or meta.get('finalUrl') or meta.get('requestedUrl') or '').strip()
    if url:
        parts = urlsplit(url)
        return 'u_' + urlunsplit((parts.scheme, parts.netloc, parts.path,
                                  parts.query, ''))
    return 'b_' + extraction_id


def is_substantive_policy_name(name: str) -> bool:
    """Financial product notices are labelled 政策文件 by the model but are not policy."""
    return not bool(NON_POLICY_TITLE.search(name or ''))


def node_region_label(name: str) -> str | None:
    value = str(name or '').strip()
    if len(value) < 3 or len(value) > 10 or value in GENERIC_REGION_NAMES:
        return None
    if value.endswith(NON_ADMIN_AREA_SUFFIXES):
        return None
    for suffix, label in REGION_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            return label
    return None


def is_public_institution(name: str) -> bool:
    return bool(name) and str(name).endswith(PUBLIC_INSTITUTION_SUFFIXES)


def canonical_label(labels: Iterable[str], name: str) -> str:
    values = {str(label).strip() for label in labels if str(label).strip()}
    if not values:
        return ''
    region = node_region_label(name)
    if region and region in values:
        return region
    if is_public_institution(name) and '机构组织' in values:
        return '机构组织'
    if region is None and values & REGION_LABELS and name.endswith(
            ('公司', '集团', '企业', '银行', '学校', '医院', '研究院',
             '社区', '小区', '居委会', '业委会')):
        non_region = values - REGION_LABELS
        if non_region:
            values = non_region
    return max(values, key=lambda label: LABEL_RANK.get(label, -1))


def canonical_name(name: str, label: str, explicit: str = '') -> str:
    if explicit:
        return str(explicit)
    value = str(name or '').strip()
    if label in POLICY_LABELS or value.startswith('《'):
        value = value.strip('《》').strip()
        value = re.sub(r'\s+', '', value)
        value = re.sub(r'[（(][^（）()]{0,30}[）)]$', '', value)
    return value


def region_level_for(name: str, label: str, explicit: Any = None) -> int | None:
    if isinstance(explicit, int):
        return explicit
    if str(explicit or '').isdigit():
        return int(explicit)
    if name in {'国家', '中国', '中华人民共和国'}:
        return 0
    if label in REGION_LEVEL and (node_region_label(name) or label in ('省级机构', '市级机构')):
        return REGION_LEVEL[label]
    return None


def classify_content(title: str) -> str:
    text = str(title or '')
    if re.search(r'(动态|速递|摘要|一周|早知道|盘点|汇总|集锦|资讯|周报)', text):
        return 'digest'
    if re.search(r'(举办|召开|举行|开展|启动|揭牌|签约|学习贯彻|专题学习|集中学习|志愿服务)', text):
        return 'activity'
    if re.search(r'(报告|统计|数据发布|发布会)', text):
        return 'report'
    if re.search(r'(评论|观察|启示|思考|党课|论坛|笔谈|综述|述评|调研报告|工作法|实践与思考)', text):
        return 'commentary'
    return 'news'


def quality_flags_for(title: str, published_date: str, content_type: str,
                      entity_count: int, relation_count: int) -> list[str]:
    flags = []
    if content_type in ('digest', 'commentary', 'report'):
        flags.append(content_type)
    if entity_count and relation_count == 0:
        flags.append('zero_relation')
    if entity_count and relation_count / entity_count < 0.08:
        flags.append('low_density')
    if not published_date:
        flags.append('no_date')
    return sorted(set(flags))


def _append_unique(target: list, values: Iterable[Any]) -> None:
    seen = set(target)
    for value in values:
        if value not in seen:
            target.append(value)
            seen.add(value)


class Snapshot:
    """Queryable, JSON-serializable graph snapshot for one ontology library."""

    def __init__(self, library_id: str, blocks: dict | None = None,
                 articles: dict | None = None, nodes: dict | None = None,
                 edges: dict | None = None, built_at: str = '',
                 warnings: list | None = None, normalization: dict | None = None):
        self.library_id = library_id
        self.blocks = blocks or {}
        self.articles = articles or {}
        self.nodes = nodes or {}
        self.edges = edges or {}
        self.built_at = built_at or datetime.now(timezone.utc).isoformat()
        self.warnings = warnings or []
        self.normalization = normalization

    @property
    def schema_versions(self) -> dict:
        return dict(Counter(block.get('schemaId', '') for block in self.blocks.values()
                            if block.get('schemaId')))

    @property
    def stats(self) -> dict:
        result = dict(libraryId=self.library_id, builtAt=self.built_at,
                      blocks=len(self.blocks), articles=len(self.articles),
                      nodes=len(self.nodes), edges=len(self.edges),
                      schemaVersions=self.schema_versions,
                      warnings=self.warnings[:20])
        if getattr(self, 'normalization', None):
            result['normalization'] = self.normalization
        return result

    def to_dict(self) -> dict:
        result = dict(libraryId=self.library_id, builtAt=self.built_at,
                      blocks=self.blocks, articles=self.articles,
                      nodes=self.nodes, edges=self.edges,
                      warnings=self.warnings)
        if self.normalization:
            result['normalization'] = self.normalization
        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'Snapshot':
        return cls(data.get('libraryId', ''), data.get('blocks') or {},
                   data.get('articles') or {}, data.get('nodes') or {},
                   data.get('edges') or {}, data.get('builtAt', ''),
                   data.get('warnings') or [],
                   data.get('normalization'))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding='utf-8')
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> 'Snapshot':
        return cls.from_dict(json.loads(path.read_text(encoding='utf-8')))


SEED_CLASS_LABELS = {'NationalGov': '国家级机构', 'ProvinceGov': '省级机构',
                     'CityGov': '市级机构', 'DistrictGov': '区级机构',
                     'StreetGov': '街镇机构'}
SEED_FILE = Path(__file__).with_name('schemas') / 'seeds_shanghai.ttl'
NS = 'https://onto.pilot.local/policy#'


def merge_seed_graph(snapshot: Snapshot, path: Path | None = None) -> int:
    """Merge the deterministic Shanghai admin skeleton into a snapshot.

    Seed facts have no extraction block/article source, so they are available
    for entity search and region roll-ups but never counted as articles.
    """
    path = Path(path or SEED_FILE)
    if not path.is_file():
        snapshot.warnings.append(f'seed file missing: {path}')
        return 0
    try:
        from rdflib import Graph, URIRef
        from rdflib.namespace import RDF
    except Exception as error:  # pragma: no cover - dependency is in requirements
        snapshot.warnings.append(f'seed parse unavailable: {error}')
        return 0
    graph = Graph().parse(data=path.read_text(encoding='utf-8'), format='turtle')
    name_predicate = URIRef(NS + 'name')
    part_of = URIRef(NS + 'partOf')
    added = 0
    for subject, name_node in graph.subject_objects(name_predicate):
        uri, name = str(subject), str(name_node).strip()
        class_node = next(graph.objects(subject, RDF.type), None)
        short = str(class_node).rsplit('#', 1)[-1] if class_node is not None else ''
        label = SEED_CLASS_LABELS.get(short, '')
        node = snapshot.nodes.get(uri)
        if node is None:
            node = dict(uri=uri, names=[name], name=name, canonicalName=name,
                        labels=[label] if label else [], label=label,
                        regionLevel=REGION_LEVEL.get(label), tags=[],
                        blockIds=[], articleIds=[], degree=0, seed=True)
            snapshot.nodes[uri] = node
            added += 1
        else:
            if name not in node['names']:
                node['names'].append(name)
            if label and label not in node['labels']:
                node['labels'].append(label)
            node['seed'] = True
    # Recompute canonical labels with seed classes included.
    for node in snapshot.nodes.values():
        node['label'] = canonical_label(node['labels'], node['name'])
        node['canonicalName'] = canonical_name(node['name'], node['label'],
                                               node['canonicalName'])
        if node.get('seed') and node.get('regionLevel') is None:
            node['regionLevel'] = REGION_LEVEL.get(node['label'])
    for subject, parent in graph.subject_objects(part_of):
        subject_uri, parent_uri = str(subject), str(parent)
        if subject_uri not in snapshot.nodes or parent_uri not in snapshot.nodes:
            continue
        key = f'{subject_uri}|{part_of}|{parent_uri}'
        if key not in snapshot.edges:
            snapshot.edges[key] = dict(
                key=key, subject=subject_uri, predicate=str(part_of),
                predicateLabel='隶属', object=parent_uri,
                subjectText=snapshot.nodes[subject_uri]['name'],
                objectText=snapshot.nodes[parent_uri]['name'],
                blockIds=[], articleIds=[], seed=True)
    for node in snapshot.nodes.values():
        node['degree'] = 0
    for edge in snapshot.edges.values():
        if edge['subject'] in snapshot.nodes:
            snapshot.nodes[edge['subject']]['degree'] += 1
        if edge['object'] in snapshot.nodes:
            snapshot.nodes[edge['object']]['degree'] += 1
    return added


def build_snapshot(library_id: str, block_results: Iterable[dict],
                   warnings: list | None = None,
                   *, include_seeds: bool = True) -> Snapshot:
    """Merge active block results into articles/nodes/edges.

    Input items are the exact payloads returned by
    ``GET /v1/ontology/blocks/{extraction_id}``. ``supersededBy`` blocks are
    ignored; callers may pass them safely.
    """
    blocks: dict[str, dict] = {}
    articles: dict[str, dict] = {}
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    warnings = list(warnings or [])

    for result in block_results:
        if not isinstance(result, dict) or result.get('supersededBy'):
            continue
        extraction_id = str(result.get('id') or '')
        if not extraction_id:
            continue
        provenance = result.get('provenance') or {}
        meta = provenance.get('source_meta') or {}
        title = str(provenance.get('source_title') or meta.get('title') or '').strip()
        url = str(provenance.get('source_url') or meta.get('canonicalUrl')
                  or meta.get('finalUrl') or meta.get('requestedUrl') or '').strip()
        publisher = str(meta.get('publisher') or meta.get('author') or '').strip()
        published_date = (str(provenance.get('published_date') or '')
                          or parse_date(meta.get('publishedAt'))
                          or parse_date(meta.get('capturedAt')))
        captured_at = str(meta.get('capturedAt') or '')
        focus_id = provenance.get('focus_id')
        board = str(provenance.get('board_label') or '').strip() or BOARD_BY_FOCUS.get(focus_id, '')
        entity_count = len(result.get('entities') or [])
        relation_count = len(result.get('relations') or [])
        content_type = str(provenance.get('content_type') or '').strip() or classify_content(title)
        flags = provenance.get('quality_flags')
        if not isinstance(flags, list):
            flags = quality_flags_for(title, published_date, content_type,
                                      entity_count, relation_count)
        schema_id = str(result.get('schemaId') or '')
        article_id = article_key(provenance, extraction_id)

        node_uris, edge_keys = [], []
        for entity in result.get('entities') or []:
            uri = str(entity.get('uri') or '')
            if not uri:
                continue
            name = str(entity.get('text') or entity.get('name') or '').strip()
            labels = [str(entity.get('label') or '').strip()]
            node = nodes.get(uri)
            if node is None:
                node = dict(uri=uri, names=[name] if name else [], name=name,
                            canonicalName='', labels=[], label='', regionLevel=None,
                            tags=[], blockIds=[], articleIds=[], degree=0)
                nodes[uri] = node
            if name and name not in node['names']:
                node['names'].append(name)
            if not node['name'] and name:
                node['name'] = name
            for label in labels:
                if label and label not in node['labels']:
                    node['labels'].append(label)
            explicit_name = str(entity.get('canonicalName') or '')
            if explicit_name and not node['canonicalName']:
                node['canonicalName'] = explicit_name
            if node['regionLevel'] is None and entity.get('regionLevel') is not None:
                node['regionLevel'] = entity.get('regionLevel')
            if entity.get('tags'):
                for tag in entity['tags']:
                    if tag not in node['tags']:
                        node['tags'].append(tag)
            _append_unique(node['blockIds'], [extraction_id])
            _append_unique(node['articleIds'], [article_id])
            node_uris.append(uri)

        for relation in result.get('relations') or []:
            subject = str(relation.get('subject') or '')
            predicate = str(relation.get('predicate') or '')
            obj = str(relation.get('object') or '')
            if not subject or not predicate or not obj:
                continue
            key = f'{subject}|{predicate}|{obj}'
            edge = edges.get(key)
            if edge is None:
                edge = dict(key=key, subject=subject, predicate=predicate,
                            predicateLabel=str(relation.get('label') or ''),
                            object=obj,
                            subjectText=str(relation.get('subjectText') or ''),
                            objectText=str(relation.get('objectText') or ''),
                            blockIds=[], articleIds=[])
                edges[key] = edge
            _append_unique(edge['blockIds'], [extraction_id])
            _append_unique(edge['articleIds'], [article_id])
            edge_keys.append(key)

        block = dict(extractionId=extraction_id, articleId=article_id, url=url,
                     title=title, publisher=publisher, publishedDate=published_date,
                     capturedAt=captured_at, board=board, contentType=content_type,
                     qualityFlags=flags, schemaId=schema_id, focusId=focus_id,
                     extractionVersion=str(provenance.get('extraction_version') or ''),
                     entityCount=entity_count, relationCount=relation_count,
                     nodeUris=node_uris, edgeKeys=edge_keys,
                     rawText=str(provenance.get('raw_text') or '')[:12000])
        blocks[extraction_id] = block

        article = articles.get(article_id)
        if article is None:
            article = dict(articleId=article_id, url=url, title=title,
                           publisher=publisher, publishedDate=published_date,
                           capturedAt=captured_at, boards=[], contentTypes=[],
                           qualityFlags=[], schemaIds=[], blockIds=[],
                           nodeUris=[], edgeKeys=[])
            articles[article_id] = article
        if title and not article['title']:
            article['title'] = title
        if url and not article['url']:
            article['url'] = url
        if publisher and not article['publisher']:
            article['publisher'] = publisher
        if published_date and (not article['publishedDate']
                               or published_date < article['publishedDate']):
            article['publishedDate'] = published_date
        if captured_at and (not article['capturedAt'] or captured_at < article['capturedAt']):
            article['capturedAt'] = captured_at
        _append_unique(article['boards'], [board] if board else [])
        _append_unique(article['contentTypes'], [content_type] if content_type else [])
        _append_unique(article['qualityFlags'], flags)
        _append_unique(article['schemaIds'], [schema_id] if schema_id else [])
        _append_unique(article['blockIds'], [extraction_id])
        _append_unique(article['nodeUris'], node_uris)
        _append_unique(article['edgeKeys'], edge_keys)

    for node in nodes.values():
        node['label'] = canonical_label(node['labels'], node['name'])
        node['canonicalName'] = canonical_name(node['name'], node['label'],
                                               node['canonicalName'])
        node['regionLevel'] = region_level_for(node['name'], node['label'],
                                               node['regionLevel'])
        if node['label'] in POLICY_LABELS:
            node['policySubstantive'] = is_substantive_policy_name(node['name'])
    for edge in edges.values():
        edge['predicateLabel'] = edge['predicateLabel'] or edge['predicate'].rsplit('#', 1)[-1]
    for node in nodes.values():
        node['degree'] = 0
    for edge in edges.values():
        if edge['subject'] in nodes:
            nodes[edge['subject']]['degree'] += 1
        if edge['object'] in nodes:
            nodes[edge['object']]['degree'] += 1

    snapshot = Snapshot(library_id, blocks, articles, nodes, edges,
                        warnings=warnings)
    if include_seeds:
        merge_seed_graph(snapshot)
    # Read-model normalization: merge alias URIs, fix deterministic label/role
    # errors, and drop high-confidence generic hubs. No model call and no
    # write-back to Oxigraph; rebuilding the snapshot is enough.
    from .read_normalize import normalize_snapshot
    normalize_snapshot(snapshot)
    return snapshot


# -- server query-path helpers ------------------------------------------------

# One normalized snapshot per library, rebuilt only when the active receipt
# set changes. Evidence paging and graph/query views share the same instance.
_CACHE: dict[str, tuple[tuple, Snapshot]] = {}


def _signature(block_results: list[dict]) -> tuple:
    return tuple(sorted((str(result.get('id') or ''),
                         str(result.get('supersededBy') or ''))
                        for result in block_results if isinstance(result, dict)))


def cached_snapshot(library_id: str, entries) -> Snapshot:
    """Build (or reuse) the normalized snapshot for (job_id, block_result) rows."""
    results = [result for _, result in entries]
    signature = _signature(results)
    cached = _CACHE.get(library_id)
    if cached is not None and cached[0] == signature:
        return cached[1]
    snapshot = build_snapshot(library_id, results)
    _CACHE[library_id] = (signature, snapshot)
    return snapshot


def node_view(node: dict) -> dict:
    """Full normalized node payload for downstream (client) consumption."""
    return dict(node)


def seed_view(snapshot: Snapshot) -> dict:
    """Seed skeleton nodes + 隶属 edges, for the evidence response payload."""
    nodes = [node_view(node) for node in snapshot.nodes.values() if node.get('seed')]
    edges = [dict(edge) for edge in snapshot.edges.values() if edge.get('seed')]
    return dict(seedNodes=nodes, seedEdges=edges)


def uri_map(snapshot: Snapshot) -> dict:
    """Alias URI -> representative URI, from the auditable mergedUris records."""
    mapping = {}
    for uri, node in snapshot.nodes.items():
        for merged in node.get('mergedUris') or []:
            mapping[merged] = uri
    return mapping
