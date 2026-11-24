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
