"""Apply the normalized read model to viewer-shaped graph/query data.

The viewer collector reads Oxigraph directly (raw merged nodes/edges). This
mapping layer redirects alias URIs onto their normalized representative,
overrides names/labels/degrees with the read-model values, removes dropped
generic hubs and adds the seed skeleton — the same view the evidence API
serves. Read-only: the store is never modified.
"""
from __future__ import annotations

from .read_model import SEED_CLASS_LABELS, uri_map

_SEED_CLASS_BY_LABEL = {label: short for short, label in SEED_CLASS_LABELS.items()}


def normalize_graph_data(data: dict, snapshot, class_by_label: dict | None = None) -> dict:
    """Return a copy of viewer data with read-model normalization applied.

    ``class_by_label`` maps Chinese class labels to schema short names so a
    relabelled node also carries the matching ``cls`` for the viewer.
    """
    class_by_label = class_by_label or {}
    mapping = uri_map(snapshot)
    nodes: dict[str, dict] = {}

    def normalized_fields(node: dict, snap: dict) -> None:
        node['name'] = snap.get('name') or node.get('name', '')
        node['label'] = snap.get('label') or node.get('label', '')
        node['canonicalName'] = snap.get('canonicalName') or node['name']
        node['degree'] = snap.get('degree', 0)
        node['regionLevel'] = snap.get('regionLevel')
        node['seed'] = bool(snap.get('seed'))
        if snap.get('qualified') is not None:
            node['qualified'] = snap['qualified']
        cls = (class_by_label.get(node['label'])
               or _SEED_CLASS_BY_LABEL.get(node['label']))
        if cls:
            node['cls'] = cls

    for raw in data.get('nodes', []):
        uri = mapping.get(raw['id'], raw['id'])
        snap = snapshot.nodes.get(uri)
        if snap is None:
            # Dropped generic hub (or a node outside the active block set).
            continue
        existing = nodes.get(uri)
        if existing is not None:
            for source in raw.get('sources', []):
                if source not in existing['sources']:
                    existing['sources'].append(source)
            for alt in raw.get('alt_cls', []):
                if alt != existing['cls'] and alt not in existing.setdefault('alt_cls', []):
                    existing['alt_cls'].append(alt)
            continue
        node = dict(raw)
        node['id'] = uri
        node['nblocks'] = len(snap.get('blockIds') or [])
        normalized_fields(node, snap)
        nodes[uri] = node

    # Seed skeleton nodes missing from the raw collection (seeds live in a
    # standalone graph and carry no extraction blocks).
    for uri, snap in snapshot.nodes.items():
        if not snap.get('seed') or uri in nodes:
            continue
        node = dict(id=uri, name=snap['name'],
                    cls=_SEED_CLASS_BY_LABEL.get(snap.get('label'), ''),
                    nblocks=0, up=[], sources=[], out_edges=0, in_edges=0)
        normalized_fields(node, snap)
        nodes[uri] = node

    edges, seen = [], set()
    for raw in data.get('edges', []):
        source = mapping.get(raw['source'], raw['source'])
        target = mapping.get(raw['target'], raw['target'])
        if source not in nodes or target not in nodes:
            continue
        key = (source, raw['pred'], target)
        if key in seen:
            continue
        seen.add(key)
        edges.append(dict(source=source, target=target, pred=raw['pred']))
    for edge in snapshot.edges.values():
        if not edge.get('seed'):
            continue
        source, target = edge['subject'], edge['object']
        if source not in nodes or target not in nodes:
            continue
        pred = str(edge['predicate']).rsplit('#', 1)[-1].rsplit('/', 1)[-1]
        key = (source, pred, target)
        if key in seen:
            continue
        seen.add(key)
        edges.append(dict(source=source, target=target, pred=pred))

    for node in nodes.values():
        node['out_edges'] = 0
        node['in_edges'] = 0
    for edge in edges:
        nodes[edge['source']]['out_edges'] += 1
        nodes[edge['target']]['in_edges'] += 1

    result = dict(data)
    result['nodes'] = list(nodes.values())
    result['edges'] = edges
    result['normalization'] = snapshot.normalization
    return result
