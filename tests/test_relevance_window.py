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

    async def enqueue(self, request, block, *, channel, library_id, publication=None):
        self.enqueued.append((block.id, channel.id, publication.date if publication else ''))
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
                       'links': [{'id': 'L1', 'text': '原文链接'}]},
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
    assert response.selection.linkIds == []
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
