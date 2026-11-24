"""Metadata privacy, outage recovery, retention and real queue/batch lifecycle."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path

import httpx
import pytest
import yaml

from core.async_database import AsyncDatabaseManager
from wiseflow_server.constant import Settings
from wiseflow_server.control_plane import SupabaseControl
from wiseflow_server.job_monitor import JobMonitor, SCHEMA, TABLE
from wiseflow_server.models import ExtractRequest
from wiseflow_server.runtime_config import NodeKeyRegistry, load_runtime_config
from wiseflow_server.worker import ExtractionService

ROOT = Path(__file__).resolve().parents[1]


async def database(tmp_path):
    db = AsyncDatabaseManager(db_path=tmp_path/'data.db', logger=logging.getLogger('test'))
    await db.initialize()
    async def create(connection):
        await connection.execute(SCHEMA)
    await db.execute_with_retry(create)
    return db


async def records(db):
    async def read(connection):
        async with connection.execute(f'SELECT job_id,record,dirty FROM {TABLE}') as cursor:
            return {key: (json.loads(record), dirty) for key,record,dirty in await cursor.fetchall()}
    return await db.execute_with_retry(read)


def test_delivery_failure_retries_and_revision_ack_does_not_lose_updates(tmp_path):
    async def run():
        db = await database(tmp_path)
        sent = []
        fail = True
        monitor = None
        async def publish(rows):
            if fail:
                raise RuntimeError('offline')
            sent.extend(rows)
            if rows[0]['status']=='running':
                await monitor.emit('sel_a', 'selection', status='succeeded')
        monitor = JobMonitor(db,'government',publish)
        try:
            await monitor.emit('sel_a','selection',status='running',node_ids=['node-a'])
            with pytest.raises(RuntimeError):
                await monitor.flush()
            first=(await records(db))['sel_a'][0]
            assert (await records(db))['sel_a'][1]==1
            fail=False
            await monitor.flush()
            assert (await records(db))['sel_a'][1]==1
            await monitor.flush()
            assert (await records(db))['sel_a'][1]==0
            assert sent[-1]['status']=='succeeded'
            assert sent[-1]['created_at']==first['created_at']
            assert all('result' not in row and 'text' not in row for row in sent)
            # A newly constructed sender resumes the same durable metadata.
            recovered=JobMonitor(db,'government',publish)
            await recovered.emit('sel_b','selection',status='failed')
            await recovered.flush()
            assert 'sel_b' in {row['job_id'] for row in sent}
            await monitor.emit('forbidden','selection',text='private original')
            assert 'forbidden' not in await records(db)
        finally:
            await db.cleanup()
    asyncio.run(run())


def test_retention_and_restart_preserve_business_jobs_and_historical_timestamps(tmp_path):
    async def run():
        db=await database(tmp_path)
        async def publish(rows): pass
        monitor=JobMonitor(db,'government',publish)
        old=(datetime.now(timezone.utc)-timedelta(days=8)).isoformat()
        recent=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        try:
            await monitor.emit('old','ontology',updated_at=old,created_at=old)
            await monitor.emit('recent','ontology',updated_at=recent,created_at=recent,status='succeeded')
            await monitor.emit('inflight','batch',status='running')
            async def seed(connection):
                await connection.execute("INSERT INTO ontology_jobs(id,payload,created,updated,library_id) VALUES ('old','{}',?,?,'policy')",(old,old))
            await db.execute_with_retry(seed)
            class Queue:
                async def pending_ids(self,**kwargs): return []
            await monitor.start(Queue())
            rows=await records(db)
            assert 'old' not in rows
            assert rows['recent'][0]['updated_at']==recent
            assert rows['inflight'][0]['status']=='interrupted'
            async def count(connection):
                async with connection.execute('SELECT count(*) FROM ontology_jobs') as cursor:
                    return (await cursor.fetchone())[0]
            assert await db.execute_with_retry(count)==1
        finally:
            await monitor.close()
            await db.cleanup()
    asyncio.run(run())


def test_work_profile_upsert_uses_existing_control_credentials():
    async def run():
        seen=[]
        async def handler(request):
            seen.append(request)
            return httpx.Response(201)
        control=SupabaseControl(NodeKeyRegistry({},mode='supabase'),client=httpx.AsyncClient(
            base_url='https://control.test/rest/v1/',transport=httpx.MockTransport(handler),
            headers={'Authorization':'Bearer internal-test','Content-Profile':'ops'}))
        try:
            await control.publish_jobs([{'job_id':'a','net_id':'government'}])
            request=seen[0]
            assert request.headers['Content-Profile']=='work'
            assert request.headers['Authorization']=='Bearer internal-test'
            assert request.url.params['on_conflict']=='net_id,job_id'
            assert 'merge-duplicates' in request.headers['Prefer']
            assert request.url.path=='/rest/v1/osbi_jobs'
        finally:
            await control.close()
    asyncio.run(run())


def test_selection_batch_progress_retry_and_interruption_are_metadata_only(tmp_path):
    async def run():
        data=yaml.safe_load((ROOT/'server/config/example/runtime.yaml').read_text())
        data['server']['auth']={'mode':'none'}
        data['server']['dataDir']=str(tmp_path/'state')
        data['server']['seeds']=[]
        for name,store in data['server']['stores'].items():
            store['schemaPath']=str(ROOT/'server/wiseflow_server/schemas'/f'{name}.ttl')
        path=tmp_path/'runtime.yaml';path.write_text(yaml.safe_dump(data,allow_unicode=True))
        settings=Settings(config=load_runtime_config(path), selected_model='selection',performance_model='unused',
                          base_url='https://unused.test',api_key='unused',ontology_model='ontology',fallback_model='fallback',
                          ontology_batch_enabled=False)
        db=await database(tmp_path)
        async def publish(rows): pass
        monitor=JobMonitor(db,'government',publish)
        fail=False
        hold=False
        entered=asyncio.Event();release=asyncio.Event()
        async def chat(messages,model):
            payload=json.loads(messages[1]['content'])
            if payload.get('stage'):
                entered.set()
                if hold: await release.wait()
                if fail: raise RuntimeError('private model output must never reach Supabase')
                return ('{"entities":[{"text":"临汾路街道","label":"街镇机构"}]}'
                        if payload['stage']=='ontology_entities' else '{"relations":[]}')
            return '{"blockIds":["B1"],"linkIds":[]}'
        service=ExtractionService(settings,chat,db)
        service.monitor=service.batch.monitor=monitor
        def request(text):
            return ExtractRequest.model_validate(dict(channel='community',materialId='a'*64,nodeId='node-a',
                input={'title':'社区治理','blocks':[{'id':'B1','text':text}],'links':[]},
                meta={'requestedUrl':'https://private.test/original'}))
        try:
            await service.extract(request('临汾路街道推进社区治理。'))
            hold=True
            service.batch.start()
            await asyncio.wait_for(entered.wait(),5)
            rows=await records(db)
            ontology=[row for row,_ in rows.values() if row['job_type']=='ontology']
            assert ontology[0]['status']=='running'
            assert ontology[0]['phase']=='ontology_entities'
            assert service.batch.last_report['currentJobId']==ontology[0]['job_id']
            await service.extract(request('临汾路街道推进社区治理。'))
            duplicate=(await records(db))[ontology[0]['job_id']][0]
            assert duplicate['status']=='running' and duplicate['phase']=='ontology_entities'
            release.set();await service.batch.task
            rows=await records(db)
            job=next(row for row,_ in rows.values() if row['job_type']=='ontology')
            assert job['status']=='succeeded' and job['entity_count']==1
            assert job['model_calls']==2 and job['attempts']==1
            assert any(row['job_type']=='selection' and row['status']=='succeeded' for row,_ in rows.values())
            assert '临汾路街道推进' not in json.dumps(rows,ensure_ascii=False)
            assert 'private.test' not in json.dumps(rows)
            hold=False;fail=True
            await service.extract(request('临汾路街道开展居民服务。'))
            await service.batch.run()
            retry=next(row for row,_ in (await records(db)).values() if row['status']=='retrying')
            assert retry['error_code']=='ModelError' and retry['attempts']==1
            assert 'private model output' not in json.dumps(await records(db))
            fail=False;await service.batch.run()
            assert (await records(db))[retry['job_id']][0]['status']=='succeeded'
            assert (await records(db))[retry['job_id']][0]['attempts']==2
            await service.extract(request('临汾路街道开展新的志愿服务。'))
            hold=True;release.clear();entered.clear()
            service.batch.start();await asyncio.wait_for(entered.wait(),5)
            await service.batch.close()
            rows=await records(db)
            assert any(row['job_type']=='ontology' and row['status']=='interrupted' for row,_ in rows.values())
            assert any(row['job_type']=='batch' and row['status']=='interrupted' for row,_ in rows.values())
        finally:
            await service.close()
            await db.cleanup()
    asyncio.run(run())
