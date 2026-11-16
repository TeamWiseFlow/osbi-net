---
name: osbi-query
description: Search OSBI shared-channel entities, inspect relationships and paths, and answer questions using original evidence from the shared server.
---

# OSBI Evidence Queries

Discover available channels with the OSBI `channels` tool. Use `query` with a
channel ID and an entity name; inspect returned candidates and select a URI when
names are ambiguous. The same CLI operation is
`osbi-client query CHANNEL SOURCE [--target URI] [--depth 1]`.

Queries by URI return a bounded neighborhood, or a bounded path when a target is
given. Query results include original blocks and source URLs/titles/publication
metadata. Cite those sources near the claims they support. Keep asserted relations,
co-occurrence and unresolved same-name identities distinct. Empty results establish
only that the queried graph currently lacks evidence; they are not proof that a
real-world event or relationship does not exist.

Use server processing status through the dashboard or local API when freshness
matters. Pending graph work or a batch window can make fresh acquisition temporarily
absent from the graph. Do not infer that a failed request is an empty graph.

Acquisition requests go through `osbi-manage` operations if that skill is available;
graph queries themselves do not navigate source sites or mutate schema. Policy
and economy start with fixed domain schemas; new channels use a fixed generic
schema. Clients never add schema classes or relationship predicates.

For an explicitly requested weekly action brief, use `weekly_select` with the
channel, dates, board and a stable report `profile`. Narrow `candidate_uris` when
the board needs a specific editorial scope. The default locality preset is
Shanghai/Jing'an. The server returns original evidence and a writing brief: omit
stale topics; use progress topics only after fresh ones, explain this week's delta
and cite its new evidence instead of retelling an old meeting. After successfully
writing the report, call `weekly_record` with its `plan_id`. Never record a preview,
failed draft or failed artifact write. These operations do not schedule or deliver
reports. Historical Word import is available through `osbi-client weekly-backfill`.
