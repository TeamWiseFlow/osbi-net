"""Local settings privacy, origin checks, durable prompts and acquisition."""
import asyncio
import json
from pathlib import Path
import time
from fastapi.testclient import TestClient
from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.state import State
from osbi_client.browser import BrowserError
from osbi_client.models import ClientConfig


class Browser:
    def __init__(self):
        self.calls=[]
    async def command(self, session, action, params=None, lease=None, **kwargs):
        self.calls.append((action,params))
        return {"leaseToken":"lease-test"} if action=="lease.acquire" else {}
    async def capture(self,session,url,lease,seed_meta=None,enricher=None):
        return {"materialId":"sample-id","meta":{"title":"动态","requestedUrl":url,"finalUrl":url,"canonicalUrl":url},
                "blocks":[{"id":"B1","text":"甲科技公司参与产品发布会。"}],"links":[],"localMaps":{"urls":{}}}


def test_browser_finds_backend_package_and_project_node_independently_of_custom_root(tmp_path, monkeypatch):
    import osbi_client.browser as module
    project = tmp_path / "checkout"
    node = project / ".osbi/bin/node"
    node.parent.mkdir(parents=True)
    node.write_text("fixture; never executed")
    monkeypatch.setattr(module, "__file__", str(project / "client/backend/osbi_client/browser.py"))
    monkeypatch.delenv("OSBI_BROWSER_ROOT", raising=False)
    monkeypatch.delenv("OSBI_BROWSER_NODE", raising=False)
    default = module.Browser(tmp_path / "state")
    assert default.root == project / "client/backend/browser"
    assert default.node == str(node)
    custom = module.Browser(tmp_path / "state", root=tmp_path / "external-runtime")
    assert custom.root == tmp_path / "external-runtime"
    assert custom.node == str(node)


def test_acquisition_uses_production_fixed_identity(tmp_path, monkeypatch):
    # Production camoufox_runtime runs geoip:false with a fixed zh-CN locale
    # and 1440x1000 window; government WAFs tarpit the geoip-derived identity.
    # See browser.py command() and the fgj.sh.gov.cn A/B verification.
    from osbi_client.browser import Browser as ManagedBrowser
    root = tmp_path / "runtime"
    (root / "dist").mkdir(parents=True)
    (root / "dist/cli.js").write_text("// Fixture; never executed.\n")
    captured = []
    class Process:
        async def communicate(self, data):return b'{"ok":true,"data":{}}', b''
    async def spawn(*args, **kwargs):
        captured.extend(args)
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("OSBI_BROWSER_NODE", "isolated-node-fixture")
    asyncio.run(ManagedBrowser(tmp_path / "state", root=root).command("fixture", "start"))
    assert "--no-geoip" in captured and "--geoip" not in captured
    assert captured[captured.index("--locale") + 1] == "zh-CN"
    assert captured[captured.index("--viewport") + 1] == "1440x1000"


def test_geoip_warning_is_persisted_without_interrupting_browser_commands(tmp_path, monkeypatch):
    daemon = Daemon(tmp_path / "client")
    root = tmp_path / "runtime"
    (root / "dist").mkdir(parents=True)
    (root / "dist/cli.js").write_text("// Fixture; never executed.\n")
    daemon.browser.root = root
    daemon.browser.node = "isolated-node-fixture"
    class Process:
        async def communicate(self, data):
            return json.dumps({"ok": True, "data": {"generation": "fixture", "running": True,
                "warnings": [{"code": "GEOIP_UNAVAILABLE", "message": "fixture"}]}}).encode(), b''
    async def spawn(*args, **kwargs):return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(daemon.browser.command("fixture", "start"))["running"]
    assert asyncio.run(daemon.browser.command("fixture", "start"))["running"]
    events = [e for e in daemon.state.events() if e["kind"] == "browser_warning"]
    assert len(events) == 1 and events[0]["level"] == "warning"
    assert "可能会增加暴露的风险，降低获取成功率" in events[0]["message"]
    daemon.state.close()


def test_headed_mode_is_sticky_across_session_commands(tmp_path, monkeypatch):
    from osbi_client.browser import Browser as ManagedBrowser
    root = tmp_path / "runtime"
    (root / "dist").mkdir(parents=True)
    (root / "dist/cli.js").write_text("// Fixture; never executed.\n")
    captured = []
    class Process:
        async def communicate(self, data): return b'{"ok":true,"data":{}}', b''
    async def spawn(*args, **kwargs):
        captured.append(list(args))
        return Process()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("OSBI_BROWSER_NODE", "isolated-node-fixture")
    browser = ManagedBrowser(tmp_path / "state", root=root)
    asyncio.run(browser.command("s", "start", {"headless": False}, headed=True))
    asyncio.run(browser.command("s", "open", {"url": "https://example.org"}))
    asyncio.run(browser.command("other", "open", {"url": "https://example.org"}))
    assert "--headed" in captured[0]      # start headed
    assert "--headed" in captured[1]      # open keeps headed for the same session
    assert "--headed" not in captured[2]  # unrelated session stays headless


