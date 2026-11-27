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
bash client/scripts/install.sh
bash client/scripts/run.sh
```

The installer installs `.[client]`, builds `client/backend/browser`, and verifies
pinned browser, fingerprint, addon and GeoIP assets. It reuses suitable Python
3.12+ and Node 24.9+ runtimes or provisions pinned runtimes under `.osbi/runtime`.
Python packages live in `.venv`; system runtimes and shell profiles are unchanged.
Server graph-store dependencies and an agent host are not required.

The backend listens at **http://127.0.0.1:8781**. Keep it running independently of
dashboard tabs and agent sessions. To select another data directory or port:

```bash
bash client/scripts/run.sh --data "$HOME/.osbi/client-demo" serve --port 8782
```

To keep the backend resident, install the optional systemd `--user` unit:

```bash
bash client/scripts/install.sh --systemd
systemctl --user status osbi-client.service
```

The generated unit runs `ExecStart=/bin/bash <checkout>/client/scripts/run.sh serve`.
Always point an existing unit at `client/scripts/run.sh` instead of any wrapper
script, and restart the service after upgrading the checkout so that changed code
takes effect. The unit starts no display; add `Environment=DISPLAY=:0` to its
`[Service]` section when a visible browser is required.

MP source capture runs WeChat container commands, so the unit also pins the
container name it must use: `--systemd` auto-detects a running
`wechat-linux-container` or a container whose name starts with `mimicwx`, and
writes `Environment=WECHAT_LINUX_CONTAINER_CONTAINER=<name>`. Pass
`--wechat-container NAME` to choose another container, or `--wechat-container
none` to omit the override and fall back to the built-in default.

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
URL exclusion fragments, timeouts, concurrent submissions and cache TTLs.

With no daily capture times, a collection interval of `0` means manual runs only. Nonzero intervals are handled
by the running backend. Browser profiles retain session state; login or verification may need a visible session.
Sources can use typed workflows for navigate/click/fill/select/wait/scroll/switch/
capture/manual-wait steps. Use the management skill for complex workflows.

For WeChat public-account sources, enter exact account display names, one per
line; their fixed entry is `https://mp.weixin.qq.com/`. The catalog hunter needs a
separately provisioned, logged-in wechat-linux-container installation and its database/key access.
`WECHAT_LINUX_CONTAINER_CONTAINER`, `WECHAT_LINUX_CONTAINER_WX_USER_DIR` and `WECHAT_LINUX_CONTAINER_KEYS_FILE` select that
installation. The installer does not provide it. The hunter discovers article
URLs and the browser captures article bodies; programmable steps are not used
for this source type.

### Parallel Pages and Manual Verification

Sources sharing a browser profile use one browser instance and separate task pages.
New profile rounds start headless. Pages can run concurrently within one source or
across sources. Total page capacity is automatically `max(6, configured profile
count)`: up to six profiles use capacity 6; eight profiles use capacity 8. Count
distinct `sources[].profile` IDs in the local configuration, including profiles
whose sources are currently idle. Several sources sharing an ID count once.
`browserConcurrency` reports this derived value; old saved values do not override
it. The dashboard displays capacity as read-only. Domain limits apply across all
profiles and may reduce actual concurrency; assigning more profiles does not bypass
a domain limit. Reuse a profile for sources that should share login/verification
state. Separate profiles have separate browser identities and cookies.

When a page needs verification, the client pauses new work for its profile,
closes that instance, and reopens the same profile visibly with one active page.
Unfinished URL captures return to the queue; completed material stays saved.
Confirm **Done, continue** after verifying. The client captures the current page
before restarting headless parallel work. Another verification within 10 minutes
keeps this profile visible and serial for the rest of the current round. Other
profiles continue normally. A new round resets to headless after the profile's
previous sources have finished; starting a source never resets another source's
active verification. Visible mode needs a graphical session (or a virtual desktop).

