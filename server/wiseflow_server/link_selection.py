"""Deterministic handling of client-classified, model-selected links."""
import unicodedata

from .models import Link

_ROLE_ORDER = {'content': 0, 'unknown': 1, 'navigation': 2}


def _normalize(value: str | None) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def refine_link_selection(link_ids: list[str], links: list[Link]) -> tuple[list[str], dict[str, int]]:
    """Deduplicate selected links, then apply stable role ordering and caps.

    Candidates outside the model selection never replace selected candidates.
    Without navigation, the surviving links keep the model's original order.
    """
    by_id = {link.id: link for link in links}
    selected = [by_id[link_id] for link_id in dict.fromkeys(link_ids)]
    keys = {link.id: (_normalize(link.text), _normalize(link.context)) for link in selected}
    winners = {}
    for link in selected:
        key = keys[link.id]
        if key not in winners or _ROLE_ORDER[link.role] < _ROLE_ORDER[winners[key].role]:
            winners[key] = link
    selected = [link for link in selected if winners[keys[link.id]].id == link.id]
    navigation_count = sum(link.role == 'navigation' for link in selected)
    navigation_only = bool(selected) and navigation_count == len(selected)
    dropped = 0
    if navigation_count:
        selected.sort(key=lambda link: _ROLE_ORDER[link.role])
        cap = 3 if navigation_only else 1
        dropped = max(0, navigation_count - cap)
        selected = selected[:len(selected) - dropped]
    counters = {
        'selected_content_links': sum(link.role == 'content' for link in selected),
        'selected_unknown_links': sum(link.role == 'unknown' for link in selected),
        'selected_navigation_links': sum(link.role == 'navigation' for link in selected),
        'navigation_nav_cap_dropped': dropped,
        'navigation_only_fallback': int(navigation_only),
    }
    return [link.id for link in selected], counters
