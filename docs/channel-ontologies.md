# Client Channels and Deployment-Owned Ontologies

The client selects source blocks and follow-up links and assigns a channel to
each submitted material or block. The server authenticates the caller, validates
the declared channels, and extracts entities and relations from every submitted
original block. It does not infer relevance, infer channels, create channels, or
expand ontology vocabulary.

## Material Contract

`osbi-material/v2` requires either a material-level `channel` inherited by its
blocks, or a `channel` on every block. A block-level channel overrides the material
channel. Only deployment-configured, enabled and authorized channels are accepted;
missing/invalid assignments return 422, while unavailable/unauthorized channels
return 403 before model work is queued. Unknown channels are never created.

```json
{
  "protocolVersion": "osbi-material/v2",
  "clientId": "client-1",
  "requestId": "request-1",
  "channel": "economy",
  "title": "Original source title",
  "blocks": [
    {"id": "B1", "text": "Original business evidence"},
    {"id": "B2", "text": "Original policy evidence", "channel": "policy"}
  ],
  "links": [],
  "meta": {"requestedUrl": "https://example.org/source"}
}
```

The legacy `channelHint` field is accepted as an explicit material assignment
when `channel` is absent; conflicting values are rejected. It never asks a model
to choose a channel. Client `focus` is retained as transport metadata but is not
used for server relevance decisions or content identity.

`selection.blockIds` and `selection.linkIds` acknowledge all client-submitted
blocks and links. They are not server relevance judgments. Only send links the
client has chosen to follow; the server does not fetch them. `infos` stays empty.
`assignments` records each block's channel and ontology revision pinned at
admission. Channel assignments participate in material/work identity.

`GET /v1/capabilities` advertises the protocol version,
`channelAssignment: "client"`, `relevanceSelection: "client"`, and
`automaticChannels: false`. Fetch authorized channel IDs and active revision
fingerprints from `GET /v1/channels`; clients cannot upload or activate a schema
through material submission.

The local backend sends `source.channel` or its `defaultChannel` setting. A
prepared block can override that assignment. Configure these through settings,
CLI or MCP; the fresh default is the template channel `default`.

## Private Deployment Assets

Every channel has exactly one active ontology schema revision in its own server
instance/net. Its classes, relationships and iterative tuning are deployment
assets. Keep private manifests, source Turtle files and tuned revisions outside
the public repository and include them in restricted operational backups.

The bundled `schemas/default.ttl` is only a small bootstrap template, not a
universal production ontology. The policy and economy files under
`server/examples/schemas/` are optional public examples. They are not loaded by
default and must not become the destination for production tuning.

A fresh server without a private manifest initializes only `default`. For each
project, define channels for its own domain, adapt a private ontology per channel,
review extraction evidence, and continuously improve those private revisions.
Setting a channel name does not automatically select a bundled domain ontology.

For example, keep `/etc/osbi/channels.json` and private Turtle files together:

```json
{
  "channels": [
    {"id": "policy", "title": "Policy", "schema": "ontologies/policy.ttl"},
    {"id": "economy", "title": "Economy", "schema": "ontologies/economy.ttl"}
  ]
}
```

```bash
export OSBI_CHANNELS_FILE=/etc/osbi/channels.json
osbi-server serve
```

Paths are resolved relative to the manifest, or may be absolute. The manifest is
read at startup. New channels must have a valid private Turtle ontology with
labeled classes and relationship predicates. A managed net's legacy `schemas`
list and each key's `schema_permissions` contain these channel IDs; they no
longer reference a hardcoded policy/economy/general family list.

Bootstrap files initialize a channel only once. Existing active revisions are
read from the instance's immutable revision store. Changing code, examples or a
bootstrap file never silently replaces an active private ontology. Restored
existing channel state can still load if its original bootstrap file is missing.

## Iterate and Preserve Revisions

Export and edit private files, then explicitly activate the reviewed revision:

```bash
osbi-server schema-export --channel economy --out /private/economy-current.ttl
osbi-server schema-review --channel economy
osbi-server schema-activate --channel economy \
  --file /private/economy-reviewed.ttl --expected-revision CURRENT_SCHEMA_ID
osbi-server reprocess --channel economy
```

Export creates a new 0600 file and refuses to overwrite an existing path. In
Supabase mode place `--net NET_ID` before each subcommand. Revision activation
validates the ontology and current revision, preserves historical evidence, and
does not rebuild graphs until explicitly reprocessed. Queued v2 assignments keep
their admission-time schema IDs even if an administrator later activates another
revision. Backups contain private ontology bodies and original evidence.

## Upgrade from Inferred Channels

Update clients to send explicit channels before switching the production server.
V1 material submissions are rejected; completed historical jobs and graphs remain
readable. Previously assigned graph-stage work can continue. Legacy pending work
without assignments fails with `client_channel_required` and needs a new explicit
client submission; the server never guesses a channel to repair it.

Before the first upgrade, export the active deployment revisions into private
assets and configure the manifest for new channel/bootstrap recovery. Keep
existing net/key permissions aligned with those channel IDs. Do not reapply the
control-plane SQL merely to replace existing nets with the fresh-install default.
Take a stopped-server snapshot before changing state. No schema revision needs
to be copied back into the public repository as part of an application release.
