"""Shadow weekly writing stage over the deterministic focus selection.

Ported from midplatform/workflows/ontology_weekly_shadow.py: per-board local
model briefing with [n] citations, one combined 关联解读/综合研判/导读 stage,
markdown assembly and deterministic fallbacks. Word export and group pushes
are out of scope for the client; markdown + 导语 TXT are the visible outputs.
"""
from __future__ import annotations

from datetime import datetime
import logging
import re

from .shadow_llm import LlmError, ReportLlm
from .shadow_relations import format_relation_markdown

logger = logging.getLogger(__name__)

CN_NUMS = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
NODE_NAME = '上海市静安区临汾路街道'
TARGET_CITY = '上海市'
TARGET_DISTRICT = '静安区'
TARGET_STREET = '临汾路街道'


def _base_prompt():
    return (
        f"You are a policy-information analyst for {NODE_NAME}, writing an internal weekly "
        "brief for grassroots staff. The audience is a residential district with many older "
        "residents, high density and limited community resources; do not assume business-park "
        "or investment-led models. Use only the supplied material and graph relations; never "
        "invent facts.\n"
        "Format: the first line must be `### <specific hotspot or policy title>`. Write 2-4 "
        "coherent paragraphs of analysis, like an internal report. Do not use field-style "
        "subheadings such as 'event / policy chain / related action / lessons for us'. Do not "
        "output tables, numbered lists or query-style dumps. If no relation information exists, "
        "write from the material itself. Never write 'material does not provide / does not "
        "specify / does not mention'; state only what is available. When the user message "
        "provides leader activity, name or title, state clearly who the leader is, their title, "
        f"where they went and what they said. For hotspots or policies outside {TARGET_CITY}/"
        f"{TARGET_DISTRICT}, spell out the province/city the first time (e.g. 'Chongqing', "
        "'Jiangsu'); never write only 'the city' or 'the district'. Mark outside cases as "
        f"'external case'. End every `###` subsection with a natural implication for {TARGET_STREET}."
    )


def _prompts():
    base = _base_prompt()
    action = base + (
        " This section writes the hotspot selected by the graph (see the user message); do not "
        "choose another. Explain its relationship network: who implements it, which organisations "
        "and groups are involved, and which policies and actions it connects to. Emphasise that "
        "this is this week's hotspot; no need to trace long policy history. If the material "
        "contains a cross-week note, focus on what is new this week compared with last week and "
        "open with the continuity; do not repeat last week's meeting/event. If two hotspots are "
        "selected, write two `### hotspot name` subsections with 2-3 paragraphs each; do not merge "
        "them."
    )
    policy = base + (
        " This section writes the policy chain selected by the graph (see the user message); do "
        "not choose another. Explain the lineage and implementation chain: higher-level/basis "
        "policy, prior policies, which streets or units are already implementing it, and usable "
        "two-hop/three-hop relations. Write analytical prose, not a relation list. If two chains "
        "are selected, write two `### policy name` subsections with 2-3 paragraphs each."
    )
    economy = base + (
        " This section writes the economic information selected by the graph (see the user "
        "message); do not choose another. Write only about residents' livelihoods, employment, "
        "consumption, community commerce, SMEs and grassroots economic work. Do not write about "
        "capital-market moves (A-share/HK share indices, dividends, buybacks, fund NAVs). If two "
        "items are selected, write two `### title` subsections with 2-3 paragraphs each; end each "
        f"with an implication for {TARGET_STREET}."
    )
    analyst = (
        f"You are a policy-information analyst for {NODE_NAME}. Below are this week's section "
        "drafts and the policy-action relation clues from the graph. Synthesise and output three "
        "tags:\n"
        "<relations> Write 3-4 coherent paragraphs on cross-section connections: policy lineage "
        "(higher-level/prior policy), streets or units already implementing it, and implications "
        f"for {TARGET_STREET}. Do not use field-style labels such as 'policy intent / implementation "
        f"action / implication for {TARGET_STREET}'. Use only direct implementation relations; do "
        "not invent higher-level policy without a basis edge.</relations>\n"
        "<analysis> 300-500 words: summarise the week's most important policies, actions and "
        f"grassroots signals across sections; state implications for {TARGET_STREET}; finish with "
        "'work suggestions' giving 3-5 concrete actions. Do not list everything.</analysis>\n"
        "<digest> 150-250 words suitable for a WeChat group: summarise the 2-3 selected hotspots or "
        "policy chains, prioritising concrete actions, working methods and implementation "
        "relations; do not list only policy names. If target-territory leaders inspected, visited "
        "or spoke this week, include their names, titles and activities.</digest>\n"
        "Output only these three tags, no explanation."
    )
    return base, action, policy, economy, analyst


