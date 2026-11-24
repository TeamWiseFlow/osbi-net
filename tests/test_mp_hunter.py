"""Unit tests for the hunters subsystem and the WeChat mp hunter."""
import json
import sys
import types
from pathlib import Path

import pytest
import zstandard

from hunters import hunter_for, register
from hunters.base import CatalogDiscovery, HUNTERS
from hunters import wx_mp


SENTINEL = "https://mp.weixin.qq.com/"


def appmsg(title="标题", url="https://mp.weixin.qq.com/s/abc123", author="第一财经",
           pub_time=1763000000, cover="https://mmbiz.qpic.cn/x.jpg", des=None):
    des_xml = f"<des><![CDATA[{des}]]></des>" if des is not None else ""
    xml = (f"<msg><appmsg><title><![CDATA[{title}]]></title>"
           f"<url><![CDATA[{url}]]></url>"
           f"<name><![CDATA[{author}]]></name>"
           f"<pub_time>{pub_time}</pub_time>"
           f"<cover><![CDATA[{cover}]]></cover>{des_xml}</appmsg></msg>")
    return zstandard.ZstdCompressor().compress(xml.encode("utf-8"))


# --- URL normalization -----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("https://mp.weixin.qq.com/s/abc123?scene=21&chksm=xyz#rd", "https://mp.weixin.qq.com/s/abc123"),
    ("https://mp.weixin.qq.com/s/abc123", "https://mp.weixin.qq.com/s/abc123"),
    ("http://mp.weixin.qq.com/s?__biz=MzA1&mid=200&idx=1&sn=deadbeef&scene=27&chksm=abc&subscene=10000",
     "https://mp.weixin.qq.com/s?__biz=MzA1&mid=200&idx=1&sn=deadbeef"),
])
def test_mp_article_url_normalization(raw, expected):
    assert wx_mp.mp_article_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "https://mp.weixin.qq.com/mp/profile_ext?action=home",
    "https://mp.weixin.qq.com/",
    "https://example.com/s/abc",
    "https://mp.weixin.qq.com/s?scene=21",  # /s without identity tuple
    "not a url",
])
def test_mp_article_url_rejects_non_permalinks(raw):
    assert wx_mp.mp_article_url(raw) is None


def test_mp_article_url_preserves_identity_tuple_order():
    url = "https://mp.weixin.qq.com/s?sn=sn1&__biz=biz1&idx=2&mid=mid1&from=groupmessage"
    result = wx_mp.mp_article_url(url)
    assert result is not None
    for part in ("__biz=biz1", "mid=mid1", "idx=2", "sn=sn1"):
        assert part in result
    assert "from=" not in result and "scene=" not in result


# --- type-49 message parsing ----------------------------------------------

def test_parse_49_message_valid():
    msg = wx_mp.parse_49_message(appmsg())
    assert msg["title"] == "标题"
    assert msg["url"] == "https://mp.weixin.qq.com/s/abc123"
    assert msg["author"] == "第一财经"
    assert msg["publish_time"]  # YYYY-MM-DD
    assert msg["cover"] == "https://mmbiz.qpic.cn/x.jpg"


def test_parse_49_message_rejects_bad_input():
    assert wx_mp.parse_49_message(b"") is None
    assert wx_mp.parse_49_message(b"not zstd") is None
    assert wx_mp.parse_49_message(zstandard.ZstdCompressor().compress(b"<msg></msg>")) is None
    assert wx_mp.parse_49_message("a string") is None


def test_parse_49_message_missing_url():
    blob = zstandard.ZstdCompressor().compress(b"<msg><title><![CDATA[t]]></title></msg>")
    assert wx_mp.parse_49_message(blob) is None


def test_parse_49_message_extracts_description():
    msg = wx_mp.parse_49_message(appmsg(des="华为OSTA星芒开启AI原生终端的新体验"))
    assert msg["description"] == "华为OSTA星芒开启AI原生终端的新体验"


def test_parse_49_message_empty_or_zero_width_description():
    assert wx_mp.parse_49_message(appmsg())["description"] == ""
    assert wx_mp.parse_49_message(appmsg(des="‍"))["description"] == ""


def test_clean_description_strips_zero_width():
    assert wx_mp.clean_description("‍摘要内容​") == "摘要内容"
    assert wx_mp.clean_description("") == ""
    assert wx_mp.clean_description(None) == ""


