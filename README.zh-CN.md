# OSBI-NET

[English](README.md) | 简体中文

开源商业智能网络

OSBI-Net 将独立部署的采集客户端连接到共享情报服务器。客户端获取信源材料；
server 提取实体与关系、保留原文证据，并按频道提供查询服务。

> 2024年11月，极越闪崩，背后是无数供应商的泪。但如果他们提前三个月发现极越在销量并未大幅增加的情况下突然拉升了三倍下游订单量，那么就可能辨别出风险了。
> 这些信息并非隐秘而不可得，恰恰相反，它们都是“开源”的，只是“提炼”难度极大。这就好比在一片沙滩中找那么几粒金子碎屑……

## 工作方式

```text
Sources -> local client (browse, clean, chunk) -> HTTPS -> shared server
                                                        | relevance and extraction
                                                        | schemas and graph storage
Local dashboard / agent <- local backend <- HTTPS queries and original evidence
```

| 组件 | 职责 | 持久化数据 |
|---|---|---|
| 共享 server | 相关性判断、自动频道归类、ontology 提取、Schema、图存储与查询 | 原文内容块及来源元数据、图数据、任务、Schema 版本和报告覆盖台账 |
| 本地 client backend | 交互式 Camoufox、可编程信源 workflow、采集间隔、CLI/MCP 和 dashboard | 配置、浏览器 profile、采集材料、提交队列、任务事件及 server 摘要／回执 |
| Agent | 通过 skill 和 MCP 管理采集、基于证据查询 | 由所选 agent 平台管理 |

ontology 提取和图存储都在 server。本地 client 不维护持久化图谱副本，
dashboard 将查询结果加载到页面内存展示。backend 独立常驻，不依赖 agent 会话或
dashboard 页面保持打开。

server 启动即启用 `policy` 和 `economy` 及各自领域 Schema。
提交内容由 server 自动归类，新建频道使用固定的通用 Schema，不自动新增实体类型或
关系谓词。Schema 校准须经过证据审阅及管理员显式激活。
**同一频道的数据在获得授权的使用者之间共享。** 周报覆盖台账则按客户、频道和报告配置独立隔离。

## 部署前须知：原文存储、知识产权与责任声明

> **部署提醒：server 会保存原文内容块，而不只是摘要或提取出的实体。**
> 同频道内获得授权的使用者可以查询原文证据，因此 server 数据目录及备份均可能包含信源原文。
> 相关性判断和提取过程还会将信源内容发送到配置的模型服务端点。

在采集、上传、存储、处理或共享内容前，使用者须了解并遵守所在地及其他适用地区的
知识产权相关法律法规，取得必要的权利人授权，并遵守适用的信源使用条款及许可条件。
本项目公开代码不代表授予使用者任何第三方内容的权利。

**项目声明：我们仅为技术分享与开源社区交流而将代码向开源社区开放，
不参与任何使用者的部署运营、内容经营或其他运营活动。
所有使用者须确保其使用行为不侵犯任何人的知识产权。
使用者须对自身使用及运营行为负责，由此产生的侵权争议、损失、法律责任及其他后果，
由使用者自行承担，具体责任依适用法律确定。**

部署运营者选择 server／模型服务所在地，以及配置访问权限、保留期限、删除和备份策略时，
应充分考虑原文内容的存储与处理。本声明补充现有 [LICENSE](LICENSE)，不替代其条款，
也不排除适用法律规定不得排除的责任。

## 快速开始

