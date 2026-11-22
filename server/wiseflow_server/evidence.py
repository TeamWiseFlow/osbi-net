"""Original-block evidence pages for downstream topnews/weekly selection.

Reads the exact block-result shape produced by OntologyStore.read and applies
generic date/board/content-type/quality/entity/text filters. Pure functions
only: the HTTP route gathers (job_id, block_result) entries from the durable
ontology_jobs receipts and the graph owner, then calls evidence_page.

Legacy blocks extracted before provenance carried board_label/content_type/
quality_flags are normalized here with the same deterministic rules used at
extraction time, so no re-extraction is required.
"""
from datetime import datetime, timezone

from pydantic import Field

from .models import StrictModel
from .ontology_rules import BOARD_LABELS, classify_content, quality_flags
from .read_model import node_view, seed_view, uri_map

MAX_PAGE_LIMIT = 200


class EvidenceQuery(StrictModel):
    fromDate: str | None = None
    toDate: str | None = None
    boards: list[str] = Field(default_factory=list)
    contentTypes: list[str] = Field(default_factory=list)
    excludeQuality: list[str] = Field(default_factory=list)
    entityUris: list[str] = Field(default_factory=list)
    text: str | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)


def _item_view(evidence_id, result, library_id):
    """Normalize one active block result into a serializable evidence item."""
    provenance = result.get('provenance') or {}
    entities = result.get('entities') or []
    relations = result.get('relations') or []
    title = str(provenance.get('source_title') or '')
    published = str(provenance.get('published_date') or '')
    focus_id = provenance.get('focus_id')
    board = str(provenance.get('board_label') or '') or BOARD_LABELS.get(focus_id, '')
    content_type = (str(provenance.get('content_type') or '')
                    or classify_content(title, focus_id, len(entities), len(relations)))
    flags = provenance.get('quality_flags')
    if not isinstance(flags, list):
        flags = quality_flags(title, published, content_type, len(entities), len(relations))
    return dict(
        id=result['id'], evidenceId=evidence_id, libraryId=library_id,
        text=str(provenance.get('raw_text') or ''), title=title, publishedDate=published,
        boards=[board] if board else [],
        contentTypes=[content_type] if content_type else [],
        qualityFlags=flags, entityCount=len(entities), relationCount=len(relations),
        sources=[dict(title=title, meta=provenance.get('source_meta') or {})],
        entities=[dict(uri=e['uri'], name=e['text'], label=e['label']) for e in entities],
        relations=[dict(subject=r['subject'], object=r['object'], label=r['label'])
                   for r in relations])


def _apply_normalization(item, result, snapshot, mapping, dropped):
    """Overlay the normalized read model onto one evidence item.

    Entity URIs collapse onto their alias representative; names/labels/degrees
    come from the library-wide normalized snapshot (seed skeleton included).
    Relations are remapped the same way; edges touching dropped generic hubs
    disappear, mirroring production ``_rewrite_references``. Dangling
    endpoints (never extracted as entities) keep their original text, exactly
    like production edges which do not require both nodes to exist.
    """
    entities, seen_uris = [], set()
    for entity in item['entities']:
        uri = mapping.get(entity['uri'], entity['uri'])
        node = snapshot.nodes.get(uri)
        if node is None or uri in seen_uris:
            continue
        seen_uris.add(uri)
        entities.append(dict(
            uri=uri, name=node.get('name', ''), label=node.get('label', ''),
            canonicalName=node.get('canonicalName', ''),
            regionLevel=node.get('regionLevel'), degree=node.get('degree', 0),
            seed=bool(node.get('seed')), qualified=node.get('qualified'),
            policySubstantive=node.get('policySubstantive'),
            aliases=node.get('aliases') or [],
            mergedUris=node.get('mergedUris') or []))
    relations, seen_relations = [], set()
    raw_relations = result.get('relations') or []
    for index, relation in enumerate(item['relations']):
        raw = raw_relations[index] if index < len(raw_relations) else {}
        subject = mapping.get(relation['subject'], relation['subject'])
        obj = mapping.get(relation['object'], relation['object'])
        key = (subject, relation['label'], obj)
        if subject in dropped or obj in dropped or key in seen_relations:
            continue
        seen_relations.add(key)
        relations.append(dict(
            subject=subject, object=obj, label=relation['label'],
            subjectText=(snapshot.nodes.get(subject, {}).get('name')
                         or raw.get('subjectText') or ''),
            objectText=(snapshot.nodes.get(obj, {}).get('name')
                        or raw.get('objectText') or '')))
    item['entities'] = entities
    item['relations'] = relations
    item['entityCount'] = len(entities)
    item['relationCount'] = len(relations)
    item['normalized'] = True
    return item


