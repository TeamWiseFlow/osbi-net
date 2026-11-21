"""VisionConfig validation, config merge and key masking."""
import pytest
from pydantic import ValidationError

from osbi_client.models import ClientConfig, VisionConfig
from osbi_client.daemon import Daemon
from test_client import Browser


def test_vision_defaults_ship_production_models():
    v = VisionConfig()
    assert v.enabled is False
    assert v.baseUrl == "https://api.siliconflow.cn/v1"
    assert v.model == "Qwen/Qwen3-VL-32B-Instruct"
    assert v.fallbackModel == "Qwen/Qwen3-VL-30B-A3B-Instruct"
    assert v.extraModel == "zai-org/GLM-4.5V"
    assert v.concurrency == 3
    assert v.timeoutSeconds == 240
    assert v.apiKey == ""


def test_vision_requires_https_or_loopback():
    with pytest.raises(ValidationError):
        VisionConfig(baseUrl="http://api.example.com/v1")
    assert VisionConfig(baseUrl="http://127.0.0.1:8000/v1").baseUrl == "http://127.0.0.1:8000/v1"
    assert VisionConfig(baseUrl="https://api.example.com/v1/").baseUrl == "https://api.example.com/v1"


def test_vision_enabled_remote_requires_key():
    with pytest.raises(ValidationError):
        VisionConfig(enabled=True, baseUrl="https://api.example.com/v1")
    assert VisionConfig(enabled=True, baseUrl="https://api.example.com/v1", apiKey="k").enabled
    # loopback may enable without a key
    assert VisionConfig(enabled=True, baseUrl="http://localhost:8000/v1").enabled


def test_vision_bounds_and_extra_forbidden():
    with pytest.raises(ValidationError):
        VisionConfig(concurrency=0)
    with pytest.raises(ValidationError):
        VisionConfig(concurrency=7)
    with pytest.raises(ValidationError):
        VisionConfig(timeoutSeconds=5)
    with pytest.raises(ValidationError):
        VisionConfig(unknown="x")
    with pytest.raises(ValidationError):
        VisionConfig(model="")


def test_client_config_has_default_vision_and_legacy_validates():
    cfg = ClientConfig()
    assert isinstance(cfg.vision, VisionConfig) and cfg.vision.enabled is False
    legacy = ClientConfig.model_validate({"serverUrl": "https://shared.example", "sources": []})
    assert legacy.vision.enabled is False


def test_save_config_deep_merges_vision_preserving_key(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"vision": {"enabled": True, "baseUrl": "https://api.siliconflow.cn/v1", "apiKey": "secret-vl-key"}})
    # partial PATCH without apiKey preserves the stored key
    daemon.state.save_config({"vision": {"concurrency": 2}})
    assert daemon.state.config().vision.apiKey == "secret-vl-key"
    assert daemon.state.config().vision.concurrency == 2
    assert daemon.state.config().vision.enabled is True
    daemon.state.close()


def test_public_config_masks_vision_key(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"vision": {"enabled": True, "apiKey": "secret-vl-key"}})
    public = daemon.state.public_config()
    assert "secret-vl-key" not in str(public)
    assert "apiKey" not in public["vision"]
    assert public["vision"]["keyConfigured"] is True
    assert public["vision"]["keySuffix"] == "-key"
    daemon.state.close()
