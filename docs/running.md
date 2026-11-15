# Running the Demo

The shared server and local client are separate services. Start them from this
checkout or its packaged source distribution; no other development repository is
needed. This release is verified on Linux x86_64 with Python 3.12 and Node 26.
The pinned browser requires Node >=22.15 and the selected DSH release >=24.9.0.
Native Windows/macOS installation has not been verified.

## Install

Create the environment and install the needed deployment extras. The lock file
contains tested dependency constraints; optional extras select only the packages
required by each deployment.

```bash
python3 -m venv .venv
# Server host:
.venv/bin/pip install -c requirements.lock -e '.[server]'
# Client host:
.venv/bin/pip install -c requirements.lock -e '.[client]'
npm ci --ignore-scripts --prefix client/browser
npm run build --prefix client/browser
node client/browser/scripts/install-assets.mjs
```

The browser installer explicitly downloads and verifies pinned assets. Normal
acquisition does not download browsers or reset identities. Do not copy another
product's logged-in profiles. An incompatible profile needs explicit recovery.
The client uses browsing without GeoIP lookup. GeoIP installation is optional
(`OSBI_BROWSER_WITH_GEOIP=1`); that path also requires the pinned data file or a
verified local download cache if the upstream no longer retains its release.

Server schemas and dashboard assets are package data. Browser source, lock and
build scripts must accompany client source distributions. A standalone Python
wheel by itself does not include the Node/browser installation.

## Configure and start the shared server

Use [the environment template](../deploy/server.env.example), filling in your
model provider endpoint/key and supported model names. Do not commit the resulting
environment file. All model stages run on the server.

```bash
export OSBI_LLM_BASE_URL=https://your-model-provider.example/v1
export OSBI_LLM_API_KEY=YOUR_PROVIDER_KEY
export OSBI_SELECTED_MODEL=YOUR_SELECTION_MODEL
export OSBI_INFO_MODEL=YOUR_INFO_MODEL
export OSBI_ONTOLOGY_MODEL=YOUR_EXTRACTION_MODEL
export OSBI_FALLBACK_MODEL=YOUR_FALLBACK_MODEL
.venv/bin/osbi-server serve
```

Default state is `~/.osbi/server`; use `OSBI_SERVER_DATA` or the global `--data`
option to choose another directory. One service owns a state directory. Initial
startup enables policy and economy with their fixed domain schemas; new channels
bind a fixed generic schema. Missing model credentials cause explicit work errors,
not synthetic graph results.

The server binds to loopback port 8780. Put an HTTPS reverse proxy in front of it;
[the Nginx example](../deploy/nginx.conf.example) trusts only a local upstream.
An optional [systemd unit](../deploy/osbi-server.service.example) illustrates
supervision; replace install locations/user and keep the environment file private.
The app trusts forwarded scheme information only from configured loopback proxies.

For a direct TLS listener, supply `serve --ssl-certfile CERT --ssl-keyfile KEY`.
Clients never use an insecure HTTP fallback or disable certificate verification.
The public `healthz` endpoint contains only status; business APIs require HTTPS
and a valid header key.

Issue a customer key locally:

```bash
.venv/bin/osbi-server issue-key --customer customer-demo --days 30
.venv/bin/osbi-server list-keys
.venv/bin/osbi-server renew-key KEY_ID --days 30
.venv/bin/osbi-server revoke-key KEY_ID
```

Issuance prints the new plaintext once. Normal listing contains metadata only.
Keys have all shared-channel access by default, including new channels. Repeated
`--channel policy --channel economy` restricts a key and disallows automatic
creation outside those grants. Rotation means issue a new key, configure the
client, and explicitly revoke the old one. Renewal does not undo revocation.

## Start the local client and dashboard

```bash
.venv/bin/osbi-client serve
```

Open `http://127.0.0.1:8781/settings`. Set the HTTPS server URL and customer key,
then add a source. Use the graph page to browse channel data and notifications to
follow work and human-verification prompts. All static assets are local. Local
dashboard HTTP stays on loopback; outbound server requests always use HTTPS.

Default client state is `~/.osbi/client`. `OSBI_CLIENT_DATA` or global `--data`
selects another independent client. `serve --port PORT` changes the listener;
CLI/MCP callers must set `OSBI_CLIENT_URL` to the same local address.

For a private CA, configure `serverCA` to the local PEM path. Public certificate
chains need no override. Settings readback masks keys. Secrets are in a private
local SQLite file; preserve that directory to retain settings, browser identities,
pending submissions and event cursors.

The default browser is visible for interaction on a desktop session. On a host
without a display, select headless mode in settings. Manual verification in that
mode requires a suitable graphical client/session; there is no automated CAPTCHA
solver or remote desktop feature in this release.

CLI settings updates read JSON from stdin, keeping secrets out of arguments:

