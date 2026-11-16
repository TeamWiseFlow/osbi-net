/** Browser launch and managed asset boundary for the pinned SDK. */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { NewBrowser, type NewBrowserOptions } from "camoufox";
import { LIBRARY_VERSION } from "camoufox/dist/__version__.js";
import { generateFingerprint } from "camoufox/dist/fingerprints.js";
import { MODEL_PIN, setModelDir } from "fpgen-js";
import { firefox, type Browser, type BrowserContext } from "playwright-core";
import { assetPaths, fingerprintModelInstalled, lock } from "../../scripts/asset-paths.mjs";
import { RuntimeError } from "../protocol/index.js";
import { locateExit } from "./geolocation.js";

export { assetPaths, lock };
export function assetStatus() {
  const paths = assetPaths();
  let version: string | null = null;
  let addonInstalled = false;
  try { const v = JSON.parse(readFileSync(join(paths.directory, "version.json"), "utf8")); version = `${v.version}-${v.release}`; } catch {}
  try { addonInstalled = JSON.parse(readFileSync(join(paths.addon, "manifest.json"), "utf8")).version === lock.ubo.version; } catch {}
  return { ...paths, installed: existsSync(paths.launcher), version,
    versionMatches: version === `${lock.version}-${lock.release}`,
    modelInstalled: fingerprintModelInstalled(paths.model),
    geoipInstalled: existsSync(paths.geoip), addonInstalled };
}
export function requireAssets(): ReturnType<typeof assetPaths> {
  const status = assetStatus();
  if (!status.installed || !status.versionMatches || !status.addonInstalled || !status.modelInstalled)
    throw new RuntimeError("ASSET_MISSING", "Pinned Camoufox/browser model/addon missing; run osbi-browser install before starting");
  if (lock.sdk.name !== "camoufox" || LIBRARY_VERSION !== lock.sdk.version || MODEL_PIN.sha256 !== lock.model.sha256)
    throw new RuntimeError("ASSET_MISSING", "SDK or fingerprint model differs from assets.lock.json");
  // The SDK configures fpgen during module import; changing an env var afterwards
  // leaves it pointing at the SDK cache. Set the public model directory explicitly.
  setModelDir(status.model);
  return status;
}
export function newFingerprint(os: string, window?: [number, number] | null): Record<string, unknown> {
  requireAssets();
  return generateFingerprint({ os, ...(window ? { window } : {}) });
}
export async function launchBrowser(options: NewBrowserOptions): Promise<Browser | BrowserContext> {
  const paths = requireAssets();
  // Explicit paths and an installed fpgen model prevent SDK auto-install paths.
  // GeoIP has already been resolved with the pinned local database. Disable only
  // the SDK's separate auto-refresh/download path, retaining the resolved config.
  // NewBrowser also applies the SDK's viewport/media defaults for persistent contexts.
  return NewBrowser(firefox, { ...options, executable_path: paths.launcher,
    addons: [paths.addon], exclude_addons: ["UBO"], geoip: false });
}
export const GEOIP_WARNING = { code: "GEOIP_UNAVAILABLE", message: "GeoIP is unavailable; continuing may increase exposure risk and reduce acquisition success" };
export type BrowserWarning = typeof GEOIP_WARNING;
export async function exitGeo(proxy: string | null, previous: Record<string, unknown> = {},
  warn: (warning: BrowserWarning) => void = warning => console.warn(warning.message)): Promise<Record<string, unknown> | null> {
  const paths = requireAssets();
  try {
    if (!existsSync(paths.geoip)) throw new Error("Missing GeoIP database");
    return await locateExit(paths.geoip, proxy, previous, lock.geoip.sha256);
  } catch {
    warn({ ...GEOIP_WARNING });
    return null;
  }
}
