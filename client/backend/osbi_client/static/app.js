/* Local dashboard. Remote data always crosses the authenticated backend facade. */
"use strict";
const $ = (s, root = document) => root.querySelector(s);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  if (["input", "textarea", "select"].includes(tag)) {
    // Avoid password managers / secure-input keyboards treating ordinary
    // configuration fields as credential prompts.
    if (!("autocomplete" in attrs)) n.setAttribute("autocomplete", "off");
    n.setAttribute("data-lpignore", "true");
    n.setAttribute("data-1p-ignore", "true");
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) n.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return n;
};
const state = { page: "", config: null, chart: null, data: null, events: new Map(), cursor: 0, stream: null, channels: [] };
let routeVersion = 0;
async function api(path, options = {}) {
  const r = await fetch("/api/" + path, { ...options, headers: { "Content-Type": "application/json", "X-OSBI-Local": "1", ...options.headers } });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || data.detail || "请求失败");
  return data;
}
function toast(text) { const n = $("#toast"); n.textContent = text; n.hidden = false; setTimeout(() => n.hidden = true, 3500); }
function banner(text) {
  const n = $("#banner"); n.replaceChildren(); n.hidden = !text;
  if (text) n.append(document.createTextNode(text), el("a", { href: "/settings" }, "检查设置 →"));
}
function empty(title, text) { return el("div", { class: "empty" }, el("div", { class: "empty-symbol" }, "◈"), el("strong", {}, title), el("p", {}, text)); }
function heading(title, subtitle, actions = []) {
  return el("div", { class: "page-heading" }, el("div", {}, el("div", { class: "eyebrow" }, "OPEN-SOURCE BUSINESS INTELLIGENCE"), el("h1", {}, title), el("p", {}, subtitle)), el("div", { class: "row" }, actions));
}
function stats(items) {
  return el("div", { class: "stats" }, items.map(([symbol, label, value, word]) => el("div", { class: "stat" }, el("span", { class: "stat-icon" }, symbol), el("div", {}, el("div", { class: "stat-label" }, label), el("div", { class: "stat-value" + (word ? " word" : "") }, value)))));
}
function titlebar(title, controls = []) { return el("div", { class: "card-title" }, el("h2", {}, title), el("div", { class: "row" }, controls)); }
const names = { pending: "等待采集", running: "采集中", waiting_for_user: "等待人工操作", waiting_server: "等待共享服务", interrupted: "任务中断", succeeded: "已完成", failed: "处理失败", cancelled: "已取消", graph_pending: "等待图谱提取", retry: "稍后重试" };
function time(value) { return value ? new Date(value * 1000).toLocaleString("zh-CN", { hour12: false }) : "尚无已完成数据"; }
function remote(path, options) { return api("server/" + path, options); }
function safeUrl(url) { try { const u = new URL(url); return ["http:", "https:"].includes(u.protocol) ? u.href : "#"; } catch { return "#"; } }

async function graphPage(version) {
  const root = $("#content");
  const select = el("select", { "aria-label": "频道" });
  const search = el("input", { placeholder: "搜索实体…", "aria-label": "搜索实体" });
  const refresh = el("button", { onclick: () => load(), class: "small" }, "↻ 刷新");
  const metric = el("div");
  const chartNode = el("div", { id: "chart" });
  const stage = el("div", { class: "graph-stage" }, chartNode);
  const foot = el("div", { class: "graph-foot" }, el("span", {}, "拖动平移 · 滚轮缩放 · 点击查看证据"), el("span", { id: "graph-freshness" }, "等待连接"));
  const details = el("aside", { class: "card detail" }, el("div", { class: "detail-label" }, "实体与证据"), empty("选择一个实体", "点击图谱中的节点，查看它的关系和原文出处。"));
  const target = el("input", { placeholder: "目标实体名称", "aria-label": "路径目标实体" });
  let channelId = "", selectedNode = null;
  const pathButton = el("button", { class: "small", onclick: async () => {
    if (!selectedNode || !target.value.trim()) return toast("先选择起点，再填写目标实体");
    try {
      const candidates = state.data.nodes.filter(n => n.name === target.value.trim());
      if (candidates.length !== 1) return toast("目标实体不存在或有同名候选，请先确认名称");
      const data = await remote(`channels/${channelId}/query`, { method: "POST", body: JSON.stringify({ source: selectedNode.id, target: candidates[0].id, depth: 4 }) });
      if (!data.paths?.length) return toast("当前范围内未找到明确关系路径");
      paint({ ...state.data, ...data });
    } catch (e) { banner(e.message); }
  } }, "查找路径");
  root.replaceChildren(heading("情报图谱", "从实体出发，发现关联，让每条线索都有出处。", [refresh]), metric,
    el("div", { class: "graph-layout" }, el("section", { class: "card graph-card" }, titlebar("共享知识网络", [el("div", { class: "graph-tools" }, select, search)]), stage,
      el("div", { class: "card-title" }, el("span", { class: "hint" }, "选择节点作为路径起点"), el("div", { class: "graph-tools" }, target, pathButton)), foot), details));
  function showNode(n) {
    selectedNode = n;
    details.replaceChildren(el("div", { class: "detail-label" }, "实体详情"), el("h3", {}, n.name), el("span", { class: "tag" }, n.label),
      el("p", {}, n.identityStatus === "unresolved" ? "人物身份尚未跨来源确认，请结合原文判断。" : "仅展示原文支持的实体与关系。"), el("hr"), el("div", { class: "detail-label" }, "原文证据"));
    for (const id of n.blocks || []) {
      const b = state.data.blocks[id]; if (!b) continue;
      const section = el("div", { class: "source" });
      for (const src of b.sources || []) {
        section.append(el("a", { href: safeUrl(src.meta.finalUrl || src.meta.requestedUrl), target: "_blank", rel: "noopener noreferrer" }, src.title || "查看来源 ↗"), el("small", {}, [src.meta.publisher, src.meta.publishedAt].filter(Boolean).join(" · ")));
      }
      section.append(el("blockquote", {}, b.text)); details.append(section);
    }
  }
  function paint(data) {
    if (!state.chart) state.chart = echarts.init(chartNode, null, { renderer: "canvas" });
    stage.querySelector(".empty")?.remove();
    if (!data.nodes.length) stage.append(empty("这个频道还没有图谱数据", "在设置中添加信源并运行采集；完成提取后，实体和关系会显示在这里。"));
    state.chart.setOption({ tooltip: { renderMode: "richText", formatter: p => p.name }, animationDuration: 400,
      series: [{ type: "graph", layout: "force", roam: true, draggable: true, force: { repulsion: 300, edgeLength: 110, gravity: .05 },
        data: data.nodes.map(n => ({ ...n, symbolSize: 22 + Math.min(n.blocks.length * 3, 18), itemStyle: { color: n.label.includes("人物") ? "#e4af6b" : n.label.includes("政策") ? "#7da7ca" : "#8c80d1" }, label: { show: true, position: "bottom", color: "#737c94", fontSize: 11 } })),
        links: data.edges, edgeSymbol: ["none", "arrow"], edgeSymbolSize: 5, lineStyle: { color: "#c8c6de", width: 1.1, curveness: .08 },
        edgeLabel: { show: true, formatter: p => p.data.label, fontSize: 9, color: "#a0a5b7" }, emphasis: { focus: "adjacency", lineStyle: { width: 2 } } }] });
    state.chart.off("click"); state.chart.on("click", p => { if (p.dataType === "node") showNode(p.data); });
  }
  async function load() {
    refresh.disabled = true;
    try {
      const result = await remote("channels"); if (version !== routeVersion) return;
      const old = select.value;
      select.replaceChildren(...result.items.map(c => el("option", { value: c.id }, c.title)));
      select.value = result.items.some(c => c.id === old) ? old : result.items[0]?.id || "";
      channelId = select.value; state.channels = result.items;
      if (!channelId) throw new Error("当前客户 Key 没有可用频道");
      const data = await remote(`channels/${channelId}/graph`); if (version !== routeVersion) return;
      state.data = data; banner("");
      const pending = Object.entries(data.status.states || {}).filter(([k]) => !["succeeded", "failed"].includes(k)).reduce((s, [,v]) => s + v, 0);
      const mode = data.status.processingMode === "batch" ? "时段批处理" : "持续后台提取";
      metric.replaceChildren(stats([["◈", "实体", data.nodes.length], ["↗", "关系", data.edges.length], ["◷", "待处理任务", pending], ["≋", "处理模式", mode, true]]));
      $("#graph-freshness").textContent = data.truncated ? "当前为部分图谱" : "最近完成 · " + time(data.status.completedWatermark);
      details.replaceChildren(el("div", { class: "detail-label" }, "频道信息"), el("h3", {}, select.selectedOptions[0]?.textContent || channelId),
        el("p", {}, data.status.graphAllowed ? "后台正在持续接收与处理材料。" : `等待处理时段 ${data.status.batchWindow}（${data.status.timezone}）`),
        el("p", {}, "共享图谱 · 同一 schema 的频道展示同一张图；上方实体/关系数是图谱总数，不是单频道计数。"),
        el("p", {}, "Schema · " + data.schemaId.slice(0, 12)), el("hr"), el("div", { class: "detail-label" }, "选择实体查看证据"));
      paint(data);
    } catch (e) {
      if (version !== routeVersion) return;
      banner(e.message); metric.replaceChildren(stats([["◈", "实体", "—"], ["↗", "关系", "—"], ["◷", "待处理任务", "—"], ["≋", "服务状态", "未连接", true]]));
      if (!state.data) { stage.querySelector(".empty")?.remove(); stage.append(empty("连接你的情报网络", "前往设置，填写共享服务地址和客户 Key，即可查看频道图谱。")); }
    } finally { refresh.disabled = false; }
  }
  select.addEventListener("change", load);
  search.addEventListener("input", () => {
    if (!state.data) return;
    const q = search.value.trim().toLowerCase();
    const found = state.data.nodes.filter(n => n.name.toLowerCase().includes(q));
    if (!q) return;
    details.replaceChildren(el("div", { class: "detail-label" }, `搜索结果 · ${found.length}`), el("div", { class: "entity-list" }, found.map(n => el("button", { onclick: () => showNode(n) }, n.name, el("span", {}, n.label)))));
  });
  await load();
}

