import assert from "node:assert/strict";
import { test, mock, after } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, unlinkSync } from "node:fs";
import { join, dirname } from "node:path";
import { tmpdir } from "node:os";

const root = mkdtempSync(join(tmpdir(), "osbi-sdk-warning-unit-"));
const previous = process.env.OSBI_BROWSER_ASSETS;
process.env.OSBI_BROWSER_ASSETS = root;
after(() => {
  if (previous === undefined) delete process.env.OSBI_BROWSER_ASSETS; else process.env.OSBI_BROWSER_ASSETS = previous;
  rmSync(root, { recursive: true, force: true });
});
mock.module(new URL("../client/backend/browser/node_modules/camoufox/dist/index.js", import.meta.url).href, {
  namedExports: { NewBrowser: () => { throw new Error("Real browser launch is forbidden in this unit test"); } },
});
mock.module(new URL("../client/backend/browser/dist/driver/geolocation.js", import.meta.url).href, {
  namedExports: { locateExit: async () => { throw new Error("fixture lookup failure with private-secret"); } },
});
const { assetPaths, lock } = await import("../client/backend/browser/scripts/asset-paths.mjs");
const { exitGeo } = await import("../client/backend/browser/dist/driver/sdk.js");
const paths = assetPaths();
mkdirSync(dirname(paths.launcher), { recursive: true });
writeFileSync(paths.launcher, "fake launcher; never executed");
writeFileSync(join(root, "version.json"), JSON.stringify({ version: lock.version, release: lock.release }));
mkdirSync(paths.addon, { recursive: true });
writeFileSync(join(paths.addon, "manifest.json"), JSON.stringify({ version: lock.ubo.version }));
mkdirSync(paths.model);
writeFileSync(join(paths.model, ".pinned-model"), lock.model.sha256);
for (const name of [...lock.model.files, "values.dat"]) writeFileSync(join(paths.model, name), "fixture");

test("missing database returns a warning and a safe fallback", async () => {
  const warnings = [];
  assert.equal(await exitGeo(null, {}, w => warnings.push(w)), null);
  assert.equal(warnings[0].code, "GEOIP_UNAVAILABLE");
});

test("lookup errors become warnings without leaking provider details", async () => {
  writeFileSync(paths.geoip, "fixture database; lookup is mocked");
  const warnings = [];
  assert.equal(await exitGeo(null, {}, w => warnings.push(w)), null);
  assert.ok(!JSON.stringify(warnings).includes("private-secret"));
});

test("missing core browser assets remain a real installation error", async () => {
  unlinkSync(paths.launcher);
  await assert.rejects(exitGeo(null), { code: "ASSET_MISSING" });
});