BASE_PROMPT, ACTION_SYSTEM_PROMPT, POLICY_SYSTEM_PROMPT, ECONOMY_SYSTEM_PROMPT, ANALYST_PROMPT = _prompts()


def configure_writing_context(*, node_name=None, city=None, district=None, street=None):
    """Switch the node wording used by report writing prompts."""
    global NODE_NAME, TARGET_CITY, TARGET_DISTRICT, TARGET_STREET
    global BASE_PROMPT, ACTION_SYSTEM_PROMPT, POLICY_SYSTEM_PROMPT, ECONOMY_SYSTEM_PROMPT, ANALYST_PROMPT
    NODE_NAME = str(node_name or NODE_NAME)
    TARGET_CITY = str(city or TARGET_CITY)
    TARGET_DISTRICT = str(district or TARGET_DISTRICT)
    TARGET_STREET = str(street or TARGET_STREET)
    BASE_PROMPT, ACTION_SYSTEM_PROMPT, POLICY_SYSTEM_PROMPT, ECONOMY_SYSTEM_PROMPT, ANALYST_PROMPT = _prompts()


CITATION_RE = re.compile(r'\[(\d+)\]')
CITATION_CLEAN = re.compile(r'\s*\[[\d\s]+\]')
RAW_POLICY_PREDICATES = {'落实', '依据', '响应', '发布', '实施主体', '提及'}


def _format_items(items: list, limit: int = 8) -> tuple[str, dict]:
    lines = []
    link_dict = {}
    for item in items[:limit]:
        key = f'[{len(link_dict) + 1}]'
        link_dict[key] = item
        entities = '、'.join(
            f"{e['name']}({e['label']})" for e in item.get('keyEntities', [])[:6])
        relations = '；'.join(
            f"{r['subjectText']} {r['predicateLabel']} {r['objectText']}"
            for r in (item.get('relations') or [])[:8]
            if r.get('predicateLabel') not in RAW_POLICY_PREDICATES)
        excerpt = (item.get('excerpt') or '').strip()[:900]
        lines.append(
            f"{key}{item.get('title', '')}\n"
            f"发布者：{item.get('publisher', '')} 日期：{item.get('publishedDate', '')}\n"
            f"实体：{entities}\n"
            + (f"摘要：{excerpt}\n" if excerpt else '')
            + (f"其他关系：{relations}\n" if relations else ''))
    return '\n'.join(lines), link_dict


def _fallback_section(board: str, items: list) -> str:
    """Deterministic brief when the model is unavailable: facts only."""
    lines = []
    for item in items[:3]:
        title = item.get('title', '')
        lines.append(f"### {title}")
        meta = '、'.join(v for v in (item.get('publisher', ''),
                                     item.get('publishedDate', '')) if v)
        excerpt = (item.get('excerpt') or '').strip()[:400]
        paragraph = f"（{meta}）" if meta else ''
        if excerpt:
            paragraph += excerpt
        entities = '、'.join(e.get('name', '') for e in item.get('keyEntities', [])[:5])
        if entities:
            paragraph += f" 涉及主体：{entities}。"
        lines.append(paragraph or title)
    return '\n\n'.join(lines)


