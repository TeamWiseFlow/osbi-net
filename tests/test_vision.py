"""VisionClient VL request shape, model chain, retry and invalid-image handling."""
import asyncio
import json

import httpx
import pytest

from osbi_client.models import VisionConfig
from osbi_client.vision import VisionClient, VisionError, InvalidImageError, PROMPT


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    async def nosleep(*a, **k): return None
    monkeypatch.setattr("osbi_client.vision.asyncio.sleep", nosleep)


def make_client(handler, **cfg):
    config = VisionConfig(enabled=True, baseUrl="https://vl.example/v1", apiKey="k", **cfg)
    return VisionClient(config, transport=httpx.MockTransport(handler))


def ok(content="识别文字", finish="stop"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]})


def test_request_shape_and_success():
    captured = {}
    def handler(request):
        captured["json"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        captured["url"] = str(request.url)
        return ok()
    result = asyncio.run(make_client(handler).recognize("https://img.example/a.png"))
    assert result == "识别文字"
    assert captured["url"] == "https://vl.example/v1/chat/completions"
    assert captured["auth"] == "Bearer k"
    body = captured["json"]
    assert body["model"] == "Qwen/Qwen3-VL-32B-Instruct"
    assert body["temperature"] == 0.1 and body["max_tokens"] == 8192 and body["stream"] is False
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"] == {"url": "https://img.example/a.png", "detail": "high"}
    assert content[1]["text"] == PROMPT


def test_candidates_dedup_and_drop_empty():
    client = make_client(lambda r: ok(), model="m1", fallbackModel="m1", extraModel="")
    assert client.candidates() == ["m1"]
    client2 = make_client(lambda r: ok(), model="m1", fallbackModel="m2", extraModel="m3")
    assert client2.candidates() == ["m1", "m2", "m3"]


def test_chain_falls_through_on_retryable_failure():
    calls = []
    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "m1":
            return httpx.Response(500, text="boom")
        return ok("fallback文字")
    result = asyncio.run(make_client(handler, model="m1", fallbackModel="m2", extraModel="m3").recognize("https://img/a.png"))
    assert result == "fallback文字"
    assert calls.count("m1") == 3 and calls[-1] == "m2"  # retries primary, then falls through


def test_invalid_image_status_aborts_chain():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(400, text="bad image")
    with pytest.raises(InvalidImageError):
        asyncio.run(make_client(handler).recognize("https://img/a.png"))
    assert len(calls) == 1  # no further model attempts


def test_invalid_image_provider_code_aborts_chain():
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json={"error": {"code": "20015", "message": "invalid image"}})
    with pytest.raises(InvalidImageError):
        asyncio.run(make_client(handler).recognize("https://img/a.png"))
    assert len(calls) == 1


def test_invalid_image_detail_regex():
    def handler(request):
        return httpx.Response(422, text="The image is unsupported or too large")
    with pytest.raises(InvalidImageError):
        asyncio.run(make_client(handler).recognize("https://img/a.png"))


def test_429_backoff_then_success(monkeypatch):
    sleeps = []
    async def nosleep(s): sleeps.append(s)
    monkeypatch.setattr("osbi_client.vision.asyncio.sleep", nosleep)
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(429, text="slow down") if len(calls) == 1 else ok()
    result = asyncio.run(make_client(handler).recognize("https://img/a.png"))
    assert result == "识别文字"
    assert sleeps == [20]  # 20 * 2^0


def test_finish_reason_length_is_retryable_then_chain():
    calls = []
    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        return ok("partial", finish="length") if model == "m1" else ok("完整")
    result = asyncio.run(make_client(handler, model="m1", fallbackModel="m2").recognize("https://img/a.png"))
    assert result == "完整"
    assert calls[-1] == "m2"


def test_empty_content_retryable():
    calls = []
    def handler(request):
        calls.append(1)
        return ok("") if len(calls) == 1 else ok("有文字")
    assert asyncio.run(make_client(handler).recognize("https://img/a.png")) == "有文字"


def test_think_tag_stripped():
    def handler(request):
        return ok("<think>推理过程</think>最终文字")
    assert asyncio.run(make_client(handler).recognize("https://img/a.png")) == "最终文字"


def test_chain_exhausted_raises_vision_error():
    def handler(request):
        return httpx.Response(503, text="unavailable")
    with pytest.raises(VisionError) as exc:
        asyncio.run(make_client(handler, model="m1", fallbackModel="m2").recognize("https://img/a.png"))
    assert "Model chain failed" in str(exc.value)


def test_semaphore_bounds_concurrency():
    in_flight = {"now": 0, "max": 0}
    async def handler(request):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return ok()
    config = VisionConfig(enabled=True, baseUrl="https://vl.example/v1", apiKey="k", concurrency=2)
    client = VisionClient(config, transport=httpx.MockTransport(handler))
    async def run():
        await asyncio.gather(*(client.recognize(f"https://img/{i}.png") for i in range(6)))
    asyncio.run(run())
    assert in_flight["max"] <= 2
