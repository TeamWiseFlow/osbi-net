"""Small OpenAI-compatible HTTP client shared by local vision and the server."""
import asyncio
import json
import re
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import httpx

_usage_observer = ContextVar('llm_usage_observer', default=None)


@contextmanager
def observe_usage(observer):
    """Scope metadata delivery to this task, including transport retries."""
    token = _usage_observer.set(observer)
    try:
        yield
    finally:
        _usage_observer.reset(token)


def _token_count(value):
    return value if type(value) is int and 0 <= value <= 2**63-1 else None


class ModelError(RuntimeError):
    def __init__(self, message, *, retryable=True, status=None, code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.code = code


class InvalidImageError(ModelError):
    """The provider rejected the image payload itself; another model cannot fix it."""

    def __init__(self, message, *, status=None, code=None):
        super().__init__(message, retryable=False, status=status, code=code)


# SiliconFlow image-input errors: no model in the chain can consume this URL/data.
_INVALID_IMAGE_CODES = {'20015', '20040'}
_INVALID_IMAGE_PATTERN = re.compile(
    r'invalid image|not a valid image|downloadable URL|image.{0,40}(?:invalid|unsupported|too large|exceed)',
    re.IGNORECASE)


def _invalid_image_response(status, code, detail):
    return status in {400, 413, 415, 422} and (
        str(code) in _INVALID_IMAGE_CODES or bool(_INVALID_IMAGE_PATTERN.search(detail or '')))


def _is_image_request(messages):
    for message in messages or []:
        content = message.get('content') if isinstance(message, dict) else None
        if isinstance(content, list) and any(
                isinstance(part, dict) and part.get('type') == 'image_url' for part in content):
            return True
    return False


async def generate(chat, messages, model, fallback_model, validate=lambda text: text, *extra_models):
    """Fallback covers transport AND invalid outputs. Return value + actual model.

    extra_models extends the candidate chain (e.g. the third vision model)."""
    chain = list(dict.fromkeys(name for name in (model, fallback_model, *extra_models) if name))
    if not chain:
        raise ModelError('No model configured')
    for index, candidate in enumerate(chain):
        try:
            return validate(await chat(messages, candidate)), candidate
        except Exception as error:
            # A bad image payload is deterministic: retrying the same request on
            # the other vision models only burns quota and delays the page.
            if isinstance(error, ModelError) and not error.retryable:
                raise
            if index == len(chain) - 1:
                detail = str(error)[:250] if isinstance(error, ModelError) else type(error).__name__
                raise ModelError(f'Model chain failed ({", ".join(chain)}): {detail}') from error
            logging.getLogger(__name__).warning('Model %s failed (%s); trying %s',
                                               candidate, type(error).__name__, chain[index + 1])


def parse_json(text):
    if not isinstance(text, str) or not text.strip():
        raise ModelError('Model returned no result')
    text = text.rsplit('</think>', 1)[-1].strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    try:
        return json.loads(text)
    except ValueError as error:
        raise ModelError('Model must return a complete JSON value') from error


class LLMClient:
    def __init__(self, base_url, api_key, *, concurrency=3, timeout=240, client=None, semaphore=None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.timeout = timeout
        self.limit = semaphore if semaphore is not None else asyncio.Semaphore(concurrency)
        self.client = client or httpx.AsyncClient(timeout=timeout)
        self.owns_client = client is None

    async def chat(self, messages, model):
        if not self.api_key or not model:
            raise ModelError('LLM API key and model must be configured')
        async with self.limit:
            try:
                async with asyncio.timeout(self.timeout):
                    return await self._request(messages, model)
            except TimeoutError as error:
                raise ModelError(f'LLM model deadline exceeded ({self.timeout}s)') from error

    async def _post(self, payload):
        observer = _usage_observer.get()
        if observer is None:
            return await self.client.post(self.base_url + '/chat/completions',
                headers={'Authorization': f'Bearer {self.api_key}'}, json=payload)
        event = dict(provider=urlsplit(self.base_url).hostname, model=payload['model'],
                     requested_model=payload['model'], prompt_tokens=None,
                     completion_tokens=None, total_tokens=None, http_status=None,
                     error_code=None)
        try:
            response = await self.client.post(self.base_url + '/chat/completions',
                headers={'Authorization': f'Bearer {self.api_key}'}, json=payload)
            event['http_status'] = response.status_code
            try:
                body = response.json()
                usage = body.get('usage') or {}
                if isinstance(usage, dict):
                    event['prompt_tokens'] = _token_count(usage.get('prompt_tokens', usage.get('input_tokens')))
                    event['completion_tokens'] = _token_count(usage.get('completion_tokens', usage.get('output_tokens')))
                    event['total_tokens'] = _token_count(usage.get('total_tokens'))
                    if event['total_tokens'] is None and all(event[k] is not None for k in ('prompt_tokens','completion_tokens')):
                        event['total_tokens'] = event['prompt_tokens'] + event['completion_tokens']
                actual = body.get('model')
                if isinstance(actual, str) and re.fullmatch(r'[A-Za-z0-9._:/+\-]{1,200}', actual):
                    event['model'] = actual
            except (ValueError, TypeError, AttributeError):
                pass
            return response
        except BaseException as error:
            event['error_code'] = type(error).__name__
            raise
        finally:
            try:
                await observer(event)
            except Exception:
                logging.getLogger(__name__).warning('LLM usage delivery failed; execution is unaffected')

    async def _request(self, messages, model):
        for attempt in range(3):
            try:
                payload = {'model': model, 'messages': messages, 'temperature': 0.1,
                           'max_tokens': 8192, 'stream': False}
                if (model.startswith('Qwen/Qwen3') and '-VL-' not in model) or model.startswith('deepseek-ai/DeepSeek-V3.2'):
                    payload['enable_thinking'] = False
                response = await self._post(payload)
                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(20 * 2 ** attempt)
                    continue
                response.raise_for_status()
                choice = response.json()['choices'][0]
                if choice.get('finish_reason') in {'length', 'content_filter'}:
                    raise ModelError('Model output was truncated or filtered')
                value = choice['message']['content']
                if not isinstance(value, str) or not value.strip():
                    raise ModelError('Model returned no text')
                return value.rsplit('</think>', 1)[-1].strip()
            except httpx.RequestError as error:
                if attempt == 2:
                    raise ModelError('LLM transport failed after three attempts') from error
                await asyncio.sleep(20 * 2 ** attempt)
            except httpx.HTTPStatusError as error:
                status = error.response.status_code
                code = detail = None
                try:
                    payload = error.response.json()
                    if isinstance(payload, dict):
                        code = payload.get('code')
                        detail = payload.get('message') or payload.get('error')
                        if isinstance(detail, (dict, list)):
                            detail = json.dumps(detail, ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
                message = f'LLM request failed (status={status}, code={code}): {detail}'[:500]
                if _invalid_image_response(status, code, detail) or (
                        _is_image_request(messages) and status in {400, 413, 415, 422}):
                    raise InvalidImageError(message, status=status, code=code) from error
                # Any other provider error may be model-specific; keep the old
                # fallback behaviour for text and transient vision failures.
                raise ModelError(message, status=status, code=code) from error
            except (KeyError, IndexError, TypeError, ValueError) as error:
                raise ModelError('LLM response was malformed') from error

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()
