"""Text delivery through the independent wechat-linux-container HTTP API.

Explicit report target values take precedence over defaults loaded here:
1. ``WECHAT_LINUX_CONTAINER_BASE_URL``, ``WECHAT_LINUX_CONTAINER_TOKEN`` and
   ``WECHAT_LINUX_CONTAINER_WINDOW`` environment variables;
2. the target's ``configPath`` JSON, with a ``wechat-linux-container`` section
   containing ``base_url``, ``token`` and ``window``;
3. built-in defaults (loopback base, empty token/window skips delivery).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)
DEFAULT_BASE = 'http://127.0.0.1:8899'


def load_config(config_path: str | Path = '') -> dict:
    config = {}
    if config_path:
        path = Path(config_path)
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                value = None
            section = value.get('wechat-linux-container') if isinstance(value, dict) else None
            if isinstance(section, dict):
                config = section
    return dict(
        base_url=os.environ.get('WECHAT_LINUX_CONTAINER_BASE_URL') or config.get('base_url') or DEFAULT_BASE,
        token=os.environ.get('WECHAT_LINUX_CONTAINER_TOKEN') or config.get('token', ''),
        window=os.environ.get('WECHAT_LINUX_CONTAINER_WINDOW') or config.get('window', ''),
    )


class WeChatLinuxContainerPusher:
    def __init__(self, *, base_url: str | None = None, token: str | None = None,
                 window: str | None = None, timeout: float = 30,
                 config_path: str | Path = ''):
        config = load_config(config_path)
        self.base_url = (base_url or config['base_url']).rstrip('/')
        self.token = token if token is not None else config['token']
        self.window = window if window is not None else config['window']
        self.timeout = timeout

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f'{self.base_url}/status')
                return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def send_text(self, text: str, window: str | None = None) -> dict:
        target = window or self.window
        if not target:
            logger.warning('wechat-linux-container window 未配置，跳过推送')
            return dict(sent=False, verified=False, window='', message='window 未配置')
        if not self.token:
            logger.warning('wechat-linux-container token 未配置，跳过推送')
            return dict(sent=False, verified=False, window=target, message='token 未配置')
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f'{self.base_url}/send',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={'to': target, 'text': text})
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as error:
            logger.warning('wechat-linux-container 推送失败: %s', error)
            return dict(sent=False, verified=False, window=target,
                        message=f'{type(error).__name__}: {error}')
        sent = bool(body.get('sent'))
        verified = bool(body.get('verified'))
        if not sent:
            logger.warning('wechat-linux-container 推送未被接受: %s', body.get('message', body))
        elif not verified:
            logger.warning('wechat-linux-container 已发送但未通过 DB 验证: %s', body.get('message', body))
        return dict(sent=sent, verified=verified, window=target,
                    message=str(body.get('message', '')), raw=body)

    async def push(self, text: str, *, save_path: Path | None = None,
                   window: str | None = None) -> dict:
        if save_path is not None:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_text(text, encoding='utf-8')
        return await self.send_text(text, window=window)
