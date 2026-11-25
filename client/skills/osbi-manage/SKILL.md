---
name: osbi-manage
description: Configure OSBI connections, acquisition sources, browser workflows, local models and scheduled reports from user requests through MCP or CLI; monitor runs and resolve waiting tasks.
---

# OSBI Management

Translate the user's request into local configuration through MCP `settings` and
`configure(update)`. The user need not open the dashboard or edit a file. The
persistent backend owns acquisition, schedules and reports independently of the
agent session. Use `osbi-query` for evidence retrieval and agent-written reports.

## Connect and update settings

MCP tools use `OSBI_CLIENT_DATA` (default `~/.osbi/client`) and `OSBI_CLIENT_URL`
(default `http://127.0.0.1:8781`). These identify the local backend, while
`serverUrl` identifies the shared server. Match both local variables to the
running instance; a different data directory has a different local token.
MCP does not start a daemon. If it is unavailable, report the connection problem;
start or install it when that is part of the user's request. Installation from
checkout root uses `bash client/scripts/install.sh --agent dsh`; startup uses
`bash client/scripts/run.sh`. Other agent hosts use the same MCP/CLI backend.

CLI equivalents (global options precede the command):

```bash
osbi-client config
osbi-client channels
osbi-client configure < requested-update.json
osbi-client --data ~/.osbi/client-demo --url http://127.0.0.1:8782 config
```

CLI `configure` reads a JSON object from stdin; pass secret input through stdin
or a protected local file, without embedding it in command arguments or reports.
`client/config/example/client.yaml` is an example, not an automatically loaded
runtime file. Saved settings reside in `<client-state>/client.db`; use the API
instead of editing SQLite during normal configuration.

Read `settings` first, resolve the requested source/profile/section by stable ID,
and send only writable fields needed for the change. Configuration is strict:
use JSON booleans/numbers/arrays, exact field names and declared enum values.
Read back saved settings and describe the result. Saving does not start a manual
run or send a test message, but enabled schedules can run when due. Do not add
capture, model calls, connection probes or delivery tests unless they are part
of the request. A requested real run can itself query models or send messages
according to its configuration.

**Patch semantics matter:**

- `sources` and `reportProfiles` replace their entire arrays. Read, edit the
  selected item, and send the complete preserved array; omission removes items.
  Nested `sections`, `boards`, `targets`, `accounts`, `channels` and `targetIds`
  are also lists, not updates keyed by ID. Deleting a section/target requires
  updating its task references in the same patch.
- `jev`, `vision`, `reportLlm`, `captureCache`, `submissionCache` and
  `domainConcurrency` merge one object level. Their nested maps, such as `domains`
  and `domainTtlSeconds`, replace the whole map if supplied.
- `nodeRules` and `queryDefaults` replace their whole objects; merge current
  values yourself to preserve customizations. Omitted members reset to defaults.
- Never echo a whole `settings` response into `configure`. Remove read-only
  `revision`, `sourceNextRuns`, root/model `keyConfigured` and `keySuffix`.
  `sourceNextRuns` contains Unix timestamps of saved source schedules, not inputs.
- Omit an unchanged `serverKey` or model `apiKey`; an explicit empty string
  clears it. Target `token`/`webhookUrl` behave differently: omitted or blank
  values preserve the stored secret for the same profile/target IDs. Target
  readback markers are not credentials. Keep stable IDs to retain those secrets
  and private report history. To remove a target secret, remove/recreate the
  target as intended rather than treating blank as a clear operation.

## Shared connection, acquisition and caches

Defaults below are model defaults; keep saved values unless changing them.

