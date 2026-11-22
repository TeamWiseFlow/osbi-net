"""Clients sharing graph evidence retain separate editorial choices and history."""
import asyncio
import json
from types import SimpleNamespace
import httpx
from fastapi.testclient import TestClient
import pytest
from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.models import ClientConfig
from osbi_client.state import State
from osbi_client.weekly import select_topnews, select_weekly, record_weekly, backfill_weekly
from osbi_client.weekly_models import WeeklySelect, TopNewsSelect, WeeklyBackfill


def profile(identifier, keyword):
    board = dict(id="updates", title=keyword + " updates", channel="economy", query=keyword,
                 focus="Explain the departmental impact", topN=1)
    return dict(id=identifier, name=identifier, focus=keyword + " department", topnews=[board], weeklyreport=[board])


def test_two_clients_share_channel_but_select_different_reports(tmp_path):
    queries = []

    def remote(request):
        assert request.url.path == "/v1/libraries/economy/evidence"
        body = json.loads(request.content)
        queries.append(body)
        keyword = body.get("text", "")
        item = {"text": f"甲科技公司参与产品发布会。{keyword} progress and implications.",
                "title": keyword + " update", "publishedDate": "2026-11-15",
                "boards": [], "contentTypes": ["news"], "qualityFlags": [],
                "entityCount": 0, "relationCount": 0,
                "sources": [{"title": keyword + " update",
                             "meta": {"requestedUrl": f"https://example.org/{keyword}",
                                      "publishedAt": "2026-11-15"}}],
                "entities": [], "relations": []}
        return httpx.Response(200, json={"items": [item], "cursor": ""})

    for name in ("Research", "Sales"):
        daemon = Daemon(tmp_path / name, transport=httpx.MockTransport(remote))
        daemon.state.save_config(dict(serverUrl="http://127.0.0.1:8078",
                                      reportProfiles=[profile("department", name)]))
        with TestClient(create_app(daemon=daemon), base_url="http://127.0.0.1") as local:
            local.get("/settings")
            options = dict(profile="department", board="updates", fromDate="2026-11-14", toDate="2026-11-15")
            for path in ("/api/topnews/economy/select", "/api/weekly/economy/select"):
                response = local.post(path, json=options, headers={"X-OSBI-Local": "1"})
                assert response.status_code == 200, response.text
                result = response.json()
                assert len(result["articles"]) == 1 and result["articles"][0]["title"] == name + " update"
                assert name + " department" in result["brief"]
                assert "Explain the departmental impact" in result["brief"]
                if "planId" in result:
                    assert record_weekly(daemon, "economy", result["planId"])["recorded"] == 1
            assert len(daemon.state.rows("SELECT * FROM weekly_hotspots")) == 1
            assert local.post("/api/topnews/policy/select", json=options, headers={"X-OSBI-Local": "1"}).status_code == 400
        daemon.state.close()
    # the server only saw generic evidence retrieval, never editorial state
    assert queries and all("focus" not in body and "profile" not in body for body in queries)


def test_profile_settings_survive_restart_and_reject_ambiguous_ids(tmp_path):
    state = State(tmp_path / "client.db")
    preferences = [profile("research", "Research"), profile("sales", "Sales")]
    state.save_config({"reportProfiles": preferences})
    state.close()
    restored = State(tmp_path / "client.db")
    assert [p.id for p in restored.config().reportProfiles] == ["research", "sales"]
    assert restored.public_config()["reportProfiles"][1]["weeklyreport"][0]["query"] == "Sales"
    with pytest.raises(ValueError):
        restored.save_config({"reportProfiles": [preferences[0], preferences[0]]})
    invalid = profile("research", "Research")
    invalid["topnews"] *= 2
    with pytest.raises(ValueError):
        restored.save_config({"reportProfiles": [invalid]})
    assert len(restored.config().reportProfiles) == 2
    restored.close()


