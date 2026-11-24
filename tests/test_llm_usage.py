"""Actual HTTP usage survives fallback, validation failures and concurrent jobs."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from llm_client import LLMClient, ModelError, generate, observe_usage, parse_json
from test_job_monitor import database, records
from wiseflow_server.constant import Settings
from wiseflow_server.job_monitor import JobMonitor
from wiseflow_server.models import ExtractRequest
from wiseflow_server.runtime_config import load_runtime_config
from wiseflow_server.worker import ExtractionService

ROOT = Path(__file__).resolve().parents[1]


def response(text='{}', usage=None, **extra):
    return httpx.Response(200, json={
        'choices':[{'message':{'content':text},'finish_reason':extra.pop('finish','stop')}],
        'usage':usage, **extra})


def test_retry_fallback_and_invalid_output_all_report_actual_usage(monkeypatch):
    async def no_sleep(*args): pass
    monkeypatch.setattr('llm_client.asyncio.sleep',no_sleep)
    async def run():
        events=[];attempts=[]
        async def handler(request):
            model=json.loads(request.content)['model'];attempts.append(model)
            if len(attempts)==1:
                return httpx.Response(429,json={'error':'private provider error'})
            if model=='primary':
                return response('invalid JSON',{'prompt_tokens':10,'completion_tokens':3,'total_tokens':13})
            return response('{"ok":true}',{'input_tokens':20,'output_tokens':4},model='actual-fallback')
        async def observed(event): events.append(event)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client=LLMClient('https://name:secret@api.example/v1?secret=hidden','private-key',client=transport)
            with observe_usage(observed):
                result,model=await generate(client.chat,[], 'primary','fallback',parse_json)
        assert result=={'ok':True} and model=='fallback'
        assert attempts==['primary','primary','fallback']
        assert [e['total_tokens'] for e in events]==[None,13,24]
        assert events[-1]['model']=='actual-fallback' and events[-1]['requested_model']=='fallback'
        assert all(e['provider']=='api.example' for e in events)
        assert 'secret' not in json.dumps(events) and 'private' not in json.dumps(events)
    asyncio.run(run())


@pytest.mark.parametrize('usage,expected', [
    (None,(None,None,None)),
    ({'prompt_tokens':True,'completion_tokens':-1,'total_tokens':'4'},(None,None,None)),
    ({'prompt_tokens':9,'completion_tokens':2},(9,2,11)),
    ({'prompt_tokens':9,'completion_tokens':2,'total_tokens':12},(9,2,12)),
    ({'prompt_tokens':9},(9,None,None)),
])
def test_usage_is_never_guessed_and_truncated_responses_are_still_counted(usage,expected):
    async def run():
        events=[]
        async def observed(event): events.append(event)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request:response('partial',usage,finish='length'))) as transport:
            client=LLMClient('https://api.example/v1','key',client=transport)
            with observe_usage(observed),pytest.raises(ModelError):
                await client.chat([], 'm')
        assert tuple(events[0][key] for key in ('prompt_tokens','completion_tokens','total_tokens'))==expected
    asyncio.run(run())


def test_concurrent_job_observers_are_isolated_and_failures_do_not_change_output():
    async def run():
        seen={'a':[],'b':[]};entered=0;ready=asyncio.Event()
        async def handler(request):
            nonlocal entered
            entered+=1
            if entered==2:ready.set()
            await ready.wait()
            return response(usage={'prompt_tokens':1,'completion_tokens':2,'total_tokens':3})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client=LLMClient('https://api.example/v1','key',client=transport)
            async def call(name):
                async def observed(event): seen[name].append(event)
                with observe_usage(observed): return await client.chat([],name)
            assert await asyncio.gather(call('a'),call('b'))==['{}','{}']
            async def broken(event): raise RuntimeError('monitor offline')
            with observe_usage(broken): assert await client.chat([],'c')=='{}'
            await client.chat([],'unobserved')
        assert [e['model'] for e in seen['a']]==['a']
        assert [e['model'] for e in seen['b']]==['b']
    asyncio.run(run())


def test_tokens_accumulate_across_retries_and_survive_duplicate_submissions(tmp_path):
    async def run():
        db=await database(tmp_path)
        async def publish(rows): pass
        monitor=JobMonitor(db,'government',publish)
        def event(tokens):
            return dict(provider='api.example',model='m',requested_model='m',
                prompt_tokens=tokens,completion_tokens=1,total_tokens=tokens+1,http_status=200)
        try:
            await monitor.emit('job','ontology',initialize_usage=True,status='running')
            await monitor.usage('job','ontology',event(10),phase='ontology_entities')
            await monitor.emit('job','ontology',status='retrying')
            await monitor.emit('job','ontology',status='running')
            await monitor.usage('job','ontology',event(20),phase='ontology_relations')
            await monitor.emit('job','ontology',preserve_running=True,status='queued',model_calls=0)
            row=(await records(db))['job'][0]
            assert row['status']=='running' and row['total_tokens']==32 and row['api_calls']==2
            assert row['usage_complete'] and len(row['model_usage'])==2
            recovered=JobMonitor(db,'government',publish)
            await recovered.usage('job','ontology',event(0)|{
                'prompt_tokens':None,'completion_tokens':None,'total_tokens':None})
            row=(await records(db))['job'][0]
            assert row['total_tokens']==32 and not row['usage_complete'] and row['api_calls']==3
            await monitor.emit('historical','ontology',status='succeeded')
            old=(await records(db))['historical'][0]
            assert old['total_tokens'] is None and not old['usage_complete']
        finally: await db.cleanup()
    asyncio.run(run())


def test_real_selection_and_batch_journal_record_per_call_usage_without_content(tmp_path):
    async def run():
        data=yaml.safe_load((ROOT/'server/config/example/runtime.yaml').read_text())
        data['server']['auth']={'mode':'none'};data['server']['dataDir']=str(tmp_path/'state')
        data['server']['seeds']=[]
        for name,store in data['server']['stores'].items():
            store['schemaPath']=str(ROOT/'server/wiseflow_server/schemas'/f'{name}.ttl')
        path=tmp_path/'runtime.yaml';path.write_text(yaml.safe_dump(data,allow_unicode=True))
        settings=Settings(config=load_runtime_config(path),selected_model='selection',performance_model='unused',
            base_url='https://api.example/v1',api_key='private-key',ontology_model='ontology',
            fallback_model='fallback',ontology_batch_enabled=False)
        db=await database(tmp_path)
        async def publish(rows): pass
        monitor=JobMonitor(db,'government',publish)
        async def handler(request):
            body=json.loads(request.content);payload=json.loads(body['messages'][1]['content'])
            stage=payload.get('stage');model=body['model']
            text=('not JSON' if stage=='ontology_entities' and model=='ontology' else
                  '{"entities":[{"text":"临汾路街道","label":"街镇机构"}]}' if stage=='ontology_entities' else
                  '{"relations":[]}' if stage else '{"blockIds":["B1"],"linkIds":[]}')
            return response(text,{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
            client=LLMClient(settings.base_url,settings.api_key,client=transport)
            service=ExtractionService(settings,client.chat,db)
            service.monitor=service.batch.monitor=monitor
            payload=ExtractRequest.model_validate(dict(channel='community',materialId='a'*64,nodeId='node-a',
                input={'title':'社区治理','blocks':[{'id':'B1','text':'临汾路街道推进社区治理。'}],'links':[]},
                meta={'requestedUrl':'https://private.example/original'}))
            try:
                await service.extract(payload);await service.batch.run()
                rows=[record for record,_ in (await records(db)).values()]
                selection=next(r for r in rows if r['job_type']=='selection')
                ontology=next(r for r in rows if r['job_type']=='ontology')
                batch=next(r for r in rows if r['job_type']=='batch')
                assert selection['total_tokens']==12 and selection['api_calls']==1
                assert ontology['total_tokens']==batch['total_tokens']==36
                assert ontology['api_calls']==batch['api_calls']==3
                assert [c['model'] for c in ontology['model_usage']]==['ontology','fallback','ontology']
                assert all(r['usage_complete'] for r in rows)
                assert all(r['provider']=='api.example' for r in rows)
                encoded=json.dumps(rows,ensure_ascii=False)
                assert '临汾路街道' not in encoded and 'private' not in encoded and 'not JSON' not in encoded
            finally: await service.close()
        await db.cleanup()
    asyncio.run(run())
