# OSBI Client ↔ Server Contract (target)

Status: proposed — server refactor reference
Goal: OSBI is the production wiseflow architecture with the server moved from
local to remote. The client↔server semantics mirror production
(`material-extract/v3`): the client specifies the channel, the server selects
links the client then follows, ontology is extracted and stored server-side, and
the legacy `info` summary stage is removed.

This document is the contract the server refactor should implement and the client
will target. Field names are chosen for convenience; production parity is in
semantics, not literal protocol strings.

## 0. Principles (production parity)

- **Client decides the channel.** Production maps `focusId → library`
  (`ontology_libraries.json`: focusIds [2,9] → economy, default policy) and uploads
  `ontologyLibrary` explicitly; the server resolves it and uses it directly with
  **zero content routing**. OSBI: client sends `channel`; server does not LLM-route.
- **Board is derived from focus.** Production writes `provenance.board_label` from
  `BOARD_BY_FOCUS {2:经济动态,8:社区治理,9:经济动态,10:基层党建,11:政策速递,12:数字社区}`.
  OSBI server derives `board` from `focus.id` the same way and stores it in
  provenance, so evidence can be filtered by board.
- **Selection link-back is unchanged.** Server relevance-selects blocks/links,
  returns `selection.linkIds`; the client queues those URLs and captures them.
  Production does exactly this (`recorder.add_url(result.related_urls)`).
- **No info stage.** Server no longer returns journal summaries. The client
  topnews/weekly consume original evidence text, not info.
- **Ontology is async and server-owned.** Submission queues selected original
  blocks for graph extraction; the graph is stored server-side. The client does
  not wait for graph completion to follow links (production: completion only
  requires selection + durable block queue, not the nightly graph result).

## 1. Material submission

`POST /v1/materials`  (Bearer customer key; HTTPS)

Request body (`osbi-material/v2`):

```jsonc
{
  "protocolVersion": "osbi-material/v2",
  "clientId": "c_…",
  "requestId": "<64hex>",
  "channel": "economy",            // REQUIRED. client-decided channel/library
  "focus": { "id": 9, "focuspoint": "经济形势、稳定增长、…" },  // per-source focus
  "title": "…",
  "blocks": [ { "id": "B1", "text": "原文块…" } ],   // ≤64; empty for catalog
  "links":  [ { "id": "L1", "text": "标题", "url": "https://…", "context": null } ], // ≤200
  "meta": {
    "requestedUrl": "https://…", "finalUrl": "…", "canonicalUrl": "…",
    "author": "…", "publisher": "第一财经", "publishedAt": "2026-11-20",
    "capturedAt": "…", "adapter": "browser", "textOnly": true, "mediaPending": false
  },
  "materialHash": "<64hex>"
}
```

Changes vs current `osbi-material/v1`:
- `channel` becomes **required** (repurposes the unused `channelHint`). The server
  validates it against the key's grants and uses it directly — no routing model call.
- `focus.id` is a real per-source focus id (not 0). The server derives `board` from
  it via `BOARD_BY_FOCUS` and stores `board`/`board_label`/`focus_id` in provenance.
- `channel` + `focus.id` participate in `materialHash` identity (same content to a
  different channel/focus is a different job — matches production `oq_+block_focus_key`
  including library).

Response: `202` with `{ "jobId": "w_…", "state": "pending" }` (async work).

wx-mp catalog submission is the same endpoint with `blocks: []` and only `links`
(the article URL list). Logically identical; server selects links, client follows.

## 2. Server processing (no route, no info)

For a submitted material the server:
1. **select** — relevance model selects `blockIds` / `linkIds` against `focus`.
   (Keep the existing `select_relevance`; prompts may be Chinese to match production.)
   Apply the production "最近N天" hard date gate when `focus.focuspoint` specifies a
   window and `meta.publishedAt` falls outside it.
2. **queue ontology** — for each selected block, enqueue graph extraction into the
   channel's engine/store (async). Provenance carries `focus_id`, `board`,
   `board_label`, `source_url`, `published_date`, `content_type`, `quality_flags`.
3. **no routing** (channel given), **no info summary**.

The graph extraction itself is unchanged (entity/relation extraction with evidence,
gate_relations, controlled tags) and stored per channel/schema.

## 3. Job polling / selection link-back

`GET /v1/jobs/{jobId}` →

```jsonc
{
  "jobId": "w_…",
  "state": "pending|running|succeeded|failed",
  "selection": { "blockIds": ["B1"], "linkIds": ["L1","L3"] },
  "assignments": [ { "blockId": "B1", "channel": "economy", "board": "经济动态" } ]
  // NO info field
}
```

