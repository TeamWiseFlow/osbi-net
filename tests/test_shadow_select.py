"""Shadow topnews/weekly selection ported from the production wiseflow pipeline.

All fixtures are fake evidence records shaped like the osbi server
/v1/libraries/{channel}/evidence response; no network or model calls.
"""
import json
import pytest

from osbi_client.shadow_snapshot import (
    SnapshotService, article_view, dedupe_articles, snapshot_from_evidence,
    strip_publish_date_suffix,
)
from osbi_client.shadow_candidates import (
    LOCALITY_SCORE, SECTIONS, _quality_excluded, _same_event, _score,
    dedupe_events, format_markdown, topnews_candidates,
)
from osbi_client.publisher_penalty import PublisherPenaltyLedger, publisher_key
from osbi_client.shadow_relations import build_relation_dossier, _similarity
from osbi_client.shadow_weekly_select import (
    ECONOMY_MIN_SCORE, _economy_relevance, _action_focus, _policy_focus,
    select_weekly_focus,
)
from osbi_client.shadow_topnews import _parse_links, format_push
from osbi_client.shadow_weekly_history import record_shadow_hotspots
from osbi_client.state import State


def entity(uri, name, label):
    return dict(uri=uri, name=name, label=label)


def relation(subject, obj, label):
    return dict(subject=subject, object=obj, label=label)


def evidence(text="原文证据。", title="社区治理观察", url="https://example.org/story",
             published="2026-11-20", entities=(), relations=(), quality_flags=(),
             boards=("社区治理",), content_types=("news",), publisher="示例发布",
             evidence_id=None):
    return dict(id=evidence_id or "o_" + title, evidenceId=evidence_id or "oq_" + title,
                text=text, title=title, publishedDate=published,
                boards=list(boards), contentTypes=list(content_types),
                qualityFlags=list(quality_flags),
                entityCount=len(entities), relationCount=len(relations),
                sources=[dict(title=title, meta=dict(
                    canonicalUrl=url, finalUrl=url, requestedUrl=url,
                    publisher=publisher, publishedAt=published))],
                entities=list(entities), relations=list(relations))


def service(records, library="policy"):
    return SnapshotService(snapshot_from_evidence(records, library))


# -- data bridge -------------------------------------------------------------

def test_snapshot_groups_blocks_into_one_article():
    records = [
        evidence(title="同一篇文章", url="https://example.org/a",
                 entities=[entity("u1", "临汾路街道", "街镇机构")],
                 relations=[relation("u1", "u2", "位于")], evidence_id="b1"),
        evidence(title="同一篇文章", url="https://example.org/a",
                 entities=[entity("u2", "静安区", "区级机构")],
                 relations=[relation("u2", "u3", "隶属")], evidence_id="b2"),
    ]
    snap = snapshot_from_evidence(records, "policy")
    assert len(snap.articles) == 1
    article = next(iter(snap.articles.values()))
    assert len(article["blockIds"]) == 2
    assert set(article["nodeUris"]) == {"u1", "u2"}
    assert len(article["edgeKeys"]) == 2
    assert article["boards"] == ["社区治理"]
    view = article_view(snap, article)
    assert view["entityCount"] == 2 and view["relationCount"] == 2
    assert "原文证据" in snap.blocks[article["blockIds"][0]]["rawText"]
    # nodes carry merged provenance and degree like the production snapshot
    assert snap.nodes["u1"]["articleIds"] == [article["articleId"]]
    assert snap.nodes["u1"]["degree"] == 1
    assert snap.nodes["u2"]["degree"] == 2


def test_key_entities_ranked_and_relations_filtered():
    records = [evidence(
        entities=[entity("p", "人物甲", "人物"),
                  entity("pol", "《静安区社区治理办法》", "区级政策"),
                  entity("fin", "某基金份额发售公告", "政策文件")],
        relations=[relation("pol", "p", "提及"), relation("p", "fin", "合作")])]
    snap = snapshot_from_evidence(records, "policy")
    view = article_view(snap, next(iter(snap.articles.values())))
    names = [e["name"] for e in view["keyEntities"]]
    assert names[0] == "《静安区社区治理办法》"
    assert "某基金份额发售公告" not in names  # noise policy node dropped
    predicates = [r["predicateLabel"] for r in view["relations"]]
    assert predicates == ["提及"]  # 合作 is not a high-value predicate


