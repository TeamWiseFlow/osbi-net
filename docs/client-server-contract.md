# Client–Server HTTP Contract

The active protocol is `material-extract/v4`, implemented by
`server/wiseflow_server/models.py` and exposed by `POST /v1/materials/extract`.
This document describes the current runtime. The retired `osbi_server` async
material polling protocol, caller `focus` and channel-routing stage do not apply.

## Authentication and Channels

Clients use HTTPS and `Authorization: Bearer ...` for shared business routes.
The server resolves existing channel statements/schema/date settings. In a managed
deployment, the key determines the net and node; an optional submitted `nodeId`
must match that identity. Revocation, expiry and enabled-net checks apply on each
authenticated request. See [managed v4 deployment](supabase-v4-deployment.md).

`GET /v1/channels` lists available channels. Sources assign one or more existing
channels; each channel gets its own request. The server does not infer a channel
from content. A source without channels collects locally without submission.

## Material Extraction

```json
{
  "protocolVersion": "material-extract/v4",
  "channel": "community",
  "materialId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "input": {
    "title": "Community update",
    "blocks": [{"id": "B1", "text": "Original body evidence."}],
    "links": [
      {"id": "L1", "text": "Article details", "context": "Related story", "role": "content"},
      {"id": "L2", "text": "Latest updates", "context": "Section", "role": "navigation"}
    ]
  },
  "meta": {
    "requestedUrl": "https://example.org/story",
    "publishedAt": "2026-11-25"
  },
  "mode": "both"
}
```

Block/link IDs must be unique within their candidate lists. `materialId` is a
64-character lowercase hex identifier. Link roles are **required**, with only
`content`, `unknown` and `navigation` accepted; missing/null/invalid roles return
422. URL maps remain client-side, while source metadata is separate from selector
input. Role classification does not change material identity. Catalog/hunter
links without DOM classification use `unknown`.

`mode=both` selects body blocks and links independently. `only_link` excludes
blocks from selection and the ontology queue, even if supplied. Relevance prompts
receive the server-owned channel statement, required link roles and applicable
date evidence. There is no standalone info summary or content-based routing.

Selection returns synchronously. Selected original blocks are durably queued;
graph extraction completes in an independent batch. The client can follow selected
links without waiting for that batch.

```json
{
  "protocolVersion": "material-extract/v4",
  "materialId": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "selection": {"blockIds": ["B1"], "linkIds": ["L1", "L2"]},
  "modelCalls": 1,
  "ontologyJobs": [{
    "blockId": "B1",
    "id": "oq_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "status": "pending",
    "cached": false
  }],
  "errors": []
}
```

The client restores selected link IDs through its URL map. The server deduplicates
selected text/context, prioritizing content over unknown over navigation. With
navigation present it stably orders by role and keeps at most one navigation
alongside content/unknown, or at most three for pure navigation. Without navigation
it keeps model order. Limits apply globally across batches and to raw cache hits.
See [complete selection policy and counters](link-selection.md).

Body and link date checks remain independent. A page date can reject selected
body blocks with `date_gate` errors without clearing eligible links. Links use
only their own supported date evidence; undated links remain eligible. Captured
article bodies face the body date gate again. Other error stages are `selection`
and `ontology_queue`; partial stage errors preserve the response structure.

## Evidence and Graphs

`POST /v1/channels/{channel}/evidence` retrieves persisted original blocks, sources,
entities and relations. Query options include date windows, text, entity URIs,
source scope, locality, limit and cursor. `sourceScope=own` restricts node sources;
`all` uses authorized shared evidence. Pagination can return an empty page with a
nonempty cursor; continue until the cursor is empty.

Channels can share an ontology store. Graph views are store-wide where the API
specifies a library/store; channel evidence is filtered by provenance channel.
`GET /v1/ontology/blocks/{id}` reads a completed result; batch status is available
at `GET /v1/ontology/batches/status`. The client backend adapts graph/query endpoints
for its dashboard. UI language, local report profiles, DOCX names and source
capture schedules are not server configuration.

## Local Client Configuration

The loopback dashboard uses a separate authenticated `/api/*` facade. It protects
writes with its local cookie/bearer, same-origin checks and `X-OSBI-Local: 1`.
`PATCH /api/config` validates source/report settings, including:

- `sources[].schedule.times`: multiple unique daily `HH:MM` clocks.
- `sources[].schedule.timezone`: IANA timezone; daily clocks override the interval.
- `reportProfiles[].weeklyreport.docxFilename`: base name, with automatic date suffix.

Public configuration masks secrets and returns read-only `sourceNextRuns` metadata.
Patch intended configuration fields, not this metadata. Language is stored per
browser and is never sent as channel/schema or report-content instructions.
