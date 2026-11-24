"""Deployment configuration for the OSBI extraction server.

Runtime configuration is externalized into ``runtime.yaml`` (see
``server/config/example/runtime.yaml``). The code repository never carries a
production channel catalogue, board title or city name: channels, statements,
schemas, seeds, stores and node identities all come from this file.

Startup is fail-fast: an invalid file raises ``ConfigError`` before the server
opens any store or model connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

import yaml

PROTOCOL_VERSION = 'material-extract/v4'
ID_PATTERN = re.compile(r'^[a-z][a-z0-9_-]{0,63}$')
SHA256_PATTERN = re.compile(r'^(?:sha256:)?[a-fA-F0-9]{64}$')

APP_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_ENV = 'OSBI_CONFIG_FILE'
DEFAULT_CONFIG_DIR_ENV = 'OSBI_CONFIG_DIR'


class ConfigError(ValueError):
    """A deployment configuration error; the process must not start."""


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ConfigError(f'{name} must be a mapping')
    return dict(value)


def _text(value, name, *, allow_empty=False):
    if not isinstance(value, str):
        raise ConfigError(f'{name} must be a string')
    value = value.strip()
    if not value and not allow_empty:
        raise ConfigError(f'{name} must not be empty')
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f'{name} must be a positive integer')
    return value


def _id(value, name):
    value = _text(value, name)
    if not ID_PATTERN.fullmatch(value):
        raise ConfigError(f'{name} must match {ID_PATTERN.pattern}')
    return value


def _clock(value, name):
    value = _text(value, name)
    if len(value) != 5:
        raise ConfigError(f'{name} must use HH:MM')
    try:
        time.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f'{name} must use HH:MM') from error
    return value


def _path(value, base: Path, name, *, must_exist=False):
    raw = _text(value, name)
    candidate = Path(raw).expanduser()
    candidate = candidate if candidate.is_absolute() else (base / candidate)
    candidate = candidate.resolve()
    if must_exist and not candidate.exists():
        raise ConfigError(f'{name} does not exist: {candidate}')
    return candidate


@dataclass(frozen=True)
class ModelConfig:
    base_url: str
    api_key_env: str
    selected: str
    performance: str
    ontology: str
    fallback: str
    concurrency: int = 3
    timeout_seconds: float = 240
    ontology_chunk_chars: int = 3000
    ontology_chunk_overlap: int = 200

    @property
    def api_key(self) -> str:
        names = [self.api_key_env, 'WISEFLOW_LLM_API_KEY', 'SF_KEY']
        for name in names:
            if name and (value := os.environ.get(name, '').strip()):
                return value
        return ''


@dataclass(frozen=True)
class BatchConfig:
    enabled: bool = True
    window_start: str = '01:18'
    window_end: str = '06:18'
    timezone: str = 'Asia/Shanghai'
    max_seconds: float = 4 * 3600


@dataclass(frozen=True)
class StoreConfig:
    id: str
    title: str
    schema_path: Path
    graph_path: Path
    schema_fingerprint: str

    def prompt_title(self) -> str:
        return self.title or self.id


@dataclass(frozen=True)
class ChannelConfig:
    id: str
    title: str
    schema_ref: str
    statement: str
    date_window_days: int | None = None
    date_field: str = 'publishedDate'
    rules: Mapping[str, Any] = field(default_factory=dict)

    @property
    def statement_fingerprint(self) -> str:
        return hashlib.sha256(self.statement.encode()).hexdigest()

    @property
    def rules_fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(dict(self.rules), sort_keys=True,
                                         ensure_ascii=False).encode()).hexdigest()

    def rule(self, key, default=None):
        return self.rules.get(key, default)


@dataclass(frozen=True)
class NodeConfig:
    id: str
    name: str


@dataclass(frozen=True)
class NodeKeyRegistry:
    """Hashed node keys. Plaintext keys never live in repository or config files."""

    keys: Mapping[str, str]
    mode: str = 'node-key'
    node_aliases: Mapping[str, str] = field(default_factory=dict)
    data_scope: str = 'net'

    @property
    def enabled(self) -> bool:
        return self.mode == 'node-key'

    def authenticate(self, token: str | None) -> str | None:
        if not token or not self.enabled:
            return None
        digest = hashlib.sha256(token.strip().encode()).hexdigest()
        return self.keys.get(digest)


@dataclass(frozen=True)
class ServerConfig:
    protocol: str
    data_dir: Path
    models: ModelConfig
    batch: BatchConfig
    auth: NodeKeyRegistry
    stores: Mapping[str, StoreConfig]
    channels: Mapping[str, ChannelConfig]
    nodes: Mapping[str, NodeConfig]
    seeds: tuple[Path, ...] = ()
    default_store: str = 'policy'
    config_fingerprint: str = ''

    def channel(self, channel_id: str) -> ChannelConfig:
        try:
            return self.channels[channel_id]
        except KeyError as error:
            raise ValueError(f'Unknown channel: {channel_id}') from error

    def store_for_channel(self, channel_id: str) -> StoreConfig:
        return self.stores[self.channel(channel_id).schema_ref]

    def store(self, store_id: str | None = None) -> StoreConfig:
        key = store_id or self.default_store
        try:
            return self.stores[key]
        except KeyError as error:
            raise ValueError(f'Unknown ontology store: {key}') from error

    def channels_for_store(self, store_id: str) -> tuple[ChannelConfig, ...]:
        return tuple(channel for channel in self.channels.values()
                     if channel.schema_ref == store_id)


class StoreRegistry:
    """Schema stores backing ontology graphs (one graph per schemaRef).

    Channels sharing a schemaRef share this store; the channel remains a
    provenance/query key. The ``libraries`` view keeps the historical graph
    viewer/API shape without retaining any focus routing.
    """

    def __init__(self, stores: Mapping[str, StoreConfig], default_id: str):
        self.stores = dict(stores)
        self.default_id = default_id
        self.libraries = {key: dict(title=store.title, schema_path=store.schema_path,
                                    channels=[])
                          for key, store in self.stores.items()}

    def attach_channels(self, channels: Mapping[str, ChannelConfig]) -> 'StoreRegistry':
        for channel in channels.values():
            self.libraries[channel.schema_ref]['channels'].append(channel.id)
        return self

    def resolve(self, store_id: str | None = None, *, focus_id=None) -> str:
        if focus_id is not None:
            raise ValueError('Focus routing is removed; select a channel instead')
        key = store_id or self.default_id
        if key not in self.stores:
            raise ValueError(f'Unknown ontology store: {key}')
        return key

    def store(self, store_id: str | None = None) -> StoreConfig:
        return self.stores[self.resolve(store_id)]


def config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if value := os.environ.get(DEFAULT_CONFIG_ENV, '').strip():
        return Path(value).expanduser().resolve()
    if directory := os.environ.get(DEFAULT_CONFIG_DIR_ENV, '').strip():
        return (Path(directory).expanduser() / 'runtime.yaml').resolve()
    raise ConfigError(f'Set {DEFAULT_CONFIG_ENV} or {DEFAULT_CONFIG_DIR_ENV} to a runtime.yaml')


def _load_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except OSError as error:
        raise ConfigError(f'Cannot read runtime configuration: {path}') from error
    except yaml.YAMLError as error:
        raise ConfigError(f'Invalid YAML in {path}: {error}') from error
    if not isinstance(data, Mapping):
        raise ConfigError('runtime.yaml must contain a mapping')
    return dict(data)


def _models(value) -> ModelConfig:
    data = _mapping(value, 'server.models')
    config = ModelConfig(
        base_url=_text(data.get('baseUrl'), 'server.models.baseUrl'),
        api_key_env=_text(data.get('apiKeyEnv', 'OSBI_LLM_API_KEY'),
                          'server.models.apiKeyEnv'),
        selected=_text(data.get('selected'), 'server.models.selected'),
        performance=_text(data.get('performance'), 'server.models.performance'),
        ontology=_text(data.get('ontology'), 'server.models.ontology'),
        fallback=_text(data.get('fallback'), 'server.models.fallback'),
        concurrency=_positive_int(data.get('concurrency', 3), 'server.models.concurrency'),
        timeout_seconds=float(data.get('timeoutSeconds', 240)),
        ontology_chunk_chars=_positive_int(data.get('ontologyChunkChars', 3000),
                                           'server.models.ontologyChunkChars'),
        ontology_chunk_overlap=int(data.get('ontologyChunkOverlap', 200)),
    )
    if config.timeout_seconds <= 0:
        raise ConfigError('server.models.timeoutSeconds must be positive')
    if not 0 <= config.ontology_chunk_overlap < config.ontology_chunk_chars:
        raise ConfigError('server.models.ontologyChunkOverlap must be smaller than ontologyChunkChars')
    return config


def _batch(value) -> BatchConfig:
    data = _mapping(value, 'server.batch')
    config = BatchConfig(
        enabled=bool(data.get('enabled', True)),
        window_start=_clock(data.get('windowStart', '01:18'), 'server.batch.windowStart'),
        window_end=_clock(data.get('windowEnd', '06:18'), 'server.batch.windowEnd'),
        timezone=_text(data.get('timezone', 'Asia/Shanghai'), 'server.batch.timezone'),
        max_seconds=float(data.get('maxSeconds', 4 * 3600)),
    )
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(config.timezone)
    except Exception as error:
        raise ConfigError(f'Unknown server.batch.timezone: {config.timezone}') from error
    if config.max_seconds <= 0:
        raise ConfigError('server.batch.maxSeconds must be positive')
    return config


def _stores(value, base: Path, data_dir: Path) -> dict[str, StoreConfig]:
    data = _mapping(value, 'server.stores')
    if not data:
        raise ConfigError('server.stores must contain at least one schema store')
    stores = {}
    for key, entry in data.items():
        store_id = _id(key, 'server.stores key')
        settings = _mapping(entry, f'server.stores.{store_id}')
        schema_path = _path(settings.get('schemaPath'), base,
                            f'server.stores.{store_id}.schemaPath', must_exist=True)
        graph_path = (_path(settings['path'], base, f'server.stores.{store_id}.path')
                      if settings.get('path') else (data_dir / store_id).resolve())
        stores[store_id] = StoreConfig(
            id=store_id,
            title=_text(settings.get('title', store_id), f'server.stores.{store_id}.title',
                        allow_empty=True),
            schema_path=schema_path,
            graph_path=graph_path,
            schema_fingerprint=hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
    return stores


def _nodes(value) -> dict[str, NodeConfig]:
    if not isinstance(value, list) or not value:
        raise ConfigError('nodes must be a non-empty list')
    nodes = {}
    for index, entry in enumerate(value):
        data = _mapping(entry, f'nodes[{index}]')
        node_id = _id(data.get('id'), f'nodes[{index}].id')
        if node_id in nodes:
            raise ConfigError(f'Duplicate node id: {node_id}')
        nodes[node_id] = NodeConfig(id=node_id,
                                    name=_text(data.get('name', node_id), f'nodes[{index}].name',
                                               allow_empty=True))
    return nodes


def _auth(value, base: Path, nodes: Mapping[str, NodeConfig]) -> NodeKeyRegistry:
    data = _mapping(value or {}, 'server.auth')
    mode = _text(data.get('mode', 'node-key'), 'server.auth.mode')
    if mode not in {'node-key', 'none', 'supabase'}:
        raise ConfigError("server.auth.mode must be 'node-key', 'supabase' or 'none'")
    if mode == 'supabase':
        aliases = _mapping(data.get('nodeAliases', {}), 'server.auth.nodeAliases')
        for customer, node in aliases.items():
            _text(customer, 'node alias customer')
            if node not in nodes:
                raise ConfigError(f'Node alias references unknown node: {node}')
        scope = data.get('dataScope', 'net')
        if scope not in {'net', 'shared'}:
            raise ConfigError("server.auth.dataScope must be 'net' or 'shared'")
        return NodeKeyRegistry(keys={}, mode=mode, node_aliases=aliases, data_scope=scope)
    if mode == 'none':
        return NodeKeyRegistry(keys={}, mode='none')
    keys = {}
    key_path = data.get('keysFile')
    if not key_path:
        raise ConfigError('server.auth.keysFile is required for node-key mode')
    file = _path(key_path, base, 'server.auth.keysFile', must_exist=True)
    try:
        payload = json.loads(file.read_text(encoding='utf-8'))
    except OSError as error:
        raise ConfigError(f'Cannot read node keys file: {file}') from error
    except ValueError as error:
        raise ConfigError(f'Invalid node keys JSON: {file}: {error}') from error
    if isinstance(payload, Mapping):
        entries = payload.get('keys', payload)
        if not isinstance(entries, Mapping):
            raise ConfigError('nodes.keys.json must map hashed keys to node IDs')
        raw = list(entries.items())
    elif isinstance(payload, list):
        raw = []
        for index, item in enumerate(payload):
            item = _mapping(item, f'node key[{index}]')
            raw.append((item.get('sha256'), item.get('nodeId')))
    else:
        raise ConfigError('nodes.keys.json must be an object or a list')
    for hashed, node_id in raw:
        hashed = _text(hashed, 'node key hash')
        if not SHA256_PATTERN.fullmatch(hashed):
            raise ConfigError(f'Node key entries must be SHA-256 hashes: {hashed[:20]}...')
        digest = hashed.removeprefix('sha256:').lower()
        node_id = _id(node_id, 'node key nodeId')
        if node_id not in nodes:
            raise ConfigError(f'Node key references unknown node: {node_id}')
        if digest in keys:
            raise ConfigError('Duplicate node key hash')
        keys[digest] = node_id
    return NodeKeyRegistry(keys=keys, mode='node-key')


def _channels(value, stores: Mapping[str, StoreConfig]) -> dict[str, ChannelConfig]:
    if not isinstance(value, list) or not value:
        raise ConfigError('channels must be a non-empty list')
    channels = {}
    for index, entry in enumerate(value):
        data = _mapping(entry, f'channels[{index}]')
        channel_id = _id(data.get('id'), f'channels[{index}].id')
        if channel_id in channels:
            raise ConfigError(f'Duplicate channel id: {channel_id}')
        schema_ref = _id(data.get('schemaRef'), f'channels[{index}].schemaRef')
        if schema_ref not in stores:
            raise ConfigError(f'Channel {channel_id} references unknown store: {schema_ref}')
        window = data.get('dateWindowDays')
        if window is not None:
            try:
                window = _positive_int(window, f'channels[{index}].dateWindowDays')
            except ConfigError:
                raise
        rules = data.get('rules') or {}
        channels[channel_id] = ChannelConfig(
            id=channel_id,
            title=_text(data.get('title'), f'channels[{index}].title'),
            schema_ref=schema_ref,
            statement=_text(data.get('statement'), f'channels[{index}].statement'),
            date_window_days=window,
            date_field=_text(data.get('dateField', 'publishedDate'),
                             f'channels[{index}].dateField', allow_empty=False),
            rules=_mapping(rules, f'channels[{index}].rules'),
        )
    return channels


def _seeds(value, base: Path) -> tuple[Path, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError('server.seeds must be a list of paths')
    return tuple(_path(item, base, f'server.seeds[{index}]', must_exist=True)
                 for index, item in enumerate(value))


def _fingerprint(models: ModelConfig, batch: BatchConfig,
                 stores: Mapping[str, StoreConfig],
                 channels: Mapping[str, ChannelConfig], seeds=()) -> str:
    identity = dict(
        protocol=PROTOCOL_VERSION,
        models=dict(selected=models.selected, performance=models.performance,
                    ontology=models.ontology, fallback=models.fallback,
                    ontologyChunkChars=models.ontology_chunk_chars,
                    ontologyChunkOverlap=models.ontology_chunk_overlap),
        batch=dict(enabled=batch.enabled, windowStart=batch.window_start,
                   windowEnd=batch.window_end, timezone=batch.timezone),
        stores={key: store.schema_fingerprint for key, store in stores.items()},
        seeds=[hashlib.sha256(path.read_bytes()).hexdigest() for path in seeds],
        channels={key: dict(title=channel.title, schemaRef=channel.schema_ref,
                            statement=channel.statement,
                            dateWindowDays=channel.date_window_days,
                            dateField=channel.date_field,
                            rules=dict(channel.rules))
                  for key, channel in channels.items()},
    )
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_runtime_config(explicit: str | Path | None = None) -> ServerConfig:
    path = config_path(explicit)
    data = _load_yaml(path)
    server = _mapping(data.get('server'), 'server')
    base = path.parent
    protocol = _text(server.get('protocol', PROTOCOL_VERSION), 'server.protocol')
    if protocol != PROTOCOL_VERSION:
        raise ConfigError(f'server.protocol must be {PROTOCOL_VERSION}')
    data_dir = _path(server.get('dataDir', './data'), base, 'server.dataDir')
    stores = _stores(server.get('stores'), base, data_dir)
    default_store = _text(server.get('defaultStore', 'policy'), 'server.defaultStore')
    if default_store not in stores:
        raise ConfigError(f'server.defaultStore references unknown store: {default_store}')
    models = _models(server.get('models'))
    batch = _batch(server.get('batch'))
    nodes = _nodes(data.get('nodes'))
    auth = _auth(server.get('auth'), base, nodes)
    channels = _channels(data.get('channels'), stores)
    seeds = _seeds(server.get('seeds'), base)
    return ServerConfig(
        protocol=protocol,
        data_dir=data_dir,
        models=models,
        batch=batch,
        auth=auth,
        stores=stores,
        channels=channels,
        nodes=nodes,
        seeds=seeds,
        default_store=default_store,
        config_fingerprint=_fingerprint(models, batch, stores, channels, seeds),
    )
