"""Outbound Awada v2 publisher, ported from midplatform/awada_bot/publisher.py."""
import asyncio
import uuid

from .awada_relay import AwadaRelayClient, RelayError, load_config, prepare_payload
from .awada_schemas import FileObject, TextObject


class AwadaPublisher:
    def __init__(self, config, logger):
        self.config = load_config(config)
        self.logger = logger
        self.relay = AwadaRelayClient(self.config, logger)
        self.default_lane = self.relay.lane
        self.default_platform = self.config.get("platform")
        self.last_operation_id = None

    async def send_text(self, text, channel_id="0", platform=None, tenant_id="0",
                        lane=None, user_id_external="0", request_id=None):
        return await self.send_message(
            [TextObject(text=text)], channel_id, platform, tenant_id, lane,
            user_id_external, request_id=request_id)

    async def send_file(self, file_path, file_name=None, channel_id="0", platform=None,
                        tenant_id="0", lane=None, user_id_external="0", request_id=None):
        return await self.send_message(
            [FileObject(file_path=file_path, file_name=file_name)], channel_id, platform,
            tenant_id, lane, user_id_external, request_id=request_id)

    async def send_message(self, payload, channel_id="0", platform=None, tenant_id="0",
                           lane=None, user_id_external="0", event_type="REPLY_MESSAGE", request_id=None):
        """Return True only on provider success; retain the operation ID on failure/pending.

        Supply a stable request_id to retry a scheduled task across process restarts.
        Each call otherwise creates a fresh logical message; transport retries reuse it.
        """
        self.last_operation_id = None
        try:
            if event_type != "REPLY_MESSAGE":
                raise RelayError("UNSUPPORTED_EVENT_TYPE")
            meta = {"channel_id": str(channel_id or "0"),
                    "user_id_external": str(user_id_external or "0"),
                    "tenant_id": str(tenant_id or "0")}
            platform = platform or self.default_platform
            if platform:
                meta["platform"] = platform
            queued = await self.relay.send_message(
                await prepare_payload(payload), meta, lane=lane,
                request_id=request_id or "linxiaozhu-push-" + uuid.uuid4().hex)
            self.last_operation_id = queued["eventId"]
            await self.relay.wait_operation(self.last_operation_id, lane=lane)
            return True
        except Exception as exc:
            code = exc.code if isinstance(exc, RelayError) else type(exc).__name__
            self.logger.error(f"Awada publish failed/pending: {code}, operation={self.last_operation_id}")
            return False

    async def close(self):
        await self.relay.close()


def create_send_msg_func(config, logger):
    def send_msg(text, channel_id="0", platform=None, tenant_id="0", lane=None,
                 user_id_external="0", request_id=None):
        async def send():
            publisher = AwadaPublisher(config, logger)
            try:
                return await publisher.send_text(text, channel_id, platform, tenant_id,
                                                 lane, user_id_external, request_id=request_id)
            finally:
                await publisher.close()
        return asyncio.run(send())
    return send_msg
