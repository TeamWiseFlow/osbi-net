"""Report delivery targets: MimicWX, Awada and the reserved 企微 webhook.

Low-level helpers remain available for parity scripts. Report tasks select
targets by ID from the profile; delivery is dry-run unless the task says
otherwise. ``wecom-webhook`` is stored but returns ``not_implemented``.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .awada_publisher import AwadaPublisher
from .push_mimicwx import MimicWXPusher

SHADOW_AWADA_GROUP_NAME = '临小助升级测试'
SHADOW_AWADA_CHANNEL_ID = '10854594337574110'

logger = logging.getLogger(__name__)


async def send_awada_text(text: str, *, config_path: str = '',
                          channel_id: str | None = None,
                          request_id: str | None = None) -> dict:
    """Send one text message through Awada; never raise on provider errors."""
    target = str(channel_id or SHADOW_AWADA_CHANNEL_ID)
    if not config_path:
        return dict(sent=False, channelId=target, operationId=None,
                    message='awada config 未配置')
    publisher = AwadaPublisher(config_path, logger)
    try:
        ok = await publisher.send_text(text, channel_id=target,
                                       request_id=request_id)
        return dict(sent=bool(ok), channelId=target,
                    operationId=publisher.last_operation_id,
                    message='' if ok else 'provider rejected or pending')
    except Exception as error:  # noqa: BLE001 - shadow must not crash
        return dict(sent=False, channelId=target,
                    operationId=publisher.last_operation_id,
                    message=f'{type(error).__name__}: {error}')
    finally:
        await publisher.close()


async def send_awada_file(file_path: str | Path, channel_id: str, *,
                          file_name: str | None = None,
                          config_path: str = '') -> dict:
    """Optional Word-file delivery through the Awada/QiWe publisher.

    MimicWX only exposes text/image endpoints. When a Word document must arrive
    in a group, use the Awada ``send_file`` path with an explicit channel id.
    This is opt-in and never targets production rooms unless the caller passes
    that id. Provider rejections are reported, not raised, so the saved
    docx/TXT remain usable.
    """
    path = Path(file_path)
    if not channel_id:
        return dict(sent=False, channelId='', message='channel_id 未提供',
                    file=str(path))
    if not path.is_file():
        return dict(sent=False, channelId=channel_id, message='文件不存在',
                    file=str(path))
    if not config_path:
        return dict(sent=False, channelId=channel_id, message='awada config 未配置',
                    file=str(path))
    publisher = AwadaPublisher(config_path, logger)
    try:
        ok = await publisher.send_file(
            str(path), file_name=file_name or path.name, channel_id=str(channel_id))
        return dict(sent=bool(ok), channelId=str(channel_id),
                    operationId=publisher.last_operation_id,
                    message='' if ok else 'provider rejected or pending',
                    file=str(path))
    except Exception as error:  # noqa: BLE001 - shadow must not crash
        return dict(sent=False, channelId=str(channel_id),
                    message=f'{type(error).__name__}: {error}', file=str(path))
    finally:
        await publisher.close()


def _cfg(config, *names, default=''):
    config = dict(config or {})
    for name in names:
        value = config.get(name)
        if value not in (None, ''):
            return value
    return default


async def push_to_target(target, text: str, *, file_path: str | None = None,
                         file_name: str | None = None,
                         request_id: str | None = None) -> dict:
    """Send one configured target; reserved types report not_implemented."""
    base = dict(targetId=target.id, type=target.type, label=target.label, sent=False)
    if not target.enabled:
        return {**base, 'message': 'disabled'}
    if target.type == 'wecom-webhook':
        return {**base, 'message': 'not_implemented'}
    if target.type == 'mimicwx':
        pusher = MimicWXPusher(
            base_url=_cfg(target.config, 'baseUrl', 'base_url', default='') or None,
            token=_cfg(target.config, 'token', default='') or None,
            window=_cfg(target.config, 'window', default='') or None,
            config_path=_cfg(target.config, 'configPath', 'config_path', default=''))
        try:
            result = await pusher.send_text(text)
            return {**base, **result, 'sent': bool(result.get('sent'))}
        except Exception as error:  # noqa: BLE001 - delivery must not crash a report
            return {**base, 'message': f'{type(error).__name__}: {error}'}
    if target.type == 'awada':
        config_path = _cfg(target.config, 'configPath', 'config_path', default='')
        channel_id = _cfg(target.config, 'channelId', 'channel_id', default='')
        if file_path:
            file_channel = _cfg(target.config, 'fileChannelId', 'file_channel_id',
                                default=channel_id)
            result = await send_awada_file(file_path, file_channel,
                                           file_name=file_name, config_path=config_path)
        else:
            result = await send_awada_text(text, config_path=config_path,
                                           channel_id=channel_id, request_id=request_id)
        return {**base, **result, 'sent': bool(result.get('sent'))}
    return {**base, 'message': f'unsupported target type: {target.type}'}


async def deliver_targets(profile, task, text: str, *, file_path: str | None = None,
                          file_name: str | None = None, request_id: str | None = None,
                          file_only: bool = False) -> dict:
    """Deliver to every enabled target referenced by the task; never raises."""
    targets = {target.id: target for target in profile.targets}
    results = []
    for target_id in task.targetIds:
        target = targets.get(target_id)
        if target is None:
            results.append(dict(targetId=target_id, sent=False, message='target missing'))
            continue
        if file_only and target.type != 'awada':
            continue
        results.append(await push_to_target(target, text, file_path=file_path,
                                            file_name=file_name, request_id=request_id))
    sent = any(result.get('sent') for result in results)
    if not results:
        message = 'no targets configured'
    elif not any(result.get('message') != 'disabled' for result in results):
        message = 'all targets disabled'
    else:
        message = '' if sent else 'all targets failed or not implemented'
    return dict(sent=sent, dryRun=False, targets=results, message=message)
