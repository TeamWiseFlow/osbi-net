"""Real MCP stdio discovery must not launch or create the local backend."""
import asyncio
import os
import sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_stdio_discovery_has_no_backend_side_effects(tmp_path):
    home=tmp_path/'absent-client'
    async def run():
        params=StdioServerParameters(command=sys.executable,args=['-c','from osbi_client.mcp import main; main()'],
                                    env={**os.environ,'OSBI_CLIENT_DATA':str(home)})
        async with stdio_client(params) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                tools=await session.list_tools()
                assert {'settings','configure','channels','query','start_source','runs','resume','cancel','browser_action'} <= {t.name for t in tools.tools}
        assert not home.exists()
    asyncio.run(run())
