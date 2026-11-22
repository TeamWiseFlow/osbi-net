"""Controlled tag vocabulary: entity types are attributes, never graph nodes."""
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re

DEFAULT_TAGS = Path(__file__).with_name('schemas') / 'tags.json'
_DIMENSION = re.compile(r'^[a-z][a-zA-Z0-9_]{0,31}$')
_SINGLE_VALUED = {'entityType', 'industry', 'ownership', 'scale', 'function'}
# 名称后缀是可靠证据时补标签；模型给出的同维度标签优先。
_FUNCTION_SUFFIXES = [
    ('住房公积金管理中心', '住房公积金管理中心'),
    ('社会工作部', '社会工作部'),
    ('数据资源管理局', '数据管理部门'), ('数据管理局', '数据管理部门'),
    ('大数据中心', '数据管理部门'), ('数据局', '数据管理部门'),
    ('交易所', '交易所'),
    ('基金管理有限公司', '基金公司'), ('基金管理人', '基金公司'), ('基金公司', '基金公司'),
    ('保险公司', '保险公司'), ('证券公司', '证券公司'),
    ('资产管理公司', '资产管理公司'), ('银行', '银行'),
    ('研究院', '研究机构'), ('研究所', '研究机构'),
]
_INDUSTRY_HINTS = [
    ('毛衫', '纺织服装'), ('纺织', '纺织服装'), ('服装', '纺织服装'),
    ('光伏', '光伏'), ('半导体', '半导体'), ('芯片', '半导体'),
    ('人工智能', '人工智能'), ('新能源', '新能源'),
    ('银行', '银行业'), ('证券', '证券业'), ('保险', '保险业'), ('基金', '资产管理'),
]


class TagVocabulary:
    def __init__(self, text):
        self.fingerprint = hashlib.sha256(text.encode()).hexdigest()
        data = json.loads(text)
        dimensions = data.get('dimensions')
        if not isinstance(dimensions, dict) or not dimensions:
            raise ValueError('Tag vocabulary requires dimensions')
        self.dimensions = {}
        for key, entry in dimensions.items():
            if not _DIMENSION.fullmatch(key) or not isinstance(entry, dict):
                raise ValueError(f'Invalid tag dimension: {key}')
            values = entry.get('values')
            if not isinstance(values, list) or not values or any(
                    not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f'Tag dimension needs nonempty string values: {key}')
            if len(set(values)) != len(values):
                raise ValueError(f'Duplicate tag value in dimension: {key}')
            self.dimensions[key] = dict(
                label=entry.get('label', key),
                description=entry.get('description', ''),
                values=values,
                allowed=set(values))
        self.aliases = {}
        for mention, target in (data.get('aliases') or {}).items():
            if (not isinstance(mention, str) or not mention.strip() or not isinstance(target, list)
                    or len(target) != 2 or target[0] not in self.dimensions
                    or target[1] not in self.dimensions[target[0]]['allowed']):
                raise ValueError(f'Invalid tag alias: {mention!r} -> {target!r}')
            self.aliases[mention.strip()] = (target[0], target[1])

    def prompt(self):
        return [dict(dimension=key, label=value['label'], description=value['description'],
                     values=value['values'])
                for key, value in sorted(self.dimensions.items())]

    def normalize(self, rows):
        """Return (deduplicated tags, dropped count) for model-proposed tags."""
        tags = {}
        dropped = 0
        for row in rows or []:
            dimension = (row.get('dimension') if isinstance(row, dict)
                         else getattr(row, 'dimension', None))
            value = (row.get('value') if isinstance(row, dict)
                     else getattr(row, 'value', None))
            dimension = str(dimension).strip() if dimension is not None else ''
            value = str(value).strip() if value is not None else ''
            if dimension in self.dimensions and value in self.dimensions[dimension]['allowed']:
                tags[(dimension, value)] = dict(dimension=dimension, value=value)
                continue
            alias = self.aliases.get(value)
            if alias:
                tags[alias] = dict(dimension=alias[0], value=alias[1])
                continue
            dropped += 1
        return [tags[key] for key in sorted(tags)], dropped

    def derive(self, text, is_organization):
        """从明确后缀补充标签；只做高置信推断，不猜所有制/规模/资质。"""
        if not is_organization:
            return []
        tags = {}
        for suffix, value in _FUNCTION_SUFFIXES:
            if text.endswith(suffix) and len(text) > len(suffix):
                tags[('function', value)] = dict(dimension='function', value=value)
                break
        if text.endswith(('交易所',)):
            tags[('entityType', '交易场所')] = dict(dimension='entityType', value='交易场所')
        elif text.endswith(('银行', '保险公司', '证券公司', '基金公司', '基金管理有限公司',
                            '资产管理公司')):
            tags[('entityType', '金融机构')] = dict(dimension='entityType', value='金融机构')
        elif text.endswith('市场'):
            if any(word in text for word in ('金融', '证券', '资本')):
                market = '金融交易市场'
            elif any(word in text for word in ('碳', '绿证', '数据', '能源', '环境')):
                market = '要素交易市场'
            else:
                market = '商品交易市场'
            tags[('entityType', market)] = dict(dimension='entityType', value=market)
        for word, industry in _INDUSTRY_HINTS:
            if word in text:
                tags[('industry', industry)] = dict(dimension='industry', value=industry)
                break
        return [tags[key] for key in sorted(tags)]

    def merge(self, existing, derived):
        """模型标签优先；单值维度不被名称推断覆盖。"""
        merged = {(dimension, value) for dimension, value in existing}
        present = {dimension for dimension, _ in existing}
        for tag in derived:
            key = (tag['dimension'], tag['value'])
            if key in merged:
                continue
            if tag['dimension'] in _SINGLE_VALUED and tag['dimension'] in present:
                continue
            merged.add(key)
            present.add(tag['dimension'])
        return tuple(sorted(merged))


@lru_cache(maxsize=4)
def _load(path, text):
    return TagVocabulary(text)


def load_vocabulary(path=None):
    path = Path(path or DEFAULT_TAGS)
    return _load(str(path), path.read_text())
