"""OSBI extraction server: channel relevance, ontology queue and evidence."""
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
import logging
import asyncio
import os

from fastapi import FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse

from llm_client import LLMClient
from .constant import Settings
from .evidence import EvidenceQuery, evidence_page
from .read_model import cached_snapshot
from .models import BatchRequest, ExtractRequest, ExtractResponse, PROTOCOL_VERSION
from .ontology_batch import BatchBusy
from .worker import ExtractionService

logger = logging.getLogger(__name__)


def _node_registry(service):
    config = getattr(getattr(service, 'settings', None), 'config', None)
    return getattr(config, 'auth', None)


def _authorize(service, token: str):
    """Return (allowed, node_id) for a bearer token.

    Node keys map to a node identity and are enforced when configured. The
    legacy shared OSBI_SERVER_TOKEN stays available for local integration but
    carries no node identity. With neither configured, requests are open.
    """
    registry = _node_registry(service)
    if registry is not None and registry.enabled:
        node_id = registry.authenticate(token)
        return bool(node_id), node_id
    shared = os.environ.get('OSBI_SERVER_TOKEN', '').strip()
    if shared:
        supplied = token or ''
        return hmac.compare_digest(supplied.encode('utf-8'), shared.encode('utf-8')), None
    return True, None


def create_app(service=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.service = service
            yield
            return
        from core.async_database import AsyncDatabaseManager
        from core.wis.async_cache import SqliteCache
        settings = Settings.from_env()
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        db = AsyncDatabaseManager(logger=logging.getLogger('wiseflow-server'), db_path=settings.data_dir / 'data.db')
        cache = SqliteCache(settings.data_dir / 'main_cache.sqlite')
        client = LLMClient(settings.base_url, settings.api_key, concurrency=settings.concurrency, timeout=settings.timeout)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(client.close)
            resources.push_async_callback(db.cleanup)
            resources.push_async_callback(cache.close)
            await db.initialize()
            await cache.open()
            active = ExtractionService(settings, client.chat, db, cache)
            await active.metrics.ensure_schema()
            resources.push_async_callback(active.close)
            app.state.service = active
            active.batch.schedule()
            yield

    app = FastAPI(title='OSBI material extraction', lifespan=lifespan)
    from .ontology_api import router_for
    app.include_router(router_for(app))
    if service is not None:
        app.state.service = service

    @app.middleware('http')
    async def node_auth(request: Request, call_next):
        # /health stays open; every /v1/* route requires a node key when auth is
        # configured. Authentication runs before any cached read.
        if request.url.path.startswith('/v1/'):
            supplied = request.headers.get('Authorization', '')
            token = supplied[7:].strip() if supplied.startswith('Bearer ') else ''
            active = app.state.service
            allowed, node_id = _authorize(active, token)
            if not allowed:
                return JSONResponse(status_code=401,
                                    content={'detail': 'Invalid or missing node key'})
            request.state.node_id = node_id
        else:
            request.state.node_id = None
        return await call_next(request)

    def service_for() -> ExtractionService:
        return app.state.service

    @app.get('/health')
    async def health():
        active = service_for()
        config = getattr(active.settings, 'config', None)
        return {'status': 'ok', 'protocolVersion': PROTOCOL_VERSION,
                'configFingerprint': getattr(active.settings, 'config_fingerprint', None),
                'authMode': getattr(getattr(config, 'auth', None), 'mode', 'none'),
                'channels': sorted(getattr(config, 'channels', {}) or {}),
                'nodes': sorted(getattr(config, 'nodes', {}) or {}),
                'selected_model': active.settings.selected_model,
                'performance_model': active.settings.performance_model,
                'ontology_model': active.settings.ontology_model,
                'fallback_model': active.settings.fallback_model,
                'databasePath': str(active.db.db_path),
                'ontology': {'status': 'batch',
                             'scheduleTime': active.settings.ontology_batch_time,
                             'timezone': active.settings.ontology_batch_timezone,
                             'graphPath': str(active.ontology_engine.store.path) if active.ontology_engine else None,
                             'schemaId': active.ontology_engine.schema.fingerprint if active.ontology_engine else None}}

    @app.get('/v1/channels')
    async def channels():
        config = service_for().settings.config
        return {'items': [dict(id=channel.id, title=channel.title,
                               schemaRef=channel.schema_ref,
                               dateWindowDays=channel.date_window_days,
                               dateField=channel.date_field,
                               statement=channel.statement)
                          for channel in config.channels.values()]}

    @app.get('/v1/ontology/blocks/{extraction_id}')
    async def read_ontology(extraction_id: str = Path(pattern=r'^o_[a-f0-9]{64}$'),
                            libraryId: str | None = None):
        try:
            engine = service_for().engine(libraryId)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        result = await asyncio.to_thread(engine.store.read, extraction_id) if engine else None
        if result is None:
            raise HTTPException(status_code=404, detail='Ontology block not found')
        return result

    async def _evidence(store_id, request, *, channel=None, node_id=None):
        active = service_for()
        engine = active.engine(store_id)
        if engine is None:
            raise HTTPException(503, 'Ontology engine is unavailable')
        receipts = await active.queue.done_receipts(store_id)
        seeds = getattr(active.settings, 'seed_paths', None)

        def load():
            entries = []
            for job in receipts:
                result = engine.store.read(job['receipt']['id'])
                if result is None:
                    continue
                provenance = result.get('provenance') or {}
                if channel is not None and provenance.get('channel') != channel.id:
                    continue
                entries.append((job['id'], result))
            return entries, cached_snapshot(store_id, entries, seed_paths=seeds)

        entries, snapshot = await asyncio.to_thread(load)
        try:
            return evidence_page(entries, request, library_id=store_id, snapshot=snapshot,
                                 channel=channel, node_id=node_id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post('/v1/channels/{channel}/evidence')
    async def channel_evidence(channel: str, query: EvidenceQuery, request: Request):
        """Evidence for one global channel (sourceScope/territory aware)."""
        active = service_for()
        try:
            configured = active.channel(channel)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return await _evidence(configured.schema_ref, query, channel=configured,
                               node_id=getattr(request.state, 'node_id', None))

    @app.post('/v1/libraries/{library_id}/evidence')
    async def library_evidence(library_id: str, query: EvidenceQuery, request: Request):
        """Legacy store-scoped evidence route kept for the transitional client."""
        active = service_for()
        try:
            resolver = getattr(active, 'stores', None) or active.libraries
            key = resolver.resolve(library_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        return await _evidence(key, query, node_id=getattr(request.state, 'node_id', None))

    @app.post('/v1/materials/extract', response_model=ExtractResponse)
    async def extract(payload: ExtractRequest, request: Request):
        active = service_for()
        try:
            active.channel(payload.channel)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        authenticated = getattr(request.state, 'node_id', None)
        if authenticated and payload.nodeId and authenticated != payload.nodeId:
            raise HTTPException(403, 'nodeId does not match the authenticated node')
        if authenticated and not payload.nodeId:
            payload = payload.model_copy(update=dict(nodeId=authenticated))
        return await active.extract(payload)

    @app.get('/v1/metrics/sources')
    async def source_metrics(nodeId: str | None = None, sourceKey: str | None = None,
                             limit: int = Query(500, ge=1, le=2000)):
        active = service_for()
        return {'items': await active.metrics.query(nodeId, sourceKey, limit)}

    @app.get('/v1/ontology/batches/status')
    async def batch_status(libraryId: str | None = None):
        if libraryId is not None:
            try:
                service_for().stores.resolve(libraryId)
            except ValueError as error:
                raise HTTPException(404, str(error)) from error
        return await service_for().batch.status(libraryId)

    @app.post('/v1/ontology/batches', status_code=202)
    async def start_batch(request: BatchRequest):
        active = service_for()
        try:
            if request.libraryId is not None:
                active.stores.resolve(request.libraryId)
            active.batch.start(
                request.maxBlocks, reprocess=request.reprocess, library_id=request.libraryId,
                created_from=request.createdFrom, created_to=request.createdTo)
        except BatchBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {'started': True}

    return app


app = create_app()
