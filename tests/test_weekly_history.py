"""Coverage, ordering, scoping, historical boundaries and real heading imports."""
import asyncio
from types import SimpleNamespace
import zipfile
import pytest
from osbi_client.weekly_models import WeeklySelect, WeeklyBackfill
from osbi_client.state import State
from osbi_client.weekly_history import (load_reported_hotspots, record_reported_hotspots,
    match_priors, classify_repeat, select_hotspots)
from osbi_client.weekly import (Snapshot, action_candidates, backfill_weekly, format_action_brief,
                               snapshot_from_evidence, record_weekly, select_weekly)
from osbi_client.report_headings import parse_sections


@pytest.fixture
def repo(tmp_path):
    value = State(tmp_path / "client.db")
    yield value
    value.close()


def row(name="渝邻共治", uri="u1", ids=("a1",), score=10):
    return dict(name=name, uri=uri, label="治理机制", articleIds=list(ids), score=score, edgeCount=4, units=[])


def record(repo, week, hotspot=None, customer="a", channel="policy", profile="default", board="社区治理"):
    return record_reported_hotspots(repo, customer, channel, profile, week,
                                    {board: dict(hotspots=[hotspot or row()])})


def test_ledger_replaces_same_week_and_isolates_customer_channel_profile_and_board(repo):
    record(repo, "2026-11-02")
    record(repo, "2026-11-02", row(ids=("a1", "a2")))
    for scope in [{"customer": "b"}, {"channel": "economy"}, {"profile": "other"}, {"board": "数字社区"}]:
        record(repo, "2026-11-02", **scope)
    rows = load_reported_hotspots(repo, "a", "policy", "default", "2026-11-09")
    assert len(rows) == 2
    assert next(p for p in rows if p["board"] == "社区治理")["articleIds"] == ["a1", "a2"]
    assert len(load_reported_hotspots(repo, "b", "policy", "default", "2026-11-09")) == 1
    assert not load_reported_hotspots(repo, "unknown", "policy", "default", "2026-11-09")


def test_lookback_includes_immediately_previous_week_and_excludes_current_rerun(repo):
    for week in ["2026-09-01", "2026-10-12", "2026-10-19", "2026-11-02", "2026-11-09"]:
        record(repo, week)
    rows = load_reported_hotspots(repo, "a", "policy", "default", "2026-11-09")
    assert [p["weekEnd"] for p in rows] == ["2026-11-02", "2026-10-19", "2026-10-12"]
    assert rows[0]["weekEnd"] == "2026-11-02"


def test_match_prefers_exact_uri_then_normalized_name():
    priors = [dict(weekEnd="2026-11-02", uri="u1", name="渝邻共治", articleIds=["a1"])]
    assert match_priors("无关名称", "u1", priors)
    assert match_priors("“渝邻共治”", "u2", priors)
    assert not match_priors("枫桥经验", "u3", priors)


@pytest.mark.parametrize("flags,expected", [([], "progress"), (["commentary"], "stale"),
                                          (["digest"], "stale"), (["commentary", "digest"], "stale")])
def test_only_substantive_new_material_permits_progress(flags, expected):
    priors = [dict(weekEnd="2026-11-02", **row())]
    status, info = classify_repeat({"n1": {"qualityFlags": flags}}, row(ids=("a1", "n1")), priors)
    assert status == expected and info["newCount"] == 1


def test_union_of_all_prior_weeks_prevents_old_article_reappearing():
    priors = [dict(weekEnd="2026-11-02", **row(ids=("a2",))), dict(weekEnd="2026-10-26", **row(ids=("a1",)))]
    status, info = classify_repeat({}, row(ids=("a1", "a2")), priors)
    assert status == "stale" and info["newCount"] == 0 and info["weekEnd"] == "2026-11-02"
    assert classify_repeat({}, row(uri="new", name="全新热点"), priors) == ("fresh", None)
    assert classify_repeat({}, row(ids=("a1", "unknown")), priors)[0] == "stale"


