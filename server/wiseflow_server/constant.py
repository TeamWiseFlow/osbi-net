"""Server-owned model routing. Task-level model switches do not override it."""
from dataclasses import dataclass
import os
from pathlib import Path
from datetime import time
from zoneinfo import ZoneInfo

import model_config as defaults


@dataclass(frozen=True)
class Settings:
    selected_model: str = defaults.SELECTED_MODEL
    performance_model: str = defaults.PERFORMANCE_MODEL
    base_url: str = defaults.SF_BASE_URL
    api_key: str = ''
    concurrency: int = 3
    timeout: float = 240
    ontology_model: str = defaults.ONTOLOGY_MODEL
    fallback_model: str = defaults.FALLBACK_MODEL
    schema_path: Path | None = None
    graph_path: Path | None = None
    ontology_chunk_chars: int = 3000
    ontology_chunk_overlap: int = 200
    ontology_batch_enabled: bool = True
    ontology_batch_time: str = '03:00'
    ontology_batch_timezone: str = 'Asia/Shanghai'
    ontology_batch_max_seconds: float = 4 * 3600
    libraries_path: Path | None = None

    @classmethod
    def from_env(cls):
        settings = cls(
            selected_model=os.environ.get('SELECTED_MODEL', defaults.SELECTED_MODEL).strip(),
            performance_model=os.environ.get('PERFORMANCE_MODEL', defaults.PERFORMANCE_MODEL).strip(),
            base_url=defaults.credentials()[0],
            api_key=defaults.credentials()[1],
            ontology_model=os.environ.get('ONTOLOGY_MODEL', defaults.ONTOLOGY_MODEL).strip(),
            fallback_model=os.environ.get('FALLBACK_MODEL', defaults.FALLBACK_MODEL).strip(),
            schema_path=Path(os.environ['ONTOLOGY_SCHEMA_PATH']).expanduser().resolve() if os.environ.get('ONTOLOGY_SCHEMA_PATH') else None,
            graph_path=Path(os.environ['ONTOLOGY_GRAPH_PATH']).expanduser().resolve() if os.environ.get('ONTOLOGY_GRAPH_PATH') else None,
            libraries_path=Path(os.environ['ONTOLOGY_LIBRARIES_CONFIG']).expanduser().resolve() if os.environ.get('ONTOLOGY_LIBRARIES_CONFIG') else None,
            concurrency=int(os.environ.get('LLM_CONCURRENT_NUMBER', '3')),
            timeout=float(os.environ.get('WISEFLOW_LLM_TIMEOUT', '240')),
            ontology_chunk_chars=int(os.environ.get('ONTOLOGY_CHUNK_CHARS', '3000')),
            ontology_chunk_overlap=int(os.environ.get('ONTOLOGY_CHUNK_OVERLAP', '200')),
            ontology_batch_enabled=os.environ.get('ONTOLOGY_BATCH_ENABLED', '1') != '0',
            ontology_batch_time=os.environ.get('ONTOLOGY_BATCH_TIME', '03:00'),
            ontology_batch_timezone=os.environ.get('ONTOLOGY_BATCH_TIMEZONE', 'Asia/Shanghai'),
            ontology_batch_max_seconds=float(os.environ.get('ONTOLOGY_BATCH_MAX_SECONDS', '14400')))
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
