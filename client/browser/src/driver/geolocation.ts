/** Match browser geography to the real direct or configured proxy exit. */
import { readFileSync } from "node:fs";
import { Reader } from "maxmind";
import { publicIP, validIPv4, validIPv6 } from "camoufox/dist/ip.js";
import { Geolocation, SELECTOR } from "camoufox/dist/locales.js";
import { parseProxySettings } from "../state/proxy.js";
import { RuntimeError } from "../protocol/index.js";

export async function locateExit(database: string, proxy: string | null,
  previous: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
  try {
    let route: string | undefined;
    if (proxy) {
      const settings = parseProxySettings(proxy).proxy;
      const url = new URL(settings.server);
      if (settings.username) { url.username = settings.username; url.password = settings.password ?? ""; }
      route = url.href;
    }
    // The SDK probes over HTTPS through the same proxy, or directly without one.
    // Only the resulting public IP is looked up in the pinned local database.
    const ip = await publicIP(route);
    if (!validIPv4(ip) && !validIPv6(ip)) throw new Error("Invalid exit IP");
    const record = new Reader<any>(readFileSync(database)).get(ip);
    const region = record?.country?.iso_code;
    const location = record?.location;
    if (typeof region !== "string" || !/^[A-Z]{2}$/.test(region) ||
        typeof location?.time_zone !== "string" || !Number.isFinite(location?.longitude) ||
        !Number.isFinite(location?.latitude) || Math.abs(location.longitude) > 180 || Math.abs(location.latitude) > 90)
      throw new Error("Incomplete GeoIP record");
    new Intl.DateTimeFormat("en", { timeZone: location.time_zone });
    const locale = await SELECTOR.fromRegion(region);
    const config: Record<string, unknown> = new Geolocation(locale, location.longitude, location.latitude, location.time_zone).asConfig();
    if (previous["locale:region"] === region && typeof previous["locale:language"] === "string")
      for (const key of ["locale:language", "locale:region", "locale:script"])
        if (typeof previous[key] === "string") config[key] = previous[key];
    config[validIPv4(ip) ? "webrtc:ipv4" : "webrtc:ipv6"] = ip;
    return config;
  } catch {
    // Proxy/network exceptions may contain credentials. Never forward them to logs.
    throw new RuntimeError("GEOIP_LOOKUP_FAILED", "Could not match browser geography to its network exit; check connectivity/proxy and the installed GeoIP database");
  }
}
