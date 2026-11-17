# Shared Contracts

`osbi_contracts` is a common Python library installed in both client and server
environments. It is not a separate service or a shared runtime process.

- `models.py`: versioned transport models for original materials, source metadata
  and evidence/graph queries.
- `urls.py`: URL and publication-date normalization.
- `material.py`: versioned material SHA-256 normalization and caller-local result
  ID restoration. These are transport utilities; extraction identities and schema
  decisions remain server-owned.
- `storage.py`: generic SQLite, serialization and digest helpers. Each deployment
  owns its own database and tables.
- `instance.py`: local state-directory process locking.

Domain schemas, extraction algorithms and graph persistence live in `server/`.
Acquisition, browser management, personalized reports and agent integration live
in `client/`. Contracts carry shared data shapes and utility behavior across this
boundary, without importing either application's implementation.

The root package metadata includes this library with the selected `server` or
`client` extra. Use a separate checkout/environment for each deployment and keep
client/server protocol versions compatible. See the
[client guide](../client/README.md) and [server guide](../server/README.md).
