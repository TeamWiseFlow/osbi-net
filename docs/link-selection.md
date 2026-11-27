# v4 Link Roles and Selection

Every link in `input.links[]` sent to `POST /v1/materials/extract` must include
`role`, with exactly one of `content`, `navigation`, or `unknown`. Missing, null,
and unsupported roles receive HTTP 422. There is no missing-role compatibility
mode. The client classifies links; the server does not infer roles from keywords,
URLs, domains, or page structure.

```json
{
  "id": "L1",
  "text": "Latest updates",
  "context": "News section",
  "role": "navigation"
}
```

The relevance prompt receives each link's role. Relevant `content` links take
priority; `unknown` links are judged against the channel statement as usual.
`navigation` links should be selected only when they are necessary entry points
to obtain the latest content, after relevant content and unknown links. A missing
publication date on a content link is not a reason to replace it with navigation.
Role is classification metadata, not source content or an instruction.

After all relevance batches are combined, the server preserves the model's link
order and applies the existing per-link publication-date filter. It then:

1. Deduplicates selected links by normalized `(text, context)`. Normalization uses
   Unicode NFKC, case folding, and trimmed, collapsed whitespace; absent and empty
   contexts are equivalent. Prefer content over unknown over navigation. Equal
   roles keep the first model-selected occurrence; unselected candidates are never
   introduced.
2. If navigation remains, orders links by content, unknown, then navigation,
   preserving model order within each role. If no navigation remains, keeps the
   surviving links in model order without a role sort or count limit.
3. Keeps at most one navigation link alongside content/unknown, or at most three
   when the selection contains only navigation. Pure-navigation selections retain
   an entry-point fallback.

These limits apply once per request, across all batches and on cache hits.
The relevance cache stores the uncapped selection in model order. Link roles do
not enter its content identity, so a role change reuses the model selection and
reapplies the deterministic policy using current roles. Prompt revisions invalidate
old relevance results for both windowed and non-windowed channels. Material hashes
and ontology queue/extraction identities are unchanged.

Block selection and the ontology queue keep their existing behavior.
Page publication dates still gate only selected body blocks; each link
retains its existing date rules, and captured article bodies undergo the body date
gate before entering the ontology queue.

## Invalid output recovery and errors

Every relevance batch validates block and link IDs against its own candidate
sets. An out-of-batch ID triggers one additional attempt on that model with a
correction instruction and the original candidates. The invalid answer is never
accepted or cached. If the second answer is still invalid, the existing fallback
chain continues. Distinct model names are deduplicated: identical primary and
fallback names allow at most two attempts; two distinct models allow at most
four. This extra attempt applies only to invalid IDs, not transport failures,
model deadlines, malformed JSON, or task cancellation. Each actual attempt and
its returned token usage remain part of the job's usage totals.

The synchronous response still uses `errors[].stage = "selection"` for a failed
selection and adds an optional machine-readable `errors[].code`. The final job
monitor `error_code` uses the same value. Codes are `invalid_selection_ids`,
`invalid_model_output`, `llm_deadline`, `llm_transport_error`, `llm_http_error`,
or `selection_failed`. Existing `stage`, `message`, and date-gate behavior remain
available; successful recovered jobs have no error. Historical jobs are not
rewritten. Error metadata contains no candidate text or raw model answer.

The client has a separate material-extract read timeout, default 300 seconds,
while its general network timeout remains unchanged. Waiting for a response may
include model admission, an earlier identical selection, or multiple batches;
300 seconds is not a server execution guarantee. A client read timeout does not
necessarily cancel server work. See the client manual for the setting and
separate connection, certificate, authentication, and read-timeout diagnostics.

Each selection logs these fields, including empty selections and cache hits.
Failed selections log zero counts and retain their existing stage errors:

| Field | Meaning |
| --- | --- |
| `selected_content_links` | Content links in the returned selection |
| `selected_unknown_links` | Unknown-role links in the returned selection |
| `selected_navigation_links` | Navigation links in the returned selection |
| `navigation_nav_cap_dropped` | Navigation links removed by the count limit, excluding deduplication and date filtering |
| `navigation_only_fallback` | 1 when an eligible, nonempty selection contains only navigation; otherwise 0 |

For requests with a node identity, the same values accumulate per node/source in
SQLite `node_source_metrics`, visible through `GET /v1/metrics/sources`. Startup
adds missing counter columns with zero defaults, preserving existing metrics.
Historical role counts are not reconstructed. Logs are also emitted without a
node identity. Metrics are observability only; a metric write failure does not
reject content or stop the ontology queue.