async def _section(llm: ReportLlm | None, board: str, items: list,
                   max_items: int = 8, relations: str = '',
                   style: str = 'action', prompt: str = '') -> dict:
    material, link_dict = _format_items(items, max_items)
    if not material:
        return dict(board=board, markdown='', source='empty', citations=[])
    relation_block = f'\n{relations}\n' if relations else ''
    system_prompt = (prompt or (POLICY_SYSTEM_PROMPT if style == 'policy'
                     else ECONOMY_SYSTEM_PROMPT if style == 'economy'
                     else ACTION_SYSTEM_PROMPT))
    reply = ''
    source = 'fallback:no_llm'
    if llm is not None:
        messages = [
            dict(role='system', content=system_prompt),
            dict(role='user', content=f'方向：{board}\n{relation_block}\n材料：\n{material}'),
        ]
        try:
            reply = await llm.chat(messages, temperature=0.2, max_tokens=1800,
                                   timeout=240)
            source = 'llm'
        except LlmError as error:
            logger.warning('weekly section degraded: board=%s error=%s',
                           board, error)
            reply = ''
            source = f'fallback:{error}'
    if not reply.strip():
        reply = _fallback_section(board, items)
        if source == 'llm':
            source = 'fallback'
    citations = []
    seen = set()
    for number in CITATION_RE.findall(reply):
        item = link_dict.get(f'[{number}]')
        if not item:
            continue
        article_id = item.get('articleId') or item.get('url')
        if article_id and article_id not in seen:
            seen.add(article_id)
            citations.append(item.get('evidence', [{}])[0])
    return dict(board=board, markdown=reply.strip(), source=source,
                citations=citations, material=material)


def _tagged(reply: str, tag: str, next_tag: str | None = None) -> str:
    start = reply.find(f'<{tag}>')
    if start < 0:
        return ''
    body = reply[start + len(tag) + 2:]
    if next_tag:
        stop = body.find(f'<{next_tag}>')
        if stop >= 0:
            body = body[:stop]
    stop = body.find(f'</{tag}>')
    if stop >= 0:
        body = body[:stop]
    return body.strip()


async def _combined(llm: ReportLlm | None, sections: dict, date_range: str,
                    relations: str = '', *, analyst_prompt: str = '') -> dict:
    body = '\n\n'.join(
        f"## {board}\n{section.get('markdown', '')}"
        for board, section in sections.items() if section.get('markdown'))
    if not body:
        return dict(analysis='', digest='', relation='', source='empty')
    relation_block = f'\n{relations}\n' if relations else ''
    reply = ''
    source = 'fallback:no_llm'
    if llm is not None:
        messages = [
            dict(role='system', content=analyst_prompt or ANALYST_PROMPT),
            dict(role='user', content=f'Report date: {date_range}\n{relation_block}\n{body}'),
        ]
        try:
            reply = await llm.chat(messages, temperature=0.2, max_tokens=1600,
                                   timeout=240)
            source = 'llm'
        except LlmError as error:
            logger.warning('weekly combined degraded: error=%s', error)
            reply = ''
            source = f'fallback:{error}'
    relation = _tagged(reply, 'relations', 'analysis')
    analysis = _tagged(reply, 'analysis', 'digest')
    digest = _tagged(reply, 'digest').replace('**', '')
    return dict(analysis=analysis.replace('**', ''), digest=digest,
                relation=relation.replace('**', ''), source=source,
                rawReply=reply[:4000])


def _date_range(from_date: str, to_date: str) -> str:
    try:
        start = datetime.strptime(from_date, '%Y-%m-%d')
        end = datetime.strptime(to_date, '%Y-%m-%d')
        return f"{start.strftime('%Y年%m月%d日')}—{end.strftime('%m月%d日')}"
    except ValueError:
        return f'{from_date}—{to_date}'


def report_markdown(from_date: str, to_date: str, sections: dict, analysis: str,
                    relation: str = '', digest: str = '') -> str:
    parts = [f'# 基层工作动态回顾与热点展望（{_date_range(from_date, to_date)}）']
    index = 0
    for board, section in sections.items():
        text = (section.get('markdown') or '').strip()
        if not text:
            continue
        parts.append(f'## {CN_NUMS[index]}、{board}')
        parts.append(CITATION_CLEAN.sub('', text))
        index += 1
    if relation:
        parts.append(f'## {CN_NUMS[index]}、本周关联解读')
        parts.append(CITATION_CLEAN.sub('', relation))
        index += 1
    if analysis:
        parts.append(f'## {CN_NUMS[index]}、综合研判')
        parts.append(CITATION_CLEAN.sub('', analysis))
        index += 1
    # Client addition vs production: the 导读 also lives inside the markdown
    # (production keeps it only in the separate push TXT).
    if digest:
        parts.append(f'## {CN_NUMS[index]}、本周导读')
        parts.append(CITATION_CLEAN.sub('', digest))
    return '\n\n'.join(parts)


