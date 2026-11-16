# Implementation Verification

Date: 2026-11-15. This is local implementation evidence, not a customer deployment
or a claim about live model quality.

## Implemented baseline

- Standalone shared extraction server with header keys/expiry/revocation, HTTPS
  enforcement, original evidence, durable staged work and channel/query APIs.
- Policy/economy enabled at initial startup with semantically preserved domain
  schemas; novel channels use a fixed generic schema, with no vocabulary mutation.
- Background graph extraction, common daily/cross-midnight batch windows, durable
  receipts/intents and restart recovery.
- Persistent local acquisition backend, managed browser, programmable current-tab
  workflows, human prompts, source intervals, outbox and event history/live stream.
- Locally served graph, settings and notifications pages using real backend APIs.
- DSH management/query skill bundles, real stdio MCP adapter, skill installer and
  pinned host launcher/overlay.
- Basic explicit schema review/activation/reprocessing and offline verified backup/
  restore, without the deferred intelligent curation suite.

## Checks actually completed

On Linux x86_64, Python 3.12.3 and Node 26.8.2:

- 20 Python tests passed with opt-in browser acceptance enabled. Coverage includes
  twenty concurrent duplicate submissions, private request access, expiring keys,
  restricted channels, automatic generic-channel creation, fixed schema vocabulary,
  batch/restart behavior, schema revision/reprocessing, offline restore/tamper checks,
  local settings/privacy/origin checks, prompt acknowledgement, unknown-action
  non-replay, selected-link acquisition before batch graph completion, real MCP
  discovery, explicit retry after provider failure and two independent client
  daemons over verified HTTPS.
- A fresh managed Camoufox profile opened a local dynamic page, clicked into its
  result state and captured original blocks without reopening the URL. Existing
  user profiles were not used.
- The two copied domain schemas were checked for RDF semantic equivalence:
  policy 228 triples, economy 214 triples. Comment cleanup did not change vocabulary
  or constraints.
- Browser TypeScript compilation and dashboard JavaScript syntax checks passed.
- Both skill packages passed skill-creator validation.
- DSH 0.2.2-rc.3 CLI startup and composed OSBI MCP overlay were exercised in an
  isolated host home. The overlay uses an insertion patch, preserving host profiles.
- The three dashboard pages were opened in an isolated real browser against a local
  TLS fixture. Channel switching showed two evidence-backed nodes and one relation
  from the simulated provider. Entity search opened the matching original evidence;
  settings saved successfully without exposing the stored key, and notifications
  established the live-event connection.
- The lock-constrained Python environment passed dependency checks. Documentation
  links, language rules and internal-reference-name/path scans were checked.

## Rule and weekly-history update: 2026-11-16

53 local regression cases passed across the unit/in-process suite and a stored-graph
regression for the corrected post. Coverage includes grassroots jurisdiction,
concurrent substantive posts, preserved hold-post relations, genuine city leaders
visiting another district, report-ledger scoping/idempotency, adjacent-week bounds,
all-prior-evidence union, stale commentary/digests, fresh-before-progress ordering,
delta instructions, Word headings, dated backfill, authorization and local forwarding.
Explicit same-schema rule reprocessing is covered, including repeated completion
without duplicate source observations.

The MCP tool definitions were validated without launching a host/backend. The query
skill passed validation. No DSH, real browser, external model or network integration
environment was started for this update; those acceptance steps belong on the new
integration server. This update adds on-demand action selection and coverage/backfill
operations, not a scheduled report generator or delivery service.

## Remaining acceptance and limits

- The server-model pipeline was exercised with isolated simulated provider responses.
  Real provider credentials, chosen model availability, quality, throughput and cost
  still need end-to-end acceptance. No existing production credentials were copied.
- Real MCP protocol discovery and host configuration passed; a full DSH conversation
  using the selected host model remains untested without its provider setup.
- Intended customer OS/architecture, deployment host/domain and preferred real
  source/interaction path have not been supplied. Native Windows/macOS packaging
  and live account/login behavior are not claimed as verified.
- Acquisition is text-focused. OCR/transcription, complex document parsing, hunters,
  scheduled report generation/outgoing delivery, intelligent curation and online backup automation remain
  outside this implementation. Optional Jev is not enabled or required.
- A dependency currently emits a Starlette/httpx TestClient deprecation notice;
  the tests pass. It does not affect the verified HTTPX production SDK path.

See [running instructions](running.md) for installation, model/key configuration,
local dashboard, DSH, processing modes and explicit schema calibration.
