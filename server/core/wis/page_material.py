"""Coarse material, minimal relevance input and deterministic ID restoration."""
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta

SCHEMA_VERSION = 'snapshot-index/v2'
PageMaterial = dict

_DATE_PATTERN = re.compile(r'(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})')


class MaterialError(ValueError):
    pass


def normalize_publish_date(value):
    """统一发布日期为 YYYY-MM-DD，便于统计与模型判断；无法解析返回空串。

    兼容 ISO（2026-10-21T08:30）、中文（2026年10月21日 17:13）、
    斜杠（2026/10/21）及相对日期（今天/昨天）。单一实现：客户端 title 后缀、
    server 前缀与时间窗门控共用。
    """
    value = (value or '').strip()
    if not value:
        return ''
    if value.startswith('今天'):
        return datetime.now().strftime('%Y-%m-%d')
    if value.startswith('昨天'):
        return (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
    match = _DATE_PATTERN.search(value)
    if match:
        try:
            return datetime(int(match[1]), int(match[2]), int(match[3])).strftime('%Y-%m-%d')
        except ValueError:
            return ''
    return ''


def _digest(material):
    identity = {k: material.get(k) for k in ('schemaVersion', 'blocks', 'links')}
    identity['title'] = material.get('meta', {}).get('title', '')
    identity['url'] = material.get('meta', {}).get('canonicalUrl', '')
    identity['urls'] = material.get('localMaps', {}).get('urls', {})
    identity['images'] = material.get('localMaps', {}).get('images', {})
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def seal_material(material):
    material['schemaVersion'] = SCHEMA_VERSION
    material['materialId'] = _digest(material)
    return material


def selector_input(material, *, allow_partial=False):
    if material.get('schemaVersion') != SCHEMA_VERSION:
        raise MaterialError('Unsupported page material version')
    if material.get('materialId') != _digest(material):
        raise MaterialError('Material changed after IDs were assigned')
    if material.get('status', {}).get('access') != 'ok':
        raise MaterialError('Page is not publicly accessible')
    scope = material.get('scope', {})
    if scope.get('metadataUnresolved'):
        raise MaterialError('Unresolved metadata cannot enter relevance selection')
    if scope.get('imagesPending'):
        raise MaterialError('Image recognition is incomplete; do not classify as irrelevant')
    if material.get('status', {}).get('load') != 'ready' and not allow_partial:
        raise MaterialError('Partial material requires an explicit allow_partial decision')
    # 发布日期以"内容发布日期：YYYY-MM-DD"拼入 title 随相关性/摘要/本体提示传递：
    # 时间窗关注点据此判断新旧；本体侧日期停留在结构化标题字段中（类目无日期类、
    # 非实体门控拦截），不会被误当成政策日期或事件时间。无/无法解析的日期不拼。
    published = normalize_publish_date(material.get('meta', {}).get('publishedAt'))
    title = material.get('meta', {}).get('title', '')
    if published:
        title = f'{title}（内容发布日期：{published}）'
    return {
        'title': title,
        'blocks': [{'id': b['id'], 'text': b['text']} for b in material['blocks']],
        'links': [{k: copy.deepcopy(l[k]) for k in ('id', 'text', 'context') if l.get(k)}
                  for l in material['links']],
    }


def restore_selection(material, selection, *, allow_partial=False):
    """Return independent extraction units, never concatenate selected blocks."""
    selector_input(material, allow_partial=allow_partial)
    if not isinstance(selection, dict) or set(selection) != {'blockIds', 'linkIds'}:
        raise MaterialError('Selection must contain only blockIds and linkIds')

    def checked(name, items):
        value = selection[name]
        if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
            raise MaterialError('Selections must be lists of string IDs')
        ids = set(value)
        if not ids <= {x['id'] for x in items}:
            raise MaterialError('Selection contains unknown IDs')
        return ids

    block_ids = checked('blockIds', material['blocks'])
    link_ids = checked('linkIds', material['links'])
    urls = material['localMaps']['urls']
    if not link_ids <= urls.keys():
        raise MaterialError('Selected link has no URL mapping')
    return {
        'materialId': material['materialId'],
        'title': material.get('meta', {}).get('title', ''),
        'blocks': [copy.deepcopy(b) for b in material['blocks'] if b['id'] in block_ids],
        'links': [{**copy.deepcopy(l), 'url': urls[l['id']]} for l in material['links'] if l['id'] in link_ids],
        'meta': copy.deepcopy(material.get('meta', {})),
    }


def empty_material(url, status, *, error=None):
    return seal_material({'status': status, 'meta': {'requestedUrl': url, 'canonicalUrl': url},
                          'blocks': [], 'links': [], 'scope': {},
                          'localMaps': {'urls': {}, 'images': {}, 'blockSources': {}, 'itemMeta': {}},
                          'error': error})


def relevance_batches(payload, max_chars=12000):
    """Bound small-model input without fragmenting an extraction-ready block."""
    base = {'title': payload.get('title', ''), 'blocks': [], 'links': []}
    current = copy.deepcopy(base)
    size = len(json.dumps(base, ensure_ascii=False))
    for kind in ('blocks', 'links'):
        for item in payload[kind]:
            length = len(json.dumps(item, ensure_ascii=False))
            if size + length > max_chars and (current['blocks'] or current['links']):
                yield current
                current = copy.deepcopy(base)
                size = len(json.dumps(base, ensure_ascii=False))
            current[kind].append(item)
            size += length
    if current['blocks'] or current['links']:
        yield current
