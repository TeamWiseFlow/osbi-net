"""Bounded metadata retention without deleting active knowledge or in-flight inputs."""
import json
import shutil
import time


def prune_metadata(repo, policy, *, now=None, reserve_requests=0):
    now = time.time() if now is None else now
    counts = dict(requests=0, work=0, intents=0, reviews=0, sources=0, cache=0)
    limit = policy.batch_size
    with repo.transaction() as conn:
        surplus = max(0, conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] - policy.max_requests + reserve_requests)
        completed = conn.execute("SELECT COUNT(*) FROM work WHERE state IN ('succeeded','failed')").fetchone()[0]
        old_work = {r[0] for r in conn.execute("SELECT id FROM work WHERE state IN ('succeeded','failed') ORDER BY updated LIMIT ?",
                                               (min(limit, max(0, completed - policy.max_completed_work)),))}
        rows = conn.execute("""SELECT r.id,r.created,r.work_id,w.state FROM requests r JOIN work w ON w.id=r.work_id
            WHERE w.state IN ('succeeded','failed') AND
            (r.created<CASE WHEN w.state='failed' THEN ? ELSE ? END OR ?>0
             OR r.work_id IN (SELECT value FROM json_each(?))) ORDER BY r.created LIMIT ?""",
            (now - min(policy.request_days, policy.failed_days) * 86400, now - policy.request_days * 86400,
             surplus, json.dumps(sorted(old_work)), limit)).fetchall()
        for i, row in enumerate(rows):
            days = min(policy.request_days, policy.failed_days) if row['state'] == 'failed' else policy.request_days
            if row['created'] >= now - days * 86400 and i >= surplus and row['work_id'] not in old_work:
                continue
            conn.execute("DELETE FROM graph_sources WHERE request_id=?", (row['id'],))
            counts['requests'] += conn.execute("DELETE FROM requests WHERE id=?", (row['id'],)).rowcount
        rows = conn.execute("""SELECT * FROM work w WHERE state IN ('succeeded','failed')
            AND NOT EXISTS(SELECT 1 FROM requests r WHERE r.work_id=w.id)
            AND (updated<CASE WHEN state='failed' THEN ? ELSE ? END OR id IN (SELECT value FROM json_each(?)))
            ORDER BY updated LIMIT ?""", (now - policy.failed_days * 86400, now - policy.succeeded_days * 86400,
                                        json.dumps(sorted(old_work)), limit)).fetchall()
        for row in rows:
            days = policy.succeeded_days if row['state'] == 'succeeded' else policy.failed_days
            if row['updated'] >= now - days * 86400 and row['id'] not in old_work:
                continue
            counts['intents'] += conn.execute("DELETE FROM graph_intents WHERE work_id=?", (row['id'],)).rowcount
            conn.execute("UPDATE active_graphs SET work_id='' WHERE work_id=?", (row['id'],))
            counts['work'] += conn.execute("DELETE FROM work WHERE id=?", (row['id'],)).rowcount
        counts['sources'] = conn.execute("""DELETE FROM unit_sources WHERE rowid IN
            (SELECT s.rowid FROM unit_sources s WHERE NOT EXISTS(SELECT 1 FROM knowledge_units u
             WHERE u.channel=s.channel AND u.logical_id=s.logical_id) LIMIT ?)""", (limit,)).rowcount
        counts['reviews'] = conn.execute("""DELETE FROM review_records WHERE rowid IN
            (SELECT r.rowid FROM review_records r WHERE NOT EXISTS(SELECT 1 FROM knowledge_units u
             WHERE u.channel=r.channel AND u.extraction_id=r.extraction_id)
             AND NOT EXISTS(SELECT 1 FROM work w WHERE w.id=r.work_id) LIMIT ?)""", (limit,)).rowcount
        counts['cache'] = conn.execute("DELETE FROM model_cache WHERE id IN (SELECT id FROM model_cache WHERE created<? LIMIT ?)",
                                      (now - policy.orphan_days * 86400, limit)).rowcount
    return counts


