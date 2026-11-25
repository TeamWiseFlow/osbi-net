"""WeChat Official Account (mp) hunter: wechat-linux-container catalog discovery.

Ported from the production wiseflow wx_crawler. The hunter only discovers article
catalogs by reading biz_message_0.db in the local wechat-linux-container
(SQLCipher + Zstd type-49 article messages); article bodies belong to Camoufox
and are captured by the daemon after server relevance selection.

Read-only and privacy-bounded: the copied database holds all private messages, so
it is snapshotted to a temp dir, filtered to the account whitelist, and removed in
a finally block. Keys and message bodies are never logged.
"""
import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from osbi_contracts.storage import digest
from osbi_contracts.urls import canonical_url
from osbi_client.page_material import seal_material

from .base import CatalogDiscovery, register

logger = logging.getLogger(__name__)

DEFAULT_CONTAINER = "wechat-linux-container"
DEFAULT_WX_USER_DIR = "/home/wechat/Documents/xwechat_files/lonely2012_b040"
DEFAULT_KEYS_FILE = "/home/wechat/.xwechat/wechat_keys.json"
BIZ_DB_REL = "db_storage/message/biz_message_0.db"
BIZ_DB_KEY_NAME = "message/biz_message_0.db"

KEY_HEX_LEN = 96
EXEC_TIMEOUT = 30.0
CP_TIMEOUT = 60.0
DEFAULT_WINDOW_HOURS = 24
WINDOW_OVERLAP_HOURS = 2
CATALOG_TTL_SECONDS = 900
MAX_CATALOG_LINKS = 200
LINK_TEXT_LIMIT = 1000
IDENTITY_PARAMS = ("__biz", "mid", "idx", "sn")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
TABLE_NAME_RE = re.compile(r"^Msg_[A-Za-z0-9_]+$")
KEY_HEX_RE = re.compile(r"^[0-9a-fA-F]{96}$")

_RE_TITLE = re.compile(r"<title><!\[CDATA\[(.*?)\]\]></title>", re.DOTALL)
_RE_URL = re.compile(r"<url><!\[CDATA\[(.*?)\]\]></url>", re.DOTALL)
_RE_AUTHOR = re.compile(r"<name><!\[CDATA\[(.*?)\]\]></name>", re.DOTALL)
_RE_PUB_TIME = re.compile(r"<pub_time>(\d+)</pub_time>", re.DOTALL)
_RE_COVER = re.compile(r"<cover><!\[CDATA\[(.*?)\]\]></cover>", re.DOTALL)
_RE_DES = re.compile(r"<des>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</des>", re.DOTALL)
_ZERO_WIDTH = re.compile(r"[​-‏  ﻿⁠]")


class MpScanError(RuntimeError):
    """Actionable failure discovering the WeChat catalog (container/key/dependency)."""