以下命令以 Linux Bash 为例，分别在对应主机的仓库根目录执行。需要 Python 3.12+；
客户端浏览器安装需要 Node.js 22.15+，可选的 DSH 启动脚本需要 Node.js 24.9.0+。
已验证环境为 Linux x86_64、Python 3.12.3 和 Node.js 26.8.2；
尚未验证原生 Windows／macOS 安装。

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
python3 -m venv .venv
```

### 1. 部署共享 server

在 server 主机安装服务端依赖，配置兼容 OpenAI 接口的模型服务，
并选择该服务实际支持的模型：

```bash
.venv/bin/pip install -c requirements.lock -e '.[server]'
export OSBI_LLM_BASE_URL=https://your-model-provider.example/v1
export OSBI_LLM_API_KEY=YOUR_PROVIDER_KEY
export OSBI_SELECTED_MODEL=YOUR_SELECTION_MODEL
export OSBI_INFO_MODEL=YOUR_INFO_MODEL
export OSBI_ONTOLOGY_MODEL=YOUR_EXTRACTION_MODEL
export OSBI_FALLBACK_MODEL=YOUR_FALLBACK_MODEL
.venv/bin/osbi-server serve
```

默认监听 `127.0.0.1:8780`。参考 [Nginx 示例](deploy/nginx.conf.example) 配置 HTTPS
反向代理，或使用 `serve --ssl-certfile CERT --ssl-keyfile KEY` 直接提供 TLS。
业务 API 要求 HTTPS，并在 `Authorization: Bearer ...` header 中携带有效客户 key。

在 server 主机的另一个终端中，为客户端客户签发 key：

```bash
.venv/bin/osbi-server issue-key --customer customer-demo --days 30
.venv/bin/osbi-server list-keys
```

签发时仅输出一次明文 key。key 默认可访问所有频道，包括后续新频道；
签发时追加 `--channel policy --channel economy` 可限制权限。
受限 key 不能创建授权范围之外的频道。使用 `renew-key KEY_ID --days 30` 续期，
使用 `revoke-key KEY_ID` 吊销。

server 主机无需安装 DSH 或浏览器资源。常驻服务部署可参考
[环境变量模板](deploy/server.env.example)、[systemd 示例](deploy/osbi-server.service.example)
及[完整运行说明](docs/running.md)。

### 2. 启动本地 client

在每台 client 主机安装 backend 和托管浏览器：

```bash
bash scripts/install-client.sh
.venv/bin/osbi-client serve
```

安装脚本创建项目虚拟环境、按锁定版本安装 Python／Node 依赖、构建运行时，
并下载经过校验的浏览器资源。client 默认不使用 GeoIP 查询；浏览器资源通过安装步骤显式安装。

打开 **http://127.0.0.1:8781/settings**，配置 server 的 HTTPS 地址和客户 key。
添加网页或 RSS 信源，保存后再运行采集。信源可设置监控间隔，也可配置浏览器操作步骤，
通过交互路径抵达真正需要采集的结果页。

- **情报图谱**（`/graph`）：切换频道、搜索实体，并查看关系及原文证据。
- **设置**（`/settings`）：配置服务访问、信源、workflow 和本地采集偏好。
- **消息通知**（`/notifications`）：跟进任务事件，处理待确认的人工登录／验证步骤。

使用 CLI 时保持 backend 运行：

```bash
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

