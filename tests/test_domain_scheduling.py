"""Per-domain scheduling, forbidden-fragment admission and production retry delays."""
import asyncio
import json
import time

import pytest

from osbi_client.browser import BrowserError
from osbi_client.daemon import Daemon
from osbi_client.models import ClientConfig

POLICY = {"defaultConcurrency": 1, "domains": {"mp.weixin.qq.com": 2, "gov.cn": 3}}


class FakeBrowser:
    def __init__(self, failure=None, links=None):
        self.failure = failure
        self.links = links or {}
        self.calls = []

    async def command(self, session, action, params=None, lease=None, **kwargs):
        self.calls.append((session, action))
        if action == "lease.acquire":
            return {"leaseToken": "lease-test"}
        return {}

    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        if self.failure:
            raise BrowserError(self.failure, "fixture capture failure")
        links = self.links.get(url, [])
        return {"materialId": "m", "meta": {"title": "t", "requestedUrl": url, "finalUrl": url, "canonicalUrl": url},
                "blocks": [{"id": "B1", "text": "正文内容。"}],
                "links": [{"id": lid, "text": "链接"} for lid, _ in links],
                "localMaps": {"urls": dict(links)}}


def insert_run(daemon, run_id, profile, state, url, *, queue=None, browser=None, created=None):
    source = {"id": run_id, "url": url, "kind": "web", "profile": profile, "enabled": True,
              "intervalMinutes": 0, "steps": [], "accounts": [], "channel": "policy", "focusId": 0, "focuspoint": ""}
    now = created or time.time()
    daemon.state.execute(
        "INSERT INTO runs(id,source,config,state,queue,browser,created,updated) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, json.dumps(source), json.dumps(daemon.state.config().model_dump()), state,
         json.dumps([url] if queue is None else queue), json.dumps(browser or {}), now, now))


async def schedule_once(daemon):
    before = set(daemon.running)
    daemon._schedule_pending(daemon.state.config())
    started = set(daemon.running) - before
    for task in daemon.running.values():
        task.cancel()
    await asyncio.gather(*daemon.running.values(), return_exceptions=True)
    daemon.running.clear()
    return started


def make_daemon(tmp_path, browser=None, **config):
    daemon = Daemon(tmp_path / "client", browser=browser or FakeBrowser())
    base = {"serverUrl": "https://example.test", "serverKey": "key", "browserConcurrency": 6,
            "domainConcurrency": POLICY, "sources": []}
    base.update(config)
    daemon.state.save_config(base)
    return daemon


def test_per_domain_limits_schedule_independently(tmp_path):
    daemon = make_daemon(tmp_path)
    insert_run(daemon, "r1", "p1", "running", "https://mp.weixin.qq.com/s/a", created=1)
    insert_run(daemon, "r2", "p2", "running", "https://mp.weixin.qq.com/s/b", created=2)
    insert_run(daemon, "r3", "p3", "running", "https://www.gov.cn/x", created=3)
    insert_run(daemon, "r4", "p4", "running", "https://gov.cn/y", created=4)
    insert_run(daemon, "r5", "p5", "pending", "https://mp.weixin.qq.com/s/c", created=5)  # mp full (2)
    insert_run(daemon, "r6", "p6", "pending", "https://www.gov.cn/z", created=6)  # gov 2/3 -> admit
    insert_run(daemon, "r7", "p7", "pending", "https://example.org/a", created=7)  # default 1 -> admit
    insert_run(daemon, "r8", "p8", "pending", "https://example.org/b", created=8)  # default now full
    started = asyncio.run(schedule_once(daemon))
    assert started == {"r6", "r7"}
    daemon.state.close()


def test_global_browser_concurrency_caps_total(tmp_path):
    daemon = make_daemon(tmp_path, browserConcurrency=2)
    insert_run(daemon, "r1", "p1", "running", "https://a.example/1", created=1)
    insert_run(daemon, "r2", "p2", "running", "https://b.example/1", created=2)
    insert_run(daemon, "r3", "p3", "pending", "https://c.example/1", created=3)

    async def scenario():
        # The global cap counts live acquisition tasks, as in production's
        # in-flight scheduler; register the two running rows as tasks.
        async def holder():
            await asyncio.sleep(60)
        daemon.running["r1"] = asyncio.create_task(holder())
        daemon.running["r2"] = asyncio.create_task(holder())
        return await schedule_once(daemon)

    assert asyncio.run(scenario()) == set()
    daemon.state.close()


