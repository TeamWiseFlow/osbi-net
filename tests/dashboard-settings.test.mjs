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
  get selectedOptions() { return this.children.filter(option => option.selected); }
}

function find(root, predicate) {
  if (predicate(root)) return root;
  for (const child of root.children) { const result = find(child, predicate); if (result) return result; }
}

function baseConfig() {
  const section = { id: "updates", title: "Research updates", channels: ["economy"],
    query: { text: "research", entityUris: [], contentTypes: [], territoryPreference: ["上海市"],
             territoryMode: "prefer", sourceScope: "all" },
    localRules: { keywords: [], excludeKeywords: [], leaderBoost: true, localityPenalty: true, jev: "inherit" } };
  const board = { section: "updates", selector: "candidates", topN: 3, minEdges: 4,
                  purpose: "Product impact", prompt: "" };
  return { serverUrl: "https://shared.example", serverCA: "", keyConfigured: false,
    maxUrls: 20, browserConcurrency: 2, domainConcurrency: { defaultConcurrency: 1, domains: {} },
    timeoutSeconds: 60, headless: false, forbiddenDomains: [], sources: [],
    jev: { enabled: false, postQuery: true, baseUrl: "https://api.typesafe.ai/v1",
           model: "jev-latest", discardConfidence: .8 },
    vision: { enabled: false, baseUrl: "https://api.siliconflow.cn/v1", model: "Qwen/Qwen3-VL-32B-Instruct",
      fallbackModel: "Qwen/Qwen3-VL-30B-A3B-Instruct", extraModel: "zai-org/GLM-4.5V",
      concurrency: 3, timeoutSeconds: 240, keyConfigured: false },
    reportLlm: { enabled: true, baseUrl: "http://127.0.0.1:9997/v1", model: "xin_local",
      timeoutSeconds: 240, keyConfigured: false },
    nodeRules: { nodeName: "上海市静安区临汾路街道", city: "上海市", district: "静安区",
                 street: "临汾路街道", leaderBoost: true, localityPenalty: true,
                 genericEntityNames: [], localityScore: {}, leaderOverrides: {}, relationHints: [], topnewsShout: '[太阳] 今日份', topnewsSuffix: '尾注', weeklyIntro: 'Weekly report {date}\n\n{digest}' },
    reportProfiles: [{ id: "research", name: "Research", sections: [section], targets: [],
      topnews: { enabled: true, schedule: { time: "08:40", weekday: null, timezone: "Asia/Shanghai" },
                 boards: [board], targetIds: [], dryRun: true },
      weeklyreport: { enabled: true, schedule: { time: "07:50", weekday: "monday", timezone: "Asia/Shanghai" },
                      boards: [{ ...board, selector: "action", topN: 2 }], targetIds: [], dryRun: true } }] };
}

