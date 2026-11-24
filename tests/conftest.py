"""Isolated fixtures for the material-extract/v4 contract; no production credentials."""
import hashlib
import json

from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from wiseflow_server.models import (ExtractRequest, ExtractResponse,
                                    OntologyJobReceipt, Selection)

# The legacy osbi_server was replaced by wiseflow_server (synchronous extract,
# nightly ontology batch). These suites target the removed async job protocol
# and stay ignored until they are ported to the new server.
collect_ignore = [
    "test_backup.py",
    "test_evidence_query.py",
    "test_material_lifecycle.py",
    "test_multi_net.py",
    "test_schema_review.py",
    "test_server.py",
    "test_server_prompts.py",
    "test_tls_e2e.py",
]


def _hex(value):
    return hashlib.sha256(value.encode()).hexdigest()


def extract_app(handler=None):
    """In-process v4 extract endpoint enforcing the real server contract.

    The default handler selects every candidate and returns pending ontology
    receipts (the nightly batch owns graph extraction). A custom handler
    receives the validated request and may return a dict.
    """
    app = FastAPI()
    app.state.requests = []

    @app.post("/v1/materials/extract")
    async def extract(body: dict):
        try:
            request = ExtractRequest.model_validate(body)
        except ValidationError as error:
            raise HTTPException(422, detail=error.errors()) from error
        app.state.requests.append(json.loads(request.model_dump_json()))
        if handler is not None:
            return handler(request)
        return ExtractResponse(
            materialId=request.materialId,
            selection=Selection(blockIds=[b.id for b in request.input.blocks],
                                linkIds=[l.id for l in request.input.links]),
            modelCalls=1,
            ontologyJobs=[OntologyJobReceipt(blockId=b.id, status="pending",
                                             id="oq_" + _hex(b.text + request.channel))
                          for b in request.input.blocks],
        ).model_dump()

    return app


def evidence_item(text="原文证据。", title="社区治理观察", url="https://example.org/story",
                  published="2026-11-02", entities=(), relations=(), quality_flags=(),
                  boards=("社区治理",)):
    """One wiseflow evidence item (entities carry no ancestors)."""
    return {"text": text, "title": title, "publishedDate": published,
            "channel": "community", "board": "社区治理",
            "boards": list(boards), "contentTypes": ["news"],
            "qualityFlags": list(quality_flags),
            "entityCount": len(entities), "relationCount": len(relations),
            "sources": [{"title": title, "meta": {
                "requestedUrl": url, "finalUrl": url, "canonicalUrl": url,
                "publisher": "示例发布", "publishedAt": published},
                "url": url, "publisher": "示例发布", "publishedAt": published,
                "nodeId": "linfen-shanghai"}],
            "entities": list(entities), "relations": list(relations)}
