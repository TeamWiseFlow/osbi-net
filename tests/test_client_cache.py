"""URL acquisition reuse and material processing reuse remain independent."""
import asyncio
from copy import deepcopy
import json
import pytest
from osbi_client.cache_settings import CaptureCacheSettings
from osbi_client.daemon import Daemon
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

    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        self.captures.append(url)
        body = await super().capture(session, url, lease, seed_meta=seed_meta)
        body['meta']['capturedAt'] = 'fixed-original-capture'
        if url.endswith('/home'):
            body['links'] = [{'id': 'L1', 'text': 'Article'}]
            body['localMaps']['urls'] = {'L1': 'https://example.org/article'}
        else:
            body['links'] = [{'id': 'L1', 'text': 'Back to home'}]
            body['localMaps']['urls'] = {'L1': 'https://example.org/home?utm_source=loop'}
        return body


class Remote:
    """Synchronous wiseflow extract stub; selection errors simulate processing failure.

    An optional gate holds the response open (after recording the POST) so a
    second run must share the pending cache slot.
    """

    def __init__(self):
        self.posts = []
        self.fail_selection = False
        self.gate = None

    async def __call__(self, config, method, path, payload=None, **kwargs):
        assert method == 'POST' and path == '/v1/materials/extract'
        self.posts.append(deepcopy(payload))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_selection:
            return {'protocolVersion': 'material-extract/v3', 'materialId': payload['materialId'],
                    'selection': {'blockIds': [], 'linkIds': []},
                    'errors': [{'stage': 'selection', 'message': 'provider down'}]}
        return {'protocolVersion': 'material-extract/v3', 'materialId': payload['materialId'],
                'selection': {'blockIds': [b['id'] for b in payload['input']['blocks']],
                              'linkIds': [l['id'] for l in payload['input']['links']]},
                'ontologyJobs': [{'blockId': b['id'], 'id': 'oq_' + '0' * 64, 'status': 'pending'}
                                 for b in payload['input']['blocks']],
                'modelCalls': 1, 'errors': []}


async def finished(daemon, run_id, state='succeeded'):
    for _ in range(600):
        current = daemon.state.run(run_id)
        if current['state'] == state:
            return current
        if current['state'] == 'failed' and state != 'failed':
            raise AssertionError(current)
        await asyncio.sleep(.01)
    raise AssertionError(daemon.state.run(run_id))


def test_cross_run_capture_and_submission_reuse_focus_library_refresh_and_restart(tmp_path, monkeypatch):
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
            # library (channel) participates in the submission identity
            daemon.state.save_config({'sources': [{'id': 'a', 'url': 'https://example.org/home',
                                                   'channel': 'economy'}]})
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
    remote.gate = asyncio.Event()
    monkeypatch.setattr('osbi_client.daemon.request', remote)
    async def run():
        daemon = Daemon(tmp_path, browser=Browser())
        daemon.state.save_config({'serverUrl': 'https://shared.example', 'serverKey': 'fake-key',
            'sources': [{'id': i, 'url': 'https://example.org/article'} for i in ['a', 'b']]})
        await daemon.start()
        try:
            first, second = daemon.start_run('a'), daemon.start_run('b')
            for _ in range(500):
                rows = daemon.state.rows("SELECT * FROM outbox")
                if len(rows) == 2 and len(remote.posts) == 1 and any(r['state'] == 'waiting_local' for r in rows):
                    break
                await asyncio.sleep(.01)
            assert len(rows) == 2 and len(remote.posts) == 1
            remote.gate.set()
            await finished(daemon, first['id'])
            await finished(daemon, second['id'])
            assert len(remote.posts) == 1  # second run reused the cached extract result
            # a selection failure clears the cache and stays retryable
            failing = Remote()
            failing.fail_selection = True
            monkeypatch.setattr('osbi_client.daemon.request', failing)
            daemon.state.save_config({'focus': 'Failure phase'})
            await finished(daemon, daemon.start_run('a')['id'], 'failed')
            failed_row = daemon.state.one("SELECT * FROM outbox WHERE state='failed' ORDER BY rowid DESC")
            # the failed submission's cache slot is cleared (earlier-focus entries may survive)
            assert not daemon.state.rows('SELECT * FROM submission_cache WHERE owner_id=?', (failed_row['id'],))
            failing.fail_selection = False
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(failing.posts) == 2
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
    """The real wiseflow v3 pydantic contract validates every submitted payload."""
    import httpx
    from conftest import extract_app
    from wiseflow_server.models import ExtractResponse, Selection

    def blocks_only(request):
        return ExtractResponse(materialId=request.materialId,
                               selection=Selection(blockIds=[b.id for b in request.input.blocks],
                                                   linkIds=[]))

    app = extract_app(blocks_only)
    posts = []

    async def forward(request):
        if request.method == 'POST':
            posts.append(request.url.path)
        return await httpx.ASGITransport(app=app).handle_async_request(request)

    async def run():
        browser = CachedBrowser()
        daemon = Daemon(tmp_path / 'client', browser=browser, transport=httpx.MockTransport(forward))
        daemon.state.save_config({'serverUrl': 'http://127.0.0.1:8078',
            'sources': [{'id': 'a', 'url': 'https://example.org/article'}]})
        await daemon.start()
        try:
            await finished(daemon, daemon.start_run('a')['id'])
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(posts) == len(browser.captures) == 1
            daemon.state.save_config({'focus': 'Different department'})
            await finished(daemon, daemon.start_run('a')['id'])
            assert len(posts) == 2 and len(browser.captures) == 1
            daemon.state.save_config({'sources': [{'id': 'b', 'url': 'https://example.org/mirror'}]})
            await finished(daemon, daemon.start_run('b')['id'])
            assert len(posts) == 3 and len(browser.captures) == 2
            assert posts == ['/v1/materials/extract'] * 3
            # strict contract accepted the payloads: no local extras, no link urls
            for submitted in app.state.requests:
                assert 'urlMap' not in submitted and 'requestId' not in submitted
                assert all('url' not in link for link in submitted['input']['links'])
        finally:
            await daemon.close()
    asyncio.run(run())