function field(label, input, hint = "", full = false) { return el("div", { class: "field" + (full ? " full" : "") }, el("label", {}, label), input, hint ? el("small", {}, hint) : null); }
async function settingsPage(version) {
  const root = $("#content"); root.replaceChildren(heading("设置", "配置共享服务、信源和本地采集偏好；报告任务在「下游任务」中配置。"), empty("读取本地设置…", ""));
  try {
    const config = await api("config"); if (version !== routeVersion) return; state.config = config;
    try { const catalogue = await remote("channels"); if (version === routeVersion) state.channels = catalogue.items || []; } catch {}
    const serverUrl = el("input", { type: "url", value: config.serverUrl, placeholder: "https://your-server.example" });
    const key = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.keyConfigured ? `已配置 · 尾号 ${config.keySuffix}，留空保留` : "填写客户 Key" });
    const ca = el("input", { value: config.serverCA, placeholder: "可选：本地 CA 证书路径" });
    const maxUrls = el("input", { type: "number", min: 1, max: 200, value: config.maxUrls });
    const concurrency = el("input", { type: "number", min: 1, max: 6, value: config.browserConcurrency });
    // Production DOMAIN_CONCURRENCY shape; legacy configs stored a single int.
    const domainPolicy = typeof config.domainConcurrency === "number"
      ? { defaultConcurrency: config.domainConcurrency, domains: {} }
      : (config.domainConcurrency || { defaultConcurrency: 1, domains: {} });
    const domainDefault = el("input", { type: "number", min: 1, max: 6, value: domainPolicy.defaultConcurrency ?? 1 });
    const domainLimits = el("textarea", { "aria-label": "按域名并发 JSON" }, JSON.stringify(domainPolicy.domains || {}, null, 2));
    const timeout = el("input", { type: "number", min: 10, max: 300, value: config.timeoutSeconds });
    const headed = el("select", {}, el("option", { value: "false" }, "显示窗口，支持人工交互"), el("option", { value: "true" }, "后台无头模式")); headed.value = String(config.headless);
    const forbidden = el("input", { value: config.forbiddenDomains.join(", "), placeholder: "禁爬 URL 片段，以逗号分隔，如 www.gov.cn/home" });
    const captureTtl = el("input", { type: "number", min: 0, max: 31536000, value: config.captureCache?.defaultTtlSeconds ?? 21600 });
    const domainTtl = el("textarea", { "aria-label": "域名缓存时间 JSON" }, JSON.stringify(config.captureCache?.domainTtlSeconds || {}, null, 2));
    const submitTtl = el("input", { type: "number", min: 0, max: 31536000, value: config.submissionCache?.ttlSeconds ?? 604800 });
    const visionEnabled = el("input", { type: "checkbox" }); visionEnabled.checked = config.vision.enabled;
    const visionUrl = el("input", { type: "url", value: config.vision.baseUrl });
    const visionKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.vision.keyConfigured ? `已配置 · 尾号 ${config.vision.keySuffix}，留空保留` : "视觉模型独立 Key" });
    const visionModel = el("input", { value: config.vision.model });
    const visionFallback = el("input", { value: config.vision.fallbackModel });
    const visionExtra = el("input", { value: config.vision.extraModel });
    const visionConcurrency = el("input", { type: "number", min: 1, max: 6, value: config.vision.concurrency });
    const visionTimeout = el("input", { type: "number", min: 10, max: 600, value: config.vision.timeoutSeconds });
    let sources = structuredClone(config.sources);
    const MP_SENTINEL = "https://mp.weixin.qq.com/";
    const sourceContainer = el("div");
    function renderSources() {
      sourceContainer.replaceChildren();
      if (!sources.length) sourceContainer.append(empty("添加第一个信源", "支持公开网页、RSS 和微信公众号；复杂的操作流程可以交给管理技能配置。"));
      for (const [i, s] of sources.entries()) {
        const isMp = s.kind === "mp";
        const id = el("input", { value: s.id, placeholder: "source-1" });
        const url = el("input", { type: "url", value: isMp ? MP_SENTINEL : s.url, placeholder: "https://example.com/news" });
        if (isMp) url.setAttribute("readonly", "");
        const kind = el("select", {}, el("option", { value: "web" }, "网页"), el("option", { value: "rss" }, "RSS"), el("option", { value: "mp" }, "微信公众号")); kind.value = s.kind;
        const interval = el("input", { type: "number", min: 0, value: s.intervalMinutes });
        const profile = el("input", { value: s.profile || "default" });
        const enabled = el("input", { type: "checkbox" }); enabled.checked = s.enabled;
        const accounts = el("textarea", { "aria-label": "公众号白名单" }, (s.accounts || []).join("\n"));
        const steps = el("textarea", { "aria-label": "操作流程 JSON" }, JSON.stringify(s.steps || [], null, 2));
        s.channels = s.channels || [];
        const channels = channelSelect(s.channels, next => s.channels = next, "信源频道");
        const bind = (n, name, convert = v => v) => n.addEventListener("input", () => s[name] = convert(n.type === "checkbox" ? n.checked : n.value));
        bind(id, "id"); bind(url, "url"); bind(interval, "intervalMinutes", Number); bind(profile, "profile"); bind(enabled, "enabled");
        kind.addEventListener("change", () => {
          s.kind = kind.value;
          if (s.kind === "mp") { s.url = MP_SENTINEL; s.steps = []; s.accounts = s.accounts || []; delete s.stepsText; }
          else { s.accounts = []; }
          renderSources();
        });
        accounts.addEventListener("input", () => s.accounts = accounts.value.split("\n").map(x => x.trim()).filter(Boolean));
        steps.addEventListener("input", () => s.stepsText = steps.value);
        const run = el("button", { class: "small", onclick: async () => { try { const r = await api("runs", { method: "POST", body: JSON.stringify({ sourceId: s.id }) }); toast("任务已创建 · " + r.id.slice(-8)); } catch (e) { toast(e.message); } } }, "运行");
        const fields = [field("信源 ID", id), field("类型", kind), field("采集间隔 / 分钟", interval, "0 表示只手动运行。"), field("浏览器 Profile", profile),
          field("提交频道", channels, "可多选；不选表示只采集、不提交。频道目录来自共享服务。", true)];
        if (isMp) {
          fields.push(field("公众号白名单", accounts, "每行一个公众号名，须与本机微信关注的显示名完全一致；过去时段 0 命中的账号会在通知中报告。", true));
          fields.push(field("信源地址", url, "公众号信源固定入口；文章正文由浏览器抓取。", true));
        } else {
          fields.push(field("信源地址", url, "操作流程将从这个入口开始。", true));
        }
        const card = el("div", { class: "source-edit" }, el("div", { class: "row" }, el("label", { class: "row hint" }, enabled, "启用信源"), el("div", { class: "row" }, run, el("button", { class: "small danger", onclick: () => { sources.splice(i, 1); renderSources(); } }, "移除"))),
          el("div", { class: "form-grid" }, fields));
        if (!isMp) card.append(el("details", {}, el("summary", {}, "高级：可编程操作流程"), el("p", { class: "hint" }, "可使用管理技能生成步骤；留空则直接采集页面。"), steps));
        sourceContainer.append(card);
      }
    }
    renderSources();
    const visionDependents = [visionUrl, visionKey, visionModel, visionFallback, visionExtra, visionConcurrency, visionTimeout];
    function syncVisionToggle() { for (const input of visionDependents) input.disabled = !visionEnabled.checked; }
    visionEnabled.addEventListener("change", syncVisionToggle);
    syncVisionToggle();

    async function saveSection(button, buildUpdate, onSuccess) {
      button.disabled = true;
      try {
        const result = await api("config", { method: "PATCH", body: JSON.stringify(buildUpdate()) });
        key.placeholder = result.keyConfigured ? `已配置 · 尾号 ${result.keySuffix}，留空保留` : "填写客户 Key";
        visionKey.placeholder = result.vision.keyConfigured ? `已配置 · 尾号 ${result.vision.keySuffix}，留空保留` : "视觉模型独立 Key";
        banner(""); toast("本段设置已保存，下次任务使用新配置");
        if (onSuccess) onSuccess(result);
      } catch (e) { toast(e instanceof SyntaxError ? "本段含无效 JSON，请检查格式" : e.message); }
      finally { button.disabled = false; }
    }
    const saveServer = el("button", { class: "small primary", onclick: () => saveSection(saveServer, () => {
      const update = { serverUrl: serverUrl.value.trim(), serverCA: ca.value.trim() };
      if (key.value) update.serverKey = key.value;
      return update;
    }, () => { key.value = ""; }) }, "保存");
    const saveSources = el("button", { class: "small primary", onclick: () => saveSection(saveSources, () => {
      const cleanSources = sources.map(({ stepsText, ...s }) => {
        const steps = stepsText !== undefined ? JSON.parse(stepsText) : (s.steps || []);
        if (s.kind === "mp") return { ...s, url: MP_SENTINEL, steps: [], accounts: s.accounts || [] };
        return { ...s, steps, accounts: [] };
      });
      return { sources: cleanSources };
    }) }, "保存");
    const savePrefs = el("button", { class: "small primary", onclick: () => saveSection(savePrefs, () => ({
      maxUrls: Number(maxUrls.value), browserConcurrency: Number(concurrency.value),
      domainConcurrency: { defaultConcurrency: Number(domainDefault.value), domains: JSON.parse(domainLimits.value) },
      timeoutSeconds: Number(timeout.value), headless: headed.value === "true",
      forbiddenDomains: forbidden.value.split(",").map(x => x.trim()).filter(Boolean),
      captureCache: { defaultTtlSeconds: Number(captureTtl.value), domainTtlSeconds: JSON.parse(domainTtl.value) },
      submissionCache: { ttlSeconds: Number(submitTtl.value) },
    })) }, "保存");
    const saveVision = el("button", { class: "small primary", onclick: () => saveSection(saveVision, () => {
      const vision = { enabled: visionEnabled.checked, baseUrl: visionUrl.value.trim(), model: visionModel.value.trim(),
        fallbackModel: visionFallback.value.trim(), extraModel: visionExtra.value.trim(),
        concurrency: Number(visionConcurrency.value), timeoutSeconds: Number(visionTimeout.value) };
      if (visionKey.value) vision.apiKey = visionKey.value;
      return { vision };
    }, () => { visionKey.value = ""; }) }, "保存");
    const forms = el("div", {}, el("div", { class: "note" }, "设置保存在本机，每段可独立保存。客户 Key 由本地后台持有；保存不会发送测试消息或探测平台账号。"),
      el("section", { class: "card form-card" }, titlebar("01 · 共享服务", [saveServer]), el("div", { class: "form-body form-grid" }, field("服务地址", serverUrl, "服务端必须启用 HTTPS。"), field("客户 Key", key, "留空保留当前 Key，不会读回原值。"), field("自定义 CA", ca, "使用公共证书时留空。", true))),
      el("section", { class: "card form-card" }, titlebar("02 · 采集信源", [el("button", { class: "small", onclick: () => { sources.push({ id: "source-" + (sources.length + 1), url: "", kind: "web", enabled: true, intervalMinutes: 0, profile: "default", steps: [], channels: [] }); renderSources(); } }, "+ 添加信源"), saveSources]), el("div", { class: "form-body" }, el("p", { class: "hint" }, "相关性判断和本体提取全部由共享服务按频道完成；同一信源可提交到多个频道。"), el("div", { style: "height:12px" }), sourceContainer)),
      el("section", { class: "card form-card" }, titlebar("03 · 本地采集偏好", [savePrefs]), el("div", { class: "form-body form-grid" }, field("每次最多访问页面", maxUrls), field("全局浏览器并发", concurrency), field("默认同域名并发", domainDefault, "未单独配置的域名使用此上限；与生产 DOMAIN_CONCURRENCY 一致。"), field("网络超时 / 秒", timeout), field("窗口模式", headed),
        field("按域名并发覆盖", domainLimits, 'JSON 对象，例如 {"mp.weixin.qq.com": 2, "gov.cn": 3}；键按主域归一（www.gov.cn → gov.cn），上限 6。', true),
        field("排除域名", forbidden, "URL 子串匹配：可填域名或域名+路径前缀（如 www.gov.cn/home），命中即不访问；信源起点 URL 豁免。", true),
        field("页面缓存时间 / 秒", captureTtl, "默认 21600 秒（6 小时）；0 关闭。有效期从实际抓取开始计算。"),
        field("提交结果缓存时间 / 秒", submitTtl, "复用同一服务与处理版本的结果；0 关闭。"),
        field("域名缓存时间覆盖", domainTtl, 'JSON 对象，例如 {"news.example.com": 1800}；更具体的子域名优先。', true))),
      el("section", { class: "card form-card" }, titlebar("04 · 图片识别（VL）", [el("label", { class: "jev-switch" }, visionEnabled, el("span", {}, "启用图片识别")), saveVision]), el("div", { class: "form-body form-grid" },
        field("服务地址", visionUrl, "OpenAI 兼容视觉端点；须能公网抓取图片 URL。"),
        field("视觉 Key", visionKey, "保存在本机，不发送给共享 server。启用远程识别需填写。"),
        field("主模型", visionModel), field("备用模型", visionFallback, "可留空。"),
        field("补充模型", visionExtra, "可留空。"), field("并发数", visionConcurrency, "1–6，跨任务共享上限。"),
        field("超时 / 秒", visionTimeout, "单图识别超时；整页另有 300 秒预算，超时降级不丢页。", true))));

    root.replaceChildren(heading("信源配置", "配置共享服务、采集信源、采集偏好与视觉模型。"), el("div", { class: "form-layout" }, el("aside", { class: "section-index" }, el("div", {}, "◈ 共享服务"), el("div", {}, "↗ 采集信源"), el("div", {}, "⚙ 采集偏好"), el("div", {}, "▣ 图片识别")), forms));
  } catch (e) { banner(e.message); }
}

