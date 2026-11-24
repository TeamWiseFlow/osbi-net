# Design: Body-Image Vision Recognition (VL Image-to-Text)

Status: approved (dev branch)
Scope: osbi-net client. Port of the production wiseflow image-vision capability;
implements the "VL image recognition" item deferred in `docs/mp-source-design.md` §12.

## 1. Goals and Scope

Production wiseflow recognizes article images with a vision LLM before submission,
so text embedded in images (notice screenshots, tables, slogans, scanned documents)
reaches extraction. The osbi-net client is currently text-only: `Browser.capture`
preserves image evidence but never fills `image.text`, and every page with images is
submitted with `meta.mediaPending=True`.

The indexing layer already knows how to consume recognized image text: `_image_text`
splices `image.text` into the segment body at the recorded DOM offset
(snapshot_index.py:135-147), and `index_snapshot` records every eligible image in
`localMaps.images` (snapshot_index.py:211-216). The only missing stage is enrichment
between DOM capture and indexing. This design adds exactly that stage, opt-in, with
zero behavior change when disabled.

In scope:

- `VisionConfig` settings section (dashboard, CLI `configure`, MCP `configure`),
  default off.
- `osbi_client/vision.py`: OpenAI-compatible VL client with the production prompt,
  model chain, retry/backoff, and invalid-image classification.
- `osbi_client/image_material.py`: `ImageEnricher` port with eligibility heuristics,
  decorative/tracker filtering, per-URL dedup, result caching, invalid-image events.
- Integration into `Browser.capture` before `index_snapshot`; daemon wiring from the
  run's frozen config snapshot.
- Updated `mediaPending` semantics; `textOnly` unchanged; no contract or server changes.
- A bounded image-text cache table in `LocalCache`.
- A settings UI section with master toggle and per-section save.

Out of scope (later phases):

- Client-side image download / base64 data-URL mode (candidate mitigation for WeChat
  hotlink protection; §13).
- Vision on hunter/catalog materials (links-only, no body images).
- Binary media submission; SVG/canvas OCR (dom-material.js already excludes these);
  env-var model overrides (config is the single interface, unlike production
  `VL_MODEL` / `VL_FALLBACK_MODEL` / `VL_EXTRA_MODEL`).

## 2. Architecture and Data Flow

```
Browser.capture(session, url, lease, seed_meta)
  snapshot → readDom ×2 → stability/verification checks  (unchanged)
  capture = {requestedUrl, finalUrl, canonicalUrl, capturedAt, status, dom}
        │
        ▼  NEW (only when vision.enabled)
ImageEnricher.enrich(capture)                            image_material.py
  prepare_capture(capture) → working copy                snapshot_index.py:42
  for segment where image_eligible(segment, dom):        snapshot_index.py:86
    per image: heuristics → cache → VisionClient.recognize(url)
    fills image.text / source / status on the copy
        │
        ▼
index_snapshot(capture, seed_meta)                       snapshot_index.py:187
  _image_text splices image.text at offset               snapshot_index.py:135-147
  localMaps.images I{n} → {url, src, text, source, status}
  blocks now contain image-derived text
        │
        ▼
CAPTURE_EMPTY check (now sees image-only pages)
mediaPending recomputed (§9)
        │
        ▼
daemon._submit_material → outbox → server
  textOnly=True (unchanged), mediaPending per §9
```

Properties:

- Enrichment runs before indexing and before the emptiness check, so a pure-image
  article (no DOM text) becomes capturable instead of raising `CAPTURE_EMPTY`.
- `index_snapshot`, `_image_text`, `image_eligible`, `prepare_capture` are not modified.
- All existing heuristics that avoid VL calls (alt text, surrounding text, trackers,
  tiny/broken images) are preserved, so most images never reach the provider.

## 3. Configuration Model

New `VisionConfig(StrictModel)` in models.py, modeled on `JevConfig`:

