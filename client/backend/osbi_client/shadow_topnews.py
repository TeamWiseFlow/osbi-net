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
WELCOME = '今日份的临小助内参来啦'
SUFFIX = ''
GENERAL_LOCALITY_RULES = (
    "候选已按上海/静安本地优先排序；存在上海/静安本地同类内容时，"
    "不要选外省市的会议、活动、综述类报道；上海其他区的内容最多保留 1 条；"
    "静安区/临汾路街道内容优先；"
    "只有上海市/静安区领导（市委、市政府、静安区委区政府及区级部门）的调研、"
    "考察、走访、出席、讲话、强调类信息权重最高，同等条件下必须优先保留；"
    "其他地区（如杨浦区及其他区、外省市）或职务属地不明确的领导活动一律不要选；"
    "国家级/中央级领导活动不加权也不降权，按新闻价值正常判断；"
)
DANGJIAN_LOCALITY_RULES = (
    "候选已按属地权重排序：静安/临汾 > 上海其他区 > 外省市；"
    "同等条件下优先上海市/静安区内容，上海市/静安区领导活动权重最高；"
    "只要本地候选不足，就正常选择上海其他区或外省市的党建经验、做法、案例，"
    "不要让板块空缺；"
    "外省市领导活动权重较低，仅在本地候选不足时考虑；"
    "国家级/中央级领导活动不加权也不降权，按新闻价值正常判断；"
)
SYSTEM_PROMPT = (
    "你是基层工作者的早报编辑。下面 <markdown> 是过去 24 小时「{board}」板块的"
    "本体图候选，每条以引用标记（如 [1]）开头，含标题、链接、来源和已抽取的"
    "图关系。请从中挑出最有新闻价值、最值得基层工作者关注的 {top_n} 条；"
    "不足 {top_n} 条时按实际数量返回。"
    "优先政策动向、经验典型、社会治理创新。"
    "严格去重：同一人物在同一会议/活动/场合的发言、表态或报道，即使标题措辞"
    "不同，也是同一条新闻，只保留信息量最大的一条；重点对照各条的「事件/活动」"
    "与图关系里的人物及其『参与/出席/召开』的事件，名称写法略有差异但指向同一场"
    "活动的要合并。届次或地域不同（如首届与第二届、江苏省与全国）则是不同活动，"
    "不要合并。候选重复或不足时宁可少选，按去重后的实际数量返回，不要为凑满"
    " {top_n} 条而保留重复内容；"
    "{locality_rules}"
    "图关系是原文抽取的证据，不要编造。"
    "选到的条目每行一个 [n]，包在 <links></links> 里；不要解释。"
)
FACT_SELECT_PROMPT = (
    "你是基层工作者的早报编辑。用户会给出若干条从政策原文抽取的候选事实。"
    "请只选一条最适合放在每日简报「加油站」栏目的：要有反差感、反常识或"
    "鲜为人知，让人看完觉得“原来如此/竟然是这样”；优先有明确数字、条件、"
    "例外、责任主体或处罚后果的；不要选人人都懂的常识性禁止、口号、原则、"
    "政策目标数字或泛泛而谈的内容。只能从候选里选，不能改写或补充候选之外"
    "的信息。只输出一个编号，例如 [2]，不要解释。"
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


async def _select_fact(llm: ReportLlm | None, *, candidates: list) -> dict | None:
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
        dict(role='system', content=FACT_SELECT_PROMPT),
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
        messages = [
            dict(role='system',
                 content=SYSTEM_PROMPT.format(top_n=top_n, board=board,
                                              locality_rules=locality_rules) + (f'\n{prompt}' if prompt else '')),
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
                         llm: ReportLlm | None = None,
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
        fact = await _select_fact(llm, candidates=facts)
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
