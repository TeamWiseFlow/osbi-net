"""Sections + task overlays, schedules and target references."""
import pytest
from pydantic import ValidationError

from osbi_client.models import ClientConfig, NodeReportRules
from osbi_client.report_settings import (ReportProfile, ReportSchedule,
                                         board_views, next_run_at,
                                         resolve_board)


def config_dict():
    return ClientConfig().model_dump()


def test_sections_and_task_overlays_merge_without_mutating_section():
    config = ClientConfig()
    views = board_views(config, 'default', 'topnews')
    economy = next(view for view in views if view.title == '经济动态')
    assert economy.channels == ('economy',)
    assert economy.selector == 'candidates' and economy.topN == 3
    weekly = board_views(config, 'default', 'weeklyreport')
    weekly_economy = next(view for view in weekly if view.title == '经济动态')
    assert weekly_economy.selector == 'policy' and weekly_economy.topN == 2
    # Section template itself is unchanged by the task overlay.
    section = config.reportProfiles[0].sections[1]
    assert not hasattr(section, 'selector')


def test_resolve_board_validates_task_and_channel():
    config = ClientConfig()
    profile, board, intent = resolve_board(config, 'default', 'weeklyreport',
                                           '社区治理', 'community')
    assert board.channels == ('community',)
    assert board.selector == 'action' and board.topN == 2
    with pytest.raises(ValueError):
        resolve_board(config, 'default', 'weeklyreport', '社区治理', 'economy')
    with pytest.raises(ValueError):
        resolve_board(config, 'missing', 'weeklyreport', '社区治理', 'community')


def test_duplicate_section_per_task_is_rejected():
    data = config_dict()
    profile = data['reportProfiles'][0]
    profile['topnews']['boards'].append(dict(profile['topnews']['boards'][0]))
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(data)


def test_unknown_section_or_target_reference_is_rejected():
    data = config_dict()
    profile = data['reportProfiles'][0]
    profile['topnews']['boards'][0]['section'] = 'missing'
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(data)
    data = config_dict()
    data['reportProfiles'][0]['topnews']['targetIds'] = ['missing']
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(data)


def test_schedule_validation_and_next_run():
    with pytest.raises(ValidationError):
        ReportSchedule(time='8:40')
    with pytest.raises(ValidationError):
        ReportSchedule(time='25:00')
    with pytest.raises(ValidationError):
        ReportSchedule(timezone='Not/AZone')
    daily = ReportSchedule(time='08:40')
    weekly = ReportSchedule(time='07:50', weekday='monday')
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 11, 23, 9, 0, tzinfo=ZoneInfo('Asia/Shanghai'))  # Monday
    assert next_run_at(daily, now=now) > now.timestamp()  # next day
    assert next_run_at(weekly, now=now) > now.timestamp()  # next Monday


def test_enabled_task_requires_a_configured_section():
    data = config_dict()
    data['reportProfiles'][0]['topnews']['boards'] = []
    with pytest.raises(ValidationError):
        ClientConfig.model_validate(data)


def test_source_scope_layers_from_global_through_section_to_board():
    from osbi_client.models import QueryDefaults
    data = config_dict()
    data['queryDefaults'] = {'sourceScope': 'own'}
    data['reportProfiles'][0]['sections'][0]['query']['sourceScope'] = 'inherit'
    data['reportProfiles'][0]['topnews']['boards'][0]['sourceScope'] = 'inherit'
    config = ClientConfig.model_validate(data)
    own = board_views(config, 'default', 'topnews')[0]
    assert own.sourceScope == 'own'
    data['reportProfiles'][0]['sections'][0]['query']['sourceScope'] = 'all'
    config = ClientConfig.model_validate(data)
    assert board_views(config, 'default', 'topnews')[0].sourceScope == 'all'
    data['reportProfiles'][0]['topnews']['boards'][0]['sourceScope'] = 'own'
    config = ClientConfig.model_validate(data)
    assert board_views(config, 'default', 'topnews')[0].sourceScope == 'own'


def test_node_rules_defaults_are_shanghai_parity_and_overridable():
    assert ClientConfig().nodeRules.city == '上海市'
    rules = NodeReportRules(nodeName='成都市锦江区狮子山街道', city='成都市',
                            district='锦江区', street='狮子山街道',
                            relationHints=['成都', '锦江'])
    assert rules.city == '成都市'