def _gate_browser(tmp_path, monkeypatch, probe_states):
    from osbi_client.browser import Browser as ManagedBrowser
    browser = ManagedBrowser(tmp_path / "state")
    calls = []
    states = iter(probe_states)
    async def fake_command(session, action, params=None, lease=None, *, headed=False, timeout=75):
        expr = (params or {}).get("expression")
        calls.append(expr)
        if action == "eval" and expr == wx_probe():
            return {"result": next(states)}
        if action == "eval" and expr == wx_click():
            return {"result": True}
        return {}
    async def nosleep(*a, **k): return None
    monkeypatch.setattr(browser, "command", fake_command)
    monkeypatch.setattr("osbi_client.browser.asyncio.sleep", nosleep)
    return browser, calls


def wx_probe():
    from osbi_client.browser import _WECHAT_PROBE
    return _WECHAT_PROBE


def wx_click():
    from osbi_client.browser import _WECHAT_CLICK_VERIFY
    return _WECHAT_CLICK_VERIFY


def test_wechat_gate_article_present_skips_click(tmp_path, monkeypatch):
    browser, calls = _gate_browser(tmp_path, monkeypatch, [{"article": "正文", "gate": False}])
    asyncio.run(browser._wechat_gate("s", "lease"))
    assert wx_click() not in calls


def test_wechat_gate_clicks_verify_once_then_article(tmp_path, monkeypatch):
    browser, calls = _gate_browser(tmp_path, monkeypatch,
                                   [{"article": "", "gate": True}, {"article": "正文", "gate": False}])
    asyncio.run(browser._wechat_gate("s", "lease"))
    assert calls.count(wx_click()) == 1


def test_wechat_gate_persistent_gate_clicks_only_once(tmp_path, monkeypatch):
    browser, calls = _gate_browser(tmp_path, monkeypatch,
                                   [{"article": "", "gate": True}] * 15)
    asyncio.run(browser._wechat_gate("s", "lease"))
    assert calls.count(wx_click()) == 1  # never loops clicking; slider left to human


def test_waiting_for_user_expires_after_cache_ttl(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"serverUrl": "https://shared.example", "serverKey": "k",
        "sources": [{"id": "s", "url": "https://example.org"}],
        "captureCache": {"defaultTtlSeconds": 60, "domainTtlSeconds": {}}})
    run = daemon.start_run("s")
    config = daemon.state.config()
    daemon.state.execute("UPDATE runs SET state='waiting_for_user',prompt='p1',updated=? WHERE id=?",
                         (time.time(), run["id"]))
    daemon._expire_stale_prompts(config)
    assert daemon.state.run(run["id"])["state"] == "waiting_for_user"  # within TTL survives
    daemon.state.execute("UPDATE runs SET updated=? WHERE id=?", (time.time() - 120, run["id"]))
    daemon._expire_stale_prompts(config)
    expired = daemon.state.run(run["id"])
    assert expired["state"] == "cancelled" and "超时" in expired["error"]
    assert any(e["kind"] == "run_cancelled" for e in daemon.state.events())
    daemon.state.close()


def test_waiting_for_user_fallback_when_cache_disabled(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"serverUrl": "https://shared.example", "serverKey": "k",
        "sources": [{"id": "s", "url": "https://example.org"}],
        "captureCache": {"defaultTtlSeconds": 0, "domainTtlSeconds": {}}})
    run = daemon.start_run("s")
    daemon.state.execute("UPDATE runs SET state='waiting_for_user',prompt='p1',updated=? WHERE id=?",
                         (time.time() - 120, run["id"]))
    daemon._expire_stale_prompts(daemon.state.config())
    # TTL 0 falls back to 7200s; 120s wait must not expire
    assert daemon.state.run(run["id"])["state"] == "waiting_for_user"
    daemon.state.close()


def test_settings_are_local_masked_validated_and_survive_restart(tmp_path):
    data=tmp_path/"client"
    daemon=Daemon(data,browser=Browser())
    with TestClient(create_app(daemon=daemon),base_url="http://127.0.0.1") as c:
        assert c.get("/api/config").status_code==401
        c.get("/settings")
        h={"X-OSBI-Local":"1"}
        r=c.patch("/api/config",headers=h,json={"serverUrl":"https://shared.example","serverKey":"secret-not-returned"})
        assert r.status_code==200,r.text
        assert "secret-not-returned" not in r.text and r.json()["keyConfigured"]
        assert c.patch("/api/config",headers=h,json={"serverUrl":"http://bad.example"}).status_code==400
        assert c.get("/api/config").json()["serverUrl"]=="https://shared.example"
        assert c.patch("/api/config",headers={**h,"Origin":"https://evil.example"},json={"focus":"changed"}).status_code==403
        assert c.patch("/api/config",json={"focus":"changed"}).status_code==403
    state=State(data/"client.db")
    assert state.config().serverKey=="secret-not-returned"
    assert state.public_config()["revision"]==2
    state.close()


