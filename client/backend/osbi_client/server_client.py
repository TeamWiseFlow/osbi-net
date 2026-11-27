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
    def __init__(self, message, status=0, retry_after=10, *, code="server_error"):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.code = code


def is_loopback(url):
    return urlsplit(url).hostname in LOOPBACK_HOSTS


def _tls_error(error):
    """Recognize wrapped certificate errors without exposing their details."""
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        pending.extend(cause for cause in (current.__cause__, current.__context__) if cause)
    return False


async def request(config, method, path, payload=None, *, transport=None):
    parsed = urlsplit(config.serverUrl)
    local = parsed.hostname in LOOPBACK_HOSTS
    if not config.serverUrl or not (parsed.scheme == "https" or (local and parsed.scheme == "http")):
        raise ServerError("请先设置服务地址（远程必须 HTTPS，本地联调可用 HTTP loopback）", 401,
                          code="invalid_service_url")
    headers = {"Authorization": "Bearer " + config.serverKey} if config.serverKey else {}
    try:
        verify = ssl.create_default_context(cafile=config.serverCA or None) if parsed.scheme == "https" else False
    except (OSError, ValueError) as error:
        raise ServerError("共享服务证书配置无效，请检查自定义 CA 设置", code="tls_configuration_error") from error
    selection = method.upper() == "POST" and urlsplit(path).path == "/v1/materials/extract"
    read_timeout = config.selectionReadTimeoutSeconds if selection else config.timeoutSeconds
    timeout = httpx.Timeout(config.timeoutSeconds, connect=min(10, config.timeoutSeconds), read=read_timeout)
    try:
        async with httpx.AsyncClient(verify=verify, timeout=timeout, transport=transport,
                                    follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, config.serverUrl + path, headers=headers, json=payload)
        if not response.is_success:
            labels = {401: "客户 Key 无效、已到期或被撤销", 403: "客户 Key 无此操作权限", 404: "请求的数据不存在", 429: "服务繁忙，稍后重试"}
            try:
                retry_after = min(300, max(1, int(response.headers.get("retry-after", "10"))))
            except ValueError:
                retry_after = 10
            codes = {401: "authentication_failed", 403: "permission_denied", 404: "not_found", 429: "rate_limited"}
            code = codes.get(response.status_code, "server_unavailable" if response.status_code >= 500 else "request_failed")
            raise ServerError(labels.get(response.status_code, "服务请求失败"), response.status_code, retry_after, code=code)
        try:
            return response.json()
        except ValueError as error:
            raise ServerError("共享服务返回了无效的 JSON 响应", code="invalid_response") from error
    except ServerError:
        raise
    except httpx.ReadTimeout as error:
        raise ServerError(f"等待共享服务响应超时（读取等待 {read_timeout} 秒），请求可能仍在服务端处理中",
                          code="read_timeout") from error
    except httpx.ConnectTimeout as error:
        raise ServerError("连接共享服务超时，请检查网络和服务地址", code="connect_timeout") from error
    except httpx.WriteTimeout as error:
        raise ServerError("向共享服务发送请求超时，请检查网络后重试", code="write_timeout") from error
    except httpx.PoolTimeout as error:
        raise ServerError("等待共享服务连接资源超时，请稍后重试", code="pool_timeout") from error
    except (httpx.HTTPError, OSError) as error:
        if _tls_error(error):
            raise ServerError("共享服务 TLS 或证书验证失败，请检查证书和自定义 CA 设置", code="tls_error") from error
        raise ServerError("共享服务连接或传输失败，请检查网络和服务地址", code="connection_error") from error
