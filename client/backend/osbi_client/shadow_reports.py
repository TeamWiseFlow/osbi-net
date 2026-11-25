"""Manual shadow report generation for the local dashboard.

Fetches evidence from the shared server (policy + economy libraries), rebuilds
production-shaped snapshots (the server query side already applies the seed
skeleton + read-model normalization), runs the ported shadow selection/writing
stages and saves visible outputs under the client data dir ``reports/``:

- topnews_graph_{YYYY-MM-DD}.txt   (traditional group-message shape + 加油站)
- 基层工作动态回顾与热点展望_图谱版_{YYYYMMDD}.md + .docx + _导语.txt

Sections, channels, selectors, schedules and delivery targets come from the
configured report profile; nothing here hardcodes a city, board set or channel
list. LLM failures degrade to deterministic selection/formatting; a run never
fails because the model is unavailable. Delivery is dry-run unless the task's
``dryRun`` is false. The 加油站 fact ledger is only marked after a successful
send, exactly like production.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
from pathlib import Path

from osbi_contracts.storage import digest

from .publisher_penalty import PublisherPenaltyLedger
from .report_docx import md_to_docx
from .server_client import ServerError, request
from .report_settings import board_views
from .shadow_candidates import weekly_dossier
from .shadow_facts import FactLedger
from .shadow_llm import ReportLlm
from .shadow_push import deliver_targets
from .shadow_relations import build_relation_dossier, format_relation_brief
from .shadow_snapshot import SnapshotService, article_view, snapshot_from_evidence
from .shadow_topnews import SUFFIX, WELCOME, format_push, select_topnews
from .shadow_weekly import write_weekly
from .shadow_weekly_history import (load_prior_policies, load_shadow_priors,
                                    record_shadow_hotspots)
from .shadow_weekly_select import select_weekly_focus

logger = logging.getLogger(__name__)

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


def _apply_local_rules(records: list, local_rules) -> list:
    keywords = [value.casefold() for value in getattr(local_rules, 'keywords', []) if value]
    excludes = [value.casefold() for value in getattr(local_rules, 'excludeKeywords', []) if value]
    filtered = []
    for record in records:
        text = (str(record.get('title') or '') + '\n' + str(record.get('text') or '')).casefold()
        if keywords and not any(keyword in text for keyword in keywords):
            continue
        if excludes and any(keyword in text for keyword in excludes):
            continue
        filtered.append(record)
    return filtered


async def fetch_evidence(config, channel: str, from_date: str, to_date: str,
                         *, query=None, local_rules=None, source_scope=None,
                         transport=None) -> tuple[list, dict]:
    """Page through one channel with sourceScope/territory/query parameters.

    The server query side owns seed merge + normalization; each page carries
    the referenced normalized nodes, the seed skeleton and the normalization
    stats. They are identical across pages, so merging is idempotent.
    """
    records, cursor = [], ''
    extras: dict = {}
    nodes_by_uri: dict = {}
    body = {'fromDate': from_date, 'toDate': to_date,
            'cursor': cursor, 'limit': EVIDENCE_PAGE_LIMIT,
            'sourceScope': source_scope or (getattr(query, 'sourceScope', 'all') if query else 'all'),
            'territoryPreference': list(getattr(query, 'territoryPreference', []) or []),
            'territoryMode': getattr(query, 'territoryMode', 'prefer') if query else 'prefer',
            'text': getattr(query, 'text', '') if query else '',
            'entityUris': list(getattr(query, 'entityUris', []) or []),
            'contentTypes': list(getattr(query, 'contentTypes', []) or [])}
    for _ in range(EVIDENCE_MAX_PAGES):
        body['cursor'] = cursor
        page = await request(config, 'POST', f'/v1/channels/{channel}/evidence',
                             body, transport=transport)
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
    if local_rules is not None:
        records = _apply_local_rules(records, local_rules)
    return records, extras


def _merge_extras(target: dict, source: dict) -> dict:
    nodes = {node['uri']: node for node in target.get('nodes', [])}
    nodes.update({node['uri']: node for node in source.get('nodes', [])})
    if nodes:
        target['nodes'] = [nodes[uri] for uri in sorted(nodes)]
    for key in ('seedNodes', 'seedEdges'):
        if source.get(key):
            target[key] = source[key]
    if source.get('normalization'):
        target['normalization'] = source['normalization']
    return target


async def load_services(daemon, from_date: str, to_date: str, boards) -> dict:
    """One snapshot service per board, merging every channel it declares.

    Each board is queried with its own text/entity/content/territory filters.
    An unavailable board stays empty so other sections still produce output.
    """
    config = daemon.state.config()
    services = {}
    for board in boards:
        records, extras = [], {}
        for channel in board.channels:
            try:
                part, part_extras = await fetch_evidence(
                    config, channel, from_date, to_date, query=board.query,
                    local_rules=board.localRules, source_scope=board.sourceScope,
                    transport=daemon.transport)
            except ServerError as error:
                logger.warning('evidence fetch degraded: board=%s channel=%s error=%s',
                               board.title, channel, error)
                continue
            records.extend(part)
            extras = _merge_extras(extras, part_extras)
        jev_mode = board.localRules.jev
        use_jev = (jev_mode == 'on' or (jev_mode == 'inherit'
                   and config.jev.enabled and config.jev.postQuery))
        if use_jev and records:
            from .decisions import filter_evidence
            records = await filter_evidence(daemon, records, board.intent)
        services[board.title] = SnapshotService(
            snapshot_from_evidence(records, '+'.join(board.channels),
                                   graph_extras=extras, board_label=board.title))
    return services


def _save_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return str(path)


def _omitted_boards(selected, boards) -> list:
    return [board for board in boards if board not in selected]


def _apply_node_rules(config) -> None:
    """Apply node wording/locality/leader overrides for this report run."""
    from .leader_rules import configure as configure_leader
    from .shadow_candidates import configure_rules as configure_candidates
    from .shadow_relations import configure as configure_relations
    from .shadow_weekly import configure_writing_context
    rules = config.nodeRules
    configure_writing_context(node_name=rules.nodeName, city=rules.city,
                              district=rules.district, street=rules.street)
    configure_candidates(generic_entity_names=rules.genericEntityNames or None,
                         locality_score=rules.localityScore or None,
                         leader_boost=500 if rules.leaderBoost else 0,
                         non_target_penalty=300 if rules.localityPenalty else 0)
    overrides = dict(rules.leaderOverrides or {})
    configure_leader(targetCity=rules.city, targetDistrict=rules.district,
                     **{key: value for key, value in overrides.items()
                        if key in {'targetTerritory', 'cityLeaderTerms', 'otherDistricts',
                                   'otherDistrictShort', 'otherProvinces',
                                   'knownNationalLeaders', 'nationalMarkers',
                                   'nationalTitles', 'leaderTitleTerms', 'leaderVerbs',
                                   'concurrentCityTitles', 'districtPostTerms'}})
    configure_relations(territory_hints=rules.relationHints or None,
                        **{key: value for key, value in overrides.items()
                           if key in {'relevantBoards', 'noiseImplementers',
                                      'genericPolicyTerms'}})


def _profile_task(config, profile_id, task_name):
    profile = next((p for p in config.reportProfiles if p.id == profile_id), None)
    if profile is None:
        raise ValueError('Report profile not configured on this client')
    return profile, getattr(profile, task_name)


async def _deliver(profile, task, text: str, *, send: bool | None = None,
                   file_path: str | None = None,
                   file_name: str | None = None) -> dict:
    """Dry-run unless the task says otherwise; target results are per target."""
    if not text and not file_path:
        return dict(sent=False, dryRun=True, message='empty')
    dry_run = task.dryRun if send is None else not bool(send)
    if dry_run:
        return dict(sent=False, dryRun=True, message='dry-run')
    return await deliver_targets(profile, task, text, file_path=file_path,
                                 file_name=file_name)


async def run_topnews(daemon, *, from_date: str = '', to_date: str = '',
                      top_n: int | None = None, profile: str = 'default',
                      record_penalty: bool = True,
                      use_llm: bool = True, did_you_know: bool = True,
                      send: bool | None = None, window: str | None = None) -> dict:
    current = date.today()
    today = current.isoformat()
    from_date = from_date or (current - timedelta(days=1)).isoformat()
    to_date = to_date or today
    if top_n is not None and not (1 <= top_n <= 20):
        raise ValueError('topN 必须在 1-20 之间')
    config = daemon.state.config()
    _apply_node_rules(config)
    profile_config, task = _profile_task(config, profile, 'topnews')
    boards = board_views(config, profile, 'topnews')
    directory = reports_dir(daemon)
    ledger = PublisherPenaltyLedger(directory / 'publisher_penalty.json').load()
    if not ledger.updated_date:
        ledger.seed_from_outputs(before_date=today)
    ledger.advance_to_before(today)
    services = await load_services(daemon, from_date, to_date, boards)
    llm = ReportLlm.from_config(config) if use_llm else None
    fact_ledger = FactLedger(directory / FACT_LEDGER_FILENAME)
    result = await select_topnews(services, from_date=from_date, to_date=to_date,
                                  top_n=top_n, boards=boards, llm=llm,
                                  publisher_penalties=ledger.penalties(),
                                  did_you_know=did_you_know,
                                  fact_ledger=fact_ledger)
    selected = result.get('selected') or []
    push = format_push(selected, date=today,
                       welcome=config.nodeRules.topnewsShout or WELCOME,
                       suffix=config.nodeRules.topnewsSuffix or SUFFIX,
                       extra=result.get('factText') or '')
    path = ''
    if push:
        path = _save_text(directory / TOPNEWS_FILENAME.format(date=today), push)
    delivery = await _deliver(profile_config, task, push, send=send)
    send_enabled = not delivery.get('dryRun')
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
                omittedBoards=[b for b in [view.title for view in boards]
                               if b not in boards_with_items],
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
                     profile: str = 'default',
                     send: bool | None = None, window: str | None = None,
                     send_file: bool = False,
                     file_channel_id: str | None = None) -> dict:
    today = date.today()
    to_date = to_date or today.isoformat()
    from_date = from_date or (today - timedelta(days=7)).isoformat()
    config = daemon.state.config()
    _apply_node_rules(config)
    profile_config, task = _profile_task(config, profile, 'weeklyreport')
    boards = board_views(config, profile, 'weeklyreport')
    board_names = [view.title for view in boards]
    directory = reports_dir(daemon)
    customer = _customer(daemon)
    services = await load_services(daemon, from_date, to_date, boards)
    dossier = weekly_dossier(services, from_date=from_date, to_date=to_date,
                             boards=board_names)
    from .shadow_weekly_history import configure as configure_history
    configure_history(action_boards={view.title for view in boards
                                     if view.selector == 'action'},
                      policy_boards={view.title for view in boards
                                     if view.selector == 'policy'})
    priors = load_shadow_priors(daemon.state, customer, to_date, profile)
    prior_policies = load_prior_policies(daemon.state, customer, from_date, profile_id=profile)
    relations = build_relation_dossier(services, from_date=from_date,
                                       to_date=to_date, priors=prior_policies,
                                       max_chains=60)
    relation_brief = format_relation_brief(relations)
    focus = select_weekly_focus(services, from_date=from_date, to_date=to_date,
                                boards=board_names, board_views=boards,
                                priors=priors, relation_dossier=relations)
    data_scarce = not focus
    if focus:
        # Record right after selection, action boards only (production timing).
        record_shadow_hotspots(daemon.state, customer, to_date, focus, profile)
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
    delivery = await _deliver(profile_config, task, written['pushText'], send=send)
    file_delivery = None
    if send_file and saved.get('docx') and not delivery.get('dryRun'):
        file_delivery = await deliver_targets(
            profile_config, task, written['pushText'],
            file_path=saved.get('docx'), file_name=Path(saved['docx']).name,
            file_only=True)
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
                omittedBoards=_omitted_boards(focus, [view.title for view in boards]),
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
