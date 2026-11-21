"""Isolated providers and original-material fixtures; no production credentials."""
import json
import threading
import time
import pytest
from fastapi.testclient import TestClient
from osbi_server.config import Settings
from osbi_server.channels import ChannelDefinition
from pathlib import Path
from osbi_server.service import Service
from osbi_server.app import create_app


class Model:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.unknown = False
        self.fail = False

    async def __call__(self, messages, model):
        body = json.loads(messages[1]["content"])
        with self.lock:
            self.calls.append(body.get("stage", "unexpected"))
        if self.fail:
            raise RuntimeError("Synthetic provider failure")
        if body.get("stage") == "ontology_entities":
            generic = not any(item['label'] == '企业' for item in body.get('schema', {}).get('classes', []))
            if "天文" in body["text"]:
                items = [{"text": "天文研究中心", "label": "机构组织"}, {"text": "星空观测活动", "label": "事件"}]
            else:
                items = [{"text": "甲科技公司", "label": "机构组织" if generic else "企业"},
                         {"text": "产品发布会", "label": "事件" if generic else "会议论坛"}]
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
        raise AssertionError("Server must call only ontology entity/relation models")


def material(client="client-a", request="request-a", block="original", url="https://example.org/news", text=None, channel="economy"):
    return {"clientId": client, "requestId": request, "title": "最新动态", "focus": {"focuspoint": "关注企业经营动态"},
            "channel": channel,
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
    service = Service(ExampleSettings(tmp_path / "server"), model)
    key = service.repo.issue_key("customer-a", time.time() + 10000)["key"]
    headers = {"Authorization": "Bearer " + key}
    with TestClient(create_app(service=service), base_url="https://testserver") as client:
        yield client, service, model, headers


def example_channels():
    root = Path(__file__).resolve().parents[1] / 'server'
    return (ChannelDefinition('policy', 'Policy', 'Example policy channel', root/'examples/schemas/policy.ttl'),
        ChannelDefinition('economy', 'Economy', 'Example economy channel', root/'examples/schemas/economy.ttl'),
        ChannelDefinition('general', 'General', 'Example template channel', root/'osbi_server/schemas/default.ttl'))


class ExampleSettings(Settings):
    """Tests opt into public examples; production defaults remain a single template."""
    def __init__(self, *args, **kwargs):
        kwargs.setdefault('channel_definitions', example_channels())
        kwargs.setdefault('enabled_schemas', ('policy', 'economy'))
        super().__init__(*args, **kwargs)


@pytest.fixture
def example_channel_manifest(tmp_path, monkeypatch):
    path = tmp_path / 'channels.json'
    path.write_text(json.dumps({'channels': [dict(id=c.id, title=c.title, description=c.description,
        schema=str(c.schema)) for c in example_channels()]}))
    monkeypatch.setenv('OSBI_CHANNELS_FILE', str(path))
    return path
