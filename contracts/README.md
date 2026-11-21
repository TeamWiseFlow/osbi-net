# Shared Contracts

`osbi_contracts` is a common Python library installed in both client and server
environments. It is not a separate service or a shared runtime process.

- `models.py`: versioned transport models for original materials, source metadata
  and evidence/graph queries.
- `urls.py`: URL and publication-date normalization.
- `material.py`: versioned material SHA-256 normalization and caller-local result
  ID restoration. These are transport utilities; extraction identities and schema
  revision validation remains server-owned. Materials use `osbi-material/v2`;
  the normalized material hash is `osbi-material-sha256/v2` and includes explicit
  material/block channel assignments, not client focus preferences.
- `storage.py`: generic SQLite, serialization and digest helpers. Each deployment
  owns its own database and tables.
- `instance.py`: local state-directory process locking.

Domain schemas, extraction algorithms and graph persistence live in `server/`.
Acquisition, browser management, personalized reports and agent integration live
in `client/`. Contracts carry shared data shapes and utility behavior across this
boundary, without importing either application's implementation.

Clients choose relevance and channels. The server accepts only configured and
authorized channels and never creates them from submitted text. Each deployment
owns its private per-channel ontology revisions; policy/economy are examples and
the bundled default is only a template. See [the channel contract](../docs/channel-ontologies.md).

The root package metadata includes this library with the selected `server` or
`client` extra. Use a separate checkout/environment for each deployment and keep
client/server protocol versions compatible. See the
[client guide](../client/README.md) and [server guide](../server/README.md).
