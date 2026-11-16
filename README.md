# OSBI-NET

English | [Simplified Chinese](README.zh-CN.md)

Open-Source Business Intelligence Network

> In November 2024, Jiyue's sudden collapse left countless suppliers facing losses. Had they noticed three months earlier that Jiyue had abruptly tripled its downstream order volume without a substantial increase in sales, they might have recognized the risk.
> This information is neither secret nor inaccessible. On the contrary, it is publicly available; extracting useful signals from it is the difficult part, like finding a few flecks of gold on a vast beach.

## Directory Structure

```
osbi-net/
├── server/                # Information-sharing server, including signal extraction algorithms
├── client/
└── docs/                  # Project documentation
```

## Development Status

The project is preparing a customer demo for the week of November 16, 2026.
The first implementation includes a shared HTTPS server,
automatic channel assignment/creation starting with `policy` and `economy`,
a persistent acquisition client, and a local graph/settings/notifications dashboard.
Graph extraction runs continuously by default, with an environment-configured
batch-window option.

The first server deployment includes both channels with their existing domain
schemas. Newly created channels use a simple predefined generic schema. Entity
types and relation predicates are never added automatically; schemas are calibrated
through periodic evidence review and explicitly published revisions.

The first agent integration targets DeepSeek Harness; Claude Code, Codex,
WorkBuddy, and Pi are reserved for later development. Hunters, scheduled reports,
and intelligent curation CLI are deferred beyond this demo.

- [Installation and operation](docs/running.md)
- [Verification evidence and remaining acceptance](docs/verification.md)
- [On-demand weekly selection and coverage history](docs/weekly-selection.md)
- [Architecture proposal](docs/architecture.md)
- [Customer demo delivery plan](docs/development-plan.md)
- [Local dashboard specification](docs/dashboard-demo.md)
- [Technical baseline and adaptation findings](docs/reference-baseline.md)

## Citation

If you reference or use any part of this project in your work, please include the following attribution:

```
Author: Wiseflow Team
https://github.com/TeamWiseFlow/osbi-net
```

## Related Links

[<img src="https://github.com/TeamWiseFlow/wiseflow/raw/4.x/docs/logos/SiliconFlow.png" alt="siliconflow" width="360">](https://cloud.siliconflow.cn/i/WNLYbBpi)
