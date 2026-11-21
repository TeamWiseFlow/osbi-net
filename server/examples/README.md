# Public Ontology Examples

The policy and economy Turtle files are public examples. They are not the
authoritative ontologies of any production deployment, and future private tuning
must not be synchronized back here automatically.

Each deployment defines its own channels and binds one active ontology revision
to each channel. Copy an example, or the bundled `default.ttl` template, into a
private directory outside the source checkout before adapting it. Configure the
private channel manifest with `OSBI_CHANNELS_FILE`.

For an isolated example deployment only, `channels.json` in this directory loads
the unchanged public examples. It is not a production asset-management workflow.
See [channel assignments and private ontologies](../../docs/channel-ontologies.md)
for the v2 material contract, private manifests and revision activation.
