"""Relevance -> durable original-block backlog (no journal, no graph LLM)."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from dataclasses import replace
import hashlib
import json
import logging
import re

from core.ontology_libraries import LibraryRegistry
from core.wis.page_material import normalize_publish_date, relevance_batches
from .llmuse import select_relevance
from .models import Block, Focus, ExtractResponse, Selection, StageError
from .ontology import OntologyExtractor
from .ontology_queue import OntologyQueue
from .ontology_batch import OntologyBatch

logger = logging.getLogger(__name__)

_CN_NUM = {'一': 1, '两': 2, '二': 2, '三': 3, '四': 4, '五': 5,
           '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def focus_window_days(text):
    """Parse an explicit recency window ("最近N天") from the focuspoint text.

    Returns the number of days, or None when the focus states no such window.
    Only this narrow, explicit phrasing enables the hard date gate — free-form
    time hints stay with the relevance prompt.
    """
    match = re.search(r'最近\s*([0-9]+|[一二两三四五六七八九十]+)\s*天', text or '')
    if not match:
        return None
    value = match[1]
    days = int(value) if value.isdigit() else _CN_NUM.get(value)
    return days if isinstance(days, int) and days > 0 else None


class ExtractionService:
    def __init__(self, settings, chat, db, cache=None, *, ontology=None):
        self.settings, self.chat, self.db, self.cache = settings, chat, db, cache
        self.libraries = LibraryRegistry(settings.libraries_path)
        self.ontology_engines = {}
        if ontology is None:
            try:
                for library_id, entry in self.libraries.libraries.items():
                    path = ((settings.graph_path or db.db_path.parent / 'ontology') if library_id == 'policy'
                            else db.db_path.parent / 'ontology_libraries' / library_id)
                    schema_path = settings.schema_path if library_id == 'policy' and settings.schema_path else entry['schema_path']
                    self.ontology_engines[library_id] = OntologyExtractor(replace(settings, schema_path=schema_path), path)
            except Exception:
                for engine in self.ontology_engines.values():
                    engine.store.close()
                raise
        self.ontology_engine = self.ontology_engines.get(self.libraries.default_id)
        self._custom_ontology = ontology
        self.ontology = self.extract_ontology
        self.queue = OntologyQueue(db)
        self.batch = OntologyBatch(settings, self.queue, self.ontology, chat,
                                   refresh=self.refresh_viewer if self.ontology_engine else None,
                                   reprocess_ids=self.reprocess_ids if self.ontology_engine else None,
                                   supersede=self.supersede if self.ontology_engine else None)
        self._locks = {}

    async def refresh_viewer(self):
        from .ontology_viewer import refresh_viewer
        for library_id, engine in self.ontology_engines.items():
            out = self.db.db_path.parent / 'viewer' / library_id
            await asyncio.to_thread(refresh_viewer, engine.store.path, out,
                                    store=engine.store.store, title=self.libraries.libraries[library_id]['title'])

    def engine(self, library_id=None):
        key = self.libraries.resolve(library_id)
        return self.ontology_engines.get(key)

    async def extract_ontology(self, *, library_id=None, **kwargs):
        key = self.libraries.resolve(library_id)
        if self._custom_ontology is not None:
            return await self._custom_ontology(**kwargs)
        return await self.ontology_engines[key].extract(**kwargs)

    async def reprocess_ids(self, limit=None, *, library_id=None, created_from=None, created_to=None):
        ids = []
        for job_id in await self.queue.pending_ids(include_done=True, library_id=library_id,
                                                    created_from=created_from, created_to=created_to):
            job = await self.queue.get(job_id)
            payload = job['payload']
            engine = self.engine(job['library_id'])
            extraction_id, _, _ = engine.identity(
                focus=Focus.model_validate(payload['focus']), title=payload['title'],
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

    async def _selection(self, request, chat):
        payload = request.input.model_dump(exclude_none=True)
        if request.mode == 'only_link':
            payload['blocks'] = []
        key = digest(['relevance/v4', request.focus.model_input(), payload,
                      self.settings.selected_model, self.settings.fallback_model])
        async with self._lock(key):
            cached = await self.cache.get(key, namespace='relevance/v4') if self.cache else None
            if cached is not None:
                selection = Selection.model_validate(cached)
            else:
                selection = Selection(blockIds=[], linkIds=[])
                for batch in relevance_batches(payload):
                    part = await select_relevance(chat, request.focus, batch, self.settings.selected_model,
                                                  self.settings.fallback_model)
                    selection.blockIds.extend(part.blockIds)
                    selection.linkIds.extend(part.linkIds)
            blocks, links = set(selection.blockIds), set(selection.linkIds)
            if not blocks <= {b['id'] for b in payload['blocks']} or not links <= {l['id'] for l in payload['links']}:
                raise ValueError('Selection does not match this input/mode')
            selection = Selection(blockIds=[b['id'] for b in payload['blocks'] if b['id'] in blocks],
                                  linkIds=[l['id'] for l in payload['links'] if l['id'] in links])
            if self.cache and cached is None:
                await self.cache.set(key, selection.model_dump(), 5, namespace='relevance/v4')
            return selection

    async def extract(self, request):
        library_id = self.libraries.resolve(request.ontologyLibrary, focus_id=request.focus.id)
        response = ExtractResponse(materialId=request.materialId)

        async def chat(messages, model):
            response.modelCalls += 1
            return await self.chat(messages, model)

        try:
            response.selection = await self._selection(request, chat)
        except Exception as error:
            response.errors.append(StageError(stage='selection', message=str(error)[:500]))
            return response

        # Hard date gate for explicitly time-windowed focuses ("最近N天"): a
        # page whose meta publish date is outside the window is rejected as a
        # whole — no ontology backlog, no link recursion. The relevance prompt
        # enforces this too, but the model demonstrably leaks (nightly policy
        # lists full of undated old-policy links); this layer keeps stale
        # material out of the graph regardless.
        window = focus_window_days(request.focus.focuspoint)
        if window is not None:
            published = normalize_publish_date(str(request.meta.get('publishedAt') or ''))
            if published:
                age = (datetime.now() - datetime.strptime(published, '%Y-%m-%d')).days
                if age >= window:
                    logger.info('Focus %s recency gate rejected material %s (published %s, window %dd)',
                                request.focus.id, request.materialId[:16], published, window)
                    response.selection = Selection(blockIds=[], linkIds=[])

        selected = set(response.selection.blockIds)
        blocks = [b for b in request.input.blocks if b.id in selected]
        # Persist the selected ORIGINAL text; the graph model is invoked ONLY
        # by the independent nightly batch.
        jobs = [self.queue.enqueue(request, b, library_id=library_id) for b in blocks]
        outcomes = await asyncio.gather(*jobs, return_exceptions=True)
        for block, outcome in zip(blocks, outcomes):
            if isinstance(outcome, BaseException):
                response.errors.append(StageError(stage='ontology_queue', blockId=block.id,
                                                  message=str(outcome)[:500]))
            else:
                response.ontologyJobs.append(outcome)
        return response
