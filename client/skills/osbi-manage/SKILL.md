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

Sources have `id`, `url`, `kind` (`web`, `rss` or `mp`), `enabled`,
`intervalMinutes`, `profile`, `accounts` (mp whitelist) and `channels`. `channels`
lists global server channel IDs; multiple channels fan out separate submissions and
an empty list means acquisition-only. Interval zero means manual runs. A workflow starts at its
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
preserve unrelated profiles. A profile has stable `id`, `name`, `sections`,
`targets`, `topnews` and `weeklyreport`. `sections` are shared templates with
`id`, `title`, `channels`, `query` (`text`, `entityUris`, `contentTypes`,
`territoryPreference`, `territoryMode` prefer|only, `sourceScope` all|own) and
`localRules` (`keywords`, `excludeKeywords`, `leaderBoost`, `localityPenalty`,
`jev` inherit|on|off). A task board only overlays `section`, `selector`,
`topN`, `minEdges`, `purpose` and `prompt`; never mutate the section template.
Selectors are `candidates` (TopNews editor), `action` (weekly action network),
`policy` (weekly policy chain) and `evidence` (generic retrieval). A task has
`enabled`, `schedule` (`time` HH:MM, optional `weekday`, `timezone`), `boards`,
`targetIds` and `dryRun`. `purpose` guides local writing and optional post-query
Jev; it is never uploaded as server relevance.

`targets` are profile-level delivery endpoints: `mimicwx` (`baseUrl`, `window`,
write-only `token`, optional `configPath`), `awada` (`configPath`, `channelId`,
optional `fileChannelId`), and reserved `wecom-webhook` (write-only `webhookUrl`,
`mentioned`, `msgType`; sending returns `not_implemented`). Secret fields are
write-only: readback shows only configured/suffix, and a blank value preserves
the stored secret. `dryRun` records reports without sending. Discover eligible
channels before choosing them; boards are local report units and may share or
combine channels. Stable profile IDs retain their private history.
The local dashboard has graph, reports, downstream tasks, settings and notifications pages. Hunters, outgoing
reports and intelligent schema curation are not demo capabilities. Channel routing
and creation happen on the server; do not generate domain schemas or add types
or predicates from client workflows.

Jev is optional, disabled by default, and post-query only. Its local `jev` settings
contain `enabled`, `baseUrl`, `apiKey`, `model`, `postQuery` and `discardConfidence`.
Configure it only at the user's request. Explain that enabled decisions send content
to that endpoint; never copy the customer key into Jev settings. Low confidence or
unavailable decisions keep the material. A GeoIP warning also permits acquisition
to continue while making the exposure/success-rate risk visible in notifications.
