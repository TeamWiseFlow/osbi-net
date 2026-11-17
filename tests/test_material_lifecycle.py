"""Independent evidence survives retention; duplicate extraction is coordinated."""
import asyncio
from dataclasses import replace
import json
import os
import subprocess
import sys
import time
import pytest
from fastapi.testclient import TestClient
from osbi_contracts.material import material_hash
from osbi_contracts.models import Material
from osbi_server.admin import reprocess, review, activate
from osbi_server.app import create_app
from osbi_server.config import Settings, RetentionSettings
from osbi_server.maintenance import cleanup
from osbi_server.service import Service
from conftest import Model, material, wait_job


def test_versioned_material_identity_and_hash_verification(shared):
    client, service, model, headers = shared
    first = Material.model_validate(material())
    other = Material.model_validate(material(client='other', request='other', block='elsewhere', url='https://other.example/'))
    assert material_hash(first) == material_hash(other)
    changed = other.model_copy(update={'focus': other.focus.model_copy(update={'focuspoint': 'Different focus'})})
    assert material_hash(first) != material_hash(changed)
    body = first.model_dump()
    body['materialHash'] = '0' * 64
    assert client.post('/v1/materials', headers=headers, json=body).status_code == 422
    assert not model.calls and not service.repo.rows('SELECT * FROM work')
    body['materialHash'] = material_hash(first)
    job = client.post('/v1/materials', headers=headers, json=body).json()
    assert wait_job(client, headers, job['jobId'])['state'] == 'succeeded'
    work = service.repo.one('SELECT payload FROM work')
    request = service.repo.one('SELECT payload FROM requests')
    assert 'blocks' not in json.loads(work['payload'])
    assert 'blocks' not in json.loads(request['payload'])
    assert body['blocks'][0]['text'] not in work['payload'] + request['payload']


def test_distinct_work_first_extracts_same_block_only_once(tmp_path):
    class SlowModel(Model):
        async def __call__(self, messages, model):
            await asyncio.sleep(.02)
            return await super().__call__(messages, model)
    async def run():
        model = SlowModel()
        service = Service(Settings(tmp_path), model)
        try:
            work_ids = []
            for i in range(2):
                body = material(client=f'c{i}', request=f'r{i}', url=f'https://example.org/{i}')
                body['focus']['focuspoint'] = str(i)
                job = service.submit(Material.model_validate(body), {'customer': 'c', 'grants': ['*']})
                work_ids.append(service.repo.one('SELECT work_id FROM requests WHERE id=?', (job['jobId'],))['work_id'])
            for work_id in work_ids:
                await service.select(service.repo.one('SELECT * FROM work WHERE id=?', (work_id,)))
            await asyncio.gather(*(service.extract_graph(service.repo.one('SELECT * FROM work WHERE id=?', (w,))) for w in work_ids))
            assert model.calls.count('ontology_entities') == model.calls.count('ontology_relations') == 1
            assert len(service.repo.rows('SELECT * FROM knowledge_units')) == 1
            assert len(service.repo.rows('SELECT * FROM unit_sources')) == 2
            assert not service.engine('economy').inflight
        finally:
            await service.close()
    asyncio.run(run())


def test_retention_preserves_queries_review_and_reprocess_without_work(shared):
    client, service, _, headers = shared
    job = client.post('/v1/materials', headers=headers, json=material()).json()
    wait_job(client, headers, job['jobId'])
    future = time.time() + 40 * 86400
    result = client.portal.call(lambda: cleanup(service, now=future))
    assert result['counts']['requests'] == result['counts']['work'] == 1
    assert not service.repo.rows('SELECT * FROM work')
    assert not service.repo.rows('SELECT * FROM requests')
    assert client.get('/v1/jobs/' + job['jobId'], headers=headers).status_code == 404
    graph = client.get('/v1/channels/economy/graph', headers=headers).json()
    assert len(graph['nodes']) == 2 and len(graph['edges']) == 1
    assert next(iter(graph['blocks'].values()))['sources'][0]['meta']['requestedUrl']
    evidence = client.post('/v1/channels/economy/evidence', headers=headers, json={}).json()
    assert len(evidence['items']) == 1 and evidence['items'][0]['text'] == material()['blocks'][0]['text']
    assert review(service.repo, 'economy')['samples'][0]['originalBlock']['text']
    jobs = reprocess(service.repo, 'economy', force=True, settings=service.settings)['workIds']
    assert len(jobs) == 1
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        row = service.repo.one('SELECT state FROM work WHERE id=?', (jobs[0],))
        if row['state'] == 'succeeded':
            break
        time.sleep(.02)
    assert row['state'] == 'succeeded'
    assert len(service.repo.sources('economy', service.repo.one('SELECT logical_id FROM knowledge_units')['logical_id'])) == 1


