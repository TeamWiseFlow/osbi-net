"""Evidence query endpoint and optional bearer auth for wiseflow_server.

Pure-function tests build fake block results (the exact shape of
OntologyStore.read); route tests inject a fake service into create_app so no
lifespan, database, Oxigraph handle or model API is needed.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from wiseflow_server.app import create_app
from wiseflow_server.evidence import EvidenceQuery, evidence_page

URI_A = 'https://onto.pilot.local/instance/aaa'
URI_B = 'https://onto.pilot.local/instance/bbb'


def oid(char):
    return 'o_' + char * 64


def block_result(extraction_id, *, title='某政策发布', text='甲部门发布某办法。',
                 published='2026-11-15', board_label='政策速递', channel='policy-latest',
                 content_type='policy', quality_flags=None, entities=None,
                 relations=None, superseded_by=None, meta=None, legacy=False,
                 node_ids=('linfen-shanghai',)):
    entities = entities if entities is not None else [
        dict(uri=URI_A, text='甲部门', label='政府机构（含党组织）')]
    relations = relations if relations is not None else [
        dict(subject=URI_A, predicate='https://onto.pilot.local/policy#发布',
             object=URI_B, label='发布', subjectText='甲部门', objectText='某办法')]
    provenance = dict(material_id='m' * 64, block_id='B1',
                      channel=channel, channel_title=board_label,
                      node_ids=list(node_ids),
                      source_url='https://example.org/x',
                      source_title=title, raw_text=text,
                      source_meta=meta if meta is not None else {'publisher': '某发布号'},
                      published_date=published,
                      date_source='meta' if published else 'unknown')
    if not legacy:
        provenance.update(content_type=content_type,
                          quality_flags=quality_flags if quality_flags is not None else [])
    elif quality_flags is not None:
        provenance['quality_flags'] = quality_flags
    result = dict(id=extraction_id, schemaId='schema-1', entities=entities,
                  relations=relations, provenance=provenance, statistics={})
    if superseded_by:
        result['supersededBy'] = superseded_by
    return result


def query(**overrides):
    return EvidenceQuery.model_validate(overrides)


# --------------------------------------------------------------------------
# Pure filtering / paging logic
# --------------------------------------------------------------------------

def test_empty_entries_return_empty_page():
    page = evidence_page([], query(), library_id='policy')
    assert page['items'] == []
    assert page['cursor'] == ''
    assert page['asOf']


def test_item_structure():
    # Arrange
    result = block_result(oid('a'))
    # Act
    page = evidence_page([('oq_1', result)], query(), library_id='policy')
    # Assert
    item = page['items'][0]
    assert set(item) == {'id', 'evidenceId', 'libraryId', 'channel', 'board',
                         'text', 'title', 'publishedDate', 'dateSource', 'boards',
                         'contentTypes', 'qualityFlags', 'entityCount', 'relationCount',
                         'sources', 'entities', 'relations'}
    assert item['id'] == oid('a')
    assert item['evidenceId'] == 'oq_1'
    assert item['libraryId'] == 'policy'
    assert item['channel'] == 'policy-latest'
    assert item['board'] == '政策速递'
    assert item['text'] == '甲部门发布某办法。'
    assert item['boards'] == ['政策速递']
    assert item['dateSource'] == 'meta'
    assert item['contentTypes'] == ['policy']
    assert item['entityCount'] == 1 and item['relationCount'] == 1
    source = item['sources'][0]
    assert source['nodeId'] == 'linfen-shanghai'
    assert source['url'] == 'https://example.org/x'
    assert source['publisher'] == '某发布号'
    assert source['title'] == '某政策发布'
    assert source['meta'] == {'publisher': '某发布号'}
    assert item['entities'] == [{'uri': URI_A, 'name': '甲部门', 'label': '政府机构（含党组织）'}]
    assert item['relations'] == [{'subject': URI_A, 'object': URI_B, 'label': '发布'}]


def test_date_window_is_inclusive_and_drops_undated():
    entries = [('j1', block_result(oid('a'), published='2026-11-10')),
               ('j2', block_result(oid('b'), published='2026-11-15')),
               ('j3', block_result(oid('c'), published='2026-11-20')),
               ('j4', block_result(oid('d'), published=''))]
    page = evidence_page(entries, query(fromDate='2026-11-10', toDate='2026-11-15'),
                         library_id='policy')
    assert {i['id'] for i in page['items']} == {oid('a'), oid('b')}
    # Without a window, undated evidence stays available.
    page = evidence_page(entries, query(), library_id='policy')
    assert oid('d') in {i['id'] for i in page['items']}


def test_boards_filter_uses_channel_title():
    entries = [('j1', block_result(oid('a'), board_label='政策速递')),
               ('j2', block_result(oid('b'), board_label='经济动态', channel='economy')),
               # A block without any channel title has no board to match.
               ('j3', block_result(oid('c'), board_label='', legacy=True))]
    page = evidence_page(entries, query(boards=['经济动态']), library_id='economy')
    assert {i['id'] for i in page['items']} == {oid('b')}


def test_content_types_filter_and_legacy_fallback():
    entries = [('j1', block_result(oid('a'), content_type='policy')),
               ('j2', block_result(oid('b'), content_type='digest')),
               # Legacy block without content_type: digest title classifies.
               ('j3', block_result(oid('c'), title='一周经济动态', legacy=True))]
    page = evidence_page(entries, query(contentTypes=['digest']), library_id='policy')
    assert {i['id'] for i in page['items']} == {oid('b'), oid('c')}


def test_exclude_quality_and_legacy_flag_fallback():
    entries = [('j1', block_result(oid('a'), quality_flags=['digest'])),
               ('j2', block_result(oid('b'), quality_flags=[])),
               # Legacy block without quality_flags: undated => no_date flag.
               ('j3', block_result(oid('c'), published='', legacy=True))]
    page = evidence_page(entries, query(excludeQuality=['digest', 'no_date']),
                         library_id='policy')
    assert {i['id'] for i in page['items']} == {oid('b')}


def test_entity_uris_intersection():
    other = [dict(uri=URI_B, text='乙公司', label='企业')]
    entries = [('j1', block_result(oid('a'))),
               ('j2', block_result(oid('b'), entities=other, relations=[]))]
    page = evidence_page(entries, query(entityUris=[URI_B]), library_id='policy')
    assert {i['id'] for i in page['items']} == {oid('b')}


def test_text_matches_title_and_body_casefold():
    entries = [('j1', block_result(oid('a'), title='GDP 数据发布', text='正文甲')),
               ('j2', block_result(oid('b'), title='标题乙', text='包含 gdp 的正文')),
               ('j3', block_result(oid('c'), title='标题丙', text='无关正文'))]
    page = evidence_page(entries, query(text='GdP'), library_id='policy')
    assert {i['id'] for i in page['items']} == {oid('a'), oid('b')}


def test_default_sort_is_date_then_relation_then_entity_desc():
    entries = [
        ('j1', block_result(oid('a'), published='2026-11-15', relations=[])),
        ('j2', block_result(oid('b'), published='2026-11-15')),
        ('j3', block_result(oid('c'), published='2026-11-20', relations=[])),
        ('j4', block_result(oid('d'), published='2026-11-15',
                            relations=[dict(subject=URI_A, predicate='p', object=URI_B, label='x'),
                                       dict(subject=URI_B, predicate='p', object=URI_A, label='y')])),
    ]
    page = evidence_page(entries, query(), library_id='policy')
    assert [i['id'] for i in page['items']] == [oid('c'), oid('d'), oid('b'), oid('a')]


def test_limit_and_cursor_pagination():
    entries = [('j1', block_result(oid('a'), published='2026-11-20')),
               ('j2', block_result(oid('b'), published='2026-11-19')),
               ('j3', block_result(oid('c'), published='2026-11-18'))]
    first = evidence_page(entries, query(limit=2), library_id='policy')
    assert [i['id'] for i in first['items']] == [oid('a'), oid('b')]
    assert first['cursor'] == 'j2'
    second = evidence_page(entries, query(limit=2, cursor=first['cursor']), library_id='policy')
    assert [i['id'] for i in second['items']] == [oid('c')]
    assert second['cursor'] == ''


def test_unknown_cursor_raises():
    with pytest.raises(ValueError):
        evidence_page([('j1', block_result(oid('a')))], query(cursor='missing'),
                      library_id='policy')


def test_superseded_and_duplicate_blocks_are_skipped():
    entries = [('j1', block_result(oid('a'), superseded_by=oid('b'))),
               ('j2', block_result(oid('b'))),
               ('j3', block_result(oid('b')))]
    page = evidence_page(entries, query(), library_id='policy')
    assert [i['id'] for i in page['items']] == [oid('b')]


def test_limit_is_capped_at_200():
    with pytest.raises(ValidationError):
        query(limit=201)
    with pytest.raises(ValidationError):
        query(limit=0)


# --------------------------------------------------------------------------
# HTTP route with an injected fake service
# --------------------------------------------------------------------------

class FakeStore:
    def __init__(self, results):
        self.results = results

    def read(self, extraction_id):
        return self.results.get(extraction_id)


class FakeQueue:
    def __init__(self, jobs):
        self.jobs = jobs

    async def done_receipts(self, library_id=None):
        return [dict(id=j['id'], receipt=j['receipt']) for j in self.jobs
                if library_id is None or j['library_id'] == library_id]


class FakeLibraries:
    def resolve(self, library_id=None, *, focus_id=None):
        if library_id in (None, 'policy', 'economy'):
            return library_id or 'policy'
        raise ValueError(f'Unknown ontology library: {library_id}')


def fake_service(results=None, jobs=None, engine_missing=False):
    results = results if results is not None else {}
    jobs = jobs if jobs is not None else []
    engine = None if engine_missing else SimpleNamespace(store=FakeStore(results))
    return SimpleNamespace(
        libraries=FakeLibraries(),
        queue=FakeQueue(jobs),
        engine=lambda library_id=None: engine,
        settings=SimpleNamespace(selected_model='sel', performance_model='perf',
                                 ontology_model='onto', fallback_model='fb',
                                 ontology_batch_time='03:00',
                                 ontology_batch_timezone='Asia/Shanghai'),
        db=SimpleNamespace(db_path='/tmp/fake-data.db'),
        ontology_engine=None)


def jobs_for(*pairs):
    return [dict(id=job_id, library_id='policy', receipt={'id': extraction_id})
            for job_id, extraction_id in pairs]


def test_route_returns_evidence_page():
    # Arrange
    result = block_result(oid('a'))
    service = fake_service({oid('a'): result}, jobs_for(('j1', oid('a'))))
    # Act
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/policy/evidence', json={})
    # Assert
    assert response.status_code == 200
    body = response.json()
    # The query side now serves the normalized read model: items plus the
    # seed skeleton, referenced nodes and auditable normalization stats.
    assert set(body) == {'items', 'cursor', 'asOf', 'channel', 'normalized',
                         'normalization', 'nodes', 'seedNodes', 'seedEdges'}
    assert body['normalized'] is True
    assert body['items'][0]['id'] == oid('a')
    assert body['cursor'] == ''
    item = body['items'][0]
    assert item['normalized'] is True
    # Dangling relation endpoint (某办法 was never an entity) keeps its text.
    assert item['relations'][0]['objectText'] == '某办法'
    # Seed skeleton rides along with every page.
    assert any(n['name'] == '临汾路街道' for n in body['seedNodes'])
    assert any(e['predicateLabel'] == '隶属' for e in body['seedEdges'])


def test_route_empty_library_returns_empty_items():
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/economy/evidence', json={})
    assert response.status_code == 200
    assert response.json()['items'] == []


def test_route_applies_filters():
    results = {oid('a'): block_result(oid('a'), published='2026-11-20'),
               oid('b'): block_result(oid('b'), published='2026-10-01')}
    service = fake_service(results, jobs_for(('j1', oid('a')), ('j2', oid('b'))))
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/policy/evidence',
                               json={'fromDate': '2026-11-01'})
    assert response.status_code == 200
    assert [i['id'] for i in response.json()['items']] == [oid('a')]


def test_route_unknown_library_is_404():
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/nope/evidence', json={})
    assert response.status_code == 404


def test_route_missing_engine_is_503():
    service = fake_service(engine_missing=True)
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/policy/evidence', json={})
    assert response.status_code == 503


def test_route_rejects_oversized_limit():
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/libraries/policy/evidence', json={'limit': 500})
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Optional shared-token bearer auth
# --------------------------------------------------------------------------

def test_auth_disabled_when_token_unset(monkeypatch):
    monkeypatch.delenv('OSBI_SERVER_TOKEN', raising=False)
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        assert client.get('/health').status_code == 200
        assert client.post('/v1/libraries/policy/evidence', json={}).status_code == 200


def test_auth_enabled_requires_matching_bearer(monkeypatch):
    monkeypatch.setenv('OSBI_SERVER_TOKEN', 'secret-token')
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        # /health stays exempt.
        assert client.get('/health').status_code == 200
        # Missing / wrong / malformed credentials are rejected.
        assert client.post('/v1/libraries/policy/evidence', json={}).status_code == 401
        assert client.post('/v1/libraries/policy/evidence', json={},
                           headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert client.post('/v1/libraries/policy/evidence', json={},
                           headers={'Authorization': 'Basic secret-token'}).status_code == 401
        # Matching bearer passes.
        assert client.post('/v1/libraries/policy/evidence', json={},
                           headers={'Authorization': 'Bearer secret-token'}).status_code == 200


def test_auth_empty_token_means_disabled(monkeypatch):
    monkeypatch.setenv('OSBI_SERVER_TOKEN', '   ')
    service = fake_service()
    with TestClient(create_app(service=service)) as client:
        assert client.post('/v1/libraries/policy/evidence', json={}).status_code == 200