| Field | Type / bounds | Default |
|---|---|---|
| enabled | bool | False |
| baseUrl | str, HTTPS or loopback HTTP | https://api.siliconflow.cn/v1 |
| apiKey | str ≤500 | "" |
| model | str 1..200, required | Qwen/Qwen3-VL-32B-Instruct |
| fallbackModel | str 0..200, optional | Qwen/Qwen3-VL-30B-A3B-Instruct |
| extraModel | str 0..200, optional | zai-org/GLM-4.5V |
| concurrency | int 1..6 | 3 |
| timeoutSeconds | int 10..600 | 240 |

Validator mirrors `JevConfig.validate_endpoint`: strip and rstrip baseUrl; reject
userinfo/query/fragment; require HTTPS, or HTTP on localhost/127.0.0.1/::1;
`enabled` + remote endpoint requires a non-empty apiKey. Empty fallback/extra entries
are dropped from the chain (§4).

Wiring:

- `ClientConfig.vision: VisionConfig = Field(default_factory=VisionConfig)`.
  Additive with defaults: stored configs predating this feature validate unchanged;
  `extra="forbid"` still rejects unknown keys.
- `save_config` deep-merge list becomes `("jev", "captureCache", "submissionCache",
  "vision")` (state.py:59), so partial PATCHes preserve the stored apiKey.
- `public_config` masks `vision.apiKey` exactly like jev (state.py:48-54): pop the
  key, expose `keyConfigured` / `keySuffix` (last 4). This automatically covers
  GET /api/config, MCP `settings`, and CLI/MCP `configure` responses.
- No environment overrides. osbi's dashboard/CLI is the configuration interface, and
  run snapshots already freeze config per run.

Defaults ship the production model names (public identifiers, no secrets). The key is
never defaulted, logged, or committed; §11 uses a placeholder.

## 4. vision.py — VL Client

Ported constants (source: wiseflow llm_client.py, image_client.py):

- `PROMPT` — verbatim from image_client.py:16-19: extract legible text verbatim in
  reading order, no guessing/completion/rewriting, no invented slogans; pure
  decoration (logo/icon/pattern/divider) → `§decorative-image§`; textless body photo →
  one short scene description wrapped in （）, no identity/place/event inference;
  output only text/description/marker.
- Request shape: `POST {baseUrl}/chat/completions`, `Authorization: Bearer {apiKey}`,
  body `{model, messages:[{role:"user", content:[{type:"image_url",
  image_url:{url, detail:"high"}}, {type:"text", text:PROMPT}]}], temperature:0.1,
  max_tokens:8192, stream:false}`.
- `RETRY_STATUSES={429,500,502,503,504}`, `MAX_ATTEMPTS=3`, backoff `20·2^attempt`
  seconds.
- Invalid-image classification: for image requests, any 400/413/415/422 is an invalid
  image; additionally provider codes `20015`/`20040` or the detail regex
  (`invalid image|not a valid image|downloadable URL|image.{0,40}(invalid|unsupported|too large|exceed)`).

Errors:

- `VisionError(message, *, retryable=True, status=None, code=None)`.
- `InvalidImageError(VisionError)` with `retryable=False` — the payload is
  deterministically unusable; retrying other models only burns quota.

`VisionClient(config, *, transport=None, limit=None)`:

- `candidates()`: `list(dict.fromkeys(m for m in (model, fallbackModel, extraModel) if m))`.
- `async recognize(url) -> str`: iterate candidates; per candidate hold the semaphore
  across the attempt loop, wrap in `asyncio.timeout(config.timeoutSeconds)`; retry
  transport errors and RETRY_STATUSES up to MAX_ATTEMPTS with backoff; parse
  `choices[0].message.content`; `finish_reason in {length, content_filter}` → retryable
  VisionError; strip trailing `<think>` segment; reject empty/non-str.
- `InvalidImageError` aborts the whole chain immediately. Any other failure falls
  through to the next candidate; exhaustion raises `VisionError("Model chain failed ...")`.
