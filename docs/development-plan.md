# Customer Demo Delivery Plan

Updated: 2026-11-15
Target: Customer deployment during the week of 2026-11-16.
Status: Initial local implementation available; customer deployment has not been performed.

Read the [architecture](architecture.md), [dashboard scope](dashboard-demo.md), and
[technical baseline](reference-baseline.md). Develop on `dev`; use pull requests
for `master`.

Implementation and check results are recorded in [verification](verification.md).

## 1. Scope frozen for the demo

| Included | Required behavior |
|---|---|
| Shared server | Independent schemas/extraction/graph/query service |
| Customer access | HTTPS, header keys, expiry/revocation and shared-channel authorization |
| Automatic channels | Both policy/economy active at first startup with their existing schemas; auto-created channels bind a predefined generic schema |
| Schema control | No automatic entity-type/predicate additions; immutable revisions, original evidence and diagnostics for periodic manual calibration |
| Graph modes | Continuous default plus a common environment-configured batch window |
| Client | Persistent local backend, original acquisition/cleaning/chunking and durable submission |
| Browser/workflows | Current managed Camoufox, general process, a programmable interactive source path and human wait/resume |
| Basic monitoring | Persisted source interval/next-run state and duplicate prevention |
| Local dashboard | Mandatory graph viewer, settings and notifications; no public web hosting |
| DSH | Management/query skills, CLI/MCP, independent backend lifetime |
| Reliability | Cross-client reuse, source evidence, explicit job progress/errors and restart recovery |

Deferred: hunter integration; scheduled topnews/weeklyreport generation/delivery;
report resend UI; intelligent curation CLI; rich visual workflow editor; the other
four agent platform implementations. Jev is optional and cannot block the demo.
Minimal key administration and service lifecycle commands remain necessary.
Deferring intelligent curation does not defer schema versioning, strict vocabulary
validation, evidence retention, or the ability to review and publish a manual
schema revision.

## 2. Confirmed decisions and deployment inputs

| Decision | State |
|---|---|
| Visibility | Confirmed: same-channel shared data, separate customer operational state |
| Initial channels | Confirmed: policy and economy both active from first deployment, each with its existing domain schema |
| Category selection | Confirmed: automatic server assignment and no-match creation |
| Graph freshness | Confirmed: continuous default, environment-controlled common window |
| Dashboard | Confirmed: required now, local graph/settings/notifications |
| Deferred work | Confirmed: hunters, scheduled reports and intelligent curation may follow |
| Jev | Identified through TypeSafe's official docs; optional client assistance |
| Generic schema | Confirmed: simple predefined schema for new channels without a dedicated schema |
| Schema vocabulary | Confirmed: ingestion cannot automatically add entity types or relation predicates |
| Schema calibration | Confirmed: periodically review accumulated evidence, explicitly revise/validate/activate schemas; intelligent assistance may follow |

Implementation recommendations: one server host with embedded stores, a small
backend-served dashboard, backend-mediated authenticated server queries, and
simple source intervals retained from the monitoring requirement. Before packaging,
identify the customer's actual OS/architecture and demo source. Exact customer
installation day is not specified; do not claim untested native platform support.

## 3. Five-day working sequence

Dates below are working targets for November 16-20, 2026, not completed milestones
or a claim about the final installation slot. Integrate the dashboard early; it
must not wait behind deferred features.

### D1 — Contracts and standalone shared server (November 16)

- Freeze original-material, automatic routing, job/channel and access contracts.
- Separate extraction/schemas/storage from client imports/databases.
- Package the existing policy/economy schema definitions and required controlled
  tags; enable both channels before first readiness. Freeze their semantic content
  and validate sample extraction without replacing either with generic vocabulary.
- Define a small generic schema; add match/no-match routing, atomic channel creation
  and binding to its existing published revision. Enforce allowed types/predicates
  and domain/range rules; never mutate schema during ingestion.
- Record schema revisions, original block references, class/predicate usage and
  rejected assertion reasons/samples so later data review has usable evidence.
- Add durable work, original observations, bounded extraction, graph owner/query,
  customer key operations and initial HTTPS setup.
