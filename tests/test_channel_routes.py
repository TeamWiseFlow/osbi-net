"""Channel-scoped evidence route and node-key authentication."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from wiseflow_server.app import create_app
from wiseflow_server.runtime_config import (ChannelConfig, NodeKeyRegistry,
                                            StoreConfig, StoreRegistry)

TOKEN = 'node-token'
SCHEMA = Path(__file__).resolve().parents[1] / 'server' / 'wiseflow_server' / 'schemas' / 'policy.ttl'


class FakeStore:
    def __init__(self, results):
        self.results = results

    def read(self, extraction_id):
        return self.results.get(extraction_id)


class FakeQueue:
    def __init__(self, jobs):
        self.jobs = jobs

    async def done_receipts(self, library_id=None):
        return [dict(id=job['id'], receipt=job['receipt']) for job in self.jobs
                if library_id is None or job['library_id'] == library_id]


def block(extraction_id, node_ids, entities=(), *, channel='community', title='社区治理'):
    return dict(
        id=extraction_id,
        provenance=dict(channel=channel, channel_title=title,
                        node_ids=list(node_ids), source_url='https://example.org/x',
                        source_title='材料', published_date='2026-11-20',
                        date_source='meta', raw_text='正文。',
                        source_meta={'publisher': '某发布'}),
        entities=[dict(uri='https://onto.pilot.local/instance/' + name, text=name, label=label)
                  for name, label in entities],
        relations=[])


def service():
    stores = StoreRegistry({
        'policy': StoreConfig(id='policy', title='政策与治理图谱', schema_path=SCHEMA,
                              graph_path=Path('/tmp/policy'), schema_fingerprint=''),
        'economy': StoreConfig(id='economy', title='经济动态图谱', schema_path=SCHEMA,
                               graph_path=Path('/tmp/economy'), schema_fingerprint=''),
    }, 'policy')
    channels = {
        'community': ChannelConfig(id='community', title='社区治理', schema_ref='policy',
                                   statement='社区治理。'),
        'policy-latest': ChannelConfig(id='policy-latest', title='政策速递', schema_ref='policy',
                                       statement='政策。', date_window_days=3),
    }
    stores.attach_channels(channels)
    results = {
        'o_a': block('o_a', ('linfen-shanghai', 'shizishan-chengdu'),
                     [('上海市', '市级机构')]),
        'o_b': block('o_b', ('shizishan-chengdu',), [('成都市', '市级机构')],
                     channel='policy-latest', title='政策速递'),
    }
    jobs = [dict(id='j' + name, library_id='policy', receipt={'id': name}) for name in results]
    config = SimpleNamespace(auth=NodeKeyRegistry(
        {hashlib.sha256(TOKEN.encode()).hexdigest(): 'linfen-shanghai'}, mode='node-key'),
        channels=channels)

    def channel(key):
        if key not in channels:
            raise ValueError(f'Unknown channel: {key}')
        return channels[key]

    return SimpleNamespace(
        stores=stores, libraries=stores, channel=channel,
        queue=FakeQueue(jobs), engine=lambda key=None: SimpleNamespace(store=FakeStore(results)),
        settings=SimpleNamespace(config=config, selected_model='sel', performance_model='perf',
                                 ontology_model='onto', fallback_model='fb',
                                 ontology_batch_time='03:00',
                                 ontology_batch_timezone='Asia/Shanghai', seed_paths=None),
        db=SimpleNamespace(db_path='/tmp/fake.db'), ontology_engine=None)


def test_channel_evidence_requires_node_key_and_filters_scope():
    active = service()
    with TestClient(create_app(service=active)) as client:
        assert client.post('/v1/channels/community/evidence', json={}).status_code == 401
        headers = {'Authorization': 'Bearer ' + TOKEN}
        body = client.post('/v1/channels/community/evidence', headers=headers, json={}).json()
        assert body['channel'] == 'community'
        assert {item['id'] for item in body['items']} == {'o_a'}
        latest = client.post('/v1/channels/policy-latest/evidence', headers=headers,
                             json={}).json()
        assert {item['id'] for item in latest['items']} == {'o_b'}
        own = client.post('/v1/channels/community/evidence', headers=headers,
                          json={'sourceScope': 'own'}).json()
        assert {item['id'] for item in own['items']} == {'o_a'}


def test_channel_evidence_territory_only_and_unknown_channel():
    active = service()
    headers = {'Authorization': 'Bearer ' + TOKEN}
    with TestClient(create_app(service=active)) as client:
        body = client.post('/v1/channels/community/evidence', headers=headers,
                           json={'territoryPreference': ['上海市'],
                                 'territoryMode': 'only'}).json()
        assert [item['id'] for item in body['items']] == ['o_a']
        assert client.post('/v1/channels/missing/evidence', headers=headers,
                           json={}).status_code == 404


def test_shared_store_channel_filter_excludes_untagged_and_handles_cached_switches():
    active = service()
    results = active.engine('policy').store.results
    unknown = block('o_unknown', ['linfen-shanghai'])
    unknown['provenance'].pop('channel')
    results['o_unknown'] = unknown
    active.queue.jobs.append(dict(id='j_unknown', library_id='policy', receipt={'id': 'o_unknown'}))
    headers = {'Authorization': 'Bearer ' + TOKEN}
    with TestClient(create_app(service=active)) as client:
        # Alternating queries must rebuild the shared-store snapshot for the
        # selected channel; a missing provenance tag never implies membership.
        for channel, expected in [('community', {'o_a'}), ('policy-latest', {'o_b'}),
                                  ('community', {'o_a'}), ('policy-latest', {'o_b'})]:
            reply = client.post(f'/v1/channels/{channel}/evidence', headers=headers, json={})
            assert reply.status_code == 200
            assert {item['id'] for item in reply.json()['items']} == expected
            assert all(item['channel'] == channel for item in reply.json()['items'])
        own = client.post('/v1/channels/policy-latest/evidence', headers=headers,
                          json={'sourceScope': 'own'}).json()
        assert own['items'] == []
        # The explicit legacy store route retains its store-wide semantics.
        legacy = client.post('/v1/libraries/policy/evidence', headers=headers, json={}).json()
        assert {item['id'] for item in legacy['items']} == {'o_a', 'o_b', 'o_unknown'}
