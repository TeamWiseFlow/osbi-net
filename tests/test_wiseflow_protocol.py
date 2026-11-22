"""Client contract with the production wiseflow server (material-extract/v3).

Covers transport policy (HTTP loopback, optional Bearer), the synchronous
extract request/response shape, local id->url link-back, info discarding,
submission-cache identity and the libraries evidence query.
"""
import asyncio
import json
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import evidence_item, extract_app
from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.models import ClientConfig, Source
from osbi_client.server_client import ServerError, request as server_request
from osbi_client.weekly import action_candidates, load_snapshot, snapshot_from_evidence
from test_client import Browser


class LinkedBrowser(Browser):
    """Home page lists one article link; the article body has no links."""

    async def capture(self, session, url, lease, seed_meta=None, enricher=None):
        value = await super().capture(session, url, lease, seed_meta=seed_meta)
        if url.endswith("/home"):
            value["links"] = [{"id": "L1", "text": "下一篇"}]
            value["localMaps"]["urls"] = {"L1": "https://example.org/article"}
        return value


def recording_transport(app):
    """MockTransport that records every request path and delegates to the ASGI app."""
    seen = []

    async def forward(request):
        seen.append((request.method, request.url.path))
        return await httpx.ASGITransport(app=app).handle_async_request(request)

    return httpx.MockTransport(forward), seen


def local_config(**overrides):
    base = {"serverUrl": "http://127.0.0.1:8078",
            "sources": [{"id": "a", "url": "https://example.org/home"}]}
    base.update(overrides)
    return base


async def finished(daemon, run_id, state="succeeded", timeout=10):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = daemon.state.run(run_id)
        if current["state"] == state:
            return current
        if current["state"] == "failed" and state != "failed":
            raise AssertionError(current)
        await asyncio.sleep(0.02)
    raise AssertionError(daemon.state.run(run_id))


# --- transport policy -------------------------------------------------------


def test_loopback_http_without_key_sends_no_authorization():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"status": "ok"})

    config = ClientConfig(serverUrl="http://127.0.0.1:8078")
    result = asyncio.run(server_request(config, "GET", "/health",
                                        transport=httpx.MockTransport(handler)))
    assert result == {"status": "ok"}
    assert captured["auth"] is None
    assert captured["url"] == "http://127.0.0.1:8078/health"


def test_loopback_http_with_key_sends_bearer():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    config = ClientConfig(serverUrl="http://localhost:8078", serverKey="local-key")
    asyncio.run(server_request(config, "GET", "/health", transport=httpx.MockTransport(handler)))
    assert captured["auth"] == "Bearer local-key"


def test_remote_https_without_key_is_allowed():
    captured = {}

    def handler(request):
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    config = ClientConfig(serverUrl="https://shared.example")
    asyncio.run(server_request(config, "GET", "/health", transport=httpx.MockTransport(handler)))
    assert captured["auth"] is None


def test_remote_http_and_missing_url_are_rejected():
    with pytest.raises(ValidationError):
        ClientConfig(serverUrl="http://shared.example")
    config = ClientConfig()
    with pytest.raises(ServerError):
        asyncio.run(server_request(config, "GET", "/health"))


def test_server_errors_keep_status_and_retry_after():
    def handler(request):
        return httpx.Response(429, headers={"retry-after": "30"})

    config = ClientConfig(serverUrl="http://127.0.0.1:8078")
    with pytest.raises(ServerError) as error:
        asyncio.run(server_request(config, "GET", "/health", transport=httpx.MockTransport(handler)))
    assert error.value.status == 429 and error.value.retry_after == 30


# --- configuration ----------------------------------------------------------


