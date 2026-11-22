"""Local-model chat client for shadow report editing/writing.

Production shadow pipelines use xin_local on the local xinference endpoint
(http://127.0.0.1:9997/v1, OpenAI-compatible, no auth). The client config
``reportLlm`` section mirrors that default; remote HTTPS endpoints may carry
their own key. Every caller must degrade to deterministic selection/formatting
when the model is unavailable — a report run never fails on LLM errors.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE = 'http://127.0.0.1:9997/v1'
DEFAULT_MODEL = 'xin_local'
LOOPBACK_HOSTS = {'localhost', '127.0.0.1', '::1'}


class LlmError(RuntimeError):
    """Bounded, redacted model-call failure."""


class ReportLlm:
    def __init__(self, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL,
                 api_key: str = '', timeout: int = 240):
        self.base_url = base_url.rstrip('/')
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @classmethod
    def from_config(cls, config) -> 'ReportLlm | None':
        settings = getattr(config, 'reportLlm', None)
        if settings is None or not settings.enabled:
            return None
        parsed = urlsplit(settings.baseUrl)
        local = parsed.hostname in LOOPBACK_HOSTS
        if not local and not settings.apiKey:
            logger.warning('report LLM disabled: remote endpoint without key')
            return None
        return cls(settings.baseUrl, settings.model, settings.apiKey,
                   settings.timeoutSeconds)

    async def chat(self, messages: list, *, temperature: float = 0.1,
                   max_tokens: int = 1024, timeout: int | None = None) -> str:
        headers = {'Authorization': f'Bearer {self.api_key}'} if self.api_key else {}
        payload = dict(model=self.model, messages=messages,
                       temperature=temperature, max_tokens=max_tokens)
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout,
                                         trust_env=False) as client:
                response = await client.post(
                    self.base_url + '/chat/completions',
                    headers=headers, json=payload)
            if not response.is_success:
                raise LlmError(f'model HTTP {response.status_code}')
            data = response.json()
            content = data['choices'][0]['message'].get('content') or ''
            return content
        except (httpx.HTTPError, ValueError, KeyError, IndexError, OSError) as error:
            raise LlmError(f'{type(error).__name__}') from error
