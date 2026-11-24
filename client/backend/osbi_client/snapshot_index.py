"""Build extraction-ready coarse blocks and deduplicated exploration targets."""
import copy
import re
from urllib.parse import urlsplit

from .material_urls import canonical_url
from .page_material import empty_material, seal_material

_UI = re.compile(r'^(?:登录|注册|首页|简体|繁体|关怀版|无障碍|字号[：:]?.*|分享[：:]?|求(?:分享|收藏|在看|点赞)|往期回顾|打印|关闭|返回顶部|网站地图|政务邮箱|纠错|我要纠错|网站纠错|举报|联系我们|隐私政策|.*(?:总客服|服务热线)[：:].*|沪[公I].*|政府网站标识码.*)$', re.S)
_CHROME_HOSTS = {'service.weibo.com', 'connect.qq.com', 'shuo.douban.com', 'bszs.conac.cn',
                 'zfwzgl.www.gov.cn', 'www.12377.cn', 'beian.miit.gov.cn', 'www.beian.gov.cn'}


def _fold(text):
    return ''.join(text.split())


def _native_text(nodes):
    parts = []
    for node in nodes:
        if node.get('role') in {'img', 'button', 'option', 'iframe'}:
            continue
        parts.append(_native_text(node.get('children', [])) or node.get('text') or node.get('name') or '')
    return ''.join(parts)


def _in_main(segment, dom):
    if 'inMain' in segment:
        return segment['inMain']
    target = dom.get('targetPath', '')
    return bool(target and target != 'html > body:nth-of-type(1)' and
                (segment.get('path') == target or segment.get('path', '').startswith(target + ' > ')))


def _chrome(segment, dom):
    if segment.get('chrome'):
        return True
    group = next((g for g in dom.get('groups', []) if g['id'] == segment.get('groupId')), {})
    return group.get('kind') in {'nav', 'footer', 'header'}


_AUTH_URL = re.compile(
    r'/(?:login|log-in|signin|sign-in|register|signup|sign-up|account|profile|'
    r'search|sitemap|privacy|terms|contact|share|print|subscribe)(?:/|$|\.)', re.I)
_SECTION_URL = re.compile(
    r'/(?:index|list|channel|column|category|tag|search)(?:/|$|\.)', re.I)
_LANGUAGE_TEXT = {'en', 'english', 'english version', '繁', '简', '简体', '繁体',
                  '简体中文', '繁体中文', '日本語', '한국어'}


def _hard_navigation(url, text, current_url, current_host):
    """Only clearly non-content destinations are dropped at capture time."""
    if canonical_url(url) == current_url:
        return True
    parts = urlsplit(url)
    if parts.hostname == current_host and parts.path in ('', '/'):
        return True
    if _AUTH_URL.search(parts.path):
        return True
    if (text or '').strip().casefold() in _LANGUAGE_TEXT:
        return True
    return False


def _section_link(url):
    parts = urlsplit(url)
    path = parts.path.lower()
    segments = [value for value in path.split('/') if value]
    # Shallow trailing-slash pages are hubs; deep ones may be article canonicals.
    if path.endswith('/') and path != '/' and len(segments) <= 2:
        return True
    return bool(_SECTION_URL.search(path))


def _link_role(segment, dom, *, navigation_hub, url, current_url, current_host):
    text = (segment.get('text') or '').strip()
    if _chrome(segment, dom) or navigation_hub:
        return 'navigation'
    if _section_link(url) or _hard_navigation(url, '', current_url, current_host):
        return 'navigation'
    if _in_main(segment, dom) and not _chrome(segment, dom):
        return 'content'
    return 'unknown'


def prepare_capture(capture):
    """Resolve explicit bylines and link-only catalogue tables before enrichment.

    Work on a copy so the browser evidence remains intact. A table becomes a
    catalogue scope only when at least three distinct linked records account
    for every row; ordinary data tables keep their original body treatment.
    """
    result = copy.deepcopy(capture)
    dom = result.get('dom') or {}
    segments = dom.get('segments', [])
    meta = dom.setdefault('meta', {})
    for s in segments:
        text = s.get('text', '').strip()
        labelled = re.fullmatch(r'(供稿|稿件来源|撰稿|作者|摄影|编辑|责编|校审|审核|制作|出品)\s*[：:]\s*([^\n]{1,100})', text)
        production = re.fullmatch(r'(.{2,60}?)微信综合制作', text)
        if labelled or production:
            role, name = labelled.groups() if labelled else ('制作', production[1])
            contributors = meta.setdefault('contributors', [])
            record = {'role': role, 'name': name}
            if record not in contributors:
                contributors.append(record)
            if role == '作者' and not meta.get('author'):
                meta['author'] = name
            s['chrome'] = True
    if not any(_in_main(s, dom) for s in segments):
        tables = {}
        for s in segments:
            if s.get('kind') == 'table-row' and not _chrome(s, dom):
                tables.setdefault(s.get('groupId'), []).append(s)
        catalogues = set()
        for group, rows in tables.items():
            urls = {canonical_url(a.get('url', ''), supported_only=True)
                    for row in rows for a in row.get('links', [])}
            urls.discard('')
            if group and len(urls) >= 3 and all(_link_only(row) for row in rows):
                catalogues.add(group)
        if catalogues:
            for s in segments:
                s['inMain'] = s.get('groupId') in catalogues
                if s['inMain'] and s.get('kind') == 'table-header':
                    s['chrome'] = True
    return result


