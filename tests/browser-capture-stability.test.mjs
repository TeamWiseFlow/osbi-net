import assert from "node:assert/strict";
import { test } from "node:test";

const { execute } = await import("../client/backend/browser/dist/actions/commands.js");

function fakePage({ gotoError = null, states = [{ text: "正文", url: "https://example.org/" }] } = {}) {
  let index = 0;
  const calls = { evaluate: 0, ariaSnapshot: 0, goto: 0 };
  return {
    calls,
    async goto() { calls.goto++; if (gotoError) throw gotoError; },
    url: () => "https://example.org/",
    async title() { return "示例页面"; },
    async evaluate() {
      calls.evaluate++;
      const state = states[Math.min(index, states.length - 1)];
      index++;
      return state;
    },
    async waitForTimeout() {},
    locator() { return { ariaSnapshot: async () => { calls.ariaSnapshot++; return "- heading 示例"; } }; },
  };
}

function fakeManager(page, { captureImpl } = {}) {
  const refsCalls = { capture: 0 };
  return {
    refsCalls,
    isRunning: true,
    async launch() {},
    getPage: () => page,
    pushHistory() {},
    tabId: () => "tab-1",
    refs: {
      snapshotId: "snap-1",
      async capture() {
        refsCalls.capture++;
        if (captureImpl) return captureImpl(refsCalls.capture);
        return "- link 示例 [ref=e1]";
      },
    },
  };
}

test("open tolerates a navigation timeout like production capture.mjs", async () => {
  const page = fakePage({ gotoError: new Error("page.goto: Timeout 30000ms exceeded.") });
  const response = await execute(fakeManager(page), { action: "open", params: { url: "https://example.org/" } });
  assert.equal(response.success, true);
  assert.equal(response.data.navigationTimedOut, true);
});

test("open still aborts on non-timeout navigation errors", async () => {
  const page = fakePage({ gotoError: new Error("net::ERR_CONNECTION_RESET") });
  const response = await execute(fakeManager(page), { action: "open", params: { url: "https://example.org/" } });
  assert.equal(response.success, false);
});

test("snapshot waits for two consecutive stable content reads before binding refs", async () => {
  const page = fakePage({ states: [{ text: "加载一", url: "https://example.org/" },
    { text: "加载二", url: "https://example.org/" },
    { text: "稳定正文", url: "https://example.org/" },
    { text: "稳定正文", url: "https://example.org/" },
    { text: "稳定正文", url: "https://example.org/" }] });
  const manager = fakeManager(page);
  const response = await execute(manager, { action: "snapshot", params: {} });
  assert.equal(response.success, true);
  assert.equal(manager.refsCalls.capture, 1);
  // change, change, stable-read(0), stable-read(1), stable-read(2) -> 5 reads.
  assert.equal(page.calls.evaluate, 5);
  assert.match(response.data.snapshot, /\[ref=e1\]/);
});

test("snapshot re-stabilizes and retries once on STALE_REF", async () => {
  const page = fakePage();
  const manager = fakeManager(page, {
    captureImpl: attempt => {
      if (attempt === 1) throw Object.assign(new Error("Page changed while capturing refs"), { code: "STALE_REF" });
      return "- link 示例 [ref=e2]";
    },
  });
  const response = await execute(manager, { action: "snapshot", params: {} });
  assert.equal(response.success, true);
  assert.equal(manager.refsCalls.capture, 2);
  assert.match(response.data.snapshot, /\[ref=e2\]/);
});

test("snapshot surfaces STALE_REF when the retry also fails", async () => {
  const page = fakePage();
  const manager = fakeManager(page, {
    captureImpl: () => { throw Object.assign(new Error("Page changed"), { code: "STALE_REF" }); },
  });
  const response = await execute(manager, { action: "snapshot", params: {} });
  assert.equal(response.success, false);
  assert.equal(response.code, "STALE_REF");
  assert.equal(manager.refsCalls.capture, 2);
});

test("snapshot refs:false takes a plain aria snapshot without binding refs", async () => {
  const page = fakePage();
  const manager = fakeManager(page);
  const response = await execute(manager, { action: "snapshot", params: { refs: false } });
  assert.equal(response.success, true);
  assert.equal(manager.refsCalls.capture, 0);
  assert.equal(page.calls.ariaSnapshot, 1);
  assert.equal(response.data.snapshot, "- heading 示例");
  assert.equal(response.data.snapshotId, undefined);
});

test("snapshot keeps waiting when navigation destroys the JS context", async () => {
  let reads = 0;
  const page = fakePage();
  page.evaluate = async () => {
    reads++;
    if (reads <= 2) throw new Error("Execution context was destroyed");
    return { text: "稳定正文", url: "https://example.org/" };
  };
  const manager = fakeManager(page);
  const response = await execute(manager, { action: "snapshot", params: {} });
  assert.equal(response.success, true);
  // 2 destroyed + 3 stable reads (stable counter needs two matches).
  assert.equal(reads, 5);
});