async function queryPage(version) {
  const root = $("#content");
  root.replaceChildren(heading("查询设置", "Jev 后置过滤与本地报告/查询模型；TopNews、Weeklyreport 和本地查询共用。"), empty("读取模型设置…", ""));
  const config = await api("config"); if (version !== routeVersion) return; state.config = config;
  const jevEnabled = el("input", { type: "checkbox" }); jevEnabled.checked = config.jev.enabled;
  const jevPost = el("input", { type: "checkbox" }); jevPost.checked = config.jev.postQuery;
  const jevUrl = el("input", { type: "url", value: config.jev.baseUrl });
  const jevModel = el("input", { value: config.jev.model });
  const jevKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.jev.keyConfigured ? `已配置 · 尾号 ${config.jev.keySuffix}，留空保留` : "Jev 独立 Key" });
  const jevConfidence = el("input", { type: "number", min: 0, max: 1, step: .05, value: config.jev.discardConfidence });
  const reportEnabled = el("input", { type: "checkbox" }); reportEnabled.checked = config.reportLlm.enabled;
  const reportUrl = el("input", { type: "url", value: config.reportLlm.baseUrl });
  const reportModelInput = el("input", { value: config.reportLlm.model });
  const reportKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.reportLlm.keyConfigured ? `已配置 · 尾号 ${config.reportLlm.keySuffix}，留空保留` : "本地 xin_local 无需 Key；远程端点才需要" });
  const reportTimeout = el("input", { type: "number", min: 10, max: 600, value: config.reportLlm.timeoutSeconds });
  const queryScope = el("select", {}, el("option", { value: "all" }, "所有 node 的贡献"), el("option", { value: "own" }, "仅本 node 的信源"));
  queryScope.value = config.queryDefaults?.sourceScope || "all";
  const dependents = [jevPost, jevUrl, jevModel, jevKey, jevConfidence];
  function syncJev() { for (const input of dependents) input.disabled = !jevEnabled.checked; }
  jevEnabled.addEventListener("change", syncJev); syncJev();
  const reportDeps = [reportUrl, reportModelInput, reportKey, reportTimeout];
  function syncReport() { for (const input of reportDeps) input.disabled = !reportEnabled.checked; }
  reportEnabled.addEventListener("change", syncReport); syncReport();
  async function save(button, buildUpdate, onSuccess) {
    button.disabled = true;
    try {
      const result = await api("config", { method: "PATCH", body: JSON.stringify(buildUpdate()) });
      jevKey.placeholder = result.jev.keyConfigured ? `已配置 · 尾号 ${result.jev.keySuffix}，留空保留` : "Jev 独立 Key";
      reportKey.placeholder = result.reportLlm.keyConfigured ? `已配置 · 尾号 ${result.reportLlm.keySuffix}，留空保留` : "本地 xin_local 无需 Key；远程端点才需要";
      toast("查询设置已保存"); if (onSuccess) onSuccess();
    } catch (e) { toast(e.message); }
    finally { button.disabled = false; }
  }
  const saveJev = el("button", { class: "small primary", onclick: () => save(saveJev, () => {
    const jev = { enabled: jevEnabled.checked, postQuery: jevPost.checked,
      baseUrl: jevUrl.value.trim(), model: jevModel.value.trim(), discardConfidence: Number(jevConfidence.value) };
    if (jevKey.value) jev.apiKey = jevKey.value; return { jev };
  }, () => { jevKey.value = ""; }) }, "保存");
  const saveScope = el("button", { class: "small primary", onclick: () => save(saveScope, () => ({ queryDefaults: { sourceScope: queryScope.value } })) }, "保存");
  const saveReport = el("button", { class: "small primary", onclick: () => save(saveReport, () => {
    const reportLlm = { enabled: reportEnabled.checked, baseUrl: reportUrl.value.trim(),
      model: reportModelInput.value.trim(), timeoutSeconds: Number(reportTimeout.value) };
    if (reportKey.value) reportLlm.apiKey = reportKey.value; return { reportLlm };
  }, () => { reportKey.value = ""; }) }, "保存");
  root.replaceChildren(heading("查询设置", "Jev 后置过滤与本地报告/查询模型；TopNews、Weeklyreport 和本地查询共用。"),
    el("div", { class: "note" }, "两项都只在本地使用：Jev 对 evidence 结果做后置筛选；本地模型负责 TopNews 编辑、Weeklyreport 撰写和本地查询辅助。均不会上传为 server 相关性依据。"),
    el("section", { class: "card form-card" }, titlebar("01 · 查询范围", [saveScope]), el("div", { class: "form-body form-grid" },
      field("默认来源范围", queryScope, "all 查询本 server 内所有 node 贡献的该频道内容（默认）；own 仅查询本 node 自己的信源。Section/板块可逐级覆盖。"))),
    el("section", { class: "card form-card" }, titlebar("02 · Jev 后置决策", [el("label", { class: "jev-switch" }, jevEnabled, el("span", {}, "启用 Jev")), saveJev]), el("div", { class: "form-body form-grid" },
      field("查询后筛选证据", jevPost, "只在 evidence 取回后执行；采集/提交阶段不会调用 Jev。"),
      field("Jev 服务地址", jevUrl), field("模型", jevModel), field("Jev Key", jevKey, "保存在本机，不发送给共享 server。"),
      field("舍弃所需最低置信度", jevConfidence, "低置信度或服务不可用时保留材料并继续。", true))),
    el("section", { class: "card form-card" }, titlebar("03 · 本地报告/查询模型", [el("label", { class: "jev-switch" }, reportEnabled, el("span", {}, "启用本地模型")), saveReport]), el("div", { class: "form-body form-grid" },
      field("服务地址", reportUrl, "默认本地 xinference（http://127.0.0.1:9997/v1），与生产影子管线一致。"),
      field("模型", reportModelInput, "默认 xin_local；TopNews 编辑、Weeklyreport 撰写和本地查询共用。"),
      field("API Key", reportKey, "本地端点无需 Key；仅远程 HTTPS 端点需要。保存在本机。"),
      field("超时 / 秒", reportTimeout, "单次模型调用超时；失败自动回退确定性选材与格式化。", true))));
}

