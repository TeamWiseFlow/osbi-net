---
name: osbi-manage
description: Configure OSBI local sources, browser workflows and personalized TopNews/Weeklyreport boards, run monitoring, and resolve waiting tasks through the persistent backend.
---

# OSBI Management

Use the OSBI MCP tools when available, otherwise `osbi-client --help`. The local
backend must already be running; an agent session does not own its lifecycle.

Read `settings` before changing configuration. Use `configure` for the user's
requested fields; preserve other fields. Secret readback is masked. Configure a
customer-provided server key through the protected local flow; do not print it
in chat, command arguments, or skill files. CLI `configure` reads JSON from stdin.

Sources have `id`, `url`, `kind` (`web` or `rss`), `enabled`, `intervalMinutes`,
`profile`, and `steps`. Interval zero means manual runs. A workflow starts at its
source URL and executes typed steps: `navigate`, `click`, `fill`, `select`, `wait`,
`scroll`, `switch`, `capture`, or `wait_for_user`. Interactions accept a CSS
`selector` or a current snapshot `ref`; text entry uses `text`, navigation uses
`url`, and waits use `selector` or `ms`. Capture the actual current result page,
not a newly opened copy of its URL. A `capture` step ends the navigation sequence.

Use `start_source`, then `runs` to follow the returned durable ID. For a waiting
run, inspect its prompt and use `browser_action` on that same session if interaction
is requested. Refresh `snapshot` after navigation before using action refs. Resume
with the current prompt ID only after the requested interaction is complete.
An uncertain action result requires checking state; never replay it blindly.
Cancel only the selected run. Do not close unrelated browser profiles.

Report preferences live in local `reportProfiles`, independently for each client.
Read settings before editing: `reportProfiles` updates replace the entire list, so
preserve unrelated profiles and boards. A profile has stable `id`, `name`, `focus`,
and separate `topnews`/`weeklyreport` lists. A board has `id`, `title`, `channel`,
`focus`, `query`, `topN`, `selector` and `minEdges`. Use `evidence` selection for a
generic channel; `policy-actions` is the existing Shanghai/Jing'an action preset.
`query` is a literal source-text/title filter; focus guides writing and optional
Jev decisions. Natural-language focus alone is not a deterministic retrieval filter.
Discover eligible channels before choosing them. Boards are local editorial groups,
not channel schemas. Different departments may share a channel and use different
profiles, interests and boards. Stable profile IDs retain their private history.

The local dashboard has graph, settings, and notifications pages. Hunters, outgoing
reports and intelligent schema curation are not demo capabilities. Channel routing
and creation happen on the server; do not generate domain schemas or add types
or predicates from client workflows.

Jev is optional and disabled by default. Its local `jev` settings contain `enabled`,
`baseUrl`, `apiKey`, `model`, `preSubmit`, `postQuery` and `discardConfidence`.
Configure it only at the user's request. Explain that enabled decisions send content
to that endpoint; never copy the customer key into Jev settings. Low confidence or
unavailable decisions keep the material. A GeoIP warning also permits acquisition
to continue while making the exposure/success-rate risk visible in notifications.
