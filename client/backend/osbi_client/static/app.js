/* Local dashboard. Remote data always crosses the authenticated backend facade. */
"use strict";
const i18n = window.OSBI_I18N;
const t = i18n.t;
const text = i18n.text;
const $ = (s, root = document) => root.querySelector(s);
const el = (tag, attrs = {}, ...children) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (k === "text") text(n, v);
    else i18n.attribute(n, k, v);
  }
  if (["input", "textarea", "select"].includes(tag)) {
    // Avoid password managers / secure-input keyboards treating ordinary
    // configuration fields as credential prompts.
    if (!("autocomplete" in attrs)) n.setAttribute("autocomplete", "off");
    n.setAttribute("data-lpignore", "true");
    n.setAttribute("data-1p-ignore", "true");
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) n.append(c instanceof Node ? c : i18n.textNode(c));
  return n;
};
const state = { page: "", config: null, chart: null, data: null, events: new Map(), cursor: 0, stream: null, channels: [] };
let routeVersion = 0;
async function api(path, options = {}) {
  const r = await fetch("/api/" + path, { ...options, headers: { "Content-Type": "application/json", "X-OSBI-Local": "1", ...options.headers } });
  const data = await r.json();
  if (!r.ok) throw new Error(i18n.error(data.error || data.detail || "Request failed"));
  return data;
}
function toast(message) { const n = $("#toast"); text(n, message); n.hidden = false; setTimeout(() => n.hidden = true, 3500); }
function banner(message) {
  const n = $("#banner"); n.replaceChildren(); n.hidden = !message;
  if (message) n.append(i18n.textNode(message), el("a", { href: "/settings" }, t("Check settings →")));
}
function empty(title, text) { return el("div", { class: "empty" }, el("div", { class: "empty-symbol" }, "◈"), el("strong", {}, title), el("p", {}, text)); }
function heading(title, subtitle, actions = []) {
  return el("div", { class: "page-heading" }, el("div", {}, el("div", { class: "eyebrow" }, "OPEN-SOURCE BUSINESS INTELLIGENCE"), el("h1", {}, title), subtitle ? el("p", {}, subtitle) : null), el("div", { class: "row" }, actions));
}
function stats(items) {
  return el("div", { class: "stats" }, items.map(([symbol, label, value, word]) => el("div", { class: "stat" }, el("span", { class: "stat-icon" }, symbol), el("div", {}, el("div", { class: "stat-label" }, label), el("div", { class: "stat-value" + (word ? " word" : "") }, value)))));
}
function titlebar(title, controls = []) { return el("div", { class: "card-title" }, el("h2", {}, title), el("div", { class: "row" }, controls)); }
const names = { pending: t("Queued"), running: t("Collecting"), waiting_for_user: t("Waiting for your action"), waiting_server: t("Waiting for shared server"), interrupted: t("Interrupted"), succeeded: t("Completed"), failed: t("Failed"), cancelled: t("Cancelled"), graph_pending: t("Awaiting graph extraction"), retry: t("Retry later") };
function time(value) { return value ? i18n.date(value) : t("No completed data yet"); }
function remote(path, options) { return api("server/" + path, options); }
function safeUrl(url) { try { const u = new URL(url); return ["http:", "https:"].includes(u.protocol) ? u.href : "#"; } catch { return "#"; } }

async function graphPage(version) {
  const root = $("#content");
  const select = el("select", { "aria-label": t("Channel") });
  const search = el("input", { placeholder: t("Search entities…"), "aria-label": t("Search entities") });
  const refresh = el("button", { onclick: () => load(), class: "small" }, t("↻ Refresh"));
  const metric = el("div");
  const chartNode = el("div", { id: "chart" });
  const stage = el("div", { class: "graph-stage" }, chartNode);
  const foot = el("div", { class: "graph-foot" }, el("span", {}, t("Drag to pan · Scroll to zoom · Click for evidence")), el("span", { id: "graph-freshness" }, t("Waiting to connect")));
  const details = el("aside", { class: "card detail" }, el("div", { class: "detail-label" }, t("Entities and evidence")), empty(t("Select an entity"), t("Click a graph node to explore its relationships and original sources.")));
  const target = el("input", { placeholder: t("Target entity name"), "aria-label": t("Path target entity") });
  let channelId = "", selectedNode = null;
  const pathButton = el("button", { class: "small", onclick: async () => {
    if (!selectedNode || !target.value.trim()) return toast(t("Select a starting entity, then enter a target"));
    try {
      const candidates = state.data.nodes.filter(n => n.name === target.value.trim());
      if (candidates.length !== 1) return toast(t("Target missing or ambiguous. Check the entity name."));
      const data = await remote(`channels/${channelId}/query`, { method: "POST", body: JSON.stringify({ source: selectedNode.id, target: candidates[0].id, depth: 4 }) });
      if (!data.paths?.length) return toast(t("No supported relationship path found in this scope"));
      paint({ ...state.data, ...data });
    } catch (e) { banner(e.message); }
  } }, t("Find path"));
  root.replaceChildren(heading(t("Intelligence graph"), t("Explore connections, with evidence behind every insight."), [refresh]), metric,
    el("div", { class: "graph-layout" }, el("section", { class: "card graph-card" }, titlebar(t("Shared knowledge network"), [el("div", { class: "graph-tools" }, select, search)]), stage,
      el("div", { class: "card-title" }, el("span", { class: "hint" }, t("Select a node as the path start")), el("div", { class: "graph-tools" }, target, pathButton)), foot), details));
  function showNode(n) {
    selectedNode = n;
    details.replaceChildren(el("div", { class: "detail-label" }, t("Entity details")), el("h3", {}, n.name), el("span", { class: "tag" }, n.label),
      el("p", {}, n.identityStatus === "unresolved" ? t("This identity is unconfirmed across sources. Check the original evidence.") : t("Only entities and relationships supported by original evidence are shown.")), el("hr"), el("div", { class: "detail-label" }, t("Original evidence")));
    for (const id of n.blocks || []) {
      const b = state.data.blocks[id]; if (!b) continue;
      const section = el("div", { class: "source" });
      for (const src of b.sources || []) {
        section.append(el("a", { href: safeUrl(src.meta.finalUrl || src.meta.requestedUrl), target: "_blank", rel: "noopener noreferrer" }, src.title || t("View source ↗")), el("small", {}, [src.meta.publisher, src.meta.publishedAt].filter(Boolean).join(" · ")));
      }
      section.append(el("blockquote", {}, b.text)); details.append(section);
    }
  }
  function paint(data) {
    if (!state.chart) state.chart = echarts.init(chartNode, null, { renderer: "canvas" });
    stage.querySelector(".empty")?.remove();
    if (!data.nodes.length) stage.append(empty(t("No graph data in this channel yet"), t("Add a source in settings and collect it. Extracted entities and relationships will appear here.")));
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
      if (!channelId) throw new Error(t("No channels available for this customer key"));
      const data = await remote(`channels/${channelId}/graph`); if (version !== routeVersion) return;
      state.data = data; banner("");
      const pending = Object.entries(data.status.states || {}).filter(([k]) => !["succeeded", "failed"].includes(k)).reduce((s, [,v]) => s + v, 0);
      const mode = data.status.processingMode === "batch" ? t("Scheduled batch") : t("Continuous background extraction");
      metric.replaceChildren(stats([["◈", t("Entities"), data.nodes.length], ["↗", t("Relationships"), data.edges.length], ["◷", t("Pending tasks"), pending], ["≋", t("Processing mode"), mode, true]]));
      text($("#graph-freshness"), data.truncated ? t("Partial graph shown") : i18n.join([t("Last completed · "), time(data.status.completedWatermark)], ""));
      details.replaceChildren(el("div", { class: "detail-label" }, t("Channel information")), el("h3", {}, select.selectedOptions[0]?.textContent || channelId),
        el("p", {}, data.status.graphAllowed ? t("The server is receiving and processing material in the background.") : t("Waiting for processing window {window} ({timezone})", { window: data.status.batchWindow, timezone: data.status.timezone })),
        el("p", {}, t("Shared graph · Channels using the same schema show one graph. Counts are graph totals, not per-channel totals.")),
        el("p", {}, "Schema · " + data.schemaId.slice(0, 12)), el("hr"), el("div", { class: "detail-label" }, t("Select an entity to see evidence")));
      paint(data);
    } catch (e) {
      if (version !== routeVersion) return;
      banner(e.message); metric.replaceChildren(stats([["◈", t("Entities"), "—"], ["↗", t("Relationships"), "—"], ["◷", t("Pending tasks"), "—"], ["≋", t("Server status"), t("Disconnected"), true]]));
      if (!state.data) { stage.querySelector(".empty")?.remove(); stage.append(empty(t("Connect your intelligence network"), t("Enter the shared server URL and customer key in settings to explore channel graphs."))); }
    } finally { refresh.disabled = false; }
  }
  select.addEventListener("change", load);
  search.addEventListener("input", () => {
    if (!state.data) return;
    const q = search.value.trim().toLowerCase();
    const found = state.data.nodes.filter(n => n.name.toLowerCase().includes(q));
    if (!q) return;
    details.replaceChildren(el("div", { class: "detail-label" }, t("Search results \u00b7 {count}", { count: found.length })), el("div", { class: "entity-list" }, found.map(n => el("button", { onclick: () => showNode(n) }, n.name, el("span", {}, n.label)))));
  });
  await load();
}