function splitList(value) {
  return String(value || "").split(/[,，\n]/).map(item => item.trim()).filter(Boolean);
}
function channelSelect(selected, onChange, ariaLabel = "频道") {
  let values = [...(selected || [])];
  const box = el("div", { class: "channel-checks", role: "group", "aria-label": ariaLabel });
  const ids = [...new Set([...values, ...(state.channels || []).map(c => c.id)])];
  for (const id of ids) {
    const known = (state.channels || []).find(c => c.id === id);
    const checkbox = el("input", { type: "checkbox" });
    checkbox.checked = values.includes(id);
    checkbox.addEventListener("change", () => {
      values = checkbox.checked ? [...new Set([...values, id])] : values.filter(value => value !== id);
      onChange(values);
    });
    box.append(el("label", { class: "row hint" }, checkbox, known?.title || id));
  }
  return box;
}
function bindValue(object, key, input, convert = value => value) {
  input.addEventListener("input", () => { object[key] = convert(input.type === "checkbox" ? input.checked : input.value); });
  return input;
}
const SELECTORS = {
  candidates: "候选池 + 编辑模型（TopNews）",
  action: "行动热点网络（Weekly）",
  policy: "政策—行动链（Weekly）",
  evidence: "按日期直接取原文（通用）",
};

async function tasksPage(version) {
  const root = $("#content");
  root.replaceChildren(heading("任务设定", "配置板块、TopNews/Weeklyreport、调度与发送目标。"), empty("读取任务配置…", ""));
  const config = await api("config"); if (version !== routeVersion) return;
  let profiles = structuredClone(config.reportProfiles);
  try { const catalogue = await remote("channels"); if (version === routeVersion) state.channels = catalogue.items || []; } catch {}
  let selectedProfile = profiles[0]?.id || "";
  const profileBar = el("div", { class: "row", style: "margin-bottom:16px" });
  const body = el("div");
  const current = () => profiles.find(profile => profile.id === selectedProfile) || profiles[0];
  function nextId(items, prefix) { let i = 1; while (items.some(item => item.id === prefix + i)) i++; return prefix + i; }
  async function save(button, message = "任务配置已保存") {
    button.disabled = true;
    try {
      await api("config", { method: "PATCH", body: JSON.stringify({ reportProfiles: profiles }) });
      toast(message); render();
    } catch (e) { toast(e.message); }
    finally { button.disabled = false; }
  }
  function sectionCard(section, index, remove) {
    const channelPicker = channelSelect(section.channels, next => section.channels = next, "板块频道");
    const territory = el("input", { value: (section.query.territoryPreference || []).join(", "), placeholder: "上海市, 静安区, 临汾路街道" });
    territory.addEventListener("input", () => section.query.territoryPreference = splitList(territory.value));
    const territoryMode = el("select", {}, el("option", { value: "prefer" }, "优先但不排除"), el("option", { value: "only" }, "只保留该属地"));
    territoryMode.value = section.query.territoryMode || "prefer";
    territoryMode.addEventListener("change", () => section.query.territoryMode = territoryMode.value);
    const jev = el("select", {}, el("option", { value: "inherit" }, "继承全局"), el("option", { value: "on" }, "启用"), el("option", { value: "off" }, "关闭"));
    jev.value = section.localRules.jev || "inherit";
    jev.addEventListener("change", () => section.localRules.jev = jev.value);
    const scope = el("select", {}, el("option", { value: "inherit" }, "继承查询设置"), el("option", { value: "all" }, "所有 node 的贡献"), el("option", { value: "own" }, "仅本 node 的信源"));
    scope.value = section.query.sourceScope || "inherit";
    scope.addEventListener("change", () => section.query.sourceScope = scope.value);
    return el("div", { class: "source-card" },
      el("div", { class: "row" }, el("strong", {}, `Sections ${index + 1}`), el("button", { class: "small danger", onclick: remove }, "移除板块")),
      el("div", { class: "form-grid" },
        field("板块 ID", bindValue(section, "id", el("input", { value: section.id }))),
        field("板块标题", bindValue(section, "title", el("input", { value: section.title }))),
        field("查询频道", channelPicker, "可多选；同一板块可合并多个共享频道。", true),
        field("查询文本", bindValue(section.query, "text", el("input", { value: section.query.text || "" })), "服务端原文/标题过滤；留空取整个频道。"),
        field("属地偏好", territory, "查询时的软偏好，逗号分隔；可临时改成其他地区。"),
        field("属地模式", territoryMode),
        field("查询范围", scope, "inherit 时跟随「查询设置」的总开关。"),
        field("内容类型", el("input", { value: (section.query.contentTypes || []).join(", "), "oninput": event => section.query.contentTypes = splitList(event.target.value) }), "如 policy, news, activity；留空不限。"),
        field("本地关键词", el("input", { value: (section.localRules.keywords || []).join(", "), "oninput": event => section.localRules.keywords = splitList(event.target.value) }), "取回后仅保留包含任一关键词的材料。"),
        field("排除关键词", el("input", { value: (section.localRules.excludeKeywords || []).join(", "), "oninput": event => section.localRules.excludeKeywords = splitList(event.target.value) })),
        field("Jev 过滤", jev, "on/off 覆盖全局 post-query 开关。"))
    );
  }
  function boardCard(board, index, task, remove) {
    const sectionSelect = el("select", {}, ...current().sections.map(section => el("option", { value: section.id }, section.title)));
    sectionSelect.value = board.section;
    sectionSelect.addEventListener("change", () => board.section = sectionSelect.value);
    let selector;
    if (task === "topnews") {
      board.selector = "candidates";
      selector = el("span", { class: "hint" }, SELECTORS.candidates);
    } else {
      const selectorOptions = ["action", "policy", "evidence"];
      selector = el("select", {}, ...selectorOptions.map(value => el("option", { value }, SELECTORS[value])));
      selector.value = board.selector;
      selector.addEventListener("change", () => board.selector = selector.value);
    }
    const scope = el("select", {}, el("option", { value: "inherit" }, "继承 Section/全局"), el("option", { value: "all" }, "所有 node 的贡献"), el("option", { value: "own" }, "仅本 node 的信源"));
    scope.value = board.sourceScope || "inherit";
    scope.addEventListener("change", () => board.sourceScope = scope.value);
    return el("div", { class: "source-card" },
      el("div", { class: "row" }, el("strong", {}, `板块 ${index + 1}`), el("button", { class: "small danger", onclick: remove }, "移除")),
      el("div", { class: "form-grid" },
        field("Section", sectionSelect), field("选材方式", selector, task === "topnews" ? "TopNews 固定为候选池 + 编辑模型。" : "Weekly 按 action/policy 选材；evidence 仅用于通用原文检索。"),
        field("查询范围", scope, "inherit 时依次继承 Section、查询设置总开关。"),
        field("条数", bindValue(board, "topN", el("input", { type: "number", min: 1, max: 20, value: board.topN }), Number)),
        field("最少关系边", bindValue(board, "minEdges", el("input", { type: "number", min: 1, max: 100, value: board.minEdges }), Number)),
        field("写作意图", bindValue(board, "purpose", el("textarea", {}, board.purpose || "")), "只用于本地写作/编辑，不上传 server。", true),
        field("编辑提示", bindValue(board, "prompt", el("textarea", {}, board.prompt || "")), "可选，追加到本地编辑模型提示。", true))
    );
  }
  function taskCard(taskName, title) {
    const profile = current(); const task = profile[taskName];
    const enabled = el("input", { type: "checkbox" }); enabled.checked = task.enabled;
    enabled.addEventListener("change", () => task.enabled = enabled.checked);
    const dryRun = el("input", { type: "checkbox" }); dryRun.checked = task.dryRun;
    dryRun.addEventListener("change", () => task.dryRun = dryRun.checked);
    const timeInput = el("input", { value: task.schedule.time, placeholder: "08:40" });
    bindValue(task.schedule, "time", timeInput);
    const weekday = el("select", {}, el("option", { value: "" }, "每天"), ...["monday","tuesday","wednesday","thursday","friday","saturday","sunday"].map(value => el("option", { value }, value)));
    weekday.value = task.schedule.weekday || "";
    weekday.addEventListener("change", () => task.schedule.weekday = weekday.value || null);
    const timezone = el("input", { value: task.schedule.timezone || "Asia/Shanghai" });
    bindValue(task.schedule, "timezone", timezone);
    const targets = el("div", { class: "form-grid" });
    if (!profile.targets.length) targets.append(el("p", { class: "hint" }, "尚未配置发送目标；dry-run 时仍可生成报告。"));
    for (const target of profile.targets) {
      const box = el("input", { type: "checkbox" }); box.checked = (task.targetIds || []).includes(target.id);
      box.addEventListener("change", () => {
        task.targetIds = box.checked ? [...new Set([...(task.targetIds || []), target.id])] : (task.targetIds || []).filter(id => id !== target.id);
      });
      targets.append(el("label", { class: "row hint" }, box, `${target.label || target.id} · ${target.type}`));
    }
    const manual = el("button", { class: "small", onclick: async () => {
      const body = { profile: profile.id };
      if (taskName === "weeklyreport") { body.fromDate = localDate(-6); body.toDate = localDate(); }
      else { body.fromDate = localDate(); body.toDate = localDate(); }
      try { await api(`${taskName === "topnews" ? "topnews" : "weekly"}/run`, { method: "POST", body: JSON.stringify(body) }); toast("报告任务已创建，请到「报告」页查看进度"); }
      catch (e) { toast(e.message); }
    } }, "手动运行");
    const saveButton = el("button", { class: "small primary", onclick: () => save(saveButton) }, "保存");
    const addBoard = el("button", { class: "small", onclick: () => {
      const available = profile.sections.find(section => !task.boards.some(board => board.section === section.id));
      if (!available) return toast("所有 section 都已加入该任务");
      task.boards.push({ section: available.id, selector: taskName === "topnews" ? "candidates" : "action", topN: taskName === "topnews" ? 3 : 2, minEdges: 4, purpose: "", prompt: "", sourceScope: "inherit" });
      render();
    } }, "+ 添加板块");
    return el("section", { class: "card form-card" }, titlebar(title, [el("label", { class: "jev-switch" }, enabled, el("span", {}, "启用")), el("label", { class: "jev-switch" }, dryRun, el("span", {}, "Dry-run")), manual, saveButton]),
      el("div", { class: "form-body" }, el("div", { class: "form-grid" }, field("发送时间", timeInput, "HH:MM"), field("星期", weekday, "每周只跑一次；每天任务选“每天”。"), field("时区", timezone)),
        el("div", { class: "card-title" }, el("h3", {}, "发送目标"), addBoard),
        targets,
        ...task.boards.map((board, index) => boardCard(board, index, task, () => { task.boards.splice(index, 1); render(); }))));
  }
  function targetsCard(profile, saveButton) {
    return el("section", { class: "card form-card" }, titlebar("发送目标", [saveButton]),
      el("div", { class: "form-body" }, el("p", { class: "hint" }, "MimicWX / Awada 已实现；企业微信 hook 预留，发送时返回 not_implemented。秘密字段只写不读，留空保留已存值。"),
        ...profile.targets.map((target, index) => {
          const type = el("select", {}, el("option", { value: "mimicwx" }, "MimicWX"), el("option", { value: "awada" }, "Awada"), el("option", { value: "wecom-webhook" }, "企微 hook（预留，未开发）"));
          type.value = target.type;
          type.addEventListener("change", () => { target.type = type.value; target.config = {}; render(); });
          const fields = [field("目标 ID", bindValue(target, "id", el("input", { value: target.id }))),
                          field("标签", bindValue(target, "label", el("input", { value: target.label || "" })))];
          const enabled = el("input", { type: "checkbox" }); enabled.checked = target.enabled;
          enabled.addEventListener("change", () => target.enabled = enabled.checked);
          const configFields = [];
          if (target.type === "mimicwx") {
            configFields.push(field("服务地址", el("input", { value: target.config.baseUrl || "", "oninput": event => target.config.baseUrl = event.target.value })));
            configFields.push(field("群名", el("input", { value: target.config.window || "", "oninput": event => target.config.window = event.target.value })));
            configFields.push(field("Token", el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: target.config.tokenConfigured ? `已配置 · 尾号 ${target.config.tokenSuffix || ""}，留空保留` : "填写 Token", "oninput": event => target.config.token = event.target.value })));
            configFields.push(field("配置文件路径", el("input", { value: target.config.configPath || "", "oninput": event => target.config.configPath = event.target.value })));
          } else if (target.type === "awada") {
            configFields.push(field("Awada 配置路径", el("input", { value: target.config.configPath || "", "oninput": event => target.config.configPath = event.target.value })));
            configFields.push(field("频道 ID", el("input", { value: target.config.channelId || "", "oninput": event => target.config.channelId = event.target.value })));
            configFields.push(field("文件频道 ID", el("input", { value: target.config.fileChannelId || "", "oninput": event => target.config.fileChannelId = event.target.value }), "可选：周报 docx 文件发送频道。"));
          } else {
            configFields.push(field("Webhook URL", el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: target.config.webhookConfigured ? "已配置，留空保留" : "https://qyapi.weixin.qq.com/...", "oninput": event => target.config.webhookUrl = event.target.value })));
            configFields.push(field("提及", el("input", { value: target.config.mentioned || "", "oninput": event => target.config.mentioned = event.target.value })));
            configFields.push(field("消息类型", el("input", { value: target.config.msgType || "text", "oninput": event => target.config.msgType = event.target.value })));
          }
          return el("div", { class: "source-card" }, el("div", { class: "row" }, type, el("label", { class: "row hint" }, enabled, "启用"), el("button", { class: "small danger", onclick: () => { profile.targets.splice(index, 1); render(); } }, "移除")),
            el("div", { class: "form-grid" }, ...fields, ...configFields));
        }),
        el("button", { class: "small", onclick: () => { profile.targets.push({ id: nextId(profile.targets, "target-"), type: "mimicwx", label: "MimicWX", enabled: false, config: {} }); render(); } }, "+ 添加发送目标")));
  }
  function render() {
    const profile = current();
    profileBar.replaceChildren(...profiles.map(item => el("button", { class: item.id === profile.id ? "small primary" : "small", onclick: () => { selectedProfile = item.id; render(); } }, item.name || item.id)));
    const sectionSave = el("button", { class: "small primary", onclick: () => save(sectionSave, "Sections 已保存") }, "保存 Sections");
    const targetsSave = el("button", { class: "small primary", onclick: () => save(targetsSave, "发送目标已保存") }, "保存目标");
    body.replaceChildren(
      el("section", { class: "card form-card" }, titlebar("方案", []), el("div", { class: "form-grid" },
        field("方案 ID", bindValue(profile, "id", el("input", { value: profile.id }))), field("方案名称", bindValue(profile, "name", el("input", { value: profile.name })))),
        el("p", { class: "hint" }, "方案 = 一组 Sections + 两个任务；所有内容保存在本机。")),
      el("section", { class: "card form-card" }, titlebar("Sections · 共享板块模板", [el("button", { class: "small", onclick: () => { profile.sections.push({ id: nextId(profile.sections, "section-"), title: "新板块", channels: (state.channels || []).slice(0, 1).map(c => c.id), query: { text: "", entityUris: [], contentTypes: [], territoryPreference: structuredClone(profile.sections[0]?.query?.territoryPreference || []), territoryMode: "prefer", sourceScope: "inherit" }, localRules: { keywords: [], excludeKeywords: [], leaderBoost: true, localityPenalty: true, jev: "inherit" } }); render(); } }, "+ 添加 Section"), sectionSave]), el("div", { class: "form-body" }, el("p", { class: "hint" }, "Section 是共享模板：频道、查询、属地偏好和本地过滤。下面的 TopNews/Weekly 板块只是引用某个 Section，并覆盖选材方式、条数、写作意图，不会修改 Section 本身。"), ...profile.sections.map((section, index) => sectionCard(section, index, () => { profile.sections.splice(index, 1); render(); })))),
      taskCard("topnews", "TopNews · 每日简报"),
      taskCard("weeklyreport", "Weeklyreport · 每周热点"),
      targetsCard(profile, targetsSave));
  }
  render();
  root.replaceChildren(heading("任务设定", "Sections、TopNews、Weeklyreport、调度与发送目标。"), profileBar, body);
}

