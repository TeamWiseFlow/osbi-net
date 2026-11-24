"""Report queries carry sourceScope/territory and allow temporary cross-region queries."""
import asyncio
import json

import httpx

from osbi_client.daemon import Daemon
from osbi_client.weekly import load_snapshot

ITEM = {"text": "原文", "title": "材料", "publishedDate": "2026-11-20",
        "boards": ["社区治理"], "contentTypes": ["news"], "qualityFlags": [],
        "entityCount": 0, "relationCount": 0,
        "sources": [{"title": "材料", "meta": {"requestedUrl": "https://example.org/a",
                                               "publishedAt": "2026-11-20"}}],
        "entities": [], "relations": []}


def test_snapshot_query_carries_scope_and_territory(tmp_path):
    bodies = []

    def handler(request):
        assert request.url.path == "/v1/channels/community/evidence"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"items": [ITEM], "cursor": ""})

    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(handler))
    daemon.state.save_config({"serverUrl": "http://127.0.0.1:8078"})
    snapshot = asyncio.run(load_snapshot(
        daemon, "community", from_date="2026-11-01", to_date="2026-11-22",
        text="治理", territory_preference=["上海市", "静安区"],
        territory_mode="prefer"))
    assert len(snapshot.articles) == 1
    body = bodies[0]
    assert body["sourceScope"] == "all"
    assert body["territoryPreference"] == ["上海市", "静安区"]
    assert body["territoryMode"] == "prefer"
    daemon.state.close()


def test_cross_region_override_is_query_time_only(tmp_path):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"items": [], "cursor": ""})

    daemon = Daemon(tmp_path / "client", transport=httpx.MockTransport(handler))
    daemon.state.save_config({"serverUrl": "http://127.0.0.1:8078"})
    asyncio.run(load_snapshot(
        daemon, "community", from_date="2026-11-01", to_date="2026-11-22",
        territory_preference=["成都市", "锦江区"], territory_mode="only"))
    body = bodies[0]
    assert body["territoryPreference"] == ["成都市", "锦江区"]
    assert body["territoryMode"] == "only"
    # The stored config is untouched; this was a one-off query override.
    assert "成都市" not in json.dumps(daemon.state.config().model_dump(),
                                      ensure_ascii=False)
    daemon.state.close()
