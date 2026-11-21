"""Load immutable Turtle labels and subclass-aware relation constraints."""
import hashlib
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .ontology_rules import ENTITY_HINTS, PREDICATE_HINTS

DEFAULT_SCHEMA = Path(__file__).with_name('schemas') / 'default.ttl'


class OntologySchema:
    def __init__(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Ontology schema must be nonempty Turtle text')
        self.text = text
        self.fingerprint = hashlib.sha256(text.encode()).hexdigest()
        self.graph = Graph().parse(data=text, format='turtle')
        self.classes = self._labels(OWL.Class)
        properties = self._labels(OWL.ObjectProperty)
        if not self.classes:
            raise ValueError('Ontology schema requires labeled owl:Class definitions')
        self.ancestry = {label: self._ancestors(URIRef(uri)) for label, uri in self.classes.items()}
        self.predicates = {}
        for label, uri in properties.items():
            node = URIRef(uri)
            self.predicates[label] = {
                'uri': uri,
                'domain': self._constraint(node, RDFS.domain),
                'range': self._constraint(node, RDFS.range),
            }

    def _labels(self, kind):
        result = {}
        for node in sorted(self.graph.subjects(RDF.type, kind), key=str):
            if not isinstance(node, URIRef):
                raise ValueError('Ontology classes and properties must have explicit URIs')
            labels = sorted({str(label).strip() for label in self.graph.objects(node, RDFS.label) if str(label).strip()})
            if not labels:
                raise ValueError(f'Ontology definition lacks rdfs:label: {node}')
            for label in labels:
                if label in result and result[label] != str(node):
                    raise ValueError(f'Ambiguous ontology label: {label}')
                result[label] = str(node)
        return result

    def _ancestors(self, node):
        seen, queue = set(), [node]
        while queue:
            current = queue.pop()
            if current in seen:
                continue
            seen.add(current)
            queue.extend(self.graph.objects(current, RDFS.subClassOf))
        return {label for label, uri in self.classes.items() if URIRef(uri) in seen}

    def _constraint(self, node, predicate):
        uris = set(self.graph.objects(node, predicate))
        if any(not isinstance(uri, URIRef) or str(uri) not in self.classes.values() for uri in uris):
            raise ValueError('Ontology domain/range must reference labeled classes')
        return sorted(label for label, uri in self.classes.items() if URIRef(uri) in uris)

    def prompt(self):
        return {
            'classes': [{'label': label, 'ancestors':sorted(self.ancestry[label] - {label}),
                         'hint':'；'.join(ENTITY_HINTS.get(parent, '') for parent in sorted(self.ancestry[label])
                                         if ENTITY_HINTS.get(parent))} for label in sorted(self.classes)],
            'predicates': [dict(label=label, domain=value['domain'], range=value['range'],
                                hint=PREDICATE_HINTS.get(label, ''))
                           for label, value in sorted(self.predicates.items())],
        }

    def matches(self, label, allowed):
        return not allowed or bool(self.ancestry.get(label, set()) & set(allowed))
