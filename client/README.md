# OSBI Client

The client runs a persistent local backend with an interactive browser, dashboard,
CLI and MCP interface. Agent frameworks connect to that backend through shared
management/query skills and host-specific setup scripts. Install the backend and
browser once per client deployment, regardless of how many agent hosts use it.

The server owns extraction algorithms, schemas and authoritative graph storage.
The client collects, cleans and chunks original material, selects relevant content,
assigns a channel, submits it over HTTPS,
then queries evidence as needed for local reports and answers. Clients do not
maintain a persistent graph replica. Read the project's
[content-storage and responsibility notice](../README.md#content-storage-intellectual-property-and-responsibility)
before collecting or uploading content.

Configure `defaultChannel` and optionally `sources[].channel` using channel IDs
returned by the server. Every material/block must declare a channel under
`osbi-material/v2`; per-block assignments override a material default. The server
does not infer missing channels or filter submitted links. Only submit content
and follow-up links the client intends to process. See
[channel assignments and private ontologies](../docs/channel-ontologies.md).

## File Structure

```text
client/
├── README.md
├── backend/
│   ├── osbi_client/                # Python service, CLI/MCP and local task logic
│   │   └── static/                 # Dashboard pages and local assets
│   └── browser/                    # Independent Node/TypeScript browser package
│       ├── package.json
│       ├── assets.lock.json        # Browser, model, addon and GeoIP pins
│       ├── src/                    # Managed browser and interaction runtime
│       └── scripts/                # Browser build and asset installation
├── skills/                         # Shared business instructions
│   ├── osbi-manage/SKILL.md
│   └── osbi-query/SKILL.md
└── scripts/                        # Client setup and agent host adapters
    ├── install.sh                  # Common backend and browser installation
    ├── run.sh                      # Backend launcher
    ├── runtime.sh                  # Python/Node setup helpers
    ├── runtime-pins.sh              # Verified runtime versions and checksums
    ├── backfill-weekly-history.py   # Local historical report import
    ├── deepseek-harness/
    │   ├── README.md
    │   ├── install.py              # Install the shared skills into DSH
    │   ├── run.sh                  # Launch the pinned host with OSBI MCP
    │   └── cordis.patch.yml        # Host MCP configuration overlay
    ├── claude-code/README.md
    ├── codex/README.md
    ├── workbuddy/README.md
    └── pi/README.md
```

The repository's top-level `scripts/` preserves `install-client.sh` and
`run-client.sh` as small entry points to `client/scripts/install.sh` and `run.sh`.
Client setup, runtime helpers and report-history import are owned by this directory.
Agent-specific installation, launch and MCP configuration live together under
`client/scripts/<host>/`. Browser build scripts remain inside its Node package.
The browser is part of the backend's acquisition capabilities and retains its own
dependencies, build and asset locks.

## Separate Deployment

Use an independent checkout and environment on each client host. From the
repository root, the common installer installs the `client` Python extra. It
requires the shared `contracts/` library and root package metadata/constraints;
it does not import server implementation code or install graph-store/extraction
dependencies. The shared server runs on its own host and is accessed over HTTPS.
Server deployment templates are in `server/deploy/`; they are not client setup.
This release uses role-specific dependency extras in one source repository.

## Install and Start

Run these commands from the repository root:

```bash
bash scripts/install-client.sh
bash scripts/run-client.sh
```

The installer sets up Python dependencies, builds the browser package and installs
checksum-verified browser, fingerprint-model, addon and GeoIP resources. It reuses
suitable Python/Node runtimes or provisions pinned copies under `.osbi/runtime`;
Python dependencies live in `.venv`. Host browser libraries and a graphical session
for interactive browsing remain prerequisites. See
[installation and operation](../docs/running.md) for platform requirements, download
caches and proxy settings. GeoIP failure produces a warning and permits acquisition
to continue.

Open **http://127.0.0.1:8781/settings**, enter the shared server's HTTPS URL and
customer key, then configure sources and save. The dashboard also provides
`/graph` for evidence-backed graph browsing and `/notifications` for task events
and waiting login/verification steps. Keep the backend running independently of
dashboard tabs and agent sessions; it listens on loopback.

```bash
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

`source-1` must be a configured source ID. The launcher defaults to `serve` and
also forwards explicit CLI arguments, such as `bash scripts/run-client.sh serve
--port 8782`. Use the same `OSBI_CLIENT_DATA` and `OSBI_CLIENT_URL` in CLI/agent
processes when selecting a nondefault backend.

## Agent Frameworks

| Host | Adapter | Status |
|---|---|---|
| DeepSeek Harness | [deepseek-harness](scripts/deepseek-harness/README.md) | Management/query skills and MCP integration available |
| Claude Code | [claude-code](scripts/claude-code/README.md) | Not implemented |
| Codex | [codex](scripts/codex/README.md) | Not implemented |
| WorkBuddy | [workbuddy](scripts/workbuddy/README.md) | Not implemented |
| Pi | [pi](scripts/pi/README.md) | Not implemented |

For DSH, add `--agent dsh` to the common installer to install the two OSBI skills
alongside the backend/browser. To add skills to an existing installation:

```bash
.venv/bin/python client/scripts/deepseek-harness/install.py
bash client/scripts/deepseek-harness/run.sh
```

Start the local backend before launching the host. The skill installer preserves
host profiles and accepts `--home PATH` (default `DSH_HOME` or `~/.dsh`). Use the
same `DSH_HOME` for the launcher. The pinned DSH host and dependencies download
through npm on the first explicit launch; neither the common installer nor the
skill installer downloads DSH itself. Configure the host's conversational model
separately from server extraction models and the OSBI customer key.

Skills in `skills/` contain shared instructions. Host adapters choose where to
install those skills, register the common `osbi-mcp` executable and provide launch
configuration. They use the existing backend and browser installation.

## Local Preferences and State

Sources, programmable browser workflows, optional Jev settings and personalized
report profiles are edited through the settings page, CLI or management skill.
Each report profile defines its own interests and separate TopNews/Weeklyreport
boards. Different departments can share a server/channel while keeping these
preferences and weekly coverage histories independent on their clients.

The query skill consumes server evidence and prepares/writes outputs locally.
Selection tools provide evidence and writing briefs; complete automatic document
generation, report schedules and outgoing delivery are not enabled. See
[local downstream tasks](../docs/downstream-tasks.md) for board configuration,
selection, coverage recording, historical backfill and optional Jev decisions.

Default persistent state is `~/.osbi/client`, including configuration, browser
profiles, acquisition queues, events and local report history. The source directory
layout does not change this state location. If `OSBI_BROWSER_ROOT` was set
explicitly to the former browser source location, update it to
`client/backend/browser`; default path discovery already uses the new location.
Protect customer/model keys, original content and logged-in browser profiles.

## Capture and Submission Caches

The backend shares successful page acquisitions across tasks for **six hours** by
default. A hit supplies the saved material without starting the browser; it does
not change the original capture time or extend the expiration. Browser profiles,
workflow steps and acquisition modes have separate cache identities. Redirect and
canonical aliases participate in task-local cycle prevention. RSS discovery also
uses the acquisition cache.

Configure defaults and domain overrides in the settings page, or send a partial
configuration update through `osbi-client configure` / the management skill:

```json
{
  "captureCache": {
    "defaultTtlSeconds": 21600,
    "domainTtlSeconds": {"example.org": 3600, "news.example.org": 300},
    "maxEntries": 1000,
    "maxBytes": 134217728
  },
  "submissionCache": {
    "ttlSeconds": 604800,
    "maxEntries": 10000,
    "historyDays": 30
  }
}
```

Domain keys are hostnames without schemes, ports or wildcard prefixes. A rule
also covers subdomains; the most specific match wins. TTL values are seconds;
zero disables that cache. Failed, blocked or incomplete captures are not saved
as successful material. Size/count limits evict older acquisitions. A shorter
domain policy also limits redirect targets.

```bash
.venv/bin/osbi-client run source-1 --refresh
```

`--refresh` bypasses acquisition caches for that run. The local run API accepts
`forceRefresh: true`, and MCP `start_source` accepts `force_refresh=True`.
It does not force ontology re-extraction; use the server's explicit reprocess
command for that purpose.

Submission reuse is independent of acquisition reuse. The backend computes a
versioned material SHA-256 and checks the server's authenticated processing
revision. Its local completion cache is scoped to the server, credential,
material/channel assignments, source and processing revision. Changes to permissions, schemas,
model endpoint/models or prompts can require a new server request while reusing
the captured page. If a compatible revision cannot be obtained, a normal server
submission remains possible.
Identical content from a new source is submitted once to register its provenance;
the server can reuse the existing work and graph without repeating model calls.

Client-selected links acknowledged by the server still enter the new task's URL queue, with
the new task's cycle guard. Pending material attaches to the existing local
submission/job; failures remain retryable. Expired server jobs are resubmitted
from the retained outbox material. The backend prunes expired/over-capacity caches
every five minutes and removes terminal run/outbox history after `historyDays`.
Active outbox entries are preserved.
