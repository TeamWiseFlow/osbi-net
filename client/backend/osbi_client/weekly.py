"""Local weekly selection and coverage, consuming generic server evidence queries."""
from collections import defaultdict
from dataclasses import dataclass
import json
import re
import time
from osbi_contracts.storage import digest, encode
from osbi_contracts.urls import canonical_url, normalize_publish_date
from .leader_rules import is_target_jurisdiction, is_target_leader_sentence, is_non_target_leader
from .weekly_history import (load_reported_hotspots, normalized_name,
                             record_reported_hotspots, select_hotspots, similarity)
from .report_settings import resolve_board

HOTSPOT_LABELS = {"行动事项", "市级行动", "区级行动", "街镇行动", "社区行动",
                  "居民活动", "调研视察", "治理机制", "数字平台系统"}
LEADER_TEXT_LABELS = {"调研视察", "会议论坛", "事件", "行动事项", "市级行动",
                      "区级行动", "街镇行动", "社区行动"}
SIGNAL_PREDICATES = {"参与", "实施主体", "主办", "落实", "服务", "采用", "建设运营",
                     "前往", "考察", "依据", "响应", "提及", "涉及", "发布"}
ACTION_SYSTEM_PROMPT = (
    "围绕选出的热点撰写连贯段落，每条事实必须有原文依据。"
    "若含跨周提示，必须聚焦本周相对上周的新进展、新做法或动态变化，"
    "开头点明承接关系，不要重复上周已报的会议/事件本身。若只是旧事重述则不写。"
)


@dataclass
class Snapshot:
    nodes: dict
    edges: list
    articles: dict
    decision_filtered: bool = False
    discarded_records: int = 0


def snapshot_from_evidence(records):
    nodes, edges, articles = {}, {}, {}
    for record in records:
        article_ids = set()
        for source in record["sources"]:
            meta = source["meta"]
            url = canonical_url(meta.get("canonicalUrl") or meta.get("finalUrl") or meta["requestedUrl"])
            published = normalize_publish_date(meta.get("publishedAt", ""))
            aid = "a_" + digest([url, published])
            item = articles.setdefault(aid, dict(articleId=aid, url=url, publishedDate=published,
                title=source["title"], publisher=meta.get("publisher", ""), nodeUris=set(), qualityFlags=set(), blocks={}))
            item["nodeUris"].update(e["uri"] for e in record["entities"])
            item["qualityFlags"].update(record.get("qualityFlags", []))
            item["blocks"][record["evidenceId"]] = record["text"]
            article_ids.add(aid)
        for node in record["entities"]:
            nodes[node["uri"]] = dict(node)
        for edge in record["relations"]:
            key = (edge["subject"], edge["label"], edge["object"])
            item = edges.setdefault(key, dict(subject=edge["subject"], object=edge["object"],
                                              predicateLabel=edge["label"], articleIds=set()))
            item["articleIds"].update(article_ids)
    for item in articles.values():
        item["nodeUris"] = sorted(item["nodeUris"])
        item["qualityFlags"] = sorted(item["qualityFlags"])
        item["rawText"] = "\n".join(item.pop("blocks").values())
    return Snapshot(nodes, list(edges.values()), articles)


async def load_snapshot(daemon, channel, *, from_date="", to_date="", entity_uris=(), text="", intent="", apply_decisions=True):
    from .server_client import request
    from .decisions import filter_evidence
    config = daemon.state.config()
    records, cursor, as_of = [], "", None
    for _ in range(100):
        page = await request(config, "POST", f"/v1/channels/{channel}/evidence",
            {"fromDate": from_date, "toDate": to_date, "entityUris": list(entity_uris), "text": text,
             "cursor": cursor, "asOf": as_of, "limit": 100},
            transport=daemon.transport)
        records.extend(page["items"])
        next_cursor = page["cursor"]
        if not next_cursor:
            break
        if next_cursor == cursor:
            raise ValueError("Evidence cursor did not advance")
        cursor, as_of = next_cursor, page["asOf"]
    else:
        raise ValueError("Evidence query is too broad; narrow the dates or entity selection")
    before = len(records)
    if apply_decisions:
        records = await filter_evidence(daemon, records, intent or config.focus)
    snapshot = snapshot_from_evidence(records)
    snapshot.decision_filtered = bool(apply_decisions and config.jev.enabled and config.jev.postQuery)
    snapshot.discarded_records = before - len(records)
    return snapshot


async def coverage_scope(daemon):
    from .server_client import request
    config = daemon.state.config()
    identity = await request(config, "GET", "/v1/identity", transport=daemon.transport)
    return digest([config.serverUrl, identity["customerId"]])


