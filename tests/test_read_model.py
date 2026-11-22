"""Server-side read-model normalization ported from production ontology_query.

Covers the seed skeleton merge, alias/label/hub normalization, the evidence
overlay and the graph/query view mapping. No model calls, no Oxigraph writes.
"""
import json
from pathlib import Path

import pytest

from wiseflow_server.read_model import (SEED_FILE, Snapshot, build_snapshot,
                                        cached_snapshot, merge_seed_graph,
                                        node_view, seed_view)
from wiseflow_server.read_normalize import normalize_snapshot
from wiseflow_server.evidence import EvidenceQuery, evidence_page
from wiseflow_server.read_graph import normalize_graph_data

NS = 'https://onto.pilot.local/policy#'
INSTANCE = 'https://onto.pilot.local/instance/'


def block_result(block_id, *, title='静安区发布促进就业实施方案',
                 url='https://example.org/p1', published='2026-11-20',
                 entities=(), relations=(), raw_text='政策原文。',
                 content_type='policy', board='政策速递', publisher='静安区人民政府',
                 material_id=''):
    return dict(
        id=block_id, schemaId='policy-v12',
        provenance=dict(source_title=title, source_url=url,
                        published_date=published, board_label=board,
                        content_type=content_type, raw_text=raw_text,
                        material_id=material_id,
                        source_meta=dict(publisher=publisher,
                                         canonicalUrl=url, capturedAt=published)),
        entities=[dict(uri=uri, text=name, label=label) for uri, name, label in entities],
        relations=[dict(subject=s, predicate=NS + p, object=o, label=p)
                   for s, p, o in relations])


def test_seed_file_ships_with_server():
    assert SEED_FILE.is_file()
    assert '临汾路街道' in SEED_FILE.read_text(encoding='utf-8')


def test_merge_seed_graph_adds_skeleton_and_degrees():
    snapshot = build_snapshot('policy', [block_result(
        'o_a', entities=[(INSTANCE + 'c85bda1b4459', '静安区', '区级机构')],
        relations=[])], include_seeds=False)
    added = merge_seed_graph(snapshot)
    assert added > 50
    jingan = snapshot.nodes[INSTANCE + 'c85bda1b4459']
    assert jingan['seed'] is True
    assert jingan['label'] == '区级机构'
    # partOf chain reaches 上海市 and 国家 with seed edges.
    part_of = [e for e in snapshot.edges.values() if e['predicateLabel'] == '隶属']
    assert any(e['subject'] == INSTANCE + 'c85bda1b4459' for e in part_of)
    assert all(e.get('seed') for e in part_of)
    assert jingan['degree'] >= 1
    # Seed facts never count as articles.
    assert len(snapshot.articles) == 1


def test_alias_policy_nodes_merge_and_keep_versions_separate():
    results = [
        block_result('o_a', entities=[
            (INSTANCE + 'p1', '《静安区促进就业实施方案》', '区级政策'),
            (INSTANCE + 'p2', '静安区促进就业实施方案', '区级政策'),
            (INSTANCE + 'p3', '静安区促进就业实施方案（2024—2026年）', '区级政策')],
            relations=[(INSTANCE + 'p1', '依据', INSTANCE + 'p2')]),
    ]
    snapshot = build_snapshot('policy', results)
    # 《X》 and bare X merge; the sole year-range variant absorbs them.
    merged = [n for n in snapshot.nodes.values() if n.get('mergedUris')]
    assert merged and len(merged[0]['mergedUris']) == 2
    assert snapshot.normalization['mergedNodes'] == 2
    # The remapped 依据 edge collapsed onto the representative (self-edge kept
    # only if production keeps it; endpoints must exist either way).
    assert all(e['subject'] in snapshot.nodes and e['object'] in snapshot.nodes
               for e in snapshot.edges.values())


