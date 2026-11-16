import assert from "node:assert/strict";
import { mock, test, beforeEach, after } from "node:test";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { isIP } from "node:net";

const directory = mkdtempSync(join(tmpdir(), "osbi-geolocation-unit-"));
const database = join(directory, "fixture.mmdb");
writeFileSync(database, "isolated database fixture");
after(() => rmSync(directory, { recursive: true, force: true }));
let ip, record, lookupError, calls;
mock.module(new URL("../client/backend/browser/node_modules/camoufox/dist/ip.js", import.meta.url).href, {
  namedExports: {
    publicIP: async proxy => { calls.push(proxy); if (lookupError) throw lookupError; return ip; },
    validIPv4: value => isIP(value) === 4,
    validIPv6: value => isIP(value) === 6,
  },
});
mock.module(new URL("../client/backend/browser/node_modules/maxmind/lib/index.js", import.meta.url).href, {
  namedExports: { Reader: class { get(value) { assert.equal(value, ip); return record; } } },
});
const { SELECTOR, Locale } = await import("../client/backend/browser/node_modules/camoufox/dist/locales.js");
mock.method(SELECTOR, "fromRegion", async region => new Locale(region === "JP" ? "ja" : "en", region, "Latn"));
const { locateExit } = await import("../client/backend/browser/dist/driver/geolocation.js");
beforeEach(() => {
  ip = "203.0.113.8"; calls = []; lookupError = null;
  record = { country: { iso_code: "JP" }, location: { time_zone: "Asia/Tokyo", longitude: 139.7, latitude: 35.7 } };
});

test("direct launch resolves its public exit without inventing a proxy", async () => {
  const config = await locateExit(database, null);
  assert.deepEqual(calls, [undefined]);
  assert.equal(config.timezone, "Asia/Tokyo");
  assert.equal(config["locale:language"], "ja");
  assert.equal(config["geolocation:latitude"], 35.7);
  assert.equal(config["webrtc:ipv4"], ip);
});

test("proxy and credentials are used by the exit probe", async () => {
  await locateExit(database, "http://alice:p%40ss@proxy.example:8080");
  const route = new URL(calls[0]);
  assert.equal(route.hostname, "proxy.example");
  assert.equal(decodeURIComponent(route.password), "p@ss");
  assert.equal(route.username, "alice");
});

test("same-country launches keep language while refreshing IP and geography", async () => {
  const previous = { "locale:region": "JP", "locale:language": "en", "locale:script": "Latn", "webrtc:ipv6": "2001:db8::1" };
  const config = await locateExit(database, null, previous);
  assert.equal(config["locale:language"], "en");
  assert.equal(config["webrtc:ipv4"], ip);
  assert.equal(config["webrtc:ipv6"], undefined);
  assert.equal(previous["webrtc:ipv6"], "2001:db8::1");
});

test("a different country replaces the previous locale", async () => {
  const config = await locateExit(database, null, { "locale:region": "US", "locale:language": "en" });
  assert.equal(config["locale:language"], "ja");
  assert.equal(config["locale:region"], "JP");
});

test("IPv6 exit replaces the old IPv4 hint", async () => {
  ip = "2001:db8::8";
  const config = await locateExit(database, null, { "webrtc:ipv4": "203.0.113.1" });
  assert.equal(config["webrtc:ipv6"], ip);
  assert.equal(config["webrtc:ipv4"], undefined);
});

test("a failed lookup stops without exposing proxy credentials", async () => {
  lookupError = new Error("Failed http://alice:private-secret@proxy.example:8080");
  await assert.rejects(locateExit(database, "http://alice:private-secret@proxy.example:8080"), error =>
    error.code === "GEOIP_LOOKUP_FAILED" && !String(error).includes("private-secret"));
});

for (const invalid of [null, {}, { country: { iso_code: "JP" }, location: { latitude: 999, longitude: 0, time_zone: "Asia/Tokyo" } },
  { country: { iso_code: "JP" }, location: { latitude: 35, longitude: 139, time_zone: "Invalid/Timezone" } }]) {
  test(`incomplete or invalid location blocks startup: ${JSON.stringify(invalid)}`, async () => {
    record = invalid;
    await assert.rejects(locateExit(database, null), { code: "GEOIP_LOOKUP_FAILED" });
  });
}

test("invalid exit IP and missing database do not fall back to an unaligned identity", async () => {
  ip = "invalid";
  await assert.rejects(locateExit(database, null), { code: "GEOIP_LOOKUP_FAILED" });
  ip = "203.0.113.8";
  await assert.rejects(locateExit(join(directory, "absent.mmdb"), null), { code: "GEOIP_LOOKUP_FAILED" });
});

test("an unverified database is rejected before any network probe", async () => {
  await assert.rejects(locateExit(database, null, {}, "0".repeat(64)), { code: "GEOIP_LOOKUP_FAILED" });
  assert.deepEqual(calls, []);
});

test("a stalled IP probe is bounded so warning fallback can proceed", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  lookupError = null;
  ip = new Promise(() => {});
  const pending = locateExit(database, null);
  t.mock.timers.tick(10001);
  await assert.rejects(pending, { code: "GEOIP_LOOKUP_FAILED" });
  t.mock.timers.reset();
});
