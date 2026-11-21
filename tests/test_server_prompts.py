"""Configured prompts reach every model stage and isolate cached and queued work."""
import asyncio
from dataclasses import asdict, replace
import json
import time
import pytest
from fastapi.testclient import TestClient
from osbi_contracts.models import Material
from osbi_server.admin import reprocess
from osbi_server.app import create_app
from osbi_server.config import PromptSettings
from conftest import ExampleSettings as Settings
from osbi_server.service import Service
from conftest import Model, material, wait_job


@pytest.fixture(autouse=True)
def private_example_manifest(example_channel_manifest):
    pass


class CaptureModel(Model):
    def __init__(self):
        super().__init__()
        self.prompts = {}

    async def __call__(self, messages, model):
        body = json.loads(messages[1]["content"])
        stage = body.get("stage") or "unexpected"
        self.prompts.setdefault(stage, []).append(messages[0]["content"])
        return await super().__call__(messages, model)


def configured(marker):
    return PromptSettings(**{key: value + "\n" + marker for key, value in asdict(PromptSettings()).items()})


def assert_prompts(model, prompts, index=-1):
    for stage, key in {"ontology_entities": "entities", "ontology_relations": "relations"}.items():
        assert model.prompts[stage][index] == getattr(prompts, key)


def wait_work(service, work_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        row = service.repo.one("SELECT * FROM work WHERE id=?", (work_id,))
        if row["state"] in {"succeeded", "failed"}:
            assert row["state"] == "succeeded", row
            return json.loads(row["result"])
        time.sleep(.03)
    raise AssertionError("Queued work did not finish")


def test_partial_prompt_file_loads_once_through_config_and_preserves_other_defaults(tmp_path, monkeypatch):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"entities": "Custom entity instructions"}))
    monkeypatch.setenv("OSBI_PROMPTS_FILE", str(path))
    monkeypatch.setenv("OSBI_SERVER_DATA", str(tmp_path / "state"))
    settings = Settings.from_env()
    assert settings.prompts.entities == "Custom entity instructions"
    assert settings.prompts.relations == PromptSettings().relations
    path.write_text(json.dumps({"entities": "Revised entity instructions"}))
    assert settings.prompts.entities == "Custom entity instructions"
    assert Settings.from_env().prompts.fingerprint != settings.prompts.fingerprint
    monkeypatch.delenv("OSBI_PROMPTS_FILE")
    assert Settings.from_env().prompts == PromptSettings()


@pytest.mark.parametrize("body", [[], "invalid", {"entity": "unknown key"}, {"entities": ""},
                                         {"relations": "  "}, {"entities": None}, {"relations": 3}])
def test_invalid_prompt_configuration_fails_before_startup(tmp_path, monkeypatch, body):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(body))
    monkeypatch.setenv("OSBI_PROMPTS_FILE", str(path))
    with pytest.raises(ValueError):Settings.from_env()


def test_configured_prompts_reach_all_stages_and_keep_schema_validation(tmp_path, monkeypatch):
    prompts = configured("Operator customization.")
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(asdict(prompts)))
    monkeypatch.setenv("OSBI_PROMPTS_FILE", str(path))
    monkeypatch.setenv("OSBI_SERVER_DATA", str(tmp_path / "server"))
    model = CaptureModel()
    model.unknown = True
    service = Service(Settings.from_env(), model)
    token = service.repo.issue_key("customer-a", time.time() + 1000)["key"]
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(service=service), base_url="https://testserver") as client:
        job = client.post("/v1/materials", headers=headers, json=material()).json()
        result = wait_job(client, headers, job["jobId"])
        assert result["state"] == "succeeded"
        assert_prompts(model, prompts)
        graph = client.get("/v1/channels/economy/graph", headers=headers).json()
        assert all(node["label"] != "UnapprovedNovelType" for node in graph["nodes"])
        assert all(edge["label"] != "UnapprovedNovelPredicate" for edge in graph["edges"])
        assert "Operator customization" not in json.dumps(result)


