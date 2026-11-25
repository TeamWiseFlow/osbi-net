"""Persistent per-publisher repetition penalty for shadow topnews.

Ported from midplatform/workflows/publisher_penalty.py. Publisher identity:
wechat articles use the ``//publisher date//`` prefix author when present,
everything else uses the URL domain. Daily ledger: selected -20, unselected
+10, clamped to [-300, +30]. The JSON state file lives next to the daily
``topnews_graph_*.txt`` outputs in the client reports directory and seeds
itself from recent outputs on first use.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import re
from urllib.parse import urlparse

DEFAULT_PENALTY = 20
DEFAULT_RECOVERY = 10
DEFAULT_FLOOR = -300
DEFAULT_CEILING = 30
DEFAULT_SEED_DAYS = 14
MAX_SELECTED_DATES = 60
STATE_FILENAME = 'publisher_penalty.json'
WECHAT_HOST = 'mp.weixin.qq.com'

_OUTPUT_DATE_RE = re.compile(r'topnews_graph_(\d{4}-\d{2}-\d{2})\.txt$')
_URL_RE = re.compile(r'^https?://\S+$')
_WECHAT_PREFIX_RE = re.compile(
    r'//([^/\n]{1,80}?)\s+'
    r'(?:\d{4}[-年]\d{1,2}[-月]\d{1,2}日?'
    r'|今天\s*\d{1,2}:\d{2}'
    r'|昨天\s*\d{1,2}:\d{2}'
    r'|\d{1,2}月\d{1,2}日)'
    r'(?:\s+\d{1,2}:\d{2})?\s*//')


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


def default_state_path() -> Path:
    base = Path(os.environ.get('OSBI_CLIENT_DATA', '~/.osbi/client')).expanduser()
    return base / 'reports' / STATE_FILENAME


def _wechat_publisher(raw_text: str) -> str:
    match = _WECHAT_PREFIX_RE.search(str(raw_text or ''))
    return match.group(1).strip() if match else ''


def publisher_key(url: str, publisher: str = '', raw_text: str = '') -> str:
    """Stable publisher identity used by the repetition ledger."""
    url = str(url or '').strip()
    host = urlparse(url).netloc.lower()
    if WECHAT_HOST in host:
        return (_wechat_publisher(raw_text)
                or str(publisher or '').strip()
                or WECHAT_HOST)
    host = host.split(':', 1)[0]
    if host.startswith('www.'):
        host = host[4:]
    return host or str(publisher or '').strip()


def _clamp(value: float, floor: float, ceiling: float) -> float:
    return max(floor, min(ceiling, value))


class PublisherPenaltyLedger:
    """JSON-backed publisher scores, updated once per topnews day."""

    def __init__(self, path=None, *, penalty: int | None = None,
                 recovery: int | None = None, floor: int | None = None,
                 ceiling: int | None = None, seed_days: int | None = None):
        self.path = Path(path) if path else default_state_path()
        self.penalty = (_env_int('ONTOLOGY_SHADOW_PUBLISHER_PENALTY',
                                 DEFAULT_PENALTY) if penalty is None
                        else int(penalty))
        self.recovery = (_env_int('ONTOLOGY_SHADOW_PUBLISHER_RECOVERY',
                                  DEFAULT_RECOVERY) if recovery is None
                         else int(recovery))
        self.floor = (_env_int('ONTOLOGY_SHADOW_PUBLISHER_FLOOR',
                               DEFAULT_FLOOR) if floor is None
                      else int(floor))
        self.ceiling = DEFAULT_CEILING if ceiling is None else int(ceiling)
        self.seed_days = (_env_int('ONTOLOGY_SHADOW_PUBLISHER_SEED_DAYS',
                                   DEFAULT_SEED_DAYS) if seed_days is None
                          else int(seed_days))
        self.updated_date = ''
        self.scores: dict[str, float] = {}
        self.selected_dates: dict[str, list[str]] = {}

    # -- state I/O ---------------------------------------------------------

    def load(self) -> 'PublisherPenaltyLedger':
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return self
        if not isinstance(data, dict):
            return self
        self.updated_date = str(data.get('updatedDate') or '')
        publishers = data.get('publishers') or {}
        if isinstance(publishers, dict):
            for key, value in publishers.items():
                if not isinstance(value, dict):
                    continue
                try:
                    score = float(value.get('score') or 0)
                except (TypeError, ValueError):
                    score = 0.0
                self.scores[str(key)] = _clamp(score, self.floor, self.ceiling)
                dates = value.get('selectedDates') or []
                if isinstance(dates, list):
                    self.selected_dates[str(key)] = [
                        str(item) for item in dates[-MAX_SELECTED_DATES:]]
        return self

    def save(self) -> None:
        payload = dict(
            version=1,
            updatedDate=self.updated_date,
            publishers={
                key: dict(score=round(float(score), 2),
                          selectedDates=self.selected_dates.get(key, []))
                for key, score in sorted(self.scores.items())
                if score != 0 or self.selected_dates.get(key)
            },
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding='utf-8')
        tmp.replace(self.path)

    def reset(self, date: str | None = None) -> None:
        """Clear scores and mark today handled so seeding does not repopulate them."""
        self.scores = {}
        self.selected_dates = {}
        self.updated_date = str(date or datetime.now().strftime('%Y-%m-%d'))
        self.save()

    def penalties(self) -> dict[str, float]:
        return dict(self.scores)

    # -- daily updates -----------------------------------------------------

    def advance_to_before(self, date: str) -> 'PublisherPenaltyLedger':
        """Apply missing days up to ``date - 1`` as "not selected"."""
        if not self.updated_date or date <= self.updated_date:
            return self
        day = self._next_day(self.updated_date)
        guard = 0
        while day < date and guard < 366:
            self._apply_day(day, set())
            day = self._next_day(day)
            guard += 1
        return self

    def record(self, date: str, selected_keys, participant_keys=(),
               *, persist: bool = True) -> bool:
        date = str(date)
        if self.updated_date and date <= self.updated_date:
            return False
        self.advance_to_before(date)
        self._apply_day(date, set(selected_keys), set(participant_keys))
        if persist:
            self.save()
        return True

    def _apply_day(self, date: str, selected_keys: set,
                   participant_keys: set = frozenset()) -> None:
        keys = set(self.scores) | set(participant_keys) | set(selected_keys)
        for key in keys:
            if not key:
                continue
            current = self.scores.get(key, 0.0)
            delta = -self.penalty if key in selected_keys else self.recovery
            self.scores[key] = _clamp(current + delta, self.floor, self.ceiling)
            if key in selected_keys:
                dates = self.selected_dates.setdefault(key, [])
                if date not in dates:
                    dates.append(date)
                del dates[:-MAX_SELECTED_DATES]
        self.updated_date = date

    @staticmethod
    def _next_day(date: str) -> str:
        return (datetime.strptime(date, '%Y-%m-%d')
                + timedelta(days=1)).strftime('%Y-%m-%d')

    # -- seeding from existing daily outputs ------------------------------

    def seed_from_outputs(self, output_dir=None,
                          before_date: str | None = None
                          ) -> 'PublisherPenaltyLedger':
        if self.updated_date:
            return self
        directory = Path(output_dir) if output_dir else self.path.parent
        dates = self._output_dates(directory, before_date)
        for date in dates:
            if self.updated_date and date <= self.updated_date:
                continue
            selected = self._read_output_keys(
                directory / f'topnews_graph_{date}.txt')
            self.record(date, selected, persist=False)
        return self

    def _output_dates(self, directory: Path,
                      before_date: str | None) -> list[str]:
        dated = []
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        for name in names:
            match = _OUTPUT_DATE_RE.search(name)
            if not match:
                continue
            date = match.group(1)
            if before_date and date >= before_date:
                continue
            dated.append(date)
        dated.sort()
        return dated[-self.seed_days:]

    @staticmethod
    def _read_output_keys(path: Path) -> set[str]:
        keys: set[str] = set()
        pending = ''
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except OSError:
            return keys
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            match = _WECHAT_PREFIX_RE.match(line)
            if match:
                pending = match.group(1).strip()
                continue
            if _URL_RE.match(line):
                keys.add(pending or publisher_key(line))
                pending = ''
                continue
            if line.startswith('#'):
                pending = ''
        return keys

    def snapshot(self) -> dict:
        return dict(updatedDate=self.updated_date,
                    penalty=self.penalty, recovery=self.recovery,
                    floor=self.floor, ceiling=self.ceiling,
                    scores=dict(self.scores))