def fallback_digest(dossier: dict, relations: dict | None = None) -> str:
    policies = '、'.join(row['name'] for row in dossier.get('topPolicies', [])[:3])
    actions = '、'.join(row['name'] for row in dossier.get('topActions', [])[:3])
    pieces = []
    if policies:
        pieces.append(f'政策方面重点关注：{policies}')
    if actions:
        pieces.append(f'基层行动方面：{actions}')
    chains = (relations or {}).get('chains') or []
    if chains:
        top = chains[0]
        actions_text = '、'.join(item['entity']['name']
                                 for item in top.get('implementers', [])[:2])
        if actions_text:
            pieces.append(f'关联线索：《{top["policy"]["name"]}》对应{actions_text}')
    return '；'.join(pieces) + '。' if pieces else '本周暂无可汇总的实质内容。'


def fallback_analysis(sections: dict, dossier: dict,
                      relations: dict | None = None) -> str:
    titles = [f'{board}：{section.get("markdown", "").splitlines()[0]}'
              for board, section in sections.items() if section.get('markdown')]
    policies = '、'.join(row['name'] for row in dossier.get('topPolicies', [])[:5])
    actions = '、'.join(row['name'] for row in dossier.get('topActions', [])[:5])
    parts = []
    if policies:
        parts.append(f'本周政策信号集中在：{policies}。')
    if actions:
        parts.append(f'基层行动层面，{actions}等事项反复出现，说明工作重心仍在组织学习、'
                     '服务新就业群体和社区治理创新。')
    chain_md = format_relation_markdown(relations or {}, max_chains=3)
    if chain_md:
        parts.append('政策—行动关联线索：\n' + chain_md)
    if titles:
        parts.append('板块要点：' + '；'.join(titles[:5]) + '。')
    parts.append('对临汾路街道而言，优先关注可落到纯居民区、老年服务和社区党建的做法，'
                 '不采用依赖楼宇经济或园区招商的模式。')
    parts.append('工作提示\n1. 对照本周政策脉络，梳理与临汾路街道职能直接相关的上位政策。\n'
                 '2. 优先研究其他街道已有直接落实动作的做法，评估本地适用性。\n'
                 '3. 对仅有同篇提及、无直接落实关系的政策，先核实再跟进。')
    return '\n\n'.join(parts)


DEFAULT_WEEKLY_INTRO = 'Weekly report for {date}\n\n{digest}'


def push_text(digest: str, date: str, intro: str = '') -> str:
    template = (intro or DEFAULT_WEEKLY_INTRO).strip()
    if '{digest}' not in template:
        template = template + '\n\n{digest}'
    try:
        return template.format(date=date, digest=digest).strip()
    except (KeyError, IndexError, ValueError):
        return (template.replace('{date}', date) + '\n\n' + digest).strip()


async def write_weekly(llm: ReportLlm | None, focus_by_board: dict, *,
                       from_date: str, to_date: str, dossier: dict,
                       relations: dict, relation_brief: str = '',
                       max_items: int = 8, weekly_intro: str = '',
                       analyst_prompt: str = '') -> dict:
    """Per-board sections + combined analysis; deterministic on any failure."""
    results = {}
    for board, focus in focus_by_board.items():
        results[board] = await _section(
            llm, board, focus['items'], max_items,
            relations=focus['brief'], style=focus['type'],
            prompt=focus.get('prompt') or '')
    combined = await _combined(llm, results, _date_range(from_date, to_date),
                               relations=relation_brief,
                               analyst_prompt=analyst_prompt)
    digest = combined.get('digest') or fallback_digest(dossier, relations)
    analysis = combined.get('analysis') or fallback_analysis(results, dossier, relations)
    relation_md = combined.get('relation') or format_relation_markdown(relations)
    markdown = report_markdown(from_date, to_date, results, analysis,
                               relation=relation_md, digest=digest)
    return dict(fromDate=from_date, toDate=to_date, sections=results,
                analysis=analysis, digest=digest, relation=relation_md,
                markdown=markdown,
                pushText=push_text(digest, datetime.now().strftime('%Y-%m-%d'),
                                   intro=weekly_intro),
                combinedSource=combined.get('source'),
                combinedRaw=combined.get('rawReply', '')[:2000],
                model=llm.model if llm else '')
