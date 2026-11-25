# OSBI Client: Deployment and User Manual

English | [简体中文](README.zh-CN.md)

The client runs a persistent local backend, managed browser, dashboard, CLI and
MCP interface. It collects sources and consumes shared evidence. The separate
server owns relevance selection, channel definitions, ontology extraction and
graph storage. The client does not keep a persistent graph replica.

## Install and Start

Use a separate checkout and data directory for each client deployment. Linux
x86_64 is the verified environment. The Bash installer has Linux/macOS x86_64 and
arm64 paths; native Windows and macOS operation have not been verified. Supply
Bash, curl, tar, a SHA-256 utility, browser OS libraries, and a graphical session
when using a visible browser. Installation needs Python/npm and asset downloads.

From the repository root:

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
bash scripts/install-client.sh
bash scripts/run-client.sh
```

The installer installs `.[client]`, builds `client/backend/browser`, and verifies
pinned browser, fingerprint, addon and GeoIP assets. It reuses suitable Python
3.12+ and Node 24.9+ runtimes or provisions pinned runtimes under `.osbi/runtime`.
Python packages live in `.venv`; system runtimes and shell profiles are unchanged.
Server graph-store dependencies and an agent host are not required.

The backend listens at **http://127.0.0.1:8781**. Keep it running independently of
dashboard tabs and agent sessions. To select another data directory or port:

```bash
bash scripts/run-client.sh --data "$HOME/.osbi/client-demo" serve --port 8782
```

CLI and agent processes must use the matching `OSBI_CLIENT_DATA` and
`OSBI_CLIENT_URL`. The default state is `~/.osbi/client`. Keep the control API on
loopback; for remote access, use an SSH tunnel, for example
`ssh -L 8781:127.0.0.1:8781 CLIENT_HOST`. Visible browser interaction still needs
the client host's graphical session.

For runtime/asset caches and manual setup, see [installation details](../docs/running.md).
The installer does not configure a shared server, login to WeChat, or send messages.

## Interface Language

The dashboard defaults to **English**, including when the browser's language is
Chinese. Use **Interface language** in the header to select **简体中文**. The choice
is saved in browser-local storage and retained across navigation and reloads.
Switching language updates labels, hints, placeholders and dates in place;
unsaved form input is retained and no configuration is submitted by the switch.
If browser storage is unavailable, the dashboard still works; a reload starts in English.

TopNews' Chinese **加油站** is labeled **Do you Know** in English. Language selection
does not translate collected evidence, user-defined names, report copy, model
output or original diagnostic messages. Configure report wording/prompts separately.

## First Configuration

Open `/settings` (**Sources**):

1. In **Shared server**, enter the server's HTTPS URL and customer/node key.
   Use **Custom CA** only for a private certificate authority. Save this section.
2. Add a source, give it a stable ID and select web, RSS or WeChat public account.
3. Select its submission channels from the server catalogue. Multiple channels
   create separate submissions; no channels means collection without submission.
4. Set the browser profile, collection interval and optional workflow. Save sources.
5. Click **Run**. Check **Notifications** for progress or manual login/verification.

Each settings section saves independently. Keys are write-only; blank key inputs
retain stored values. Acquisition preferences include global/per-domain concurrency,
URL exclusion fragments, timeouts, headless mode and cache TTLs.

With no daily capture times, a collection interval of `0` means manual runs only. Nonzero intervals are handled
by the running backend. Browser profiles retain session state; a headless task
may still need a visible session to complete a source's login or verification.
Sources can use typed workflows for navigate/click/fill/select/wait/scroll/switch/
capture/manual-wait steps. Use the management skill for complex workflows.

For WeChat public-account sources, enter exact account display names, one per
line; their fixed entry is `https://mp.weixin.qq.com/`. The catalog hunter needs a
separately provisioned, logged-in MimicWX installation and its database/key access.
`MIMICWX_CONTAINER`, `MIMICWX_WX_USER_DIR` and `MIMICWX_KEYS_FILE` select that
installation. The installer does not provide it. The hunter discovers article
URLs and the browser captures article bodies; programmable steps are not used
for this source type. See [MP design](../docs/mp-source-design.md).

