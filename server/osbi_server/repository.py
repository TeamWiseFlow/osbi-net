"""Durable request, evidence, key, channel and graph-intent records."""
from datetime import datetime, timezone
import hashlib
import json
import secrets
import time
from pathlib import Path
from osbi_contracts.storage import Database, digest, encode
from osbi_contracts.material import map_result


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
        CREATE TABLE IF NOT EXISTS knowledge_units (
          channel TEXT NOT NULL,logical_id TEXT NOT NULL,extraction_id TEXT NOT NULL,schema_id TEXT NOT NULL,
          original TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,
          PRIMARY KEY(channel,logical_id));
        CREATE INDEX IF NOT EXISTS units_extraction ON knowledge_units(channel,extraction_id);
        CREATE TABLE IF NOT EXISTS unit_sources (
          channel TEXT NOT NULL,logical_id TEXT NOT NULL,source_id TEXT NOT NULL,body TEXT NOT NULL,
          first_seen REAL NOT NULL,last_seen REAL NOT NULL,observations INTEGER NOT NULL DEFAULT 1,
          PRIMARY KEY(channel,logical_id,source_id));
        CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY,completed REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS schema_activity (id TEXT PRIMARY KEY,last_seen REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS requests_age ON requests(created);
        CREATE INDEX IF NOT EXISTS work_age ON work(state,updated);
        PRAGMA user_version=2;
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

    def submit(self, material, canonical, work_id, customer, *, request_limit=50000):
        payload = material.model_dump()
        payload.pop("materialHash", None)
        payload_hash = digest(payload)
        envelope = self.envelope(payload)
        envelope["eligible"] = [{"id": item["id"]} for item in canonical["eligible"]]
        with self.transaction() as conn:
            old = conn.execute("SELECT * FROM requests WHERE customer=? AND client_id=? AND request_key=?",
                               (customer, material.clientId, material.requestId)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise Conflict("Idempotency key already belongs to different material")
                return dict(old), True
            if conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= request_limit:
                raise OverflowError("Server request retention capacity is full")
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
                         (request_id, customer, material.clientId, material.requestId, payload_hash, encode(envelope), work_id, now))
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
                unit = conn.execute("SELECT logical_id FROM knowledge_units WHERE channel=? AND extraction_id=?",
                                    (graph["channelId"], graph["id"])).fetchone()
                if not unit:
                    continue
                for request in conn.execute("SELECT * FROM requests WHERE work_id=?", (work_id,)).fetchall():
                    added = conn.execute("INSERT OR IGNORE INTO graph_sources VALUES (?,?,?)",
                                         (graph["channelId"], graph["id"], request["id"])).rowcount
                    if added:
                        self.merge_source(conn, graph["channelId"], unit["logical_id"],
                                          json.loads(request["payload"]), request["created"])

    @staticmethod
    def envelope(body):
        """Keep caller IDs and provenance, without retaining another copy of original text."""
        return {"title": body["title"], "meta": body["meta"],
                "blockIds": body.get("blockIds", [b["id"] for b in body.get("blocks", [])]),
                "linkIds": body.get("linkIds", [b["id"] for b in body.get("links", [])]),
                **({"eligible": body["eligible"]} if "eligible" in body else {})}

    @staticmethod
    def merge_source(conn, channel, logical_id, body, seen):
        meta = body["meta"]
        url = meta.get("canonicalUrl") or meta.get("finalUrl") or meta.get("requestedUrl", "")
        source_id = digest([body["title"], url, *[meta.get(k, "") for k in ("author", "publisher", "publishedAt")]])
        source = encode({"title": body["title"], "meta": meta})
        conn.execute("""INSERT INTO unit_sources VALUES (?,?,?,?,?,?,1)
            ON CONFLICT(channel,logical_id,source_id) DO UPDATE SET
            body=CASE WHEN excluded.last_seen>=last_seen THEN excluded.body ELSE body END,
            first_seen=MIN(first_seen,excluded.first_seen),last_seen=MAX(last_seen,excluded.last_seen),
            observations=observations+1""", (channel, logical_id, source_id, source, seen, seen))

    def save_unit(self, channel, logical_id, extraction_id, schema_id, original, *, updated=None):
        now = time.time() if updated is None else updated
        previous = self.one("SELECT schema_id FROM knowledge_units WHERE channel=? AND logical_id=?", (channel, logical_id))
        for schema in {schema_id, previous['schema_id'] if previous else schema_id}:
            self.touch_schema(schema, now)
        self.execute("""INSERT INTO knowledge_units VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(channel,logical_id) DO UPDATE SET extraction_id=excluded.extraction_id,
            schema_id=excluded.schema_id,original=excluded.original,updated=excluded.updated""",
            (channel, logical_id, extraction_id, schema_id, encode(original), now, now))

    def touch_schema(self, schema, now):
        self.execute("INSERT INTO schema_activity VALUES (?,?) ON CONFLICT(id) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)", (schema, now))

    def sources(self, channel, logical_id):
        return [json.loads(r["body"]) for r in self.rows(
            "SELECT body FROM unit_sources WHERE channel=? AND logical_id=? ORDER BY first_seen,source_id", (channel, logical_id))]

    def compact_work(self, work_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT payload,state FROM work WHERE id=?", (work_id,)).fetchone()
            if row and row["state"] == "succeeded":
                payload = json.loads(row["payload"])
                compact = {"eligible": [{"id": item["id"]} for item in payload.get("eligible", [])],
                           "materialHash": payload.get("materialHash", ""),
                           "processingRevision": payload.get("processingRevision", ""), "compacted": True}
                conn.execute("UPDATE work SET payload=? WHERE id=?", (encode(compact), work_id))

    def compact_requests(self):
        """Upgrade legacy request envelopes before any historical work can be removed."""
        with self.transaction() as conn:
            for row in conn.execute("SELECT id,payload,work_id FROM requests").fetchall():
                body = json.loads(row["payload"])
                if "blocks" in body:
                    envelope = self.envelope(body)
                    work = conn.execute("SELECT payload FROM work WHERE id=?", (row["work_id"],)).fetchone()
                    eligible = json.loads(work["payload"]).get("eligible", []) if work else []
                    envelope["eligible"] = [{"id": item["id"]} for item in eligible]
                    conn.execute("UPDATE requests SET payload=? WHERE id=?", (encode(envelope), row["id"]))

    def update_work(self, work_id, state, result=None, error="", retry_delay=0):
        now = time.time()
        self.execute("UPDATE work SET state=?,result=COALESCE(?,result),error=?,lease=0,next_at=?,updated=? WHERE id=?",
                     (state, encode(result) if result is not None else None, error, now + retry_delay, now, work_id))

    def work_result(self, row, request, reused=False):
        body = json.loads(request["payload"])
        result = map_result(json.loads(row["result"]), body)
        return {"jobId": request["id"], "state": row["state"], "reused": reused,
                "error": row["error"], "updatedAt": row["updated"], **result}