async function notificationsPage(version) {
  const root = $("#content");
  const filter = el("select", { "aria-label": "消息级别" }, el("option", { value: "all" }, "全部消息"), el("option", { value: "warning" }, "提醒"), el("option", { value: "error" }, "错误"));
  const list = el("div", { class: "event-list" });
  const prompts = el("div");
  const live = el("span", { class: "hint" }, "连接实时消息…");
  const metric = el("div");
  function render() {
    const values = [...state.events.values()].filter(e => filter.value === "all" || e.level === filter.value).reverse();
    const entries = values.map(e => el("div", { class: "event " + e.level },
      el("span", { class: "event-dot" }),
      el("div", {}, el("div", { class: "event-message" }, e.message), el("div", { class: "event-meta" }, e.run_id || e.kind)),
      el("time", {}, new Date(e.at * 1000).toLocaleTimeString("zh-CN", { hour12: false }))));
    list.replaceChildren(...entries);
    if (!values.length) list.append(empty("还没有消息", "开始一个采集任务后，可以在这里跟进进度和待办。"));
    metric.replaceChildren(stats([["◷", "显示消息", values.length], ["!", "提醒", values.filter(e => e.level === "warning").length], ["×", "错误", values.filter(e => e.level === "error").length], ["≋", "消息状态", live.textContent, true]]));
  }
  async function loadPrompts() {
    try {
      const result = await api("runs"); if (version !== routeVersion) return;
      const items = result.items.filter(r => ["waiting_for_user", "interrupted"].includes(r.state));
      prompts.replaceChildren(...items.map(r => el("div", { class: "prompt-card" }, el("h3", {}, "需要人工操作"), el("p", {}, r.error || "请检查浏览器中的任务状态后继续。"), el("small", {}, r.id),
        el("div", { class: "row" }, el("button", { class: "small", onclick: () => action(r, "resume") }, "已完成，继续"), el("button", { class: "small", onclick: () => action(r, "cancel") }, "取消任务")))));
      if (!items.length) prompts.append(empty("暂无待处理操作", "需要登录或验证时，任务会在这里等待你。"));
    } catch (e) { toast(e.message); }
  }
  async function action(r, a) { try { await api(`runs/${r.id}/${a}`, { method: "POST", body: JSON.stringify({ promptId: r.prompt }) }); await loadPrompts(); updatePendingBadge(); } catch (e) { toast(e.message); } }
  async function history() {
    try {
      let cursor = 0;
      while (true) { const batch = await api(`events?after=${cursor}&limit=500`); if (version !== routeVersion) return; for (const e of batch.items) state.events.set(e.id, e); if (batch.items.length < 500) break; cursor = batch.items.at(-1).id; }
      state.cursor = Math.max(0, ...state.events.keys()); render(); await loadPrompts();
    } catch (e) { banner(e.message); }
  }
  filter.addEventListener("change", render);
  root.replaceChildren(heading("消息通知", "跟进采集进度、查看运行记录，及时处理需要你的操作。"), metric,
    el("div", { class: "event-layout" }, el("section", { class: "card" }, titlebar("任务消息", [live, filter, el("button", { class: "small", onclick: history }, "↻ 历史"), el("button", { class: "small", onclick: () => { state.events.clear(); render(); } }, "清空视图")]), list), el("aside", { class: "card" }, titlebar("人工待办"), prompts)));
  await history(); if (version !== routeVersion) return;
  state.stream = new EventSource(`/api/events/stream?after=${state.cursor}`);
  state.stream.onopen = () => { live.textContent = "实时已连接"; render(); };
  state.stream.onerror = () => { live.textContent = "断线重连中"; render(); };
  state.stream.onmessage = event => {
    const e = JSON.parse(event.data); state.events.set(e.id, e); state.cursor = Math.max(state.cursor, e.id); render();
    if (["prompt", "prompt_resolved", "run_cancelled"].includes(e.kind)) loadPrompts();
  };
}

