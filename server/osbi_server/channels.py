"""Deployment-owned channel definitions and private ontology bootstrap files."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

CHANNEL_ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"


@dataclass(frozen=True)
class ChannelDefinition:
    id: str
    title: str
    description: str
    schema: Path

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(CHANNEL_ID_PATTERN, self.id):
            raise ValueError("Channel IDs must be lowercase names using letters, digits and hyphens")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200:
            raise ValueError("A channel requires a title of at most 200 characters")
        if not isinstance(self.description, str) or len(self.description) > 2000:
            raise ValueError("Channel descriptions must be strings of at most 2000 characters")
        object.__setattr__(self, 'schema', Path(self.schema).expanduser().resolve())


def default_channels():
    return (ChannelDefinition('default', 'Default', 'Replace this template for your deployment',
        Path(__file__).with_name('schemas') / 'default.ttl'),)


def load_channels(path):
    """Resolve private schema paths relative to the deployment manifest."""
    path = Path(path).expanduser().resolve()
    body = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(body, dict) or set(body) != {'channels'} or not isinstance(body['channels'], list) or not body['channels']:
        raise ValueError("Channel manifest must contain a nonempty channels list")
    definitions = []
    for item in body['channels']:
        if (not isinstance(item, dict) or set(item) - {'id', 'title', 'description', 'schema'}
                or not {'id', 'schema'} <= set(item) or not isinstance(item['schema'], str) or not item['schema']):
            raise ValueError("Each channel requires id and a private schema file path")
        target = Path(item['schema']).expanduser()
        definitions.append(ChannelDefinition(item['id'], item.get('title', item['id']),
            item.get('description', ''), target if target.is_absolute() else path.parent / target))
    if len({item.id for item in definitions}) != len(definitions):
        raise ValueError("Channel IDs must be unique in a deployment manifest")
    return tuple(definitions)