async def cleanup(service, *, now=None):
    """Run through the store owner, preserving schema and source references."""
    now = time.time() if now is None else now
    repo, policy = service.repo, service.settings.retention
    async with service.routing_lock:
        counts = prune_metadata(repo, policy, now=now)
        active_channels, active_schemas = set(), set(service.schema_ids.values())
        for row in repo.rows("SELECT payload,result FROM work WHERE state NOT IN ('succeeded','failed')"):
            payload, result = json.loads(row['payload']), json.loads(row['result'])
            for item in payload.get('eligible', []):
                active_channels.add(item['id'])
                if item.get('schemaId'):
                    active_schemas.add(item['schemaId'])
            for item in result.get('assignments', []):
                active_channels.add(item['channelId'])
                active_schemas.add(item['schemaId'])
        counts.update(channels=0, schemas=0)
        cutoff = now - policy.orphan_days * 86400
        for row in repo.rows("""SELECT * FROM channels c WHERE created<? AND id NOT IN ('policy','economy','general')
            AND NOT EXISTS(SELECT 1 FROM knowledge_units u WHERE u.channel=c.id) LIMIT ?""", (cutoff, policy.batch_size)):
            channel = row['id']
            if channel in active_channels:
                continue
            if repo.one("SELECT 1 FROM active_graphs WHERE channel=?", (channel,)):
                continue
            for key in [key for key in service.engines if key[0] == channel]:
                await service.engines.pop(key).close()
            directory = service.settings.data_dir / 'graphs' / channel
            if directory.exists():
                shutil.rmtree(directory)
            with repo.transaction() as conn:
                conn.execute("DELETE FROM graph_intents WHERE channel=?", (channel,))
                conn.execute("DELETE FROM review_records WHERE channel=?", (channel,))
                conn.execute("DELETE FROM channel_families WHERE channel=?", (channel,))
                conn.execute("DELETE FROM channels WHERE id=?", (channel,))
            counts['channels'] += 1
        active_schemas.update(r['schema_id'] for r in repo.rows("SELECT schema_id FROM channels"))
        active_schemas.update(r['schema_id'] for r in repo.rows("SELECT DISTINCT schema_id FROM knowledge_units"))
        # Cached receipts remain meaningful until their work records expire.
        for row in repo.rows("SELECT result FROM work WHERE state IN ('succeeded','failed')"):
            active_schemas.update(a['schemaId'] for a in json.loads(row['result']).get('assignments', []))
        for schema in active_schemas:
            repo.touch_schema(schema, now)
        for row in repo.rows("""SELECT s.id FROM schema_revisions s LEFT JOIN schema_activity a ON a.id=s.id
                             WHERE COALESCE(a.last_seen,s.created)<? AND s.id NOT IN (SELECT value FROM json_each(?))
                             ORDER BY s.created LIMIT ?""",
                             (cutoff, json.dumps(sorted(active_schemas)), policy.batch_size)):
            schema = row['id']
            if schema in active_schemas:
                continue
            for key in [key for key in service.engines if key[1] == schema]:
                await service.engines.pop(key).close()
            for path in (service.settings.data_dir / 'graphs').glob('*/' + schema):
                shutil.rmtree(path)
            (service.settings.data_dir / 'schemas' / (schema + '.ttl')).unlink(missing_ok=True)
            repo.execute("DELETE FROM review_records WHERE schema_id=?", (schema,))
            counts['schemas'] += repo.execute("DELETE FROM schema_revisions WHERE id=?", (schema,)).rowcount
            repo.execute("DELETE FROM schema_activity WHERE id=?", (schema,))
        repo.execute("PRAGMA wal_checkpoint(PASSIVE)")
        repo.execute("PRAGMA incremental_vacuum(200)")
        service.last_cleanup = {"at": now, "counts": counts}
        return service.last_cleanup