## Source Capture Schedules

In **Sources**, configure each source's **Daily capture times**. Add multiple
24-hour `HH:MM` times, such as `08:00`, `12:30`, and `18:00`, choose an IANA
**Capture time zone** (default `Asia/Shanghai`), and save sources. The saved
**Next scheduled capture** is shown in that zone; reload or save to refresh it.
There may be up to 48 distinct times per source.

The running backend queues each due source automatically. Daily times take
priority over **Collection interval / minutes**; removing all daily times restores
the interval policy, or manual-only behavior if the interval is zero. Disabled or
deleted sources do not run automatically. An already active source is skipped
for that slot, with a notification, rather than creating overlapping captures.
Different sources still obey browser/profile and domain concurrency limits.

Schedule state is durable across restarts. An unchanged schedule is not reset by
saving unrelated settings. After downtime, overdue slots coalesce into one capture,
then advance to the next future time; they are not replayed as a backlog. New or
changed schedules start at the next future time. Nonexistent daylight-saving
clocks are skipped and repeated clocks run only at their first occurrence.
This is separate from the TopNews/Weeklyreport generation schedules.

Example source fragment (merge it into the source's other fields):

```json
{
  "schedule": {
    "times": ["08:00", "12:30", "18:00"],
    "timezone": "Asia/Shanghai"
  }
}
```

## Dashboard Pages

| Page | Use |
| --- | --- |
| Intelligence graph (`/graph`) | Select channels, search entities, inspect paths and original evidence. Channels sharing a schema can show the same graph. |
| Sources (`/settings`) | Server/key, sources, collection preferences and optional vision models. |
| Query settings (`/query`) | Default source scope, optional Jev post-filtering and local report/query model. |
| Tasks (`/tasks`) | Profiles, shared sections, task boards, prompts, schedules, output copy and DOCX filename. |
| Outputs (`/reports`) | Generate reports for a chosen window, view recent run logs/files and maintain delivery targets. |
| Notifications (`/notifications`) | Collection events, connection state and pending manual actions. |

The source scope `all` includes authorized contributions from other nodes;
`own` restricts evidence to this node's sources. Shared schema graphs are not
per-channel ingestion counters. Evidence remains subject to server date gates.

## Configure Reports

In **Query settings**, configure the local report model. The default endpoint is
`http://127.0.0.1:9997/v1`, model `xin_local`; provide your own running service or
configure a remote HTTPS model with its own key. The OSBI installer does not
install a model server. This model is independent of server extraction models.
Model failures fall back to deterministic selection/formatting and appear in output metadata.

In **Tasks**, each report profile has:

- Shared **Sections**: titles, one or more channels, query filters, locality and local rules.
- **TopNews** boards: candidate-pool selection, item limits and optional editor prompt overrides.
- **Weeklyreport** boards: action/policy/evidence selection, relationship thresholds and prompts.
- Separate schedules, selected delivery target IDs, enabled and dry-run switches.
- Output copy: TopNews introduction/closing text and Weekly introduction template.

A task board references a shared section and overrides task-specific choices;
it does not rewrite the section. Configure interests using sections and prompts,
not a server routing instruction. Built-in presets contain Chinese localities and
section names; replace those values for your deployment. Switching UI language
keeps those configured values intact.

TopNews defaults to yesterday through today; Weeklyreport defaults to a seven-day
lookback through today (eight calendar dates). Manual runs with a window in
**Outputs** use those explicit dates; **Run manually** in Tasks uses backend defaults.
Schedules require the backend to remain running and use their configured timezone.
An enabled task requires at least one board. Keep **Dry-run** on while reviewing output;
generation still saves files but does not send them.

### Weekly DOCX Filename

In **Tasks → Weeklyreport → DOCX filename**, enter a base name and save the task.
Every generation appends the generation date and extension:

```text
Council weekly → Council weekly_20261125.docx
部门周报        → 部门周报_20261125.docx
```

The format is always `{base}_{YYYYMMDD}.docx`, using the generation day's date,
independently of the evidence window. An optional pasted `.docx` extension is
removed before appending the suffix. Paths, control characters and reserved filename
characters are rejected. Names may be up to 120 characters and 240 UTF-8 bytes.
Existing configurations retain the previous default base name.

The setting is per profile, applies to manual and scheduled runs, and controls the
saved DOCX, recent-file listing, download filename and file delivery. Markdown and
introduction TXT filenames retain their existing names. Generating the same base
on the same date replaces that date's file; use different bases for different profiles.
**Send DOCX file** controls delivery, not generation. DOCX export failure still
leaves Markdown/TXT and a diagnostic available.

### Delivery and Logs

Manage **Delivery targets** in **Outputs**, then select target IDs in the task:

- MimicWX: text delivery; configure URL, group and token or a private config file.
  File delivery currently returns `file_not_supported`.
- Awada: text and DOCX delivery; configure its private config path and channel IDs.
  An optional file channel can differ from the text channel.
- WeCom webhook: reserved; sending returns `not_implemented`.

Saving a target does not send a test message. Actual delivery requires the task's
dry-run switch to be off and the selected target to be enabled. Review the last
10 run records, fallback reasons, paths and delivery results in **Outputs**.
All generated files are under `<client-state>/reports/`.

## Capture and Submission Caches

Successful page acquisitions are shared across local tasks for six hours by
default; per-domain TTLs can override that. A hit retains the original capture
age. Submission caching is separate (seven days by default), and reuses results
only within the applicable server/content/configuration scope. Neither cache is
a server-wide freshness guarantee. TTL `0` disables its cache.

URL fragments in **Excluded URL fragments** use substring matching, with source
start URLs exempt. Refresh a source explicitly when retesting a cached page:

```bash
.venv/bin/osbi-client run source-1 --refresh
```

A source's channel selection is client-owned. v4 links always include
`content`, `unknown` or `navigation` role; the server limits selected navigation
links without changing the body's date gate. See [link selection](../docs/link-selection.md).

## CLI and Agent Hosts

Keep the backend running for these commands:

```bash
.venv/bin/osbi-client status
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

`configure` reads a JSON patch from stdin. Start with current settings and preserve
other profiles when replacing `reportProfiles`; its arrays are not element patches.
[Example configuration](config/example/client.yaml) documents the structure; the
backend is configured through its API/CLI, not by automatically loading that YAML.

DeepSeek Harness has an adapter; the other agent-host directories are placeholders:

```bash
bash scripts/install-client.sh --agent dsh
# With the backend already running, in another terminal:
bash client/scripts/deepseek-harness/run.sh
```

The adapter installs shared management/query skills. DSH itself downloads on its
first explicit launch. Configure its conversational model separately. See
[DSH setup](scripts/deepseek-harness/README.md). All hosts share one client backend
and managed browser per deployment.

## Upgrade, Backup and Troubleshooting

Finish active collection/report work, stop the client backend and owned browser
sessions, then back up the complete client state, including `client.db`, browser
profiles, reports and ledgers. Update your checkout with a reviewed `dev` revision,
rerun the installer if dependencies/browser assets changed, and restart with the
same data directory. Browser assets and model services are independent of UI language.

| Symptom | Check |
| --- | --- |
| No channels | Server URL/key, expiry, enabled net and CA trust. |
| Collected data is absent from graphs | Source has channels; review server date gates and nightly ontology backlog. |
| Waiting for manual action | Open the client browser, complete login/verification, then resume in Notifications. |
| Report is empty or a material overview | Date window, scope/locality/query filters, relation thresholds and available evidence. |
| Model fallback | Local model URL/model/key, service availability and timeout. |
| No outgoing message/file | Task dry-run, enabled target, target IDs and that adapter's file support. |
| Old result on a retest | Refresh acquisition; check the separate submission cache and server state. |
| UI language resets | Browser-local storage availability; the preference is per browser origin. |

Read the [content-storage and responsibility notice](../README.md#content-storage-intellectual-property-and-responsibility).
Protect keys, logged-in browser profiles, original evidence and backups.