def action_candidates(snapshot, from_date, to_date, min_edges=4, candidate_uris=()):
    window = {aid for aid, a in snapshot.articles.items()
              if a["publishedDate"] and from_date <= a["publishedDate"] <= to_date}
    node_articles = defaultdict(set)
    for aid in window:
        for uri in snapshot.articles[aid]["nodeUris"]:
            node_articles[uri].add(aid)
    target_persons = {e["subject"] for e in snapshot.edges if e["predicateLabel"] in {"现任", "任职"}
                      and snapshot.nodes[e["object"]]["label"] == "职务"
                      and is_target_jurisdiction(snapshot.nodes[e["object"]]["name"])}
    rows = []
    for uri, ids in sorted(node_articles.items()):
        if candidate_uris and uri not in candidate_uris:
            continue
        node = snapshot.nodes[uri]
        text = "\n".join(snapshot.articles[aid]["title"] + "\n" + snapshot.articles[aid]["rawText"] for aid in sorted(ids))
        snippets = [s.strip()[:180] for s in re.split(r'[。；！？\n]', text)
                    if is_target_leader_sentence(s, text[:300])] if node["label"] in LEADER_TEXT_LABELS else []
        adjacent = [e for e in snapshot.edges if uri in {e["subject"], e["object"]} and window.intersection(e["articleIds"])]
        leader_edges = [e for e in adjacent if e["predicateLabel"] in {"参与", "前往", "考察", "强调"}
                        and ({e["subject"], e["object"]} - {uri}).intersection(target_persons)]
        leader = bool(snippets or leader_edges) and node["label"] in LEADER_TEXT_LABELS
        if node["label"] not in HOTSPOT_LABELS and not (leader and node["label"] in {"会议论坛", "事件"}):
            continue
        signal = [e for e in adjacent if e["predicateLabel"] in SIGNAL_PREDICATES
                  and snapshot.nodes[e["object"] if e["subject"] == uri else e["subject"]]["label"] not in {"概念议题", "通用名词", "职务", "人物"}]
        if len(signal) < (1 if leader else min_edges):
            continue
        non_target = not leader and node["label"] in LEADER_TEXT_LABELS and is_non_target_leader(text)
        units = {snapshot.nodes[e["object"] if e["subject"] == uri else e["subject"]]["name"] for e in signal
                 if "机构组织" in snapshot.nodes[e["object"] if e["subject"] == uri else e["subject"]].get("ancestors", [])}
        rows.append(dict(**node, articleIds=sorted(ids), edgeCount=len(signal), units=sorted(units),
                         score=len(signal) + len(units) + (300 if leader else 0) - (300 if non_target else 0),
                         leader=leader, nonTarget=non_target, leaderSnippets=snippets[:3]))
    return rows


def format_action_brief(hotspots):
    lines = [ACTION_SYSTEM_PROMPT]
    for row in hotspots:
        if row["label"] == "Article":
            lines.append("候选原文：" + row["name"])
        else:
            lines.append(f"{row['name']}（{row['label']}）：{row['edgeCount']} 条关系边；实施/参与单位：" + '、'.join(row['units']))
        if row.get("priorReported"):
            prior = row["priorReported"]
            lines.append(f"跨周提示：该热点在 {prior['weekEnd']} 已报道（当时主题：{prior['topic']}）。"
                         f"本周新增 {prior['newCount']} 篇材料，必须聚焦本周相对上周的新进展，"
                         '开头点明“上周报道的…本周…”，不要重复旧会议/事件。')
        lines.extend("领导活动原文：" + s for s in row.get("leaderSnippets", []))
    return '\n'.join(lines)


async def select_weekly(daemon, channel, request):
    _, board, intent = resolve_board(daemon.state.config(), request.profile, "weeklyreport", request.board, channel)
    customer = await coverage_scope(daemon)
    snapshot = await load_snapshot(daemon, channel, from_date=request.fromDate, to_date=request.toDate,
        entity_uris=request.candidateUris, text=board.query, intent=intent)
    rows = (action_candidates(snapshot, request.fromDate, request.toDate, request.minEdges or board.minEdges, set(request.candidateUris))
            if board.selector == "policy-actions" else evidence_candidates(snapshot, request.fromDate, request.toDate))
    # The upper bound is the current week end, including the immediately prior week.
    priors = load_reported_hotspots(daemon.state, customer, channel, request.profile, request.toDate)
    selected, counts = select_hotspots(rows, snapshot.articles, priors, request.topN or board.topN)
    ids = {aid for row in selected for aid in row["articleIds"]}
    articles = [{k: v for k, v in snapshot.articles[aid].items() if k not in {"nodeUris"}} for aid in sorted(ids)]
    result = {**request.model_dump(exclude={"candidateUris"}), "channelId": channel, "board": board.title,
              "boardId": board.id, "topN": request.topN or board.topN, "minEdges": request.minEdges or board.minEdges,
              "selector": board.selector, "focus": intent, "hotspots": selected, "articles": articles,
              "brief": intent + "\n" + format_action_brief(selected), "classificationCounts": counts}
    if snapshot.decision_filtered:
        result["jev"] = {"filtered": True, "discardedRecords": snapshot.discarded_records}
    plan_id = "wp_" + digest([customer, result])
    daemon.state.execute("INSERT OR IGNORE INTO weekly_plans VALUES (?,?,?,?,?)",
                         (plan_id, customer, channel, encode(result), time.time()))
    return dict(planId=plan_id, **result)


