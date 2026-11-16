"""Local daemon lifecycle and JSON agent commands."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import httpx


def local_credentials(data_dir):
    from osbi_contracts.storage import Database
    path = Path(data_dir).expanduser() / "client.db"
    if not path.exists():
        raise RuntimeError("Start osbi-client serve before using client commands")
    db = Database(path)
    try:
        row = db.one("SELECT local_token FROM identity")
        return row["local_token"]
    finally:
        db.close()


async def call_local(data_dir, base_url, method, path, body=None):
    from urllib.parse import urlsplit
    p = urlsplit(base_url)
    if p.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local backend URL must use loopback")
    key = local_credentials(data_dir)
    async with httpx.AsyncClient(timeout=80, trust_env=False) as client:
        response = await client.request(method, base_url.rstrip("/") + path, json=body,
                                        headers={"Authorization": "Bearer " + key, "X-OSBI-Local": "1"})
    if not response.is_success:
        try:
            message = response.json().get("error", "Local backend request failed")
        except ValueError:
            message = "Local backend request failed"
        raise RuntimeError(message)
    return response.json()


def main():
    parser = argparse.ArgumentParser(prog="osbi-client")
    parser.add_argument("--data", default=os.environ.get("OSBI_CLIENT_DATA", "~/.osbi/client"))
    parser.add_argument("--url", default=os.environ.get("OSBI_CLIENT_URL", "http://127.0.0.1:8781"))
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8781)
    for name in ["status", "config", "runs", "channels", "events"]:
        commands.add_parser(name)
    commands.add_parser("configure", help="Read a configuration update from JSON stdin")
    run = commands.add_parser("run")
    run.add_argument("source_id")
    for name in ["resume", "cancel", "browser"]:
        p = commands.add_parser(name)
        p.add_argument("run_id")
        if name == "resume":
            p.add_argument("--prompt-id")
    query = commands.add_parser("query")
    query.add_argument("channel")
    query.add_argument("source")
    query.add_argument("--target")
    query.add_argument("--depth", type=int, default=1)
    evidence = commands.add_parser("evidence", help="Read generic evidence-query options from JSON stdin")
    evidence.add_argument("channel")
    weekly_select = commands.add_parser("weekly-select", help="Read selection options from JSON stdin")
    weekly_select.add_argument("channel")
    topnews_select = commands.add_parser("topnews-select", help="Prepare one configured local TopNews board from JSON stdin")
    topnews_select.add_argument("channel")
    weekly_record = commands.add_parser("weekly-record", help="Record a plan only after its report was produced successfully")
    weekly_record.add_argument("channel")
    weekly_record.add_argument("plan_id")
    weekly_backfill = commands.add_parser("weekly-backfill", help="Preview or apply history from an actual Word report")
    weekly_backfill.add_argument("channel")
    weekly_backfill.add_argument("report")
    weekly_backfill.add_argument("--week-end", required=True)
    weekly_backfill.add_argument("--profile", default="default")
    weekly_backfill.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn
        from .app import create_app
        uvicorn.run(create_app(Path(args.data).expanduser()), host="127.0.0.1", port=args.port)
        return
    routes = {"status": "/api/status", "config": "/api/config", "runs": "/api/runs", "channels": "/api/server/channels", "events": "/api/events"}
    method, body = "GET", None
    if args.command in routes:
        path = routes[args.command]
    elif args.command == "configure":
        method, path, body = "PATCH", "/api/config", json.load(sys.stdin)
    elif args.command == "run":
        method, path, body = "POST", "/api/runs", {"sourceId": args.source_id}
    elif args.command == "query":
        method, path = "POST", "/api/server/channels/" + args.channel + "/query"
        body = {"source": args.source, "target": args.target, "depth": args.depth}
    elif args.command == "evidence":
        method, path, body = "POST", f"/api/server/channels/{args.channel}/evidence", json.load(sys.stdin)
    elif args.command == "topnews-select":
        method, path, body = "POST", f"/api/topnews/{args.channel}/select", json.load(sys.stdin)
    elif args.command.startswith("weekly-"):
        action = args.command.removeprefix("weekly-")
        method, path = "POST", f"/api/weekly/{args.channel}/{action}"
        if action == "select":
            body = json.load(sys.stdin)
        elif action == "record":
            body = {"planId": args.plan_id}
        else:
            from .report_headings import parse_sections
            body = {"weekEnd": args.week_end, "profile": args.profile, "apply": args.apply,
                    "sections": parse_sections(args.report)}
    else:
        method, path = "POST", "/api/runs/" + args.run_id + "/" + args.command
        body = json.load(sys.stdin) if args.command == "browser" else {"promptId": getattr(args, "prompt_id", None)}
    try:
        result = asyncio.run(call_local(args.data, args.url, method, path, body))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError, httpx.HTTPError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
