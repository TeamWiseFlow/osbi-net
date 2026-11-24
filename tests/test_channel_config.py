"""Runtime configuration: channel catalogue, schema stores, node keys."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
import yaml

from wiseflow_server.runtime_config import ConfigError, load_runtime_config

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'server' / 'config' / 'example' / 'runtime.yaml'
PACKAGE = ROOT / 'server' / 'wiseflow_server' / 'schemas'


def _write_config(tmp_path, mutate=None, keys=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load(EXAMPLE.read_text(encoding='utf-8'))
    data['server']['dataDir'] = str(tmp_path / 'data')
    for entry in data['server']['stores'].values():
        name = Path(entry['schemaPath']).name
        entry['schemaPath'] = str(PACKAGE / name)
    data['server']['seeds'] = [str(PACKAGE / 'seeds_shanghai.ttl')]
    keys = keys if keys is not None else {}
    keys_file = tmp_path / 'nodes.keys.json'
    keys_file.write_text(json.dumps({'keys': keys}), encoding='utf-8')
    data['server']['auth']['keysFile'] = str(keys_file)
    if mutate:
        mutate(data)
    path = tmp_path / 'runtime.yaml'
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
    return path


def test_example_config_loads_the_seven_channel_catalogue():
    config = load_runtime_config(EXAMPLE)
    assert list(config.channels) == ['party-building', 'economy', 'community', 'digital',
                                     'policy-latest', 'stability', 'production-safety']
    assert config.channels['party-building'].schema_ref == 'policy'
    assert config.channels['economy'].schema_ref == 'economy'
    assert config.channels['policy-latest'].date_window_days == 3
    assert config.channels['policy-latest'].date_field == 'publishedDate'
    assert config.channels['community'].date_window_days is None
    assert config.channels['stability'].id != config.channels['production-safety'].id
    assert set(config.stores) == {'policy', 'economy'}
    assert [node.id for node in config.nodes.values()] == ['linfen-shanghai', 'shizishan-chengdu']
    assert config.protocol == 'material-extract/v4'
    assert config.config_fingerprint


def test_config_fingerprint_tracks_channel_statement(tmp_path):
    first = _write_config(tmp_path / 'a')
    second_dir = tmp_path / 'b'
    second_dir.mkdir()

    def mutate(data):
        data['channels'][0]['statement'] += '（口径调整）'

    second = _write_config(second_dir, mutate)
    assert load_runtime_config(first).config_fingerprint != load_runtime_config(second).config_fingerprint


def test_duplicate_channel_is_rejected(tmp_path):
    def mutate(data):
        data['channels'].append(deepcopy(data['channels'][0]))

    with pytest.raises(ConfigError, match='Duplicate channel id'):
        load_runtime_config(_write_config(tmp_path, mutate))


def test_unknown_schema_ref_is_rejected(tmp_path):
    def mutate(data):
        data['channels'][0]['schemaRef'] = 'missing'

    with pytest.raises(ConfigError, match='unknown store'):
        load_runtime_config(_write_config(tmp_path, mutate))


def test_nonpositive_date_window_is_rejected(tmp_path):
    def mutate(data):
        data['channels'][0]['dateWindowDays'] = 0

    with pytest.raises(ConfigError, match='dateWindowDays'):
        load_runtime_config(_write_config(tmp_path, mutate))


def test_invalid_batch_time_is_rejected(tmp_path):
    def mutate(data):
        data['server']['batch']['windowStart'] = '25:00'

    with pytest.raises(ConfigError, match='HH:MM'):
        load_runtime_config(_write_config(tmp_path, mutate))


def test_node_keys_are_hashed_and_map_to_known_nodes(tmp_path):
    token = 'node-token'
    keys = {hashlib.sha256(token.encode()).hexdigest(): 'linfen-shanghai'}
    config = load_runtime_config(_write_config(tmp_path, keys=keys))
    assert config.auth.authenticate(token) == 'linfen-shanghai'
    assert config.auth.authenticate('wrong') is None
    assert config.auth.authenticate('') is None


def test_node_key_file_must_reference_a_configured_node(tmp_path):
    keys = {hashlib.sha256('k'.encode()).hexdigest(): 'unknown-node'}
    with pytest.raises(ConfigError, match='unknown node'):
        load_runtime_config(_write_config(tmp_path, keys=keys))


def test_missing_keys_file_fails_fast(tmp_path):
    def mutate(data):
        data['server']['auth']['keysFile'] = str(tmp_path / 'missing.json')

    with pytest.raises(ConfigError, match='does not exist'):
        load_runtime_config(_write_config(tmp_path, mutate))


def test_runtime_path_requires_an_explicit_environment(monkeypatch):
    from wiseflow_server.runtime_config import config_path
    monkeypatch.delenv('OSBI_CONFIG_FILE', raising=False)
    monkeypatch.delenv('OSBI_CONFIG_DIR', raising=False)
    with pytest.raises(ConfigError):
        config_path()