def test_source_channel_and_focus_defaults_and_validation():
    source = Source(id="a", url="https://example.org")
    assert source.channel == "policy" and source.focusId == 0 and source.focuspoint == ""
    source = Source(id="a", url="https://example.org", channel="economy", focusId=9,
                    focuspoint="经济形势、稳定增长")
    assert (source.channel, source.focusId, source.focuspoint) == ("economy", 9, "经济形势、稳定增长")
    with pytest.raises(ValidationError):
        Source(id="a", url="https://example.org", channel="Economy")
    with pytest.raises(ValidationError):
        Source(id="a", url="https://example.org", focusId=-1)


def test_server_url_allows_http_loopback_and_keeps_legacy_configs():
    assert ClientConfig(serverUrl="http://127.0.0.1:8078/").serverUrl == "http://127.0.0.1:8078"
    assert ClientConfig(serverUrl="http://localhost:8078").serverUrl == "http://localhost:8078"
    assert ClientConfig(serverUrl="https://shared.example/").serverUrl == "https://shared.example"
    with pytest.raises(ValidationError):
        ClientConfig(serverUrl="http://shared.example")
    legacy = ClientConfig.model_validate({"serverUrl": "https://shared.example", "serverKey": "k",
                                          "sources": [{"id": "s", "url": "https://example.org"}]})
    assert legacy.sources[0].channel == "policy" and legacy.sources[0].focusId == 0


def test_start_run_requires_only_server_url(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config(local_config())
    run = daemon.start_run("a")
    assert run["state"] == "pending"
    daemon.state.close()


# --- extract request shape --------------------------------------------------


def test_extract_request_shape_focus_and_local_url_map(tmp_path):
    app = extract_app()
    daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                    transport=httpx.ASGITransport(app=app))
    daemon.state.save_config(local_config(focus="全局关注",
        sources=[{"id": "a", "url": "https://example.org/home", "channel": "economy", "focusId": 9}]))
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    row = daemon.state.one("SELECT * FROM outbox")
    payload = json.loads(row["payload"])
    assert payload["protocolVersion"] == "material-extract/v3"
    assert re.fullmatch(r"[a-f0-9]{64}", payload["materialId"])
    assert payload["focus"] == {"id": 9, "focuspoint": "全局关注"}
    assert payload["ontologyLibrary"] == "economy"
    assert payload["mode"] == "both"
    assert payload["input"]["blocks"] == [{"id": "B1", "text": "甲科技公司参与产品发布会。"}]
    assert payload["input"]["links"] == [{"id": "L1", "text": "下一篇", "context": None}]
    assert all("url" not in link for link in payload["input"]["links"])
    assert payload["urlMap"] == {"L1": "https://example.org/article"}
    assert payload["meta"]["requestedUrl"] == "https://example.org/home"
    assert payload["meta"]["textOnly"] is True and payload["meta"]["mediaPending"] is False
    # acquire only enqueues; the outbox loop performs the synchronous POST later
    assert row["state"] == "pending" and app.state.requests == []
    daemon.state.close()


def test_source_focuspoint_overrides_config_focus(tmp_path):
    app = extract_app()
    daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                    transport=httpx.ASGITransport(app=app))
    daemon.state.save_config(local_config(focus="全局关注",
        sources=[{"id": "a", "url": "https://example.org/home", "focuspoint": "来源关注"}]))
    asyncio.run(daemon.acquire(daemon.start_run("a")["id"]))
    payload = json.loads(daemon.state.one("SELECT * FROM outbox")["payload"])
    assert payload["focus"] == {"id": 0, "focuspoint": "来源关注"}
    daemon.state.close()


def test_empty_focus_falls_back_to_nonempty_topic(tmp_path):
    app = extract_app()
    daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                    transport=httpx.ASGITransport(app=app))
    daemon.state.save_config(local_config())
    asyncio.run(daemon.acquire(daemon.start_run("a")["id"]))
    payload = json.loads(daemon.state.one("SELECT * FROM outbox")["payload"])
    # server Focus requires a nonempty topic even without any configured focus
    assert payload["focus"]["focuspoint"].strip()
    daemon.state.close()


