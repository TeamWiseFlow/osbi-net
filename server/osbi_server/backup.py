"""Offline consistent snapshots with a verified restore manifest."""
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from osbi_contracts.instance import InstanceLock


def fingerprints(root):
    values = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("Snapshot must not contain symbolic links")
        if path.is_file() and path.name != "snapshot.json":
            with path.open("rb") as handle:
                values[str(path.relative_to(root))] = hashlib.file_digest(handle, "sha256").hexdigest()
    return values


def backup(data, target):
    data, target = Path(data).resolve(), Path(target).resolve()
    if target == data or data in target.parents or target.exists():
        raise ValueError("Backup target must be a new directory outside server state")
    databases = sorted(data.rglob("metadata.db"))
    if not databases:
        raise ValueError("Server state does not exist")
    lock = InstanceLock(data / "server.lock")
    lock.acquire()
    try:
        shutil.copytree(data, target, symlinks=True, ignore=shutil.ignore_patterns("metadata.db*", "server.lock"))
        for database in databases:
            output = target / database.relative_to(data)
            output.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(database) as source, sqlite3.connect(output) as destination:
                source.backup(destination)
            output.chmod(0o600)
        (target / "snapshot.json").write_text(json.dumps({"version": 1, "files": fingerprints(target)}, indent=2))
        return {"snapshot": str(target), "files": len(fingerprints(target))}
    finally:
        lock.release()


def restore(snapshot, target):
    snapshot, target = Path(snapshot).resolve(), Path(target).resolve()
    if target.exists() or snapshot in target.parents:
        raise ValueError("Restore target must be a new directory outside the snapshot")
    manifest = json.loads((snapshot / "snapshot.json").read_text())
    if manifest.get("version") != 1 or manifest.get("files") != fingerprints(snapshot):
        raise ValueError("Snapshot manifest verification failed")
    if not any(Path(name).name == "metadata.db" for name in manifest["files"]):
        raise ValueError("Snapshot has no metadata database")
    shutil.copytree(snapshot, target, ignore=shutil.ignore_patterns("snapshot.json"))
    return {"restored": str(target), "files": len(manifest["files"])}
