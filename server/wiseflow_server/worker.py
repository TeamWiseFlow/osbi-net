"""Relevance -> durable original-block backlog (no journal, no graph LLM)."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import logging
import time
import uuid

from core.wis.page_material import relevance_batches
from llm_client import observe_usage
from .llmuse import relevance_prompt_fingerprint, select_relevance
from .link_publication_date import link_date_rejection
from .link_selection import refine_link_selection
from .models import Block, ExtractResponse, Selection, StageError
from .metrics import NodeSourceMetrics
from .ontology import OntologyExtractor
from .ontology_queue import OntologyQueue
from .ontology_batch import OntologyBatch
from .publication_date import TIMEZONE, check_window, extract_publication_date, window_bounds
from .runtime_config import StoreRegistry

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _date_gate_reason(reason):
    # Keep the response categories stable; vague relative block dates remain
    # rejected as unknown, while their richer parser/quality flags are intact.
    return reason if reason in {'date_window', 'date_unknown', 'date_ambiguous', 'date_invalid'} else 'date_unknown'


def _log_link_selection(request, counters):
    logger.info('Link selection channel=%s node=%s material=%s %s', request.channel,
                request.nodeId, request.materialId[:16], json.dumps(counters, sort_keys=True),
                extra=counters)


class ExtractionService:
    def __init__(self, settings, chat, db, cache=None, *, ontology=None):
        self.settings, self.chat, self.db, self.cache = settings, chat, db, cache
        self.config = settings.config
        self.stores = StoreRegistry(self.config.stores, self.config.default_store)
        self.stores.attach_channels(self.config.channels)
        # Reference for legacy ontology API/viewer code; routing is by channel.
        self.libraries = self.stores
        self.ontology_engines = {}
        if ontology is None:
            try:
                for store_id, store in self.config.stores.items():
                    schema_path = settings.schema_path if store_id == self.config.default_store and settings.schema_path else store.schema_path
                    self.ontology_engines[store_id] = OntologyExtractor(
                        replace(settings, schema_path=schema_path), store.graph_path)
            except Exception:
                for engine in self.ontology_engines.values():
                    engine.store.close()
                raise
        self.ontology_engine = self.ontology_engines.get(self.config.default_store)
        self._custom_ontology = ontology
        self.ontology = self.extract_ontology
        self.queue = OntologyQueue(db)
        self.metrics = NodeSourceMetrics(db)
        self.batch = OntologyBatch(settings, self.queue, self.ontology, chat,
                                   refresh=self.refresh_viewer if self.ontology_engine else None,
                                   reprocess_ids=self.reprocess_ids if self.ontology_engine else None,
                                   supersede=self.supersede if self.ontology_engine else None)
        self._locks = {}
        self.monitor = None

    def channel(self, channel_id):
        return self.config.channel(channel_id)

    def store_id_for_channel(self, channel_id):
        return self.config.channel(channel_id).schema_ref

    def store_config(self, store_id=None):
        return self.config.store(store_id)

    async def refresh_viewer(self):
        from .ontology_viewer import refresh_viewer
        for store_id, engine in self.ontology_engines.items():
            out = self.db.db_path.parent / 'viewer' / store_id
            await asyncio.to_thread(refresh_viewer, engine.store.path, out,
                                    store=engine.store.store,
                                    title=self.config.stores[store_id].title)

    def engine(self, store_id=None):
        key = self.stores.resolve(store_id)
        return self.ontology_engines.get(key)

    async def extract_ontology(self, *, library_id=None, **kwargs):
        key = self.stores.resolve(library_id)
        if self._custom_ontology is not None:
            result = await self._custom_ontology(**kwargs)
        else:
            result = await self.ontology_engines[key].extract(**kwargs)
        node_ids = [str(value) for value in (kwargs.get('node_ids') or []) if value]
        if node_ids and isinstance(result, dict) and not result.get('status') == 'failed':
            entities = int(result.get('entityCount') or 0)
            relations = int(result.get('relationCount') or 0)
            for node_id in node_ids:
                await self._record(self.metrics.record_extraction, node_id,
                                   kwargs.get('meta') or {}, blocks=1, entities=entities,
                                   relations=relations,
                                   zero_relation_blocks=1 if entities and not relations else 0)
        return result

    async def reprocess_ids(self, limit=None, *, library_id=None, created_from=None, created_to=None):
        ids = []
        for job_id in await self.queue.pending_ids(include_done=True, library_id=library_id,
                                                    created_from=created_from, created_to=created_to):
            job = await self.queue.get(job_id)
            payload = job['payload']
            channel = self.config.channel(payload['channel'])
            engine = self.engine(job['library_id'])
            extraction_id, _, _ = engine.identity(
                channel=channel, title=payload['title'],
                block=Block.model_validate(payload['block']), meta=payload['meta'])
            if job['status'] == 'pending' or (job.get('receipt') or {}).get('id') != extraction_id:
                ids.append(job_id)
                if limit is not None and len(ids) >= limit:
                    break
        return ids

    async def supersede(self, old_id, new_id, *, library_id=None):
        await asyncio.to_thread(self.engine(library_id).store.supersede, old_id, new_id)

    async def close(self):
        await self.batch.close()
        if self.monitor is not None:
            await self.monitor.close()
        for engine in self.ontology_engines.values():
            await engine.close()

    @asynccontextmanager
    async def _lock(self, key):
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)

    async def _selection(self, request, channel, window, chat, *, now=None, publication=None,
                         cache_trace=None):
        payload = request.input.model_dump(exclude_none=True)
        if request.mode == 'only_link':
            payload['blocks'] = []
        # Roles reach the prompt and response policy, but do not create a new
        # content/cache identity. Cache the uncapped selection for each caller.
        identity_payload = {**payload, 'links': [
            {key: value for key, value in link.items() if key != 'role'}
            for link in payload['links']]}
        identity = ['relevance/v4', channel.id, channel.statement_fingerprint,
                    channel.rules_fingerprint, window, request.mode, identity_payload,
                    self.settings.selected_model, self.settings.fallback_model,
                    relevance_prompt_fingerprint(window, request.mode == 'only_link')]
        block_publication = None
        if window:
            if publication is not None:
                reason = check_window(publication, channel.date_window_days, now=now)
                block_publication = dict(date=publication.date, source=publication.source,
                                         reason=_date_gate_reason(reason) if reason else '')
            identity.append(block_publication)
        key = digest(identity)
        async with self._lock(key):
            cached = await self.cache.get(key, namespace='relevance/v4') if self.cache else None
            if cached is not None:
                selection = Selection.model_validate(cached)
            else:
                selection = Selection(blockIds=[], linkIds=[])
                for batch in relevance_batches(payload):
                    part = await select_relevance(chat, channel, window, batch,
                                                  self.settings.selected_model,
                                                  self.settings.fallback_model,
                                                  catalog=request.mode == 'only_link',
                                                  block_publication=block_publication)
                    selection.blockIds.extend(part.blockIds)
                    selection.linkIds.extend(part.linkIds)
            blocks, links = set(selection.blockIds), set(selection.linkIds)
            if not blocks <= {b['id'] for b in payload['blocks']} or not links <= {l['id'] for l in payload['links']}:
                raise ValueError('Selection does not match this input/mode')
            selection = Selection(blockIds=[b['id'] for b in payload['blocks'] if b['id'] in blocks],
                                  linkIds=list(dict.fromkeys(selection.linkIds)))
            if self.cache and cached is None:
                await self.cache.set(key, selection.model_dump(), 5, namespace='relevance/v4')
            if cache_trace is not None:
                cache_trace['hit'] = cached is not None
            return selection

    async def _record(self, method, *args, **kwargs):
        try:
            await method(*args, **kwargs)
        except Exception:
            # Metrics are observability only; a metric write must never reject
            # a material that passed the actual gates.
            logger.exception('Node source metric write failed')

    async def extract(self, request, *, now=None):
        monitor = getattr(self, 'monitor', None)
        if monitor is None:
            return await self._extract(request, now=now)
        from .job_monitor import timestamp
        job_id = 'sel_' + uuid.uuid4().hex
        started = time.monotonic()
        cache_trace = {}
        await monitor.emit(job_id, 'selection', status='running', phase='selection',
                           node_ids=[request.nodeId] if request.nodeId else [],
                           channel_id=request.channel, material_id=request.materialId,
                           store_id=self.store_id_for_channel(request.channel),
                           attempts=1, started_at=timestamp(), initialize_usage=True)
        async def usage(event):
            await monitor.usage(job_id, 'selection', event, phase='selection')
        try:
            with observe_usage(usage):
                response = await self._extract(request, now=now, cache_trace=cache_trace)
        except BaseException as error:
            await monitor.emit(job_id, 'selection',
                status='interrupted' if isinstance(error, asyncio.CancelledError) else 'failed',
                error_code=type(error).__name__, finished_at=timestamp(),
                duration_ms=int((time.monotonic()-started)*1000))
            raise
        rejected = (any(error.stage=='date_gate' for error in response.errors)
                    and not response.selection.linkIds)
        failed = any(error.stage!='date_gate' for error in response.errors)
        await monitor.emit(job_id, 'selection', status=(
            'failed' if failed else 'rejected' if rejected else 'succeeded'),
            phase='finished', error_code=response.errors[0].stage if response.errors else None,
            cache_hit=cache_trace.get('hit', False),
            finished_at=timestamp(), duration_ms=int((time.monotonic()-started)*1000),
            model_calls=response.modelCalls,
            selected_blocks=len(response.selection.blockIds) if response.selection else 0,
            selected_links=len(response.selection.linkIds) if response.selection else 0)
        return response

    async def _extract(self, request, *, now=None, cache_trace=None):
        # The channel is server-owned; resolve its schema store and statement.
        channel = self.config.channel(request.channel)
        store_id = channel.schema_ref
        window = None
        publication = None
        if channel.date_window_days:
            now = now or datetime.now(TIMEZONE)
            start, end = window_bounds(channel.date_window_days, now=now)
            window = dict(days=channel.date_window_days, asOf=end, **{'from': start, 'to': end})
            if request.mode != 'only_link':
                publication = extract_publication_date(
                    request.meta, request.input.title, [b.model_dump() for b in request.input.blocks], now=now)
        response = ExtractResponse(materialId=request.materialId)

        async def chat(messages, model):
            response.modelCalls += 1
            return await self.chat(messages, model)

        try:
            response.selection = await self._selection(request, channel, window, chat,
                                                      now=now, publication=publication,
                                                      cache_trace=cache_trace)
        except Exception as error:
            response.errors.append(StageError(stage='selection', message=str(error)[:500]))
            _, link_counters = refine_link_selection([], request.input.links)
            _log_link_selection(request, link_counters)
            return response

        # Each selected link owns its date evidence. Page dates
        # gate only selected original blocks, never the links on that page.
        if window:
            links = {link.id: link for link in request.input.links}
            response.selection.linkIds = [link_id for link_id in response.selection.linkIds
                if not link_date_rejection(links[link_id].text, links[link_id].context,
                                           channel.date_window_days, now=now)]
        # Apply one policy across all relevance batches, including cache hits.
        response.selection.linkIds, link_counters = refine_link_selection(
            response.selection.linkIds, request.input.links)
        _log_link_selection(request, link_counters)
        if publication is None:
            publication = extract_publication_date(
                request.meta, request.input.title, [b.model_dump() for b in request.input.blocks], now=now)
        rejection = ''
        if request.mode != 'only_link' and response.selection.blockIds:
            rejection = check_window(publication, channel.date_window_days, now=now)
        if rejection:
            logger.info('Channel %s date gate rejected %s selected blocks in material %s (%s, source=%s)',
                        channel.id, len(response.selection.blockIds), request.materialId[:16],
                        rejection, publication.source)
            response.errors.extend(StageError(stage='date_gate', blockId=block_id,
                                               message=_date_gate_reason(rejection))
                                   for block_id in response.selection.blockIds)
            response.selection.blockIds = []

        if request.nodeId:
            await self._record(self.metrics.record_submission, request.nodeId, request.meta, channel.id)
            await self._record(self.metrics.record_selection, request.nodeId, request.meta,
                               selected_blocks=len(response.selection.blockIds),
                               selected_links=len(response.selection.linkIds), **link_counters)

        selected = set(response.selection.blockIds)
        blocks = [b for b in request.input.blocks if b.id in selected]
        # Persist the selected ORIGINAL text; the graph model is invoked ONLY
        # by the independent nightly batch.
        jobs = [self.queue.enqueue(request, b, channel=channel, library_id=store_id,
                                   publication=publication) for b in blocks]
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        for block, outcome in zip(blocks, outcomes):
            if isinstance(outcome, BaseException):
                response.errors.append(StageError(stage='ontology_queue', blockId=block.id,
                                                  message=str(outcome)[:500]))
            else:
                if outcome.status == 'done' and request.nodeId:
                    job = await self.queue.get(outcome.id)
                    receipt = (job or {}).get('receipt') or {}
                    engine = self.engine(store_id)
                    if engine is not None and receipt.get('id'):
                        await asyncio.to_thread(engine.store.merge_node_provenance,
                                                receipt['id'], [request.nodeId])
                response.ontologyJobs.append(outcome)
                monitor = getattr(self, 'monitor', None)
                if monitor is not None:
                    await monitor.job(await self.queue.get(outcome.id))
        return response
