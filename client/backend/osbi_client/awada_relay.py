"""Awada v2 HTTP/WS transport, ported from midplatform/awada_bot/relay_client.py.

Provider credentials stay on the relay; the client only carries the OFB key
(environment: AWADA_OFB_KEY / OFB_KEY, or relay.ofb_key in the config file).
"""
import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx
from dotenv import load_dotenv
from websockets.asyncio.client import connect

DEFAULT_BASE_URL = "https://relay.openclaw-for-business.com/api/v2/awada"
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_BODY_BYTES = 32 * 1024 * 1024
TERMINAL_STATES = {"succeeded", "failed", "unknown"}


class RelayError(Exception):
    """Expose stable error codes, without logging credentials or provider responses."""

    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def load_config(config):
    if isinstance(config, (str, Path)):
        with open(config, encoding="utf-8") as handle:
            return json.load(handle)
    return dict(config)


def request_id(kind, identity):
    return f"linxiaozhu-{kind}-{hashlib.sha256(identity.encode()).hexdigest()}"


def _first_value(data, *keys):
    """Return the first non-empty value; Qiwe callbacks mix camelCase variants."""
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def operation_response(job):
    """Return provider ``data`` from the last successful operation result."""
    for result in reversed(job.get("results") or []):
        response = result.get("response") or {}
        if response.get("code") == 0:
            data = response.get("data")
            return data if isinstance(data, dict) else {}
    return {}


async def prepare_payload(payload):
    """Snapshot local files before persisting/retrying an immutable outbound body."""
    result = []
    for obj in payload:
        item = obj.model_dump(exclude_none=True) if hasattr(obj, "model_dump") else dict(obj)
        path = item.pop("file_path", None)
        if path:
            def read_file():
                with open(path, "rb") as handle:
                    data = handle.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    raise RelayError("FILE_TOO_LARGE")
                return base64.b64encode(data).decode("ascii")

            item["base64"] = await asyncio.to_thread(read_file)
            item["file_name"] = item.get("file_name") or Path(path).name
            item.pop("file_id", None)
            item.pop("file_url", None)
        result.append(item)
    if not result or len(result) > 100:
        raise RelayError("BAD_PAYLOAD_COUNT")
    return result


