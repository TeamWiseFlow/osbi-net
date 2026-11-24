"""Versioned HTTP contract; metadata and URL maps never enter model inputs.

Protocol v4 removes the caller-owned ``focus``. A node names a server-side
``channel``; the server resolves its statement, schema, rules and date window.
"""
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = 'material-extract/v4'


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Block(StrictModel):
    id: str = Field(pattern=r'^B[1-9]\d*$')
    text: str = Field(min_length=1)


class Link(StrictModel):
    id: str = Field(pattern=r'^L[1-9]\d*$')
    text: str = Field(min_length=1)
    context: str | None = None
    role: Literal["content", "navigation", "unknown"]


class SelectorInput(StrictModel):
    title: str
    blocks: list[Block] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_ids(self):
        for records in (self.blocks, self.links):
            if len({r.id for r in records}) != len(records) or any(not r.text.strip() for r in records):
                raise ValueError('Candidate IDs must be unique and text nonempty')
        return self


class ExtractRequest(StrictModel):
    protocolVersion: Literal['material-extract/v4'] = PROTOCOL_VERSION
    # Channel is required and server-owned; every channel is open to every
    # authenticated node. Node identity only feeds provenance and metrics.
    channel: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    nodeId: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    materialId: str = Field(pattern=r'^[a-f0-9]{64}$')
    input: SelectorInput
    meta: dict[str, Any] = Field(default_factory=dict)
    mode: Literal['both', 'only_link'] = 'both'


class Selection(StrictModel):
    blockIds: list[str]
    linkIds: list[str]


class StageError(StrictModel):
    stage: Literal['selection', 'ontology_queue', 'date_gate']
    blockId: str | None = None
    message: str


class DroppedRelationSample(StrictModel):
    """One discarded relation, retained in the batch report for diagnosis."""
    reason: str
    stage: str
    subject: str
    predicate: str
    rawPredicate: str | None = None
    object: str
    subjectLabels: list[str] = Field(default_factory=list)
    objectLabels: list[str] = Field(default_factory=list)
    domain: list[str] = Field(default_factory=list)
    range: list[str] = Field(default_factory=list)


class OntologyReceipt(StrictModel):
    blockId: str
    status: Literal['stored', 'empty', 'failed']
    id: str | None = Field(default=None, pattern=r'^o_[a-f0-9]{64}$')
    cached: bool = False
    entityCount: int = Field(default=0, ge=0)
    relationCount: int = Field(default=0, ge=0)
    models: list[str] = Field(default_factory=list)
    droppedSamples: list[DroppedRelationSample] = Field(default_factory=list)
    droppedCounts: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode='after')
    def consistent_result(self):
        if self.status != 'failed' and (not self.id or not self.models):
            raise ValueError('Completed ontology receipts require an ID and actual model evidence')
        if self.status == 'empty' and (self.entityCount or self.relationCount):
            raise ValueError('Empty ontology receipts cannot contain entities or relations')
        if self.status == 'stored' and self.entityCount == 0:
            raise ValueError('Stored ontology receipts require entities')
        return self


class OntologyJobReceipt(StrictModel):
    blockId: str
    id: str = Field(pattern=r'^oq_[a-f0-9]{64}$')
    status: Literal['pending', 'done']
    cached: bool = False


class ExtractResponse(StrictModel):
    protocolVersion: Literal['material-extract/v4'] = PROTOCOL_VERSION
    materialId: str
    selection: Selection = Field(default_factory=lambda: Selection(blockIds=[], linkIds=[]))
    modelCalls: int = Field(default=0, ge=0)
    ontologyJobs: list[OntologyJobReceipt] = Field(default_factory=list)
    errors: list[StageError] = Field(default_factory=list)


class BatchRequest(StrictModel):
    maxBlocks: int | None = Field(default=None, ge=1)
    reprocess: bool = False
    libraryId: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    # Date-only values use Asia/Shanghai; full ISO values keep their offset.
    createdFrom: str | None = Field(default=None, min_length=10, max_length=64)
    createdTo: str | None = Field(default=None, min_length=10, max_length=64)
