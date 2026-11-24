"""Public transport contracts without domain extraction logic."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "osbi-material/v1"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChannelRef(StrictModel):
    """Server-owned channel identity; nodes never carry a relevance statement."""
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(default="", max_length=500)


class NodeIdentity(StrictModel):
    """Authenticated node identity (derived from its key, not the request body)."""
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(default="", max_length=500)




class Block(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=24000)


class Link(StrictModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=1000)
    url: str = Field(min_length=1, max_length=4000)
    context: str | None = Field(default=None, max_length=2000)


class SourceMeta(StrictModel):
    requestedUrl: str = Field(min_length=1, max_length=4000)
    finalUrl: str = Field(default="", max_length=4000)
    canonicalUrl: str = Field(default="", max_length=4000)
    author: str = Field(default="", max_length=500)
    publisher: str = Field(default="", max_length=500)
    publishedAt: str = Field(default="", max_length=100)
    capturedAt: str = Field(default="", max_length=100)
    adapter: str = Field(default="browser", max_length=100)
    textOnly: bool = True
    mediaPending: bool = False


class Material(StrictModel):
    protocolVersion: Literal["osbi-material/v1"] = PROTOCOL_VERSION
    clientId: str = Field(min_length=1, max_length=100)
    requestId: str = Field(min_length=1, max_length=100)
    materialHash: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    title: str = Field(default="", max_length=2000)
    # material-extract/v4: the channel is the content-identity dimension.
    channel: str = Field(min_length=1, max_length=64,
                         pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    blocks: list[Block] = Field(default_factory=list, max_length=64)
    links: list[Link] = Field(default_factory=list, max_length=200)
    meta: SourceMeta
    channelHint: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_content(self):
        from .urls import canonical_url
        if not canonical_url(self.meta.requestedUrl):
            raise ValueError("A source HTTP(S) URL is required")
        if not self.blocks and not self.links:
            raise ValueError("At least one original block or candidate link is required")
        for items in (self.blocks, self.links):
            if len({x.id for x in items}) != len(items):
                raise ValueError("Block/link IDs must be unique")
        if any(not b.text.strip() for b in self.blocks):
            raise ValueError("Original blocks cannot be blank")
        if sum(len(b.text) for b in self.blocks) > 240000:
            raise ValueError("Material exceeds the total text limit")
        if any(not canonical_url(x.url) for x in self.links):
            raise ValueError("Candidate links require HTTP(S) URLs")
        return self


class Selection(StrictModel):
    blockIds: list[str] = Field(default_factory=list)
    linkIds: list[str] = Field(default_factory=list)


class GraphQuery(StrictModel):
    source: str = Field(min_length=1, max_length=500)
    target: str | None = Field(default=None, max_length=500)
    depth: int = Field(default=1, ge=1, le=4)
    limit: int = Field(default=200, ge=1, le=1000)


class EvidenceQuery(StrictModel):
    sourceScope: Literal["all", "own"] = "all"
    fromDate: str = ""
    toDate: str = ""
    territoryPreference: list[str] = Field(default_factory=list, max_length=20)
    territoryMode: Literal["prefer", "only"] = "prefer"
    text: str = Field(default="", max_length=1000)
    entityUris: list[str] = Field(default_factory=list, max_length=500)
    cursor: str = Field(default="", pattern=r"^(?:[0-9a-f]{64})?$")
    asOf: float | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=200)

    @model_validator(mode="after")
    def validate_dates(self):
        from datetime import date
        for value in (self.fromDate, self.toDate):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Dates must use YYYY-MM-DD")
        if self.fromDate and self.toDate and self.fromDate > self.toDate:
            raise ValueError("fromDate must not follow toDate")
        return self
