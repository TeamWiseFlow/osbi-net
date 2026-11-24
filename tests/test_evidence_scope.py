"""Evidence sourceScope and territory preference/mode."""
from wiseflow_server.evidence import EvidenceQuery, evidence_page

URI = 'https://onto.pilot.local/instance/{name}'


def block(extraction_id, *, nodes=('linfen-shanghai',), title='材料',
          published='2026-11-20', entities=()):
    return dict(
        id=extraction_id,
        provenance=dict(channel='community', channel_title='社区治理',
                        node_ids=list(nodes), source_url='https://example.org/x',
                        source_title=title, published_date=published,
                        date_source='meta', raw_text='正文。',
                        source_meta={'publisher': '某发布'}),
        entities=[dict(uri=URI.format(name=name), text=name, label=label)
                  for name, label in entities],
        relations=[])


def query(**overrides):
    return EvidenceQuery.model_validate(overrides)


def test_source_scope_all_returns_all_contributors():
    entries = [('j1', block('o_a', nodes=('linfen-shanghai', 'shizishan-chengdu'))),
               ('j2', block('o_b', nodes=('shizishan-chengdu',)))]
    page = evidence_page(entries, query(sourceScope='all'), library_id='policy',
                         node_id='linfen-shanghai')
    assert {item['id'] for item in page['items']} == {'o_a', 'o_b'}
    assert page['items'][0]['sources'][0]['nodeId'] in ('linfen-shanghai',
                                                        'shizishan-chengdu')


def test_source_scope_own_matches_any_provenance_node():
    entries = [('j1', block('o_a', nodes=('linfen-shanghai', 'shizishan-chengdu'))),
               ('j2', block('o_b', nodes=('shizishan-chengdu',)))]
    page = evidence_page(entries, query(sourceScope='own'), library_id='policy',
                         node_id='linfen-shanghai')
    assert [item['id'] for item in page['items']] == ['o_a']
    page = evidence_page(entries, query(sourceScope='own'), library_id='policy',
                         node_id='shizishan-chengdu')
    assert {item['id'] for item in page['items']} == {'o_a', 'o_b'}
    # Without an authenticated node identity, "own" is empty.
    page = evidence_page(entries, query(sourceScope='own'), library_id='policy',
                         node_id=None)
    assert page['items'] == []


def test_territory_prefer_keeps_unknown_and_orders_local_first():
    entries = [
        ('j1', block('o_chengdu', published='2026-11-21',
                     entities=[('成都市', '市级机构')])),
        ('j2', block('o_shanghai', published='2026-11-20',
                     entities=[('上海市', '市级机构'), ('静安区', '区级机构')])),
        ('j3', block('o_unknown', published='2026-11-19', entities=[])),
    ]
    page = evidence_page(entries, query(territoryPreference=['上海市', '静安区'],
                                        territoryMode='prefer'), library_id='policy')
    assert {item['id'] for item in page['items']} == {'o_chengdu', 'o_shanghai', 'o_unknown'}
    assert page['items'][0]['id'] == 'o_shanghai'


def test_territory_only_excludes_unmatched_and_unknown():
    entries = [
        ('j1', block('o_chengdu', entities=[('成都市', '市级机构')])),
        ('j2', block('o_shanghai', entities=[('上海市', '市级机构')])),
        ('j3', block('o_unknown', entities=[])),
    ]
    page = evidence_page(entries, query(territoryPreference=['上海市'],
                                        territoryMode='only'), library_id='policy')
    assert [item['id'] for item in page['items']] == ['o_shanghai']


def test_query_date_is_not_the_ingestion_window():
    # An old material that entered the graph through a dated channel remains
    # queryable: the 3-day window gates ingestion, not retrieval.
    entries = [('j1', block('o_old', published='2020-01-02'))]
    page = evidence_page(entries, query(fromDate='2020-01-01', toDate='2020-01-31'),
                         library_id='policy')
    assert [item['id'] for item in page['items']] == ['o_old']