`source-1` 是设置中保存的信源 ID。浏览器默认显示窗口，便于人工交互；
可在设置中开启无头模式，但人工验证仍需合适的图形会话。
参见[信源 workflow](docs/running.md#programmable-source-workflow)。

### 3. 可选：接入 DeepSeek Harness

先保持本地 backend 运行，再安装管理／查询技能，并在另一个终端启动 DSH：

```bash
.venv/bin/python scripts/install-dsh.py
bash scripts/run-dsh.sh
```

启动脚本固定 DSH `0.2.2-rc.3`，通过 MCP overlay 接入而不改写已有平台 profile。
首次运行会下载 DSH 及其依赖树到 npm 缓存；使用 dashboard／CLI 不需要 DSH。
agent 平台的对话模型须单独配置，其凭据、server 的模型服务凭据与 OSBI 客户 key
用途不同。参见 [DSH 配置](docs/running.md#dsh)。

## 部署与维护要点

- **网络**：共享 server 使用有效证书对外提供 HTTPS；dashboard 保持在 client 本机回环地址。
  本地 dashboard HTTP 与远程 server HTTPS 职责不同，不要将本地控制 API 暴露到公网。
- **状态与进程归属**：默认目录为 `~/.osbi/server` 和 `~/.osbi/client`，
  可通过 `OSBI_SERVER_DATA`／`OSBI_CLIENT_DATA` 或全局 `--data` 参数修改。
  每个 server 数据目录由一个 server 进程持有，多 client 通过 HTTPS 接入；
  不要让多个 worker 进程竞争同一个嵌入式图库。
- **凭据与内容**：保护模型 key、客户 key、浏览器 profile、原文和备份。
  使用私有 CA 时配置 `serverCA`，client 会校验证书。开始采集前须落实上文的原文存储提醒。
- **处理时段**：图谱默认持续后台提取。配置以下环境变量并重启 server，可统一在指定时段处理；
  等待提取期间，材料接收与查询仍可使用。

```dotenv
OSBI_GRAPH_PROCESSING_MODE=batch
OSBI_GRAPH_BATCH_WINDOW=22:00-03:00
OSBI_GRAPH_BATCH_TIMEZONE=Asia/Shanghai
```

- **Schema 与规则更新**：使用 `schema-review`、`schema-activate` 和显式 `reprocess`；
  Schema 未变但提取规则更新时，使用 `reprocess --channel policy --force`。
  升级不会自动重建历史数据。参见 [Schema 校准](docs/running.md#manual-schema-calibration)。
- **备份**：停服后创建一致性快照。server 的 `backup --out DIRECTORY` 与
  `restore --from DIRECTORY` 命令校验文件哈希；恢复到全新的数据目录。
  client 也应保全完整状态目录。参见[备份说明](docs/running.md#verification-and-state-backup)。
- **周报准备**：可通过 CLI／MCP／API 按需选取行动热点，使用独立的跨周覆盖台账，
  并从 Word 标题回填历史记录。报告成功后再记录覆盖情况。
  尚未开启调度、自动报告生成或对外推送。参见[周报选材](docs/weekly-selection.md)。

## 目录结构

```
osbi-net/
├── server/                # Extraction, schemas, graph ownership and shared APIs
├── client/                # Local backend, browser, dashboard and agent skills
├── contracts/             # Shared transport models
├── deploy/                # Environment, HTTPS proxy and service examples
├── scripts/               # Installation, DSH launcher and history import
├── tests/                 # Isolated regression and opt-in acceptance checks
└── docs/                  # Architecture, operations and verification
```

## 开发状态

当前为首版 demo 实现，本地回归结果及其边界见[验证记录](docs/verification.md)；
真实模型效果及完整部署验收仍需在目标环境中验证。

首个 agent 集成面向 DeepSeek Harness；Claude Code、Codex、WorkBuddy 和 Pi
仍为占位。hunter、定时报告生成与推送、智能整理、OCR／转写和复杂文档解析仍后置。
可选 Jev 尚未启用，也不是运行前提。

## 文档

- [安装与运行](docs/running.md)
- [验证记录与待验收事项](docs/verification.md)
- [按需周报选材与跨周去重台账](docs/weekly-selection.md)
- [架构提案](docs/architecture.md)
- [客户 demo 交付计划](docs/development-plan.md)
- [本地 dashboard 规格](docs/dashboard-demo.md)
- [技术基线与适配分析](docs/reference-baseline.md)

## 许可证

使用须遵守现有 [LICENSE](LICENSE)，包括其中的附加条件和禁止用途。
随附依赖与资源的许可见[第三方声明](THIRD_PARTY_NOTICES.md)。
代码许可证不授予部署中采集到的信源内容的使用权。

## 引用

如果您在相关工作中参考或引用了本项目的部分或全部，请注明如下信息：

```
Author: Wiseflow Team
https://github.com/TeamWiseFlow/osbi-net
```

## 友情链接

[<img src="https://github.com/TeamWiseFlow/wiseflow/raw/4.x/docs/logos/SiliconFlow.png" alt="siliconflow" width="360">](https://cloud.siliconflow.cn/i/WNLYbBpi)
