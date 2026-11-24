"""Generic capture-time link classification and optional role rollout."""
import asyncio
import json

import httpx

from osbi_client.daemon import Daemon
from osbi_client.snapshot_index import index_snapshot
from test_client import Browser


def capture():
    page = 'https://www.example.gov.cn/policy/index.html'
    return {
        'requestedUrl': page, 'finalUrl': page, 'canonicalUrl': page,
        'capturedAt': '2026-11-24T20:00:00+00:00',
        'status': {'access': 'ok', 'load': 'ready'},
        'dom': {
            'url': page, 'meta': {'title': '政策目录'},
            'targetPath': 'html > body:nth-of-type(1) > main:nth-of-type(1)',
            'groups': [{'id': 'G0001', 'kind': 'page', 'parentId': None},
                       {'id': 'G0002', 'kind': 'main', 'parentId': 'G0001'}],
            'segments': [{
                'text': '政策目录。本页列出政策文件。', 'groupId': 'G0002',
                'kind': 'paragraph', 'inMain': True,
                'links': [
                    {'url': 'https://www.example.gov.cn/policy/2026/1124/content_1.html',
                     'text': '某政策通知', 'rawHref': '/policy/2026/1124/content_1.html'},
                    {'url': 'https://www.example.gov.cn/nw39220/index.html',
                     'text': '政策栏目', 'rawHref': '/nw39220/index.html'},
                    {'url': 'https://www.example.gov.cn/login', 'text': '登录', 'rawHref': '/login'},
                    {'url': 'https://www.example.gov.cn/en/', 'text': 'EN', 'rawHref': '/en/'},
                    {'url': page, 'text': '当前页', 'rawHref': 'index.html'},
                ],
            }],
        },
    }


def test_capture_classifies_content_and_navigation_and_drops_chrome():
    material = index_snapshot(capture())
    links = {link['text']: link for link in material['links']}
    assert links['某政策通知']['role'] == 'content'
    assert links['政策栏目']['role'] == 'navigation'
    assert '登录' not in links
    assert 'EN' not in links
    assert '当前页' not in links
    assert material['localMaps']['urls']


class RoleBrowser(Browser):
    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        return {
            'materialId': 'material', 'meta': {
                'title': '政策目录', 'requestedUrl': url, 'finalUrl': url,
                'canonicalUrl': url, 'capturedAt': '2026-11-24T20:00:00+00:00'},
            'blocks': [{'id': 'B1', 'text': '正文内容。'}],
            'links': [{'id': 'L1', 'text': '政策栏目', 'context': '栏目',
                       'role': 'navigation'}],
            'localMaps': {'urls': {'L1': 'https://example.org/policy/list'}},
        }


def _health(supported):
    def handler(request):
        if request.method == 'GET' and request.url.path == '/health':
            body = {'status': 'ok'}
            if supported:
                body['features'] = {'linkRoles': True}
            return httpx.Response(200, json=body)
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_client_only_sends_link_roles_when_server_advertises_support(tmp_path):
    async def run(supported):
        daemon = Daemon(tmp_path / ('yes' if supported else 'no'),
                        browser=RoleBrowser(), transport=_health(supported))
        daemon.state.save_config({
            'serverUrl': 'https://example.test', 'serverKey': 'key',
            'sources': [{'id': 'a', 'url': 'https://example.org/policy',
                         'channels': ['policy-latest']}]})
        run_id = daemon.start_run('a')['id']
        await daemon.acquire(run_id)
        payload = json.loads(daemon.state.one('SELECT * FROM outbox')['payload'])
        link = payload['input']['links'][0]
        assert ('role' in link) is supported
        assert 'url' not in link
        daemon.state.close()

    asyncio.run(run(True))
    asyncio.run(run(False))
