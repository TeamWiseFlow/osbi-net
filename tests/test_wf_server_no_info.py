"""material-extract/v3 without the info stage: selection + ontology queue only.

Unit-level tests against wiseflow_server; no lifespan, database or model API.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from wiseflow_server import llmuse, worker
from wiseflow_server.models import (ExtractRequest, ExtractResponse,
                                    OntologyJobReceipt, StageError)

MATERIAL_ID = 'a' * 64
JOB_ID = 'oq_' + 'b' * 64


class FakeLibraries:
    def resolve(self, library_id=None, *, focus_id=None):
        if library_id in (None, 'policy', 'economy'):
            return library_id or 'policy'
        raise ValueError(f'Unknown ontology library: {library_id}')


class FakeQueue:
    def __init__(self, fail=False):
        self.fail = fail
        self.enqueued = []

    async def enqueue(self, request, block, *, library_id='policy'):
        if self.fail:
            raise RuntimeError('queue write failed')
        self.enqueued.append((block.id, library_id))
        return OntologyJobReceipt(blockId=block.id, id=JOB_ID, status='pending')


def make_service(chat, queue=None):
    """ExtractionService without __init__: only extract() collaborators."""
    service = worker.ExtractionService.__new__(worker.ExtractionService)
    service.settings = SimpleNamespace(selected_model='sel', fallback_model='fb',
                                       performance_model='perf')
    service.chat = chat
    service.cache = None
    service._locks = {}
    service.libraries = FakeLibraries()
    service.queue = queue if queue is not None else FakeQueue()
    return service


def select_all_chat(calls):
    async def chat(messages, model):
        calls.append(model)
        payload = json.loads(messages[1]['content'])
        return json.dumps({'blockIds': [b['id'] for b in payload['material']['blocks']],
                           'linkIds': [l['id'] for l in payload['material']['links']]})
    return chat


def request(**overrides):
    data = dict(materialId=MATERIAL_ID,
                focus={'id': 11, 'focuspoint': '关注政策发布'},
                input={'title': '某政策发布',
                       'blocks': [{'id': 'B1', 'text': '甲部门发布某办法。'},
                                  {'id': 'B2', 'text': '乙部门召开座谈会。'}],
                       'links': [{'id': 'L1', 'text': '原文链接'}]},
                meta={'requestedUrl': 'https://example.org/a', 'publishedAt': '2026-11-20'})
    data.update(overrides)
    return ExtractRequest.model_validate(data)


def test_response_contract_has_no_info_fields():
    # Arrange / Act
    dumped = ExtractResponse(materialId=MATERIAL_ID).model_dump()
    # Assert
    assert set(dumped) == {'protocolVersion', 'materialId', 'selection',
                           'modelCalls', 'ontologyJobs', 'errors'}


def test_only_info_mode_is_rejected():
    with pytest.raises(ValidationError):
        request(mode='only_info')


def test_info_stage_error_is_rejected():
    with pytest.raises(ValidationError):
        StageError(stage='info', message='x')


def test_info_helpers_are_gone():
    assert not hasattr(llmuse, 'summarize_block')
    assert not hasattr(llmuse, 'INFO_PROMPT')
    assert hasattr(llmuse, 'select_relevance')
    assert hasattr(llmuse, 'RELEVANCE_PROMPT')
    assert not hasattr(worker, 'journal_prefix')
    assert not hasattr(worker.ExtractionService, '_info')


def test_extract_enqueues_without_info_stage():
    # Arrange
    calls = []
    queue = FakeQueue()
    service = make_service(select_all_chat(calls), queue)
    # Act
    response = asyncio.run(service.extract(request()))
    # Assert: one relevance call only, both blocks queued, no info output
    assert calls == ['sel']
    assert response.modelCalls == 1
    assert queue.enqueued == [('B1', 'policy'), ('B2', 'policy')]
    assert [j.blockId for j in response.ontologyJobs] == ['B1', 'B2']
    assert response.errors == []
    dumped = response.model_dump()
    assert 'infos' not in dumped and 'infoCount' not in dumped


def test_extract_queue_failure_reports_ontology_queue_stage_only():
    # Arrange
    service = make_service(select_all_chat([]), FakeQueue(fail=True))
    # Act
    response = asyncio.run(service.extract(request()))
    # Assert
    assert response.ontologyJobs == []
    assert {e.stage for e in response.errors} == {'ontology_queue'}
    assert len(response.errors) == 2


def test_recency_gate_still_blocks_stale_material():
    # Arrange: explicit "最近3天" focus with an old publish date
    queue = FakeQueue()
    service = make_service(select_all_chat([]), queue)
    stale = request(focus={'id': 11, 'focuspoint': '最近3天内的政策发布'},
                    meta={'requestedUrl': 'https://example.org/a',
                          'publishedAt': '2020-01-01'})
    # Act
    response = asyncio.run(service.extract(stale))
    # Assert: hard date gate keeps the ontology queue clean
    assert response.selection.blockIds == []
    assert response.selection.linkIds == []
    assert queue.enqueued == []
    assert response.ontologyJobs == []


def test_only_link_mode_still_supported():
    # Arrange
    queue = FakeQueue()
    service = make_service(select_all_chat([]), queue)
    # Act
    response = asyncio.run(service.extract(request(mode='only_link')))
    # Assert
    assert response.selection.blockIds == []
    assert response.selection.linkIds == ['L1']
    assert queue.enqueued == []