def _matches(item, request):
    published = item['publishedDate']
    if (request.fromDate or request.toDate) and not published:
        return False
    if request.fromDate and published < request.fromDate:
        return False
    if request.toDate and published > request.toDate:
        return False
    if request.boards and not set(item['boards']).intersection(request.boards):
        return False
    if request.contentTypes and not set(item['contentTypes']).intersection(request.contentTypes):
        return False
    if request.excludeQuality and set(item['qualityFlags']).intersection(request.excludeQuality):
        return False
    if request.entityUris and not {e['uri'] for e in item['entities']}.intersection(request.entityUris):
        return False
    if request.text and request.text.casefold() not in (item['title'] + '\n' + item['text']).casefold():
        return False
    return True


def _sort_key(item):
    # Freshest and most connected evidence first; evidenceId breaks ties so
    # paging order is deterministic across requests.
    return (item['publishedDate'], item['relationCount'], item['entityCount'], item['evidenceId'])


def evidence_page(entries, request, *, library_id, snapshot=None):
    """Filter and page (evidence_id, block_result) entries.

    Superseded and duplicate graphs are skipped. Without a cursor the first
    page starts at the top of the sorted list; a cursor resumes right after
    the item whose evidenceId it carries. Raises ValueError for an unknown
    cursor (the route maps it to 422).

    When ``snapshot`` (the normalized read model) is supplied, items carry
    normalized entities/relations and the response adds the seed skeleton,
    the referenced normalized nodes and the auditable normalization stats.
    """
    mapping = uri_map(snapshot) if snapshot is not None else {}
    dropped = set()
    if snapshot is not None:
        # Generic hubs removed by normalization: raw entity URIs that are
        # neither surviving nodes nor merged aliases.
        raw_uris = set()
        for _, result in entries:
            if isinstance(result, dict) and not result.get('supersededBy'):
                raw_uris.update(str(e.get('uri') or '')
                                for e in result.get('entities') or [])
        dropped = {uri for uri in raw_uris
                   if uri and uri not in snapshot.nodes and uri not in mapping}
        if request.entityUris:
            request = request.model_copy(update=dict(
                entityUris=sorted({mapping.get(uri, uri) for uri in request.entityUris})))
    items, seen = [], set()
    for evidence_id, result in entries:
        if not isinstance(result, dict) or not result.get('id') or result.get('supersededBy'):
            continue
        if result['id'] in seen:
            continue
        seen.add(result['id'])
        item = _item_view(evidence_id, result, library_id)
        if snapshot is not None:
            item = _apply_normalization(item, result, snapshot, mapping, dropped)
        if _matches(item, request):
            items.append(item)
    items.sort(key=_sort_key, reverse=True)
    start = 0
    if request.cursor:
        start = next((i + 1 for i, item in enumerate(items)
                      if item['evidenceId'] == request.cursor), None)
        if start is None:
            raise ValueError('Unknown evidence cursor')
    page = items[start:start + request.limit]
    next_cursor = page[-1]['evidenceId'] if len(page) == request.limit else ''
    result = dict(items=page, cursor=next_cursor,
                  asOf=datetime.now(timezone.utc).isoformat())
    if snapshot is not None:
        referenced = set()
        for item in page:
            referenced.update(entity['uri'] for entity in item['entities'])
            for relation in item['relations']:
                referenced.update((relation['subject'], relation['object']))
        seeds = seed_view(snapshot)
        seed_uris = {node['uri'] for node in seeds['seedNodes']}
        result.update(
            normalized=True,
            normalization=snapshot.normalization,
            nodes=[node_view(snapshot.nodes[uri]) for uri in sorted(referenced - seed_uris)
                   if uri in snapshot.nodes],
            **seeds)
    return result
