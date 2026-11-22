"""MimicWX + Awada shadow delivery (ported production push stack).

All provider traffic is mocked; no real group ever receives a message here.
"""
import asyncio
import json
import logging

import httpx
import pytest

from osbi_client import push_mimicwx, shadow_push
from osbi_client.awada_publisher import AwadaPublisher
from osbi_client.models import ReportPushConfig
from osbi_client.push_mimicwx import MimicWXPusher, load_config
from osbi_client.shadow_push import (SHADOW_AWADA_CHANNEL_ID, push_shadow_text,
                                     send_awada_file, send_awada_text)


def run(coro):
    return asyncio.run(coro)


def test_mimicwx_config_resolution_env_over_file(tmp_path, monkeypatch):
    config_file = tmp_path / 'mimicwx.json'
    config_file.write_text(json.dumps({'mimicwx': {
        'base_url': 'http://127.0.0.1:9999', 'token': 'file-token',
        'window': '文件测试群'}}), encoding='utf-8')
    monkeypatch.delenv('MIMICWX_BASE_URL', raising=False)
    monkeypatch.delenv('MIMICWX_TOKEN', raising=False)
    monkeypatch.delenv('MIMICWX_WINDOW', raising=False)
    config = load_config(config_file)
    assert config == dict(base_url='http://127.0.0.1:9999', token='file-token',
                          window='文件测试群')
    monkeypatch.setenv('MIMICWX_WINDOW', '环境测试群')
    assert load_config(config_file)['window'] == '环境测试群'
    assert load_config()['base_url'] == 'http://127.0.0.1:8899'


def test_mimicwx_skips_without_window_or_token(monkeypatch):
    monkeypatch.delenv('MIMICWX_TOKEN', raising=False)
    monkeypatch.delenv('MIMICWX_WINDOW', raising=False)
    result = run(MimicWXPusher().send_text('hello'))
    assert result['sent'] is False and 'window' in result['message']
    result = run(MimicWXPusher(window='测试群').send_text('hello'))
    assert result['sent'] is False and 'token' in result['message']


