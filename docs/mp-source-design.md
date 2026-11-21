# Design: WeChat Official Account (mp) Source Acquisition — Phase 1

Status: approved (dev branch)
Scope: osbi-net client; first phase of porting the production wiseflow mp capability.

## 1. Goals and Scope

Bring the production wiseflow WeChat Official Account (公众号, "mp") acquisition
capability into the osbi-net client as a first-class source kind, and validate it
end-to-end against the economy (经济动态) focus with its production account
whitelist.

In scope (phase 1):

- A new `mp` source kind with a simple account-name whitelist (one array per
  source, exactly like production `sources.detail`).
- A reusable **hunter** subsystem for platform-specific catalog discovery,
  sibling to `browser` and `osbi_client`; the WeChat mp hunter is the first
  implementation.
- Catalog-first acquisition: scan the local MimicWX message database for article
  catalogs, submit a links-only catalog material, let the shared server select
  relevant links, then capture selected article bodies with Camoufox and submit
  them for ontology extraction.
- Seed metadata injection (publisher / publishedAt / title) into article bodies.
- Dashboard settings improvements requested alongside this work: per-section
  save buttons, an explicit Jev master switch, and mp source editing.

Out of scope (later phases, see §12): linxiaozhu decoupling, VL image
recognition, journal/KB data plane, hard time gating, report writing/delivery,
WeChat notification chain, snapshot Q&A layer, and the missing
`seeds_shanghai.ttl`.

Non-goals for the whitelist: no per-account enable/disable, no per-account
scheduling or granularity. One mp source carries one account array; parity with
production is the explicit requirement.

## 2. Architecture

### 2.1 Data flow

```
MimicWX container (biz_message_0.db, SQLCipher + Zstd)
        │  docker exec (read key) + docker cp (db/-wal/-shm snapshot)
        ▼
hunters/wx_mp.py  ── scan ──► records[{title,url,author,publish_time,cover}]
        │  whitelist filter, mp URL normalization, url dedup, time sort
        ▼
links-only catalog Material (blocks=[], links=[L1..Ln],
        localMaps.urls + localMaps.itemMeta, meta.adapter="mp-catalog")
        │  normal durable outbox path (POST /v1/materials)
        ▼
server select stage (select_relevance) ──► selection.linkIds
        │  existing outbox_loop link-back expansion (daemon.py:394-411)
        ▼
runs.queue ◄── selected article URLs (forbiddenDomains filtered, maxUrls capped)
        │  kind-agnostic capture_page → Camoufox
        ▼
Browser.capture(session, url, lease, seed_meta=…)
        │  index_snapshot(capture, seed_meta) → body Material
        ▼
outbox → server route + summarize + graph extraction (existing path)
```

### 2.2 Relationship to existing web/rss paths

- `web`: unchanged. Entry URL is captured directly; links expand via selection.
- `rss`: unchanged in phase 1. RSS discovers URLs and pushes them straight into
  the queue (daemon.py:171-192) with **no server pre-selection**.
- `mp`: discovery produces a **catalog material**, not a URL list. The catalog
  goes through the same submission/selection machinery as any material, so the
  server's relevance model decides which articles are worth a browser capture.

Why catalog pre-selection instead of the simpler rss-style direct queue fill:

1. **Cost control.** A 22-account whitelist over a 24h window can yield far more
   articles than `maxUrls`. Direct queue fill would spend the whole browser
   budget on whatever sorts first; catalog selection spends it on articles the
   server judges relevant to the focus. Browser captures (Camoufox lease,
   anti-bot risk, verification prompts) are the scarce resource.
2. **Production parity.** wiseflow submits mp catalogs with `only_link`
   (general_process.py:42-46) and only captures selected URLs. We port behavior
   that is already validated in production, not a new approximation.
3. **Zero new server work.** The contract already allows links-only materials
   (contracts models.py:62-63), and the server select stage already returns
   `selection.linkIds` with no assignments for block-less materials
   (service.py:266-288 → work `succeeded`). The client outbox_loop already
   expands selected links into the queue. The mp path is composition of
   existing, tested machinery.
4. **Seed metadata survives selection.** itemMeta on catalog links carries
   author/publish_time/cover through the run, so body captures can be stamped
   with the scanned publisher even when the page hides its byline.

The rss-style alternative remains available as a fallback design if server
selection ever proves too aggressive, but it is explicitly rejected for phase 1.

### 2.3 Hunter subsystem

Platform-specific catalog discovery is generalized as **hunters**, so future
self-media platforms (Weibo, Zhihu, Toutiao, …) plug in without touching the
daemon again.

Location: `client/backend/hunters/` — a top-level Python package **sibling to
`browser` and `osbi_client`** under `client/backend/`, per the user's request
that hunters be a first-class, visible subsystem. Rationale:

- setuptools `packages.find` (pyproject.toml `where = ["contracts", "server",
  "client/backend"]`, `namespaces = false`) discovers it automatically once it
  has an `__init__.py`; no packaging changes needed.
- It is importable as top-level `hunters` everywhere the daemon runs (CLI, MCP,
  HTTP app all share the installed environment). The daemon imports
  `from hunters import hunter_for`.
