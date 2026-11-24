"""Optional local decision routing with isolated providers and conservative failures."""
import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.decisions import keep_items, filter_graph_result
from test_client import Browser


def configured(tmp_path, handler):
    daemon = Daemon(tmp_path / "client", browser=Browser(), decision_transport=httpx.MockTransport(handler))
    daemon.state.save_config({"serverUrl": "https://shared.example", "serverKey": "server-secret",
        "sources": [{"id": "source", "url": "https://example.org"}],
        "jev": {"enabled": True, "baseUrl": "https://decision.example/v1", "apiKey": "decision-secret", "model": "fixture-model"}})
    return daemon


def response(request, *, choice="discard", confidence=.99):
    payload = json.loads(request.content)
    assert request.url == "https://decision.example/v1/systemone"
    assert request.headers["Authorization"] == "Bearer decision-secret"
    assert "server-secret" not in request.content.decode()
    return httpx.Response(200, json={"answers": {key: {"type": "choice", "choice": choice, "confidence": confidence} for key in payload["questions"]}})


def test_disabled_jev_does_not_call_provider(tmp_path):
    calls = []
    daemon = Daemon(tmp_path / "client", decision_transport=httpx.MockTransport(lambda r: calls.append(r)))
    assert asyncio.run(keep_items(daemon, [{"id": "a", "text": "evidence"}], "intent")) == {"a"}
    assert calls == []
    daemon.state.close()


@pytest.mark.parametrize("mode", ["low-confidence", "unavailable", "malformed", "null-answer", "redirect"])
def test_uncertain_or_failed_post_query_decisions_keep_evidence(tmp_path, mode):
    def handler(request):
        if mode == "low-confidence":return response(request, confidence=.2)
        if mode == "unavailable":return httpx.Response(503)
        if mode == "redirect":return httpx.Response(302, headers={"Location": "https://elsewhere.example"})
        if mode == "null-answer":return httpx.Response(200, json={"answers": {"item_0": None}})
        return httpx.Response(200, json={"answers": {}})
    daemon = configured(tmp_path, handler)
    kept = asyncio.run(keep_items(daemon, [{"id": "a", "title": "T", "text": "evidence"}], "intent"))
    assert kept == {"a"}
    daemon.state.close()


def test_post_query_filter_drops_dangling_paths_without_changing_shared_data(tmp_path):
    daemon = configured(tmp_path, response)
    data = {"nodes": [{"id": "a", "name": "A", "blocks": ["b1"]}, {"id": "b", "name": "B", "blocks": ["b1"]}],
            "edges": [{"source": "a", "target": "b", "blocks": ["b1"]}], "paths": [["a", "b"]],
            "blocks": {"b1": {"text": "original evidence"}}}
    result = asyncio.run(filter_graph_result(daemon, data, "intent"))
    assert result["blocks"] == {} and result["nodes"] == [] and result["paths"] == []
    assert result["jev"]["discardedBlocks"] == 1
    assert data["nodes"] and data["blocks"]
    daemon.state.close()


def test_jev_credentials_are_masked_and_partial_edits_preserve_them(tmp_path):
    daemon = configured(tmp_path, response)
    with TestClient(create_app(daemon=daemon), base_url="http://127.0.0.1") as client:
        client.get("/settings")
        config = client.get("/api/config")
        assert "decision-secret" not in config.text and "server-secret" not in config.text
        assert config.json()["jev"]["keyConfigured"]
        saved = client.patch("/api/config", headers={"X-OSBI-Local": "1"}, json={"jev": {"postQuery": False}})
        assert saved.status_code == 200 and daemon.state.config().jev.apiKey == "decision-secret"
        assert not daemon.state.config().jev.postQuery
        invalid = client.patch("/api/config", headers={"X-OSBI-Local": "1"}, json={"jev": {"baseUrl": "http://remote.example"}})
        assert invalid.status_code == 400 and "decision-secret" not in invalid.text


def test_post_query_hook_only_changes_local_response(tmp_path):
    daemon = configured(tmp_path, response)
    item = {"text": "original evidence", "title": "Story", "publishedDate": "2026-11-15",
            "boards": [], "contentTypes": ["news"], "qualityFlags": [],
            "entityCount": 0, "relationCount": 0,
            "sources": [{"title": "Story", "meta": {"requestedUrl": "https://example.org/story"}}],
            "entities": [], "relations": []}
    seen = []

    def remote(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/libraries/economy/evidence":
            return httpx.Response(200, json={"items": [dict(item)], "cursor": ""})
        if request.url.path == "/v1/ontology/query":
            # wiseflow graph queries carry no original blocks; nothing to filter locally
            return httpx.Response(200, json={"nodes": [{"id": "a", "name": "A", "cls": "Concept"}],
                                             "edges": [], "paths": [], "source": "a",
                                             "target": None, "truncated": False})
        return httpx.Response(404)

    daemon.transport = httpx.MockTransport(remote)
    daemon.state.save_config({"serverUrl": "http://127.0.0.1:8078"})
    with TestClient(create_app(daemon=daemon), base_url="http://127.0.0.1") as local:
        local.get("/graph")
        result = local.post("/api/server/channels/economy/query", headers={"X-OSBI-Local": "1"}, json={"source": "a"})
        assert result.status_code == 200 and result.json()["nodes"]
        page = local.post("/api/server/channels/economy/evidence", headers={"X-OSBI-Local": "1"}, json={})
        assert page.json()["items"] == [] and page.json()["jev"]["discardedRecords"] == 1
        again = local.post("/api/server/channels/economy/evidence", headers={"X-OSBI-Local": "1"}, json={})
        assert again.json()["jev"]["discardedRecords"] == 1  # server data untouched; local filter only
    assert seen == ["/v1/ontology/query", "/v1/ontology/stats",
                    "/v1/libraries/economy/evidence", "/v1/libraries/economy/evidence"]
    daemon.state.close()