def image_eligible(segment, dom):
    if _chrome(segment, dom):
        return False

    if segment.get('links') and not any(canonical_url(a.get('url', ''), supported_only=True)
                                        or re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', a.get('url', ''), re.I)
                                        or a.get('rawHref', '').startswith(('#', 'javascript:'))
                                        for a in segment['links']):
        return False
    positions = [i for i, s in enumerate(dom.get('segments', [])) if _in_main(s, dom)]
    if _UI.fullmatch(segment.get('text', '').strip()):
        return False
    if _in_main(segment, dom) or not positions:
        return True

    index = next((i for i, s in enumerate(dom['segments']) if s is segment), -1)
    return index > max(positions) and any(
        canonical_url(a.get('url', ''), supported_only=True) and
        urlsplit(a.get('url', '')).hostname not in _CHROME_HOSTS for a in segment.get('links', []))


def _heading_level(segment):
    if segment.get('kind') != 'heading':


        return 2 if re.fullmatch(r'第[一二三四五六七八九十百\d]+[章节]\s*\S.*', segment.get('text', '')) else None
    if segment.get('headingLevel'):
        return segment['headingLevel']
    match = re.search(r'h([1-6]):nth', segment.get('path', ''))
    return int(match[1]) if match else 2


def _link_only(segment):
    anchors = segment.get('links', [])
    if not anchors:
        return False
    if any(im.get('text') for im in segment.get('images', [])) and all(
            a.get('rawHref', '').startswith(('#', 'javascript:')) or
            re.search(r'\.(?:jpe?g|png|webp|gif)(?:[?#]|$)', a.get('url', ''), re.I)
            for a in anchors):
        return False
    remainder = segment.get('text', '')
    for a in anchors:
        for text in (a.get('text', ''), a.get('title', '')):
            if text:
                remainder = remainder.replace(text, '', 1)
    return len(re.sub(r'[\s\d|：:./—-]', '', remainder)) < 8


def _image_text(segment):
    text = segment.get('text', '')

    for image in sorted(segment.get('images', []), key=lambda x: x.get('offset', len(text)), reverse=True):
        value = image.get('text', '').strip()
        if value and value not in text:
            offset = max(0, min(image.get('offset', len(text)), len(text)))
            text = text[:offset] + '\n' + value + '\n' + text[offset:]
    if segment.get('kind') in {'table-row', 'table-header'} and segment.get('cells'):
        cells = ' | '.join((f"{c['label']}：" if c.get('label') and segment['kind'] != 'table-header' else '') + c['text'] for c in segment['cells'])
        supplements = [i['text'] for i in segment.get('images', []) if i.get('text') and i['text'] not in cells]
        text = '\n'.join([cells, *supplements])
    return text.strip()


def _partition(parts, limit, title=''):
    """Split oversized sections at child headings, then paragraph/sentence boundaries."""
    if not parts:
        return []
    if len('\n\n'.join(x['text'] for x in parts)) <= limit:
        return [(title, parts)]
    ranks = [x['level'] for x in parts[1:] if x['level'] is not None]
    if ranks:
        level = min(ranks)
        groups, current = [], []
        for p in parts:
            if p['level'] == level and current:
                groups.append(current); current = []
            current.append(p)
        if current:
            groups.append(current)
        if len(groups) > 1:
            return [unit for group in groups for unit in _partition(group, limit, title)]
    units, current, size = [], [], 0
    for part in parts:
        fragments = [part]
        if len(part['text']) > limit:

            sentences = re.split(r'(?<=[。！？!?；;])', part['text'])
            fragments = []
            for sentence in sentences:
                fragments.extend({**part, 'text': sentence[i:i + limit]} for i in range(0, len(sentence), limit))
        for fragment in fragments:
            n = len(fragment['text']) + 2
            if current and size + n > limit:
                units.append((title, current)); current, size = [], 0
            current.append(fragment); size += n
    if current:
        units.append((title, current))
    return units


