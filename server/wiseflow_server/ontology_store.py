"""One Oxigraph owner; atomic per-original-block graphs and durable receipts."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from pyoxigraph import BlankNode, Literal, NamedNode, Quad, Store
from rdflib import BNode, Graph, Literal as RDFLiteral, URIRef
from rdflib.namespace import RDF

from .ontology_rules import canonical_entity_name, geo_level

NS = 'https://onto.pilot.local/policy#'
BLOCK_BASE = 'https://onto.pilot.local/block/'
SCHEMA_BASE = 'https://onto.pilot.local/ontology/'
INSTANCE_BASE = 'https://onto.pilot.local/instance/'
RDF_TYPE = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#type'
SEED_GRAPH = BLOCK_BASE + '_seeds_shanghai'
DEFAULT_SEEDS = Path(__file__).with_name('schemas') / 'seeds_shanghai.ttl'
_SEED_CLASS_LEVEL = {'NationalGov': ('国家级机构', 0), 'ProvinceGov': ('省级机构', 1),
                     'CityGov': ('市级机构', 2), 'DistrictGov': ('区级机构', 3),
                     'StreetGov': ('街镇机构', 4)}


def uri_for(text, scope=None):
    # Optional scope remains available for reading historical source identities.
    identity = text.strip() if scope is None else json.dumps([text.strip(), scope], ensure_ascii=False)
    return INSTANCE_BASE + hashlib.sha1(identity.encode()).hexdigest()[:12]


def rdf_term(term):
    if isinstance(term, BNode):
        return BlankNode(str(term))
    if isinstance(term, RDFLiteral):
        return Literal(str(term), language=term.language) if term.language else Literal(
            str(term), datatype=NamedNode(str(term.datatype))) if term.datatype else Literal(str(term))
    return NamedNode(str(term))


class OntologyStore:
    def __init__(self, path):
        self.path = path.resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.store = Store(str(self.path))

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

    def supersede(self, old_id, new_id):
        if old_id == new_id:
            return
        if self.read(new_id) is None:
            raise ValueError('Replacement graph is not complete')
        graph = NamedNode(BLOCK_BASE + old_id)
        # Preserve original facts for audit; public queries use active graphs.
        self.store.extend([Quad(NamedNode(graph.value + '/source'), NamedNode(NS + 'supersededBy'),
                                NamedNode(BLOCK_BASE + new_id), graph)])
        self.store.flush()

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
            node = dict(text=e.text, label=e.label, uri=uri_for(e.text), classUri=schema.classes[e.label],
                        canonicalName=canonical_entity_name(e.text, e.label),
                        tags=[dict(dimension=d, value=v) for d, v in getattr(e, 'tags', ())])
            level = geo_level(e.text, e.label)
            if level is not None:
                node['regionLevel'] = level
            if person:
                node.update(identityStatus='name_merged', identityHints=qualifiers)
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
        # extend is transactional: provenance/result and graph facts commit together.
        self.store.extend(quads)
        self.store.flush()
        return result

    def load_seeds(self, path=None):
        """Load the deterministic administrative skeleton into a standalone graph.

        Seeds are idempotent and never superseded. Their instance URIs use the
        same uri_for(text) scheme as extracted nodes, so 上海市/静安区/临汾路街道
        merge with article entities and supply the missing partOf chain.
        """
        path = Path(path or DEFAULT_SEEDS)
        if not path.is_file():
            return 0
        graph = Graph().parse(data=path.read_text(encoding='utf-8'), format='turtle')
        seed_graph = NamedNode(SEED_GRAPH)
        quads = []
        for subject, predicate, obj in graph:
            quads.append(Quad(rdf_term(subject), rdf_term(predicate), rdf_term(obj), seed_graph))
        name_predicate = URIRef(NS + 'name')
        for subject, name in graph.subject_objects(name_predicate):
            value = str(name)
            quads.append(Quad(rdf_term(subject), NamedNode(NS + 'canonicalName'),
                              Literal(value), seed_graph))
            quads.append(Quad(rdf_term(subject), NamedNode(NS + 'seedScope'),
                              Literal('shanghai'), seed_graph))
            class_node = next(graph.objects(subject, RDF.type), None)
            short = str(class_node).rsplit('#', 1)[-1] if class_node is not None else ''
            entry = _SEED_CLASS_LEVEL.get(short)
            if entry is not None:
                quads.append(Quad(rdf_term(subject), NamedNode(NS + 'regionLevel'),
                                  Literal(entry[1]), seed_graph))
        self.store.extend(quads)
        self.store.flush()
        return len(quads)

    def close(self):
        if self.store is not None:
            try:
                self.store.flush()
            finally:
                self.store = None
