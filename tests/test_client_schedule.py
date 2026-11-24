"""Report schedule slots, enabled=false and single-run mutual exclusion."""
import asyncio
import time

from osbi_client.daemon import Daemon
from test_client import Browser


def test_due_report_slot_runs_once_and_is_exclusive(tmp_path):
    async def run():
        daemon = Daemon(tmp_path / "client", browser=Browser())
        daemon.state.save_config({"serverUrl": "https://example.test"})
        calls = []

        async def fake_run(profile_id, task_name):
            calls.append((profile_id, task_name))
            await asyncio.sleep(0)

        daemon._run_report = fake_run
        config = daemon.state.config()
        now = time.time()
        daemon.state.execute(
            "INSERT INTO report_schedules(profile,task,next_at) VALUES ('default','topnews',?)",
            (now - 1,))
        daemon._schedule_reports(config)  # due -> starts one task
        await asyncio.sleep(0.01)
        daemon._schedule_reports(config)  # still running/next slot future -> no second task
        await asyncio.sleep(0.05)
        assert calls == [('default', 'topnews')]
        assert daemon.report_running == {}
        await daemon.close()

    asyncio.run(run())


def test_disabled_task_is_not_scheduled(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"serverUrl": "https://example.test"})
    data = daemon.state.config().model_dump()
    data['reportProfiles'][0]['topnews']['enabled'] = False
    daemon.state.save_config({'reportProfiles': data['reportProfiles']})
    config = daemon.state.config()
    daemon.state.execute(
        "INSERT INTO report_schedules(profile,task,next_at) VALUES ('default','topnews',?)",
        (time.time() - 1,))
    daemon._schedule_reports(config)
    assert daemon.state.rows(
        "SELECT * FROM report_schedules WHERE task='topnews'") == []
    daemon.state.close()


def test_new_task_slot_is_future_not_immediate(tmp_path):
    daemon = Daemon(tmp_path / "client", browser=Browser())
    daemon.state.save_config({"serverUrl": "https://example.test"})
    config = daemon.state.config()
    daemon._schedule_reports(config)
    rows = {row['task']: row for row in daemon.state.rows("SELECT * FROM report_schedules")}
    assert rows['topnews']['next_at'] > time.time()
    assert rows['weeklyreport']['next_at'] > time.time()
    assert daemon.report_running == {}
    daemon.state.close()
