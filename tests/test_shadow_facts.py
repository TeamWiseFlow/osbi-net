"""Deterministic 加油站 fact extraction + FactLedger (ported production facts)."""
import json

from osbi_client.shadow_facts import (FactLedger, SOURCE_COOLDOWN_DAYS,
                                      _distinct_sources, extract_facts,
                                      format_fact, leading_facts, top_fact)
from osbi_client.shadow_snapshot import snapshot_from_evidence

POLICY_TEXT = (
    '《静安区社区养老服务办法》规定，符合条件的65周岁以上老年人可以免费享受居家养老服务，所需经费由区财政承担。'
    '第十八条修改为：对特困人员给予每月不低于800元的照料补贴，但是已享受长期护理保险待遇的除外。'
    '坚持党建引领，加强队伍建设。'
    '不得违规收取费用。')


def policy_record(text=POLICY_TEXT, title='静安区社区养老服务办法',
                  url='https://example.org/policy1', published='2026-11-20',
                  content_types=('policy',), policy_name='静安区社区养老服务办法'):
    return dict(
        id='o_' + url, evidenceId='oq_' + url, text=text, title=title,
        publishedDate=published, boards=['政策速递'],
        contentTypes=list(content_types), qualityFlags=[],
        sources=[dict(title=title, meta=dict(
            canonicalUrl=url, publisher='静安区民政局', publishedAt=published))],
        entities=[dict(uri='u_policy_' + url.rsplit('/', 1)[-1],
                       name='《' + policy_name + '》', label='区级政策'),
                  dict(uri='u_issuer', name='静安区民政局', label='区级机构')],
        relations=[dict(subject='u_issuer', object='u_policy_' + url.rsplit('/', 1)[-1],
                        label='发布')])


def snapshot(records):
    return snapshot_from_evidence(records, 'policy')


def test_extract_facts_verbatim_with_threshold_and_exception():
    facts = extract_facts(snapshot([policy_record()]))
    assert facts
    top = facts[0]
    assert top['text'] == '对特困人员给予每月不低于800元的照料补贴，但是已享受长期护理保险待遇的除外'
    assert top['article'] == '第十八条'
    assert top['policy'] == '静安区社区养老服务办法'
    assert top['score'] >= 9  # threshold 4 + exception 5
    assert top['issuer'] == '静安区民政局'
    # Generic principle slogans and short generic prohibitions never qualify.
    assert all('党建' not in f['text'] for f in facts)
    assert all(f['text'] != '不得违规收取费用' for f in facts)


def test_extract_facts_news_lead_gate():
    record = policy_record(
        text='办法明确，对特困人员给予每月不低于800元的照料补贴，但是已享受长期护理保险待遇的除外。',
        title='静安区发布社区养老服务办法', content_types=('news',))
    facts = extract_facts(snapshot([record]))
    assert facts and facts[0]['policy'] == '静安区社区养老服务办法'
    # News without a lead verb / release / strong rule is rejected.
    record2 = policy_record(text='社区养老服务体系建设取得积极进展，老年人幸福感不断提升。',
                            title='静安区社区养老服务观察', content_types=('news',))
    assert extract_facts(snapshot([record2])) == []


def test_omnibus_amendment_scopes_regulation_per_item():
    text = ('一、将《静安区社区养老服务办法》第十八条修改为：对特困人员给予每月不低于800元的照料补贴，'
            '但是已享受长期护理保险待遇的除外。\n'
            '二、将《静安区社会救助实施细则》第九条修改为：救助家庭人均收入低于最低生活保障标准1.5倍的，'
            '可以纳入救助范围，但是已通过其他渠道获得足额补偿的除外。')
    record = policy_record(text=text, title='静安区关于修改部分规范性文件的决定')
    record['entities'].append(dict(uri='u_policy2', name='《静安区社会救助实施细则》', label='区级政策'))
    facts = extract_facts(snapshot([record]))
    by_policy = {f['policy']: f for f in facts}
    assert '静安区社区养老服务办法' in by_policy
    assert '静安区社会救助实施细则' in by_policy
    assert by_policy['静安区社区养老服务办法']['article'] == '第十八条'
    assert by_policy['静安区社会救助实施细则']['article'] == '第九条'


