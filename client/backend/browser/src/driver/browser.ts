/** Browser manager: launches and manages Camoufox instance. */

import { mkdirSync, existsSync, readFileSync, writeFileSync, renameSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { type Browser, type BrowserContext, type Page } from "playwright-core";
import { launchBrowser, requireAssets, exitGeo, type BrowserWarning } from "./sdk.js";
import { loadOrCreate, toLaunchOptions } from "../state/identity.js";
import { parseProxySettings } from "../state/proxy.js";
import { RefRegistry } from "../actions/refs.js";

const MAX_HISTORY = 200;

export class BrowserManager {
  readonly warnings: BrowserWarning[] = [];
  private warn = (warning: BrowserWarning): void => { this.warnings.push(warning); };
  refs = new RefRegistry();
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  private page: Page | null = null;
  private persistent: string | null;
  private proxy: string | null;
  private geoip: boolean;
  private locale: string | null;
  private viewport: [number, number] | null;
  private history: string[] = [];
  private historyIndex = -1;
  private tabIds = new Map<Page, string>();
  private histories = new Map<Page, { urls: string[]; index: number }>();
  private ownedPages: Set<Page> | null = null;

  /** A task owns its pages and refs; the root manager alone owns the context. */
  async createTask(): Promise<BrowserManager> {
    const view = new BrowserManager(this.persistent, this.proxy, this.geoip, this.locale, this.viewport);
    view.context = this.getContext();
    view.page = await view.context.newPage();
    view.ownedPages = new Set();
    const watch = (page: Page) => {
      view.ownedPages!.add(page);
      view.tabId(page);
      page.on("popup", watch);
      page.on("framenavigated", frame => {
        if (page === view.page && frame === page.mainFrame()) view.refs.invalidate();
      });
      page.on("close", () => {
        view.ownedPages!.delete(page);
        if (page === view.page) { view.refs.invalidate(); view.page = view.pages()[0] ?? null; }
      });
    };
    watch(view.page);
    return view;
  }

  private pages(): Page[] {
    return this.ownedPages ? [...this.ownedPages].filter(p => !p.isClosed()) : this.getContext().pages();
  }

  async disposeTask(): Promise<void> {
    this.refs.invalidate();
    await Promise.allSettled(this.pages().map(page => page.close()));
    if (this.pages().length) throw new Error("Task pages did not close; retry disposal");
  }

  constructor(persistent: string | null = null, proxy: string | null = null, geoip: boolean = true, locale: string | null = null, viewport: [number, number] | null = null) {
    this.persistent = persistent;
    this.proxy = proxy;
    this.geoip = geoip;
    this.locale = locale;
    this.viewport = viewport;
  }

  async launch(headless: boolean = true): Promise<void> {
    if (this.browser || this.context) return;

    requireAssets();
    this.warnings.length = 0;

    const launchOpts: Record<string, unknown> = { headless };
    if (this.viewport) {
      // Fixed window size — overrides the fingerprint-derived (often mobile-ratio)
      // default. Needed for headed logins where the QR code is cut off by a
      // mobile viewport (weibo / xianyu / wechat-channels). The SDK maps
      // `window` to a fixed window size in the generated fingerprint.
      launchOpts.window = this.viewport;
    }
    if (this.proxy) {
      const settings = parseProxySettings(this.proxy);
      launchOpts.proxy = settings.proxy;
    }

    if (this.persistent) {
      // Persistent identity: freeze fingerprint/OS on first launch; reload
      // it on subsequent launches. CLI-passed locale / exit-derived geo
      // overwrite the stored values so the file tracks current intent.
      mkdirSync(this.persistent, { recursive: true });
      const identity = await loadOrCreate(
        this.persistent,
        this.locale,
        this.proxy,
        this.geoip,
        this.viewport,
        this.warn,
      );
      Object.assign(launchOpts, toLaunchOptions(identity));
      this.context = await launchBrowser({ ...launchOpts, persistent_context: true, user_data_dir: this.persistent }) as BrowserContext;
      const cookies = join(this.persistent, ".osbi-runtime", "cookies.json");
      // Session cookies also survive a deliberate headed/headless restart.
      if (existsSync(cookies)) await this.context.addCookies(JSON.parse(readFileSync(cookies, "utf8")));
      const pages = this.context.pages();
      this.page = pages[0] || await this.context.newPage();
    } else {
      if (this.locale) {
        // Non-persistent path: locale is a one-shot override, no identity file.
        const locales = this.locale.split(",").map((s) => s.trim()).filter(Boolean);
        if (locales.length > 0) {
          launchOpts.locale = locales.length > 1 ? locales : locales[0];
        }
      }
      if (this.geoip) {
        launchOpts.config = await exitGeo(this.proxy, {}, this.warn) ?? {};
        if ((launchOpts.config as Record<string, unknown>)["webrtc:ipv4"])
          launchOpts.firefox_user_prefs = { "network.dns.disableIPv6": true };
      }
      this.browser = await launchBrowser(launchOpts) as Browser;
      this.page = await this.browser.newPage();
      this.context = this.page.context();
    }

    // Proxy credentials stay in Playwright's proxy options, never page HTTP headers.
    const watchPage = (page: Page) => {
      this.tabId(page);
      page.on("framenavigated", frame => {
        if (page === this.page && frame === page.mainFrame()) this.refs.invalidate();
      });
      page.on("close", () => {
        this.tabIds.delete(page);
        this.histories.delete(page);
        if (page === this.page) {
          this.refs.invalidate();
          this.page = this.context?.pages().find(p => !p.isClosed()) ?? null;
          this.restoreHistory();
        }
      });
    };
    for (const page of this.context.pages()) watchPage(page);
    this.context.on("page", watchPage);
  }

  getPage(): Page {
    if (!this.page || this.page.isClosed()) throw new Error("No active page; open a tab or restart the session.");
    return this.page;
  }

  /** Persistent profile dir in use, or null for a non-persistent session. */
  get persistentDir(): string | null {
    return this.persistent;
  }

  getContext(): BrowserContext {
    if (!this.context) throw new Error("Browser not launched. Send 'open' command first.");
    return this.context;
  }

  tabId(page: Page): string {
    let id = this.tabIds.get(page);
    if (!id) { id = `tab-${randomUUID()}`; this.tabIds.set(page, id); }
    return id;
  }

  private saveHistory(): void {
    if (this.page) this.histories.set(this.page, { urls: this.history, index: this.historyIndex });
  }
  private restoreHistory(): void {
    const history = this.page ? this.histories.get(this.page) : undefined;
    this.history = history?.urls ?? []; this.historyIndex = history?.index ?? -1;
  }

  async getTabsAsync(): Promise<{ index: number; tabId: string; url: string; title: string; active: boolean }[]> {
    const ctx = this.getContext();
    const pages = this.pages();
    const tabs = [];
    for (let i = 0; i < pages.length; i++) {
      tabs.push({
        index: i,
        tabId: this.tabId(pages[i]),
        url: pages[i].url(),
        title: await pages[i].title(),
        active: pages[i] === this.page,
      });
    }
    return tabs;
  }

  async switchToTab(target: number | string): Promise<Page> {
    const ctx = this.getContext();
    const pages = this.pages();
    const index = typeof target === "string" ? pages.findIndex(p => this.tabId(p) === target) : target;
    if (!Number.isInteger(index) || index < 0 || index >= pages.length) {
      throw new RangeError(`Tab ${target} does not exist`);
    }
    this.saveHistory();
    this.page = pages[index];
    this.restoreHistory();
    this.refs.invalidate();
    await this.page.bringToFront();
    return this.page;
  }

  async closeTab(tabId?: string): Promise<void> {
    const ctx = this.getContext();
    const pages = this.pages();
    if (pages.length <= 1) {
      throw new Error("Cannot close the last tab. Use 'close' to shut down the browser.");
    }
    const current = tabId === undefined ? this.getPage() : pages.find(p => this.tabId(p) === tabId);
    if (!current) throw new RangeError(`Tab ${tabId} does not exist`);
    if (current !== this.page) { await current.close(); return; }
    const idx = pages.indexOf(current);
    const newIdx = idx > 0 ? idx - 1 : 1;
    this.page = pages[newIdx];
    this.restoreHistory();
    this.refs.invalidate();
    await this.page.bringToFront();
    await current.close();
  }

  pushHistory(url: string): void {
    this.history = this.history.slice(0, this.historyIndex + 1);
    this.history.push(url);
    if (this.history.length > MAX_HISTORY) {
      this.history = this.history.slice(-MAX_HISTORY);
    }
    this.historyIndex = this.history.length - 1;
    this.saveHistory();
  }

  async goBack(): Promise<string | null> {
    if (this.historyIndex <= 0) return null;
    this.historyIndex--;
    const url = this.history[this.historyIndex];
    await this.getPage().goto(url, { waitUntil: "domcontentloaded" });
    this.saveHistory();
    return url;
  }

  async goForward(): Promise<string | null> {
    if (this.historyIndex >= this.history.length - 1) return null;
    this.historyIndex++;
    const url = this.history[this.historyIndex];
    await this.getPage().goto(url, { waitUntil: "domcontentloaded" });
    this.saveHistory();
    return url;
  }

  async close(): Promise<void> {
    this.refs.invalidate();
    try {
      if (this.persistent && this.context) {
        const directory = join(this.persistent, ".osbi-runtime");
        mkdirSync(directory, { recursive: true, mode: 0o700 });
        const temporary = join(directory, `cookies-${randomUUID()}.tmp`);
        writeFileSync(temporary, JSON.stringify(await this.context.cookies()), { mode: 0o600 });
        renameSync(temporary, join(directory, "cookies.json"));
      }
    } finally {
      try {
        if (this.browser) await this.browser.close();
        else if (this.context) await this.context.close();
      } finally {
        this.browser = null; this.context = null; this.page = null;
        this.history = []; this.historyIndex = -1;
        this.tabIds.clear(); this.histories.clear();
      }
    }
  }

  get isRunning(): boolean {
    return this.browser !== null || this.context !== null;
  }
}
