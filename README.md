# OSBI-NET

English | [Simplified Chinese](README.zh-CN.md)

Open-Source Business Intelligence Network

OSBI-Net connects independently deployed acquisition clients to a shared
intelligence server. Clients collect source material; the server extracts entities
and relationships, retains original evidence, and serves channel-based queries.

> In November 2024, Jiyue's sudden collapse left countless suppliers facing losses. Had they noticed three months earlier that Jiyue had abruptly tripled its downstream order volume without a substantial increase in sales, they might have recognized the risk.
> This information is neither secret nor inaccessible. On the contrary, it is publicly available; extracting useful signals from it is the difficult part, like finding a few flecks of gold on a vast beach.

## How It Works

```text
Sources -> local client (browse, clean, chunk) -> HTTPS -> shared server
                                                        | relevance and extraction
                                                        | schemas and graph storage
Local dashboard / agent <- local backend <- HTTPS queries and original evidence
```

| Component | Responsibilities | Persistent data |
|---|---|---|
| Shared server | Relevance, automatic channel routing, ontology extraction, schemas, graphs and queries | Original content blocks and source metadata, graph data, jobs and schema revisions |
| Local client backend | Interactive Camoufox, programmable source workflows, acquisition intervals, CLI/MCP, dashboard and downstream tasks | Settings, browser profiles, collected material, outbox, task events, selected evidence and local report coverage history |
| Agent | Management and evidence-based queries through skills and MCP | Managed by the chosen agent host |

Ontology extraction and graph storage run on the server. Clients do not maintain
a persistent graph replica; the dashboard loads query results into page memory.
The backend runs independently of agent sessions and browser dashboard tabs.

Deploy client and server on separate hosts, using independent checkouts,
environments and state directories. Each host installs its role's Python extra:
`.[server]` for extraction/storage/query service, `.[client]` for local acquisition
and consumption. Both include the shared `contracts/` library. Server-only setup
requires no Node, browser or agent host; client-only setup requires no server
graph-store/extraction libraries. Communication uses HTTPS, without shared database
files. This release uses one source repository with role-specific dependencies.

The server starts with `policy` and `economy`, each using its domain schema.
Submissions are classified on the server; new channels bind a fixed generic
schema. Entity types and relation predicates are never added automatically.
Schema calibration requires evidence review and explicit administrator activation.
**Data is shared among authorized users of the same channel.** Weekly coverage
history is stored by each local backend and scoped by server/customer, channel and report profile.

