"""Private graph-owner operations. External clients use core.backend on 8077."""
import asyncio
from collections import Counter, defaultdict, deque

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse

from core.backend.ontology_models import OntologyQuery
from .ontology_viewer import STATIC_DIR, collect, render_html
from .read_graph import normalize_graph_data
from .read_model import cached_snapshot
from .control_plane import current_service


def query_graph(data, request):
    nodes = {n['id']: n for n in data['nodes']}

    def resolve(value):
        if value in nodes:
            return value
        legacy = [n for n in nodes.values() if value in n.get('aliases', [])]
        if len(legacy) == 1:
            return legacy[0]['id']
        matches = [n for n in nodes.values() if n['name'] == value]
        if not matches:
            matches = [n for n in nodes.values() if value in n['name']]
        if len(matches) != 1:
            raise HTTPException(409 if matches else 404, detail={
                'message': 'Choose an entity URI' if matches else 'Entity not found', 'candidates': matches})
        return matches[0]['id']

    source = resolve(request.source)
    adjacency = defaultdict(list)
    for edge in data['edges']:
        adjacency[edge['source']].append((edge['target'], edge))
        adjacency[edge['target']].append((edge['source'], edge))
    target = resolve(request.target) if request.target else None
    paths, truncated = [], False
    if target is None:
        edges = [edge for _, edge in adjacency[source]]
        ids = {source} | {other for other, _ in adjacency[source]}
    elif source == target:
        edges, ids = [], {source}
    else:
        queue = deque([(source, {source}, [])])
        explored, shortest = 0, None
        while queue and len(paths) < request.maxPaths:
            current, visited, path = queue.popleft()
            explored += 1
            if explored > 20000:
                truncated = True
                break
            if len(path) >= (shortest or request.maxHops):
                continue
            for other, edge in adjacency[current]:
                if other in visited:
                    continue
                trail = path + [edge]
                if other == target:
                    shortest = len(trail)
                    paths.append(trail)
                    if len(paths) >= request.maxPaths:
                        break
                elif len(trail) < request.maxHops:
                    if len(queue) >= 20000:
                        truncated = True
                        continue
                    queue.append((other, visited | {other}, trail))
        edges = list({(e['source'], e['target'], e['pred']): e for p in paths for e in p}.values())
        ids = {source, target} | {e[k] for e in edges for k in ('source', 'target')}
    return dict(nodes=[nodes[i] for i in sorted(ids)], edges=edges, paths=paths,
                source=source, target=target, truncated=truncated)


