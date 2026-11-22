"""Content/focus identity shared by local dedup and the durable ontology backlog."""
import hashlib
import json


def block_focus_key(text, focus, library_id='policy'):
    identity = [text, focus.strip()]
    if library_id != 'policy':
        identity.extend(['ontology-library/v1', library_id])
    value = json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(value.encode()).hexdigest()