def test_dedupe_articles_merges_near_duplicate_titles():
    items = [
        dict(articleId="a1", title="静安区开展社区治理调研活动", publishedDate="2026-11-20",
             boards=["社区治理"], url="https://a", relationCount=2, entityCount=5, qualityFlags=[]),
        dict(articleId="a2", title="静安区开展社区治理调研活动（内容发布日期：2026-11-20）",
             publishedDate="2026-11-20", boards=["社区治理"], url="https://b",
             relationCount=6, entityCount=8, qualityFlags=["digest"]),
    ]
    merged = dedupe_articles(items, threshold=90)
    assert len(merged) == 1
    assert merged[0]["articleId"] == "a2"  # richer graph wins
    assert merged[0]["duplicateCount"] == 1
    assert "digest" in merged[0]["qualityFlags"]
    assert strip_publish_date_suffix("标题（内容发布日期：2026-11-20）") == "标题"


# -- topnews scoring / gates -------------------------------------------------

def item(**overrides):
    base = dict(articleId="a1", title="标题", url="https://example.org/x",
                publisher="示例发布", publishedDate="2026-11-20", boards=["社区治理"],
                contentTypes=["news"], qualityFlags=[], relationCount=3, entityCount=10,
                keyEntities=[], relations=[], leaderText="")
    base.update(overrides)
    return base


def test_score_locality_tiers():
    assert LOCALITY_SCORE == {"target": 120, "shanghai": 60, "other_district": 10,
                              "other_province": -40, "national": 0, "unknown": 0}
    base = _score(item(leaderText="社区活动"))
    assert _score(item(leaderText="静安区临汾路街道活动")) == base + 120
    assert _score(item(leaderText="上海市社区活动")) == base + 60
    assert _score(item(leaderText="杨浦区社区活动")) == base + 10
    assert _score(item(leaderText="江苏省社区活动")) == base - 40


def test_score_leader_boost_penalty_and_publisher_penalty():
    target = item(leaderText="上海市委书记陈吉宁赴静安区调研社区治理")
    assert _score(target) >= _score(item()) + 500
    # 市委书记 is a recognized grassroots title; 江苏省 makes it non-target.
    other = item(leaderText="江苏省南京市委书记韩立明在南京调研")
    assert _score(other) <= _score(item()) - 300
    penalized = item(publisherKey="example.org")
    assert _score(penalized, {"example.org": -20}) == _score(item()) - 20


def test_score_quality_and_type_adjustments():
    base = _score(item())
    assert _score(item(qualityFlags=["low_density"])) == base - 2
    assert _score(item(qualityFlags=["digest"])) == base - 3
    assert _score(item(contentTypes=["policy"])) == base + 3
    assert _score(item(keyEntities=[dict(name="某政策", label="区级政策")])) == base + 2
    assert _score(item(keyEntities=[dict(name="某行动", label="街镇行动")])) == base + 1


def test_quality_gate_commentary_needs_low_density():
    excluded = {"zero_relation", "report"}
    assert _quality_excluded(item(qualityFlags=["zero_relation"]), excluded)
    assert _quality_excluded(item(qualityFlags=["report"]), excluded)
    assert not _quality_excluded(item(qualityFlags=["commentary"]), excluded)
    assert _quality_excluded(item(qualityFlags=["commentary", "low_density"]), excluded)


def test_same_event_merges_shared_event_node_but_not_national_umbrella():
    left = item(keyEntities=[dict(name="静安区社区治理推进会", label="会议论坛")])
    right = item(articleId="a2", title="另一标题",
                 keyEntities=[dict(name="静安区社区治理推进会", label="会议论坛")])
    assert _same_event(left, right)
    national_left = item(keyEntities=[dict(name="全国基层治理大会", label="会议论坛")])
    national_right = item(articleId="a2", title="完全不同的标题文字",
                          keyEntities=[dict(name="全国基层治理大会", label="会议论坛")])
    assert not _same_event(national_left, national_right)
    assert not _same_event(left, dict(right, publishedDate="2026-11-21"))


def test_dedupe_events_keeps_richer_representative():
    left = item(relationCount=2, keyEntities=[dict(name="静安区加装电梯推进会", label="会议论坛")])
    right = item(articleId="a2", url="https://example.org/y", title="不同措辞的标题",
                 publisher="另一发布", relationCount=9,
                 keyEntities=[dict(name="静安区加装电梯推进会", label="会议论坛")])
    merged = dedupe_events([left, right])
    assert len(merged) == 1
    assert merged[0]["articleId"] == "a2"
    assert merged[0]["eventDuplicateCount"] == 1
    assert merged[0]["eventDuplicatePublishers"] == ["示例发布"]


