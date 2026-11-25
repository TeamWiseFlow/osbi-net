"""Daily source slots persist, enqueue once, and start the real capture loop."""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.source_schedule import CaptureSchedule, next_capture_at
from test_client import Browser


def stamp(value, zone='Asia/Shanghai', fold=0):
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(zone), fold=fold).timestamp()


def configure(daemon, *, times=None, zone='Asia/Shanghai', interval=0, enabled=True):
    source = dict(id='news', url='https://example.org/news', enabled=enabled,
                  intervalMinutes=interval, schedule=dict(times=times or [], timezone=zone))
    daemon.state.save_config(dict(serverUrl='https://shared.example', sources=[source]))
    return daemon.state.config()


@pytest.mark.parametrize('times', [['8:00'], ['24:00'], ['08:60'], ['08:00:00'],
                                  [''], ['08:00', '08:00'], [' 08:00'], [1]])
def test_capture_times_require_unique_24_hour_clocks(times):
    with pytest.raises(ValidationError):
        CaptureSchedule(times=times)


def test_schedule_sorts_times_and_validates_timezone():
    assert CaptureSchedule(times=['18:00', '08:00']).times == ['08:00', '18:00']
    with pytest.raises(ValidationError):
        CaptureSchedule(times=['08:00'], timezone='unknown/timezone')


@pytest.mark.parametrize(('now', 'expected'), [
    ('2026-11-25T07:59:59', '2026-11-25T08:00:00'),
    ('2026-11-25T08:00:00', '2026-11-25T12:30:00'),
    ('2026-11-25T18:01:00', '2026-11-26T08:00:00'),
])
def test_next_daily_slot_and_day_rollover(now, expected):
    schedule = CaptureSchedule(times=['08:00', '12:30', '18:00'])
    assert next_capture_at(schedule, now=stamp(now)) == stamp(expected)


def test_dst_skips_nonexistent_clocks_and_never_repeats_a_daily_clock():
    zone = 'America/New_York'
    schedule = CaptureSchedule(times=['02:30'], timezone=zone)
    assert next_capture_at(schedule, now=stamp('2026-03-08T01:59:00', zone)) == stamp('2026-03-09T02:30:00', zone)
    schedule = CaptureSchedule(times=['01:30'], timezone=zone)
    first = stamp('2026-11-01T01:30:00', zone, fold=0)
    assert next_capture_at(schedule, now=first-1) == first
    assert next_capture_at(schedule, now=first+1) == stamp('2026-11-02T01:30:00', zone)


def test_multiple_slots_enqueue_once_and_respect_active_runs(tmp_path, monkeypatch):
    monkeypatch.setattr('osbi_client.state.time.time', lambda: stamp('2026-11-25T07:00:00'))
    daemon = Daemon(tmp_path/'client', browser=Browser())
    try:
        config = configure(daemon, times=['08:00', '12:30', '18:00'])
        assert daemon.state.public_config()['sourceNextRuns']['news'] == stamp('2026-11-25T08:00:00')
        for now in [stamp('2026-11-25T08:00:00'), stamp('2026-11-25T08:00:01')]:
            daemon._schedule_sources(config, now=now)
        assert len(daemon.state.rows('SELECT * FROM runs')) == 1
        daemon._schedule_sources(config, now=stamp('2026-11-25T12:30:00'))
        assert len(daemon.state.rows('SELECT * FROM runs')) == 1
        assert daemon.state.events()[-1]['kind'] == 'source_schedule_skipped'
        daemon.state.execute("UPDATE runs SET state='succeeded'")
        daemon._schedule_sources(config, now=stamp('2026-11-25T18:00:00'))
        assert len(daemon.state.rows('SELECT * FROM runs')) == 2
        assert daemon.state.one('SELECT next_at FROM source_time_schedules')['next_at'] == stamp('2026-11-26T08:00:00')
    finally:
        daemon.state.close()


def test_daily_schedule_wins_over_interval_and_disable_removes_slots(tmp_path, monkeypatch):
    monkeypatch.setattr('osbi_client.state.time.time', lambda: stamp('2026-11-25T07:00:00'))
    daemon = Daemon(tmp_path/'client', browser=Browser())
    try:
        configure(daemon, interval=15)
        assert len(daemon.state.rows('SELECT * FROM schedules')) == 1
        configure(daemon, times=['08:00', '18:00'], interval=15)
        assert daemon.state.rows('SELECT * FROM schedules') == []
        assert len(daemon.state.rows('SELECT * FROM source_time_schedules')) == 1
        config = configure(daemon, times=['08:00'], enabled=False)
        daemon._schedule_sources(config, now=stamp('2026-11-25T09:00:00'))
        assert daemon.state.rows('SELECT * FROM source_time_schedules') == []
        assert daemon.state.rows('SELECT * FROM runs') == []
        configure(daemon, times=['08:00'])
        daemon.state.save_config({'sources': []})
        assert daemon.state.rows('SELECT * FROM source_time_schedules') == []
    finally:
        daemon.state.close()


