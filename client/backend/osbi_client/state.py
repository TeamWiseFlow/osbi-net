"""Private local state, monotonic events and durable acquisition runs."""
import json
import secrets
import time
from osbi_contracts.storage import Database, encode
from .models import ClientConfig


class State(Database):
    def __init__(self, path):
        super().__init__(path)
        self.script("""
        CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1),body TEXT NOT NULL,revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY CHECK(id=1),client_id TEXT NOT NULL,local_token TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,kind TEXT NOT NULL,
          level TEXT NOT NULL,message TEXT NOT NULL,run_id TEXT NOT NULL DEFAULT '',details TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,source TEXT NOT NULL,config TEXT NOT NULL,state TEXT NOT NULL,
          step INTEGER NOT NULL DEFAULT 0,queue TEXT NOT NULL,visited TEXT NOT NULL DEFAULT '[]',
          browser TEXT NOT NULL DEFAULT '{}',prompt TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',created REAL NOT NULL,updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,
          remote_id TEXT NOT NULL DEFAULT '',result TEXT NOT NULL DEFAULT '{}',attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL DEFAULT 0,server_url TEXT NOT NULL,server_ca TEXT NOT NULL DEFAULT '',credential_generation INTEGER NOT NULL,error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS schedules(source_id TEXT PRIMARY KEY,next_at REAL NOT NULL,interval_seconds INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS domain_cooldowns(domain TEXT PRIMARY KEY,next_at REAL NOT NULL,strikes INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS weekly_hotspots (
          customer TEXT NOT NULL,channel TEXT NOT NULL,profile TEXT NOT NULL,
          week_end TEXT NOT NULL,board TEXT NOT NULL,uri TEXT NOT NULL,name TEXT NOT NULL,
          label TEXT NOT NULL,article_ids TEXT NOT NULL,topic TEXT NOT NULL,created REAL NOT NULL,
          PRIMARY KEY(customer,channel,profile,week_end,board,uri));
        CREATE TABLE IF NOT EXISTS weekly_plans (
          id TEXT PRIMARY KEY,customer TEXT NOT NULL,channel TEXT NOT NULL,
          body TEXT NOT NULL,created REAL NOT NULL);
        PRAGMA user_version=1;
        """)
        with self.transaction() as conn:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(outbox)")}
            if "links_applied" not in columns:
                conn.execute("ALTER TABLE outbox ADD COLUMN links_applied INTEGER NOT NULL DEFAULT 0")
            for name in ("cache_key", "remote_ids"):
                if name not in columns:
                    conn.execute(f"ALTER TABLE outbox ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        self.execute("INSERT OR IGNORE INTO config VALUES (1,?,1)", (encode(ClientConfig().model_dump()),))
        self.execute("INSERT OR IGNORE INTO identity VALUES (1,?,?)", ("c_" + secrets.token_hex(12), secrets.token_urlsafe(32)))

    def config(self):
        return ClientConfig.model_validate(json.loads(self.one("SELECT body FROM config WHERE id=1")["body"]))

    def public_config(self):
        row = self.one("SELECT * FROM config WHERE id=1")
        body = self.config().model_dump()
        key = body.pop("serverKey")
        jev_key = body["jev"].pop("apiKey")
        body["jev"].update(keyConfigured=bool(jev_key), keySuffix=jev_key[-4:] if jev_key else "")
        vision_key = body["vision"].pop("apiKey")
        body["vision"].update(keyConfigured=bool(vision_key), keySuffix=vision_key[-4:] if vision_key else "")
        report_key = body["reportLlm"].pop("apiKey")
        body["reportLlm"].update(keyConfigured=bool(report_key), keySuffix=report_key[-4:] if report_key else "")
        return {**body, "keyConfigured": bool(key), "keySuffix": key[-4:] if key else "", "revision": row["revision"]}

    def save_config(self, update):
        body = self.config().model_dump()
        update = dict(update)
        for name in ("jev", "vision", "reportLlm", "captureCache", "submissionCache", "domainConcurrency"):
            if name in update:
                # Legacy int domainConcurrency replaces the whole policy.
                if name == "domainConcurrency" and isinstance(update[name], int):
                    continue
                if not isinstance(update[name], dict):
                    raise ValueError(f"{name} settings require a JSON object")
                update[name] = {**body[name], **update[name]}
        body.update(update)
        validated = ClientConfig.model_validate(body)
        with self.transaction() as conn:
            conn.execute("UPDATE config SET body=?,revision=revision+1 WHERE id=1", (encode(validated.model_dump()),))
            for source in validated.sources:
                if source.enabled and source.intervalMinutes:
                    conn.execute("INSERT INTO schedules VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET next_at=CASE WHEN interval_seconds!=excluded.interval_seconds THEN excluded.next_at ELSE next_at END,interval_seconds=excluded.interval_seconds",
                                 (source.id, time.time() + source.intervalMinutes * 60, source.intervalMinutes * 60))
            allowed = [s.id for s in validated.sources if s.enabled and s.intervalMinutes]
            for row in conn.execute("SELECT source_id FROM schedules").fetchall():
                if row[0] not in allowed:
                    conn.execute("DELETE FROM schedules WHERE source_id=?", (row[0],))
            conn.execute("UPDATE outbox SET state='pending',next_at=0,error='' WHERE state='blocked'")
        self.event("config_saved", "设置已保存")
        return self.public_config()

    def event(self, kind, message, *, run_id="", level="info", details=None):
        self.execute("INSERT INTO events(at,kind,level,message,run_id,details) VALUES (?,?,?,?,?,?)",
                     (time.time(), kind, level, message, run_id, encode(details or {})))
        self.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-10000 FROM events)")

    def events(self, after=0, limit=100):
        rows = self.rows("SELECT * FROM events WHERE id>? ORDER BY id LIMIT ?", (after, limit))
        return [{**r, "details": json.loads(r["details"])} for r in rows]

    def run(self, run_id):
        row = self.one("SELECT * FROM runs WHERE id=?", (run_id,))
        if row:
            row = {k: v for k, v in row.items() if k not in {"config", "browser"}}
            row["source"] = json.loads(row["source"])
            row["queue"] = json.loads(row["queue"])
            row["visited"] = json.loads(row["visited"])
        return row
