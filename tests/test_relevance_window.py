"""Relevance prompt + deterministic date-window fallback."""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from wiseflow_server import llmuse, worker
from wiseflow_server.models import ExtractRequest, OntologyJobReceipt
from wiseflow_server.runtime_config import ChannelConfig

NOW = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
WINDOW = dict(days=3, asOf='2026-11-23', **{'from': '2026-11-21', 'to': '2026-11-23'})
POLICY_CHANNEL = ChannelConfig(id='policy-latest', title='政策速递', schema_ref='policy',
                               statement='最近三天内发布的政策。', date_window_days=3)
COMMUNITY_CHANNEL = ChannelConfig(id='community', title='社区治理', schema_ref='policy',
                                  statement='社区治理与居民服务。')


def test_prompt_carries_channel_statement_and_date_window():
    seen = []

    async def chat(messages, model):
        seen.append(messages)
        return json.dumps({'blockIds': [], 'linkIds': []})

    selection = asyncio.run(llmuse.select_relevance(
        chat, POLICY_CHANNEL, WINDOW,
        {'title': '材料', 'blocks': [], 'links': []}, 'sel', 'fb'))
    assert selection.blockIds == []
    system = seen[0][0]['content']
    assert '发布日期窗口' in system
    assert '2026-11-21' in system and '2026-11-23' in system
    payload = json.loads(seen[0][1]['content'])
    assert payload['channel']['id'] == 'policy-latest'
    assert payload['channel']['statement'] == POLICY_CHANNEL.statement
    assert payload['dateWindow'] == WINDOW


def test_catalog_prompt_defers_the_date_window_to_article_bodies():
    seen = []

    async def chat(messages, model):
        seen.append(messages)
        return json.dumps({'blockIds': [], 'linkIds': []})

    asyncio.run(llmuse.select_relevance(
        chat, POLICY_CHANNEL, WINDOW,
        {'title': '目录', 'blocks': [], 'links': [{'id': 'L1', 'text': '政策标题'}]},
        'sel', 'fb', catalog=True))
    system = seen[0][0]['content']
    assert '目录/链接阶段' in system
    assert '发布日期窗口不在本阶段执行' in system
    assert '不得选择任何 block 或 link' not in system


def test_non_window_prompt_has_no_date_gate_language():
    seen = []

    async def chat(messages, model):
        seen.append(messages)
        return json.dumps({'blockIds': [], 'linkIds': []})

    asyncio.run(llmuse.select_relevance(
        chat, COMMUNITY_CHANNEL, None,
        {'title': '材料', 'blocks': [], 'links': []}, 'sel', 'fb'))
    assert '发布日期窗口' not in seen[0][0]['content']
    assert 'dateWindow' not in json.loads(seen[0][1]['content'])


# --------------------------------------------------------------------------
# Deterministic worker fallback
# --------------------------------------------------------------------------

class FakeConfig:
    def channel(self, channel_id):
        if channel_id == 'policy-latest':
            return POLICY_CHANNEL
        if channel_id == 'community':
            return COMMUNITY_CHANNEL
        raise ValueError(channel_id)


class FakeQueue:
    def __init__(self):
        self.enqueued = []
        self.publications = []

    async def enqueue(self, request, block, *, channel, library_id, publication=None):
        self.enqueued.append((block.id, channel.id, publication.date if publication else ''))
        self.publications.append(publication)
        return OntologyJobReceipt(blockId=block.id, id='oq_' + 'b' * 64, status='pending')


class FakeMetrics:
    async def record_submission(self, *args, **kwargs):
        return None

    async def record_selection(self, *args, **kwargs):
        return None


def make_service(chat, queue):
    service = worker.ExtractionService.__new__(worker.ExtractionService)
    service.settings = SimpleNamespace(selected_model='sel', fallback_model='fb')
    service.config = FakeConfig()
    service.chat = chat
    service.cache = None
    service._locks = {}
    service.stores = SimpleNamespace(resolve=lambda key=None: key or 'policy')
    service.queue = queue
    service.metrics = FakeMetrics()
    return service


def select_all_chat():
    async def chat(messages, model):
        payload = json.loads(messages[1]['content'])['material']
        return json.dumps({'blockIds': [b['id'] for b in payload['blocks']],
                           'linkIds': [l['id'] for l in payload['links']]})
    return chat


