"""Per-profile DOCX base names keep date suffixes across report and API flows."""
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from pydantic import ValidationError

from osbi_client.app import create_app
from osbi_client.daemon import Daemon
from osbi_client.report_settings import ReportSchedule, ReportTask
from osbi_client import shadow_reports
from test_shadow_reports import fake_daemon, run


@pytest.mark.parametrize(('value', 'expected'), [
    ('Weeklyreport', 'Weeklyreport'), (' Council report ', 'Council report'),
    ('周报', '周报'), ('Council.report.docx', 'Council.report'),
    ('Council.DOCX', 'Council'),
])
def test_docx_base_name_normalization(value, expected):
    task = ReportTask(enabled=False, schedule=ReportSchedule(), docxFilename=value)
    assert task.docxFilename == expected


@pytest.mark.parametrize('value', ['', ' ', '..', '.', '.docx', '../report',
    'folder/report', 'folder\\report', 'bad:name', 'bad?name', 'bad*name',
    'bad\nname', 'bad\x00name', 'trailing.', 'x'*121, '周'*81])
def test_docx_name_rejects_paths_controls_and_unportable_names(value):
    with pytest.raises(ValidationError):
        ReportTask(enabled=False, schedule=ReportSchedule(), docxFilename=value)


def test_custom_docx_names_follow_generation_date_and_reach_delivery(tmp_path, monkeypatch):
    class Clock(date):
        day = 25

        @classmethod
        def today(cls):
            return date(2026, 11, cls.day)

    async def text_delivery(*args, **kwargs):
        return dict(sent=True, dryRun=False)

    deliveries = []

    async def file_delivery(*args, **kwargs):
        deliveries.append(kwargs)
        return dict(sent=True, dryRun=False)

    monkeypatch.setattr(shadow_reports, 'date', Clock)
    monkeypatch.setattr(shadow_reports, '_deliver', text_delivery)
    monkeypatch.setattr(shadow_reports, 'deliver_targets', file_delivery)
    daemon = fake_daemon(tmp_path, {}, report_llm={'enabled': False})
    try:
        profiles = daemon.state.config().reportProfiles
        profiles[0].weeklyreport.docxFilename = 'Council weekly'
        daemon.state.save_config({'reportProfiles': [profile.model_dump() for profile in profiles]})
        for day in (25, 26):
            Clock.day = day
            result = run(shadow_reports.run_weekly(daemon, use_llm=False, send=True))
            docx = Path(result['docxPath'])
            assert docx.name == f'Council weekly_202611{day}.docx' and docx.is_file()
            assert not result['docxError']
            assert deliveries[-1]['file_name'] == docx.name
            assert shadow_reports.download_report(daemon, docx.name) == docx
            assert docx.name in {item['name'] for item in shadow_reports.list_reports(daemon)}
            assert Path(result['path']).name.startswith('基层工作动态回顾与热点展望_图谱版_')
        assert len(list((tmp_path/'reports').glob('Council weekly_*.docx'))) == 2
    finally:
        daemon.state.close()


def test_dashboard_config_persists_docx_name_and_download_uses_it(tmp_path):
    daemon = Daemon(tmp_path/'client')
    try:
        with TestClient(create_app(daemon=daemon), base_url='http://127.0.0.1') as client:
            shell = client.get('/tasks')
            assert '<html lang="en">' in shell.text
            assert '/static/i18n.js?v=' in shell.text
            profiles = client.get('/api/config').json()['reportProfiles']
            profiles[0]['weeklyreport']['docxFilename'] = '部门周报.docx'
            response = client.patch('/api/config', json={'reportProfiles': profiles},
                                    headers={'X-OSBI-Local': '1'})
            assert response.status_code == 200
            assert response.json()['reportProfiles'][0]['weeklyreport']['docxFilename'] == '部门周报'
            assert client.get('/api/config').json()['reportProfiles'][0]['weeklyreport']['docxFilename'] == '部门周报'
            directory = shadow_reports.reports_dir(daemon)
            saved = shadow_reports._save_outputs('# Report', 'Digest', directory, '20261125', docx_filename='部门周报')
            name = Path(saved['docx']).name
            response = client.get('/api/reports/download', params={'name': name})
            assert response.status_code == 200
            assert response.headers['content-type'].startswith('application/vnd.openxmlformats-officedocument')
            assert 'filename*=utf-8' in response.headers['content-disposition']
            assert response.content == Path(saved['docx']).read_bytes()
    finally:
        daemon.state.close()
