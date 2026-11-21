"""Isolated providers and original-material fixtures; no production credentials."""
import json
import threading
import time
import pytest
from fastapi.testclient import TestClient
from osbi_server.config import Settings
from osbi_server.service import Service
from osbi_server.app import create_app


class Model:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.unknown = False
        self.fail = False

    async def __call__(self, messages, model):
        prompt = messages[0]["content"]
        body = json.loads(messages[1]["content"])
        with self.lock:
            self.calls.append(body.get("stage", "select" if "blockIds" in prompt else "info"))
        if self.fail:
            raise RuntimeError("Synthetic provider failure")
        if body.get("stage") == "channel_routing":
            if "天文" in body["text"]:
                return json.dumps({"channelId": None, "category": {"id": "astronomy", "title": "天文学", "description": "Astronomy and space observations"}})
            return json.dumps({"channelId": "economy"})
        if body.get("stage") == "ontology_entities":
            if "天文" in body["text"]:
                items = [{"text": "天文研究中心", "label": "机构组织"}, {"text": "星空观测活动", "label": "事件"}]
            else:
                items = [{"text": "甲科技公司", "label": "企业"}, {"text": "产品发布会", "label": "会议论坛"}]
            if self.unknown:
                items.append({"text": "新产品", "label": "UnapprovedNovelType"})
            return json.dumps({"entities": items}, ensure_ascii=False)
        if body.get("stage") == "ontology_relations":
            items = [{"subject": "甲科技公司", "predicate": "参与", "object": "产品发布会"}]
            if "天文" in body["text"]:
                items = [{"subject": "天文研究中心", "predicate": "参与", "object": "星空观测活动"}]
            if self.unknown:
                items.append({"subject": "甲科技公司", "predicate": "UnapprovedNovelPredicate", "object": "产品发布会"})
            return json.dumps({"relations": items}, ensure_ascii=False)
        if "blockIds" in prompt:
            return json.dumps({"blockIds": [b["id"] for b in body["material"]["blocks"]], "linkIds": []})
        raise AssertionError("Unexpected model stage: independent summaries are not supported")


def material(client="client-a", request="request-a", block="original", url="https://example.org/news", text=None):
    return {"clientId": client, "requestId": request, "title": "最新动态", "focus": {"focuspoint": "关注企业经营动态"},
            "blocks": [{"id": block, "text": text or "甲科技公司参与产品发布会，展示新产品。"}], "links": [],
            "meta": {"requestedUrl": url, "publishedAt": "2026-11-15"}}


def wait_job(client, headers, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/v1/jobs/" + job_id, headers=headers)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.03)
    raise AssertionError("Job did not finish: " + str(job))


@pytest.fixture
def shared(tmp_path):
    model = Model()
    service = Service(Settings(tmp_path / "server"), model)
    key = service.repo.issue_key("customer-a", time.time() + 10000)["key"]
    headers = {"Authorization": "Bearer " + key}
    with TestClient(create_app(service=service), base_url="https://testserver") as client:
        yield client, service, model, headers