def test_retention_protects_live_work_and_referenced_schema(shared):
    client, service, _, headers = shared
    job = client.post('/v1/materials', headers=headers, json=material()).json()
    wait_job(client, headers, job['jobId'])
    old = service.repo.channel('economy')['schema_id']
    text = service.repo.one('SELECT body FROM schema_revisions WHERE id=?', (old,))['body']
    new = activate(service.repo, 'economy', text + '\n# Revised schema.\n', old)['activeSchemaId']
    service.repo.execute('UPDATE schema_revisions SET created=0')
    client.portal.call(lambda: cleanup(service, now=time.time() + 40 * 86400))
    assert service.repo.one('SELECT 1 FROM schema_revisions WHERE id=?', (old,))
    assert service.repo.one('SELECT 1 FROM schema_revisions WHERE id=?', (new,))
    pending = service.submit(Material.model_validate(material(request='pending', text='A new original block.')), {'customer': 'c', 'grants': ['*']})
    work_id = service.repo.one('SELECT work_id FROM requests WHERE id=?', (pending['jobId'],))['work_id']
    service.repo.execute("UPDATE work SET state='running',lease=?,created=0,updated=0 WHERE id=?", (time.time() + 600, work_id))
    service.repo.execute('UPDATE requests SET created=0 WHERE id=?', (pending['jobId'],))
    client.portal.call(lambda: cleanup(service, now=time.time() + 40 * 86400))
    assert service.repo.one('SELECT 1 FROM work WHERE id=?', (work_id,))
    assert service.repo.one('SELECT 1 FROM requests WHERE id=?', (pending['jobId'],))


def test_legacy_evidence_migration_and_restart(tmp_path):
    settings = Settings(tmp_path)
    service = Service(settings, Model())
    token = service.repo.issue_key('c', time.time() + 1000)['key']
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(service=service), base_url='https://testserver') as client:
        body = Material.model_validate(material())
        job = client.post('/v1/materials', headers=headers, json=body.model_dump()).json()
        wait_job(client, headers, job['jobId'])
        original = service.repo.one('SELECT original FROM knowledge_units')['original']
        # Reconstruct the old on-disk shape, as an older release would have left it.
        payload = body.model_dump()
        payload['blocks'][0]['id'] = 'B1'
        payload['eligible'] = service.channels({'grants': ['*']})
        payload['sourceUrl'] = body.meta.requestedUrl
        payload['meta'] = {k: getattr(body.meta, k) for k in ('author', 'publisher', 'publishedAt')}
        service.repo.execute('UPDATE work SET payload=?', (json.dumps(payload),))
        service.repo.execute('UPDATE requests SET payload=?', (body.model_dump_json(),))
        service.repo.execute('DELETE FROM knowledge_units')
        service.repo.execute('DELETE FROM unit_sources')
        service.repo.execute('DELETE FROM migrations')
    restarted = Service(settings, Model())
    with TestClient(create_app(service=restarted), base_url='https://testserver') as client:
        unit = restarted.repo.one('SELECT * FROM knowledge_units')
        assert json.loads(unit['original'])['block']['text'] == json.loads(original)['block']['text']
        assert len(restarted.repo.sources('economy', unit['logical_id'])) == 1
        assert client.get('/v1/channels/economy/graph', headers=headers).json()['nodes']
        assert 'blocks' not in json.loads(restarted.repo.one('SELECT payload FROM requests')['payload'])


def test_request_capacity_protects_pending_and_eviction_keeps_knowledge(tmp_path):
    async def run():
        service = Service(Settings(tmp_path, retention=RetentionSettings(max_requests=1)), Model())
        principal = {'customer': 'c', 'grants': ['*']}
        try:
            first = service.submit(Material.model_validate(material()), principal)
            with pytest.raises(OverflowError):
                service.submit(Material.model_validate(material(request='another')), principal)
            assert service.submit(Material.model_validate(material()), principal)['jobId'] == first['jobId']
            work = service.repo.one('SELECT * FROM work')
            await service.select(work)
            await service.extract_graph(service.repo.one('SELECT * FROM work'))
            second = service.submit(Material.model_validate(material(request='another')), principal)
            assert second['state'] == 'succeeded'
            assert len(service.repo.rows('SELECT * FROM requests')) == 1
            assert service.repo.rows('SELECT * FROM knowledge_units')
        finally:
            await service.close()
    asyncio.run(run())


def test_orphan_cleanup_is_idempotent_and_preserves_active_schema(shared):
    client, service, _, _ = shared
    repo = service.repo
    repo.create_channel('unused', 'Unused', 'Unused scope', service.schema_ids['generic'], 'general')
    service.engine('unused')
    repo.execute("UPDATE channels SET created=0 WHERE id='unused'")
    old = repo.one('SELECT body FROM schema_revisions WHERE id=?', (service.schema_ids['generic'],))['body']
    from osbi_server.ontology_schema import OntologySchema
    text = old + '\n# Unreferenced revision.\n'
    schema = OntologySchema(text)
    repo.execute('INSERT INTO schema_revisions VALUES (?,?,?,0)', (schema.fingerprint, 'unused', text))
    path = service.settings.data_dir / 'schemas' / (schema.fingerprint + '.ttl')
    path.write_text(text)
    result = client.portal.call(lambda: cleanup(service))
    assert result['counts']['channels'] == result['counts']['schemas'] == 1
    assert not repo.channel('unused') and not path.exists()
    assert not (service.settings.data_dir / 'graphs' / 'unused').exists()
    again = client.portal.call(lambda: cleanup(service))
    assert again['counts']['channels'] == again['counts']['schemas'] == 0
    assert repo.channel('policy') and repo.channel('economy')


