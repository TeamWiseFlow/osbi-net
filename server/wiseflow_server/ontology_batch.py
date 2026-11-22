"""Nightly, restart-safe graph extraction from the local selected-block backlog."""
import asyncio
from datetime import datetime, timedelta
import logging
import time
from zoneinfo import ZoneInfo

from .models import Block, Focus, OntologyReceipt

logger = logging.getLogger(__name__)


def next_run(at, timezone, now=None):
    zone = ZoneInfo(timezone)
    current = now.astimezone(zone) if now else datetime.now(zone)
    hour, minute = map(int, at.split(':'))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > current else target + timedelta(days=1)


class BatchBusy(RuntimeError):
    pass


class OntologyBatch:
    def __init__(self, settings, queue, extract, chat, refresh=None, reprocess_ids=None, supersede=None):
        self.settings, self.queue, self.extract, self.chat = settings, queue, extract, chat
        self.refresh = refresh
        self.reprocess_ids, self.supersede = reprocess_ids, supersede
        self.lock = asyncio.Lock()
        self.task = self.timer = None
        self.last_report = None

    async def run(self, max_blocks=None, *, reprocess=False, library_id=None,
                  created_from=None, created_to=None):
        if self.lock.locked():
            raise BatchBusy('An ontology batch is already running')
        async with self.lock:
            report = dict(running=True, startedAt=datetime.now().astimezone().isoformat(),
                          attempted=0, completed=0, failed=0, modelCalls=0, windowExpired=False, jobs=[], reprocess=reprocess,
                          libraryId=library_id, createdFrom=created_from, createdTo=created_to,
                          droppedSamples=[], droppedCounts={})
            self.last_report = report
            deadline = time.monotonic() + self.settings.ontology_batch_max_seconds

            async def chat(messages, model):
                report['modelCalls'] += 1
                return await self.chat(messages, model)

            try:
                # A fixed snapshot: failures and newly arriving blocks wait for
                # the next batch, rather than creating an unbounded retry loop.
                ids = (await self.reprocess_ids(max_blocks, library_id=library_id,
                                                created_from=created_from, created_to=created_to)
                       if reprocess and self.reprocess_ids
                       else await self.queue.pending_ids(max_blocks, library_id=library_id,
                                                         created_from=created_from, created_to=created_to))
                report['candidates'] = len(ids)
                for job_id in ids:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        report['windowExpired'] = True
                        break
                    job = await self.queue.get(job_id)
                    if not job or (job['status'] == 'done' and not reprocess):
                        continue
                    payload = job['payload']
                    job_library = job['library_id']
                    outcome = dict(jobId=job_id, blockId=payload['block']['id'], libraryId=job_library)
                    report['attempted'] += 1
                    try:
                        async with asyncio.timeout(remaining):
                            receipt = OntologyReceipt.model_validate(await self.extract(
                                focus=Focus.model_validate(payload['focus']), title=payload['title'],
                                block=Block.model_validate(payload['block']), meta=payload['meta'],
                                material_id=payload['materialId'], chat=chat, library_id=job_library))
                            if receipt.status == 'failed' or receipt.blockId != payload['block']['id']:
                                raise ValueError('Invalid ontology completion receipt')
                            value = receipt.model_dump(exclude_none=True)
                            previous = job.get('receipt') or {}
                            if self.supersede and previous.get('id') and previous['id'] != receipt.id:
                                await self.supersede(previous['id'], receipt.id, library_id=job_library)
                            await self.queue.finish(job_id, receipt=value)
                        report['completed'] += 1
                        outcome.update(status='done', extraction=value)
                        sample_room = 100 - len(report['droppedSamples'])
                        if sample_room > 0:
                            report['droppedSamples'].extend(
                                (value.get('droppedSamples') or [])[:sample_room])
                        for key, count in (value.get('droppedCounts') or {}).items():
                            report['droppedCounts'][key] = report['droppedCounts'].get(key, 0) + count
                    except Exception as error:
                        report['failed'] += 1
                        if time.monotonic() >= deadline:
                            report['windowExpired'] = True
                        outcome.update(status='pending', error=str(error)[:500])
                        try:
                            await self.queue.finish(job_id, error=outcome['error'])
                        except Exception:
                            logger.exception('Could not persist ontology retry state for %s', job_id)
                    report['jobs'].append(outcome)
                return report
            except asyncio.CancelledError:
                report['interrupted'] = True
                raise
            finally:
                report.update(running=False, finishedAt=datetime.now().astimezone().isoformat())
                if report.get('completed') and self.refresh:
                    # Reuse the graph owner's handle; refresh failures do not
                    # invalidate already committed blocks.
                    try:
                        await self.refresh()
                    except Exception:
                        logger.exception('Viewer refresh after batch failed')

    def start(self, max_blocks=None, *, reprocess=False, library_id=None,
              created_from=None, created_to=None):
        if self.lock.locked() or (self.task and not self.task.done()):
            raise BatchBusy('An ontology batch is already running')
        self.task = asyncio.create_task(
            self.run(max_blocks, reprocess=reprocess, library_id=library_id,
                     created_from=created_from, created_to=created_to),
            name='ontology-batch')
        self.task.add_done_callback(self._log_failure)

    @staticmethod
    def _log_failure(task):
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error('Ontology batch failed: %s', type(error).__name__)

    def schedule(self):
        if self.settings.ontology_batch_enabled:
            self.timer = asyncio.create_task(self._daily(), name='ontology-daily')

    async def _daily(self):
        while True:
            target = next_run(self.settings.ontology_batch_time, self.settings.ontology_batch_timezone)
            await asyncio.sleep((target - datetime.now(target.tzinfo)).total_seconds())
            try:
                await self.run()
            except BatchBusy:
                logger.info('Daily ontology batch skipped: a manual batch is running')
            except Exception:
                logger.exception('Daily ontology batch failed; pending blocks remain retryable')

    async def status(self, library_id=None):
        return dict(queue=await self.queue.counts(library_id), running=self.lock.locked() or bool(self.task and not self.task.done()),
                    schedule=dict(enabled=self.settings.ontology_batch_enabled,
                                  time=self.settings.ontology_batch_time,
                                  timezone=self.settings.ontology_batch_timezone,
                                  maxSeconds=self.settings.ontology_batch_max_seconds),
                    lastBatch=self.last_report)

    async def close(self):
        tasks = [t for t in (self.timer, self.task) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