| Field | Meaning and allowed values |
| --- | --- |
| `serverUrl` | Shared OSBI API base, without credentials/query/fragment. Remote requires HTTPS; HTTP is allowed on loopback. Required to start acquisition, including sources with no contribution channels. |
| `serverKey` | Write-only node Bearer key supplied by the server operator. It determines identity and authorized network/channels; it is not an LLM key. |
| `serverCA` | Local path to a PEM CA bundle for the shared server. Empty uses system trust. |
| `maxUrls` | Maximum visited URLs per source run, including followed links; 1–200, default 20. Not a global daily quota. |
| `browserConcurrency` | Acquisition scheduler concurrency, 1–6, default 2. Per-domain limits may reduce actual concurrency. |
| `domainConcurrency.defaultConcurrency` | Concurrent work per normalized base domain, 1–6, default 1. |
| `domainConcurrency.domains` | Domain-to-limit overrides, e.g. `{"gov.cn":3,"mp.weixin.qq.com":2}`. Keys normalize to base domains; the WeChat article domain is separate from `qq.com`. Preserve other overrides. |
| `timeoutSeconds` | Shared-server/RSS HTTP request timeout, 10–300 seconds, default 60. Browser and model operations have their own timeout rules. |
| `headless` | Browser visibility, default false. False opens a visible browser; true runs without a window. Human verification can still pause a run. |
| `forbiddenDomains` | Unique nonempty URL substrings blocking discovered/followed URLs, including domain/path fragments. Explicit seed URLs are exempt; this is not an access-control list. |
| `captureCache.defaultTtlSeconds` | Local page reuse lifetime, 0–31536000 seconds, default 21600 (6 h); zero prevents reuse. |
| `captureCache.domainTtlSeconds` | Hostname-to-TTL overrides with the same bounds; exact hosts/subdomains match and the longest match wins. No ports, paths or wildcard prefixes. |
| `captureCache.maxEntries` / `maxBytes` | Capture-cache limits: 1–100000 entries (default 1000), 1–2147483648 bytes (default 134217728). |
| `submissionCache.ttlSeconds` | Reuse lifetime for prior server submission outcomes; 0–31536000 seconds, default 604800 (7 d). Separate from page reuse. |
| `submissionCache.maxEntries` | Submission-cache entry limit, 1–100000, default 10000. |
| `submissionCache.historyDays` | Local submission history retention, 1–3650 days, default 30. Does not set shared-server retention. |
| `queryDefaults.sourceScope` | Default report evidence scope: `all` (default) reads authorized shared contributions; `own` restricts to the authenticated node's contributions. |

## Sources and workflows

Each `sources[]` item describes one acquisition job template, not a report.

| Field | Meaning and allowed values |
| --- | --- |
| `id` | Unique stable ID, starts with a letter, then letters/digits/`_`/`-`, at most 64 characters. Used by `start_source` and schedules. |
| `url` | HTTP(S) starting page/feed; for `mp`, use a sentinel such as `https://mp.weixin.qq.com/`. |
| `kind` | `web` (default): browser page/workflow; `rss`: feed discovery followed by article capture; `mp`: local WeChat catalog discovery followed by browser article capture. |
| `enabled` | Default true. Controls automatic scheduling; the manual start endpoint can still run a disabled source. |
| `intervalMinutes` | Repeating interval, 0–525600, default 0. Used only when daily times are empty; zero with empty times means manual only. |
| `schedule.times` | Up to 48 unique 24-hour `HH:MM` daily capture slots, e.g. `["08:00","12:30","18:00"]`. Nonempty times take priority over the interval. Empty `[]` disables daily slots. |
| `schedule.timezone` | IANA zone for daily capture times, default `Asia/Shanghai`. Independent of report schedules. |
| `profile` | Persistent browser profile/session ID, default `default`; controls login/cookie continuity. Unrelated to a report profile ID. |
| `channels` | Up to 8 unique global server channel IDs. Multiple channels create separate submissions; `[]` means acquisition only. Discover channels before choosing IDs. |
| `accounts` | `mp` only: required, nonempty whitelist of exact account display names, unique after trimming, at most 100 names. Invalid for other kinds. |
| `steps` | Up to 40 typed browser steps for a web workflow. `mp` forbids them; RSS is a feed workflow rather than a programmable web workflow. |

For an existing source requested to run at three daily times, edit its `schedule`
in the current `sources` array and preserve all other sources. Return the new
`sourceNextRuns` value after saving. Missed daily slots coalesce into one catch-up
run; an active run prevents another run for that source. Disabling/removing a
source stops future scheduling but does not cancel an existing run.

