"""Network isolation and authoritative authentication without production credentials."""
import asyncio
import hashlib
import json
import time
from dataclasses import replace

from fastapi.testclient import TestClient
import httpx
import pytest

from osbi_server.app import create_app
from osbi_server.config import Settings
from osbi_server.control import NetRegistry, SupabaseControl
from osbi_server.service import Service
from conftest import Model, material, wait_job


KEYS = {'gov': 'osbi_' + 'a' * 32, 'policy': 'osbi_' + 'b' * 32,
        'general': 'osbi_' + 'c' * 32, 'limited': 'osbi_' + 'd' * 32}


class Control:
    def __init__(self):
        self.revoked = set()
        self.expired = set()
        self.unavailable = False

    async def definitions(self):
        return [{'id': 'government', 'schemas': ['policy', 'economy']}, {'id': 'general', 'schemas': ['general']}]

    async def authenticate(self, key):
        from osbi_server.control import ControlUnavailable
        if self.unavailable:
            raise ControlUnavailable('Synthetic outage')
        name = next((name for name, value in KEYS.items() if value == key), None)
        if name is None or name in self.revoked or name in self.expired:
            return None
        generic = name == 'general'
        return dict(id=name, customer='same-customer', netId='general' if generic else 'government',
                    netSchemas=['general'] if generic else ['policy', 'economy'],
                    schemaGrants=['general'] if generic else ['policy'] if name == 'policy' else ['policy', 'economy'],
                    rpm=1 if name == 'limited' else 0, expires=time.time() + 100)

    async def close(self):
        pass


@pytest.fixture
def nets(tmp_path):
    settings = Settings(tmp_path / 'server', api_key='synthetic-provider-key', auth_mode='supabase',
                        control_url='https://control.invalid', control_key='synthetic-control-key')
    control = Control()
    models = {}

    def factory(config):
        model = Model()
        models[config.data_dir.name] = model
        return Service(config, model)

    registry = NetRegistry(settings, control, factory)
    with TestClient(create_app(settings=settings, registry=registry), base_url='https://testserver') as client:
        yield client, registry, control, models


def headers(name):
    return {'Authorization': 'Bearer ' + KEYS[name]}


def test_net_and_schema_channel_visibility(nets):
    client, registry, _, _ = nets
    assert {x['id'] for x in client.get('/v1/channels', headers=headers('gov')).json()['items']} == {'policy', 'economy'}
    assert {x['id'] for x in client.get('/v1/channels', headers=headers('policy')).json()['items']} == {'policy'}
    assert {x['id'] for x in client.get('/v1/channels', headers=headers('general')).json()['items']} == {'general'}
    for route in ['/graph', '/entities', '/blocks/missing']:
        assert client.get('/v1/channels/economy' + route, headers=headers('policy')).status_code == 404
        assert client.get('/v1/channels/economy' + route, headers=headers('general')).status_code == 404
    for route, body in [('/query', {'source': 'anything'}), ('/evidence', {})]:
        assert client.post('/v1/channels/economy' + route, headers=headers('general'), json=body).status_code == 404
    assert client.get('/v1/identity', headers=headers('general')).json()['netId'] == 'general'
    assert registry.services['general'].repo.path != registry.services['government'].repo.path
    assert registry.services['general'].client.limit is registry.services['government'].client.limit
    assert client.get('/readyz').status_code == 200


def test_net_jobs_and_deduplication_are_separate(nets):
    client, registry, _, _ = nets
    body = material()
    gov = client.post('/v1/materials', headers=headers('gov'), json=body).json()
    assert wait_job(client, headers('gov'), gov['jobId'])['state'] == 'succeeded'
    assert client.get('/v1/jobs/' + gov['jobId'], headers=headers('general')).status_code == 404
    other = client.post('/v1/materials', headers=headers('general'), json=body).json()
    assert other['jobId'] != gov['jobId'] and not other['reused']
    assert registry.services['general'].repo.one('SELECT count(*) n FROM requests')['n'] == 1
    assert registry.services['government'].repo.one('SELECT count(*) n FROM requests')['n'] == 1
    assert client.get('/v1/status', headers=headers('general')).status_code == 200