def test_restart_and_unrelated_saves_preserve_slots_and_coalesce_downtime(tmp_path, monkeypatch):
    monkeypatch.setattr('osbi_client.state.time.time', lambda: stamp('2026-11-25T07:00:00'))
    directory = tmp_path/'client'
    first = Daemon(directory, browser=Browser())
    configure(first, times=['08:00', '12:30', '18:00'])
    expected = first.state.one('SELECT * FROM source_time_schedules')
    first.state.close()
    second = Daemon(directory, browser=Browser())
    try:
        second.state.save_config({'maxUrls': 10})
        assert second.state.one('SELECT * FROM source_time_schedules') == expected
        second._schedule_sources(second.state.config(), now=stamp('2026-11-26T17:00:00'))
        assert len(second.state.rows('SELECT * FROM runs')) == 1
        assert second.state.one('SELECT next_at FROM source_time_schedules')['next_at'] == stamp('2026-11-26T18:00:00')
        second._schedule_sources(second.state.config(), now=stamp('2026-11-26T17:00:01'))
        assert len(second.state.rows('SELECT * FROM runs')) == 1
    finally:
        second.state.close()


def test_changed_times_or_timezone_recompute_next_slot(tmp_path, monkeypatch):
    monkeypatch.setattr('osbi_client.state.time.time', lambda: stamp('2026-11-25T07:00:00'))
    daemon = Daemon(tmp_path/'client', browser=Browser())
    try:
        configure(daemon, times=['08:00'])
        configure(daemon, times=['12:30'])
        assert daemon.state.public_config()['sourceNextRuns']['news'] == stamp('2026-11-25T12:30:00')
        configure(daemon, times=['12:30'], zone='UTC')
        assert daemon.state.public_config()['sourceNextRuns']['news'] == stamp('2026-11-25T12:30:00', 'UTC')
    finally:
        daemon.state.close()


def test_legacy_interval_still_queues_automatically_without_daily_times(tmp_path, monkeypatch):
    now = stamp('2026-11-25T07:00:00')
    monkeypatch.setattr('osbi_client.state.time.time', lambda: now)
    daemon = Daemon(tmp_path/'client', browser=Browser())
    try:
        config = configure(daemon, interval=15)
        assert daemon.state.public_config()['sourceNextRuns']['news'] == now+900
        daemon._schedule_sources(config, now=now+900)
        assert len(daemon.state.rows('SELECT * FROM runs')) == 1
        assert daemon.state.one('SELECT next_at FROM schedules')['next_at'] == now+1800
    finally:
        daemon.state.close()


def test_background_loop_starts_scheduled_capture_without_manual_run(tmp_path):
    async def run():
        class TrackingBrowser(Browser):
            async def capture(self, session, url, lease, **kwargs):
                captured.append(url)
                return await super().capture(session, url, lease, **kwargs)
        captured = []
        daemon = Daemon(tmp_path/'client', browser=TrackingBrowser())
        configure(daemon, times=['08:00', '18:00'])
        daemon.state.execute('UPDATE source_time_schedules SET next_at=0')
        await daemon.start()
        try:
            for _ in range(100):
                if captured and daemon.state.one("SELECT id FROM runs WHERE state='succeeded'"):
                    break
                await asyncio.sleep(0.02)
            assert captured == ['https://example.org/news']
            assert len(daemon.state.rows('SELECT * FROM runs')) == 1
            assert daemon.state.one("SELECT id FROM runs WHERE state='succeeded'")
        finally:
            await daemon.close()
    asyncio.run(run())


def test_api_saves_multiple_times_and_rejects_invalid_configuration_atomically(tmp_path):
    daemon = Daemon(tmp_path/'client', browser=Browser())
    try:
        with TestClient(create_app(daemon=daemon), base_url='http://127.0.0.1') as client:
            client.get('/settings')
            source = dict(id='news', url='https://example.org', schedule={
                'times': ['18:00', '08:00'], 'timezone': 'Asia/Shanghai'})
            headers = {'X-OSBI-Local': '1'}
            response = client.patch('/api/config', json={'sources': [source]}, headers=headers)
            assert response.status_code == 200
            assert response.json()['sources'][0]['schedule']['times'] == ['08:00', '18:00']
            assert response.json()['sourceNextRuns']['news'] > 0
            source['schedule']['times'] = ['25:00']
            response = client.patch('/api/config', json={'sources': [source]}, headers=headers)
            assert response.status_code == 400
            assert client.get('/api/config').json()['sources'][0]['schedule']['times'] == ['08:00', '18:00']
    finally:
        daemon.state.close()
