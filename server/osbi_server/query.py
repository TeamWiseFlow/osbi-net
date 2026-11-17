"""Bounded graph views and original evidence from the sole store owner."""
from collections import deque
import time


def evidence_page(service, channel_id, request):
    """Page authorized original evidence using generic date/entity/text filters."""
    as_of = min(request.asOf if request.asOf is not None else time.time(), time.time())
    rows = service.repo.rows("""SELECT * FROM knowledge_units WHERE channel=? AND logical_id>? AND updated<=?
                               ORDER BY logical_id LIMIT ?""", (channel_id, request.cursor, as_of, request.limit))
    items = []
    for row in rows:
        engine = service.engine(channel_id, row["schema_id"])
        result = engine.store.read(row["extraction_id"])
        if not result:
            continue
        p = result["provenance"]
        published = p.get("published_date", "")
        if ((request.fromDate or request.toDate) and not published) or (request.fromDate and published < request.fromDate) or (request.toDate and published > request.toDate):
            continue
        if request.entityUris and not {n["uri"] for n in result["entities"]}.intersection(request.entityUris):
            continue
        if request.text and request.text.casefold() not in (p["source_title"] + "\n" + p["raw_text"]).casefold():
            continue
        sources = service.repo.sources(channel_id, row["logical_id"])
        items.append(dict(id=result["id"], evidenceId=row["logical_id"], schemaId=result["schemaId"],
            text=p["raw_text"], title=p["source_title"], publishedDate=published,
            qualityFlags=p.get("quality_flags", []),
            sources=[{"title": s["title"], "meta": s["meta"]} for s in sources] or [{"title": p["source_title"], "meta": p["source_meta"]}],
            entities=[dict(uri=n["uri"], name=n["text"], label=n["label"],
                           ancestors=sorted(engine.schema.ancestry.get(n["label"], set()))) for n in result["entities"]],
            relations=[dict(subject=e["subject"], object=e["object"], label=e["label"]) for e in result["relations"]]))
    return dict(items=items, cursor=rows[-1]["logical_id"] if len(rows) == request.limit else "", asOf=as_of)


def graph(service, channel_id, limit=500):
    nodes, edges, blocks = {}, {}, {}
    rows = service.repo.rows("SELECT * FROM knowledge_units WHERE channel=? ORDER BY updated DESC LIMIT 2000", (channel_id,))
    for row in rows:
        store = service.engine(channel_id, row["schema_id"]).store
        result = store.read(row["extraction_id"])
        if result is None:
            continue
        block_id = result["id"]
        observations = service.repo.sources(channel_id, row["logical_id"])
        blocks[block_id] = {"id": block_id, "text": result["provenance"]["raw_text"],
                            "sources": observations, "schemaId": result["schemaId"],
                            "statistics": result["statistics"]}
        for n in result["entities"]:
            item = nodes.setdefault(n["uri"], {"id": n["uri"], "name": n["text"], "label": n["label"],
                                             "identityStatus": n.get("identityStatus", "source_asserted"), "blocks": []})
            if block_id not in item["blocks"]:
                item["blocks"].append(block_id)
        for e in result["relations"]:
            key = (e["subject"], e["predicate"], e["object"])
            item = edges.setdefault(key, {"source": e["subject"], "target": e["object"], "label": e["label"], "blocks": []})
            if block_id not in item["blocks"]:
                item["blocks"].append(block_id)
    selected = sorted(nodes.values(), key=lambda n: (-len(n["blocks"]), n["name"]))[:limit]
    selected_ids = {n["id"] for n in selected}
    return {"channelId": channel_id, "schemaId": service.repo.channel(channel_id)["schema_id"],
            "nodes": selected, "edges": [e for e in edges.values() if e["source"] in selected_ids and e["target"] in selected_ids],
            "blocks": blocks, "truncated": len(nodes) > limit or len(rows) == 2000,
            "status": service.status()}


def traverse(data, source, target=None, depth=1, limit=200):
    nodes = {n["id"]: n for n in data["nodes"]}
    if source not in nodes:
        candidates = [n for n in nodes.values() if source.casefold() in n["name"].casefold()]
        return {"candidates": candidates[:50], "nodes": [], "edges": [], "paths": [], "ambiguous": len(candidates) > 1}
    adjacency = {i: [] for i in nodes}
    for e in data["edges"]:
        adjacency[e["source"]].append((e["target"], e))
        adjacency[e["target"]].append((e["source"], e))
    reached, selected_edges, paths = {source}, {}, []
    queue = deque([(source, [source])])
    while queue and len(reached) < limit:
        current, path = queue.popleft()
        if len(path) > depth:
            continue
        for neighbor, edge in adjacency[current]:
            if neighbor in path:
                continue
            selected_edges[(edge["source"], edge["label"], edge["target"])] = edge
            if target == neighbor:
                paths.append(path + [neighbor])
            if neighbor not in reached and len(reached) < limit:
                reached.add(neighbor)
                queue.append((neighbor, path + [neighbor]))
    if target:
        reached = {n for p in paths for n in p}
    return {"nodes": [nodes[i] for i in reached],
            "edges": [e for e in selected_edges.values() if e["source"] in reached and e["target"] in reached],
            "paths": paths[:50], "truncated": data["truncated"] or len(reached) >= limit}
