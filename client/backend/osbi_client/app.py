"""Loopback dashboard, local control API and durable event stream."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from .daemon import Daemon
from .server_client import request as server_request, ServerError
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
        import re
        allowed = path in {"channels", "status", "capabilities", "identity"} or bool(re.fullmatch(r"channels/[a-z][a-z0-9-]{0,63}/(?:graph|entities|query|evidence|blocks/[a-zA-Z0-9_-]+)", path))
        if not allowed or (request.method == "POST" and not path.endswith(("/query", "/evidence"))):
            raise HTTPException(404, "不支持的服务接口")
        suffix = "?" + request.url.query if request.url.query else ""
        payload = await request.json() if request.method == "POST" else None
        daemon = app.state.daemon
        result = await server_request(daemon.state.config(), request.method, "/v1/" + path + suffix, payload,
                                      transport=daemon.transport)
        if request.method == "POST":
            from .decisions import filter_evidence, filter_graph_result
            if path.endswith("/evidence"):
                before = len(result["items"])
                result["items"] = await filter_evidence(daemon, result["items"], payload.get("text") or daemon.state.config().focus)
                if daemon.state.config().jev.enabled and daemon.state.config().jev.postQuery:
                    result["jev"] = {"filtered": True, "discardedRecords": before - len(result["items"])}
            elif path.endswith("/query"):
                intent = " ".join([daemon.state.config().focus, *[n["name"] for n in result.get("nodes", [])
                                   if n["id"] in {payload.get("source"), payload.get("target")} ]]) or payload["source"]
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

    return app
