"""Schema-guided entities/relations from each selected ORIGINAL body block."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re

from pydantic import Field

from osbi_contracts.urls import canonical_url
from osbi_contracts.urls import normalize_publish_date
from .llm_client import ModelError, generate, parse_json
from osbi_contracts.models import StrictModel
from .ontology_rules import (BOARD_LABELS, admin_level, canonical_event_text,
                             canonical_policy_text, classify_content, dedupe_entities,
                             inspection_title_candidate, is_region, meeting_title_candidate,
                             normalize_entities, normalize_predicate, policy_title_candidate,
                             quality_flags, region_level, strip_hint, valid_policy_text, _Triplet)
from .ontology_schema import DEFAULT_SCHEMA, OntologySchema
from .ontology_store import OntologyStore
from .ontology_tags import load_vocabulary
from .prompts import ENTITY_PROMPT, RELATION_PROMPT

VERSION = 'osbi-original-ontology/v2-concurrent-posts'
RULES_PATH = Path(__file__).with_name('ontology_rules.py')


@lru_cache(maxsize=32)
def _rules_fingerprint(entity_prompt=ENTITY_PROMPT, relation_prompt=RELATION_PROMPT) -> str:
    """Fingerprint rules together with the effective entity/relation instructions."""
    digest = hashlib.sha256()
    digest.update(VERSION.encode())
    digest.update(RULES_PATH.read_bytes())
    digest.update(json.dumps([entity_prompt, relation_prompt], ensure_ascii=False).encode())
    return digest.hexdigest()


def extraction_fingerprint(settings, prompts=None):
    """Identify the effective graph configuration for extraction and reprocessing."""
    prompts = prompts or settings.prompts
    identity = [VERSION, _rules_fingerprint(prompts.entities, prompts.relations),
                load_vocabulary().fingerprint, settings.base_url, settings.ontology_model, settings.fallback_model,
                settings.ontology_chunk_chars, settings.ontology_chunk_overlap]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


class JurisdictionValue(StrictModel):
    city: str = Field(min_length=2)
    district: str | None = None


class TagValue(StrictModel):
    dimension: str = Field(min_length=1)
    value: str = Field(min_length=1)


class EntityValue(StrictModel):
    text: str = Field(min_length=1)
    label: str = Field(min_length=1)
    identityAmbiguous: bool = False
    sourceText: str | None = None
    jurisdiction: JurisdictionValue | None = None
    tags: list[TagValue] = Field(default_factory=list)


class EntityPayload(StrictModel):
    entities: list[EntityValue]


class RelationValue(StrictModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)


class RelationPayload(StrictModel):
    relations: list[RelationValue]


@dataclass(frozen=True)
class Entity:
    text: str
    label: str
    city: str | None = None
    district: str | None = None
    tags: tuple = ()


@lru_cache(maxsize=32)
def load_schema(text):
    return OntologySchema(text)


def chunks(text, size, overlap):
    """Bound model input within a selected block; preserve every source character."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind('\n', start + size // 2, end)
            if boundary > start:
                end = boundary + 1
        part = text[start:end]
        if part.strip():
            yield part
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