```bash
.venv/bin/osbi-client config
.venv/bin/osbi-client configure < private-config.json
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client resume RUN_ID --prompt-id PROMPT_ID
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

Delete or protect a temporary secret configuration file after use; the dashboard
is a convenient alternative that does not require one. Existing outbox records
remain bound to their original server URL and are blocked on a different URL.
Saving corrected credentials resumes blocked submissions to the original server.

## Programmable source workflow

Sources support `web` and `rss`, a profile, an optional monitoring interval in
minutes, and typed steps. Zero interval means manual execution. For example:

```json
{
  "id": "notices",
  "url": "https://example.com/",
  "kind": "web",
  "enabled": true,
  "intervalMinutes": 60,
  "profile": "notices",
  "steps": [
    {"action": "click", "selector": "a.notices"},
    {"action": "wait", "selector": "#results"},
    {"action": "capture"}
  ]
}
```

The current result tab is captured; the URL is not reopened. `wait_for_user`
persists the next step and protects the session. A resumed run does not repeat
the initial navigation. Unknown outcomes require inspecting the current browser
before resumption. Multiple runs use independent profiles or wait for profile
ownership, with global/domain concurrency and verification cooldown.

This first acquisition path is text-focused. It cleans and groups DOM text/tables
and follows server-selected page links. OCR/transcription, complex-table decoding,
downloaded document parsing, hunters, scheduled reports and outgoing delivery are
not implemented. Incomplete/unstable pages are not accepted as complete captures.

## DSH

Start the local backend first. Install only the two skill bundles:

```bash
.venv/bin/python scripts/install-dsh.py
bash scripts/run-dsh.sh
```

The launcher pins DSH `0.2.2-rc.3`, adds this environment's CLI/MCP executables to
PATH, and applies [the MCP overlay](../client/deepseek-harness/cordis.patch.yml)
without rewriting existing host profiles. Configure the host's own conversational
model separately; its key is different from the OSBI customer and server-model keys.
Use `--home` on the skill installer and `DSH_HOME` for an isolated DSH environment.

Management/query skills use the same local operations as the dashboard. MCP
discovery/probing never starts a daemon. Real stdio discovery and DSH configuration
composition are checked; a provider-backed conversational acceptance still needs
the chosen host model credentials.

## Continuous and batch processing

```dotenv
OSBI_GRAPH_PROCESSING_MODE=batch
OSBI_GRAPH_BATCH_WINDOW=22:00-03:00
OSBI_GRAPH_BATCH_TIMEZONE=Asia/Shanghai
```

Restart the server to apply environment changes. Default mode is `continuous`.
Batch mode admits graph work only in the common window, including cross-midnight
windows. Material intake and queries stay available; waiting is shown separately
from failure. Durable jobs continue on restart. No client report scheduler is
needed for this setting.

## Manual schema calibration

No ingestion path can add schema classes or predicates. Review retained original
evidence, usage counts and rejected assertion samples before changing vocabulary:

```bash
.venv/bin/osbi-server schema-review --channel economy
.venv/bin/osbi-server schema-activate --channel economy \
  --file reviewed-economy.ttl --expected-revision CURRENT_SCHEMA_ID
.venv/bin/osbi-server reprocess --channel economy
```

These are simple explicit administrator operations, not the deferred intelligent
curation suite. Validate a proposed definition on an isolated evidence sample
first. Activation validates Turtle, labels and domain/range references, records an
immutable revision, and checks the expected active revision. It does not silently
overwrite old graphs. Reprocessing builds replacements from originals; failures
leave the last completed graph active. To roll back, activate the preserved old
schema file against the current revision and explicitly reprocess again.

For a new generic template, provide a reviewed `generic.ttl` through
`OSBI_SCHEMA_DIR` along with the required domain definitions on server startup.
This affects newly created channels; existing channels retain their explicit
revision bindings until individually activated. Startup does not undo a manually
activated domain revision.

## Verification and state backup

```bash
.venv/bin/pip install -c requirements.lock -e '.[server,client,test]'
.venv/bin/pytest -q
OSBI_BROWSER_E2E=1 .venv/bin/pytest -q tests/test_browser_e2e.py
```

The opt-in browser check requires installed pinned assets and uses a fresh profile
and local dynamic page. Tests use fake model responses and isolated state; they
are not live model quality/latency measurements. `tests/demo_harness.py` starts a
separate TLS fixture with a simulated model for local visual/integration checking.
It must not be used as the customer service.

Before backup, stop the client/server gracefully and archive their complete state
directories, including SQLite files, graph stores, schema revisions and browser
state. Restore into a fresh directory and verify ownership before startup. Do not
copy only a live SQLite main file or an open graph directory. This initial release
uses offline consistent backups; online coordinated backup automation is pending.

The server provides a stopped-service snapshot/restore helper with file hashes:

```bash
.venv/bin/osbi-server backup --out /secure-backups/osbi-snapshot
.venv/bin/osbi-server --data /new-server-state restore --from /secure-backups/osbi-snapshot
```

Backup refuses an actively owned server directory; restore requires a new target
and verifies the manifest before copying. Keep snapshots private and outside the
source checkout. Client state should be archived separately after stopping it.
