# DeepSeek Harness Integration

The first implementation provides management/query skills and a stdio MCP adapter
to the persistent local backend. Start the backend, install skills with
`scripts/install-dsh.py`, and use `scripts/run-dsh.sh` to launch the pinned host
with the included MCP overlay. Existing host profiles are preserved.
For a fresh machine, `bash scripts/install-client.sh --agent dsh` installs the
common backend/browser and these skills together. It does not download the DSH
host until the launcher is explicitly run. Other agent frameworks use the same
backend; only their skill paths, MCP registration and launch adapter differ.

See [installation and operation](../../docs/running.md) for current setup instructions.
Local weekly selection consumes generic server queries; automated report delivery is not available.
