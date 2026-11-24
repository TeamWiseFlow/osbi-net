"""Read the existing Supabase OSBI control plane without changing its schema.

Keys remain authoritative in ops.osbi_keys; customer_id is the node identity.
Every channel is shared within the selected data scope. Legacy schema grants
are retained in the database but are not channel ACLs in the v4 protocol.
"""
import asyncio
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import replace
import hashlib
import logging
import os
import re
import time

import httpx
from fastapi import HTTPException

from llm_client import LLMClient

logger = logging.getLogger(__name__)
_active_service = ContextVar('osbi_active_service', default=None)
NET_ID = re.compile(r'^[a-z][a-z0-9-]{0,63}$')
NODE_ID = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')


def current_service(app):
    active = _active_service.get()
    return active if active is not None else app.state.service


class SupabaseControl:
    def __init__(self, registry, *, client=None):
        self.registry = registry
        url = os.environ.get('OSBI_CONTROL_URL', '').rstrip('/')
        key = os.environ.get('OSBI_CONTROL_KEY', '').strip()
        if client is None and (not url or not key):
            raise ValueError('Supabase mode requires OSBI_CONTROL_URL and OSBI_CONTROL_KEY')
        self.client = client or httpx.AsyncClient(
            base_url=url.rstrip('/') + '/rest/v1/', timeout=10, trust_env=False,
            headers={'apikey': key, 'Authorization': 'Bearer ' + key,
                     'Content-Profile': 'ops', 'Accept-Profile': 'ops'})
        self.rates = OrderedDict()

    async def close(self):
        await self.client.aclose()

    async def rpc(self, name, body):
        try:
            response = await self.client.post('rpc/' + name, json=body)
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except (httpx.HTTPError, ValueError):
            # Never include headers, response bodies or tokens in a client error.
            logger.warning('Supabase control RPC unavailable: %s', name)
            raise HTTPException(503, 'Authentication control plane unavailable') from None

    async def definitions(self):
        rows = await self.rpc('osbi_net_definitions', {})
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or not NET_ID.fullmatch(str(row.get('id', '')))
                for row in rows):
            raise HTTPException(503, 'Invalid control-plane network definitions')
        return rows

    async def publish_jobs(self, rows):
        response = await self.client.post('osbi_jobs',
            params={'on_conflict': 'net_id,job_id'}, json=rows, timeout=5,
            headers={'Content-Profile': 'work', 'Accept-Profile': 'work',
                     'Prefer': 'resolution=merge-duplicates,return=minimal'})
        response.raise_for_status()

    async def authenticate(self, token):
        if not token or len(token) > 256 or not token.startswith('osbi_'):
            raise HTTPException(401, 'Invalid or missing node key')
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = await self.rpc('osbi_authenticate', {'p_key_hash': digest})
        if not row:
            raise HTTPException(401, 'Invalid or missing node key')
        try:
            net = row['netId']
            customer = row['customer']
            if not NET_ID.fullmatch(net) or not isinstance(customer, str) or not customer.strip():
                raise ValueError
            if float(row['expires']) <= time.time():
                raise HTTPException(401, 'Expired node key')
            rpm = int(row['rpm'])
            if rpm < 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise HTTPException(503, 'Invalid control-plane identity') from None
        # Every request checks the database, including revocation and net enablement.
        # Only the small rate counter is cached; identities are never cached.
        now = time.monotonic()
        while self.rates and next(iter(self.rates.values()))[0] <= now - 60:
            self.rates.popitem(last=False)
        if rpm:
            started, count = self.rates.get(digest, (now, 0))
            if count >= rpm:
                raise HTTPException(429, 'Node key request limit exceeded', headers={'Retry-After': '60'})
            if digest not in self.rates and len(self.rates) >= 100000:
                raise HTTPException(503, 'Authentication rate tracker is full')
            self.rates[digest] = (started, count + 1)
        node = self.registry.node_aliases.get(customer, customer)
        if not NODE_ID.fullmatch(node):
            node = 'node-' + hashlib.sha256(customer.encode()).hexdigest()[:40]
        return net, node


class NetServices:
    """One database/cache/graph owner per net, with a shared model semaphore."""

    def __init__(self, settings, resources, control=None):
        self.settings, self.resources, self.control = settings, resources, control
        self.services = {}
        self.lock = asyncio.Lock()
        self.client = LLMClient(settings.base_url, settings.api_key,
                                concurrency=settings.concurrency, timeout=settings.timeout)
        resources.push_async_callback(self.client.close)

    async def get(self, net):
        scope = net if self.settings.config.auth.data_scope == 'net' else 'shared'
        async with self.lock:
            if scope in self.services:
                return self.services[scope]
            from core.async_database import AsyncDatabaseManager
            from core.wis.async_cache import SqliteCache
            from .worker import ExtractionService
            root = self.settings.data_dir / 'nets' / scope
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            stores = {key: replace(store, graph_path=root / key)
                      for key, store in self.settings.config.stores.items()}
            config = replace(self.settings.config, data_dir=root, stores=stores)
            settings = replace(self.settings, config=config)
            db = AsyncDatabaseManager(logger=logger, db_path=root / 'data.db')
            cache = SqliteCache(root / 'main_cache.sqlite')
            # Register cleanup before opening resources, including failed startup.
            self.resources.push_async_callback(db.cleanup)
            self.resources.push_async_callback(cache.close)
            await db.initialize()
            await cache.open()
            active = ExtractionService(settings, self.client.chat, db, cache)
            self.resources.push_async_callback(active.close)
            await active.metrics.ensure_schema()
            if self.control is not None:
                from .job_monitor import JobMonitor
                active.monitor = JobMonitor(db, scope, self.control.publish_jobs)
                active.batch.monitor = active.monitor
                await active.monitor.start(active.queue)
            for engine in active.ontology_engines.values():
                await asyncio.to_thread(engine.store.load_seeds, settings.seed_paths)
            active.batch.schedule()
            self.services[scope] = active
            return active