Client behavior (unchanged from current osbi): on `selection.linkIds`, map ids to
the submitted payload's link URLs, filter `forbiddenDomains`, dedup, extend the run
queue (capped at `maxUrls`), capture those URLs. Graph completion does not block
link-following. `assignments` is provenance only (channel is already known client-side).

## 4. Evidence query (topnews / weekly / QA data source)

`POST /v1/channels/{channel}/evidence`

Request:

```jsonc
{
  "fromDate": "2026-11-14", "toDate": "2026-11-21",
  "boards": ["经济动态"],          // NEW: board filter (production list_articles)
  "contentTypes": ["news","activity"],   // NEW: optional体裁 filter
  "excludeQuality": ["zero_relation","report"],  // NEW: optional quality exclusion
  "entityUris": ["…"],            // existing: graph entity filter
  "text": "…",                    // existing: keyword substring on title+raw_text
  "cursor": "<64hex>", "asOf": 1795238573.28, "limit": 100
}
```

Response:

```jsonc
{
  "items": [{
    "id": "<extractionId>", "evidenceId": "<logicalId>", "schemaId": "…",
    "text": "原文块全文", "title": "来源标题", "publishedDate": "2026-11-20",
    "boards": ["经济动态"],         // NEW
    "contentTypes": ["news"],       // NEW
    "qualityFlags": ["low_density"],
    "entityCount": 6, "relationCount": 4,   // NEW: for ranking
    "sources": [{ "title": "…", "meta": { "requestedUrl","finalUrl","publisher","publishedAt",… } }],
    "entities": [{ "uri","name","label","ancestors" }],
    "relations": [{ "subject","object","label" }]
  }],
  "cursor": "…", "asOf": 1795238573.28
}
```

Server additions vs current `evidence_page`:
- Store and return `boards`, `contentTypes`, `entityCount`, `relationCount` per unit
  (derived at extraction time from focus.id / title classification / graph counts).
- Support `boards`, `contentTypes`, `excludeQuality` filters.
- Ordering for selection convenience: `(publishedDate, relationCount, entityCount)`
  desc when no cursor (production `list_articles` order); cursor paging otherwise.

This makes the evidence query equivalent to production `list_articles` +
`article_view`, so the client can replicate shadow selection without a local
snapshot file. (Seed skeleton / normalize equivalence is a client read-model
concern, see §6.)

## 5. Graph query (unchanged)

`POST /v1/channels/{channel}/query` `{source, target?, depth}` → neighborhood/path
(`nodes/edges/paths` or `candidates/ambiguous`). Used by QA and the graph page.
No change required.

## 6. Client-side changes (for reference, not server scope)

- `Source` config gains `channel` (required) and `focus {id, focuspoint}` (per-source,
  replacing the single global `config.focus` for submission). The mp-economy source:
  `channel=economy, focus.id=9`. A 基层党建 source: `channel=policy, focus.id=10`.
- `_submit_material` fills `channel` + `focus` from the source; bumps protocol to v2.
- topnews/weekly selection algorithms replicated from production shadow processes
  (deterministic graph scoring + dedup + quality gates + locality + publisher penalty
  + policy chains + economy fallback), consuming the §4 evidence query. Whether the
  LLM editing/docx/push layer lives in client or agent is a separate product decision.
- Read-model normalization (seed skeleton, alias merge, canonical labels) to match
  production snapshot semantics for leader/policy detection — client-side, optional
  phase.

## 7. Server refactor checklist (vs current osbi server)

1. `Material` contract: require `channel`, real `focus.id`; bump to v2; include both
   in materialHash identity.
2. `submit`: validate channel against grants; **remove `route`/`_route_current`**
   (no content routing, no dynamic channel creation).
3. `select`: **remove `summarize_block`/info**; keep relevance selection + date gate;
   queue ontology with provenance `focus_id/board/board_label/content_type/quality_flags`.
4. Derive `board` from `focus.id` (BOARD_BY_FOCUS); classify `content_type` from title;
   compute `quality_flags`, `entityCount`, `relationCount`; persist on knowledge_units.
5. `evidence_page`: return + filter `boards/contentTypes/excludeQuality`, return counts,
   selection-friendly ordering.
6. Job result: drop `info`; keep `selection`/`assignments` (channel/board provenance).
7. Keep selection link-back, async ontology, per-channel engine/store (already aligned).

## 8. Open product decisions (client scope, not blocking server contract)

- LLM editing / docx / push placement (client vs agent) for "完全一样" shadow parity.
- Coverage-history semantics: record-at-selection vs record-after-report; action-only
  vs all boards; global vs per-profile scope.
- Read-model seed/normalize equivalence depth.
