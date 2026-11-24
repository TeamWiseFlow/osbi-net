"""Loopback dashboard, local control API and durable event stream."""
import asyncio
from contextlib import asynccontextmanager
from datetime import date
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from .daemon import Daemon
from .server_client import ServerError
from .weekly_models import WeeklySelect, WeeklyRecord, WeeklyBackfill, TopNewsSelect


def create_app(data_dir=None, daemon=None):
    @asynccontextmanager
    async def lifespan(app):
        active = daemon or Daemon(Path(data_dir or os.environ.get("OSBI_CLIENT_DATA", "~/.osbi/client")).expanduser())
        app.state.daemon = active
        try:
            await active.start()
            yield
        finally:
            await active.close()

    app = FastAPI(title="OSBI Local Client", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    static = Path(__file__).with_name("static")

    @app.middleware("http")
    async def local_auth(request, call_next):
        host = request.url.hostname
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"error": "Local host required"}, status_code=403)
        if request.url.path.startswith("/api/"):
            token = app.state.daemon.state.one("SELECT local_token FROM identity")["local_token"]
            bearer = request.headers.get("authorization", "")
            supplied = bearer[7:] if bearer.startswith("Bearer ") else request.cookies.get("osbi_local", "")
            if not hmac.compare_digest(token, supplied):
                return JSONResponse({"error": "Local authentication required"}, status_code=401)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"error": "Invalid local origin"}, status_code=403)
            if request.method not in {"GET", "HEAD"} and request.headers.get("x-osbi-local") != "1":
                return JSONResponse({"error": "Local write header required"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ServerError)
    async def server_error(request, error):
        return JSONResponse({"error": str(error)}, status_code=error.status if 400 <= error.status < 600 else 502)

    @app.exception_handler(ValueError)
    async def local_error(request, error):
        # Validation errors can include supplied secrets; never serialize their input.
        message = "设置或参数无效，请检查字段格式" if isinstance(error, ValidationError) else str(error)
        return JSONResponse({"error": message}, status_code=400)

    @app.get("/")
    async def home():
        return RedirectResponse("/graph")

    @app.get("/graph")
    @app.get("/settings")
    @app.get("/notifications")
    @app.get("/reports")
    @app.get("/tasks")
    @app.get("/query")
    async def shell():
        response = FileResponse(static / "index.html")
        token = app.state.daemon.state.one("SELECT local_token FROM identity")["local_token"]
        response.set_cookie("osbi_local", token, httponly=True, samesite="strict", max_age=86400)
        return response

    app.mount("/static", StaticFiles(directory=static), name="static")

    @app.get("/api/status")
    async def status():
        active = app.state.daemon
        return {"status": "running", "clientId": active.state.one("SELECT client_id FROM identity")["client_id"],
                "outbox": active.state.rows("SELECT state,COUNT(*) count FROM outbox GROUP BY state")}

    @app.get("/api/config")
    async def config():
        return app.state.daemon.state.public_config()

    @app.patch("/api/config")
    async def save(request: Request):
        value = await request.json()
        if not isinstance(value, dict):
            raise ValueError("设置必须是 JSON 对象")
        return app.state.daemon.state.save_config(value)

    @app.get("/api/runs")
    async def runs():
        active = app.state.daemon
        return {"items": [active.state.run(r["id"]) for r in active.state.rows("SELECT id FROM runs ORDER BY created DESC LIMIT 100")]}

    @app.post("/api/runs")
    async def start(request: Request):
        value = await request.json()
        if not isinstance(value, dict) or type(value.get("forceRefresh", False)) is not bool:
            raise ValueError("任务参数必须为 JSON 对象，forceRefresh 必须为布尔值")
        return app.state.daemon.start_run(value.get("sourceId", ""), force_refresh=value.get("forceRefresh", False))

    @app.get("/api/runs/{run_id}")
    async def read_run(run_id: str):
        result = app.state.daemon.state.run(run_id)
        if not result:
            raise HTTPException(404, "任务不存在")
        return result

    @app.post("/api/runs/{run_id}/{action}")
    async def run_action(run_id: str, action: str, request: Request):
        body = await request.json()
        if action in {"resume", "cancel"}:
            return app.state.daemon.act(run_id, action, body.get("promptId"))
        if action == "browser":
            row = app.state.daemon.state.one("SELECT * FROM runs WHERE id=?", (run_id,))
            if not row:
                raise HTTPException(404, "任务不存在")
            allowed = {"snapshot", "click", "fill", "select", "scroll", "wait", "switch", "tabs", "open", "eval", "url"}
            if body.get("action") not in allowed or row["state"] not in {"waiting_for_user", "interrupted"}:
                raise ValueError("此任务或浏览器操作当前不可用")
            source = json.loads(row["source"])
            lease = json.loads(row["browser"]).get("lease")
            return await app.state.daemon.browser.command(source["profile"], body["action"], body.get("params", {}), lease)
        raise HTTPException(404, "操作不存在")

    @app.get("/api/events")
    async def events(after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
        return {"items": app.state.daemon.state.events(after, limit)}

    @app.get("/api/events/stream")
    async def stream(request: Request, after: int = Query(0, ge=0)):
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(400, "Invalid cursor")
        async def body():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = app.state.daemon.state.events(cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if not batch:
                    yield ": keepalive\n\n"
                    await asyncio.sleep(1)
        return StreamingResponse(body(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.api_route("/api/server/{path:path}", methods=["GET", "POST"])
    async def remote(path: str, request: Request):
        from .server_proxy import CHANNEL_PATH, fetch
        allowed = path in {"channels", "status"} or bool(CHANNEL_PATH.fullmatch(path))
        if not allowed or (request.method == "POST" and not path.endswith(("/query", "/evidence"))):
            raise HTTPException(404, "不支持的服务接口")
        payload = await request.json() if request.method == "POST" else None
        daemon = app.state.daemon
        result = await fetch(daemon, request.method, path, payload, request.url.query)
        if request.method == "POST":
            from .decisions import filter_evidence, filter_graph_result
            if path.endswith("/evidence"):
                before = len(result["items"])
                result["items"] = await filter_evidence(daemon, result["items"], payload.get("text") or "")
                if daemon.state.config().jev.enabled and daemon.state.config().jev.postQuery:
                    result["jev"] = {"filtered": True, "discardedRecords": before - len(result["items"])}
            elif path.endswith("/query"):
                intent = " ".join([n["name"] for n in result.get("nodes", [])
                                   if n["id"] in {payload.get("source"), payload.get("target")}]) or payload["source"]
                result = await filter_graph_result(daemon, result, intent)
        return result

    @app.post("/api/topnews/{channel}/select")
    async def topnews_select(channel: str, body: TopNewsSelect):
        from .weekly import select_topnews
        return await select_topnews(app.state.daemon, channel, body)

    @app.post("/api/weekly/{channel}/select")
    async def weekly_select(channel: str, body: WeeklySelect):
        from .weekly import select_weekly
        return await select_weekly(app.state.daemon, channel, body)

    @app.post("/api/weekly/{channel}/record")
    async def weekly_record(channel: str, body: WeeklyRecord):
        from .weekly import record_weekly
        return record_weekly(app.state.daemon, channel, body.planId)

    @app.post("/api/weekly/{channel}/backfill")
    async def weekly_backfill(channel: str, body: WeeklyBackfill):
        from .weekly import backfill_weekly
        return await backfill_weekly(app.state.daemon, channel, body)

    # -- shadow report generation (ported production selection + local model) --

    def _date_param(value):
        if value in (None, ""):
            return ""
        if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式")
        return value

    def _bool_param(value, default):
        if value is None:
            return default
        if type(value) is not bool:
            raise ValueError("开关参数必须为布尔值")
        return value

    def _start_report_job(kind, runner, params):
        jobs = getattr(app.state, "report_jobs", None)
        if jobs is None:
            jobs = app.state.report_jobs = {}
        if any(j["kind"] == kind and j["state"] == "running" for j in jobs.values()):
            raise ValueError("同类报告正在生成中，请稍候再试")
        job_id = "rj_" + secrets.token_hex(8)
        job = dict(id=job_id, kind=kind, state="running", params=params,
                   result=None, error="", created=time.time())
        jobs[job_id] = job
        for old in sorted(jobs.values(), key=lambda j: j["created"])[:-20]:
            if old["state"] != "running":
                jobs.pop(old["id"], None)

        async def body():
            try:
                job["result"] = await runner(app.state.daemon, **params)
                job["state"] = "succeeded"
            except Exception as error:  # noqa: BLE001 - surface failure to the dashboard
                logging.getLogger(__name__).exception("report job failed: %s", kind)
                job["state"] = "failed"
                job["error"] = str(error) or type(error).__name__

        task = asyncio.create_task(body())
        app.state.daemon.tasks.append(task)
        return dict(jobId=job_id, kind=kind, state="running")

    @app.post("/api/topnews/run")
    async def topnews_run(request: Request):
        from .shadow_reports import run_topnews
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("任务参数必须为 JSON 对象")
        top_n = body.get("topN")
        if top_n is not None and type(top_n) is not int:
            raise ValueError("topN 必须为整数")
        params = dict(from_date=_date_param(body.get("fromDate")),
                      to_date=_date_param(body.get("toDate")),
                      top_n=top_n,
                      profile=body.get("profile") or "default",
                      record_penalty=_bool_param(body.get("recordPenalty"), True),
                      use_llm=_bool_param(body.get("useLlm"), True),
                      did_you_know=_bool_param(body.get("didYouKnow"), True),
                      send=_bool_param(body.get("send"), None),
                      window=body.get("window"))
        return _start_report_job("topnews", run_topnews, params)

    @app.post("/api/weekly/run")
    async def weekly_run(request: Request):
        from .shadow_reports import run_weekly
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("任务参数必须为 JSON 对象")
        params = dict(from_date=_date_param(body.get("fromDate")),
                      to_date=_date_param(body.get("toDate")),
                      profile=body.get("profile") or "default",
                      use_llm=_bool_param(body.get("useLlm"), True),
                      send=_bool_param(body.get("send"), None),
                      window=body.get("window"),
                      send_file=_bool_param(body.get("sendFile"), False),
                      file_channel_id=body.get("fileChannelId"))
        return _start_report_job("weekly", run_weekly, params)

    @app.get("/api/reports/jobs/{job_id}")
    async def report_job(job_id: str):
        job = getattr(app.state, "report_jobs", {}).get(job_id)
        if not job:
            raise HTTPException(404, "报告任务不存在")
        return job

    @app.get("/api/topnews/latest")
    async def topnews_latest():
        from .shadow_reports import latest_report
        return latest_report(app.state.daemon, "topnews") or dict(empty=True)

    @app.get("/api/weekly/latest")
    async def weekly_latest():
        from .shadow_reports import latest_report
        return latest_report(app.state.daemon, "weekly") or dict(empty=True)

    @app.get("/api/reports")
    async def reports_list():
        from .shadow_reports import list_reports
        return {"items": list_reports(app.state.daemon)}

    @app.get("/api/reports/file")
    async def reports_file(kind: str = Query(...), name: str = Query(...)):
        from .shadow_reports import read_report
        return read_report(app.state.daemon, kind, name)

    @app.get("/api/reports/download")
    async def reports_download(name: str = Query(...)):
        from .shadow_reports import download_report
        path = download_report(app.state.daemon, name)
        media = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                 if path.suffix == ".docx" else "text/plain; charset=utf-8")
        return FileResponse(path, media_type=media, filename=path.name)

    return app
