"""Existing control-plane RPCs, concurrent net isolation and durable v4 jobs."""
import asyncio
from contextlib import AsyncExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time

import httpx
import pytest
import yaml
from fastapi import HTTPException
from fastapi.testclient import TestClient

from wiseflow_server.app import create_app
from wiseflow_server.constant import Settings
from wiseflow_server.control_plane import NetServices, SupabaseControl
from wiseflow_server.runtime_config import NodeKeyRegistry, load_runtime_config

ROOT = Path(__file__).resolve().parents[1]


def config_file(tmp_path):
    data = yaml.safe_load((ROOT / 'server/config/example/runtime.yaml').read_text())
    data['server']['dataDir'] = str(tmp_path / 'state')
    data['server']['auth'] = dict(mode='supabase', dataScope='net',
                                  nodeAliases={'legacy-customer': 'linfen-shanghai'})
    data['server']['batch']['enabled'] = False
    for store in data['server']['stores'].values():
        store['schemaPath'] = str(ROOT / 'server/wiseflow_server/schemas' / Path(store['schemaPath']).name)
    data['server']['seeds'] = []
    path = tmp_path / 'runtime.yaml'
    path.write_text(yaml.safe_dump(data, allow_unicode=True))
    return path


def test_control_plane_checks_every_request_and_fails_closed():
    async def run():
        row = dict(netId='government', customer='legacy-customer', rpm=1, expires=time.time() + 100)
        bodies = []

        async def handler(request):
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=row or None)

        registry = NodeKeyRegistry({}, mode='supabase', node_aliases={'legacy-customer': 'linfen-shanghai'})
        control = SupabaseControl(registry, client=httpx.AsyncClient(
            base_url='https://control.test/rest/v1/', transport=httpx.MockTransport(handler)))
        try:
            token = 'osbi_' + 'a' * 64
            assert await control.authenticate(token) == ('government', 'linfen-shanghai')
            with pytest.raises(HTTPException) as error:
                await control.authenticate(token)
            assert error.value.status_code == 429
            row.clear()
            with pytest.raises(HTTPException) as error:
                await control.authenticate(token)
            assert error.value.status_code == 401
            assert bodies[0] == {'p_key_hash': hashlib.sha256(token.encode()).hexdigest()}
            assert len(bodies) == 3
        finally:
            await control.close()
        async def unavailable(request):
            return httpx.Response(500, text='private provider error')
        control = SupabaseControl(registry, client=httpx.AsyncClient(
            base_url='https://control.test/', transport=httpx.MockTransport(unavailable)))
        try:
            with pytest.raises(HTTPException) as error:
                await control.authenticate(token)
            assert error.value.status_code == 503
            assert 'private' not in error.value.detail
        finally:
            await control.close()
    asyncio.run(run())


def test_managed_routes_isolate_nets_and_preserve_node_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv('OSBI_CONFIG_FILE', str(config_file(tmp_path)))
    monkeypatch.setenv('OSBI_LLM_API_KEY', 'isolated-test-key')
    monkeypatch.setenv('WISEFLOW_WORK_DIR', str(tmp_path / 'logs'))
    settings = Settings.from_env()
    app = create_app(service=type('Stub', (), {'settings': settings})())

    async def run():
        async with AsyncExitStack() as resources:
            pool = NetServices(settings, resources)
            calls = []
            async def chat(messages, model):
                payload = json.loads(messages[1]['content'])
                calls.append(payload.get('stage', 'selection'))
                if payload.get('stage') == 'ontology_entities':
                    return json.dumps({'entities': [{'text': '临汾路街道', 'label': '街镇机构'}]}, ensure_ascii=False)
                if payload.get('stage') == 'ontology_relations':
                    return '{"relations":[]}'
                return '{"blockIds":["B1"],"linkIds":[]}'
            pool.client.chat = chat
            a, b = await pool.get('government'), await pool.get('general')
            app.state.service = a
            app.state.net_services = pool
            class Control:
                async def authenticate(self, token):
                    if token == 'osbi_a': return 'government', 'linfen-shanghai'
                    if token == 'osbi_b': return 'government', 'shizishan-chengdu'
                    if token == 'osbi_c': return 'general', 'other-node'
                    if token == 'osbi_later': return 'government', 'later-node'
                    raise HTTPException(401, 'Invalid node key')
            app.state.control = Control()
            payload = dict(protocolVersion='material-extract/v4', channel='community', materialId='a' * 64,
                           input=dict(title='社区治理', blocks=[dict(id='B1', text='临汾路街道推进社区治理。')], links=[]),
                           meta={'requestedUrl': 'https://example.test/a', 'publisher': '测试发布'})
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://osbi.test') as client:
                def headers(key): return {'Authorization': 'Bearer ' + key}
                replies = await asyncio.gather(*[
                    client.post('/v1/materials/extract', json=payload, headers=headers(key))
                    for key in ['osbi_a', 'osbi_b', 'osbi_c']])
                assert [r.status_code for r in replies] == [200, 200, 200]
                ids = [r.json()['ontologyJobs'][0]['id'] for r in replies]
                assert ids[0] == ids[1]
                assert all('infos' not in r.json() and not r.json()['errors'] for r in replies)
                assert (await a.queue.counts())['pending'] == 1
                assert (await b.queue.counts())['pending'] == 1
                await a.batch.run()
                await b.batch.run()
                job = await a.queue.get(ids[0])
                assert job['status'] == 'done'
                stored = a.engine('policy').store.read(job['receipt']['id'])
                assert set(stored['provenance']['node_ids']) == {'linfen-shanghai', 'shizishan-chengdu'}
                for key, expected in [('osbi_a', {'linfen-shanghai','shizishan-chengdu'}), ('osbi_c', {'other-node'})]:
                    result = await client.post('/v1/channels/community/evidence', json={'sourceScope':'all'}, headers=headers(key))
                    assert result.status_code == 200
                    assert {s['nodeId'] for item in result.json()['items'] for s in item['sources']} == expected
                    assert len((await client.get('/v1/channels', headers=headers(key))).json()['items']) == 7
                spoof = await client.post('/v1/materials/extract', json={**payload, 'nodeId':'other-node'}, headers=headers('osbi_a'))
                assert spoof.status_code == 403
                late = await client.post('/v1/materials/extract', json=payload, headers=headers('osbi_later'))
                assert late.status_code == 200
                late_evidence = await client.post('/v1/channels/community/evidence', json={'sourceScope':'own'}, headers=headers('osbi_later'))
                assert len(late_evidence.json()['items']) == 1
                assert 'later-node' in {s['nodeId'] for item in late_evidence.json()['items'] for s in item['sources']}
                assert (await client.get('/v1/channels')).status_code == 401
                assert (await client.post('/v1/materials/extract', json={**payload, 'focus':{}}, headers=headers('osbi_a'))).status_code == 422
                assert 'info' not in calls
            # A pending job survives closing every database/graph owner.
            from wiseflow_server.models import ExtractRequest
            request = ExtractRequest.model_validate({**payload, 'channel':'digital', 'nodeId':'linfen-shanghai'})
            await a.extract(request)
            assert (await a.queue.counts())['pending'] == 1
        async with AsyncExitStack() as resources:
            restarted = NetServices(settings, resources)
            resumed = await restarted.get('government')
            assert (await resumed.queue.counts()) == dict(pending=1, done=1, retrying=0)
    asyncio.run(run())
