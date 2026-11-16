"""Authenticated shared server API with durable asynchronous extraction."""
import asyncio
from collections import defaultdict, deque
from contextlib import asynccontextmanager
import time
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from osbi_contracts.models import Material, GraphQuery, EvidenceQuery, PROTOCOL_VERSION
from .config import Settings
from .service import Service, permitted
from .repository import Conflict
from .query import graph, traverse


def create_app(settings=None, service=None):
    @asynccontextmanager
    async def lifespan(app):
        active = service or Service(settings or Settings.from_env())
        app.state.service = active
        await active.start()
        try:
            yield
        finally:
            await active.close()

    app = FastAPI(title="OSBI Shared Server", version="0.1.0", lifespan=lifespan)
    limits = defaultdict(deque)

    @app.middleware("http")
    async def authentication(request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        active = app.state.service
        if active.settings.require_https and request.url.scheme != "https":
            return JSONResponse({"error": "https_required"}, status_code=400)
        try:
            content_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            return JSONResponse({"error": "invalid_content_length"}, status_code=400)
        if content_length > 2 * 1024 * 1024:
            return JSONResponse({"error": "payload_too_large"}, status_code=413)
        token = request.headers.get("authorization", "")
        principal = active.repo.authenticate(token[7:]) if token.startswith("Bearer ") else None
        if not principal:
            return JSONResponse({"error": "invalid_expired_or_revoked_key"}, status_code=401)
        window = limits[principal["id"]]
        now = time.monotonic()
        while window and window[0] < now - 60:
            window.popleft()
        if len(window) >= principal["rpm"]:
            return JSONResponse({"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"})
        window.append(now)
        request.state.principal = principal
        if request.method in {"POST", "PUT", "PATCH"}:
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 2 * 1024 * 1024:
                    return JSONResponse({"error": "payload_too_large"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        return await call_next(request)

    def authorized_channel(request, channel_id):
        active = app.state.service
        if not permitted(request.state.principal, channel_id) or not active.repo.channel(channel_id):
            raise HTTPException(404, "Channel not found")
        return active

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/capabilities")
    async def capabilities():
        return {"protocolVersion": PROTOCOL_VERSION, "maxBlocks": 64, "maxLinks": 200,
                "automaticChannels": True, "automaticSchemaExpansion": False}

    @app.get("/v1/channels")
    async def channels(request: Request):
        return {"items": app.state.service.channels(request.state.principal)}

    @app.get("/v1/status")
    async def status():
        return app.state.service.status()

    @app.get("/v1/identity")
    async def identity(request: Request):
        return {"customerId": request.state.principal["customer"]}

    @app.post("/v1/materials")
    async def submit(body: Material, request: Request):
        try:
            result = app.state.service.submit(body, request.state.principal)
        except Conflict as error:
            raise HTTPException(409, str(error)) from error
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except OverflowError as error:
            raise HTTPException(429, str(error), headers={"Retry-After": "60"}) from error
        return JSONResponse(result, status_code=200 if result["state"] == "succeeded" else 202)

    @app.get("/v1/jobs/{job_id}")
    async def job(job_id: str, request: Request):
        try:
            result = app.state.service.job(job_id, request.state.principal)
        except PermissionError as error:
            raise HTTPException(404, "Job not found") from error
        if result is None:
            raise HTTPException(404, "Job not found")
        return result

    @app.get("/v1/channels/{channel_id}/graph")
    async def graph_view(channel_id: str, request: Request, limit: int = Query(500, ge=1, le=2000)):
        active = authorized_channel(request, channel_id)
        return await asyncio.to_thread(graph, active, channel_id, limit)

    @app.get("/v1/channels/{channel_id}/entities")
    async def entities(channel_id: str, request: Request, q: str = Query("", max_length=500), limit: int = Query(100, ge=1, le=500)):
        data = await asyncio.to_thread(graph, authorized_channel(request, channel_id), channel_id, 2000)
        items = [n for n in data["nodes"] if q.casefold() in n["name"].casefold()]
        return {"items": items[:limit], "total": len(items), "truncated": data["truncated"]}

    @app.post("/v1/channels/{channel_id}/query")
    async def query(channel_id: str, body: GraphQuery, request: Request):
        data = await asyncio.to_thread(graph, authorized_channel(request, channel_id), channel_id, 2000)
        result = traverse(data, body.source, body.target, body.depth, body.limit)
        ids = {b for n in result["nodes"] for b in n["blocks"]}
        result["blocks"] = {i: data["blocks"][i] for i in ids}
        return result

    @app.get("/v1/channels/{channel_id}/blocks/{block_id}")
    async def block(channel_id: str, block_id: str, request: Request):
        data = await asyncio.to_thread(graph, authorized_channel(request, channel_id), channel_id, 2000)
        if block_id not in data["blocks"]:
            raise HTTPException(404, "Block not found")
        return data["blocks"][block_id]

    @app.post("/v1/channels/{channel_id}/evidence")
    async def evidence(channel_id: str, body: EvidenceQuery, request: Request):
        from .query import evidence_page
        return await asyncio.to_thread(evidence_page, authorized_channel(request, channel_id), channel_id, body)

    return app
