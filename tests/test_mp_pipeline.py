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

    async def capture(self, session, url, lease, seed_meta=None):
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
                         "accounts": ["第一财经", "经济日报"]}]}
    base.update(overrides)
    return base


def server_transport(select_links=("L1",)):
    remote = {}

    async def fake_request(config, method, path, payload=None, **kwargs):
        if method == "POST":
            key = payload["requestId"]
            if not payload["blocks"]:  # links-only catalog
                remote[key] = {"jobId": key, "state": "succeeded",
                               "selection": {"blockIds": [], "linkIds": list(select_links)}, "assignments": []}
            else:  # article body
                remote[key] = {"jobId": key, "state": "succeeded",
                               "selection": {"blockIds": ["B1"], "linkIds": []},
                               "assignments": [{"blockId": "B1", "channelId": "economy", "schemaId": "s"}]}
            return remote[key]
        return remote[path.rsplit("/", 1)[-1]]
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
            payloads = [json.loads(row["payload"]) for row in daemon.state.rows("SELECT * FROM outbox ORDER BY rowid")]
            catalog = payloads[0]
            assert catalog["blocks"] == [] and catalog["meta"]["adapter"] == "mp-catalog"
            assert catalog["meta"]["requestedUrl"] == SENTINEL
            body = payloads[1]
            assert body["blocks"] and body["meta"]["publisher"] == "第一财经"
            assert body["meta"]["adapter"] == "browser"
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


def test_mp_catalog_bypasses_jev_but_bodies_do_not(tmp_path, monkeypatch):
    monkeypatch.setattr("hunters.wx_mp.scan_mimicwx", lambda accounts, window: scan_records())
    monkeypatch.setattr("osbi_client.daemon.request", server_transport(select_links=("L1",)))

    def discard_handler(request):
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
            # catalog submitted despite Jev discard-all; article body discarded (not in outbox)
            assert len(payloads) == 1
            assert payloads[0]["meta"]["adapter"] == "mp-catalog"
            assert any(e["kind"] == "jev_discarded" for e in daemon.state.events())
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
