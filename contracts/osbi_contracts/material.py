"""Versioned content identity and caller-local ID restoration for materials."""
from copy import deepcopy
from .storage import digest

MATERIAL_HASH_VERSION = "osbi-material-sha256/v1"


def canonical_material(material):
    from .models import Material
    body = material.model_dump() if hasattr(material, "model_dump") else Material.model_validate(material).model_dump()
    for key in ("clientId", "requestId", "materialHash"):
        body.pop(key, None)
    body["meta"] = {key: body["meta"].get(key, "") for key in ("author", "publisher", "publishedAt")}
    body["focus"]["id"] = 0
    for name, prefix in (("blocks", "B"), ("links", "L")):
        for i, item in enumerate(body[name], 1):
            item["id"] = f"{prefix}{i}"
    return body


def material_hash(material):
    return digest([MATERIAL_HASH_VERSION, canonical_material(material)])


def map_result(result, material, *, canonical=False):
    """Translate only transport IDs; graph identities and job references are stable."""
    body = material.model_dump() if hasattr(material, "model_dump") else material
    value = deepcopy(result)
    maps = {}
    for name, prefix in (("blocks", "B"), ("links", "L")):
        ids = body.get("blockIds" if name == "blocks" else "linkIds") or [item["id"] for item in body.get(name, [])]
        pairs = [(f"{prefix}{i}", item) for i, item in enumerate(ids, 1)]
        maps[name] = dict((b, a) if canonical else (a, b) for a, b in pairs)
    if "selection" in value:
        for name in ("blocks", "links"):
            key = "blockIds" if name == "blocks" else "linkIds"
            value["selection"][key] = [maps[name][item] for item in value["selection"][key]]
    for name in ("infos", "assignments", "graphs"):
        for item in value.get(name, []):
            item["blockId"] = maps["blocks"][item["blockId"]]
    return value
