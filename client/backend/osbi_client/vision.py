"""OpenAI-compatible vision client for body-image text recognition.

Ported from the production wiseflow image_client/llm_client: verbatim prompt,
primary->fallback->extra model chain, bounded retry with backoff, and
invalid-image classification that aborts the chain (retrying other models on a
deterministically unusable image only burns quota).
"""
import asyncio
import json
import logging
import re

import httpx

logger = logging.getLogger(__name__)

PROMPT = """逐字提取图片中实际可辨认的文字，保留阅读顺序。不要猜测、补全、改写模糊文字，不要根据照片场景编造标语、说明或规定。
如果仅是网站logo、商标、图标、花纹、分隔线等装饰，直接输出 §decorative-image§。
正文照片没有可辨认文字时，只输出用（）包裹的一句简短场景描述，不推断人物身份、地点或事件。
只输出提取出的文字、简短描述或装饰标记，不要输出解释。"""

DECORATIVE_MARKER = "§decorative-image§"
EMPTY_MARKERS = {"§to_be_recognized_by_visual_llm§", "**empty**"}
RETRY_STATUSES = {429, 500, 502, 503, 504}
INVALID_IMAGE_STATUSES = {400, 413, 415, 422}
INVALID_IMAGE_CODES = {"20015", "20040"}
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 20
_INVALID_DETAIL = re.compile(
    r"invalid image|not a valid image|downloadable URL|image.{0,40}(invalid|unsupported|too large|exceed)",
    re.IGNORECASE)
_THINK_TAG = re.compile(r"<think>.*?</think>", re.DOTALL)


class VisionError(RuntimeError):
    def __init__(self, message, *, retryable=True, status=None, code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.code = code


class InvalidImageError(VisionError):
    def __init__(self, message, *, status=None, code=None):
        super().__init__(message, retryable=False, status=status, code=code)


def _extract_code(text):
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("code") is not None:
            return str(error["code"])
        if data.get("code") is not None:
            return str(data["code"])
    return None


def _is_invalid_image(status, text, code):
    return status in INVALID_IMAGE_STATUSES or str(code) in INVALID_IMAGE_CODES or bool(_INVALID_DETAIL.search(text or ""))


class VisionClient:
    def __init__(self, config, *, transport=None, limit=None):
        self.config = config
        self.transport = transport
        self.limit = limit or asyncio.Semaphore(config.concurrency)

    def candidates(self):
        return list(dict.fromkeys(m for m in (self.config.model, self.config.fallbackModel, self.config.extraModel) if m))

    async def recognize(self, url):
        errors = []
        for model in self.candidates():
            try:
                return await self._attempt(model, url)
            except InvalidImageError:
                raise
            except (VisionError, TimeoutError) as error:
                errors.append(f"{model}: {error}")
        raise VisionError("Model chain failed (" + "; ".join(errors) + ")")

    async def _attempt(self, model, url):
        async with self.limit:
            async with asyncio.timeout(self.config.timeoutSeconds):
                last_error = None
                for attempt in range(MAX_ATTEMPTS):
                    try:
                        return await self._call(model, url)
                    except InvalidImageError:
                        raise
                    except VisionError as error:
                        if not error.retryable:
                            raise
                        last_error = error
                        if attempt < MAX_ATTEMPTS - 1:
                            await asyncio.sleep(BACKOFF_BASE_SECONDS * (2 ** attempt))
                raise last_error

    async def _call(self, model, url):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": url, "detail": "high"}},
                {"type": "text", "text": PROMPT},
            ]}],
            "temperature": 0.1, "max_tokens": 8192, "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.config.apiKey}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.config.timeoutSeconds, follow_redirects=False,
                                     trust_env=False, transport=self.transport) as client:
            try:
                response = await client.post(f"{self.config.baseUrl}/chat/completions", json=body, headers=headers)
            except httpx.HTTPError as error:
                raise VisionError(f"transport error: {error}", retryable=True) from error
        text = response.text
        code = _extract_code(text)
        if _is_invalid_image(response.status_code, text, code):
            raise InvalidImageError(f"invalid image (HTTP {response.status_code}): {text[:200]}",
                                    status=response.status_code, code=code)
        if response.status_code in RETRY_STATUSES:
            raise VisionError(f"HTTP {response.status_code}", retryable=True, status=response.status_code)
        if response.status_code != 200:
            raise VisionError(f"HTTP {response.status_code}: {text[:200]}", retryable=False, status=response.status_code)
        try:
            choice = response.json()["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise VisionError(f"malformed response: {error}", retryable=True) from error
        if finish in {"length", "content_filter"}:
            raise VisionError(f"finish_reason={finish}", retryable=True)
        if not isinstance(content, str) or not content.strip():
            raise VisionError("vision model returned no text", retryable=True)
        return _THINK_TAG.sub("", content).strip()
