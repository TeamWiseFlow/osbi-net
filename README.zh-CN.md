# OSBI-NET

[English](README.md) | 简体中文

开源商业智能网络

OSBI-Net 将独立部署的采集客户端连接到共享情报服务器。客户端获取信源材料；
server 提取实体与关系、保留原文证据，并按频道提供查询服务。

> 2024年11月，极越闪崩，背后是无数供应商的泪。但如果他们提前三个月发现极越在销量并未大幅增加的情况下突然拉升了三倍下游订单量，那么就可能辨别出风险了。
> 这些信息并非隐秘而不可得，恰恰相反，它们都是“开源”的，只是“提炼”难度极大。这就好比在一片沙滩中找那么几粒金子碎屑……

## 工作方式

```text
Sources → local client (capture, clean, assign channels) → HTTPS → shared server
                                                               relevance selection
Local dashboard / CLI / agent ← shared evidence queries ← ontology queue + graphs
                   └→ local TopNews / Weeklyreport → files and optional delivery
```

| 组件 | 职责 |
| --- | --- |
| 共享 Server | 已有频道陈述、相关性选择、发布日期门禁、持久化本体队列、Schema、图存储与证据查询。 |
| 本地 Client | 网页／RSS／微信公众号采集、手动及定时抓取、界面、本地缓存、报告选材／写作、文件和发送。 |
| Agent 主机 | 可选，通过共享技能／MCP 管理采集并基于证据查询。 |

Client 与 Server 使用独立的源码、环境和状态目录。Client 选择已有 Server 频道，
Server 不根据材料推断或创建频道。多个频道可以共享 Schema／图谱。
同一授权 net 的节点共享证据，本地报告方案和编辑历史独立保存。

现役 Server 是 `server/wiseflow_server`，使用 `material-extract/v4`。
相关性选择同步返回可继续访问的链接；选中的原文 block 进入持久化队列，独立批处理本体提取。
没有独立 info／摘要阶段。Client 不维护持久化图谱副本。

## 启动 Client

在 Client 主机的仓库根目录执行：

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
bash scripts/install-client.sh
bash scripts/run-client.sh
```

打开 **http://127.0.0.1:8781/settings**，填写共享 Server 的 HTTPS 地址和客户／节点 Key，
添加信源并选择提交频道。安装脚本安装 Python 依赖、构建浏览器并校验资源，
复用合格的运行时或在仓库内安装锁定版本。有头浏览器需要图形会话。
已验证 Linux x86_64，原生 Windows／macOS 运行尚未验证。

Dashboard **默认英文**，可在页头切换为 **简体中文**，切换时保留未保存的输入。
TopNews 的 **加油站** 在英文界面中为 **Do you Know**。
语言偏好按浏览器保存，不改写证据原文、用户配置名称和报告内容。

| 页面 | 用途 |
| --- | --- |
| `/settings` 信源配置 | Server 接入、网页／RSS／公众号信源、多个每日抓取时刻、间隔备选、浏览器偏好及视觉模型。 |
| `/graph` 情报图谱 | 搜索实体，查看关系、路径和原文证据。 |
| `/query` 查询设置 | 共享／本节点来源范围、可选 Jev 后置过滤、本地报告／查询模型。 |
| `/tasks` 任务设定 | 方案、共享 Sections、任务板块／Prompt、报告调度、输出文案及 DOCX 文件名。 |
| `/reports` 任务产出 | 手动报告、最近文件／运行日志和发送目标。 |
| `/notifications` 消息通知 | 采集进度及人工待办。 |

每个信源可配置多个每日 `HH:MM` 抓取时间和时区。常驻 backend 自动将到期信源加入队列，
不重叠运行同一信源。每日时间表优先于分钟间隔；没有时间表、且间隔为 `0` 时只手动运行。
信源抓取与报告生成各自独立调度。

TopNews／Weeklyreport 支持手动和定时生成。在“查询设置”配置本地写作模型，
默认为 `http://127.0.0.1:9997/v1` 的 `xin_local`；安装程序不安装模型服务器。
模型失败回退到确定性格式化。Dry-run 仍保存本地文件；实际发送需要配置并启用目标，且关闭 Dry-run。
MimicWX 支持文本，Awada 支持文本／文件，企业微信 hook 尚未实现。

Weeklyreport 的 **DOCX 文件名** 按方案配置，每次固定追加 `_{YYYYMMDD}.docx`，
例如 `部门周报_20261125.docx`，日期为生成当天。名称适用于本地文件、列表、下载和发送；
Markdown／导语 TXT 保持原文件名规则。

完整 Client 手册：**[English](client/README.md)** · **[简体中文](client/README.zh-CN.md)**，
涵盖环境要求、公众号依赖、首次配置、调度、报告、CLI／Agent、缓存、升级与排障。

## 运行共享 Server

在独立 Server 主机使用 Python 3.12+，仅安装 server extra：

```bash
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
export OSBI_CONFIG_FILE=/path/to/private/runtime.yaml
.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
```

参考 [`server/config/example/runtime.yaml`](server/config/example/runtime.yaml) 在仓库外准备配置，
按部署设置模型凭据、存储／Schema／种子路径、频道、鉴权和批处理时间；路径应适配私有配置位置。
远程 Client 通过 HTTPS 反向代理访问，应用默认只监听 loopback。
Server 不需要 Node／浏览器或 Agent 主机。

Supabase 托管鉴权将 Key 关联到 net 和节点，每次鉴权检查吊销、过期和 net 启用状态。
配置和任务监控见[现役 v4 部署](docs/supabase-v4-deployment.md)。
部署专用 Schema、种子、凭据和状态留在公开仓库外，停服后再做一致性备份。
不要使用已退役的 `osbi_server` CLI、8780 说明或旧 info／routing 模型配置。

每个链接必须包含 Client 分类的 `content`／`unknown`／`navigation` role。
Server 优先符合条件的正文入口并限制导航数量，不放宽正文日期门禁。
详见[链接选择](docs/link-selection.md)和[HTTP 协议](docs/client-server-contract.md)。

## CLI 与可选 Agent 集成

Client backend 常驻后：

```bash
.venv/bin/osbi-client status
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

DeepSeek Harness 已有管理／查询适配器：

```bash
bash scripts/install-client.sh --agent dsh
# In another terminal after starting the backend:
bash client/scripts/deepseek-harness/run.sh
```

DSH 主机仅在首次显式启动时下载，其他 Agent 平台适配目录目前为占位。
Dashboard／CLI／MCP 共用常驻 backend 与浏览器。
Agent 凭据、OSBI 客户 Key 和模型服务凭据分别配置。

## 目录结构

```text
server/wiseflow_server/    Active v4 server
server/config/example/    Public bootstrap configuration
server/deploy/            Deployment/monitoring SQL and templates
client/backend/           Python client and managed browser
client/scripts/           Installer, runtime helpers and host adapters
client/skills/            Shared management/query skills
contracts/                Shared transport and utility library
scripts/                  Stable client install/start entry points
tests/                    Offline regression and opt-in integration checks
docs/                     Current usage and protocol documentation
```

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

## 文档

- [Client 部署与使用手册 — English](client/README.md)
- [Client 部署与使用手册 — 简体中文](client/README.zh-CN.md)
- [安装和运行](docs/running.md)
- [v4 鉴权与运维](docs/supabase-v4-deployment.md)
- [Client／Server HTTP 协议](docs/client-server-contract.md)
- [本地报告任务](docs/downstream-tasks.md)
- [链接 role 与选择](docs/link-selection.md)
- [公共协议库](contracts/README.md)