def test_topnews_candidates_locality_hard_filter_and_dangjian_exception():
    records = [
        evidence(title="静安区领导调研社区治理", url="https://example.org/local",
                 text="上海市委书记陈吉宁赴静安区调研社区治理工作。",
                 entities=[entity("e1", "静安区社区治理调研", "调研视察")],
                 relations=[relation("e1", "o1", "参与")], publisher="静安发布",
                 boards=("社区治理", "基层党建")),
        evidence(title="江苏省领导调研社区治理", url="https://example.org/other",
                 text="江苏省南京市委书记韩立明在南京调研社区治理。",
                 entities=[entity("e2", "江苏省社区治理调研", "调研视察")],
                 relations=[relation("e2", "o2", "参与")], publisher="江苏发布",
                 boards=("社区治理", "基层党建")),
    ]
    services = {"policy": service(records)}
    result = topnews_candidates(services, from_date="2026-11-20", to_date="2026-11-20",
                                boards=["社区治理", "基层党建"])
    community = result["boards"]["社区治理"]
    assert [i["url"] for i in community] == ["https://example.org/local"]
    # 基层党建 keeps non-local material (client-fixed sources), locality-sorted
    dangjian = result["boards"]["基层党建"]
    assert len(dangjian) == 2
    assert dangjian[0]["url"] == "https://example.org/local"


def test_format_markdown_and_parse_links():
    candidates = {"boards": {"社区治理": [item(title="第一条"), item(articleId="a2", title="第二条")]}}
    markdown, link_dict = format_markdown(candidates)
    assert "[1]第一条" in markdown and "[2]第二条" in markdown
    selected = _parse_links("<links>[2]\n[1]\n[2]</links>", link_dict, 3)
    assert [i["title"] for i in selected] == ["第二条", "第一条"]
    assert _parse_links("没有标签", link_dict, 3) == []


def test_format_push_wechat_prefix_and_publisher_dedupe():
    selected = [
        dict(section="经济动态", title="微信稿一", url="https://mp.weixin.qq.com/s?x=1",
             publisher="经济日报", publishedDate="2026-11-22"),
        dict(section="经济动态", title="微信稿二", url="https://mp.weixin.qq.com/s?x=2",
             publisher="经济日报", publishedDate="2026-11-22"),
        dict(section="经济动态", title="网站稿", url="https://example.org/news",
             publisher="某网站", publishedDate="2026-11-22"),
    ]
    text = format_push(selected, date="2026-11-22", welcome="今日份的临小助内参来啦", suffix="")
    assert "#经济动态" in text
    assert "//经济日报 2026-11-22// 微信稿一" in text
    assert "微信稿二" not in text  # same wechat publisher kept once per board
    assert "网站稿" in text and "//某网站" not in text


# -- publisher penalty ledger ------------------------------------------------

def test_publisher_key_wechat_prefix_and_domain():
    raw = "//经济日报 2026-11-22// 标题\nhttps://mp.weixin.qq.com/s?x=1"
    assert publisher_key("https://mp.weixin.qq.com/s?x=1", "meta名", raw) == "经济日报"
    assert publisher_key("https://www.example.org/news", "") == "example.org"


def test_ledger_record_clamp_and_advance(tmp_path):
    ledger = PublisherPenaltyLedger(tmp_path / "penalty.json")
    assert ledger.record("2026-11-20", {"win"}, {"win", "lose"})
    assert ledger.scores["win"] == -20 and ledger.scores["lose"] == 10
    assert not ledger.record("2026-11-20", {"win"})  # idempotent per day
    ledger.record("2026-11-21", {"win"}, {"win", "lose"})
    assert ledger.scores["win"] == -40 and ledger.scores["lose"] == 20
    ledger.advance_to_before("2026-11-25")  # 3 missing days recover +10 each, capped
    assert ledger.scores["lose"] == 30
    reloaded = PublisherPenaltyLedger(tmp_path / "penalty.json").load()
    assert reloaded.scores["win"] == -40
    for _ in range(20):
        ledger.record(ledger._next_day(ledger.updated_date), {"win"}, {"win"})
    assert ledger.scores["win"] == -300  # floor


# -- weekly: economy fallback ------------------------------------------------

