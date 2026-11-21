"""Shared extraction orchestration with automatic fixed-schema channel routing."""
import asyncio
from dataclasses import asdict, replace
import json
import logging
from pathlib import Path
import re
import time
from threading import RLock
from osbi_contracts.models import Block, Focus, Material
from osbi_contracts.storage import digest, encode
from osbi_contracts.material import canonical_material, material_hash
from .config import PromptSettings, Settings
from .repository import Repository
from .llm_client import LLMClient, ModelError, generate, parse_json
from .llmuse import select_relevance
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
        self.stage_cache = {}
        self.routing_lock = asyncio.Lock()
        schema_dir = settings.schema_dir or Path(__file__).with_name("schemas")
        self.schema_ids = {}
        for name in ("policy", "economy", "generic"):
            text = (schema_dir / (name + ".ttl")).read_text()
            schema = OntologySchema(text)
            self.schema_ids[name] = schema.fingerprint
            self.repo.execute("INSERT OR IGNORE INTO schema_revisions VALUES (?,?,?,?)",
                              (schema.fingerprint, name, text, time.time()))
        seeds = [("policy", "政策与治理", "Policy, laws, regulation and public governance"),
                 ("economy", "经济与商业", "Economy, enterprises, industries and business developments")]
        for key, title, description in seeds:
            if key in settings.enabled_schemas:
                self.repo.create_channel(key, title, description, self.schema_ids[key], key)
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
        row = self.repo.one("SELECT family FROM channel_families WHERE channel=?", (channel_id,))
        return row["family"] if row else channel_id if channel_id in {"policy", "economy"} else "general"

    def configure_schemas(self, names):
        """Keep disabled data on disk while removing it from admission and query access."""
        names = tuple(names)
        if not names or set(names) - {"policy", "economy", "general"}:
            raise ValueError("A net references schema definitions not deployed on this server")
        self.settings.enabled_schemas = names
        for family, title, description in [
            ("policy", "政策与治理", "Policy, laws, regulation and public governance"),
            ("economy", "经济与商业", "Economy, enterprises, industries and business developments")]:
            if family in names:
                self.repo.create_channel(family, title, description, self.schema_ids[family], family)
        if names == ("general",):
            self.repo.create_channel("general", "General", "General public information", self.schema_ids["generic"], "general")

    def schema_principal(self, principal):
        """Resolve schema grants to channel grants inside this net only."""
        grants = set(principal["schemaGrants"]) & set(self.settings.enabled_schemas)
        channels = [r["id"] for r in self.repo.rows("SELECT id FROM channels") if self.schema_family(r["id"]) in grants]
        return {**principal, "grants": channels,
                "canCreate": "general" in grants and "general" in self.settings.enabled_schemas}

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
        canonical["eligible"] = eligible
        canonical["canCreate"] = principal.get("canCreate", "*" in principal["grants"]) and "general" in self.settings.enabled_schemas
        canonical["schemaGrants"] = list(principal.get("schemaGrants", self.settings.enabled_schemas))
        canonical["sourceUrl"] = material.meta.canonicalUrl or material.meta.finalUrl or material.meta.requestedUrl
        # Pin instructions at admission so a restart cannot mix prompt revisions.
        canonical["prompts"] = asdict(self.settings.prompts)
        # The source URL is provenance, not input to selection/entity models.
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
            "canCreate": principal.get("canCreate", "*" in principal["grants"]),
            "schemaGrants": sorted(principal.get("schemaGrants", [])),
            "models": [self.settings.selected_model, self.settings.ontology_model,
                       self.settings.fallback_model, VERSION],
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

    async def route(self, payload, block):
        async with self.routing_lock:
            current = dict(payload)
            eligible_ids = {c["id"] for c in payload["eligible"]}
            schema_grants = set(payload.get("schemaGrants", self.settings.enabled_schemas)) & set(self.settings.enabled_schemas)
            current["eligible"] = [r for r in self.repo.rows("SELECT id,title,description,schema_id AS schemaId FROM channels ORDER BY created,id")
                                   if (payload["canCreate"] or r["id"] in eligible_ids)
                                   and self.schema_family(r["id"]) in schema_grants]
            return await self._route_current(current, block)

    async def _route_current(self, payload, block):
        user = {"stage": "channel_routing", "title": payload["title"], "text": block["text"],
                "channels": [{k: c[k] for k in ("id", "title", "description")} for c in payload["eligible"]]}
        def validate(raw):
            value = parse_json(raw)
            existing = {x["id"] for x in payload["eligible"]}
            if value.get("channelId") in existing:
                return self.repo.channel(value["channelId"])
            if value.get("channelId") is not None or not payload["canCreate"]:
                raise ModelError("Routing did not select an authorized channel")
            category = value.get("category", {})
            category_id = category.get("id", "")
            if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", category_id) or category_id in {"generic", "other", "unknown"}:
                raise ModelError("A meaningful stable category ID is required")
            if not all(isinstance(category.get(k), str) and 1 <= len(category[k]) <= 500 for k in ("title", "description")):
                raise ModelError("New channel requires a title and scope")
            if not self.repo.channel(category_id) and self.repo.one("SELECT COUNT(*) n FROM channels WHERE id NOT IN ('policy','economy','general')")["n"] >= self.settings.retention.max_dynamic_channels:
                raise ModelError("Dynamic channel limit reached; administrator review required")
            return self.repo.create_channel(category_id, category["title"], category["description"], self.schema_ids["generic"], "general")
        channel, _ = await generate(self.chat_provider, [{"role": "system", "content": self.work_prompts(payload).routing},
                                    {"role": "user", "content": encode(user)}],
                                    self.settings.selected_model, self.settings.fallback_model, validate)
        return channel

    async def select(self, work):
        payload = json.loads(work["payload"])
        prompts = self.work_prompts(payload)
        focus = Focus.model_validate(payload["focus"])
        selector = {"title": payload["title"], "blocks": payload["blocks"],
                    "links": [{k: v for k, v in x.items() if k != "url"} for x in payload["links"]]}
        selection = await select_relevance(self.chat_provider, focus, selector,
                                          self.settings.selected_model, self.settings.fallback_model,
                                          system_prompt=prompts.relevance)
        assignments = []
        for block in payload["blocks"]:
            if block["id"] not in selection.blockIds:
                continue
            channel = await self.route(payload, block)
            assignments.append({"blockId": block["id"], "channelId": channel["id"], "schemaId": channel["schema_id"]})
        # Preserve the response field for clients that still expect it.
        result = {"selection": selection.model_dump(), "infos": [], "assignments": assignments, "graphs": []}
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
                    await self.select(work)
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
