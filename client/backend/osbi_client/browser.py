"""Python facade to the managed Node runtime; never launch a second browser SDK."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from osbi_contracts.urls import canonical_url
from .snapshot_index import index_snapshot

WECHAT_GATE_ROUNDS = 10
WECHAT_GATE_WAIT_SECONDS = 1.0
CAPTURE_STABILITY_ROUNDS = 15
CAPTURE_STABILITY_INTERVAL = 0.4
_WECHAT_PROBE = (
    "(() => ({article: (document.querySelector('#js_content')?.innerText || '').trim(), "
    "gate: /wappoc_appmsgcaptcha/.test(location.href) || "
    "(document.body?.innerText || '').includes('当前环境异常，完成验证后即可继续访问')}))()"
)
_WECHAT_CLICK_VERIFY = (
    "(() => {const els = [...document.querySelectorAll('a,button,div,span')]; "
    "const el = els.find(e => (e.innerText || '').trim() === '去验证' && e.offsetParent !== null); "
    "if (el) {el.click(); return true;} return false;})()"
)
# Production capture.mjs scroll pass: trigger lazy lists/images, stop when the
# bottom is reached and the height stops growing, then return to the top.
_SCROLL_SETTLE = (
    "(async () => {let height = 0, same = 0, steps = 0, reachedBottom = false; "
    "for (let n = 0; n < 50; n++) {scrollBy(0, 1000); steps = n + 1; "
    "const h = document.documentElement.scrollHeight; "
    "const bottom = scrollY + innerHeight >= h - 10; reachedBottom = bottom; "
    "same = h === height ? same + 1 : 0; height = h; "
    "if (bottom && same >= 2) break; "
    "await new Promise(r => setTimeout(r, 200));} "
    "scrollTo(0, 0); await new Promise(r => setTimeout(r, 500)); "
    "return {steps, reachedBottom};})()"
)


class BrowserError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


_VERIFICATION_TEXTS = ("请完成安全验证", "完成验证后即可继续访问", "当前位置异常",
                       "当前环境异常", "拖动滑块", "Checking your browser")
_WECHAT_GATE_MARKERS = ("wappoc_appmsgcaptcha",)


class Browser:
    def __init__(self, home, root=None, warning_handler=None):
        self.home = Path(home)
        backend_root = Path(__file__).resolve().parents[1]
        self.root = Path(root or os.environ.get("OSBI_BROWSER_ROOT") or backend_root / "browser").resolve()
        installed_node = backend_root.parent.parent / ".osbi/bin/node"
        self.node = os.environ.get("OSBI_BROWSER_NODE") or (str(installed_node) if installed_node.is_file() else shutil.which("node"))
        self.warning_handler = warning_handler
        self.warned = set()
        self._headed_sessions = set()
        self._page = ContextVar("osbi_browser_page", default=None)

    @contextmanager
    def page_scope(self, page_id, generation=None):
        token = self._page.set({"pageId": page_id, "generation": generation})
        try:
            yield
        finally:
            self._page.reset(token)

    async def restart_profile(self, session, *, headed):
        await self.command(session, "profile.stop")
        self._headed_sessions.discard(session)
        return await self.command(session, "start", {"headless": not headed}, headed=headed)

    async def command(self, session, action, params=None, lease=None, *, headed=False, timeout=75):
        page = self._page.get() if action not in {"start", "stop", "close", "profile.stop", "sessions", "status"} else None
        payload = {"action": action, "params": {**(params or {}), **({"pageId": page["pageId"]} if page else {})}}
        if page and page.get("generation"):
            payload["expectedGeneration"] = page["generation"]
        entry = self.root / "dist/cli.js"
        if not self.node or not entry.exists():
            raise BrowserError("RUNTIME_MISSING", "浏览器运行时未安装，请运行客户端安装脚本")
        # The headed mode is a session property: once a session starts headed, every
        # later command must keep passing --headed. Otherwise the CLI re-derives
        # headless:true for commands like `open` and its re-start conflicts with the
        # running headed browser (MODE_CONFLICT).
        if action == "start" and params and params.get("headless") is True:
            self._headed_sessions.discard(session)
        if headed:
            self._headed_sessions.add(session)
        argv = [self.node, str(entry), "--session", session, "--request-stdin", "--json"]
        # Production camoufox_runtime identity (browser.mjs): fixed zh-CN
        # locale, 1440x1000 window, empty config (geoip: false). Government
        # WAFs tarpit the geoip-derived identity (WebRTC exit-IP override +
        # non-zh locale); the fixed production identity passes. Verified on
        # fgj.sh.gov.cn list pages with the same profile both ways.
        argv.extend(["--no-geoip", "--locale", "zh-CN", "--viewport", "1440x1000"])
        if headed or session in self._headed_sessions:
            argv.append("--headed")
        if lease:
            argv.extend(["--lease-token", lease])
        env = {**os.environ, "OSBI_BROWSER_HOME": str(self.home), "CAMOUFOX_CLI_CONFIG": str(self.home / "config.json")}
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, limit=10 * 1024 * 1024)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode()), timeout)
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
        except asyncio.TimeoutError as error:
            proc.kill()
            await proc.wait()
            raise BrowserError("OUTCOME_UNKNOWN", "浏览器操作结果未知，请检查会话状态后处理") from error
        try:
            result = json.loads(out)
            if not result.get("ok"):
                error = result.get("error", {})
                code = error.get("code", "BROWSER_ERROR")
                detail = error.get("message", "")
                raise BrowserError(code, f"浏览器操作失败：{code}" + (f"（{detail[:160]}）" if detail else ""))
            data = result.get("data", {})
            if page and action == "lease.acquire":
                page["generation"] = result.get("meta", {}).get("generation")
                data["generation"] = page["generation"]
            for warning in data.get("warnings", []):
                key = (session, data.get("generation"), warning.get("code"))
                if self.warning_handler and key not in self.warned:
                    self.warned.add(key)
                    self.warning_handler(warning)
            return data
        except (ValueError, TypeError) as error:
            raise BrowserError("OUTCOME_UNKNOWN", "浏览器未返回有效操作结果") from error

    async def _wechat_gate(self, session, lease):
        """Wait for the WeChat article body; click the gate's '去验证' once.

        Clicking sometimes opens the article directly (no slider). A visual slider
        is never solved here: if the gate persists, normal verification detection
        raises VERIFICATION_REQUIRED for a human to resolve.
        """
        clicked = False
        for _ in range(WECHAT_GATE_ROUNDS):
            try:
                state = (await self.command(session, "eval", {"expression": _WECHAT_PROBE}, lease))["result"]
            except BrowserError as error:
                if "context was destroyed" in str(error) or "navigation" in str(error).lower():
                    await asyncio.sleep(WECHAT_GATE_WAIT_SECONDS)
                    continue
                raise
            if state.get("article") or not state.get("gate"):
                return
            if clicked:
                return
            try:
                clicked = bool((await self.command(session, "eval", {"expression": _WECHAT_CLICK_VERIFY}, lease))["result"])
            except BrowserError:
                clicked = False
            await asyncio.sleep(WECHAT_GATE_WAIT_SECONDS)

    async def human_state(self, session, lease):
        """Read the existing DOM to decide whether a human gate has cleared.

        It never navigates, refreshes or scrolls. The daemon calls this after a
        headed prompt opens and then once per minute.
        """
        script = (Path(__file__).with_name("static") / "dom-material.js").read_text()
        expression = "(() => {" + script + ";return readDom(1200);})()"
        dom = (await self.command(session, "eval", {"expression": expression}, lease))["result"]
        text = str(dom.get("bodyText") or "")
        url = str(dom.get("url") or "")
        verification = bool(dom.get("passwordForm")) or any(
            marker in text for marker in _VERIFICATION_TEXTS) or any(
            marker in url for marker in _WECHAT_GATE_MARKERS)
        partial = bool(dom.get("truncated") or dom.get("metadataUnresolved")
                       or dom.get("complexTables"))
        return dict(verification=verification, partial=partial, url=url,
                    resolved=not (verification or partial))

    async def capture(self, session, url, lease, seed_meta=None, *, enricher=None,
                      settle: bool = True):
        # Plain aria snapshot (production capture.mjs never binds refs for
        # material extraction); the command waits for content stability first.
        # ``settle=False`` is the partial-page grace recheck: read the existing
        # DOM only, never navigate/refresh or re-run the lazy-load scroll pass.
        if settle:
            await self.command(session, "snapshot", {"refs": False}, lease=lease)
            if urlsplit(url).hostname == "mp.weixin.qq.com":
                await self._wechat_gate(session, lease)
            try:
                await self.command(session, "eval", {"expression": _SCROLL_SETTLE}, lease)
            except BrowserError:
                # Best-effort lazy-load pass; the stability loop below still gates
                # the material read (production records and continues likewise).
                pass
        script = (Path(__file__).with_name("static") / "dom-material.js").read_text()
        expression = "(() => {" + script + ";return readDom(1200);})()"
        def signature(dom):
            return hashlib.sha256(json.dumps(dom.get("segments", []), sort_keys=True).encode()).hexdigest()
        # Wait for the page to settle: dynamic article pages (WeChat ads/loaders)
        # need several rounds before two consecutive snapshots match. A redirect
        # mid-eval destroys the JS context; treat that as "not settled" and retry.
        previous = None
        dom = None
        for _ in range(CAPTURE_STABILITY_ROUNDS):
            await asyncio.sleep(CAPTURE_STABILITY_INTERVAL)
            try:
                current = (await self.command(session, "eval", {"expression": expression}, lease))["result"]
            except BrowserError as error:
                if "context was destroyed" in str(error) or "navigation" in str(error).lower():
                    previous = None
                    continue
                raise
            if previous is not None and signature(previous) == signature(current):
                dom = current
                break
            previous = current
        if dom is None:
            raise BrowserError("PAGE_CHANGED", "页面内容仍在变化，请等待稳定后重试")
        text = dom.get("bodyText", "")
        if dom.get("passwordForm") or any(p in text for p in ["请完成安全验证", "完成验证后即可继续访问", "拖动滑块", "Checking your browser"]):
            raise BrowserError("VERIFICATION_REQUIRED", "请在浏览器中完成登录或验证")
        if dom.get("truncated") or dom.get("metadataUnresolved") or dom.get("complexTables"):
            raise BrowserError("CAPTURE_PARTIAL", "页面不完整或表格结构复杂，需要检查后再采集")
        capture = {"requestedUrl": url, "finalUrl": dom["url"], "canonicalUrl": canonical_url(dom["url"]),
                   "capturedAt": datetime.now(timezone.utc).isoformat(), "status": {"access": "ok", "load": "ready"}, "dom": dom}
        # All browser commands are done; VL enrichment holds no lease.
        if enricher is not None and enricher.enabled:
            capture = await enricher.enrich(capture)
        material = index_snapshot(capture, seed_meta)
        if not material["blocks"] and not material["links"]:
            raise BrowserError("CAPTURE_EMPTY", "页面未获取到正文或有效链接")
        images = material.get("localMaps", {}).get("images") or {}
        if enricher is not None and enricher.enabled:
            # Cleared when every eligible image resolved; honest signal on partial failure.
            material["meta"]["mediaPending"] = any(im.get("status") in {"pending", "failed"} for im in images.values())
        elif images:
            # Text-only client: preserve image evidence without inventing OCR text.
            material["meta"]["mediaPending"] = True
        return material
