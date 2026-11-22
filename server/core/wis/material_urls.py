"""HTTP URL normalization, tracking cleanup and domain admission identities."""
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, quote, unquote_plus, urlencode, urljoin, urlsplit, urlunsplit

import idna

URL_RULES = json.loads(Path(__file__).with_name('url_rules.json').read_text())
TRACKING_PARAMS = set(URL_RULES['trackingParameters'])


def _browser_ipv4(host):
    """Match the browser's numeric IPv4 notation before grouping by domain."""
    parts = host.split('.')
    if not re.fullmatch(r'[0-9]+|0x[0-9a-f]*', parts[-1]):
        return None
    if len(parts) > 4:
        raise ValueError('Invalid IPv4 address')
    numbers = []
    for part in parts:
        if not part:
            raise ValueError('Invalid IPv4 address')
        base, digits = (16, part[2:]) if part.startswith('0x') else (8, part[1:]) if len(part) > 1 and part.startswith('0') else (10, part)
        numbers.append(int(digits or '0', base))
    if any(n > 255 or n < 0 for n in numbers[:-1]) or not 0 <= numbers[-1] < 256 ** (5 - len(parts)):
        raise ValueError('Invalid IPv4 address')
    address = numbers[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(numbers[:-1]))
    return str(ipaddress.IPv4Address(address))


def _host(value):
    host = value.lower().removesuffix('.')
    if host.endswith('.'):
        raise ValueError('Invalid hostname')
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return _browser_ipv4(host) or idna.encode(host, uts46=True).decode('ascii')


def _path(value):
    parts = []
    for part in (value or '/').split('/'):
        dot = re.sub('%2e', '.', part, flags=re.I)
        if dot == '..':
            if len(parts) > 1:
                parts.pop()
        elif dot != '.':
            parts.append(part)
    if re.search(r'(?:^|/)(?:\.|%2e){1,2}$', value, flags=re.I):
        parts.append('')
    return quote('/'.join(parts) or '/', safe="/:@!$&'()*+,;=-._~%[]")


def normalize_url(url, base_url=None, *, clean_tracking=True):
    """Resolve/repair HTTP URLs. Keep non-tracking query bytes/signatures intact."""
    if not isinstance(url, str) or not url.strip():
        return ''
    value = url.strip()
    if re.match(r'^(?:mailto|tel|javascript|data):', value, re.I):
        return value
    if value.lower().startswith(('www.', '/www.')):
        value = 'https://' + value.lstrip('/')
    elif value.startswith('//'):
        value = (urlsplit(base_url).scheme if base_url else 'https') + ':' + value
    elif re.match(r'^https?:/[^/]', value, re.I):
        value = re.sub(r'^(https?):/', r'\1://', value, flags=re.I)
    elif base_url:
        value = urljoin(base_url, value)
    try:
        p = urlsplit(value)
        if p.scheme not in {'http', 'https'} or not p.hostname:
            return ''
        host = _host(p.hostname)
        netloc = '[' + host + ']' if ':' in host else host
        port = p.port  # Validates malformed/out-of-range ports.
        if port is not None and (p.scheme, port) not in {('http', 80), ('https', 443)}:
            netloc += ':' + str(port)
        if '@' in p.netloc:
            netloc = p.netloc.rsplit('@', 1)[0] + '@' + netloc
        query = p.query
        if clean_tracking:
            def keep(part):
                key = unquote_plus(part.split('=', 1)[0]).lower()
                return key not in TRACKING_PARAMS and not any(key.startswith(prefix) for prefix in URL_RULES['trackingPrefixes'])
            query = '&'.join(part for part in query.split('&') if keep(part))
        query = quote(query, safe="!$%&()*+,-./:;=?@[\\]^_`{|}~")
        fragment = quote(p.fragment, safe="/?:@!$&'()*+,;=-._~%[]#\\^|{}")
        return urlunsplit((p.scheme, netloc, _path(p.path), query, fragment))
    except (ValueError, TypeError, idna.IDNAError):
        return ''


def get_base_domain(url):
    """Reference-project grouping: base domain, with WeChat isolated from qq.com."""
    try:
        p = urlsplit(normalize_url(url, clean_tracking=False))
        if p.scheme not in {'http', 'https'} or not p.hostname:
            return ''
        host = _host(p.hostname)
        if host in URL_RULES['separateDomains']:
            return host
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            pass
        host = host.removeprefix('www.')
        parts = host.split('.')
        count = 3 if len(parts) > 2 and parts[-2] in URL_RULES['secondLevelDomains'] else 2
        return '.'.join(parts[-count:])
    except (ValueError, TypeError, idna.IDNAError):
        return ''

ATTACHMENTS = {'.pdf', '.wps', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.zip',
               '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.mp4', '.mp3'}


def canonical_url(url, *, supported_only=False):
    try:
        p = urlsplit(normalize_url(url))
        if p.scheme not in {'http', 'https'} or not p.hostname:
            return ''
        if supported_only and any(p.path.lower().endswith(x) for x in ATTACHMENTS):
            return ''
        query = p.query
        q = parse_qs(query, keep_blank_values=True)
        if p.hostname.lower() == 'mp.weixin.qq.com' and all(q.get(k, [''])[0] for k in ('__biz', 'mid', 'idx', 'sn')):
            query = urlencode({k: q[k][0] for k in ('__biz', 'mid', 'idx', 'sn')})
        scheme = 'https' if p.hostname.lower() == 'mp.weixin.qq.com' else p.scheme.lower()
        return normalize_url(urlunsplit((scheme, p.netloc, p.path or '/', query, '')), clean_tracking=False)
    except (ValueError, TypeError):
        return ''


def forbidden_url(url, fragments):
    """Match URL substrings (domain or domain+path prefix), mirroring the
    legacy engine's FORBIDDEN_DOMAINS admission rule."""
    return any(fragment in url for fragment in fragments)


def new_urls(urls, pending, visited, forbidden=()):
    """Keep original fetch URLs while comparing canonical targets across recursion."""
    seen = {canonical_url(u) for u in set(pending) | set(visited)}
    result = []
    for url in [urls] if isinstance(urls, str) else urls:
        target = normalize_url(url, clean_tracking=False)
        if forbidden and forbidden_url(target, forbidden):
            continue
        key = canonical_url(target, supported_only=True)
        if key and key not in seen:
            seen.add(key)
            result.append(target)
    return result
