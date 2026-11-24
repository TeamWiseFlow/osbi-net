"""Original-block evidence pages for downstream topnews/weekly selection.

Reads the exact block-result shape produced by OntologyStore.read and applies
generic date/board/content-type/quality/entity/text filters plus channel
provenance (sourceScope) and territory preference. Pure functions only: the
HTTP route gathers (job_id, block_result) entries from the durable
ontology_jobs receipts and the graph owner, then calls evidence_page.

The response keeps the legacy ``boards``/``sources[].meta`` fields for the
transitional client while adding the v4 provenance fields (channel, board,
dateSource, sources[].nodeId/url).
"""
from datetime import datetime, timezone

from pydantic import Field, model_validator

from .models import StrictModel
from .ontology_rules import classify_content, quality_flags
from .read_model import node_view, seed_view, uri_map

MAX_PAGE_LIMIT = 200


class EvidenceQuery(StrictModel):
    sourceScope: str = Field(default='all', pattern=r'^(all|own)$')
    fromDate: str | None = None
    toDate: str | None = None
    # Legacy board filter retained for the transitional client; channel is the
    # authoritative query key.
    boards: list[str] = Field(default_factory=list)
    territoryPreference: list[str] = Field(default_factory=list, max_length=20)
    territoryMode: str = Field(default='prefer', pattern=r'^(prefer|only)$')
    contentTypes: list[str] = Field(default_factory=list)
    excludeQuality: list[str] = Field(default_factory=list)
    entityUris: list[str] = Field(default_factory=list)
    text: str | None = None
    cursor: str | None = None
    asOf: float | None = None
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_LIMIT)

    @model_validator(mode='after')
    def validate_dates(self):
        from datetime import date
        for value in (self.fromDate, self.toDate):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError('Dates must use YYYY-MM-DD')
        if self.fromDate and self.toDate and self.fromDate > self.toDate:
            raise ValueError('fromDate must not follow toDate')
        return self


def _channel_fields(provenance, channel):
    channel_id = str(provenance.get('channel') or '') or (channel.id if channel else '')
    channel_title = str(provenance.get('channel_title') or '') or (channel.title if channel else '')
    board = channel_title or str(provenance.get('board_label') or '')
    return channel_id, channel_title, board


def _sources(provenance, title):
    meta = provenance.get('source_meta') or {}
    url = str(provenance.get('source_url') or meta.get('canonicalUrl')
              or meta.get('finalUrl') or meta.get('requestedUrl') or '')
    publisher = str(meta.get('publisher') or '')
    published = str(meta.get('publishedAt') or '')
    node_ids = [str(value) for value in (provenance.get('node_ids') or []) if value]
    if not node_ids:
        node_ids = [None]
    return [dict(nodeId=node_id, url=url, publisher=publisher, publishedAt=published,
                 title=title, meta=meta) for node_id in node_ids]


def _item_view(evidence_id, result, library_id, channel=None):
    """Normalize one active block result into a serializable evidence item."""
    provenance = result.get('provenance') or {}
    entities = result.get('entities') or []
    relations = result.get('relations') or []
    title = str(provenance.get('source_title') or '')
    published = str(provenance.get('published_date') or '')
    channel_id, channel_title, board = _channel_fields(provenance, channel)
    policy_content = bool(channel.rule('policyContent', False)) if channel else False
    content_type = (str(provenance.get('content_type') or '')
                    or classify_content(title, policy_content=policy_content,
                                        entity_count=len(entities), relation_count=len(relations)))
    flags = provenance.get('quality_flags')
    if not isinstance(flags, list):
        flags = quality_flags(title, published, content_type, len(entities), len(relations))
    date_source = str(provenance.get('date_source') or '') or ('meta' if published else 'unknown')
    return dict(
        id=result['id'], evidenceId=evidence_id, libraryId=library_id,
        channel=channel_id, board=board,
        text=str(provenance.get('raw_text') or ''), title=title, publishedDate=published,
        dateSource=date_source,
        boards=[board] if board else [],
        contentTypes=[content_type] if content_type else [],
        qualityFlags=flags, entityCount=len(entities), relationCount=len(relations),
        sources=_sources(provenance, title),
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


def _entity_names(item):
    return {str(entity.get('name') or '') for entity in item['entities']} | {
        str(entity.get('canonicalName') or '') for entity in item['entities']}


def _territory_score(item, preferences):
    """Number of declared territory preferences that identify this item."""
    if not preferences:
        return 0
    names = _entity_names(item)
    score = 0
    for preference in preferences:
        preference = preference.strip()
        if preference and any(preference in name for name in names if name):
            score += 1
    return score


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
    if request.territoryMode == 'only' and request.territoryPreference:
        if _territory_score(item, request.territoryPreference) <= 0:
            return False
    return True


def _sort_key(item, preferences):
    # Freshest and most connected evidence first; territory preferences are a
    # soft ordering hint (prefer), never a hard filter unless mode is "only".
    # evidenceId breaks ties so paging order is deterministic.
    return (_territory_score(item, preferences), item['publishedDate'],
            item['relationCount'], item['entityCount'], item['evidenceId'])


def evidence_page(entries, request, *, library_id=None, snapshot=None, channel=None,
                  node_id=None):
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
        provenance = result.get('provenance') or {}
        if request.sourceScope == 'own':
            contributors = {str(value) for value in (provenance.get('node_ids') or [])}
            if not node_id or node_id not in contributors:
                continue
        seen.add(result['id'])
        item = _item_view(evidence_id, result, library_id, channel)
        if snapshot is not None:
            item = _apply_normalization(item, result, snapshot, mapping, dropped)
        if _matches(item, request):
            items.append(item)
    preferences = request.territoryPreference if request.territoryMode == 'prefer' else []
    items.sort(key=lambda item: _sort_key(item, preferences), reverse=True)
    start = 0
    if request.cursor:
        start = next((i + 1 for i, item in enumerate(items)
                      if item['evidenceId'] == request.cursor), None)
        if start is None:
            raise ValueError('Unknown evidence cursor')
    page = items[start:start + request.limit]
    next_cursor = page[-1]['evidenceId'] if len(page) == request.limit else ''
    result = dict(items=page, cursor=next_cursor,
                  asOf=datetime.now(timezone.utc).isoformat(),
                  channel=(channel.id if channel else None))
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
