"""Publication-date resolution and the 3-day ingestion gate (doc 5.7)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from wiseflow_server.publication_date import (PublicationDate, check_window,
                                               extract_publication_date,
                                               window_bounds)

NOW = datetime(2026, 11, 23, 12, tzinfo=ZoneInfo('Asia/Shanghai'))
CAPTURED = '2026-11-23T10:00:00+08:00'


def resolve(meta=None, title='', blocks=(), *, captured=CAPTURED):
    data = dict(meta or {})
    if captured is not None:
        data.setdefault('capturedAt', captured)
    return extract_publication_date(data, title, [{'text': text} for text in blocks],
                                    now=NOW)


def reason(publication, days=3):
    return check_window(publication, days, now=NOW)


def test_meta_date_inside_window_passes():
    publication = resolve({'publishedAt': '2026-11-21'})
    assert publication.date == '2026-11-21'
    assert publication.source == 'meta'
    assert reason(publication) == ''


def test_meta_date_outside_window_is_rejected():
    publication = resolve({'publishedAt': '2026-11-18'})
    assert reason(publication) == 'date_window'


def test_title_explicit_release_date_is_used():
    publication = resolve({}, '某通知（内容发布日期：2026-11-22）')
    assert (publication.date, publication.source) == ('2026-11-22', 'title')
    assert reason(publication) == ''


def test_body_labeled_absolute_release_date():
    publication = resolve({}, '某通知', ['发布日期：2026-11-21。'])
    assert (publication.date, publication.source) == ('2026-11-21', 'body_absolute')


def test_relative_three_days_ago_is_rejected():
    publication = resolve({}, '', ['正文。发布于3天前。'])
    assert publication.date == '2026-11-20'
    assert publication.source == 'body_relative'
    assert publication.reference == 'capturedAt'
    assert reason(publication) == 'date_window'


def test_relative_two_days_ago_passes():
    publication = resolve({}, '', ['2天前发布。'])
    assert publication.date == '2026-11-21'
    assert reason(publication) == ''


def test_hours_cross_midnight_by_shanghai_date():
    assert resolve({}, '', ['24小时前发布。']).date == '2026-11-22'
    assert reason(resolve({}, '', ['72小时前发布。'])) == 'date_window'


def test_yesterday_and_day_before_pass_before_yesterday_rejected():
    assert reason(resolve({}, '', ['昨日发布。'])) == ''
    assert reason(resolve({}, '', ['前日发布。'])) == ''
    assert reason(resolve({}, '', ['大前天发布。'])) == 'date_window'


def test_week_month_year_are_vague_and_rejected():
    for expression in ('一周前发布。', '上个月发布。', '去年发布。'):
        publication = resolve({}, '', [expression])
        assert publication.date == ''
        assert reason(publication) == 'date_relative_vague'


def test_vague_relative_expressions_are_unknown():
    for expression in ('近日发布。', '日前发布。', '近期发布。', '不久前发布。',
                       '几天前发布。', '2天以前发布。'):
        publication = resolve({}, '', [expression])
        assert publication.date == ''
        assert reason(publication) == 'date_relative_vague'


def test_just_now_is_estimated_but_inside_window():
    publication = resolve({}, '', ['发布于刚刚。'])
    assert publication.date == '2026-11-23'
    assert publication.estimated is True
    assert 'date_estimated' in publication.flags
    assert reason(publication) == ''


def test_chinese_numerals_match_arabic_behaviour():
    assert resolve({}, '', ['两天前发布。']).date == resolve({}, '', ['2天前发布。']).date
    assert (resolve({}, '', ['二十四小时前发布。']).date
            == resolve({}, '', ['24小时前发布。']).date)


def test_event_relative_time_is_not_a_release_date():
    publication = resolve({}, '', ['事故发生于3天前。'])
    assert publication.date == ''
    assert reason(publication) == 'date_unknown'


def test_relative_without_captured_at_is_unknown():
    publication = resolve({}, '', ['发布于3天前。'], captured=None)
    assert publication.date == ''
    assert reason(publication) == 'date_unknown'


def test_absolute_and_relative_conflict_uses_absolute_and_flags():
    publication = resolve({}, '某通知（内容发布日期：2026-11-21）',
                          ['4天前发布。'])
    assert publication.date == '2026-11-21'
    assert publication.conflict is True
    assert 'date_conflict' in publication.flags
    assert reason(publication) == ''


def test_conflicting_relative_expressions_are_ambiguous():
    publication = resolve({}, '', ['昨天发布。3天前发布。'])
    assert publication.source == 'ambiguous'
    assert publication.date == ''
    assert reason(publication) == 'date_ambiguous'


def test_conflicting_body_absolute_dates_are_ambiguous():
    publication = resolve({}, '某通知',
                          ['发布日期：2026-11-20。公布日期：2026-11-10。'])
    assert publication.source == 'ambiguous'
    assert reason(publication) == 'date_ambiguous'


def test_event_date_in_window_does_not_override_old_release_date():
    publication = resolve({}, '', ['发布日期：2026-11-10。事故发生于今天。'])
    assert publication.date == '2026-11-10'
    assert reason(publication) == 'date_window'


def test_future_date_beyond_timezone_tolerance_is_invalid():
    publication = resolve({'publishedAt': '2026-11-25'})
    assert publication.date == ''
    assert reason(publication) == 'date_invalid'


def test_one_day_future_is_treated_as_unknown():
    publication = resolve({'publishedAt': '2026-11-24'})
    assert reason(publication) == 'date_unknown'


def test_window_bounds_cover_today_and_two_prior_days():
    assert window_bounds(3, now=NOW) == ('2026-11-21', '2026-11-23')


def test_non_window_channel_never_rejects(monkeypatch):
    publication = PublicationDate()
    assert check_window(publication, None) == ''
