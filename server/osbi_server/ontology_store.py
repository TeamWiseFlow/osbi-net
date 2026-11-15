"""One Oxigraph owner; atomic per-original-block graphs and durable receipts."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from functools import wraps
from threading import RLock

from pyoxigraph import BlankNode, Literal, NamedNode, Quad, Store
from rdflib import BNode, Graph, Literal as RDFLiteral, URIRef
from rdflib.namespace import RDF

from .ontology_rules import canonical_entity_name, geo_level

NS = 'https://osbi.net/provenance#'
BLOCK_BASE = 'https://osbi.net/block/'
SCHEMA_BASE = 'https://osbi.net/schema/'
INSTANCE_BASE = 'https://osbi.net/entity/'
RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
SEED_GRAPH = BLOCK_BASE + '_seeds_shanghai'
DEFAULT_SEEDS = Path(__file__).with_name('schemas') / 'seeds_shanghai.ttl'
_SEED_CLASS_LEVEL = {'NationalGov': ('国家级机构', 0), 'ProvinceGov': ('省级机构', 1),
                     'CityGov': ('市级机构', 2), 'DistrictGov': ('区级机构', 3),
                     'StreetGov': ('街镇机构', 4)}


def uri_for(text, scope=None):

    identity = text.strip() if scope is None else json.dumps([text.strip(), scope], ensure_ascii=False)
    return INSTANCE_BASE + hashlib.sha1(identity.encode()).hexdigest()[:12]


def rdf_term(term):
    if isinstance(term, BNode):
        return BlankNode(str(term))
    if isinstance(term, RDFLiteral):
        return Literal(str(term), language=term.language) if term.language else Literal(
            str(term), datatype=NamedNode(str(term.datatype))) if term.datatype else Literal(str(term))
    return NamedNode(str(term))


def synchronized(method):
    @wraps(method)
    def run(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return run


class OntologyStore:
    def __init__(self, path):
        self.lock = RLock()
        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(self.path))

    @synchronized
    def read(self, extraction_id):
        graph = NamedNode(BLOCK_BASE + extraction_id)
        head = NamedNode(graph.value + '/source')
        rows = list(self.store.quads_for_pattern(head, NamedNode(NS + 'result'), None, graph))
        if not rows:
            return None
        result = json.loads(rows[0].object.value)
        replacements = list(self.store.quads_for_pattern(head, NamedNode(NS + 'supersededBy'), None, graph))
        if replacements:
            result['supersededBy'] = replacements[0].object.value
        return result

    @synchronized
    def supersede(self, old_id, new_id):
        if old_id == new_id:
            return
        if self.read(new_id) is None:
            raise ValueError('Replacement graph is not complete')
        graph = NamedNode(BLOCK_BASE + old_id)

        self.store.extend([Quad(NamedNode(graph.value + '/source'), NamedNode(NS + 'supersededBy'),
                                NamedNode(BLOCK_BASE + new_id), graph)])
        self.store.flush()

    @synchronized
    def write(self, extraction_id, schema, entities, relations, provenance, models, statistics):
        graph_uri = BLOCK_BASE + extraction_id
        graph, head = NamedNode(graph_uri), NamedNode(graph_uri + '/source')
        quads = []
        schema_graph = NamedNode(SCHEMA_BASE + schema.fingerprint)
        for s, p, o in schema.graph:
            quads.append(Quad(rdf_term(s), rdf_term(p), rdf_term(o), schema_graph))
        nodes = []
        for e in entities:
            person = schema.matches(e.label, ['人物']) if '人物' in schema.classes else False
            qualifiers = sorted({r.object for r in relations if r.subject == e.text and r.predicate in {'任职', '现任'}}) if person else []
            node = dict(text=e.text, label=e.label, uri=uri_for(e.text, (self.path.parent.name, schema.classes[e.label], extraction_id if person else None)), classUri=schema.classes[e.label],
                        canonicalName=canonical_entity_name(e.text, e.label),
                        tags=[dict(dimension=d, value=v) for d, v in getattr(e, 'tags', ())])
            level = geo_level(e.text, e.label)
            if level is not None:
                node['regionLevel'] = level
            if person:
                node.update(identityStatus='unresolved' , identityHints=qualifiers)
            nodes.append(node)
        by_text = {e['text']: e['uri'] for e in nodes}
        edges = []
        for e in nodes:
            quads.extend([
                Quad(NamedNode(e['uri']), NamedNode(RDF_TYPE), NamedNode(e['classUri']), graph),
                Quad(NamedNode(e['uri']), NamedNode(NS + 'name'), Literal(e['text']), graph),
            ])
            if e.get('tags'):
                quads.append(Quad(NamedNode(e['uri']), NamedNode(NS + 'tags'),
                                  Literal(json.dumps(e['tags'], ensure_ascii=False)), graph))
            if e.get('canonicalName'):
                quads.append(Quad(NamedNode(e['uri']), NamedNode(NS + 'canonicalName'),
                                  Literal(e['canonicalName']), graph))
            if 'regionLevel' in e:
                quads.append(Quad(NamedNode(e['uri']), NamedNode(NS + 'regionLevel'),
                                  Literal(e['regionLevel']), graph))
            if 'identityStatus' in e:
                quads.extend([
                    Quad(NamedNode(e['uri']), NamedNode(NS + 'identityStatus'), Literal(e['identityStatus']), graph),
                    Quad(NamedNode(e['uri']), NamedNode(NS + 'identityHints'), Literal(json.dumps(e['identityHints'], ensure_ascii=False)), graph),
                ])
        for r in relations:
            edge = dict(subject=by_text[r.subject], predicate=schema.predicates[r.predicate]['uri'],
                        object=by_text[r.object], subjectText=r.subject, label=r.predicate, objectText=r.object)
            edges.append(edge)
            quads.append(Quad(NamedNode(edge['subject']), NamedNode(edge['predicate']), NamedNode(edge['object']), graph))
        result = dict(id=extraction_id, graphUri=graph_uri, schemaId=schema.fingerprint,
                      entities=nodes, relations=edges, models=models, statistics=statistics,
                      provenance=provenance, createdAt=datetime.now(timezone.utc).isoformat())
        for key, value in provenance.items():
            quads.append(Quad(head, NamedNode(NS + key), Literal(
                value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)), graph))
        quads.extend([
            Quad(head, NamedNode(NS + 'schema'), schema_graph, graph),
            Quad(head, NamedNode(NS + 'result'), Literal(json.dumps(result, ensure_ascii=False)), graph),
        ])

        self.store.extend(quads)
        self.store.flush()
        return result

    @synchronized
    def close(self):
        if self.store is not None:
            try:
                self.store.flush()
            finally:
                self.store = None