@pytest.mark.parametrize("key", ["entities", "relations"])
def test_prompt_change_does_not_reuse_old_work_and_unchanged_graph_prompts_keep_graph_cache(tmp_path, key):
    defaults = PromptSettings()
    first_model = CaptureModel()
    service = Service(Settings(tmp_path / "server"), first_model)
    token = service.repo.issue_key("customer-a", time.time() + 1000)["key"]
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(service=service), base_url="https://testserver") as client:
        first = client.post("/v1/materials", headers=headers, json=material()).json()
        old = wait_job(client, headers, first["jobId"])
        assert old["state"] == "succeeded"
    prompts = replace(defaults, **{key: getattr(defaults, key) + "\nUpdated instructions."})
    second_model = CaptureModel()
    restarted = Service(Settings(tmp_path / "server", prompts=prompts), second_model)
    with TestClient(create_app(service=restarted), base_url="https://testserver") as client:
        original = client.post("/v1/materials", headers=headers, json=material()).json()
        assert original["jobId"] == first["jobId"] and not second_model.calls
        fresh = client.post("/v1/materials", headers=headers, json=material(request="new-settings")).json()
        assert not fresh["reused"]
        current = wait_job(client, headers, fresh["jobId"])
        assert current["state"] == "succeeded"
        changed_graph = key in {"entities", "relations"}
        assert (current["graphs"][0]["id"] != old["graphs"][0]["id"]) is changed_graph
        assert current["graphs"][0]["cached"] is not changed_graph
        assert len(restarted.repo.rows("SELECT * FROM work")) == 2
        before = len(second_model.calls)
        cached = client.post("/v1/materials", headers=headers, json=material(request="same-settings")).json()
        assert cached["reused"] and cached["state"] == "succeeded"
        assert len(second_model.calls) == before


def test_queued_work_keeps_admission_prompts_across_restart(tmp_path):
    old_prompts, new_prompts = configured("Previous instructions."), configured("New instructions.")
    service = Service(Settings(tmp_path / "server", prompts=old_prompts), CaptureModel())
    token = service.repo.issue_key("customer-a", time.time() + 1000)["key"]
    job = service.submit(Material.model_validate(material()), {"customer": "customer-a", "grants": ["*"]})
    assert job["state"] == "pending"
    row = service.repo.one("SELECT id,payload FROM work")
    payload = json.loads(row["payload"])
    payload["prompts"]["info"] = "Retired summary instructions from a pre-upgrade job."
    payload["prompts"]["relevance"] = "Retired relevance instructions."
    payload["prompts"]["routing"] = "Retired routing instructions."
    service.repo.execute("UPDATE work SET payload=? WHERE id=?", (json.dumps(payload), row["id"]))
    asyncio.run(service.close())
    model = CaptureModel()
    restarted = Service(Settings(tmp_path / "server", prompts=new_prompts), model)
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(service=restarted), base_url="https://testserver") as client:
        assert wait_job(client, headers, job["jobId"])["state"] == "succeeded"
        assert_prompts(model, old_prompts)
        fresh = client.post("/v1/materials", headers=headers, json=material(request="new-prompts")).json()
        assert wait_job(client, headers, fresh["jobId"])["state"] == "succeeded"
        assert_prompts(model, new_prompts)


def test_retired_summary_settings_do_not_affect_processing_identity(tmp_path, monkeypatch):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"info": "Retired summary instructions"}))
    monkeypatch.setenv("OSBI_PROMPTS_FILE", str(path))
    monkeypatch.setenv("OSBI_SERVER_DATA", str(tmp_path / "state"))
    monkeypatch.setenv("OSBI_INFO_MODEL", "obsolete-summary-model")
    settings = Settings.from_env()
    assert settings.prompts == PromptSettings()
    assert "info" not in asdict(settings.prompts)
    assert not hasattr(settings, "performance_model")
    path.write_text(json.dumps({"info": "Different retired summary instructions"}))
    assert Settings.from_env().prompts.fingerprint == settings.prompts.fingerprint


def test_force_reprocess_after_previous_reprocess_uses_new_prompt_revision(tmp_path):
    settings = Settings(tmp_path / "server")
    service = Service(settings, CaptureModel())
    token = service.repo.issue_key("customer-a", time.time() + 1000)["key"]
    headers = {"Authorization": "Bearer " + token}
    with TestClient(create_app(service=service), base_url="https://testserver") as client:
        job = client.post("/v1/materials", headers=headers, json=material()).json()
        original = wait_job(client, headers, job["jobId"])
        work_id = reprocess(service.repo, "economy", force=True, settings=settings)["workIds"][0]
        wait_work(service, work_id)
    updated = replace(settings, prompts=configured("Recalibrated extraction."))
    model = CaptureModel()
    restarted = Service(updated, model)
    with TestClient(create_app(service=restarted), base_url="https://testserver"):
        assert reprocess(restarted.repo, "economy", settings=updated)["workIds"] == []
        ids = reprocess(restarted.repo, "economy", force=True, settings=updated)["workIds"]
        assert len(ids) == 1 and ids[0] != work_id
        current = wait_work(restarted, ids[0])
        assert current["graphs"][0]["id"] != original["graphs"][0]["id"]
        assert model.prompts["ontology_entities"][-1] == updated.prompts.entities
        assert model.prompts["ontology_relations"][-1] == updated.prompts.relations
        assert reprocess(restarted.repo, "economy", force=True, settings=updated)["workIds"] == []