Workflow steps start from the source page. Each has `action` and the relevant
parameters; these are stored step fields, not arbitrary browser API parameters:

| Action | Parameters and behavior |
| --- | --- |
| `navigate` | `url`: HTTP(S) destination. |
| `click` | `selector` (CSS) or current snapshot `ref`. |
| `fill` / `select` | `selector` or `ref`, plus `text` for input or selection value. |
| `wait` | `selector` to await an element, or `ms` (0–30000, default 1000). |
| `scroll` | `amount`, -3000–3000 pixels, default 500. |
| `switch` | `index`, tab index 0–20, default 0. |
| `capture` | Capture the current result page and end the navigation sequence. Do not reopen its URL and lose interactive state. |
| `wait_for_user` | Pause; `text` explains the interaction required. |

`frame` is an optional frame selector forwarded with interaction steps. Prefer
stable CSS selectors for saved workflows; snapshot refs are session-specific.

An `mp` source needs a separately deployed, logged-in `wechat-linux-container`
and access to its database/key files. In the backend environment,
`WECHAT_LINUX_CONTAINER_CONTAINER` selects the Docker name (default
`wechat-linux-container`), `WECHAT_LINUX_CONTAINER_WX_USER_DIR` the user directory
inside it, and `WECHAT_LINUX_CONTAINER_KEYS_FILE` the key JSON path inside it.
These are environment variables, not `configure` fields. `OSBI_MP_WINDOW_HOURS`
can set a positive integer catalog lookback; otherwise it is at least 24 h
(interval-based lookback includes a 2 h overlap). Catalog reuse is fixed at
15 minutes, separate from article capture TTLs. A missing whitelist account is a
warning; discovery emits only whitelisted article catalogs, not arbitrary chats.

## Local model settings

These credentials are independent of `serverKey` and each other. Do not reuse a
server key as a model key. Endpoints permit HTTPS remotely and HTTP on loopback,
without embedded credentials/query/fragment. Enabling a remote model requires
its own `apiKey`. Explain what content goes to an endpoint when configuring it.

| Object / field | Meaning and default |
| --- | --- |
| `jev.enabled` | Optional local post-query evidence decision service, default false. It does not gate submission or change the shared graph. |
| `jev.baseUrl` / `apiKey` / `model` | Decision endpoint, write-only provider key, model ID. Defaults: `https://api.typesafe.ai/v1`, empty key, `jev-latest`. |
| `jev.postQuery` | Enables decisions after retrieval when the service is enabled, default true. |
| `jev.discardConfidence` | Minimum confidence for a discard decision, 0–1, default 0.8; low confidence or unavailable decisions keep material. |
| `jev.timeoutSeconds` | Decision request timeout, 1–120 seconds, default 20. |
| `vision.enabled` | Body-image text recognition, default false. Sends image URLs to the configured vision model service. |
| `vision.baseUrl` / `apiKey` | OpenAI-compatible vision endpoint and write-only key; defaults `https://api.siliconflow.cn/v1`, empty key. |
| `vision.model` | Primary model, default `Qwen/Qwen3-VL-32B-Instruct`. |
| `vision.fallbackModel` / `extraModel` | Second/third models after failures; defaults `Qwen/Qwen3-VL-30B-A3B-Instruct`, `zai-org/GLM-4.5V`. Empty disables that fallback. Invalid images stop the chain. |
| `vision.concurrency` / `timeoutSeconds` | Concurrent recognitions 1–6 (default 3); per-model timeout including retries, 10–600 seconds (default 240). |
| `reportLlm.enabled` | Report editor/writer model, default true. Independent of the agent host's model. Disabled/unavailable models use deterministic fallbacks. |
| `reportLlm.baseUrl` / `apiKey` / `model` | OpenAI-compatible report endpoint, write-only key, model ID. Defaults `http://127.0.0.1:9997/v1`, empty key, `xin_local`. Deploy the model service separately. |
| `reportLlm.timeoutSeconds` | Report request timeout, 10–600 seconds, default 240. Evidence and report prompts go to this endpoint. |

