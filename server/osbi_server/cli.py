"""Server lifecycle and minimal credential administration."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from .config import Settings
from .repository import Repository


def main():
    parser = argparse.ArgumentParser(prog="osbi-server")
    parser.add_argument("--data", default=os.environ.get("OSBI_SERVER_DATA", "~/.osbi/server"))
    parser.add_argument("--net", help="Network identity for schema administration in multi-net mode")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8780)
    serve.add_argument("--ssl-certfile")
    serve.add_argument("--ssl-keyfile")
    issue = sub.add_parser("issue-key")
    issue.add_argument("--customer", required=True)
    issue.add_argument("--days", type=int, default=365)
    issue.add_argument("--channel", action="append")
    issue.add_argument("--rpm", type=int, default=300)
    revoke = sub.add_parser("revoke-key")
    revoke.add_argument("id")
    renew = sub.add_parser("renew-key")
    renew.add_argument("id")
    renew.add_argument("--days", type=int, required=True)
    sub.add_parser("list-keys")
    schema_review = sub.add_parser("schema-review")
    schema_review.add_argument("--channel", required=True)
    schema_activate = sub.add_parser("schema-activate")
    schema_activate.add_argument("--channel", required=True)
    schema_activate.add_argument("--file", required=True)
    schema_activate.add_argument("--expected-revision", required=True)
    reprocess = sub.add_parser("reprocess")
    reprocess.add_argument("--channel", required=True)
    reprocess.add_argument("--force", action="store_true", help="Re-extract after rule changes even when the schema is unchanged")
    snapshot = sub.add_parser("backup")
    snapshot.add_argument("--out", required=True)
    recovery = sub.add_parser("restore")
    recovery.add_argument("--from", dest="snapshot", required=True)
    args = parser.parse_args()
    data = Path(args.data).expanduser().resolve()
    if args.command == "serve":
        import uvicorn
        from .app import create_app
        settings = Settings.from_env()
        settings.data_dir = data
        uvicorn.run(create_app(settings), host=args.host, port=args.port,
                    ssl_certfile=args.ssl_certfile, ssl_keyfile=args.ssl_keyfile,
                    proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1", workers=1)
        return
    if args.command in {"backup", "restore"}:
        from .backup import backup, restore
        result = backup(data, args.out) if args.command == "backup" else restore(args.snapshot, data)
        print(json.dumps(result, indent=2))
        return
    if os.environ.get("OSBI_AUTH_MODE", "local") == "supabase":
        if args.command in {"issue-key", "list-keys", "revoke-key", "renew-key"}:
            parser.error("Manage keys in Supabase Studio: ops.osbi_keys")
        import re
        if not args.net or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", args.net):
            parser.error("A valid --net is required in Supabase mode")
        data = data / "nets" / args.net
        if not (data / "metadata.db").is_file():
            parser.error("Unknown network state")
    repo = Repository(data / "metadata.db")
    try:
        if args.command == "issue-key":
            value = repo.issue_key(args.customer, (datetime.now(timezone.utc) + timedelta(days=args.days)).timestamp(), args.channel, args.rpm)
        elif args.command == "list-keys":
            value = repo.rows("SELECT id,customer,expires,revoked,grants,rpm FROM api_keys")
        elif args.command == "revoke-key":
            if not repo.execute("UPDATE api_keys SET revoked=1 WHERE id=?", (args.id,)).rowcount:
                parser.error("Unknown key ID")
            value = {"revoked": args.id}
        elif args.command == "renew-key":
            if args.days <= 0:
                parser.error("Renewal days must be positive")
            expires = (datetime.now(timezone.utc) + timedelta(days=args.days)).timestamp()
            if not repo.execute("UPDATE api_keys SET expires=? WHERE id=?", (expires, args.id)).rowcount:
                parser.error("Unknown key ID")
            value = {"renewed": args.id, "expires": expires}
        elif args.command == "schema-review":
            from .admin import review
            value = review(repo, args.channel)
        elif args.command == "schema-activate":
            from .admin import activate
            value = activate(repo, args.channel, Path(args.file).read_text(), args.expected_revision)
        elif args.command == "reprocess":
            from .admin import reprocess
            value = reprocess(repo, args.channel, force=args.force, settings=Settings.from_env())
        print(json.dumps(value, ensure_ascii=False, indent=2))
    finally:
        repo.close()


if __name__ == "__main__":
    main()
