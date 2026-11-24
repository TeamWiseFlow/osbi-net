"""Navigation screening uses only unambiguous absolute release-date fields."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from wiseflow_server.link_publication_date import link_date_rejection

NOW = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai'))


@pytest.mark.parametrize('text,context,reason', [
    ('2020-01-01', None, 'date_window'),
    ('某政策 2020-01-01', None, 'date_window'),
    ('某政策（2020年1月1日）', None, 'date_window'),
    ('【2020-01-01】某政策', None, 'date_window'),
    ('2026-11-22 政策通知', None, ''),
    ('某政策', '2026年11月21日', ''),
    ('某政策', '发布日期：2020-01-01', 'date_window'),
    ('某政策', '公布日期：2020年1月1日', 'date_window'),
    ('某政策', '印发日期：2020/01/01', 'date_window'),
    ('某政策', '成文日期：2020.01.01', 'date_window'),
    ('某政策', '签发日期：2020-01-01', 'date_window'),
    ('某政策', '更新日期：2020-01-01', 'date_window'),
    ('某政策', '发布于2020年01月01日', 'date_window'),
    ('2020-01-01发布的政策', None, 'date_window'),
    ('2020-01-01发布《关于开展培训的通知》', None, 'date_window'),
    ('某政策', '发布日期：2026-02-30', 'date_invalid'),
    ('2026-13-01 某政策', None, 'date_invalid'),
    ('2026-11-00', None, 'date_invalid'),
    ('0000-11-22', None, 'date_invalid'),
    ('某政策', '发布日期：2026-11-22，更新日期：2026-11-23', 'date_ambiguous'),
    ('2026-11-22', '2026-11-23', 'date_ambiguous'),
    ('某政策', '发布日期：2026年11月22日，公布日期：2026-11-22', ''),
    ('某政策', '发布日期：2026-11-24', 'date_window'),
    # Explicit release fields beat unrelated bare dates in the other field.
    ('2020-01-01某政策', '发布日期：2026-11-22', ''),
    ('某政策 2026-11-22', '发布日期：2020-01-01', 'date_window'),
    ('2020-01-01举行政策宣讲活动', '发布日期：2026-11-22', ''),
    ('某政策', None, ''),
    ('某政策', '昨天发布', ''),
    ('某政策', '更新于72小时前', ''),
    ('某政策', '发布于刚刚', ''),
    ('某政策', '近日发布', ''),
    ('2020年印发某政策', None, ''),
    ('第三届2020年政策论坛', None, ''),
    ('2020-01-01召开第二届会议', None, ''),
    ('某政策', '活动日期：2020-01-01', ''),
    ('某政策', '生效日期：2020-01-01', ''),
    ('某政策', '事故发生于2020-01-01', ''),
    ('2020-01-01新闻发布会', None, ''),
    ('某政策', '报名截止 2020-01-01', ''),
    ('某政策', 'event_date: 2020-01-01', ''),
    ('回顾政策沿革 2020-01-01', None, ''),
    ('关于2020-01-01举办培训的通知', None, ''),
    ('编号A2020-01-01X政策', None, ''),
    ('某政策', 'https://example.org/archive/2020/01/01', ''),
    # Invalid calendar dates are still ignored when clearly an event date.
    ('某政策', '会议日期：2026-02-30', ''),
])
def test_link_date_screening(text, context, reason):
    assert link_date_rejection(text, context, 3, now=NOW) == reason


def test_non_window_link_never_rejected():
    assert link_date_rejection('发布日期：2020-01-01', None, None, now=NOW) == ''
