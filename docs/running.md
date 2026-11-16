# Installation and Operation

The shared server and local client are separate services. Start them from this
checkout or its packaged source distribution; no other development repository is
needed. This release is verified on Linux x86_64 with Python 3.12 and Node 26.
The pinned browser requires Node >=22.15 and the selected DSH release >=24.9.0.
The one-command client installer targets Bash on Linux/macOS x86_64/arm64; native
Windows and macOS execution have not been verified. Linux requires glibc and the
host's Firefox-compatible libraries. Interactive browsing needs a graphical session.

Before deployment, read the [content-storage and intellectual property notice](../README.md#content-storage-intellectual-property-and-responsibility).
The server retains original content blocks and serves evidence within shared
channels; model processing and backups can also contain source content. Operators
are responsible for applicable authorizations and lawful use, storage and sharing.

## Install

Use the common installer on every client, regardless of agent framework:

```bash
bash scripts/install-client.sh
bash scripts/run-client.sh
```

This installs backend, browser code and pinned browser/model/addon/GeoIP assets.
It reuses Python >=3.12 with venv/ensurepip and Node >=24.9 with npm/npx. If missing,
it downloads pinned CPython 3.12.16 and Node 24.22.3 archives, verifies SHA-256, and
places them under `.osbi/runtime`; `.venv` holds Python packages. No system runtime
or shell profile is changed. An incompatible existing `.venv` is rejected rather
than overwritten. Runtime links in `.osbi/bin` are used by the client/DSH launchers;
the Python browser facade also discovers this Node without PATH activation.
The installer does not start services or install host OS libraries.
The common implementation is in `client/scripts/`; root `scripts/` preserves its
client install/launch entry points. See the [client layout](../client/README.md).

For slow/offline runtime downloads, pre-stage the exact archive basenames from
[the runtime pins](../client/scripts/runtime-pins.sh) in `OSBI_RUNTIME_DOWNLOADS`.
The pins link the upstream Node/Python releases. `OSBI_GITHUB_PROXY` also applies
to Python downloads. Browser assets retain their separate verified cache settings
below. Package installation still needs its Python/npm dependencies available.
Interrupted installs can be retried; if an interruption leaves `.osbi/install.lock`,
verify no installer is running before removing that empty lock directory.

To install OSBI's DSH skills in the same command:

```bash
bash scripts/install-client.sh --agent dsh
# Optional isolated host directory:
bash scripts/install-client.sh --agent dsh --dsh-home /path/to/dsh-home
```

The adapter step installs skills only, preserving host profiles. Use the same
`DSH_HOME` when launching an isolated host. No DSH host is downloaded until
`bash client/scripts/deepseek-harness/run.sh` is explicitly run. Other agent adapters are placeholders.

For server-only installation, or manual client setup with existing runtimes, use
the deployment extras below. The lock file contains tested dependency constraints.

```bash
python3 -m venv .venv
# Server host:
.venv/bin/pip install -c requirements.lock -e '.[server]'
# Client host:
.venv/bin/pip install -c requirements.lock -e '.[client]'
npm ci --ignore-scripts --prefix client/backend/browser
npm run build --prefix client/backend/browser
node client/backend/browser/scripts/install-assets.mjs
```

The browser installer explicitly downloads and verifies pinned assets. Normal
acquisition does not download browsers or reset identities. Do not copy another
product's logged-in profiles. An incompatible profile needs explicit recovery.
Client acquisition attempts GeoIP for both direct and configured proxy connections.
At browser launch the runtime queries the public exit IP over HTTPS, then looks it
up in the pinned local database to set timezone, language, geolocation and WebRTC
IP. Persistent fingerprints and cookies are retained; the auto-selected locale stays stable within
the same country. Missing GeoIP data or failed/incomplete lookup produces a warning
and continues without geography matching. The local notification explains that
exposure risk may increase and acquisition success may decrease.

The installer includes GeoIP automatically. The pinned all-IP database is about
116 MiB. It is not refreshed or downloaded during acquisition. Since upstream can
remove old releases, keep the verified file with deployment packages or in
`OSBI_BROWSER_DOWNLOADS`, named `geoip-aio-all-2026.11.11.mmdb`. The installer checks
its SHA-256 before use and also verifies an already installed database. An
unavailable GeoIP download or invalid checksum produces a warning and lets the
core installation complete. Invalid databases are not used by the runtime. Core
browser/model/addon failures still require repair. `OSBI_BROWSER_WITH_GEOIP` is no longer used.

Advanced browser launch settings live in `<client-state>/browser/config.json`.
For a profile requiring an HTTP(S) proxy, use the managed runtime's per-session
settings, for example:

```json
{"sessions":{"notices":{"proxy":"http://proxy.example:8080"}}}
```

Use the same profile name in the source settings. The browser and public-IP probe
use that proxy; installation download routing is independent. Keep credentials in
the private configuration file. Acquisition explicitly enables GeoIP even if an
old browser config disabled it. After upgrading, finish active work and stop only
the owned idle browser sessions before restarting them with the new options; do
not reset profiles or discard cookies. Geography is resolved at launch; use a
stable exit for the duration of a session.

Server schemas and dashboard assets are package data. Browser source, lock and
build scripts must accompany client source distributions. A standalone Python
wheel by itself does not include the Node/browser installation.

## Configure and start the shared server

Use [the environment template](../server/deploy/server.env.example), filling in your
model provider endpoint/key and supported model names. Do not commit the resulting
environment file. Ingestion/extraction models run on the server; optional Jev
decisions and all downstream task processing belong to the local client.
English system prompts can be overridden through `config.py`/`PromptSettings`,
or a partial JSON file selected by `OSBI_PROMPTS_FILE`. See
[server prompt configuration](../server/README.md#system-prompts) for stage keys,
examples, restart behavior and explicit historical reprocessing.

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
[the Nginx example](../server/deploy/nginx.conf.example) trusts only a local upstream.
An optional [systemd unit](../server/deploy/osbi-server.service.example) illustrates
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
.venv/bin/python client/scripts/deepseek-harness/install.py
bash client/scripts/deepseek-harness/run.sh
```

The launcher pins DSH `0.2.2-rc.3`, adds this environment's CLI/MCP executables to
PATH, and applies [the MCP overlay](../client/scripts/deepseek-harness/cordis.patch.yml)
without rewriting existing host profiles. Configure the host's own conversational
model separately; its key is different from the OSBI customer and server-model keys.
Use `--home` on the skill installer and `DSH_HOME` for an isolated DSH environment.

Management/query skills use the same local operations as the dashboard. MCP
discovery/probing never starts a daemon. Configure the host model with its own
credentials; task execution uses the already running local backend.

The backend and browser are agent-independent: all hosts on the same machine can
connect to one backend/state directory. Skill business instructions are reusable,
but skill discovery directories, MCP registration and launcher configuration need
host-specific adapters. DSH is implemented; Claude Code, Codex, WorkBuddy and Pi
directories contain README placeholders. A separate backend installation per
framework is unnecessary. Keep every host pointed at the intended
`OSBI_CLIENT_DATA`/`OSBI_CLIENT_URL` if using a nondefault backend.

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

On-demand action-hotspot selection, private cross-week coverage history and Word
report backfill run in the local backend, as described in [local downstream tasks](downstream-tasks.md). They do
not enable report scheduling or outgoing delivery.

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

After extraction-rule updates with an unchanged schema, use
`osbi-server reprocess --channel policy --force` explicitly. Existing completed
graphs are not silently rebuilt on application upgrades.

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

The opt-in browser check requires installed pinned browser assets and uses a fresh
profile and local dynamic page. GeoIP absence/failure is a warning and does not
prevent the page check. Successful matching also needs the database and public-IP access.
It belongs in the intended integration environment. Tests use fake model responses and isolated state; they
are not live model quality/latency measurements. `tests/demo_harness.py` starts a
separate TLS fixture with a simulated model for local visual/integration checking.
It must not be used as the customer service.

For isolated browser logic checks after building the runtime, use
`npm test --prefix client/backend/browser`. These Node module-mock tests replace network
lookups, databases and browser launches with fixtures. They require Node's
`--experimental-test-module-mocks` support and do not install assets or start a
real browser. DSH is not installed by either the client installer or these tests;
only the optional DSH launcher downloads its host dependency tree.

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