def test_mimicwx_send_posts_bearer_text(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            captured.update(url=url, headers=headers, json=json)
            return httpx.Response(200, json={'sent': True, 'verified': True,
                                             'message': 'ok'},
                                  request=httpx.Request('POST', url))

    monkeypatch.setattr(push_mimicwx.httpx, 'AsyncClient', FakeClient)
    pusher = MimicWXPusher(base_url='http://127.0.0.1:8899', token='t',
                           window='测试群')
    result = run(pusher.send_text('早报文本'))
    assert result['sent'] is True and result['verified'] is True
    assert captured['url'] == 'http://127.0.0.1:8899/send'
    assert captured['headers']['Authorization'] == 'Bearer t'
    assert captured['json'] == {'to': '测试群', 'text': '早报文本'}


class FakeRelay:
    """Stands in for AwadaRelayClient; records outbound meta/payload."""

    instances = []

    def __init__(self, config, logger, **kwargs):
        self.config = config
        self.lane = config.get('lane', 'user')
        self.sent = []
        self.fail = False
        FakeRelay.instances.append(self)

    async def send_message(self, payload, meta, *, request_id, lane=None):
        self.sent.append(dict(payload=payload, meta=meta, request_id=request_id))
        return {'eventId': 'op_123'}

    async def wait_operation(self, operation_id, *, lane=None):
        if self.fail:
            from osbi_client.awada_relay import RelayError
            raise RelayError('PROVIDER_REJECTED')
        return {'state': 'succeeded'}

    async def close(self):
        pass


def test_awada_publisher_success_and_failure(monkeypatch):
    import osbi_client.awada_publisher as module
    monkeypatch.setattr(module, 'AwadaRelayClient', FakeRelay)
    FakeRelay.instances.clear()
    logger = logging.getLogger('test')
    publisher = AwadaPublisher({'platform': 'qiwe', 'lane': 'user'}, logger)
    ok = run(publisher.send_text('文本', channel_id=SHADOW_AWADA_CHANNEL_ID))
    assert ok is True and publisher.last_operation_id == 'op_123'
    relay = FakeRelay.instances[-1]
    assert relay.sent[0]['meta']['channel_id'] == SHADOW_AWADA_CHANNEL_ID
    assert relay.sent[0]['meta']['platform'] == 'qiwe'
    assert relay.sent[0]['payload'][0]['type'] == 'text'
    run(publisher.close())
    # Provider rejection returns False but keeps the operation id.
    publisher2 = AwadaPublisher({}, logger)
    FakeRelay.instances[-1].fail = True
    ok = run(publisher2.send_text('文本', channel_id='1'))
    assert ok is False and publisher2.last_operation_id == 'op_123'
    run(publisher2.close())


def test_send_awada_text_without_config_is_safe():
    result = run(send_awada_text('文本'))
    assert result['sent'] is False
    assert result['channelId'] == SHADOW_AWADA_CHANNEL_ID
    assert '未配置' in result['message']


def test_push_shadow_text_combines_channels(monkeypatch):
    class FakeMimic:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def send_text(self, text, window=None):
            return dict(sent=False, message='window 未配置')

    async def fake_awada(text, *, config_path='', channel_id=None, request_id=None):
        return dict(sent=True, channelId=channel_id, operationId='op_1', message='')

    monkeypatch.setattr(shadow_push, 'MimicWXPusher', FakeMimic)
    monkeypatch.setattr(shadow_push, 'send_awada_text', fake_awada)
    result = run(push_shadow_text('早报', push_config=ReportPushConfig()))
    assert result['sent'] is True  # either channel succeeding counts
    assert result['mimicwx']['sent'] is False and result['awada']['sent'] is True
    # Both failing -> sent False, never raises.
    async def fake_awada_fail(text, **kwargs):
        return dict(sent=False, message='provider rejected or pending')
    monkeypatch.setattr(shadow_push, 'send_awada_text', fake_awada_fail)
    result = run(push_shadow_text('早报'))
    assert result['sent'] is False


def test_send_awada_file_guards(tmp_path):
    docx = tmp_path / 'weekly.docx'
    docx.write_bytes(b'PK')
    result = run(send_awada_file(docx, ''))
    assert result['sent'] is False and result['message'] == 'channel_id 未提供'
    missing = tmp_path / 'missing.docx'
    result = run(send_awada_file(missing, '123'))
    assert result['sent'] is False and result['message'] == '文件不存在'
    result = run(send_awada_file(docx, '123'))
    assert result['sent'] is False and '未配置' in result['message']


def test_send_awada_file_uses_publisher(monkeypatch, tmp_path):
    import osbi_client.awada_publisher as module
    monkeypatch.setattr(module, 'AwadaRelayClient', FakeRelay)
    FakeRelay.instances.clear()
    docx = tmp_path / 'weekly.docx'
    docx.write_bytes(b'PK\x03\x04')
    config = tmp_path / 'awada.json'
    config.write_text(json.dumps({'platform': 'qiwe'}), encoding='utf-8')
    result = run(send_awada_file(docx, '10854594337574110', config_path=str(config)))
    assert result['sent'] is True and result['operationId'] == 'op_123'
    payload = FakeRelay.instances[-1].sent[0]['payload'][0]
    assert payload['type'] == 'file' and payload['file_name'] == 'weekly.docx'
    assert payload['base64']  # file content snapshotted for the relay


def test_report_push_config_defaults_dry_run_and_validates_endpoint():
    config = ReportPushConfig()
    assert config.enabled is False
    assert config.awadaChannelId == '10854594337574110'
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReportPushConfig(mimicwxBaseUrl='http://remote.example.com')
    assert ReportPushConfig(mimicwxBaseUrl='http://127.0.0.1:8899/').mimicwxBaseUrl == 'http://127.0.0.1:8899'
