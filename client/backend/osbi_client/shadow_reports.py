"""Manual shadow report generation for the local dashboard.

Fetches evidence from the shared server (policy + economy libraries), rebuilds
production-shaped snapshots (the server query side already applies the seed
skeleton + read-model normalization), runs the ported shadow selection/writing
stages and saves visible outputs under the client data dir ``reports/``:

- topnews_graph_{YYYY-MM-DD}.txt   (traditional group-message shape + 加油站)
- 基层工作动态回顾与热点展望_图谱版_{YYYYMMDD}.md + .docx + _导语.txt

LLM failures degrade to deterministic selection/formatting; a run never fails
because the model is unavailable. When no board qualifies at all (e.g. the
policy library is still being filled), the weekly falls back to a clearly
labelled deterministic material overview so an output always exists.

Delivery is dry-run unless ``reportPush.enabled``: MimicWX test group plus an
Awada copy to 临小助升级测试 (production shadow_push parity). The 加油站 fact
ledger is only marked after a successful send, exactly like production.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path

from osbi_contracts.storage import digest

from .publisher_penalty import PublisherPenaltyLedger
from .report_docx import md_to_docx
from .server_client import ServerError, request
from .shadow_candidates import SECTIONS, weekly_dossier
from .shadow_facts import FactLedger
from .shadow_llm import ReportLlm
from .shadow_push import push_shadow_text, send_awada_file
from .shadow_relations import build_relation_dossier, format_relation_brief
from .shadow_snapshot import SnapshotService, article_view, snapshot_from_evidence
from .shadow_topnews import SUFFIX, WELCOME, format_push, select_topnews
from .shadow_weekly import write_weekly
from .shadow_weekly_history import (load_prior_policies, load_shadow_priors,
                                    record_shadow_hotspots)
from .shadow_weekly_select import select_weekly_focus

logger = logging.getLogger(__name__)

LIBRARIES = ('policy', 'economy')
EVIDENCE_PAGE_LIMIT = 100
EVIDENCE_MAX_PAGES = 100
TOPNEWS_FILENAME = 'topnews_graph_{date}.txt'
WEEKLY_FILENAME = '基层工作动态回顾与热点展望_图谱版_{date}.md'
WEEKLY_DOCX_FILENAME = '基层工作动态回顾与热点展望_图谱版_{date}.docx'
WEEKLY_DIGEST_FILENAME = '基层工作动态回顾与热点展望_图谱版_{date}_导语.txt'
FACT_LEDGER_FILENAME = 'did_you_know_sent.json'


def reports_dir(daemon) -> Path:
    return Path(daemon.data_dir) / 'reports'


def _customer(daemon) -> str:
    config = daemon.state.config()
    client_id = daemon.state.one('SELECT client_id FROM identity')['client_id']
    return digest([config.serverUrl, client_id])


async def fetch_evidence(config, channel: str, from_date: str, to_date: str,
                         *, transport=None) -> tuple[list, dict]:
    """Page through evidence; also collect the server read-model payload.

    The server query side owns seed merge + normalization; each page carries
    the referenced normalized nodes, the seed skeleton and the normalization
    stats. They are identical across pages, so merging is idempotent.
    """
    records, cursor = [], ''
    extras: dict = {}
    nodes_by_uri: dict = {}
    for _ in range(EVIDENCE_MAX_PAGES):
        page = await request(config, 'POST', f'/v1/libraries/{channel}/evidence',
                             {'fromDate': from_date, 'toDate': to_date,
                              'cursor': cursor, 'limit': EVIDENCE_PAGE_LIMIT},
                             transport=transport)
        records.extend(page.get('items') or [])
        if page.get('normalized'):
            for node in page.get('nodes') or []:
                nodes_by_uri[node['uri']] = node
            extras['seedNodes'] = page.get('seedNodes') or []
            extras['seedEdges'] = page.get('seedEdges') or []
            extras['normalization'] = page.get('normalization')
        next_cursor = page.get('cursor') or ''
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise ValueError('Evidence cursor did not advance')
        cursor = next_cursor
    else:
        raise ValueError('Evidence query is too broad; narrow the date window')
    if nodes_by_uri:
        extras['nodes'] = [nodes_by_uri[uri] for uri in sorted(nodes_by_uri)]
    return records, extras


async def load_services(daemon, from_date: str, to_date: str) -> dict:
    """One snapshot service per library; an unavailable library stays empty."""
    config = daemon.state.config()
    services = {}
    for library in LIBRARIES:
        try:
            records, extras = await fetch_evidence(config, library, from_date,
                                                   to_date,
                                                   transport=daemon.transport)
        except ServerError as error:
            logger.warning('evidence fetch degraded: library=%s error=%s',
                           library, error)
            records, extras = [], {}
        services[library] = SnapshotService(
            snapshot_from_evidence(records, library, graph_extras=extras))
    return services


def _save_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return str(path)


def _omitted_boards(focus_or_candidates: dict) -> list:
    return [board for board in SECTIONS if board not in focus_or_candidates]


async def _deliver(text: str, config, *, send: bool, window: str | None) -> dict:
    """Production deliver_text semantics: dry-run unless explicitly sending."""
    if not send or not text:
        return dict(sent=False, message='dry-run')
    return await push_shadow_text(text, window=window,
                                  push_config=config.reportPush)


async def run_topnews(daemon, *, from_date: str = '', to_date: str = '',
                      top_n: int = 3, record_penalty: bool = True,
                      use_llm: bool = True, did_you_know: bool = True,
                      send: bool | None = None, window: str | None = None) -> dict:
    today = date.today().isoformat()
    from_date = from_date or today
    to_date = to_date or today
    if not (1 <= top_n <= 20):
        raise ValueError('topN 必须在 1-20 之间')
    config = daemon.state.config()
    directory = reports_dir(daemon)
    ledger = PublisherPenaltyLedger(directory / 'publisher_penalty.json').load()
    if not ledger.updated_date:
        ledger.seed_from_outputs(before_date=today)
    ledger.advance_to_before(today)
    services = await load_services(daemon, from_date, to_date)
    llm = ReportLlm.from_config(config) if use_llm else None
    fact_ledger = FactLedger(directory / FACT_LEDGER_FILENAME)
    result = await select_topnews(services, from_date=from_date, to_date=to_date,
                                  top_n=top_n, llm=llm,
                                  publisher_penalties=ledger.penalties(),
                                  did_you_know=did_you_know,
                                  fact_ledger=fact_ledger)
    selected = result.get('selected') or []
    push = format_push(selected, date=today, welcome=WELCOME, suffix=SUFFIX,
                       extra=result.get('factText') or '')
    path = ''
    if push:
        path = _save_text(directory / TOPNEWS_FILENAME.format(date=today), push)
    send_enabled = config.reportPush.enabled if send is None else bool(send)
    delivery = await _deliver(push, config, send=send_enabled, window=window)
    fact = result.get('didYouKnow')
    if send_enabled and fact and delivery.get('sent'):
        # Record source + send date: the same document stays out of 加油站
        # for the next 7 days (production timing: only after a real send).
        fact_ledger.mark([fact], sent_date=today)
    selected_keys = {item.get('publisherKey') for item in selected
                     if item.get('publisherKey')}
    participants = set(result.get('candidates', {}).get('candidatePublishers') or [])
    recorded = (ledger.record(today, selected_keys, participants)
                if record_penalty and selected else False)
    boards_with_items = [board for board, rows
                         in result.get('candidates', {}).get('boards', {}).items()
                         if rows]
    logger.info('shadow topnews generated: date=%s selected=%s source=%s fact=%s',
                today, len(selected), result.get('source'), bool(fact))
    daemon.state.event('report_topnews',
                       f'TopNews 已生成：{len(selected)} 条（{result.get("source")}）',
                       details=dict(path=path, date=today))
    return dict(kind='topnews', date=today, fromDate=from_date, toDate=to_date,
                path=path, content=push, source=result.get('source'),
                boardSources=result.get('boardSources', {}),
                selectedCount=len(selected),
                boardsWithItems=boards_with_items,
                omittedBoards=[b for b in SECTIONS if b not in boards_with_items],
                model=result.get('model', ''),
                didYouKnow=fact, delivery=delivery,
                publisherPenalty=dict(recorded=recorded,
                                      selected=sorted(selected_keys),
                                      updatedDate=ledger.updated_date),
                snapshots=result.get('snapshots', {}))


def _data_scarce_section(services: dict, from_date: str, to_date: str) -> dict:
    """Clearly-labelled deterministic overview when no board qualifies.

    Deviation from production (which returns empty): the client demo must
    always produce a readable weekly, so the richest window articles are
    listed verbatim with source metadata instead of model prose.
    """
    items = []
    for service in services.values():
        snapshot = service.snapshot
        for article in snapshot.articles.values():
            published = article.get('publishedDate') or ''
            if not published or not (from_date <= published <= to_date):
                continue
            item = article_view(snapshot, article, with_relations=False)
            item['excerpt'] = ''
            for block_id in article['blockIds']:
                raw = snapshot.blocks.get(block_id, {}).get('rawText', '')
                if raw:
                    item['excerpt'] = raw[:600]
                    break
            items.append(item)
    items.sort(key=lambda item: (item.get('relationCount', 0),
                                 item.get('entityCount', 0)), reverse=True)
    items = items[:3]
    lines = ['### 本周材料概览（数据不足兜底）',
             '本周各板块均未达到图谱选材门槛（政策库信源仍在填充或关系网络不足），'
             '以下按图关系丰富度列出窗口内原始材料，供人工研判，未经模型撰写。']
    for item in items:
        lines.append(f"### {item.get('title', '')}")
        meta = '、'.join(v for v in (item.get('publisher', ''),
                                     item.get('publishedDate', '')) if v)
        body = f"（{meta}）" if meta else ''
        if item.get('excerpt'):
            body += item['excerpt']
        lines.append(body or item.get('title', ''))
        if item.get('url'):
            lines.append(f"原文链接：{item['url']}")
    return dict(board='材料概览', markdown='\n\n'.join(lines),
                source='fallback:data_scarce', citations=[], items=items)


def _save_outputs(markdown: str, digest_push: str, directory: Path,
                  stamp: str) -> dict:
    """Production _save_outputs: docx (penholder style) + 导语 TXT.

    A docx failure never blocks the markdown/TXT outputs; the error is
    reported alongside them.
    """
    saved: dict = {}
    docx_path = directory / WEEKLY_DOCX_FILENAME.format(date=stamp)
    txt_path = directory / WEEKLY_DIGEST_FILENAME.format(date=stamp)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        md_to_docx(markdown, str(docx_path))
        saved['docx'] = str(docx_path)
    except Exception as error:  # noqa: BLE001 - markdown/TXT still usable
        saved['docxError'] = f'{type(error).__name__}: {error}'
    saved['txt'] = _save_text(txt_path, digest_push)
    return saved


async def run_weekly(daemon, *, from_date: str = '', to_date: str = '',
                     use_llm: bool = True, max_items: int = 8,
                     send: bool | None = None, window: str | None = None,
                     send_file: bool = False,
                     file_channel_id: str | None = None) -> dict:
    today = date.today()
    to_date = to_date or today.isoformat()
    from_date = from_date or (today - timedelta(days=6)).isoformat()
    config = daemon.state.config()
    directory = reports_dir(daemon)
    customer = _customer(daemon)
    services = await load_services(daemon, from_date, to_date)
    dossier = weekly_dossier(services, from_date=from_date, to_date=to_date)
    priors = load_shadow_priors(daemon.state, customer, to_date)
    prior_policies = load_prior_policies(daemon.state, customer, from_date)
    relations = build_relation_dossier(services, from_date=from_date,
                                       to_date=to_date, priors=prior_policies,
                                       max_chains=60)
    relation_brief = format_relation_brief(relations)
    focus = select_weekly_focus(services, from_date=from_date, to_date=to_date,
                                priors=priors, relation_dossier=relations)
    data_scarce = not focus
    if focus:
        # Record right after selection, action boards only (production timing).
        record_shadow_hotspots(daemon.state, customer, to_date, focus)
    llm = ReportLlm.from_config(config) if use_llm else None
    if data_scarce:
        section = _data_scarce_section(services, from_date, to_date)
        results = {section['board']: section}
        from .shadow_weekly import (fallback_analysis, fallback_digest,
                                     push_text, report_markdown)
        digest = fallback_digest(dossier, relations)
        analysis = fallback_analysis(results, dossier, relations)
        markdown = report_markdown(from_date, to_date, results, analysis,
                                   digest=digest)
        written = dict(sections=results, analysis=analysis, digest=digest,
                       relation='', markdown=markdown,
                       pushText=push_text(digest, today.isoformat()),
                       combinedSource='fallback:data_scarce', model='')
    else:
        written = await write_weekly(llm, focus, from_date=from_date,
                                     to_date=to_date, dossier=dossier,
                                     relations=relations,
                                     relation_brief=relation_brief,
                                     max_items=max_items)
    stamp = today.strftime('%Y%m%d')
    path = _save_text(directory / WEEKLY_FILENAME.format(date=stamp),
                      written['markdown'])
    saved = _save_outputs(written['markdown'], written['pushText'], directory, stamp)
    digest_path = saved.get('txt', '')
    send_enabled = config.reportPush.enabled if send is None else bool(send)
    delivery = await _deliver(written['pushText'], config, send=send_enabled,
                              window=window)
    file_delivery = None
    if send_file:
        file_delivery = await send_awada_file(
            saved.get('docx', ''),
            file_channel_id or config.reportPush.fileChannelId,
            config_path=config.reportPush.awadaConfigPath)
    logger.info('shadow weekly generated: window=%s..%s boards=%s data_scarce=%s',
                from_date, to_date, list(written['sections']), data_scarce)
    daemon.state.event('report_weekly',
                       f'Weeklyreport 已生成：{len(written["sections"])} 个板块',
                       details=dict(path=path, fromDate=from_date, toDate=to_date))
    return dict(kind='weekly', date=today.isoformat(), fromDate=from_date,
                toDate=to_date, path=path, digestPath=digest_path,
                docxPath=saved.get('docx', ''), docxError=saved.get('docxError', ''),
                delivery=delivery, fileDelivery=file_delivery,
                content=written['markdown'], digest=written['digest'],
                pushText=written['pushText'],
                sectionSources={board: section.get('source')
                                for board, section in written['sections'].items()},
                boards=list(written['sections']),
                omittedBoards=_omitted_boards(focus),
                dataScarce=data_scarce,
                combinedSource=written.get('combinedSource'),
                model=written.get('model', ''),
                topPolicies=dossier.get('topPolicies', [])[:8],
                topActions=dossier.get('topActions', [])[:10],
                snapshots=dossier.get('snapshots', {}))


def _report_files(daemon, pattern: str) -> list:
    directory = reports_dir(daemon)
    if not directory.is_dir():
        return []
    return sorted(directory.glob(pattern), key=lambda p: p.stat().st_mtime,
                  reverse=True)


def latest_report(daemon, kind: str) -> dict | None:
    if kind == 'topnews':
        files = _report_files(daemon, 'topnews_graph_*.txt')
    elif kind == 'weekly':
        files = _report_files(daemon, '基层工作动态回顾与热点展望_图谱版_*.md')
    else:
        raise ValueError('未知报告类型')
    if not files:
        return None
    path = files[0]
    return dict(kind=kind, name=path.name, path=str(path),
                modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds'),
                content=path.read_text(encoding='utf-8'))


def list_reports(daemon, limit: int = 20) -> list:
    entries = []
    for kind, pattern in (('topnews', 'topnews_graph_*.txt'),
                          ('weekly', '基层工作动态回顾与热点展望_图谱版_*.md'),
                          ('weekly', '基层工作动态回顾与热点展望_图谱版_*.docx')):
        for path in _report_files(daemon, pattern):
            entries.append(dict(kind=kind, name=path.name, path=str(path),
                                modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec='seconds'),
                                size=path.stat().st_size))
    entries.sort(key=lambda entry: entry['modified'], reverse=True)
    return entries[:limit]


def read_report(daemon, kind: str, name: str) -> dict:
    """Read one saved report by file name (bounded to the reports dir)."""
    directory = reports_dir(daemon).resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        raise ValueError('报告不存在')
    if path.suffix == '.docx':
        raise ValueError('docx 报告请使用下载接口')
    return dict(kind=kind, name=path.name, path=str(path),
                content=path.read_text(encoding='utf-8'))


def download_report(daemon, name: str) -> Path:
    """Resolve one saved report file for download (bounded to the reports dir)."""
    directory = reports_dir(daemon).resolve()
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        raise ValueError('报告不存在')
    return path
