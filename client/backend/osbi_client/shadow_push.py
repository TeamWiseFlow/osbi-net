"""Shadow report delivery: MimicWX test group + Awada copy to the temp group.

Ported from midplatform/workflows/shadow_push.py and awada_file_push.py.
The existing MimicWX push keeps targeting its configured test group, unchanged.
On top of that, each shadow push is also sent through Awada to the temporary
群「临小助升级测试」. The extra group is hardcoded on purpose (production
parity); remove this module when the migration ends.

Delivery is opt-in: shadow_reports only calls these helpers when
``reportPush.enabled`` is true; otherwise runs stay dry-run.
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


async def push_shadow_text(text: str, *, window: str | None = None,
                           request_id: str | None = None,
                           push_config=None) -> dict:
    """Keep the existing MimicWX push and add an Awada copy to the temp group."""
    cfg = push_config
    mimicwx = MimicWXPusher(
        base_url=(cfg.mimicwxBaseUrl if cfg else '') or None,
        token=(cfg.mimicwxToken if cfg else '') or None,
        window=window,
        config_path=(cfg.mimicwxConfigPath if cfg else ''))
    mimic, awada = await asyncio.gather(
        mimicwx.send_text(text),
        send_awada_text(text,
                        config_path=(cfg.awadaConfigPath if cfg else ''),
                        channel_id=(cfg.awadaChannelId if cfg else None),
                        request_id=request_id),
        return_exceptions=True,
    )
    if isinstance(mimic, Exception):
        mimic = dict(sent=False, message=f'{type(mimic).__name__}: {mimic}')
    if isinstance(awada, Exception):
        awada = dict(sent=False, message=f'{type(awada).__name__}: {awada}')
    return dict(sent=bool(mimic.get('sent') or awada.get('sent')),
                mimicwx=mimic, awada=awada)


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
