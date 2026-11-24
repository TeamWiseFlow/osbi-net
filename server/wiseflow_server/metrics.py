"""Node/source quality metrics (recorded for operators; never used as a gate).

The table is deliberately additive: submissions, selection/extraction quality
and capture failures per node and normalized source. v1 exposes a read-only
operator view.
"""
import asyncio
from datetime import datetime, timezone
import logging
import re
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

TABLE = 'node_source_metrics'

SCHEMA = f'''CREATE TABLE IF NOT EXISTS {TABLE} (
    node_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_url TEXT DEFAULT '',
    publisher TEXT DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    submissions INTEGER NOT NULL DEFAULT 0,
    materials INTEGER NOT NULL DEFAULT 0,
    selected_blocks INTEGER NOT NULL DEFAULT 0,
    selected_links INTEGER NOT NULL DEFAULT 0,
    selected_content_links INTEGER NOT NULL DEFAULT 0,
    selected_unknown_links INTEGER NOT NULL DEFAULT 0,
    selected_navigation_links INTEGER NOT NULL DEFAULT 0,
    navigation_nav_cap_dropped INTEGER NOT NULL DEFAULT 0,
    navigation_only_fallback INTEGER NOT NULL DEFAULT 0,
    extracted_blocks INTEGER NOT NULL DEFAULT 0,
    entities INTEGER NOT NULL DEFAULT 0,
    relations INTEGER NOT NULL DEFAULT 0,
    zero_relation_blocks INTEGER NOT NULL DEFAULT 0,
    capture_failures INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (node_id, source_key)
)'''

_LINK_COUNTERS = ('selected_content_links', 'selected_unknown_links',
                  'selected_navigation_links', 'navigation_nav_cap_dropped',
                  'navigation_only_fallback')

_COUNTERS = ('submissions', 'materials', 'selected_blocks', 'selected_links',
             'extracted_blocks', 'entities', 'relations', 'zero_relation_blocks',
             'capture_failures', *_LINK_COUNTERS)


def source_url(meta) -> str:
    meta = dict(meta or {})
    return str(meta.get('canonicalUrl') or meta.get('finalUrl')
               or meta.get('requestedUrl') or '').strip()


def source_key(meta) -> str:
    """Canonical source identity: publisher name, else normalized host."""
    meta = dict(meta or {})
    publisher = re.sub(r'\s+', '', str(meta.get('publisher') or ''))
    if publisher:
        return publisher[:200]
    host = urlsplit(source_url(meta)).netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    return host or 'unknown'


class NodeSourceMetrics:
    def __init__(self, db):
        self.db = db
        self._ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self):
        if self._ready:
            return
        async with self._schema_lock:
            if self._ready:
                return
            async def create(db):
                await db.execute(SCHEMA)
                async with db.execute(f'PRAGMA table_info({TABLE})') as cursor:
                    columns = {row[1] for row in await cursor.fetchall()}
                for name in _LINK_COUNTERS:
                    if name not in columns:
                        await db.execute(f'ALTER TABLE {TABLE} ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0')
            await self.db.execute_with_retry(create)
            self._ready = True

    async def _increment(self, node_id, meta, *, counters, channel=None):
        if not node_id:
            return
        await self.ensure_schema()
        key, url = source_key(meta), source_url(meta)
        publisher = str((meta or {}).get('publisher') or '').strip()
        now = datetime.now(timezone.utc).isoformat()
        names = [name for name in _COUNTERS if name in counters]
        values = [max(0, int(counters[name])) for name in names]
        assert names, 'At least one counter is required'
        columns = ', '.join(names)
        placeholders = ','.join('?' * len(names))
        updates = ', '.join(f'{name}={name}+?' for name in names)
        async def write(db):
            await db.execute(
                f'''INSERT INTO {TABLE}
                    (node_id, source_key, source_url, publisher, first_seen, last_seen,
                     {columns})
                    VALUES (?,?,?,?,?,?,{placeholders})
                    ON CONFLICT(node_id, source_key) DO UPDATE SET
                        last_seen=excluded.last_seen,
                        source_url=CASE WHEN excluded.source_url!='' THEN excluded.source_url ELSE source_url END,
                        publisher=CASE WHEN excluded.publisher!='' THEN excluded.publisher ELSE publisher END,
                        {updates}''',
                [node_id, key, url, publisher, now, now, *values, *values])
        await self.db.execute_with_retry(write)

    async def record_submission(self, node_id, meta, channel=None):
        await self._increment(node_id, meta, counters=dict(submissions=1, materials=1),
                              channel=channel)

    async def record_selection(self, node_id, meta, *, selected_blocks=0, selected_links=0,
                               selected_content_links=0, selected_unknown_links=0,
                               selected_navigation_links=0, navigation_nav_cap_dropped=0,
                               navigation_only_fallback=0):
        await self._increment(node_id, meta, counters=dict(
            selected_blocks=max(0, selected_blocks), selected_links=max(0, selected_links),
            selected_content_links=selected_content_links,
            selected_unknown_links=selected_unknown_links,
            selected_navigation_links=selected_navigation_links,
            navigation_nav_cap_dropped=navigation_nav_cap_dropped,
            navigation_only_fallback=navigation_only_fallback))

    async def record_extraction(self, node_id, meta, *, blocks=1, entities=0, relations=0,
                                zero_relation_blocks=0):
        await self._increment(node_id, meta, counters=dict(
            extracted_blocks=max(0, blocks), entities=max(0, entities),
            relations=max(0, relations),
            zero_relation_blocks=max(0, zero_relation_blocks)))

    async def record_capture_failure(self, node_id, meta):
        await self._increment(node_id, meta, counters=dict(capture_failures=1))

    async def query(self, node_id=None, source_key_value=None, limit=500):
        await self.ensure_schema()
        async def read(db):
            clauses, args = [], []
            if node_id:
                clauses.append('node_id=?')
                args.append(node_id)
            if source_key_value:
                clauses.append('source_key=?')
                args.append(source_key_value)
            where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
            args.append(min(max(int(limit), 1), 2000))
            async with db.execute(
                    f'SELECT * FROM {TABLE} {where} ORDER BY last_seen DESC, node_id, source_key LIMIT ?',
                    args) as cursor:
                return [dict(zip((column[0] for column in cursor.description), row))
                        for row in await cursor.fetchall()]
        return await self.db.execute_with_retry(read)
