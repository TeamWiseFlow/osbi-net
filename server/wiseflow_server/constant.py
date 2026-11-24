"""Runtime settings. Model routing and deployment configuration are server-owned.

Deployment configuration (channels, statements, schemas, seeds, stores, node
keys) lives in ``runtime.yaml``; see ``runtime_config.py``. Environment
variables remain available as operator overrides for the model and batch knobs,
but the code no longer hardcodes a channel/schema map.
"""
from dataclasses import dataclass
from datetime import time
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from .runtime_config import ConfigError, ServerConfig, load_runtime_config


@dataclass(frozen=True)
class Settings:
    config: ServerConfig
    selected_model: str
    performance_model: str
    base_url: str
    api_key: str
    ontology_model: str
    fallback_model: str
    concurrency: int = 3
    timeout: float = 240
    ontology_chunk_chars: int = 3000
    ontology_chunk_overlap: int = 200
    ontology_batch_enabled: bool = True
    ontology_batch_time: str = '01:18'
    ontology_batch_timezone: str = 'Asia/Shanghai'
    ontology_batch_max_seconds: float = 4 * 3600
    # Legacy path overrides retained for deployments that still point the
    # default store at explicit locations; runtime.yaml takes precedence.
    schema_path: Path | None = None
    graph_path: Path | None = None
    libraries_path: Path | None = None

    @property
    def data_dir(self) -> Path:
        return self.config.data_dir

    @property
    def channels(self):
        return self.config.channels

    def channel_config(self, channel_id):
        return self.config.channel(channel_id)

    def store_for_channel(self, channel_id):
        return self.config.store_for_channel(channel_id)

    @property
    def seed_paths(self) -> tuple[Path, ...]:
        return self.config.seeds

    @property
    def config_fingerprint(self) -> str:
        return self.config.config_fingerprint

    @classmethod
    def from_env(cls) -> 'Settings':
        try:
            config = load_runtime_config()
        except ConfigError as error:
            raise ValueError(str(error)) from error
        models = config.models
        settings = cls(
            config=config,
            selected_model=os.environ.get('SELECTED_MODEL', models.selected).strip(),
            performance_model=os.environ.get('PERFORMANCE_MODEL', models.performance).strip(),
            base_url=os.environ.get('SF_BASE_URL', models.base_url).strip(),
            api_key=models.api_key,
            ontology_model=os.environ.get('ONTOLOGY_MODEL', models.ontology).strip(),
            fallback_model=os.environ.get('FALLBACK_MODEL', models.fallback).strip(),
            schema_path=None,
            graph_path=None,
            libraries_path=None,
            concurrency=int(os.environ.get('LLM_CONCURRENT_NUMBER', str(models.concurrency))),
            timeout=float(os.environ.get('WISEFLOW_LLM_TIMEOUT', str(models.timeout_seconds))),
            ontology_chunk_chars=int(os.environ.get('ONTOLOGY_CHUNK_CHARS',
                                                     str(models.ontology_chunk_chars))),
            ontology_chunk_overlap=int(os.environ.get('ONTOLOGY_CHUNK_OVERLAP',
                                                       str(models.ontology_chunk_overlap))),
            ontology_batch_enabled=(os.environ.get('ONTOLOGY_BATCH_ENABLED',
                                                   '1' if config.batch.enabled else '0') != '0'),
            ontology_batch_time=os.environ.get('ONTOLOGY_BATCH_TIME', config.batch.window_start),
            ontology_batch_timezone=os.environ.get('ONTOLOGY_BATCH_TIMEZONE', config.batch.timezone),
            ontology_batch_max_seconds=float(os.environ.get('ONTOLOGY_BATCH_MAX_SECONDS',
                                                             str(config.batch.max_seconds)))
        )
        if not all((settings.selected_model, settings.performance_model, settings.ontology_model,
                    settings.fallback_model, settings.api_key)):
            raise ValueError('Configure model names and WISEFLOW_LLM_API_KEY (or SF_KEY)')
        if settings.concurrency < 1 or settings.timeout <= 0:
            raise ValueError('LLM concurrency and timeout must be positive')
        if not 0 <= settings.ontology_chunk_overlap < settings.ontology_chunk_chars:
            raise ValueError('Ontology overlap must be nonnegative and smaller than chunk size')
        if len(settings.ontology_batch_time) != 5:
            raise ValueError('ONTOLOGY_BATCH_TIME must use HH:MM')
        time.fromisoformat(settings.ontology_batch_time)
        ZoneInfo(settings.ontology_batch_timezone)
        if settings.ontology_batch_max_seconds <= 0:
            raise ValueError('ONTOLOGY_BATCH_MAX_SECONDS must be positive')
        return settings
