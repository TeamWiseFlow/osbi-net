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

test("settings saves independent report boards alongside existing acquisition settings", async () => {
  const elements = new Map();
  const document = {
    createElement: tag => new Element(tag), createTextNode: text => new Element("text", text),
    querySelector: id => { if (!elements.has(id)) elements.set(id, new Element("div")); return elements.get(id); },
    querySelectorAll: () => [], addEventListener() {},
  };
  const config = { serverUrl: "https://shared.example", serverCA: "", keyConfigured: false, focus: "Company",
    maxUrls: 20, browserConcurrency: 2, domainConcurrency: 1, timeoutSeconds: 60, headless: false,
    forbiddenDomains: [], sources: [], jev: { enabled: false, preSubmit: true, postQuery: true,
      baseUrl: "https://api.typesafe.ai/v1", model: "jev-latest", discardConfidence: .8 },
    reportProfiles: [{ id: "research", name: "Research", focus: "Research interests", topnews: [{
      id: "products", title: "Products", channel: "economy", query: "research", focus: "Product impact",
      topN: 3, selector: "evidence", minEdges: 4 }], weeklyreport: [] }] };
  let saved;
  const context = vm.createContext({ document, Node: Element, location: { pathname: "/settings" },
    window: { addEventListener() {} }, structuredClone, URL, setInterval() {}, setTimeout() {},
    fetch: async (url, options = {}) => {
      if (options.method === "PATCH") {
        saved = JSON.parse(options.body);
        return { ok: true, json: async () => ({ ...saved, keySuffix: "", jev: { ...saved.jev, keyConfigured: false } }) };
      }
      return { ok: true, json: async () => url === "/api/config" ? structuredClone(config) : {} };
    },
  });
  vm.runInContext(readFileSync(new URL("../client/backend/osbi_client/static/app.js", import.meta.url), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  const content = elements.get("#content");
  const button = text => find(content, node => node.tag === "button" && node.textContent === text);
  const field = (root, label) => find(root, node => node.tag === "div" && node.children[0]?.tag === "label" && node.children[0].textContent === label)?.children[1];
  await button("+ 添加方案").handlers.click();
  const newProfile = find(content, node => node.tag === "details" && node.children[0]?.textContent === "新方案");
  assert.ok(newProfile);
  field(newProfile, "方案关注点").value = "Sales department";
  await field(newProfile, "方案关注点").handlers.input();
  const addBoard = find(newProfile, node => node.tag === "button" && node.textContent === "+ 添加板块");
  await addBoard.handlers.click();
  const refreshed = find(content, node => node.tag === "details" && node.children[0]?.textContent === "新方案");
  field(refreshed, "板块标题").value = "Sales updates";
  await field(refreshed, "板块标题").handlers.input();
  field(refreshed, "查询关键词").value = "sales";
  await field(refreshed, "查询关键词").handlers.input();
  await button("保存设置").handlers.click();
  assert.equal(saved.sources.length, 0);
  assert.equal(saved.focus, "Company");
  assert.equal(saved.reportProfiles.length, 2);
  assert.equal(saved.reportProfiles[0].topnews[0].query, "research");
  assert.equal(saved.reportProfiles[1].focus, "Sales department");
  assert.equal(saved.reportProfiles[1].topnews[0].title, "Sales updates");
  assert.equal(saved.reportProfiles[1].topnews[0].query, "sales");
  assert.equal(saved.reportProfiles[1].weeklyreport.length, 0);
  assert.equal(saved.jev.enabled, false);
});
