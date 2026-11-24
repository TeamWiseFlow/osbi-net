"""Translate the local osbi-style server API onto wiseflow server endpoints.

The dashboard and MCP tools keep their local paths; this module maps them to
/v1/ontology/* (libraries, graph, query, entities) and /v1/libraries/{id}/evidence,
and adapts the viewer-shaped graph data to the local dashboard envelope.
"""
from datetime import datetime
import re

from .server_client import ServerError, request as server_request

CHANNEL_PATH = re.compile(r"channels/([a-z][a-z0-9_-]{0,63})/(graph|entities|query|evidence)")


async def _optional(config, path, transport):
    """Decoration-only calls (labels, batch status) must not break primary data."""
    try:
        return await server_request(config, "GET", path, transport=transport)
    except ServerError:
        return {}


def _labels(stats):
    classes = {c["name"]: c["label"] for c in stats.get("classes", [])}
    predicates = {p["name"]: p["label"] for p in stats.get("predicates", [])}
    return classes, predicates


def label_graph(result, stats):
    """Give viewer nodes/edges Chinese labels and empty block lists for the UI."""
    classes, predicates = _labels(stats)
    nodes = [{**n, "label": n.get("label") or classes.get(n.get("cls", ""), n.get("cls", "")),
              "blocks": n.get("blocks", [])} for n in result.get("nodes", [])]
    edges = [{**e, "label": e.get("label") or predicates.get(e.get("pred", ""), e.get("pred", ""))}
             for e in result.get("edges", [])]
    return {**result, "nodes": nodes, "edges": edges,
            "schemaId": stats.get("schemaId") or result.get("schemaId", "")}


def graph_envelope(graph, stats, batch):
    """Map wiseflow graph + stats + batch status to the local dashboard shape."""
    result = label_graph(graph, stats)
    queue = batch.get("queue", {})
    schedule = batch.get("schedule", {})
    watermark = (batch.get("lastBatch") or {}).get("finishedAt") or 0
    if isinstance(watermark, str):
        try:
            watermark = datetime.fromisoformat(watermark).timestamp()
        except ValueError:
            watermark = 0
    result["blocks"] = {}
    result["status"] = {
        "states": {"succeeded": queue.get("done", 0), "pending": queue.get("pending", 0),
                   "retry": queue.get("retrying", 0)},
        "processingMode": "batch",
        "completedWatermark": watermark,
        "graphAllowed": True,
        "batchWindow": schedule.get("time", ""),
        "timezone": schedule.get("timezone", ""),
    }
    return result


async def fetch(daemon, method, path, payload=None, query_string=""):
    config = daemon.state.config()
    transport = daemon.transport
    if path == "channels":
        return await server_request(config, "GET", "/v1/ontology/libraries", transport=transport)
    if path == "status":
        return await server_request(config, "GET", "/health", transport=transport)
    match = CHANNEL_PATH.fullmatch(path)
    if not match:
        raise ValueError("不支持的服务接口")
    channel, action = match.groups()
    if action == "evidence":
        return await server_request(config, "POST", f"/v1/libraries/{channel}/evidence", payload, transport=transport)
    if action == "query":
        body = {"source": (payload or {}).get("source", ""), "target": (payload or {}).get("target"),
                "maxHops": (payload or {}).get("depth", 1), "libraryId": channel}
        result = await server_request(config, "POST", "/v1/ontology/query", body, transport=transport)
        stats = await _optional(config, f"/v1/ontology/stats?libraryId={channel}", transport)
        return label_graph(result, stats)
    if action == "entities":
        suffix = f"?{query_string}&libraryId={channel}" if query_string else f"?libraryId={channel}"
        return await server_request(config, "GET", "/v1/ontology/entities" + suffix, transport=transport)
    graph = await server_request(config, "GET", f"/v1/ontology/graph?libraryId={channel}", transport=transport)
    stats = await _optional(config, f"/v1/ontology/stats?libraryId={channel}", transport)
    batch = await _optional(config, f"/v1/ontology/batches/status?libraryId={channel}", transport)
    return graph_envelope(graph, stats, batch)
