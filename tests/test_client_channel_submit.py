"""Source -> channels fan-out, content+channel identity and acquisition-only sources."""
import asyncio
import json

import pytest
from pydantic import ValidationError

from osbi_client.daemon import Daemon
from osbi_client.models import ClientConfig, Source
from test_client import Browser


def config(**overrides):
    base = {"serverUrl": "https://example.test", "serverKey": "key",
            "sources": [{"id": "a", "url": "https://example.org/home",
                         "channels": ["community"]}]}
    base.update(overrides)
    return base


def test_source_rejects_legacy_focus_fields_and_accepts_channel_lists():
    with pytest.raises(ValidationError):
        Source(id="a", url="https://example.org", channel="policy")
    with pytest.raises(ValidationError):
        Source(id="a", url="https://example.org", focusId=9)
    with pytest.raises(ValidationError):
        Source(id="a", url="https://example.org", focuspoint="旧关注")
    assert Source(id="a", url="https://example.org").channels == []
    assert Source(id="a", url="https://example.org",
                  channels=["party-building", "community"]).channels == [
        "party-building", "community"]


def test_client_config_has_no_global_focus_and_no_jev_pre_submit():
    assert "focus" not in ClientConfig().model_dump()
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"focus": "旧全局关注"})
    with pytest.raises(ValidationError):
        ClientConfig.model_validate({"jev": {"preSubmit": True}})


def test_one_source_fans_out_to_each_channel(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config(config(sources=[
        {"id": "a", "url": "https://example.org/home",
         "channels": ["community", "digital"]}]))
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    rows = daemon.state.rows("SELECT * FROM outbox ORDER BY rowid")
    payloads = [json.loads(row["payload"]) for row in rows]
    assert [payload["channel"] for payload in payloads] == ["community", "digital"]
    assert len({payload["materialId"] for payload in payloads}) == 2
    assert payloads[0]["input"] == payloads[1]["input"]
    assert all("focus" not in payload and "ontologyLibrary" not in payload
               for payload in payloads)
    assert all(row["state"] == "pending" for row in rows)
    daemon.state.close()


def test_empty_channels_means_acquisition_only(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config(config(sources=[
        {"id": "a", "url": "https://example.org/home"}]))
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    assert daemon.state.rows("SELECT * FROM outbox") == []
    assert any(event["kind"] == "acquisition_only" for event in daemon.state.events())
    daemon.state.close()


def test_material_identity_changes_with_channel(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config(config(sources=[
        {"id": "a", "url": "https://example.org/home",
         "channels": ["community"]}]))
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    first = json.loads(daemon.state.one("SELECT * FROM outbox")["payload"])
    daemon.state.execute("UPDATE runs SET state='succeeded' WHERE id=?", (run["id"],))
    daemon.state.save_config(config(sources=[
        {"id": "a", "url": "https://example.org/home",
         "channels": ["economy"]}]))
    run = daemon.start_run("a")
    asyncio.run(daemon.acquire(run["id"]))
    second = json.loads(daemon.state.one(
        "SELECT * FROM outbox WHERE state='pending' ORDER BY rowid DESC")["payload"])
    assert first["input"] == second["input"]
    assert first["channel"] != second["channel"]
    assert first["materialId"] != second["materialId"]
    daemon.state.close()
