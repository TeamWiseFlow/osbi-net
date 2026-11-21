"""ImageEnricher heuristics, decorative filtering, dedup, caching and degradation."""
import asyncio

import pytest

from osbi_client.models import VisionConfig
from osbi_client.image_material import ImageEnricher, _decorative_text
from osbi_client.vision import VisionError, InvalidImageError, DECORATIVE_MARKER
from osbi_client.daemon import Daemon
from test_client import Browser

BASE = "https://mp.weixin.qq.com/s/article"


class FakeVision:
    def __init__(self, mapping=None, fail=None):
        self.mapping = mapping or {}
        self.fail = fail or {}
        self.calls = []

    async def recognize(self, url):
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.mapping.get(url, "识别文字")


def enricher(client=None, cache=None, events=None):
    config = VisionConfig(enabled=True, apiKey="k")
    return ImageEnricher(config, client=client or FakeVision(), cache=cache,
                         event=lambda kind, msg, **k: (events if events is not None else []).append((kind, k.get("details"))))


def img(src="https://img.example/a.png", **kw):
    base = {"src": src, "dataSrc": "", "alt": "", "complete": True, "width": 800, "height": 600, "offset": 0}
    base.update(kw)
    return base


def run_one(enr, image, segment=None):
    asyncio.run(enr._one(image, segment or {"text": "短正文", "links": []}, BASE))
    return image


def test_recognized_text_fills_image():
    client = FakeVision({"https://img.example/a.png": "图中文字"})
    image = run_one(enricher(client), img())
    assert image["text"] == "图中文字" and image["source"] == "vision" and image["status"] == "ok"
    assert client.calls == ["https://img.example/a.png"]


def test_tracker_skipped_without_call():
    client = FakeVision()
    image = run_one(enricher(client), img(src="https://img.example/1x1.gif"))
    assert image["status"] == "skipped" and client.calls == []


def test_tiny_image_skipped():
    client = FakeVision()
    image = run_one(enricher(client), img(width=10, height=10))
    assert image["status"] == "skipped" and client.calls == []


def test_broken_image_skipped():
    client = FakeVision()
    image = run_one(enricher(client), img(width=0, height=0))
    assert image["source"] == "unavailable-image" and client.calls == []


def test_empty_src_skipped():
    client = FakeVision()
    image = run_one(enricher(client), img(src="", dataSrc=""))
    assert image["status"] == "skipped" and client.calls == []


def test_sufficient_alt_shortcut():
    client = FakeVision()
    image = run_one(enricher(client), img(alt="一张足够长的替代描述文字"))
    assert image["text"] == "一张足够长的替代描述文字" and image["source"] == "alt"
    assert client.calls == []


def test_generic_alt_cleared_then_recognized():
    client = FakeVision({"https://img.example/a.png": "文字"})
    image = run_one(enricher(client), img(alt="图片"))
    assert image["source"] == "vision" and client.calls


def test_surrounding_text_covered():
    client = FakeVision()
    image = run_one(enricher(client), img(), segment={"text": "长" * 200, "links": []})
    assert image["source"] == "surrounding-text" and image["status"] == "covered"
    assert client.calls == []


def test_decorative_marker_dropped():
    client = FakeVision({"https://img.example/a.png": DECORATIVE_MARKER})
    image = run_one(enricher(client), img())
    assert image["source"] == "vision-decoration" and image["status"] == "skipped" and image["text"] == ""


def test_decorative_keyword_description_dropped():
    client = FakeVision({"https://img.example/a.png": "（简约插画渐变色线条背景）"})
    image = run_one(enricher(client), img())
    assert image["source"] == "vision-decoration"


def test_genuine_scene_description_kept():
    client = FakeVision({"https://img.example/a.png": "（一间办公室的照片）"})
    image = run_one(enricher(client), img())
    assert image["text"] == "（一间办公室的照片）" and image["status"] == "ok"


def test_mixed_marker_stripped():
    client = FakeVision({"https://img.example/a.png": f"{DECORATIVE_MARKER}真实文字"})
    image = run_one(enricher(client), img())
    assert image["text"] == "真实文字" and image["status"] == "ok"


def test_invalid_image_skipped_and_event():
    events = []
    client = FakeVision(fail={"https://img.example/a.png": InvalidImageError("bad image", status=400)})
    image = run_one(enricher(client, events=events), img())
    assert image["source"] == "vision-invalid" and image["status"] == "skipped"
    assert any(k == "image_invalid" for k, _ in events)


def test_failure_degrades_to_failed():
    client = FakeVision(fail={"https://img.example/a.png": VisionError("chain down")})
    image = run_one(enricher(client), img())
    assert image["source"] == "vision-failed" and image["status"] == "failed"


def test_per_url_dedup_one_call():
    client = FakeVision()
    enr = enricher(client)
    capture = {"requestedUrl": BASE, "finalUrl": BASE, "status": {"access": "ok", "load": "ready"},
               "dom": {"segments": [{"text": "短", "links": [], "inMain": True,
                                     "images": [img(), img()]}]}}
    import osbi_client.image_material as mod
    orig = mod.image_eligible
    mod.image_eligible = lambda s, d: True
    try:
        result = asyncio.run(enr.enrich(capture))
    finally:
        mod.image_eligible = orig
    assert client.calls == ["https://img.example/a.png"]  # repeated image recognized once
    assert all(i["text"] == "识别文字" for i in result["dom"]["segments"][0]["images"])


def test_cache_hit_avoids_second_call(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    client = FakeVision()
    enr = enricher(client, cache=daemon.cache)
    run_one(enr, img())
    assert len(client.calls) == 1
    # a fresh enricher with the same cache hits the stored text
    client2 = FakeVision()
    enr2 = enricher(client2, cache=daemon.cache)
    image = run_one(enr2, img())
    assert image["text"] == "识别文字" and client2.calls == []
    daemon.state.close()


def test_failures_not_cached(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    client = FakeVision(fail={"https://img.example/a.png": VisionError("down")})
    run_one(enricher(client, cache=daemon.cache), img())
    assert daemon.cache.image_text_get("https://img.example/a.png") is None
    daemon.state.close()


def test_enrich_does_not_mutate_input():
    client = FakeVision()
    capture = {"requestedUrl": BASE, "finalUrl": BASE, "status": {"access": "ok", "load": "ready"},
               "dom": {"segments": [{"text": "短", "links": [], "inMain": True, "images": [img()]}]}}
    import osbi_client.image_material as mod
    orig = mod.image_eligible
    mod.image_eligible = lambda s, d: True
    try:
        asyncio.run(enricher(client).enrich(capture))
    finally:
        mod.image_eligible = orig
    assert "text" not in capture["dom"]["segments"][0]["images"][0]  # original untouched


def test_decorative_text_helper():
    assert _decorative_text(DECORATIVE_MARKER)
    assert _decorative_text("（城市天际线轮廓剪影）")
    assert not _decorative_text("（会议室里正在开会）")
    assert not _decorative_text("普通识别文字")
