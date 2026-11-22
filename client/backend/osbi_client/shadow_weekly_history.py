"""Cross-week coverage adapter for the shadow weekly pipeline.

Production records selected hotspots right after selection, action boards
only (基层党建/社区治理/数字社区), idempotent per week. This adapter keeps that
timing/scope on top of the client weekly_hotspots ledger, using a dedicated
``shadow`` channel scope so the legacy per-board pipelines stay isolated.
Prior-policy matching reads policy-board rows from the same ledger (empty
until policy boards are recorded/backfilled, mirroring a fresh production
weekly_events.db).
"""
from __future__ import annotations

import logging

from .weekly_history import (load_reported_hotspots, record_reported_hotspots)

logger = logging.getLogger(__name__)

CHANNEL = 'shadow'
PROFILE = 'default'
ACTION_BOARDS = {'基层党建', '社区治理', '数字社区'}
POLICY_BOARDS = {'经济动态', '政策速递'}


def load_shadow_priors(state, customer: str, before_date: str) -> list:
    """Hotspots reported before ``before_date`` (upper bound excludes self)."""
    return load_reported_hotspots(state, customer, CHANNEL, PROFILE, before_date)


def record_shadow_hotspots(state, customer: str, week_end: str,
                           focus_by_board: dict) -> int:
    """Persist this week's selected action hotspots (idempotent per week)."""
    action_only = {board: focus for board, focus in (focus_by_board or {}).items()
                   if board in ACTION_BOARDS}
    count = record_reported_hotspots(state, customer, CHANNEL, PROFILE,
                                     week_end, action_only)
    logger.info('shadow weekly history: recorded %s hotspots for week %s',
                count, week_end)
    return count


def load_prior_policies(state, customer: str, before_date: str,
                        *, limit: int = 400) -> list:
    """Prior policy topics from policy-board coverage rows, newest first."""
    rows = state.rows(
        'SELECT week_end, board, topic FROM weekly_hotspots '
        'WHERE customer=? AND channel=? AND profile=? AND week_end<? '
        'ORDER BY week_end DESC LIMIT ?',
        (customer, CHANNEL, PROFILE, before_date, limit))
    return [dict(weekEnd=row['week_end'], section=row['board'],
                 topic=row['topic'], units='', summary='')
            for row in rows if row['board'] in POLICY_BOARDS and row['topic']]