- Connect a minimal graph page to real API data to verify the dashboard path now.

Checkpoint: original material is assigned/extracted and visible with evidence.
A novel topic creates a channel using the generic schema without changing any
schema vocabulary. Both initial domain channels are already active; their existing
schema semantics are preserved. Missing/expired keys fail on submission and reads.

### D2 — Persistent client and browser loop (November 17)

- Package local daemon, separate state root, protected settings, server SDK,
  outbox, lifecycle/CLI commands and durable events.
- Adapt the managed browser/material projection: DOM evidence, clean blocks,
  metadata/link maps, domain rules and server-selected-link recursion.
- Prove one actual interactive workflow captures its current result tab and can
  pause/resume for human verification.
- Add simple persisted acquisition intervals and next-run/claim state.
- Wire the local viewer to backend-mediated HTTPS queries and live channel discovery.

Checkpoint: source acquisition produces server graph evidence in the local viewer.
Human wait produces an event; pending submissions survive restart with provenance.

### D3 — Finish all three local pages (November 18)

- Graph: search/highlight, bounded paths/neighborhoods, evidence links, dynamic
  channels and graph revision/freshness/processing state.
- Settings: load/save/validate, masked key replacement, sources/focus/interval,
  workflow parameters and browser limits; no account/billing onboarding.
- Notifications: durable history, filters, live updates/reconnect/backfill,
  run/job references and human-action prompt controls.
- Serve assets locally from one backend; apply local session/origin checks.

Checkpoint: all pages work against real local APIs after a clean restart. Settings
persist; events replay without duplicates; graph data is not a bundled export.

### D4 — DSH, graph modes and two clients (November 19)

- Add management/query skills and a pinned DSH MCP configuration.
- Share backend operations across CLI/MCP/dashboard, including long-running IDs,
  errors and prompt state. MCP probe startup must have no service side effects.
- Implement continuous/default and common batch windows, cross-midnight behavior,
  backlog display and restart inside/outside the window.
- Run two isolated clients: reuse compatible work, preserve distinct observations,
  discover new channels, and create one category under concurrent novelty.
- Leave Jev disabled unless an optional adapter has passed isolated checks.

Checkpoint: DSH configures/runs a source and queries evidence; closing it leaves
backend work alive. A second client reuses results. Batch waiting is visible as
progress. Concurrent submissions of one novel topic create one channel.

### D5 — Customer package and deployment rehearsal (November 20)

- Install from the package on the intended OS/architecture with fresh OSBI state;
  verify dependency paths, pinned assets and portable execution.
- Rehearse the script below over HTTPS with a dedicated demo key. Runtime execution
  must not depend on any other development checkout or existing profile.
- Check targeted restart, graph intent reconciliation, offline outbox, key expiry
  and invalid configuration; avoid repetitive unrelated testing.
- Rehearse manual schema calibration on an isolated sample: inspect stored evidence
  and rejection diagnostics, validate an edited revision, explicitly activate it,
  reprocess affected originals and restore the prior revision on failure. A
  validated configuration/restart path is sufficient; intelligent CLI is deferred.
- Provide concise service/install/update/key/processing-mode instructions and
  aligned root READMEs. State actual platform evidence and rollback/start/stop steps.
- Rehearse a consistent metadata/graph backup and isolated restore.

Checkpoint: clean installation completes the script with all three pages, and
restart preserves settings, events, source progress and graph jobs. Actual customer
deployment follows the agreed slot and environment.

## 4. Interface deliverables

| Server route | Purpose |
|---|---|
| `GET /v1/capabilities` | Protocol versions, limits and operations |
| `GET /v1/channels` | Eligible channels, including automatically created ones |
| `POST /v1/materials` | Original blocks; server chooses/creates assignments |
| `GET /v1/jobs/{id}` | Authorized routing/selection/info/graph status and receipts |
| `GET /v1/channels/{id}/entities` | Search and ambiguity candidates |
| `POST /v1/channels/{id}/query` | Bounded entity/neighborhood/path query |
| `GET /v1/channels/{id}/blocks/{id}` | Source evidence under channel policy |
| `GET /v1/status` | Processing mode, pending count, watermark and batch window |

