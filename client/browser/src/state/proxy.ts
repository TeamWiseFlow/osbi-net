export interface ProxySettings {
  proxy: {
    server: string;
    username?: string;
    password?: string;
  };
}

export function parseProxySettings(proxyUrl: string): ProxySettings {
  try {
    const parsed = new URL(proxyUrl);
    const scheme = parsed.protocol.slice(0, -1).toLowerCase();
    if (!parsed.hostname || (scheme !== "http" && scheme !== "https")) throw new Error();
    const hostPort = parsed.port ? `${parsed.hostname}:${parsed.port}` : parsed.hostname;
    const proxy: ProxySettings["proxy"] = { server: `${scheme}://${hostPort}` };
    if (parsed.username) {
      proxy.username = decodeURIComponent(parsed.username);
      proxy.password = decodeURIComponent(parsed.password);
    }
    return { proxy };
  } catch {
    // URL parser errors can include credentials from the supplied URL.
    throw new Error("Proxy URL must use http:// or https:// with a valid host and port");
  }
}
