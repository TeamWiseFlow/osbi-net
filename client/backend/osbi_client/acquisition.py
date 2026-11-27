"""Durable page workers and per-profile browser mode coordination."""
import asyncio
from collections import Counter, defaultdict, deque
from contextlib import nullcontext
import json
import secrets
import time

import feedparser
import httpx
from osbi_contracts.storage import encode, digest
from osbi_contracts.urls import canonical_url
from hunters import hunter_for
from .browser import BrowserError
from .domain_policy import base_domain, page_capacity, DOMAIN_GAP_MS, RETRY_DELAYS_MS, WECHAT_DOMAIN
from .material_urls import forbidden_url
from .models import ClientConfig, Source

RUN_TIMEOUT_SECONDS = 3 * 3600
REPEAT_VERIFICATION_SECONDS = 10 * 60
TERMINAL = {"succeeded", "failed", "cancelled"}
PAGE_TERMINAL = {"done", "failed", "cancelled"}


class Acquisition:
    def __init__(self, daemon):
        self.daemon = daemon
        self.state = daemon.state
        self.switching = set()
        self.transitions = {}
        self.locks = defaultdict(asyncio.Lock)
        self.background = set()
        self.probes = {}
        self.cleanups = set()
        self.last_profile = ""
        self.retired = set()

    def spawn(self, work):
        task = asyncio.create_task(work)
        self.background.add(task)
        def done(future):
            self.background.discard(future)
            if not future.cancelled():
                error = future.exception()
                if error:
                    self.state.event("browser_control_error", "浏览器后台操作失败", level="warning", details={"type": type(error).__name__})
        task.add_done_callback(done)
        return task

    def profile(self, name):
        self.state.execute("INSERT OR IGNORE INTO browser_profiles(profile,updated) VALUES (?,?)", (name, time.time()))
        return self.state.one("SELECT * FROM browser_profiles WHERE profile=?", (name,))

    def active(self, run_id):
        row = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if not row or row["state"] in TERMINAL:
            return False
        if time.time() >= (row["deadline"] or row["created"] + RUN_TIMEOUT_SECONDS):
            self.cancel(run_id, "信源本次执行已达到 3 小时上限")
            return False
        return True

    def scope(self, page_id, browser):
        factory = getattr(self.daemon.browser, "page_scope", None)
        return factory(page_id, browser.get("runtimeGeneration")) if factory else nullcontext()

    def save_browser(self, page_id, browser, *, step=None):
        if step is None:
            self.state.execute("UPDATE acquisition_pages SET browser=?,updated=? WHERE id=? AND state NOT IN ('done','failed','cancelled') AND generation=?",
                               (encode(browser), time.time(), page_id, browser.get('epoch', 0)))
        else:
            self.state.execute("UPDATE acquisition_pages SET browser=?,step=?,updated=? WHERE id=? AND state NOT IN ('done','failed','cancelled') AND generation=?",
                               (encode(browser), step, time.time(), page_id, browser.get('epoch', 0)))

    def seed(self, row):
        browser = json.loads(row["browser"])
        source = Source.model_validate(json.loads(row["source"]))
        if not browser.get("pageQueueReady"):
            others = self.state.one("SELECT 1 FROM runs WHERE id!=? AND state NOT IN ('succeeded','failed','cancelled') AND json_extract(source,'$.profile')=? AND json_extract(browser,'$.pageQueueReady')=1",
                                    (row["id"], source.profile))
            self.profile(source.profile)
            if not others:
                self.state.execute("UPDATE browser_profiles SET mode='headless',sticky=0,verified=0,return_page='' WHERE profile=?", (source.profile,))
            browser["pageQueueReady"] = True
            self.state.execute("UPDATE runs SET browser=?,deadline=CASE WHEN deadline=0 THEN created+? ELSE deadline END WHERE id=?",
                               (encode(browser), RUN_TIMEOUT_SECONDS, row["id"]))
            if source.kind in {"mp", "rss"} and not browser.get("hunter_discovered") and not browser.get("rss_discovered"):
                self.enqueue(row["id"], source.url, kind="discovery")
                return
        discovery = self.state.one("SELECT 1 FROM acquisition_pages WHERE run_id=? AND kind='discovery' AND state NOT IN ('done','failed','cancelled')", (row["id"],))
        if discovery:
            return
        current = self.state.one("SELECT * FROM runs WHERE id=?", (row["id"],))
        config = ClientConfig.model_validate(json.loads(row["config"]))
        visited = json.loads(current["visited"])
        aliases = set(json.loads(current["browser"]).get("visitedAliases", []))
        outstanding = self.state.one("SELECT count(*) n FROM acquisition_pages WHERE run_id=? AND kind='page' AND state NOT IN ('done','failed','cancelled')", (row["id"],))["n"]
        budget = max(0, config.maxUrls - len(visited) - outstanding)
        for url in json.loads(current["queue"]):
            key = canonical_url(url)
            if key in aliases or key in {canonical_url(u) for u in visited}:
                continue
            if url != source.url and forbidden_url(url, config.forbiddenDomains):
                self.drop_url(row["id"], url)
                continue
            if self.state.one("SELECT 1 FROM acquisition_pages WHERE run_id=? AND url_key=?", (row["id"], key)):
                continue
            if not budget:
                break
            self.enqueue(row["id"], url)
            budget -= 1

    def enqueue(self, run_id, url, *, kind="page"):
        now = time.time()
        page_id = "page_" + secrets.token_hex(12)
        self.state.execute("INSERT OR IGNORE INTO acquisition_pages(id,run_id,url,url_key,kind,created,updated) VALUES (?,?,?,?,?,?,?)",
                           (page_id, run_id, url, canonical_url(url), kind, now, now))

    def drop_url(self, run_id, url):
        row = self.state.one("SELECT queue FROM runs WHERE id=?", (run_id,))
        queue = [u for u in json.loads(row["queue"]) if canonical_url(u) != canonical_url(url)]
        self.state.execute("UPDATE runs SET queue=? WHERE id=?", (encode(queue), run_id))

    def refresh(self, run_id):
        if not self.active(run_id):
            return
        run = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        pages = self.state.rows("SELECT * FROM acquisition_pages WHERE run_id=?", (run_id,))
        states = Counter(page["state"] for page in pages)
        prompt = next((page for page in pages if page["state"] == "waiting_for_user"), None)
        outstanding = self.state.one("SELECT count(*) n FROM outbox WHERE run_id=? AND state NOT IN ('done','failed','cancelled')", (run_id,))["n"]
        error = ""
        if prompt:
            state, error = "waiting_for_user", prompt["error"]
        elif states["interrupted"]:
            state = "interrupted"
            error = next(page["error"] for page in pages if page["state"] == "interrupted")
        elif states["running"]:
            state = "running"
        elif states["pending"]:
            state = "pending"
        elif outstanding:
            state = "waiting_server"
        else:
            failed = states["failed"] or self.state.one("SELECT 1 FROM outbox WHERE run_id=? AND state='failed'", (run_id,))
            state = "failed" if failed else "succeeded"
            if failed:
                error = next((p["error"] for p in pages if p["state"] == "failed"), "共享服务处理失败")
        self.state.execute("UPDATE runs SET state=?,prompt=?,error=?,updated=? WHERE id=? AND state NOT IN ('succeeded','failed','cancelled')",
                           (state, prompt["prompt"] if prompt else "", error, time.time(), run_id))
        if state in TERMINAL:
            self.state.event("run_finished" if state == "succeeded" else "run_failed",
                             "采集与筛选处理已完成" if state == "succeeded" else error, run_id=run_id)

    def cancel(self, run_id, reason="任务已取消", *, terminal="cancelled"):
        row = self.state.one("SELECT state FROM runs WHERE id=?", (run_id,))
        if not row or row["state"] in TERMINAL:
            return
        self.state.execute("UPDATE runs SET state=?,prompt='',error=?,updated=? WHERE id=?", (terminal, reason, time.time(), run_id))
        self.state.execute("UPDATE acquisition_pages SET state='cancelled',prompt='' WHERE run_id=? AND state NOT IN ('done','failed','cancelled')", (run_id,))
        self.state.execute("UPDATE outbox SET state='cancelled' WHERE run_id=? AND state NOT IN ('done','failed','cancelled')", (run_id,))
        for page in self.state.rows("SELECT id FROM acquisition_pages WHERE run_id=?", (run_id,)):
            task = self.daemon.running.get(page["id"])
            if task and (not self._in_loop() or task is not asyncio.current_task()):
                task.cancel()
            probe = self.probes.get(page["id"])
            if probe:
                probe.cancel()
        for out_id, task in getattr(self.daemon, "submitting", {}).items():
            owner = self.state.one("SELECT run_id FROM outbox WHERE id=?", (out_id,))
            if owner and owner["run_id"] == run_id:
                task.cancel()
        self.state.event("run_cancelled" if terminal == "cancelled" else "run_failed", reason, run_id=run_id, level="warning")

    @staticmethod
    def _in_loop():
        try:
            asyncio.get_running_loop()
            return True
        except RuntimeError:
            return False

    def expire(self):
        now = time.time()
        for row in self.state.rows("SELECT id FROM runs WHERE state NOT IN ('succeeded','failed','cancelled') AND (CASE WHEN deadline=0 THEN created+? ELSE deadline END)<=?", (RUN_TIMEOUT_SECONDS, now)):
            self.cancel(row["id"], "信源本次执行已达到 3 小时上限")
        for page in self.state.rows("SELECT * FROM acquisition_pages WHERE state='waiting_for_user'"):
            config = ClientConfig.model_validate(json.loads(self.state.one("SELECT config FROM runs WHERE id=?", (page["run_id"],))["config"]))
            if now >= page["prompt_started"] + config.humanPromptTimeoutSeconds:
                self.cancel(page["run_id"], "人工等待超时，任务已终止")

    def schedule(self, config):
        self.expire()
        self.daemon.running = {key: task for key, task in self.daemon.running.items() if not task.done()}
        runs = self.state.rows("SELECT * FROM runs WHERE state NOT IN ('succeeded','failed','cancelled') AND (state!='interrupted' OR EXISTS(SELECT 1 FROM acquisition_pages p WHERE p.run_id=runs.id)) ORDER BY created")
        for run in runs:
            self.seed(run)
        backlog = self.state.one("SELECT count(*) n FROM outbox WHERE state NOT IN ('done','failed','cancelled')")["n"]
        if backlog >= config.maxPendingSubmissions:
            return
        capacity = page_capacity(source.profile for source in config.sources)
        active = self.state.rows("SELECT p.*,r.source FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE p.state='running' AND r.state NOT IN ('succeeded','failed','cancelled')")
        active_profiles = Counter(json.loads(p["source"])["profile"] for p in active)
        active_domains = Counter(base_domain(p["url"]) for p in active)
        waiting = self.state.rows("SELECT p.*,r.source FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE p.state IN ('waiting_for_user','interrupted')")
        occupied = {}
        for page in waiting:
            profile = json.loads(page["source"])["profile"]
            if page["state"] == "waiting_for_user" or (json.loads(page["browser"]).get("lease") and self.profile(profile)["mode"] == "headed"):
                occupied[profile] = page["id"]
        candidates = defaultdict(deque)
        for page in self.state.rows("SELECT p.*,r.source FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE p.state='pending' AND r.state NOT IN ('succeeded','failed','cancelled') ORDER BY p.priority DESC,p.updated,p.created"):
            candidates[json.loads(page["source"])["profile"]].append(page)
        order = deque(sorted(candidates))
        if self.last_profile in order:
            order.rotate(-order.index(self.last_profile)-1)
        admitted = len(self.daemon.running)
        while order and admitted < capacity:
            profile = order.popleft()
            if profile in self.switching or profile in occupied:
                continue
            mode = self.profile(profile)
            if mode["mode"] == "headed" and active_profiles[profile]:
                continue
            selected = None
            for page in candidates[profile]:
                if page["id"] in self.daemon.running:
                    continue
                if page["id"] in self.probes and not self.probes[page["id"]].done():
                    continue
                browser = json.loads(page["browser"])
                domain = base_domain(page["url"])
                resuming = browser.get("pageReady") or mode["mode"] == "headed" and page["priority"] > 0
                cooldown = self.state.one("SELECT next_at FROM domain_cooldowns WHERE domain=?", (domain,))
                not_before = browser.get("retryNotBefore", 0)
                if not resuming:
                    not_before = max(not_before, self.daemon._domain_gap.get(domain, 0), cooldown["next_at"] if cooldown else 0)
                if active_domains[domain] >= config.domainConcurrency.limit_for(domain) or time.time() < not_before:
                    continue
                selected = page
                break
            if selected is None:
                continue
            candidates[profile].remove(selected)
            self.state.execute("UPDATE acquisition_pages SET state='running',updated=?,generation=? WHERE id=? AND state='pending'", (time.time(), mode['generation'], selected["id"]))
            self.daemon.running[selected["id"]] = asyncio.create_task(self.process(selected["id"]))
            self.last_profile = profile
            admitted += 1
            active_profiles[profile] += 1
            active_domains[base_domain(selected["url"])] += 1
            self.daemon._domain_gap[base_domain(selected["url"])] = time.time() + DOMAIN_GAP_MS / 1000
            self.refresh(selected["run_id"])
            if candidates[profile]:
                order.append(profile)

    async def acquire_next(self, run_id):
        if not self.active(run_id):
            return
        self.seed(self.state.one("SELECT * FROM runs WHERE id=?", (run_id,)))
        page = self.state.one("SELECT id FROM acquisition_pages WHERE run_id=? AND state='pending' ORDER BY priority DESC,created LIMIT 1", (run_id,))
        if page:
            self.state.execute("UPDATE acquisition_pages SET state='running' WHERE id=?", (page["id"],))
            await self.process(page["id"])
        else:
            self.refresh(run_id)

    async def discover(self, run, source, config):
        browser = json.loads(run["browser"])
        if source.kind == "mp":
            await self.daemon._discover_catalog(run["id"], source, config, browser, hunter_for(source.kind))
        else:
            scope = digest(["rss-feed/v1", source.profile])
            cached = None if browser.get("forceRefresh") else self.daemon.cache.capture_get(source.url, scope, config.captureCache)
            if cached:
                urls = cached["feedUrls"]
            else:
                async with httpx.AsyncClient(timeout=config.timeoutSeconds, follow_redirects=True, trust_env=False) as client:
                    response = await client.get(source.url)
                    response.raise_for_status()
                urls = list(dict.fromkeys(entry.link for entry in feedparser.parse(response.content).entries if canonical_url(getattr(entry, "link", ""))))
                if urls:
                    self.daemon.cache.capture_put(source.url, scope, {"feedUrls": urls}, config.captureCache)
            urls = [url for url in urls if not forbidden_url(url, config.forbiddenDomains)][:config.maxUrls]
            if not urls:
                raise ValueError("RSS 未返回有效文章链接")
            if self.active(run["id"]):
                browser.update(rss_discovered=True, pageQueueReady=True)
                self.state.execute("UPDATE runs SET queue=?,browser=? WHERE id=?", (encode(urls), encode(browser), run["id"]))

    async def process(self, page_id):
        page = self.state.one("SELECT * FROM acquisition_pages WHERE id=?", (page_id,))
        run_id = page["run_id"]
        if not self.active(run_id):
            return
        run = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        source = Source.model_validate(json.loads(run["source"]))
        config = ClientConfig.model_validate(json.loads(run["config"]))
        profile = source.profile
        epoch = self.profile(profile)["generation"]
        browser = json.loads(page["browser"])
        browser["epoch"] = epoch
        self.state.execute("UPDATE acquisition_pages SET generation=? WHERE id=?", (epoch, page_id))
        try:
            if page["kind"] == "discovery":
                await self.discover(run, source, config)
                current = self.state.one("SELECT state FROM runs WHERE id=?", (run_id,))
                self.state.execute("UPDATE acquisition_pages SET state=? WHERE id=? AND state!='cancelled'",
                                   ("failed" if current["state"] == "failed" else "done", page_id))
                return
            config.headless = self.profile(profile)["mode"] == "headless"
            if config.headless and page["url"] == source.url and any(step.action == "wait_for_user" for step in source.steps):
                await self.switch(profile, page_id, headed=True)
                return
            with self.scope(page_id, browser):
                parent_browser = json.loads(run["browser"])
                first = page["url"] == source.url
                cache_scope = self.daemon.cache.scope(source, config, first)
                material = None
                if not parent_browser.get("forceRefresh") and not browser.get("pageReady") and page["step"] == 0:
                    material = self.daemon.cache.capture_get(page["url"], cache_scope, config.captureCache)
                if material is None:
                    material = await self.capture(page, source, config, browser, parent_browser)
                    if material is None:
                        return
                    if not self.active(run_id) or epoch != self.profile(profile)["generation"]:
                        return
                    self.daemon.cache.capture_put(page["url"], cache_scope, material, config.captureCache)
                    self.state.execute("UPDATE domain_cooldowns SET strikes=0 WHERE domain=?", (base_domain(page["url"]),))
                else:
                    self.state.event("capture_cached", "复用本地页面缓存，跳过浏览器抓取", run_id=run_id)
                if not self.active(run_id) or epoch != self.profile(profile)["generation"]:
                    return
                fresh = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
                await self.daemon._submit_material(run_id, source, config, page["url"], material,
                                                   json.loads(fresh["queue"]), json.loads(fresh["visited"]), json.loads(fresh["browser"]))
                self.state.execute("UPDATE acquisition_pages SET state='done',prompt='',error='' WHERE id=?", (page_id,))
            await self.finish_verification(profile, page_id)
        except asyncio.CancelledError:
            raise
        except BrowserError as error:
            if not self.active(run_id) or epoch != self.profile(profile)["generation"]:
                return
            if error.code in {"CAPTURE_PARTIAL", "CAPTURE_UNSUPPORTED"}:
                self.state.execute("UPDATE acquisition_pages SET state='failed',error=? WHERE id=?", (str(error), page_id))
                self.drop_url(run_id, page["url"])
                self.state.event("capture_failed", str(error), run_id=run_id, level="warning",
                                 details={"code": error.code, "url": page["url"]})
                await self.finish_verification(profile, page_id)
            elif error.code == "OUTCOME_UNKNOWN":
                self.state.execute("UPDATE acquisition_pages SET state='interrupted',error='交互结果未知，请检查后确认继续',browser=? WHERE id=?", (encode(browser), page_id))
            elif error.code == "VERIFICATION_REQUIRED":
                domain = base_domain(page["url"])
                if domain != WECHAT_DOMAIN and (config.headless or browser.pop("confirmed", False)):
                    old = self.state.one("SELECT strikes FROM domain_cooldowns WHERE domain=?", (domain,))
                    strikes = old["strikes"] + 1 if old else 1
                    self.state.execute("INSERT INTO domain_cooldowns VALUES (?,?,?) ON CONFLICT(domain) DO UPDATE SET next_at=excluded.next_at,strikes=excluded.strikes", (domain, time.time()+60, strikes))
                if config.headless:
                    await self.switch(profile, page_id, headed=True)
                else:
                    self.prompt(page_id, str(error), browser)
            elif error.code in {"STALE_GENERATION", "RUNTIME_UPGRADE_REQUIRED"}:
                self.state.execute("UPDATE acquisition_pages SET state='interrupted',browser='{}',step=0,error=? WHERE id=?", (str(error) + "；页面已失效，确认可以重新执行信源步骤后继续", page_id))
            else:
                retries = browser.get("captureRetries", 0)
                if error.code != "CAPTURE_EMPTY" and retries < len(RETRY_DELAYS_MS):
                    browser["captureRetries"] = retries + 1
                    browser["retryNotBefore"] = time.time() + RETRY_DELAYS_MS[retries] / 1000
                    self.state.execute("UPDATE acquisition_pages SET state='pending',browser=? WHERE id=?", (encode(browser), page_id))
                    self.state.event("capture_retry", "页面采集失败，稍后重试", run_id=run_id, details={"code": error.code, "url": page["url"]})
                else:
                    self.state.execute("UPDATE acquisition_pages SET state='failed',error=? WHERE id=?", (str(error), page_id))
                    self.drop_url(run_id, page["url"])
        except Exception as error:
            if self.active(run_id):
                self.state.execute("UPDATE acquisition_pages SET state='failed',error=? WHERE id=?", (f"页面采集失败：{type(error).__name__}", page_id))
                self.drop_url(run_id, page["url"])
        finally:
            if profile not in self.switching and epoch == self.profile(profile)["generation"]:
                current = self.state.one("SELECT * FROM acquisition_pages WHERE id=?", (page_id,))
                if current and current["state"] in PAGE_TERMINAL:
                    await self.dispose(current, profile)
            if self.active(run_id):
                self.seed(self.state.one("SELECT * FROM runs WHERE id=?", (run_id,)))
                self.refresh(run_id)

    async def finish_verification(self, profile, page_id):
        # A technical extraction failure also proves the verification gate cleared.
        mode = self.profile(profile)
        if mode["mode"] == "headed" and mode["return_page"] == page_id:
            self.state.execute("UPDATE browser_profiles SET verified=?,return_page='' WHERE profile=?", (time.time(), profile))
            if not mode["sticky"]:
                await self.switch(profile, page_id, headed=False)

    async def retire_legacy(self, session):
        # Older resident daemons understand session leases but not page leases.
        owners = self.state.rows("SELECT * FROM runs WHERE json_extract(source,'$.profile')=? OR json_extract(browser,'$.session')=?", (session, session))
        if any(row["state"] not in TERMINAL and not json.loads(row["browser"]).get("pageQueueReady") for row in owners):
            raise BrowserError("RUNTIME_UPGRADE_REQUIRED", "请先取消同一 Profile 尚未结束的旧版本任务")
        tokens = [None] + [json.loads(row["browser"]).get("lease") for row in owners if row["state"] in TERMINAL]
        for token in dict.fromkeys(tokens):
            try:
                await self.daemon.browser.command(session, "stop", {}, token)
                return {"running": False}
            except BrowserError as error:
                if error.code not in {"SESSION_BUSY", "LEASE_EXPIRED"}:
                    raise
        raise BrowserError("RUNTIME_UPGRADE_REQUIRED", "旧浏览器仍被其他操作占用，请关闭旧会话后继续")

    async def capture(self, page, source, config, browser, parent_browser):
        session, page_id = source.profile, page["id"]
        self.retired.discard(session)
        async with self.locks[session]:
            if session in self.switching:
                raise asyncio.CancelledError()
            status = await self.daemon.browser.command(session, "status")
            capabilities = status.get("capabilities")
            if status.get("running") and isinstance(capabilities, list) and "page.create" not in capabilities:
                status = await self.retire_legacy(session)
            if status.get("running") and status.get("headless") != config.headless:
                restart = getattr(self.daemon.browser, "restart_profile", None)
                if restart:
                    await restart(session, headed=not config.headless)
                else:
                    await self.daemon.browser.command(session, "profile.stop")
            await self.daemon.browser.command(session, "start", {"headless": config.headless}, headed=not config.headless)
        if browser.get("lease"):
            try:
                await self.daemon.browser.command(session, "lease.renew", {"ttlMs": 300000}, browser["lease"])
            except BrowserError as error:
                if error.code != "LEASE_EXPIRED":
                    raise
                browser.pop("lease", None)
        if not browser.get("lease"):
            lease = await self.daemon.browser.command(session, "lease.acquire", {"ttlMs": 300000})
            browser.update(lease=lease["leaseToken"], runtimeGeneration=lease.get("generation"))
        self.save_browser(page_id, browser)
        lease = browser["lease"]
        if not config.headless:
            await self.daemon.browser.command(session, "page.activate", {}, lease)
        if page["step"] == 0 and not browser.get("pageReady"):
            browser["pendingStep"] = -1
            self.save_browser(page_id, browser)
            await self.daemon.browser.command(session, "open", {"url": page["url"]}, lease)
            browser["pageReady"] = True
            browser.pop("pendingStep", None)
            self.save_browser(page_id, browser)
        for index, step in enumerate(source.steps if page["url"] == source.url else []):
            if index < page["step"]:
                continue
            if step.action == "wait_for_user":
                browser["manualStep"] = True
                self.save_browser(page_id, browser, step=index+1)
                self.prompt(page_id, step.text or "请完成操作后继续", browser)
                return None
            if step.action == "capture":
                break
            params = {key: value for key, value in step.model_dump().items() if key != "action" and value not in ("", None)}
            action = {"navigate": "open"}.get(step.action, step.action)
            if action == "wait":
                params = {"selector": step.selector} if step.selector else {"ms": step.ms}
            if action == "select":
                params["value"] = step.text
            browser["pendingStep"] = index
            self.save_browser(page_id, browser)
            await self.daemon.browser.command(session, action, params, lease)
            browser.pop("pendingStep", None)
            self.save_browser(page_id, browser, step=index+1)
        return await self.daemon._capture_with_partial_grace(
            session, page["url"], lease, parent_browser.get("seedMeta", {}).get(canonical_url(page["url"])),
            self.daemon._vision_enricher(config, page["run_id"]), config, browser, page["run_id"])

    def prompt(self, page_id, message, browser):
        page = self.state.one("SELECT * FROM acquisition_pages WHERE id=?", (page_id,))
        browser["humanCheckAt"] = time.time()
        self.state.execute("UPDATE acquisition_pages SET state='waiting_for_user',prompt=?,prompt_started=CASE WHEN prompt_started=0 THEN ? ELSE prompt_started END,error=?,browser=?,updated=? WHERE id=?",
                           ("prompt_" + secrets.token_hex(12), time.time(), message, encode(browser), time.time(), page_id))
        self.state.event("prompt", message, run_id=page["run_id"], level="warning", details={"pageId": page_id, "url": page["url"]})
        self.refresh(page["run_id"])

    def act(self, run_id, action, prompt_id=None):
        if not self.active(run_id):
            return self.state.run(run_id)
        if action == "cancel":
            self.cancel(run_id)
            return self.state.run(run_id)
        if action != "resume":
            raise ValueError("不支持的操作")
        if prompt_id:
            page = self.state.one("SELECT * FROM acquisition_pages WHERE run_id=? AND state='waiting_for_user' AND prompt=?", (run_id, prompt_id))
        else:
            page = self.state.one("SELECT * FROM acquisition_pages WHERE run_id=? AND state='interrupted' ORDER BY created LIMIT 1", (run_id,))
        if not page:
            row = self.state.run(run_id)
            if prompt_id and not row["prompt"]:
                return row
            raise ValueError("人工提示已失效或任务当前不能继续")
        browser = json.loads(page["browser"])
        browser.pop("manualStep", None)
        browser["confirmed"] = True
        step = page["step"]
        if "pendingStep" in browser:
            step = max(step, browser.pop("pendingStep") + 1)
            browser["pageReady"] = True
        self.state.execute("UPDATE acquisition_pages SET state='pending',prompt='',error='',browser=?,step=?,priority=2 WHERE id=?",
                           (encode(browser), step, page["id"]))
        self.state.event("prompt_resolved", "任务已恢复", run_id=run_id)
        self.refresh(run_id)
        return self.state.run(run_id)

    async def switch(self, profile, page_id, *, headed, uncertain=False):
        if profile in self.transitions:
            await asyncio.shield(self.transitions[profile])
            return
        self.switching.add(profile)
        task = self.spawn(self._switch(profile, page_id, headed, uncertain))
        self.transitions[profile] = task
        try:
            await asyncio.shield(task)
        finally:
            if task.done():
                self.transitions.pop(profile, None)

    async def _switch(self, profile, page_id, headed, uncertain):
        try:
            async with self.locks[profile]:
                mode = self.profile(profile)
                siblings = self.state.rows("SELECT p.* FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE json_extract(r.source,'$.profile')=? AND p.state NOT IN ('done','failed','cancelled')", (profile,))
                tasks = []
                for sibling in siblings:
                    if sibling["id"] == page_id:
                        continue
                    task = self.daemon.running.get(sibling["id"])
                    if task and not task.done():
                        task.cancel()
                        tasks.append(task)
                await asyncio.gather(*tasks, return_exceptions=True)
                if not any(self.active(sibling["run_id"]) for sibling in siblings):
                    await self.daemon.browser.command(profile, "profile.stop")
                    return
                restart = getattr(self.daemon.browser, "restart_profile", None)
                if restart:
                    await restart(profile, headed=headed)
                else:
                    await self.daemon.browser.command(profile, "profile.stop")
                    await self.daemon.browser.command(profile, "start", {"headless": not headed}, headed=headed)
                if not any(self.active(sibling["run_id"]) for sibling in siblings):
                    await self.daemon.browser.command(profile, "profile.stop")
                    return
                sticky = bool(mode["sticky"] or (headed and mode["verified"] and time.time()-mode["verified"] < REPEAT_VERIFICATION_SECONDS))
                self.state.execute("UPDATE browser_profiles SET mode=?,generation=generation+1,sticky=?,return_page=?,updated=? WHERE profile=?",
                                   ("headed" if headed else "headless", int(sticky), page_id if headed else "", time.time(), profile))
                for sibling in siblings:
                    if not self.active(sibling["run_id"]):
                        continue
                    old = json.loads(sibling["browser"])
                    interrupted = sibling["state"] == "interrupted" or uncertain and sibling["id"] == page_id or old.get("pendingStep", -1) >= 0
                    self.state.execute("UPDATE acquisition_pages SET state=?,step=0,browser='{}',prompt='',error=?,priority=?,updated=?,generation=? WHERE id=? AND state NOT IN ('done','failed','cancelled')",
                                       ("interrupted" if interrupted else "pending", "交互结果未知；确认可以重新执行信源步骤后继续" if interrupted else "", 2 if sibling["id"] == page_id else 0, time.time(), mode["generation"]+1, sibling["id"]))
                    self.refresh(sibling["run_id"])
                self.state.event("browser_mode_changed", "浏览器已切换为有头串行" if headed else "浏览器已恢复无头并发",
                                 details={"profile": profile, "sticky": sticky})
        except Exception:
            for sibling in self.state.rows("SELECT p.id,p.run_id FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE json_extract(r.source,'$.profile')=? AND p.state NOT IN ('done','failed','cancelled')", (profile,)):
                self.state.execute("UPDATE acquisition_pages SET state='interrupted',error='浏览器切换失败，请检查后重新执行' WHERE id=?", (sibling["id"],))
                self.refresh(sibling["run_id"])
        finally:
            self.switching.discard(profile)
            self.transitions.pop(profile, None)

    async def dispose(self, page, profile):
        browser = json.loads(page["browser"])
        if not browser.get("lease"):
            return
        try:
            with self.scope(page["id"], browser):
                try:
                    await self.daemon.browser.command(profile, "page.close", {}, browser["lease"])
                except BrowserError as error:
                    if error.code == "LEASE_EXPIRED":
                        await self.daemon.browser.command(profile, "page.close")
                    else:
                        raise
        except BrowserError as error:
            if error.code != "STALE_GENERATION":
                return
        self.state.execute("UPDATE acquisition_pages SET browser='{}' WHERE id=? AND state IN ('done','failed','cancelled')", (page["id"],))

    async def check_prompt(self, page):
        run = self.state.one("SELECT * FROM runs WHERE id=?", (page["run_id"],))
        browser = json.loads(page["browser"])
        config = ClientConfig.model_validate(json.loads(run["config"]))
        if browser.get("manualStep") or time.time()-browser.get("humanCheckAt", 0) < config.humanCheckIntervalSeconds:
            return
        browser["humanCheckAt"] = time.time()
        self.save_browser(page["id"], browser)
        profile = json.loads(run["source"])["profile"]
        try:
            with self.scope(page["id"], browser):
                await self.daemon.browser.command(profile, "lease.renew", {"ttlMs": 300000}, browser.get("lease"))
                result = await self.daemon.browser.human_state(profile, browser.get("lease"))
            if result.get("resolved") and self.active(run["id"]):
                current = self.state.one("SELECT prompt,state FROM acquisition_pages WHERE id=?", (page["id"],))
                if current["state"] == "waiting_for_user" and current["prompt"] == page["prompt"]:
                    self.act(run["id"], "resume", page["prompt"])
        except BrowserError:
            pass

    async def maintenance(self):
        self.expire()
        for page in self.state.rows("SELECT p.*,r.source FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE p.state='waiting_for_user'"):
            if page["id"] not in self.probes or self.probes[page["id"]].done():
                self.probes[page["id"]] = self.spawn(self.check_prompt(page))
        for page in self.state.rows("SELECT p.*,r.source FROM acquisition_pages p JOIN runs r ON r.id=p.run_id WHERE p.state IN ('done','failed','cancelled') AND json_extract(p.browser,'$.lease') IS NOT NULL"):
            if page["id"] not in self.cleanups:
                self.cleanups.add(page["id"])
                self.spawn(self._cleanup(page))

    async def _cleanup(self, page):
        try:
            await self.dispose(page, json.loads(page["source"])["profile"])
        finally:
            self.cleanups.discard(page["id"])

    async def retire_idle(self):
        for mode in self.state.rows("SELECT * FROM browser_profiles"):
            profile = mode["profile"]
            async with self.locks[profile]:
                busy = self.state.one("SELECT 1 FROM runs WHERE state NOT IN ('succeeded','failed','cancelled') AND (json_extract(source,'$.profile')=? OR json_extract(browser,'$.session')=?)", (profile, profile))
                if busy or profile in self.switching or profile in self.retired:
                    continue
                self.switching.add(profile)
                try:
                    status = await self.daemon.browser.command(profile, "status")
                    capabilities = status.get("capabilities")
                    if status.get("running") and isinstance(capabilities, list) and "page.create" not in capabilities:
                        await self.retire_legacy(profile)
                    else:
                        await self.daemon.browser.command(profile, "profile.stop")
                    self.retired.add(profile)
                    self.state.execute("UPDATE browser_profiles SET mode='headless',sticky=0,verified=0,return_page='' WHERE profile=?", (profile,))
                    self.state.event("browser_closed", "空闲浏览器已关闭", details={"profile": profile})
                except BrowserError:
                    pass
                finally:
                    self.switching.discard(profile)

    def apply_links(self, row, result, payload):
        if not self.active(row["run_id"]):
            return
        run = self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
        config = ClientConfig.model_validate(json.loads(run["config"]))
        queue, visited = json.loads(run["queue"]), json.loads(run["visited"])
        seen = {canonical_url(u) for u in queue+visited} | set(json.loads(run["browser"]).get("visitedAliases", []))
        mapping = payload.get("urlMap") or {item["id"]: item["url"] for item in payload.get("links", []) if "url" in item}
        for link_id in result.get("selection", {}).get("linkIds", []):
            url = mapping.get(link_id)
            if url and canonical_url(url) not in seen and not forbidden_url(url, config.forbiddenDomains):
                queue.append(url)
                seen.add(canonical_url(url))
        with self.state.transaction() as conn:
            conn.execute("UPDATE outbox SET links_applied=1 WHERE id=?", (row["id"],))
            conn.execute("UPDATE runs SET queue=? WHERE id=?", (encode(queue[:max(0, config.maxUrls-len(visited))]), row["run_id"]))
        self.seed(self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],)))
        self.refresh(row["run_id"])
