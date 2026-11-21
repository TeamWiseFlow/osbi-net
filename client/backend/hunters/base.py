"""Hunter protocol and registry for platform-specific catalog discovery.

A hunter discovers article catalogs for a platform source kind (WeChat mp,
Weibo, Zhihu, ...). It returns a links-only catalog material plus seed metadata;
caching, submission, server selection, queue expansion and body capture all stay
in the daemon. Hunters never talk to the server and never launch browsers.
"""
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class CatalogDiscovery:
    material: dict | None
    seed_meta: dict


@runtime_checkable
class Hunter(Protocol):
    kind: str

    async def discover(self, source, config, *, force_refresh, cache, event) -> CatalogDiscovery:
        ...


HUNTERS: dict[str, Hunter] = {}


def register(hunter):
    if hunter.kind in HUNTERS:
        raise ValueError(f"hunter kind already registered: {hunter.kind}")
    HUNTERS[hunter.kind] = hunter
    return hunter


def hunter_for(kind):
    return HUNTERS.get(kind)