def test_fresh_precedes_progress_even_with_lower_score_and_brief_requires_delta():
    priors = [dict(weekEnd="2026-11-02", **row())]
    rows = [row(ids=("a1", "n1"), score=999), row(name="新热点", uri="u2", ids=("b1",), score=1)]
    selected, counts = select_hotspots(rows, {"n1": {"qualityFlags": []}}, priors, 2)
    assert [p["uri"] for p in selected] == ["u2", "u1"]
    assert counts == {"stale": 0, "progress": 1, "fresh": 1}
    brief = format_action_brief(selected)
    assert "2026-11-02" in brief and "上周报道的…本周…" in brief
    assert "不要重复旧会议/事件" in brief
    assert len(select_hotspots(rows, {"n1": {"qualityFlags": []}}, priors, 1)[0]) == 1


def snapshot():
    nodes = {"u1": dict(uri="u1", name="渝邻共治", label="治理机制"),
             "u2": dict(uri="u2", name="新方法", label="治理机制"),
             "org": dict(uri="org", name="示例社区居委会", label="社区自治组织")}
    articles = {
        "a1": dict(articleId="a1", publishedDate="2026-11-02", nodeUris=["u1", "org"], title="启动活动", rawText="开展治理活动。", qualityFlags=[]),
        "n1": dict(articleId="n1", publishedDate="2026-11-09", nodeUris=["u1", "org"], title="落地进展", rawText="项目落地新增服务。", qualityFlags=[]),
        "b1": dict(articleId="b1", publishedDate="2026-11-08", nodeUris=["u2", "org"], title="新方法推广", rawText="开展新活动。", qualityFlags=[]),
        "future": dict(articleId="future", publishedDate="2026-11-20", nodeUris=["u1"], title="后续", rawText="", qualityFlags=[]),
        "undated": dict(articleId="undated", publishedDate="", nodeUris=["u1"], title="无日期", rawText="", qualityFlags=[])}
    edges = [dict(subject="org", object="u1", predicateLabel="采用", articleIds={"a1", "n1"}),
             dict(subject="org", object="u2", predicateLabel="采用", articleIds={"b1"})]
    return Snapshot(nodes, edges, articles)


def setup_local(monkeypatch, repo):
    import osbi_client.weekly as module
    async def load(*args, **kwargs):return snapshot()
    async def scope(daemon):return daemon.scope
    monkeypatch.setattr(module, "load_snapshot", load)
    monkeypatch.setattr(module, "coverage_scope", scope)
    return SimpleNamespace(state=repo, scope="a")


def test_select_record_and_backfill_are_connected_and_private(repo, monkeypatch):
    daemon = setup_local(monkeypatch, repo)
    request = WeeklySelect(fromDate="2026-10-26", toDate="2026-11-02", minEdges=1)
    plan = asyncio.run(select_weekly(daemon, "community", request))
    assert plan["hotspots"][0]["uri"] == "u1"
    assert not load_reported_hotspots(repo, "a", "policy", "default", "2026-11-09")
    assert record_weekly(daemon, "community", plan["planId"])["recorded"] == 1
    assert record_weekly(daemon, "community", plan["planId"])["recorded"] == 1
    with pytest.raises(ValueError):record_weekly(daemon, "community", "missing-plan")
    current = asyncio.run(select_weekly(daemon, "community", WeeklySelect(fromDate="2026-11-02", toDate="2026-11-09", minEdges=1)))
    assert [h["uri"] for h in current["hotspots"]] == ["u2", "u1"]
    assert current["hotspots"][1]["priorReported"]["weekEnd"] == "2026-11-02"
    daemon.scope = "b"
    assert not asyncio.run(select_weekly(daemon, "community", request))["hotspots"][0].get("priorReported")
    backfill = WeeklyBackfill(weekEnd="2026-11-02", sections={"社区治理": ["深化“渝邻共治”工作机制"]})
    preview = asyncio.run(backfill_weekly(daemon, "community", backfill))
    assert preview["dryRun"] and not preview["recorded"]
    assert preview["focusByBoard"]["社区治理"]["hotspots"][0]["articleIds"] == ["a1"]
    backfill.apply = True
    assert asyncio.run(backfill_weekly(daemon, "community", backfill))["recorded"] == 1


def test_backfill_unmatched_heading_does_not_destroy_existing_history(repo, monkeypatch):
    daemon = setup_local(monkeypatch, repo)
    record(repo, "2026-11-02", channel="community")
    request = WeeklyBackfill(weekEnd="2026-11-02", apply=True, sections={"社区治理": ["完全无关标题"]})
    with pytest.raises(ValueError):asyncio.run(backfill_weekly(daemon, "community", request))
    assert load_reported_hotspots(repo, "a", "community", "default", "2026-11-09")[0]["uri"] == "u1"


