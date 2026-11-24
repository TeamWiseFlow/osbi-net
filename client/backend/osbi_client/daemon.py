"""Independent client runtime, supervised acquisition and durable server outbox."""
import asyncio
from datetime import datetime, timezone
import json
import logging
import secrets
import time
from collections import Counter
import feedparser
import httpx
from osbi_contracts.models import Material, SourceMeta
from osbi_contracts.storage import digest, encode
from osbi_contracts.material import material_hash
from osbi_contracts.urls import canonical_url
from hunters import hunter_for
from hunters.wx_mp import MpScanError
from .browser import Browser, BrowserError
from .domain_policy import (DOMAIN_GAP_MS, MAX_CONCURRENCY, RETRY_DELAYS_MS,
                            VERIFICATION_DELAYS_MS, WECHAT_DOMAIN, base_domain)
from .material_urls import forbidden_url
from .models import ClientConfig, Source
from .state import State
from .cache import LocalCache
from .server_client import request, ServerError

logger = logging.getLogger(__name__)

# Fallback human-verification wait when the page cache is disabled (TTL 0).
DEFAULT_VERIFICATION_WAIT_SECONDS = 7200
# material-extract/v4: synchronous channel selection, ontology owned by the nightly batch.
EXTRACT_PROTOCOL_VERSION = "material-extract/v4"
# Local-only outbox payload extras, stripped before POSTing to the server.
LOCAL_PAYLOAD_KEYS = ("requestId", "urlMap")
# A run owns its browser profile until it reaches a terminal state; a
# waiting_for_user run keeps the page open so the human can finish verification.
ACTIVE_RUN_STATES_SQL = "('pending','running','waiting_for_user','waiting_server','interrupted')"
TERMINAL_RUN_STATES = {"succeeded", "failed", "cancelled"}
# Idle camoufox sessions are swept periodically, not on every scheduler tick.
IDLE_BROWSER_SWEEP_SECONDS = 5


