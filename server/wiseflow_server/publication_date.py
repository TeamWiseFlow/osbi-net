"""Deterministic publication-date resolution for the ingestion gate.

Publication date is a *release* date, never ``capturedAt`` and never an event
date mentioned in the body. Absolute evidence wins over relative expressions.
When no trustworthy date can be derived the material is unknown and a
date-windowed channel rejects it; ``capturedAt`` is only the reference clock
for relative release expressions.

The result is deliberately richer than a bare date string so provenance and
quality markers stay auditable downstream (dateSource/dateExpression/
dateReference/dateEstimated/dateConflict).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
import re
from typing import Any, Iterable
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo('Asia/Shanghai')

_DATE_TOKEN = re.compile(r'(\d{4})(?:[-/年.])(\d{1,2})(?:[-/月.])(\d{1,2})(?:日)?')
_LABELED_DATE = re.compile(
    r'(?:内容)?(?:发布|公布|印发|成文|签发|更新)(?:日期|时间)?\s*[:：]?\s*'
    r'(\d{4}(?:[-/年.]\d{1,2}(?:[-/月.]\d{1,2})(?:日)?))')
_RELEASE_PREFIX = re.compile(
    r'(?:内容发布日期|最后更新|发布(?:于|时间)?|发表(?:于|时间)?|公布(?:于)?|'
    r'更新(?:于|时间)?|成文(?:于)?|印发(?:于)?)\s*[:：]?\s*')
_RELEASE_SUFFIX = re.compile(r'\s*(?:发布|发表|公布|更新)\s*')

_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_NUM = r'[0-9]+|[零〇一二两三四五六七八九十百]+'

_JUST_NOW = re.compile(r'刚刚|刚才')
_MINUTES_APPROX = re.compile(r'几分钟前')
_HALF_HOUR = re.compile(r'半小时前')
_HOURS_APPROX = re.compile(r'几小时前')
_HALF_DAY = re.compile(r'半天前')
_MINUTES_N = re.compile(rf'({_NUM})\s*分钟前')
_HOURS_N = re.compile(rf'({_NUM})\s*(?:个)?小时前')
_DAY_BEFORE_YESTERDAY = re.compile(r'大前天')
_DAY_BEFORE = re.compile(r'前天|前日')
_YESTERDAY = re.compile(r'昨天|昨日')
_TODAY = re.compile(r'今天|今日')
_DAYS_N = re.compile(rf'({_NUM})\s*(?:天|日)前')
_DAYS_OPEN = re.compile(rf'({_NUM})\s*(?:天|日)\s*(?:以前|之前)')
_WEEK_MONTH_YEAR = re.compile(
    rf'(?:({_NUM})\s*(?:周|个?星期|个?月|年)\s*(?:前|以前|之前))|(?:上周|上星期|上个月|去年)')
_VAGUE = re.compile(r'近日|日前|近期|不久前|前段时间|早些时候|几天前|几天以前|前几天')

# Only expressions in a release-position window count. A "事故发生于3天前"
# carries no release marker and is therefore ignored.
_MARKER_WINDOW = 40


@dataclass(frozen=True)
class PublicationDate:
    date: str = ''
    source: str = 'unknown'  # meta | title | body_absolute | body_relative | unknown | ambiguous
    expression: str = ''
    reference: str = ''      # absolute | meta | capturedAt
    estimated: bool = False
    conflict: bool = False
    reason: str = ''         # date_unknown | date_ambiguous | date_relative_vague | date_invalid
    flags: tuple[str, ...] = field(default_factory=tuple)

    def quality_flags(self) -> list[str]:
        return list(self.flags)


@dataclass(frozen=True)
class _Relative:
    expression: str
    date: str = ''
    estimated: bool = False
    vague: bool = False


def _parse_cn_number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if not text:
        return None
    total = 0
    if '百' in text:
        head, _, tail = text.partition('百')
        tens = _parse_cn_number(head) if head else 1
        total += (tens or 1) * 100
        text = tail
    if '十' in text:
        head, _, tail = text.partition('十')
        tens = _parse_cn_number(head) if head else 1
        total += (tens or 1) * 10
        text = tail
    if text:
        if len(text) != 1 or text not in _CN_DIGITS:
            return None
        total += _CN_DIGITS[text]
    return total


def _absolute(value: str) -> str:
    """Return YYYY-MM-DD for the first explicit absolute date, else ''."""
    for match in _DATE_TOKEN.finditer(str(value or '')):
        try:
            parsed = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            continue
        return parsed.isoformat()
    return ''


def _today(now: datetime | None) -> date:
    current = now or datetime.now(TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIMEZONE)
    return current.astimezone(TIMEZONE).date()


def _meta_reference(value: Any) -> datetime | None:
    text = str(value or '').strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('UTC'))
    return parsed.astimezone(TIMEZONE)


def _blocks_text(blocks: Iterable[Any], limit=3, max_chars=2500) -> str:
    parts, size = [], 0
    for index, block in enumerate(blocks):
        if index >= limit or size >= max_chars:
            break
        text = block.get('text', '') if isinstance(block, dict) else str(block or '')
        if not text:
            continue
        parts.append(text[:max_chars - size])
        size += len(parts[-1])
    return '\n'.join(parts)


def _released_relative(text: str, reference: datetime) -> list[_Relative]:
    """Find relative release expressions adjacent to release markers."""
    found: list[_Relative] = []

    def scan(fragment: str, *, anchor: str):
        for matcher, kind in (
                (_JUST_NOW, 'just_now'),
                (_MINUTES_APPROX, 'minutes_approx'),
                (_HALF_HOUR, 'half_hour'),
                (_HOURS_APPROX, 'hours_approx'),
                (_HALF_DAY, 'half_day'),
                (_MINUTES_N, 'minutes_n'),
                (_HOURS_N, 'hours_n'),
                (_DAY_BEFORE_YESTERDAY, 'days_3'),
                (_DAY_BEFORE, 'days_2'),
                (_YESTERDAY, 'days_1'),
                (_TODAY, 'days_0'),
                (_DAYS_N, 'days_n'),
                (_DAYS_OPEN, 'vague'),
                (_WEEK_MONTH_YEAR, 'vague'),
                (_VAGUE, 'vague')):
            matches = list(matcher.finditer(fragment))
            if not matches:
                continue
            match = matches[0] if anchor == 'start' else matches[-1]
            # The expression must sit next to the release marker: prefix
            # markers own their right edge, suffix markers their left edge.
            if anchor == 'start' and match.start() > 12:
                continue
            if anchor == 'end' and len(fragment) - match.end() > 12:
                continue
            if kind == 'vague':
                found.append(_Relative(expression=match.group(0), vague=True))
                return
            number = None
            if match.lastindex:
                raw = match.group(1)
                number = _parse_cn_number(raw) if raw else None
                if kind in {'minutes_n', 'hours_n', 'days_n'} and number is None:
                    return
            value, estimated = _relative_date(kind, number, reference)
            found.append(_Relative(expression=match.group(0), date=value, estimated=estimated))
            return

    for match in _RELEASE_PREFIX.finditer(text):
        scan(text[match.end():match.end() + _MARKER_WINDOW], anchor='start')
    for match in _RELEASE_SUFFIX.finditer(text):
        before = text[max(0, match.start() - _MARKER_WINDOW):match.start()]
        scan(before, anchor='end')
    return found


def _relative_date(kind: str, number: int | None, reference: datetime) -> tuple[str, bool]:
    if kind == 'just_now':
        return reference.date().isoformat(), True
    if kind == 'minutes_approx':
        return (reference - timedelta(minutes=5)).date().isoformat(), True
    if kind == 'half_hour':
        return (reference - timedelta(minutes=30)).date().isoformat(), False
    if kind == 'hours_approx':
        return (reference - timedelta(hours=3)).date().isoformat(), True
    if kind == 'half_day':
        return (reference - timedelta(hours=12)).date().isoformat(), True
    if kind == 'minutes_n':
        return (reference - timedelta(minutes=number or 0)).date().isoformat(), False
    if kind == 'hours_n':
        return (reference - timedelta(hours=number or 0)).date().isoformat(), False
    if kind == 'days_3':
        return (reference - timedelta(days=3)).date().isoformat(), False
    if kind == 'days_2':
        return (reference - timedelta(days=2)).date().isoformat(), False
    if kind == 'days_1':
        return (reference - timedelta(days=1)).date().isoformat(), False
    if kind == 'days_0':
        return reference.date().isoformat(), False
    if kind == 'days_n':
        return (reference - timedelta(days=number or 0)).date().isoformat(), False
    return '', False


def _result(date_value: str, source: str, *, expression='', reference='',
            estimated=False, conflict=False, reason='', flags=()) -> PublicationDate:
    flags = list(flags)
    if estimated:
        flags.append('date_estimated')
    if conflict:
        flags.append('date_conflict')
    if source == 'ambiguous':
        flags.append('date_ambiguous')
        reason = reason or 'date_ambiguous'
    if reason and reason not in flags:
        flags.append(reason)
    return PublicationDate(date=date_value, source=source, expression=expression,
                           reference=reference, estimated=estimated, conflict=conflict,
                           reason=reason, flags=tuple(sorted(set(flags))))


def _labeled(text: str) -> list[str]:
    return [value for match in _LABELED_DATE.finditer(text)
            if (value := _absolute(match[1]))]


def extract_publication_date(meta: Mapping[str, Any] | None, title: str,
                             blocks: Iterable[Any] | None, *,
                             now: datetime | None = None) -> PublicationDate:
    """Resolve the release date with meta > title > body absolute > relative.

    ``now`` makes relative/today calculations deterministic in tests; production
    callers use the Asia/Shanghai wall clock.
    """
    meta = dict(meta or {})
    title = str(title or '')
    body = _blocks_text(blocks or [])
    today = _today(now)
    captured = _meta_reference(meta.get('capturedAt'))

    # 1. Source-page metadata.
    meta_date = _absolute(str(meta.get('publishedAt') or ''))
    if not meta_date:
        raw_meta = str(meta.get('publishedAt') or '').strip()
        if raw_meta.startswith('今天'):
            meta_date = today.isoformat()
        elif raw_meta.startswith('昨天'):
            meta_date = (today - timedelta(days=1)).isoformat()
        elif raw_meta.startswith('前天'):
            meta_date = (today - timedelta(days=2)).isoformat()
    if meta_date and _today(now) and (date.fromisoformat(meta_date) - today).days > 1:
        return _result('', 'unknown', reference='meta', reason='date_invalid',
                       flags=['date_invalid'])

    # 2. Explicit title dates (client appends "（内容发布日期：YYYY-MM-DD）").
    title_values = _labeled(title)
    if not title_values:
        for match in _DATE_TOKEN.finditer(title):
            try:
                title_values.append(date(int(match[1]), int(match[2]), int(match[3])).isoformat())
            except ValueError:
                pass
            break

    # 3. Explicit absolute release dates in the first body blocks.
    body_values = _labeled(body)
    if not body_values:
        leading = re.match(rf'\s*({_DATE_TOKEN.pattern})', body)
        if leading and _absolute(leading.group(0)):
            body_values.append(_absolute(leading.group(0)))

    conflict = False
    absolute, source, reference = '', '', ''
    if meta_date:
        absolute, source, reference = meta_date, 'meta', 'meta'
        candidates = [value for value in (*title_values, *body_values) if value]
        if any(abs((date.fromisoformat(value) - date.fromisoformat(meta_date)).days) > 1
               for value in candidates):
            conflict = True
    elif title_values:
        if len(set(title_values)) > 1:
            return _result('', 'ambiguous', reference='absolute')
        absolute, source, reference = title_values[0], 'title', 'absolute'
        if any(abs((date.fromisoformat(value) - date.fromisoformat(absolute)).days) > 1
               for value in body_values):
            conflict = True
    elif body_values:
        if len(set(body_values)) > 1:
            return _result('', 'ambiguous', reference='absolute')
        absolute, source, reference = body_values[0], 'body_absolute', 'absolute'

    # 4. Relative release expressions. They are ignored once an absolute date
    # exists, but they still raise a date_conflict when they disagree >1 day.
    relative_text = title + '\n' + body
    relatives = _released_relative(relative_text, captured) if captured else []
    if not captured:
        relatives = []

    if absolute:
        conflict = conflict or any(
            item.date and abs((date.fromisoformat(item.date)
                               - date.fromisoformat(absolute)).days) > 1
            for item in relatives)
        return _result(absolute, source, reference=reference, conflict=conflict)

    if not relatives:
        return _result('', 'unknown', reason='date_unknown',
                       flags=['date_unknown'])

    if any(item.vague for item in relatives):
        return _result('', 'unknown', expression=next(
            item.expression for item in relatives if item.vague),
            reason='date_relative_vague', flags=['date_relative_vague'])

    values = [item.date for item in relatives if item.date]
    if not values:
        return _result('', 'unknown', reason='date_unknown', flags=['date_unknown'])
    if len(set(values)) > 1 and (
            max(date.fromisoformat(v) for v in values)
            - min(date.fromisoformat(v) for v in values)).days > 1:
        return _result('', 'ambiguous', reference='capturedAt')
    first = next(item for item in relatives if item.date)
    return _result(first.date, 'body_relative', expression=first.expression,
                   reference='capturedAt', estimated=first.estimated)


def window_bounds(days: int, *, now: datetime | None = None) -> tuple[str, str]:
    """Inclusive [fromDate, toDate] for "最近 N 天" (today included)."""
    today = _today(now)
    return (today - timedelta(days=days - 1)).isoformat(), today.isoformat()


def check_window(publication: PublicationDate, window_days: int | None, *,
                 now: datetime | None = None) -> str:
    """Return a rejection reason, or '' when the material passes the gate."""
    if window_days is None:
        return ''
    if not publication.date:
        return publication.reason or 'date_unknown'
    age = (_today(now) - date.fromisoformat(publication.date)).days
    if age < -1:
        return 'date_invalid'
    if age == -1:
        # Timezone/cross-midnight noise: not a trustworthy release date.
        return 'date_unknown'
    if age >= window_days:
        return 'date_window'
    return ''
