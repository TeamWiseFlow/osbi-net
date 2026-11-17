"""Small transactional SQLite boundary shared by independent deployments."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
import hashlib


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


class Database:
    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.lock = threading.RLock()
        new_database = not path.exists()
        self.connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
        os.chmod(path, 0o600)
        self.connection.row_factory = sqlite3.Row
        if new_database:
            self.connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=30000")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.connection.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.rows(sql, args)
        return rows[0] if rows else None

    def execute(self, sql, args=()):
        with self.lock:
            return self.connection.execute(sql, args)

    def script(self, text):
        with self.lock:
            self.connection.executescript(text)

    def close(self):
        with self.lock:
            self.connection.close()
