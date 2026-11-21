"""Supabase-backed authentication and independent network state ownership."""
import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import re
import time

import httpx

from osbi_contracts.instance import InstanceLock
from .service import Service


class ControlUnavailable(RuntimeError):
    pass


class SupabaseControl:
    def __init__(self, url, key, *, client=None):
        self.url = url.rstrip('/')
        self.key = key
        self.client = client or httpx.AsyncClient(timeout=10)
        self.owns_client = client is None

    async def rpc(self, name, body):
        try:
            response = await self.client.post(self.url + '/rest/v1/rpc/' + name, json=body,
                headers={'apikey': self.key, 'Authorization': 'Bearer ' + self.key,
                         'Content-Profile': 'ops', 'Accept-Profile': 'ops'})
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            # Neither credential values nor provider response bodies reach clients/logs.
            raise ControlUnavailable('Authentication control plane unavailable') from error

    async def authenticate(self, key):
        if not re.fullmatch(r'osbi_[A-Za-z0-9_-]{24,128}', key):
            return None
        result = await self.rpc('osbi_authenticate', {'p_key_hash': hashlib.sha256(key.encode()).hexdigest()})
        if result is None:
            return None
        try:
            assert isinstance(result, dict)
            assert isinstance(result['id'], str) and isinstance(result['customer'], str)
            assert re.fullmatch(r'[a-z][a-z0-9-]{0,63}', result['netId'])
            for field in ['netSchemas', 'schemaGrants']:
                assert isinstance(result[field], list) and all(isinstance(x, str) for x in result[field])
            assert result['netSchemas'] and set(result['schemaGrants']) <= set(result['netSchemas'])
            assert type(result['rpm']) is int and 0 <= result['rpm'] <= 100000
            expiry = float(result['expires'])
            assert expiry == expiry
        except (AssertionError, KeyError, TypeError, ValueError):
            raise ControlUnavailable('Invalid authentication control response') from None
        return result if expiry > time.time() else None

    async def definitions(self):
        result = await self.rpc('osbi_net_definitions', {})
        if not isinstance(result, list):
            raise ControlUnavailable('Invalid network configuration')
        return result

    async def close(self):
        if self.owns_client:
            await self.client.aclose()


class NetRegistry:
    def __init__(self, settings, control=None, service_factory=None):
        self.settings = settings
        self.control = control or SupabaseControl(settings.control_url, settings.control_key)
        self.factory = service_factory or Service
        self.services = {}
        self.lock = asyncio.Lock()
        self.model_limit = asyncio.Semaphore(settings.concurrency)
        self.instance = InstanceLock(settings.data_dir / 'server.lock')
        self.instance.acquire()

    async def start(self):
        if not self.settings.api_key:
            raise ValueError('OSBI model credentials are required')
        try:
            for net in await self.control.definitions():
                await self.service(net['id'], net['schemas'])
        except BaseException:
            await self.close()
            raise

    async def service(self, net_id, schemas):
        if not isinstance(net_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', net_id):
            raise ControlUnavailable('Invalid network identity')
        if (not isinstance(schemas, (list, tuple)) or not schemas
                or any(not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', name) for name in schemas)):
            raise ControlUnavailable('Network schema definitions are unavailable')
        async with self.lock:
            active = self.services.get(net_id)
            if active is None:
                config = replace(self.settings, data_dir=self.settings.data_dir / 'nets' / net_id,
                                 enabled_schemas=tuple(schemas))
                active = self.factory(config)
                active.client.limit = self.model_limit
                try:
                    await active.start()
                except BaseException:
                    await active.close()
                    raise
                self.services[net_id] = active
            else:
                active.configure_schemas(schemas)
            return active

    def ready(self):
        return bool(self.services) and all(
            service.tasks and all(not task.done() for task in service.tasks)
            for service in self.services.values())

    async def close(self):
        for service in self.services.values():
            await service.close()
        self.services.clear()
        await self.control.close()
        self.instance.release()
