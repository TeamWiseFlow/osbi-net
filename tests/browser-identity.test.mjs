import assert from "node:assert/strict";
import { mock, test, beforeEach, afterEach } from "node:test";
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
let directory, fail, generated, seen, launches, checks;
const page = { on() {}, isClosed: () => false, evaluate: async () => 2, context: () => context };
const context = { pages: () => [page], on() {}, close: async () => {} };
mock.module(new URL("../client/browser/dist/driver/sdk.js", import.meta.url).href, {
  namedExports: {
    newFingerprint: () => { generated++; return { navigator: { userAgent: "stable fixture fingerprint" } }; },
    exitGeo: async (proxy, previous = {}, warn) => {
      seen.push({ proxy, previous });
      if (fail) { warn?.({ code: "GEOIP_UNAVAILABLE", message: "fixture GeoIP warning" }); return null; }
      return { timezone: "Asia/Tokyo", "locale:region": "JP", "locale:language": "ja", "webrtc:ipv4": "203.0.113.8" };
    },
    requireAssets: required => { checks.push(required); return {}; },
    assetStatus: () => ({}),
    launchBrowser: async options => {
      launches.push(options);
      return options.persistent_context ? context : { newPage: async () => page, close: async () => {} };
    },
  },
});
const { loadOrCreate, toLaunchOptions } = await import("../client/browser/dist/state/identity.js");
const { BrowserManager } = await import("../client/browser/dist/driver/browser.js");
const { CamoufoxDriver } = await import("../client/browser/dist/driver/runtime.js");
beforeEach(() => {
  directory = mkdtempSync(join(tmpdir(), "osbi-identity-unit-"));
  fail = false; generated = 0; seen = []; launches = []; checks = [];
});
afterEach(() => rmSync(directory, { recursive: true, force: true }));

test("direct persistent launch refreshes GeoIP without redrawing fingerprint or losing cookies", async () => {
  const first = await loadOrCreate(directory, null, null, true);
  writeFileSync(join(directory, "cookies.sqlite"), "owned cookie fixture");
  const second = await loadOrCreate(directory, null, null, true);
  assert.equal(generated, 1);
  assert.deepEqual(first.fingerprint, second.fingerprint);
  assert.equal(first.created_at, second.created_at);
  assert.deepEqual(seen.map(c => c.proxy), [null, null]);
  assert.equal(seen[1].previous.timezone, "Asia/Tokyo");
  assert.equal(readFileSync(join(directory, "cookies.sqlite"), "utf8"), "owned cookie fixture");
  assert.equal(toLaunchOptions(second).firefox_user_prefs["network.dns.disableIPv6"], true);
});

test("failed GeoIP leaves an existing identity untouched", async () => {
  await loadOrCreate(directory, null, null, true);
  const file = join(directory, "camoufox-cli.json"), before = readFileSync(file);
  fail = true;
  const fallback = await loadOrCreate(directory, null, null, true);
  assert.deepEqual(fallback.config, {});
  assert.deepEqual(readFileSync(file), before);
});

for (const persistent of [false, true]) {
  test(`manager applies direct-exit GeoIP before launch, persistent=${persistent}`, async () => {
    const manager = new BrowserManager(persistent ? directory : null, null, true);
    await manager.launch();
    assert.equal(seen[0].proxy, null);
    assert.equal(launches[0].config.timezone, "Asia/Tokyo");
    assert.equal(launches[0].firefox_user_prefs["network.dns.disableIPv6"], true);
    assert.ok(checks.length > 0);
    await manager.close();
  });
  test(`lookup failure warns and continues browser launch, persistent=${persistent}`, async () => {
    fail = true;
    const manager = new BrowserManager(persistent ? directory : null, null, true);
    await manager.launch();
    assert.equal(launches.length, 1);
    assert.deepEqual(launches[0].config, {});
    assert.equal(manager.warnings[0].code, "GEOIP_UNAVAILABLE");
    await manager.close();
  });
}

test("driver exposes a nonblocking GeoIP warning for direct acquisition", async () => {
  fail = true;
  const driver = new CamoufoxDriver({ persistent: null, headless: true, proxy: null, geoip: true, locale: null, viewport: null, idleTimeout: 0 });
  await driver.launch();
  assert.equal(driver.warnings[0].code, "GEOIP_UNAVAILABLE");
  await driver.close();
});

test("malformed proxy errors do not expose credentials or launch a browser", async () => {
  const manager = new BrowserManager(null, "alice:private-secret@invalid", true);
  await assert.rejects(manager.launch(), error => !String(error).includes("private-secret"));
  assert.equal(launches.length, 0);
});
