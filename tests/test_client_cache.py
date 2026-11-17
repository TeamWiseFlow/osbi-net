"""URL acquisition reuse and material processing reuse remain independent."""
import asyncio
from copy import deepcopy
import json
import time
import pytest
from osbi_contracts.material import MATERIAL_HASH_VERSION
from osbi_client.cache_settings import CaptureCacheSettings
from osbi_client.daemon import Daemon
from osbi_client.models import ClientConfig
from osbi_client.state import State
from osbi_client.cache import LocalCache
from test_client import Browser


def test_capture_ttl_domains_aliases_context_and_restart(tmp_path):
    path = tmp_path / 'client.db'
    state = State(path)
    cache = LocalCache(state)
    policy = CaptureCacheSettings(defaultTtlSeconds=21600, domainTtlSeconds={'example.org': 600, 'news.example.org': 60})
    body = {'blocks': [{'id': 'B1', 'text': 'Original evidence.'}], 'links': [],
            'meta': {'requestedUrl': 'https://news.example.org/a', 'finalUrl': 'https://news.example.org/b', 'capturedAt': 'original-time'}}
    cache.capture_put('https://news.example.org/a', 'scope', body, policy, now=100)
    assert cache.capture_get('https://news.example.org/b', 'scope', policy, now=150)['meta']['capturedAt'] == 'original-time'
    assert cache.capture_get('https://news.example.org/a', 'other-profile', policy, now=150) is None
    assert cache.capture_get('https://news.example.org/a', 'scope', policy, now=160) is None
    assert policy.ttl('https://other.example.org/') == 600
    assert policy.ttl('https://notexample.org/') == 21600
    state.close()
    state = State(path)
    cache = LocalCache(state)
    assert cache.capture_get('https://news.example.org/b', 'scope', policy, now=159)
    assert cache.capture_get('https://news.example.org/b', 'scope', CaptureCacheSettings(defaultTtlSeconds=0), now=159) is None
    cache.prune_capture(policy, now=161)
    assert not state.rows('SELECT * FROM capture_cache')
    assert not state.rows('SELECT * FROM capture_aliases')
    state.close()


def test_capture_cache_capacity_failed_pages_and_partial_settings(tmp_path):
    state = State(tmp_path / 'client.db')
    cache = LocalCache(state)
    policy = CaptureCacheSettings(maxEntries=1)
    for i in range(3):
        cache.capture_put(f'https://example.org/{i}', 'scope', {'blocks': [{'id': 'B1', 'text': str(i)}]}, policy, now=i)
    assert len(state.rows('SELECT * FROM capture_cache')) == 1
    cache.capture_put('https://example.org/bad', 'scope', {'status': {'access': 'blocked', 'load': 'ready'}, 'blocks': [{'text': 'captcha'}]}, policy)
    assert len(state.rows('SELECT * FROM capture_cache')) == 1
    state.save_config({'captureCache': {'domainTtlSeconds': {'example.org': 100}, 'maxEntries': 7}})
    state.save_config({'captureCache': {'defaultTtlSeconds': 200}})
    assert state.config().captureCache.maxEntries == 7
    assert state.config().captureCache.domainTtlSeconds == {'example.org': 100}
    with pytest.raises(ValueError):
        state.save_config({'captureCache': {'domainTtlSeconds': {'https://bad': 10}}})
    state.close()


class CachedBrowser(Browser):
    def __init__(self):
        super().__init__()
        self.captures = []

    async def capture(self, session, url, lease):
        self.captures.append(url)
        body = await super().capture(session, url, lease)
        body['meta']['capturedAt'] = 'fixed-original-capture'
        if url.endswith('/home'):
            body['links'] = [{'id': 'L1', 'text': 'Article'}]
            body['localMaps']['urls'] = {'L1': 'https://example.org/article'}
        else:
            body['links'] = [{'id': 'L1', 'text': 'Back to home'}]
            body['localMaps']['urls'] = {'L1': 'https://example.org/home?utm_source=loop'}
        return body


class Remote:
    def __init__(self):
        self.revision = 'a' * 64
        self.posts = []
        self.jobs = {}
        self.state = 'succeeded'

    async def __call__(self, config, method, path, payload=None, **kwargs):
        if path == '/v1/capabilities':
            return {'materialHashVersion': MATERIAL_HASH_VERSION, 'processingRevision': self.revision}
        if method == 'POST':
            self.posts.append(deepcopy(payload))
            result = {'jobId': payload['requestId'], 'state': self.state, 'selection': {
                'blockIds': [b['id'] for b in payload['blocks']], 'linkIds': [b['id'] for b in payload['links']]},
                'infos': [], 'assignments': [], 'graphs': []}
            self.jobs[result['jobId']] = result
            return deepcopy(result)
        result = deepcopy(self.jobs[path.rsplit('/', 1)[-1]])
        result['state'] = self.state
        return result


async def finished(daemon, run_id, state='succeeded'):
    for _ in range(600):
        current = daemon.state.run(run_id)
        if current['state'] == state:
            return current
        if current['state'] == 'failed' and state != 'failed':
            raise AssertionError(current)
        await asyncio.sleep(.01)
    raise AssertionError(daemon.state.run(run_id))


