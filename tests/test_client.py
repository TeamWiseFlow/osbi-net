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
    async def capture(self,session,url,lease):
        return {"materialId":"sample-id","meta":{"title":"动态","requestedUrl":url,"finalUrl":url,"canonicalUrl":url},
                "blocks":[{"id":"B1","text":"甲科技公司参与产品发布会。"}],"links":[],"localMaps":{"urls":{}}}


def test_acquisition_explicitly_requires_geoip(tmp_path, monkeypatch):
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
    assert "--geoip" in captured and "--no-geoip" not in captured


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
        async def capture(self,session,url,lease):
            value=await super().capture(session,url,lease)
            if url.endswith('/home'):
                value['links']=[{'id':'L1','text':'下一篇'}]
                value['localMaps']['urls']={'L1':'https://example.org/article'}
            return value
    remote={}
    async def fake_request(config,method,path,payload=None,**kwargs):
        if method=='POST':
            key=payload['requestId']
            remote[key]={'jobId':key,'state':'graph_pending','selection':{'blockIds':['B1'],'linkIds':[x['id'] for x in payload['links']]},'assignments':[]}
            return remote[key]
        return remote[path.rsplit('/',1)[-1]]
    monkeypatch.setattr('osbi_client.daemon.request',fake_request)
    async def run():
        daemon=Daemon(tmp_path/'client',browser=LinkedBrowser())
        daemon.state.save_config({'serverUrl':'https://example.test','serverKey':'key','maxUrls':2,
                                 'sources':[{'id':'a','url':'https://example.org/home'}]})
        await daemon.start()
        try:
            task=daemon.start_run('a')
            for _ in range(100):
                current=daemon.state.run(task['id'])
                if len(current['visited'])==2:break
                await asyncio.sleep(.05)
            assert current['visited']==['https://example.org/home','https://example.org/article']
            assert current['state']=='waiting_server'
            assert len(daemon.state.rows("SELECT * FROM outbox WHERE state IN ('pending','polling')"))==2
        finally:await daemon.close()
    asyncio.run(run())