def economy_item(title, text="", publisher="示例发布", flags=()):
    records = [evidence(title=title, text=text or title, url="https://example.org/" + title,
                        publisher=publisher, boards=("经济动态",), quality_flags=flags)]
    snap = snapshot_from_evidence(records, "economy")
    return article_view(snap, next(iter(snap.articles.values())))


def test_economy_relevance_prefers_resident_facing_over_capital_market():
    local = economy_item("静安区临汾路街道就业服务进社区，消费券补贴惠及居民",
                         text="街道便民生活圈开展就业服务与消费券补贴活动。")
    market = economy_item("A股三大指数涨跌互现，个股分红回购增持公告汇总",
                          text="A股港股行情：个股涨停、基金净值、券商市值复盘。")
    local_score, matched = _economy_relevance(local)
    market_score, _ = _economy_relevance(market)
    assert local_score >= ECONOMY_MIN_SCORE and "就业" in matched
    assert market_score < local_score


def test_economy_focus_dedupes_similar_titles():
    records = [
        evidence(title="静安区街道就业服务进社区活动", url="https://example.org/e1",
                 text="静安区临汾路街道就业服务、消费券补贴居民。", boards=("经济动态",)),
        evidence(title="静安区街道就业服务进社区活动启动", url="https://example.org/e2",
                 text="静安区临汾路街道就业服务、消费券补贴居民。", boards=("经济动态",)),
    ]
    focus = select_weekly_focus({"economy": service(records, "economy")},
                                from_date="2026-11-16", to_date="2026-11-22",
                                boards=["经济动态"])
    assert "经济动态" in focus
    assert focus["经济动态"]["type"] == "economy"
    assert len(focus["经济动态"]["items"]) == 1


# -- weekly: action focus ----------------------------------------------------

def action_records(leader_text="社区治理活动。", event_name="渝邻共治协商会",
                   extra_article=False):
    entities = [entity("ev", event_name, "治理机制"),
                entity("org1", "临汾路街道办", "街镇机构"),
                entity("org2", "静安区民政局", "区级机构"),
                entity("pol", "《静安区社区治理办法》", "区级政策"),
                entity("act", "邻里互助行动", "社区行动")]
    relations = [relation("org1", "ev", "参与"), relation("org2", "ev", "服务"),
                 relation("ev", "pol", "落实"), relation("ev", "act", "涉及")]
    records = [evidence(title="渝邻共治协商会在静安召开", url="https://example.org/w1",
                        text=leader_text, entities=entities, relations=relations,
                        publisher="静安发布")]
    if extra_article:
        records.append(evidence(title="渝邻共治协商会持续推进", url="https://example.org/w2",
                                text="协商会持续推进社区治理。",
                                entities=[entity("ev", event_name, "治理机制")],
                                relations=[relation("org1", "ev", "参与")],
                                publisher="静安发布"))
    return records


def test_action_focus_scores_network_breadth_and_leader_boost():
    snap = service(action_records()).snapshot
    focus = _action_focus(SnapshotService(snap), "社区治理", "2026-11-16", "2026-11-22",
                          min_edges=4, top_n=2)
    assert focus["hotspots"], "hotspot with 4 signal edges must qualify"
    row = focus["hotspots"][0]
    assert row["name"] == "渝邻共治协商会"
    assert row["edgeCount"] >= 4 and row["score"] >= 4
    leader_snap = service(action_records(
        leader_text="上海市委书记陈吉宁赴静安区调研渝邻共治协商会。")).snapshot
    leader_focus = _action_focus(SnapshotService(leader_snap), "社区治理",
                                 "2026-11-16", "2026-11-22", min_edges=4, top_n=2)
    # Leader text boosts action-label nodes of the same article above the
    # ordinary hotspot; subset dedupe then keeps only the stronger one.
    leader_row = leader_focus["hotspots"][0]
    assert leader_row["leader"] and leader_row["score"] >= 300
    assert leader_row["score"] > row["score"]