def evidence_candidates(snapshot, from_date, to_date):
    """Rank original stories by publication date without assuming ontology classes."""
    from datetime import date
    return [dict(uri="article:" + aid, name=item["title"], label="Article", articleIds=[aid],
                 score=date.fromisoformat(item["publishedDate"]).toordinal(), edgeCount=0, units=[])
            for aid, item in snapshot.articles.items()
            if item["publishedDate"] and from_date <= item["publishedDate"] <= to_date]


async def select_topnews(daemon, channel, request):
    _, board, intent = resolve_board(daemon.state.config(), request.profile, "topnews", request.board, channel)
    snapshot = await load_snapshot(daemon, channel, from_date=request.fromDate, to_date=request.toDate,
        entity_uris=request.candidateUris, text=board.query, intent=intent)
    rows = (action_candidates(snapshot, request.fromDate, request.toDate, request.minEdges or board.minEdges, set(request.candidateUris))
            if board.selector == "policy-actions" else evidence_candidates(snapshot, request.fromDate, request.toDate))
    selected, _ = select_hotspots(rows, snapshot.articles, [], request.topN or board.topN)
    ids = {aid for row in selected for aid in row["articleIds"]}
    result = dict(channelId=channel, profile=request.profile, board=board.title, boardId=board.id,
        fromDate=request.fromDate, toDate=request.toDate, topN=request.topN or board.topN, focus=intent,
        selector=board.selector, hotspots=selected,
        articles=[{k: v for k, v in snapshot.articles[aid].items() if k != "nodeUris"} for aid in sorted(ids)],
        brief=intent + "\n" + format_action_brief(selected))
    if snapshot.decision_filtered:
        result["jev"] = {"filtered": True, "discardedRecords": snapshot.discarded_records}
    return result


def record_weekly(daemon, channel, plan_id):
    row = daemon.state.one("SELECT body,customer FROM weekly_plans WHERE id=? AND channel=?", (plan_id, channel))
    if not row:
        raise ValueError("Weekly plan not found")
    plan = json.loads(row["body"])
    customer = row["customer"]
    count = record_reported_hotspots(daemon.state, customer, channel, plan["profile"], plan["toDate"], {plan["board"]: plan})
    return dict(planId=plan_id, recorded=count, weekEnd=plan["toDate"])


def match_nodes(title, snapshot):
    normalized = normalized_name(title)
    nodes = [n for n in snapshot.nodes.values() if n["label"] in HOTSPOT_LABELS]
    contained = [n for n in nodes if len(normalized_name(n["name"])) >= 4 and normalized_name(n["name"]) in normalized]
    if contained:
        return sorted(contained, key=lambda n: (-len(normalized_name(n["name"])), n["uri"]))[:3]
    return sorted([n for n in nodes if similarity(title, n["name"]) >= .6],
                  key=lambda n: (-similarity(title, n["name"]), n["uri"]))[:2]


async def backfill_weekly(daemon, channel, request):
    """Match actual historical report headings, never rerun today's selection for old weeks."""
    customer = await coverage_scope(daemon)
    snapshot = await load_snapshot(daemon, channel, to_date=request.weekEnd, apply_decisions=False)
    focus, unmatched = {}, []
    for board, titles in request.sections.items():
        _, settings, _ = resolve_board(daemon.state.config(), request.profile, "weeklyreport", board, channel)
        hotspots = {}
        for title in titles:
            matches = (match_nodes(title, snapshot) if settings.selector == "policy-actions" else
                       [n for n in evidence_candidates(snapshot, "0001-01-01", request.weekEnd)
                        if similarity(title, n["name"]) >= .6][:3])
            if not matches:
                unmatched.append(dict(board=board, title=title))
            for node in matches:
                ids = sorted(aid for aid, a in snapshot.articles.items() if (node["uri"] in a["nodeUris"] or aid in node.get("articleIds", []))
                             and a["publishedDate"] and a["publishedDate"] <= request.weekEnd)
                if not ids:
                    unmatched.append(dict(board=board, title=title, reason="no dated evidence before report"))
                    continue
                hotspots[node["uri"]] = {**node, "articleIds": ids, "topic": title}
        focus[settings.title] = dict(hotspots=list(hotspots.values()))
    if request.apply and unmatched:
        raise ValueError("Some historical headings have no dated graph match; review the dry run before applying")
    count = record_reported_hotspots(daemon.state, customer, channel, request.profile, request.weekEnd, focus) if request.apply else 0
    return dict(weekEnd=request.weekEnd, focusByBoard=focus, unmatched=unmatched, recorded=count, dryRun=not request.apply)
