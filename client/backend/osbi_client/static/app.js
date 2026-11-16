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
  const root = $("#content"); root.replaceChildren(heading("设置", "配置你的共享服务、信源和本地采集偏好。"), empty("读取本地设置…", ""));
  try {
    const config = await api("config"); if (version !== routeVersion) return; state.config = config;
    const serverUrl = el("input", { type: "url", value: config.serverUrl, placeholder: "https://your-server.example" });
    const key = el("input", { type: "password", autocomplete: "new-password", placeholder: config.keyConfigured ? `已配置 · 尾号 ${config.keySuffix}，留空保留` : "填写客户 Key" });
    const ca = el("input", { value: config.serverCA, placeholder: "可选：本地 CA 证书路径" });
    const focus = el("textarea", {}, config.focus);
    const maxUrls = el("input", { type: "number", min: 1, max: 200, value: config.maxUrls });
    const concurrency = el("input", { type: "number", min: 1, max: 6, value: config.browserConcurrency });
    const domains = el("input", { type: "number", min: 1, max: 6, value: config.domainConcurrency });
    const timeout = el("input", { type: "number", min: 10, max: 300, value: config.timeoutSeconds });
    const headed = el("select", {}, el("option", { value: "false" }, "显示窗口，支持人工交互"), el("option", { value: "true" }, "后台无头模式")); headed.value = String(config.headless);
    const forbidden = el("input", { value: config.forbiddenDomains.join(", "), placeholder: "不继续访问的域名，以逗号分隔" });
    const jevEnabled = el("input", { type: "checkbox" }); jevEnabled.checked = config.jev.enabled;
    const jevPre = el("input", { type: "checkbox" }); jevPre.checked = config.jev.preSubmit;
    const jevPost = el("input", { type: "checkbox" }); jevPost.checked = config.jev.postQuery;
    const jevUrl = el("input", { type: "url", value: config.jev.baseUrl });
    const jevModel = el("input", { value: config.jev.model });
    const jevKey = el("input", { type: "password", autocomplete: "new-password", placeholder: config.jev.keyConfigured ? `已配置 · 尾号 ${config.jev.keySuffix}，留空保留` : "Jev 独立 Key" });
    const jevConfidence = el("input", { type: "number", min: 0, max: 1, step: .05, value: config.jev.discardConfidence });
    const reportProfiles = structuredClone(config.reportProfiles);
    const reportContainer = el("div");
    function reportInput(object, key, type = "text") {
      const input = type === "textarea" ? el("textarea", {}, object[key] || "") : el("input", { type, value: object[key] ?? "" });
      input.addEventListener("input", () => { object[key] = type === "number" ? Number(input.value) : input.value; });
      return input;
    }
    function nextId(items, prefix) {
      let i = 1; while (items.some(item => item.id === prefix + i)) i++;
      return prefix + i;
    }
    function renderReports() {
      reportContainer.replaceChildren();
      if (!reportProfiles.length) reportContainer.append(empty("尚未设置报告方案", "添加部门或个人方案，分别配置 TopNews 和 Weeklyreport。"));
      for (const [i, profile] of reportProfiles.entries()) {
        const sections = [];
        for (const task of ["topnews", "weeklyreport"]) {
          const boards = profile[task];
          const cards = boards.map((board, index) => {
            const selector = el("select", {}, el("option", { value: "evidence" }, "通用：按日期选择原文"), el("option", { value: "policy-actions" }, "政策行动：上海／静安预设"));
            selector.value = board.selector;
            selector.addEventListener("change", () => { board.selector = selector.value; });
            return el("div", { class: "source-card" }, el("div", { class: "row" }, el("strong", {}, `板块 ${index + 1}`), el("button", { class: "small", onclick: () => { boards.splice(index, 1); renderReports(); } }, "移除板块")),
              el("div", { class: "form-grid" }, field("板块 ID", reportInput(board, "id")), field("板块标题", reportInput(board, "title")),
                field("查询频道", reportInput(board, "channel")), field("选材条数", reportInput(board, "topN", "number")),
                field("选材方式", selector), field("查询关键词", reportInput(board, "query"), "原文包含该文字时才纳入；留空获取整个频道。"),
                field("板块关注点与写作要求", reportInput(board, "focus", "textarea"), "用于本地写作及可选 Jev 筛选。", true)),
              el("details", {}, el("summary", {}, "政策行动高级设置"), field("最少关系边数", reportInput(board, "minEdges", "number"))));
          });
          sections.push(el("section", {}, titlebar(task === "topnews" ? "TopNews 板块" : "Weeklyreport 板块", [el("button", { class: "small", onclick: () => {
            boards.push({ id: nextId(boards, "board-"), title: "新板块", channel: "policy", focus: "", query: "", topN: 3, selector: "evidence", minEdges: 4 }); renderReports();
          } }, "+ 添加板块")]), ...cards));
        }
        reportContainer.append(el("details", { class: "source-card", ...(i === 0 ? { open: "" } : {}) }, el("summary", {}, profile.name || profile.id),
          el("div", { class: "form-grid" }, field("方案 ID", reportInput(profile, "id"), "保持稳定，用于隔离本地周报历史。"), field("方案名称", reportInput(profile, "name")),
            field("方案关注点", reportInput(profile, "focus", "textarea"), "同一频道可供不同部门使用，每个方案独立设置。", true)),
          ...sections, el("button", { class: "small", onclick: () => { reportProfiles.splice(i, 1); renderReports(); } }, "移除方案")));
      }
    }
    renderReports();
    let sources = structuredClone(config.sources);
    const sourceContainer = el("div");
    function renderSources() {
      sourceContainer.replaceChildren();
      if (!sources.length) sourceContainer.append(empty("添加第一个信源", "支持公开网页和 RSS；复杂的操作流程可以交给管理技能配置。"));
      for (const [i, s] of sources.entries()) {
        const id = el("input", { value: s.id, placeholder: "source-1" });
        const url = el("input", { type: "url", value: s.url, placeholder: "https://example.com/news" });
        const kind = el("select", {}, el("option", { value: "web" }, "网页"), el("option", { value: "rss" }, "RSS")); kind.value = s.kind;
        const interval = el("input", { type: "number", min: 0, value: s.intervalMinutes });
        const profile = el("input", { value: s.profile || "default" });
        const enabled = el("input", { type: "checkbox" }); enabled.checked = s.enabled;
        const steps = el("textarea", { "aria-label": "操作流程 JSON" }, JSON.stringify(s.steps || [], null, 2));
        const bind = (n, name, convert = v => v) => n.addEventListener("input", () => s[name] = convert(n.type === "checkbox" ? n.checked : n.value));
        bind(id, "id"); bind(url, "url"); bind(kind, "kind"); bind(interval, "intervalMinutes", Number); bind(profile, "profile"); bind(enabled, "enabled");
        steps.addEventListener("input", () => s.stepsText = steps.value);
        const run = el("button", { class: "small", onclick: async () => { try { const r = await api("runs", { method: "POST", body: JSON.stringify({ sourceId: s.id }) }); toast("任务已创建 · " + r.id.slice(-8)); } catch (e) { toast(e.message); } } }, "运行");
        sourceContainer.append(el("div", { class: "source-edit" }, el("div", { class: "row" }, el("label", { class: "row hint" }, enabled, "启用信源"), el("div", { class: "row" }, run, el("button", { class: "small danger", onclick: () => { sources.splice(i, 1); renderSources(); } }, "移除"))),
          el("div", { class: "form-grid" }, field("信源 ID", id), field("类型", kind), field("信源地址", url, "操作流程将从这个入口开始。", true), field("采集间隔 / 分钟", interval, "0 表示只手动运行。"), field("浏览器 Profile", profile)),
          el("details", {}, el("summary", {}, "高级：可编程操作流程"), el("p", { class: "hint" }, "可使用管理技能生成步骤；留空则直接采集页面。"), steps)));
      }
    }
    renderSources();
    const save = el("button", { class: "primary", onclick: async () => {
      save.disabled = true;
      try {
        const cleanSources = sources.map(({ stepsText, ...s }) => ({ ...s, steps: stepsText !== undefined ? JSON.parse(stepsText) : s.steps }));
        const update = { serverUrl: serverUrl.value.trim(), serverCA: ca.value.trim(), focus: focus.value, sources: cleanSources,
          maxUrls: Number(maxUrls.value), browserConcurrency: Number(concurrency.value), domainConcurrency: Number(domains.value), timeoutSeconds: Number(timeout.value), headless: headed.value === "true", forbiddenDomains: forbidden.value.split(",").map(x => x.trim()).filter(Boolean) };
        update.reportProfiles = reportProfiles;
        update.jev = { enabled: jevEnabled.checked, preSubmit: jevPre.checked, postQuery: jevPost.checked, baseUrl: jevUrl.value.trim(), model: jevModel.value.trim(), discardConfidence: Number(jevConfidence.value) };
        if (key.value) update.serverKey = key.value;
        if (jevKey.value) update.jev.apiKey = jevKey.value;
        const result = await api("config", { method: "PATCH", body: JSON.stringify(update) });
        key.value = ""; key.placeholder = `已配置 · 尾号 ${result.keySuffix}，留空保留`; banner(""); toast("设置已保存，下次任务使用新配置");
        jevKey.value = ""; jevKey.placeholder = result.jev.keyConfigured ? `已配置 · 尾号 ${result.jev.keySuffix}，留空保留` : "Jev 独立 Key";
      } catch (e) { toast(e instanceof SyntaxError ? "操作流程不是有效 JSON，请检查格式" : e.message); }
      finally { save.disabled = false; }
    } }, "保存设置");
    const forms = el("div", {}, el("div", { class: "note" }, "设置保存在本机。客户 Key 由本地后台持有；保存设置不会发送测试消息或探测平台账号。"),
      el("section", { class: "card form-card" }, titlebar("01 · 共享服务"), el("div", { class: "form-body form-grid" }, field("服务地址", serverUrl, "服务端必须启用 HTTPS。"), field("客户 Key", key, "留空保留当前 Key，不会读回原值。"), field("自定义 CA", ca, "使用公共证书时留空。", true))),
      el("section", { class: "card form-card" }, titlebar("02 · 采集信源", [el("button", { class: "small", onclick: () => { sources.push({ id: "source-" + (sources.length + 1), url: "", kind: "web", enabled: true, intervalMinutes: 0, profile: "default", steps: [] }); renderSources(); } }, "+ 添加信源")]), el("div", { class: "form-body" }, field("关注内容", focus, "可选 Jev 在本地预筛；领域提取与频道归类由共享服务完成。", true), el("div", { style: "height:20px" }), sourceContainer)),
      el("section", { class: "card form-card" }, titlebar("03 · 本地采集偏好"), el("div", { class: "form-body form-grid" }, field("每次最多访问页面", maxUrls), field("全局浏览器并发", concurrency), field("同域名并发", domains), field("网络超时 / 秒", timeout), field("窗口模式", headed), field("排除域名", forbidden))),
      el("section", { class: "card form-card" }, titlebar("04 · 可选 Jev 决策"), el("div", { class: "form-body form-grid" },
        field("启用本地 Jev 决策", jevEnabled, "默认关闭。启用后，待判定内容将发送到你配置的 Jev 服务。"),
        field("提交 server 前判断相关性", jevPre), field("查询后筛选证据", jevPost),
        field("Jev 服务地址", jevUrl), field("模型", jevModel), field("Jev Key", jevKey, "保存在本机，不发送给共享 server。"),
        field("舍弃所需最低置信度", jevConfidence, "低置信度或服务不可用时保留材料并继续。"))),
      el("section", { class: "card form-card" }, titlebar("05 · 本地报告方案", [el("button", { class: "small", onclick: () => {
        reportProfiles.push({ id: nextId(reportProfiles, "profile-"), name: "新方案", focus: "", topnews: [], weeklyreport: [] }); renderReports();
      } }, "+ 添加方案")]), el("div", { class: "form-body" }, el("p", { class: "hint" }, "各方案的关注点、板块和周报历史保存在本机。先保存设置，再通过查询技能或 CLI 按板块准备材料；自动调度和推送暂未启用。"), reportContainer)),
      el("div", { class: "actions" }, save));
    root.replaceChildren(heading("设置", "配置你的共享服务、信源和本地采集偏好。"), el("div", { class: "form-layout" }, el("aside", { class: "section-index" }, el("div", {}, "◈ 共享服务"), el("div", {}, "↗ 采集信源"), el("div", {}, "⚙ 采集偏好")), forms));
  } catch (e) { banner(e.message); }
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
  async function action(r, a) { try { await api(`runs/${r.id}/${a}`, { method: "POST", body: JSON.stringify({ promptId: r.prompt }) }); await loadPrompts(); } catch (e) { toast(e.message); } }
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

