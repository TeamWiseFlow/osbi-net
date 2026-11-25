# Local Downstream Tasks

The client generates TopNews and Weeklyreport from shared-server evidence. The
server owns channel relevance, original-block retention, ontology and retrieval;
local report models, editorial rules, schedules, files and delivery run on the
client. UI walkthroughs are in the [English](../client/README.md#configure-reports)
and [Chinese](../client/README.zh-CN.md#配置报告任务) client manuals.

## Local Report Profiles

Each `reportProfiles` entry has an ID, name, shared `sections`, delivery `targets`,
and separate `topnews`/`weeklyreport` tasks. A section supplies a title, one or
more server channels, query/scope/locality preferences and local rules. A task
board references its section and overrides selector, item count, minimum edges,
purpose, prompt or source scope. There is no caller-owned server focus or
client-created channel/schema.

Example profile fragment:

```json
{
  "id": "research",
  "name": "Research",
  "sections": [{
    "id": "updates",
    "title": "Research updates",
    "channels": ["economy"],
    "query": {"text": "research", "sourceScope": "all"},
    "localRules": {"jev": "inherit"}
  }],
  "targets": [],
  "topnews": {
    "enabled": true,
    "schedule": {"time": "08:40", "timezone": "Asia/Shanghai"},
    "boards": [{"section": "updates", "selector": "candidates", "topN": 3}],
    "dryRun": true
  },
  "weeklyreport": {
    "enabled": true,
    "schedule": {"weekday": "monday", "time": "07:50", "timezone": "Asia/Shanghai"},
    "boards": [{"section": "updates", "selector": "policy", "topN": 2}],
    "docxFilename": "Research weekly",
    "sendFile": true,
    "dryRun": true
  }
}
```

Configure the profile under Tasks or through `configure`. Replacing
`reportProfiles` replaces that entire array, so retain other profiles and stable
IDs. Target IDs are selected independently for each task. The example client
YAML is a reference, not an automatically loaded runtime file.

## Selection and Writing

TopNews uses per-section candidate pools, title/event deduplication, locality/
quality checks and publisher penalties, followed by local editing. Its **加油站**
label is **Do you Know** in English. Weeklyreport combines action-network breadth,
policy–action chains and economic fallback, with local cross-week coverage.
Models write sections, analysis and digest. Source evidence and configured report
copy are not translated by changing dashboard language.

The client report model is configured in Query settings (`reportLlm`); the default
is local `xin_local` at `http://127.0.0.1:9997/v1`. Deploy a model service separately
or use a remote HTTPS endpoint/key. Model failures fall back to deterministic
selection/formatting; insufficient evidence can produce a material overview.
Task/board `prompt` values override the corresponding built-in prompts. Local
`nodeRules.topnewsShout`, `topnewsSuffix` and `weeklyIntro` configure output copy;
Weekly introduction supports `{date}` and `{digest}`.

## Manual and Scheduled Runs

Use Outputs for explicit date windows and recent run logs. Task-level manual
buttons use the default windows: yesterday→today for TopNews, and seven days
before today→today for Weeklyreport. Enabled report schedules run in the local
backend's configured task timezone. Source capture schedules are independent:
collect evidence in advance and account for the server's ontology batch timing.

Dry-run generates files without sending. Turn it off only for intended delivery
to enabled targets: wechat-linux-container sends text, Awada sends text/files, and WeCom webhook
is reserved. Saving settings does not send a test message. Reports and ledgers
remain under `<client-state>/reports/`.

## DOCX Names and Delivery

Set `reportProfiles[].weeklyreport.docxFilename` through Tasks. It is a base name;
generation always appends `_{YYYYMMDD}.docx`, using the generation date. Unicode
and spaces are supported; path/control/reserved characters are rejected. Pasted
`.docx` is normalized away before suffixing. Old configurations retain their
previous default base. Same base/date replaces that file; use distinct bases
for profiles whose files must coexist.

This name is used for the saved DOCX, recent-file list, download and file delivery.
Markdown and digest TXT keep their previous filename rules. `sendFile` controls
file delivery, not DOCX creation. DOCX failure leaves Markdown/TXT and a diagnostic.
See [filename configuration](../client/README.md#weekly-docx-filename).

## Evidence, Scope and Optional Jev

`all` retrieves a channel's authorized shared contributions; `own` uses the current
node's sources. Task-board scope overrides the section, which overrides the global
query default. Local filters and optional Jev apply after retrieval and never
remove server facts. Jev is disabled by default; low confidence or provider failure
keeps the evidence. It has its own local endpoint/key/model, separate from server
extraction credentials. There is no pre-submission Jev stage in the current UI.

For local agent preparation, CLI/MCP evidence and selection tools remain available:

```bash
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
printf '%s' '{"profile":"research","board":"updates","fromDate":"2026-11-01","toDate":"2026-11-07"}' \
  | .venv/bin/osbi-client topnews-select economy
```

Original evidence retains source metadata. Continue generic evidence pagination
until its cursor is empty, even if one page has no items. Keep coverage histories
local; use the existing weekly-history/backfill tools only with the intended
profile, channel and report dates. Backfill previews by default, and `--apply`
records history. Read the [content-storage notice](../README.md#content-storage-intellectual-property-and-responsibility)
when choosing model endpoints and handling original evidence.
