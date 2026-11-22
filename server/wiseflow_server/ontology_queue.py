"""Durable selected ORIGINAL blocks; independent of graph success."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import json
import logging

from core.wis.extraction_identity import block_focus_key
from .models import OntologyJobReceipt

logger = logging.getLogger(__name__)

JOB_TIMEZONE = ZoneInfo('Asia/Shanghai')


def normalise_created(value: str | None, *, end_of_day: bool = False) -> str | None:
    """Normalise an API date bound to the UTC ISO format used by ontology_jobs.

    Date-only values are interpreted in Asia/Shanghai (the scheduler's timezone),
    so `createdFrom=2026-10-23` means 10-23 00:00 Beijing. Full ISO values keep
    their own offset; naive values are treated as UTC.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) == 10:
        current = datetime.fromisoformat(text).replace(tzinfo=JOB_TIMEZONE)
        if end_of_day:
            current = current.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        current = datetime.fromisoformat(text.replace('Z', '+00:00'))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


class OntologyQueue:
    def __init__(self, db):
        self.db = db

    async def enqueue(self, request, block, *, library_id='policy'):
        job_id = 'oq_' + block_focus_key(block.text, request.focus.focuspoint, library_id)
        payload = dict(materialId=request.materialId, focus=request.focus.model_dump(),
                       title=request.input.title, block=block.model_dump(), meta=request.meta, libraryId=library_id)
        now = datetime.now(timezone.utc).isoformat()

        async def write(db):
            cursor = await db.execute('''INSERT INTO ontology_jobs (id,payload,created,updated,library_id)
                VALUES (?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
                (job_id, json.dumps(payload, ensure_ascii=False), now, now, library_id))
            created = cursor.rowcount == 1
            async with db.execute('SELECT status FROM ontology_jobs WHERE id=?', (job_id,)) as cursor:
                status = (await cursor.fetchone())[0]
            return OntologyJobReceipt(id=job_id, blockId=block.id, status=status, cached=not created)

        return await self.db.execute_with_retry(write)

    async def pending_ids(self, limit=None, *, include_done=False, library_id=None,
                          created_from=None, created_to=None):
        async def read(db):
            clauses = [] if include_done else ["status='pending'"]
            args = []
            if library_id is not None:
                clauses.append('library_id=?')
                args.append(library_id)
            start = normalise_created(created_from)
            end = normalise_created(created_to, end_of_day=True)
            if start:
                clauses.append('created>=?')
                args.append(start)
            if end:
                clauses.append('created<=?')
                args.append(end)
            where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
            args.append(-1 if limit is None else limit)
            async with db.execute(f'''SELECT id FROM ontology_jobs {where}
                                    ORDER BY created,id LIMIT ?''', args) as cursor:
                return [row[0] for row in await cursor.fetchall()]
        return await self.db.execute_with_retry(read)

    async def get(self, job_id):
        async def read(db):
            async with db.execute('SELECT * FROM ontology_jobs WHERE id=?', (job_id,)) as cursor:
                row = await cursor.fetchone()
                if row is None:
                    return None
                value = dict(zip((c[0] for c in cursor.description), row))
                value['payload'] = json.loads(value['payload'])
                value['receipt'] = json.loads(value['receipt']) if value['receipt'] else None
                return value
        return await self.db.execute_with_retry(read)

    async def done_receipts(self, library_id=None):
        """Completed jobs with a durable graph receipt, for evidence queries."""
        async def read(db):
            clauses = ["status='done'", 'receipt IS NOT NULL']
            args = []
            if library_id is not None:
                clauses.append('library_id=?')
                args.append(library_id)
            async with db.execute('SELECT id,receipt FROM ontology_jobs WHERE '
                                  + ' AND '.join(clauses) + ' ORDER BY created,id', args) as cursor:
                rows = await cursor.fetchall()
            jobs = []
            for job_id, receipt in rows:
                try:
                    parsed = json.loads(receipt)
                except (TypeError, ValueError):
                    logger.warning('Skipping malformed receipt for ontology job %s', job_id)
                    continue
                if isinstance(parsed, dict) and parsed.get('id'):
                    jobs.append(dict(id=job_id, receipt=parsed))
            return jobs
        return await self.db.execute_with_retry(read)

    async def finish(self, job_id, *, receipt=None, error=''):
        # Graph success is recorded first. If SQLite fails afterward, replay reads
        # the existing complete graph rather than invoking the model again.
        async def write(db):
            await db.execute('''UPDATE ontology_jobs SET status=?,receipt=COALESCE(?,receipt),last_error=?,
                attempts=attempts+1,updated=? WHERE id=?''',
                ('done' if receipt is not None else 'pending',
                 json.dumps(receipt, ensure_ascii=False) if receipt is not None else None,
                 error[:500], datetime.now(timezone.utc).isoformat(), job_id))
        await self.db.execute_with_retry(write)

    async def counts(self, library_id=None):
        async def read(db):
            where, args = ('WHERE library_id=?', [library_id]) if library_id is not None else ('', [])
            async with db.execute('''SELECT status,COUNT(*),SUM(last_error!='') FROM ontology_jobs
                                    ''' + where + ' GROUP BY status', args) as cursor:
                rows = await cursor.fetchall()
            result = dict(pending=0, done=0, retrying=0)
            for status, count, failed in rows:
                result[status] = count
                if status == 'pending':
                    result['retrying'] = failed
            return result
        return await self.db.execute_with_retry(read)
