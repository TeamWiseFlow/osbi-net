"""Required v4 link roles, global selection policy, cache reuse and date gates."""
import asyncio
import json
import logging

from fastapi.testclient import TestClient
import pytest

from core.async_database import AsyncDatabaseManager
from osbi_contracts.material import material_hash
from osbi_contracts.models import Material
from test_relevance_window import FakeQueue, NOW, make_service, request
from wiseflow_server.app import create_app
from wiseflow_server.link_selection import refine_link_selection
from wiseflow_server.metrics import NodeSourceMetrics
from wiseflow_server.models import Link
from wiseflow_server.ontology_queue import OntologyQueue


def links_for(*roles):
    return [{'id': f'L{i}', 'text': f'Candidate {i}', 'role': role}
            for i, role in enumerate(roles, 1)]


def page(links, **overrides):
    data = dict(channel='community', input={
        'title': 'Community updates', 'blocks': [{'id': 'B1', 'text': 'Original body.'}],
        'links': links})
    data.update(overrides)
    return request(**data)


def selected_chat(link_ids=None, seen=None):
    async def chat(messages, model):
        if seen is not None:
            seen.append(messages)
        material = json.loads(messages[1]['content'])['material']
        return json.dumps({'blockIds': [block['id'] for block in material['blocks']],
                           'linkIds': link_ids if link_ids is not None else
                           [link['id'] for link in material['links']]})
    return chat


class MemoryCache:
    def __init__(self):
        self.values = {}

    async def get(self, key, *, namespace):
        return self.values.get((namespace, key))

    async def set(self, key, value, ttl, *, namespace):
        self.values[namespace, key] = value


@pytest.mark.parametrize('role', ['missing', None, 'invalid'])
def test_extract_http_rejects_missing_or_invalid_link_roles(role, monkeypatch):
    monkeypatch.delenv('OSBI_SERVER_TOKEN', raising=False)
    seen = []
    service = make_service(selected_chat(seen=seen), FakeQueue())
    payload = page(links_for('unknown')).model_dump()
    if role == 'missing':
        payload['input']['links'][0].pop('role')
    else:
        payload['input']['links'][0]['role'] = role
    with TestClient(create_app(service=service)) as client:
        response = client.post('/v1/materials/extract', json=payload)
    assert response.status_code == 422
    assert response.json()['detail'][0]['loc'] == ['body', 'input', 'links', 0, 'role']
    assert seen == [] and service.queue.enqueued == []


@pytest.mark.parametrize(('channel', 'mode'), [
    ('community', 'both'), ('policy-latest', 'both'), ('policy-latest', 'only_link')])
def test_all_link_roles_reach_prompt_with_explicit_selection_guidance(channel, mode):
    seen = []
    service = make_service(selected_chat(seen=seen), FakeQueue())
    response = asyncio.run(service.extract(page(
        links_for('navigation', 'content', 'unknown'), channel=channel, mode=mode), now=NOW))
    assert response.selection.linkIds == ['L2', 'L3', 'L1']
    payload = json.loads(seen[0][1]['content'])
    assert [link['role'] for link in payload['material']['links']] == [
        'navigation', 'content', 'unknown']
    system = seen[0][0]['content']
    assert 'content:' in system and 'unknown:' in system and 'navigation:' in system
    assert 'necessary' in system and 'publication date is missing' in system
    assert 'Role is classification metadata' in system


@pytest.mark.parametrize(('roles', 'model_order', 'expected', 'dropped', 'fallback'), [
    (['navigation', 'content', 'unknown', 'navigation'],
     ['L1', 'L2', 'L3', 'L4'], ['L2', 'L3', 'L1'], 1, 0),
    (['navigation'] * 4, ['L4', 'L2', 'L3', 'L1'], ['L4', 'L2', 'L3'], 1, 1),
    (['content', 'unknown', 'content'], ['L2', 'L3', 'L1'], ['L2', 'L3', 'L1'], 0, 0),
    (['navigation', 'content', 'content', 'unknown', 'unknown', 'navigation'],
     ['L6', 'L3', 'L5', 'L1', 'L2', 'L4'], ['L3', 'L2', 'L5', 'L4', 'L6'], 1, 0),
    (['navigation'], ['L1'], ['L1'], 0, 1),
    ([], [], [], 0, 0),
])
def test_role_order_and_caps(roles, model_order, expected, dropped, fallback):
    links = [Link.model_validate(link) for link in links_for(*roles)]
    actual, counters = refine_link_selection(model_order, links)
    assert actual == expected
    assert counters['navigation_nav_cap_dropped'] == dropped
    assert counters['navigation_only_fallback'] == fallback
    for role in ('content', 'unknown', 'navigation'):
        assert counters[f'selected_{role}_links'] == sum(
            link.role == role and link.id in expected for link in links)


