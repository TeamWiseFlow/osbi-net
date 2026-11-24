"""End-to-end mp acquisition through the real daemon loops (mocked scan/browser/server)."""
import asyncio
import json
import time

import httpx
import pytest

from osbi_client.daemon import Daemon
from hunters.wx_mp import MpScanError
from test_client import Browser

SENTINEL = "https://mp.weixin.qq.com/"
ART1 = "https://mp.weixin.qq.com/s/art1"
ART2 = "https://mp.weixin.qq.com/s/art2"


def scan_records():
    return [
        {"title": "文章一", "url": ART1, "author": "第一财经", "publish_time": "2026-11-20",
         "cover": "", "original_url": ART1},
        {"title": "文章二", "url": ART2, "author": "经济日报", "publish_time": "2026-11-19",
         "cover": "", "original_url": ART2},
    ]


class MpBrowser(Browser):
    def __init__(self):
        super().__init__()
        self.captured = []

    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        self.captured.append((url, seed_meta))
        seed = seed_meta or {}
        meta = {"title": seed.get("title", "文章"), "requestedUrl": url, "finalUrl": url,
                "canonicalUrl": url, "publisher": seed.get("publisher", ""),
                "publishedAt": seed.get("publishedAt", "")}
        return {"materialId": "body-" + url, "meta": meta,
                "blocks": [{"id": "B1", "text": "正文内容。"}], "links": [], "localMaps": {"urls": {}}}


def mp_config(**overrides):
    base = {"serverUrl": "https://example.test", "serverKey": "key", "maxUrls": 5,
            "sources": [{"id": "mp-economy", "url": SENTINEL, "kind": "mp",
                         "channels": ["economy"],
                         "accounts": ["第一财经", "经济日报"]}]}
    base.update(overrides)
    return base


def server_transport(select_links=("L1",)):
    async def fake_request(config, method, path, payload=None, **kwargs):
        assert method == "POST" and path == "/v1/materials/extract"
        assert payload["protocolVersion"] == "material-extract/v4"
        assert payload["channel"] == "economy" and "focus" not in payload
        assert all("url" not in link for link in payload["input"]["links"])
        if not payload["input"]["blocks"]:  # links-only catalog
            assert payload["mode"] == "only_link"
            selection = {"blockIds": [], "linkIds": list(select_links)}
            jobs = []
        else:  # article body
            assert payload["mode"] == "both"
            selection = {"blockIds": ["B1"], "linkIds": []}
            jobs = [{"blockId": "B1", "id": "oq_" + "0" * 64, "status": "pending"}]
        return {"protocolVersion": "material-extract/v4", "materialId": payload["materialId"],
                "selection": selection, "modelCalls": 1, "ontologyJobs": jobs, "errors": []}
    return fake_request