Only login/verification gates and explicit manual workflow steps request human
action. Complex tables or unresolved metadata that the extractor cannot reliably
parse fail that page with `CAPTURE_UNSUPPORTED`; other pages continue. Capture
failure notifications show the full page URL as a link that opens in a new tab,
including saved notifications. Truncated captures get the configured headless
grace period, then fail with `CAPTURE_PARTIAL` if still incomplete. Neither
extraction error switches the profile to headed mode or creates a manual prompt.
Existing technical prompts can auto-resume once no verification gate remains,
then record the extraction failure.

Mode changes reuse the profile directory, frozen fingerprint and session cookies.
They recreate tabs, so temporary page state is not guaranteed to survive. An
interaction whose outcome is unknown requires explicit confirmation before it is
continued; it is never automatically replayed. On the first upgrade to page-based
acquisition, active legacy runs are interrupted: cancel them and start the source
again. Saved materials, completed submissions and profile files remain available.

Each source execution has a fixed **3-hour** deadline from creation, including
queueing, retries, manual waits and waiting for submission. Each manual prompt
waits at most **30 minutes**, independently of page-cache TTL. Heartbeats, resume
and backend restarts do not extend either deadline. Expiry cancels the source's
pending/in-flight local work and releases its pages without closing a browser
still used by another source. Already accepted server material is retained.

Submission runs independently of page capture: `submissionConcurrency` is 1–32
(default 4). `maxPendingSubmissions` is 1–2000 (default 100); reaching it pauses new
page admission while existing page workers finish. Known URLs can continue while
older material awaits selection; links discovered by selection wait for that
result. Both queues are durable. These controls are available in **Sources →
Collection preferences**. The old `headless` configuration field is accepted for
upgrades but normalized to automatic mode management.

In **Sources → Shared server**, **Selection read timeout / seconds** controls
only the synchronous material-extract response wait (`selectionReadTimeoutSeconds`,
10–600, default 300). Existing configurations receive the new default automatically;
the general `timeoutSeconds` setting remains unchanged. Shared-server connection
setup waits at most 10 seconds. Read timeouts, connection/TLS failures, and Key
authentication/permission failures have separate messages and event codes. A read
timeout can leave the original request running on the server; a later retry may
reuse its cached selection. Longer client waiting does not eliminate upstream
model deadlines, queue waits, or the source run's 3-hour deadline.


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

- wechat-linux-container: text delivery; configure URL, group and token or a private config file.
  File delivery currently returns `file_not_supported`.
- Awada: text and DOCX delivery; configure its private config path and channel IDs.
  An optional file channel can differ from the text channel.
- WeCom webhook: reserved; sending returns `not_implemented`.

The WeChat target's `type` is `wechat-linux-container`. Optional private JSON
uses a `wechat-linux-container` section containing `base_url`, `token` and
`window`. Environment defaults are `WECHAT_LINUX_CONTAINER_BASE_URL`,
`WECHAT_LINUX_CONTAINER_TOKEN` and `WECHAT_LINUX_CONTAINER_WINDOW`; explicit target
values take precedence. Existing installations must migrate saved target types,
provider JSON sections and environment variable names before starting this
version. Keep profile/target IDs and secrets intact. The MP hunter's default
Docker name is also `wechat-linux-container`; use
`WECHAT_LINUX_CONTAINER_CONTAINER` to select a differently named container.

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
bash client/scripts/install.sh --agent dsh
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
rerun the installer when dependencies, browser runtime code or assets changed,
and restart with the same data directory. Browser assets and model services are
independent of UI language.

After syncing code, an existing systemd user service can be updated from the
repository root with:

```bash
systemctl --user stop osbi-client.service
bash client/scripts/install.sh
systemctl --user restart osbi-client.service
systemctl --user status osbi-client.service --no-pager
```

The installer rebuilds the browser runtime. Running it without `--systemd`
preserves the existing unit's display, container and data-directory settings.
Use `--agent dsh` on the installation command to refresh the installed DSH skills
as well (retain your existing `--dsh-home` when using a custom location). For a
manually launched backend, stop its foreground process and start it again with
`bash client/scripts/run.sh serve` after installation.

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