function field(label, input, hint = "", full = false) { return el("div", { class: "field" + (full ? " full" : "") }, el("label", {}, label), input, hint ? el("small", {}, hint) : null); }
async function settingsPage(version) {
  const root = $("#content"); root.replaceChildren(heading(t("Settings"), t("Configure the server, sources and collection preferences. Report tasks are configured under Tasks.")), empty(t("Loading local settings…"), ""));
  try {
    const config = await api("config"); if (version !== routeVersion) return; state.config = config;
    try { const catalogue = await remote("channels"); if (version === routeVersion) state.channels = catalogue.items || []; } catch {}
    const serverUrl = el("input", { type: "url", value: config.serverUrl, placeholder: "https://your-server.example" });
    const key = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: config.keySuffix }) : t("Enter customer key") });
    const ca = el("input", { value: config.serverCA, placeholder: t("Optional: local CA certificate path") });
    const maxUrls = el("input", { type: "number", min: 1, max: 200, value: config.maxUrls });
    const concurrency = el("input", { type: "number", min: 1, max: 100, value: config.browserConcurrency });
    // Production DOMAIN_CONCURRENCY shape; legacy configs stored a single int.
    const domainPolicy = typeof config.domainConcurrency === "number"
      ? { defaultConcurrency: config.domainConcurrency, domains: {} }
      : (config.domainConcurrency || { defaultConcurrency: 1, domains: {} });
    const domainDefault = el("input", { type: "number", min: 1, max: 100, value: domainPolicy.defaultConcurrency ?? 1 });
    const domainLimits = el("textarea", { "aria-label": t("Domain concurrency JSON") }, JSON.stringify(domainPolicy.domains || {}, null, 2));
    const timeout = el("input", { type: "number", min: 10, max: 300, value: config.timeoutSeconds });
    const submitConcurrency = el("input", { type: "number", min: 1, max: 32, value: config.submissionConcurrency ?? 4 });
    const submitBacklog = el("input", { type: "number", min: 1, max: 2000, value: config.maxPendingSubmissions ?? 100 });
    const forbidden = el("input", { value: config.forbiddenDomains.join(", "), placeholder: t("Blocked URL fragments, comma-separated, e.g. www.gov.cn/home") });
    const captureTtl = el("input", { type: "number", min: 0, max: 31536000, value: config.captureCache?.defaultTtlSeconds ?? 21600 });
    const domainTtl = el("textarea", { "aria-label": t("Domain cache TTL JSON") }, JSON.stringify(config.captureCache?.domainTtlSeconds || {}, null, 2));
    const submitTtl = el("input", { type: "number", min: 0, max: 31536000, value: config.submissionCache?.ttlSeconds ?? 604800 });
    const visionEnabled = el("input", { type: "checkbox" }); visionEnabled.checked = config.vision.enabled;
    const visionUrl = el("input", { type: "url", value: config.vision.baseUrl });
    const visionKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.vision.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: config.vision.keySuffix }) : t("Separate vision model key") });
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
      if (!sources.length) sourceContainer.append(empty(t("Add your first source"), t("Public web pages, RSS and WeChat public accounts are supported. Use the management skill for complex workflows.")));
      for (const [i, s] of sources.entries()) {
        const isMp = s.kind === "mp";
        const id = el("input", { value: s.id, placeholder: "source-1" });
        const url = el("input", { type: "url", value: isMp ? MP_SENTINEL : s.url, placeholder: "https://example.com/news" });
        if (isMp) url.setAttribute("readonly", "");
        const kind = el("select", {}, el("option", { value: "web" }, t("Web page")), el("option", { value: "rss" }, "RSS"), el("option", { value: "mp" }, t("WeChat public account"))); kind.value = s.kind;
        const interval = el("input", { type: "number", min: 0, value: s.intervalMinutes });
        const schedule = s.schedule ||= { times: [], timezone: "Asia/Shanghai" };
        const captureTimes = el("div", { class: "capture-times" });
        function renderCaptureTimes() {
          interval.disabled = schedule.times.length > 0;
          captureTimes.replaceChildren(...schedule.times.map((clock, index) =>
            el("div", { class: "row" }, el("input", {
              type: "time", value: clock, step: 60,
              "aria-label": t("Capture time {number}", { number: index + 1 }),
              oninput: event => schedule.times[index] = event.target.value,
            }), el("button", { class: "small danger", onclick: () => {
              schedule.times.splice(index, 1); renderCaptureTimes();
            } }, t("Remove time")))));
          const add = el("button", { class: "small", onclick: () => {
            const options = ["08:00", "12:00", "18:00", ...Array.from({ length: 48 }, (_, i) =>
              `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`)];
            schedule.times.push(options.find(clock => !schedule.times.includes(clock)));
            renderCaptureTimes();
          } }, t("+ Add capture time"));
          add.disabled = schedule.times.length >= 48;
          captureTimes.append(add);
        }
        renderCaptureTimes();
        const captureTimezone = el("input", { value: schedule.timezone,
          oninput: event => schedule.timezone = event.target.value, placeholder: "Asia/Shanghai" });
        const savedSource = config.sources.find(source => source.id === s.id);
        const nextAt = config.sourceNextRuns?.[s.id];
        const nextCapture = !s.enabled ? t("Source disabled") : nextAt
          ? i18n.date(nextAt, { timeZone: savedSource?.schedule?.timezone || "Asia/Shanghai" })
          : t("No scheduled capture");
        const profile = el("input", { value: s.profile || "default" });
        const enabled = el("input", { type: "checkbox" }); enabled.checked = s.enabled;
        const accounts = el("textarea", { "aria-label": t("Public account allowlist") }, (s.accounts || []).join("\n"));
        const steps = el("textarea", { "aria-label": t("Workflow JSON") }, JSON.stringify(s.steps || [], null, 2));
        s.channels = s.channels || [];
        const channels = channelSelect(s.channels, next => s.channels = next, t("Source channels"));
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
        const run = el("button", { class: "small", onclick: async () => { try { const r = await api("runs", { method: "POST", body: JSON.stringify({ sourceId: s.id }) }); toast(i18n.join([t("Task created · "), r.id.slice(-8)], "")); } catch (e) { toast(e.message); } } }, t("Run"));
        const fields = [field(t("Source ID"), id), field(t("Type"), kind), field(t("Collection interval / minutes"), interval, t("Used only without daily capture times; 0 means manual only.")), field(t("Browser profile"), profile),
          field(t("Daily capture times"), captureTimes, t("Add multiple daily times. Daily times take priority over the interval."), true),
          field(t("Capture time zone"), captureTimezone, t("IANA time zone for the daily times, for example Asia/Shanghai.")),
          field(t("Next scheduled capture"), el("span", { class: "hint" }, nextCapture), t("Saved schedule; updates after saving. The backend must remain running.")),
          field(t("Submission channels"), channels, t("Select multiple channels, or none to collect without submitting. The server provides the channel list."), true)];
        if (isMp) {
          fields.push(field(t("Public account allowlist"), accounts, t("One account per line, matching its exact display name in local WeChat. Accounts with no matches are reported in notifications."), true));
          fields.push(field(t("Source URL"), url, t("Fixed entry for public accounts; the browser captures article bodies."), true));
        } else {
          fields.push(field(t("Source URL"), url, t("The workflow starts at this URL."), true));
        }
        const card = el("div", { class: "source-edit" }, el("div", { class: "row" }, el("label", { class: "row hint" }, enabled, t("Enable source")), el("div", { class: "row" }, run, el("button", { class: "small danger", onclick: () => { sources.splice(i, 1); renderSources(); } }, t("Remove")))),
          el("div", { class: "form-grid" }, fields));
        if (!isMp) card.append(el("details", {}, el("summary", {}, t("Advanced: programmable workflow")), el("p", { class: "hint" }, t("Use the management skill to generate steps, or leave blank for direct collection.")), steps));
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
        i18n.property(key, "placeholder", result.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: result.keySuffix }) : t("Enter customer key"));
        i18n.property(visionKey, "placeholder", result.vision.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: result.vision.keySuffix }) : t("Separate vision model key"));
        banner(""); toast(t("Settings saved for future tasks"));
        if (onSuccess) onSuccess(result);
      } catch (e) { toast(e instanceof SyntaxError ? t("Invalid JSON in this section. Check the format.") : e.message); }
      finally { button.disabled = false; }
    }
    const saveServer = el("button", { class: "small primary", onclick: () => saveSection(saveServer, () => {
      const update = { serverUrl: serverUrl.value.trim(), serverCA: ca.value.trim() };
      if (key.value) update.serverKey = key.value;
      return update;
    }, () => { key.value = ""; }) }, t("Save"));
    const saveSources = el("button", { class: "small primary", onclick: () => saveSection(saveSources, () => {
      const cleanSources = sources.map(({ stepsText, ...s }) => {
        const steps = stepsText !== undefined ? JSON.parse(stepsText) : (s.steps || []);
        if (s.kind === "mp") return { ...s, url: MP_SENTINEL, steps: [], accounts: s.accounts || [] };
        return { ...s, steps, accounts: [] };
      });
      return { sources: cleanSources };
    }, result => {
      config.sources = result.sources;
      config.sourceNextRuns = result.sourceNextRuns || {};
      sources = structuredClone(result.sources);
      renderSources();
    }) }, t("Save"));
    const savePrefs = el("button", { class: "small primary", onclick: () => saveSection(savePrefs, () => ({
      maxUrls: Number(maxUrls.value), browserConcurrency: Number(concurrency.value),
      domainConcurrency: { defaultConcurrency: Number(domainDefault.value), domains: JSON.parse(domainLimits.value) },
      timeoutSeconds: Number(timeout.value), headless: true,
      submissionConcurrency: Number(submitConcurrency.value), maxPendingSubmissions: Number(submitBacklog.value),
      forbiddenDomains: forbidden.value.split(",").map(x => x.trim()).filter(Boolean),
      captureCache: { defaultTtlSeconds: Number(captureTtl.value), domainTtlSeconds: JSON.parse(domainTtl.value) },
      submissionCache: { ttlSeconds: Number(submitTtl.value) },
    })) }, t("Save"));
    const saveVision = el("button", { class: "small primary", onclick: () => saveSection(saveVision, () => {
      const vision = { enabled: visionEnabled.checked, baseUrl: visionUrl.value.trim(), model: visionModel.value.trim(),
        fallbackModel: visionFallback.value.trim(), extraModel: visionExtra.value.trim(),
        concurrency: Number(visionConcurrency.value), timeoutSeconds: Number(visionTimeout.value) };
      if (visionKey.value) vision.apiKey = visionKey.value;
      return { vision };
    }, () => { visionKey.value = ""; }) }, t("Save"));
    const forms = el("div", {}, el("div", { class: "note" }, t("Settings are local and each section saves independently. The backend holds customer keys. Saving does not send test messages or probe platform accounts.")),
      el("section", { class: "card form-card" }, titlebar(t("01 · Shared server"), [saveServer]), el("div", { class: "form-body form-grid" }, field(t("Service URL"), serverUrl, t("The shared server must use HTTPS.")), field(t("Customer key"), key, t("Leave blank to retain the current key. Its value is never returned.")), field(t("Custom CA"), ca, t("Leave blank for a public certificate."), true))),
      el("section", { class: "card form-card" }, titlebar(t("02 · Collection sources"), [el("button", { class: "small", onclick: () => { sources.push({ id: "source-" + (sources.length + 1), url: "", kind: "web", enabled: true, intervalMinutes: 0, profile: "default", steps: [], channels: [] }); renderSources(); } }, t("+ Add source")), saveSources]), el("div", { class: "form-body" }, el("p", { class: "hint" }, t("The shared server handles relevance and ontology extraction by channel. A source can submit to multiple channels.")), el("div", { style: "height:12px" }), sourceContainer)),
      el("section", { class: "card form-card" }, titlebar(t("03 · Collection preferences"), [savePrefs]), el("div", { class: "form-body form-grid" }, field(t("Maximum pages per run"), maxUrls), field(t("Global page concurrency"), concurrency, t("Capacity is at least the number of active profiles; domain limits apply across all profiles.")), field(t("Default per-domain concurrency"), domainDefault, t("Used for domains without an override; follows the DOMAIN_CONCURRENCY policy.")), field(t("Network timeout / seconds"), timeout), field(t("Concurrent submissions"), submitConcurrency), field(t("Pending submission limit"), submitBacklog, t("Pause new page captures while this many submissions are pending.")), el("p", { class: "hint full" }, t("Profiles start headless with parallel pages. Verification uses the same profile in a visible browser. Each source run lasts up to 3 hours; manual waits last up to 30 minutes.")),
        field(t("Per-domain concurrency overrides"), domainLimits, t("JSON object, e.g. {\"mp.weixin.qq.com\": 2, \"gov.cn\": 3}. Keys normalize to the main domain (www.gov.cn → gov.cn); maximum 100."), true),
        field(t("Excluded URL fragments"), forbidden, t("URL substring matching: use a domain or domain/path prefix (e.g. www.gov.cn/home). Matches are skipped; source start URLs are exempt."), true),
        field(t("Page cache TTL / seconds"), captureTtl, t("Default 21600 seconds (6 hours); 0 disables caching. TTL starts at the actual capture time.")),
        field(t("Submission cache TTL / seconds"), submitTtl, t("Reuse results for the same server and processing revision; 0 disables caching.")),
        field(t("Domain cache TTL overrides"), domainTtl, t("JSON object, e.g. {\"news.example.com\": 1800}. More specific subdomains take precedence."), true))),
      el("section", { class: "card form-card" }, titlebar(t("04 · Image recognition (VL)"), [el("label", { class: "jev-switch" }, visionEnabled, el("span", {}, t("Enable image recognition"))), saveVision]), el("div", { class: "form-body form-grid" },
        field(t("Service URL"), visionUrl, t("OpenAI-compatible vision endpoint; it must be able to fetch public image URLs.")),
        field(t("Vision key"), visionKey, t("Stored locally and never sent to the shared server. Required for remote recognition.")),
        field(t("Primary model"), visionModel), field(t("Fallback model"), visionFallback, t("Optional.")),
        field(t("Supplementary model"), visionExtra, t("Optional.")), field(t("Concurrency"), visionConcurrency, t("1–6, shared across tasks.")),
        field(t("Timeout / seconds"), visionTimeout, t("Per-image timeout; each page also has a 300-second budget. Timeout fallback preserves the page."), true))));

    root.replaceChildren(heading(t("Sources"), t("Configure the shared server, collection sources, browser preferences and vision models.")), el("div", { class: "form-layout" }, el("aside", { class: "section-index" }, el("div", {}, t("◈ Shared server")), el("div", {}, t("↗ Collection sources")), el("div", {}, t("⚙ Collection preferences")), el("div", {}, t("▣ Image recognition"))), forms));
  } catch (e) { banner(e.message); }
}

