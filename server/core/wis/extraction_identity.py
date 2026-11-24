"""Content/channel identity shared by local dedup and the durable ontology backlog."""
import hashlib
import json


def block_channel_key(text, channel_id):
    """Stable job key for one original block under one channel.

    The channel is part of the identity: the same text selected by another
    channel has its own statement/rules/schema and must not reuse this job.
    Statement/schema/model revisions are handled by the extraction identity,
    not by this content-level key.
    """
    value = json.dumps(['block/channel/v1', channel_id, text],
                       ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()
