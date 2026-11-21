"""Client assignments and deployment-owned ontologies remain explicit and private."""
import asyncio
import json
from pathlib import Path
import time

from fastapi.testclient import TestClient
import pytest

from osbi_contracts.material import material_hash
from osbi_contracts.models import Material
from osbi_server.admin import activate, export_schema
from osbi_server.app import create_app
from osbi_server.channels import ChannelDefinition, default_channels, load_channels
from osbi_server.config import Settings
from osbi_server.service import Service
from conftest import Model, ExampleSettings, material, wait_job


def test_fresh_default_is_only_a_template(tmp_path):
    service = Service(Settings(tmp_path), Model())
    assert [row['id'] for row in service.repo.rows('SELECT id FROM channels')] == ['default']
    assert not service.repo.channel('policy') and not service.repo.channel('economy')
    asyncio.run(service.close())


def test_block_channels_override_material_and_are_authorized(shared):
    client, service, model, headers = shared
    body = material(channel='economy')
    body['blocks'].append({'id':'policy-block', 'text':body['blocks'][0]['text'], 'channel':'policy'})
    result = wait_job(client, headers, client.post('/v1/materials', headers=headers, json=body).json()['jobId'])
    assert result['state'] == 'succeeded', result
    assert [(a['blockId'],a['channelId']) for a in result['assignments']] == [('original','economy'),('policy-block','policy')]
    assert all(stage in {'ontology_entities','ontology_relations'} for stage in model.calls)
    restricted = service.repo.issue_key('restricted', time.time()+60, ['economy'])['key']
    response = client.post('/v1/materials', headers={'Authorization':'Bearer '+restricted}, json=body)
    assert response.status_code == 403


def test_material_and_per_block_assignment_have_the_same_content_identity():
    inherited = Material.model_validate(material())
    explicit = inherited.model_dump()
    explicit['channel'] = None
    explicit['blocks'][0]['channel'] = 'economy'
    assert material_hash(inherited) == material_hash(Material.model_validate(explicit))
    explicit['blocks'][0]['channel'] = 'policy'
    assert material_hash(inherited) != material_hash(Material.model_validate(explicit))


def test_missing_channel_and_old_protocol_fail_before_processing(shared):
    client, service, model, headers = shared
    body = material()
    body.pop('channel')
    assert client.post('/v1/materials', headers=headers, json=body).status_code == 422
    body['channel'] = 'economy'
    body['protocolVersion'] = 'osbi-material/v1'
    assert client.post('/v1/materials', headers=headers, json=body).status_code == 422
    assert not model.calls and not service.repo.rows('SELECT * FROM requests')


def test_private_active_revision_survives_changed_or_missing_bootstrap(tmp_path):
    private = tmp_path / 'private'
    private.mkdir()
    source = Path(__file__).resolve().parents[1] / 'server/examples/schemas/economy.ttl'
    schema = private / 'business.private.ttl'
    original = source.read_text()
    schema.write_text(original)
    manifest = private / 'channels.json'
    manifest.write_text(json.dumps({'channels':[{'id':'business','title':'Business','schema':schema.name}]}))
    settings = Settings(tmp_path/'state', channel_definitions=load_channels(manifest))
    service = Service(settings, Model())
    before = service.repo.channel('business')['schema_id']
    private_text = original + '\n# Synthetic private deployment revision.\n'
    active = activate(service.repo, 'business', private_text, before)['activeSchemaId']
    asyncio.run(service.close())
    schema.write_text(default_channels()[0].schema.read_text())
    restarted = Service(settings, Model())
    assert restarted.repo.channel('business')['schema_id'] == active
    assert restarted.engine('business').schema.text == private_text
    asyncio.run(restarted.close())
    schema.unlink()
    restored = Service(settings, Model())
    assert restored.repo.channel('business')['schema_id'] == active
    assert not restored.repo.channel('policy')
    asyncio.run(restored.close())


def test_custom_manifest_relative_paths_and_invalid_definitions(tmp_path):
    path = tmp_path/'channels.json'
    path.write_text(json.dumps({'channels':[{'id':'custom','schema':'ontologies/custom.ttl'}]}))
    assert load_channels(path)[0].schema == tmp_path/'ontologies/custom.ttl'
    for body in [{}, {'channels':[]}, {'channels':[{'id':'../wrong','schema':'x.ttl'}]},
                 {'channels':[{'id':'custom','schema':'x.ttl'},{'id':'custom','schema':'y.ttl'}]}]:
        path.write_text(json.dumps(body))
        with pytest.raises(ValueError): load_channels(path)


