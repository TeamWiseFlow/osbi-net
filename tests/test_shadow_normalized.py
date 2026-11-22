"""Client consumes server-normalized evidence (seed skeleton + read model).

The server query side owns seed merge / alias merge / relabel / hub drop /
degree; the client bridge must consume those values verbatim instead of
recomputing them locally, while keeping window-scoped block/article provenance.
"""
from wiseflow_server.read_model import build_snapshot
from wiseflow_server.evidence import EvidenceQuery, evidence_page

from osbi_client.shadow_snapshot import (article_view, snapshot_from_evidence)

NS = 'https://onto.pilot.local/policy#'
INSTANCE = 'https://onto.pilot.local/instance/'


def block_result(block_id, *, title='静安区领导调研社区治理', url='https://example.org/n1',
                 published='2026-11-20', entities=(), relations=(),
                 raw_text='静安区领导调研社区治理。', content_type='news',
                 board='社区治理', publisher='静安发布'):
    return dict(
        id=block_id, schemaId='policy-v12',
        provenance=dict(source_title=title, source_url=url,
                        published_date=published, board_label=board,
                        content_type=content_type, raw_text=raw_text,
                        source_meta=dict(publisher=publisher, canonicalUrl=url,
                                         capturedAt=published)),
        entities=[dict(uri=uri, text=name, label=label) for uri, name, label in entities],
        relations=[dict(subject=s, predicate=NS + p, object=o, label=p)
                   for s, p, o in relations])


def normalized_page():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'p1', '《静安区促进就业实施方案》', '区级政策'),
        (INSTANCE + 'p2', '静安区促进就业实施方案', '区级政策'),
        (INSTANCE + 'person', '张三', '人物'),
        (INSTANCE + 'post', '静安区民政局局长', '职务'),
        (INSTANCE + 'hub', '党建引领', '治理机制'),
        (INSTANCE + 'insp', '社区环境专项整顿工作', '调研视察')],
        relations=[(INSTANCE + 'person', '现任', INSTANCE + 'post'),
                   (INSTANCE + 'p1', '依据', INSTANCE + 'p2'),
                   (INSTANCE + 'person', '前往', INSTANCE + 'insp'),
                   (INSTANCE + 'post', '采用', INSTANCE + 'hub')])]
    snapshot = build_snapshot('policy', results)
    page = evidence_page([('job1', results[0])], EvidenceQuery(),
                         library_id='policy', snapshot=snapshot)
    return snapshot, page


def test_client_consumes_server_labels_degrees_and_seeds():
    server, page = normalized_page()
    extras = dict(nodes=page['nodes'], seedNodes=page['seedNodes'],
                  seedEdges=page['seedEdges'], normalization=page['normalization'])
    snapshot = snapshot_from_evidence(page['items'], 'policy', graph_extras=extras)
    # Relabelled official survives: local canonical_label would demote it back
    # to 人物 only if recomputed; the server value must be kept.
    person = snapshot.nodes[INSTANCE + 'person']
    assert person['label'] == '领导干部'
    assert person['degree'] == server.nodes[INSTANCE + 'person']['degree']
    # Inspection topic relabel kept as well.
    assert snapshot.nodes[INSTANCE + 'insp']['label'] == '行动事项'
    # Alias policy URIs collapsed by the server stay collapsed on the client.
    assert INSTANCE + 'p1' not in snapshot.nodes or INSTANCE + 'p2' not in snapshot.nodes
    # Generic hub never re-enters the client graph.
    assert INSTANCE + 'hub' not in snapshot.nodes
    # Seed skeleton merged with 隶属 edges and seed degrees.
    jingan = snapshot.nodes[INSTANCE + 'c85bda1b4459']
    assert jingan['seed'] is True and jingan['label'] == '区级机构'
    assert jingan['degree'] == server.nodes[INSTANCE + 'c85bda1b4459']['degree']
    assert any(e['predicateLabel'] == '隶属' and e.get('seed')
               for e in snapshot.edges.values())
    assert snapshot.normalization == page['normalization']
    # Window provenance stays client-scoped (a_ article ids, not server u_/m_).
    assert all(a.startswith('a_') for a in person['articleIds'])


def test_client_article_view_uses_normalized_key_entities():
    _, page = normalized_page()
    extras = dict(nodes=page['nodes'], seedNodes=page['seedNodes'],
                  seedEdges=page['seedEdges'], normalization=page['normalization'])
    snapshot = snapshot_from_evidence(page['items'], 'policy', graph_extras=extras)
    article = next(iter(snapshot.articles.values()))
    view = article_view(snapshot, article)
    labels = {e['label'] for e in view['keyEntities']}
    assert '领导干部' in labels
    assert '调研视察' not in labels  # relabelled to 行动事项 by the server
    relations = {r['predicateLabel'] for r in view['relations']}
    assert '现任' in relations
    assert '采用' not in relations  # hub edge dropped by the server


def test_legacy_evidence_without_normalization_still_bridges():
    record = dict(id='o_x', evidenceId='oq_x', text='原文。', title='社区治理观察',
                  publishedDate='2026-11-20', boards=['社区治理'],
                  contentTypes=['news'], qualityFlags=[],
                  sources=[dict(title='社区治理观察', meta=dict(
                      canonicalUrl='https://example.org/x', publisher='示例'))],
                  entities=[dict(uri=INSTANCE + 'a', name='临汾路街道', label='街镇机构')],
                  relations=[])
    snapshot = snapshot_from_evidence([record], 'policy')
    node = snapshot.nodes[INSTANCE + 'a']
    assert node['label'] == '街镇机构' and node['degree'] == 0
    assert snapshot.normalization is None
