"""URL entry parsing for task source configuration."""
import re
from urllib.parse import urlsplit
from .material_urls import get_base_domain, normalize_url


def extract_urls(text):
    urls = set()
    for url in re.findall(r'(?:https?://|www\.)[^\s<>\"\u3000]+', text, re.I):
        url = url.rstrip('，。；、）),;')
        url = normalize_url(url, clean_tracking=False)
        try:
            if urlsplit(url).hostname:
                urls.add(url)
        except ValueError:
            continue
    return urls