def test_date_window_filters_blocks():
    records = [policy_record()]
    assert extract_facts(snapshot(records), from_date='2026-11-21') == []
    assert extract_facts(snapshot(records), to_date='2026-11-19') == []
    assert extract_facts(snapshot(records), from_date='2026-11-16',
                         to_date='2026-11-22')


def test_fact_ledger_dedupe_and_source_cooldown(tmp_path):
    ledger = FactLedger(tmp_path / 'did_you_know_sent.json')
    other = policy_record(url='https://example.org/policy2',
                          title='静安区社会救助实施细则',
                          policy_name='静安区社会救助实施细则',
                          text='《静安区社会救助实施细则》规定，救助家庭人均收入低于最低生活保障标准1.5倍的，'
                               '可以纳入救助范围，但是已通过其他渠道获得足额补偿的除外。')
    snap = snapshot([policy_record(), other])
    facts = extract_facts(snap)
    assert len(facts) >= 3
    # Mark a fact from the multi-fact document so cooldown expiry can bring
    # its sibling back (exact factId dedupe is permanent, by design).
    first = next(f for f in facts if f['docTitle'] == '静安区社区养老服务办法')
    assert ledger.unseen(facts) == facts
    ledger.mark([first], sent_date='2026-11-20')
    remaining = ledger.unseen(facts)
    assert first not in remaining
    # Same document within the 7-day cooldown: other facts from it are hidden
    # while the fresh second document still supplies candidates.
    picked = leading_facts([snap], from_date='2026-11-16', to_date='2026-11-22',
                           ledger=ledger, limit=8)
    assert picked and all(f['docTitle'] != first['docTitle'] for f in picked)
    # After the cooldown window the source is eligible again.
    later = leading_facts([snap], from_date='2026-11-16', to_date='2026-11-30',
                          ledger=ledger, limit=8)
    assert any(f['docTitle'] == first['docTitle'] for f in later)
    # Persisted shape keeps factIds + records (old format readable).
    data = json.loads(ledger.path.read_text(encoding='utf-8'))
    assert first['factId'] in data['factIds']
    assert data['records'][0]['sourceKey']
    assert data['records'][0]['sentDate'] == '2026-11-20'


def test_cooldown_fallback_when_no_fresh_source(tmp_path):
    ledger = FactLedger(tmp_path / 'ledger.json')
    # Single-document window: after marking its best fact, every remaining
    # candidate shares the cooled source.
    snap = snapshot([policy_record()])
    facts = extract_facts(snap)
    assert len(facts) >= 2
    ledger.mark(facts[:1], sent_date='2026-11-20')
    # Every remaining candidate shares the cooled source: production falls
    # back to factId-dedupe so the column never disappears.
    picked = leading_facts([snap], from_date='2026-11-16', to_date='2026-11-22',
                           ledger=ledger, limit=8)
    assert picked and all(f['factId'] != facts[0]['factId'] for f in picked)


def test_distinct_sources_one_per_document():
    facts = [dict(factId='1', docTitle='静安区社区养老服务办法', score=9),
             dict(factId='2', docTitle='静安区社区养老服务办法（2026年修订）', score=8),
             dict(factId='3', docTitle='静安区社会救助实施细则', score=7)]
    picked = _distinct_sources(facts, 8)
    assert [f['factId'] for f in picked] == ['1', '3']


def test_format_fact_shape():
    facts = extract_facts(snapshot([policy_record()]))
    text = format_fact(facts[0])
    assert text.startswith('#加油站\n《静安区社区养老服务办法》第十八条规定，')
    assert '依据：《静安区社区养老服务办法》（静安区民政局 2026-11-20）' in text


def test_top_fact_returns_best_or_none(tmp_path):
    snap = snapshot([policy_record()])
    assert top_fact([snap])['score'] >= 9
    empty = snapshot([])
    assert top_fact([empty]) is None
