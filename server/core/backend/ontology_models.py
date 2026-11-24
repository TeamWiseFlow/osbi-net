"""Public query contract shared with the private graph worker."""
from pydantic import BaseModel, ConfigDict, Field


class OntologyQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source: str = Field(min_length=1, max_length=500)
    target: str | None = Field(default=None, min_length=1, max_length=500)
    maxHops: int = Field(default=4, ge=1, le=4)
    maxPaths: int = Field(default=3, ge=1, le=10)
    libraryId: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')


class OntologyBatchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    maxBlocks: int | None = Field(default=None, ge=1)
    reprocess: bool = False
    libraryId: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    createdFrom: str | None = Field(default=None, min_length=10, max_length=64)
    createdTo: str | None = Field(default=None, min_length=10, max_length=64)