async function queryPage(version) {
  const root = $("#content");
  root.replaceChildren(heading(t("Query settings"), t("Jev post-filtering and a local model shared by TopNews, Weeklyreport and local queries.")), empty(t("Loading model settings…"), ""));
  const config = await api("config"); if (version !== routeVersion) return; state.config = config;
  const jevEnabled = el("input", { type: "checkbox" }); jevEnabled.checked = config.jev.enabled;
  const jevPost = el("input", { type: "checkbox" }); jevPost.checked = config.jev.postQuery;
  const jevUrl = el("input", { type: "url", value: config.jev.baseUrl });
  const jevModel = el("input", { value: config.jev.model });
  const jevKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.jev.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: config.jev.keySuffix }) : t("Separate Jev key") });
  const jevConfidence = el("input", { type: "number", min: 0, max: 1, step: .05, value: config.jev.discardConfidence });
  const reportEnabled = el("input", { type: "checkbox" }); reportEnabled.checked = config.reportLlm.enabled;
  const reportUrl = el("input", { type: "url", value: config.reportLlm.baseUrl });
  const reportModelInput = el("input", { value: config.reportLlm.model });
  const reportKey = el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: config.reportLlm.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: config.reportLlm.keySuffix }) : t("Local xin_local needs no key; remote endpoints do") });
  const reportTimeout = el("input", { type: "number", min: 10, max: 600, value: config.reportLlm.timeoutSeconds });
  const queryScope = el("select", {}, el("option", { value: "all" }, t("Contributions from all nodes")), el("option", { value: "own" }, t("Only this node’s sources")));
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
      i18n.property(jevKey, "placeholder", result.jev.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: result.jev.keySuffix }) : t("Separate Jev key"));
      i18n.property(reportKey, "placeholder", result.reportLlm.keyConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: result.reportLlm.keySuffix }) : t("Local xin_local needs no key; remote endpoints do"));
      toast(t("Query settings saved")); if (onSuccess) onSuccess();
    } catch (e) { toast(e.message); }
    finally { button.disabled = false; }
  }
  const saveJev = el("button", { class: "small primary", onclick: () => save(saveJev, () => {
    const jev = { enabled: jevEnabled.checked, postQuery: jevPost.checked,
      baseUrl: jevUrl.value.trim(), model: jevModel.value.trim(), discardConfidence: Number(jevConfidence.value) };
    if (jevKey.value) jev.apiKey = jevKey.value; return { jev };
  }, () => { jevKey.value = ""; }) }, t("Save"));
  const saveScope = el("button", { class: "small primary", onclick: () => save(saveScope, () => ({ queryDefaults: { sourceScope: queryScope.value } })) }, t("Save"));
  const saveReport = el("button", { class: "small primary", onclick: () => save(saveReport, () => {
    const reportLlm = { enabled: reportEnabled.checked, baseUrl: reportUrl.value.trim(),
      model: reportModelInput.value.trim(), timeoutSeconds: Number(reportTimeout.value) };
    if (reportKey.value) reportLlm.apiKey = reportKey.value; return { reportLlm };
  }, () => { reportKey.value = ""; }) }, t("Save"));
  root.replaceChildren(heading(t("Query settings"), t("Jev post-filtering and a local model shared by TopNews, Weeklyreport and local queries.")),
    el("div", { class: "note" }, t("Both run locally: Jev filters retrieved evidence; the local model edits TopNews, writes Weeklyreport and assists local queries. These settings do not control server relevance.")),
    el("section", { class: "card form-card" }, titlebar(t("01 · Query scope"), [saveScope]), el("div", { class: "form-body form-grid" },
      field(t("Default source scope"), queryScope, t("all retrieves this channel’s contributions from every node (default); own retrieves only this node’s sources. Sections and task boards can override this.")))),
    el("section", { class: "card form-card" }, titlebar(t("02 · Jev post-filtering"), [el("label", { class: "jev-switch" }, jevEnabled, el("span", {}, t("Enable Jev"))), saveJev]), el("div", { class: "form-body form-grid" },
      field(t("Filter retrieved evidence"), jevPost, t("Runs only after evidence retrieval, never during collection or submission.")),
      field(t("Jev service URL"), jevUrl), field(t("Model"), jevModel), field("Jev Key", jevKey, t("Stored locally, never sent to the shared server.")),
      field(t("Minimum discard confidence"), jevConfidence, t("Keep material when confidence is low or the service is unavailable."), true))),
    el("section", { class: "card form-card" }, titlebar(t("03 · Local report/query model"), [el("label", { class: "jev-switch" }, reportEnabled, el("span", {}, t("Enable local model"))), saveReport]), el("div", { class: "form-body form-grid" },
      field(t("Service URL"), reportUrl, t("Default: local xinference at http://127.0.0.1:9997/v1.")),
      field(t("Model"), reportModelInput, t("Default: xin_local, shared by TopNews editing, Weeklyreport writing and local queries.")),
      field("API Key", reportKey, t("No key needed locally; remote HTTPS endpoints need a key, stored on this client.")),
      field(t("Timeout / seconds"), reportTimeout, t("Per-call timeout. Model failures fall back to deterministic selection and formatting."), true))));
}