def test_draft_marker_stays_separate_and_blocks_year_absorption():
    results = [
        block_result('o_a', entities=[
            (INSTANCE + 'p1', '《静安区促进就业实施方案》', '区级政策'),
            (INSTANCE + 'p2', '静安区促进就业实施方案', '区级政策'),
            (INSTANCE + 'p3', '静安区促进就业实施方案（2024—2026年）', '区级政策'),
            (INSTANCE + 'p4', '静安区促进就业实施方案（征求意见稿）', '政策征求意见稿')]),
    ]
    snapshot = build_snapshot('policy', results)
    names = {n['name'] for n in snapshot.nodes.values()}
    assert '静安区促进就业实施方案（征求意见稿）' in names
    # Two non-empty versions exist (year range + draft): production keeps the
    # bare reference group separate instead of guessing which one it means.
    assert INSTANCE + 'p3' in snapshot.nodes
    merged = [n for n in snapshot.nodes.values() if n.get('mergedUris')]
    assert merged and len(merged[0]['mergedUris']) == 1


def test_relabel_inspection_topic_and_official_from_post():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'insp', '社区环境专项整顿工作', '调研视察'),
        (INSTANCE + 'person', '张三', '人物'),
        (INSTANCE + 'post', '静安区民政局局长', '职务'),
        (INSTANCE + 'badpost', '中心主任', '职务')],
        relations=[(INSTANCE + 'person', '现任', INSTANCE + 'post')])]
    snapshot = build_snapshot('policy', results)
    assert snapshot.nodes[INSTANCE + 'insp']['label'] == '行动事项'
    assert snapshot.nodes[INSTANCE + 'person']['label'] == '领导干部'
    assert snapshot.nodes[INSTANCE + 'post']['qualified'] is True
    assert snapshot.nodes[INSTANCE + 'badpost']['qualified'] is False


def test_generic_hubs_dropped_and_degrees_recomputed():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'hub', '党建引领', '治理机制'),
        (INSTANCE + 'org', '临汾路街道办', '街镇机构')],
        relations=[(INSTANCE + 'org', '采用', INSTANCE + 'hub')])]
    snapshot = build_snapshot('policy', results)
    assert INSTANCE + 'hub' not in snapshot.nodes
    assert snapshot.normalization['droppedGenericNodes'] >= 1
    org = snapshot.nodes[INSTANCE + 'org']
    # The hub edge is gone; degree reflects only surviving edges.
    assert all(e['subject'] != INSTANCE + 'hub' and e['object'] != INSTANCE + 'hub'
               for e in snapshot.edges.values())
    assert org['degree'] == sum(
        1 for e in snapshot.edges.values()
        if INSTANCE + 'org' in (e['subject'], e['object']))


def test_cached_snapshot_rebuilds_only_when_receipts_change():
    entries = [('job1', block_result('o_a'))]
    first = cached_snapshot('policy', entries)
    again = cached_snapshot('policy', entries)
    assert first is again
    changed = cached_snapshot('policy', [('job2', block_result('o_b'))])
    assert changed is not first


def test_evidence_page_overlay_returns_normalized_entities_and_seeds():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'p1', '《静安区促进就业实施方案》', '区级政策'),
        (INSTANCE + 'p2', '静安区促进就业实施方案', '区级政策'),
        (INSTANCE + 'person', '张三', '人物'),
        (INSTANCE + 'post', '静安区民政局局长', '职务')],
        relations=[(INSTANCE + 'p1', '依据', INSTANCE + 'p2'),
                   (INSTANCE + 'person', '现任', INSTANCE + 'post')])]
    snapshot = build_snapshot('policy', results)
    entries = [('job1', results[0])]
    page = evidence_page(entries, EvidenceQuery(), library_id='policy',
                         snapshot=snapshot)
    assert page['normalized'] is True
    item = page['items'][0]
    uris = {e['uri'] for e in item['entities']}
    # Alias URI collapsed onto one representative.
    assert len([u for u in uris if u in (INSTANCE + 'p1', INSTANCE + 'p2')]) == 1
    person = next(e for e in item['entities'] if e['uri'] == INSTANCE + 'person')
    assert person['label'] == '领导干部'
    assert person['degree'] >= 1
    rel = next(r for r in item['relations'] if r['label'] == '现任')
    assert rel['subjectText'] == '张三' and rel['objectText'] == '静安区民政局局长'
    # Seed skeleton rides along for the client aggregation.
    seed_uris = {n['uri'] for n in page['seedNodes']}
    assert INSTANCE + 'c85bda1b4459' in seed_uris
    assert all(n.get('seed') for n in page['seedNodes'])
    assert any(e['predicateLabel'] == '隶属' for e in page['seedEdges'])
    assert page['normalization']['mergedNodes'] >= 1


