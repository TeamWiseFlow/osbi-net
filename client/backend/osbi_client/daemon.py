"""Independent client runtime, supervised acquisition and durable server outbox."""
import asyncio
import json
import logging
import secrets
import time
from osbi_contracts.models import Material, SourceMeta
from osbi_contracts.storage import digest, encode
from osbi_contracts.material import material_hash
from osbi_contracts.urls import canonical_url
from hunters.wx_mp import MpScanError
from .browser import Browser, BrowserError
from .state import State
from .cache import LocalCache
from .server_client import request, ServerError

logger = logging.getLogger(__name__)

# Headless partial-page retries before moving a run to the human queue.
PARTIAL_RETRY_DELAYS_SECONDS = (15, 30, 60, 90, 105)
# material-extract/v4: synchronous channel selection, ontology owned by the nightly batch.
EXTRACT_PROTOCOL_VERSION = "material-extract/v4"
# Local-only outbox payload extras, stripped before POSTing to the server.
LOCAL_PAYLOAD_KEYS = ("requestId", "urlMap")
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
        self._vision_limit = None
        self._vision_concurrency = None
        self.tasks = []
        self.running = {}
        self.report_running = {}
        self.instance = None
        self._last_idle_sweep = 0.0
        # Production domainGapMs: earliest next attempt start per base domain.
        self._domain_gap = {}
        self.submitting = {}
        from .acquisition import Acquisition
        self.acquisition = Acquisition(self)

    def browser_warning(self, warning):
        if warning.get("code") == "GEOIP_UNAVAILABLE":
            self.state.event("browser_warning", "GeoIP 定位失败或数据库缺失，可能会增加暴露的风险，降低获取成功率；程序将继续运行。", level="warning")

    async def start(self):
        from osbi_contracts.instance import InstanceLock
        self.instance = InstanceLock(self.data_dir / "client.lock")
        self.instance.acquire()
        self.acquisition.expire()
        self.state.execute("UPDATE outbox SET state='pending' WHERE state='sending'")
        self.state.execute("UPDATE acquisition_pages SET state='interrupted',error='客户端重启，请检查页面后继续' WHERE state='running'")
        self.state.execute("UPDATE runs SET state='interrupted',error='客户端重启，检查浏览器状态后可继续',updated=? WHERE state='running'", (time.time(),))
        # Recompute report slots from now; missed slots outside the process are not replayed.
        self.state.execute("DELETE FROM report_schedules")
        self.tasks = [asyncio.create_task(self.run_loop()), asyncio.create_task(self.outbox_loop())]
        self.state.event("backend_started", "本地后台已启动")

    async def close(self):
        report_tasks = list(self.report_running.values())
        extra_tasks = [*self.submitting.values(), *self.acquisition.background]
        for task in [*self.tasks, *self.running.values(), *report_tasks, *extra_tasks]:
            task.cancel()
        await asyncio.gather(*self.tasks, *self.running.values(), *report_tasks, *extra_tasks, return_exceptions=True)
        # Release camoufox processes left behind by finished runs; browsers of
        # waiting/interrupted runs stay open so those runs can resume.
        await self._close_idle_browsers()
        self.state.event("backend_stopped", "本地后台已停止")
        self.state.close()
        if self.instance:
            self.instance.release()

    async def _close_idle_browsers(self):
        await self.acquisition.retire_idle()

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
        self.state.execute("UPDATE runs SET deadline=? WHERE id=?", (now + 10800, run_id))
        self.state.event("run_started", "采集任务已创建", run_id=run_id)
        return self.state.run(run_id)

    def prompt(self, run_id, message):
        self.acquisition.seed(self.state.one("SELECT * FROM runs WHERE id=?", (run_id,)))
        page = self.state.one("SELECT * FROM acquisition_pages WHERE run_id=? AND state NOT IN ('done','failed','cancelled') ORDER BY created LIMIT 1", (run_id,))
        if page:
            self.acquisition.prompt(page["id"], message, json.loads(page["browser"]))

    def act(self, run_id, action, prompt_id=None):
        if not self.state.run(run_id):
            raise ValueError("任务不存在")
        return self.acquisition.act(run_id, action, prompt_id)

    def _expire_stale_prompts(self, config):
        self.acquisition.expire()

    async def _check_human_prompts(self, config):
        await self.acquisition.maintenance()

    async def run_loop(self):
        while True:
            config = self.state.config()
            await self.acquisition.maintenance()
            if time.time() - self.last_cache_cleanup >= 300:
                self.cache.prune(config)
                self.last_cache_cleanup = time.time()
            self._schedule_sources(config)
            self._schedule_pending(config)
            self._schedule_reports(config)
            if time.time() - self._last_idle_sweep >= IDLE_BROWSER_SWEEP_SECONDS:
                self._last_idle_sweep = time.time()
                self.acquisition.spawn(self._close_idle_browsers())
            await asyncio.sleep(0.25)

    def _schedule_sources(self, config, *, now=None):
        """Atomically consume daily/interval slots and enqueue eligible sources.

        After downtime, coalesce overdue clocks into one run and advance to the
        next future slot. An active run consumes a slot without overlapping it.
        """
        if not config.serverUrl:
            return
        from .source_schedule import next_capture_at
        now = time.time() if now is None else now
        sources = {source.id: source for source in config.sources}
        for table in ('source_time_schedules', 'schedules'):
            for schedule in self.state.rows(f'SELECT * FROM {table} WHERE next_at<=?', (now,)):
                with self.state.transaction() as conn:
                    current = conn.execute(f'SELECT next_at FROM {table} WHERE source_id=?',
                                           (schedule['source_id'],)).fetchone()
                    if not current or current[0] > now:
                        continue
                    source = sources.get(schedule['source_id'])
                    if source is None or not source.enabled:
                        conn.execute(f'DELETE FROM {table} WHERE source_id=?', (schedule['source_id'],))
                        continue
                    if table == 'source_time_schedules':
                        if not source.schedule.times:
                            conn.execute(f'DELETE FROM {table} WHERE source_id=?', (source.id,))
                            continue
                        next_at = next_capture_at(source.schedule, now=now)
                    else:
                        if source.schedule.times or not source.intervalMinutes:
                            conn.execute(f'DELETE FROM {table} WHERE source_id=?', (source.id,))
                            continue
                        next_at = now + source.intervalMinutes * 60
                    conn.execute(f'UPDATE {table} SET next_at=? WHERE source_id=?', (next_at, source.id))
                    active = conn.execute("SELECT 1 FROM runs WHERE state IN ('pending','running','waiting_for_user','waiting_server','interrupted') AND json_extract(source,'$.id')=?",
                                          (source.id,)).fetchone()
                    run_id = ''
                    if not active:
                        run_id = 'run_' + secrets.token_hex(12)
                        conn.execute("INSERT INTO runs(id,source,config,state,queue,created,updated,deadline) VALUES (?,?,?,'pending',?,?,?,?)",
                            (run_id, encode(source.model_dump()), encode(config.model_dump()), encode([source.url]), now, now, now+10800))
                    conn.execute("INSERT INTO events(at,kind,level,message,run_id,details) VALUES (?,?,?,?,?,?)",
                        (now, 'source_schedule_skipped' if active else 'source_scheduled', 'info',
                         'Scheduled capture skipped: source already active' if active else 'Scheduled capture queued',
                         run_id, encode(dict(sourceId=source.id, scheduledFor=current[0], nextAt=next_at,
                                             timezone=source.schedule.timezone if source.schedule.times else None))))
                    conn.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM events)")

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
                                      "kind": "weekly" if task_name == "weeklyreport" else "topnews",
                                      "path": result.get("path", ""),
                                      "docxPath": result.get("docxPath", ""),
                                      "delivery": result.get("delivery"),
                                      "fileDelivery": result.get("fileDelivery")})
        except Exception as error:  # noqa: BLE001 - scheduler must survive
            logger.exception("scheduled report failed: profile=%s task=%s", profile_id, task_name)
            self.state.event("report_schedule_failed",
                             f'{task_name} 定时任务失败：{type(error).__name__}',
                             level="error", details={"profile": profile_id, "task": task_name})
        finally:
            self.report_running.pop(key, None)

    def _schedule_pending(self, config):
        self.acquisition.schedule(config)

    async def acquire(self, run_id):
        await self.acquisition.acquire_next(run_id)

    async def _discover_catalog(self, run_id, source, config, browser_state, hunter):
        def event(kind, message, level="info"):
            self.state.event(kind, message, run_id=run_id, level=level)
        try:
            discovery = await hunter.discover(source, config,
                                              force_refresh=bool(browser_state.get("forceRefresh")),
                                              cache=self.cache, event=event)
        except MpScanError as error:
            self.acquisition.cancel(run_id, str(error), terminal="failed")
            self.state.event("run_failed", str(error), run_id=run_id, level="error")
            return
        if not self.acquisition.active(run_id):
            return
        new_state = {**browser_state, "hunter_discovered": True, "pageQueueReady": True, "seedMeta": discovery.seed_meta}
        self.state.execute("UPDATE runs SET browser=? WHERE id=?", (encode(new_state), run_id))
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
        links = []
        for link in material["links"][:200]:
            record = {**link, "url": material["localMaps"]["urls"][link["id"]],
                      "role": link.get("role") or "unknown"}
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
                          "links": [{k: v for k, v in link.items() if k != "url"}
                                    for link in candidate["links"]]},
                "meta": candidate["meta"],
                "mode": "only_link" if not candidate["blocks"] else "both",
                # Local-only extras, stripped before POSTing to the server.
                "requestId": request_id,
                "urlMap": url_map,
            })
        if not self.acquisition.active(run_id):
            return
        # Concurrent page workers merge with the latest parent state, never a stale queue.
        current = self.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
        queue, visited = json.loads(current["queue"]), json.loads(current["visited"])
        browser_state = {**json.loads(current["browser"]), **browser_state}
        seen = {canonical_url(u) for u in visited} | set(browser_state.get("visitedAliases", []))
        if canonical_url(url) in seen:
            self.acquisition.drop_url(run_id, url)
            return
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
            remaining_queue = [item for item in queue if canonical_url(item) != canonical_url(url)]
            if remaining_queue and len(visited) < config.maxUrls:
                next_state = "pending"
            elif payloads:
                next_state = "waiting_server"
            else:
                outstanding = conn.execute("SELECT COUNT(*) FROM outbox WHERE run_id=? AND state NOT IN ('done','failed')", (run_id,)).fetchone()[0]
                next_state = ("pending" if queue[1:] and len(visited) < config.maxUrls
                              else "waiting_server" if outstanding else "pending")
            conn.execute("UPDATE runs SET queue=?,visited=?,step=0,state=?,browser=?,updated=? WHERE id=?",
                         (encode(remaining_queue), encode(visited), next_state, encode(browser_state), time.time(), run_id))
        if payloads:
            # A catalog has only links; an article has original blocks.
            catalog = not payloads[0]["input"]["blocks"]
            message = ("公众号目录已提交，等待共享服务筛选相关文章" if catalog
                       else "原文已清洗切块，等待共享服务处理")
            self.state.event("captured", message, run_id=run_id)
        else:
            self.state.event("acquisition_only", "信源未配置 channel，本次仅采集未提交", run_id=run_id)

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

    async def _capture_with_partial_grace(self, session, url, lease, seed_meta, enricher,
                                          config, browser_state, run_id):
        """Headless partial-page snapshots get a cumulative grace window.

        The same page/lease stays open and is retried with backoff. A headed
        human session (or a non-headless run) skips this and prompts at once.
        """
        if not config.headless or browser_state.get("headed"):
            return await self.browser.capture(session, url, lease,
                                              seed_meta=seed_meta, enricher=enricher)
        started = time.monotonic()
        delays = PARTIAL_RETRY_DELAYS_SECONDS
        attempt = 0
        settle = True
        while True:
            try:
                return await self.browser.capture(session, url, lease,
                                                  seed_meta=seed_meta, enricher=enricher,
                                                  settle=settle)
            except BrowserError as error:
                if error.code != "CAPTURE_PARTIAL" or attempt >= len(delays):
                    raise
                if time.monotonic() - started >= config.partialCaptureGraceSeconds:
                    raise
                delay = delays[attempt]
                attempt += 1
                settle = False  # status recheck only: never refresh/navigate the page
                self.state.event(
                    "capture_retry",
                    f"页面不完整，{delay} 秒后检查页面状态（第 {attempt} 次，累计宽限 "
                    f"{config.partialCaptureGraceSeconds} 秒）",
                    run_id=run_id, level="warning",
                    details={"code": error.code, "attempt": attempt, "delaySeconds": delay})
                await asyncio.sleep(delay)
                await self.browser.command(session, "lease.renew", {"ttlMs": 300000}, lease)

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
            self.submitting = {key: task for key, task in self.submitting.items() if not task.done()}
            config = self.state.config()
            available = max(0, config.submissionConcurrency - len(self.submitting))
            for row in self.state.rows("SELECT * FROM outbox WHERE state IN ('pending','polling','waiting_local') AND next_at<=? ORDER BY rowid LIMIT ?", (time.time(), available)):
                if row["id"] in self.submitting:
                    continue
                self.state.execute("UPDATE outbox SET state='sending' WHERE id=?", (row["id"],))
                self.submitting[row["id"]] = asyncio.create_task(self._process_outbox(row))
            await asyncio.sleep(0.1)

    async def _process_outbox(self, row):
        config = self.state.config()
        if not self.acquisition.active(row["run_id"]):
            self.state.execute("UPDATE outbox SET state='cancelled' WHERE id=?", (row["id"],))
            return
        owner = self.state.one("SELECT state FROM runs WHERE id=?", (row["run_id"],))
        if owner["state"] == "interrupted" and not self.state.one("SELECT 1 FROM acquisition_pages WHERE run_id=?", (row["run_id"],)):
            self.state.execute("UPDATE outbox SET state='pending',next_at=? WHERE id=?", (time.time()+30, row["id"]))
            return
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
                    return
                cached = self.cache.submission(row["cache_key"])
                if cached.get("result"):
                    result = cached["result"]
                    remote_ids = cached["remoteIds"]
                elif self.cache.reserve(row["cache_key"], row["id"], config.submissionCache.ttlSeconds):
                    self.state.execute("UPDATE outbox SET state='waiting_local',next_at=? WHERE id=?", (time.time() + 1, row["id"]))
                    return
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
                    return
                result = await request(config, "POST", "/v1/materials/extract", body, transport=self.transport)
            if not self.acquisition.active(row["run_id"]):
                return
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
                self.acquisition.cancel(row["run_id"], "共享服务提取失败", terminal="failed")
                return
            self.state.event("server_job", "共享服务：筛选完成，本体提取进入夜间批处理", run_id=row["run_id"],
                             details={"materialId": payload.get("materialId", ""),
                                      "blockIds": result.get("selection", {}).get("blockIds", []),
                                      "linkIds": result.get("selection", {}).get("linkIds", []),
                                      "ontologyJobs": len(result.get("ontologyJobs", [])),
                                      "modelCalls": result.get("modelCalls", 0),
                                      "errors": result.get("errors", [])})
            run = self.state.one("SELECT * FROM runs WHERE id=?", (row["run_id"],))
            if not run or run["state"] in {"cancelled", "failed", "succeeded"}:
                return
            if not row["links_applied"]:
                self.acquisition.apply_links(row, result, payload)
            self.acquisition.refresh(row["run_id"])
        except asyncio.CancelledError:
            if self.acquisition.active(row["run_id"]):
                self.state.execute("UPDATE outbox SET state='pending' WHERE id=? AND state='sending'", (row["id"],))
            raise
        except ServerError as error:
            if not self.acquisition.active(row["run_id"]):
                return
            state = "blocked" if error.status in {401, 403} else "failed" if error.status in {400, 409, 422} else "pending"
            self.state.execute("UPDATE outbox SET state=?,error=?,attempts=attempts+1,next_at=? WHERE id=?",
                               (state, str(error), time.time() + error.retry_after, row["id"]))
            if state == "failed" and row["cache_key"]:
                self.state.execute("DELETE FROM submission_cache WHERE id=?", (row["cache_key"],))
            if row["error"] != str(error):
                self.state.event("server_error", str(error), run_id=row["run_id"], level="error")
            if state == "failed":
                self.acquisition.cancel(row["run_id"], str(error), terminal="failed")
        except Exception:
            if not self.acquisition.active(row["run_id"]):
                return
            self.state.execute("UPDATE outbox SET state='pending',next_at=?,error='共享服务返回了无效数据' WHERE id=?", (time.time()+15, row["id"]))