def request(**overrides):
    data = dict(materialId='a' * 64, channel='policy-latest', nodeId='linfen-shanghai',
                input={'title': '某政策通知',
                       'blocks': [{'id': 'B1', 'text': '政策正文。'}],
                       'links': [{'id': 'L1', 'text': '原文链接', 'role': 'unknown'}]},
                meta={'requestedUrl': 'https://example.org/a'})
    data.update(overrides)
    return ExtractRequest.model_validate(data)


def test_model_selection_is_cleared_when_date_is_outside_window():
    queue = FakeQueue()
    service = make_service(select_all_chat(), queue)
    response = asyncio.run(service.extract(
        request(meta={'requestedUrl': 'https://example.org/a',
                      'publishedAt': '2026-11-18'}), now=NOW))
    assert response.selection.blockIds == []
    assert response.selection.linkIds == ['L1']
    assert queue.enqueued == []
    assert response.errors[0].stage == 'date_gate'
    assert response.errors[0].message == 'date_window'


def test_model_selection_is_cleared_when_date_is_unknown():
    queue = FakeQueue()
    service = make_service(select_all_chat(), queue)
    response = asyncio.run(service.extract(request(), now=NOW))
    assert response.selection.blockIds == []
    assert response.errors[0].message == 'date_unknown'


def test_date_gate_is_not_a_job_failure():
    queue = FakeQueue()
    service = make_service(select_all_chat(), queue)
    response = asyncio.run(service.extract(
        request(meta={'requestedUrl': 'https://example.org/a',
                      'publishedAt': '2020-01-01'}), now=NOW))
    assert response.ontologyJobs == []
    assert {error.stage for error in response.errors} == {'date_gate'}


def test_catalog_selects_undated_links_without_enqueuing_supplied_blocks():
    async def run():
        seen = []
        async def chat(messages, model):
            seen.append(messages)
            payload = json.loads(messages[1]['content'])['material']
            assert payload['blocks'] == []
            assert '发布日期窗口不在本阶段执行' in messages[0]['content']
            return json.dumps({'blockIds': [], 'linkIds': ['L1']})
        queue = FakeQueue()
        service = make_service(chat, queue)
        for published in [None, '2020-01-01']:
            meta = {'requestedUrl': 'https://example.org/catalog'}
            if published:
                meta['publishedAt'] = published
            response = await service.extract(request(mode='only_link', meta=meta), now=NOW)
            assert response.selection.blockIds == []
            assert response.selection.linkIds == ['L1']
            assert response.errors == [] and response.ontologyJobs == []
        assert queue.enqueued == [] and len(seen) == 2
        # The subsequent article request continues to enforce its own date.
        body_service = make_service(select_all_chat(), queue)
        stale = await body_service.extract(request(meta={'publishedAt': '2020-01-01'}), now=NOW)
        assert stale.errors[0].message == 'date_window' and queue.enqueued == []
        fresh = await body_service.extract(request(meta={'publishedAt': '2026-11-23'}), now=NOW)
        assert fresh.errors == [] and len(queue.enqueued) == 1
    asyncio.run(run())


def test_catalog_and_article_selection_caches_are_isolated_by_mode():
    async def run():
        calls = []
        async def chat(messages, model):
            catalog = '目录/链接阶段' in messages[0]['content']
            calls.append(catalog)
            return json.dumps({'blockIds': [], 'linkIds': ['L1'] if catalog else []})
        class Cache:
            def __init__(self): self.values = {}
            async def get(self, key, *, namespace): return self.values.get((namespace, key))
            async def set(self, key, value, ttl, *, namespace): self.values[namespace, key] = value
        queue = FakeQueue()
        service = make_service(chat, queue)
        service.cache = Cache()
        payload = {'title': '政策目录', 'blocks': [], 'links': [{'id': 'L1', 'text': '政策文件'}]}
        for mode in ['only_link', 'both', 'only_link']:
            response = await service.extract(request(mode=mode, input=payload), now=NOW)
            if mode == 'only_link':
                assert response.selection.linkIds == ['L1'] and response.errors == []
            else:
                assert response.selection.linkIds == [] and response.errors == []
        assert calls == [True, False]
        assert len(service.cache.values) == 2 and queue.enqueued == []
    asyncio.run(run())


