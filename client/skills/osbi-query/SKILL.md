---
name: osbi-query
description: Query OSBI shared-channel entities, relationships and original evidence; prepare local TopNews or Weeklyreport drafts with the user's configured report profiles and coverage history.
---

# OSBI Evidence Queries

Use OSBI MCP tools against the existing local backend. CLI fallbacks use the same
backend; `--data`/`--url` precede the command. Defaults: `OSBI_CLIENT_DATA` is
`~/.osbi/client`, `OSBI_CLIENT_URL` is `http://127.0.0.1:8781`. Read `settings` for
report interests. Configuration, acquisition and full backend report generation
belong to `osbi-manage`; queries do not start capture or change server schemas.

## Entities and paths

Discover accessible server channel IDs with `channels()` (`osbi-client channels`).
Channels are server-owned, with fixed ontology libraries such as policy/economy;
do not invent a channel from a report heading. The proxy resolves a channel's
schema for graph queries; original evidence remains channel-scoped. A graph
neighborhood alone does not establish channel-specific source scope.

Call `query(channel, source, target=null, depth=1)` with an entity name or URI:

```bash
osbi-client query economy ENTITY_NAME
osbi-client query economy SOURCE_URI --target TARGET_URI --depth 4
```

Without `target`, the current server returns immediate neighbors; increasing
`depth` does not expand that neighborhood. With `target`, `depth` bounds path
hops (1–4); paths are bounded shortest paths, not exhaustive reachability.
Ambiguous names can return HTTP 409 and no match HTTP 404. The local error may
hide candidate details. Use authenticated local
`GET /api/server/channels/CHANNEL/entities?q=NAME&limit=50&offset=0` via
`osbi_client.cli.call_local`, inspect candidates, and query the chosen URI.
There is no separate MCP entity-search tool.

Preserve relation direction, type and scope. Distinguish asserted relations,
co-occurrence and unresolved identities. Inspect `truncated` where returned;
missing results in a bounded search do not establish global disconnection.
Graph responses can lack original blocks; use `evidence(entity_uris=[...])` for
source text before making factual claims. Cite original URLs/titles/publication
metadata beside supported claims, not internal entity IDs as citations.

## Original evidence and pagination

`evidence` supports these MCP parameters; CLI JSON uses API camelCase names:

| MCP parameter | CLI/API field | Meaning |
| --- | --- | --- |
| `channel` | Channel in route/CLI argument | Existing global server channel ID. |
| `from_date` / `to_date` | `fromDate` / `toDate` | Inclusive publication dates, `YYYY-MM-DD`; empty leaves that bound open. Date filters exclude undated evidence. |
| `text` | `text` | Literal case-insensitive title/body substring, not semantic search. |
| `entity_uris` | `entityUris` | Evidence involving any listed graph URI; empty adds no filter. |
| `content_types` | `contentTypes` | Match any supplied content-type label from evidence; not schema classes. |
| `source_scope` | `sourceScope` | `all` (default): authorized shared contributions; `own`: current authenticated node's contributions, not a client source ID filter. |
| `territory_preference` | `territoryPreference` | Territory names matched against evidence entities. |
| `territory_mode` | `territoryMode` | `prefer` (default) ranks matches higher; `only` requires a match when preferences are supplied. |
| `cursor` | `cursor` | Opaque continuation token; start empty, use the returned cursor unchanged. |
| `limit` | `limit` | Page size 1–200, default 100. |

```bash
osbi-client evidence economy <<'JSON'
{"fromDate":"2026-11-01","toDate":"2026-11-07","sourceScope":"own","text":"employment","limit":100}
JSON
```

Choose explicit scope from the request/profile: generic `evidence` defaults to
`all` and does not automatically load `queryDefaults`. Keep filters unchanged
while paging. Continue until the returned `cursor` is empty, even when a locally
filtered page has no items. A repeated cursor is an error; narrow the query
instead of looping. HTTP/API failures are not empty evidence results. CLI/API
also accept server fields such as `asOf`, `excludeQuality` and transitional
`boards`; these are not MCP parameters. Prefer channel IDs and current content
labels over legacy board filters.

Use `publishedDate`, `dateSource`, `sources[].url`/`meta`, entity URIs and relation
labels from returned evidence. Distinguish publication, capture and retrieval
dates. Seed nodes are reference structure, not proof of a reported event.
Local `article:` references are report items, not graph entity URIs.

Optional Jev filtering runs locally after retrieval when configured and enabled.
`jev.filtered`/`discardedRecords` describe that local response, not deletion from
the server. A fully discarded page is not an empty graph. Low confidence or
service failure keeps evidence; report uncertainty honestly.

