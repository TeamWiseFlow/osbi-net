"""Metadata-only job journal with a durable Supabase delivery outbox.

SQLite remains the execution authority. Monitoring failures never fail a job;
dirty revisions survive restarts and are retried independently of model work.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import time

logger = logging.getLogger(__name__)
RETENTION = timedelta(days=7)
TABLE = 'osbi_job_monitor_outbox'
SCHEMA = f'''CREATE TABLE IF NOT EXISTS {TABLE} (
    job_id TEXT PRIMARY KEY, record TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
)'''


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


class JobMonitor:
    def __init__(self, db, net_id, publish):
        self.db, self.net_id, self.publish = db, net_id, publish
        self.lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.task = None
        self.last_warning = -float('inf')

    def defaults(self, job_id, job_type):
        now = timestamp()
        return dict(net_id=self.net_id, job_id=job_id, job_type=job_type,
                    status='queued', phase=None, node_ids=[], channel_id=None,
                    store_id=None, material_id=None, block_id=None, batch_id=None,
                    trigger=None, attempts=0, model_calls=0, selected_blocks=0,
                    selected_links=0, entity_count=0, relation_count=0,
                    total_jobs=0, completed_jobs=0, failed_jobs=0, duration_ms=None,
                    provider=None, model=None, api_calls=0, prompt_tokens=None,
                    completion_tokens=None, total_tokens=None, usage_complete=False,
                    model_usage=[],
                    error_code=None, created_at=now, started_at=None,
                    finished_at=None, updated_at=now)

    async def emit(self, job_id, job_type, *, preserve_running=False,
                   initialize_usage=False, usage_event=None, **changes):
        """Persist only explicitly allowed operational fields, never free text."""
        try:
            async with self.lock:
                default = self.defaults(job_id, job_type)
                if set(changes) - default.keys():
                    raise ValueError('Unknown job monitoring fields')
                async def write(db):
                    async with db.execute(f'SELECT record FROM {TABLE} WHERE job_id=?', (job_id,)) as cursor:
                        row = await cursor.fetchone()
                    record = {**default, **json.loads(row[0])} if row else default
                    if row is None and initialize_usage:
                        record.update(prompt_tokens=0, completion_tokens=0,
                                      total_tokens=0, usage_complete=True)
                    updates = dict(changes)
                    if preserve_running and record['status']=='running':
                        for key in ['status','phase','attempts','model_calls','finished_at',
                                    'error_code','duration_ms']:
                            updates.pop(key, None)
                        updates['updated_at'] = max(record['updated_at'],
                                                    updates.get('updated_at', record['updated_at']))
                    record.update(updates)
                    if usage_event is not None:
                        self._add_usage(record, usage_event)
                    record['updated_at'] = updates.get('updated_at', timestamp())
                    await db.execute(f'''INSERT INTO {TABLE}(job_id,record,updated_at)
                        VALUES(?,?,?) ON CONFLICT(job_id) DO UPDATE SET
                        record=excluded.record, updated_at=excluded.updated_at,
                        version=version+1, dirty=1''',
                        (job_id, json.dumps(record), record['updated_at']))
                await self.db.execute_with_retry(write)
            self.wake.set()
        except Exception as error:
            self.warning('Local monitoring write failed', error)

    @staticmethod
    def _add_usage(record, event):
        # Events are produced by the HTTP client; retain only bounded metadata.
        entry = {key: event.get(key) for key in ['provider','model','requested_model',
            'prompt_tokens','completion_tokens','total_tokens','http_status','error_code','phase']}
        record['api_calls'] += 1
        entry['call'] = record['api_calls']
        record['model_usage'].append(entry)
        record.update(provider=entry['provider'], model=entry['model'])
        for key in ['prompt_tokens','completion_tokens','total_tokens']:
            value = entry[key]
            if value is None:
                record['usage_complete'] = False
                if record['api_calls'] == 1:
                    record[key] = None
            else:
                record[key] = (record[key] or 0) + value

    async def usage(self, job_id, job_type, event, *, phase=None):
        await self.emit(job_id, job_type, usage_event={**event, 'phase':phase})

    async def job(self, row, **changes):
        payload, receipt = row['payload'], row.get('receipt') or {}
        state = ('succeeded' if row['status']=='done' else
                 'retrying' if row.get('last_error') else 'queued')
        fields = dict(status=state, phase='finished' if row['status']=='done' else 'queued',
                      node_ids=payload.get('nodeIds') or [],
                      channel_id=payload.get('channel'), store_id=row['library_id'],
                      material_id=payload.get('materialId'),
                      block_id=(payload.get('block') or {}).get('id'),
                      attempts=row.get('attempts', 0), created_at=row['created'],
                      updated_at=row['updated'], entity_count=receipt.get('entityCount', 0),
                      relation_count=receipt.get('relationCount', 0),
                      error_code='previous_attempt_failed' if row.get('last_error') else None,
                      finished_at=row['updated'] if row['status']=='done' else None)
        fields.update(changes)
        # Live transitions use the current event time; startup imports preserve
        # the original timestamps so restarts never extend the retention period.
        if changes and 'updated_at' not in changes:
            fields['updated_at'] = timestamp()
        await self.emit(row['id'], 'ontology', preserve_running=not changes,
                        initialize_usage=(row.get('attempts', 0)==0 and row['status']!='done'
                                          and not row.get('last_error')), **fields)

    async def start(self, queue):
        cutoff = (datetime.now(timezone.utc) - RETENTION).isoformat()
        async def initialize(db):
            await db.execute(SCHEMA)
            async with db.execute(f'SELECT record FROM {TABLE} WHERE updated_at >= ?', (cutoff,)) as cursor:
                return [json.loads(row[0]) for row in await cursor.fetchall()]
        previous = await self.db.execute_with_retry(initialize)
        previous_by_id = {row['job_id']: row for row in previous}
        for row in previous:
            if row['status']=='running':
                await self.emit(row['job_id'], row['job_type'], status='interrupted',
                                error_code='server_restarted', finished_at=timestamp())
        for job_id in await queue.pending_ids(include_done=True):
            row = await queue.get(job_id)
            if row and row['updated'] >= cutoff:
                # Only import absent jobs, or reconcile jobs that were in flight.
                old = previous_by_id.get(job_id)
                if old is None or old['status'] in {'running','interrupted'}:
                    await self.job(row)
        await self.prune()
        self.task = asyncio.create_task(self._run(), name='supabase-job-monitor')

    async def prune(self):
        cutoff = (datetime.now(timezone.utc) - RETENTION).isoformat()
        async def remove(db):
            await db.execute(f'DELETE FROM {TABLE} WHERE updated_at < ?', (cutoff,))
        await self.db.execute_with_retry(remove)

    async def flush(self):
        async def read(db):
            async with db.execute(f'SELECT job_id,record,version FROM {TABLE} WHERE dirty=1 ORDER BY updated_at LIMIT 100') as cursor:
                return await cursor.fetchall()
        rows = await self.db.execute_with_retry(read)
        if not rows:
            return False
        await self.publish([json.loads(row[1]) for row in rows])
        async def acknowledge(db):
            for job_id, _, version in rows:
                # A transition occurring during an HTTP request stays dirty.
                await db.execute(f'UPDATE {TABLE} SET dirty=0 WHERE job_id=? AND version=?', (job_id, version))
        await self.db.execute_with_retry(acknowledge)
        return len(rows)==100

    def warning(self, message, error):
        if time.monotonic() - self.last_warning >= 60:
            logger.warning('%s (%s); execution is unaffected', message, type(error).__name__)
            self.last_warning = time.monotonic()

    async def _run(self):
        last_prune = time.monotonic()
        while True:
            self.wake.clear()
            try:
                if time.monotonic() - last_prune > 600:
                    await self.prune()
                    last_prune = time.monotonic()
                more = await self.flush()
                if more:
                    continue
            except Exception as error:
                self.warning('Supabase monitoring delivery failed', error)
                await asyncio.sleep(5)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=10)
            except TimeoutError:
                pass

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        try:
            async with asyncio.timeout(5):
                while await self.flush():
                    pass
        except Exception as error:
            self.warning('Monitoring shutdown delivery deferred', error)