class Daemon:
    def __init__(self, data_dir, *, browser=None, transport=None, decision_transport=None, vision_transport=None):
        self.data_dir = data_dir
        self.state = State(data_dir / "client.db")
        self.cache = LocalCache(self.state)
        self.last_cache_cleanup = 0
        self.browser = browser or Browser(data_dir / "browser", warning_handler=self.browser_warning)
        self.transport = transport
        self.decision_transport = decision_transport
        self.vision_transport = vision_transport
        self._server_features = None
        self._server_features_at = 0.0
        self._vision_limit = None
        self._vision_concurrency = None
        self.tasks = []
        self.running = {}
        self.report_running = {}
        self.instance = None
        self._last_idle_sweep = 0.0
        # Production domainGapMs: earliest next attempt start per base domain.
        self._domain_gap = {}

    def browser_warning(self, warning):
        if warning.get("code") == "GEOIP_UNAVAILABLE":
            self.state.event("browser_warning", "GeoIP 定位失败或数据库缺失，可能会增加暴露的风险，降低获取成功率；程序将继续运行。", level="warning")

    async def start(self):
        from osbi_contracts.instance import InstanceLock
        self.instance = InstanceLock(self.data_dir / "client.lock")
        self.instance.acquire()
        self.state.execute("UPDATE runs SET state='interrupted',error='客户端重启，检查浏览器状态后可继续',updated=? WHERE state='running'", (time.time(),))
        # Recompute report slots from now; missed slots outside the process are not replayed.
        self.state.execute("DELETE FROM report_schedules")
        self.tasks = [asyncio.create_task(self.run_loop()), asyncio.create_task(self.outbox_loop())]
        self.state.event("backend_started", "本地后台已启动")

    async def close(self):
        report_tasks = list(self.report_running.values())
        for task in [*self.tasks, *self.running.values(), *report_tasks]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.running.values(), *report_tasks, return_exceptions=True)
        # Release camoufox processes left behind by finished runs; browsers of
        # waiting/interrupted runs stay open so those runs can resume.
        await self._close_idle_browsers()
        self.state.event("backend_stopped", "本地后台已停止")
        self.state.close()
        if self.instance:
            self.instance.release()

    async def _close_idle_browsers(self):
        """Close browser sessions whose profile has no active run.

        Mirrors production browser.mjs close(): the persistent context shuts
        down and the camoufox process exits. The managed runtime exposes the
        same semantics as the CLI 'close' action. Leased, busy or starting
        sessions are never touched, and waiting_for_user runs keep their page.
        """
        busy = {json.loads(r["source"])["profile"] for r in self.state.rows(
            f"SELECT source FROM runs WHERE state IN {ACTIVE_RUN_STATES_SQL}")}
        try:
            sessions = (await self.browser.command("default", "sessions")).get("sessions", [])
        except BrowserError as error:
            logger.warning("browser session enumeration failed: %s", error)
            return
        for info in sessions:
            session = info.get("session")
            if not session or session in busy or not info.get("running"):
                continue
            if info.get("leased") or info.get("state") in {"busy", "waiting_human", "starting"}:
                continue
            try:
                await self.browser.command(session, "close", {})
                self.state.event("browser_closed", f"任务结束，空闲浏览器会话已关闭：{session}")
            except BrowserError as error:
                logger.warning("idle browser close failed for %s: %s", session, error)

    def start_run(self, source_id, *, force_refresh=False):
        config = self.state.config()
        source = next((s for s in config.sources if s.id == source_id), None)
        if source is None:
            raise ValueError("信源不存在")
        if not config.serverUrl:
            raise ValueError("请先配置共享服务地址")
        if self.state.one("SELECT id FROM runs WHERE state IN ('pending','running','waiting_for_user','waiting_server','interrupted') AND json_extract(source,'$.id')=?", (source_id,)):
            raise ValueError("该信源已有尚未结束的任务")
        run_id = "run_" + secrets.token_hex(12)
        now = time.time()
        self.state.execute("INSERT INTO runs(id,source,config,state,queue,created,updated) VALUES (?,?,?,'pending',?,?,?)",
                           (run_id, encode(source.model_dump()), encode(config.model_dump()), encode([source.url]), now, now))
        if force_refresh:
            self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode({"forceRefresh": True}), run_id))
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

    def _expire_stale_prompts(self, config):
        # A human-verification wait follows the local page-cache validity: once the
        # cached page would expire, waiting longer cannot reuse it, so terminate.
        max_wait = config.captureCache.defaultTtlSeconds or DEFAULT_VERIFICATION_WAIT_SECONDS
        now = time.time()
        for row in self.state.rows("SELECT * FROM runs WHERE state='waiting_for_user'"):
            if now - row["updated"] > max_wait:
                self.state.execute("UPDATE runs SET state='cancelled',prompt='',error=?,updated=? WHERE id=?",
                                   ("人工验证等待超时（超过本地缓存有效期），任务已终止", now, row["id"]))
                self.state.event("run_cancelled", "人工验证等待超时，任务已终止", run_id=row["id"], level="warning")

    async def run_loop(self):
        while True:
            config = self.state.config()
            self._expire_stale_prompts(config)
            if time.time() - self.last_cache_cleanup >= 300:
                self.cache.prune(config)
                self.last_cache_cleanup = time.time()
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
            self._schedule_pending(config)
            self._schedule_reports(config)
            if time.time() - self._last_idle_sweep >= IDLE_BROWSER_SWEEP_SECONDS:
                self._last_idle_sweep = time.time()
                await self._close_idle_browsers()
            await asyncio.sleep(0.25)

    def _schedule_reports(self, config):
        """Run configured report tasks at their Asia/Shanghai schedule slots."""
        if not config.serverUrl:
            return
        self.report_running = {key: task for key, task in self.report_running.items()
                               if not task.done()}
        for profile in config.reportProfiles:
            for task_name in ("topnews", "weeklyreport"):
                task_config = getattr(profile, task_name)
                key = (profile.id, task_name)
                if not task_config.enabled:
                    self.state.execute(
                        "DELETE FROM report_schedules WHERE profile=? AND task=?", key)
                    continue
                row = self.state.one(
                    "SELECT next_at FROM report_schedules WHERE profile=? AND task=?", key)
                if row is None:
                    from .report_settings import next_run_at
                    self.state.execute(
                        "INSERT INTO report_schedules(profile,task,next_at) VALUES (?,?,?)",
                        (*key, next_run_at(task_config.schedule)))
                    continue
                if row["next_at"] > time.time() or key in self.report_running:
                    continue
                from .report_settings import next_run_at
                self.state.execute(
                    "UPDATE report_schedules SET next_at=? WHERE profile=? AND task=?",
                    (next_run_at(task_config.schedule), *key))
                self.report_running[key] = asyncio.create_task(
                    self._run_report(profile.id, task_name), name=f'report-{key[0]}-{key[1]}')

    async def _run_report(self, profile_id, task_name):
        key = (profile_id, task_name)
        try:
            from .shadow_reports import run_topnews, run_weekly
            if task_name == "topnews":
                result = await run_topnews(self, profile=profile_id)
            else:
                result = await run_weekly(self, profile=profile_id)
            self.state.event("report_scheduled",
                             f'{task_name} 已按计划生成',
                             details={"profile": profile_id, "task": task_name,
                                      "path": result.get("path", ""),
                                      "delivery": result.get("delivery")})
        except Exception as error:  # noqa: BLE001 - scheduler must survive
            logger.exception("scheduled report failed: profile=%s task=%s", profile_id, task_name)
            self.state.event("report_schedule_failed",
                             f'{task_name} 定时任务失败：{type(error).__name__}',
                             level="error", details={"profile": profile_id, "task": task_name})
        finally:
            self.report_running.pop(key, None)

    def _schedule_pending(self, config):
        """Admit pending runs under the production per-domain policy: base-domain
        limits (domain-policy.mjs), the global concurrency cap, the 1s domain
        gap, verification cooldowns and scheduled capture retries."""
        self.running = {k: t for k, t in self.running.items() if not t.done()}
        active_rows = self.state.rows("SELECT source,queue FROM runs WHERE state IN ('running','waiting_for_user','interrupted')")
        active_profiles = {json.loads(r["source"])["profile"] for r in active_rows}
        for run_id in self.running:
            row = self.state.one("SELECT source FROM runs WHERE id=?", (run_id,))
            if row:
                active_profiles.add(json.loads(row["source"])["profile"])
        active_domains = Counter(base_domain((json.loads(r["queue"]) or [json.loads(r["source"])["url"]])[0]) for r in active_rows)
        policy = config.domainConcurrency
        for row in self.state.rows("SELECT * FROM runs WHERE state='pending' ORDER BY created"):
            source = json.loads(row["source"])
            browser_state = json.loads(row["browser"])
            domain = base_domain((json.loads(row["queue"]) or [source["url"]])[0])
            cooldown = self.state.one("SELECT next_at FROM domain_cooldowns WHERE domain=?", (domain,))
            resuming = bool(browser_state.get("pageReady"))
            if len(self.running) >= min(config.browserConcurrency, MAX_CONCURRENCY):
                break
            # Retry delays gate every start; cooldowns and the domain gap only
            # gate fresh attempts, never a human-confirmed resume.
            not_before = browser_state.get("retryNotBefore", 0)
            if not resuming:
                not_before = max(not_before, self._domain_gap.get(domain, 0), cooldown["next_at"] if cooldown else 0)
            if active_domains[domain] >= policy.limit_for(domain) or not_before > time.time():
                continue
            if row["id"] not in self.running and source["profile"] not in active_profiles:
                self.state.execute("UPDATE runs SET state='running',updated=? WHERE id=? AND state='pending'", (time.time(), row["id"]))
                self.running[row["id"]] = asyncio.create_task(self.acquire(row["id"]))
                active_profiles.add(source["profile"])
                active_domains[domain] += 1

    async def acquire(self, run_id):
        row = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        config, source = ClientConfig.model_validate(json.loads(row["config"])), Source.model_validate(json.loads(row["source"]))
        session = source.profile
        browser_state = json.loads(row["browser"])
        lease = browser_state.get("lease")
        # Attempt URL for domain policy; falls back to the seed before a queue head exists.
        url = source.url
        try:
            hunter = hunter_for(source.kind)
            if hunter and not browser_state.get("hunter_discovered"):
                await self._discover_catalog(run_id, source, config, browser_state, hunter)
                return
            if source.kind == "rss" and not browser_state.get("rss_discovered"):
                feed_scope = digest(["rss-feed/v1", source.profile])
                cached_feed = None if browser_state.get("forceRefresh") else self.cache.capture_get(source.url, feed_scope, config.captureCache)
                if cached_feed:
                    urls = cached_feed["feedUrls"]
                else:
                    async with httpx.AsyncClient(timeout=config.timeoutSeconds, follow_redirects=True, trust_env=False) as client:
                        response = await client.get(source.url)
                        response.raise_for_status()
                    feed = feedparser.parse(response.content)
                    unique = {}
                    for entry in feed.entries:
                        link = getattr(entry, "link", "")
                        if canonical_url(link):
                            unique.setdefault(canonical_url(link), link)
                    urls = list(unique.values())
                    if urls:
                        self.cache.capture_put(source.url, feed_scope, {"feedUrls": urls}, config.captureCache)
                # Discovered feed entries pass the forbidden-fragment filter;
                # the feed URL itself is an explicit seed and stays exempt.
                urls = [u for u in urls if not forbidden_url(u, config.forbiddenDomains)][:config.maxUrls]
                if not urls:
                    raise ValueError("RSS 未返回有效文章链接")
                self.state.execute("UPDATE runs SET queue=?,browser=? WHERE id=?", (encode(urls), encode({"rss_discovered": True}), run_id))
            current = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
            queue, visited = json.loads(current["queue"]), json.loads(current["visited"])
            if not queue or len(visited) >= config.maxUrls:
                outstanding = self.state.one("SELECT COUNT(*) n FROM outbox WHERE run_id=? AND state NOT IN ('done','failed','cancelled')", (run_id,))["n"]
                state = 'waiting_server' if outstanding else 'succeeded'
                self.state.execute("UPDATE runs SET state=?,updated=? WHERE id=?", (state, time.time(), run_id))
                if state == 'succeeded':
                    self.state.event('run_finished', '采集与筛选处理已完成', run_id=run_id)
                return
            url = queue[0]
            # Admission guard for queued links; the explicit depth-0 seed is
            # exempt, mirroring production general_process allow_forbidden.
            if url != source.url and forbidden_url(url, config.forbiddenDomains):
                self.state.execute("UPDATE runs SET queue=?,state='pending',updated=? WHERE id=?", (encode(queue[1:]), time.time(), run_id))
                self.state.event("url_forbidden", "链接命中禁爬片段，已跳过", run_id=run_id, details={"url": url})
                return
            seen = {canonical_url(u) for u in visited} | set(browser_state.get("visitedAliases", []))
            if canonical_url(url) in seen and not browser_state.get("pageReady"):
                self.state.execute("UPDATE runs SET queue=?,state='pending',updated=? WHERE id=?", (encode(queue[1:]), time.time(), run_id))
                return
            scope = self.cache.scope(source, config, not visited)
            material = None
            if not browser_state.get("forceRefresh") and not browser_state.get("pageReady") and current["step"] == 0:
                material = self.cache.capture_get(url, scope, config.captureCache)
            if material is None:
                material = await self.capture_page(run_id, source, config, current, url, visited, browser_state)
                if material is None:
                    return
                # Production batch-queue resets the verification streak on access ok.
                self.state.execute("UPDATE domain_cooldowns SET strikes=0 WHERE domain=?", (base_domain(url),))
                self.cache.capture_put(url, scope, material, config.captureCache)
            else:
                self.state.event("capture_cached", "复用本地页面缓存，跳过浏览器抓取", run_id=run_id)
            await self._submit_material(run_id, source, config, url, material, queue, visited, browser_state)
        except BrowserError as error:
            if error.code in {"VERIFICATION_REQUIRED", "CAPTURE_PARTIAL", "OUTCOME_UNKNOWN", "LEASE_EXPIRED"}:
                if error.code == "VERIFICATION_REQUIRED":
                    domain = base_domain(url)
                    # Production verificationDelaysMs [60000]: non-WeChat
                    # verification pages cool the base domain down for 60s.
                    # WeChat gates are skipped without a cooldown in production;
                    # osbi keeps the human prompt but never cools mp.weixin.
                    if domain != WECHAT_DOMAIN:
                        old = self.state.one("SELECT strikes FROM domain_cooldowns WHERE domain=?", (domain,))
                        strikes = old["strikes"] + 1 if old else 1
                        delay = VERIFICATION_DELAYS_MS[min(strikes - 1, len(VERIFICATION_DELAYS_MS) - 1)] / 1000
                        self.state.execute("INSERT INTO domain_cooldowns VALUES (?,?,?) ON CONFLICT(domain) DO UPDATE SET next_at=excluded.next_at,strikes=excluded.strikes",
                                           (domain, time.time() + delay, strikes))
                if lease:
                    try:
                        await self.browser.command(session, "lease.wait", {}, lease)
                    except BrowserError:
                        pass
                self.prompt(run_id, str(error))
            else:
                # Production retryDelaysMs [20000, 40000]: retryable capture
                # failures requeue the run twice before it fails. CAPTURE_EMPTY
                # is a completed access with no content, not a retryable error.
                retries = browser_state.get("captureRetries", 0)
                if error.code != "CAPTURE_EMPTY" and retries < len(RETRY_DELAYS_MS):
                    delay = RETRY_DELAYS_MS[retries] / 1000
                    browser_state["captureRetries"] = retries + 1
                    browser_state["retryNotBefore"] = time.time() + delay
                    self.state.execute("UPDATE runs SET state='pending',error='',browser=?,updated=? WHERE id=?",
                                       (encode(browser_state), time.time(), run_id))
                    self.state.event("capture_retry", f"采集失败，{int(delay)} 秒后自动重试（第 {retries + 1} 次，共 {len(RETRY_DELAYS_MS)} 次）",
                                     run_id=run_id, level="warning", details={"code": error.code})
                else:
                    self.state.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=?", (str(error), time.time(), run_id))
                    self.state.event("run_failed", str(error), run_id=run_id, level="error")
        except Exception:
            self.state.execute("UPDATE runs SET state='failed',error='采集失败，请检查信源和运行设置',updated=? WHERE id=?", (time.time(), run_id))
            self.state.event("run_failed", "采集失败，请检查信源和运行设置", run_id=run_id, level="error")
        finally:
            lease = browser_state.get("lease", lease)
            if lease:
                # Production domainGapMs: one second between attempts on a domain.
                self._domain_gap[base_domain(url)] = time.time() + DOMAIN_GAP_MS / 1000
            state = self.state.one("SELECT state FROM runs WHERE id=?", (run_id,))
            if lease and state and state["state"] not in {"waiting_for_user", "interrupted"}:
                try:
                    await self.browser.command(session, "lease.release", {}, lease)
                    browser_state.pop("lease", None)
                    self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
                except BrowserError:
                    pass

    async def _discover_catalog(self, run_id, source, config, browser_state, hunter):
        def event(kind, message, level="info"):
            self.state.event(kind, message, run_id=run_id, level=level)
        try:
            discovery = await hunter.discover(source, config,
                                              force_refresh=bool(browser_state.get("forceRefresh")),
                                              cache=self.cache, event=event)
        except MpScanError as error:
            self.state.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=?",
                               (str(error), time.time(), run_id))
            self.state.event("run_failed", str(error), run_id=run_id, level="error")
            return
        new_state = {"hunter_discovered": True, "seedMeta": discovery.seed_meta}
        if browser_state.get("forceRefresh"):
            new_state["forceRefresh"] = True
        if discovery.material is None:
            self.state.execute("UPDATE runs SET queue=?,visited=?,step=0,state='succeeded',browser=?,updated=? WHERE id=?",
                               (encode([]), encode([source.url]), encode(new_state), time.time(), run_id))
            self.state.event("run_finished", "公众号目录为空，本次无文章可采集", run_id=run_id)
            return
        # Acquisition performs no relevance screening; server channel relevance
        # is the only gate.
        await self._submit_material(run_id, source, config, source.url, discovery.material,
                                    [source.url], [], new_state)

    async def _submit_material(self, run_id, source, config, url, material, queue, visited, browser_state):
        meta = material["meta"]
        valid_meta = {k: str(meta.get(k) or "") for k in SourceMeta.model_fields if k not in {"adapter", "textOnly", "mediaPending"}}
        valid_meta.update(textOnly=True, mediaPending=bool(meta.get("mediaPending")), adapter=meta.get("adapter") or "browser")
        if valid_meta["mediaPending"]:
            self.state.event("media_pending", "正文图片尚未识别，本次仅提交可用文字", run_id=run_id, level="warning")
        include_roles = await self.link_role_support(config)
        links = []
        for link in material["links"][:200]:
            record = {**link, "url": material["localMaps"]["urls"][link["id"]]}
            if not include_roles:
                record.pop("role", None)
            links.append(record)
        client_id = self.state.one("SELECT client_id FROM identity")["client_id"]
        url_map = {link["id"]: link["url"] for link in links}
        remote_ids = {"blockIds": [b["id"] for b in material["blocks"][:64]],
                      "linkIds": [link["id"] for link in links]}
        payloads = []
        for channel in source.channels:
            # materialHash = normalized content + channel. The same page may be
            # submitted to several channels; each is a distinct server job with
            # its own statement/rules/date window.
            request_id = digest([run_id, url, channel, material.get("materialId", "")])
            candidate = Material(clientId=client_id, requestId=request_id, title=meta.get("title", ""),
                                 channel=channel, blocks=material["blocks"][:64], links=links,
                                 meta=SourceMeta(**valid_meta)).model_dump()
            payloads.append({
                "protocolVersion": EXTRACT_PROTOCOL_VERSION,
                "materialId": material_hash(candidate),
                "channel": channel,
                # input.links never carries URLs; the server selects by ID and
                # the local urlMap below restores them for link acquisition.
                "input": {"title": candidate["title"], "blocks": candidate["blocks"],
                          "links": [{k: v for k, v in link.items()
                                     if k != "url" and (include_roles or k != "role")}
                                    for link in candidate["links"]]},
                "meta": candidate["meta"],
                "mode": "only_link" if not candidate["blocks"] else "both",
                # Local-only extras, stripped before POSTing to the server.
                "requestId": request_id,
                "urlMap": url_map,
            })
        seen = {canonical_url(u) for u in visited} | set(browser_state.get("visitedAliases", []))
        with self.state.transaction() as conn:
            for payload in payloads:
                conn.execute("INSERT OR IGNORE INTO outbox(id,run_id,payload,state,server_url,server_ca,credential_generation,cache_key,remote_ids) VALUES (?,?,?,?,?,?,?,?,?)",
                             (payload["requestId"], run_id, encode(payload), "pending", config.serverUrl, config.serverCA,
                              self.state.one("SELECT revision FROM config WHERE id=1")["revision"], "", encode(remote_ids)))
            visited.append(url)
            seen.update(canonical_url(u) for u in (url, meta.get("finalUrl", ""), meta.get("canonicalUrl", "")))
            browser_state["visitedAliases"] = sorted(seen - {""})
            browser_state.pop("pageReady", None)
            # The next queue head is a fresh job: retry budget starts over.
            browser_state.pop("captureRetries", None)
            browser_state.pop("retryNotBefore", None)
            if payloads:
                next_state = "waiting_server"
            else:
                outstanding = conn.execute("SELECT COUNT(*) FROM outbox WHERE run_id=? AND state NOT IN ('done','failed')", (run_id,)).fetchone()[0]
                next_state = ("pending" if queue[1:] and len(visited) < config.maxUrls
                              else "waiting_server" if outstanding else "succeeded")
            conn.execute("UPDATE runs SET queue=?,visited=?,step=0,state=?,browser=?,updated=? WHERE id=?",
                         (encode(queue[1:]), encode(visited), next_state, encode(browser_state), time.time(), run_id))
        if payloads:
            # A catalog has only links; an article has original blocks.
            catalog = not payloads[0]["input"]["blocks"]
            message = ("公众号目录已提交，等待共享服务筛选相关文章" if catalog
                       else "原文已清洗切块，等待共享服务处理")
            self.state.event("captured", message, run_id=run_id)
        else:
            self.state.event("acquisition_only", "信源未配置 channel，本次仅采集未提交", run_id=run_id)

    async def link_role_support(self, config):
        """Detect server support for optional link roles; cache per endpoint."""
        key = (config.serverUrl, digest(config.serverKey), config.serverCA)
        now = time.time()
        if self._server_features and self._server_features[0] == key and now - self._server_features_at < 3600:
            return self._server_features[1]
        supported = False
        try:
            health = await request(config, "GET", "/health", transport=self.transport)
            features = health.get("features") if isinstance(health, dict) else None
            supported = bool((features or {}).get("linkRoles")
                             or (isinstance(health, dict) and health.get("linkRolesSupported")))
        except Exception:  # noqa: BLE001 - capability probe must never block submission
            supported = False
        self._server_features = (key, supported)
        self._server_features_at = now
        return supported

    async def submission_context(self, config, payload):
        """Local reuse identity: material content + channel + source URL.

        wiseflow extract is synchronous and has no capabilities endpoint, so the
        identity is computed locally; a new source URL must reach the server once
        even when its content is shared.
        """
        if not config.submissionCache.ttlSeconds:
            return "", 0
        meta = payload['meta']
        source = meta.get('canonicalUrl') or meta.get('finalUrl') or meta['requestedUrl']
        key = digest([config.serverUrl, digest(config.serverKey), config.serverCA,
                      payload["materialId"], payload.get("channel"), source])
        return key, config.submissionCache.ttlSeconds

    async def capture_page(self, run_id, source, config, current, url, visited, browser_state):
        session, lease = source.profile, browser_state.get("lease")
        if not lease:
            await self.browser.command(session, "start", {"headless": config.headless}, headed=not config.headless)
            lease = (await self.browser.command(session, "lease.acquire", {"ttlMs": 300000}))["leaseToken"]
        else:
            try:
                await self.browser.command(session, "lease.renew", {"ttlMs": 300000}, lease)
            except BrowserError as error:
                if error.code != "LEASE_EXPIRED":
                    raise
                lease = (await self.browser.command(session, "lease.acquire", {"ttlMs": 300000}))["leaseToken"]
        browser_state["lease"] = lease
        self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
        if current["step"] == 0 and not browser_state.get("pageReady"):
            browser_state["pendingStep"] = -1
            self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
            await self.browser.command(session, "open", {"url": url}, lease)
            browser_state.pop("pendingStep", None)
            browser_state["pageReady"] = True
            self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(browser_state), run_id))
        for i, step in enumerate(source.steps if not visited else []):
            if i < current["step"]:
                continue
            if step.action == "wait_for_user":
                self.state.execute("UPDATE runs SET step=? WHERE id=?", (i + 1, run_id))
                await self.browser.command(session, "lease.wait", {}, lease)
                self.prompt(run_id, step.text or "请在浏览器中完成操作后继续")
                return None
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
        seed_meta = browser_state.get("seedMeta", {}).get(canonical_url(url))
        enricher = self._vision_enricher(config, run_id)
        return await self.browser.capture(session, url, lease, seed_meta=seed_meta, enricher=enricher)

    def _vision_enricher(self, config, run_id):
        if not config.vision.enabled:
            return None
        from .vision import VisionClient
        from .image_material import ImageEnricher
        # A daemon-shared semaphore bounds total VL concurrency across parallel runs.
        if self._vision_limit is None or self._vision_concurrency != config.vision.concurrency:
            self._vision_concurrency = config.vision.concurrency
            self._vision_limit = asyncio.Semaphore(config.vision.concurrency)
        client = VisionClient(config.vision, transport=self.vision_transport, limit=self._vision_limit)
        return ImageEnricher(config.vision, cache=self.cache, client=client,
                             event=lambda kind, message, level="info", details=None:
                             self.state.event(kind, message, run_id=run_id, level=level, details=details))

    async def outbox_loop(self):
        while True:
            for row in self.state.rows("SELECT * FROM outbox WHERE state IN ('pending','polling','waiting_local') AND next_at<=? ORDER BY rowid LIMIT 10", (time.time(),)):
                config = self.state.config()
                owner = self.state.one("SELECT state FROM runs WHERE id=?", (row["run_id"],))
                if owner and owner["state"] == "cancelled":
                    self.state.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
                    continue
                try:
                    if config.serverUrl != row["server_url"]:
                        raise ServerError("待提交材料绑定的服务地址已变更，请恢复原地址后继续", 401)
                    payload = json.loads(row["payload"])
                    if payload.get("protocolVersion") != EXTRACT_PROTOCOL_VERSION:
                        # Pre-migration outbox rows cannot be sent to wiseflow;
                        # fail them explicitly instead of retrying forever.
                        raise ServerError("旧协议待提交材料无法发送到新服务，请重新采集", 422)
                    body = {k: v for k, v in payload.items() if k not in LOCAL_PAYLOAD_KEYS}
                    selector = payload.get("input", {})
                    remote_ids = json.loads(row["remote_ids"]) if row["remote_ids"] else {
                        "blockIds": [b["id"] for b in selector.get("blocks", [])],
                        "linkIds": [l["id"] for l in selector.get("links", [])]}
                    if row["state"] == "waiting_local":
                        key, _ = await self.submission_context(config, payload)
                        if not key or key != row["cache_key"]:
                            self.state.execute("UPDATE outbox SET state='pending',cache_key='',next_at=0 WHERE id=?", (row["id"],))
                            continue
                        cached = self.cache.submission(row["cache_key"])
                        if cached.get("result"):
                            result = cached["result"]
                            remote_ids = cached["remoteIds"]
                        elif self.cache.reserve(row["cache_key"], row["id"], config.submissionCache.ttlSeconds):
                            self.state.execute("UPDATE outbox SET next_at=? WHERE id=?", (time.time() + 1, row["id"]))
                            continue
                        else:
                            result = await request(config, "POST", "/v1/materials/extract", body, transport=self.transport)
                    else:
                        # 'polling' rows are legacy async jobs from the old server;
                        # wiseflow extract is synchronous, so resubmit them once.
                        key, ttl = await self.submission_context(config, payload)
                        row["cache_key"] = key
                        self.state.execute("UPDATE outbox SET cache_key=? WHERE id=?", (key, row["id"]))
                        if self.cache.reserve(key, row["id"], ttl):
                            self.state.execute("UPDATE outbox SET state='waiting_local',next_at=0 WHERE id=?", (row["id"],))
                            continue
                        result = await request(config, "POST", "/v1/materials/extract", body, transport=self.transport)
                    # Synchronous ExtractResponse: info receipts are discarded and
                    # ontology jobs stay pending for the server's nightly batch.
                    result = {k: v for k, v in result.items() if k not in ("infos", "infoCount")}
                    failed = any(e.get("stage") == "selection" for e in result.get("errors", []))
                    self.cache.save_result(row["cache_key"], result, remote_ids, failed=failed)
                    state = "failed" if failed else "done"
                    self.state.execute("UPDATE outbox SET state=?,remote_id=?,result=?,remote_ids=?,next_at=?,error='' WHERE id=?",
                                       (state, payload.get("materialId", ""), encode(result), encode(remote_ids), time.time() + 2, row["id"]))
                    if failed:
                        self.state.event("server_job", "共享服务：相关性筛选失败", run_id=row["run_id"], level="error",
                                         details={"materialId": payload.get("materialId", ""), "errors": result.get("errors", [])})
                        self.state.execute("UPDATE runs SET state='failed',error='共享服务提取失败',updated=? WHERE id=? AND state!='cancelled'", (time.time(), row["run_id"]))
                        continue
                    self.state.event("server_job", "共享服务：筛选完成，本体提取进入夜间批处理", run_id=row["run_id"],
                                     details={"materialId": payload.get("materialId", ""),
                                              "blockIds": result.get("selection", {}).get("blockIds", []),
                                              "linkIds": result.get("selection", {}).get("linkIds", []),
                                              "ontologyJobs": len(result.get("ontologyJobs", [])),
                                              "modelCalls": result.get("modelCalls", 0),
                                              "errors": result.get("errors", [])})
                    run = self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
                    if not run or run["state"] in {"cancelled", "failed", "succeeded"}:
                        continue
                    if not row["links_applied"]:
                        # Restore selected link URLs from the local map (never sent
                        # to the server), then filter, dedup and extend the queue.
                        selected = result.get("selection", {}).get("linkIds", [])
                        url_map = payload.get("urlMap") or {l["id"]: l["url"] for l in payload.get("links", []) if "url" in l}
                        found = [url_map[link_id] for link_id in selected if link_id in url_map]
                        visited, queue = json.loads(run["visited"]), json.loads(run["queue"])
                        seen = {canonical_url(u) for u in visited} | set(json.loads(run["browser"]).get("visitedAliases", []))
                        queued = {canonical_url(u) for u in queue}
                        run_config = ClientConfig.model_validate(json.loads(run["config"]))
                        for url in found:
                            # Production forbidden_url: URL substring fragments
                            # (domain or domain+path prefix), not host-endswith.
                            if canonical_url(url) not in seen | queued and not forbidden_url(url, run_config.forbiddenDomains):
                                queue.append(url)
                                queued.add(canonical_url(url))
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
                        self.state.event("run_finished", "采集与筛选处理已完成", run_id=row["run_id"])
                except ServerError as error:
                    state = "blocked" if error.status in {401, 403} else "failed" if error.status in {400, 409, 422} else row["state"]
                    self.state.execute("UPDATE outbox SET state=?,error=?,attempts=attempts+1,next_at=? WHERE id=?",
                                       (state, str(error), time.time() + error.retry_after, row["id"]))
                    if state == "failed" and row["cache_key"]:
                        self.state.execute("DELETE FROM submission_cache WHERE id=?", (row["cache_key"],))
                    if row["error"] != str(error):
                        self.state.event("server_error", str(error), run_id=row["run_id"], level="error")
                    if state == "failed":
                        self.state.execute("UPDATE runs SET state='failed',error=?,updated=? WHERE id=? AND state!='cancelled'", (str(error), time.time(), row["run_id"]))
                except Exception:
                    self.state.execute("UPDATE outbox SET next_at=?,error='共享服务返回了无效数据' WHERE id=?", (time.time()+15, row["id"]))
            await asyncio.sleep(0.5)
