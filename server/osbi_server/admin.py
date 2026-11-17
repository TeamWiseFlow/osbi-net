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
    for row in repo.rows("SELECT r.*,a.original FROM review_records r JOIN knowledge_units a ON a.channel=r.channel AND a.extraction_id=r.extraction_id WHERE r.channel=?", (channel,)):
        body = json.loads(row["body"])
        classes.update(body["classUsage"])
        predicates.update(body["predicateUsage"])
        if len(samples) < limit:
            original = json.loads(row["original"])
            samples.append({"extractionId": row["extraction_id"], "schemaId": row["schema_id"],
                           "originalBlock": original["block"], "title": original["title"], "statistics": body["statistics"]})
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
        repo.touch_schema(expected_revision, time.time())
        repo.touch_schema(schema.fingerprint, time.time())
    return {"channelId": channel_id, "previousSchemaId": expected_revision, "activeSchemaId": schema.fingerprint,
            "reprocessing": "Explicit reprocess required; previous graph remains active"}


def reprocess(repo, channel_id, *, force=False, settings=None):
    from dataclasses import asdict
    from .config import Settings
    from .ontology import extraction_fingerprint
    settings = settings or Settings.from_env()
    config_id = extraction_fingerprint(settings)
    channel = repo.channel(channel_id)
    if not channel:
        raise ValueError("Unknown channel")
    scheduled = []
    for unit in repo.rows("SELECT * FROM knowledge_units WHERE channel=?", (channel_id,)):
        if not force and unit["schema_id"] == channel["schema_id"]:
            continue
        original = json.loads(unit["original"])
        meta = original["meta"]
        payload = {"title": original["title"], "blocks": [original["block"]], "links": [],
            "meta": {k: meta.get(k, "") for k in ("author", "publisher", "publishedAt")},
            "sourceUrl": meta.get("canonicalUrl") or meta.get("finalUrl") or meta.get("requestedUrl", ""),
            "eligible": [{"id": channel_id}], "prompts": asdict(settings.prompts)}
        result = {"selection": {"blockIds": [original["block"]["id"]], "linkIds": []}, "infos": [],
            "assignments": [{"blockId": original["block"]["id"], "channelId": channel_id, "schemaId": channel["schema_id"]}], "graphs": []}
        body = encode(payload)
        work_id = "w_" + digest(["reprocess/v2", channel_id, unit["logical_id"], channel["schema_id"], config_id])
        existing = repo.one("SELECT state FROM work WHERE id=?", (work_id,))
        if existing and existing["state"] == "succeeded":
            continue
        now = time.time()
        with repo.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO work(id,payload,state,result,created,updated) VALUES (?,?,'graph_pending',?,?,?)",
                         (work_id, body, encode(result), now, now))
            conn.execute("UPDATE work SET state='graph_pending',attempts=0,next_at=0,error='',updated=? WHERE id=? AND state='failed'",
                         (now, work_id))
            # Source observations belong to the logical unit, independently of work.
        scheduled.append(work_id)
    return {"channelId": channel_id, "workIds": scheduled}
