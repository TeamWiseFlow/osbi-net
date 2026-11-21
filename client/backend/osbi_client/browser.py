"""Python facade to the managed Node runtime; never launch a second browser SDK."""
import asyncio
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


class BrowserError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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

    async def command(self, session, action, params=None, lease=None, *, headed=False, timeout=75):
        entry = self.root / "dist/cli.js"
        if not self.node or not entry.exists():
            raise BrowserError("RUNTIME_MISSING", "浏览器运行时未安装，请运行客户端安装脚本")
        # The headed mode is a session property: once a session starts headed, every
        # later command must keep passing --headed. Otherwise the CLI re-derives
        # headless:true for commands like `open` and its re-start conflicts with the
        # running headed browser (MODE_CONFLICT).
        if headed:
            self._headed_sessions.add(session)
        argv = [self.node, str(entry), "--session", session, "--request-stdin", "--json"]
        argv.append("--geoip")
        if headed or session in self._headed_sessions:
            argv.append("--headed")
        if lease:
            argv.extend(["--lease-token", lease])
        env = {**os.environ, "OSBI_BROWSER_HOME": str(self.home), "CAMOUFOX_CLI_CONFIG": str(self.home / "config.json")}
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, limit=10 * 1024 * 1024)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(json.dumps({"action": action, "params": params or {}}).encode()), timeout)
        except asyncio.TimeoutError as error:
            proc.kill()
            await proc.wait()
            raise BrowserError("OUTCOME_UNKNOWN", "浏览器操作结果未知，请检查会话状态后处理") from error
        try:
            result = json.loads(out)
            if not result.get("ok"):
                code = result.get("error", {}).get("code", "BROWSER_ERROR")
                raise BrowserError(code, "浏览器操作失败：" + code)
            data = result.get("data", {})
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
            state = (await self.command(session, "eval", {"expression": _WECHAT_PROBE}, lease))["result"]
            if state.get("article") or not state.get("gate"):
                return
            if clicked:
                return
            clicked = bool((await self.command(session, "eval", {"expression": _WECHAT_CLICK_VERIFY}, lease))["result"])
            await asyncio.sleep(WECHAT_GATE_WAIT_SECONDS)

    async def capture(self, session, url, lease, seed_meta=None, *, enricher=None):
        await self.command(session, "snapshot", lease=lease)
        if urlsplit(url).hostname == "mp.weixin.qq.com":
            await self._wechat_gate(session, lease)
        script = (Path(__file__).with_name("static") / "dom-material.js").read_text()
        expression = "(() => {" + script + ";return readDom(1200);})()"
        first = (await self.command(session, "eval", {"expression": expression}, lease))["result"]
        await asyncio.sleep(0.25)
        second = (await self.command(session, "eval", {"expression": expression}, lease))["result"]
        def signature(dom):
            return hashlib.sha256(json.dumps(dom.get("segments", []), sort_keys=True).encode()).hexdigest()
        if signature(first) != signature(second):
            raise BrowserError("PAGE_CHANGED", "页面内容仍在变化，请等待稳定后重试")
        dom = second
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
