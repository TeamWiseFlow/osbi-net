"""One runtime configuration shared by acquisition and the local API."""
import json
import os
from pathlib import Path
import tempfile
from core.async_logger import base_directory

custom_config_path = Path(os.environ.get('WISEFLOW_CUSTOMER_CONFIG') or base_directory / 'custom_config.json').expanduser().resolve()
# customer_config.json was briefly introduced as the canonical name on
# 2026-10-23; read it once so already-migrated installs move back to the
# original custom_config.json filename.
_legacy_path = base_directory / 'customer_config.json'
_defaults = json.loads((Path(__file__).resolve().parents[2] / 'custom_config.json').read_text())
config = dict(_defaults)


def _write_config(values):
    custom_config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=custom_config_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(values, stream, ensure_ascii=False, indent=2)
            stream.close()
            temporary.replace(custom_config_path)
        finally:
            temporary.unlink(missing_ok=True)


def _valid_time(value):
    if not isinstance(value, str):
        return False
    try:
        h, m = map(int, value.split(':'))
        return 0 <= h < 24 and 0 <= m < 60
    except ValueError:
        return False


def _valid(key, value):
    if key == 'DOMAIN_CONCURRENCY':
        from .material_urls import get_base_domain
        if (not isinstance(value, dict) or set(value) != {'defaultConcurrency', 'domains'}
                or type(value['defaultConcurrency']) is not int or value['defaultConcurrency'] < 1
                or not isinstance(value['domains'], dict)):
            return False
        normalized = {}
        for domain, limit in value['domains'].items():
            if (not isinstance(domain, str) or not domain or any(c in domain for c in ' /@?#*\t\n')
                    or type(limit) is not int or limit < 1):
                return False
            try:
                key = get_base_domain('https://' + domain)
            except ValueError:
                return False
            if not key or (key in normalized and normalized[key] != limit):
                return False
            normalized[key] = limit
        return True
    if key == 'MAX_URLS_PER_TASK':
        return type(value) is int and value > 0
    if key == 'TIME_SLOTS_START':
        return _valid_time(value)
    # 显式槽位表：覆盖 TIME_SLOTS_START 的 6 小时网格（如 12:18/23:18 间隔 11 小时）
    if key == 'TIME_SLOTS':
        return (isinstance(value, list) and 1 <= len(value) <= 6
                and all(_valid_time(t) for t in value)
                and len(set(value)) == len(value))
    # 禁爬片段：域名或域名+路径前缀，URL 子串命中即排除（任务显式 web 种子豁免）
    if key == 'FORBIDDEN_DOMAINS':
        return (isinstance(value, list)
                and all(isinstance(v, str) and v.strip() for v in value)
                and len(set(value)) == len(value))
    return False


def load_runtime_overrides():
    config.clear()
    config.update(_defaults)
    if not custom_config_path.exists() and _legacy_path.exists():
        # Move a briefly-migrated customer_config.json back to custom_config.json.
        try:
            data = json.loads(_legacy_path.read_text())
            migrated = {**_defaults, **{k: v for k, v in data.items() if _valid(k, v)}}
            _write_config(migrated)
        except (ValueError, AttributeError, OSError) as error:
            raise ValueError('Cannot migrate customer_config.json to custom_config.json') from error
    if custom_config_path.exists():
        try:
            data = json.loads(custom_config_path.read_text())
            if not isinstance(data, dict):
                raise ValueError('Customer configuration must be an object')
            if 'DOMAIN_CONCURRENCY' in data and not _valid('DOMAIN_CONCURRENCY', data['DOMAIN_CONCURRENCY']):
                raise ValueError('Invalid DOMAIN_CONCURRENCY')
            config.update({k: v for k, v in data.items() if _valid(k, v)})
        except (ValueError, AttributeError, OSError) as error:
            raise ValueError(f'Cannot read custom configuration: {error}') from error


def save_config(updates):
    if 'DOMAIN_CONCURRENCY' in updates and not _valid('DOMAIN_CONCURRENCY', updates['DOMAIN_CONCURRENCY']):
        return False
    values = {k: v for k, v in updates.items() if _valid(k, v)}
    if not values:
        return False
    try:
        existing = json.loads(custom_config_path.read_text()) if custom_config_path.exists() else {}
        values = {**existing, **config, **values}
        _write_config(values)
        load_runtime_overrides()
        return True
    except (OSError, ValueError):
        return False


def restore_default_config():
    try:
        _write_config(_defaults)
        load_runtime_overrides()
        return True
    except OSError:
        return False


load_runtime_overrides()