def test_workflow_resume_keeps_current_page_and_outbox_is_durable(tmp_path):
    browser=Browser();daemon=Daemon(tmp_path/"client",browser=browser)
    daemon.state.save_config({"serverUrl":"https://example.test","serverKey":"fake-key",
                             "sources":[{"id":"web","url":"https://example.org","steps":[{"action":"wait_for_user","text":"测试人工验证"}]}]})
    run=daemon.start_run("web")
    asyncio.run(daemon.acquire(run["id"]))
    waiting=daemon.state.run(run["id"])
    assert waiting["state"]=="waiting_for_user"
    assert sum(x[0]=="open" for x in browser.calls)==1
    daemon.act(run["id"],"resume",waiting["prompt"])
    asyncio.run(daemon.acquire(run["id"]))
    assert sum(x[0]=="open" for x in browser.calls)==1
    assert len(daemon.state.rows("SELECT * FROM outbox WHERE state='pending'"))==1
    daemon.state.close()
    restored=State(tmp_path/"client/client.db")
    assert restored.run(run["id"])["state"]=="waiting_server"
    assert len(restored.rows("SELECT * FROM outbox"))==1
    restored.close()


def test_prompt_acknowledgement_and_events_are_idempotent(tmp_path):
    daemon=Daemon(tmp_path/"client",browser=Browser())
    daemon.state.save_config({"serverUrl":"https://example.test","serverKey":"key","sources":[{"id":"a","url":"https://example.org"}]})
    run=daemon.start_run("a")
    daemon.prompt(run["id"],"请验证")
    prompt=daemon.state.run(run["id"])["prompt"]
    daemon.act(run["id"],"resume",prompt)
    daemon.act(run["id"],"resume",prompt)
    events=daemon.state.events()
    assert len([e for e in events if e["kind"]=="prompt_resolved"])==1
    assert [e["id"] for e in events]==sorted({e["id"] for e in events})
    cursor=events[-2]["id"]
    assert [e["id"] for e in daemon.state.events(cursor)]==[events[-1]["id"]]
    daemon.state.close()


def test_unknown_dispatched_step_is_not_replayed_after_confirmed_resume(tmp_path):
    class UncertainBrowser(Browser):
        async def command(self,session,action,params=None,lease=None,**kwargs):
            value=await super().command(session,action,params,lease,**kwargs)
            if action=='click':raise BrowserError('OUTCOME_UNKNOWN','请检查当前步骤并完成操作后继续')
            return value
    browser=UncertainBrowser();daemon=Daemon(tmp_path/'client',browser=browser)
    daemon.state.save_config({'serverUrl':'https://example.test','serverKey':'key','sources':[
        {'id':'a','url':'https://example.org','steps':[{'action':'click','selector':'#go'}]}]})
    run=daemon.start_run('a');asyncio.run(daemon.acquire(run['id']))
    waiting=daemon.state.run(run['id']);assert waiting['state']=='waiting_for_user'
    daemon.act(run['id'],'resume',waiting['prompt']);asyncio.run(daemon.acquire(run['id']))
    assert sum(c[0]=='click' for c in browser.calls)==1
    assert sum(c[0]=='open' for c in browser.calls)==1
    assert len(daemon.state.rows('SELECT * FROM outbox'))==1
    daemon.state.close()


def test_selected_links_are_acquired_without_waiting_for_batch_graphs(tmp_path,monkeypatch):
    class LinkedBrowser(Browser):
        async def capture(self,session,url,lease,seed_meta=None,enricher=None):
            value=await super().capture(session,url,lease,seed_meta=seed_meta)
            if url.endswith('/home'):
                value['links']=[{'id':'L1','text':'下一篇'}]
                value['localMaps']['urls']={'L1':'https://example.org/article'}
            return value
    async def fake_request(config,method,path,payload=None,**kwargs):
        # wiseflow extract is synchronous; ontology receipts stay pending (nightly batch)
        assert method=='POST' and path=='/v1/materials/extract'
        return {'protocolVersion':'material-extract/v3','materialId':payload['materialId'],
                'selection':{'blockIds':[b['id'] for b in payload['input']['blocks']],
                             'linkIds':[l['id'] for l in payload['input']['links']]},
                'ontologyJobs':[{'blockId':b['id'],'id':'oq_'+'0'*64,'status':'pending'}
                                for b in payload['input']['blocks']],
                'modelCalls':1,'errors':[]}
    monkeypatch.setattr('osbi_client.daemon.request',fake_request)
    async def run():
        daemon=Daemon(tmp_path/'client',browser=LinkedBrowser())
        daemon.state.save_config({'serverUrl':'http://127.0.0.1:8078','maxUrls':2,
                                 'sources':[{'id':'a','url':'https://example.org/home'}]})
        await daemon.start()
        try:
            task=daemon.start_run('a')
            for _ in range(100):
                current=daemon.state.run(task['id'])
                if current['state']=='succeeded':break
                await asyncio.sleep(.05)
            assert current['visited']==['https://example.org/home','https://example.org/article']
            # link-following never waits for the nightly graph: extract done == run done
            assert current['state']=='succeeded'
            rows=daemon.state.rows("SELECT * FROM outbox")
            assert len(rows)==2 and all(r['state']=='done' for r in rows)
            assert all('infos' not in json.loads(r['result']) for r in rows)
        finally:await daemon.close()
    asyncio.run(run())