Local surfaces: `/graph`, `/settings`, `/notifications`; masked config read/update;
source/workflow/run operations shared by CLI/MCP; cursor-based event history/live
stream; validated prompt resume/cancel; authenticated HTTPS server query facade.

Proposed environment contract:

```dotenv
OSBI_GRAPH_PROCESSING_MODE=continuous
OSBI_GRAPH_BATCH_WINDOW=02:00-06:00
OSBI_GRAPH_BATCH_TIMEZONE=Asia/Shanghai
```

Set mode to `batch` for the common daily window. Validate on startup, preserve
backlog and expose waiting explicitly. Per-channel calendars and scheduling UI
are not demo requirements.

## 5. Customer script and acceptance gates

1. Start the HTTPS server and verify policy/economy are already active with their
   existing domain schemas, then start the local backend and dashboard.
2. Save URL/key, source/focus/workflow and acquisition settings. Reload/restart
   to show local persistence and masked credential readback.
3. Start acquisition through DSH/CLI or a settings-page source action; follow its
   notification progress and resolve a human-verification prompt when needed.
4. Show automatic policy/economy routing and source-backed graph results; submit
   a novel topic and show its new channel bound to the predefined generic schema,
   without client reconfiguration or any automatic type/predicate addition.
5. Search an entity, inspect relationships/path and original evidence; distinguish
   pending, empty and completed graph work accurately.
6. Submit the same content from a second independent client and show shared graph,
   reused work and additional provenance where applicable.
7. Use a controlled clock/window configuration to demonstrate batch waiting and
   later consumption without losing queued work.
8. Show useful offline/expired-key recovery without lost submissions or credential
   disclosure. Closing the UI and DSH leaves the service running.

Technical gates cover twenty concurrent duplicate submissions; atomic novel-channel
creation; revision-aware reuse; private handle authorization; crash between graph
commit/SQL finalization; notification replay without duplication; and stale/duplicate
prompt acknowledgement. Use temporary state, recorded fixtures and fake providers
for deterministic checks, followed by explicit live browser/model rehearsal.
Outgoing platform messages are unnecessary for this demo.

Schema-specific gates verify that startup enables both existing domain schemas,
unknown types/predicates never alter vocabulary, new channels use the generic
revision, and review evidence survives rejected assertions. Validate manual
revision/activation and failed-reprocessing recovery without requiring the
deferred intelligent CLI or adding a dashboard schema editor.

## 6. Deadline risks and controls

| Risk | Response |
|---|---|
| Snapshot/material mismatch | Prove current-tab projection on D2; do not rebuild the driver |
| Forced or duplicate categories | Separate fit/no-match from choice; alias checks and transactional creation |
| Ingestion changes vocabulary or seed schemas are replaced | Freeze both existing domain definitions, bind generic only to new channels, validate every assertion and keep explicit revision activation |
| Deferring curation loses calibration evidence | Keep originals, revisions, usage counts and rejected-assertion samples in the demo; add assisted review tooling later |
| Inherited cloud UI dependencies | Port only graph/config/message behavior into the local shell |
| Customer OS/source differs | Obtain intended environment for acceptance; label unsupported combinations honestly |
| Optional work consumes the week | Defer hunters/reports/curation/Jev; dashboard stays mandatory |

Do not add a hosted login/billing stack, new graph database, rich workflow editor
or multi-host server control plane for this demo. Keep durable correctness and
server/client boundaries. Dependency independence does not imply delegated or
parallel agent work.

## 7. Post-demo roadmap

- Eight hunter adapters and full dependency/normalization coverage.
- Personalized topnews/weeklyreport, report schedules, artifacts/citations,
  outgoing connectors and regenerate/resend controls.
- Assisted entity merge/split and schema optimization through reviewable,
  auditable, reversible CLI changes.
- Optional Jev, richer workflows, other agent platforms, broader OS support and
  capacity-driven scaling.

Define follow-up acceptance separately; deferred capabilities do not block a demo
meeting shared-server, acquisition, DSH and local dashboard gates.