def test_cross_run_capture_and_submission_reuse_focus_revision_refresh_and_restart(tmp_path, monkeypatch):
    remote = Remote()
    monkeypatch.setattr('osbi_client.daemon.request', remote)
    async def run():
        browser = CachedBrowser()
        path = tmp_path / 'client'
        daemon = Daemon(path, browser=browser)
        daemon.state.save_config({'serverUrl': 'https://shared.example', 'serverKey': 'fake-key',
            'sources': [{'id': 'a', 'url': 'https://example.org/home'}]})
        await daemon.start()
        try:
            first = daemon.start_run('a')
            assert len((await finished(daemon, first['id']))['visited']) == 2
            assert len(browser.captures) == len(remote.posts) == 2
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(browser.captures) == len(remote.posts) == 2
            daemon.state.save_config({'focus': 'Different department'})
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(browser.captures) == 2 and len(remote.posts) == 4
            remote.revision = 'b' * 64
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(browser.captures) == 2 and len(remote.posts) == 6
            await finished(daemon, daemon.start_run('a', force_refresh=True)['id'])
            assert len(browser.captures) == 4 and len(remote.posts) == 6
        finally:
            await daemon.close()
        restored = Daemon(path, browser=browser)
        await restored.start()
        try:
            await finished(restored, restored.start_run('a')['id'])
            assert len(browser.captures) == 4 and len(remote.posts) == 6
        finally:
            await restored.close()
    asyncio.run(run())


def test_pending_material_is_shared_and_failure_is_retryable(tmp_path, monkeypatch):
    remote = Remote()
    remote.state = 'graph_pending'
    monkeypatch.setattr('osbi_client.daemon.request', remote)
    async def run():
        daemon = Daemon(tmp_path, browser=Browser())
        daemon.state.save_config({'serverUrl': 'https://shared.example', 'serverKey': 'fake-key',
            'sources': [{'id': i, 'url': 'https://example.org/article'} for i in ['a', 'b']]})
        await daemon.start()
        try:
            first, second = daemon.start_run('a'), daemon.start_run('b')
            for _ in range(500):
                rows = daemon.state.rows("SELECT * FROM outbox WHERE state='polling'")
                if len(rows) == 2:
                    break
                await asyncio.sleep(.01)
            assert len(rows) == 2 and len(remote.posts) == 1
            remote.state = 'failed'
            await finished(daemon, first['id'], 'failed')
            await finished(daemon, second['id'], 'failed')
            assert not daemon.state.rows('SELECT * FROM submission_cache')
            remote.state = 'succeeded'
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(remote.posts) == 2
        finally:
            await daemon.close()
    asyncio.run(run())


def test_skipped_redirect_alias_does_not_leave_a_finished_run_waiting(tmp_path):
    async def run():
        browser = Browser()
        daemon = Daemon(tmp_path, browser=browser)
        daemon.state.save_config({'serverUrl': 'https://shared.example', 'serverKey': 'fake-key',
            'sources': [{'id': 'a', 'url': 'https://example.org/home'}]})
        task = daemon.start_run('a')
        daemon.state.execute("UPDATE runs SET visited=?,queue=? WHERE id=?",
            (json.dumps(['https://example.org/home']), json.dumps(['https://example.org/home?utm_source=again']), task['id']))
        await daemon.acquire(task['id'])
        await daemon.acquire(task['id'])
        assert daemon.state.run(task['id'])['state'] == 'succeeded'
        assert not browser.calls
        await daemon.close()
    asyncio.run(run())


def test_client_server_hash_and_cache_contract_in_process(tmp_path):
    import httpx
    from fastapi.testclient import TestClient
    from osbi_server.app import create_app
    from osbi_server.config import Settings
    from osbi_server.service import Service
    from conftest import Model
    model = Model()
    service = Service(Settings(tmp_path / 'server'), model)
    key = service.repo.issue_key('c', time.time() + 1000)['key']
    with TestClient(create_app(service=service), base_url='https://shared.example') as server:
        posts = []
        def forward(request):
            if request.method == 'POST':
                posts.append(request.url.path)
            response = server.request(request.method, str(request.url), content=request.content, headers=dict(request.headers))
            return httpx.Response(response.status_code, headers=response.headers, content=response.content, request=request)
        async def run():
            browser = CachedBrowser()
            daemon = Daemon(tmp_path / 'client', browser=browser, transport=httpx.MockTransport(forward))
            daemon.state.save_config({'serverUrl': 'https://shared.example', 'serverKey': key,
                'sources': [{'id': 'a', 'url': 'https://example.org/article'}]})
            await daemon.start()
            try:
                await finished(daemon, daemon.start_run('a')['id'])
                await finished(daemon, daemon.start_run('a')['id'])
                assert len(posts) == len(browser.captures) == 1
                daemon.state.save_config({'focus': 'Different department'})
                await finished(daemon, daemon.start_run('a')['id'])
                assert len(posts) == 2 and len(browser.captures) == 1
                assert model.calls.count('ontology_entities') == model.calls.count('ontology_relations') == 1
                assert len(service.repo.rows('SELECT * FROM knowledge_units')) == 1
            finally:
                await daemon.close()
        asyncio.run(run())