def test_unknown_page_gates_only_selected_blocks_and_keeps_navigation():
    queue = FakeQueue()
    service = make_service(select_all_chat(), queue)
    page = request(input={'title': '政策列表',
        'blocks': [{'id': 'B1', 'text': '栏目简介。'}, {'id': 'B2', 'text': '办理说明。'}],
        'links': [{'id': 'L1', 'text': '政策甲'}, {'id': 'L2', 'text': '政策乙'}, {'id': 'L3', 'text': '政策丙'}]})
    response = asyncio.run(service.extract(page, now=NOW))
    assert response.selection.blockIds == []
    assert response.selection.linkIds == ['L1', 'L2', 'L3']
    assert {(e.stage, e.blockId, e.message) for e in response.errors} == {
        ('date_gate', 'B1', 'date_unknown'), ('date_gate', 'B2', 'date_unknown')}
    assert queue.enqueued == [] and response.ontologyJobs == []


def test_link_only_selection_on_undated_both_page_has_no_block_error():
    async def chat(messages, model):
        return json.dumps({'blockIds': [], 'linkIds': ['L1']})
    service = make_service(chat, FakeQueue())
    response = asyncio.run(service.extract(request(), now=NOW))
    assert response.selection.blockIds == [] and response.selection.linkIds == ['L1']
    assert response.errors == [] and response.ontologyJobs == []


def test_page_date_does_not_gate_links_but_link_release_fields_do():
    async def run():
        links = [{'id': 'L1', 'text': '某政策', 'context': '发布日期：2020-01-01'},
                 {'id': 'L2', 'text': '2026-11-22 某政策'},
                 {'id': 'L3', 'text': '新政策'},
                 {'id': 'L4', 'text': '2020-01-01 举办培训活动'},
                 {'id': 'L5', 'text': '政策', 'context': '发布日期：2026-02-30'},
                 {'id': 'L6', 'text': '政策', 'context': '发布日期：2026-11-22；更新日期：2026-11-23'}]
        for mode in ['both', 'only_link']:
            queue = FakeQueue()
            service = make_service(select_all_chat(), queue)
            page = request(mode=mode, meta={'publishedAt': '2020-01-01'},
                input={'title': '某政策', 'blocks': [{'id': 'B1', 'text': '正文。'}], 'links': links})
            result = await service.extract(page, now=NOW)
            assert result.selection.blockIds == []
            assert result.selection.linkIds == ['L2', 'L3', 'L4']
            assert [e.message for e in result.errors] == (['date_window'] if mode=='both' else [])
            assert queue.enqueued == []
        queue = FakeQueue()
        ordinary = make_service(select_all_chat(), queue)
        result = await ordinary.extract(request(channel='community', meta={'publishedAt':'2020-01-01'},
            input={'title':'某政策', 'blocks':[{'id':'B1','text':'正文。'}], 'links':links}), now=NOW)
        assert result.selection.blockIds == ['B1']
        assert result.selection.linkIds == [link['id'] for link in links]
        assert result.errors == [] and len(queue.enqueued)==1
    asyncio.run(run())


