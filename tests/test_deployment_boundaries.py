"""Each deployment role imports without its peer or the peer's optional libraries."""
import os
from pathlib import Path
import subprocess
import sys
import pytest


@pytest.mark.parametrize("role", ["client", "server"])
def test_role_imports_with_peer_dependencies_unavailable(tmp_path, role):
    script = '''
import importlib, importlib.abc, sys
role = sys.argv[1]
blocked = ({"osbi_server", "rdflib", "pyoxigraph"} if role == "client" else
           {"osbi_client", "mcp", "bs4", "feedparser"})
attempted = []
class BlockPeer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            attempted.append(fullname)
            raise ImportError("Peer dependency is unavailable: " + fullname)
sys.meta_path.insert(0, BlockPeer())
prefix = "osbi_" + role
for suffix in (["app", "cli", "mcp"] if role == "client" else ["app", "cli", "service"]):
    importlib.import_module(prefix + "." + suffix)
assert not attempted, attempted
print(role + " imports independently")
'''
    environment = {**os.environ, "OSBI_CLIENT_DATA": str(tmp_path / "client-state"),
                   "OSBI_SERVER_DATA": str(tmp_path / "server-state")}
    result = subprocess.run([sys.executable, "-c", script, role], env=environment, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == role + " imports independently"
    assert not Path(environment["OSBI_CLIENT_DATA"]).exists()
    assert not Path(environment["OSBI_SERVER_DATA"]).exists()