function nextId(items, prefix) {
  let i = 1; while (items.some(item => item.id === prefix + i)) i++;
  return prefix + i;
}
function splitList(value) {
  return String(value || "").split(/[,，\n]/).map(item => item.trim()).filter(Boolean);
}
function channelSelect(selected, onChange, ariaLabel = t("Channel")) {
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
const TARGET_TYPE_LABELS = {"wechat-linux-container": t("wechat-linux-container"), awada: "Awada", "wecom-webhook": t("WeCom webhook")};
const SELECTORS = {
  candidates: t("Candidate pool + editor model (TopNews)"),
  action: t("Action hotspot network (Weekly)"),
  policy: t("Policy–action chain (Weekly)"),
  evidence: t("Original evidence by date (general)"),
};

async function tasksPage(version) {
  const root = $("#content");
  root.replaceChildren(heading(t("Tasks"), t("Configure sections, TopNews/Weeklyreport, schedules and delivery targets.")), empty(t("Loading task configuration…"), ""));
  const config = await api("config"); if (version !== routeVersion) return;
  let profiles = structuredClone(config.reportProfiles);
  let nodeRules = structuredClone(config.nodeRules || {});
  try { const catalogue = await remote("channels"); if (version === routeVersion) state.channels = catalogue.items || []; } catch {}
  let selectedProfile = profiles[0]?.id || "";
  const profileBar = el("div", { class: "row", style: "margin-bottom:16px" });
  const body = el("div");
  const current = () => profiles.find(profile => profile.id === selectedProfile) || profiles[0];
  async function save(button, message = t("Task configuration saved")) {
    button.disabled = true;
    try {
      await api("config", { method: "PATCH", body: JSON.stringify({ reportProfiles: profiles, nodeRules }) });
      toast(message); render();
    } catch (e) { toast(e.message); }
    finally { button.disabled = false; }
  }
  function sectionCard(section, index, remove) {
    const channelPicker = channelSelect(section.channels, next => section.channels = next, t("Section channels"));
    const territory = el("input", { value: (section.query.territoryPreference || []).join(", "), placeholder: t("City, district, neighborhood") });
    territory.addEventListener("input", () => section.query.territoryPreference = splitList(territory.value));
    const territoryMode = el("select", {}, el("option", { value: "prefer" }, t("Prefer, without excluding others")), el("option", { value: "only" }, t("Only this locality")));
    territoryMode.value = section.query.territoryMode || "prefer";
    territoryMode.addEventListener("change", () => section.query.territoryMode = territoryMode.value);
    const jev = el("select", {}, el("option", { value: "inherit" }, t("Use global setting")), el("option", { value: "on" }, t("Enabled")), el("option", { value: "off" }, t("Off")));
    jev.value = section.localRules.jev || "inherit";
    jev.addEventListener("change", () => section.localRules.jev = jev.value);
    const scope = el("select", {}, el("option", { value: "inherit" }, t("Use query settings")), el("option", { value: "all" }, t("Contributions from all nodes")), el("option", { value: "own" }, t("Only this node’s sources")));
    scope.value = section.query.sourceScope || "inherit";
    scope.addEventListener("change", () => section.query.sourceScope = scope.value);
    return el("div", { class: "source-card" },
      el("div", { class: "row" }, el("strong", {}, `Sections ${index + 1}`), el("button", { class: "small danger", onclick: remove }, t("Remove section"))),
      el("div", { class: "form-grid" },
        field(t("Section ID"), bindValue(section, "id", el("input", { value: section.id }))),
        field(t("Section title"), bindValue(section, "title", el("input", { value: section.title }))),
        field(t("Query channels"), channelPicker, t("Select multiple channels to combine their evidence in one section."), true),
        field(t("Query text"), bindValue(section.query, "text", el("input", { value: section.query.text || "" })), t("Server-side title/body filtering; blank retrieves the whole channel.")),
        field(t("Locality preference"), territory, t("Soft query preference, comma-separated; change it to any region.")),
        field(t("Locality mode"), territoryMode),
        field(t("Query scope"), scope, t("inherit follows the default in Query settings.")),
        field(t("Content types"), el("input", { value: (section.query.contentTypes || []).join(", "), "oninput": event => section.query.contentTypes = splitList(event.target.value) }), t("For example: policy, news, activity. Blank means any type.")),
        field(t("Local keywords"), el("input", { value: (section.localRules.keywords || []).join(", "), "oninput": event => section.localRules.keywords = splitList(event.target.value) }), t("After retrieval, keep material containing any of these keywords.")),
        field(t("Excluded keywords"), el("input", { value: (section.localRules.excludeKeywords || []).join(", "), "oninput": event => section.localRules.excludeKeywords = splitList(event.target.value) })),
        field(t("Jev filter"), jev, t("on/off overrides the global post-query setting.")))
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
    const scope = el("select", {}, el("option", { value: "inherit" }, t("Use section/global setting")), el("option", { value: "all" }, t("Contributions from all nodes")), el("option", { value: "own" }, t("Only this node’s sources")));
    scope.value = board.sourceScope || "inherit";
    scope.addEventListener("change", () => board.sourceScope = scope.value);
    const fields = [field("Section", sectionSelect),
      field(t("Query scope"), scope, t("inherit uses the section setting, then the global query scope.")),
      field(t("Item count"), bindValue(board, "topN", el("input", { type: "number", min: 1, max: 20, value: board.topN }), Number)),
      field(t("Minimum relationship edges"), bindValue(board, "minEdges", el("input", { type: "number", min: 1, max: 100, value: board.minEdges }), Number)),
      field(t("System prompt override"), bindValue(board, "prompt", el("textarea", {}, board.prompt || "")), t("Replaces the default system prompt. Leave blank for built-in defaults. Supports {top_n}/{board}/{locality_rules}."), true)];
    if (task !== "topnews") fields.splice(1, 0, field(t("Selection method"), selector, t("Weekly uses action/policy selection; evidence retrieves original text.")));
    return el("div", { class: "source-card" },
      el("div", { class: "row" }, el("strong", {}, t("Board {number}", { number: index + 1 })), el("button", { class: "small danger", onclick: remove }, t("Remove"))),
      el("div", { class: "form-grid" }, ...fields)
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
    const weekday = el("select", {}, el("option", { value: "" }, t("Every day")), ...["monday","tuesday","wednesday","thursday","friday","saturday","sunday"].map(value => el("option", { value }, t(value[0].toUpperCase() + value.slice(1)))));
    weekday.value = task.schedule.weekday || "";
    weekday.addEventListener("change", () => task.schedule.weekday = weekday.value || null);
    const timezone = el("input", { value: task.schedule.timezone || "Asia/Shanghai" });
    bindValue(task.schedule, "timezone", timezone);
    const targets = el("div", { class: "form-grid" });
    if (!profile.targets.length) targets.append(el("p", { class: "hint" }, t("Manage delivery targets on the Outputs page. Dry-run still generates reports.")));
    for (const target of profile.targets) {
      const box = el("input", { type: "checkbox" }); box.checked = (task.targetIds || []).includes(target.id);
      box.addEventListener("change", () => {
        task.targetIds = box.checked ? [...new Set([...(task.targetIds || []), target.id])] : (task.targetIds || []).filter(id => id !== target.id);
      });
      targets.append(el("label", { class: "row hint" }, box, i18n.join([target.label || target.id, TARGET_TYPE_LABELS[target.type] || target.type])));
    }
    const manual = el("button", { class: "small", onclick: async () => {
      // Omit the window so both manual buttons use the same backend defaults
      // as Outputs: TopNews yesterday→today, Weeklyreport 7-day lookback.
      const body = { profile: profile.id };
      try { await api(`${taskName === "topnews" ? "topnews" : "weekly"}/run`, { method: "POST", body: JSON.stringify(body) }); toast(t("Report task created. View its run logs on the Outputs page.")); }
      catch (e) { toast(e.message); }
    } }, t("Run manually"));
    const saveButton = el("button", { class: "small primary", onclick: () => save(saveButton) }, t("Save"));
    const taskPrompt = el("textarea", {}, task.prompt || "");
    taskPrompt.addEventListener("input", () => task.prompt = taskPrompt.value);
    const sendFileBox = taskName === "weeklyreport" ? el("input", { type: "checkbox" }) : null;
    const docxFilename = taskName === "weeklyreport" ? el("input", {
      value: task.docxFilename ?? "基层工作动态回顾与热点展望_图谱版", placeholder: "Weeklyreport",
      "oninput": event => task.docxFilename = event.target.value,
    }) : null;
    if (sendFileBox) {
      sendFileBox.checked = task.sendFile !== false;
      sendFileBox.addEventListener("change", () => task.sendFile = sendFileBox.checked);
    }
    const penaltyReset = taskName === "topnews" ? el("button", { class: "small danger", onclick: async () => {
      if (!confirm(t("Clear the publisher penalty ledger?"))) return;
      try { await api("reports/publisher-penalty/reset", { method: "POST" }); toast(t("Publisher penalty ledger cleared")); }
      catch (e) { toast(e.message); }
    } }, t("Clear publisher penalties")) : null;
    const addBoard = el("button", { class: "small", onclick: () => {
      const available = profile.sections.find(section => !task.boards.some(board => board.section === section.id));
      if (!available) return toast(t("All sections have already been added to this task"));
      task.boards.push({ section: available.id, selector: taskName === "topnews" ? "candidates" : "action", topN: taskName === "topnews" ? 3 : 2, minEdges: 4, purpose: "", prompt: "", sourceScope: "inherit" });
      render();
    } }, t("+ Add board"));
    return el("section", { class: "card form-card" }, titlebar(title, [el("label", { class: "jev-switch" }, enabled, el("span", {}, t("Enabled"))), el("label", { class: "jev-switch" }, dryRun, el("span", {}, "Dry-run")), penaltyReset, manual, saveButton]),
      el("div", { class: "form-body" }, el("div", { class: "form-grid" }, field(t("Delivery time"), timeInput, "HH:MM"), field(t("Weekday"), weekday, t("Select one weekday for a weekly task, or Every day for a daily task.")), field(t("Time zone"), timezone),
        ...(sendFileBox ? [field(t("Document delivery"), el("label", { class: "jev-switch" }, sendFileBox, el("span", {}, t("Send DOCX file"))), t("Also send DOCX outside dry-run. wechat-linux-container reports file_not_supported; Awada supports files."))] : []),
        ...(docxFilename ? [field(t("DOCX filename"), docxFilename, t("Base name only. _YYYYMMDD.docx is appended automatically; Markdown and digest filenames stay the same."))] : []),
        field(taskName === "topnews" ? t("Do you Know fact selection prompt") : t("Analysis/digest prompt"), taskPrompt, t("Replaces the default task prompt. Leave blank for built-in defaults."), true)),
        el("div", { class: "card-title" }, el("h3", {}, t("Delivery targets")), addBoard),
        targets,
        ...task.boards.map((board, index) => boardCard(board, index, taskName, () => { task.boards.splice(index, 1); render(); }))));
  }
  function render() {
    const scroll = Number(window.scrollY || 0);
    const profile = current();
    profileBar.replaceChildren(...profiles.map(item => el("button", { class: item.id === profile.id ? "small primary" : "small", onclick: () => { selectedProfile = item.id; render(); } }, item.name || item.id)));
    const sectionSave = el("button", { class: "small primary", onclick: () => save(sectionSave, t("Sections saved")) }, t("Save sections"));
    const outputSave = el("button", { class: "small primary", onclick: () => save(outputSave, t("Output copy saved")) }, t("Save output copy"));
    body.replaceChildren(
      el("section", { class: "card form-card" }, titlebar(t("Profile"), []), el("div", { class: "form-grid" },
        field(t("Profile ID"), bindValue(profile, "id", el("input", { value: profile.id }))), field(t("Profile name"), bindValue(profile, "name", el("input", { value: profile.name })))),
        el("p", { class: "hint" }, t("A profile combines shared sections and two tasks. All settings stay on this client."))),
      el("section", { class: "card form-card" }, titlebar(t("Sections · Shared templates"), [el("button", { class: "small", onclick: () => { profile.sections.push({ id: nextId(profile.sections, "section-"), title: String(t("New section")), channels: (state.channels || []).slice(0, 1).map(c => c.id), query: { text: "", entityUris: [], contentTypes: [], territoryPreference: structuredClone(profile.sections[0]?.query?.territoryPreference || []), territoryMode: "prefer", sourceScope: "inherit" }, localRules: { keywords: [], excludeKeywords: [], leaderBoost: true, localityPenalty: true, jev: "inherit" } }); render(); } }, t("+ Add section")), sectionSave]), el("div", { class: "form-body" }, el("p", { class: "hint" }, t("Sections define shared channels, queries, locality preferences and local filters. Task boards reference sections and override item counts and prompts without changing the section.")), ...profile.sections.map((section, index) => sectionCard(section, index, () => { profile.sections.splice(index, 1); render(); })))),
      taskCard("topnews", t("TopNews · Do you Know")),
      taskCard("weeklyreport", t("Weeklyreport · Weekly insights")),
      el("section", { class: "card form-card" }, titlebar(t("Output copy"), [outputSave]), el("div", { class: "form-body form-grid" },
        field(t("TopNews introduction"), bindValue(nodeRules, "topnewsShout", el("input", { value: nodeRules.topnewsShout || "" })), t("Shown after the date, e.g. your daily briefing…")),
        field(t("TopNews closing text"), bindValue(nodeRules, "topnewsSuffix", el("textarea", {}, nodeRules.topnewsSuffix || "")), t("Appended to the end of the briefing."), true),
        field(t("Weekly introduction template"), bindValue(nodeRules, "weeklyIntro", el("textarea", {}, nodeRules.weeklyIntro || "")), t("Supports {date} and {digest}; blank uses the built-in template."), true))),
      );
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => window.scrollTo(0, scroll));
  }
  render();
  root.replaceChildren(heading(t("Tasks"), t("Sections, TopNews, Weeklyreport and schedules.")), profileBar, body);
}

async function notificationsPage(version) {
  const root = $("#content");
  const filter = el("select", { "aria-label": t("Notification level") }, el("option", { value: "all" }, t("All notifications")), el("option", { value: "warning" }, t("Warnings")), el("option", { value: "error" }, t("Errors")));
  const list = el("div", { class: "event-list" });
  const prompts = el("div");
  let liveStatus = t("Connecting to live updates…");
  const live = el("span", { class: "hint" }, liveStatus);
  const metric = el("div");
  function render() {
    const values = [...state.events.values()].filter(e => filter.value === "all" || e.level === filter.value).reverse();
    const entries = values.map(e => el("div", { class: "event " + e.level },
      el("span", { class: "event-dot" }),
      el("div", {}, el("div", { class: "event-message" }, e.kind.startsWith("source_schedule") ? i18n.join([t(e.message), e.details?.sourceId || ""]) : e.message), el("div", { class: "event-meta" }, e.run_id || e.kind)),
      el("time", {}, time(e.at))));
    list.replaceChildren(...entries);
    if (!values.length) list.append(empty(t("No notifications yet"), t("Start a collection task to follow progress and pending actions here.")));
    metric.replaceChildren(stats([["◷", t("Visible notifications"), values.length], ["!", t("Warnings"), values.filter(e => e.level === "warning").length], ["×", t("Errors"), values.filter(e => e.level === "error").length], ["≋", t("Connection status"), liveStatus, true]]));
  }
  async function loadPrompts() {
    try {
      const result = await api("runs"); if (version !== routeVersion) return;
      const items = result.items.filter(r => ["waiting_for_user", "interrupted"].includes(r.state));
      prompts.replaceChildren(...items.map(r => el("div", { class: "prompt-card" }, el("h3", {}, t("Action required")), el("p", {}, r.error || t("Check the browser task before continuing.")), el("small", {}, `${r.source?.id || r.id} · ${r.source?.profile || "default"}`), r.promptUrl ? el("p", { class: "hint" }, r.promptUrl) : null,
        el("div", { class: "row" }, el("button", { class: "small", onclick: () => action(r, "resume") }, t("Done, continue")), el("button", { class: "small", onclick: () => action(r, "cancel") }, t("Cancel task"))))));
      if (!items.length) prompts.append(empty(t("No pending actions"), t("Tasks wait here when login or verification is needed.")));
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
  root.replaceChildren(heading(t("Notifications"), t("Follow collection progress, run history and actions that need your attention.")), metric,
    el("div", { class: "event-layout" }, el("section", { class: "card" }, titlebar(t("Task notifications"), [live, filter, el("button", { class: "small", onclick: history }, t("↻ History")), el("button", { class: "small", onclick: () => { state.events.clear(); render(); } }, t("Clear view"))]), list), el("aside", { class: "card" }, titlebar(t("Pending actions")), prompts)));
  await history(); if (version !== routeVersion) return;
  state.stream = new EventSource(`/api/events/stream?after=${state.cursor}`);
  state.stream.onopen = () => { text(live, liveStatus = t("Live updates connected")); render(); };
  state.stream.onerror = () => { text(live, liveStatus = t("Reconnecting…")); render(); };
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
  const config = await api("config"); if (version !== routeVersion) return;
  const root = $("#content");
  const profiles = structuredClone(config.reportProfiles);
  const profile = profiles[0];
  const targetList = el("div");
  async function saveTargets(button) {
    button.disabled = true;
    try {
      await api("config", { method: "PATCH", body: JSON.stringify({ reportProfiles: profiles }) });
      toast(t("Delivery targets saved")); renderTargets();
    } catch (e) { toast(e.message); }
    finally { button.disabled = false; }
  }
  function renderTargets() {
    const add = el("button", { class: "small", onclick: () => {
      profile.targets.push({ id: nextId(profile.targets, "target-"), type: "wechat-linux-container",
                             label: String(t("wechat-linux-container")), enabled: false, config: {} });
      renderTargets();
    } }, t("+ Add delivery target"));
    targetList.replaceChildren(
      el("p", { class: "hint" }, t("wechat-linux-container and Awada are available. WeCom webhooks are reserved and return not_implemented. wechat-linux-container does not support files. Secrets are write-only; blank preserves stored values.")),
      ...profile.targets.map((target, index) => {
        const type = el("select", {}, el("option", { value: "wechat-linux-container" }, t("wechat-linux-container")),
                        el("option", { value: "awada" }, "Awada"),
                        el("option", { value: "wecom-webhook" }, t("WeCom webhook (not implemented)")));
        type.value = target.type;
        type.addEventListener("change", () => { target.type = type.value; target.config = {}; renderTargets(); });
        const fields = [field(t("Target ID"), bindValue(target, "id", el("input", { value: target.id }))),
                        field(t("Label"), bindValue(target, "label", el("input", { value: target.label || "" })))];
        const enabled = el("input", { type: "checkbox" }); enabled.checked = target.enabled;
        enabled.addEventListener("change", () => target.enabled = enabled.checked);
        const configFields = [];
        if (target.type === "wechat-linux-container") {
          configFields.push(field(t("Service URL"), el("input", { value: target.config.baseUrl || "", "oninput": event => target.config.baseUrl = event.target.value })));
          configFields.push(field(t("Group name"), el("input", { value: target.config.window || "", "oninput": event => target.config.window = event.target.value })));
          configFields.push(field("Token", el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: target.config.tokenConfigured ? t("Configured · ending {suffix}; leave blank to keep", { suffix: target.config.tokenSuffix || "" }) : t("Enter token"), "oninput": event => target.config.token = event.target.value })));
          configFields.push(field(t("Configuration file path"), el("input", { value: target.config.configPath || "", "oninput": event => target.config.configPath = event.target.value })));
        } else if (target.type === "awada") {
          configFields.push(field(t("Awada configuration path"), el("input", { value: target.config.configPath || "", "oninput": event => target.config.configPath = event.target.value })));
          configFields.push(field(t("Channel ID"), el("input", { value: target.config.channelId || "", "oninput": event => target.config.channelId = event.target.value })));
          configFields.push(field(t("File channel ID"), el("input", { value: target.config.fileChannelId || "", "oninput": event => target.config.fileChannelId = event.target.value }), t("Optional: delivery channel for weekly DOCX files.")));
        } else {
          configFields.push(field("Webhook URL", el("input", { type: "text", autocomplete: "off", style: "-webkit-text-security: disc", placeholder: target.config.webhookConfigured ? t("Configured; leave blank to keep") : "https://qyapi.weixin.qq.com/...", "oninput": event => target.config.webhookUrl = event.target.value })));
          configFields.push(field(t("Mentions"), el("input", { value: target.config.mentioned || "", "oninput": event => target.config.mentioned = event.target.value })));
          configFields.push(field(t("Message type"), el("input", { value: target.config.msgType || "text", "oninput": event => target.config.msgType = event.target.value })));
        }
        return el("div", { class: "source-card target-card" },
          el("div", { class: "row" }, type, el("label", { class: "row hint" }, enabled, t("Enabled")),
             el("button", { class: "small danger", onclick: () => { profile.targets.splice(index, 1); renderTargets(); } }, t("Remove"))),
          el("div", { class: "form-grid" }, ...fields, ...configFields));
      }),
      add);
  }
  const addTargetSave = el("button", { class: "small primary", onclick: () => saveTargets(addTargetSave) }, t("Save targets"));
  renderTargets();
  const recentList = el("div", { class: "report-list" });
  const viewer = el("pre", { class: "report-output", hidden: true });
  const viewerMeta = el("div", { class: "report-meta" });
  async function loadRecent() {
    try {
      const result = await api("reports"); if (version !== routeVersion) return;
      recentList.replaceChildren(...result.items.map(item => el("button", { onclick: () => showFile(item) },
        `${item.kind === "topnews" ? "▤ TopNews" : "▦ Weekly"} · ${item.name}`, el("span", {}, item.modified.replace("T", " ")))));
      if (!result.items.length) recentList.append(empty(t("No reports generated yet"), t("Use the buttons above to generate your first TopNews or Weeklyreport with the local model.")));
    } catch (e) { recentList.replaceChildren(empty(t("Unable to list reports"), e.message)); }
  }
  async function showFile(item) {
    if (/\.docx$/i.test(item.name)) {
      viewer.hidden = false;
      viewer.textContent = item.path;
      text(viewerMeta, t("Local path (DOCX preview is unavailable)"));
      return;
    }
    try {
      const data = await api(`reports/file?kind=${encodeURIComponent(item.kind)}&name=${encodeURIComponent(item.name)}`);
      viewer.hidden = false; viewer.textContent = data.content; text(viewerMeta, i18n.join([t("File: "), data.path], ""));
    } catch (e) { toast(e.message); }
  }
  function reportCard(kind, title, subtitle, defaultFrom, defaultTo, buttonLabel) {
    const from = el("input", { type: "date", value: defaultFrom, "aria-label": t("Start date") });
    const to = el("input", { type: "date", value: defaultTo, "aria-label": t("End date") });
    const status = el("span", { class: "hint" }, t("Last 10 runs"));
    const logList = el("div", { class: "report-log" });
    const output = el("pre", { class: "report-output", hidden: true });
    const meta = el("div", { class: "report-meta" });
    const run = el("button", { class: "small primary", onclick: start }, buttonLabel);
    function show(result) {
      output.hidden = false; text(output, result.content || t("(No eligible material in this window)"));
      const parts = [];
      if (result.path) parts.push(i18n.join([t("File: "), result.path], ""));
      if (result.source || result.combinedSource) parts.push(i18n.join([t("Source: "), result.source || result.combinedSource], ""));
      if (result.omittedBoards?.length) parts.push(i18n.join([t("Omitted due to insufficient data: "), result.omittedBoards.join(", ")], ""));
      if (result.dataScarce) parts.push(t("No section met the selection threshold; a deterministic material overview was used."));
      text(meta, i18n.join(parts));
      text(status, i18n.join([t("Generated · "), result.date || ""], ""));
    }
    function logRow(item) {
      const details = item.details || {};
      const notes = [];
      if (details.fromDate || details.toDate) notes.push(t("Window {from} ~ {to}", { from: details.fromDate || "?", to: details.toDate || "?" }));
      if (details.path) notes.push(i18n.join([t("File: "), details.path], ""));
      if (details.docxPath) notes.push("docx：" + details.docxPath);
      if (details.error) notes.push(i18n.join([t("Error: "), details.error], ""));
      const delivery = details.delivery;
      if (delivery) notes.push(delivery.dryRun ? t("Dry-run, not sent") : (delivery.sent ? t("Text sent") : t("Text not sent{reason}", { reason: delivery.message ? ": " + delivery.message : "" })));
      const fileDelivery = details.fileDelivery;
      if (fileDelivery) notes.push(fileDelivery.sent ? t("File sent") : t("File not sent{reason}", { reason: fileDelivery.message ? ": " + fileDelivery.message : "" }));
      return el("div", { class: "report-log-row" + (item.level === "error" ? " error" : "") },
        el("div", { class: "report-log-head" }, el("span", { class: "report-log-time" }, (item.at || "").replace("T", " ")),
          el("span", { class: "report-log-message" }, item.message)),
        notes.length ? el("div", { class: "report-log-meta" }, i18n.join(notes)) : null);
    }
    async function refreshLog() {
      try {
        const result = await api(`reports/${kind}/logs?limit=10`);
        if (version !== routeVersion) return;
        const items = result.items || [];
        logList.replaceChildren(...(items.length ? items.map(logRow) : [el("p", { class: "hint" }, t("No run history for this task yet."))]));
      } catch (e) { logList.replaceChildren(el("p", { class: "hint" }, i18n.join([t("Unable to load run logs: "), e.message], ""))); }
    }
    async function start() {
      run.disabled = true; text(status, t("Generating: local model writing may take a few minutes…"));
      try {
        const job = await api(`${kind}/run`, { method: "POST", body: JSON.stringify({ fromDate: from.value, toDate: to.value }) });
        await refreshLog();
        for (;;) {
          await new Promise(r => setTimeout(r, 2000));
          if (version !== routeVersion) return;
          const j = await api(`reports/jobs/${job.jobId}`);
          if (j.state === "running") { text(status, i18n.join([t("Generating: "), j.kind === "weekly" ? t("selection and writing") : t("candidates and editing"), "…"], "")); continue; }
          if (j.state === "failed") throw new Error(j.error || t("Generation failed"));
          show(j.result); await Promise.all([loadRecent(), refreshLog()]); break;
        }
      } catch (e) { text(status, t("Generation failed")); banner(e.message); await refreshLog(); }
      finally { run.disabled = false; }
    }
    refreshLog();
    return el("section", { class: "card form-card" }, titlebar(title, [run]),
      el("div", { class: "form-body" }, el("p", { class: "hint" }, subtitle),
        el("div", { class: "row", style: "margin:12px 0" }, field(t("Start date"), from), field(t("End date"), to), status),
        logList, output, meta));
  }
  root.replaceChildren(heading(t("Outputs"), ""),
    el("div", { class: "report-grid" },
      reportCard("topnews", t("TopNews · Do you Know"), t("Per-section candidate pools with title/event deduplication, locality and quality filters, publisher penalties and local-model editing. Default window: yesterday through today."), localDate(-1), localDate(), t("Generate TopNews")),
      reportCard("weekly", t("Weeklyreport · Weekly insights"), t("Action networks, policy chains and economic evidence with cross-week deduplication. Local models write sections, analysis and digest. Default: the last eight calendar dates (seven-day lookback)."), localDate(-7), localDate(), t("Generate Weeklyreport"))),
    el("section", { class: "card form-card" }, titlebar(t("Delivery targets"), [addTargetSave]),
      el("div", { class: "form-body" }, targetList)),
    el("section", { class: "card form-card" }, titlebar(t("Recent reports"), [el("button", { class: "small", onclick: loadRecent }, t("↻ Refresh"))]),
      el("div", { class: "form-body" }, recentList, viewer, viewerMeta)));
  await loadRecent();
}

async function route() {
  const version = ++routeVersion;
  state.stream?.close(); state.stream = null;
  state.chart?.dispose(); state.chart = null; state.data = null;
  state.page = location.pathname.split("/")[1] || "graph";
  if (!["graph", "reports", "tasks", "query", "settings", "notifications"].includes(state.page)) state.page = "graph";
  const labels = { graph: t("Intelligence graph"), reports: t("Outputs"), tasks: t("Tasks"), query: t("Query settings"), settings: t("Sources"), notifications: t("Notifications") };
  text($("#page-label"), labels[state.page]); i18n.property(document, "title", i18n.join([labels[state.page], " · OSBI"], ""));
  document.querySelectorAll("[data-nav]").forEach(n => n.classList.toggle("active", n.dataset.nav === state.page));
  banner("");
  try { await ({ graph: graphPage, reports: reportsPage, tasks: tasksPage, query: queryPage, settings: settingsPage, notifications: notificationsPage }[state.page])(version); } catch (e) { banner(e.message); }
  updatePendingBadge();
  i18n.prune();
}
document.addEventListener("click", e => { const a = e.target.closest("a"); if (a && a.origin === location.origin && ["/graph", "/reports", "/tasks", "/query", "/settings", "/notifications"].includes(a.pathname)) { e.preventDefault(); history.pushState({}, "", a.pathname); route(); } });
window.addEventListener("popstate", route);
window.addEventListener("resize", () => state.chart?.resize());
async function localState() { try { await api("status"); text($("#local-label"), t("Local backend running")); $("#local-dot").classList.remove("offline"); } catch { text($("#local-label"), t("Local backend disconnected")); $("#local-dot").classList.add("offline"); } }
async function updatePendingBadge() {
  const badge = $("#unread"); if (!badge) return;
  try {
    const result = await api("runs");
    const count = result.items.filter(r => ["waiting_for_user", "interrupted"].includes(r.state)).length;
    badge.textContent = count; badge.hidden = count === 0;
  } catch { badge.hidden = true; }
}
i18n.shell();
const languagePicker = $("#language-select");
languagePicker.value = i18n.language();
languagePicker.addEventListener("change", () => i18n.setLanguage(languagePicker.value));
i18n.onChange(language => { languagePicker.value = language; });
localState(); setInterval(localState, 15000); updatePendingBadge(); setInterval(updatePendingBadge, 15000); route();
