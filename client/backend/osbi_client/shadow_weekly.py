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
BASE_PROMPT = (
    "你是上海市静安区临汾路街道的政务信息分析员，为街道干部撰写内部参考周报。"
    "服务对象是纯居民生活居住区：老年人口多、人口密度高、社区资源少，"
    "不依赖楼宇经济/园区招商。只依据给定材料和图谱关系，不编造。\n"
    "格式：第一行必须是 `### <具体热点或政策标题>`；正文写 2-4 段连贯的政务分析文字，"
    "像一份内部报告。不要使用“事件/政策脉络/关联行动/对临汾的借鉴”等字段式小标题，"
    "不要输出表格、编号清单或查询结果式罗列。没有关系信息就直接写材料本身，"
    "不要出现“材料未提供/未明确列出/未提及”之类缺失说明，只写已有内容。"
    "用户消息给出领导活动原文、领导姓名或职务时，必须写清是哪位领导、什么职务、"
    "去了哪里或讲了什么，不得省略。"
    "非上海/静安的热点或政策，正文第一次出现时必须写明所属省市全称"
    "（如“重庆市”“江苏省”），不得只写“全市/全区/我市/我区”；"
    "外地案例要明确标注“外地案例”。"
    "每个 `###` 小节最后都用一段自然落到对临汾路街道的启示。"
)
ACTION_SYSTEM_PROMPT = BASE_PROMPT + (
    "本板块写图谱选出的热点主体（见用户消息），不要另选。"
    "把它的关系网络写清楚：谁在实施、涉及哪些单位和群体、对接了什么政策和行动；"
    "重点表现这是本周热点，不必追溯政策沿革。"
    "若材料含“跨周提示”，说明该热点上周已报道：必须聚焦本周相对上周的新进展、"
    "新做法或动态变化，开头点明承接关系，不要重复上周已报的会议/事件本身。"
    "如果选出 2 个热点，请分别用 `### 热点名称` 写两个小节，每节 2-3 段；"
    "不要把两个热点合并成一个主题。"
)
POLICY_SYSTEM_PROMPT = BASE_PROMPT + (
    "本板块写图谱选出的政策链（见用户消息），不要另选。"
    "把政策脉络和落实链条讲清楚：上位/依据政策、此前政策、哪些街道或单位已经在落实、"
    "以及可用的二跳三跳关系；用解读性文字，不要罗列关系清单。"
    "如果选出 2 条政策链，请分别用 `### 政策名称` 写两个小节，每节 2-3 段。"
)
ECONOMY_SYSTEM_PROMPT = BASE_PROMPT + (
    "本板块写图谱选出的经济信息（见用户消息），不要另选。"
    "只写与居民生活、就业、消费、社区商业、中小微企业和基层经济工作相关的内容；"
    "资本市场行情（A股/港股涨跌、个股分红、回购、基金净值等）不要写。"
    "如果选出 2 条，请分别用 `### 标题` 写两个小节，每节 2-3 段，"
    "每节最后都要落到对临汾路街道的工作启示。"
)
ANALYST_PROMPT = (
    "你是上海市静安区临汾路街道的政务信息分析员。下面是本周各板块的周报正文，"
    "以及图谱给出的政策—行动关系线索。请综合后输出三个标签：\n"
    "<关联解读>用 3-4 段连贯文字写跨板块关联，讲清政策脉络（上位/此前政策）、"
    "已经在落实的街道或单位、以及对临汾的启示；不要用“政策意图：/落地行动：/"
    "对临汾启示：”这种字段式写法。只能使用关系线索中的直接落实关系；"
    "没有依据边时不要编造上位政策。</关联解读>\n"
    "<综合研判>300-500 字：跨板块总结本周最重要的政策、行动和基层工作信号，"
    "指出对临汾路街道的启示；最后写“工作提示”，给出 3-5 条具体工作建议。"
    "不要罗列全部内容。</综合研判>\n"
    "<导读>150-250 字：适合微信群发送，概括本周各板块选出的 2-3 个热点或政策链，"
    "优先写具体行动、工作方法和落实关系，不要只列政策名称；"
    "如果本周有上海市/静安区领导调研、考察、讲话，必须写清领导姓名、职务和活动，"
    "不要只写活动名称。</导读>\n"
    "只输出这三个标签，不要解释。"
)
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
                   style: str = 'action') -> dict:
    material, link_dict = _format_items(items, max_items)
    if not material:
        return dict(board=board, markdown='', source='empty', citations=[])
    relation_block = f'\n{relations}\n' if relations else ''
    system_prompt = (POLICY_SYSTEM_PROMPT if style == 'policy'
                     else ECONOMY_SYSTEM_PROMPT if style == 'economy'
                     else ACTION_SYSTEM_PROMPT)
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
                    relations: str = '') -> dict:
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
            dict(role='system', content=ANALYST_PROMPT),
            dict(role='user', content=f'周报日期：{date_range}\n{relation_block}\n{body}'),
        ]
        try:
            reply = await llm.chat(messages, temperature=0.2, max_tokens=1600,
                                   timeout=240)
            source = 'llm'
        except LlmError as error:
            logger.warning('weekly combined degraded: error=%s', error)
            reply = ''
            source = f'fallback:{error}'
    relation = _tagged(reply, '关联解读', '综合研判')
    analysis = _tagged(reply, '综合研判', '导读')
    digest = _tagged(reply, '导读').replace('**', '')
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


def push_text(digest: str, date: str) -> str:
    return (f'[咖啡] 为您呈上最新的《基层工作动态回顾与热点展望（{date}）》，'
            f'这就开启能量满满的一周吧！\n\n{digest}').strip()


async def write_weekly(llm: ReportLlm | None, focus_by_board: dict, *,
                       from_date: str, to_date: str, dossier: dict,
                       relations: dict, relation_brief: str = '',
                       max_items: int = 8) -> dict:
    """Per-board sections + combined analysis; deterministic on any failure."""
    results = {}
    for board, focus in focus_by_board.items():
        results[board] = await _section(
            llm, board, focus['items'], max_items,
            relations=focus['brief'], style=focus['type'])
    combined = await _combined(llm, results, _date_range(from_date, to_date),
                               relations=relation_brief)
    digest = combined.get('digest') or fallback_digest(dossier, relations)
    analysis = combined.get('analysis') or fallback_analysis(results, dossier, relations)
    relation_md = combined.get('relation') or format_relation_markdown(relations)
    markdown = report_markdown(from_date, to_date, results, analysis,
                               relation=relation_md, digest=digest)
    return dict(fromDate=from_date, toDate=to_date, sections=results,
                analysis=analysis, digest=digest, relation=relation_md,
                markdown=markdown,
                pushText=push_text(digest, datetime.now().strftime('%Y-%m-%d')),
                combinedSource=combined.get('source'),
                combinedRaw=combined.get('rawReply', '')[:2000],
                model=llm.model if llm else '')