def mp_article_url(url):
    """Normalize a WeChat article permalink, dropping share/tracking parameters.

    The wechat-linux-container message XML stores http:// URLs; force https://
    like production canonicalUrl does. Returns None for non-article URLs so
    they never enter the catalog.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.hostname != "mp.weixin.qq.com":
        return None
    path = parts.path
    if path.startswith("/s/") and len(path) > len("/s/"):
        return f"https://{parts.netloc}{path}"
    if path == "/s":
        query = parse_qs(parts.query)
        kept = {key: query[key][0] for key in IDENTITY_PARAMS if query.get(key) and query[key][0]}
        if not kept:
            return None
        rebuilt = "&".join(f"{key}={kept[key]}" for key in IDENTITY_PARAMS if key in kept)
        return f"https://{parts.netloc}/s?{rebuilt}"
    return None


def clean_description(value):
    """Strip zero-width filler and whitespace; many <des> are empty or just ZWJ."""
    return _ZERO_WIDTH.sub("", value or "").strip()


def parse_49_message(content_bytes):
    """Parse a Zstd-compressed type-49 appmsg XML blob into catalog metadata."""
    if not isinstance(content_bytes, (bytes, bytearray, memoryview)):
        return None
    data = bytes(content_bytes)
    if not data.startswith(ZSTD_MAGIC):
        return None
    try:
        import zstandard
        xml = zstandard.ZstdDecompressor().decompress(data).decode("utf-8", errors="replace")
    except Exception as error:
        logger.debug("zstd decompress failed: %s", error)
        return None
    title_match = _RE_TITLE.search(xml)
    url_match = _RE_URL.search(xml)
    if not title_match or not url_match:
        return None
    title = title_match.group(1).strip()
    url = url_match.group(1).strip()
    if not title or not url:
        return None
    author_match = _RE_AUTHOR.search(xml)
    pub_match = _RE_PUB_TIME.search(xml)
    cover_match = _RE_COVER.search(xml)
    des_match = _RE_DES.search(xml)
    publish_time = ""
    if pub_match:
        try:
            publish_time = datetime.fromtimestamp(int(pub_match.group(1))).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            publish_time = ""
    return {
        "title": title,
        "url": url,
        "author": author_match.group(1).strip() if author_match else "",
        "publish_time": publish_time,
        "cover": cover_match.group(1).strip() if cover_match else "",
        "description": clean_description(des_match.group(1) if des_match else ""),
    }


def _docker_exec(container, args, timeout=EXEC_TIMEOUT):
    proc = subprocess.run(["docker", "exec", container, *args], capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise MpScanError(
            f"wechat-linux-container container '{container}' exec failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip() or 'is the container running?'}")
    return proc.stdout


def _docker_cp(container, src, dst, timeout=CP_TIMEOUT):
    proc = subprocess.run(["docker", "cp", f"{container}:{src}", dst], capture_output=True, timeout=timeout)
    if proc.returncode != 0:
        raise MpScanError(
            f"wechat-linux-container container '{container}' copy failed (rc={proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace').strip()}")


def _read_key(container, keys_file):
    keys = json_loads(_docker_exec(container, ["cat", keys_file]))
    key = keys.get(BIZ_DB_KEY_NAME) or keys.get(BIZ_DB_REL)
    if not key or not KEY_HEX_RE.match(str(key)):
        raise MpScanError(
            f"biz_message_0.db key missing or not {KEY_HEX_LEN} hex chars in {keys_file}; "
            "WeChat may need re-login or the key scheme changed after an upgrade")
    return str(key)


def json_loads(raw):
    import json
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as error:
        raise MpScanError(f"wechat-linux-container keys file is not valid JSON: {error}") from error


def _copy_db(container, user_dir, tmp_dir):
    container_db = f"{user_dir}/{BIZ_DB_REL}"
    for suffix in ("", "-wal", "-shm"):
        try:
            _docker_cp(container, f"{container_db}{suffix}", str(tmp_dir))
        except MpScanError:
            if suffix:
                continue  # -wal/-shm may not exist
            raise
    return tmp_dir / Path(BIZ_DB_REL).name


def _open_db(db_path, key_hex):
    import sqlcipher3
    conn = sqlcipher3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute(f"PRAGMA key = \"x'{key_hex}'\";")
        cur.execute("PRAGMA cipher_compatibility = 4;")
        cur.execute("SELECT count(*) FROM sqlite_master;")
        cur.fetchone()
    except sqlcipher3.DatabaseError as error:
        conn.close()
        raise MpScanError(f"Failed to decrypt biz_message_0.db (wrong key or cipher mode): {error}") from error
    return conn


def scan_wechat_linux_container(accounts, window_hours, *, container=None, user_dir=None, keys_file=None):
    """Scan the wechat-linux-container biz database for whitelisted article catalogs (blocking).

    Returns records sorted by publish_time desc, deduped by normalized URL.
    Raises MpScanError on any container/key/dependency/decrypt failure.
    """
    try:
        import sqlcipher3  # noqa: F401
        import zstandard  # noqa: F401
    except ImportError as error:
        raise MpScanError(
            "mp hunter requires sqlcipher3-binary and zstandard; "
            "install the [client] extra (pip install -e '.[client]')") from error

    whitelist = {name for name in accounts if name}
    if not whitelist:
        return []
    container = container or os.environ.get("WECHAT_LINUX_CONTAINER_CONTAINER", DEFAULT_CONTAINER)
    user_dir = user_dir or os.environ.get("WECHAT_LINUX_CONTAINER_WX_USER_DIR", DEFAULT_WX_USER_DIR)
    keys_file = keys_file or os.environ.get("WECHAT_LINUX_CONTAINER_KEYS_FILE", DEFAULT_KEYS_FILE)
    cutoff = time.time() - window_hours * 3600

    tmp_dir = Path(tempfile.mkdtemp(prefix="osbi_mp_"))
    records = []
    seen = set()
    try:
        key_hex = _read_key(container, keys_file)
        db_path = _copy_db(container, user_dir, tmp_dir)
        conn = _open_db(db_path, key_hex)
        try:
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%';")
            tables = [row[0] for row in cur.fetchall()]
            for table in tables:
                if not TABLE_NAME_RE.match(table):
                    logger.warning("skipping unexpected wechat-linux-container table name: %r", table)
                    continue
                cur.execute(
                    f'SELECT local_id, create_time, message_content FROM "{table}" '
                    "WHERE (local_type & 0xFFFF) = 49 AND create_time > ? ORDER BY create_time DESC;",
                    (cutoff,))
                for _local_id, _create_time, content in cur.fetchall():
                    message = parse_49_message(content)
                    if not message or message["author"] not in whitelist:
                        continue
                    normalized = mp_article_url(message["url"])
                    if not normalized or normalized in seen:
                        continue
                    seen.add(normalized)
                    records.append({**message, "url": normalized, "original_url": message["url"]})
        finally:
            conn.close()
    except MpScanError:
        raise
    except Exception as error:
        raise MpScanError(f"wechat-linux-container catalog scan failed: {error}") from error
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    records.sort(key=lambda record: record.get("publish_time") or "0000-00-00", reverse=True)
    return records


def build_catalog(records, source_id, sentinel):
    """Synthesize a links-only catalog material and per-URL seed metadata."""
    links, urls, item_meta, seed_meta = [], {}, {}, {}
    for record in records:
        normalized = mp_article_url(record["url"]) or record["url"]
        title = (record.get("title") or "").strip()
        description = clean_description(record.get("description", ""))
        if not normalized or not title:
            continue
        if len(links) >= MAX_CATALOG_LINKS:
            break
        # Relevance judges by link text: fold a real description into the title.
        text = (f"{title}\n{description}" if description else title)[:LINK_TEXT_LIMIT]
        link_id = f"L{len(links) + 1}"
        links.append({"id": link_id, "text": text, "context": None})
        urls[link_id] = normalized
        item_meta[link_id] = {
            "author": record.get("author", ""),
            "publish_time": record.get("publish_time", ""),
            "cover": record.get("cover", ""),
            "description": description,
            "originalUrl": record.get("original_url", record["url"]),
        }
        seed_meta[canonical_url(normalized)] = {
            "title": title,
            "publisher": record.get("author", ""),
            "publishedAt": record.get("publish_time", ""),
        }
    if not links:
        return None, {}
    material = seal_material({
        "status": {"access": "ok", "load": "ready"},
        "meta": {
            "requestedUrl": sentinel, "canonicalUrl": sentinel, "adapter": "mp-catalog",
            "capturedAt": datetime.now(timezone.utc).isoformat(),
            "title": f"MP catalog: {source_id}", "author": "", "publisher": "", "publishedAt": "",
        },
        "blocks": [], "links": links, "scope": {},
        "localMaps": {"urls": urls, "images": {}, "blockSources": {}, "itemMeta": item_meta},
    })
    return material, seed_meta


def _seed_from_material(material):
    urls = material["localMaps"]["urls"]
    item_meta = material["localMaps"].get("itemMeta", {})
    texts = {link["id"]: link["text"] for link in material["links"]}
    seed = {}
    for link_id, url in urls.items():
        meta = item_meta.get(link_id, {})
        seed[canonical_url(url)] = {
            "title": texts.get(link_id, ""),
            "publisher": meta.get("author", ""),
            "publishedAt": meta.get("publish_time", ""),
        }
    return seed


def compute_window(interval_minutes):
    override = os.environ.get("OSBI_MP_WINDOW_HOURS")
    if override:
        try:
            hours = int(override)
            if hours > 0:
                return hours
        except ValueError:
            pass
    if interval_minutes > 0:
        return max(DEFAULT_WINDOW_HOURS, math.ceil(interval_minutes / 60) + WINDOW_OVERLAP_HOURS)
    return DEFAULT_WINDOW_HOURS


class WxMpHunter:
    kind = "mp"

    async def discover(self, source, config, *, force_refresh, cache, event):
        window_hours = compute_window(source.intervalMinutes)
        scope = digest(["mp-catalog/v1", source.profile, sorted(source.accounts), window_hours])
        catalog_policy = config.captureCache.model_copy(
            update={"defaultTtlSeconds": CATALOG_TTL_SECONDS, "domainTtlSeconds": {}})
        cached = None if force_refresh else cache.capture_get(source.url, scope, catalog_policy)
        if cached:
            return CatalogDiscovery(cached, _seed_from_material(cached))

        records = await asyncio.to_thread(scan_wechat_linux_container, source.accounts, window_hours)
        hit_authors = {record["author"] for record in records}
        missing = sorted(set(source.accounts) - hit_authors)
        if missing:
            event("mp_account_health",
                  f"过去 {window_hours}h 未扫到以下公众号文章（共 {len(missing)} 个，可能未关注/未推送/名称不符）："
                  + "、".join(missing), level="warning")
        material, seed_meta = build_catalog(records, source.id, source.url)
        if material is None:
            event("mp_empty_catalog", f"过去 {window_hours}h 白名单内无可提交文章目录", level="warning")
            return CatalogDiscovery(None, {})
        cache.capture_put(source.url, scope, material, catalog_policy)
        return CatalogDiscovery(material, seed_meta)


register(WxMpHunter())
