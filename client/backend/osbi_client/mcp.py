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
    """Read current local settings, without returning the customer key."""
    return await call("GET", "/api/config")


@mcp.tool()
async def configure(update: dict[str, Any]) -> dict:
    """Update explicit local acquisition/workflow settings; secrets are not echoed."""
    return await call("PATCH", "/api/config", update)


@mcp.tool()
async def channels() -> dict:
    """Discover eligible shared channels and their active schema revisions."""
    return await call("GET", "/api/server/channels")


@mcp.tool()
async def query(channel: str, source: str, target: str | None = None, depth: int = 1) -> dict:
    """Find an entity or inspect evidence-backed neighborhoods/paths; ambiguous names return candidates."""
    return await call("POST", f"/api/server/channels/{channel}/query", {"source": source, "target": target, "depth": depth})


@mcp.tool()
async def start_source(source_id: str) -> dict:
    """Start a configured source run and return its durable run ID."""
    return await call("POST", "/api/runs", {"sourceId": source_id})


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
