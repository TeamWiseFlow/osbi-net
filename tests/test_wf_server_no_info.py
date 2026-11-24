"""material-extract/v4 without the info stage: channel selection + queue only.

Unit-level tests against wiseflow_server; no lifespan, database or model API.
"""
import asyncio
from datetime import datetime
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from wiseflow_server import llmuse, worker
from wiseflow_server.models import (ExtractRequest, ExtractResponse,
                                    OntologyJobReceipt, StageError)
from wiseflow_server.runtime_config import ChannelConfig

MATERIAL_ID = 'a' * 64
JOB_ID = 'oq_' + 'b' * 64
NOW = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai'))

CHANNELS = {
    'community': ChannelConfig(id='community', title='社区治理', schema_ref='policy',
                               statement='社区治理与居民服务。'),
    'policy-latest': ChannelConfig(id='policy-latest', title='政策速递', schema_ref='policy',
                                   statement='最近三天内发布的政策。', date_window_days=3,
                                   date_field='publishedDate', rules={'policyContent': True}),
}


class FakeConfig:
    channels = CHANNELS

    def channel(self, channel_id):
        if channel_id not in CHANNELS:
            raise ValueError(f'Unknown channel: {channel_id}')
        return CHANNELS[channel_id]


class FakeQueue:
    def __init__(self, fail=False):
        self.fail = fail
        self.enqueued = []

    async def enqueue(self, request, block, *, channel, library_id, publication=None):
        if self.fail:
            raise RuntimeError('queue write failed')
        self.enqueued.append((block.id, channel.id, library_id, publication.source if publication else ''))
        return OntologyJobReceipt(blockId=block.id, id=JOB_ID, status='pending')


class FakeMetrics:
    def __init__(self):
        self.calls = []

    async def record_submission(self, node_id, meta, channel=None):
        self.calls.append(('submission', node_id))

    async def record_selection(self, node_id, meta, *, selected_blocks=0, selected_links=0):
        self.calls.append(('selection', node_id, selected_blocks, selected_links))


def make_service(chat, queue=None):
    """ExtractionService without __init__: only extract() collaborators."""
    service = worker.ExtractionService.__new__(worker.ExtractionService)
    service.settings = SimpleNamespace(selected_model='sel', fallback_model='fb',
                                       performance_model='perf')
    service.config = FakeConfig()
    service.chat = chat
    service.cache = None
    service._locks = {}
    service.stores = SimpleNamespace(resolve=lambda key=None: key or 'policy')
    service.queue = queue if queue is not None else FakeQueue()
    service.metrics = FakeMetrics()
    return service


def select_all_chat(calls):
    async def chat(messages, model):
        calls.append(model)
        payload = json.loads(messages[1]['content'])
        material = payload['material']
        return json.dumps({'blockIds': [b['id'] for b in material['blocks']],
                           'linkIds': [l['id'] for l in material['links']]})
    return chat


def request(**overrides):
    data = dict(materialId=MATERIAL_ID, channel='community',
                nodeId='linfen-shanghai',
                input={'title': '某社区活动',
                       'blocks': [{'id': 'B1', 'text': '甲社区开展志愿活动。'},
                                  {'id': 'B2', 'text': '乙社区召开座谈会。'}],
                       'links': [{'id': 'L1', 'text': '原文链接', 'role': 'unknown'}]},
                meta={'requestedUrl': 'https://example.org/a', 'publishedAt': '2026-11-22',
                      'capturedAt': '2026-11-23T10:00:00+08:00'})
    data.update(overrides)
    return ExtractRequest.model_validate(data)


def test_response_contract_has_no_info_fields():
    dumped = ExtractResponse(materialId=MATERIAL_ID).model_dump()
    assert set(dumped) == {'protocolVersion', 'materialId', 'selection',
                           'modelCalls', 'ontologyJobs', 'errors'}
    assert dumped['protocolVersion'] == 'material-extract/v4'


def test_focus_and_missing_channel_are_rejected():
    with pytest.raises(ValidationError):
        request(focus={'id': 10, 'focuspoint': '旧关注'})
    data = request().model_dump()
    data.pop('channel')
    with pytest.raises(ValidationError):
        ExtractRequest.model_validate(data)


def test_only_info_mode_is_rejected():
    with pytest.raises(ValidationError):
        request(mode='only_info')


def test_info_stage_error_is_rejected():
    with pytest.raises(ValidationError):
        StageError(stage='info', message='x')
    with pytest.raises(ValidationError):
        StageError(stage='date_window', message='x')


def test_info_helpers_are_gone():
    assert not hasattr(llmuse, 'summarize_block')
    assert not hasattr(llmuse, 'INFO_PROMPT')
    assert hasattr(llmuse, 'select_relevance')
    assert hasattr(llmuse, 'RELEVANCE_PROMPT')
    assert not hasattr(worker, 'journal_prefix')
    assert not hasattr(worker, 'focus_window_days')
    assert not hasattr(worker.ExtractionService, '_info')


def test_extract_enqueues_without_info_stage():
    calls = []
    queue = FakeQueue()
    service = make_service(select_all_chat(calls), queue)
    response = asyncio.run(service.extract(request(), now=NOW))
    assert calls == ['sel']
    assert response.modelCalls == 1
    assert queue.enqueued == [('B1', 'community', 'policy', 'meta'),
                              ('B2', 'community', 'policy', 'meta')]
    assert [j.blockId for j in response.ontologyJobs] == ['B1', 'B2']
    assert response.errors == []
    assert 'infos' not in response.model_dump()


def test_extract_queue_failure_reports_ontology_queue_stage_only():
    service = make_service(select_all_chat([]), FakeQueue(fail=True))
    response = asyncio.run(service.extract(request(), now=NOW))
    assert response.ontologyJobs == []
    assert {e.stage for e in response.errors} == {'ontology_queue'}
    assert len(response.errors) == 2


def test_window_channel_rejects_stale_material():
    queue = FakeQueue()
    service = make_service(select_all_chat([]), queue)
    stale = request(channel='policy-latest',
                    meta={'requestedUrl': 'https://example.org/a',
                          'publishedAt': '2020-01-01',
                          'capturedAt': '2020-01-02T10:00:00+08:00'})
    response = asyncio.run(service.extract(stale, now=NOW))
    assert response.selection.blockIds == []
    assert response.selection.linkIds == ['L1']
    assert queue.enqueued == []
    assert response.ontologyJobs == []
    assert [e.message for e in response.errors] == ['date_window', 'date_window']
    assert [e.blockId for e in response.errors] == ['B1', 'B2']
    assert response.errors[0].stage == 'date_gate'


def test_window_channel_rejects_undated_material():
    service = make_service(select_all_chat([]))
    undated = request(channel='policy-latest',
                      meta={'requestedUrl': 'https://example.org/a',
                            'capturedAt': '2026-11-23T10:00:00+08:00'})
    response = asyncio.run(service.extract(undated, now=NOW))
    assert response.selection.blockIds == []
    assert [e.message for e in response.errors] == ['date_unknown', 'date_unknown']


def test_non_window_channel_accepts_undated_material():
    service = make_service(select_all_chat([]))
    undated = request(meta={'requestedUrl': 'https://example.org/a'})
    response = asyncio.run(service.extract(undated, now=NOW))
    assert response.selection.blockIds == ['B1', 'B2']


def test_only_link_mode_still_supported():
    service = make_service(select_all_chat([]), FakeQueue())
    response = asyncio.run(service.extract(request(mode='only_link'), now=NOW))
    assert response.selection.blockIds == []
    assert response.selection.linkIds == ['L1']
    assert response.ontologyJobs == []
