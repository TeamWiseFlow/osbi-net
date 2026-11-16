"""Generic server retrieval and local consumption without downstream server policies."""
import asyncio
from httpx import ASGITransport
from osbi_client.daemon import Daemon
from osbi_client.weekly import load_snapshot
from conftest import material, wait_job


def test_evidence_date_entity_and_cursor_queries_are_authorized_and_complete(shared):
    client, service, _, headers = shared
    for i in range(3):
        payload = material(request=f"q{i}", url=f"https://example.org/{i}", text=f"甲科技公司参与产品发布会，展示第{i}款产品。")
        payload["meta"]["publishedAt"] = f"2026-11-{13+i}"
        job = client.post("/v1/materials", headers=headers, json=payload).json()
        assert wait_job(client, headers, job["jobId"])["state"] == "succeeded"
    path = "/v1/channels/economy/evidence"
    assert client.post(path, json={}).status_code == 401
    request = {"fromDate": "2026-11-14", "toDate": "2026-11-15", "limit": 1}
    items = []
    for _ in range(5):
        page = client.post(path, headers=headers, json=request).json()
        items.extend(page["items"])
        if not page["cursor"]:break
        request.update(cursor=page["cursor"], asOf=page["asOf"])
    assert len(items) == 2 and {i["publishedDate"] for i in items} == {"2026-11-14", "2026-11-15"}
    assert all(i["sources"] and i["text"] and i["entities"] for i in items)
    assert client.post(path, headers=headers, json={"entityUris": ["unknown"]}).json()["items"] == []
    assert client.post(path, headers=headers, json={"fromDate": "2026-12-01", "toDate": "2026-11-01"}).status_code == 422
    assert not service.repo.rows("SELECT name FROM sqlite_master WHERE name LIKE 'weekly_%'")
    daemon = Daemon(service.settings.data_dir / "query-client", transport=ASGITransport(app=client.app))
    daemon.state.save_config({"serverUrl": "https://testserver", "serverKey": headers["Authorization"][7:]})
    snapshot = asyncio.run(load_snapshot(daemon, "economy", from_date="2026-11-14", to_date="2026-11-15"))
    assert len(snapshot.articles) == 2 and len(snapshot.nodes) == 2
    assert not daemon.state.rows("SELECT * FROM weekly_plans")
    daemon.state.close()