def test_build_catalog_folds_description_into_text():
    recs = [
        {"title": "标题一", "url": "https://mp.weixin.qq.com/s/a1", "author": "第一财经",
         "publish_time": "2026-11-20", "cover": "", "description": "这是摘要",
         "original_url": "https://mp.weixin.qq.com/s/a1"},
        {"title": "标题二", "url": "https://mp.weixin.qq.com/s/a2", "author": "第一财经",
         "publish_time": "2026-11-20", "cover": "", "description": "",
         "original_url": "https://mp.weixin.qq.com/s/a2"},
    ]
    material, seed = wx_mp.build_catalog(recs, "mp-economy", SENTINEL)
    texts = {link["id"]: link["text"] for link in material["links"]}
    assert texts["L1"] == "标题一\n这是摘要"
    assert texts["L2"] == "标题二"
    from osbi_contracts.urls import canonical_url
    assert seed[canonical_url("https://mp.weixin.qq.com/s/a1")]["title"] == "标题一"


# --- catalog synthesis -----------------------------------------------------

def records():
    return [
        {"title": "文章一", "url": "https://mp.weixin.qq.com/s/aaa?scene=1", "author": "第一财经",
         "publish_time": "2026-11-19", "cover": "c1", "original_url": "https://mp.weixin.qq.com/s/aaa?scene=1"},
        {"title": "文章二", "url": "https://mp.weixin.qq.com/s/bbb", "author": "经济日报",
         "publish_time": "2026-11-20", "cover": "c2", "original_url": "https://mp.weixin.qq.com/s/bbb"},
    ]


def test_build_catalog_shape_and_seed_meta():
    material, seed_meta = wx_mp.build_catalog(records(), "mp-economy", SENTINEL)
    assert material["blocks"] == []
    assert material["status"] == {"access": "ok", "load": "ready"}
    assert material["meta"]["requestedUrl"] == SENTINEL
    assert material["meta"]["adapter"] == "mp-catalog"
    assert material["schemaVersion"] == "snapshot-index/v2"
    assert material["materialId"]
    ids = [link["id"] for link in material["links"]]
    assert len(ids) == len(set(ids)) == 2
    for link in material["links"]:
        assert link["text"]
        assert "url" not in link  # daemon restores url from localMaps
        assert material["localMaps"]["urls"][link["id"]]
    # seed_meta keyed by canonical URL, publisher is the account name
    from osbi_contracts.urls import canonical_url
    assert seed_meta[canonical_url("https://mp.weixin.qq.com/s/bbb")]["publisher"] == "经济日报"
    assert seed_meta[canonical_url("https://mp.weixin.qq.com/s/aaa")]["publishedAt"] == "2026-11-19"


def test_build_catalog_caps_links_at_200():
    many = [{"title": f"t{i}", "url": f"https://mp.weixin.qq.com/s/id{i}", "author": "第一财经",
             "publish_time": "2026-11-20", "cover": "", "original_url": f"https://mp.weixin.qq.com/s/id{i}"}
            for i in range(250)]
    material, _ = wx_mp.build_catalog(many, "mp-economy", SENTINEL)
    assert len(material["links"]) == 200


def test_build_catalog_empty_records():
    material, seed_meta = wx_mp.build_catalog([], "mp-economy", SENTINEL)
    assert material is None
    assert seed_meta == {}


# --- registry --------------------------------------------------------------

def test_registry_dispatch():
    assert hunter_for("mp") is not None
    assert hunter_for("mp").kind == "mp"
    assert hunter_for("web") is None
    assert hunter_for("rss") is None


def test_registry_rejects_duplicate_kind():
    class Dup:
        kind = "mp"
        async def discover(self, *a, **k):
            return CatalogDiscovery(None, {})
    with pytest.raises(ValueError):
        register(Dup())


# --- scan (mocked docker + sqlcipher) -------------------------------------

