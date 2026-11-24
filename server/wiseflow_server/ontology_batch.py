"""Nightly, restart-safe graph extraction from the local selected-block backlog."""
import asyncio
from datetime import datetime, timedelta
import logging
import json
import time
import uuid
from zoneinfo import ZoneInfo
from llm_client import observe_usage

from .models import Block, OntologyReceipt
from .publication_date import PublicationDate
from .job_monitor import timestamp

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
        self.monitor = None

    async def _monitor_job(self, row, **changes):
        if self.monitor is not None:
            await self.monitor.job(row, **changes)

    async def _monitor_batch(self, batch_id, **changes):
        if self.monitor is not None:
            await self.monitor.emit(batch_id, 'batch', **changes)

    async def run(self, max_blocks=None, *, reprocess=False, library_id=None,
                  created_from=None, created_to=None, trigger='manual'):
        if self.lock.locked():
            raise BatchBusy('An ontology batch is already running')
        async with self.lock:
            batch_id = 'batch_' + uuid.uuid4().hex
            started = time.monotonic()
            current_job = None
            job_calls = 0
            report = dict(running=True, startedAt=datetime.now().astimezone().isoformat(),
                          attempted=0, completed=0, failed=0, modelCalls=0, windowExpired=False, jobs=[], reprocess=reprocess,
                          libraryId=library_id, createdFrom=created_from, createdTo=created_to,
                          droppedSamples=[], droppedCounts={}, batchId=batch_id,
                          currentJobId=None, currentPhase=None)
            self.last_report = report
            deadline = time.monotonic() + self.settings.ontology_batch_max_seconds
            await self._monitor_batch(batch_id, status='running', phase='starting',
                trigger=trigger, store_id=library_id, started_at=timestamp(), attempts=1,
                initialize_usage=True)

            async def chat(messages, model):
                nonlocal job_calls
                report['modelCalls'] += 1
                job_calls += 1
                try:
                    phase = json.loads(messages[1]['content']).get('stage', 'ontology')
                except (IndexError, KeyError, TypeError, ValueError):
                    phase = 'ontology'
                report['currentPhase'] = phase
                if current_job is not None:
                    await self._monitor_job(current_job, status='running', phase=phase,
                        attempts=current_job['attempts']+1, model_calls=job_calls,
                        batch_id=batch_id)
                await self._monitor_batch(batch_id, phase=phase, model_calls=report['modelCalls'])
                async def usage(event):
                    if self.monitor is not None:
                        if current_job is not None:
                            await self.monitor.usage(current_job['id'], 'ontology', event, phase=phase)
                        await self.monitor.usage(batch_id, 'batch', event, phase=phase)
                with observe_usage(usage):
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
                await self._monitor_batch(batch_id, total_jobs=len(ids))
                for job_id in ids:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        report['windowExpired'] = True
                        break
                    job = await self.queue.get(job_id)
                    if not job or (job['status'] == 'done' and not reprocess):
                        continue
                    payload = job['payload']
                    current_job, job_calls = job, 0
                    job_started = time.monotonic()
                    report['currentJobId'] = job_id
                    await self._monitor_job(job, status='running', phase='ontology',
                        attempts=job['attempts']+1, batch_id=batch_id, error_code=None,
                        started_at=timestamp(), finished_at=None)
                    job_library = job['library_id']
                    outcome = dict(jobId=job_id, blockId=payload['block']['id'], libraryId=job_library,
                                   channel=payload.get('channel'))
                    report['attempted'] += 1
                    try:
                        channel = self.settings.config.channel(payload['channel'])
                        publication = (PublicationDate(**payload['publication'])
                                       if payload.get('publication') else None)
                        node_ids = [str(value) for value in
                                    (payload.get('nodeIds') or []) if value]
                        async with asyncio.timeout(remaining):
                            receipt = OntologyReceipt.model_validate(await self.extract(
                                channel=channel, title=payload['title'],
                                block=Block.model_validate(payload['block']), meta=payload['meta'],
                                publication=publication, node_ids=node_ids,
                                material_id=payload['materialId'], chat=chat, library_id=job_library))
                            if receipt.status == 'failed' or receipt.blockId != payload['block']['id']:
                                raise ValueError('Invalid ontology completion receipt')
                            value = receipt.model_dump(exclude_none=True)
                            previous = job.get('receipt') or {}
                            if self.supersede and previous.get('id') and previous['id'] != receipt.id:
                                await self.supersede(previous['id'], receipt.id, library_id=job_library)
                            await self.queue.finish(job_id, receipt=value)
                        report['completed'] += 1
                        await self._monitor_job(job, status='succeeded', phase='finished',
                            cache_hit=receipt.cached,
                            attempts=job['attempts']+1, model_calls=job_calls,
                            entity_count=value.get('entityCount', 0),
                            relation_count=value.get('relationCount', 0), error_code=None,
                            batch_id=batch_id, finished_at=timestamp(),
                            duration_ms=int((time.monotonic()-job_started)*1000))
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
                        await self._monitor_job(job, status='retrying', phase='finished',
                            attempts=job['attempts']+1, model_calls=job_calls,
                            batch_id=batch_id, error_code=type(error).__name__,
                            finished_at=timestamp(), duration_ms=int((time.monotonic()-job_started)*1000))
                    report['jobs'].append(outcome)
                    current_job = None
                    report['currentJobId'] = report['currentPhase'] = None
                    await self._monitor_batch(batch_id, completed_jobs=report['completed'],
                                              failed_jobs=report['failed'])
                return report
            except asyncio.CancelledError:
                report['interrupted'] = True
                if current_job is not None:
                    await self._monitor_job(current_job, status='interrupted',
                        error_code='batch_interrupted', batch_id=batch_id,
                        attempts=current_job['attempts']+1, model_calls=job_calls,
                        finished_at=timestamp())
                raise
            except Exception:
                report['aborted'] = True
                raise
            finally:
                report.update(running=False, finishedAt=datetime.now().astimezone().isoformat())
                report['currentJobId'] = report['currentPhase'] = None
                state = ('interrupted' if report.get('interrupted') else
                         'failed' if report.get('aborted') else
                         'partial' if report['failed'] or report['windowExpired'] else 'succeeded')
                await self._monitor_batch(batch_id, status=state, phase='finished',
                    cache_hit=(state=='succeeded' and report['completed']>0 and
                               all(item.get('extraction',{}).get('cached')
                                   for item in report['jobs'])),
                    finished_at=timestamp(), duration_ms=int((time.monotonic()-started)*1000),
                    completed_jobs=report['completed'], failed_jobs=report['failed'],
                    model_calls=report['modelCalls'])
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
                await self.run(trigger='scheduled')
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