- HTTP via `httpx.AsyncClient(timeout=..., follow_redirects=False, trust_env=False,
  transport=transport)`. httpx is already a core dependency; nothing new to install.
- `limit`: externally supplied `asyncio.Semaphore` (daemon-shared, §6) or a private
  `Semaphore(config.concurrency)`.

## 5. image_material.py — ImageEnricher

Port of wiseflow image_material.py, same naming for traceability. Operates on copies;
never mutates the caller's capture (prepare_capture deep-copies).

`ImageEnricher(config, *, cache=None, client=None, limit=None, event=None)`:

- `enabled` comes from `config.enabled`.
- `client` injectable for tests; defaults to `VisionClient(config, ...)`.
- `event(kind, message, *, level, details)` callback → `State.event` (daemon wires run_id).

`async enrich(capture) -> capture`:

1. `result = prepare_capture(capture)`; return unchanged if `status.access != "ok"`.
2. `base = finalUrl or requestedUrl`.
3. For every segment passing `image_eligible(segment, dom)` (unchanged), schedule
   `_one(image, segment, base)` for each `segment.images[]`.
4. `await asyncio.wait_for(gather(...), VISION_PAGE_BUDGET_SECONDS)` with budget 300s.
   On timeout, cancel pending work, mark unfinished images `status="failed",
   source="vision-failed"`, and return the page — degrade, never lose the article.
5. Optionally emit one `vision_enriched` summary event (counts ok/skipped/failed/cached).

`_one(image, segment, base)` — production heuristics in order. Every field used exists
in osbi's readDom output (dom-material.js:215-219: path, alt, title, src, dataSrc,
offset, complete, width=naturalWidth, height=naturalHeight):

1. Initialize evidence `{url:"", text:"", source:"", status:"skipped"}`.
2. Empty `dataSrc`/`src` → skipped. Never urljoin an empty src.
3. `url = urljoin(base, dataSrc or src)`; record `image.url`.
4. Generic alt (`图片/图/image/img/photo`) cleared.
5. `is_link`: image src appears in any `segment.links[].images`.
6. `complete` but zero width/height → broken image → skipped.
7. Tracker substrings in the URL path (`1x1. pixel. spacer. blank. tracking beacon
   dot.gif clear.gif`) or `complete && min(width,height) < 28` → skipped, no VL call.
8. `len(alt) > (2 if is_link else 5)` → `text=alt, source="alt", status="ok"`, no VL call.
9. `not is_link and len(segment.text) > 150` → `text=alt, source="surrounding-text",
   status="covered"`, no VL call.
10. Scheme outside {http, https, data} → `status="pending", source="unavailable-image"`.
11. Per-URL task dedup within the page (`self._tasks` + `asyncio.shield`).
12. Result mapping: invalid → `text="", source="vision-invalid", status="skipped"` +
    event; chain failure → `source="vision-failed", status="failed"`; decorative →
    `source="vision-decoration", status="skipped"`; otherwise strip any embedded
    `§decorative-image§` token and set `text=value, source="vision", status="ok"`
    (`"pending"` if stripping leaves nothing).

`_recognize(url)`:

- Cache hit returns immediately; sentinel `§to_be_recognized_by_visual_llm§` and
  `**empty**` are treated as empty, never served as hits.
- Otherwise `client.recognize(url)` under the shared limit; on success cache the value
  (TTL 7 days) — including decorative markers and scene descriptions.
- `InvalidImageError` → emit `image_invalid` warning event with `{url, status, code,
  message[:500]}` and return the invalid marker. Other exceptions → log warning,
  return None (failed). Failures are not cached.

Invalid-image recording uses the bounded events table (osbi-native), already surfaced
in the notifications UI and queryable via API/MCP. No new file log.

## 6. Browser and Daemon Integration

browser.py — `capture(self, session, url, lease, seed_meta=None, *, enricher=None)`:

