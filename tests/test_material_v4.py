"""material-extract/v4 contract, content+channel identity and node provenance."""
import asyncio
import logging
import json

import pytest
from pydantic import ValidationError

from core.async_database import AsyncDatabaseManager
from osbi_contracts.material import material_hash
from osbi_contracts.models import Material, SourceMeta
from wiseflow_server.models import ExtractRequest
from wiseflow_server.ontology_queue import OntologyQueue
from wiseflow_server.runtime_config import ChannelConfig


def request(**overrides):
    data = dict(materialId='a' * 64, channel='community', nodeId='linfen-shanghai',
                input={'title': '材料', 'blocks': [{'id': 'B1', 'text': '同一段原文。'}],
                       'links': []},
                meta={'requestedUrl': 'https://example.org/a'})
    data.update(overrides)
    return ExtractRequest.model_validate(data)


def test_v4_requires_channel_and_rejects_focus_and_v3():
    with pytest.raises(ValidationError):
        request(channel='Community')
    data = request().model_dump()
    data.pop('channel')
    with pytest.raises(ValidationError):
        ExtractRequest.model_validate(data)
    with pytest.raises(ValidationError):
        request(focus={'id': 10, 'focuspoint': '旧关注'})
    with pytest.raises(ValidationError):
        request(protocolVersion='material-extract/v3')


def test_material_hash_uses_content_and_channel():
    base = dict(clientId='c', requestId='r', blocks=[{'id': 'B1', 'text': '同一段原文。'}],
                links=[], meta=SourceMeta(requestedUrl='https://example.org/a'))
    first = material_hash(Material(**base, channel='community'))
    second = material_hash(Material(**base, channel='economy'))
    assert first != second
    # Candidate-ID renumbering keeps the same identity.
    renumbered = dict(base, blocks=[{'id': 'B7', 'text': '同一段原文。'}])
    assert first == material_hash(Material(**renumbered, channel='community'))


def test_queue_dedupes_one_content_channel_job_across_nodes(tmp_path):
    async def run():
        db = AsyncDatabaseManager(db_path=tmp_path / 'data.db', logger=logging.getLogger('test'))
        await db.initialize()
        try:
            queue = OntologyQueue(db)
            channel = ChannelConfig(id='community', title='社区治理', schema_ref='policy',
                                    statement='社区治理。')
            first = await queue.enqueue(request(nodeId='linfen-shanghai'),
                                        request().input.blocks[0], channel=channel,
                                        library_id='policy')
            second = await queue.enqueue(request(nodeId='shizishan-chengdu'),
                                         request().input.blocks[0], channel=channel,
                                         library_id='policy')
            assert first.id == second.id
            assert second.cached is True
            job = await queue.get(first.id)
            assert job['payload']['channel'] == 'community'
            assert job['payload']['nodeIds'] == ['linfen-shanghai', 'shizishan-chengdu']
            # Different channels are distinct jobs.
            other = await queue.enqueue(
                request(channel='party-building', nodeId='linfen-shanghai'),
                request().input.blocks[0],
                channel=ChannelConfig(id='party-building', title='基层党建', schema_ref='policy',
                                      statement='基层党建。'), library_id='policy')
            assert other.id != first.id
        finally:
            await db.cleanup()

    asyncio.run(run())


def test_response_has_no_infos_or_info_count():
    from wiseflow_server.models import ExtractResponse
    dumped = ExtractResponse(materialId='a' * 64).model_dump()
    assert 'infos' not in dumped and 'infoCount' not in dumped
    assert dumped['protocolVersion'] == 'material-extract/v4'