def test_action_focus_drops_stale_and_marks_progress(tmp_path):
    state = State(tmp_path / "client.db")
    try:
        records = action_records(extra_article=True)
        snap = service(records).snapshot
        ids = sorted(snap.articles)
        record_shadow_hotspots(state, "cust", "2026-11-15",
                               {"社区治理": dict(hotspots=[dict(
                                   uri="ev", name="渝邻共治协商会", label="治理机制",
                                   articleIds=[ids[0]])])})
        focus = _action_focus(SnapshotService(snap), "社区治理", "2026-11-16",
                              "2026-11-22", min_edges=4, top_n=2,
                              priors=__import__("osbi_client.weekly_history", fromlist=["load_reported_hotspots"])
                              .load_reported_hotspots(state, "cust", "shadow", "default", "2026-11-22"))
        row = focus["hotspots"][0]
        assert row.get("priorReported") and row["priorReported"]["newCount"] == 1
        # stale: no new articles at all
        stale_focus = _action_focus(SnapshotService(snap), "社区治理", "2026-11-16",
                                    "2026-11-22", min_edges=4, top_n=2,
                                    priors=[dict(weekEnd="2026-11-15", board="社区治理",
                                                 uri="ev", name="渝邻共治协商会",
                                                 label="治理机制", articleIds=ids,
                                                 topic="渝邻共治协商会")])
        assert not stale_focus["hotspots"]
    finally:
        state.close()


def test_record_shadow_hotspots_only_action_boards(tmp_path):
    state = State(tmp_path / "client.db")
    try:
        count = record_shadow_hotspots(state, "cust", "2026-11-22", {
            "社区治理": dict(hotspots=[dict(uri="ev", name="热点", label="治理机制", articleIds=["a1"])]),
            "经济动态": dict(hotspots=[dict(uri="ec", name="经济热点", label="治理机制", articleIds=["a2"])]),
        })
        assert count == 1
        from osbi_client.weekly_history import load_reported_hotspots
        rows = load_reported_hotspots(state, "cust", "shadow", "default", "2026-11-29")
        assert [r["board"] for r in rows] == ["社区治理"]
    finally:
        state.close()


# -- weekly: policy chains ---------------------------------------------------

def policy_records():
    entities = [entity("pol", "《静安区促进就业实施方案》", "区级政策"),
                entity("upper", "《上海市就业促进条例》", "市级政策"),
                entity("street", "临汾路街道就业促进行动", "街镇行动"),
                entity("issuer", "静安区人民政府", "区级机构")]
    relations = [relation("pol", "upper", "依据"),
                 relation("street", "pol", "落实"),
                 relation("issuer", "pol", "发布")]
    return [evidence(title="静安区发布促进就业实施方案", url="https://example.org/p1",
                     text="静安区临汾路街道落实就业促进政策。",
                     entities=entities, relations=relations, boards=("政策速递",),
                     publisher="静安发布")]


def test_relation_dossier_builds_evidence_bearing_chain():
    services = {"policy": service(policy_records())}
    dossier = build_relation_dossier(services, from_date="2026-11-16", to_date="2026-11-22")
    assert dossier["chains"]
    chain = dossier["chains"][0]
    assert chain["policy"]["name"] == "《静安区促进就业实施方案》"
    assert [i["entity"]["name"] for i in chain["lineage"]] == ["《上海市就业促进条例》"]
    assert [i["entity"]["name"] for i in chain["implementers"]] == ["临汾路街道就业促进行动"]
    assert chain["issuers"] and chain["score"] > 0
    assert chain["implementers"][0]["evidence"]["articles"][0]["url"] == "https://example.org/p1"


def test_policy_focus_requires_proximity_and_richness():
    services = {"policy": service(policy_records())}
    dossier = build_relation_dossier(services, from_date="2026-11-16", to_date="2026-11-22")
    focus = _policy_focus(services, dossier, "政策速递", "2026-11-16", "2026-11-22", top_n=2)
    assert focus["focuses"] and focus["items"]
    # non-local chain without lineage/implementer fails the gate
    remote = [evidence(title="江苏省发布某方案", url="https://example.org/p2",
                       text="江苏省发布方案。",
                       entities=[entity("pol2", "《江苏省某方案》", "省级政策"),
                                 entity("iss2", "江苏省人民政府", "省级机构")],
                       relations=[relation("iss2", "pol2", "发布")],
                       boards=("政策速递",), publisher="江苏发布")]
    remote_services = {"policy": service(remote)}
    remote_dossier = build_relation_dossier(remote_services, from_date="2026-11-16",
                                            to_date="2026-11-22")
    remote_focus = _policy_focus(remote_services, remote_dossier, "政策速递",
                                 "2026-11-16", "2026-11-22", top_n=2)
    assert not remote_focus["focuses"]


def test_similarity_containment_and_bigrams():
    assert _similarity("静安区社区治理办法", "静安区社区治理办法") == 1.0
    assert _similarity("完全不同的甲", "毫无关系的乙") < 0.62
