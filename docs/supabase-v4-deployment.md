# Supabase-managed v4 deployment

Run `python -m wiseflow_server` with the repository's `server/` on `PYTHONPATH`.
Install the `server` extra using the root `requirements.lock`. No client,
browser, Node or agent assets are required.

Copy `server/config/example/runtime.yaml` outside the checkout. Configure:

```yaml
server:
  auth:
    mode: supabase
    dataScope: net
    nodeAliases: {}
```

Set `OSBI_CONTROL_URL` to the existing Supabase API origin and
`OSBI_CONTROL_KEY` to its service-role secret, in a private systemd environment
file. The adapter calls the existing `ops.osbi_authenticate(p_key_hash)` and
`ops.osbi_net_definitions()` RPCs. **Do not reapply deployment SQL or modify the
existing tables/functions.** Revocation, expiry and disabled nets are checked
on every authenticated request; a control-plane outage fails closed. The
existing per-key `rpm` remains enforced (zero means unlimited).

`ops.osbi_keys.customer_id` is the stable node identity. Use a short node ID
such as `shizishan-chengdu` when issuing a new key in Studio. `nodeAliases` can
map an existing customer ID to an external node ID without editing its row.
Other customer IDs outside the node-ID syntax get a deterministic hashed ID.
The request's optional `nodeId` must match the authenticated identity.

With `dataScope: net`, databases, caches, metrics and graph owners are isolated
at `<dataDir>/nets/<net-id>/`. All configured channels are available inside
each net. As required by the v4 shared-channel design, the legacy `schemas`
and `schema_permissions` columns remain untouched but do not restrict channels.
With `dataScope: shared`, all enabled nets intentionally share one dataset at
`<dataDir>/nets/shared/`; net enablement still controls authentication. Changing
scope selects another dataset and does not migrate existing data.

The model client and its concurrency semaphore are shared across nets. Each
net has its own durable nightly backlog. Schema and seed files stay outside
the checkout; seeds load idempotently at startup and affect the configuration
fingerprint. New releases must use fresh state when replacing the retired
`osbi_server`; its old databases are not an input to the v4 engine.

`GET /health` reports protocol, channels, model names, paths and fingerprint.
`/healthz` and `/readyz` are compatibility aliases for that process/configuration
check; they do not make model calls. Business routes require a key. Validate
the control plane separately with an authenticated channel request. Terminate
TLS at Nginx and bind the app to loopback. Allow enough proxy read time for a
synchronous extraction request (the old 90-second timeout is insufficient).

Stop the service before copying state for a consistent backup. Keep production
secrets and source-content backups out of repositories.

## Date windows for blocks and navigation

Channels without `dateWindowDays` keep their existing selection behavior. In
windowed channels, page publication dates gate selected original blocks only.
An unknown, ambiguous, invalid or out-of-window page date clears `blockIds`
without clearing `linkIds`. Each rejected selected block receives a
`date_gate` error with its `blockId` and one of `date_window`, `date_unknown`,
`date_ambiguous` or `date_invalid`. This is a normal HTTP 200 gate result, not
a job failure. No block selection means no block date-gate error. Rejected
blocks never enter the ontology queue.

`only_link` never applies the page date gate or queues supplied blocks. Both
modes independently screen selected links using only their own `text` and
`context`. Explicit absolute release-date labels take precedence; otherwise
only standalone or leading/trailing date metadata is recognized. Invalid,
conflicting or out-of-window release dates discard that link. Undated links
remain eligible. Relative dates, event dates, editions and historical years
do not establish a link release date, and page dates never substitute for it.
There is no directory-page classifier in this decision.

The relevance prompt selects blocks strictly and links by channel topic,
including undated links worth following. It receives the resolved block date
when available. Returned links remain available for client recursion; fetched
article bodies pass through the existing publication-date parser and gate,
preserving date source, expression, reference, estimation and conflict fields.

Windowed relevance caches include the effective prompt revision and resolved
block publication date. Non-windowed prompts and cache identities are unchanged.
Client submission caches are independent: clear an affected directory's old
submission result when retesting a previously cached empty selection.

## Job monitoring in Studio

Apply the additive administrator migration `server/deploy/job-monitor.sql` once
before enabling the updated service. It creates `work.osbi_jobs` and its
cleanup schedule; it does not modify the key/net control plane or the legacy
`work.llm_requests` table.

In Supabase Studio, choose **work → osbi_jobs**, sort `updated_at` descending,
and filter by `net_id`, `node_ids`, `channel_id`, `job_type` or `status`.
`selection` rows track new material requests; `ontology` rows track durable
block jobs; `batch` rows track manual and scheduled runs. Existing block jobs
from the last seven days are imported at startup. Earlier material requests
and completed batch runs without a journal cannot be reconstructed.

Statuses include `queued`, `running`, `succeeded`, `retrying`, `interrupted`,
`rejected` (for example a date gate), `failed` and `partial` (batch with failed
or deferred work). `phase` distinguishes entity and relation model calls.
Attempt counts, timestamps, elapsed milliseconds and numeric output counts
support operations without copying original text, URLs, model responses,
entities or relations. `error_code` is a bounded exception/stage category;
detailed messages remain in the server logs.

A selection with date-gated blocks and retained links is `succeeded`, with
`error_code=date_gate` and the final block/link counts. A selection consisting
only of date-rejected blocks is `rejected`. Neither case is a failed job.

SQLite is still the execution authority. A local durable outbox retries failed
Supabase deliveries without affecting extraction, and revisions prevent a
concurrent update from being acknowledged accidentally. Restart recovery marks
unfinished selection/batch records interrupted and reconciles ontology jobs
against the durable queue. Monitor cleanup never deletes that queue or graphs.
The server batch status also reports `currentJobId` and `currentPhase`.

Both the local journal and the Studio table retain records for seven days
after the last state change. The PostgreSQL `work_cleanup_osbi_jobs` cron runs
every ten minutes. Successful delivery and ordinary restarts do not refresh
historical timestamps, so exporting does not extend retention. Only the
service role and Studio administrator can access the new table.

Apply `server/deploy/job-model-usage.sql` once before deploying usage-aware
monitoring (also apply it after `job-monitor.sql` for a new installation).
`provider` is the provider hostname and `model` is the latest actual response
model, falling back to the requested name when the provider omits it. No
credentials, URL paths, request text or model output are exported.

`prompt_tokens`, `completion_tokens` and `total_tokens` sum known API-reported
counts. `model_usage` records each HTTP attempt, including provider retries,
fallback models and responses rejected by output validation. `api_calls` counts
these HTTP attempts; the existing `model_calls` counts logical invocations.
Unknown usage stays null with `usage_complete=false`; partially known totals
are explicitly incomplete. Historical records are not estimated. A new cache
hit has zero calls and zero tokens. Ontology usage accumulates across job
retries; batch rows aggregate their own calls, so do not add batch totals to
their child jobs when calculating consumption. The seven-day retention applies
to all this metadata as well.

Pure server-cache jobs have `phase=cached`; their existing terminal `status`
is preserved (normally `succeeded`, or `rejected` for a normal date gate).
This requires an actual cache hit and a complete zero-call usage history.
Zero calls alone, empty batches, partial cache hits and jobs with any model
invocation are not marked cached. A batch is cached only when every completed
item came from cache and the whole batch made no model calls. Client-side
submission cache hits do not create new server job records. Historical rows
without explicit cache evidence are not reclassified.
