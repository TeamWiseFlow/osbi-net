# Weekly Hotspot Selection and Coverage History

Weekly selection runs on the server against the shared channel graph. It is an
on-demand preparation interface for action hotspots; report scheduling, automatic
document generation and outgoing delivery remain deferred.

## Ownership and locality

Ontology extraction, entity normalization, schemas, RDF stores and graph-derived
selection stay on the server. The client stores acquisition material, pending
submissions, task settings/events and server receipts/summaries. It does not persist
a graph replica. The dashboard displays graph query responses in page memory.

Coverage history is private to `(customer, channel, profile)`. Customers sharing
a channel share facts, but do not consume each other's weekly coverage. Use a
different `profile` for independently edited reports. Boards are editorial groups,
not graph channels or schema classes.

The supported action boards are grassroots party building, community governance
(the default) and digital communities. Their Chinese API enum values are defined
in the [transport contract](../contracts/osbi_contracts/weekly.py). The caller
chooses a board and may supply `candidateUris` from a graph query to narrow its
editorial scope. Without that list, selection considers action hotspots throughout
the chosen channel; automatic board assignment is not implemented. This action
selector uses a Shanghai/Jing'an locality preset. It does not restrict ingestion
or alter graph facts from other locations.

Grassroots posts do not qualify as district/city leaders merely because a local
place name is present. A concurrent city standing-committee title follows its
substantive district post. Extraction strips that concurrent segment only when
the remaining post independently identifies a concrete place. District committee
titles and unqualified remainders are preserved. A city leader visiting another
district remains a city leader; the destination is not their office jurisdiction.

## Select, write, then record

Start the normal local backend with its HTTPS server configuration. Supply JSON
options to the CLI:

```bash
echo '{"fromDate":"2026-11-02","toDate":"2026-11-09","profile":"community-weekly","minEdges":4,"topN":2}' \
  | .venv/bin/osbi-client weekly-select policy
```

The result contains `planId`, selected hotspots, original evidence articles, a
writing brief and classification counts. MCP exposes the same `weekly_select`
operation. Selection is model-free and does not record a report as completed.

The ledger looks back 28 days from `toDate`. It includes the immediately prior
week even when its end equals `fromDate`, and excludes the current week on reruns.
Match exact entity URIs first, then normalized similar names. Union evidence
article IDs across all matching prior weeks:

- **fresh**: no matching prior coverage; ranked first by score and edge count.
- **stale**: no new evidence, or new evidence is exclusively commentary/digest;
  excluded, even when slots remain.
- **progress**: substantive new evidence exists; fills only slots remaining after
  fresh hotspots. The brief requires a transition from prior coverage and only
  this week's developments, without retelling the old meeting/event.

Dates must come from publication metadata; unknown dates do not silently become
current news. Article identities are independent of extraction/schema revisions,
so re-extraction alone is not treated as a new article. Missing evidence is not
accepted as substantive progress.

After successfully producing a report from the plan, record its actual coverage:

```bash
.venv/bin/osbi-client weekly-record policy PLAN_ID
```

MCP exposes `weekly_record` for the same completion hook. A report-producing agent
must call it only after the report succeeds; previews, failed drafts and failed
artifact writes must not consume coverage. To change the chosen items, request a
plan with the actual `candidateUris` before writing. Re-recording a plan replaces
that week's board idempotently, preserving other boards and profiles. Authorization
is checked again; another customer cannot record a private plan.

The authenticated server operations are `POST /v1/channels/{channel}/weekly/select`,
`.../weekly/record` and `.../weekly/backfill`. The local facade uses the corresponding
`/api/server/channels/{channel}/weekly/...` routes. The Word import sends headings
to the server, never downloads a persistent graph snapshot.

## Backfill actual prior reports

Read Heading 2 board names and Heading 3 hotspot titles from actual `.docx` reports.
Do not rerun today's selector over historical windows: that would reconstruct a
different selection from what was actually published. Word parsing uses Python's
standard library and needs no office-suite dependency.

```bash
# Preview a single historical report; no ledger writes.
.venv/bin/osbi-client weekly-backfill policy report_20261102.docx \
  --week-end 2026-11-02 --profile community-weekly
# Apply after reviewing the returned entity/evidence matches.
.venv/bin/osbi-client weekly-backfill policy report_20261102.docx \
  --week-end 2026-11-02 --profile community-weekly --apply
# Preview all earlier reports with a YYYYMMDD.docx filename suffix.
.venv/bin/python scripts/backfill-weekly-history.py --reports-dir /path/to/reports \
  --channel policy --profile community-weekly --before 2026-11-16
```

The directory tool accepts `--apply` for explicit writes. The server matches titles
to graph hotspot names and keeps only dated articles available by the old report's
end date. Future/undated material is excluded. Unmatched headings are returned for
review and prevent an apply from replacing existing coverage. Successful reruns
are idempotent within the same customer/channel/profile/week/board.

## Existing graph data

New submissions use the corrected post extraction rules. Existing completed
graphs are preserved until explicitly reprocessed; installing code does not start
paid historical extraction. To rebuild a channel after a rule update without
changing its schema:

```bash
.venv/bin/osbi-server reprocess --channel policy --force
```

This queues replacements using retained originals and the current extraction
revision. The last successful graph remains active until its replacement succeeds.
The same revision/payload produces the same work ID on repeated administrative
requests. Normal `reprocess` still targets schema changes only.
