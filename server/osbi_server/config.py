"""Validated server configuration and graph admission windows."""
from dataclasses import dataclass
from datetime import datetime, time
import os
from pathlib import Path
from zoneinfo import ZoneInfo


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

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).expanduser().resolve()
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
            schema_dir=Path(os.environ["OSBI_SCHEMA_DIR"]) if os.environ.get("OSBI_SCHEMA_DIR") else None,
            processing_mode=os.environ.get("OSBI_GRAPH_PROCESSING_MODE", "continuous"),
            batch_window=os.environ.get("OSBI_GRAPH_BATCH_WINDOW", "02:00-06:00"),
            batch_timezone=os.environ.get("OSBI_GRAPH_BATCH_TIMEZONE", "Asia/Shanghai"))