async function setup(config = baseConfig(), runsItems = [], page = "/settings", extra = {}) {
  const elements = new Map();
  const saves = [];
  const posts = [];
  const document = {
    createElement: tag => new Element(tag), createTextNode: text => new Element("text", text),
    querySelector: id => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); },
    querySelectorAll: () => [], addEventListener() {},
  };
  const context = vm.createContext({ document, Node: Element, location: { pathname: page },
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
      if (options.method === "POST") {
        posts.push({ url, body: options.body ? JSON.parse(options.body) : null });
        return { ok: true, json: async () => ({}) };
      }
      if (url === "/api/runs") return { ok: true, json: async () => ({ items: runsItems }) };
      if (url === "/api/reports") return { ok: true, json: async () => ({ items: extra.reports || [] }) };
      if (url.startsWith("/api/reports/") && url.includes("/logs")) {
        return { ok: true, json: async () => ({ items: extra.logs || [] }) };
      }
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
  return { content, saves, posts, button, field, section, saveIn, context, elements };
}

test("settings keeps source and acquisition saves independent", async () => {
  const { content, saves, button, field, saveIn } = await setup();
  await button("+ 添加信源").handlers.click();
  field(content, "信源 ID").value = "source-1";
  await field(content, "信源 ID").handlers.input();
  field(content, "信源地址").value = "https://example.org/news";
  await field(content, "信源地址").handlers.input();
  await saveIn("02 · 采集信源").handlers.click();
  const sourceSave = saves.at(-1);
  assert.equal(sourceSave.sources[0].id, "source-1");
  assert.deepEqual(sourceSave.sources[0].channels, []);
  assert.equal(sourceSave.maxUrls, undefined);

  field(content, "页面缓存时间 / 秒").value = 3600;
  field(content, "域名缓存时间覆盖").value = '{"example.org": 120}';
  await saveIn("03 · 本地采集偏好").handlers.click();
  const prefsSave = saves.at(-1);
  assert.equal(prefsSave.captureCache.defaultTtlSeconds, 3600);
  assert.equal(prefsSave.captureCache.domainTtlSeconds["example.org"], 120);
  assert.equal(prefsSave.sources, undefined);
  assert.equal(find(content, node => node.tag === "section" && find(node, n => n.tag === "h2" && n.textContent === "本地报告方案")), undefined);
});

test("jev master switch gates dependent fields and persists enabled", async () => {
  const { content, saves, field, section, saveIn } = await setup(baseConfig(), [], "/query");
  const jevSection = section("02 · Jev 后置决策");
  const toggle = find(jevSection, node => node.className === "jev-switch").children[0];
  const post = field(jevSection, "查询后筛选证据");
  assert.equal(toggle.checked, false);
  assert.equal(post.disabled, true);  // off -> dependents disabled
  toggle.checked = true;
  await toggle.handlers.change();
  assert.equal(post.disabled, false);  // on -> dependents enabled
  await saveIn("02 · Jev 后置决策").handlers.click();
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
  const vSection = section("04 · 图片识别（VL）");
  const toggle = find(vSection, node => node.className === "jev-switch").children[0];
  const url = field(vSection, "服务地址");
  assert.equal(toggle.checked, false);
  assert.equal(url.disabled, true);  // off -> dependents disabled
  toggle.checked = true;
  await toggle.handlers.change();
  assert.equal(url.disabled, false);
  field(vSection, "主模型").value = "Qwen/Qwen3-VL-32B-Instruct";
  await saveIn("04 · 图片识别（VL）").handlers.click();
  const saved = saves.at(-1);
  assert.equal(saved.vision.enabled, true);
  assert.equal(saved.vision.model, "Qwen/Qwen3-VL-32B-Instruct");
  assert.equal(saved.vision.apiKey, undefined);  // blank preserves stored key
  assert.equal(saved.jev, undefined);  // section-local save
});

test("tasks page edits sections, task overlays and targets independently", async () => {
  const { content, saves, button, field, section } = await setup(baseConfig(), [], "/tasks");
  const sectionCard = section("Sections · 共享板块模板");
  assert.ok(sectionCard);
  field(sectionCard, "板块标题").value = "Research insights";
  await field(sectionCard, "板块标题").handlers.input();
  await button("保存 Sections").handlers.click();
  assert.equal(saves.at(-1).reportProfiles[0].sections[0].title, "Research insights");
  assert.equal(saves.at(-1).sources, undefined);

  const topnews = section("TopNews · 每日简报");
  assert.equal(field(topnews, "选材方式"), undefined);
  field(topnews, "条数").value = 2;
  await field(topnews, "条数").handlers.input();
  await find(topnews, node => node.tag === "button" && node.textContent === "保存").handlers.click();
  assert.equal(saves.at(-1).reportProfiles[0].topnews.boards[0].topN, 2);
});

test("reports page owns delivery target management", async () => {
  const { saves, button, section } = await setup(baseConfig(), [], "/reports");
  const targets = section("发送目标");
  assert.ok(targets);
  await button("+ 添加发送目标").handlers.click();
  await button("保存目标").handlers.click();
  const target = saves.at(-1).reportProfiles[0].targets[0];
  assert.equal(target.type, "mimicwx");
  assert.equal(target.label, "个微容器");
  assert.equal(target.enabled, false);
});

test("tasks page exposes configurable output copy and task prompts", async () => {
  const { content, saves, button, field, section } = await setup(baseConfig(), [], "/tasks");
  const output = section("输出文案");
  assert.ok(output);
  field(output, "TopNews 导语").value = "[sun] Daily";
  await field(output, "TopNews 导语").handlers.input();
  field(output, "TopNews 结尾语").value = "Thanks";
  await field(output, "TopNews 结尾语").handlers.input();
  await button("保存输出文案").handlers.click();
  assert.match(saves.at(-1).nodeRules.weeklyIntro, /Weekly report/);
  assert.equal(saves.at(-1).nodeRules.topnewsShout, "[sun] Daily");
  assert.equal(saves.at(-1).nodeRules.topnewsSuffix, "Thanks");

  const weekly = section("Weeklyreport · 每周热点");
  const prompt = field(weekly, "综合解读/导读 Prompt");
  prompt.value = "CUSTOM ANALYST";
  await prompt.handlers.input();
  await find(weekly, node => node.tag === "button" && node.textContent === "保存").handlers.click();
  assert.equal(saves.at(-1).reportProfiles[0].weeklyreport.prompt, "CUSTOM ANALYST");
});

test("weekly task exposes the docx file switch and manual runs reuse defaults", async () => {
  const { posts, saves, field, section } = await setup(baseConfig(), [], "/tasks");
  const weekly = section("Weeklyreport · 每周热点");
  const fileSwitch = field(weekly, "文档文件");
  assert.ok(fileSwitch);
  const box = fileSwitch.children[0];
  assert.equal(box.checked, true);
  box.checked = false;
  await box.handlers.change();
  await find(weekly, node => node.tag === "button" && node.textContent === "保存").handlers.click();
  assert.equal(saves.at(-1).reportProfiles[0].weeklyreport.sendFile, false);
  await find(weekly, node => node.tag === "button" && node.textContent === "手动运行").handlers.click();
  const post = posts.at(-1);
  assert.equal(post.url, "/api/weekly/run");
  assert.equal(post.body.fromDate, undefined);
  assert.equal(post.body.toDate, undefined);
});

test("recent docx report shows its local path instead of calling the file viewer", async () => {
  const reports = [{ kind: "weekly", name: "weekly.docx", path: "/tmp/reports/weekly.docx",
                     modified: "2026-11-25T10:50:00" }];
  const { content, elements } = await setup(baseConfig(), [], "/reports", { reports });
  await new Promise(resolve => setImmediate(resolve));
  const reportButton = find(content, node => node.tag === "button" && node.textContent.includes("weekly.docx"));
  await reportButton.handlers.click();
  const pathView = find(content, node => node.tag === "pre" && node.textContent.includes("/tmp/reports/weekly.docx"));
  assert.ok(pathView);
  assert.equal(pathView.hidden, false);
});