## Prepare TopNews or Weeklyreport for the agent to write

Read the intended `reportProfiles` entry and task. Use the requested department/
profile or clarify when multiple profiles fit. A section defines heading,
channels and query; a task board overlays selector/count/intent. Use stable
profile and section IDs explicitly: defaults such as TopNews `board="policy"`
may not exist in the user's configuration.

MCP calls:

```text
topnews_select(channel, from_date, to_date, board=SECTION_ID, profile=PROFILE_ID)
weekly_select(channel, from_date, to_date, board=SECTION_ID,
              profile=PROFILE_ID, candidate_uris=[GRAPH_ENTITY_URI, ...])
weekly_record(channel, plan_id)
```

CLI selection reads JSON stdin; recording takes the returned plan ID:

```bash
osbi-client topnews-select economy <<'JSON'
{"profile":"default","board":"economy","fromDate":"2026-11-01","toDate":"2026-11-07"}
JSON
osbi-client weekly-select community <<'JSON'
{"profile":"default","board":"community","fromDate":"2026-11-01","toDate":"2026-11-07","candidateUris":[]}
JSON
osbi-client weekly-record community PLAN_ID
```

Examples require those channels/boards to exist. Weeklyreport selection requires
`fromDate < toDate`; TopNews permits a single date. `candidate_uris` narrows
evidence/entities using graph URIs; do not supply `article:` references. CLI
selection additionally accepts `topN`, `minEdges`, `candidateUris`,
`territoryPreference`, `territoryMode`, `sourceScope` overrides; MCP has the
smaller signature above.

For each board, call selection for its configured channel. If a section combines
channels, prepare each separately and merge/deduplicate locally. Backend report
generation combines all section channels itself. Selections return `hotspots`,
`articles`, `brief`, resolved board metadata, and Weeklyreport `planId` and
classification counts. Compose from originals, preserve configured headings,
and apply the task/board writing instructions.

**Selection and backend generation have different capabilities.** Lightweight
`*_select` endpoints use action-network selection only for `selector=action`;
other selectors use generic date-ranked originals. They do not run the full
TopNews editor or weekly policy-chain writer. They apply board text, content-type,
territory and source-scope filters, but do not apply section `query.entityUris`
or keyword inclusion/exclusion rules automatically. Use `candidate_uris`/CLI
`candidateUris` for entity restrictions, or retrieve generic evidence and apply
those rules locally. `purpose` supplies writing/Jev intent, not guaranteed
semantic classification; the agent must apply prompt overrides when writing.
Local Jev `on` does not bypass global enable switches.

If selection needs semantic relevance beyond literal filters, retrieve evidence
pages and assess originals locally. Publication-date ordering does not establish
topical relevance. Default Shanghai/Jing'an rules are a configurable preset, not
a universal classifier for every jurisdiction/schema.

For weekly writing, omit stale repeats. Put progress topics after fresh topics,
state this week's delta, and cite new evidence rather than retelling an old
meeting. After successfully producing the actual report artifact, call
`weekly_record` for each included plan with its channel. Do not record previews,
omitted topics or failed artifact writes. Recording covers the entire plan: if
the draft uses only some selected hotspots, obtain a matching plan before
recording, or leave it unrecorded and explain the mismatch. TopNews does not consume
weekly coverage. These tools do not generate DOCX, schedule or deliver reports;
report completion and provider delivery are separate facts.

Full generation uses authenticated local `POST /api/topnews/run` and
`POST /api/weekly/run`, followed by `/api/reports/jobs/{jobId}`. Use `osbi-manage`
for those operations and their `send`/dry-run settings. Generation uses separate
report ledgers; lightweight `weekly_record` does not control every backend
report-history path.

## Freshness and historical coverage

Fresh acquisition can still await server ontology batch processing. If freshness
is part of the request, inspect existing run/outbox information or the graph
envelope's processing status (`GET /api/server/channels/CHANNEL/graph`).
`/api/server/status` is basic health, not detailed processing progress. An empty
result establishes only that no matching evidence was returned in this scope/
window at query time.

Historical Word coverage import previews by default:

```bash
osbi-client weekly-backfill community /path/to/actual-report.docx \
  --week-end 2026-11-07 --profile default
```

Review `unmatched` headings and dates; add `--apply` for an intended history
import after matches are valid. Use the actual historical artifact instead of
rerunning today's selection as historical coverage. Coverage is private to the
local client, server identity, channel and stable profile ID. Do not borrow
another client's interests/history or claim server facts were removed by local
ranking or history decisions.