async function route() {
  const version = ++routeVersion;
  state.stream?.close(); state.stream = null;
  state.chart?.dispose(); state.chart = null; state.data = null;
  state.page = location.pathname.split("/")[1] || "graph";
  if (!["graph", "settings", "notifications"].includes(state.page)) state.page = "graph";
  const labels = { graph: "情报图谱", settings: "设置", notifications: "消息通知" };
  $("#page-label").textContent = labels[state.page]; document.title = labels[state.page] + " · OSBI";
  document.querySelectorAll("[data-nav]").forEach(n => n.classList.toggle("active", n.dataset.nav === state.page));
  banner("");
  try { await ({ graph: graphPage, settings: settingsPage, notifications: notificationsPage }[state.page])(version); } catch (e) { banner(e.message); }
}
document.addEventListener("click", e => { const a = e.target.closest("a"); if (a && a.origin === location.origin && ["/graph", "/settings", "/notifications"].includes(a.pathname)) { e.preventDefault(); history.pushState({}, "", a.pathname); route(); } });
window.addEventListener("popstate", route);
window.addEventListener("resize", () => state.chart?.resize());
async function localState() { try { await api("status"); $("#local-label").textContent = "本地后台运行中"; $("#local-dot").classList.remove("offline"); } catch { $("#local-label").textContent = "本地后台未连接"; $("#local-dot").classList.add("offline"); } }
localState(); setInterval(localState, 15000); route();