## Report profiles, sections and tasks

`reportProfiles[]` stores client-local interests. Each profile has a stable `id`,
display `name`, `sections`, `targets`, `topnews` and `weeklyreport`. Profile,
section and target IDs use letters/digits/`_`/`-`, start with an alphanumeric and
are at most 64 characters. Use distinct profile IDs for departments; preserve
other profiles. Limits: 30 profiles; 20 sections and 10 targets per profile;
20 boards/target references per task.

Sections are shared templates; tasks overlay them by section ID. Changing a
section affects both tasks that use it. Change the overlay when only one task's
item count, purpose, prompt or scope should differ.

| `sections[]` field | Meaning |
| --- | --- |
| `id` / `title` | Stable reference ID and output heading. IDs and titles must each be unique within the profile. |
| `channels` | 1–8 unique existing server channels merged for backend-generated reports. Sections may combine/share channels. |
| `query.text` | Literal case-insensitive substring in evidence title/body, at most 1000 characters; not a semantic relevance prompt. Empty adds no text filter. |
| `query.entityUris` | Up to 500 graph entity URIs; keeps evidence involving any listed entity. Use discovered URIs, not `article:` item references. |
| `query.contentTypes` | Up to 20 content-type labels returned by evidence, matching any listed label. Empty accepts all; these are not ontology classes. |
| `query.territoryPreference` | Up to 20 territory names matched against evidence entities; default empty. |
| `query.territoryMode` | `prefer` (default) ranks territorial matches higher; `only` requires a match when preferences are nonempty. |
| `query.sourceScope` | `inherit` (default), `all` or `own`. Inherit uses the global query default. |
| `localRules.keywords` | Local title/body inclusion substrings; any match passes, empty allows all. Up to 50. Applied by backend report generation after retrieval. |
| `localRules.excludeKeywords` | Local title/body exclusion substrings; any match discards. Up to 50. |
| `localRules.leaderBoost` / `localityPenalty` | Enable leader priority / nonlocal-leader penalty in local report ranking, each default true. Do not change shared graph admission. |
| `localRules.jev` | `inherit` (default), `on` or `off` for optional local filtering. `off` bypasses it; `on` still requires global `jev.enabled` and `postQuery`. |

| Task field (`topnews` / `weeklyreport`) | Meaning |
| --- | --- |
| `enabled` | Automatic generation switch, default true. Enabled tasks require at least one board. Manual report endpoints can still run a disabled task. |
| `schedule.time` | One `HH:MM` generation time; not source capture times. Default profiles use TopNews `08:40`, Weeklyreport `07:50`. |
| `schedule.weekday` | Lowercase `monday`…`sunday`, or null for daily. Default Weeklyreport is Monday; TopNews is daily. |
| `schedule.timezone` | IANA zone, default `Asia/Shanghai`. Collect sources early enough for server batch processing before generation. |
| `boards` | Ordered section overlays; each section appears at most once per task. Order defines report section order. |
| `boards[].section` | Reference to a configured section ID. |
| `boards[].selector` | `candidates`: TopNews editorial pool; `action`: weekly action networks; `policy`: policy/action chains; `evidence`: generic originals. Default `candidates`. Specialized selectors depend on available schema data. |
| `boards[].topN` | Requested maximum items, 1–20, default 3; insufficient evidence may yield fewer. |
| `boards[].minEdges` | Minimum supporting relations for action-network candidates, 1–100, default 4; not an evidence-page limit. |
| `boards[].purpose` | Local writing intent/optional Jev intent, at most 4000 characters. Does not become shared-server relevance or automatic topical filtering. |
| `boards[].prompt` | Board-level writer/editor prompt override, at most 8000 characters. |
| `boards[].sourceScope` | `inherit` (default), `all` or `own`. Resolution: explicit board scope → explicit section scope → `queryDefaults.sourceScope`. |
| `prompt` | Task-level prompt override, at most 12000 characters. TopNews uses it for the Do you Know fact editor; Weeklyreport uses it for analysis. Empty keeps the built-in prompt. |
| `targetIds` | IDs of profile-level targets used by this task. Empty means no delivery destinations. |
| `dryRun` | Default true: generate/save without sending. False permits delivery to the task's enabled targets when a report runs. |
| `sendFile` | Weeklyreport-only behavior, default true: also deliver DOCX when sending is enabled. Does not control DOCX creation; TopNews ignores it. |
| `docxFilename` | Weeklyreport DOCX base, 1–120 characters and at most 240 UTF-8 bytes. Generation appends `_YYYYMMDD.docx` using the generation date; pasted `.docx` is stripped. No paths/control/reserved characters. TopNews ignores it. |