- After the capture dict is built and before `index_snapshot`:
  `if enricher is not None and enricher.enabled: capture = await enricher.enrich(capture)`.
- `index_snapshot(capture, seed_meta)` and the CAPTURE_EMPTY check stay as-is; the
  check now benefits from image-derived blocks.
- mediaPending (§9): with enrichment active, set only if some image in
  `localMaps.images` ended `pending`/`failed`; without enrichment, keep today's rule
  (any images → True).
- All browser commands finish before enrichment starts, so no lease is held during VL calls.

daemon.py:

- `__init__` gains `vision_transport=None` (mirrors `decision_transport`) for
  MockTransport injection in tests.
- A daemon-owned `asyncio.Semaphore` bounds total VL concurrency across parallel runs.
  Recreated when the configured concurrency value changes; passed as `limit` to every
  enricher.
- `capture_page` builds `ImageEnricher(config.vision, cache=self.cache, limit=shared,
  transport=self.vision_transport, event=...)` from the run's frozen config snapshot
  and passes `enricher=`. Mid-run config edits do not affect in-flight runs.
- `_submit_material` unchanged: `textOnly=True` stays correct; the `media_pending`
  warning event now fires only for genuinely unresolved images.
- mp/rss catalog discovery paths are untouched (links-only materials).

## 7. Decorative and Tracker Handling

Two filter stages:

- Pre-VL (no provider cost): empty src, broken 0×0, tracker URL patterns, min
  dimension <28px, sufficient alt text, surrounding paragraph >150 chars.
- Post-VL classification: exact `§decorative-image§`; parenthesized ≤300-char
  descriptions matching the ported decoration keyword regex (图标/卡通图案/简约插画/
  渐变色线条/手绘线条/抽象背景|剪影/城市天际线轮廓/花朵|小花|枫叶图案/标志，) — port
  `_decorative_text` verbatim. Genuine photo scene descriptions in （） without those
  keywords are kept as body text.
- Control tokens never become extraction facts: any embedded `§decorative-image§` is
  stripped even when mixed with real OCR text; sentinel/`**empty**` values are treated
  as empty.
- Every image keeps `source`/`status` evidence in `localMaps.images`, so skips and
  failures are auditable per material.

## 8. Caching Strategy

New bounded table in LocalCache (cache.py):

```
image_text_cache(id TEXT PRIMARY KEY, body TEXT NOT NULL,
                 expires REAL NOT NULL, updated REAL NOT NULL)
```

- `id = digest(["image-text/v1", absolute_url])`. The version prefix allows wholesale
  invalidation when the prompt changes (bump to v2).
- `image_text_get(url)` / `image_text_put(url, text, ttl)`; TTL constant
  `IMAGE_TEXT_TTL_SECONDS = 7*86400`.
- Pruning hooks into `LocalCache.prune`: delete expired, then cap at
  `IMAGE_TEXT_MAX_ENTRIES = 5000` oldest-updated-first.
- Cache all successful recognitions (text, scene descriptions, decorative markers).
  Never cache failures or invalid images.
- Keyed by URL only, matching production. Switching models does not invalidate cached
  text; the version prefix provides a manual escape hatch.
- Synergy with the page cache: `capture_put` refuses bodies with `mediaPending`. Fully
  resolved vision pages become page-cacheable, so re-captures within captureCache TTL
  cost zero VL calls; the image-text cache additionally dedups shared images across
  different pages and runs.

## 9. mediaPending Semantics

No contract change: `SourceMeta.textOnly` / `mediaPending` already exist, and no server
code keys on mediaPending today — it is provenance plus the local cache gate.