class FakeCursor:
    def __init__(self, tables, rows_by_table):
        self.tables = tables
        self.rows_by_table = rows_by_table
        self._rows = []
        self._sql = ""

    def execute(self, sql, params=()):
        self._sql = sql
        if "sqlite_master" in sql and "count" in sql:
            self._rows = [(1,)]
        elif "sqlite_master" in sql and "Msg_" in sql:
            self._rows = [(t,) for t in self.tables]
        elif "local_type" in sql:
            table = sql.split('FROM "')[1].split('"')[0]
            self._rows = self.rows_by_table.get(table, [])
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, tables, rows_by_table):
        self.tables = tables
        self.rows_by_table = rows_by_table
        self.closed = False

    def cursor(self):
        return FakeCursor(self.tables, self.rows_by_table)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_scan_env(tmp_path, monkeypatch):
    """Mock docker subprocess + sqlcipher3 module; return a configurator."""
    keys = {"message/biz_message_0.db": "a" * 96}
    state = {"tables": ["Msg_abc"], "rows": {"Msg_abc": []}, "cp_calls": [], "key": "a" * 96}

    def fake_run(args, capture_output=False, timeout=None):
        if args[:2] == ["docker", "exec"] and "cat" in args:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(keys).encode(), stderr=b"")
        if args[:2] == ["docker", "cp"]:
            src, dst = args[2], args[3]
            state["cp_calls"].append(src)
            name = Path(src).name
            if name.endswith((".db", "-wal", "-shm")):
                Path(dst, name).write_bytes(b"x")
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"unknown command")

    fake_module = types.ModuleType("sqlcipher3")

    class DatabaseError(Exception):
        pass

    fake_module.DatabaseError = DatabaseError
    fake_module.connect = lambda path: FakeConn(state["tables"], state["rows"])
    monkeypatch.setitem(sys.modules, "sqlcipher3", fake_module)
    monkeypatch.setattr(wx_mp.subprocess, "run", fake_run)
    monkeypatch.setenv("MIMICWX_CONTAINER", "mimicwx-linux")
    return state


def test_scan_filters_whitelist_dedups_and_sorts(fake_scan_env, monkeypatch):
    now = 1763100000
    monkeypatch.setattr(wx_mp.time, "time", lambda: now)
    rows = [
        (1, now - 100, appmsg(title="新", url="https://mp.weixin.qq.com/s/new?scene=1", author="第一财经", pub_time=now - 100)),
        (2, now - 200, appmsg(title="旧", url="https://mp.weixin.qq.com/s/old", author="第一财经", pub_time=now - 200)),
        (3, now - 100, appmsg(title="重复", url="https://mp.weixin.qq.com/s/new?scene=2", author="第一财经", pub_time=now - 100)),
        (4, now - 50, appmsg(title="外号", url="https://mp.weixin.qq.com/s/other", author="不在白名单", pub_time=now - 50)),
    ]
    fake_scan_env["rows"]["Msg_abc"] = rows
    result = wx_mp.scan_mimicwx(["第一财经"], window_hours=24)
    urls = [r["url"] for r in result]
    assert urls == ["https://mp.weixin.qq.com/s/new", "https://mp.weixin.qq.com/s/old"]
    assert all(r["author"] == "第一财经" for r in result)


def test_scan_rejects_bad_key(fake_scan_env, monkeypatch):
    fake_scan_env["key"] = "short"
    # patch the keys returned by docker exec
    import hunters.wx_mp as m
    orig = m.subprocess.run

    def bad_key_run(args, **kw):
        if args[:2] == ["docker", "exec"] and "cat" in args:
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"message/biz_message_0.db": "zz"}).encode(), stderr=b"")
        return orig(args, **kw)
    monkeypatch.setattr(m.subprocess, "run", bad_key_run)
    with pytest.raises(wx_mp.MpScanError):
        wx_mp.scan_mimicwx(["第一财经"], window_hours=24)


def test_scan_skips_invalid_table_names(fake_scan_env, monkeypatch):
    monkeypatch.setattr(wx_mp.time, "time", lambda: 1763100000)
    fake_scan_env["tables"] = ["Msg_abc", "Msg_bad; DROP TABLE x"]
    fake_scan_env["rows"]["Msg_abc"] = [(1, 1763099900, appmsg())]
    result = wx_mp.scan_mimicwx(["第一财经"], window_hours=24)
    assert len(result) == 1  # invalid table skipped, valid one parsed


def test_scan_cleans_temp_dir_on_success(fake_scan_env, monkeypatch, tmp_path):
    monkeypatch.setattr(wx_mp.time, "time", lambda: 1763100000)
    created = []
    real_mkdtemp = wx_mp.tempfile.mkdtemp

    def spy_mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(Path(d))
        return d
    monkeypatch.setattr(wx_mp.tempfile, "mkdtemp", spy_mkdtemp)
    fake_scan_env["rows"]["Msg_abc"] = [(1, 1763099900, appmsg())]
    wx_mp.scan_mimicwx(["第一财经"], window_hours=24)
    assert created and all(not d.exists() for d in created)


