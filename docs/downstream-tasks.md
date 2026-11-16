# Local Downstream Tasks

The server owns ingestion, ontology extraction, schemas, graph storage and generic
retrieval. For downstream use it fills the retrieval/storage role of a RAG system.
Weekly reports, TopNews and question answering consume query results locally;
their editorial rules, prompts, drafting and task history belong to the local
backend or agent host. The server has no weekly-report selection or drafting API.

The current task interface provides evidence queries, `topnews-select` and
`weekly-select`. Selection prepares evidence and a writing brief for the local
agent to compose the output; it is not a complete document generator. Automatic
document generation, schedules and outgoing delivery are not enabled.

## Local Report Profiles

Sharing a server/channel does not imply sharing editorial interests. For example,
research and sales departments can use the same economy graph while producing
different TopNews and Weeklyreport outputs. Each client stores `reportProfiles`
in its private local configuration; a client can also hold several profiles.
The settings page provides profile and board editors, and the management skill
can update the same fields through `configure`.

Each profile has a stable `id`, display `name`, `focus`, and independent `topnews`
and `weeklyreport` board lists. Each board has:

| Field | Meaning |
|---|---|
| `id`, `title` | Stable board identifier and output heading; unique within each task |
| `channel` | Shared channel to query; a board does not create or change a channel/schema |
| `focus` | Board-specific interests and writing instructions |
| `query` | Optional literal, case-insensitive substring filter on original text/source title |
| `topN` | Maximum number of selected items, from 1 to 20 |
| `selector` | `evidence` for any channel, or `policy-actions` for the existing Shanghai/Jing'an action preset |
| `minEdges` | Minimum relationship count for the action preset only |

Example configuration for one department:

```json
{
  "reportProfiles": [{
    "id": "research",
    "name": "Research department",
    "focus": "Product development, research progress and technical implications",
    "topnews": [{
      "id": "products",
      "title": "Product updates",
      "channel": "economy",
      "query": "product",
      "focus": "Explain changes relevant to our research team",
      "topN": 3
    }],
    "weeklyreport": [{
      "id": "research-progress",
      "title": "Research progress",
      "channel": "economy",
      "query": "research",
      "focus": "Summarize this week's developments and evidence gaps",
      "topN": 5
    }]
  }]
}
```

Pass that JSON to `osbi-client configure`, or edit it through settings/MCP.
`reportProfiles` replaces the whole list: read settings first and preserve other
profiles. Profile/board IDs use ASCII letters, digits, hyphens and underscores.
Keep profile IDs stable so their coverage history remains associated correctly.
Other clients may choose different boards, queries and interests for the same
channel. No report preferences or coverage ledger are synchronized to the server;
only the requested generic retrieval filters are sent there.

The local task combines acquisition `focus`, profile `focus`, board title and board
`focus` into the writing brief and optional Jev decision intent. Without Jev,
natural-language focus guides local agent writing; `query` still filters evidence
deterministically. Generic selection orders original stories by publication date,
collapses repeats and caps the board size. It assumes no entity classes or
relationship names, so it also supports economy and newly created channels.

```bash
echo '{"profile":"research","board":"products","fromDate":"2026-11-15","toDate":"2026-11-15"}' \
  | .venv/bin/osbi-client topnews-select economy
echo '{"profile":"research","board":"research-progress","fromDate":"2026-11-09","toDate":"2026-11-15"}' \
  | .venv/bin/osbi-client weekly-select economy
```

MCP exposes `topnews_select` and `weekly_select`; `settings` enumerates configured
profiles/boards. Each call prepares one board. To assemble an entire output, the
local agent iterates the chosen profile's boards, requests their configured
channels, and writes from the returned evidence according to each brief. TopNews
selection never consumes weekly coverage history. Neither operation starts a
schedule or sends a notification to an external channel.

## Retrieve Evidence as Needed

```bash
echo '{"fromDate":"2026-11-02","toDate":"2026-11-09","limit":100}' \
  | .venv/bin/osbi-client evidence policy
```

MCP exposes `evidence` and entity/path `query`. The generic authenticated server
endpoint is `POST /v1/channels/{channel}/evidence`. It accepts `fromDate`, `toDate`,
`text`, `entityUris`, `limit`, `cursor` and `asOf`. Dates use `YYYY-MM-DD`; an empty
date is unbounded. A dated query excludes undated material. Results contain raw
evidence, source metadata, entity/relationship facts and class ancestry metadata.
Authorization is checked on each request.

Pagination advances over scanned records, so a page can be empty and still return
a nonempty `cursor`. Continue using that cursor and the returned `asOf` until the
cursor is empty. Local weekly selection follows these pages automatically and
requires a narrower query if its bounded scan budget is exceeded. The backend
holds retrieved context in memory; it does not synchronize a persistent graph
replica or open the server's database files.

## Prepare a Weeklyreport Locally

