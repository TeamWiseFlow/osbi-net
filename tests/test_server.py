"""Shared-server behavior under authorization, reuse, novelty and schema limits."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import time
from rdflib import Graph
from rdflib.compare import isomorphic
from pathlib import Path
from osbi_server.config import Settings
from osbi_server.ontology_schema import OntologySchema
from osbi_server.service import Service
from osbi_server.app import create_app
from fastapi.testclient import TestClient
from conftest import material, wait_job, Model


def test_initial_schemas_are_distinct_and_available(shared):
    client, service, _, headers = shared
    items = client.get("/v1/channels", headers=headers).json()["items"]
    assert {r["id"] for r in items} == {"policy", "economy"}
    assert len({r["schemaId"] for r in items}) == 2
    assert "居民活动" in service.engine("policy").schema.classes
    assert "经济指标" in service.engine("economy").schema.classes
    assert "经济指标" not in service.engine("policy").schema.classes


def test_concurrent_clients_reuse_work_and_keep_request_ids_and_sources(shared):
    client, service, model, headers = shared
    def submit(i):
        return client.post("/v1/materials", headers=headers,
                           json=material(client=f"client-{i}", request=f"request-{i}", block=f"block-{i}", url=f"https://example.org/source-{i}")).json()
    with ThreadPoolExecutor(max_workers=10) as pool:
        jobs = list(pool.map(submit, range(20)))
    results = [wait_job(client, headers, r["jobId"]) for r in jobs]
    assert all(r["state"] == "succeeded" for r in results)
    assert len(service.repo.rows("SELECT * FROM work")) == 1
    assert len(service.repo.rows("SELECT * FROM requests")) == 20
    assert model.calls.count("ontology_entities") == 1
    assert model.calls.count("ontology_relations") == 1
    assert [r["selection"]["blockIds"] for r in results] == [[f"block-{i}"] for i in range(20)]
    before = len(model.calls)
    last = client.post("/v1/materials", headers=headers, json=material(client="later", request="later")).json()
    assert last["reused"] and last["state"] == "succeeded" and len(model.calls) == before
    data = client.get("/v1/channels/economy/graph", headers=headers).json()
    assert len(data["nodes"]) == 2 and len(data["edges"]) == 1
    assert len(next(iter(data["blocks"].values()))["sources"]) == 21


def test_idempotency_conflicts_and_customer_job_privacy(shared):
    client, service, _, headers = shared
    body = material()
    first = client.post("/v1/materials", headers=headers, json=body)
    again = client.post("/v1/materials", headers=headers, json=body)
    assert first.json()["jobId"] == again.json()["jobId"]
    body["title"] = "different payload"
    assert client.post("/v1/materials", headers=headers, json=body).status_code == 409
    other = service.repo.issue_key("customer-b", time.time()+1000)["key"]
    assert client.get("/v1/jobs/"+first.json()["jobId"], headers={"Authorization":"Bearer "+other}).status_code == 404


def test_authentication_is_checked_before_cached_reads(shared):
    client, service, _, headers = shared
    job = client.post("/v1/materials", headers=headers, json=material()).json()
    wait_job(client, headers, job["jobId"])
    assert client.get("/v1/channels").status_code == 401
    service.repo.execute("UPDATE api_keys SET expires=?", (time.time()-1,))
    assert client.get("/v1/channels", headers=headers).status_code == 401
    assert client.get("/v1/jobs/"+job["jobId"], headers=headers).status_code == 401
    assert client.post("/v1/materials", headers=headers, json=material()).status_code == 401


def test_restricted_channel_key_cannot_read_other_channels(shared):
    client, service, _, _ = shared
    token=service.repo.issue_key("restricted",time.time()+1000,["policy"])["key"]
    h={"Authorization":"Bearer "+token}
    assert [r["id"] for r in client.get("/v1/channels",headers=h).json()["items"]] == ["policy"]
    assert client.get("/v1/channels/economy/graph",headers=h).status_code == 404


def test_novel_channel_uses_existing_generic_schema(shared):
    client, service, _, headers = shared
    body = material(text="天文研究中心参与星空观测活动。")
    job = client.post("/v1/materials", headers=headers, json=body).json()
    result = wait_job(client, headers, job["jobId"])
    assert result["state"] == "succeeded", result
    assert result["assignments"][0]["channelId"] == "astronomy"
    assert service.repo.channel("astronomy")["schema_id"] == service.schema_ids["generic"]
    assert len(service.repo.rows("SELECT * FROM schema_revisions")) == 3


def test_out_of_schema_assertions_never_extend_vocabulary(shared):
    client, service, model, headers = shared
    model.unknown = True
    before = service.engine("economy").schema.fingerprint
    job = client.post("/v1/materials", headers=headers, json=material()).json()
    result = wait_job(client, headers, job["jobId"])
    assert result["state"] == "succeeded"
    graph=client.get("/v1/channels/economy/graph",headers=headers).json()
    assert all(n["label"]!="UnapprovedNovelType" for n in graph["nodes"])
    assert all(e["label"]!="UnapprovedNovelPredicate" for e in graph["edges"])
    assert service.engine("economy").schema.fingerprint == before
    stats=next(iter(graph["blocks"].values()))["statistics"]
    assert stats["dropped_predicate"] >= 1 and stats["droppedSamples"]


def test_batch_window_and_crash_recovery(tmp_path):
    settings=Settings(tmp_path/"batch",processing_mode="batch",batch_window="22:00-03:00",batch_timezone="UTC")
    assert settings.graph_allowed(datetime(2026,11,15,23,tzinfo=timezone.utc))
    assert settings.graph_allowed(datetime(2026,11,16,2,tzinfo=timezone.utc))
    assert not settings.graph_allowed(datetime(2026,11,16,12,tzinfo=timezone.utc))
    model=Model();service=Service(settings,model)
    token=service.repo.issue_key("test",time.time()+1000)["key"]; h={"Authorization":"Bearer "+token}
    settings.graph_allowed=lambda now=None:False
    with TestClient(create_app(service=service),base_url="https://testserver") as client:
        job=client.post("/v1/materials",headers=h,json=material()).json()
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            result=client.get("/v1/jobs/"+job["jobId"],headers=h).json()
            if result["state"]=="graph_pending":break
            time.sleep(.03)
        assert result["state"]=="graph_pending"
        assert "ontology_entities" not in model.calls
    restarted=Service(Settings(settings.data_dir),model)
    with TestClient(create_app(service=restarted),base_url="https://testserver") as client:
        result=wait_job(client,h,job["jobId"])
        assert result["state"]=="succeeded"
        assert len(restarted.repo.rows("SELECT * FROM active_graphs"))==1


def test_plain_http_rejected(shared):
    client, _, _, headers = shared
    assert client.get("http://testserver/v1/channels",headers=headers).status_code==400


def test_new_explicit_submission_recovers_failed_work(shared):
    client,service,model,headers=shared
    model.fail=True
    first=client.post('/v1/materials',headers=headers,json=material()).json()
    assert wait_job(client,headers,first['jobId'],timeout=15)['state']=='failed'
    model.fail=False
    retry=client.post('/v1/materials',headers=headers,json=material(request='explicit-retry')).json()
    assert wait_job(client,headers,retry['jobId'])['state']=='succeeded'
    assert len(service.repo.rows('SELECT * FROM work'))==1
