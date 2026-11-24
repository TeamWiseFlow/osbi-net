"""Evidence-to-article bridge for the shadow topnews/weekly pipelines.

The osbi server exposes block-level evidence; the production shadow selection
algorithms operate on article-level ontology snapshots. This module rebuilds
the production snapshot shape (blocks/articles/nodes/edges with provenance,
degree and canonical labels) from evidence records, plus the article views and
title dedupe used by the selectors. Constants mirror
midplatform/ontology_query/{snapshot,query}.py; the legacy weekly.py read model
is intentionally left untouched.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from difflib import SequenceMatcher
import re

from osbi_contracts.storage import digest
from osbi_contracts.urls import canonical_url, normalize_publish_date

# -- production constants (ontology_query/snapshot.py) -----------------------

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
NON_POLICY_TITLE = re.compile(
    r'(基金份额发售公告|份额发售公告|招募说明书|基金合同|托管协议|分红公告|'
    r'申购赎回|上市交易公告|净值公告|基金产品资料概要|定期报告|季度报告|年度报告)')
HIGH_VALUE_PREDICATES = {
    '落实', '依据', '提及', '响应', '发布', '实施主体', '主办', '涉及', '参与',
    '前往', '考察', '强调', '服务', '采用', '隶属', '位于', '现任', '任职',
    '建设运营', '上市', '跟踪', '发布数据',
}
# -- production constants (ontology_query/query.py) --------------------------

NODE_PRIORITY = {
    '国家级政策': 100, '省级政策': 96, '市级政策': 94, '区级政策': 92,
    '街镇级政策': 90, '政策征求意见稿': 88,
    '调研视察': 84, '街镇行动': 80, '社区行动': 78, '区级行动': 76,
    '市级行动': 74, '行动事项': 72, '会议论坛': 70, '事件': 68,
    '国家级机构': 60, '省级机构': 58, '市级机构': 56, '区级机构': 54,
    '街镇机构': 52, '政府机构（含党组织）': 50, '社区自治组织': 48,
    '社会组织': 46, '企业': 44, '机构组织': 42,
    '领导干部': 36, '人物': 34, '治理机制': 24, '数字平台系统': 22,
    '社会群体': 18, '概念议题': 12, '通用名词': 8, '职务': 6,
}
POLICY_LINK_PREDICATES = {'落实', '依据', '提及', '响应', '发布'}
TITLE_NOISE = re.compile(r'[\s\W_]+')
_PUBLISH_DATE_SUFFIX = re.compile(
    r'\s*[（(]\s*内容发布日期\s*[：:]\s*\d{4}-\d{2}-\d{2}\s*[）)]\s*$')


def strip_publish_date_suffix(title: str) -> str:
    """Remove the extraction-only '（内容发布日期：YYYY-MM-DD）' suffix."""
    return _PUBLISH_DATE_SUFFIX.sub('', title or '').strip()


def is_substantive_policy_name(name: str) -> bool:
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


def canonical_label(labels, name: str) -> str:
    values = {str(label).strip() for label in labels if str(label).strip()}
    if not values:
        return ''
    region = node_region_label(name)
    if region and region in values:
        return region
    if str(name).endswith(PUBLIC_INSTITUTION_SUFFIXES) and '机构组织' in values:
        return '机构组织'
    if region is None and values & REGION_LABELS and str(name).endswith(
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


def region_level_for(name: str, label: str, explicit=None) -> int | None:
    if isinstance(explicit, int):
        return explicit
    if name in {'国家', '中国', '中华人民共和国'}:
        return 0
    if label in REGION_LEVEL and (node_region_label(name) or label in ('省级机构', '市级机构')):
        return REGION_LEVEL[label]
    return None


def is_policy_node(node: dict) -> bool:
    return node.get('label') in POLICY_LABELS and node.get('policySubstantive', True)


def is_noise_policy_node(node: dict) -> bool:
    return node.get('label') in POLICY_LABELS and not node.get('policySubstantive', True)


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
                      entity_count: int, relation_count: int) -> list:
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


def _append_unique(target: list, values) -> None:
    seen = set(target)
    for value in values:
        if value not in seen:
            target.append(value)
            seen.add(value)


class ShadowSnapshot:
    """Production-compatible read model rebuilt from osbi evidence records."""

    def __init__(self, library_id: str, blocks: dict, articles: dict,
                 nodes: dict, edges: dict, normalization: dict | None = None,
                 warnings: list | None = None):
        self.library_id = library_id
        self.blocks = blocks
        self.articles = articles
        self.nodes = nodes
        self.edges = edges
        self.built_at = datetime.now(timezone.utc).isoformat()
        self.normalization = normalization
        self.warnings = warnings or []

    @property
    def stats(self) -> dict:
        result = dict(libraryId=self.library_id, builtAt=self.built_at,
                      blocks=len(self.blocks), articles=len(self.articles),
                      nodes=len(self.nodes), edges=len(self.edges))
        if self.normalization:
            result['normalization'] = self.normalization
        return result


# Authoritative node fields owned by the server read model (seed merge, alias
# merge, deterministic relabel, hub drop, library-wide degree). Block/article
# provenance stays client-window-scoped: the selectors intersect it with the
# local window article ids.
_SERVER_NODE_FIELDS = ('name', 'canonicalName', 'label', 'regionLevel', 'degree',
                       'seed', 'qualified', 'policySubstantive', 'aliases',
                       'mergedUris', 'officialEvidence', 'labelOriginal',
                       'labelNormalized')


def _merge_server_node(nodes: dict, payload: dict) -> None:
    """Upsert one server-normalized node, keeping local window provenance."""
    uri = str(payload.get('uri') or '')
    if not uri:
        return
    node = nodes.get(uri)
    if node is None:
        node = dict(uri=uri, names=[], name='', canonicalName='', labels=[],
                    label='', regionLevel=None, tags=[], blockIds=[],
                    articleIds=[], degree=0)
        nodes[uri] = node
    for field in _SERVER_NODE_FIELDS:
        if field in payload and payload[field] is not None:
            node[field] = payload[field]
    if payload.get('labels'):
        node['labels'] = list(dict.fromkeys(payload['labels']))
    for name in payload.get('names') or []:
        if name and name not in node['names']:
            node['names'].append(name)
    if payload.get('name') and not node['name']:
        node['name'] = payload['name']
    node['serverNormalized'] = True


def snapshot_from_evidence(records, library_id: str = 'policy',
                           *, graph_extras: dict | None = None,
                           board_label: str | None = None) -> ShadowSnapshot:
    """Aggregate block-level evidence items into production-shaped articles.

    Blocks of one article are grouped by canonical URL + published date (the
    evidence API carries no material id). Nodes merge labels/provenance across
    blocks; edges merge by subject|predicate|object and keep article sources.

    When the server query side supplies normalized evidence (``normalized``
    items plus ``graph_extras`` with nodes/seedNodes/seedEdges/normalization),
    the server values are authoritative: the client never re-runs seed merge
    or read-model normalization locally. Legacy un-normalized records keep the
    original local canonicalization path.
    """
    blocks: dict = {}
    articles: dict = {}
    nodes: dict = {}
    edges: dict = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        block_key = str(record.get('evidenceId') or record.get('id')
                        or digest([record.get('text', '')]))
        text = str(record.get('text') or '')
        title = str(record.get('title') or '').strip()
        published = normalize_publish_date(record.get('publishedDate') or '')
        entities = record.get('entities') or []
        relations = record.get('relations') or []
        boards = [str(b) for b in (record.get('boards') or []) if b]
        if board_label:
            boards = [board_label]
        content_types = [str(t) for t in (record.get('contentTypes') or []) if t]
        if not content_types:
            content_types = [classify_content(title)]
        flags = record.get('qualityFlags')
        if not isinstance(flags, list):
            flags = quality_flags_for(title, published, content_types[0],
                                      len(entities), len(relations))
        flags = sorted({str(f) for f in flags if f})
        sources = record.get('sources') or []
        source = sources[0] if sources else {}
        meta = source.get('meta') or {}
        url = canonical_url(meta.get('canonicalUrl') or meta.get('finalUrl')
                            or meta.get('requestedUrl') or source.get('url', ''))
        if not published:
            published = normalize_publish_date(meta.get('publishedAt', ''))
        publisher = str(meta.get('publisher') or meta.get('author') or '').strip()
        captured_at = str(meta.get('capturedAt') or '')
        article_id = 'a_' + digest([url, published])
        if not title:
            title = str(source.get('title') or '').strip()

        node_uris, edge_keys = [], []
        for ent in entities:
            uri = str(ent.get('uri') or '')
            if not uri:
                continue
            name = str(ent.get('name') or ent.get('text') or '').strip()
            label = str(ent.get('label') or '').strip()
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
            if label and label not in node['labels']:
                node['labels'].append(label)
            if record.get('normalized'):
                # Server read model is authoritative for this entity.
                _merge_server_node(nodes, dict(ent, uri=uri,
                                               names=[name] if name else [],
                                               labels=[label] if label else []))
            _append_unique(node['blockIds'], [block_key])
            _append_unique(node['articleIds'], [article_id])
            node_uris.append(uri)
        for rel in relations:
            subject = str(rel.get('subject') or '')
            obj = str(rel.get('object') or '')
            label = str(rel.get('label') or rel.get('predicate') or '')
            if not subject or not obj or not label:
                continue
            key = f'{subject}|{label}|{obj}'
            edge = edges.get(key)
            if edge is None:
                edge = dict(key=key, subject=subject, predicate=label,
                            predicateLabel=label, object=obj,
                            subjectText=str(rel.get('subjectText') or ''),
                            objectText=str(rel.get('objectText') or ''),
                            blockIds=[], articleIds=[])
                edges[key] = edge
            _append_unique(edge['blockIds'], [block_key])
            _append_unique(edge['articleIds'], [article_id])
            edge_keys.append(key)

        blocks[block_key] = dict(
            extractionId=block_key, articleId=article_id, url=url, title=title,
            publisher=publisher, publishedDate=published, capturedAt=captured_at,
            boards=boards, contentTypes=content_types, qualityFlags=flags,
            # Production-shaped singular fields (one block = one board/type);
            # the deterministic fact extractor reads these directly.
            board=boards[0] if boards else '',
            contentType=content_types[0] if content_types else '',
            entityCount=len(entities), relationCount=len(relations),
            nodeUris=node_uris, edgeKeys=edge_keys, rawText=text[:12000])

        article = articles.get(article_id)
        if article is None:
            article = dict(articleId=article_id, url=url, title=title,
                           publisher=publisher, publishedDate=published,
                           capturedAt=captured_at, boards=[], contentTypes=[],
                           qualityFlags=[], schemaIds=[], blockIds=[],
                           nodeUris=[], edgeKeys=[])
            articles[article_id] = article
        if title and not article['title']:
            article['title'] = title
        if publisher and not article['publisher']:
            article['publisher'] = publisher
        if published and (not article['publishedDate']
                          or published < article['publishedDate']):
            article['publishedDate'] = published
        if captured_at and (not article['capturedAt'] or captured_at < article['capturedAt']):
            article['capturedAt'] = captured_at
        _append_unique(article['boards'], boards)
        _append_unique(article['contentTypes'], content_types)
        _append_unique(article['qualityFlags'], flags)
        _append_unique(article['blockIds'], [block_key])
        _append_unique(article['nodeUris'], node_uris)
        _append_unique(article['edgeKeys'], edge_keys)

    for node in nodes.values():
        if node.get('serverNormalized'):
            # Server already applied canonical label/name/region/degree over
            # the whole library (seed skeleton included); keep those values.
            continue
        node['label'] = canonical_label(node['labels'], node['name'])
        node['canonicalName'] = canonical_name(node['name'], node['label'],
                                               node['canonicalName'])
        node['regionLevel'] = region_level_for(node['name'], node['label'],
                                               node['regionLevel'])
        if node['label'] in POLICY_LABELS:
            node['policySubstantive'] = is_substantive_policy_name(node['name'])
        node['degree'] = 0
    for edge in edges.values():
        if not edge['subjectText']:
            edge['subjectText'] = nodes.get(edge['subject'], {}).get('name', '')
        if not edge['objectText']:
            edge['objectText'] = nodes.get(edge['object'], {}).get('name', '')
        subject = nodes.get(edge['subject'])
        if subject is not None and not subject.get('serverNormalized'):
            subject['degree'] += 1
        obj = nodes.get(edge['object'])
        if obj is not None and not obj.get('serverNormalized'):
            obj['degree'] += 1

    normalization = None
    if graph_extras:
        # Server-normalized node payloads (relation endpoints, qualified posts)
        # and the seed skeleton with its 隶属 edges.
        for payload in graph_extras.get('nodes') or []:
            _merge_server_node(nodes, payload)
        for payload in graph_extras.get('seedNodes') or []:
            _merge_server_node(nodes, payload)
        for edge in graph_extras.get('seedEdges') or []:
            key = str(edge.get('key') or '')
            if key and key not in edges:
                edges[key] = dict(edge)
        normalization = graph_extras.get('normalization')
    return ShadowSnapshot(library_id, blocks, articles, nodes, edges,
                          normalization=normalization)


# -- article views (ontology_query/query.py) ---------------------------------

def _article_evidence(article: dict, block_ids=None) -> dict:
    matched = [block_id for block_id in (block_ids or article['blockIds'])
               if block_id in article['blockIds']]
    return dict(articleId=article['articleId'], url=article['url'],
                title=article['title'], publisher=article['publisher'],
                publishedDate=article['publishedDate'],
                boards=article['boards'], contentTypes=article['contentTypes'],
                qualityFlags=article['qualityFlags'],
                schemaIds=article['schemaIds'],
                blockIds=matched or article['blockIds'])


def _node_brief(node: dict) -> dict:
    return dict(uri=node['uri'], name=node['name'], names=node['names'],
                canonicalName=node['canonicalName'], label=node['label'],
                labels=node['labels'], regionLevel=node.get('regionLevel'),
                policySubstantive=node.get('policySubstantive'),
                degree=node.get('degree', 0), seed=bool(node.get('seed')),
                qualified=node.get('qualified'))


def _key_entities(snapshot: ShadowSnapshot, article: dict, limit: int = 10) -> list:
    node_uris = [uri for uri in article['nodeUris'] if uri in snapshot.nodes
                 and not is_noise_policy_node(snapshot.nodes[uri])]
    ranked = sorted(
        node_uris,
        key=lambda uri: (NODE_PRIORITY.get(snapshot.nodes[uri].get('label'), 0),
                         snapshot.nodes[uri].get('degree', 0),
                         len(snapshot.nodes[uri].get('name', ''))),
        reverse=True)
    return [_node_brief(snapshot.nodes[uri]) for uri in ranked[:limit]]


def _edge_view(snapshot: ShadowSnapshot, edge: dict) -> dict:
    subject = snapshot.nodes.get(edge['subject'], {})
    obj = snapshot.nodes.get(edge['object'], {})
    subject_text = edge.get('subjectText') or subject.get('name', '')
    object_text = edge.get('objectText') or obj.get('name', '')
    subject_uri, object_uri = edge['subject'], edge['object']
    subject_label = subject.get('label', '')
    object_label = obj.get('label', '')
    normalized = False
    # Old graphs stored 发布 as policy→issuer; normalize display direction.
    if (edge['predicateLabel'] == '发布'
            and subject_label in POLICY_LABELS
            and object_label in _ORG_LABELS):
        subject_uri, object_uri = object_uri, subject_uri
        subject_text, object_text = object_text, subject_text
        subject_label, object_label = object_label, subject_label
        normalized = True
    return dict(key=edge['key'], subject=subject_uri, subjectText=subject_text,
                subjectLabel=subject_label, predicate=edge['predicate'],
                predicateLabel=edge['predicateLabel'], object=object_uri,
                objectText=object_text, objectLabel=object_label,
                directionNormalized=normalized, blockIds=edge['blockIds'],
                evidence=[_article_evidence(snapshot.articles[a],
                                            [b for b in snapshot.articles[a]['blockIds']
                                             if b in edge['blockIds']])
                          for a in edge['articleIds'][:3]
                          if a in snapshot.articles])


_ORG_LABELS = {'机构组织', '政府机构（含党组织）', '国家级机构', '省级机构', '市级机构',
               '区级机构', '街镇机构', '社区自治组织', '社会组织', '企业',
               '境外组织', '境外国家/地区', '境外政府机构', '国际组织', '金融机构',
               '交易场所'}


def _article_relations(snapshot: ShadowSnapshot, article: dict, limit: int = 20) -> list:
    def usable(edge):
        if edge['predicateLabel'] not in HIGH_VALUE_PREDICATES:
            return False
        if edge['predicateLabel'] == '发布':
            return not (is_noise_policy_node(snapshot.nodes.get(edge['subject'], {}))
                        or is_noise_policy_node(snapshot.nodes.get(edge['object'], {})))
        return True
    edges = [snapshot.edges[key] for key in article['edgeKeys']
             if key in snapshot.edges and usable(snapshot.edges[key])]
    edges.sort(key=lambda edge: (
        1 if edge['predicateLabel'] in POLICY_LINK_PREDICATES else 0,
        snapshot.nodes.get(edge['subject'], {}).get('degree', 0),
        snapshot.nodes.get(edge['object'], {}).get('degree', 0)), reverse=True)
    return [_edge_view(snapshot, edge) for edge in edges[:limit]]


def article_view(snapshot: ShadowSnapshot, article: dict, *,
                 with_relations: bool = True, relation_limit: int = 20) -> dict:
    item = dict(articleId=article['articleId'], url=article['url'],
                title=article['title'], publisher=article['publisher'],
                publishedDate=article['publishedDate'],
                capturedAt=article['capturedAt'],
                boards=article['boards'], contentTypes=article['contentTypes'],
                qualityFlags=article['qualityFlags'],
                schemaIds=article['schemaIds'], blockIds=article['blockIds'],
                entityCount=len(article['nodeUris']),
                relationCount=len(article['edgeKeys']),
                keyEntities=_key_entities(snapshot, article),
                evidence=[_article_evidence(article)])
    if with_relations:
        item['relations'] = _article_relations(snapshot, article, relation_limit)
    return item


def list_articles(snapshot: ShadowSnapshot, *, from_date: str = '', to_date: str = '',
                  boards=None, content_types=None, exclude_quality=None,
                  include_undated: bool = False, limit: int = 100, offset: int = 0,
                  with_relations: bool = True) -> dict:
    boards = set(boards or ())
    content_types = set(content_types or ())
    exclude_quality = set(exclude_quality or ())
    items = []
    for article in snapshot.articles.values():
        date = article.get('publishedDate') or ''
        if not date:
            if not include_undated or from_date or to_date:
                continue
        else:
            if from_date and date < from_date:
                continue
            if to_date and date > to_date:
                continue
        if boards and not boards & set(article['boards']):
            continue
        if content_types and not content_types & set(article['contentTypes']):
            continue
        if exclude_quality and exclude_quality & set(article['qualityFlags']):
            continue
        items.append(article)
    items.sort(key=lambda article: (
        article.get('publishedDate') or '', len(article['edgeKeys']),
        len(article['nodeUris'])), reverse=True)
    total = len(items)
    selected = items[offset:offset + limit]
    return dict(items=[article_view(snapshot, article, with_relations=with_relations)
                       for article in selected],
                total=total, offset=offset, limit=limit, snapshot=snapshot.stats)


def _normalize_title(title: str) -> str:
    value = str(title or '')
    value = re.sub(r'[（(]内容发布日期[:：][^）)]*[）)]', '', value)
    value = re.sub(r'^.*?[|｜]\s*', '', value) if '|' in value or '｜' in value else value
    return TITLE_NOISE.sub('', value).lower()


def dedupe_articles(items: list, *, threshold: int = 90) -> list:
    """Group near-duplicate stories by date/board and title similarity."""
    groups = []
    for item in items:
        normalized = _normalize_title(item.get('title', ''))
        if not normalized:
            groups.append(dict(representative=item, duplicates=[]))
            continue
        placed = False
        for group in groups:
            rep = group['representative']
            if rep.get('publishedDate') != item.get('publishedDate'):
                continue
            if not set(rep.get('boards', [])) & set(item.get('boards', [])):
                continue
            other = _normalize_title(rep.get('title', ''))
            if not other:
                continue
            ratio = SequenceMatcher(None, normalized, other).ratio() * 100
            if ratio >= threshold:
                group['duplicates'].append(item)
                if (item.get('relationCount', 0), item.get('entityCount', 0)) > (
                        rep.get('relationCount', 0), rep.get('entityCount', 0)):
                    group['representative'] = item
                placed = True
                break
        if not placed:
            groups.append(dict(representative=item, duplicates=[]))
    result = []
    for group in groups:
        rep = dict(group['representative'])
        duplicates = group['duplicates']
        rep['duplicateCount'] = len(duplicates)
        rep['duplicateUrls'] = [d.get('url', '') for d in duplicates if d.get('url')]
        flags = list(rep.get('qualityFlags', []))
        for duplicate in duplicates:
            for flag in duplicate.get('qualityFlags', []):
                if flag not in flags:
                    flags.append(flag)
        rep['qualityFlags'] = flags
        result.append(rep)
    result.sort(key=lambda item: (item.get('publishedDate', ''),
                                  item.get('relationCount', 0)), reverse=True)
    return result


class SnapshotService:
    """Minimal stand-in for the production QueryService (no trace writer)."""

    def __init__(self, snapshot: ShadowSnapshot):
        self.snapshot = snapshot

    def list_articles(self, **kwargs):
        return list_articles(self.snapshot, **kwargs)
