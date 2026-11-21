"""Extract original evidence in client-assigned, deployment-owned channels."""
import asyncio
from dataclasses import asdict, replace
import json
import logging
import re
import time
from threading import RLock
from osbi_contracts.models import Block, Focus, Material
from osbi_contracts.storage import digest, encode
from osbi_contracts.material import canonical_material, material_hash
from .config import PromptSettings, Settings
from .repository import Repository
from .llm_client import LLMClient
from .ontology import OntologyExtractor, VERSION, extraction_fingerprint
from .ontology_schema import OntologySchema

def permitted(principal, channel):
    return "*" in principal["grants"] or channel in principal["grants"]


class Service:
    def __init__(self, settings: Settings, chat=None):
        from osbi_contracts.instance import InstanceLock
        self.instance = InstanceLock(settings.data_dir / "server.lock")
        self.instance.acquire()
        self.settings = settings
        self.repo = Repository(settings.data_dir / "metadata.db")
        self.client = LLMClient(settings.base_url, settings.api_key, concurrency=settings.concurrency, timeout=settings.timeout)
        self.chat_provider = chat or self.client.chat
        self.engines = {}
        self.engines_lock = RLock()
        self.tasks = []
        self.maintenance_task = None
        self.last_cleanup = None
        self.metadata_lock = asyncio.Lock()
        self.schema_ids = {}
        self.configure_schemas(settings.enabled_schemas)
        for row in self.repo.rows("SELECT * FROM channels"):
            self.engine(row["id"])
        self.migrate_units()

    def migrate_units(self):
        """Detach legacy evidence from request/work retention before compacting inputs."""
        if self.repo.one("SELECT 1 FROM migrations WHERE name='independent-units-v1'"):
            return
        for row in self.repo.rows("SELECT * FROM active_graphs"):
            work = self.repo.one("SELECT * FROM work WHERE id=?", (row["work_id"],))
            if not work:
                raise ValueError("Legacy evidence has no extraction metadata")
            result = json.loads(work["result"])
            receipt = next((g for g in result.get("graphs", []) if g["id"] == row["extraction_id"] and g["channelId"] == row["channel"]), None)
            if receipt is None:
                payload = json.loads(work["payload"])
                assignment = next((a for a in result.get("assignments", []) if a["channelId"] == row["channel"]
                    and digest([payload["title"], next(b["text"] for b in payload["blocks"] if b["id"] == a["blockId"]), payload["meta"]]) == row["logical_id"]), None)
                if assignment is None:
                    raise ValueError("Legacy evidence schema could not be resolved")
                receipt = assignment
            stored = self.engine(row["channel"], receipt["schemaId"]).store.read(row["extraction_id"])
            if not stored:
                raise ValueError("Legacy evidence graph is incomplete")
            p = stored["provenance"]
            original = {"title": p["source_title"], "block": {"id": "B1", "text": p["raw_text"]}, "meta": p["source_meta"]}
            with self.repo.transaction() as conn:
                self.repo.save_unit(row["channel"], row["logical_id"], row["extraction_id"], receipt["schemaId"], original, updated=row["updated"])
                for source in conn.execute("SELECT r.* FROM requests r JOIN graph_sources s ON s.request_id=r.id WHERE s.channel=? AND s.extraction_id=?",
                                           (row["channel"], row["extraction_id"])).fetchall():
                    self.repo.merge_source(conn, row["channel"], row["logical_id"], json.loads(source["payload"]), source["created"])
        self.repo.compact_requests()
        for work in self.repo.rows("SELECT id FROM work WHERE state='succeeded'"):
            self.repo.compact_work(work["id"])
        self.repo.execute("INSERT INTO migrations VALUES ('independent-units-v1',?)", (time.time(),))

    def engine(self, channel_id, schema_id=None):
        with self.engines_lock:
            return self._engine(channel_id, schema_id)

    def _engine(self, channel_id, schema_id=None):
        channel = self.repo.channel(channel_id)
        schema_id = schema_id or channel["schema_id"]
        key = (channel_id, schema_id)
        if key not in self.engines:
            stored = self.repo.one("SELECT * FROM schema_revisions WHERE id=?", (schema_id,))
            path = self.settings.data_dir / "schemas" / (schema_id + ".ttl")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(stored["body"])
            # Each immutable schema revision has one store owner.
            self.engines[key] = OntologyExtractor(replace(self.settings, schema_path=path),
                                                 self.settings.data_dir / "graphs" / channel_id / schema_id)
            if self.engines[key].schema.fingerprint != schema_id:
                raise ValueError("Stored schema revision integrity check failed")
        return self.engines[key]

    def channels(self, principal):
        return [dict(id=r["id"], title=r["title"], description=r["description"], schemaId=r["schema_id"])
                for r in self.repo.rows("SELECT * FROM channels ORDER BY created,id")
                if self.schema_family(r["id"]) in self.settings.enabled_schemas and permitted(principal, r["id"])]

    def schema_family(self, channel_id):
        return channel_id

    def configure_schemas(self, names):
        """Keep disabled data on disk while removing it from admission and query access."""
        names = tuple(names)
        from .channels import CHANNEL_ID_PATTERN
        if not names or any(not isinstance(name, str) or not re.fullmatch(CHANNEL_ID_PATTERN, name) for name in names):
            raise ValueError("A net requires valid deployment channel IDs")
        self.settings.enabled_schemas = names
        definitions = {item.id: item for item in self.settings.channel_definitions}
        for name in names:
            existing = self.repo.channel(name)
            if existing:
                # Activated revisions belong to this instance and survive code/config updates.
                self.schema_ids[name] = existing['schema_id']
                continue
            definition = definitions.get(name)
            if definition is None:
                raise ValueError("Channel has no deployment-owned schema definition: " + name)
            text = definition.schema.read_text(encoding='utf-8')
            schema = OntologySchema(text)
            if not schema.predicates:
                raise ValueError("Channel ontologies require labeled relationship definitions")
            with self.repo.transaction() as conn:
                conn.execute("INSERT OR IGNORE INTO schema_revisions VALUES (?,?,?,?)",
                    (schema.fingerprint, name, text, time.time()))
                conn.execute("INSERT OR IGNORE INTO channels VALUES (?,?,?,?,?)",
                    (name, definition.title, definition.description, schema.fingerprint, time.time()))
                conn.execute("INSERT OR IGNORE INTO channel_families VALUES (?,?)", (name, name))
            self.schema_ids[name] = schema.fingerprint

    def schema_principal(self, principal):
        """Resolve schema grants to channel grants inside this net only."""
        grants = set(principal["schemaGrants"]) & set(self.settings.enabled_schemas)
        channels = [r["id"] for r in self.repo.rows("SELECT id FROM channels") if self.schema_family(r["id"]) in grants]
        return {**principal, "grants": channels,
                "canCreate": False}

    async def start(self):
        self.tasks = [asyncio.create_task(self.worker()) for _ in range(self.settings.concurrency)]
        if self.settings.retention.enabled:
            self.maintenance_task = asyncio.create_task(self.maintenance_loop())

    async def maintenance_loop(self):
        from .maintenance import cleanup
        while True:
            try:
                totals = {}
                while True:
                    report = await cleanup(self)
                    for name, count in report['counts'].items():
                        totals[name] = totals.get(name, 0) + count
                    if not any(count >= self.settings.retention.batch_size for count in report['counts'].values()):
                        break
                    await asyncio.sleep(0)
                self.last_cleanup = {"at": time.time(), "counts": totals}
                if any(totals.values()):
                    logging.getLogger(__name__).info("Metadata maintenance counts: %s", totals)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logging.getLogger(__name__).warning("Metadata maintenance failed: %s", type(error).__name__)
            await asyncio.sleep(self.settings.retention.interval_seconds)

    async def close(self):
        if self.maintenance_task:
            self.maintenance_task.cancel()
            await asyncio.gather(self.maintenance_task, return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        # Reset only leases owned by this process after workers have stopped.
        self.repo.execute("UPDATE work SET state=CASE WHEN result='{}' THEN 'pending' ELSE 'graph_pending' END,lease=0 WHERE state='running'")
        for engine in self.engines.values():
            await engine.close()
        await self.client.close()
        self.repo.close()
        self.instance.release()

    def submit(self, material: Material, principal):
        eligible = self.channels(principal)
        if not eligible:
            raise PermissionError("No channel access")
        content_hash = material_hash(material)
        if material.materialHash and material.materialHash != content_hash:
            raise ValueError("Material hash does not match its normalized content")
        canonical = canonical_material(material)
        canonical["materialHash"] = content_hash
        declared = {block.channel or material.channel for block in material.blocks} or {material.channel}
        available = {channel['id']: channel for channel in eligible}
        if declared - available.keys():
            raise PermissionError("Client channel is unavailable or not authorized")
        canonical["eligible"] = [available[name] for name in sorted(declared)]
        canonical["assignments"] = [{"blockId": block['id'], "channelId": block['channel'],
            "schemaId": available[block['channel']]['schemaId']} for block in canonical['blocks']]
        canonical["sourceUrl"] = material.meta.canonicalUrl or material.meta.finalUrl or material.meta.requestedUrl
        # Pin instructions at admission so a restart cannot mix prompt revisions.
        canonical["prompts"] = asdict(self.settings.prompts)
        # The source URL is provenance, not input to entity/relation models.
        canonical["processingRevision"] = self.processing_revision(principal)
        identity = [content_hash, canonical["processingRevision"]]
        if self.settings.retention.enabled and self.repo.one("SELECT COUNT(*) n FROM requests")["n"] >= self.settings.retention.max_requests:
            from .maintenance import prune_metadata
            prune_metadata(self.repo, self.settings.retention, reserve_requests=1)
        request, reused = self.repo.submit(material, canonical, "w_" + digest(identity), principal["customer"],
                                         request_limit=self.settings.retention.max_requests)
        row = self.repo.one("SELECT * FROM work WHERE id=?", (request["work_id"],))
        if row["state"] == "succeeded":
            self.repo.attach_sources(row["id"], json.loads(row["result"]).get("graphs", []))
        return self.repo.work_result(row, request, reused)

    def processing_revision(self, principal):
        return digest({"channels": self.channels(principal),
            "channelAssignment": "client/v2",
            "models": [self.settings.ontology_model, self.settings.fallback_model, VERSION],
            "prompts": self.settings.prompts.fingerprint, "extraction": extraction_fingerprint(self.settings)})

    def job(self, job_id, principal):
        request = self.repo.one("SELECT * FROM requests WHERE id=? AND customer=?", (job_id, principal["customer"]))
        if not request:
            return None
        row = self.repo.one("SELECT * FROM work WHERE id=?", (request["work_id"],))
        payload = json.loads(request["payload"])
        if not all(permitted(principal, r["id"]) for r in payload["eligible"]):
            raise PermissionError("Key no longer permits this job")
        return self.repo.work_result(row, request)

    def work_prompts(self, payload):
        # Legacy queued work has no snapshot and uses the current startup defaults.
        return PromptSettings.from_mapping(payload["prompts"]) if "prompts" in payload else self.settings.prompts

    async def prepare(self, work):
        """Acknowledge client-selected material and its pinned channel assignments."""
        payload = json.loads(work["payload"])
        assignments = payload.get('assignments')
        if assignments is None:
            self.repo.update_work(work['id'], 'failed',
                error="client_channel_required: resubmit legacy unassigned work with explicit channels")
            return
        selection = {"blockIds": [b['id'] for b in payload['blocks']],
            "linkIds": [link['id'] for link in payload.get('links', [])]}
        # Preserve the response field for clients that still expect it.
        result = {"selection": selection, "infos": [], "assignments": assignments, "graphs": []}
        self.repo.update_work(work["id"], "graph_pending" if assignments else "succeeded", result)
        if not assignments:
            self.repo.compact_work(work["id"])

    async def extract_graph(self, work):
        payload, result = json.loads(work["payload"]), json.loads(work["result"])
        prompts = self.work_prompts(payload)
        graphs = []
        for assignment in result["assignments"]:
            block = next(b for b in payload["blocks"] if b["id"] == assignment["blockId"])
            engine = self.engine(assignment["channelId"], assignment["schemaId"])
            model_block = Block.model_validate(block)
            # Graph extraction is independent of the customer's selection focus.
            focus = Focus(focuspoint="Extract evidence-backed facts from the original source")
            meta = {**payload["meta"], "canonicalUrl": payload["sourceUrl"]}
            extraction_id, _, _ = engine.identity(focus=focus, title=payload["title"], block=model_block, meta=meta, prompts=prompts)
            logical_id = digest([payload["title"], block["text"], payload["meta"]])
            self.repo.execute("INSERT OR IGNORE INTO graph_intents VALUES (?,?,?,?, 'pending')",
                              (assignment["channelId"], logical_id, extraction_id, work["id"]))
            receipt = await engine.extract(focus=focus, title=payload["title"], block=model_block,
                                          meta=meta, material_id=work["id"], chat=self.chat_provider, prompts=prompts)
            stored = engine.store.read(extraction_id)
            from collections import Counter
            review = {"classUsage": dict(Counter(e["label"] for e in stored["entities"])),
                      "predicateUsage": dict(Counter(e["label"] for e in stored["relations"])),
                      "statistics": stored["statistics"], "blockId": block["id"]}
            with self.repo.transaction() as conn:
                self.repo.save_unit(assignment["channelId"], logical_id, extraction_id, assignment["schemaId"],
                    {"title": payload["title"], "block": {"id": "B1", "text": block["text"]}, "meta": meta})
                conn.execute("INSERT INTO active_graphs VALUES (?,?,?,?,?) ON CONFLICT(channel,logical_id) DO UPDATE SET extraction_id=excluded.extraction_id,work_id=excluded.work_id,updated=excluded.updated",
                             (assignment["channelId"], logical_id, extraction_id, work["id"], time.time()))
                conn.execute("UPDATE graph_intents SET state='committed' WHERE channel=? AND logical_id=? AND extraction_id=?",
                             (assignment["channelId"], logical_id, extraction_id))
                conn.execute("INSERT OR REPLACE INTO review_records VALUES (?,?,?,?,?)",
                             (assignment["channelId"], extraction_id, assignment["schemaId"], work["id"], encode(review)))
            graphs.append({**receipt, "channelId": assignment["channelId"], "schemaId": assignment["schemaId"]})
        result["graphs"] = graphs
        self.repo.attach_sources(work["id"], graphs)
        self.repo.update_work(work["id"], "succeeded", result)
        self.repo.compact_work(work["id"])

    async def worker(self):
        while True:
            work = self.repo.claim(self.settings.graph_allowed())
            if work is None:
                await asyncio.sleep(0.1)
                continue
            async def heartbeat(work_id):
                while True:
                    await asyncio.sleep(20)
                    self.repo.execute("UPDATE work SET lease=? WHERE id=? AND state='running'", (time.time() + 600, work_id))
            renewal = asyncio.create_task(heartbeat(work["id"]))
            try:
                if work["stage"] == "graph":
                    await self.extract_graph(work)
                else:
                    await self.prepare(work)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Do not expose provider payloads or paths in customer-visible errors.
                message = "Model or extraction stage failed: " + type(error).__name__
                self.repo.update_work(work["id"], "failed" if work["attempts"] >= 3 else "retry",
                                      error=message, retry_delay=min(60, 2 ** work["attempts"]))
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)

    def status(self):
        states = {r["state"]: r["n"] for r in self.repo.rows("SELECT state,COUNT(*) n FROM work GROUP BY state")}
        watermark = self.repo.one("SELECT MAX(updated) value FROM knowledge_units")["value"]
        return {"processingMode": self.settings.processing_mode, "batchWindow": self.settings.batch_window,
                "timezone": self.settings.batch_timezone, "graphAllowed": self.settings.graph_allowed(),
                "states": states, "completedWatermark": watermark, "maintenance": self.last_cleanup}
