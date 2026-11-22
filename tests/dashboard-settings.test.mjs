// Exercise settings interactions without launching a browser or an agent host.
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

class Element {
  constructor(tag, text = "") { this.tag = tag; this.text = text; this.children = []; this.handlers = {}; this.classList = { add() {}, remove() {}, toggle() {} }; }
  setAttribute(key, value) { this[key] = String(value); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this.text = ""; }
  addEventListener(name, callback) { this.handlers[name] = callback; }
  get textContent() { return this.text + this.children.map(node => node.textContent).join(""); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get value() { return this._value ?? (this.tag === "textarea" ? this.textContent : ""); }
  set value(value) { this._value = value; }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const result = find(child, predicate); if (result) return result; }
}

function baseConfig() {
  return { serverUrl: "https://shared.example", serverCA: "", keyConfigured: false, focus: "Company",
    maxUrls: 20, browserConcurrency: 2, domainConcurrency: { defaultConcurrency: 1, domains: {} },
    timeoutSeconds: 60, headless: false,
    forbiddenDomains: [], sources: [], jev: { enabled: false, preSubmit: true, postQuery: true,
      baseUrl: "https://api.typesafe.ai/v1", model: "jev-latest", discardConfidence: .8 },
    vision: { enabled: false, baseUrl: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen3-VL-32B-Instruct",
      fallbackModel: "Qwen/Qwen3-VL-30B-A3B-Instruct", extraModel: "zai-org/GLM-4.5V",
      concurrency: 3, timeoutSeconds: 240, keyConfigured: false },
    reportLlm: { enabled: true, baseUrl: "http://127.0.0.1:9997/v1", model: "xin_local",
      timeoutSeconds: 240, keyConfigured: false },
    reportProfiles: [{ id: "research", name: "Research", focus: "Research interests", topnews: [{
      id: "products", title: "Products", channel: "economy", query: "research", focus: "Product impact",
      topN: 3, selector: "evidence", minEdges: 4 }], weeklyreport: [] }] };
}