def test_deduplication_prefers_content_then_unknown_and_preserves_distinct_contexts():
    links = [Link.model_validate(link) for link in [
        {'id': 'L1', 'text': ' NEWS\tUpdate ', 'context': ' Main\nSection ', 'role': 'navigation'},
        {'id': 'L2', 'text': 'news update', 'context': 'main section', 'role': 'unknown'},
        {'id': 'L3', 'text': 'ＮＥＷＳ update', 'context': 'main section', 'role': 'content'},
        {'id': 'L4', 'text': 'news update', 'context': 'other section', 'role': 'unknown'},
        {'id': 'L5', 'text': 'Another item', 'role': 'navigation'},
        {'id': 'L6', 'text': ' Another item ', 'context': ' ', 'role': 'unknown'},
        {'id': 'L7', 'text': 'news update', 'context': 'main section', 'role': 'content'},
    ]]
    ids, counters = refine_link_selection(['L1', 'L2', 'L4', 'L5', 'L6', 'L3', 'L7', 'L3'], links)
    assert ids == ['L4', 'L6', 'L3']
    assert counters == dict(selected_content_links=1, selected_unknown_links=2,
                            selected_navigation_links=0, navigation_nav_cap_dropped=0,
                            navigation_only_fallback=0)


def test_deduplication_never_promotes_unselected_candidates():
    links = [Link(id='L1', text='Same', role='navigation'),
             Link(id='L2', text='Same', role='content')]
    ids, counters = refine_link_selection(['L1'], links)
    assert ids == ['L1'] and counters['navigation_only_fallback'] == 1


@pytest.mark.parametrize('channel', ['community', 'policy-latest'])
def test_worker_keeps_model_order_and_original_block_queue(channel):
    queue = FakeQueue()
    service = make_service(selected_chat(['L4', 'L3', 'L2', 'L1']), queue)
    payload = page(links_for('content', 'unknown', 'content', 'unknown'),
                   channel=channel, meta={'publishedAt': '2026-11-22'})
    response = asyncio.run(service.extract(payload, now=NOW))
    assert response.selection.linkIds == ['L4', 'L3', 'L2', 'L1']
    assert response.selection.blockIds == ['B1']
    assert [job.blockId for job in response.ontologyJobs] == ['B1']
    assert [item[0] for item in queue.enqueued] == ['B1']
    assert response.errors == []


@pytest.mark.parametrize(('roles', 'expected'), [
    (['navigation'] * 5, ['L1', 'L2', 'L3']),
    (['navigation', 'navigation', 'navigation', 'unknown', 'content'], ['L5', 'L4', 'L1']),
])
def test_navigation_caps_apply_globally_across_relevance_batches(roles, expected):
    seen = []
    links = links_for(*roles)
    for link in links:
        link['context'] = 'Long context. ' * 550
    queue = FakeQueue()
    service = make_service(selected_chat(seen=seen), queue)
    response = asyncio.run(service.extract(page(links), now=NOW))
    assert len(seen) > 1
    assert response.selection.linkIds == expected
    assert response.selection.blockIds == ['B1']
    assert len(queue.enqueued) == 1 and len(response.ontologyJobs) == 1


def test_cache_keeps_uncapped_model_selection_and_reapplies_current_roles(caplog):
    async def run():
        service = make_service(selected_chat(['L4', 'L2', 'L3', 'L1']), FakeQueue())
        service.cache = MemoryCache()
        payload = page(links_for(*(['navigation'] * 4)))
        with caplog.at_level(logging.INFO, logger='wiseflow_server.worker'):
            first = await service.extract(payload, now=NOW)
            payload.input.links[0].role = 'content'
            mixed = await service.extract(payload, now=NOW)
            for link in payload.input.links:
                link.role = 'unknown'
            last = await service.extract(payload, now=NOW)
        assert first.selection.linkIds == ['L4', 'L2', 'L3']
        assert mixed.selection.linkIds == ['L1', 'L4']
        assert last.selection.linkIds == ['L4', 'L2', 'L3', 'L1']
        assert [first.modelCalls, mixed.modelCalls, last.modelCalls] == [1, 0, 0]
        assert len(service.cache.values) == 1
        assert next(iter(service.cache.values.values()))['linkIds'] == ['L4', 'L2', 'L3', 'L1']
        records = [record for record in caplog.records if hasattr(record, 'navigation_only_fallback')]
        assert [record.navigation_only_fallback for record in records] == [1, 0, 0]
        assert [record.navigation_nav_cap_dropped for record in records] == [1, 2, 0]
        assert records[-1].selected_unknown_links == 4
    asyncio.run(run())


@pytest.mark.parametrize('role', ['content', 'unknown', 'navigation'])
@pytest.mark.parametrize(('meta', 'reason'), [({}, 'date_unknown'),
                                            ({'publishedAt': '2020-01-01'}, 'date_window')])
