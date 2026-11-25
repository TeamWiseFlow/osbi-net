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
Sources → local client (capture, clean, assign channels) → HTTPS → shared server
                                                               relevance selection
Local dashboard / CLI / agent ← shared evidence queries ← ontology queue + graphs
                   └→ local TopNews / Weeklyreport → files and optional delivery
```

| Component | Responsibilities |
| --- | --- |
| Shared server | Existing channel statements, relevance selection, publication-date gates, durable ontology backlog, schemas, graphs and evidence queries. |
| Local client | Web/RSS/WeChat acquisition, manual and scheduled captures, dashboard, local caches, report selection/writing, files and delivery. |
| Agent host | Optional management and evidence-based queries through shared skills/MCP. |

Use independent checkouts, environments and state directories for client and
server. Clients choose existing server channels; the server does not infer a
channel or create one from submitted content. Multiple channels can share a
schema/graph. Nodes in the same authorized net share evidence, while local report
profiles and editorial histories remain independent.

The active server is `server/wiseflow_server`, using `material-extract/v4`.
Selection returns synchronously, including links to follow; selected original
blocks enter a durable ontology queue for independent batch processing. There is
no standalone info/summary stage. The client does not keep a persistent graph replica.

## Start a Client

On the client host, from the repository root:

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
bash scripts/install-client.sh
bash scripts/run-client.sh
```

Open **http://127.0.0.1:8781/settings**, enter the shared server's HTTPS URL and
customer/node key, then add sources and select their submission channels.
The installer sets up Python dependencies, builds the browser and verifies its
assets. It reuses suitable runtimes or provisions pinned copies in the checkout.
A graphical session is needed for visible browser interaction. Linux x86_64 is
the verified environment; native Windows/macOS operation is not verified.

The dashboard defaults to **English** and can switch to **简体中文** in the header,
without discarding unsaved input. TopNews' **加油站** is **Do you Know** in English.
The language preference is per browser; evidence, configured names and report
content keep their original language.

| Page | Purpose |
| --- | --- |
| `/settings` — Sources | Server access, web/RSS/MP sources, multiple daily capture times, interval fallback, browser preferences and vision. |
| `/graph` — Intelligence graph | Search entities, inspect relationships, paths and original evidence. |
| `/query` — Query settings | Shared/own source scope, optional Jev post-filtering, local report/query model. |
| `/tasks` — Tasks | Profiles, shared sections, task boards/prompts, report schedules, output copy and DOCX filename. |
| `/reports` — Outputs | Manual reports, recent files/run logs and delivery targets. |
| `/notifications` — Notifications | Collection progress and pending manual actions. |

Each source can have multiple daily `HH:MM` capture times and its own timezone.
The persistent backend automatically queues due captures and avoids overlapping
runs of the same source. Daily times override the older minute interval; no times
and interval `0` means manual only. Source capture and report generation are
separate schedules.

TopNews and Weeklyreport run manually or on their own schedules. Configure your
local writing model in Query settings; the default is `xin_local` at
`http://127.0.0.1:9997/v1`. The installer does not install a model server. Model
failure falls back to deterministic formatting. Outputs are saved locally even
in dry-run mode. Actual delivery requires configured, enabled targets and dry-run
off: MimicWX supports text, Awada supports text/files, and WeCom webhooks remain
unimplemented.

Weeklyreport's **DOCX filename** is configurable per profile. The generator always
appends `_{YYYYMMDD}.docx`, for example `Council weekly_20261125.docx`, using the
generation date. This applies to saved files, listing, downloads and delivery;
Markdown and introduction TXT keep their established filenames.

Read the complete client manuals: **[English](client/README.md)** ·
**[简体中文](client/README.zh-CN.md)**. They cover installation prerequisites,
WeChat dependencies, first setup, schedules, report configuration, CLI/agents,
caches, upgrades and troubleshooting.

## Run the Shared Server

Use Python 3.12+ on a separate server host. Install only the server extra:

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
export OSBI_CONFIG_FILE=/path/to/private/runtime.yaml
.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
```

Prepare the private configuration from
[`server/config/example/runtime.yaml`](server/config/example/runtime.yaml),
setting model credentials, stores/schema/seed paths, channels, authentication and
batch timing for your deployment. Resolve paths for the private config location.
Use an HTTPS reverse proxy for remote client access; the application binds to
loopback by default. Server-only installation needs no Node/browser or agent host.

Supabase-managed authentication maps a key to its net and node; revocation,
expiry and net enablement are checked on authenticated requests. See
[current v4 deployment](docs/supabase-v4-deployment.md) for configuration and
operational job monitoring. Deployment-specific schemas, seeds, credentials and
state stay outside the public checkout. Back up state while the service is stopped.
Do not use the retired `osbi_server` CLI, 8780 instructions or old info/routing models.

Every submitted link requires a client-assigned `content`, `unknown` or
`navigation` role. The server prioritizes eligible article links and caps
navigation, without weakening the body date gate. See
[link selection](docs/link-selection.md) and [the HTTP contract](docs/client-server-contract.md).

## CLI and Optional Agent Integration

With the client backend running:

```bash
.venv/bin/osbi-client status
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

DeepSeek Harness has a management/query adapter:

```bash
bash scripts/install-client.sh --agent dsh
# In another terminal after starting the backend:
bash client/scripts/deepseek-harness/run.sh
```

The DSH host downloads only on first explicit launch. Other agent-host adapter
directories are placeholders. Dashboard, CLI and MCP share one persistent backend
and browser installation. Agent credentials, the OSBI customer key and model
provider keys are separate settings.

## Directory Structure

```text
server/wiseflow_server/    Active v4 server
server/config/example/    Public bootstrap configuration
server/deploy/            Deployment/monitoring SQL and templates
client/backend/           Python client and managed browser
client/scripts/           Installer, runtime helpers and host adapters
client/skills/            Shared management/query skills
contracts/                Shared transport and utility library
scripts/                  Stable client install/start entry points
tests/                    Offline regression and opt-in integration checks
docs/                     Current usage and protocol documentation
```

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

## Documentation

- [Client deployment and user manual — English](client/README.md)
- [Client 部署与使用手册 — 简体中文](client/README.zh-CN.md)
- [Installation and operation](docs/running.md)
- [v4 authentication and operations](docs/supabase-v4-deployment.md)
- [Client/server HTTP contract](docs/client-server-contract.md)
- [Local report tasks](docs/downstream-tasks.md)
- [Link roles and selection](docs/link-selection.md)
- [Shared contracts](contracts/README.md)