def test_cancelled_extraction_releases_waiters_and_failure_does_not_cache(tmp_path):
    from osbi_contracts.models import Block, Focus
    async def run():
        model = Model()
        entered = asyncio.Event()
        block_once = True
        async def provider(messages, name):
            nonlocal block_once
            if block_once:
                block_once = False
                entered.set()
                await asyncio.Event().wait()
            return await model(messages, name)
        service = Service(Settings(tmp_path), model)
        engine = service.engine('economy')
        params = dict(focus=Focus(), title='Original', block=Block(id='B1', text=material()['blocks'][0]['text']),
                      meta={'requestedUrl': 'https://example.org/a'}, material_id='test', chat=provider)
        try:
            first = asyncio.create_task(engine.extract(**params))
            await entered.wait()
            second = asyncio.create_task(engine.extract(**params))
            await asyncio.sleep(.01)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            receipt = await asyncio.wait_for(second, 2)
            assert not receipt['cached'] and not engine.inflight
            assert model.calls.count('ontology_entities') == 1
            model.fail = True
            failed = {**params, 'title': 'A different original'}
            with pytest.raises(Exception):
                await engine.extract(**failed)
            assert not engine.inflight
            model.fail = False
            assert not (await engine.extract(**failed))['cached']
        finally:
            await service.close()
    asyncio.run(run())


def test_offline_cleanup_and_compaction_refuse_active_owner(tmp_path):
    data = tmp_path / 'state'
    service = Service(Settings(data), Model())
    command = [sys.executable, '-m', 'osbi_server.cli', '--data', str(data)]
    env = {'PATH': os.environ['PATH'], 'OSBI_AUTH_MODE': 'local'}
    try:
        blocked = subprocess.run([*command, 'cleanup'], env=env, capture_output=True, text=True, timeout=15)
        assert blocked.returncode != 0
    finally:
        asyncio.run(service.close())
    for action in ('cleanup', 'compact-storage'):
        result = subprocess.run([*command, action], env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert isinstance(json.loads(result.stdout), dict)
    import sqlite3
    with sqlite3.connect(data / 'metadata.db') as conn:
        assert conn.execute('PRAGMA auto_vacuum').fetchone()[0] == 2


def test_recently_retired_schema_gets_a_full_orphan_grace_period(shared):
    client, service, _, _ = shared
    repo = service.repo
    seed = repo.channel('economy')['schema_id']
    text = repo.one('SELECT body FROM schema_revisions WHERE id=?', (seed,))['body']
    previous = activate(repo, 'economy', text + '\n# Previous custom revision.\n', seed)['activeSchemaId']
    activate(repo, 'economy', text + '\n# Current custom revision.\n', previous)
    repo.execute('UPDATE schema_revisions SET created=0')
    client.portal.call(lambda: cleanup(service))
    assert repo.one('SELECT 1 FROM schema_revisions WHERE id=?', (previous,))
    client.portal.call(lambda: cleanup(service, now=time.time() + 31 * 86400))
    assert not repo.one('SELECT 1 FROM schema_revisions WHERE id=?', (previous,))


def test_failed_retention_is_not_starved_by_unexpired_success_records(tmp_path):
    from osbi_server.repository import Repository
    from osbi_server.maintenance import prune_metadata
    from osbi_contracts.storage import encode
    repo = Repository(tmp_path / 'metadata.db')
    now = time.time()
    try:
        for i in range(5):
            failed = i == 4
            work_id = str(i)
            age = now - (8 if failed else 10) * 86400
            repo.execute("INSERT INTO work(id,payload,state,created,updated) VALUES (?,'{}',?,?,?)",
                (work_id, 'failed' if failed else 'succeeded', age, age))
            repo.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)', (work_id, 'c', 'c', work_id, '', '{}', work_id, age))
        result = prune_metadata(repo, RetentionSettings(batch_size=1), now=now)
        assert result['requests'] == result['work'] == 1
        assert repo.one("SELECT 1 FROM work WHERE id='4'") is None
        assert len(repo.rows('SELECT * FROM work')) == 4
    finally:
        repo.close()


def test_periodic_maintenance_drains_capacity_overflow_in_small_batches(tmp_path):
    async def run():
        service = Service(Settings(tmp_path, retention=RetentionSettings(batch_size=1, max_completed_work=1)), Model())
        for i in range(5):
            service.repo.execute("INSERT INTO work(id,payload,state,created,updated) VALUES (?,'{}','succeeded',?,?)",
                                 (str(i), time.time(), time.time()))
        await service.start()
        try:
            for _ in range(100):
                if service.last_cleanup and service.last_cleanup['counts'].get('work') == 4:
                    break
                await asyncio.sleep(.01)
            assert len(service.repo.rows('SELECT * FROM work')) == 1
            assert service.last_cleanup['counts']['work'] == 4
        finally:
            await service.close()
    asyncio.run(run())