function localDate(offsetDays = 0) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);
}
async function reportsPage(version) {
  const root = $("#content");
  const recentList = el("div", { class: "report-list" });
  const viewer = el("pre", { class: "report-output", hidden: true });
  const viewerMeta = el("div", { class: "report-meta" });
  async function loadRecent() {
    try {
      const result = await api("reports"); if (version !== routeVersion) return;
      recentList.replaceChildren(...result.items.map(item => el("button", { onclick: () => showFile(item) },
        `${item.kind === "topnews" ? "▤ TopNews" : "▦ Weekly"} · ${item.name}`, el("span", {}, item.modified.replace("T", " ")))));
      if (!result.items.length) recentList.append(empty("还没有生成过报告", "点击上方按钮，用本地模型生成第一篇 TopNews 或 Weeklyreport。"));
    } catch (e) { recentList.replaceChildren(empty("无法读取报告列表", e.message)); }
  }
  async function showFile(item) {
    try {
      const data = await api(`reports/file?kind=${encodeURIComponent(item.kind)}&name=${encodeURIComponent(item.name)}`);
      viewer.hidden = false; viewer.textContent = data.content; viewerMeta.textContent = "文件：" + data.path;
    } catch (e) { toast(e.message); }
  }
  function reportCard(kind, title, subtitle, defaultFrom, defaultTo, buttonLabel) {
    const from = el("input", { type: "date", value: defaultFrom, "aria-label": "开始日期" });
    const to = el("input", { type: "date", value: defaultTo, "aria-label": "结束日期" });
    const status = el("span", { class: "hint" }, "读取最近一次输出…");
    const output = el("pre", { class: "report-output", hidden: true });
    const meta = el("div", { class: "report-meta" });
    const run = el("button", { class: "small primary", onclick: start }, buttonLabel);
    function show(result) {
      output.hidden = false; output.textContent = result.content || "（本次窗口没有可选材料）";
      const parts = [];
      if (result.path) parts.push("文件：" + result.path);
      if (result.source || result.combinedSource) parts.push("来源：" + (result.source || result.combinedSource));
      if (result.omittedBoards?.length) parts.push("因数据不足省略：" + result.omittedBoards.join("、"));
      if (result.dataScarce) parts.push("全部板块未达选材门槛，已用确定性材料概览兜底");
      meta.textContent = parts.join(" · ");
      status.textContent = "已生成 · " + (result.date || "");
    }
    async function start() {
      run.disabled = true; status.textContent = "正在生成：本地模型撰写可能需要几分钟…";
      try {
        const job = await api(`${kind}/run`, { method: "POST", body: JSON.stringify({ fromDate: from.value, toDate: to.value }) });
        for (;;) {
          await new Promise(r => setTimeout(r, 2000));
          if (version !== routeVersion) return;
          const j = await api(`reports/jobs/${job.jobId}`);
          if (j.state === "running") { status.textContent = "正在生成：" + (j.kind === "weekly" ? "选材与撰写" : "候选与编辑") + "…"; continue; }
          if (j.state === "failed") throw new Error(j.error || "生成失败");
          show(j.result); loadRecent(); break;
        }
      } catch (e) { status.textContent = "生成失败"; banner(e.message); }
      finally { run.disabled = false; }
    }
    (async () => {
      try {
        const latest = await api(`${kind}/latest`); if (version !== routeVersion) return;
        if (latest.empty) { status.textContent = "尚未生成"; return; }
        output.hidden = false; output.textContent = latest.content;
        meta.textContent = "文件：" + latest.path;
        status.textContent = "最近一次 · " + latest.modified.replace("T", " ");
      } catch { status.textContent = "尚未生成"; }
    })();
    return el("section", { class: "card form-card" }, titlebar(title, [run]),
      el("div", { class: "form-body" }, el("p", { class: "hint" }, subtitle),
        el("div", { class: "row", style: "margin:12px 0" }, field("开始日期", from), field("结束日期", to), status),
        output, meta));
  }
  root.replaceChildren(heading("任务产出", "复刻生产影子选材算法：图谱候选 → 本地模型编辑/撰写 → 可追溯输出。"),
    el("div", { class: "report-grid" },
      reportCard("topnews", "TopNews · 每日简报", "五板块候选池（标题/事件去重、属地与质量门、发布者惩罚），每板块本地模型编辑；默认昨日至今窗口。", localDate(-1), localDate(), "生成 TopNews"),
      reportCard("weekly", "Weeklyreport · 每周热点", "行动热点网络广度 + 政策链 + 经济兜底选材，跨周覆盖去重；本地模型撰写板块与综合研判/导读；默认近 8 个日历日（对齐生产影子 7 天 lookback）。", localDate(-7), localDate(), "生成 Weeklyreport")),
    el("section", { class: "card form-card" }, titlebar("最近报告", [el("button", { class: "small", onclick: loadRecent }, "↻ 刷新")]),
      el("div", { class: "form-body" }, recentList, viewer, viewerMeta)));
  await loadRecent();
}