```bash
echo '{"fromDate":"2026-11-02","toDate":"2026-11-09","profile":"default","board":"community","minEdges":4,"topN":2}' \
  | .venv/bin/osbi-client weekly-select policy
```

The local backend retrieves original evidence and returns `planId`, selected
evidence, a writing brief and classification counts. A fresh installation's
`default` profile includes three policy action boards: `party-building`, `community`
(the default) and `digital`. Their Chinese titles are also accepted as board names.
This compatibility preset ranks action hotspots for Shanghai/Jing'an; it does not
restrict which locations enter the shared graph. These titles are not automatic
classifiers: configure a board's `query`, focus or selection `candidateUris` to
narrow its material. Custom boards are unrestricted, and default to the generic
`evidence` selector. The request channel must match the board's configured channel.

The local coverage ledger looks back 28 days from `toDate`, includes the previous
week ending at `fromDate`, and excludes the current week on reruns. It matches URI
first, then normalized similar names, and unions previously reported article IDs:

- `fresh`: no matching prior coverage; ranked first.
- `stale`: no new evidence, or new evidence is only commentary/digest; excluded.
- `progress`: substantive new evidence exists; fills slots after fresh topics. Its
  brief requires this week's developments and a transition from prior coverage,
  without retelling an old meeting/event.

Article identity is independent of extraction revisions. Re-extraction alone is
not new progress. Missing evidence is not treated as substantive new material.

After a report has actually been produced successfully, record the plan locally:

```bash
.venv/bin/osbi-client weekly-record policy PLAN_ID
```

MCP exposes `weekly_select` and `weekly_record`. Never record previews or failed
artifact writes. The ledger and selected task context live in `client.db`, scoped
by server/customer, channel and report profile, independently on each client.
Re-recording a board/week is idempotent and preserves other boards and profiles.
The local API routes are `/api/weekly/{channel}/select`, `/record` and `/backfill`.

## Backfill Published Reports

The client reads Heading 2 board names and Heading 3 hotspot titles from actual
Word reports, queries historical evidence and matches the titles locally. It does
not rerun today's editorial selection to guess what an earlier report contained.

```bash
.venv/bin/osbi-client weekly-backfill policy report_20261102.docx \
  --week-end 2026-11-02 --profile default
.venv/bin/osbi-client weekly-backfill policy report_20261102.docx \
  --week-end 2026-11-02 --profile default --apply
.venv/bin/python client/scripts/backfill-weekly-history.py --reports-dir /path/to/reports \
  --channel policy --profile default --before 2026-11-16
```

Default behavior previews matches; `--apply` writes the local ledger. Only dated
evidence available by the report end is included. Unmatched headings block apply
to protect existing coverage. Historical backfill bypasses the current Jev relevance
filter so present-day preferences do not rewrite past coverage. Backfill past
reports if an existing local installation has no coverage ledger yet.
Section headings must identify configured weekly boards in the selected profile
and channel. Action presets match entity names; generic boards match article
titles. Backfill uses the historical report headings, bypassing present-day board
query filters as well as Jev decisions.

## Optional Jev Decisions

Jev is configured only on the client and disabled by default. The local settings
page provides its switch, base URL, independent API key, model and separate
pre-submission/post-query switches. CLI configuration reads JSON from stdin:

```json
{
  "jev": {
    "enabled": false,
    "baseUrl": "https://api.typesafe.ai/v1",
    "apiKey": "YOUR_JEV_KEY",
    "model": "jev-latest",
    "preSubmit": true,
    "postQuery": true,
    "discardConfidence": 0.8
  }
}
```

```bash
.venv/bin/osbi-client configure < private-config.json
```

The client calls the [TypeSafe System One API](https://docs.typesafe.ai/introduction/quickstart)
at `{baseUrl}/systemone` with structured keep/discard Choice questions. No SDK
installation is required. HTTPS is required for remote providers; HTTP is allowed
for a loopback service. Changing the endpoint/key/model does not affect the shared
server's extraction models or customer key. Stored keys are masked on readback.

Before submission, Jev can skip clearly irrelevant material locally. After evidence
retrieval, it can retain or discard evidence for a local task. Normal graph browsing
remains a view of the shared graph. Graph-query filtering removes unsupported nodes,
edges and paths from that local response only, and marks filtered results; it never
deletes server facts. An empty filtered result is not proof that the shared graph
has no evidence.

Discard requires a valid decision at or above the configured confidence threshold.
Low confidence, unavailable/malformed decisions and oversized inputs retain material;
unavailable decisions create a local warning. Disabled Jev makes no decision-provider
requests. When enabled, decision context is sent to the configured endpoint from
the client; its credentials are never sent to the shared server. Apply the
[content-storage and responsibility notice](../README.md#content-storage-intellectual-property-and-responsibility)
when choosing that endpoint and processing content.