def test_block_rejection_reasons_keep_links_and_publication_provenance():
    async def run():
        cases = [({}, '正文。', 'date_unknown'),
                 ({'publishedAt':'2020-01-01'}, '正文。', 'date_window'),
                 ({}, '发布日期：2026-11-21。公布日期：2026-11-22。', 'date_ambiguous'),
                 ({'publishedAt':'2026-11-25'}, '正文。', 'date_invalid'),
                 ({'capturedAt':'2026-11-23T10:00:00+08:00'}, '近日发布。', 'date_unknown')]
        for meta, text, reason in cases:
            queue = FakeQueue()
            service = make_service(select_all_chat(), queue)
            result = await service.extract(request(meta=meta, input={'title':'通知',
                'blocks':[{'id':'B1','text':text}], 'links':[{'id':'L1','text':'相关政策'}]}), now=NOW)
            assert result.selection.blockIds == [] and result.selection.linkIds == ['L1']
            assert [(e.blockId,e.message) for e in result.errors] == [('B1',reason)]
            assert queue.enqueued == []
        queue = FakeQueue()
        service = make_service(select_all_chat(),queue)
        result = await service.extract(request(meta={'publishedAt':'2026-11-22'}), now=NOW)
        assert result.selection.blockIds == ['B1'] and result.errors == []
        assert queue.enqueued == [('B1','policy-latest','2026-11-22')]
        publication = queue.publications[-1]
        assert (publication.source,publication.reference,publication.estimated,publication.conflict) == ('meta','meta',False,False)
        relative = request(meta={'capturedAt':'2026-11-23T10:00:00+08:00'}, input={'title':'通知',
            'blocks':[{'id':'B1','text':'昨天发布。'}], 'links':[]})
        result = await service.extract(relative, now=NOW)
        publication = queue.publications[-1]
        assert result.errors == [] and publication.source == 'body_relative'
        assert publication.expression == '昨天' and publication.reference == 'capturedAt'
        assert publication.date == '2026-11-22' and not publication.estimated
    asyncio.run(run())


def test_date_gated_blocks_with_retained_links_are_not_failed_monitor_jobs():
    class Monitor:
        def __init__(self): self.events=[]
        async def emit(self,job_id,job_type,**changes): self.events.append(changes)
    async def run():
        service=make_service(select_all_chat(),FakeQueue());service.monitor=Monitor()
        result=await service.extract(request(),now=NOW)
        assert result.selection.blockIds == [] and result.selection.linkIds == ['L1']
        final=service.monitor.events[-1]
        assert final['status']=='succeeded' and final['error_code']=='date_gate'
        assert final['selected_blocks']==0 and final['selected_links']==1
    asyncio.run(run())


def test_window_prompt_revision_invalidates_old_cache_but_non_window_identity_stays():
    class Cache:
        def __init__(self): self.values={}
        async def get(self,key,*,namespace): return self.values.get((namespace,key))
        async def set(self,key,value,ttl,*,namespace): self.values[namespace,key]=value
    async def run():
        calls=[]
        async def chat(messages,model):
            calls.append(messages)
            return json.dumps({'blockIds':['B1'],'linkIds':['L1']})
        for channel_id, window in [('policy-latest', WINDOW),('community',None)]:
            service=make_service(chat,FakeQueue());service.cache=Cache()
            payload=request(channel=channel_id,meta={'publishedAt':'2026-11-22'})
            channel=service.config.channel(channel_id)
            old=worker.digest(['relevance/v4',channel.id,channel.statement_fingerprint,
                channel.rules_fingerprint,window,payload.mode,payload.input.model_dump(exclude_none=True),'sel','fb'])
            service.cache.values['relevance/v4',old]={'blockIds':[],'linkIds':['L1']}
            response=await service.extract(payload,now=NOW)
            assert response.selection.linkIds==['L1']
            assert response.selection.blockIds==(['B1'] if window else [])
        assert len(calls)==1  # Only the windowed prompt changed.
    asyncio.run(run())


def test_block_publication_is_available_to_prompt_and_in_selection_identity():
    async def run():
        seen=[]
        async def chat(messages,model):
            payload=json.loads(messages[1]['content']);seen.append(payload)
            good=payload['blockPublication']['reason']==''
            return json.dumps({'blockIds':['B1'] if good else [],'linkIds':['L1']})
        class Cache:
            def __init__(self):self.values={}
            async def get(self,key,*,namespace):return self.values.get((namespace,key))
            async def set(self,key,value,ttl,*,namespace):self.values[namespace,key]=value
        service=make_service(chat,FakeQueue());service.cache=Cache()
        for published in ['2020-01-01','2026-11-22']:
            result=await service.extract(request(meta={'publishedAt':published}),now=NOW)
            assert result.selection.linkIds==['L1'] and result.errors==[]
            assert result.selection.blockIds==([] if published=='2020-01-01' else ['B1'])
        assert len(seen)==2 and len(service.cache.values)==2
        assert seen[0]['blockPublication']['reason']=='date_window'
        assert seen[1]['blockPublication']['date']=='2026-11-22'
    asyncio.run(run())