def test_revocation_expiry_outage_and_rpm(nets):
    client, _, control, _ = nets
    assert client.get('/v1/identity', headers=headers('limited')).status_code == 200
    assert client.get('/v1/identity', headers=headers('limited')).status_code == 429
    for _ in range(3):
        assert client.get('/v1/identity', headers=headers('gov')).status_code == 200
    control.revoked.add('gov')
    assert client.get('/v1/identity', headers=headers('gov')).status_code == 401
    control.expired.add('general')
    assert client.get('/v1/identity', headers=headers('general')).status_code == 401
    control.unavailable = True
    assert client.get('/v1/identity', headers=headers('policy')).status_code == 503


def test_only_hash_is_sent_to_control_and_malformed_reply_fails_closed():
    seen = []
    async def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, content=b'null', headers={'Content-Type': 'application/json'})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            control = SupabaseControl('https://control.invalid', 'private-service-role', client=client)
            assert await control.authenticate(KEYS['gov']) is None
            assert await control.authenticate('ofb_not-an-osbi-key') is None
    asyncio.run(run())
    assert seen == [{'p_key_hash': hashlib.sha256(KEYS['gov'].encode()).hexdigest()}]


def test_general_extraction_and_new_channels_remain_in_general_net(nets):
    client, _, _, _ = nets
    body = material(text='天文研究中心参与星空观测活动。')
    submitted = client.post('/v1/materials', headers=headers('general'), json=body)
    assert submitted.status_code == 202
    job = wait_job(client, headers('general'), submitted.json()['jobId'])
    assert job['state'] == 'succeeded', job
    assert client.get('/v1/channels/astronomy/graph', headers=headers('general')).json()['nodes']
    assert client.get('/v1/channels/astronomy/graph', headers=headers('gov')).status_code == 404
    assert client.get('/v1/jobs/' + job['jobId'], headers=headers('gov')).status_code == 404


def test_multi_net_offline_backup_refuses_running_registry(nets, tmp_path):
    from osbi_server.backup import backup
    _, registry, _, _ = nets
    with pytest.raises(RuntimeError):
        backup(registry.settings.data_dir, tmp_path / 'snapshot')


def test_multi_net_stopped_snapshot_round_trip(tmp_path):
    from osbi_server.backup import backup, restore
    from osbi_server.repository import Repository
    root = tmp_path / 'state'
    for name in ['government', 'general']:
        repo = Repository(root / 'nets' / name / 'metadata.db')
        repo.issue_key(name, time.time() + 100)
        repo.close()
    snapshot, target = tmp_path / 'snapshot', tmp_path / 'restored'
    backup(root, snapshot)
    restore(snapshot, target)
    assert (target / 'nets/government/metadata.db').is_file()
    assert (target / 'nets/general/metadata.db').is_file()


def test_local_history_scope_includes_net_without_changing_legacy_scope():
    from types import SimpleNamespace
    from osbi_client.models import ClientConfig
    from osbi_client.weekly import coverage_scope
    from osbi_contracts.storage import digest
    identity = {'customerId': 'same-customer', 'netId': 'government'}
    config = ClientConfig(serverUrl='https://server.example', serverKey=KEYS['gov'])
    daemon = SimpleNamespace(state=SimpleNamespace(config=lambda: config),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=identity)))
    government = asyncio.run(coverage_scope(daemon))
    identity['netId'] = 'general'
    assert asyncio.run(coverage_scope(daemon)) != government
    identity.pop('netId')
    assert asyncio.run(coverage_scope(daemon)) == digest([config.serverUrl, identity['customerId']])


@pytest.mark.parametrize('change', [
    {'netId': '../government'}, {'rpm': -1}, {'schemaGrants': ['foreign']}, {'expires': 'invalid'},
])
def test_invalid_control_responses_fail_closed(change):
    from osbi_server.control import ControlUnavailable
    result = dict(id='test-id', customer='test-customer', netId='government',
        netSchemas=['policy'], schemaGrants=['policy'], rpm=300, expires=time.time()+100)
    result.update(change)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=result))) as client:
            control = SupabaseControl('https://control.invalid', 'private-test-credential', client=client)
            with pytest.raises(ControlUnavailable):
                await control.authenticate(KEYS['gov'])
    asyncio.run(run())
