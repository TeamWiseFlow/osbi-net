# OSBI-NET

[English](README.md) | 简体中文

开源商业智能网络

> 2024年11月，极越闪崩，背后是无数供应商的泪。但如果他们提前三个月发现极越在销量并未大幅增加的情况下突然拉升了三倍下游订单量，那么就可能辨别出风险了。
> 这些信息并非隐秘而不可得，恰恰相反，它们都是“开源”的，只是“提炼”难度极大。这就好比在一片沙滩中找那么几粒金子碎屑……

## 目录结构

```
osbi-net/
├── server/                # Information-sharing server, including signal extraction algorithms
├── client/
└── docs/                  # Project documentation
```

## 开发状态

项目已开始实施面向 2026 年 11 月 16 日当周客户 demo 的首版功能。
demo 包含共享 HTTPS server、从 `policy` 和 `economy` 起步的自动频道归类与创建、
常驻采集客户端，以及本地图谱、设置、消息通知 dashboard。
图谱提取默认持续后台运行，并支持通过环境变量配置统一批处理时段。

server 首次上线即启用这两个频道及各自现有的领域 schema。
新建频道使用简单、预定义的通用 schema，不自动新增实体类型或关系谓词；
schema 通过定期审阅实际数据、校准并显式发布新版本来演进。

首个 agent 集成面向 DeepSeek Harness；Claude Code、Codex、WorkBuddy 和 Pi
预留为后续开发平台。hunter、调度报告和智能整理 CLI 延后至 demo 之后。

- [安装与运行](docs/running.md)
- [验证记录与待验收事项](docs/verification.md)
- [按需周报选材与跨周去重台账](docs/weekly-selection.md)
- [架构提案](docs/architecture.md)
- [客户 demo 交付计划](docs/development-plan.md)
- [本地 dashboard 规格](docs/dashboard-demo.md)
- [技术基线与适配分析](docs/reference-baseline.md)

## 引用

如果您在相关工作中参考或引用了本项目的部分或全部，请注明如下信息：

```
Author: Wiseflow Team
https://github.com/TeamWiseFlow/osbi-net
```

## 友情链接

[<img src="https://github.com/TeamWiseFlow/wiseflow/raw/4.x/docs/logos/SiliconFlow.png" alt="siliconflow" width="360">](https://cloud.siliconflow.cn/i/WNLYbBpi)
