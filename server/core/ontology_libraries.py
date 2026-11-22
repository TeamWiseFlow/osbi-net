"""Shared library routing; no database handles or model calls."""
import json
import os
from pathlib import Path
import re

from core.async_database import base_directory

LIBRARY_PATTERN = r'^[a-z][a-z0-9_-]{0,63}$'
APP_ROOT = Path(__file__).resolve().parents[1]


def default_config_path():
    """Prefer the runtime config in WISEFLOW_WORK_DIR; fall back to the app template."""
    runtime = base_directory / 'ontology_libraries.json'
    return runtime if runtime.is_file() else APP_ROOT / 'ontology_libraries.json'


class LibraryRegistry:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get('ONTOLOGY_LIBRARIES_CONFIG') or
                         default_config_path()).expanduser().resolve()
        try:
            data = json.loads(self.path.read_text())
        except OSError as error:
            raise ValueError(f'Cannot read ontology library configuration: {self.path}') from error
        if not isinstance(data, dict) or set(data) != {'defaultLibrary', 'libraries'}:
            raise ValueError('Expected defaultLibrary and libraries in ontology library configuration')
        self.default_id = data['defaultLibrary']
        if not isinstance(self.default_id, str):
            raise ValueError('defaultLibrary must be a library ID')
        self.libraries, self.by_focus = {}, {}
        if not isinstance(data['libraries'], dict) or not data['libraries']:
            raise ValueError('Configure at least one ontology library')
        for key, entry in data['libraries'].items():
            if not re.fullmatch(LIBRARY_PATTERN, key) or not isinstance(entry, dict):
                raise ValueError(f'Invalid ontology library: {key}')
            if set(entry) - {'title', 'schemaPath', 'focusIds'} or not entry.get('schemaPath'):
                raise ValueError(f'Invalid library settings: {key}')
            if not isinstance(entry['schemaPath'], str) or not isinstance(entry.get('title', key), str):
                raise ValueError(f'Library schemaPath and title must be strings: {key}')
            schema = Path(entry['schemaPath']).expanduser()
            candidates = [schema] if schema.is_absolute() else [
                (self.path.parent / schema).resolve(), (APP_ROOT / schema).resolve()]
            schema = next((candidate for candidate in candidates if candidate.is_file()), None)
            if schema is None:
                raise ValueError(f'Schema not found for ontology library {key}: '
                                 + ', '.join(str(candidate) for candidate in candidates))
            ids = entry.get('focusIds', [])
            if not isinstance(ids, list) or any(type(i) is not int or i < 1 for i in ids):
                raise ValueError(f'Invalid focusIds for library {key}')
            for focus_id in ids:
                if focus_id in self.by_focus:
                    raise ValueError(f'Focus {focus_id} is assigned more than once')
                self.by_focus[focus_id] = key
            self.libraries[key] = dict(title=entry.get('title', key), schema_path=schema, focus_ids=ids)
        if self.default_id not in self.libraries:
            raise ValueError('defaultLibrary must reference a configured library')

    def resolve(self, library_id=None, *, focus_id=None):
        key = library_id or self.by_focus.get(focus_id, self.default_id)
        if key not in self.libraries:
            raise ValueError(f'Unknown ontology library: {key}')
        return key