Same DOCX base/date overwrite the same file; use distinct bases for profiles
whose files must coexist. Markdown and introduction TXT keep their existing
naming rules. A DOCX failure can leave usable Markdown/TXT. Outputs, publisher
penalties and report ledgers live in `<client-state>/reports/`; selection/coverage
history also uses the local database. Dry-run still updates local artifacts and
ledgers; it is a delivery switch, not a side-effect-free preview.

For “only our contributions, three items at 09:00, weekly Word name Research”,
edit the intended profile's board `sourceScope`/`topN`, task `schedule.time`, and
`weeklyreport.docxFilename` in the preserved full array. Use the global scope
only when all inheriting profiles should change. Set `query.text`/`keywords`
for literal filters; put semantic requirements in `purpose`/`prompt` and assess
evidence. See `osbi-query` for limitations of lightweight selection tools.

## Node wording and locality rules

`nodeRules` affects local wording/ranking. Defaults reproduce the Shanghai/Jing'an/
Linfen Road preset; configure the actual jurisdiction for a new node, including
section territories. These do not change node identity, channels or schemas.

| Field | Meaning |
| --- | --- |
| `nodeName` | Full node/jurisdiction display name used in writing. |
| `city` / `district` / `street` | Report jurisdiction names; city/district also configure local leader recognition. |
| `leaderBoost` / `localityPenalty` | Node-wide leader score boost / non-target leader penalty switches, both default true. Section switches can suppress their use. |
| `genericEntityNames` | Replace generic-name set excluded from meaningful entity scoring; empty retains built-in rules. |
| `localityScore` | Replace locality score map; empty retains built-in rules. Tier keys: `target`, `shanghai` (city tier), `other_district`, `other_province`, `national`, `unknown`, with integer scores. Supply the whole intended map. |
| `relationHints` | Territory clues for local policy/action relation scoring; empty retains built-in clues. |
| `leaderOverrides` | Advanced recognized lists below; unknown keys are not applied. Empty retains built-in rules. |
| `topnewsShout` / `topnewsSuffix` | Opening shout / trailing help text for TopNews delivery; empty uses default copy. |
| `weeklyIntro` | Weekly delivery introduction. Supports `{date}` and `{digest}`; empty uses default copy. |

Recognized `leaderOverrides` keys contain lists of strings:

- `targetTerritory`: local district/street/place clues;
  `cityLeaderTerms`: city-level leadership titles.
- `otherDistricts` / `otherDistrictShort` / `otherProvinces`: full/short district
  names and province clues distinguishing nonlocal activity.
- `knownNationalLeaders` / `nationalMarkers` / `nationalTitles`: national-level
  names, organization cues and titles used by the local classifier.
- `leaderTitleTerms` / `leaderVerbs`: titles and activity verbs for leader
  sentence recognition; `concurrentCityTitles` / `districtPostTerms`: concurrent
  city titles and substantive district posts for attribution.
- `relevantBoards`: headings favored by policy-chain scoring;
  `noiseImplementers`: generic actors excluded from implementation scoring;
  `genericPolicyTerms`: generic document/policy terms for chain selection.

These lists replace their corresponding rules rather than extending them.
Leader-recognition list overrides apply only when nonempty; retain other lists
and keys when making a targeted change.

## Delivery targets

Each `targets[]` entry has stable `id`, `type`, display `label`, `enabled`
(default true), and provider-specific `config`. It must also appear in a task's
`targetIds` to be used. A target does not provision or log in to the provider.