def test_snapshot_article_identity_survives_reextraction_and_keeps_quality_flags():
    record = dict(id="old", evidenceId="stable-block", text="原文", qualityFlags=["commentary"],
        sources=[{"title": "社区治理观察", "meta": {"requestedUrl": "https://example.org/story", "publishedAt": "2026-11-02"}}],
        entities=[dict(uri="u1", name="渝邻共治", label="治理机制")], relations=[])
    old = snapshot_from_evidence([record])
    new = snapshot_from_evidence([{**record, "id": "new"}])
    assert old.articles.keys() == new.articles.keys()
    assert next(iter(new.articles.values()))["qualityFlags"] == ["commentary"]


def test_word_headings_use_actual_report_sections(tmp_path):
    path = tmp_path / "weekly_20261102.docx"
    document = '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>二、社区治理</w:t></w:r></w:p>
    <w:p><w:pPr><w:pStyle w:val="Heading3"/></w:pPr><w:r><w:t>深化“渝邻共治”工作机制</w:t></w:r></w:p>
    <w:p><w:r><w:t>Paragraph text is not a hotspot title.</w:t></w:r></w:p></w:body></w:document>'''
    with zipfile.ZipFile(path, "w") as archive:archive.writestr("word/document.xml", document)
    assert parse_sections(path) == {"社区治理": ["深化“渝邻共治”工作机制"]}


@pytest.mark.parametrize("post,text,leader", [
    ("上海市委常委、浦东新区区委书记", "上海市委常委、浦东新区区委书记李政在会上强调", False),
    ("上海市图们路居民区前党总支书记", "上海市图们路居民区前党总支书记李芳参与活动", False),
    ("上海市委书记", "上海市委书记陈吉宁赴浦东新区调研", True),
])
def test_action_ranking_uses_corrected_locality_for_posts_and_text(post, text, leader):
    snap = Snapshot(nodes={
        "event": dict(uri="event", name="示例调研", label="调研视察"),
        "person": dict(uri="person", name="示例人名", label="领导干部"),
        "post": dict(uri="post", name=post, label="职务")}, edges=[
            dict(subject="person", object="post", predicateLabel="现任", articleIds={"a1"}),
            dict(subject="person", object="event", predicateLabel="参与", articleIds={"a1"})],
        articles={"a1": dict(publishedDate="2026-11-09", title="调研活动", rawText=text, nodeUris=["event", "person", "post"])})
    selected = action_candidates(snap, "2026-11-02", "2026-11-09", min_edges=1)
    assert len(selected) == 1 and selected[0]["leader"] is leader
    assert (selected[0]["score"] >= 300) is leader


def test_weekly_operations_stay_local_and_server_has_only_generic_queries(tmp_path, monkeypatch):
    import httpx
    import osbi_client.weekly as module
    from fastapi.testclient import TestClient
    from osbi_client.app import create_app
    from osbi_client.daemon import Daemon
    calls = []
    def remote(request):
        calls.append(request.url.path)
        return httpx.Response(404)
    async def load(*args, **kwargs):return snapshot()
    monkeypatch.setattr(module, "load_snapshot", load)
    body = dict(fromDate="2026-10-26", toDate="2026-11-02", minEdges=1)
    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(remote))
    daemon.state.save_config({"serverUrl": "http://127.0.0.1:8078"})
    with TestClient(create_app(daemon=daemon), base_url="http://127.0.0.1") as local:
        assert local.post("/api/weekly/community/select", json=body).status_code == 401
        local.get("/graph")
        plan = local.post("/api/weekly/community/select", json=body, headers={"X-OSBI-Local": "1"}).json()
        assert plan["hotspots"][0]["uri"] == "u1"
        result = local.post("/api/weekly/community/record", json={"planId": plan["planId"]}, headers={"X-OSBI-Local": "1"})
        assert result.status_code == 200 and result.json()["recorded"] == 1
        assert len(daemon.state.rows("SELECT * FROM weekly_hotspots")) == 1
        assert local.post("/api/server/channels/policy/weekly/select", json=body, headers={"X-OSBI-Local": "1"}).status_code == 404
    # coverage scope and selection are local; the server only serves generic queries
    assert calls == []
    daemon.state.close()