- It sits beside `osbi_client` and `browser`, keeping platform-acquisition
  concerns in their own subsystem rather than buried inside `osbi_client`.
- Hunters are client-only. The server never learns how a catalog was produced;
  it only sees a standard Material. No contracts change.

Files:

- `hunters/__init__.py` — re-exports `hunter_for`, `Hunter`, `CatalogDiscovery`;
  imports `wx_mp` for registration side effect.
- `hunters/base.py` — protocol + registry:

```python
class CatalogDiscovery:      # frozen dataclass
    material: dict           # links-only catalog material (OSBI shape)
    seed_meta: dict[str, dict]  # canonical article URL -> {title, publisher, publishedAt}

class Hunter(Protocol):
    kind: str
    async def discover(self, source, config, *, force_refresh: bool,
                       cache, event) -> CatalogDiscovery: ...

HUNTERS: dict[str, Hunter] = {}
def register(hunter): ...        # keyed by hunter.kind
def hunter_for(kind) -> Hunter | None: ...
```

- `hunters/wx_mp.py` — the MimicWX scanner (§4), registered as kind `"mp"`.

Daemon dispatch becomes registry-driven instead of a hardcoded `mp` branch:
`hunter = hunter_for(source.kind)`; when a hunter exists and discovery has not
run for this run, execute the generic catalog-discovery flow (§5). `web`/`rss`
return `None` and keep their existing behavior. RSS may be migrated into a
hunter later; phase 1 deliberately leaves it untouched to minimize churn.

The protocol is intentionally narrow: a hunter returns a catalog plus seed
metadata; caching, submission, selection, queue expansion, and body capture all
stay in the daemon. Hunters never talk to the server and never launch browsers.

## 3. Model Changes

File: `client/backend/osbi_client/models.py` (Source, lines 22-41).

- `kind: Literal["web", "rss", "mp"] = "web"`.
- New field `accounts: list[str] = Field(default_factory=list, max_length=100)`,
  each name `min_length=1, max_length=200`, stripped; duplicates rejected.
  Because `StrictModel` is `extra="forbid"`, the field must be declared
  explicitly; existing web/rss configs are unaffected (default empty).
- `validate_url` additions:
  - `kind == "mp"` requires a non-empty `accounts` list.
  - `kind == "mp"` requires `steps == []` (steps only run on the first page of a
    run, daemon.py:317; a multi-article mp run has no meaningful first-page
    workflow, and the sentinel URL is never opened in a browser).
  - `accounts` must be empty for `web`/`rss` (fail fast on misconfiguration).

Sentinel URL: mp sources use `url = "https://mp.weixin.qq.com/"`.

- It passes the existing HTTP(S) `canonical_url` validation unchanged, and
  `Material.meta.requestedUrl` for the catalog is a valid HTTP(S) URL as the
  contract requires.
- Article URLs live on `mp.weixin.qq.com`, so every hostname-keyed mechanism in
  the daemon works without modification: `domainConcurrency` accounting
  (daemon.py:147-161), `domain_cooldowns` after `VERIFICATION_REQUIRED`
  (daemon.py:250-255, keyed on `urlsplit(source.url).hostname`), and
  domain-based capture-cache TTLs.
- The sentinel is never opened in a browser: the mp discovery branch replaces
  the initial `[source.url]` queue via catalog submission (§5), so no
  `capture_page` call ever targets it.

No contracts changes. `SourceMeta.adapter` is a free string; the catalog uses
`adapter="mp-catalog"` for observability (requires the small daemon meta tweak
in §5.3).

## 4. Hunter Implementation: `hunters/wx_mp.py`

A faithful port of `/home/dsw/midplatform/wiseflow/core/wis/wx_crawler.py`
(`_scan_biz_messages` and helpers), adapted to OSBI conventions (English
comments/docstrings, `logging`/event callback instead of `wis_logger`, no
wiseflow cache manager dependency).

### 4.1 Scan pipeline (read-only)

1. `docker exec <container> cat <keys_file>` → parse JSON → key
   `message/biz_message_0.db` (fallback `db_storage/message/biz_message_0.db`).
   **Validate the key is exactly 96 hex chars before interpolating it into
   `PRAGMA key = "x'<hex>'"`** (hardening over production).
2. `docker cp` the trio `biz_message_0.db`, `-wal`, `-shm` into a fresh
   `tempfile.mkdtemp(prefix="osbi_mp_")`. Missing `-wal`/`-shm` tolerated;
   missing main db is a hard error. Copying avoids reading a database WeChat is
   actively writing.
3. Open with `sqlcipher3.connect`, `PRAGMA key`, `PRAGMA cipher_compatibility
   = 4`, verify with `SELECT count(*) FROM sqlite_master`.
