"""Server-owned weekly selection, completed-report receipts and historical backfill."""
from collections import defaultdict
from dataclasses import dataclass
import json
import re
import time
from osbi_contracts.storage import digest, encode
from osbi_contracts.urls import canonical_url, normalize_publish_date
from .leader_rules import is_target_jurisdiction, is_target_leader_sentence, is_non_target_leader
from .ontology_rules import ORG_LABELS
from .weekly_history import (ACTION_BOARDS, load_reported_hotspots, normalized_name,
                             record_reported_hotspots, select_hotspots, similarity)

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


def load_snapshot(service, channel):
    """Read the designated store owner; never export a persistent client graph copy."""
    nodes, edges, articles = {}, {}, {}
    for row in service.repo.rows("SELECT * FROM active_graphs WHERE channel=? ORDER BY updated", (channel,)):
        intent = service.repo.one("SELECT result FROM work WHERE id=?", (row["work_id"],))
        assignment = next((a for a in json.loads(intent["result"]).get("assignments", [])
                           if a["channelId"] == channel), None) if intent else None
        if not assignment:
            continue
        result = service.engine(channel, assignment["schemaId"]).store.read(row["extraction_id"])
        if not result:
            continue
        provenance = result["provenance"]
        observations = service.repo.rows("""SELECT r.payload FROM requests r JOIN graph_sources s ON s.request_id=r.id
                    WHERE s.channel=? AND s.extraction_id=? ORDER BY r.created""", (channel, row["extraction_id"]))
        sources = [json.loads(r["payload"]) for r in observations]
        if not sources:
            sources = [{"meta": provenance["source_meta"], "title": provenance["source_title"]}]
        article_ids = set()
        for source in sources:
            meta = source["meta"]
            url = canonical_url(meta.get("canonicalUrl") or meta.get("finalUrl") or meta.get("requestedUrl") or provenance["source_url"])
            published = normalize_publish_date(meta.get("publishedAt", ""))
            aid = "a_" + digest([url, published])
            item = articles.setdefault(aid, dict(articleId=aid, url=url, publishedDate=published,
                title=source["title"], publisher=meta.get("publisher", ""), nodeUris=set(), qualityFlags=set(), blocks={}))
            item["nodeUris"].update(e["uri"] for e in result["entities"])
            item["qualityFlags"].update(provenance.get("quality_flags", []))
            item["blocks"][result["id"]] = provenance["raw_text"]
            article_ids.add(aid)
        for node in result["entities"]:
            nodes[node["uri"]] = dict(uri=node["uri"], name=node["text"], label=node["label"])
        for edge in result["relations"]:
            key = (edge["subject"], edge["predicate"], edge["object"])
            item = edges.setdefault(key, dict(subject=edge["subject"], object=edge["object"],
                                              predicateLabel=edge["label"], articleIds=set()))
            item["articleIds"].update(article_ids)
    for item in articles.values():
        item["nodeUris"] = sorted(item["nodeUris"])
        item["qualityFlags"] = sorted(item["qualityFlags"])
        item["rawText"] = "\n".join(item.pop("blocks").values())
    return Snapshot(nodes, list(edges.values()), articles)


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
                 if snapshot.nodes[e["object"] if e["subject"] == uri else e["subject"]]["label"] in ORG_LABELS}
        rows.append(dict(**node, articleIds=sorted(ids), edgeCount=len(signal), units=sorted(units),
                         score=len(signal) + len(units) + (300 if leader else 0) - (300 if non_target else 0),
                         leader=leader, nonTarget=non_target, leaderSnippets=snippets[:3]))
    return rows