For downstream tasks the server provides retrieval, similar to the retrieval and
storage layer in RAG. Weekly reports, TopNews, question answering, evidence
selection and optional Jev decisions belong to the local backend or agent host.
Clients request evidence as needed; the server does not generate those outputs.
Clients sharing a channel can serve different departments: each local backend
stores its own report profiles, interests, TopNews boards and Weeklyreport boards.
These settings guide local evidence selection and writing, independently of the
shared channel schema. See [local report profiles](docs/downstream-tasks.md#local-report-profiles).

## Content Storage, Intellectual Property and Responsibility

> **Before deployment:** the server stores original content blocks, not just
> summaries or extracted entities. Original evidence can be returned to authorized
> users in the same channel. Server state and backups therefore contain source
> content. Source content is also sent to the configured model endpoint for
> relevance assessment and extraction.

Before collecting, uploading, storing, processing or sharing content, users must
review and comply with intellectual property laws and regulations in their
location and all other applicable jurisdictions. Users must obtain any necessary
rights-holder authorization and comply with applicable source terms and license
conditions. Making this code available does not grant rights to third-party content.

**Project statement:** we make this code available to the open-source community
for technical sharing and exchange. We do not participate in or operate any
user's deployments, business or other operational activities. All users must
ensure that their use does not infringe anyone's intellectual property rights.
Users are responsible for their own use and operations and bear the resulting
infringement disputes, losses, legal liabilities and other consequences, as
determined by applicable law.

Deployment operators should account for source-content storage when choosing
server/model locations and configuring access, retention, deletion and backups.
This notice supplements the existing [LICENSE](LICENSE); it does not replace its
terms or exclude responsibilities that applicable law does not permit to be excluded.

## Quick Start

The following commands assume Bash on Linux and run from the repository root on
each respective host. The server requires Python 3.12+. The client installer
reuses suitable Python/Node runtimes or provisions verified copies in the checkout;
it needs Bash, curl, tar and sha256sum (or shasum), without sudo.
The verified environment is Linux x86_64, Python 3.12.3 and Node.js 26.8.2.
Native Windows/macOS installation has not been verified.

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
```

### 1. Deploy the Shared Server

On the server host, install the server dependencies and configure an
OpenAI-compatible model endpoint with models available from your provider:

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
export OSBI_LLM_BASE_URL=https://your-model-provider.example/v1
export OSBI_LLM_API_KEY=YOUR_PROVIDER_KEY
export OSBI_SELECTED_MODEL=YOUR_SELECTION_MODEL
export OSBI_INFO_MODEL=YOUR_INFO_MODEL
export OSBI_ONTOLOGY_MODEL=YOUR_EXTRACTION_MODEL
export OSBI_FALLBACK_MODEL=YOUR_FALLBACK_MODEL
.venv/bin/osbi-server serve
```

The default listener is `127.0.0.1:8780`. Configure an HTTPS reverse proxy using
the [Nginx example](server/deploy/nginx.conf.example), or use
`serve --ssl-certfile CERT --ssl-keyfile KEY` for direct TLS. Business APIs require
HTTPS and a valid customer key in the `Authorization: Bearer ...` header.

In another terminal on the server host, issue a key for a client customer:

```bash
.venv/bin/osbi-server issue-key --customer customer-demo --days 30
.venv/bin/osbi-server list-keys
```

Issuance prints the plaintext key once. Keys default to all channels, including
new ones; add repeated `--channel policy --channel economy` options to restrict
access. Restricted keys cannot create channels outside their grants. Renewal and
revocation use `renew-key KEY_ID --days 30` and `revoke-key KEY_ID`.

The server host does not need DSH or browser assets. See the
[environment template](server/deploy/server.env.example),
[systemd example](server/deploy/osbi-server.service.example) and
[full operating instructions](docs/running.md) for service deployment.

### 2. Start a Local Client

On each client host, install the backend and managed browser:

```bash
bash scripts/install-client.sh
bash scripts/run-client.sh
```

One script installs the backend, browser code and checksum-verified Camoufox,
fingerprint, addon and GeoIP assets. It creates `.venv`, reuses Python 3.12+ and
Node 24.9+ when available, or installs pinned runtimes under `.osbi/runtime`.
It does not alter system runtimes or shell profiles. Linux browser libraries and
a graphical session for interactive acquisition must be supplied by the host.
Use `bash scripts/install-client.sh --agent dsh` to also install both OSBI skills;
the DSH host itself remains an optional download on first launch.
Client acquisition attempts GeoIP to match browser timezone, language, location
and WebRTC IP to the direct or configured proxy exit. Installation tries to prepare
the pinned database; startup checks the public exit IP and looks it up locally.
Missing or failed GeoIP produces a warning that exposure risk may increase and
acquisition success may decrease; the browser continues without GeoIP matching.
Existing profile fingerprints and login state are retained.

Open **http://127.0.0.1:8781/settings** and configure the server's HTTPS URL and
customer key. Add a web or RSS source and save it before running acquisition.
Sources support an optional monitoring interval and typed browser interaction
steps for reaching the actual result page.

- **Graph** (`/graph`): switch channels, search entities and inspect relationships
  with their original evidence.
- **Settings** (`/settings`): configure server access, sources, workflows, local
  acquisition preferences and per-department report profiles/boards.
- **Notifications** (`/notifications`): follow task events and acknowledge pending
  manual login/verification steps.

For CLI use, keep the backend running:

```bash
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

`source-1` is the ID saved in settings. The browser is visible by default for
manual interaction. Headless mode is configurable; manual verification still
requires a suitable graphical session. See [source workflows](docs/running.md#programmable-source-workflow).

### 3. Optional DeepSeek Harness Integration

With the local backend already running, install the management/query skills and
launch DSH in another terminal:

```bash
.venv/bin/python client/scripts/deepseek-harness/install.py
bash client/scripts/deepseek-harness/run.sh
```

The launcher pins DSH `0.2.2-rc.3` and adds the MCP overlay without rewriting host
profiles. Its first run downloads DSH and its dependency tree into the npm cache;
DSH is optional for dashboard/CLI use. Configure the agent host's conversational
model separately. Its credentials, the server model-provider credentials and the
OSBI customer key serve different purposes. See [DSH setup](docs/running.md#dsh).

Backend, browser, state and CLI/MCP are shared across agent frameworks. Skill
instructions are reusable; skill discovery paths, MCP registration and launch
configuration need host-specific adapters. DSH is implemented; the other four
platform directories currently contain placeholders. Installing another agent
does not require another backend or browser installation.

## Optional Local Jev Decisions

Jev is disabled by default and configured in the local settings page with its own
base URL, key and model. Enable either or both stages: relevance before submitting
material to the server, and keep/discard decisions after retrieving evidence.
These decisions and credentials stay on the client side. When enabled, decision
context is sent to the configured Jev endpoint. Low confidence, oversized inputs
or unavailable decisions keep the evidence and continue; errors appear as local
warnings. See [local downstream tasks](docs/downstream-tasks.md).

## Deployment and Maintenance

- **Network:** expose the shared server through HTTPS with a valid certificate.
  Keep the dashboard on client loopback. Local dashboard HTTP and remote server
  HTTPS have different roles; do not expose the local control API publicly.
- **State and ownership:** defaults are `~/.osbi/server` and `~/.osbi/client`.
  Override them with `OSBI_SERVER_DATA` / `OSBI_CLIENT_DATA` or global `--data`.
  Run one server process per server state directory; multiple clients connect over
  HTTPS. Do not use competing worker processes against the same embedded graph store.
- **Credentials and content:** protect provider keys, customer keys, browser
  profiles, original material and backups. Configure `serverCA` for a private CA;
  clients verify certificates. Apply the content-storage notice above before intake.
- **Processing window:** graph extraction defaults to continuous background work.
  Set the following environment variables and restart the server for a common
  batch window; material intake and queries remain available while graph work waits.

```dotenv
OSBI_GRAPH_PROCESSING_MODE=batch
OSBI_GRAPH_BATCH_WINDOW=22:00-03:00
OSBI_GRAPH_BATCH_TIMEZONE=Asia/Shanghai
```

- **Schema and rule updates:** use `schema-review`, `schema-activate` and explicit
  `reprocess`; use `reprocess --channel policy --force` for rule changes without a
  schema change. Upgrades do not silently rebuild historical data. See
  [schema calibration](docs/running.md#manual-schema-calibration).
- **Backup:** stop the service before taking a consistent snapshot. The server's
  `backup --out DIRECTORY` and `restore --from DIRECTORY` commands verify file
  hashes; restore into a fresh state directory. Protect complete client state as
  well. See [backup instructions](docs/running.md#verification-and-state-backup).
- **Local report preparation:** configurable TopNews/Weeklyreport boards, generic
  evidence or action-hotspot selection, private cross-week coverage history and
  Word-heading backfill are available through CLI/MCP/API.
  Record coverage only after a report succeeds. Selection and coverage records run in the local backend after generic server
  evidence queries. Full report generation, scheduling and outgoing delivery are
  not enabled. See [local downstream tasks](docs/downstream-tasks.md).

## Directory Structure

```
osbi-net/
├── server/                # Extraction, schemas, graph ownership and shared APIs
│   └── deploy/            # Server environment, HTTPS proxy and service templates
├── client/                # Local backend, browser, dashboard and agent skills
│   ├── backend/           # Python service and managed browser package
│   ├── skills/            # Shared management and query instructions
│   └── scripts/           # Agent host setup, launch and MCP configuration
├── contracts/             # Common library installed separately on both hosts
├── scripts/               # Stable client installation and launch entry points
├── tests/                 # Isolated regression and opt-in acceptance checks
└── docs/                  # Public usage and operations
```

## Supported Capabilities and Limits

The available integration is DeepSeek Harness, with a persistent backend, local
CLI/MCP and graph/settings/notifications pages. Other agent folders document their
current unsupported status. Acquisition is text-focused; hunters, scheduled report
generation/delivery, intelligent curation, OCR/transcription and complex document
parsing are not implemented. Jev is an optional local decision provider.

## Documentation

- [Client usage and file structure](client/README.md)
- [Server deployment and file structure](server/README.md)
- [Shared contracts library](contracts/README.md)
- [Installation and operation](docs/running.md)
- [Local downstream tasks and optional Jev](docs/downstream-tasks.md)

## License

Use is subject to the existing [LICENSE](LICENSE), including its additional
conditions and prohibited uses. See [third-party notices](THIRD_PARTY_NOTICES.md)
for bundled dependencies and assets. The code license does not license source
content collected by deployments.

## Citation

If you reference or use any part of this project in your work, please include the following attribution:

```
Author: Wiseflow Team
https://github.com/TeamWiseFlow/osbi-net
```

## Related Links

[<img src="https://github.com/TeamWiseFlow/wiseflow/raw/4.x/docs/logos/SiliconFlow.png" alt="siliconflow" width="360">](https://cloud.siliconflow.cn/i/WNLYbBpi)