def test_block_date_rejection_keeps_links_for_every_role(role, meta, reason):
    queue = FakeQueue()
    service = make_service(selected_chat(), queue)
    response = asyncio.run(service.extract(page(links_for(role), channel='policy-latest',
                                                meta=meta), now=NOW))
    assert response.selection.blockIds == [] and response.selection.linkIds == ['L1']
    assert [(error.stage, error.blockId, error.message) for error in response.errors] == [
        ('date_gate', 'B1', reason)]
    assert response.ontologyJobs == [] and queue.enqueued == []


def test_link_date_gate_runs_before_navigation_quota(caplog):
    links = links_for('content', 'navigation', 'navigation', 'navigation', 'navigation')
    links[0]['context'] = '发布日期：2020-01-01'
    links[1]['context'] = '发布日期：2020-01-01'
    service = make_service(selected_chat(), FakeQueue())
    with caplog.at_level(logging.INFO, logger='wiseflow_server.worker'):
        response = asyncio.run(service.extract(page(links, channel='policy-latest'), now=NOW))
    assert response.selection.linkIds == ['L3', 'L4', 'L5']
    record = next(record for record in caplog.records if hasattr(record, 'navigation_only_fallback'))
    assert record.selected_navigation_links == 3 and record.navigation_nav_cap_dropped == 0
    assert record.navigation_only_fallback == 1


def test_role_changes_preserve_material_hash_and_ontology_queue_identity(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            queue = OntologyQueue(db)
            service = make_service(selected_chat(), FakeQueue())
            ids, hashes = [], []
            for role in ('navigation', 'unknown', 'content'):
                payload = page(links_for(role))
                material = Material(clientId='client', requestId='request', channel=payload.channel,
                    title=payload.input.title, blocks=[block.model_dump() for block in payload.input.blocks],
                    links=[dict(payload.input.links[0].model_dump(), url='https://example.org/article')],
                    meta=payload.meta)
                hashes.append(material_hash(material))
                receipt = await queue.enqueue(payload, payload.input.blocks[0],
                    channel=service.config.channel(payload.channel), library_id='policy')
                ids.append(receipt.id)
            assert len(set(hashes)) == len(set(ids)) == 1
        finally:
            await db.cleanup()
    asyncio.run(run())


def test_selection_quality_metrics_match_returned_links_including_cache_hits(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            service = make_service(selected_chat(), FakeQueue())
            service.metrics = NodeSourceMetrics(db)
            service.cache = MemoryCache()
            payload = page(links_for('navigation', 'content', 'unknown', 'navigation'))
            first = await service.extract(payload, now=NOW)
            second = await service.extract(payload, now=NOW)
            assert first.selection.linkIds == second.selection.linkIds == ['L2', 'L3', 'L1']
            row, = await service.metrics.query(payload.nodeId)
            assert row['selected_links'] == 6 and row['selected_blocks'] == 2
            assert row['selected_content_links'] == row['selected_unknown_links'] == 2
            assert row['selected_navigation_links'] == row['navigation_nav_cap_dropped'] == 2
            assert row['navigation_only_fallback'] == 0
        finally:
            await db.cleanup()
    asyncio.run(run())


def test_empty_selection_logs_zero_quality_without_node(caplog):
    service = make_service(selected_chat([]), FakeQueue())
    with caplog.at_level(logging.INFO, logger='wiseflow_server.worker'):
        response = asyncio.run(service.extract(page([], nodeId=None), now=NOW))
    assert response.selection.linkIds == []
    record = next(record for record in caplog.records if hasattr(record, 'navigation_only_fallback'))
    assert record.selected_content_links == record.selected_unknown_links == 0
    assert record.selected_navigation_links == record.navigation_nav_cap_dropped == 0
    assert record.navigation_only_fallback == 0


def test_metrics_failure_never_gates_selected_links_or_block_queue():
    async def fail(*args, **kwargs):
        raise RuntimeError('Metrics unavailable')
    service = make_service(selected_chat(), FakeQueue())
    service.metrics.record_selection = fail
    response = asyncio.run(service.extract(page(links_for('navigation', 'content')), now=NOW))
    assert response.selection.linkIds == ['L2', 'L1']
    assert response.selection.blockIds == ['B1'] and len(response.ontologyJobs) == 1
    assert response.errors == []


def test_failed_selection_logs_zero_counts_and_preserves_error_response(caplog):
    async def fail(*args, **kwargs):
        raise ValueError('Selection unavailable')
    service = make_service(selected_chat(), FakeQueue())
    service._selection = fail
    with caplog.at_level(logging.INFO, logger='wiseflow_server.worker'):
        response = asyncio.run(service.extract(page(links_for('navigation')), now=NOW))
    assert response.selection.linkIds == [] and response.selection.blockIds == []
    assert response.errors[0].model_dump() == dict(
        stage='selection', blockId=None, message='Selection unavailable')
    assert response.ontologyJobs == [] and service.queue.enqueued == []
    record = next(record for record in caplog.records if hasattr(record, 'navigation_only_fallback'))
    assert record.selected_content_links == record.selected_unknown_links == 0
    assert record.selected_navigation_links == record.navigation_nav_cap_dropped == 0
    assert record.navigation_only_fallback == 0