# --- synchronous extract, link-back and completion --------------------------


def test_synchronous_extract_follows_links_and_finishes_without_polling(tmp_path):
    app = extract_app()
    transport, seen = recording_transport(app)

    async def run():
        daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(), transport=transport)
        daemon.state.save_config(local_config(maxUrls=5))
        await daemon.start()
        try:
            task = daemon.start_run("a")
            current = await finished(daemon, task["id"])
            assert current["visited"] == ["https://example.org/home", "https://example.org/article"]
            # only synchronous extract calls; the legacy /v1/jobs polling is gone
            assert seen == [("POST", "/v1/materials/extract")] * 2
            rows = daemon.state.rows("SELECT * FROM outbox ORDER BY rowid")
            assert [r["state"] for r in rows] == ["done", "done"]
            for row in rows:
                result = json.loads(row["result"])
                assert "infos" not in result and "infoCount" not in result
                assert "jobId" not in result
            body_result = json.loads(rows[1]["result"])
            assert body_result["ontologyJobs"][0]["status"] == "pending"  # nightly batch, not awaited
            assert any(e["kind"] == "run_finished" for e in daemon.state.events())
        finally:
            await daemon.close()
    asyncio.run(run())


def test_forbidden_domains_block_selected_links(tmp_path):
    app = extract_app()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                        transport=httpx.ASGITransport(app=app))
        daemon.state.save_config(local_config(forbiddenDomains=["example.org"]))
        await daemon.start()
        try:
            current = await finished(daemon, daemon.start_run("a")["id"])
            assert current["visited"] == ["https://example.org/home"]
            assert len(daemon.state.rows("SELECT * FROM outbox")) == 1
        finally:
            await daemon.close()
    asyncio.run(run())


def test_selection_stage_error_fails_run_and_clears_cache(tmp_path):
    def handler(request):
        return {"protocolVersion": "material-extract/v3", "materialId": request.materialId,
                "selection": {"blockIds": [], "linkIds": []},
                "errors": [{"stage": "selection", "message": "provider down"}]}

    async def run():
        daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                        transport=httpx.ASGITransport(app=extract_app(handler)))
        daemon.state.save_config(local_config())
        await daemon.start()
        try:
            current = await finished(daemon, daemon.start_run("a")["id"], "failed")
            assert current["error"]
            row = daemon.state.one("SELECT * FROM outbox")
            assert row["state"] == "failed"
            assert not daemon.state.rows("SELECT * FROM submission_cache")
        finally:
            await daemon.close()
    asyncio.run(run())


def test_ontology_queue_error_does_not_block_completion(tmp_path):
    def handler(request):
        return {"protocolVersion": "material-extract/v3", "materialId": request.materialId,
                "selection": {"blockIds": [b.id for b in request.input.blocks], "linkIds": []},
                "errors": [{"stage": "ontology_queue", "blockId": "B1", "message": "queue busy"}]}

    async def run():
        daemon = Daemon(tmp_path / "client", browser=LinkedBrowser(),
                        transport=httpx.ASGITransport(app=extract_app(handler)))
        daemon.state.save_config(local_config())
        await daemon.start()
        try:
            await finished(daemon, daemon.start_run("a")["id"])
            assert daemon.state.one("SELECT * FROM outbox")["state"] == "done"
        finally:
            await daemon.close()
    asyncio.run(run())


