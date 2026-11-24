"""End-to-end shadow report orchestration with a mocked server and no model.

Covers the deterministic fallback paths: LLM disabled/unavailable must still
produce readable topnews/weekly files, and the data-scarce weekly overview.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from osbi_client.models import ClientConfig, ReportLlmConfig
from osbi_client.shadow_llm import ReportLlm
from osbi_client.shadow_reports import (latest_report, list_reports, read_report,
                                        run_topnews, run_weekly)
from osbi_client.shadow_topnews import select_topnews
from osbi_client.shadow_weekly import write_weekly
from osbi_client.state import State
from test_shadow_select import (action_records, evidence, policy_records, service)


def run(coro):
    return asyncio.run(coro)


def fake_daemon(tmp_path, records_by_channel, *, report_llm=None):
    state = State(tmp_path / "client.db")
    update = {"serverUrl": "http://127.0.0.1:8078"}
    if report_llm is not None:
        update["reportLlm"] = report_llm
    state.save_config(update)

    def handler(request):
        channel = request.url.path.split("/")[3]
        records = records_by_channel.get(channel, [])
        return httpx.Response(200, json={"items": records, "cursor": ""})

    return SimpleNamespace(data_dir=tmp_path, state=state,
                           transport=httpx.MockTransport(handler))


def test_report_llm_config_defaults_local_and_guards_remote():
    config = ClientConfig()
    llm = ReportLlm.from_config(config)
    assert llm and llm.base_url == "http://127.0.0.1:9997/v1" and llm.model == "xin_local"
    assert ReportLlm.from_config(SimpleNamespace(
        reportLlm=ReportLlmConfig(enabled=False))) is None
    # The config model itself rejects remote endpoints without a key.
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ReportLlmConfig(baseUrl="https://api.example.com/v1")
    # from_config also guards unvalidated settings objects.
    assert ReportLlm.from_config(SimpleNamespace(reportLlm=SimpleNamespace(
        enabled=True, baseUrl="https://api.example.com/v1", apiKey="",
        model="m", timeoutSeconds=240))) is None
    remote = ReportLlm.from_config(SimpleNamespace(
        reportLlm=ReportLlmConfig(baseUrl="https://api.example.com/v1", apiKey="k")))
    assert remote and remote.api_key == "k"


def test_select_topnews_without_llm_falls_back_deterministically():
    records = [evidence(title="静安区社区治理创新案例", url="https://example.org/c1",
                        text="静安区临汾路街道社区治理创新。",
                        entities=[dict(uri="e1", name="社区治理创新", label="治理机制")],
                        relations=[dict(subject="e1", object="o1", label="参与")],
                        boards=("社区治理",))]
    result = run(select_topnews({"policy": service(records)},
                                from_date="2026-11-20", to_date="2026-11-20",
                                llm=None))
    assert result["source"] == "fallback"
    assert result["selected"] and result["boardSources"]["社区治理"].startswith("fallback")


def test_write_weekly_without_llm_produces_readable_markdown():
    records = action_records()
    services = {"policy": service(records)}
    from osbi_client.shadow_candidates import weekly_dossier
    from osbi_client.shadow_relations import build_relation_dossier
    from osbi_client.shadow_weekly_select import select_weekly_focus
    dossier = weekly_dossier(services, from_date="2026-11-16", to_date="2026-11-22")
    relations = build_relation_dossier(services, from_date="2026-11-16",
                                       to_date="2026-11-22")
    focus = select_weekly_focus(services, from_date="2026-11-16",
                                to_date="2026-11-22", relation_dossier=relations)
    assert focus
    written = run(write_weekly(None, focus, from_date="2026-11-16",
                               to_date="2026-11-22", dossier=dossier,
                               relations=relations))
    assert written["markdown"].startswith("# 基层工作动态回顾与热点展望")
    assert "## 一、" in written["markdown"] and "综合研判" in written["markdown"]
    assert "本周导读" in written["markdown"]
    assert written["digest"] and written["pushText"]
    assert all(s["source"].startswith("fallback") for s in written["sections"].values())


def test_run_topnews_saves_file_and_records_penalty(tmp_path):
    records = [evidence(title="静安区就业服务进社区", url="https://example.org/t1",
                        published="2026-11-22", text="静安区临汾路街道就业服务。",
                        entities=[dict(uri="e1", name="就业服务活动", label="社区行动")],
                        relations=[dict(subject="e1", object="o1", label="参与")],
                        boards=("经济动态",), publisher="静安发布")]
    daemon = fake_daemon(tmp_path, {"economy": records},
                         report_llm={"enabled": False})
    try:
        result = run(run_topnews(daemon, from_date="2026-11-22", to_date="2026-11-22"))
        assert result["selectedCount"] == 1
        assert result["source"] == "fallback"
        path = Path(result["path"])
        assert path.is_file() and "#经济动态" in path.read_text(encoding="utf-8")
        assert result["publisherPenalty"]["recorded"] is True
        ledger = json.loads((tmp_path / "reports" / "publisher_penalty.json").read_text())
        from datetime import date
        assert ledger["updatedDate"] == date.today().isoformat()
        latest = latest_report(daemon, "topnews")
        assert latest and latest["name"] == path.name
        assert read_report(daemon, "topnews", path.name)["content"] == result["content"]
        with pytest.raises(ValueError):
            read_report(daemon, "topnews", "../client.db")
        assert len(list_reports(daemon)) == 1
    finally:
        daemon.state.close()


def test_run_weekly_policy_chain_board_without_llm(tmp_path):
    daemon = fake_daemon(tmp_path, {"policy-latest": policy_records()},
                         report_llm={"enabled": False})
    try:
        result = run(run_weekly(daemon, from_date="2026-11-16", to_date="2026-11-22"))
        assert result["dataScarce"] is False
        assert "政策速递" in result["boards"]
        assert Path(result["path"]).is_file() and Path(result["digestPath"]).is_file()
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "静安区促进就业实施方案" in content and "综合研判" in content
        assert latest_report(daemon, "weekly")["name"].endswith(".md")
    finally:
        daemon.state.close()


def test_run_weekly_data_scarce_overview_when_no_board_qualifies(tmp_path):
    # National macro-finance material: no local signal, no chains, no hotspots.
    records = [evidence(title="A股三大指数涨跌互现", url="https://example.org/m1",
                        text="A股港股行情复盘，个股分红回购。", boards=("经济动态",),
                        entities=[dict(uri="m", name="A股市场", label="概念议题")])]
    daemon = fake_daemon(tmp_path, {"economy": records},
                         report_llm={"enabled": False})
    try:
        result = run(run_weekly(daemon, from_date="2026-11-16", to_date="2026-11-22"))
        assert result["dataScarce"] is True
        assert result["boards"] == ["材料概览"]
        content = Path(result["path"]).read_text(encoding="utf-8")
        assert "数据不足兜底" in content and "A股三大指数" in content
        assert set(result["omittedBoards"]) == {"基层党建", "经济动态", "社区治理",
                                                "数字社区", "政策速递"}
    finally:
        daemon.state.close()


def test_run_topnews_empty_window_returns_no_file(tmp_path):
    daemon = fake_daemon(tmp_path, {}, report_llm={"enabled": False})
    try:
        result = run(run_topnews(daemon, from_date="2026-11-22", to_date="2026-11-22"))
        assert result["selectedCount"] == 0 and result["path"] == ""
        assert result["content"] == ""
        assert latest_report(daemon, "topnews") is None
    finally:
        daemon.state.close()


# -- 加油站 / docx / push integration -----------------------------------------

def fact_policy_records():
    text = ('《静安区社区养老服务办法》规定，符合条件的65周岁以上老年人可以免费享受居家养老服务，所需经费由区财政承担。'
            '第十八条修改为：对特困人员给予每月不低于800元的照料补贴，但是已享受长期护理保险待遇的除外。')
    return [evidence(
        title='静安区社区养老服务办法', url='https://example.org/fact1',
        published='2026-11-20', text=text, boards=('政策速递',),
        content_types=('policy',), publisher='静安区民政局',
        entities=[dict(uri='fp', name='《静安区社区养老服务办法》', label='区级政策'),
                  dict(uri='fi', name='静安区民政局', label='区级机构'),
                  dict(uri='fu', name='《上海市就业促进条例》', label='市级政策')],
        relations=[dict(subject='fi', object='fp', label='发布'),
                   dict(subject='fp', object='fu', label='依据')])]


def test_run_topnews_appends_did_you_know_and_stays_dry_run(tmp_path):
    daemon = fake_daemon(tmp_path, {'policy-latest': fact_policy_records()},
                         report_llm={'enabled': False})
    try:
        result = run(run_topnews(daemon, from_date='2026-11-20', to_date='2026-11-22'))
        assert result['didYouKnow'] is not None
        assert '#加油站' in result['content']
        assert '《静安区社区养老服务办法》' in result['content']
        assert result['delivery']['dryRun'] is True and result['delivery']['sent'] is False
        # Dry-run never marks the fact ledger (production timing).
        assert not (tmp_path / 'reports' / 'did_you_know_sent.json').exists()
        assert '#加油站' in Path(result['path']).read_text(encoding='utf-8')
    finally:
        daemon.state.close()


def test_run_topnews_marks_fact_ledger_only_after_real_send(tmp_path, monkeypatch):
    import osbi_client.shadow_reports as module

    async def fake_deliver(profile, task, text, **kwargs):
        return dict(sent=True, dryRun=False, targets=[])

    monkeypatch.setattr(module, '_deliver', fake_deliver)
    daemon = fake_daemon(tmp_path, {'policy-latest': fact_policy_records()},
                         report_llm={'enabled': False})
    try:
        result = run(run_topnews(daemon, from_date='2026-11-20', to_date='2026-11-22',
                                 send=True))
        assert result['delivery']['sent'] is True
        ledger_path = tmp_path / 'reports' / 'did_you_know_sent.json'
        data = json.loads(ledger_path.read_text(encoding='utf-8'))
        assert result['didYouKnow']['factId'] in data['factIds']
        assert data['records'][0]['sentDate'] == result['date']
        # A second dry-run run within the cooldown must not repeat the fact.
        again = run(run_topnews(daemon, from_date='2026-11-20', to_date='2026-11-22'))
        assert again['didYouKnow'] is None or \
            again['didYouKnow']['factId'] != result['didYouKnow']['factId']
    finally:
        daemon.state.close()


def test_run_topnews_failed_send_does_not_mark_ledger(tmp_path, monkeypatch):
    import osbi_client.shadow_reports as module

    async def fake_deliver(profile, task, text, **kwargs):
        return dict(sent=False, dryRun=False, targets=[])

    monkeypatch.setattr(module, '_deliver', fake_deliver)
    daemon = fake_daemon(tmp_path, {'policy-latest': fact_policy_records()},
                         report_llm={'enabled': False})
    try:
        result = run(run_topnews(daemon, from_date='2026-11-20', to_date='2026-11-22',
                                 send=True))
        assert result['delivery']['sent'] is False
        assert not (tmp_path / 'reports' / 'did_you_know_sent.json').exists()
    finally:
        daemon.state.close()


def test_run_weekly_saves_docx_and_lists_download(tmp_path):
    from docx import Document
    from osbi_client.shadow_reports import download_report
    daemon = fake_daemon(tmp_path, {'policy': policy_records()},
                         report_llm={'enabled': False})
    try:
        result = run(run_weekly(daemon, from_date='2026-11-16', to_date='2026-11-22'))
        assert result['docxPath'] and not result['docxError']
        docx = Path(result['docxPath'])
        assert docx.is_file() and docx.name.endswith('.docx')
        doc = Document(str(docx))
        assert doc.paragraphs[0].text.startswith('基层工作动态回顾与热点展望')
        assert result['delivery']['dryRun'] is True and result['delivery']['sent'] is False
        names = {entry['name'] for entry in list_reports(daemon)}
        assert docx.name in names
        assert download_report(daemon, docx.name) == docx
        with pytest.raises(ValueError):
            download_report(daemon, '../client.db')
        with pytest.raises(ValueError):
            read_report(daemon, 'weekly', docx.name)
    finally:
        daemon.state.close()


def test_run_weekly_send_file_without_awada_config_reports_safely(tmp_path):
    daemon = fake_daemon(tmp_path, {'policy-latest': policy_records()},
                         report_llm={'enabled': False})
    config = daemon.state.config().model_dump()
    profile = config['reportProfiles'][0]
    profile['targets'] = [{'id': 'awada', 'type': 'awada', 'label': 'Awada',
                           'enabled': True,
                           'config': {'channelId': '10854594337574110'}}]
    for task in ('topnews', 'weeklyreport'):
        profile[task]['targetIds'] = ['awada']
    daemon.state.save_config({'reportProfiles': config['reportProfiles']})
    try:
        result = run(run_weekly(daemon, from_date='2026-11-16', to_date='2026-11-22',
                                send=True, send_file=True))
        assert result['fileDelivery']['sent'] is False
        assert '未配置' in result['fileDelivery']['targets'][0]['message']
        assert result['delivery']['sent'] is False
    finally:
        daemon.state.close()