| Vision | Image outcome | mediaPending |
|---|---|---|
| disabled | any images present | True (today's behavior, unchanged) |
| enabled | all eligible images ok / covered / skipped | False (cleared) |
| enabled | any image pending or failed | True (honest partial-failure signal) |

`scope.imagesPending` keeps counting `status=="pending"`. `textOnly` remains True in
all cases: submission is still text-only transport.

## 10. Settings UI (static/app.js)

- New card "05 · 图片识别（VL）" inserted after Jev (04); reports renumbered to 06;
  sidebar index gains an entry.
- Master toggle `visionEnabled` in the titlebar (jev-switch pattern); dependents
  `[visionUrl, visionKey, visionModel, visionFallback, visionExtra, visionConcurrency,
  visionTimeout]` disabled while off.
- Fields: baseUrl (url), apiKey (password, autocomplete=new-password, placeholder
  `已配置 · 尾号 XXXX，留空保留` from `config.vision.keyConfigured/keySuffix`), model,
  fallbackModel, extraModel, concurrency (number 1-6), timeoutSeconds (number 10-600).
  Hints: the provider must publicly fetch the image URLs; the key stays on this
  machine; recognition adds latency and provider cost.
- `saveVision` uses the existing `saveSection` helper: PATCH `{vision:{enabled, baseUrl,
  model, fallbackModel, extraModel, concurrency:Number, timeoutSeconds:Number}}`, plus
  `apiKey` only when non-empty (blank preserves). Extend saveSection's placeholder
  refresh to visionKey.
- UI copy stays zh-CN like the rest of the dashboard; code comments English per AGENTS.md.

## 11. Applying the Production Configuration (this machine)

After implementation, apply via CLI `configure` or the dashboard — JSON on stdin:

```json
{"vision": {
  "enabled": true,
  "baseUrl": "https://api.siliconflow.cn/v1",
  "model": "Qwen/Qwen3-VL-32B-Instruct",
  "fallbackModel": "Qwen/Qwen3-VL-30B-A3B-Instruct",
  "extraModel": "zai-org/GLM-4.5V",
  "concurrency": 3,
  "timeoutSeconds": 240,
  "apiKey": "<SF_KEY_PLACEHOLDER>"
}}
```

The real key is injected separately by the operator through `configure` stdin; it is
never written into the repo, this document, or logs. `public_config` masks it to
`keyConfigured`/`keySuffix` on every read path (HTTP, MCP, CLI). The main chat LLM
remains on its separate local endpoint, exactly as production separates
`LLM_API_BASE` from the vision credentials.

## 12. TDD Plan

pytest with pythonpath already configured. Follow existing patterns:
`httpx.MockTransport` injection and fake Browser. Write each test first (RED), implement
to GREEN, refactor; target ≥80% coverage on new modules.

New test files:

- tests/test_vision.py — VL client unit tests (MockTransport): request shape
  (image_url detail=high + PROMPT, Bearer header, temperature/max_tokens); chain
  fallback primary→fallback→extra on retryable failures; dedup of empty/duplicate
  models; invalid-image abort (400/413/415/422, codes 20015/20040, detail regex) with
  no further model calls; 429 backoff then success (monkeypatched sleep);
  finish_reason=length and empty content are retryable; think-tag stripping;
  per-model timeout; chain-exhausted error message; semaphore bounds in-flight requests.
- tests/test_image_material.py — enricher unit tests (fake vision client): eligible
  image text lands in blocks at the right offset (via index_snapshot);
  tracker/tiny/broken/empty-src skipped without provider calls; alt and
  surrounding-text shortcuts; decorative marker and keyword descriptions dropped,
  genuine scene descriptions kept, mixed markers stripped; invalid → skipped +
  image_invalid event; failure → failed and page proceeds; per-URL dedup (one call
  for repeated images); cache hit/put semantics, failures not cached; page-budget
  timeout degrades remaining images; input capture not mutated.
- tests/test_vision_config.py — config and privacy: HTTPS/loopback validation,
  enabled-remote-requires-key, bounds, extra=forbid; save_config deep-merge preserves
  apiKey on partial PATCH; public_config masks vision key; legacy stored config
  without vision validates with defaults.
- tests/test_browser_vision.py — integration (fake browser commands + MockTransport):
  vision on: blocks contain image text, mediaPending cleared, localMaps.images carries
  source/status; vision off: byte-identical legacy output (mediaPending True with
  images); pure-image page capturable only with vision on; partial failure keeps
  mediaPending True and still submits; daemon capture_page wires enricher from the run
  config snapshot; capture cache accepts resolved pages and still rejects mediaPending
  pages.
- Frontend: extend dashboard-settings.test.mjs (toggle greys dependents; section save
  preserves key; placeholder suffix refresh; renumbered sections).

Regression: the full existing suite must stay green with vision disabled — the
text-only path is untouched by construction (enricher=None default).

## 13. Risks and Boundaries

- VL cost and quota. Most images are filtered pre-VL (§7); caching removes repeat
  spend; concurrency caps and the page budget bound worst-case usage.
- Rate limiting. 429/5xx backoff (20s/40s) plus the model chain; the daemon-shared
  semaphore prevents parallel runs from multiplying provider load.
- Image URL reachability. The provider fetches the URL server-side: intranet,
  localhost, or auth-gated images fail and degrade to skipped/failed; `data:` URLs
  pass through inline. No local SSRF surface in URL mode.
- WeChat mmbiz.qpic.cn hotlink protection. Provider-side fetches may be rejected
  (403 → invalid-image skip). Phase-2 mitigation if observed at scale: client-side
  download with `Referer: https://mp.weixin.qq.com/` and submission as a base64 data
  URL, with a size cap and private/loopback host blocking to avoid SSRF. Out of scope
  for phase 1; `image_invalid` events provide the evidence to decide.
- Recognition quality. The verbatim prompt forbids invented slogans/captions; scene
  descriptions are short and non-inferential. Image-derived text carries the same
  trust level as DOM text: untrusted evidence downstream, never instructions.
- Latency. Enrichment adds seconds to minutes per page. Browser commands complete
  before VL work, so lease expiry during enrichment is harmless. The 300s page budget
  aligns with the 300s lease TTL and bounds run-slot occupation.
- Text-only compatibility. Default off; `enricher=None` preserves today's code path
  exactly; config additions are defaulted and additive under StrictModel.
- Secrets. apiKey lives only in the local config DB, masked on every read path, never
  logged or committed; this document uses a placeholder.
- Provider lock-in. The OpenAI-compatible shape plus HTTPS-or-loopback validation
  allows any compatible endpoint, including a local VL server over loopback HTTP.

## 14. Implementation Order

Each phase is independently mergeable to dev; the feature stays dark until enabled.

1. Config foundation — VisionConfig, ClientConfig.vision, save_config merge list,
   public_config masking; tests/test_vision_config.py. Inert on its own.
2. VL client — vision.py + tests/test_vision.py (MockTransport only, no integration).
3. Enricher + cache — image_material.py, LocalCache image_text table/prune;
   tests/test_image_material.py.
4. Integration — browser.capture enricher parameter + mediaPending logic, daemon
   wiring (vision_transport, shared semaphore); tests/test_browser_vision.py; full
   regression.
5. UI — app.js vision section, saveSection placeholder extension; dashboard tests.
6. Production application — configure this machine per §11 (key injected separately),
   run mp/web sources, observe vision_enriched / image_invalid / media_pending events,
   then decide whether to adjust defaults or pursue the data-URL phase.

## 15. Decision Points (adopted recommendations)

- D1 mediaPending strictness: keep True when any image is pending/failed (adopted).
- D2 page vision budget: 300s with degrade-and-proceed (adopted).
- D3 invalid-image record: events table, osbi-native (adopted).
- D4 concurrency scope: daemon-shared semaphore (adopted).
- D5 cache key: URL-only with version prefix (adopted, production parity).
- D6 shipped defaults: production model names as defaults (adopted).
- D7 UI placement: insert as section 05 and renumber reports to 06 (adopted).
