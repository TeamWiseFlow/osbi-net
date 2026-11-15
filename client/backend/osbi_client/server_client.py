"""HTTPS-only shared-server client with bounded and redacted errors."""
import ssl
import httpx


class ServerError(RuntimeError):
    def __init__(self, message, status=0, retry_after=10):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


async def request(config, method, path, payload=None, *, transport=None):
    if not config.serverUrl.startswith("https://") or not config.serverKey:
        raise ServerError("请先设置 HTTPS 服务地址和客户 Key", 401)
    context = ssl.create_default_context(cafile=config.serverCA or None)
    try:
        async with httpx.AsyncClient(verify=context, timeout=config.timeoutSeconds, transport=transport,
                                    follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, config.serverUrl + path,
                                            headers={"Authorization": "Bearer " + config.serverKey}, json=payload)
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
