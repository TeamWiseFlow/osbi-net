"""Conservative absolute release-date screening for navigation candidates.

Only link text/context are inspected. Explicit release labels take precedence;
otherwise only standalone or edge-position date fields are accepted, and event
or historical contexts are ignored. No page metadata or relative clock is used.
"""
from datetime import date, datetime
import re

from .publication_date import window_bounds

_DATE = re.compile(
    r'(?<![0-9A-Za-z_/])(?P<year>[0-9]{4})\s*[-/.年]\s*'
    r'(?P<month>[0-9]{1,3})\s*[-/.月]\s*(?P<day>[0-9]{1,3})(?:\s*日)?'
    r'(?![0-9A-Za-z_])')
_RELEASE_BEFORE = re.compile(
    r'(?:内容)?(?:发布|公布|印发|成文|签发|更新)(?:日期|时间)?(?:于)?\s*[:：]?\s*$')
_RELEASE_AFTER = re.compile(
    r'^\s*(?:发布|公布|印发|成文|签发|更新)(?!会|活动|仪式)'
    r'(?=的|《|[\s。；;，,）)】\]]|$)')
_EVENT_OR_HISTORY = re.compile(
    r'活动|会期|会议|论坛|培训|演练|比赛|赛事|发布会|发布仪式|召开|举行|举办|'
    r'开幕|闭幕|报名|截止|生效|实施|发生|成立|创办|开工|竣工|历史|回顾|回溯|'
    r'第\s*[0-9零〇一二两三四五六七八九十百]+\s*届|周年|截至|'
    r'\b(?:event(?:[_ -]*(?:date|time))?|meeting|conference|held|effective|deadline)\b', re.I)
_EDGES = ' \t\r\n\u200b[]【】()（）{}：:|｜·-—'


def _candidates(text):
    marked, bare = [], []
    event_context = bool(_EVENT_OR_HISTORY.search(text))
    first = len(text) - len(text.lstrip(_EDGES))
    last = len(text.rstrip(_EDGES))
    for match in _DATE.finditer(text):
        before = text[max(0, match.start()-64):match.start()]
        after = text[match.end():match.end()+64]
        if _RELEASE_BEFORE.search(before) or _RELEASE_AFTER.match(after):
            marked.append(match)
        elif not event_context:
            # Bare dates count only as a distinct leading/trailing metadata
            # field, never an arbitrary date embedded in a title or sentence.
            leading = match.start() == first
            trailing = (match.end() == last and
                        (match.start()==0 or text[match.start()-1] in _EDGES))
            if leading or trailing:
                bare.append(match)
    return marked, bare


def link_date_rejection(text: str, context: str | None, window_days: int | None,
                        *, now: datetime | None = None) -> str:
    """Return a known rejection, or '' to retain an undated/eligible link."""
    if window_days is None:
        return ''
    marked, bare = [], []
    for field in (text, context or ''):
        released, metadata = _candidates(field)
        marked.extend(released)
        bare.extend(metadata)
    candidates = marked or bare
    if not candidates:
        return ''
    values = set()
    for match in candidates:
        try:
            values.add(date(int(match['year']), int(match['month']), int(match['day'])).isoformat())
        except ValueError:
            return 'date_invalid'
    if len(values) != 1:
        return 'date_ambiguous'
    start, end = window_bounds(window_days, now=now)
    value = next(iter(values))
    return 'date_window' if value < start or value > end else ''
