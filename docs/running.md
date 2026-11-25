# Installation and Operation

The current server runtime is `wiseflow_server`, protocol `material-extract/v4`.
Client and server run on independent hosts/checkouts and use separate state.
Use the complete client manuals: [English](../client/README.md) ·
[简体中文](../client/README.zh-CN.md).

## Client Installation

From the repository root:

```bash
bash client/scripts/install.sh
bash client/scripts/run.sh
```

The common implementation is under `client/scripts/`. It installs `.[client]`,
builds the Node browser package, and explicitly installs verified assets. It
reuses suitable Python 3.12+ and Node 24.9+ runtimes, or provisions pinned copies
under `.osbi/runtime`. `.venv` holds Python packages. The installer installs no
host browser libraries or local model service. A visible browser needs a graphical
session. Linux x86_64 is verified; the Bash installer includes other
Linux/macOS targets, but native macOS/Windows operation has not been verified.

The installer does not start a background service unless `--systemd` is passed;
that option writes a `systemd --user` unit for this checkout and starts it.
Existing units must use `ExecStart=/bin/bash <checkout>/client/scripts/run.sh serve`
and be restarted after an upgrade so the checkout's code is loaded. Because `mp`
capture shells out to a WeChat container, `--systemd` also resolves that container
name (`--wechat-container auto|NAME|none`) and writes
`Environment=WECHAT_LINUX_CONTAINER_CONTAINER=<name>`; deployments whose container
is named `mimicwx-linux` therefore keep working after an upgrade.

Runtime versions/checksums are in
[`runtime-pins.sh`](../client/scripts/runtime-pins.sh). Pre-stage exact archive
basenames under `OSBI_RUNTIME_DOWNLOADS` for slow/offline runtime downloads.
`OSBI_GITHUB_PROXY` can route supported Python archive downloads. Browser assets
use `OSBI_BROWSER_DOWNLOADS` and their own pins in
[`assets.lock.json`](../client/backend/browser/assets.lock.json).
Python/npm packages still need to be available. GeoIP provisioning is best effort;
core browser/model/addon integrity failures require repair. Keep the verified
assets with deployment packages when upstream downloads are unavailable.

For manual setup with existing runtimes:

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[client]'
npm ci --ignore-scripts --prefix client/backend/browser
npm run build --prefix client/backend/browser
node client/backend/browser/scripts/install-assets.mjs
bash client/scripts/run.sh
```

Interrupted installation can be retried. If `.osbi/install.lock` remains, verify
that no installer is running before removing that empty lock directory.

## Client Configuration and State

Open **http://127.0.0.1:8781/settings**. The dashboard defaults to English and
supports Chinese through the header selector. This preference stays in browser
storage; source/report configuration stays in the backend's SQLite state.

Configure server HTTPS access and sources before collection. Sources support
multiple daily capture times with an IANA timezone and an optional interval
fallback. Next timestamps are persisted in the client; daily times override the
interval. Source schedules are separate from report-generation schedules.
See [source capture schedules](../client/README.md#source-capture-schedules).

`OSBI_CLIENT_DATA` defaults to `~/.osbi/client`; `OSBI_CLIENT_URL` defaults to
`http://127.0.0.1:8781`. CLI/agent processes must use the same state/backend.
Select another instance explicitly:

```bash
bash client/scripts/run.sh --data "$HOME/.osbi/other-client" serve --port 8782
```

Keep the local control API on loopback. A remote dashboard can use an SSH tunnel;
interactive browser windows still belong to the client host's desktop session.
Browser profiles, original material, reports, ledgers and keys are private state.

## Programmable Source Workflow

Sources can contain typed navigate/click/fill/select/wait/scroll/switch/capture/
wait_for_user steps. The source's browser profile preserves its session. Use the
management skill to create complex workflows, and Notifications to resume or
cancel waiting runs. MP catalog sources use a separately provisioned wechat-linux-container
hunter and exact account names; their article bodies are still captured by the
browser. See the [client manual](../client/README.md#first-configuration).

## DSH

With the backend running:

```bash
.venv/bin/python client/scripts/deepseek-harness/install.py
bash client/scripts/deepseek-harness/run.sh
```

Alternatively install the skills with `bash client/scripts/install.sh --agent dsh`.
The host downloads only on first explicit launch. Configure its conversational
model independently of the client report model and server extraction models.
Other agent adapters are placeholders. See [DSH setup](../client/scripts/deepseek-harness/README.md).

## Shared Server

Install `.[server]` with Python 3.12+ and root dependency constraints, then prepare
a private `runtime.yaml` from `server/config/example/runtime.yaml`. Configure
model credentials, schema/store/seed paths, server channels and node/net
authentication outside the checkout. Paths must resolve from the private config
location. Launch the active runtime:

```bash
export OSBI_CONFIG_FILE=/path/to/private/runtime.yaml
.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
```

Use HTTPS reverse proxying for remote client access and one process per graph
state directory. No browser, Node or agent assets are needed on this host.
The server selects relevant candidates synchronously and queues selected original
blocks for independent ontology batches. It neither routes materials to inferred
channels nor generates local reports. There is no standalone info stage.

See [Supabase-managed v4 deployment](supabase-v4-deployment.md) for authentication,
net isolation, date gates, batch operation and Studio job monitoring, and
[the v4 contract](client-server-contract.md) for current requests/responses.
Older `osbi_server` CLI, backup commands and 8780 examples are retired.

## Verification and State Backup

Use health/readiness and authenticated channel queries for deployment checks;
health does not exercise models. Verify local UI/CLI access, source scheduling and
actual report targets separately when authorized for that deployment.

For a consistent backup, finish active work and stop the owning process before
copying its complete state. Client backups include SQLite, browser profiles,
reports and ledgers. Server backups include every net's SQLite, cache and graph
stores plus separately protected configuration. Restore to an isolated state
location first. A code rollback does not justify overwriting newer business data.
Keep credentials, original evidence and production ontology files out of repositories.
