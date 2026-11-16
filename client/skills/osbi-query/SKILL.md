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

For TopNews or Weeklyreport, read `settings` and use the user's local
`reportProfiles`. If multiple profiles could apply, use the requested department
or clarify the intended profile. Do not replace it with another client's interests.
Call `topnews_select` or `weekly_select` for each requested board, using its configured
channel, stable profile ID, board ID and dates. The backend applies the board's
query and optional Jev intent, and returns selected originals and a writing brief.
Compose the output locally, preserving board headings and following the combined
profile/board focus. Cite the originals. These are preparation tools, not automatic
document generators; never claim a report was delivered by calling selection.
If Jev is disabled and selection needs semantic judgment beyond a literal query,
retrieve generic `evidence` pages and make that judgment locally before drafting;
publication-date ordering alone does not establish topical relevance.

Generic `evidence` boards work with any schema. The default weekly policy-action
preset is Shanghai/Jing'an; it does not classify content into board headings
automatically. Use configured `query` or narrow `candidate_uris` for that scope.
Local `article:` item references are not graph entity URIs. For weekly outputs,
omit stale topics; use progress topics only after fresh ones, explain this week's
delta and cite new evidence instead of retelling an old meeting. After successfully
writing the report, call `weekly_record` with its `plan_id`. Never record a preview,
failed draft or failed artifact write. These operations do not schedule or deliver
reports. TopNews does not consume weekly coverage. Historical Word import is
available through `osbi-client weekly-backfill`.

Use `evidence` for a generic dated/text evidence page; follow nonempty cursors even
when a filtered page is empty. Downstream selection, report preparation and coverage
history are local. Optional Jev decisions run locally before submission or after
retrieval. A result marked as Jev-filtered must not be described as an empty server
graph; evidence may have been discarded from this local response.
