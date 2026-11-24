"""Channel provenance replaces focus routing in identity and stored results."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from wiseflow_server.models import Block
from wiseflow_server.ontology import OntologyExtractor
from wiseflow_server.publication_date import PublicationDate
from wiseflow_server.read_model import build_snapshot
from wiseflow_server.runtime_config import ChannelConfig

SCHEMA = Path(__file__).resolve().parents[1] / 'server' / 'wiseflow_server' / 'schemas' / 'policy.ttl'
COMMUNITY = ChannelConfig(id='community', title='社区治理', schema_ref='policy',
                          statement='社区治理与居民服务。')
OTHER = ChannelConfig(id='digital', title='数字社区', schema_ref='policy',
                      statement='数字治理与智慧社区。')
META = {'requestedUrl': 'https://example.org/a', 'publisher': '某发布'}


def settings(tmp_path):
    return SimpleNamespace(ontology_model='onto', fallback_model='fb',
                           ontology_chunk_chars=3000, ontology_chunk_overlap=0,
                           schema_path=SCHEMA)


def test_identity_tracks_channel_statement_not_focus(tmp_path):
    extractor = OntologyExtractor(settings(tmp_path), tmp_path / 'graph')
    try:
        block = Block(id='B1', text='临汾路街道推进社区治理。')
        first, _, _ = extractor.identity(channel=COMMUNITY, title='社区治理动态',
                                         block=block, meta=META)
        assert first == extractor.identity(channel=COMMUNITY, title='社区治理动态',
                                           block=block, meta=META)[0]
        assert first != extractor.identity(channel=OTHER, title='社区治理动态',
                                           block=block, meta=META)[0]
    finally:
        extractor.store.close()


def test_extract_provenance_has_channel_and_no_focus(tmp_path):
    async def run():
        extractor = OntologyExtractor(settings(tmp_path), tmp_path / 'graph')
        try:
            async def chat(messages, model):
                payload = json.loads(messages[1]['content'])
                if payload['stage'] == 'ontology_entities':
                    return json.dumps({'entities': [
                        {'text': '临汾路街道', 'label': '街镇机构'}]})
                return json.dumps({'relations': []})

            result = await extractor.extract(
                channel=COMMUNITY, title='临汾路街道推进社区治理',
                block=Block(id='B1', text='临汾路街道推进社区治理。'),
                meta=META, publication=PublicationDate(date='2026-11-21', source='meta',
                                                       reference='meta'),
                material_id='m' * 64, chat=chat, node_ids=['linfen-shanghai'])
            provenance = result['provenance'] if 'provenance' in result else None
            if provenance is None:
                provenance = (await asyncio.to_thread(extractor.store.read, result['id']))['provenance']
            assert provenance['channel'] == 'community'
            assert provenance['channel_title'] == '社区治理'
            assert provenance['node_ids'] == ['linfen-shanghai']
            assert provenance['published_date'] == '2026-11-21'
            assert provenance['date_source'] == 'meta'
            assert 'focus_id' not in provenance and 'focus' not in provenance
            assert 'board_label' not in provenance
        finally:
            extractor.store.close()

    asyncio.run(run())


def test_read_model_uses_channel_title_for_board():
    result = dict(
        id='o_' + 'a' * 64, schemaId='schema', entities=[], relations=[],
        provenance=dict(channel='community', channel_title='社区治理',
                        source_url='https://example.org/x', source_title='材料',
                        published_date='2026-11-21', date_source='meta', raw_text='正文'))
    snapshot = build_snapshot('policy', [result], include_seeds=False)
    block = next(iter(snapshot.blocks.values()))
    assert block['channel'] == 'community'
    assert block['board'] == '社区治理'
    assert block['dateSource'] == 'meta'
    assert 'focusId' not in block
