"""Awada v2 content objects, ported from midplatform/awada_bot/schemas.py."""
from typing import Any, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class TextObject(BaseModel):
    type: Literal["text"] = "text"
    text: str


class MediaObject(BaseModel):
    file_path: Optional[str] = None  # local engine output; serialized to base64 by transport
    file_url: Optional[str] = None
    file_id: Optional[str] = None
    file_name: Optional[str] = None
    base64: Optional[str] = None
    provider_data: dict[str, Any] = Field(default_factory=dict)
    duration: Optional[float] = None


class ImageObject(MediaObject):
    type: Literal["image"] = "image"


class AudioObject(MediaObject):
    type: Literal["audio"] = "audio"


class FileObject(MediaObject):
    type: Literal["file"] = "file"


class VideoObject(MediaObject):
    type: Literal["video"] = "video"


class ProviderObject(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["provider"] = "provider"
    provider_type: Optional[str] = None
    data: Any = None


ContentObject = Union[TextObject, ImageObject, AudioObject, FileObject, VideoObject, ProviderObject]


class InboundMeta(BaseModel):
    model_config = ConfigDict(extra="allow")
    platform: Optional[str] = None
    tenant_id: Optional[str] = None
    channel_id: Optional[str] = None
    lane: Optional[str] = None
    actor_type: Optional[str] = None
    user_id_external: Optional[str] = None
    conversation_id: Optional[str] = None
    chat_id: Optional[str] = None
    source_message_id: Optional[str] = None
    source_event_id: Optional[str] = None
    raw_ref: Optional[str] = None


class InboundEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: int = 1
    event_id: str
    type: str
    timestamp: int
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None
    meta: InboundMeta
    payload: Union[ContentObject, list[ContentObject]]
