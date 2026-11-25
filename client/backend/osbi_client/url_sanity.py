"""Detect synthetic placeholder URLs carried by demonstration evidence.

Shared graphs can contain test or demonstration records whose only source URL
uses a reserved example host (``example.com``, ``example.org``, and in practice
also variants such as ``example.gov.cn``). Such a record is not a real report
item: delivering its link in a group message is worse than dropping it, so
report selection filters it out before candidates reach the editor model.
"""
from urllib.parse import urlsplit

# RFC 2606 reserves example.com/example.net/example.org; the extra suffixes
# cover demonstration hosts seen in shared evidence that are not reserved.
_PLACEHOLDER_HOSTS = frozenset({
    "example.com",
    "example.net",
    "example.org",
    "example.gov.cn",
    "example.edu",
})
_PLACEHOLDER_SUFFIXES = (".example",)


def _host(url: str) -> str:
    """Return the lowercase hostname of a URL, or an empty string."""
    try:
        return (urlsplit(str(url or "")).hostname or "").strip(".").lower()
    except ValueError:
        return ""


def is_placeholder_url(url: str) -> bool:
    """True when the URL points at a reserved/demonstration host.

    Empty or unparsable values return False: an item without a link is a
    legitimate title-only record, not evidence of a placeholder host.
    """
    host = _host(url)
    if not host:
        return False
    if host in _PLACEHOLDER_HOSTS or host.endswith(_PLACEHOLDER_SUFFIXES):
        return True
    # Any label, not only the first one, may be the reserved demonstration
    # label: example.net, www.example.net and a.b.example.org all qualify.
    return "example" in host.split(".")