async function route() {
  const version = ++routeVersion;
  state.stream?.close(); state.stream = null;
  state.chart?.dispose(); state.chart = null; state.data = null;
  state.page = location.pathname.split("/")[1] || "graph";
  if (!["graph", "reports", "tasks", "query", "settings", "notifications"].includes(state.page)) state.page = "graph";
  const labels = { graph: "情报图谱", reports: "任务产出", tasks: "任务设定", query: "查询设置", settings: "信源配置", notifications: "消息通知" };
  $("#page-label").textContent = labels[state.page]; document.title = labels[state.page] + " · OSBI";
  document.querySelectorAll("[data-nav]").forEach(n => n.classList.toggle("active", n.dataset.nav === state.page));
  banner("");
  try { await ({ graph: graphPage, reports: reportsPage, tasks: tasksPage, query: queryPage, settings: settingsPage, notifications: notificationsPage }[state.page])(version); } catch (e) { banner(e.message); }
  updatePendingBadge();
}
document.addEventListener("click", e => { const a = e.target.closest("a"); if (a && a.origin === location.origin && ["/graph", "/reports", "/tasks", "/query", "/settings", "/notifications"].includes(a.pathname)) { e.preventDefault(); history.pushState({}, "", a.pathname); route(); } });
window.addEventListener("popstate", route);
window.addEventListener("resize", () => state.chart?.resize());
async function localState() { try { await api("status"); $("#local-label").textContent = "本地后台运行中"; $("#local-dot").classList.remove("offline"); } catch { $("#local-label").textContent = "本地后台未连接"; $("#local-dot").classList.add("offline"); } }
async function updatePendingBadge() {
  const badge = $("#unread"); if (!badge) return;
  try {
    const result = await api("runs");
    const count = result.items.filter(r => ["waiting_for_user", "interrupted"].includes(r.state)).length;
    badge.textContent = count; badge.hidden = count === 0;
  } catch { badge.hidden = true; }
}
localState(); setInterval(localState, 15000); updatePendingBadge(); setInterval(updatePendingBadge, 15000); route();