async function setup(config = baseConfig(), runsItems = []) {
  const elements = new Map();
  const saves = [];
  const document = {
    createElement: tag => new Element(tag), createTextNode: text => new Element("text", text),
    querySelector: id => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = vm.createContext({ document, Node: Element, location: { pathname: "/settings" },
    window: { addEventListener() {} }, structuredClone, URL, setInterval() {}, setTimeout() {},
    fetch: async (url, options = {}) => {
      if (options.method === "PATCH") {
        const saved = JSON.parse(options.body);
        saves.push(saved);
        return { ok: true, json: async () => ({ keyConfigured: false, keySuffix: "", ...saved,
          jev: { keyConfigured: false, ...(saved.jev || {}) },
          vision: { keyConfigured: false, ...(saved.vision || {}) },
          reportLlm: { keyConfigured: false, ...(saved.reportLlm || {}) } }) };
      }
      if (url === "/api/runs") return { ok: true, json: async () => ({ items: runsItems }) };
      return { ok: true, json: async () => url === "/api/config" ? structuredClone(config) : {} };
    },
  });
  vm.runInContext(readFileSync(new URL("../client/backend/osbi_client/static/app.js", import.meta.url), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  const content = elements.get("#content");
  const button = text => find(content, node => node.tag === "button" && node.textContent === text);
  const field = (root, label) => find(root, node => node.tag === "div" && node.children[0]?.tag === "label" && node.children[0].textContent === label)?.children[1];
  const section = title => find(content, node => node.tag === "section" && find(node, n => n.tag === "h2" && n.textContent === title));
  const saveIn = title => find(section(title), node => node.tag === "button" && node.textContent === "保存");
  return { content, saves, button, field, section, saveIn, context, elements };
}

test("per-section save keeps report boards and acquisition prefs independent", async () => {
  const { content, saves, button, field, saveIn } = await setup();
  await button("+ 添加方案").handlers.click();
  const newProfile = find(content, node => node.tag === "details" && node.children[0]?.textContent === "新方案");
  assert.ok(newProfile);
  field(newProfile, "方案关注点").value = "Sales department";
  await field(newProfile, "方案关注点").handlers.input();
  await find(newProfile, node => node.tag === "button" && node.textContent === "+ 添加板块").handlers.click();
  const refreshed = find(content, node => node.tag === "details" && node.children[0]?.textContent === "新方案");
  field(refreshed, "板块标题").value = "Sales updates";
  await field(refreshed, "板块标题").handlers.input();
  field(refreshed, "查询关键词").value = "sales";
  await field(refreshed, "查询关键词").handlers.input();
  await saveIn("06 · 本地报告方案").handlers.click();
  const reportSave = saves.at(-1);
  assert.equal(reportSave.reportProfiles.length, 2);
  assert.equal(reportSave.reportProfiles[1].focus, "Sales department");
  assert.equal(reportSave.reportProfiles[1].topnews[0].title, "Sales updates");
  assert.equal(reportSave.reportProfiles[1].topnews[0].query, "sales");
  assert.equal(reportSave.sources, undefined);  // section-local: no sources key

  field(content, "页面缓存时间 / 秒").value = 3600;
  field(content, "域名缓存时间覆盖").value = '{"example.org": 120}';
  await saveIn("03 · 本地采集偏好").handlers.click();
  const prefsSave = saves.at(-1);
  assert.equal(prefsSave.captureCache.defaultTtlSeconds, 3600);
  assert.equal(prefsSave.captureCache.domainTtlSeconds["example.org"], 120);
  assert.equal(prefsSave.reportProfiles, undefined);  // section-local: no reports key
});

test("jev master switch gates dependent fields and persists enabled", async () => {
  const { content, saves, field, section, saveIn } = await setup();
  const jevSection = section("04 · 可选 Jev 决策");
  const toggle = find(jevSection, node => node.className === "jev-switch").children[0];
  const pre = field(jevSection, "提交 server 前判断相关性");
  assert.equal(toggle.checked, false);
  assert.equal(pre.disabled, true);  // off -> dependents disabled
  toggle.checked = true;
  await toggle.handlers.change();
  assert.equal(pre.disabled, false);  // on -> dependents enabled
  await saveIn("04 · 可选 Jev 决策").handlers.click();
  assert.equal(saves.at(-1).jev.enabled, true);
  assert.equal(saves.at(-1).sources, undefined);
});

test("mp source editing collects accounts and fixes the sentinel url", async () => {
  const { content, saves, button, saveIn } = await setup();
  await button("+ 添加信源").handlers.click();
  const kind = find(content, node => node.tag === "select" && find(node, n => n.tag === "option" && n.value === "mp"));
  kind.value = "mp";
  await kind.handlers.change();
  const accounts = find(content, node => node.tag === "textarea" && node["aria-label"] === "公众号白名单");
  assert.ok(accounts);
  accounts.value = "第一财经\n经济日报\n";
  await accounts.handlers.input();
  // steps editor hidden for mp
  assert.equal(find(content, node => node.tag === "textarea" && node["aria-label"] === "操作流程 JSON"), undefined);
  await saveIn("02 · 采集信源").handlers.click();
  const source = saves.at(-1).sources[0];
  assert.equal(source.kind, "mp");
  assert.deepEqual(source.accounts, ["第一财经", "经济日报"]);
  assert.equal(source.url, "https://mp.weixin.qq.com/");
  assert.deepEqual(source.steps, []);
});

test("switching mp back to web clears accounts", async () => {
  const { content, saves, button, saveIn } = await setup();
  await button("+ 添加信源").handlers.click();
  let kind = find(content, node => node.tag === "select" && find(node, n => n.tag === "option" && n.value === "mp"));
  kind.value = "mp";
  await kind.handlers.change();
  const accounts = find(content, node => node.tag === "textarea" && node["aria-label"] === "公众号白名单");
  accounts.value = "第一财经";
  await accounts.handlers.input();
  kind = find(content, node => node.tag === "select" && find(node, n => n.tag === "option" && n.value === "mp"));
  kind.value = "web";
  await kind.handlers.change();
  await saveIn("02 · 采集信源").handlers.click();
  const source = saves.at(-1).sources[0];
  assert.equal(source.kind, "web");
  assert.deepEqual(source.accounts, []);
});

test("pending badge counts waiting and interrupted runs", async () => {
  const runs = [{ id: "run_a", state: "waiting_for_user" }, { id: "run_b", state: "succeeded" }, { id: "run_c", state: "interrupted" }];
  const { context, elements } = await setup(baseConfig(), runs);
  await context.updatePendingBadge();
  const badge = elements.get("#unread");
  assert.equal(badge.textContent, "2");
  assert.equal(badge.hidden, false);
});

test("pending badge hidden when nothing awaits the user", async () => {
  const { context, elements } = await setup(baseConfig(), [{ id: "run_a", state: "succeeded" }]);
  await context.updatePendingBadge();
  assert.equal(elements.get("#unread").hidden, true);
});

test("per-domain concurrency editor saves the production policy shape", async () => {
  const config = baseConfig();
  config.domainConcurrency = { defaultConcurrency: 1, domains: { "mp.weixin.qq.com": 2 } };
  const { content, saves, field, saveIn } = await setup(config);
  assert.equal(Number(field(content, "默认同域名并发").value), 1);
  assert.deepEqual(JSON.parse(field(content, "按域名并发覆盖").value), { "mp.weixin.qq.com": 2 });
  field(content, "默认同域名并发").value = 2;
  field(content, "按域名并发覆盖").value = '{"mp.weixin.qq.com": 2, "gov.cn": 3}';
  await saveIn("03 · 本地采集偏好").handlers.click();
  assert.deepEqual(saves.at(-1).domainConcurrency, { defaultConcurrency: 2, domains: { "mp.weixin.qq.com": 2, "gov.cn": 3 } });
});

test("legacy int domainConcurrency renders as the default limit", async () => {
  const config = baseConfig();
  config.domainConcurrency = 2;
  const { content, saves, field, saveIn } = await setup(config);
  assert.equal(Number(field(content, "默认同域名并发").value), 2);
  assert.deepEqual(JSON.parse(field(content, "按域名并发覆盖").value), {});
  await saveIn("03 · 本地采集偏好").handlers.click();
  assert.deepEqual(saves.at(-1).domainConcurrency, { defaultConcurrency: 2, domains: {} });
});

test("forbidden domains field explains substring fragment matching", async () => {
  const { content, field } = await setup();
  const node = find(content, n => n.tag === "div" && n.children[0]?.tag === "label" && n.children[0].textContent === "排除域名");
  assert.ok(node);
  const hint = node.children.find(child => child.tag === "small");
  assert.ok(hint && hint.textContent.includes("子串"));
  assert.ok(hint.textContent.includes("路径"));
  field(content, "排除域名").value = "www.gov.cn/home, english.shanghai.gov.cn/";
});

test("vision master switch gates dependents and section save preserves key", async () => {
  const { saves, field, section, saveIn } = await setup();
  const vSection = section("05 · 图片识别（VL）");
  const toggle = find(vSection, node => node.className === "jev-switch").children[0];
  const url = field(vSection, "服务地址");
  assert.equal(toggle.checked, false);
  assert.equal(url.disabled, true);  // off -> dependents disabled
  toggle.checked = true;
  await toggle.handlers.change();
  assert.equal(url.disabled, false);
  field(vSection, "主模型").value = "Qwen/Qwen3-VL-32B-Instruct";
  await saveIn("05 · 图片识别（VL）").handlers.click();
  const saved = saves.at(-1);
  assert.equal(saved.vision.enabled, true);
  assert.equal(saved.vision.model, "Qwen/Qwen3-VL-32B-Instruct");
  assert.equal(saved.vision.apiKey, undefined);  // blank preserves stored key
  assert.equal(saved.jev, undefined);  // section-local save
});
