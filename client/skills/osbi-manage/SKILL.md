---
name: osbi-manage
description: Configure OSBI local acquisition sources and browser workflows, run monitoring, and resolve waiting tasks through the persistent backend.
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

The local dashboard has graph, settings, and notifications pages. Hunters, outgoing
reports and intelligent schema curation are not demo capabilities. Channel routing
and creation happen on the server; do not generate domain schemas or add types
or predicates from client workflows.
