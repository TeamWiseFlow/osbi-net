"""Validated server configuration and graph admission windows."""
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, time
import hashlib
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .prompts import ENTITY_PROMPT, INFO_PROMPT, RELATION_PROMPT, RELEVANCE_PROMPT, ROUTING_PROMPT


@dataclass(frozen=True)
class PromptSettings:
    relevance: str = RELEVANCE_PROMPT
    info: str = INFO_PROMPT
    entities: str = ENTITY_PROMPT
    relations: str = RELATION_PROMPT
    routing: str = ROUTING_PROMPT

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Prompt {item.name} must be a nonempty string")

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_file(cls, path):
        """Override selected full system prompts; all other stages retain defaults."""
        body = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        allowed = {item.name for item in fields(cls)}
        if not isinstance(body, dict) or set(body) - allowed:
            raise ValueError("Prompt file must be a JSON object using only relevance, info, entities, relations or routing")
        return cls(**body)


@dataclass
class Settings:
    data_dir: Path
    base_url: str = "https://api.siliconflow.cn/v1"
    api_key: str = ""
    selected_model: str = "deepseek-ai/DeepSeek-V3.2"
    performance_model: str = "Qwen/Qwen3-32B"
    ontology_model: str = "Qwen/Qwen3.5-27B"
    fallback_model: str = "deepseek-ai/DeepSeek-V3.2"
    concurrency: int = 3
    timeout: float = 120
    ontology_chunk_chars: int = 3000
    ontology_chunk_overlap: int = 200
    schema_path: Path | None = None
    schema_dir: Path | None = None
    processing_mode: str = "continuous"
    batch_window: str = "02:00-06:00"
    batch_timezone: str = "Asia/Shanghai"
    require_https: bool = True
    prompts: PromptSettings = field(default_factory=PromptSettings)

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).expanduser().resolve()
        if not isinstance(self.prompts, PromptSettings):
            raise ValueError("prompts must be a PromptSettings instance")
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
            selected_model=os.environ.get("OSBI_SELECTED_MODEL", "deepseek-ai/DeepSeek-V3.2"),
            performance_model=os.environ.get("OSBI_INFO_MODEL", "Qwen/Qwen3-32B"),
            ontology_model=os.environ.get("OSBI_ONTOLOGY_MODEL", "Qwen/Qwen3.5-27B"),
            fallback_model=os.environ.get("OSBI_FALLBACK_MODEL", "deepseek-ai/DeepSeek-V3.2"),
            concurrency=int(os.environ.get("OSBI_MODEL_CONCURRENCY", "3")),
            timeout=float(os.environ.get("OSBI_MODEL_TIMEOUT", "120")),
            prompts=PromptSettings.from_file(os.environ["OSBI_PROMPTS_FILE"]) if os.environ.get("OSBI_PROMPTS_FILE") else PromptSettings(),
            schema_dir=Path(os.environ["OSBI_SCHEMA_DIR"]) if os.environ.get("OSBI_SCHEMA_DIR") else None,
            processing_mode=os.environ.get("OSBI_GRAPH_PROCESSING_MODE", "continuous"),
            batch_window=os.environ.get("OSBI_GRAPH_BATCH_WINDOW", "02:00-06:00"),
            batch_timezone=os.environ.get("OSBI_GRAPH_BATCH_TIMEZONE", "Asia/Shanghai"))
