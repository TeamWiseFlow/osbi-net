"""Local weekly coverage ledgers; shared graph facts remain on the server."""
from datetime import date, timedelta
from difflib import SequenceMatcher
import json
import re
import time
from osbi_contracts.storage import encode

LOOKBACK_DAYS = 28
NAME_SIMILARITY = 0.6
COMMENTARY_FLAGS = {"commentary", "digest"}


def normalized_name(value):
    return re.sub(r'[\s\W_《》]+', '', value or '')


def similarity(left, right):
    """Compare normalized characters, containment and bigrams without tokenizers."""
    a, b = normalized_name(left), normalized_name(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()
    containment = min(len(a), len(b)) / max(len(a), len(b)) if a in b or b in a else 0.0
    def grams(value):
        return {value[i:i + 2] for i in range(len(value) - 1)} if len(value) > 1 else {value}
    aa, bb = grams(a), grams(b)
    jaccard = len(aa & bb) / len(aa | bb)
    return max(ratio, containment * .95, jaccard * .9)


def load_reported_hotspots(repo, customer, channel, profile, before_date, *, lookback_days=LOOKBACK_DAYS):
    start = (date.fromisoformat(before_date) - timedelta(days=lookback_days)).isoformat()
    rows = repo.rows("""SELECT * FROM weekly_hotspots WHERE customer=? AND channel=? AND profile=?
                        AND week_end<? AND week_end>=? ORDER BY week_end DESC,board,uri""",
                     (customer, channel, profile, before_date, start))
    return [dict(weekEnd=r["week_end"], board=r["board"], uri=r["uri"], name=r["name"],
                 label=r["label"], articleIds=json.loads(r["article_ids"]), topic=r["topic"]) for r in rows]


def match_priors(name, uri, priors):
    matches = [p for p in priors if uri and p["uri"] == uri]
    if not matches:
        matches = [p for p in priors if similarity(name, p["name"]) >= NAME_SIMILARITY]
    return sorted(matches, key=lambda p: p["weekEnd"], reverse=True)


def record_reported_hotspots(repo, customer, channel, profile, week_end, focus_by_board):
    """Replace reported boards atomically, preserving other profiles and boards."""
    date.fromisoformat(week_end)
    count = 0
    with repo.transaction() as conn:
        for board, focus in focus_by_board.items():
            conn.execute("DELETE FROM weekly_hotspots WHERE customer=? AND channel=? AND profile=? AND week_end=? AND board=?",
                         (customer, channel, profile, week_end, board))
            for hotspot in focus.get("hotspots", []):
                conn.execute("INSERT OR REPLACE INTO weekly_hotspots VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (customer, channel, profile, week_end, board, hotspot["uri"], hotspot["name"],
                              hotspot["label"], encode(sorted(set(hotspot["articleIds"]))),
                              hotspot.get("topic") or hotspot["name"], time.time()))
            count += len({h["uri"] for h in focus.get("hotspots", [])})
    return count


def classify_repeat(articles, row, priors):
    """Discard pure repeats; only substantive new evidence permits a progress slot."""
    matches = match_priors(row.get("name", ""), row.get("uri", ""), priors)
    if not matches:
        return "fresh", None
    prior_ids = {aid for match in matches for aid in match.get("articleIds", [])}
    new_ids = set(row.get("articleIds", [])) - prior_ids
    latest = matches[0]
    info = dict(weekEnd=latest["weekEnd"], topic=latest.get("topic") or latest["name"], newCount=len(new_ids))
    substantive = [aid for aid in new_ids if aid in articles and
                   not COMMENTARY_FLAGS.intersection(articles[aid].get("qualityFlags") or [])]
    return ("progress" if substantive else "stale"), info


def select_hotspots(rows, articles, priors, top_n):
    """Fresh candidates precede progress regardless of raw score; stale never fills slots."""
    kept, counts = [], dict(stale=0, progress=0, fresh=0)
    for original in rows:
        row = dict(original)
        row.pop("priorReported", None)
        status, info = classify_repeat(articles, row, priors)
        counts[status] += 1
        if status == "stale":
            continue
        if status == "progress":
            row["priorReported"] = info
        kept.append(row)
    kept.sort(key=lambda r: (bool(r.get("priorReported")), -r["score"], -r["edgeCount"], r["uri"]))
    selected = []
    for row in kept:
        if any(set(row["articleIds"]) & set(p["articleIds"]) and
               (similarity(row["name"], p["name"]) >= .45 or
                (len(row["articleIds"]) == 1 and len(p["articleIds"]) == 1)) for p in selected):
            continue
        selected.append(row)
        if len(selected) >= top_n:
            break
    return selected, counts