def gate_relations(schema, entities, relations, aliases, *, allow_location=True,
                   stage='model', sample_limit=12, per_reason_limit=4):
    labels = {}
    for entity in entities:
        labels.setdefault(entity.text, set()).add(entity.label)



    compact_entities = {}
    for text in labels:
        compact_entities.setdefault(re.sub(r'\s+', '', text), text)



    long_entities = sorted({text for text in labels if len(re.sub(r'\s+', '', text)) >= 8},
                           key=len, reverse=True)

    def resolve_endpoint(text):
        if not text:
            return None
        if text in labels:
            return text
        alias = aliases.get(text)
        if alias in labels:
            return alias
        compact = re.sub(r'\s+', '', text)
        if compact in compact_entities:
            return compact_entities[compact]
        if alias:
            compact_alias = re.sub(r'\s+', '', alias)
            if compact_alias in compact_entities:
                return compact_entities[compact_alias]
        if len(compact) >= 8:
            hits = [candidate for candidate in long_entities
                    if compact in re.sub(r'\s+', '', candidate)
                    or re.sub(r'\s+', '', candidate) in compact]
            if len(hits) == 1:
                return hits[0]
        return None

    kept = {}
    stats = dict(dropped_nonentity=0, dropped_direction=0, dropped_predicate=0, flipped=0,
                 dropped_location=0, dropped_hierarchy=0)
    samples = []
    sample_counts = {}

    def region_info(text):
        found = [label for label in labels.get(text, ()) if is_region(text, label)]
        levels = [level for label in found
                  if (level := region_level(text, label)) is not None]
        return bool(found), (max(levels) if levels else None)

    def subject_level(text):
        levels = [level for label in labels.get(text, ())
                  if (level := admin_level(label)) is not None]
        return max(levels) if levels else None

    def labels_of(text):
        return sorted(labels.get(text, ())) if text else []

    def dropped(reason, row, *, subject=None, obj=None, predicate=None, constraint=None):
        """Count a gate rejection and keep a small, serializable sample for diagnosis."""
        stats[reason] += 1
        if len(samples) >= sample_limit or sample_counts.get(reason, 0) >= per_reason_limit:
            return
        sample_counts[reason] = sample_counts.get(reason, 0) + 1
        item = dict(
            reason=reason, stage=stage,
            subject=row.subject, predicate=predicate or row.predicate,
            rawPredicate=row.predicate, object=row.object,
            subjectLabels=labels_of(subject or row.subject),
            objectLabels=labels_of(obj or row.object),
        )
        if constraint:
            item['domain'] = list(constraint.get('domain') or ())
            item['range'] = list(constraint.get('range') or ())
        samples.append(item)

    for row in relations:
        p = normalize_predicate(row.predicate)
        if p not in schema.predicates:
            dropped('dropped_predicate', row, predicate=p)
            continue
        if p == '位于' and not allow_location:

            dropped('dropped_location', row, predicate=p,
                    constraint=schema.predicates.get(p))
            continue
        subject = resolve_endpoint(row.subject)
        obj = resolve_endpoint(row.object)
        subjects = [subject] if subject else []
        objects = [obj] if obj else []
        if not subjects or not objects:
            dropped('dropped_nonentity', row, predicate=p,
                    constraint=schema.predicates.get(p))
            continue

        if p == '任职' and '现任' in schema.predicates and any(
                '职务' in labels[o] for o in objects):
            p = '现任'
        constraint = schema.predicates[p]
        def fits(subject, object):
            return (any(schema.matches(label, constraint['domain']) for label in labels[subject]) and
                    any(schema.matches(label, constraint['range']) for label in labels[object]))
        candidates = [(s, o) for s in subjects for o in objects]
        valid = next(((s, o) for s, o in candidates if fits(s, o)), None)
        if valid is None:
            valid = next(((o, s) for s, o in candidates if fits(o, s)), None)
            if valid is None:
                dropped('dropped_direction', row, predicate=p, constraint=constraint)
                continue
            stats['flipped'] += 1
        s, o = valid
        subject_region, subject_region_level = region_info(s)
        object_region, object_region_level = region_info(o)
        if p == '位于':
            if subject_region or not object_region:
                dropped('dropped_location', row, subject=s, obj=o,
                        predicate=p, constraint=constraint)
                continue
            level = subject_level(s)
            if level is not None and object_region_level is not None and object_region_level > level:
                dropped('dropped_location', row, subject=s, obj=o,
                        predicate=p, constraint=constraint)
                continue
        elif p == '隶属' and (subject_region or object_region):


            if (not allow_location or not subject_region or not object_region
                    or subject_region_level is None or object_region_level is None
                    or subject_region_level <= object_region_level):
                dropped('dropped_hierarchy', row, subject=s, obj=o,
                        predicate=p, constraint=constraint)
                continue
        kept[(s, p, o)] = _Triplet(s, p, o)
    return list(kept.values()), stats, samples