def test_active_schema_export_is_private_and_never_overwrites(shared, tmp_path):
    _, service, _, _ = shared
    path = tmp_path/'economy.private.ttl'
    result = export_schema(service.repo, 'economy', path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == service.engine('economy').schema.text
    assert result['schemaId'] == service.repo.channel('economy')['schema_id']
    with pytest.raises(FileExistsError): export_schema(service.repo, 'economy', path)


def test_channel_revision_is_pinned_at_admission(tmp_path):
    service = Service(ExampleSettings(tmp_path), Model())
    key = service.repo.issue_key('client', time.time()+60)['key']
    headers = {'Authorization':'Bearer '+key}
    original = service.repo.channel('economy')['schema_id']
    body = service.repo.one('SELECT body FROM schema_revisions WHERE id=?',(original,))['body']
    submitted = service.submit(Material.model_validate(material()), {'customer':'client','grants':['*']})
    new = activate(service.repo, 'economy', body+'\n# Revised after submission.\n', original)['activeSchemaId']
    with TestClient(create_app(service=service), base_url='https://testserver') as client:
        result = wait_job(client, headers, submitted['jobId'])
        assert result['state'] == 'succeeded'
        assert result['assignments'][0]['schemaId'] == original
        assert result['graphs'][0]['schemaId'] == original
        assert service.repo.channel('economy')['schema_id'] == new


def test_control_plane_accepts_deployment_defined_channel_names(tmp_path):
    from osbi_server.control import NetRegistry
    class Control:
        async def definitions(self): return [{'id':'custom-net','schemas':['research']}]
        async def close(self): pass
    template = default_channels()[0].schema
    settings = Settings(tmp_path, api_key='synthetic', channel_definitions=(
        ChannelDefinition('research','Research','Private project channel',template),))
    registry = NetRegistry(settings, Control(), lambda config:Service(config, Model()))
    async def run():
        try:
            await registry.start()
            assert registry.ready()
            assert registry.services['custom-net'].repo.channel('research')
        finally:
            await registry.close()
    asyncio.run(run())


def test_legacy_unassigned_pending_work_fails_without_guessing(tmp_path):
    model = Model()
    service = Service(ExampleSettings(tmp_path), model)
    key = service.repo.issue_key('client', time.time()+60)['key']
    job = service.submit(Material.model_validate(material()), {'customer':'client','grants':['*']})
    row = service.repo.one('SELECT id,payload FROM work')
    payload = json.loads(row['payload'])
    payload.pop('assignments')
    service.repo.execute('UPDATE work SET payload=? WHERE id=?', (json.dumps(payload),row['id']))
    with TestClient(create_app(service=service), base_url='https://testserver') as client:
        result = wait_job(client, {'Authorization':'Bearer '+key}, job['jobId'])
        assert result['state'] == 'failed' and 'client_channel_required' in result['error']
        assert not model.calls


def test_previously_assigned_graph_work_continues_after_upgrade(tmp_path):
    service = Service(ExampleSettings(tmp_path), Model())
    key = service.repo.issue_key('client', time.time()+60)['key']
    job = service.submit(Material.model_validate(material()), {'customer':'client','grants':['*']})
    row = service.repo.one('SELECT * FROM work')
    asyncio.run(service.prepare(row))
    payload = json.loads(row['payload'])
    payload.pop('assignments')
    payload['blocks'][0].pop('channel', None)
    payload['prompts']['relevance'] = 'Retired legacy relevance prompt'
    payload['prompts']['routing'] = 'Retired legacy routing prompt'
    service.repo.execute('UPDATE work SET payload=? WHERE id=?', (json.dumps(payload),row['id']))
    with TestClient(create_app(service=service), base_url='https://testserver') as client:
        result = wait_job(client, {'Authorization':'Bearer '+key}, job['jobId'])
        assert result['state'] == 'succeeded' and result['graphs'][0]['entityCount'] == 2


def test_client_proxy_supports_single_character_channel_ids(tmp_path):
    import httpx
    from osbi_client.app import create_app as client_app
    from osbi_client.daemon import Daemon
    daemon = Daemon(tmp_path, transport=httpx.MockTransport(lambda request: httpx.Response(200,
        json={'channelId':'x','nodes':[],'edges':[],'blocks':{}})))
    daemon.state.save_config({'serverUrl':'https://shared.example','serverKey':'synthetic'})
    with TestClient(client_app(daemon=daemon), base_url='http://127.0.0.1') as client:
        client.get('/settings')
        assert client.get('/api/server/channels/x/graph').status_code == 200


def test_empty_configured_channels_do_not_starve_orphan_cleanup(tmp_path):
    from osbi_server.config import RetentionSettings
    from osbi_server.maintenance import cleanup
    service = Service(Settings(tmp_path, retention=RetentionSettings(batch_size=1)))
    service.repo.create_channel('orphan', 'Orphan', '', service.schema_ids['default'], 'orphan')
    service.repo.execute('UPDATE channels SET created=0')
    async def run():
        try:
            result = await cleanup(service)
            assert result['counts']['channels'] == 1
            assert service.repo.channel('default') and not service.repo.channel('orphan')
        finally:
            await service.close()
    asyncio.run(run())
