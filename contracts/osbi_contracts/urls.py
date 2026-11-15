"""Deterministic URL and publication-date transport normalization."""
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, unquote_plus


def canonical_url(value):
    try:
        p = urlsplit(value.strip())
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            return ""
        host = p.hostname.encode("idna").decode().lower()
        if ":" in host:
            host = "[" + host + "]"
        if p.port and (p.scheme, p.port) not in {( "http", 80), ("https", 443)}:
            host += ":" + str(p.port)
        query = "&".join(v for v in p.query.split("&") if v and
                         not unquote_plus(v.split("=", 1)[0]).lower().startswith("utm_") and
                         unquote_plus(v.split("=", 1)[0]).lower() not in {"spm", "fbclid", "gclid"})
        return urlunsplit((p.scheme.lower(), host, p.path or "/", query, ""))
    except (ValueError, UnicodeError, AttributeError):
        return ""


def normalize_publish_date(value):
    match = re.search(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})", value or "")
    try:
        return datetime(*map(int, match.groups())).date().isoformat() if match else ""
    except ValueError:
        return ""
