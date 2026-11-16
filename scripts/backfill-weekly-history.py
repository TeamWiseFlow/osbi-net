#!/usr/bin/env python3
"""Preview or apply prior Word report headings using the running local backend."""
import argparse
import asyncio
from datetime import date
import json
import os
from pathlib import Path
import re
from osbi_client.cli import call_local
from osbi_client.report_headings import parse_sections


def prior_reports(directory, before):
    reports = []
    for path in Path(directory).glob("*.docx"):
        match = re.search(r'(\d{8})\.docx$', path.name)
        if not match:
            continue
        try:
            week_end = date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue
        if week_end < before:
            reports.append((week_end, path))
    return sorted(reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", required=True)
    parser.add_argument("--channel", default="policy")
    parser.add_argument("--profile", default="default")
    parser.add_argument("--before", default=date.today().isoformat())
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    before = date.fromisoformat(args.before).isoformat()
    for week_end, path in prior_reports(args.reports_dir, before):
        result = asyncio.run(call_local(os.environ.get("OSBI_CLIENT_DATA", "~/.osbi/client"),
            os.environ.get("OSBI_CLIENT_URL", "http://127.0.0.1:8781"), "POST",
            f"/api/weekly/{args.channel}/backfill",
            {"profile": args.profile, "weekEnd": week_end, "sections": parse_sections(path), "apply": args.apply}))
        print(json.dumps({"report": str(path), **result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
