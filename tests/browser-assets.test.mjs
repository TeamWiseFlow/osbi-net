import assert from "node:assert/strict";
import { test, afterEach } from "node:test";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync, existsSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, basename } from "node:path";
import AdmZip from "../client/backend/browser/node_modules/adm-zip/adm-zip.js";
import { installAssets, lock } from "../client/backend/browser/scripts/install-assets.mjs";
import { assetPaths } from "../client/backend/browser/scripts/asset-paths.mjs";

const originalLock = structuredClone(lock), originalFetch = globalThis.fetch;
const envKeys = ["OSBI_BROWSER_ASSETS", "OSBI_BROWSER_DOWNLOADS", "OSBI_GITHUB_PROXY", "OSBI_BROWSER_WITH_GEOIP"];
const originalEnv = Object.fromEntries(envKeys.map(k => [k, process.env[k]]));
let root;
afterEach(() => {
  Object.assign(lock, structuredClone(originalLock));
  globalThis.fetch = originalFetch;
  for (const [key, value] of Object.entries(originalEnv))
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  if (root) rmSync(root, { recursive: true, force: true });
});

function fixture({ cached = false, corrupt = false, gone = false } = {}) {
  root = mkdtempSync(join(tmpdir(), "osbi-assets-unit-"));
  process.env.OSBI_BROWSER_ASSETS = join(root, "assets");
  process.env.OSBI_BROWSER_DOWNLOADS = join(root, "downloads");
  process.env.OSBI_GITHUB_PROXY = "";
  process.env.OSBI_BROWSER_WITH_GEOIP = "0";
  mkdirSync(process.env.OSBI_BROWSER_DOWNLOADS);
  const paths = assetPaths();
  mkdirSync(join(paths.directory, "profiles"), { recursive: true });
  writeFileSync(join(paths.directory, "profiles/sentinel"), "owned user state");
  const browser = new AdmZip();
  const launcher = process.platform === "win32" ? "camoufox.exe" : process.platform === "darwin" ? "Camoufox.app/Contents/MacOS/camoufox" : "camoufox-bin";
  browser.addFile(launcher, Buffer.from("isolated binary fixture; never executed"));
  const addon = new AdmZip();
  addon.addFile("manifest.json", Buffer.from(JSON.stringify({ version: lock.ubo.version })));
  const bytes = { browser: browser.toBuffer(), addon: addon.toBuffer(), geoip: Buffer.from("isolated GeoIP fixture") };
  const hash = value => createHash("sha256").update(value).digest("hex");
  lock.assets[`${process.platform}-${process.arch}`].sha256 = hash(bytes.browser);
  lock.ubo.sha256 = hash(bytes.addon);
  lock.geoip.sha256 = hash(bytes.geoip);
  if (cached) writeFileSync(join(process.env.OSBI_BROWSER_DOWNLOADS, basename(paths.geoip)), corrupt ? "bad cache" : bytes.geoip);
  const downloads = [];
  globalThis.fetch = async url => {
    downloads.push(String(url));
    if (String(url).includes("geoip")) return gone ? new Response("gone", { status: 404 }) : new Response(bytes.geoip);
    return new Response(String(url).includes("ublock") ? bytes.addon : bytes.browser);
  };
  return { paths, downloads, modelInstaller: async () => {} };
}

test("normal install always verifies GeoIP and repeats without downloading or changing profiles", async () => {
  const f = fixture();
  await installAssets(f);
  assert.ok(existsSync(f.paths.geoip));
  assert.equal(f.downloads.length, 3);
  await installAssets(f);
  assert.equal(f.downloads.length, 3);
  assert.equal(readFileSync(join(f.paths.directory, "profiles/sentinel"), "utf8"), "owned user state");
});

test("versioned verified cache installs GeoIP when the upstream pin is unavailable", async () => {
  const f = fixture({ cached: true, gone: true });
  await installAssets(f);
  assert.ok(existsSync(f.paths.geoip));
  assert.ok(f.downloads.every(url => !url.includes("geoip")));
});

test("a bad cached database is rejected while core installation continues", async () => {
  const f = fixture({ cached: true, corrupt: true });
  await installAssets(f);
  assert.equal(existsSync(f.paths.geoip), false);
});

test("a missing upstream database permits installation without touching user state", async () => {
  const f = fixture({ gone: true });
  await installAssets(f);
  assert.equal(existsSync(f.paths.geoip), false);
  assert.equal(readFileSync(join(f.paths.directory, "profiles/sentinel"), "utf8"), "owned user state");
});

test("installed corruption fails verification and older database files are retained", async () => {
  const f = fixture();
  const old = join(f.paths.directory, "geoip-aio-all-old.mmdb");
  writeFileSync(old, "retained old database");
  await installAssets(f);
  writeFileSync(f.paths.geoip, "corrupt installed database");
  await installAssets(f);
  assert.equal(readFileSync(old, "utf8"), "retained old database");
});
