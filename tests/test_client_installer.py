"""Exercise installation wiring and verified runtime recovery with small offline fixtures."""
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import pytest

PROJECT = Path(__file__).resolve().parents[1]
HELPERS = PROJECT / "scripts/client-runtime.sh"


def shell(script, env=None):
    return subprocess.run(["bash", "-eu", "-c", script], env={**os.environ, **(env or {})},
                          capture_output=True, text=True)


def executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/bash\n" + text)
    path.chmod(0o755)


def test_help_and_unsupported_adapter_do_not_install_anything():
    for args, code in ((["--help"], 0), (["--agent", "codex"], 2), (["--agent"], 2)):
        result = subprocess.run(["bash", str(PROJECT / "scripts/install-client.sh"), *args], capture_output=True, text=True)
        assert result.returncode == code


@pytest.mark.parametrize("with_skills", [False, True])
def test_one_command_installs_core_browser_and_optional_skills_without_launching_host(tmp_path, with_skills):
    root = tmp_path / "checkout with spaces"
    shutil.copytree(PROJECT / "scripts", root / "scripts")
    binaries = tmp_path / "bin"
    log = tmp_path / "calls"
    executable(binaries / "node", 'if [[ "$1" == "-e" ]]; then exit 0; fi\nprintf "browser-assets\\n" >> "$INSTALL_LOG"\n')
    for name in ("npm", "npx"):
        executable(binaries / name, f'printf "{name} %s\\n" "$*" >> "$INSTALL_LOG"\n')
    executable(root / ".venv/bin/python", 'if [[ "$1" == "-I" ]]; then exit 0; fi\nprintf "python %s\\n" "$*" >> "$INSTALL_LOG"\n')
    environment = {**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "INSTALL_LOG": str(log)}
    options = ["--agent", "dsh", "--dsh-home", str(tmp_path / "host")] if with_skills else []
    for _ in range(2):
        result = subprocess.run(["bash", str(root / "scripts/install-client.sh"), *options],
                                env=environment, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    lines = log.read_text().splitlines()
    assert sum(line == "browser-assets" for line in lines) == 2
    assert sum("npm ci --ignore-scripts" in line for line in lines) == 2
    assert sum("npm run build" in line for line in lines) == 2
    assert sum("-m pip install" in line for line in lines) == 2
    assert sum("install-dsh.py --home" in line for line in lines) == (2 if with_skills else 0)
    assert not any("npx " in line for line in lines)
    assert (root / ".osbi/bin/node").resolve() == binaries / "node"
    assert not (root / ".osbi/install.lock").exists()


@pytest.mark.parametrize("runtime", ["node", "python"])
def test_missing_runtime_uses_verified_cached_archive_and_survives_retry(tmp_path, runtime):
    root = tmp_path / "project"
    (root / ".osbi/runtime").mkdir(parents=True)
    (root / ".osbi/bin").mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    if runtime == "node":
        archive_name = "node-v24.22.3-linux-x64.tar.gz"
        base = "node-v24.22.3-linux-x64"
        executable(tmp_path / "contents" / base / "bin/node", "exit 0\n")
        for name in ("npm", "npx"):executable(tmp_path / "contents" / base / "bin" / name, "exit 0\n")
        pin = ""
    else:
        base = "python"
        target = "x86_64-unknown-linux-gnu"
        archive_name = f"cpython-3.12.16+20261026-{target}-install_only_stripped.tar.gz"
        executable(tmp_path / "contents/python/bin/python3", "exit 0\n")
        pin = target + " "
    archive = cache / archive_name
    with tarfile.open(archive, "w:gz") as bundle:bundle.add(tmp_path / "contents" / base, arcname=base)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    # Override only the fixture pin and host usability: production verification/extraction remain real.
    prefix = f'source {shlex.quote(str(HELPERS))}\nosbi_runtime_pin() {{ printf "%s" {shlex.quote(pin + checksum)}; }}\n'
    if runtime == "python":
        prefix += 'osbi_python_usable() { [[ "$1" == *"/.osbi/runtime/"* && -x "$1" ]]; }\n'
    else:
        prefix += 'osbi_node_usable() { [[ "$1" == *"/.osbi/runtime/"* && -x "$1" ]]; }\n'
    call = f'osbi_prepare_{runtime} {shlex.quote(str(root))} linux-x64\n'
    result = shell(prefix + call + call, {"OSBI_RUNTIME_DOWNLOADS": str(cache), "OSBI_BROWSER_NODE": "/missing"})
    assert result.returncode == 0, result.stderr
    assert len(list((root / ".osbi/runtime").iterdir())) == 1
    archive.write_bytes(b"corrupt archive")
    shutil.rmtree(next((root / ".osbi/runtime").iterdir()))
    result = shell(prefix + call, {"OSBI_RUNTIME_DOWNLOADS": str(cache), "OSBI_BROWSER_NODE": "/missing"})
    assert result.returncode != 0 and "SHA-256 mismatch" in result.stderr
    assert list((root / ".osbi/runtime").iterdir()) == []


def test_existing_broken_environment_is_not_overwritten(tmp_path):
    (tmp_path / ".venv").mkdir()
    marker = tmp_path / ".venv/keep"
    marker.write_text("existing state")
    result = shell(f'source {shlex.quote(str(HELPERS))}\nosbi_prepare_python {shlex.quote(str(tmp_path))} linux-x64')
    assert result.returncode != 0 and "Existing .venv" in result.stderr
    assert marker.read_text() == "existing state"


def test_skill_binding_does_not_replace_host_configuration(tmp_path):
    config = tmp_path / "cordis.yml"
    config.write_text("owned by host\n")
    result = subprocess.run([sys.executable, str(PROJECT / "scripts/install-dsh.py"), "--home", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert config.read_text() == "owned by host\n"
    assert (tmp_path / "skills/osbi-manage/SKILL.md").is_file()
    assert (tmp_path / "skills/osbi-query/SKILL.md").is_file()
