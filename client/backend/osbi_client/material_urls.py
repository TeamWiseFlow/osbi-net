"""Client acquisition URL support, separate from server extraction."""
import re
from osbi_contracts.urls import canonical_url as normalize


def canonical_url(value, *, supported_only=False):
    url = normalize(value)
    if supported_only and re.search(r"\.(?:pdf|docx?|xlsx?|zip|rar|mp4|mp3)(?:[?#]|$)", url, re.I):
        return ""
    return url
