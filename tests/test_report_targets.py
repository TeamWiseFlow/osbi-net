"""Profile-level delivery targets: CRUD validation, secret masking, dry-run."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from osbi_client.models import ClientConfig
from osbi_client.report_settings import ReportTarget
from osbi_client.shadow_push import deliver_targets
from osbi_client.state import State


def _profile_with_target(target):
    data = ClientConfig().model_dump()
    profile = data['reportProfiles'][0]
    profile['targets'] = [target]
    profile['topnews']['targetIds'] = [target['id']]
    return data


def test_target_config_validation_and_public_masking():
    target = ReportTarget(id='m', type='mimicwx', label='MimicWX',
                          config={'baseUrl': 'http://127.0.0.1:8899',
                                  'token': 'secret-token', 'window': '测试群'})
    public = target.public_config()
    assert 'token' not in public['config']
    assert public['config']['tokenConfigured'] is True
    assert public['config']['tokenSuffix'] == 'oken'
    with pytest.raises(ValidationError):
        ReportTarget(id='m', type='mimicwx', config={'nonsense': 1})


def test_profile_target_ids_must_reference_configured_targets():
    data = ClientConfig().model_dump()
    data['reportProfiles'][0]['topnews']['targetIds'] = ['missing']
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(data)


def test_public_config_masks_target_secrets_after_save(tmp_path):
    state = State(tmp_path / 'client.db')
    data = _profile_with_target({'id': 'w', 'type': 'wecom-webhook', 'label': '企微',
                                 'config': {'webhookUrl': 'https://qy.example/hook'}})
    data['reportProfiles'][0]['targets'] = [
        {'id': 'm', 'type': 'mimicwx', 'label': 'MimicWX',
         'config': {'baseUrl': 'http://127.0.0.1:8899', 'token': 'secret-token',
                    'window': '测试群'}}]
    data['reportProfiles'][0]['topnews']['targetIds'] = ['m']
    state.save_config({'reportProfiles': data['reportProfiles']})
    public = state.public_config()
    target = public['reportProfiles'][0]['targets'][0]
    assert 'secret-token' not in json.dumps(public)
    assert target['config']['tokenConfigured'] is True
    state.close()


def test_wecom_webhook_target_is_not_implemented():
    profile_data = ClientConfig().model_dump()
    profile = profile_data['reportProfiles'][0]
    target = {'id': 'w', 'type': 'wecom-webhook', 'label': '企微', 'config': {}}
    profile['targets'] = [target]
    profile['topnews']['targetIds'] = ['w']
    profile['topnews']['dryRun'] = False
    config = ClientConfig.model_validate(profile_data)
    result = asyncio.run(deliver_targets(config.reportProfiles[0],
                                         config.reportProfiles[0].topnews, '早报'))
    assert result['sent'] is False
    assert result['targets'][0]['message'] == 'not_implemented'


def test_dry_run_task_never_calls_targets(monkeypatch):
    profile_data = ClientConfig().model_dump()
    profile = profile_data['reportProfiles'][0]
    profile['targets'] = [{'id': 'm', 'type': 'mimicwx', 'label': 'MimicWX',
                           'config': {'window': '测试群'}}]
    profile['topnews']['targetIds'] = ['m']
    profile['topnews']['dryRun'] = True
    config = ClientConfig.model_validate(profile_data)

    async def fail(*args, **kwargs):
        raise AssertionError('target must not be called in dry-run')

    monkeypatch.setattr('osbi_client.shadow_push.push_to_target', fail)
    from osbi_client.shadow_reports import _deliver
    result = asyncio.run(_deliver(config.reportProfiles[0],
                                  config.reportProfiles[0].topnews, '早报'))
    assert result == dict(sent=False, dryRun=True, message='dry-run')
