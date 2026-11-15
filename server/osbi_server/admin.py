"""Manual evidence review and explicit immutable schema revisions."""
from collections import Counter
import json
import time
from osbi_contracts.storage import digest, encode
from .ontology_schema import OntologySchema


def review(repo, channel, limit=20):
    item = repo.channel(channel)
    if not item:
        raise ValueError("Unknown channel")
    schema = OntologySchema(repo.one("SELECT body FROM schema_revisions WHERE id=?", (item["schema_id"],))["body"])
    classes, predicates, samples = Counter(), Counter(), []
    for row in repo.rows("SELECT r.* FROM review_records r JOIN active_graphs a ON a.channel=r.channel AND a.extraction_id=r.extraction_id WHERE r.channel=?", (channel,)):
        body = json.loads(row["body"])
        classes.update(body["classUsage"])
        predicates.update(body["predicateUsage"])
        if len(samples) < limit:
            work = json.loads(repo.one("SELECT payload FROM work WHERE id=?", (row["work_id"],))["payload"])
            block = next(b for b in work["blocks"] if b["id"] == body["blockId"])
            samples.append({"extractionId": row["extraction_id"], "schemaId": row["schema_id"],
                           "originalBlock": block, "title": work["title"], "statistics": body["statistics"]})
    return {"channelId": channel, "activeSchemaId": schema.fingerprint,
            "classes": {k: classes[k] for k in schema.classes},
            "predicates": {k: predicates[k] for k in schema.predicates}, "samples": samples}


def activate(repo, channel_id, text, expected_revision):
    schema = OntologySchema(text)
    if not schema.predicates:
        raise ValueError("A usable schema must define relation predicates")
    with repo.transaction() as conn:
        current = conn.execute("SELECT schema_id FROM channels WHERE id=?", (channel_id,)).fetchone()
        if not current or current[0] != expected_revision:
            raise ValueError("Channel missing or active schema revision changed")
        conn.execute("INSERT OR IGNORE INTO schema_revisions VALUES (?,?,?,?)", (schema.fingerprint, channel_id, text, time.time()))
        conn.execute("UPDATE channels SET schema_id=? WHERE id=?", (schema.fingerprint, channel_id))
    return {"channelId": channel_id, "previousSchemaId": expected_revision, "activeSchemaId": schema.fingerprint,
            "reprocessing": "Explicit reprocess required; previous graph remains active"}


def reprocess(repo, channel_id):
    channel = repo.channel(channel_id)
    if not channel:
        raise ValueError("Unknown channel")
    scheduled = []
    for record in repo.rows("SELECT DISTINCT w.* FROM work w JOIN active_graphs a ON a.work_id=w.id WHERE a.channel=?", (channel_id,)):
        result = json.loads(record["result"])
        old_schema_ids = [a["schemaId"] for a in result.get("assignments", []) if a["channelId"] == channel_id]
        if not old_schema_ids or all(i == channel["schema_id"] for i in old_schema_ids):
            continue
        for assignment in result["assignments"]:
            if assignment["channelId"] == channel_id:
                assignment["schemaId"] = channel["schema_id"]
        result["graphs"] = []
        work_id = "w_" + digest([record["id"], channel_id, channel["schema_id"]])
        now = time.time()
        with repo.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO work(id,payload,state,result,created,updated) VALUES (?,?,'graph_pending',?,?,?)",
                         (work_id, record["payload"], encode(result), now, now))
            # Preserve all source observations when replacing an active graph.
            for request in conn.execute("SELECT * FROM requests WHERE work_id=?", (record["id"],)).fetchall():
                new_id = "reprocess_" + digest([request["id"], work_id])
                conn.execute("INSERT OR IGNORE INTO requests VALUES (?,?,?,?,?,?,?,?)",
                             (new_id, request["customer"], request["client_id"], new_id, request["payload_hash"], request["payload"], work_id, now))
        scheduled.append(work_id)
    return {"channelId": channel_id, "workIds": scheduled}