def test_evidence_page_without_snapshot_keeps_legacy_shape():
    entries = [('job1', block_result('o_a', entities=[
        (INSTANCE + 'x', '某机构', '机构组织')]))]
    page = evidence_page(entries, EvidenceQuery(), library_id='policy')
    assert 'normalized' not in page
    assert page['items'][0]['entities'][0]['uri'] == INSTANCE + 'x'


def test_normalize_graph_data_maps_aliases_drops_hubs_adds_seeds():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'p1', '《静安区促进就业实施方案》', '区级政策'),
        (INSTANCE + 'p2', '静安区促进就业实施方案', '区级政策'),
        (INSTANCE + 'hub', '党建引领', '治理机制'),
        (INSTANCE + 'org', '临汾路街道办', '街镇机构')],
        relations=[(INSTANCE + 'p1', '依据', INSTANCE + 'p2'),
                   (INSTANCE + 'org', '采用', INSTANCE + 'hub')])]
    snapshot = build_snapshot('policy', results)
    data = dict(
        nodes=[dict(id=uri, name=name, cls=cls, nblocks=1, up=[], sources=[],
                    out_edges=0, in_edges=0)
               for uri, name, cls in [
                   (INSTANCE + 'p1', '《静安区促进就业实施方案》', 'DistrictPolicy'),
                   (INSTANCE + 'p2', '静安区促进就业实施方案', 'DistrictPolicy'),
                   (INSTANCE + 'hub', '党建引领', 'Mechanism'),
                   (INSTANCE + 'org', '临汾路街道办', 'StreetGov')]],
        edges=[dict(source=INSTANCE + 'p1', target=INSTANCE + 'p2', pred='依据'),
               dict(source=INSTANCE + 'org', target=INSTANCE + 'hub', pred='采用')],
        stats={}, type_conflicts=0, dangling=0)
    view = normalize_graph_data(data, snapshot)
    ids = {n['id'] for n in view['nodes']}
    assert INSTANCE + 'hub' not in ids
    assert len([i for i in ids if i in (INSTANCE + 'p1', INSTANCE + 'p2')]) == 1
    assert INSTANCE + 'c85bda1b4459' in ids  # seed skeleton present
    assert all(e['source'] in ids and e['target'] in ids for e in view['edges'])
    assert any(e['pred'] == 'partOf' for e in view['edges'])
    rep = next(n for n in view['nodes']
               if n['id'] in (INSTANCE + 'p1', INSTANCE + 'p2'))
    assert rep.get('seed') is not True
    seed = next(n for n in view['nodes'] if n['id'] == INSTANCE + 'c85bda1b4459')
    assert seed['seed'] is True and seed['cls'] == 'DistrictGov'


def test_node_view_is_json_serializable():
    snapshot = build_snapshot('policy', [block_result('o_a')])
    payload = json.dumps(seed_view(snapshot), ensure_ascii=False)
    assert '临汾路街道' in payload
    view = node_view(next(iter(snapshot.nodes.values())))
    assert view['uri'] and 'degree' in view


def test_evidence_overlay_drops_hub_edges_but_keeps_dangling_endpoints():
    results = [block_result('o_a', entities=[
        (INSTANCE + 'org', '临汾路街道办', '街镇机构'),
        (INSTANCE + 'hub', '党建引领', '治理机制')],
        relations=[(INSTANCE + 'org', '采用', INSTANCE + 'hub'),
                   (INSTANCE + 'org', '发布', INSTANCE + 'dangling')])]
    snapshot = build_snapshot('policy', results)
    page = evidence_page([('job1', results[0])], EvidenceQuery(),
                         library_id='policy', snapshot=snapshot)
    item = page['items'][0]
    assert all(e['uri'] != INSTANCE + 'hub' for e in item['entities'])
    labels = {r['label'] for r in item['relations']}
    assert '采用' not in labels       # hub edge removed
    assert '发布' in labels           # dangling endpoint kept
    rel = next(r for r in item['relations'] if r['label'] == '发布')
    assert rel['object'] == INSTANCE + 'dangling'