def test_submission_cache_identity_includes_library_and_focus_id(tmp_path):
    app = extract_app()

    async def run():
        daemon = Daemon(tmp_path / "client", browser=Browser(),
                        transport=httpx.ASGITransport(app=app))
        daemon.state.save_config(local_config(
            sources=[{"id": "a", "url": "https://example.org/home", "channel": "economy", "focusId": 9}]))
        await daemon.start()
        try:
            await finished(daemon, daemon.start_run("a")["id"])
            await finished(daemon, daemon.start_run("a")["id"])
            assert len(app.state.requests) == 1  # same material+library+focus reused
            daemon.state.save_config({"sources": [{"id": "a", "url": "https://example.org/home",
                                                   "channel": "economy", "focusId": 2}]})
            await finished(daemon, daemon.start_run("a")["id"])
            assert len(app.state.requests) == 2  # focus id participates in identity
            daemon.state.save_config({"sources": [{"id": "a", "url": "https://example.org/home",
                                                   "channel": "policy", "focusId": 2}]})
            await finished(daemon, daemon.start_run("a")["id"])
            assert len(app.state.requests) == 3  # library participates in identity
            assert [r["ontologyLibrary"] for r in app.state.requests] == ["economy", "economy", "policy"]
        finally:
            await daemon.close()
    asyncio.run(run())


def test_legacy_outbox_rows_fail_explicitly_without_reaching_server(tmp_path):
    def handler(request):
        raise AssertionError("legacy payload must not reach the wiseflow server")

    async def run():
        daemon = Daemon(tmp_path / "client", browser=Browser(),
                        transport=httpx.MockTransport(handler))
        daemon.state.save_config(local_config())
        task = daemon.start_run("a")
        legacy = {"protocolVersion": "osbi-material/v1", "requestId": "r" * 64,
                  "materialHash": "h" * 64, "blocks": [{"id": "B1", "text": "旧材料"}],
                  "links": [], "meta": {"requestedUrl": "https://example.org/home"},
                  "focus": {"focuspoint": "旧关注"}}
        daemon.state.execute(
            "INSERT INTO outbox(id,run_id,payload,state,server_url,server_ca,credential_generation,cache_key,remote_ids) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("r" * 64, task["id"], json.dumps(legacy), "polling", "http://127.0.0.1:8078", "", 1, "", ""))
        daemon.state.execute("UPDATE runs SET state='waiting_server' WHERE id=?", (task["id"],))
        await daemon.start()
        try:
            await finished(daemon, task["id"], "failed")
            row = daemon.state.one("SELECT * FROM outbox")
            assert row["state"] == "failed" and "旧协议" in row["error"]
        finally:
            await daemon.close()
    asyncio.run(run())


# --- evidence query and weekly read model -----------------------------------


def test_load_snapshot_queries_libraries_evidence_and_paginates(tmp_path):
    bodies = []

    def handler(request):
        assert request.url.path == "/v1/libraries/policy/evidence"
        body = json.loads(request.content)
        bodies.append(body)
        if not body.get("cursor"):
            return httpx.Response(200, json={"items": [evidence_item()], "cursor": "c" * 64})
        return httpx.Response(200, json={"items": [evidence_item(url="https://example.org/second",
                                                                  published="2026-11-03")], "cursor": ""})

    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(handler))
    daemon.state.save_config(local_config())
    snapshot = asyncio.run(load_snapshot(daemon, "policy", from_date="2026-11-01",
                                         to_date="2026-11-08", text="治理"))
    assert len(snapshot.articles) == 2
    assert bodies[0]["fromDate"] == "2026-11-01" and bodies[0]["toDate"] == "2026-11-08"
    assert bodies[0]["text"] == "治理" and bodies[0]["limit"] == 100
    assert "asOf" not in bodies[0] and "asOf" not in bodies[1]
    assert bodies[1]["cursor"] == "c" * 64
    daemon.state.close()


def test_snapshot_units_use_org_labels_without_ancestors():
    record = evidence_item(
        entities=[{"uri": "u1", "name": "渝邻共治", "label": "治理机制"},
                  {"uri": "org", "name": "示例社区居委会", "label": "社区自治组织"}],
        relations=[{"subject": "org", "object": "u1", "label": "采用"}])
    snapshot = snapshot_from_evidence([record])
    assert snapshot.nodes["org"]["label"] == "社区自治组织"
    rows = action_candidates(snapshot, "2026-11-01", "2026-11-08", min_edges=1)
    assert len(rows) == 1 and rows[0]["uri"] == "u1"
    assert rows[0]["units"] == ["示例社区居委会"]
    assert rows[0]["edgeCount"] == 1