def format_action_brief(hotspots):
    lines = [ACTION_SYSTEM_PROMPT]
    for row in hotspots:
        lines.append(f"{row['name']}（{row['label']}）：{row['edgeCount']} 条关系边；实施/参与单位：" + '、'.join(row['units']))
        if row.get("priorReported"):
            prior = row["priorReported"]
            lines.append(f"跨周提示：该热点在 {prior['weekEnd']} 已报道（当时主题：{prior['topic']}）。"
                         f"本周新增 {prior['newCount']} 篇材料，必须聚焦本周相对上周的新进展，"
                         '开头点明“上周报道的…本周…”，不要重复旧会议/事件。')
        lines.extend("领导活动原文：" + s for s in row.get("leaderSnippets", []))
    return '\n'.join(lines)


def select_weekly(service, customer, channel, request):
    snapshot = load_snapshot(service, channel)
    rows = action_candidates(snapshot, request.fromDate, request.toDate, request.minEdges, set(request.candidateUris))
    # The upper bound is the current week end, including the immediately prior week.
    priors = load_reported_hotspots(service.repo, customer, channel, request.profile, request.toDate)
    selected, counts = select_hotspots(rows, snapshot.articles, priors, request.topN)
    ids = {aid for row in selected for aid in row["articleIds"]}
    articles = [{k: v for k, v in snapshot.articles[aid].items() if k not in {"nodeUris"}} for aid in sorted(ids)]
    result = dict(channelId=channel, **request.model_dump(exclude={"candidateUris"}), hotspots=selected,
                  articles=articles, brief=format_action_brief(selected), classificationCounts=counts)
    plan_id = "wp_" + digest([customer, result])
    service.repo.execute("INSERT OR IGNORE INTO weekly_plans VALUES (?,?,?,?,?)",
                         (plan_id, customer, channel, encode(result), time.time()))
    return dict(planId=plan_id, **result)


def record_weekly(service, customer, channel, plan_id):
    row = service.repo.one("SELECT body FROM weekly_plans WHERE id=? AND customer=? AND channel=?", (plan_id, customer, channel))
    if not row:
        raise ValueError("Weekly plan not found")
    plan = json.loads(row["body"])
    count = record_reported_hotspots(service.repo, customer, channel, plan["profile"], plan["toDate"], {plan["board"]: plan})
    return dict(planId=plan_id, recorded=count, weekEnd=plan["toDate"])


def match_nodes(title, snapshot):
    normalized = normalized_name(title)
    nodes = [n for n in snapshot.nodes.values() if n["label"] in HOTSPOT_LABELS]
    contained = [n for n in nodes if len(normalized_name(n["name"])) >= 4 and normalized_name(n["name"]) in normalized]
    if contained:
        return sorted(contained, key=lambda n: (-len(normalized_name(n["name"])), n["uri"]))[:3]
    return sorted([n for n in nodes if similarity(title, n["name"]) >= .6],
                  key=lambda n: (-similarity(title, n["name"]), n["uri"]))[:2]


def backfill_weekly(service, customer, channel, request):
    """Match actual historical report headings, never rerun today's selection for old weeks."""
    snapshot = load_snapshot(service, channel)
    focus, unmatched = {}, []
    for board, titles in request.sections.items():
        if board not in ACTION_BOARDS:
            continue
        hotspots = {}
        for title in titles:
            matches = match_nodes(title, snapshot)
            if not matches:
                unmatched.append(dict(board=board, title=title))
            for node in matches:
                ids = sorted(aid for aid, a in snapshot.articles.items() if node["uri"] in a["nodeUris"]
                             and a["publishedDate"] and a["publishedDate"] <= request.weekEnd)
                if not ids:
                    unmatched.append(dict(board=board, title=title, reason="no dated evidence before report"))
                    continue
                hotspots[node["uri"]] = dict(**node, articleIds=ids, topic=title)
        focus[board] = dict(hotspots=list(hotspots.values()))
    if request.apply and unmatched:
        raise ValueError("Some historical headings have no dated graph match; review the dry run before applying")
    count = record_reported_hotspots(service.repo, customer, channel, request.profile, request.weekEnd, focus) if request.apply else 0
    return dict(weekEnd=request.weekEnd, focusByBoard=focus, unmatched=unmatched, recorded=count, dryRun=not request.apply)
