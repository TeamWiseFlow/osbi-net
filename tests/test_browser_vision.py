"""Browser.capture vision integration and daemon enricher wiring."""
import asyncio

import httpx
import pytest

from osbi_client.browser import Browser
from osbi_client.models import VisionConfig, ClientConfig
from osbi_client.vision import VisionClient
from osbi_client.image_material import ImageEnricher
from osbi_client.daemon import Daemon
from test_client import Browser as FakeBrowser

MP_URL = "https://mp.weixin.qq.com/s/x"


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    async def nosleep(*a, **k): return None
    monkeypatch.setattr("osbi_client.vision.asyncio.sleep", nosleep)


class DomBrowser(Browser):
    def __init__(self, dom, tmp_path):
        super().__init__(tmp_path / "browser")
        self.dom = dom

    async def command(self, session, action, params=None, lease=None, *, headed=False, timeout=75):
        if action == "eval":
            return {"result": self.dom}
        return {}


def make_dom(text="正文内容文字", image_src="https://img.example/a.png"):
    return {"url": MP_URL, "bodyText": text or "正文", "meta": {"title": "标题"},
            "segments": [{"text": text, "inMain": True, "kind": "paragraph",
                          "path": "html > body > p:nth-of-type(1)", "regionPath": "", "tree": [],
                          "links": [], "images": [{"src": image_src, "dataSrc": "", "alt": "",
                                                   "complete": True, "width": 800, "height": 600, "offset": 2}]}]}


def vl_transport(content="图中文字", status=200):
    return httpx.MockTransport(lambda r: httpx.Response(status, json={
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}]}) if status == 200
        else httpx.Response(status, text="error"))


def make_enricher(transport, **cfg):
    config = VisionConfig(enabled=True, apiKey="k", **cfg)
    return ImageEnricher(config, client=VisionClient(config, transport=transport))


def test_vision_on_fills_block_and_clears_media_pending(tmp_path):
    browser = DomBrowser(make_dom(), tmp_path)
    enr = make_enricher(vl_transport("图中文字"))
    material = asyncio.run(browser.capture("s", MP_URL, "lease", enricher=enr))
    assert any("图中文字" in b["text"] for b in material["blocks"])
    assert material["meta"]["mediaPending"] is False
    assert material["localMaps"]["images"]["I1"]["source"] == "vision"


def test_vision_off_keeps_legacy_media_pending(tmp_path):
    browser = DomBrowser(make_dom(), tmp_path)
    material = asyncio.run(browser.capture("s", MP_URL, "lease"))
    assert material["meta"]["mediaPending"] is True
    assert not any("图中文字" in b["text"] for b in material["blocks"])


def test_vision_partial_failure_keeps_media_pending(tmp_path):
    browser = DomBrowser(make_dom(), tmp_path)
    enr = make_enricher(vl_transport(status=503), model="m1", fallbackModel="", extraModel="")
    material = asyncio.run(browser.capture("s", MP_URL, "lease", enricher=enr))
    assert material["meta"]["mediaPending"] is True
    assert material["localMaps"]["images"]["I1"]["status"] == "failed"
    assert material["blocks"]  # page still submitted with DOM text


def test_pure_image_page_capturable_only_with_vision(tmp_path):
    dom = make_dom(text="")
    browser = DomBrowser(dom, tmp_path)
    enr = make_enricher(vl_transport("纯图片识别文字"))
    material = asyncio.run(browser.capture("s", MP_URL, "lease", enricher=enr))
    assert any("纯图片识别文字" in b["text"] for b in material["blocks"])


def test_daemon_vision_enricher_wiring(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=FakeBrowser())
    disabled = ClientConfig()
    assert daemon._vision_enricher(disabled, "run_x") is None
    enabled = ClientConfig(vision=VisionConfig(enabled=True, apiKey="k"))
    enr = daemon._vision_enricher(enabled, "run_x")
    assert enr is not None and enr.enabled
    # shared semaphore reused for same concurrency
    enr2 = daemon._vision_enricher(enabled, "run_y")
    assert enr.client.limit is enr2.client.limit
    daemon.state.close()
