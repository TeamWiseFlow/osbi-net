"""Validated server configuration and graph admission windows."""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .channels import ChannelDefinition, default_channels, load_channels
from .prompts import ENTITY_PROMPT, RELATION_PROMPT


@dataclass(frozen=True)
class PromptSettings:
    entities: str = ENTITY_PROMPT
    relations: str = RELATION_PROMPT

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Prompt {item.name} must be a nonempty string")

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_mapping(cls, body):
        """Load extraction prompts, ignoring retired inference in legacy snapshots."""
        allowed = {item.name for item in fields(cls)}
        if not isinstance(body, dict) or set(body) - allowed - {"info", "relevance", "routing"}:
            raise ValueError("Prompt configuration must use only entities or relations")
        return cls(**{key: value for key, value in body.items() if key in allowed})

    @classmethod
    def from_file(cls, path):
        """Override selected full system prompts; all other stages retain defaults."""
        return cls.from_mapping(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))


@dataclass
class RetentionSettings:
    enabled: bool = True
    interval_seconds: int = 3600
    request_days: int = 30
    succeeded_days: int = 30
    failed_days: int = 7
    orphan_days: int = 30
    max_requests: int = 50000
    max_completed_work: int = 10000
    max_dynamic_channels: int = 200  # Legacy setting; channel creation is now administrator-only.
    batch_size: int = 200

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("Retention enabled must be boolean")
        for item in fields(self):
            if item.name != "enabled" and (type(getattr(self, item.name)) is not int or getattr(self, item.name) < 1):
                raise ValueError(f"Retention {item.name} must be a positive integer")

    @classmethod
    def from_file(cls, path):
        body = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        if not isinstance(body, dict) or set(body) - {f.name for f in fields(cls)}:
            raise ValueError("Invalid retention configuration keys")
        return cls(**body)


@dataclass
class Settings:
    data_dir: Path
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    ontology_model: str = "Qwen/Qwen3.5-27B"
    fallback_model: str = "deepseek-ai/DeepSeek-V3.2"
    concurrency: int = 3
    timeout: float = 120
    ontology_chunk_chars: int = 3000
    ontology_chunk_overlap: int = 200
    schema_path: Path | None = None
    channel_definitions: tuple[ChannelDefinition, ...] = field(default_factory=default_channels)
    processing_mode: str = "continuous"
    batch_window: str = "02:00-06:00"
    batch_timezone: str = "Asia/Shanghai"
    require_https: bool = True
    prompts: PromptSettings = field(default_factory=PromptSettings)
    enabled_schemas: tuple[str, ...] | None = None
    auth_mode: str = "local"
    control_url: str = ""
    control_key: str = ""
    retention: RetentionSettings = field(default_factory=RetentionSettings)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if not self.channel_definitions or len({c.id for c in self.channel_definitions}) != len(self.channel_definitions):
            raise ValueError("Channel definitions must be nonempty and have unique IDs")
        if self.enabled_schemas is None:
            self.enabled_schemas = tuple(c.id for c in self.channel_definitions)
        if self.auth_mode not in {"local", "supabase"}:
            raise ValueError("OSBI_AUTH_MODE must be local or supabase")
        if self.auth_mode == "supabase" and (not self.control_url or not self.control_key):
            raise ValueError("Supabase authentication requires control URL and service credentials")
        if not isinstance(self.prompts, PromptSettings):
            raise ValueError("prompts must be a PromptSettings instance")
        if not isinstance(self.retention, RetentionSettings):
            raise ValueError("retention must be a RetentionSettings instance")
        if self.processing_mode not in {"continuous", "batch"}:
            raise ValueError("OSBI_GRAPH_PROCESSING_MODE must be continuous or batch")
        self.zone = ZoneInfo(self.batch_timezone)
        parts = self.batch_window.split("-")
        if len(parts) != 2 or any(len(p) != 5 for p in parts):
            raise ValueError("OSBI_GRAPH_BATCH_WINDOW must be HH:MM-HH:MM")
        self.window_start, self.window_end = (time.fromisoformat(p) for p in parts)
        if self.window_start == self.window_end:
            raise ValueError("Batch window must have different start and end")
        if not 1 <= self.concurrency <= 32 or self.timeout <= 0:
            raise ValueError("Invalid server model concurrency/timeout")

    def graph_allowed(self, now=None):
        if self.processing_mode == "continuous":
            return True
        current = (now or datetime.now(self.zone)).astimezone(self.zone).time().replace(tzinfo=None)
        a, b = self.window_start, self.window_end
        return a <= current < b if a < b else current >= a or current < b

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.environ.get("OSBI_SERVER_DATA", "~/.osbi/server")),
            base_url=os.environ.get("OSBI_LLM_BASE_URL", "https://api.siliconflow.cn/v1"),
            api_key=os.environ.get("OSBI_LLM_API_KEY", ""),
            ontology_model=os.environ.get("OSBI_ONTOLOGY_MODEL", "Qwen/Qwen3.5-27B"),
            fallback_model=os.environ.get("OSBI_FALLBACK_MODEL", "deepseek-ai/DeepSeek-V3.2"),
            concurrency=int(os.environ.get("OSBI_MODEL_CONCURRENCY", "3")),
            timeout=float(os.environ.get("OSBI_MODEL_TIMEOUT", "120")),
            prompts=PromptSettings.from_file(os.environ["OSBI_PROMPTS_FILE"]) if os.environ.get("OSBI_PROMPTS_FILE") else PromptSettings(),
            channel_definitions=load_channels(os.environ["OSBI_CHANNELS_FILE"]) if os.environ.get("OSBI_CHANNELS_FILE") else default_channels(),
            processing_mode=os.environ.get("OSBI_GRAPH_PROCESSING_MODE", "continuous"),
            batch_window=os.environ.get("OSBI_GRAPH_BATCH_WINDOW", "02:00-06:00"),
            batch_timezone=os.environ.get("OSBI_GRAPH_BATCH_TIMEZONE", "Asia/Shanghai"),
            auth_mode=os.environ.get("OSBI_AUTH_MODE", "local"),
            control_url=os.environ.get("OSBI_CONTROL_URL", ""),
            control_key=os.environ.get("OSBI_CONTROL_KEY", ""),
            retention=RetentionSettings.from_file(os.environ["OSBI_RETENTION_FILE"]) if os.environ.get("OSBI_RETENTION_FILE") else RetentionSettings())
