"""Run one local extraction server process alongside the acquisition scheduler."""
from contextlib import AsyncExitStack, asynccontextmanager
import hmac
import logging
import asyncio
import os

from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import JSONResponse

from llm_client import LLMClient
from .constant import Settings
from .evidence import EvidenceQuery, evidence_page
from .read_model import cached_snapshot
from .models import BatchRequest, ExtractRequest, ExtractResponse, PROTOCOL_VERSION
from .ontology_batch import BatchBusy
from .worker import ExtractionService

logger = logging.getLogger(__name__)


def create_app(service=None):
    @asynccontextmanager
    async def lifespan(app):
        if service is not None:
            app.state.service = service
            yield
            return
        from core.async_database import AsyncDatabaseManager
        from core.wis.async_cache import SqliteCache, MAIN_CACHE_FILE
        settings = Settings.from_env()
        db = AsyncDatabaseManager(logger=logging.getLogger('wiseflow-server'))
        cache = SqliteCache(MAIN_CACHE_FILE)
        client = LLMClient(settings.base_url, settings.api_key, concurrency=settings.concurrency, timeout=settings.timeout)
        async with AsyncExitStack() as resources:
            resources.push_async_callback(client.close)
            resources.push_async_callback(db.cleanup)
            resources.push_async_callback(cache.close)
            await db.initialize()
            await cache.open()
            active = ExtractionService(settings, client.chat, db, cache)
            resources.push_async_callback(active.close)
            app.state.service = active
            active.batch.schedule()
            yield

    app = FastAPI(title='Linxiaozhu material extraction', lifespan=lifespan)
    from .ontology_api import router_for
    app.include_router(router_for(app))
    if service is not None:
        app.state.service = service

    # Optional shared-token bearer auth. Empty OSBI_SERVER_TOKEN (the default)
    # keeps local integration unauthenticated; a non-empty value protects every
    # /v1/* route while /health stays open. Read once at app creation, so a
    # token change requires a restart. Per-customer keys/grants come later.
    token = os.environ.get('OSBI_SERVER_TOKEN', '').strip()
    if token:
        logger.info('Bearer auth enabled for /v1/* routes')

        @app.middleware('http')
        async def bearer_auth(request, call_next):
            if request.url.path.startswith('/v1/'):
                supplied = request.headers.get('Authorization', '')
                if not hmac.compare_digest(supplied.encode('utf-8'),
                                           f'Bearer {token}'.encode('utf-8')):
                    return JSONResponse(status_code=401,
                                        content={'detail': 'Invalid or missing bearer token'})
            return await call_next(request)

    @app.get('/health')
    async def health():
        active = app.state.service
        return {'status': 'ok', 'protocolVersion': PROTOCOL_VERSION,
                'selected_model': active.settings.selected_model,
                'performance_model': active.settings.performance_model,
                'ontology_model': active.settings.ontology_model,
                'fallback_model': active.settings.fallback_model,
                'databasePath': str(active.db.db_path),
                'ontology': {'status':'batch',
                             'scheduleTime':active.settings.ontology_batch_time,
                             'timezone':active.settings.ontology_batch_timezone,
                             'graphPath':str(active.ontology_engine.store.path) if active.ontology_engine else None,
                             'schemaId':active.ontology_engine.schema.fingerprint if active.ontology_engine else None}}

    @app.get('/v1/ontology/blocks/{extraction_id}')
    async def read_ontology(extraction_id: str = Path(pattern=r'^o_[a-f0-9]{64}$'), libraryId: str | None = None):
        try:
            engine = app.state.service.engine(libraryId)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        result = await asyncio.to_thread(engine.store.read, extraction_id) if engine else None
        if result is None:
            raise HTTPException(status_code=404, detail='Ontology block not found')
        return result

    @app.post('/v1/libraries/{library_id}/evidence')
    async def evidence(library_id: str, request: EvidenceQuery):
        """Original-block evidence pages for topnews/weekly selection."""
        active = app.state.service
        try:
            key = active.libraries.resolve(library_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        engine = active.engine(key)
        if engine is None:
            raise HTTPException(503, 'Ontology engine is unavailable')
        receipts = await active.queue.done_receipts(key)

        def load():
            # Sole graph owner handle; reads stay off the event loop.
            entries = []
            for job in receipts:
                result = engine.store.read(job['receipt']['id'])
                if result is not None:
                    entries.append((job['id'], result))
            # Read-model normalization (seed skeleton + alias/label/hub fixes)
            # happens here on the query side; the store is never written back.
            return entries, cached_snapshot(key, entries)

        entries, snapshot = await asyncio.to_thread(load)
        try:
            return evidence_page(entries, request, library_id=key, snapshot=snapshot)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error

    @app.post('/v1/materials/extract', response_model=ExtractResponse)
    async def extract(request: ExtractRequest):
        try:
            app.state.service.libraries.resolve(request.ontologyLibrary, focus_id=request.focus.id)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return await app.state.service.extract(request)

    @app.get('/v1/ontology/batches/status')
    async def batch_status(libraryId: str | None = None):
        if libraryId is not None:
            try:
                app.state.service.libraries.resolve(libraryId)
            except ValueError as error:
                raise HTTPException(404, str(error)) from error
        return await app.state.service.batch.status(libraryId)

    @app.post('/v1/ontology/batches', status_code=202)
    async def start_batch(request: BatchRequest):
        try:
            if request.libraryId is not None:
                app.state.service.libraries.resolve(request.libraryId)
            app.state.service.batch.start(
                request.maxBlocks, reprocess=request.reprocess, library_id=request.libraryId,
                created_from=request.createdFrom, created_to=request.createdTo)
        except BatchBusy as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {'started': True}

    return app


app = create_app()