def test_jev_receives_board_specific_intent_but_server_only_receives_retrieval_filters(tmp_path, monkeypatch):
    import osbi_client.decisions as decisions
    intents, queries = [], []
    async def filter_local(daemon, records, intent):
        intents.append(intent)
        return records
    def remote(request):
        queries.append(json.loads(request.content))
        return httpx.Response(200, json={"items": [], "cursor": "", "asOf": 1})
    monkeypatch.setattr(decisions, "filter_evidence", filter_local)
    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(remote))
    daemon.state.save_config(dict(serverUrl="https://testserver", serverKey="fixture", focus="Company focus",
                                  reportProfiles=[profile("research", "Research")]))
    result = asyncio.run(select_topnews(daemon, "economy", TopNewsSelect(profile="research", board="updates",
                         fromDate="2026-11-15", toDate="2026-11-15")))
    assert not result["articles"]
    assert all(text in intents[0] for text in ("Company focus", "Research department", "Explain the departmental impact"))
    assert queries[0]["text"] == "Research" and "focus" not in queries[0]
    assert "department" not in json.dumps(queries[0])
    with pytest.raises(ValueError):
        asyncio.run(select_topnews(daemon, "economy", TopNewsSelect(profile="unknown", fromDate="2026-11-14", toDate="2026-11-15")))
    daemon.state.close()


def test_weekly_profiles_do_not_consume_each_others_coverage(tmp_path, monkeypatch):
    import osbi_client.weekly as weekly
    state = State(tmp_path / "client.db")
    state.save_config({"reportProfiles": [profile("one", "Research"), profile("two", "Research")]})
    current_date = "2026-11-02"
    async def load(*args, **kwargs):
        return weekly.Snapshot({}, [], {"a1": dict(articleId="a1", title="Research update", publishedDate=current_date,
            nodeUris=[], rawText="Research progress", qualityFlags=[])})
    async def scope(daemon):return "same-customer"
    monkeypatch.setattr(weekly, "load_snapshot", load)
    monkeypatch.setattr(weekly, "coverage_scope", scope)
    daemon = SimpleNamespace(state=state)
    plan = asyncio.run(select_weekly(daemon, "economy", WeeklySelect(profile="one", board="updates",
                        fromDate="2026-10-26", toDate="2026-11-02")))
    record_weekly(daemon, "economy", plan["planId"])
    for identifier, expected in (("one", 0), ("two", 1)):
        result = asyncio.run(select_weekly(daemon, "economy", WeeklySelect(profile=identifier, board="updates",
                             fromDate="2026-11-02", toDate="2026-11-09")))
        assert len(result["hotspots"]) == expected
    state.close()


def test_default_configuration_migrates_old_saved_settings_without_editing_them():
    assert ClientConfig.model_validate({"focus": "Existing preference"}).reportProfiles[0].id == "default"
    assert TopNewsSelect(fromDate="2026-11-15", toDate="2026-11-15")
    with pytest.raises(ValueError):WeeklySelect(fromDate="2026-11-15", toDate="2026-11-15")


def test_generic_board_backfill_uses_historical_headings_and_only_past_dated_articles(tmp_path, monkeypatch):
    import osbi_client.weekly as weekly
    state = State(tmp_path / "client.db")
    preference = profile("research", "Research")
    preference["weeklyreport"][0]["query"] = "Present-day-only interest"
    state.save_config({"reportProfiles": [preference]})
    async def scope(daemon):return "same-customer"
    async def load(*args, **kwargs):
        assert kwargs["apply_decisions"] is False and kwargs["to_date"] == "2026-11-02"
        assert not kwargs.get("text")
        return weekly.Snapshot({}, [], {aid: dict(articleId=aid, title="Research update", publishedDate=published,
            nodeUris=[], rawText="Research progress", qualityFlags=[]) for aid, published in (
                ("past", "2026-11-02"), ("future", "2026-12-01"), ("undated", ""))})
    monkeypatch.setattr(weekly, "coverage_scope", scope)
    monkeypatch.setattr(weekly, "load_snapshot", load)
    result = asyncio.run(backfill_weekly(SimpleNamespace(state=state), "economy", WeeklyBackfill(
        profile="research", weekEnd="2026-11-02", sections={"Research updates": ["Research update"]}, apply=True)))
    assert result["recorded"] == 1 and not result["unmatched"]
    assert result["focusByBoard"]["Research updates"]["hotspots"][0]["articleIds"] == ["past"]
    assert len(state.rows("SELECT * FROM weekly_hotspots")) == 1
    state.close()
