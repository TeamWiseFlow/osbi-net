"""Persistent bounded acquisition caches, separate from server processing identity."""
import json
import time
from osbi_contracts.material import map_result
from osbi_contracts.storage import digest, encode
from osbi_contracts.urls import canonical_url


class LocalCache:
    def __init__(self, state):
        self.state = state
        state.script("""
        CREATE TABLE IF NOT EXISTS capture_cache (
          id TEXT PRIMARY KEY,body TEXT NOT NULL,captured REAL NOT NULL,expires REAL NOT NULL,size INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS capture_aliases (
          id TEXT PRIMARY KEY,entry_id TEXT NOT NULL REFERENCES capture_cache(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS submission_cache (
          id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,body TEXT NOT NULL DEFAULT '{}',
          expires REAL NOT NULL,updated REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS capture_age ON capture_cache(captured);
        CREATE INDEX IF NOT EXISTS submission_age ON submission_cache(updated);
        """)

    @staticmethod
    def scope(source, config, first_page):
        return digest(['capture-material/v1', source.profile, source.kind, config.headless,
                       [s.model_dump() for s in source.steps] if first_page else []])

    def capture_get(self, url, scope, policy, *, now=None):
        now = time.time() if now is None else now
        ttl = policy.ttl(url)
        if not ttl:
            return None
        row = self.state.one("SELECT c.* FROM capture_cache c JOIN capture_aliases a ON a.entry_id=c.id WHERE a.id=?",
                             (digest([scope, canonical_url(url)]),))
        if not row or min(row['expires'], row['captured'] + ttl) <= now:
            return None
        body = json.loads(row['body'])
        meta = body.get('meta', {})
        for target in (meta.get('finalUrl', ''), meta.get('canonicalUrl', '')):
            if canonical_url(target) and row['captured'] + policy.ttl(target) <= now:
                return None
        if 'meta' in body:
            body['meta']['requestedUrl'] = url
        return body

    def capture_put(self, url, scope, body, policy, *, now=None):
        now = time.time() if now is None else now
        ttl = policy.ttl(url)
        encoded = encode(body)
        size = len(encoded.encode())
        complete = bool(body.get('feedUrls') or body.get('blocks') or body.get('links'))
        if body.get('status') and body['status'] != {'access': 'ok', 'load': 'ready'}:
            complete = False
        if not complete or not ttl or size > policy.maxBytes or body.get('meta', {}).get('mediaPending'):
            return
        key = digest([scope, canonical_url(url)])
        meta = body.get('meta', {})
        aliases = {canonical_url(u) for u in (url, meta.get('finalUrl', ''), meta.get('canonicalUrl', ''))}
        aliases.discard('')
        ttl = min([ttl, *[policy.ttl(alias) for alias in aliases]])
        if not ttl:
            return
        with self.state.transaction() as conn:
            conn.execute("""INSERT INTO capture_cache VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                body=excluded.body,captured=excluded.captured,expires=excluded.expires,size=excluded.size""",
                (key, encoded, now, now + ttl, size))
            conn.execute("DELETE FROM capture_aliases WHERE entry_id=?", (key,))
            for alias in aliases:
                conn.execute("INSERT OR REPLACE INTO capture_aliases VALUES (?,?)", (digest([scope, alias]), key))
        self.prune_capture(policy, now=now)

    def prune_capture(self, policy, *, now=None):
        now = time.time() if now is None else now
        with self.state.transaction() as conn:
            conn.execute("DELETE FROM capture_cache WHERE expires<=?", (now,))
            rows = conn.execute("SELECT id,size FROM capture_cache ORDER BY captured DESC").fetchall()
            size = 0
            for i, row in enumerate(rows):
                size += row['size']
                if i >= policy.maxEntries or size > policy.maxBytes:
                    conn.execute("DELETE FROM capture_cache WHERE id=?", (row['id'],))

    def reserve(self, key, owner_id, ttl, *, now=None):
        if not key:
            return False
        now = time.time() if now is None else now
        with self.state.transaction() as conn:
            row = conn.execute("SELECT * FROM submission_cache WHERE id=?", (key,)).fetchone()
            if row:
                owner = conn.execute("SELECT state,result FROM outbox WHERE id=?", (row['owner_id'],)).fetchone()
                active = owner and owner['state'] not in {'done', 'failed', 'cancelled'}
                failed = (not owner and not json.loads(row['body']).get('result')) or (owner and (owner['state'] in {'failed', 'cancelled'} or json.loads(owner['result']).get('state') == 'failed'))
                if not failed and (active or row['expires'] > now):
                    return row['owner_id'] != owner_id
            conn.execute("INSERT OR REPLACE INTO submission_cache VALUES (?,?,'{}',?,?)", (key, owner_id, now + ttl, now))
        return False

    def save_result(self, key, result, payload, remote_ids):
        if not key:
            return
        if result['state'] == 'failed':
            self.state.execute("DELETE FROM submission_cache WHERE id=?", (key,))
            return
        body = {'result': map_result(result, payload, canonical=True), 'remoteIds': remote_ids}
        self.state.execute("UPDATE submission_cache SET body=?,updated=? WHERE id=?", (encode(body), time.time(), key))

    def submission(self, key):
        row = self.state.one("SELECT * FROM submission_cache WHERE id=?", (key,))
        return json.loads(row['body']) if row else {}

    def prune(self, config, *, now=None):
        now = time.time() if now is None else now
        self.prune_capture(config.captureCache, now=now)
        with self.state.transaction() as conn:
            rows = conn.execute("""SELECT c.*,o.state AS owner_state FROM submission_cache c
                LEFT JOIN outbox o ON o.id=c.owner_id ORDER BY c.updated DESC""").fetchall()
            for i, row in enumerate(rows):
                if row['owner_state'] and row['owner_state'] not in {'done', 'failed', 'cancelled'}:
                    continue
                if row['expires'] <= now or i >= config.submissionCache.maxEntries:
                    conn.execute("DELETE FROM submission_cache WHERE id=?", (row['id'],))
            cutoff = now - config.submissionCache.historyDays * 86400
            old = [r[0] for r in conn.execute("SELECT id FROM runs WHERE state IN ('succeeded','failed','cancelled') AND updated<? LIMIT 200", (cutoff,))]
            for run in old:
                conn.execute("DELETE FROM outbox WHERE run_id=? AND state IN ('done','failed','cancelled')", (run,))
                if not conn.execute("SELECT 1 FROM outbox WHERE run_id=?", (run,)).fetchone():
                    conn.execute("DELETE FROM runs WHERE id=?", (run,))