class AwadaRelayClient:
    def __init__(self, config, logger, *, http_client=None):
        load_dotenv(".env.awada")
        self.config = load_config(config)
        relay = self.config.get("relay", {})
        self.base_url = os.getenv("AWADA_RELAY_URL", relay.get("base_url", DEFAULT_BASE_URL)).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
                parsed.username or parsed.password or parsed.query or parsed.fragment or
                parsed.path.rstrip("/") != "/api/v2/awada"):
            raise ValueError("relay.base_url must be an Awada /api/v2/awada HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Use HTTPS for a remote Awada relay")
        self.key = (os.getenv("AWADA_OFB_KEY") or os.getenv("OFB_KEY") or relay.get("ofb_key", "")).strip()
        if not self.key:
            raise ValueError("Set AWADA_OFB_KEY (or OFB_KEY) for the Awada relay")
        self.lane = os.getenv("AWADA_LANE") or self.config.get("lane") or "user"
        self.logger = logger
        self.timeout = float(relay.get("operation_timeout", 180))
        self.poll_interval = float(relay.get("poll_interval", 10))
        if self.timeout <= 0 or self.poll_interval <= 0:
            raise ValueError("relay operation_timeout and poll_interval must be positive")
        self.http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10), follow_redirects=False
        )

    def ws_connection(self):
        parsed = urlsplit(self.base_url)
        url = urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                          parsed.path + "/inbound", urlencode({"lane": self.lane}), ""))
        return connect(url, additional_headers={"X-OFB-Key": self.key},
                       ping_interval=20, ping_timeout=20, open_timeout=15,
                       max_size=MAX_BODY_BYTES)

    async def _request(self, method, path, *, body=None, lane=None):
        if body is not None and len(json.dumps(body, ensure_ascii=False).encode()) > MAX_BODY_BYTES:
            raise RelayError("BODY_TOO_LARGE")
        headers = {"X-OFB-Key": self.key}
        if body and body.get("request_id"):
            headers["Idempotency-Key"] = body["request_id"]
        for attempt in range(3):
            try:
                response = await self.http.request(
                    method, self.base_url + path, params={"lane": lane or self.lane},
                    headers=headers, json=body
                )
            except httpx.TransportError:
                error = RelayError("RELAY_UNREACHABLE", retryable=True)
            else:
                try:
                    envelope = response.json()
                    code = (envelope.get("error") or {}).get("code", "RELAY_HTTP_ERROR")
                except (ValueError, AttributeError, TypeError):
                    envelope, code = {}, "BAD_RELAY_RESPONSE"
                if response.is_success and envelope.get("success") is True:
                    if isinstance(envelope.get("data"), dict):
                        return envelope["data"]
                    code = "BAD_RELAY_RESPONSE"
                error = RelayError(code, retryable=(response.status_code == 429 or response.status_code >= 500 or code == "BAD_RELAY_RESPONSE"))
            if not error.retryable or attempt == 2:
                raise error
            await asyncio.sleep(2 ** attempt)

    async def capabilities(self):
        return await self._request("GET", "/capabilities")

    async def send_message(self, payload, meta, *, request_id, lane=None):
        # Accept both ContentObject models and plain wire dicts.
        wire_payload = [item.model_dump(exclude_none=True) if hasattr(item, "model_dump") else item
                        for item in payload]
        return await self._request("POST", "/outbound", lane=lane, body={
            "payload": wire_payload, "meta": meta, "request_id": request_id
        })

    async def get_operation(self, operation_id, *, lane=None):
        return await self._request("GET", "/operations/" + quote(operation_id, safe=""), lane=lane)

    async def wait_operation(self, operation_id, *, lane=None):
        deadline = time.monotonic() + self.timeout
        while True:
            job = await self.get_operation(operation_id, lane=lane)
            if job["state"] in TERMINAL_STATES:
                if job["state"] != "succeeded":
                    raise RelayError((job.get("error") or {}).get("code", "OPERATION_" + job["state"].upper()))
                return job
            if time.monotonic() >= deadline:
                # Keep the existing ID: a timeout does not authorize a new write.
                raise RelayError("OPERATION_PENDING")
            await asyncio.sleep(self.poll_interval)

    async def run_operation(self, operation, params, *, request_id=None, lane=None):
        """Submit a catalogued provider operation and wait for its result."""
        body = {"operation": operation, "params": params}
        if request_id:
            body["request_id"] = request_id
        queued = await self._request("POST", "/operations", lane=lane, body=body)
        return await self.wait_operation(queued["eventId"], lane=lane)

    async def download_url(self, content, *, source_event_id):
        """Resolve Qiwe encrypted file metadata through catalogued v2 operations."""
        data = dict(content.provider_data or {})
        name = content.file_name or media_filename(data) or "document"
        try:
            file_size = int(_first_value(data, "fileSize", "size") or 0)
        except (TypeError, ValueError):
            file_size = 0

        # Qiwe callbacks use both ``fileAesKey`` and ``fileAeskey``; older
        # fixtures/tests use the lower-case variant. Accept either spelling.
        file_aes_key = _first_value(data, "fileAesKey", "fileAeskey", "aesKey")
        file_auth_key = _first_value(data, "fileAuthKey", "fileAuthkey", "wechatAuthKey")
        file_url = _first_value(data, "fileHttpUrl", "fileUrl", "url")
        file_id = _first_value(data, "fileId", "fileid") or content.file_id
        file_md5 = _first_value(data, "fileMd5", "md5") or ""

        common = {"fileSize": file_size, "filename": name, "fileMd5": file_md5, "fileType": 5}
        if file_auth_key and file_url:
            operation = ("qiweapi.cloud.wxDownloadAsync" if file_size > MAX_FILE_BYTES
                         else "qiweapi.cloud.wxDownload")
            params = {**common, "fileAeskey": file_aes_key or "",
                      "fileAuthkey": file_auth_key, "fileUrl": file_url}
            required = ["fileUrl", "fileAuthkey", "fileAeskey"]
        elif file_size > MAX_FILE_BYTES:
            operation = "qiweapi.cloud.cdnBigFileDownloadByUrlAsync"
            params = {**common, "fileType": 35, "fileId": file_id,
                      "filekey": file_aes_key or _first_value(data, "filekey") or ""}
            required = ["fileId"]
        else:
            operation = "qiweapi.cloud.wxWorkDownload"
            params = {**common, "fileAeskey": file_aes_key or "", "fileId": file_id}
            required = ["fileId", "fileAeskey"]
        if any(not params.get(key) for key in required) or file_size <= 0:
            raise RelayError("MISSING_MEDIA_METADATA")
        job = await self.run_operation(
            operation, params,
            request_id=request_id("download", f"{self.lane}:{source_event_id}:{json.dumps(params, sort_keys=True)}"))
        for result in job.get("results", []):
            candidates = [(result.get("response") or {}).get("data", {})]
            for callback in result.get("executionResults", []):
                candidates.extend([callback, callback.get("data", {}), callback.get("msgData", {})])
            for candidate in candidates:
                if isinstance(candidate, str):
                    try:
                        candidate = json.loads(candidate)
                    except ValueError:
                        continue
                if isinstance(candidate, dict):
                    url = candidate.get("cloudUrl") or candidate.get("fileUrl")
                    if isinstance(url, str) and urlsplit(url).scheme in {"http", "https"}:
                        return url
        raise RelayError("DOWNLOAD_URL_MISSING")

    async def close(self):
        await self.http.aclose()


def media_filename(data):
    """Qiwe callback fileName is normally base64 UTF-8, unlike native filename."""
    if data.get("filename"):
        return Path(data["filename"]).name
    name = data.get("fileName", "")
    if name:
        try:
            decoded = base64.b64decode(name, validate=True).decode("utf-8")
            if decoded and all(ord(char) >= 32 for char in decoded):
                name = decoded
        except (ValueError, UnicodeDecodeError):
            pass
    return Path(name).name
