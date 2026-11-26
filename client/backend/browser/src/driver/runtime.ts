import { assetStatus, requireAssets } from "./sdk.js";
import { BrowserManager } from "./browser.js";
import { execute } from "../actions/commands.js";
import { RuntimeError, type Params } from "../protocol/index.js";
import type { LaunchOptions } from "../state/registry.js";
import type { Driver } from "../runtime/server.js";

export { assetStatus };
export class CamoufoxDriver implements Driver {
  readonly capabilities = ["page.create", "page.close", "page.activate", "open", "back", "forward", "reload", "url", "title", "snapshot", "click", "fill", "type", "select", "check", "hover", "press", "text", "eval", "screenshot", "pdf", "scroll", "wait", "tabs", "switch", "close-tab", "cookies", "identity", "upload"];
  private manager: BrowserManager;
  private pages = new Map<string, BrowserManager>();
  get warnings() { return this.manager.warnings; }
  constructor(private options: LaunchOptions) {
    this.manager = new BrowserManager(options.persistent, options.proxy, options.geoip, options.locale, options.viewport);
  }
  async launch(): Promise<void> {
    requireAssets();
    await this.manager.launch(this.options.headless);
    // Listening on IPC is not enough to report browser readiness.
    if (await this.manager.getPage().evaluate(() => 1 + 1) !== 2) throw new RuntimeError("START_FAILED", "Browser readiness check failed");
  }
  async execute(action: string, params: Params): Promise<Params> {
    const pageId = typeof params.pageId === "string" ? params.pageId : "";
    if (action === "page.create") {
      if (!pageId) throw new RuntimeError("INVALID_ARGUMENT", "A page ID is required");
      if (!this.pages.has(pageId)) this.pages.set(pageId, await this.manager.createTask());
      return { pageId };
    }
    const manager = pageId ? this.pages.get(pageId) : this.manager;
    if (action === "page.close") {
      if (manager && pageId) { await manager.disposeTask(); this.pages.delete(pageId); }
      return { closed: true };
    }
    if (!manager) throw new RuntimeError("PAGE_CLOSED", "The task page no longer exists");
    if (action === "page.activate") { await manager.getPage().bringToFront(); return {}; }
    if (action === "open") params = { ...params, headless: this.options.headless };
    const response = await execute(manager, { id: "runtime", action, params });
    if (!response.success) throw new RuntimeError(response.code ?? "ACTION_FAILED", response.error || "Browser action failed");
    return response.data ?? {};
  }
  async close(): Promise<void> {
    for (const manager of this.pages.values()) manager.refs.invalidate();
    this.pages.clear();
    await this.manager.close();
  }
}