async def wait_for(daemon, run_id, predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = daemon.state.run(run_id)
        if predicate(current):
            return current
        await asyncio.sleep(0.03)
    raise AssertionError("timeout waiting for run: " + str(daemon.state.run(run_id)))


def test_mp_full_chain_catalog_selection_body(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: scan_records())
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=("L1",)))
    browser = MpBrowser()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser)
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            current = await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            # sentinel catalog + one selected article visited
            assert current["visited"] == [SENTINEL, ART1]
            # sentinel never opened in a browser; only the article was
            opened = [params.get("url") for action, params in browser.calls if action == "open"]
            assert SENTINEL not in opened and ART1 in opened
            # seed metadata flowed into the article capture
            assert browser.captured[0][0] == ART1
            assert browser.captured[0][1]["publisher"] == "第一财经"
            rows = daemon.state.rows("SELECT * FROM outbox ORDER BY rowid")
            payloads = [json.loads(row["payload"]) for row in rows]
            catalog = payloads[0]
            assert catalog["input"]["blocks"] == [] and catalog["meta"]["adapter"] == "mp-catalog"
            assert catalog["meta"]["requestedUrl"] == SENTINEL
            assert catalog["mode"] == "only_link"
            assert catalog["urlMap"]["L1"] == ART1 and catalog["urlMap"]["L2"] == ART2
            body = payloads[1]
            assert body["input"]["blocks"] and body["meta"]["publisher"] == "第一财经"
            assert body["meta"]["adapter"] == "browser"
            # synchronous extract: both submissions done, info receipts discarded
            assert all(row["state"] == "done" for row in rows)
            assert all("infos" not in json.loads(row["result"]) for row in rows)
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_full_chain_over_http_mock_transport(tmp_path, monkeypatch):
    """Catalog -> selection.linkIds -> local urlMap -> follow-up captures, over real HTTP wiring."""
    from conftest import extract_app
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: scan_records())
    app = extract_app()  # selects every catalog link, queues bodies for the nightly batch
    seen = []

    async def forward(request):
        seen.append((request.method, request.url.path))
        return await httpx.ASGITransport(app=app).handle_async_request(request)

    browser = MpBrowser()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser,
                        transport=httpx.MockTransport(forward))
        daemon.state.save_config(mp_config(serverUrl="http://127.0.0.1:8078", serverKey=""))
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            current = await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            assert current["visited"] == [SENTINEL, ART1, ART2]
            assert seen == [("POST", "/v1/materials/extract")] * 3
            assert len(app.state.requests) == 3
            catalog = app.state.requests[0]
            assert catalog["mode"] == "only_link" and catalog["channel"] == "economy"
            assert all("url" not in link for link in catalog["input"]["links"])
            assert "urlMap" not in catalog and "requestId" not in catalog
            rows = daemon.state.rows("SELECT * FROM outbox ORDER BY rowid")
            assert all(row["state"] == "done" for row in rows)
            assert all("infos" not in json.loads(row["result"]) for row in rows)
            assert json.loads(rows[1]["result"])["ontologyJobs"][0]["status"] == "pending"
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_zero_selection_finishes_without_capture(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: scan_records())
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=()))
    browser = MpBrowser()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser)
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            current = await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            assert current["visited"] == [SENTINEL]
            assert browser.captured == []
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_empty_catalog_succeeds_with_event(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: [])
    browser = MpBrowser()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser)
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            current = await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            assert not daemon.state.rows("SELECT * FROM outbox")
            assert any(e["kind"] == "mp_empty_catalog" for e in daemon.state.events())
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_scan_error_fails_run_with_message(tmp_path, monkeypatch):
    def boom(accounts, window):
        raise MpScanError("MimicWX container 'mimicwx-linux' is not running")
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", boom)

    async def run():
        daemon = Daemon(tmp_path / "client", browser=MpBrowser())
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            current = await wait_for(daemon, task["id"], lambda r: r["state"] == "failed")
            assert "not running" in current["error"]
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_account_health_warning(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: [scan_records()[0]])
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=("L1",)))

    async def run():
        daemon = Daemon(tmp_path / "client", browser=MpBrowser())
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            health = [e for e in daemon.state.events() if e["kind"] == "mp_account_health"]
            assert health and "经济日报" in health[0]["message"]
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_acquisition_never_calls_jev(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: scan_records())
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=("L1",)))
    calls = []

    def discard_handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        return httpx.Response(200, json={"answers": {
            key: {"type": "choice", "choice": "discard", "confidence": 0.99} for key in payload["questions"]}})

    async def run():
        daemon = Daemon(tmp_path / "client", browser=MpBrowser(),
                        decision_transport=httpx.MockTransport(discard_handler))
        daemon.state.save_config(mp_config(jev={"enabled": True, "baseUrl": "https://decision.example/v1",
                                                "apiKey": "decision-secret", "model": "fixture-model"}))
        await daemon.start()
        try:
            task = daemon.start_run("mp-economy")
            await wait_for(daemon, task["id"], lambda r: r["state"] == "succeeded")
            payloads = [json.loads(row["payload"]) for row in daemon.state.rows("SELECT * FROM outbox")]
            # Catalog and body both submit; Jev is post-query only.
            assert len(payloads) == 2
            assert calls == []
            assert not any(e["kind"] == "jev_discarded" for e in daemon.state.events())
        finally:
            await daemon.close()
    asyncio.run(run())


def test_mp_catalog_cache_reused_across_runs(tmp_path, monkeypatch):
    calls = []

    def counting_scan(accounts, window):
        calls.append(1)
        return scan_records()
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", counting_scan)
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=()))

    async def run():
        daemon = Daemon(tmp_path / "client", browser=MpBrowser())
        daemon.state.save_config(mp_config())
        await daemon.start()
        try:
            first = daemon.start_run("mp-economy")
            await wait_for(daemon, first["id"], lambda r: r["state"] == "succeeded")
            second = daemon.start_run("mp-economy")
            await wait_for(daemon, second["id"], lambda r: r["state"] == "succeeded")
            assert len(calls) == 1  # second run reused the cached catalog
        finally:
            await daemon.close()
    asyncio.run(run())