class OntologyExtractor:
    def __init__(self, settings, path):
        self.settings = settings
        if not 0 <= settings.ontology_chunk_overlap < settings.ontology_chunk_chars:
            raise ValueError('Invalid ontology chunk size/overlap')
        self.schema = load_schema((settings.schema_path or DEFAULT_SCHEMA).read_text())
        self.tags = load_vocabulary()
        self.store = OntologyStore(path)
        self.inflight = {}

    @asynccontextmanager
    async def admission(self, extraction_id):
        entry = self.inflight.setdefault(extraction_id, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                yield
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self.inflight.pop(extraction_id, None)

    def identity(self, *, focus, title, block, meta, prompts=None):
        schema = self.schema
        url = canonical_url(meta.get('canonicalUrl') or meta.get('finalUrl') or meta.get('requestedUrl', ''))
        if not url:
            raise ValueError('Ontology requires a source HTTP(S) URL in meta')
        identity = [extraction_fingerprint(self.settings, prompts), schema.fingerprint,
                    focus.id, focus.model_input(), title, block.text,
                    {k: meta.get(k, '') for k in ('publishedAt', 'author', 'publisher')}]
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return 'o_' + key, schema, url

    async def extract(self, *, focus, title, block, meta, material_id, chat, prompts=None):
        extraction_id, _, _ = self.identity(focus=focus, title=title, block=block, meta=meta, prompts=prompts)
        async with self.admission(extraction_id):
            return await self._extract(focus=focus, title=title, block=block, meta=meta,
                                       material_id=material_id, chat=chat, prompts=prompts)

    async def _extract(self, *, focus, title, block, meta, material_id, chat, prompts=None):
        prompts = prompts or self.settings.prompts
        extraction_id, schema, url = self.identity(focus=focus, title=title, block=block, meta=meta, prompts=prompts)
        previous = await asyncio.to_thread(self.store.read, extraction_id)
        if previous is not None:
            return self.receipt(previous, block.id, cached=True)
        entities, relations, models = {}, {}, set()
        statistics = dict(chunks=0, rejected_entities=0, dropped_nonentity=0,
                          dropped_direction=0, dropped_predicate=0, flipped=0, dropped_tags=0,
                          dropped_location=0, dropped_hierarchy=0, droppedSamples=[], rejectedEntitySamples=[])
        for text in chunks(block.text, self.settings.ontology_chunk_chars, self.settings.ontology_chunk_overlap):
            statistics['chunks'] += 1
            base = dict(focus=focus.model_input(), title=title, text=text)
            schema_prompt = schema.prompt()
            entity_schema = dict(classes=schema_prompt['classes'], tags=self.tags.prompt())
            messages = [dict(role='system', content=prompts.entities), dict(role='user', content=json.dumps(
                dict(stage='ontology_entities', **base, schema=entity_schema), ensure_ascii=False))]
            evidence = re.sub(r'\s+', '', title + '\n' + text)
            def validate_entities(raw):
                value = EntityPayload.model_validate(parse_json(raw))
                if value.entities and not any(strip_hint(e.label) in schema.classes for e in value.entities):
                    raise ModelError('All entity labels are outside the ontology')
                if value.entities and not any(e.text.strip() and strip_hint(e.label) in schema.classes
                        and re.sub(r'\s+', '', e.sourceText or e.text) in evidence for e in value.entities):
                    raise ModelError('No entity has evidence in the original block')
                return value.entities
            raw, model = await generate(chat, messages, self.settings.ontology_model,
                                        self.settings.fallback_model, validate_entities)
            models.add(model)

            if not raw and len(text) >= 100 and model != self.settings.fallback_model:
                raw, model = await generate(chat, messages, self.settings.fallback_model,
                                            self.settings.fallback_model, validate_entities)
                models.add(model)

            grounded = []
            for e in raw:
                if (strip_hint(e.label) not in schema.classes or not e.text.strip()
                        or re.sub(r'\s+', '', e.sourceText or e.text) not in evidence):
                    if len(statistics['rejectedEntitySamples']) < 20:
                        statistics['rejectedEntitySamples'].append(dict(text=e.text, label=e.label,
                            reason='unknown_type' if strip_hint(e.label) not in schema.classes else 'missing_original_evidence'))
                    continue
                tags, dropped = self.tags.normalize(e.tags)
                statistics['dropped_tags'] += dropped
                grounded.append(Entity(
                    (e.sourceText or e.text).strip(), strip_hint(e.label),
                    e.jurisdiction.city if e.jurisdiction else None,
                    e.jurisdiction.district if e.jurisdiction else None,
                    tuple((tag['dimension'], tag['value']) for tag in tags)))
            kept, aliases, minted = normalize_entities(grounded, set(schema.classes), title + '\n' + block.text)
            for e in raw:
                if e.sourceText and e.sourceText in aliases:
                    aliases[e.text] = aliases[e.sourceText]
            kept = list({(e.text, e.label):e for e in kept if e.label in schema.classes}.values())
            kept = [replace(e, tags=self.tags.merge(
                        e.tags, self.tags.derive(e.text, schema.matches(e.label, ['机构组织']))))
                    for e in kept]




            def _same_title(left: str, right: str) -> bool:
                compact_left = re.sub(r'\s+', '', left)
                compact_right = re.sub(r'\s+', '', right)
                return (compact_left == compact_right
                        or compact_left in compact_right
                        or compact_right in compact_left)

            policy_title = canonical_policy_text(policy_title_candidate(title))
            if policy_title and '政策文件' in schema.classes:
                if not any(schema.matches(e.label, ['政策文件']) and _same_title(e.text, policy_title)
                           for e in kept):
                    kept.append(Entity(text=policy_title, label='政策文件'))
                    aliases.setdefault(title, policy_title)



            meeting_title = canonical_event_text(meeting_title_candidate(title))
            if meeting_title and '会议论坛' in schema.classes:
                if not any(schema.matches(e.label, ['会议论坛']) and _same_title(e.text, meeting_title)
                           for e in kept):
                    kept.append(Entity(text=meeting_title, label='会议论坛'))
                    aliases.setdefault(title, meeting_title)



            inspection_title = canonical_event_text(inspection_title_candidate(title))
            if inspection_title and '调研视察' in schema.classes:
                if not any(schema.matches(e.label, ['调研视察']) and _same_title(e.text, inspection_title)
                           for e in kept):
                    kept.append(Entity(text=inspection_title, label='调研视察'))
                    aliases.setdefault(title, inspection_title)




            if '政策文件' in schema.classes and kept:
                existing_policy = [re.sub(r'\s+', '', e.text.strip('《》'))
                                   for e in kept if schema.matches(e.label, ['政策文件'])]
                minted_titles = 0
                for match in re.finditer(r'《([^《》\n]{3,120})》', block.text):
                    inner = canonical_policy_text(match.group(1).strip())
                    if minted_titles >= 8 or not valid_policy_text(inner):
                        continue
                    compact_title = re.sub(r'\s+', '', inner)
                    if any(compact_title == value or compact_title in value or value in compact_title
                           for value in existing_policy):
                        continue
                    kept.append(Entity(text=inner, label='政策文件'))
                    existing_policy.append(compact_title)
                    aliases.setdefault(match.group(0), inner)
                    aliases.setdefault(inner, inner)
                    minted_titles += 1
            names = {e.text for e in kept}
            statistics['rejected_entities'] += len(raw) - sum(aliases.get(e.text, e.text) in names for e in grounded)
            if kept:
                relation_schema = {key: value for key, value in schema_prompt.items() if key != 'tags'}
                messages = [dict(role='system', content=prompts.relations), dict(role='user', content=json.dumps(
                    dict(stage='ontology_relations', **base, schema=relation_schema,
                         entities=[dict(text=e.text, label=e.label) for e in kept]), ensure_ascii=False))]
                raw_relations, model = await generate(chat, messages, self.settings.ontology_model,
                    self.settings.fallback_model, lambda raw: RelationPayload.model_validate(parse_json(raw)).relations)
                models.add(model)


                gated_raw, counts, samples_raw = gate_relations(
                    schema, kept, raw_relations, aliases, allow_location=False, stage='model')
                gated_minted, minted_counts, samples_minted = gate_relations(
                    schema, kept, minted, aliases, allow_location=True, stage='minted')
                for key, count in counts.items():
                    statistics[key] += count + minted_counts.get(key, 0)

                statistics['droppedSamples'].extend(samples_raw + samples_minted)
                if len(statistics['droppedSamples']) > 20:
                    del statistics['droppedSamples'][20:]
                gated = list({(r.subject, r.predicate, r.object): r
                              for r in [*gated_raw, *gated_minted]}.values())
                relations.update({(r.subject, r.predicate, r.object):r for r in gated})
                for e in kept:
                    key = (e.text, e.label)
                    previous = entities.get(key)
                    entities[key] = (replace(e, tags=tuple(sorted(set(previous.tags) | set(e.tags))))
                                     if previous else e)
        entity_list = dedupe_entities(list(entities.values()))
        published_date = normalize_publish_date(str(meta.get('publishedAt') or ''))
        content_type = classify_content(title, focus.id, len(entity_list), len(relations))
        provenance = dict(material_id=material_id, block_id=block.id, focus_id=focus.id,
                          board=focus.focuspoint, board_label='',
                          focus=focus.model_input(), source_url=url,
                          source_title=title, raw_text=block.text, source_meta=meta,
                          published_date=published_date,
                          content_type=content_type,
                          quality_flags=quality_flags(title, published_date, content_type,
                                                      len(entity_list), len(relations)),
                          data_source='selected_original_block', extraction_version=VERSION,
                          rules_fingerprint=_rules_fingerprint(prompts.entities, prompts.relations),
                          configured_model=self.settings.ontology_model, fallback_model=self.settings.fallback_model)
        result = await asyncio.to_thread(self.store.write, extraction_id, schema, entity_list,
                                        list(relations.values()), provenance, sorted(models), statistics)
        return self.receipt(result, block.id, cached=False)

    @staticmethod
    def receipt(result, block_id, cached):
        stats = result.get('statistics') or {}
        dropped_counts = {key: value for key, value in stats.items()
                          if isinstance(value, int) and (key.startswith('dropped_') or key == 'flipped')}
        return dict(blockId=block_id, id=result['id'], status='stored' if result['entities'] else 'empty',
                    cached=cached, entityCount=len(result['entities']), relationCount=len(result['relations']),
                    models=result['models'], droppedSamples=(stats.get('droppedSamples') or [])[:20],
                    droppedCounts=dropped_counts)

    async def close(self):
        await asyncio.to_thread(self.store.close)