def test_domain_gap_and_cooldown_gate_new_starts(tmp_path):
    daemon = make_daemon(tmp_path)
    insert_run(daemon, "r1", "p1", "pending", "https://example.org/a", created=1)
    insert_run(daemon, "r2", "p2", "pending", "https://example.org/b", created=2, browser={"pageReady": True})
    daemon._domain_gap["example.org"] = time.time() + 30
    started = asyncio.run(schedule_once(daemon))
    assert started == {"r2"}  # human-confirmed resume bypasses the gap
    daemon.state.close()


def test_retry_not_before_gates_even_resuming_runs(tmp_path):
    daemon = make_daemon(tmp_path)
    insert_run(daemon, "r1", "p1", "pending", "https://example.org/a", created=1,
               browser={"pageReady": True, "retryNotBefore": time.time() + 30})
    assert asyncio.run(schedule_once(daemon)) == set()
    daemon.state.close()


def test_verification_cooldown_uses_base_domain_and_60s(tmp_path):
    daemon = make_daemon(tmp_path, browser=FakeBrowser(failure="VERIFICATION_REQUIRED"),
                         sources=[{"id": "a", "url": "https://www.gov.cn/x"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.run(run["id"])["state"] == "waiting_for_user"
    row = daemon.state.one("SELECT * FROM domain_cooldowns WHERE domain='gov.cn'")
    assert row and row["strikes"] == 1
    assert 55 < row["next_at"] - time.time() <= 61  # verificationDelaysMs [60000]
    waiting = daemon.state.run(run["id"])
    daemon.act(run["id"], "resume", waiting["prompt"])
    asyncio.run(daemon.acquire(run["id"]))
    row = daemon.state.one("SELECT * FROM domain_cooldowns WHERE domain='gov.cn'")
    assert row["strikes"] == 2
    assert 55 < row["next_at"] - time.time() <= 61  # single-entry delay list stays 60s
    daemon.state.close()


def test_wechat_verification_does_not_cool_the_domain(tmp_path):
    # Production batch-queue skips WeChat verification URLs without a cooldown;
    # osbi keeps the human prompt but must not cool mp.weixin.qq.com down.
    daemon = make_daemon(tmp_path, browser=FakeBrowser(failure="VERIFICATION_REQUIRED"),
                         sources=[{"id": "a", "url": "https://mp.weixin.qq.com/s/x"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.run(run["id"])["state"] == "waiting_for_user"
    assert daemon.state.one("SELECT * FROM domain_cooldowns") is None
    daemon.state.close()


def test_successful_capture_resets_verification_streak(tmp_path):
    browser = FakeBrowser(failure="VERIFICATION_REQUIRED")
    daemon = make_daemon(tmp_path, browser=browser, sources=[{"id": "a", "url": "https://www.gov.cn/x"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.one("SELECT strikes FROM domain_cooldowns WHERE domain='gov.cn'")["strikes"] == 1
    browser.failure = None
    waiting = daemon.state.run(run["id"])
    daemon.act(run["id"], "resume", waiting["prompt"])
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.one("SELECT strikes FROM domain_cooldowns WHERE domain='gov.cn'")["strikes"] == 0
    daemon.state.close()


def test_capture_retry_delays_20s_40s_then_fail(tmp_path):
    daemon = make_daemon(tmp_path, browser=FakeBrowser(failure="PAGE_CHANGED"),
                         sources=[{"id": "a", "url": "https://example.org/x"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    current = daemon.state.run(run["id"])
    browser_state = json.loads(daemon.state.one("SELECT browser FROM runs WHERE id=?", (run["id"],))["browser"])
    assert current["state"] == "pending" and browser_state["captureRetries"] == 1
    assert 15 < browser_state["retryNotBefore"] - time.time() <= 21  # retryDelaysMs[0]
    asyncio.run(daemon.acquire(run["id"]))
    browser_state = json.loads(daemon.state.one("SELECT browser FROM runs WHERE id=?", (run["id"],))["browser"])
    assert browser_state["captureRetries"] == 2
    assert 35 < browser_state["retryNotBefore"] - time.time() <= 41  # retryDelaysMs[1]
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.run(run["id"])["state"] == "failed"  # retry limit exhausted
    retries = [e for e in daemon.state.events() if e["kind"] == "capture_retry"]
    assert len(retries) == 2
    daemon.state.close()


def test_capture_empty_is_not_retried(tmp_path):
    daemon = make_daemon(tmp_path, browser=FakeBrowser(failure="CAPTURE_EMPTY"),
                         sources=[{"id": "a", "url": "https://example.org/x"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.run(run["id"])["state"] == "failed"
    assert not [e for e in daemon.state.events() if e["kind"] == "capture_retry"]
    daemon.state.close()


async def select_all_links(config, method, path, payload=None, **kwargs):
    return {"protocolVersion": "material-extract/v3", "materialId": payload["materialId"],
            "selection": {"blockIds": [b["id"] for b in payload["input"]["blocks"]],
                          "linkIds": [l["id"] for l in payload["input"]["links"]]},
            "ontologyJobs": [], "modelCalls": 1, "errors": []}


async def wait_state(daemon, run_id, state, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = daemon.state.run(run_id)
        if current["state"] == state:
            return current
        await asyncio.sleep(0.05)
    raise AssertionError(f"run never reached {state}: {daemon.state.run(run_id)['state']}")


def test_forbidden_fragments_block_link_back_by_substring(tmp_path, monkeypatch):
    monkeypatch.setattr("osbi_client.daemon.request", select_all_links)
    links = {"https://seed.example/home": [
        ("L1", "https://blocked.example/path/a"),   # domain+path prefix fragment
        ("L2", "https://ok.example/a"),
        ("L3", "https://other.example/x")]}        # whole-domain fragment
    browser = FakeBrowser(links=links)

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser)
        daemon.state.save_config({"serverUrl": "https://example.test", "serverKey": "key",
                                  "forbiddenDomains": ["blocked.example/path", "other.example"],
                                  "sources": [{"id": "a", "url": "https://seed.example/home"}]})
        await daemon.start()
        try:
            task = daemon.start_run("a")
            current = await wait_state(daemon, task["id"], "succeeded")
            assert current["visited"] == ["https://seed.example/home", "https://ok.example/a"]
            assert not any("blocked.example" in u or "other.example" in u for u in current["queue"])
        finally:
            await daemon.close()

    asyncio.run(run())


def test_explicit_seed_url_is_exempt_from_fragments(tmp_path, monkeypatch):
    # Production general_process: task-configured depth-0 seeds bypass the filter.
    monkeypatch.setattr("osbi_client.daemon.request", select_all_links)

    async def run():
        daemon = Daemon(tmp_path / "client", browser=FakeBrowser())
        daemon.state.save_config({"serverUrl": "https://example.test", "serverKey": "key",
                                  "forbiddenDomains": ["seed.example"],
                                  "sources": [{"id": "a", "url": "https://seed.example/home"}]})
        await daemon.start()
        try:
            task = daemon.start_run("a")
            current = await wait_state(daemon, task["id"], "succeeded")
            assert current["visited"] == ["https://seed.example/home"]
        finally:
            await daemon.close()

    asyncio.run(run())


def test_rss_entries_filtered_by_fragments(tmp_path, monkeypatch):
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>blocked</title><link>https://blocked.example/a</link></item>
    <item><title>allowed</title><link>https://allowed.example/a</link></item>
    </channel></rss>"""

    class Response:
        content = feed.encode()

        def raise_for_status(self):
            return None

    class FeedClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url):
            return Response()

    monkeypatch.setattr("osbi_client.daemon.httpx.AsyncClient", FeedClient)
    daemon = make_daemon(tmp_path, maxUrls=1, forbiddenDomains=["blocked.example"],
                         sources=[{"id": "a", "url": "https://feed.example/rss", "kind": "rss"}])
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    current = daemon.state.run(run["id"])
    assert current["visited"] == ["https://allowed.example/a"]
    daemon.state.close()


def test_save_config_domain_policy_merge_and_public_shape(tmp_path):
    daemon = make_daemon(tmp_path)
    public = daemon.state.save_config({"domainConcurrency": 2})
    assert public["domainConcurrency"] == {"defaultConcurrency": 2, "domains": {}}
    public = daemon.state.save_config({"domainConcurrency": {"domains": {"gov.cn": 3}}})
    assert public["domainConcurrency"] == {"defaultConcurrency": 2, "domains": {"gov.cn": 3}}
    public = daemon.state.save_config({"domainConcurrency": 1})
    assert public["domainConcurrency"] == {"defaultConcurrency": 1, "domains": {}}
    public = daemon.state.save_config({"domainConcurrency": POLICY,
                                       "forbiddenDomains": ["www.gov.cn/home", "english.shanghai.gov.cn/"]})
    assert public["domainConcurrency"] == POLICY
    assert public["forbiddenDomains"] == ["www.gov.cn/home", "english.shanghai.gov.cn/"]
    assert daemon.state.config().domainConcurrency.limit_for("gov.cn") == 3
    daemon.state.close()
