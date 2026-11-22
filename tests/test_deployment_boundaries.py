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
blocked = ({"osbi_server", "wiseflow_server", "rdflib", "pyoxigraph"} if role == "client" else
           {"osbi_client", "mcp", "bs4", "feedparser"})
attempted = []
class BlockPeer(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in blocked:
            attempted.append(fullname)
            raise ImportError("Peer dependency is unavailable: " + fullname)
sys.meta_path.insert(0, BlockPeer())
modules = (["osbi_client.app", "osbi_client.cli", "osbi_client.mcp"] if role == "client"
           else ["wiseflow_server.app"])
for name in modules:
    importlib.import_module(name)
assert not attempted, attempted
print(role + " imports independently")
'''
    project = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "OSBI_CLIENT_DATA": str(tmp_path / "client-state"),
                   "WISEFLOW_WORK_DIR": str(tmp_path / "server-state"),
                   "PYTHONPATH": os.pathsep.join(str(project / part)
                                                 for part in ("contracts", "server", "client/backend"))}
    result = subprocess.run([sys.executable, "-c", script, role], env=environment, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == role + " imports independently"
    assert not Path(environment["OSBI_CLIENT_DATA"]).exists()
    if role == "client":
        # the wiseflow server creates its work dir at import; the client must not
        assert not Path(environment["WISEFLOW_WORK_DIR"]).exists()
