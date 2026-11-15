# Local Dashboard Demo Specification

Updated: 2026-11-15
Status: Implemented locally; see [verification](verification.md) for actual check coverage.

## 1. Scope and hosting

Provide three pages: **Graph**, **Settings**, and **Notifications**. Reuse established
graph/configuration/message interactions, without migrating a whole hosted product.
Serve the shell, scripts/styles and ECharts locally from the backend on loopback.
Use same-origin APIs; the backend sends authenticated HTTPS queries to the server.
No public frontend, cloud account login, billing, chat provider, hunter or report
scheduler is required. Product text can be Chinese; repository docs/comments remain
English. Graph data is current server data, not a bundled graph export.

## 2. Common behavior

Use simple navigation and backend/server state indicators. Keep local settings
editable during remote failures. Give meaningful loading/empty/offline/error states.
Label the last graph with its timestamp if refreshing fails. Server-created channels
appear without restarting or rebuilding the frontend. Link actions/errors to run
or job identifiers instead of exposing internal implementation paths.

## 3. Graph page

- Discover eligible channels from the server: policy/economy are active from first
  deployment with their existing schemas; newly created channels use the generic
  schema without adding entity types or predicates.
- Search entities, select ambiguity candidates, inspect node details and bounded
  neighborhoods/paths, and follow source article/original-block evidence.
- Retain viewer pan/zoom/highlight behavior; search/selection should preserve useful
  manual zoom rather than repeatedly resetting layout.
- Show graph revision, completed watermark, pending count, continuous/batch mode,
  next processing window when waiting, and active schema revision in channel detail.
- Distinguish asserted relations, co-occurrence and unresolved identity.
- Bound rendering/pagination and label truncated results. Do not require downloading
  the full graph before the initial view.

All data comes through the backend's server facade. Schema revision metadata is
read-only. Graph/schema administration and intelligent curation controls are
deferred; server-side evidence retention and periodic manual calibration remain
required without adding a schema editor to the dashboard.

## 4. Settings page

| Group | Demo behavior |
|---|---|
| Server | HTTPS URL, masked configured-key metadata, replace/remove key and explicit save feedback |
| Acquisition | Source URLs/RSS, focus, enabled state, simple interval and URL/run budget |
| Browser | Profile/workflow association, global/domain concurrency and relevant timeouts |
| Workflow | Select a supported definition/typed parameters; run/pause/cancel with a run ID |
| Local preferences | Supported display options and configuration reset |

Validate before saving the active configuration revision. The backend stores it
locally and reloads it after restart; running jobs retain their starting revision.
Readback masks secrets, replacement uses an explicit secret field, and browser
storage contains no credential. A local save must not trigger unrelated platform
probes/messages. If connection testing is offered, it is an explicit user action.

Only backed operations appear as features. There is no report scheduling/resend,
arbitrary calendar editor, rich workflow designer or cloud account configuration.
Optional Jev fields only become operational with a tested adapter; default off.

## 5. Notifications page

- Show durable historical/live events with severity filtering, reload and useful
  reconnect status; include run/job/channel references.
- Cover acquisition start/finish, capture errors, offline/submission retry, automatic
  channel assignment/creation, graph queued/completed/window-wait and key failures.
  The backend translates observed server job-status changes into local events.
- Show human prompts with prompt/run IDs, reason/current state, and supported open
  session/resume/cancel actions. Resume validates the actual workflow/browser state.
- Persist resolution events; reconnect must neither resurrect resolved prompts nor
  hide pending human waits.

Recommended transport is SQLite history plus cursor REST reads, SSE live replay
and REST actions. WebSocket is acceptable with the same durable cursor/action
semantics; do not import a full old messaging subsystem just for its component.

Events carry stable ID/cursor, timestamp, severity/type, readable message, optional
run/job/channel IDs and prompt metadata. Persist before publishing, merge by ID,
resume from cursor, paginate history and bound retention. Exclude secrets, raw
provider exceptions and private filesystem paths from event output.

Prompt actions are idempotent and validate current run/prompt version. Hide/clear
view only changes read/display state; it does not approve an action or delete the
job/audit record. Arbitrary chat input and outgoing social-platform messages are
outside this demo.

## 6. Acceptance

- All three pages run from one local service without an external frontend/account.
- Settings persist across reload/restart; invalid values do not replace working
  configuration; no keys appear in assets or readback.
- Viewer uses shared server results, discovers new channels, exposes source evidence
  and accurately shows pending/completed/batch status.
- A source run appears in both history and live updates; reconnect has no duplicate
  entries or missing unresolved prompts.
- Human wait resumes once through a current prompt; stale/duplicate actions have no
  unintended effect; resolved prompts remain resolved.
- Empty/offline/expired-key/partial cases are clear and actionable.
- Closing UI/DSH leaves backend and source work running.

Use temporary local pages/state and fake providers for deterministic checks, then
a supported client-to-HTTPS-server rehearsal. These are planned gates, not passed
tests.