def index_snapshot(capture, seed_meta=None, *, max_block_chars=4000):
    capture = prepare_capture(capture)
    dom = capture.get('dom')
    if not dom:
        return empty_material(capture.get('requestedUrl', ''), capture.get('status', {'access': 'error', 'load': 'timeout'}), error=capture.get('error'))
    if max_block_chars < 200:
        raise ValueError('max_block_chars must be at least 200')
    meta = {**(seed_meta or {}), **{k: v for k, v in dom.get('meta', {}).items() if v}}
    meta.update(requestedUrl=capture['requestedUrl'], finalUrl=capture.get('finalUrl', capture['requestedUrl']),
                canonicalUrl=canonical_url(capture.get('canonicalUrl') or capture['requestedUrl']), capturedAt=capture.get('capturedAt'))

    meta.pop('evidence', None)
    current_url = canonical_url(meta.get('canonicalUrl') or meta.get('requestedUrl') or '')
    current_host = urlsplit(current_url).hostname
    material = {'status': copy.deepcopy(capture['status']), 'meta': meta, 'blocks': [], 'links': [],
                'scope': {'metadataUnresolved': dom.get('metadataUnresolved', False),
                          'truncated': dom.get('truncated', False), 'imagesPending': 0,
                          'textSources': {'snapshotMatched': 0, 'domSupplement': 0}},
                'localMaps': {'urls': {}, 'images': {}, 'blockSources': {}, 'itemMeta': {}, 'unsupportedLinks': []}}
    maps = material['localMaps']
    segments = dom.get('segments', [])
    main_indices = [i for i, s in enumerate(segments) if _in_main(s, dom)]
    first_main = min(main_indices) if main_indices else None
    title = meta.get('title', '')
    parts = []
    for i, segment in enumerate(segments):
        if image_eligible(segment, dom):
            for image in segment.get('images', []):
                key = f'I{len(maps["images"]) + 1}'
                maps['images'][key] = {k: image.get(k) for k in ('url', 'src', 'dataSrc', 'text', 'source', 'status') if image.get(k)}
                if image.get('status') == 'pending':
                    material['scope']['imagesPending'] += 1
        text = _image_text(segment)
        if not text or _chrome(segment, dom) or _UI.fullmatch(text) or _link_only(segment):
            continue
        if main_indices and i not in main_indices:

            if title and _fold(text).startswith(_fold(title)):
                match = re.search(r'[\u4e00-\u9fff]{1,12}〔\d{4}〕\d+号', text)
                if match:
                    parts.append({'text': match[0], 'level': None, 'source': i, 'region': ''})
            continue
        if _fold(text) == _fold(title):
            continue
        parts.append({'text': text, 'level': _heading_level(segment), 'source': i,
                      'region': segment.get('regionPath', '')})
        native = _native_text(segment.get('tree', []))
        source = 'snapshotMatched' if native and _fold(native) == _fold(segment.get('text', '')) else 'domSupplement'
        material['scope']['textSources'][source] += 1
    ranks = [p['level'] for p in parts if p['level'] is not None]
    top = min(ranks) if ranks else None
    sections, current, intro = [], [], []
    current_region = None
    for p in parts:
        heading_boundary = top is not None and p['level'] == top
        region_boundary = current_region and p['region'] and p['region'] != current_region
        boundary = heading_boundary or region_boundary
        if boundary and current:
            if heading_boundary and not region_boundary and not sections and not any(x['level'] is not None for x in current):
                intro = current
            else:
                sections.append(current)
            current = []
        current.append(p)
        if p['region']:
            current_region = p['region']
    if current:
        sections.append(current)
    if not sections and intro:
        sections = [intro]; intro = []

    def _block_text(intro_parts, heading, group):
        values = [p['text'] for p in intro_parts]
        if heading and group[0]['text'] != heading:
            values.append(heading)
        values.extend(p['text'] for p in group)
        return '\n\n'.join(values)

    def _emit(intro_parts, heading, group):
        block_id = f'B{len(material["blocks"]) + 1}'
        material['blocks'].append({'id': block_id, 'text': _block_text(intro_parts, heading, group)})
        maps['blockSources'][block_id] = sorted({p['source'] for p in [*intro_parts, *group]})

    units = []
    for section in sections:
        parent = section[0]['text'] if section[0]['level'] is not None else ''



        limit = max_block_chars - (len(parent) + 2 if parent else 0)
        if limit < max_block_chars // 2:


            limit = max_block_chars
        for heading, group in _partition(section, limit, parent):

            if not any(p['level'] is None and p['text'].strip() for p in group):
                continue
            units.append((heading, group))
    if intro:



        if units and len(_block_text(intro, units[0][0], units[0][1])) <= max_block_chars:
            _emit(intro, *units[0])
            units = units[1:]
        else:
            for heading, group in _partition(intro, max_block_chars, ''):
                if not any(p['level'] is None and p['text'].strip() for p in group):
                    continue
                _emit([], heading, group)
    for heading, group in units:
        _emit([], heading, group)

    known, label_quality = {}, {}
    def navigation_hub(segment):
        groups = {g['id']: g for g in dom.get('groups', [])}
        group = groups.get(segment.get('groupId'))
        kinds, seen_groups = set(), set()
        while group and group['id'] not in seen_groups:
            seen_groups.add(group['id'])
            kinds.add(group.get('kind'))
            group = groups.get(group.get('parentId'))
        return 'nav' in kinds and 'footer' not in kinds

    for fallback in (False, True):
        if fallback and (material['links'] or main_indices or sum(len(b['text']) for b in material['blocks']) > 200):
            break
        for i, segment in enumerate(segments):
            if fallback:
                if not navigation_hub(segment):
                    continue
            elif _chrome(segment, dom) or (first_main is not None and i < first_main):
                continue
            for anchor in segment.get('links', []):
                url = anchor.get('url', '')
                raw_href = anchor.get('rawHref', '') or ''
                canonical = canonical_url(url)
                if not canonical or raw_href.strip().startswith('#') or urlsplit(url).hostname in _CHROME_HOSTS:
                    continue
                if _hard_navigation(canonical, anchor.get('text') or anchor.get('ariaLabel') or anchor.get('title') or '',
                                    current_url, current_host):
                    continue


                text, quality = '', 0
                for field, rank in (('text', 3), ('ariaLabel', 3), ('title', 2)):
                    candidate = (anchor.get(field) or '').strip()
                    if candidate and not re.match(r'https?://', candidate, re.I):
                        text, quality = candidate, rank
                        break
                if _UI.fullmatch(text):
                    continue
                if not canonical_url(url, supported_only=True):
                    if canonical not in {x['url'] for x in maps['unsupportedLinks']}:
                        maps['unsupportedLinks'].append({'url': canonical, 'text': text})
                    continue
                prefix = text.rstrip('.… ')
                full = anchor.get('title', '').strip()
                if prefix and full.startswith(prefix) and ('...' in text or '…' in text):
                    text = full
                if not text:
                    sources = {im.get('src') for im in anchor.get('images', []) if im.get('src')}
                    images = [im for im in segment.get('images', []) if im.get('src') in sources or im.get('dataSrc') in sources]
                    if not images and anchor.get('images') and len(segment.get('images', [])) == 1:
                        images = segment['images']
                    text = next((im.get('text') or im.get('alt', '') for im in images if im.get('text') or im.get('alt')), '')
                    if text in {'图片', '图', 'image', 'img'}:
                        text = ''
                    if text:
                        quality = 1
                if not text:
                    continue
                segment_text = segment.get('text', '').strip()
                context = ''
                if len(text) < 6 or text in {'点击查看', '查看详情', '阅读全文', '更多', '详情'}:
                    context = segment_text or title
                elif segment_text and segment_text != text:




                    remainder = segment_text.replace(text, ' ', 1).strip() if text in segment_text else segment_text
                    if remainder:
                        context = remainder[:160]
                if canonical in known:
                    old = known[canonical]
                    if quality > label_quality[canonical] or (quality == label_quality[canonical] and len(text) > len(old['text'])):
                        old['text'] = text
                        label_quality[canonical] = quality
                        old.pop('context', None)
                        if context and context != text:
                            old['context'] = context
                    continue
                link = {'id': f'L{len(material["links"]) + 1}', 'text': text}
                if context and context != text:
                    link['context'] = context
                link['role'] = _link_role(segment, dom, navigation_hub=navigation_hub(segment),
                                          url=canonical, current_url=current_url,
                                          current_host=current_host)
                material['links'].append(link); known[canonical] = link; label_quality[canonical] = quality
                maps['urls'][link['id']] = url
                if segment.get('itemMeta'):
                    maps['itemMeta'][link['id']] = segment['itemMeta']
    return seal_material(material)
