"""Idle camoufox cleanup: profiles without an active run release their browser."""
import asyncio
import json
import time

from osbi_client.daemon import Daemon


def session(name, *, running=True, state="ready", leased=False):
    return {"session": name, "running": running, "state": state, "leased": leased}


class IdleBrowser:
    """Fake managed runtime exposing the CLI sessions/close surface."""

    def __init__(self, sessions=()):
        self.sessions = {s["session"]: dict(s) for s in sessions}
        self.calls = []

    async def command(self, session, action, params=None, lease=None, **kwargs):
        self.calls.append((session, action))
        if action == "sessions":
            return {"sessions": [dict(s) for s in self.sessions.values()]}
        if action == "close":
            self.sessions.pop(session, None)
            return {"closed": True}
        if action == "lease.acquire":
            return {"leaseToken": "lease-test"}
        return {}

    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        return {"materialId": "m", "meta": {"title": "t", "requestedUrl": url, "finalUrl": url, "canonicalUrl": url},
                "blocks": [{"id": "B1", "text": "正文内容。"}], "links": [], "localMaps": {"urls": {}}}


def insert_run(daemon, run_id, profile, state, url="https://example.org/"):
    source = {"id": run_id, "url": url, "kind": "web", "profile": profile, "enabled": True,
              "intervalMinutes": 0, "steps": [], "accounts": [], "channel": "policy", "focusId": 0, "focuspoint": ""}
    now = time.time()
    daemon.state.execute(
        "INSERT INTO runs(id,source,config,state,queue,browser,created,updated) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, json.dumps(source), json.dumps(daemon.state.config().model_dump()), state,
         json.dumps([url]), "{}", now, now))


def test_idle_profile_browser_closed(tmp_path):
    browser = IdleBrowser([session("default")])
    daemon = Daemon(tmp_path / "client", browser=browser)
    insert_run(daemon, "run_done", "default", "succeeded")
    asyncio.run(daemon._close_idle_browsers())
    assert ("default", "close") in browser.calls
    assert "default" not in browser.sessions
    assert any(e["kind"] == "browser_closed" for e in daemon.state.events())
    daemon.state.close()


def test_active_and_waiting_profiles_keep_browsers(tmp_path):
    states = {"pending": "p-pending", "running": "p-running", "waiting_for_user": "p-wait",
              "waiting_server": "p-server", "interrupted": "p-interrupted"}
    browser = IdleBrowser([session(p) for p in states.values()] + [session("idle")])
    daemon = Daemon(tmp_path / "client", browser=browser)
    for state, profile in states.items():
        insert_run(daemon, "run_" + state, profile, state)
    asyncio.run(daemon._close_idle_browsers())
    closed = [s for s, a in browser.calls if a == "close"]
    assert closed == ["idle"]  # waiting_for_user keeps the page for the human
    daemon.state.close()


def test_leased_busy_and_starting_sessions_never_closed(tmp_path):
    browser = IdleBrowser([session("leased", leased=True), session("busy", state="busy"),
                           session("human", state="waiting_human"), session("starting", state="starting"),
                           session("stopped", running=False, state="stopped"), session("idle")])
    daemon = Daemon(tmp_path / "client", browser=browser)
    asyncio.run(daemon._close_idle_browsers())
    closed = [s for s, a in browser.calls if a == "close"]
    assert closed == ["idle"]
    daemon.state.close()


def test_run_loop_closes_browser_after_run_completes(tmp_path, monkeypatch):
    monkeypatch.setattr("osbi_client.daemon.IDLE_BROWSER_SWEEP_SECONDS", 0)

    async def fake_request(config, method, path, payload=None, **kwargs):
        return {"protocolVersion": "material-extract/v3", "materialId": payload["materialId"],
                "selection": {"blockIds": [b["id"] for b in payload["input"]["blocks"]], "linkIds": []},
                "ontologyJobs": [], "modelCalls": 1, "errors": []}

    monkeypatch.setattr("osbi_client.daemon.request", fake_request)
    browser = IdleBrowser([session("default")])

    async def run():
        daemon = Daemon(tmp_path / "client", browser=browser)
        daemon.state.save_config({"serverUrl": "https://example.test", "serverKey": "key",
                                  "sources": [{"id": "a", "url": "https://example.org"}]})
        await daemon.start()
        try:
            task = daemon.start_run("a")
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                if daemon.state.run(task["id"])["state"] == "succeeded":
                    break
                await asyncio.sleep(0.05)
            assert daemon.state.run(task["id"])["state"] == "succeeded"
            while time.monotonic() < deadline:
                if ("default", "close") in browser.calls:
                    break
                await asyncio.sleep(0.05)
            assert ("default", "close") in browser.calls  # camoufox released after the run
        finally:
            await daemon.close()

    asyncio.run(run())


def test_daemon_close_sweeps_idle_but_keeps_waiting_browser(tmp_path):
    browser = IdleBrowser([session("default"), session("waitprofile")])
    daemon = Daemon(tmp_path / "client", browser=browser)
    insert_run(daemon, "run_done", "default", "succeeded")
    insert_run(daemon, "run_wait", "waitprofile", "waiting_for_user")
    asyncio.run(daemon.close())
    closed = [s for s, a in browser.calls if a == "close"]
    assert closed == ["default"]
    assert "waitprofile" in browser.sessions
