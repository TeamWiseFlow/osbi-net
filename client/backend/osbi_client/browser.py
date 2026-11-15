"""Python facade to the managed Node runtime; never launch a second browser SDK."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
from osbi_contracts.urls import canonical_url
from .snapshot_index import index_snapshot


class BrowserError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Browser:
    def __init__(self, home, root=None):
        self.home = Path(home)
        self.root = Path(root or os.environ.get("OSBI_BROWSER_ROOT") or Path(__file__).resolve().parents[2] / "browser")
        self.node = os.environ.get("OSBI_BROWSER_NODE") or shutil.which("node")

    async def command(self, session, action, params=None, lease=None, *, headed=False, timeout=75):
        entry = self.root / "dist/cli.js"
        if not self.node or not entry.exists():
            raise BrowserError("RUNTIME_MISSING", "浏览器运行时未安装，请运行客户端安装脚本")
        argv = [self.node, str(entry), "--session", session, "--request-stdin", "--json"]
        argv.append("--no-geoip")
        if headed:
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
            return result.get("data", {})
        except (ValueError, TypeError) as error:
            raise BrowserError("OUTCOME_UNKNOWN", "浏览器未返回有效操作结果") from error

    async def capture(self, session, url, lease):
        await self.command(session, "snapshot", lease=lease)
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
        material = index_snapshot(capture)
        if not material["blocks"] and not material["links"]:
            raise BrowserError("CAPTURE_EMPTY", "页面未获取到正文或有效链接")
        # Preserve image evidence without inventing OCR text in a text-only client.
        if material.get("localMaps", {}).get("images"):
            material["meta"]["mediaPending"] = True
        return material
