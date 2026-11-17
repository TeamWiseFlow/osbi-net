"""Durable request, evidence, key, channel and graph-intent records."""
from datetime import datetime, timezone
import hashlib
import json
import secrets
import time
from pathlib import Path
from osbi_contracts.storage import Database, digest, encode


class Conflict(ValueError):
    pass


class Repository(Database):
    def __init__(self, path):
        super().__init__(path)
        self.script("""
        CREATE TABLE IF NOT EXISTS api_keys (
          id TEXT PRIMARY KEY, secret_hash TEXT UNIQUE NOT NULL, customer TEXT NOT NULL,
          expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
          grants TEXT NOT NULL DEFAULT '["*"]', rpm INTEGER NOT NULL DEFAULT 300);
        CREATE TABLE IF NOT EXISTS schema_revisions (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS channels (
          id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL,
          schema_id TEXT NOT NULL REFERENCES schema_revisions(id), created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS channel_families (
          channel TEXT PRIMARY KEY REFERENCES channels(id), family TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS work (
          id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL,
          result TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0,
          lease REAL NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS requests (
          id TEXT PRIMARY KEY, customer TEXT NOT NULL, client_id TEXT NOT NULL,
          request_key TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
          work_id TEXT NOT NULL REFERENCES work(id), created REAL NOT NULL,
          UNIQUE(customer,client_id,request_key));
        CREATE TABLE IF NOT EXISTS graph_intents (
          channel TEXT NOT NULL, logical_id TEXT NOT NULL, extraction_id TEXT NOT NULL,
          work_id TEXT NOT NULL, state TEXT NOT NULL, PRIMARY KEY(channel,logical_id,extraction_id));
        CREATE TABLE IF NOT EXISTS active_graphs (
          channel TEXT NOT NULL, logical_id TEXT NOT NULL, extraction_id TEXT NOT NULL,
          work_id TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(channel,logical_id));
        CREATE TABLE IF NOT EXISTS model_cache (
          id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS graph_sources (
          channel TEXT NOT NULL,extraction_id TEXT NOT NULL,request_id TEXT NOT NULL REFERENCES requests(id),
          PRIMARY KEY(channel,extraction_id,request_id));
        CREATE TABLE IF NOT EXISTS review_records (
          channel TEXT NOT NULL,extraction_id TEXT NOT NULL,schema_id TEXT NOT NULL,work_id TEXT NOT NULL,
          body TEXT NOT NULL,PRIMARY KEY(channel,extraction_id));
        CREATE INDEX IF NOT EXISTS requests_work ON requests(work_id);
        CREATE INDEX IF NOT EXISTS work_pending ON work(state,next_at,lease);
        PRAGMA user_version=1;
        """)

    def issue_key(self, customer, expires, grants=None, rpm=300):
        if not customer or expires <= time.time() or not 1 <= rpm <= 100000:
            raise ValueError("Customer, future expiry and positive rate limit are required")
        key = "osbi_" + secrets.token_urlsafe(32)
        key_id = secrets.token_hex(8)
        self.execute("INSERT INTO api_keys(id,secret_hash,customer,expires,grants,rpm) VALUES (?,?,?,?,?,?)",
                     (key_id, hashlib.sha256(key.encode()).hexdigest(), customer, expires, encode(grants or ["*"]), rpm))
        return {"id": key_id, "key": key, "customer": customer,
                "expiresAt": datetime.fromtimestamp(expires, timezone.utc).isoformat()}

    def authenticate(self, key):
        row = self.one("SELECT * FROM api_keys WHERE secret_hash=?", (hashlib.sha256(key.encode()).hexdigest(),))
        if row and not row["revoked"] and row["expires"] > time.time():
            return {"id": row["id"], "customer": row["customer"], "grants": json.loads(row["grants"]), "rpm": row["rpm"]}
        return None

    def channel(self, channel_id):
        return self.one("SELECT * FROM channels WHERE id=?", (channel_id,))

    def create_channel(self, channel_id, title, description, schema_id, family=None):
        with self.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO channels VALUES (?,?,?,?,?)",
                         (channel_id, title, description, schema_id, time.time()))
            if family is not None:
                conn.execute("INSERT OR IGNORE INTO channel_families VALUES (?,?)", (channel_id, family))
        return self.channel(channel_id)

    def submit(self, material, canonical, work_id, customer):
        payload = material.model_dump()
        payload_hash = digest(payload)
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM requests WHERE customer=? AND client_id=? AND request_key=?",
                               (customer, material.clientId, material.requestId)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise Conflict("Idempotency key already belongs to different material")
                return dict(old), True
            count = conn.execute("SELECT COUNT(*) FROM work WHERE state NOT IN ('succeeded','failed')").fetchone()[0]
            if count >= 2000 and not conn.execute("SELECT 1 FROM work WHERE id=?", (work_id,)).fetchone():
                raise OverflowError("Server extraction queue is full")
            now = time.time()
            existing = conn.execute("SELECT 1 FROM work WHERE id=?", (work_id,)).fetchone() is not None
            conn.execute("INSERT OR IGNORE INTO work(id,payload,state,created,updated) VALUES (?,?,'pending',?,?)",
                         (work_id, encode(canonical), now, now))
            # A fresh explicit submission can retry failed work after configuration repair.
            conn.execute("UPDATE work SET state=CASE WHEN result='{}' THEN 'pending' ELSE 'graph_pending' END,attempts=0,next_at=0,error='',updated=? WHERE id=? AND state='failed'",
                         (now, work_id))
            request_id = "r_" + secrets.token_hex(16)
            conn.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?)",
                         (request_id, customer, material.clientId, material.requestId, payload_hash, encode(payload), work_id, now))
        return self.one("SELECT * FROM requests WHERE id=?", (request_id,)), existing

    def claim(self, graph_allowed):
        now = time.time()
        with self.transaction() as conn:
            row = conn.execute("""SELECT * FROM work WHERE next_at<=? AND
              ((state IN ('pending','retry') AND (result='{}' OR ?))
               OR (state='running' AND lease<? AND (result='{}' OR ?))
               OR (state='graph_pending' AND ?)) ORDER BY created LIMIT 1""", (now, graph_allowed, now, graph_allowed, graph_allowed)).fetchone()
            if row is None:
                return None
            stage = "graph" if row["state"] == "graph_pending" or json.loads(row["result"]).get("selection") is not None else "selection"
            conn.execute("UPDATE work SET state='running',lease=?,attempts=attempts+1,updated=? WHERE id=?",
                         (now + 600, now, row["id"]))
            return {**dict(row), "stage": stage}

    def attach_sources(self, work_id, graphs):
        with self.transaction() as conn:
            for graph in graphs:
                conn.execute("INSERT OR IGNORE INTO graph_sources SELECT ?,?,id FROM requests WHERE work_id=?",
                             (graph["channelId"], graph["id"], work_id))

    def update_work(self, work_id, state, result=None, error="", retry_delay=0):
        now = time.time()
        self.execute("UPDATE work SET state=?,result=COALESCE(?,result),error=?,lease=0,next_at=?,updated=? WHERE id=?",
                     (state, encode(result) if result is not None else None, error, now + retry_delay, now, work_id))

    def work_result(self, row, request, reused=False):
        body = json.loads(request["payload"])
        result = json.loads(row["result"])
        blocks = {f"B{i+1}": b["id"] for i, b in enumerate(body["blocks"])}
        links = {f"L{i+1}": b["id"] for i, b in enumerate(body["links"])}
        if "selection" in result:
            result["selection"] = {"blockIds": [blocks[x] for x in result["selection"]["blockIds"]],
                                   "linkIds": [links[x] for x in result["selection"]["linkIds"]]}
        for group in ("infos", "assignments", "graphs"):
            for item in result.get(group, []):
                item["blockId"] = blocks[item["blockId"]]
        return {"jobId": request["id"], "state": row["state"], "reused": reused,
                "error": row["error"], "updatedAt": row["updated"], **result}
