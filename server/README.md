# OSBI Shared Server

Deploy this service independently of local clients. It owns original evidence,
ingestion jobs, relevance/extraction algorithms, channel schemas, authoritative
graphs, customer-key management and authenticated queries. It starts with the
policy/economy schemas and assigns the fixed generic schema to new channels.
Schema entity types and relationship predicates never expand automatically.

## File Structure

```text
server/
├── osbi_server/                    # Service, worker, graph queries and admin CLI
│   └── schemas/                    # Versioned server-owned domain schemas
└── deploy/
    ├── server.env.example          # Extraction model and processing settings
    ├── nginx.conf.example          # HTTPS reverse proxy
    └── osbi-server.service.example # Optional systemd service
```

`contracts/` at the repository root supplies common API models and utility code.
It is installed with the server and does not run as another service. Root
`pyproject.toml` and `requirements.lock` select/constrain dependencies for each
deployment role. Root `scripts/` contains client convenience entry points.

## Install on the Server Host

Use a separate checkout and Python environment on the server host. This release
uses one source repository with independent `server` and `client` dependency
extras. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
```

Python 3.12+ is required. Server deployment needs no Node, browser assets, agent
framework, MCP host or client-only Python dependencies. Configure extraction
models using [the environment template](deploy/server.env.example), then start:

```bash
.venv/bin/osbi-server serve
```

The default internal listener is `127.0.0.1:8780`. Publish HTTPS using
[the reverse-proxy example](deploy/nginx.conf.example), and provision expiring
customer keys for remote clients. The optional
[systemd unit](deploy/osbi-server.service.example) illustrates persistent service
setup. Full model, key, TLS, processing-window, backup and schema-maintenance
commands are in [installation and operation](../docs/running.md).

## Deployment Boundary

Server and client processes use separate environments, credentials and state
directories. Default server state is `~/.osbi/server`, configurable through
`OSBI_SERVER_DATA`. Each client connects through HTTPS with a customer key; do not
share database files or browser profiles between machines. Same-channel evidence
is shared among authorized clients.

TopNews, Weeklyreport, personalized interests/boards, local coverage ledgers and
optional Jev decisions belong to the client backend or agent. The server provides
generic retrieval for those tasks. Read the
[original-content storage and responsibility notice](../README.md#content-storage-intellectual-property-and-responsibility)
before deployment: server state and backups contain source content.
