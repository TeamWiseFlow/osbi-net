"""Shared-server client: HTTPS for remote, HTTP loopback for local integration.

Authentication is optional: a configured serverKey is sent as a Bearer token,
while a keyless local wiseflow server receives no Authorization header.
Errors stay bounded and redacted.
"""
import ssl
from urllib.parse import urlsplit
import httpx

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ServerError(RuntimeError):
    def __init__(self, message, status=0, retry_after=10):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def is_loopback(url):
    return urlsplit(url).hostname in LOOPBACK_HOSTS


async def request(config, method, path, payload=None, *, transport=None):
    parsed = urlsplit(config.serverUrl)
    local = parsed.hostname in LOOPBACK_HOSTS
    if not config.serverUrl or not (parsed.scheme == "https" or (local and parsed.scheme == "http")):
        raise ServerError("请先设置服务地址（远程必须 HTTPS，本地联调可用 HTTP loopback）", 401)
    headers = {"Authorization": "Bearer " + config.serverKey} if config.serverKey else {}
    verify = ssl.create_default_context(cafile=config.serverCA or None) if parsed.scheme == "https" else False
    try:
        async with httpx.AsyncClient(verify=verify, timeout=config.timeoutSeconds, transport=transport,
                                    follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, config.serverUrl + path, headers=headers, json=payload)
        if not response.is_success:
            labels = {401: "客户 Key 无效、已到期或被撤销", 403: "客户 Key 无此操作权限", 404: "请求的数据不存在", 429: "服务繁忙，稍后重试"}
            try:
                retry_after = min(300, max(1, int(response.headers.get("retry-after", "10"))))
            except ValueError:
                retry_after = 10
            raise ServerError(labels.get(response.status_code, "服务请求失败"), response.status_code, retry_after)
        return response.json()
    except ServerError:
        raise
    except (httpx.HTTPError, ValueError, OSError) as error:
        raise ServerError("无法连接或验证共享服务，请检查网络、证书和服务设置") from error
