"""Side-effect-free MCP facade to the existing local daemon."""
import os
from typing import Any
from mcp.server.fastmcp import FastMCP
from .cli import call_local

mcp = FastMCP("osbi")


async def call(method, path, body=None):
    return await call_local(os.environ.get("OSBI_CLIENT_DATA", "~/.osbi/client"),
                            os.environ.get("OSBI_CLIENT_URL", "http://127.0.0.1:8781"), method, path, body)


@mcp.tool()
async def settings() -> dict:
    """Read local settings (sources, nodeRules, reportProfiles, targets) without secrets."""
    return await call("GET", "/api/config")


@mcp.tool()
async def configure(update: dict[str, Any]) -> dict:
    """Patch settings; reportProfiles carries sections/tasks/schedules/targets. Secrets are write-only."""
    return await call("PATCH", "/api/config", update)


@mcp.tool()
async def channels() -> dict:
    """Discover global server channels and their active schema revisions."""
    return await call("GET", "/api/server/channels")


@mcp.tool()
async def query(channel: str, source: str, target: str | None = None, depth: int = 1) -> dict:
    """Find an entity or inspect evidence-backed neighborhoods/paths; ambiguous names return candidates."""
    return await call("POST", f"/api/server/channels/{channel}/query", {"source": source, "target": target, "depth": depth})


@mcp.tool()
async def start_source(source_id: str, force_refresh: bool = False) -> dict:
    """Start a source run; force_refresh bypasses local page acquisition caches."""
    return await call("POST", "/api/runs", {"sourceId": source_id, "forceRefresh": force_refresh})


@mcp.tool()
async def topnews_select(channel: str, from_date: str, to_date: str, board: str = "policy",
                         profile: str = "default") -> dict:
    """Prepare a configured local TopNews board; the agent writes the final output from returned evidence."""
    return await call("POST", f"/api/topnews/{channel}/select",
                      {"fromDate": from_date, "toDate": to_date, "board": board, "profile": profile})


@mcp.tool()
async def weekly_select(channel: str, from_date: str, to_date: str, board: str = "社区治理",
                        profile: str = "default", candidate_uris: list[str] | None = None) -> dict:
    """Prepare weekly action selection locally from server queries, dropping stale repeats and ranking new progress last."""
    return await call("POST", f"/api/weekly/{channel}/select",
                      {"fromDate": from_date, "toDate": to_date, "board": board, "profile": profile,
                       "candidateUris": candidate_uris or []})


@mcp.tool()
async def weekly_record(channel: str, plan_id: str) -> dict:
    """Record actual coverage after a report succeeds; never call for a preview or failed draft."""
    return await call("POST", f"/api/weekly/{channel}/record", {"planId": plan_id})


@mcp.tool()
async def evidence(channel: str, from_date: str = "", to_date: str = "", text: str = "",
                   cursor: str = "", entity_uris: list[str] | None = None,
                   content_types: list[str] | None = None,
                   source_scope: str = "all", territory_preference: list[str] | None = None,
                   territory_mode: str = "prefer", limit: int = 100) -> dict:
    """Retrieve original evidence for reports with scope/territory/date/text filters."""
    return await call("POST", f"/api/server/channels/{channel}/evidence",
                      {"fromDate": from_date, "toDate": to_date, "text": text, "cursor": cursor,
                       "entityUris": entity_uris or [], "contentTypes": content_types or [],
                       "sourceScope": source_scope,
                       "territoryPreference": territory_preference or [],
                       "territoryMode": territory_mode, "limit": limit})


@mcp.tool()
async def runs() -> dict:
    """Read current run states and unresolved human prompts."""
    return await call("GET", "/api/runs")


@mcp.tool()
async def resume(run_id: str, prompt_id: str) -> dict:
    """Resume a current human prompt after the requested interaction is complete."""
    return await call("POST", f"/api/runs/{run_id}/resume", {"promptId": prompt_id})


@mcp.tool()
async def cancel(run_id: str) -> dict:
    """Cancel a selected acquisition run without changing other runs."""
    return await call("POST", f"/api/runs/{run_id}/cancel", {})


@mcp.tool()
async def browser_action(run_id: str, action: str, params: dict[str, Any]) -> dict:
    """Operate the existing browser for a waiting run; snapshot before using DOM action refs."""
    return await call("POST", f"/api/runs/{run_id}/browser", {"action": action, "params": params})


def main():
    mcp.run(transport="stdio")