| Type | `config` fields and behavior |
| --- | --- |
| `wechat-linux-container` | `baseUrl`: HTTP API base, default `http://127.0.0.1:8899`; `window`: exact destination chat/group name; `token`: write-only API Bearer token; `configPath`: optional private provider JSON. Text only; DOCX returns `file_not_supported`. |
| `awada` | `configPath`: private Awada relay/publisher JSON; `channelId`: text destination; `fileChannelId`: file destination, falls back to `channelId`. IDs are strings. Supports text/files. Set an explicit destination rather than relying on development defaults. |
| `wecom-webhook` | Reserved settings include write-only `webhookUrl`, `mentioned`, `msgType`; sending returns `not_implemented`. Do not present it as working delivery. |

For `wechat-linux-container`, nonempty target values override environment/file
defaults. Otherwise resolution is `WECHAT_LINUX_CONTAINER_BASE_URL`,
`WECHAT_LINUX_CONTAINER_TOKEN`, `WECHAT_LINUX_CONTAINER_WINDOW` → provider JSON
section `wechat-linux-container` with `base_url`, `token`, `window` → built-in
loopback/empty defaults. Missing token/window skips sending. `base_url` and
`config_path` are accepted target aliases; Awada also accepts `channel_id` and
`file_channel_id`. Prefer camelCase. Readback masks secrets using
`tokenConfigured`/`tokenSuffix` or `webhookConfigured`.

## Monitor and interact

Call `start_source(source_id, force_refresh=false)` and follow its durable ID
with `runs`; CLI equivalents are `run SOURCE_ID [--refresh]`, `runs`, `events`,
`resume RUN_ID --prompt-id PROMPT_ID`, and `cancel RUN_ID`. `force_refresh`
bypasses local acquisition caches; it does not promise server re-extraction.
States include `pending`, `running`, `waiting_for_user`, `waiting_server`,
`interrupted`, `succeeded`, `failed`, and `cancelled`. Acquisition completion
does not mean ontology batch processing has finished.

For a waiting/interrupted run, inspect its prompt and operate the same session
using `browser_action(run_id, action, params)`. Supported actions: `snapshot`,
`click`, `fill`, `select`, `scroll`, `wait`, `switch`, `tabs`, `open`, `eval`, `url`.
CLI `browser RUN_ID` reads `{"action":...,"params":...}` from stdin. Refresh the
snapshot after navigation before using refs. Resume with the current prompt ID
after interaction is complete. Inspect uncertain results before retrying. Cancel
only the selected run; preserve unrelated sessions. GeoIP warnings permit
acquisition to continue and appear in notifications.

## Generate reports through the local API

MCP/CLI `topnews_select` and `weekly_select` prepare evidence for an agent; they
do not generate backend files. Full generation uses authenticated local
`POST /api/topnews/run` or `POST /api/weekly/run`:

```python
import asyncio
from osbi_client.cli import call_local

job = asyncio.run(call_local(
    "~/.osbi/client", "http://127.0.0.1:8781", "POST", "/api/weekly/run",
    {"profile": "default", "fromDate": "2026-11-16",
     "toDate": "2026-11-23", "send": False},
))
```

Use actual state/URL/profile/dates. `call_local` handles the token/write header.
`send:false` explicitly generates without delivery; omitted `send` uses task
`dryRun`, and `send:true` overrides it for real delivery. Omitted dates use
yesterday→today for TopNews or seven days before today→today for Weeklyreport.
`useLlm` defaults true; Weeklyreport `sendFile` overrides file delivery for that
run. TopNews also accepts `topN`, `recordPenalty`, `didYouKnow`. Do not invent
CLI/MCP generation tools.

Poll `GET /api/reports/jobs/{jobId}` to `succeeded`/`failed`; inspect `result`,
`docxError`, text and file delivery results before claiming success.
`GET /api/reports`, `/api/topnews/latest`, `/api/weekly/latest` and
`/api/reports/{kind}/logs` inspect outputs (`kind`: `topnews` or `weekly`).
Settings changes and generation are distinct operations. Channel creation,
relevance, ontology and extraction models remain server-owned.
