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

The response schema, stage errors, block selection, and ontology queue are
unchanged. Page publication dates still gate only selected body blocks; each link
retains its existing date rules, and captured article bodies undergo the body date
gate before entering the ontology queue.

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
