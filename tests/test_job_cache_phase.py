"""Only explicitly cache-backed, zero-model-work jobs receive phase=cached."""
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import httpx
import yaml

from core.wis.async_cache import SqliteCache
from llm_client import LLMClient
from test_job_monitor import database, records
from wiseflow_server.constant import Settings
from wiseflow_server.job_monitor import JobMonitor
from wiseflow_server.models import ExtractRequest
from wiseflow_server.runtime_config import load_runtime_config
from wiseflow_server.worker import ExtractionService

ROOT = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def monitored_service(tmp_path):
    data=yaml.safe_load((ROOT/'server/config/example/runtime.yaml').read_text())
    data['server']['auth']={'mode':'none'};data['server']['dataDir']=str(tmp_path/'state')
    data['server']['seeds']=[]
    for name,store in data['server']['stores'].items():
        store['schemaPath']=str(ROOT/'server/wiseflow_server/schemas'/f'{name}.ttl')
    path=tmp_path/'runtime.yaml';path.write_text(yaml.safe_dump(data,allow_unicode=True))
    settings=Settings(config=load_runtime_config(path),selected_model='selection',performance_model='unused',
        base_url='https://api.example/v1',api_key='test-key',ontology_model='ontology',
        fallback_model='fallback',ontology_batch_enabled=False)
    db=await database(tmp_path);cache=SqliteCache(tmp_path/'cache.db');await cache.open()
    calls=[]
    async def publish(rows): pass
    async def handler(request):
        payload=json.loads(json.loads(request.content)['messages'][1]['content']);calls.append(payload)
        stage=payload.get('stage')
        text=('{"entities":[{"text":"临汾路街道","label":"街镇机构"}]}' if stage=='ontology_entities' else
              '{"relations":[]}' if stage else json.dumps({
                  'blockIds':[b['id'] for b in payload['material']['blocks']],
                  'linkIds':[link['id'] for link in payload['material']['links']]}))
        return httpx.Response(200,json={'choices':[{'message':{'content':text}}],
            'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client=LLMClient(settings.base_url,settings.api_key,client=transport)
        service=ExtractionService(settings,client.chat,db,cache)
        service.monitor=service.batch.monitor=JobMonitor(db,'government',publish)
        try: yield service,db,calls
        finally:
            await service.close();await cache.close();await db.cleanup()


def request(*, blocks=False, channel='community', empty=False):
    return ExtractRequest.model_validate(dict(channel=channel,materialId='a'*64,nodeId='node-a',
        input={'title':'社区治理',
               'blocks':[{'id':'B1','text':'临汾路街道推进社区治理。'}] if blocks else [],
               'links':[] if empty or blocks else [{'id':'L1','text':'政策原文'}]},
        meta={'requestedUrl':'https://example.org/a'}))


async def warm_graph(service, payload):
    # Simulate pre-existing graph results, independently of the pending job.
    return await service.ontology(channel=service.channel(payload.channel),title=payload.input.title,
        block=payload.input.blocks[0],meta=payload.meta,publication=None,
        material_id=payload.materialId,chat=service.chat,library_id='policy')


def test_selection_cache_hit_and_zero_work_are_distinct(tmp_path):
    async def run():
        async with monitored_service(tmp_path) as (service,db,calls):
            first=await service.extract(request());second=await service.extract(request())
            await service.extract(request(empty=True))
            rows=[r for r,_ in (await records(db)).values() if r['job_type']=='selection']
            assert [r['phase'] for r in rows]==['finished','cached','finished']
            assert [r['status'] for r in rows]==['succeeded']*3
            assert first.modelCalls==1 and second.modelCalls==0 and len(calls)==1
            assert rows[1]['api_calls']==0 and rows[1]['total_tokens']==0
            assert rows[1]['provider'] is None and rows[1]['model'] is None
    asyncio.run(run())


def test_cache_hits_preserve_date_rejections_and_queue_failures(tmp_path):
    async def run():
        async with monitored_service(tmp_path) as (service,db,calls):
            page=request(blocks=True,channel='policy-latest')
            await service.extract(page);await service.extract(page)
            rows=[r for r,_ in (await records(db)).values() if r['job_type']=='selection']
            assert [(r['status'],r['phase']) for r in rows]==[('rejected','finished'),('rejected','cached')]
            assert rows[1]['error_code']=='date_gate'
            page=request(blocks=True);await service.extract(page)
            async def broken(*args,**kwargs): raise RuntimeError('queue unavailable')
            service.queue.enqueue=broken
            result=await service.extract(page)
            rows=[r for r,_ in (await records(db)).values() if r['job_type']=='selection']
            assert result.modelCalls==0 and rows[-1]['status']=='failed'
            assert rows[-1]['phase']=='finished'
    asyncio.run(run())


def test_pure_cached_ontology_and_batch_survive_duplicate_submissions(tmp_path):
    async def run():
        async with monitored_service(tmp_path) as (service,db,calls):
            page=request(blocks=True);await service.extract(page);await warm_graph(service,page)
            before=len(calls);report=await service.batch.run()
            assert report['modelCalls']==0 and len(calls)==before
            await service.extract(page)
            rows=[r for r,_ in (await records(db)).values()]
            ontology=next(r for r in rows if r['job_type']=='ontology')
            batch=next(r for r in rows if r['job_type']=='batch')
            assert ontology['phase']==batch['phase']=='cached'
            assert ontology['status']==batch['status']=='succeeded'
            assert ontology['total_tokens']==batch['total_tokens']==0
    asyncio.run(run())


def test_mixed_and_empty_batches_are_not_cached(tmp_path):
    async def run():
        async with monitored_service(tmp_path) as (service,db,calls):
            await service.batch.run()
            page=request(blocks=True);await service.extract(page);await warm_graph(service,page)
            changed=page.model_copy(deep=True)
            changed.input.blocks[0].text='临汾路街道开展居民服务。'
            await service.extract(changed)
            report=await service.batch.run()
            rows=[r for r,_ in (await records(db)).values()]
            assert report['completed']==2 and report['modelCalls']>0
            assert [r['phase'] for r in rows if r['job_type']=='batch']==['finished','finished']
            assert sorted(r['phase'] for r in rows if r['job_type']=='ontology')==['cached','finished']
    asyncio.run(run())


def test_job_with_previous_model_usage_is_not_pure_cache(tmp_path):
    async def run():
        async with monitored_service(tmp_path) as (service,db,calls):
            page=request(blocks=True);result=await service.extract(page);await warm_graph(service,page)
            job_id=result.ontologyJobs[0].id
            await service.monitor.usage(job_id,'ontology',dict(provider='api.example',model='ontology',
                prompt_tokens=10,completion_tokens=2,total_tokens=12,http_status=200))
            await service.monitor.emit(job_id,'ontology',status='retrying',model_calls=1)
            report=await service.batch.run()
            row=(await records(db))[job_id][0]
            assert report['modelCalls']==0 and row['api_calls']==1
            assert row['phase']=='finished' and row['total_tokens']==12
    asyncio.run(run())