def router_for(app):
    router = APIRouter(prefix='/v1/ontology', include_in_schema=False)

    def engine_for(library_id=None):
        try:
            engine = current_service(app).engine(library_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        if engine is None:
            raise HTTPException(503, 'Ontology engine is unavailable')
        return engine

    async def data(library_id=None):
        engine = engine_for(library_id)
        # Reuse the sole owner's handle; never open the database in backend.
        return await asyncio.to_thread(collect, engine.store.store)

    async def normalized_data(library_id=None):
        """Viewer data with the read-model normalization applied (no write-back)."""
        engine = engine_for(library_id)
        key = current_service(app).libraries.resolve(library_id)
        graph = await asyncio.to_thread(collect, engine.store.store)
        receipts = await current_service(app).queue.done_receipts(key)

        def build():
            entries = []
            for job in receipts:
                result = engine.store.read(job['receipt']['id'])
                if result is not None:
                    entries.append((job['id'], result))
            return cached_snapshot(key, entries, seed_paths=current_service(app).settings.seed_paths,
                                   namespace=str(current_service(app).db.db_path))

        snapshot = await asyncio.to_thread(build)
        class_by_label = {label: uri.rsplit('#', 1)[-1].rsplit('/', 1)[-1]
                          for label, uri in engine.schema.classes.items()}
        return normalize_graph_data(graph, snapshot, class_by_label)

    @router.get('/status')
    async def status(libraryId: str | None = None):
        graph = await data(libraryId)
        key = current_service(app).libraries.resolve(libraryId)
        return dict(**await current_service(app).batch.status(key), graph=graph['stats'], libraryId=key,
                    typeConflicts=graph['type_conflicts'], dangling=graph['dangling'])

    @router.get('/entities')
    async def entities(q: str = Query('', max_length=500), label: str | None = None,
                       limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0), libraryId: str | None = None):
        graph = await normalized_data(libraryId)
        schema = engine_for(libraryId).schema
        if label in schema.classes:
            label = schema.classes[label].rsplit('#', 1)[-1].rsplit('/', 1)[-1]
        rows = [n for n in graph['nodes'] if q in n['name'] and (not label or n['cls'] == label or label in n['up'])]
        rows.sort(key=lambda n: (-n['nblocks'], n['name'], n['id']))
        return dict(items=rows[offset:offset + limit], total=len(rows), limit=limit, offset=offset)

    @router.get('/entity')
    async def entity(uri: str = Query(min_length=1, max_length=500), libraryId: str | None = None):
        return query_graph(await normalized_data(libraryId), OntologyQuery(source=uri))

    @router.get('/graph')
    async def graph(limit: int = Query(1000, ge=1, le=5000), libraryId: str | None = None):
        graph = await normalized_data(libraryId)
        graph['nodes'].sort(key=lambda n: (-n['nblocks'], n['id']))
        selected = graph['nodes'][:limit]
        ids = {n['id'] for n in selected}
        return dict(**{k: v for k, v in graph.items() if k not in {'nodes', 'edges'}},
                    nodes=selected, edges=[e for e in graph['edges'] if e['source'] in ids and e['target'] in ids],
                    truncated=len(graph['nodes']) > limit)

    @router.post('/query')
    async def query(request: OntologyQuery):
        return query_graph(await normalized_data(request.libraryId), request)

    @router.get('/stats')
    async def stats(libraryId: str | None = None):
        engine = engine_for(libraryId)
        service = current_service(app)
        key = service.libraries.resolve(libraryId)
        graph = await asyncio.to_thread(collect, engine.store.store)
        schema = engine.schema

        def local_name(uri):
            return uri.rsplit('#', 1)[-1].rsplit('/', 1)[-1]

        class_counts = Counter(node['cls'] for node in graph['nodes'])
        class_labels = {local_name(uri): label for label, uri in schema.classes.items()}
        predicate_counts = Counter(edge['pred'] for edge in graph['edges'])
        predicate_labels = {local_name(value['uri']): label for label, value in schema.predicates.items()}
        classes = sorted((dict(label=label, name=local_name(uri),
                               entities=class_counts.get(local_name(uri), 0))
                          for label, uri in schema.classes.items()), key=lambda row: (row['entities'], row['label']))
        predicates = sorted((dict(label=label, name=local_name(value['uri']),
                                  edges=predicate_counts.get(local_name(value['uri']), 0))
                             for label, value in schema.predicates.items()), key=lambda row: (row['edges'], row['label']))
        orphans = sorted((dict(uri=node['id'], name=node['name'], label=class_labels.get(node['cls'], node['cls']))
                          for node in graph['nodes'] if not node['out_edges'] and not node['in_edges']),
                         key=lambda row: (row['label'], row['name']))
        return dict(libraryId=key, title=service.libraries.libraries[key]['title'],
                    schemaId=engine.schema.fingerprint,
                    stats=dict(**graph['stats'], edges=len(graph['edges']), orphans=len(orphans)),
                    classes=classes, predicates=predicates, orphans=orphans)

    @router.get('/viewer/')
    async def viewer():
        service = current_service(app)
        return HTMLResponse(render_html(await data(), service.libraries.libraries[service.libraries.default_id]['title']))

    @router.post('/seeds')
    async def load_seeds(libraryId: str | None = None):
        """Idempotently load the configured deterministic admin skeletons."""
        engine = engine_for(libraryId)
        key = current_service(app).libraries.resolve(libraryId)
        loaded = await asyncio.to_thread(engine.store.load_seeds,
                                         list(current_service(app).settings.seed_paths))
        return dict(libraryId=key, loaded=loaded)

    @router.get('/libraries')
    async def libraries():
        service = current_service(app)
        return dict(defaultLibrary=service.libraries.default_id, items=[dict(
            id=key, title=entry['title'], channelIds=sorted(entry.get('channels') or []),
            schemaId=engine_for(key).schema.fingerprint,
            viewerUrl=f'/v1/ontology/libraries/{key}/viewer/')
            for key, entry in service.libraries.libraries.items()])

    @router.get('/libraries/{library_id}/viewer/')
    async def library_viewer(library_id: str):
        graph = await data(library_id)
        return HTMLResponse(render_html(graph, current_service(app).libraries.libraries[library_id]['title']))

    @router.get('/libraries/{library_id}/viewer/echarts.min.js')
    async def library_echarts(library_id: str):
        engine_for(library_id)
        return FileResponse(STATIC_DIR / 'echarts.min.js', media_type='application/javascript')

    @router.get('/viewer/echarts.min.js')
    async def echarts():
        return FileResponse(STATIC_DIR / 'echarts.min.js', media_type='application/javascript')

    return router
