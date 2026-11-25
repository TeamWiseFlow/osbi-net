"""Shadow topnews selection and push formatting over graph-first candidates.

Ported from midplatform/workflows/ontology_topnews_shadow.py: per-board editor
LLM with the production system prompt and locality rules, <links> parsing,
deterministic fallback, the traditional group-message shape, and the 加油站
fact stage (deterministic extraction + LLM index-only rerank). MimicWX/Awada
delivery lives in shadow_push.py and is wired by shadow_reports.py.
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
import logging
import re

from .shadow_candidates import _all_services, format_markdown, topnews_candidates
from .shadow_facts import FactLedger, format_fact, leading_facts
from .shadow_llm import LlmError, ReportLlm
from .shadow_snapshot import strip_publish_date_suffix

logger = logging.getLogger(__name__)

# Candidate pool larger than top_n so editor dedupe still fills the board.
CANDIDATE_POOL_SIZE = 5
WELCOME = 'Daily intelligence brief'
SUFFIX = ''
GENERAL_LOCALITY_RULES = (
    "Candidates are ordered by local relevance. When comparable local material exists, "
    "avoid meetings, events and commentary from other provinces or cities; keep at most one "
    "item from other districts. Prefer the configured target territory. Only leader inspections, "
    "visits, speeches and instructions from target-territory leaders carry the highest weight. "
    "Leader activity from other areas or with unclear jurisdiction should normally be omitted. "
    "National/central leader activity is neutral and judged by news value."
)
DANGJIAN_LOCALITY_RULES = (
    "Candidates are ordered by local weight. Prefer target-territory content and target leaders, "
    "but when local supply is thin, select relevant party-building experience, methods and cases "
    "from other districts or provinces rather than leaving the section empty. Leader activity from "
    "other areas carries lower weight. National/central leader activity is neutral."
)
SYSTEM_PROMPT = (
    "You are an editor preparing a daily brief for local government staff. The <markdown> "
    "contains ontology-graph candidates for the '{board}' section over the last 24 hours. "
    "Each candidate starts with a citation tag such as [1] and includes title, link, source and "
    "extracted graph relations. Choose the {top_n} most newsworthy and useful items; return fewer "
    "when fewer qualify. Prefer policy developments, practical experience and social-governance "
    "innovation. Deduplicate strictly: statements or reports about the same person at the same "
    "meeting/event are one story even if titles differ; keep only the most informative one. Compare "
    "event/activity entities and graph relations. Different editions or regions (e.g. first vs "
    "second session, Jiangsu vs national) are different events; do not merge them. When candidates "
    "are repetitive or insufficient, return fewer instead of filling the quota with duplicates. "
    "{locality_rules} Graph relations are extracted evidence; never invent facts. Output selected "
    "items one per line as [n] inside <links></links>. No explanation."
)
FACT_SELECT_PROMPT = (
    "You are an editor preparing a daily brief. The user provides candidate facts extracted from "
    "policy documents. Select exactly one fact most suitable for the 'Did you know' slot: it should "
    "be counter-intuitive, surprising or little known. Prefer facts with concrete numbers, "
    "conditions, exceptions, responsible parties or consequences. Do not choose common-sense "
    "prohibitions, slogans, principles, headline policy targets or vague statements. Select only "
    "from the candidates; never rewrite or add information. Output only one index such as [2], "
    "with no explanation."
)


def _parse_links(reply: str, link_dict: dict, top_n: int) -> list:
    match = re.search(r'<links>(.*?)</links>', reply or '', re.DOTALL | re.IGNORECASE)
    if not match:
        return []
    selected = []
    seen = set()
    for tag in re.findall(r'\[\d+\]', match.group(1)):
        item = link_dict.get(tag)
        if not item:
            continue
        key = item.get('articleId') or item.get('url') or tag
        if key in seen:
            continue
        seen.add(key)
        selected.append(item)
        if len(selected) >= top_n:
            break
    return selected


def _fallback(candidates: dict, top_n: int) -> list:
    items = []
    for board, rows in candidates.get('boards', {}).items():
        for row in rows:
            items.append(row)
    items.sort(key=lambda item: (item.get('publishedDate', ''),
                                 item.get('relationCount', 0)), reverse=True)
    return items[:top_n]


def _parse_fact_index(reply: str, count: int) -> int:
    match = re.search(r'[\[【](\d+)[\]】]', reply or '')
    if not match:
        match = re.search(r'(?<!\d)(\d+)(?!\d)', reply or '')
    if not match:
        return 0
    index = int(match.group(1))
    return index if 1 <= index <= count else 0


async def _select_fact(llm: ReportLlm | None, *, candidates: list,
                       prompt: str = '') -> dict | None:
    """Let the local model pick the most counter-intuitive fact.

    The model only returns an index; the fact text stays verbatim from the
    deterministic extractor, so selection cannot introduce unsupported facts.
    """
    if not candidates:
        return None
    if len(candidates) == 1 or llm is None:
        return candidates[0]
    lines = []
    for index, fact in enumerate(candidates, 1):
        policy = fact.get('policy') or ''
        article = fact.get('article') or ''
        lines.append(f"[{index}] 《{policy}》{article} "
                     f"{fact.get('text', '')}（{fact.get('publishedDate', '')}）")
    messages = [
        dict(role='system', content=prompt or FACT_SELECT_PROMPT),
        dict(role='user', content='\n'.join(lines)),
    ]
    try:
        reply = await llm.chat(messages, temperature=0.1, max_tokens=32,
                               timeout=90)
    except LlmError:  # deterministic order is a safe fallback
        return candidates[0]
    index = _parse_fact_index(reply, len(candidates))
    return candidates[index - 1] if index else candidates[0]


def format_push(selected: list, *, date: str, welcome: str, suffix: str,
                extra: str = '') -> str:
    """Exactly the traditional topnews group message shape (no graph labels)."""
    groups = OrderedDict()
    for item in selected:
        board = item.get('section') or (item.get('boards') or ['资讯'])[0]
        groups.setdefault(board, []).append(item)
    sections = []
    for board, items in groups.items():
        seen_publishers = set()
        lines = [f'#{board}']
        for item in items:
            url = item.get('url') or ''
            publisher = item.get('publisher') or ''
            published = item.get('publishedDate') or ''
            if 'mp.weixin.qq.com' in url and publisher:
                if publisher in seen_publishers:
                    continue
                seen_publishers.add(publisher)
            title = strip_publish_date_suffix(item.get('title', ''))
            prefix = f'//{publisher} {published}// ' if ('mp.weixin.qq.com' in url and (publisher or published)) else ''
            lines.append(f'{prefix}{title}\n{url}')
        sections.append('\n'.join(lines))
    news = '\n\n'.join(sections)
    if not news:
        return ''
    if extra:
        news += '\n\n' + extra
    return f'{date} {welcome}：\n\n{news}\n\n{suffix}'.strip()


async def _select_board(llm: ReportLlm | None, *, board: str, rows: list,
                        top_n: int, prompt: str = '',
                        locality_rules: str | None = None) -> tuple[list, str, str, str]:
    """Run the editor once per board, matching the traditional per-section cap."""
    markdown, link_dict = format_markdown({'boards': {board: rows}})
    locality_rules = locality_rules or (DANGJIAN_LOCALITY_RULES
                                        if board == '基层党建'
                                        else GENERAL_LOCALITY_RULES)
    reply = ''
    source = 'fallback:no_llm'
    if llm is not None:
        default_prompt = SYSTEM_PROMPT.format(top_n=top_n, board=board,
                                              locality_rules=locality_rules)
        system_prompt = prompt or default_prompt
        if prompt:
            try:
                system_prompt = prompt.format(top_n=top_n, board=board,
                                              locality_rules=locality_rules)
            except (KeyError, IndexError, ValueError):
                system_prompt = prompt
        messages = [
            dict(role='system', content=system_prompt),
            dict(role='user', content=f'<markdown>\n{markdown}\n</markdown>'),
        ]
        try:
            reply = await llm.chat(messages, temperature=0.1, max_tokens=1024,
                                   timeout=180)
            source = 'llm'
        except LlmError as error:
            logger.warning('topnews editor degraded: board=%s error=%s',
                           board, error)
            reply = ''
            source = f'fallback:{error}'
    selected = _parse_links(reply, link_dict, top_n)
    if not selected:
        selected = _fallback({'boards': {board: rows}}, top_n)
        if source == 'llm':
            source = 'fallback'
    return selected, source, reply, markdown


def _board_views(boards):
    if boards is None:
        return {}
    if isinstance(boards, dict):
        return dict(boards)
    from .report_settings import BoardView
    return {board.title: board for board in boards if isinstance(board, BoardView)}


async def select_topnews(services, *, from_date: str, to_date: str,
                         top_n: int | None = None, boards=None,
                         task_prompt: str = '', llm: ReportLlm | None = None,
                         publisher_penalties: dict | None = None,
                         did_you_know: bool = False,
                         fact_ledger: FactLedger | None = None) -> dict:
    """Candidate pool -> per-board editor -> selected items with evidence."""
    views = _board_views(boards)
    names = list(views) or (list(boards) if boards else None)
    pool = max([view.topN for view in views.values()] + [CANDIDATE_POOL_SIZE])
    candidates = topnews_candidates(
        services, from_date=from_date, to_date=to_date, boards=names,
        per_board=pool, board_views=views,
        publisher_penalties=publisher_penalties)
    boards = candidates.get('boards', {})
    if not any(boards.values()):
        return dict(fromDate=from_date, toDate=to_date, selected=[],
                    source='empty', candidates=candidates, pushText='',
                    boardSources={}, didYouKnow=None, factText='',
                    snapshots=candidates.get('snapshots', {}))
    selected = []
    board_sources = {}
    replies = []
    candidate_markdown = []
    for board, rows in boards.items():
        if not rows:
            continue
        view = views.get(board)
        board_top = top_n or (view.topN if view else 3)
        locality_rules = (DANGJIAN_LOCALITY_RULES
                          if view and 'party-building' in view.channels
                          else None)
        picked, board_source, reply, markdown = await _select_board(
            llm, board=board, rows=rows, top_n=board_top,
            prompt=view.prompt if view else '', locality_rules=locality_rules)
        selected.extend(picked)
        board_sources[board] = board_source
        replies.append(f'## {board}\n{reply}')
        candidate_markdown.append(markdown)
    source = ('llm' if board_sources
              and all(value == 'llm' for value in board_sources.values())
              else 'fallback' if board_sources
              and all(value != 'llm' for value in board_sources.values())
              else 'mixed')
    selected_markdown, _ = format_markdown(dict(boards={'已选': selected}))
    fact = None
    fact_text = ''
    if did_you_know:
        # Production window: the last 7 days ending at to_date.
        fact_to = to_date
        try:
            fact_from = (datetime.strptime(to_date, '%Y-%m-%d')
                         - timedelta(days=6)).strftime('%Y-%m-%d')
        except ValueError:
            fact_from = from_date
        facts = leading_facts([svc.snapshot for svc in _all_services(services)],
                              from_date=fact_from, to_date=fact_to,
                              ledger=fact_ledger, limit=8)
        fact = await _select_fact(llm, candidates=facts, prompt=task_prompt)
        if fact:
            fact_text = format_fact(fact)
    return dict(fromDate=from_date, toDate=to_date, selected=selected,
                source=source,
                model=llm.model if llm else '',
                markdown=selected_markdown,
                candidateMarkdown='\n'.join(candidate_markdown),
                rawReply='\n'.join(replies)[:4000],
                boardSources=board_sources,
                didYouKnow=fact,
                factText=fact_text,
                candidates=candidates,
                snapshots=candidates.get('snapshots', {}))