def test_scan_missing_dependency_raises_mp_scan_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlcipher3", None)
    with pytest.raises(wx_mp.MpScanError):
        wx_mp.scan_mimicwx(["第一财经"], window_hours=24)


def test_scan_docker_failure_raises_mp_scan_error(fake_scan_env, monkeypatch):
    def fail_run(args, **kw):
        return types.SimpleNamespace(returncode=1, stdout=b"", stderr=b"container not running")
    monkeypatch.setattr(wx_mp.subprocess, "run", fail_run)
    with pytest.raises(wx_mp.MpScanError):
        wx_mp.scan_mimicwx(["第一财经"], window_hours=24)


# --- discover (caching, health, empty) ------------------------------------

import asyncio
from osbi_client.models import ClientConfig, Source


class FakeCache:
    def __init__(self):
        self.store = {}
        self.gets = 0
        self.puts = 0

    def capture_get(self, url, scope, policy, *, now=None):
        self.gets += 1
        return self.store.get((url, scope))

    def capture_put(self, url, scope, body, policy, *, now=None):
        self.puts += 1
        self.store[(url, scope)] = body


def mp_source_obj():
    return Source(id="mp-economy", url=SENTINEL, kind="mp", accounts=["第一财经", "经济日报"])


def run_discover(hunter, source, cache, events, force_refresh=False):
    config = ClientConfig()
    return asyncio.run(hunter.discover(source, config, force_refresh=force_refresh,
                                       cache=cache, event=lambda kind, msg, level="info": events.append((kind, level))))


def test_discover_scans_caches_and_builds_seed(monkeypatch):
    calls = []
    monkeypatch.setattr(wx_mp, "scan_mimicwx", lambda accounts, window: calls.append(1) or records())
    hunter = hunter_for("mp")
    cache = FakeCache()
    events = []
    discovery = run_discover(hunter, mp_source_obj(), cache, events)
    assert discovery.material is not None
    assert discovery.seed_meta
    assert cache.puts == 1 and len(calls) == 1
    # second run within TTL reuses cache, no rescan
    again = run_discover(hunter, mp_source_obj(), cache, events)
    assert again.material["materialId"] == discovery.material["materialId"]
    assert again.seed_meta == discovery.seed_meta
    assert len(calls) == 1


def test_discover_force_refresh_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(wx_mp, "scan_mimicwx", lambda accounts, window: calls.append(1) or records())
    hunter = hunter_for("mp")
    cache = FakeCache()
    run_discover(hunter, mp_source_obj(), cache, [])
    run_discover(hunter, mp_source_obj(), cache, [], force_refresh=True)
    assert len(calls) == 2


def test_discover_accounts_change_invalidates_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(wx_mp, "scan_mimicwx", lambda accounts, window: calls.append(1) or records())
    hunter = hunter_for("mp")
    cache = FakeCache()
    run_discover(hunter, mp_source_obj(), cache, [])
    changed = Source(id="mp-economy", url=SENTINEL, kind="mp", accounts=["第一财经"])
    run_discover(hunter, changed, cache, [])
    assert len(calls) == 2  # different accounts -> different scope -> rescan


def test_discover_empty_catalog_emits_event(monkeypatch):
    monkeypatch.setattr(wx_mp, "scan_mimicwx", lambda accounts, window: [])
    events = []
    discovery = run_discover(hunter_for("mp"), mp_source_obj(), FakeCache(), events)
    assert discovery.material is None
    assert ("mp_empty_catalog", "warning") in events


def test_discover_zero_hit_account_health_warning(monkeypatch):
    # only 第一财经 hits; 经济日报 missing -> health warning
    monkeypatch.setattr(wx_mp, "scan_mimicwx", lambda accounts, window: [records()[0]])
    events = []
    run_discover(hunter_for("mp"), mp_source_obj(), FakeCache(), events)
    assert ("mp_account_health", "warning") in events


def test_compute_window_defaults_and_override(monkeypatch):
    assert wx_mp.compute_window(0) == 24
    assert wx_mp.compute_window(720) == max(24, 12 + 2)
    monkeypatch.setenv("OSBI_MP_WINDOW_HOURS", "12")
    assert wx_mp.compute_window(0) == 12
    monkeypatch.setenv("OSBI_MP_WINDOW_HOURS", "bad")
    assert wx_mp.compute_window(0) == 24