4. Enumerate `Msg_%` tables from `sqlite_master`; **validate each table name
   against `^Msg_[A-Za-z0-9_]+$` before quoting it into SQL** (hardening over
   production's raw interpolation). Per table:
   `WHERE (local_type & 0xFFFF) = 49 AND create_time > ?` with a parameterized
   cutoff (`now - window_hours*3600`), `ORDER BY create_time DESC`.
5. For each blob `message_content`: check Zstd magic `\x28\xb5\x2f\xfd`,
   decompress with `zstandard`, regex-extract CDATA fields
   `<title>/<url>/<name>(account name)/<pub_time>/<cover>` (same regexes as
   production). `pub_time` → `YYYY-MM-DD` local date (production parity).
6. Filter by exact account-name match against `source.accounts`; normalize the
   article URL (§6); dedup by normalized URL; sort by `publish_time` desc
   (undated last).
7. `finally`: close connection, `shutil.rmtree(tmp_dir, ignore_errors=True)`.
   The copied database contains **all** private messages; it must never outlive
   the scan, and neither key material nor message bodies may be logged.

The whole scan is blocking subprocess + SQL work; the daemon runs it via
`asyncio.to_thread` (production does the same). Subprocess timeouts: 30s exec,
60s cp (production values).

### 4.2 Account health diagnostics

After filtering, compute whitelist accounts with zero hits in the window and
emit a warning event through the injected `event` callback
(`state.event("mp_account_health", …, level="warning")`), listing the missing
names. Zero hits usually means: the local WeChat account does not follow the
account, no push arrived in the window, or the configured name does not exactly
match the WeChat display name. This mirrors production's 账号健康度 log and is
the primary operational signal for whitelist typos.

If the scan returns zero records overall, the run finishes `succeeded` with an
`mp_empty_catalog` warning event (no material is submitted — the contract
requires at least one link). Scan *failures* (container down, key missing,
wrong key, missing dependency) raise `MpScanError` with an actionable message;
the daemon surfaces it as a failed run with the specific message (§5.4).

### 4.3 Catalog material synthesis

Port of `catalog_material.py:7-29` ("Catalogs contain exploration targets,
never title-only extraction blocks"), adapted to the OSBI Material shape:

- `blocks=[]`; one link per record, `id="L{n}"`, `text=title` (skip records
  without title/url), optional `context` unused in phase 1 (mp `<des>` is
  always empty in production).
- Dedup by normalized URL; cap at 200 links (contract limit), keeping the
  newest by `publish_time`.
- `localMaps.urls[link_id] = normalized_url`;
  `localMaps.itemMeta[link_id] = {author, publish_time, cover}`.
- `meta`: `requestedUrl = source.url` (sentinel), `adapter="mp-catalog"`,
  `capturedAt = utcnow iso`, `title = "MP catalog: <source.id>"`.
- `status = {"access":"ok","load":"ready"}`; `materialId` = digest over the
  sorted normalized URLs (stable identity for requestId/cache).
- `seed_meta` output: `{canonical_url(normalized_url): {"title":…,
  "publisher": account_name, "publishedAt": publish_time}}` — publisher is the
  scanned account name, never the article byline (production rule).

### 4.4 Configuration

Environment variables, same names as production for operational parity:

- `MIMICWX_CONTAINER` (default `mimicwx-linux`)
- `MIMICWX_WX_USER_DIR` (default
  `/home/wechat/Documents/xwechat_files/lonely2012_b040`)
- `MIMICWX_KEYS_FILE` (default `/home/wechat/.xwechat/wechat_keys.json`)
- `OSBI_MP_WINDOW_HOURS` (optional override of the scan window)

Scan window: `window_hours = max(24, ceil(intervalMinutes/60) + 2)` for
scheduled sources (2h overlap guards against schedule drift; dedup and caches
absorb the overlap), 24h for manual runs; env override wins. (Production mp
tasks use 12h; 24h is the safer OSBI default and is tunable via env.) Constants
live in `wx_mp.py`, no magic numbers inline.

### 4.5 Dependencies

Add to `[project.optional-dependencies].client` in
`/home/dsw/osbi-net/pyproject.toml` and pin in
`/home/dsw/osbi-net/requirements.lock`:

- `sqlcipher3-binary==0.6.0` — wheel bundles SQLCipher; no system
  `libsqlcipher-dev` needed. **Verified: cp312 manylinux2014_x86_64 wheel
  available** for the OSBI runtime (Python 3.12.16).
- `zstandard==0.25.0` — cp312 manylinux wheel verified.

Both imports are **lazy inside the scan function**: clients without mp sources
keep working with no new imports; a missing dependency raises `MpScanError`
with an install hint instead of an ImportError traceback at startup.

## 5. Daemon Integration

File: `client/backend/osbi_client/daemon.py`.

### 5.1 Discovery branch (replaces the hardcoded-kind pattern)

At the top of `acquire` (currently daemon.py:171, the rss branch):

```
from hunters import hunter_for
hunter = hunter_for(source.kind)
if hunter and not browser_state.get("hunter_discovered"):
    discovery = await hunter.discover(source, config,
        force_refresh=bool(browser_state.get("forceRefresh")),
        cache=self.cache, event=self.state.event)
    # submit catalog through the shared submission tail (§5.2)
    …
```

The rss branch stays as-is above/below it; `web` falls through unchanged.

### 5.2 Submitting the catalog without a browser capture

Refactor the submission tail of `acquire` (meta validation → Material assembly
→ materialHash → Jev → outbox insert → queue/visited/state update, lines
218-247) into a helper, e.g.
`_submit_material(run_id, source, config, url, material, queue, visited, browser_state)`,
used by both the normal capture path and the discovery path. This is a
behavior-preserving extraction for web/rss.

Discovery calls the helper with `url=source.url`, `queue=[source.url]`,
`visited=[]`, and the new browser state:

```
{"hunter_discovered": True,
 "seedMeta": discovery.seed_meta,
 **({"forceRefresh": True} if force_refresh else {})}
```

Result: catalog payload lands in the outbox, `visited=[sentinel]`, `queue=[]`,
run state `waiting_server`. The existing `outbox_loop` selection handling
(daemon.py:394-411) then maps `selection.linkIds` → URLs from the payload,
filters `forbiddenDomains`, dedups against visited/queued, appends to the queue
(capped at `maxUrls`) and flips the run back to `pending`. Subsequent `acquire`
iterations see `hunter_discovered` set and take the ordinary kind-agnostic
browser path for each article. No outbox_loop changes are required.

Notes:

- **forceRefresh**: consumed for the catalog (bypasses catalog cache), and —
  unlike rss, which drops the flag when it overwrites browser_state
  (daemon.py:192) — deliberately **retained** so article captures also bypass
  the capture cache on a refresh run. The overwrite is safe here because
  discovery runs before any lease exists.
- **maxUrls budget**: the sentinel counts as one visited URL, so the effective
  article budget is `maxUrls - 1`. Documented; acceptable. (Alternative —
  exempting the catalog from `visited` — adds special-casing to the completion
  logic and is rejected.)
- **Jev pre-submit**: `allow_submission` (decisions.py:62-65) judges blocks +
  link texts as one item; a Jev "discard" would silently kill the entire
  catalog. For links-only catalog materials (`blocks == []` and
  `meta.adapter == "mp-catalog"`) the discovery path **skips Jev preSubmit**;
  server relevance selection is the gate, matching production (`only_link`
  submission without local pre-filtering). Article bodies keep normal Jev
  behavior.

### 5.3 Seed metadata injection into body captures

- `Browser.capture` (browser.py:63) gains a keyword-only parameter
  `seed_meta=None`, forwarded to `index_snapshot(capture, seed_meta)` — the
  parameter already exists (snapshot_index.py:187) but is currently never
  passed. No other call sites change.
- `capture_page` (daemon.py:296-339) looks up
  `browser_state.get("seedMeta", {}).get(canonical_url(url))` and passes it to
  `browser.capture`. No signature change to `capture_page` itself.
- Merge semantics: `index_snapshot` (line 194) merges
  `{**seed_meta, **non_empty_dom_meta}` — **non-empty DOM values win**. This is
  acceptable and intentionally kept: `dom-material.js` already reads
  WeChat-native fields (`#js_name` → publisher, `#publish_time` → publishedAt,
  `#js_author_name` → author), so the DOM publisher is the account name, not
  the byline; the production "publisher over byline" rule is satisfied by
  OSBI's separate author/publisher fields. Seeds fill gaps (hidden/deleted
  metadata, verification-stripped pages) and provide the title fallback.
  Deviation from a strict seed-wins port is documented here; if field evidence
  shows DOM metadata disagreeing with the scan, a seed-priority option can be
  added to `index_snapshot` later without contract changes.
- Meta assembly tweak (daemon.py:219-220): allow `adapter` passthrough
  (`adapter = meta.get("adapter") or "browser"`) so catalogs are identifiable
  server-side and in events; bodies remain `"browser"`. `textOnly=True` stays
  forced.

### 5.4 Error handling

- `MpScanError` is caught explicitly in the discovery branch: run → `failed`
  with the specific message (container not running / key missing / dependency
  missing / decrypt failed) plus an error event. The generic `except Exception`
  handler (daemon.py:265-267) must not swallow these into the generic message.
- Article-level `VERIFICATION_REQUIRED` keeps the existing behavior: domain
  cooldown on `mp.weixin.qq.com` (300s/900s strikes), human prompt, resume.
  Because the sentinel shares the article hostname, the cooldown correctly
  pauses *all* mp runs on that client while a human solves the verification.

## 6. URL Normalization and Dedup

`canonical_url` (contracts urls.py:17-19) strips only `utm_*`, `spm`, `fbclid`,
`gclid`; WeChat tracking params (`scene`, `chksm`, `subscene`, `ascene`, …)
survive, so the same article pushed with different share parameters would not
dedup. Normalization is therefore done **in the hunter, at discovery time**,
before URLs enter links/queue/visited:

`mp_article_url(url)` in `wx_mp.py`:

- Host must be `mp.weixin.qq.com` and path must be `/s/<id>` or `/s`; other
  URLs are dropped from the catalog (they are not article permalinks).
- `/s/<id>`: keep scheme + host + path, drop the entire query.
- `/s?…`: keep only `__biz`, `mid`, `idx`, `sn` (the article identity tuple),
  drop everything else (`scene`, `chksm`, `subscene`, `ascene`, `devicetype`,
  `version`, `lang`, `exportkey`, `pass_ticket`, `from`, `isappinstalled`,
  `clicktime`, `enterid`, sharer params, …).
- Result feeds `canonical_url`, so all downstream dedup (discovery `seen`,
  queue/visited checks, outbox link-back, capture-cache aliases) works
  unchanged.

The original URL is retained in `itemMeta` for diagnostics. Risk: a small
number of links may require extra query params to resolve; if field evidence
shows broken captures, the fallback is to queue the original URL while keeping
the normalized form as an additional `visitedAliases` entry. Phase 1 chooses
the simpler normalize-then-queue approach because public `/s` permalinks
resolve with the identity tuple alone.

Cross-run dedup: the capture cache (§7) plus server-side material identity
absorb re-discovered articles; the client does not persist a global seen-URL
set in phase 1 (production relies on its KB for that; see §12).

## 7. Caching

Two distinct cache concerns, both on the existing `LocalCache`:

1. **Catalog cache** (short-lived). Scope:
   `digest(["mp-catalog/v1", source.profile, sorted(source.accounts), window_hours])`
   — accounts are part of the scope, so whitelist edits invalidate it; `kind`
   isolation is inherent because the scope prefix differs from
   `capture-material/v1`. The cached body is the full catalog material
   (`capture_put` accepts links-only bodies, cache.py:52); on a hit, seed_meta
   is reconstructed from `localMaps.urls` + `localMaps.itemMeta`.
   TTL conflict: catalog and articles share the `mp.weixin.qq.com` hostname,
   so the domain TTL cannot serve both. Solution without schema changes: pass
   a derived policy to the catalog cache calls —
   `config.captureCache.model_copy(update={"defaultTtlSeconds": 900,
   "domainTtlSeconds": {}})` (15-minute constant; production uses 120s, but
   OSBI runs are interval-driven rather than multi-focus shared scans).
   `forceRefresh` bypasses the catalog cache.
2. **Article capture cache** (long-lived). Configure
   `captureCache.domainTtlSeconds["mp.weixin.qq.com"] = 604800` (7 days) in the
   client config; article bodies are immutable once published, and this is
   what makes overlapping scan windows cheap. The existing per-domain TTL
   machinery (cache_settings.py:28-31) needs no changes.

Submission caching is unchanged: catalog and body materials each get their own
`materialHash`-based submission-cache identity.

## 8. Dashboard Frontend Changes

File: `client/backend/osbi_client/static/app.js` (settingsPage, lines 127-250).
The backend already supports partial updates: `State.save_config` merges the
PATCH body over the stored config and revalidates the whole model
(state.py:56-78), so per-section saving needs no API change.

### 8.1 Per-section save buttons

Replace the single global `save` button (app.js:214-231, 247) with one save
button per card, each PATCHing only its own keys:

- 01 共享服务 → `{serverUrl, serverCA, serverKey?}` (key only when non-empty).
- 02 采集信源 → `{focus, sources}` (with the existing `stepsText` JSON parsing).
- 03 本地采集偏好 → `{maxUrls, browserConcurrency, domainConcurrency,
  timeoutSeconds, headless, forbiddenDomains, captureCache, submissionCache}`.
- 04 Jev → `{jev}` (apiKey only when non-empty).
- 05 报告方案 → `{reportProfiles}`.

Implementation points:

- Extract a shared `saveSection(button, buildUpdate, onSuccess)` helper:
  disable button → PATCH → toast/banner → refresh masked-key placeholders from
  the response (`keySuffix`, `jev.keySuffix`) → re-enable. JSON parse errors
  stay section-local (a bad steps JSON no longer blocks saving the server URL).
- Each successful save bumps `config.revision`; sections read fresh values from
  the PATCH response where relevant. Note the concurrency semantics: saving
  section B after editing section A without saving A discards A's unsaved edits
  only in the sense that they were never sent — unchanged from today, but now
  more visible; add a per-section "unsaved changes" hint if cheap.
- Keep the section index sidebar; add the two missing entries (Jev, 报告方案).

### 8.2 Jev master switch

`jev.enabled` already exists in the model and gates all Jev calls
(decisions.py:9), but the UI renders it as an ordinary checkbox among seven
fields (app.js:144, 240). Promote it to a prominent master toggle in the card
title bar (e.g. a labeled switch "启用 Jev 本地决策"):

- Bound to `jev.enabled`; when off, visually disable (not hide) the dependent
  fields (preSubmit/postQuery/baseUrl/model/key/confidence) so users see what
  the switch controls.
- Saving the Jev section always includes `enabled` from the master toggle.
- Validation reminder surfaced in the hint: enabling remote Jev requires its
  own API key (models.py:64-65); the section save will return that error
  inline.

### 8.3 mp source editing

In `renderSources` (app.js:193-212):

- Add `el("option", {value:"mp"}, "微信公众号")` to the kind select (line 199).
- When `kind === "mp"`: show an accounts textarea (one account name per line,
  mapped to `accounts: string[]`), auto-fill the URL field with the sentinel
  `https://mp.weixin.qq.com/` (read-only hint explaining it is a fixed entry),
  and hide the steps editor (model requires empty steps for mp).
- Switching kind back to web/rss clears `accounts` client-side to satisfy the
  model validator.
- Hint text: names must exactly match the WeChat display names followed by the
  local MimicWX WeChat account; zero-hit accounts are reported in notifications.

This section is required for the phase-1 economy joint debugging (the user
asked for the frontend work to ride along), but mp sources can also be
configured via HTTP/CLI without it if frontend timing slips.

## 9. Configuration Example (economy focus, authoritative production values)

Client config fragment (`PATCH /api/config` or settings UI). The focus text and
the 22-account whitelist below are copied verbatim from the production wiseflow
economy task (`data.db` tasks id 2 `#经济动态`, focus id 9, mp source `detail`
array). Names must match WeChat display names exactly.

```json
{
  "focus": "经济形势、稳定增长、促进就业、货币政策、稳定物价、提振经济、降准降息、稳定楼市股市、高质量发展、新质生产力、蓝色生产力、协作生产力、绿色经济",
  "sources": [
    {
      "id": "mp-economy",
      "kind": "mp",
      "url": "https://mp.weixin.qq.com/",
      "accounts": [
        "第一财经", "第一财经资讯", "经济日报", "上海商务",
        "投资上海 Invest Shanghai", "上海发展改革", "央视财经",
        "首席经济学家论坛", "毕马威KPMG", "法学学术前沿", "政治学公开课",
        "腾讯研究院", "中国信息安全", "中国证券报", "TOP创新区研究院",
        "国家发展改革委", "数据仓库与Python大数据", "原著理论学习笔记",
        "求是网", "新华社", "不让未来打盹研究", "罗兰贝格管理咨询"
      ],
      "enabled": true,
      "intervalMinutes": 0,
      "profile": "default",
      "steps": []
    }
  ],
  "maxUrls": 30,
  "captureCache": {
    "domainTtlSeconds": { "mp.weixin.qq.com": 604800 }
  }
}
```

`intervalMinutes` is 0 (manual) for joint debugging; production runs twice
daily (12:18 / 23:18), approximated by `intervalMinutes: 720` once validated.

Deployment prerequisites on the client host: Docker reachable, `mimicwx-linux`
container running with a logged-in WeChat that follows every whitelisted
account, `wechat_keys.json` present, and the `[client]` extra installed with
`sqlcipher3-binary` + `zstandard`.

## 10. TDD Test Plan

Write tests first (RED), then implement (GREEN). pytest, existing conventions
(`tests/`, pythonpath already wired in pyproject.toml).

### 10.1 Unit — `tests/test_mp_hunter.py`

Scanner (mock `subprocess.run` for docker exec/cp; inject a fake `sqlcipher3`
module via `monkeypatch.setitem(sys.modules, …)`; build real Zstd-compressed
appmsg XML fixtures with `zstandard`):

- parses a valid type-49 message into {title,url,author,publish_time,cover}.
- rejects non-Zstd blobs, bad magic, missing title/url, non-bytes content.
- whitelist filtering is exact-match; non-whitelisted authors excluded.
- dedups by normalized URL; sorts by publish_time desc, undated last.
- URL normalization: `/s/<id>` drops query; `/s?__biz&mid&idx&sn&scene&chksm`
  keeps only the identity tuple; non-`/s` mp paths and foreign hosts dropped.
- key validation: non-96-hex key rejected before any PRAGMA interpolation;
  table names not matching `^Msg_[A-Za-z0-9_]+$` skipped.
- temp dir removed on success and on every failure path (monkeypatch
  `tempfile.mkdtemp` to a spy path).
- missing `-wal`/`-shm` tolerated; missing main db raises MpScanError.
- zero-hit accounts produce a health warning event; empty result yields no
  material; docker failure / decrypt failure raise MpScanError with actionable
  messages; missing sqlcipher3/zstandard imports raise MpScanError, not
  ImportError.
- catalog synthesis: blocks empty, links ≤ 200 with unique ids, localMaps
  urls/itemMeta correct, meta.requestedUrl = sentinel, adapter = mp-catalog,
  seed_meta keyed by canonical URL with publisher = account name.
- registry: `hunter_for("mp")` returns the wx hunter; `hunter_for("web"/"rss")`
  returns None; registering a duplicate kind fails loudly.

### 10.2 Unit — model validation (extend `tests/test_client.py`)

- mp source with accounts + sentinel validates; accounts empty → error.
- mp with non-empty steps → error; web/rss with accounts → error.
- accounts trimming/duplicate rejection; unknown extra field still forbidden.
- existing web/rss configs round-trip unchanged (regression).

### 10.3 Integration — `tests/test_mp_pipeline.py`

Follow the `tests/test_client.py` harness pattern (fake Browser recording
calls, `Daemon(tmp_path, browser=…, transport=httpx.MockTransport(…))`,
monkeypatch the hunter's scan function to return fixture records):

- full chain: start_run(mp) → acquire submits a links-only catalog (no browser
  `open` call for the sentinel) → outbox POST captured by mock transport →
  job result with `selection.linkIds` → queue expanded with selected URLs only
  (forbiddenDomains filtered, maxUrls capped) → acquire captures article with
  fake browser → body payload meta carries publisher/publishedAt from seed_meta
  when DOM meta is empty → second outbox submission → run succeeded.
- selection of zero links → run succeeds without article captures.
- catalog cache: second run within TTL reuses catalog (scan called once);
  accounts change invalidates scope; forceRefresh bypasses catalog and article
  caches and is retained through discovery.
- Jev enabled with a discard-all decision transport: catalog still submitted
  (bypass), article bodies discarded (normal path).
- VERIFICATION_REQUIRED on an article: cooldown row for mp.weixin.qq.com,
  prompt created, resume continues.
- MpScanError → run failed with the specific message; health event recorded.
- seed_meta merge: DOM-provided publisher wins over seed; seed fills empty DOM
  fields (index_snapshot behavior regression).
- daemon refactor regression: existing web/rss tests in test_client.py and
  test_material_lifecycle.py pass unchanged.

### 10.4 Frontend

No JS test harness exists in the repo; verify manually (checklist): each
section saves independently and invalid JSON stays section-local; Jev master
toggle disables dependents and persists `enabled`; mp kind shows accounts
input, auto-fills sentinel, hides steps; switching kinds clears accounts.
Add a backend-side regression test that a PATCH containing only `{jev}` or
only `{sources}` leaves other sections intact (save_config merge semantics).

Coverage target: ≥80% on new/changed Python modules
(`hunters/*`, daemon discovery path, model validators).

## 11. Risks and Boundaries

- **WeChat not following an account / zero hits.** Discovery is push-based: the
  local WeChat must follow the account and have received the article. Health
  diagnostics (§4.2) surface this; it is an operational precondition, not a
  bug. Name spelling must equal the WeChat display name exactly.
- **Verification pages.** mp article captures can hit
  `VERIFICATION_REQUIRED`; existing prompt + domain-cooldown machinery handles
  it, and the shared sentinel hostname means one verification pauses all mp
  runs on the client (desired politeness behavior). Headed mode recommended
  for mp profiles.
- **MimicWX container dependency.** The mp hunter only works on hosts running
  the MimicWX container with a logged-in WeChat. Other OSBI clients cannot use
  mp sources; failure is explicit (MpScanError), never silent. This couples
  phase-1 mp deployments to the production machine; acceptable for the economy
  joint debugging, revisited in later phases.
- **WeChat upgrade breaks the key scheme.** The SQLCipher key layout, DB
  paths, and cipher_compatibility=4 are tied to the frozen WeChat 4.1.1.8
  image. Any WeChat upgrade requires re-validating the scanner; env-overridable
  paths soften but do not eliminate this.
- **sqlcipher3-binary wheel availability.** Verified cp312 manylinux wheel for
  the OSBI runtime; other platforms must confirm wheel availability (source
  build against libsqlcipher-dev is the documented fallback).
- **Privacy.** The copied DB contains all private messages. Mitigations:
  read-only access, immediate temp-dir cleanup in `finally`, whitelist
  filtering before anything leaves the scanner, no logging of keys or message
  bodies, only title/url/account metadata exported.
- **Jev catalog bypass** is a deliberate policy deviation (§5.2); if local
  pre-filtering of catalogs is wanted later, it should judge per-link items,
  not the whole catalog as one item.
- **URL normalization edge cases** (§6): rare permalinks needing extra params;
  fallback documented.
- **maxUrls budget** includes the sentinel (§5.2); size configs accordingly.
- **Server selection quality** determines recall; the economy joint debugging
  must compare selected-article counts against production before sign-off.

## 12. Phased Migration Roadmap (overview)

Phase 1 (this document): mp hunter + catalog pre-selection + economy joint
debugging + settings UI improvements.

Later phases — wiseflow capability gaps to port (overview only):

- **VL image recognition** for article images (currently `mediaPending`
  text-only submission).
- **Journal / KB data plane** (wiseflow data.db + qanything KB equivalents;
  cross-run article dedup and historical recall live here).
- **Hard time gating** for freshness-critical boards.
- **Report writing and delivery** (topnews/weekly composition and push).
- **notify WeChat chain** (MimicWX/awada outbound messaging).
- **Snapshot Q&A layer** (query_snapshot-style evidence answering).
- **Missing `seeds_shanghai.ttl`** schema seed for the server.

The linxiaozhu decoupling is the main battleground of the later phases:
`schedule_tasks.py`, workflows HTTP surface, qanything KB, shadow retrieval,
and `run_all` orchestration must be separated from the monolith before these
capabilities land in osbi-net. The hunter registry (§2.3) is the extension
point for additional self-media platforms along the way.

## 13. Implementation Order

1. Dependencies: add `sqlcipher3-binary==0.6.0`, `zstandard==0.25.0` to
   `[client]` extra + `requirements.lock`; verify wheel install in the OSBI
   venv (done — cp312 wheels confirmed).
2. Model changes + validation tests (§3, §10.2).
3. `hunters/base.py` + registry tests; `hunters/wx_mp.py` scanner + catalog
   synthesis with unit tests (§4, §10.1).
4. Daemon: extract `_submit_material` (regression-tested against existing
   web/rss tests), registry-driven discovery branch, adapter passthrough,
   MpScanError handling (§5.1-5.2, 5.4).
5. `Browser.capture(seed_meta=…)` + `capture_page` lookup + injection tests
   (§5.3).
6. Integration pipeline tests (§10.3) — full mocked chain green.
7. Frontend: per-section save, Jev master switch, mp editing (§8).
8. Live economy joint debugging on the production host: real scan, real
   server selection, compare against production output; tune window/TTL/maxUrls.

Steps 2-3 and 7 are independently mergeable; 4-6 form the core slice.

## 14. Implementation Notes from Joint Debugging

Two real issues surfaced during the first live economy run and were fixed:

1. **Browser headed-mode stickiness (`browser.py`).** The CLI re-derives
   `headless` from the presence of `--headed` on every invocation, and `open`
   re-applies launch options when any explicit flag is present (cli.ts:117-121).
   The facade passed `--headed` only on `start` but `--geoip` on every command,
   so `open` re-derived `headless:true` and its re-start conflicted with the
   running headed browser (`MODE_CONFLICT`). Fix: `Browser` remembers headed
   sessions and passes `--headed` on every command for a headed session.
   Regression test: `test_headed_mode_is_sticky_across_session_commands`. This
   was a pre-existing bug exposed by the first real browser acquisition.

2. **Asset home vs profile home (deployment env).** The daemon's `Browser.home`
   is `<client-data>/browser` (profiles/state), while the installer places assets
   under `~/.osbi-browser`. The runtime resolves assets via
   `OSBI_BROWSER_ASSETS || (CAMOUFOX_INSTALL_DIR || OSBI_BROWSER_HOME)/assets/…`,
   so the service must set `CAMOUFOX_INSTALL_DIR=~/.osbi-browser` (added to
   `osbi-client.service`) to find the installed Camoufox/model/addon/GeoIP.
   Without it the runtime fails `START_FAILED` (asset missing).

Live run outcome: scan → links-only catalog → server relevance selection
(queue expanded to maxUrls) → Camoufox `open`/`snapshot`/`eval` all succeeded;
the first article hit a WeChat verification page and the run correctly paused
`waiting_for_user` with a prompt (existing verification fallback). Passing the
verification in the headed browser (display :0) and resuming
(`osbi-client resume <run_id> --prompt-id <prompt_id>`, which sets `pageReady`)
continues acquisition. Account-health diagnostics reported 3 zero-hit accounts
(不让未来打盹研究 / 原著理论学习笔记 / 罗兰贝格管理咨询) over the 24h window.

## 15. WeChat-Specific Enhancements (production parity)

Follow-up improvements ported from the production camoufox_runtime, all tested
(183 Python + 4 frontend tests green) and validated in a live run:

- **https normalization (`wx_mp.mp_article_url`).** The MimicWX message XML stores
  `http://` article URLs (verified: 90/90 sampled records were http). Production
  `canonicalUrl` forces `https:` for mp.weixin.qq.com; the hunter now does the
  same, dropping share/tracking params and keeping the `__biz/mid/idx/sn` identity
  tuple. Catalog links and queued article URLs are https.
- **Description folding (`wx_mp`).** `<des>` is present on a minority of articles
  (23/410 sampled type-49 messages; many are empty or zero-width filler).
  `parse_49_message` extracts it, `clean_description` strips zero-width chars, and
  `build_catalog` folds a real description into the link text (`title\ndes`) so the
  server relevance model judges on title + abstract. `seed_meta.title` stays the
  pure title for body metadata.
- **Gate auto-click (`browser._wechat_gate`).** Before readDom, WeChat pages are
  probed for `#js_content` body vs the gate (`wappoc_appmsgcaptcha` / 「当前环境异常」).
  On a gate the 「去验证」 button is clicked once — this often opens the article
  directly without a slider. A visual slider is never solved; a persistent gate
  falls through to the existing `VERIFICATION_REQUIRED` human prompt.
- **Body readiness (`browser._wechat_gate`).** The probe waits for `#js_content`
  text before capture, replacing a blind readDom on WeChat pages.

Live result after these changes: catalog (149 https links, 9 with folded
descriptions) → server selection → multiple article bodies captured and submitted
with seed publisher (经济日报 / 第一财经) and graph extraction completed, without
human intervention until a genuine slider gate paused the run.

## 16. Human-Verification Wait Timeout

A run paused at `waiting_for_user` (headed browser showing a verification a human
must solve) is terminated once the wait exceeds the local page-cache validity
(`captureCache.defaultTtlSeconds`); beyond that window the cached page could not
be reused anyway. When the page cache is disabled (TTL 0) a 7200s (120 min)
fallback applies. `Daemon._expire_stale_prompts` runs each `run_loop` tick,
cancels stale waits with a `run_cancelled` warning event, and the existing
cancelled-run path releases the browser lease. Resuming a wait requires the
current prompt id (`act` rejects a stale prompt id with 「人工提示已失效」); passing
verification in the browser alone does not resume the run.