def test_snapshot_article_identity_uses_published_date_field():
    record = evidence_item(published="2026-11-02", quality_flags=["commentary"])
    snapshot = snapshot_from_evidence([record])
    article = next(iter(snapshot.articles.values()))
    assert article["publishedDate"] == "2026-11-02"
    assert article["qualityFlags"] == ["commentary"]
    assert article["rawText"] == "原文证据。"
    assert article["title"] == "社区治理观察"


# --- local API proxy translation --------------------------------------------


def graph_handler(request):
    path = request.url.path
    if path == "/v1/ontology/libraries":
        return httpx.Response(200, json={"defaultLibrary": "policy", "items": [
            {"id": "policy", "title": "政策与治理图谱", "focusIds": [], "schemaId": "s" * 64}]})
    if path == "/v1/ontology/graph":
        assert request.url.params["libraryId"] == "policy"
        return httpx.Response(200, json={
            "nodes": [{"id": "u1", "name": "上海市", "cls": "CityGov", "nblocks": 1}],
            "edges": [{"source": "u1", "target": "u2", "pred": "participates"}],
            "truncated": False, "stats": {}})
    if path == "/v1/ontology/stats":
        return httpx.Response(200, json={
            "schemaId": "s" * 64,
            "classes": [{"name": "CityGov", "label": "市级机构", "entities": 1}],
            "predicates": [{"name": "participates", "label": "参与", "edges": 1}]})
    if path == "/v1/ontology/batches/status":
        return httpx.Response(200, json={"queue": {"pending": 1, "done": 2, "retrying": 0},
                                         "running": False,
                                         "schedule": {"time": "02:00", "timezone": "Asia/Shanghai"},
                                         "lastBatch": {}})
    if path == "/v1/ontology/query":
        body = json.loads(request.content)
        assert body == {"source": "上海市", "target": None, "maxHops": 2, "libraryId": "policy"}
        return httpx.Response(200, json={"nodes": [{"id": "u1", "name": "上海市", "cls": "CityGov"}],
                                         "edges": [], "paths": [], "source": "u1",
                                         "target": None, "truncated": False})
    if path == "/v1/libraries/policy/evidence":
        return httpx.Response(200, json={"items": [evidence_item()], "cursor": ""})
    return httpx.Response(404)


def test_local_proxy_translates_to_wiseflow_endpoints(tmp_path):
    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(graph_handler))
    daemon.state.save_config(local_config())
    with TestClient(create_app(daemon=daemon), base_url="http://127.0.0.1") as local:
        local.get("/settings")
        headers = {"X-OSBI-Local": "1"}
        channels = local.get("/api/server/channels")
        assert channels.status_code == 200 and channels.json()["items"][0]["id"] == "policy"
        graph = local.get("/api/server/channels/policy/graph").json()
        assert graph["nodes"][0]["label"] == "市级机构" and graph["nodes"][0]["blocks"] == []
        assert graph["edges"][0]["label"] == "参与"
        assert graph["schemaId"] == "s" * 64
        assert graph["status"]["processingMode"] == "batch"
        assert graph["status"]["states"]["succeeded"] == 2
        query = local.post("/api/server/channels/policy/query", headers=headers,
                           json={"source": "上海市", "depth": 2})
        assert query.status_code == 200 and query.json()["nodes"][0]["label"] == "市级机构"
        evidence = local.post("/api/server/channels/policy/evidence", headers=headers,
                              json={"fromDate": "2026-11-01", "toDate": "2026-11-08"})
        assert evidence.status_code == 200 and len(evidence.json()["items"]) == 1
        assert local.post("/api/server/channels/policy/weekly/select", headers=headers,
                          json={}).status_code == 404
    daemon.state.close()
