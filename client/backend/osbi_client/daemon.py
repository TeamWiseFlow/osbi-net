"""Independent client runtime, supervised acquisition and durable server outbox."""
import asyncio
from datetime import datetime, timezone
import json
import secrets
import time
from collections import Counter
from urllib.parse import urlsplit
import feedparser
import httpx
from osbi_contracts.models import Material, SourceMeta
from osbi_contracts.storage import digest, encode
from osbi_contracts.urls import canonical_url
from .browser import Browser, BrowserError
from .models import ClientConfig, Source
from .state import State
from .server_client import request, ServerError


class Daemon:
    def __init__(self, data_dir, *, browser=None, transport=None, decision_transport=None):
        self.data_dir = data_dir
        self.state = State(data_dir / "client.db")
        self.browser = browser or Browser(data_dir / "browser", warning_handler=self.browser_warning)
        self.transport = transport
        self.decision_transport = decision_transport
        self.tasks = []
        self.running = {}
        self.instance = None

    def browser_warning(self, warning):
        if warning.get("code") == "GEOIP_UNAVAILABLE":
            self.state.event("browser_warning", "GeoIP 定位失败或数据库缺失，可能会增加暴露的风险，降低获取成功率；程序将继续运行。", level="warning")

    async def start(self):
        from osbi_contracts.instance import InstanceLock
        self.instance = InstanceLock(self.data_dir / "client.lock")
        self.instance.acquire()
        self.state.execute("UPDATE runs SET state='interrupted',error='客户端重启，检查浏览器状态后可继续',updated=? WHERE state='running'", (time.time(),))
        self.tasks = [asyncio.create_task(self.run_loop()), asyncio.create_task(self.outbox_loop())]
        self.state.event("backend_started", "本地后台已启动")

    async def close(self):
        for task in [*self.tasks, *self.running.values()]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.running.values(), return_exceptions=True)
        self.state.event("backend_stopped", "本地后台已停止")
        self.state.close()
        if self.instance:
            self.instance.release()

    def start_run(self, source_id):
        config = self.state.config()
        source = next((s for s in config.sources if s.id == source_id), None)
        if source is None:
            raise ValueError("信源不存在")
        if not config.serverUrl or not config.serverKey:
            raise ValueError("请先配置共享服务地址和客户 Key")
        if self.state.one("SELECT id FROM runs WHERE state IN ('pending','running','waiting_for_user','waiting_server','interrupted') AND json_extract(source,'$.id')=?", (source_id,)):
            raise ValueError("该信源已有尚未结束的任务")
        run_id = "run_" + secrets.token_hex(12)
        now = time.time()
        self.state.execute("INSERT INTO runs(id,source,config,state,queue,created,updated) VALUES (?,?,?,'pending',?,?,?)",
                           (run_id, encode(source.model_dump()), encode(config.model_dump()), encode([source.url]), now, now))
        self.state.event("run_started", "采集任务已创建", run_id=run_id)
        return self.state.run(run_id)

    def prompt(self, run_id, message):
        prompt_id = "prompt_" + secrets.token_hex(12)
        self.state.execute("UPDATE runs SET state='waiting_for_user',prompt=?,error=?,updated=? WHERE id=?",
                           (prompt_id, message, time.time(), run_id))
        self.state.event("prompt", message, run_id=run_id, level="warning", details={"promptId": prompt_id})

    def act(self, run_id, action, prompt_id=None):
        row = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row is None:
            raise ValueError("任务不存在")
        if action == "cancel":
            if row["state"] in {"succeeded", "cancelled", "failed"}:
                return self.state.run(run_id)
            self.state.execute("UPDATE runs SET state='cancelled',prompt='',updated=? WHERE id=?", (time.time(), run_id))
            if run_id in self.running:
                self.running[run_id].cancel()
            self.state.event("run_cancelled", "任务已取消", run_id=run_id)
        elif action == "resume":
            if row["state"] not in {"waiting_for_user", "interrupted"}:
                if prompt_id and not row["prompt"]:
                    return self.state.run(run_id)
                raise ValueError("任务当前不能继续")
            if row["state"] == "waiting_for_user" and prompt_id != row["prompt"]:
                raise ValueError("人工提示已失效，请刷新任务")
            browser_state = json.loads(row["browser"])
            if "pendingStep" in browser_state:
                # Explicit resume confirms the user completed/checked the dispatched operation.
                step = max(row["step"], browser_state.pop("pendingStep") + 1)
                browser_state["pageReady"] = True
            else:
                step = row["step"]
            self.state.execute("UPDATE runs SET state='pending',prompt='',error='',step=?,browser=?,updated=? WHERE id=?",
                               (step, encode(browser_state), time.time(), run_id))
            self.state.event("prompt_resolved", "任务已恢复", run_id=run_id, details={"promptId": row["prompt"]})
        else:
            raise ValueError("不支持的操作")
        return self.state.run(run_id)

    async def run_loop(self):
        while True:
            config = self.state.config()
            for cancelled in self.state.rows("SELECT * FROM runs WHERE state='cancelled' AND json_extract(browser,'$.lease') IS NOT NULL"):
                source, browser_state = json.loads(cancelled["source"]), json.loads(cancelled["browser"])
                try:
                    await self.browser.command(source["profile"], "lease.release", {}, browser_state["lease"])
                except BrowserError:
                    pass
                browser_state.pop("lease", None)
                self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), cancelled["id"]))
            for schedule in self.state.rows("SELECT * FROM schedules WHERE next_at<=?", (time.time(),)):
                with self.state.transaction() as conn:
                    # Schedule advancement and run creation commit together.
                    current = conn.execute("SELECT next_at FROM schedules WHERE source_id=?", (schedule["source_id"],)).fetchone()
                    if not current or current[0] > time.time():
                        continue
                    conn.execute("UPDATE schedules SET next_at=? WHERE source_id=?", (time.time() + schedule["interval_seconds"], schedule["source_id"]))
                    source = next((s for s in config.sources if s.id == schedule["source_id"]), None)
                    active = conn.execute("SELECT 1 FROM runs WHERE state IN ('pending','running','waiting_for_user','waiting_server','interrupted') AND json_extract(source,'$.id')=?", (schedule["source_id"],)).fetchone()
                    if source and source.enabled and not active:
                        run_id = "run_" + secrets.token_hex(12)
                        now = time.time()
                        conn.execute("INSERT INTO runs(id,source,config,state,queue,created,updated) VALUES (?,?,?,'pending',?,?,?)",
                                     (run_id, encode(source.model_dump()), encode(config.model_dump()), encode([source.url]), now, now))
            self.running = {k: t for k, t in self.running.items() if not t.done()}
            active_rows = self.state.rows("SELECT source,queue FROM runs WHERE state IN ('running','waiting_for_user','interrupted')")
            active_profiles = {json.loads(r["source"])["profile"] for r in active_rows}
            active_domains = Counter(urlsplit((json.loads(r["queue"]) or [json.loads(r["source"])["url"]])[0]).hostname for r in active_rows)
            for row in self.state.rows("SELECT * FROM runs WHERE state='pending' ORDER BY created"):
                source = json.loads(row["source"])
                domain = urlsplit((json.loads(row["queue"]) or [source["url"]])[0]).hostname
                cooldown = self.state.one("SELECT next_at FROM domain_cooldowns WHERE domain=?", (domain,))
                resuming = bool(json.loads(row["browser"]).get("pageReady"))
                if len(self.running) >= config.browserConcurrency:
                    break
                if active_domains[domain] >= config.domainConcurrency or (cooldown and cooldown["next_at"] > time.time() and not resuming):
                    continue
                if row["id"] not in self.running and source["profile"] not in active_profiles:
                    self.state.execute("UPDATE runs SET state='running',updated=? WHERE id=? AND state='pending'", (time.time(), row["id"]))
                    self.running[row["id"]] = asyncio.create_task(self.acquire(row["id"]))
                    active_profiles.add(source["profile"])
                    active_domains[domain] += 1
            await asyncio.sleep(0.25)

    async def acquire(self, run_id):
        row = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        config, source = ClientConfig.model_validate(json.loads(row["config"])), Source.model_validate(json.loads(row["source"]))
        session = source.profile
        browser_state = json.loads(row["browser"])
        lease = browser_state.get("lease")
        try:
            if source.kind == "rss" and not browser_state.get("rss_discovered"):
                async with httpx.AsyncClient(timeout=config.timeoutSeconds, follow_redirects=True, trust_env=False) as client:
                    response = await client.get(source.url)
                    response.raise_for_status()
                feed = feedparser.parse(response.content)
                urls = list(dict.fromkeys(e.link for e in feed.entries if getattr(e, "link", "") and canonical_url(e.link)))[:config.maxUrls]
                if not urls:
                    raise ValueError("RSS 未返回有效文章链接")
                self.state.execute("UPDATE runs SET queue=?,browser=? WHERE id=?", (encode(urls), encode({"rss_discovered": True}), run_id))
            if not lease:
                await self.browser.command(session, "start", {"headless": config.headless}, headed=not config.headless)
                lease = (await self.browser.command(session, "lease.acquire", {"ttlMs": 300000}))["leaseToken"]
                browser_state["lease"] = lease
                self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
            else:
                try:
                    await self.browser.command(session, "lease.renew", {"ttlMs": 300000}, lease)
                except BrowserError as error:
                    if error.code != "LEASE_EXPIRED":
                        raise
                    lease = (await self.browser.command(session, "lease.acquire", {"ttlMs": 300000}))["leaseToken"]
                    browser_state["lease"] = lease
                    self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
            current = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
            queue, visited = json.loads(current["queue"]), json.loads(current["visited"])
            if not queue or len(visited) >= config.maxUrls:
                self.state.execute("UPDATE runs SET state='waiting_server',updated=? WHERE id=?", (time.time(), run_id))
                return
            url = queue[0]
            # Resumed workflows continue from the persisted step in the same tab.
            if current["step"] == 0 and not browser_state.get("pageReady"):
                browser_state["pendingStep"] = -1
                self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
                await self.browser.command(session, "open", {"url": url}, lease)
                browser_state.pop("pendingStep", None)
                browser_state["pageReady"] = True
                self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
            steps = source.steps if not visited else []
            for i in range(current["step"], len(steps)):
                step = steps[i]
                if step.action == "wait_for_user":
                    self.state.execute("UPDATE runs SET step=? WHERE id=?", (i + 1, run_id))
                    await self.browser.command(session, "lease.wait", {}, lease)
                    self.prompt(run_id, step.text or "请在浏览器中完成操作后继续")
                    return
                if step.action == "capture":
                    break
                params = {k: v for k, v in step.model_dump().items() if k != "action" and v not in ("", None)}
                action = {"navigate": "open"}.get(step.action, step.action)
                if action == "wait":
                    params = {"selector": step.selector} if step.selector else {"ms": step.ms}
                if action == "select":
                    params["value"] = step.text
                await self.browser.command(session, "lease.renew", {"ttlMs": 300000}, lease)
                browser_state["pendingStep"] = i
                self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
                await self.browser.command(session, action, params, lease)
                browser_state.pop("pendingStep", None)
                self.state.execute("UPDATE runs SET step=?,browser=? WHERE id=?", (i + 1, encode(browser_state), run_id))
            material = await self.browser.capture(session, url, lease)
            meta = material["meta"]
            valid_meta = {k: str(meta.get(k) or "") for k in SourceMeta.model_fields if k not in {"adapter", "textOnly", "mediaPending"}}
            valid_meta.update(textOnly=True, mediaPending=bool(meta.get("mediaPending")))
            if valid_meta["mediaPending"]:
                self.state.event("media_pending", "正文图片尚未识别，本次仅提交可用文字", run_id=run_id, level="warning")
            links = [{**x, "url": material["localMaps"]["urls"][x["id"]]} for x in material["links"][:200]]
            payload = Material(clientId=self.state.one("SELECT client_id FROM identity")["client_id"],
                               requestId=digest([run_id, url, material["materialId"]]), title=meta.get("title", ""),
                               focus={"focuspoint": config.focus}, blocks=material["blocks"][:64], links=links,
                               meta=SourceMeta(**valid_meta)).model_dump()
            from .decisions import allow_submission
            submit = await allow_submission(self, payload, config.jev)
            with self.state.transaction() as conn:
                if submit:
                    conn.execute("INSERT OR IGNORE INTO outbox(id,run_id,payload,state,server_url,server_ca,credential_generation) VALUES (?,?,?,'pending',?,?,?)",
                                 (payload["requestId"], run_id, encode(payload), config.serverUrl, config.serverCA, self.state.one("SELECT revision FROM config WHERE id=1")["revision"]))
                visited.append(url)
                browser_state.pop("pageReady", None)
                next_state = "waiting_server"
                if not submit:
                    outstanding = conn.execute("SELECT COUNT(*) FROM outbox WHERE run_id=? AND state NOT IN ('done','failed')", (run_id,)).fetchone()[0]
                    next_state = "pending" if queue[1:] and len(visited) < config.maxUrls else "waiting_server" if outstanding else "succeeded"
                conn.execute("UPDATE runs SET queue=?,visited=?,step=0,state=?,browser=?,updated=? WHERE id=?",
                             (encode(queue[1:]), encode(visited), next_state, encode(browser_state), time.time(), run_id))
            self.state.event("captured" if submit else "jev_discarded", "原文已清洗切块，等待共享服务处理" if submit else "Jev 判定该材料与关注内容无关，已跳过提交。", run_id=run_id)
        except BrowserError as error:
            if error.code in {"VERIFICATION_REQUIRED", "CAPTURE_PARTIAL", "OUTCOME_UNKNOWN", "LEASE_EXPIRED"}:
                if error.code == "VERIFICATION_REQUIRED":
                    domain = urlsplit(source.url).hostname
                    old = self.state.one("SELECT strikes FROM domain_cooldowns WHERE domain=?", (domain,))
                    strikes = old["strikes"] + 1 if old else 1
                    self.state.execute("INSERT INTO domain_cooldowns VALUES (?,?,?) ON CONFLICT(domain) DO UPDATE SET next_at=excluded.next_at,strikes=excluded.strikes",
                                       (domain, time.time() + (300 if strikes == 1 else 900), strikes))
                if lease:
                    try:
                        await self.browser.command(session, "lease.wait", {}, lease)
                    except BrowserError:
                        pass
                self.prompt(run_id, str(error))
            else:
                self.state.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=?", (str(error), time.time(), run_id))
                self.state.event("run_failed", str(error), run_id=run_id, level="error")
        except Exception:
            self.state.execute("UPDATE runs SET state='failed',error='采集失败，请检查信源和运行设置',updated=? WHERE id=?", (time.time(), run_id))
            self.state.event("run_failed", "采集失败，请检查信源和运行设置", run_id=run_id, level="error")
        finally:
            state = self.state.one("SELECT state FROM runs WHERE id=?", (run_id,))
            if lease and state and state["state"] not in {"waiting_for_user", "interrupted"}:
                try:
                    await self.browser.command(session, "lease.release", {}, lease)
                    browser_state.pop("lease", None)
                    self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
                except BrowserError:
                    pass

    async def outbox_loop(self):
        while True:
            for row in self.state.rows("SELECT * FROM outbox WHERE state IN ('pending','polling') AND next_at<=? ORDER BY rowid LIMIT 10", (time.time(),)):
                config = self.state.config()
                owner = self.state.one("SELECT state FROM runs WHERE id=?", (row["run_id"],))
                if owner and owner["state"] == "cancelled":
                    self.state.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
                    continue
                try:
                    if config.serverUrl != row["server_url"]:
                        raise ServerError("待提交材料绑定的服务地址已变更，请恢复原地址后继续", 401)
                    if row["state"] == "pending":
                        result = await request(config, "POST", "/v1/materials", json.loads(row["payload"]), transport=self.transport)
                    else:
                        result = await request(config, "GET", "/v1/jobs/" + row["remote_id"], transport=self.transport)
                    previous = json.loads(row["result"])
                    state = "done" if result["state"] in {"succeeded", "failed"} else "polling"
                    self.state.execute("UPDATE outbox SET state=?,remote_id=?,result=?,next_at=?,error='' WHERE id=?",
                                       (state, result["jobId"], encode(result), time.time() + 2, row["id"]))
                    if previous.get("state") != result["state"]:
                        labels = {"pending": "已接收材料", "running": "正在提取", "graph_pending": "等待图谱提取", "succeeded": "图谱已完成", "failed": "处理失败", "retry": "等待重试"}
                        self.state.event("server_job", "共享服务：" + labels.get(result["state"], result["state"]), run_id=row["run_id"],
                                         details={"jobId": result["jobId"], "assignments": result.get("assignments", [])})
                    if result["state"] == "failed":
                        self.state.execute("UPDATE runs SET state='failed',error='共享服务提取失败',updated=? WHERE id=? AND state!='cancelled'", (time.time(), row["run_id"]))
                        continue
                    run = self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
                    if not run or run["state"] in {"cancelled", "failed", "succeeded"}:
                        continue
                    if "selection" in result and not row["links_applied"]:
                        payload = json.loads(row["payload"])
                        selected = set(result.get("selection", {}).get("linkIds", []))
                        found = [x["url"] for x in payload["links"] if x["id"] in selected]
                        visited, queue = json.loads(run["visited"]), json.loads(run["queue"])
                        run_config = ClientConfig.model_validate(json.loads(run["config"]))
                        for url in found:
                            host = urlsplit(url).hostname or ""
                            if url not in visited and url not in queue and not any(host == d or host.endswith("."+d) for d in run_config.forbiddenDomains):
                                queue.append(url)
                        remaining = bool(queue and len(visited) < run_config.maxUrls)
                        with self.state.transaction() as conn:
                            conn.execute("UPDATE outbox SET links_applied=1 WHERE id=?", (row["id"],))
                            conn.execute("UPDATE runs SET queue=?,state=CASE WHEN state='waiting_server' AND ? THEN 'pending' ELSE state END,updated=? WHERE id=?",
                                         (encode(queue[:run_config.maxUrls]), remaining, time.time(), row["run_id"]))
                    # Selected-link acquisition is independent of deferred graph jobs.
                    current = self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
                    remaining = bool(json.loads(current["queue"]) and len(json.loads(current["visited"])) < json.loads(current["config"])["maxUrls"])
                    outstanding = self.state.one("SELECT COUNT(*) n FROM outbox WHERE run_id=? AND state NOT IN ('done','failed')", (row["run_id"],))["n"]
                    if state == "done" and not remaining and not outstanding and current["state"] == "waiting_server":
                        self.state.execute("UPDATE runs SET state='succeeded',updated=? WHERE id=?", (time.time(), row["run_id"]))
                        self.state.event("run_finished", "采集与图谱处理已完成", run_id=row["run_id"])
                except ServerError as error:
                    state = "blocked" if error.status in {401, 403} else "failed" if error.status in {400, 409, 422} else row["state"]
                    self.state.execute("UPDATE outbox SET state=?,error=?,attempts=attempts+1,next_at=? WHERE id=?",
                                       (state, str(error), time.time() + error.retry_after, row["id"]))
                    if row["error"] != str(error):
                        self.state.event("server_error", str(error), run_id=row["run_id"], level="error")
                    if state == "failed":
                        self.state.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=? AND state!='cancelled'", (str(error), time.time(), row["run_id"]))
                except Exception:
                    self.state.execute("UPDATE outbox SET next_at=?,error='共享服务返回了无效数据' WHERE id=?", (time.time()+15, row["id"]))
            await asyncio.sleep(0.5)
