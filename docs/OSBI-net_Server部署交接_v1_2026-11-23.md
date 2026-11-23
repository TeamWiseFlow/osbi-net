# OSBI-net 共享 Server 部署交接 v1（2026-11-23）

交接对象：负责服务器部署的 Agent / 运维同学
配套文档：`docs/OSBI-net开发文档_v1_2026-11-23.md`（实现以开发文档为准）

---

## 0. 交接目标与依赖顺序

目标：把 OSBI-net 的**唯一共享 server** 部署到一台远端服务器，供两个 node 使用：

1. **临汾路街道 node**：本机 `~/midplatform` 的生产调度将切换为 `~/osbi-net/client`；
2. **成都狮子山街道 node**：新客户，另一台机器。

本轮为**初始版本**，**零历史**：不导入旧图、旧周报历史、旧惩罚账本。

依赖顺序（不要跳步）：

1. 开发 Agent 按开发文档把 `~/osbi-net` 代码改到 `material-extract/v4` + channel 配置；
2. 本 Agent 部署 server，完成 smoke test；
3. 客户端 Agent 把临汾 node 切到远端 server；
4. 双跑验收；
5. 停旧生产进程，正式切影子。

在代码完成前，当前仓库不能直接作为最终 server 部署（协议仍是 v3、仍带 focus）。

---

## 1. 目标拓扑

```
                     HTTPS / Bearer node key
临汾 node（本机） ───────────────┐
                                  ▼
                         OSBI-net Server（远端，单实例）
                          - 7 个共享 channel
                          - policy 图 + economy 图
                          - 相关性/本体提取/evidence 查询
                          - node 源与质量指标
                                  ▲
成都 node（客户机器） ────────────┘
```

- server 只有一个；所有 channel 对所有 node 开放，没有订阅/按 channel 授权。
- node 只决定“自己的信源提交到哪些 channel”；查询时显式指定 channel。
- node 身份只用于 provenance、`sourceScope=own`、质量指标。
- 同一内容同 channel 多 node 提交 → 一个处理 job + 多 node provenance（互补抓取）。

---

## 2. 当前本机生产环境（部署 Agent 需要知道的背景）

生产参考系统在 `~/midplatform`（分支 `2027`），**本轮不要修改、不要停止**，直到切流。

### 2.1 进程与端口（`run_all.sh`）

| 进程 | 端口 | 作用 |
|---|---|---|
| `wiseflow_server` | 127.0.0.1:8078 | 采集/相关性/本体提取内部服务 |
| `wiseflow_backend` | 127.0.0.1:8077 | 任务/focus/info 业务 API |
| `awada_dm` | — | 消息 DialogManager |
| `schedule_tasks` | — | 定时：抓取、topnews、weekly、ontology batch、影子任务 |
| `mimicwx_query_bot` | — | 影子问答 bot |

`run_all.sh` 还负责单例锁 `/tmp/midplatform_run_all.lock`。

### 2.2 定时任务（生产 + 影子）

| 时间 | 任务 |
|---|---|
| 12:18 | wiseflow 抓取轮 1（公众号 task1~4） |
| 23:18 | wiseflow 抓取轮 2（公众号 + 政策 task1~5） |
| 01:18 | ontology 批处理触发（窗口至 06:18） |
| 06:40 | 查询快照刷新 |
| 08:08 | 生产 topnews |
| 08:40 | 影子 topnews（图版） |
| 周一 07:18 | 生产 weekly report |
| 周一 07:50 | 影子 weekly report（图版） |

新 server 的批处理窗口建议：`01:18–06:18`，`Asia/Shanghai`。

### 2.3 数据与配置路径

| 内容 | 路径 |
|---|---|
| wiseflow 工作库 | `/home/dsw/midplatform/projects_data/wiseflow_dir/` |
| SQLite | `.../data.db`（tasks/focuses/infos） |
| 本体图 | `.../ontology/`（policy）、`.../ontology_libraries/economy/` |
| 舞台配置 | `.../custom_config.json`（TIME_SLOTS 12:18/23:18、禁爬、并发） |
| 分库拓扑 | `.../ontology_libraries.json`（policy/economy，economy focusIds [2,9]） |
| 报告侧信源映射 | `/home/dsw/midplatform/projects_data/focus_poin.json` |
| 备份 | `/home/dsw/midplatform/projects_data/backups/` |
| OSBI client 状态 | `/home/dsw/.osbi/client/client.db` |
| OSBI client 报告产物 | `/home/dsw/.osbi/client/reports/` |
| OSBI 旧 server-data | `/home/dsw/.osbi/server-data/`（**不要复用**，新 server 用新目录） |

### 2.4 生产 focus/板块（只作对照）

生产 `tasks` 表当前 5 条，每条 1 个 focus、1 批信源：

| task | focus id | focuspoint 摘要 | 信源 |
|---|---|---|---|
| #基层党建 | 10 | 基层党建/两企三新/新兴就业群体/… | 13 个公众号 |
| #经济动态 | 9 | 经济形势/稳增长/就业/货币/新质生产力… | 22 个公众号 |
| #社区治理 | 8 | 社区治理/先进经验案例人物事迹 | 33 个公众号 |
| #数字社区 | 12 | 公共数据/数据流通/AI政务/一网统管… | 11 个公众号 |
| #政策速递 | 11 | 最近三天内发布的政策/公告/公示 | 22 个政策网站 |

精确列表直接从 `projects_data/wiseflow_dir/data.db` 的 `tasks.sources` 与 `focus_poin.json` 读取。

### 2.5 模型与密钥

`~/midplatform/wiseflow/model_config.py` 当前默认：

| 用途 | 模型 |
|---|---|
| SELECTED（相关性） | `deepseek-ai/DeepSeek-V3.2` |
| PERFORMANCE（旧 info） | `Qwen/Qwen3-32B` |
| ONTOLOGY（本体提取） | `Qwen/Qwen3.5-27B` |
| FALLBACK | `deepseek-ai/DeepSeek-V3.2` |
| 视觉（client 侧） | `Qwen/Qwen3-VL-32B-Instruct` 等 |

- Provider：SiliconFlow `https://api.siliconflow.cn/v1`。
- 并发 3，超时 240s，ontology chunk 3000 / overlap 200。
- `SF_KEY` 目前写在 `run_all.sh` 中；**不要复制密钥到本文档/仓库**，部署时从运维密钥库获取，写入 server 的私有 env 文件。
- 客户端报告模型：本地 xinference `http://127.0.0.1:9997/v1`，`xin_local`；这是 node 本地配置，不在 server 上。

### 2.6 当前 OSBI client 配置（将切换）

- `~/.osbi/client/client.db`，serverUrl 当前 `http://127.0.0.1:8078`（本地旧 server）。
- sources 已按生产 5 组配置了 per-source focusId（10/9/8/12/11），本轮会迁移为 `channels`。
- `reportPush.enabled=false`；Awada 频道 `10854594337574110` 已配置（「临小助升级测试」群）。
- `reportLlm` 本地 `xin_local`。
- MimicWX 测试群名：`政务osbi-net测试体验群`（生产推送目标）。

---

## 3. 仓库真实状态与部署坑

`~/osbi-net` 分支 `dev`，当前干净；但处于“wiseflow 引擎替换旧 osbi_server”的过渡期：

- **实际 server 模块**：`server/wiseflow_server/` + `server/core/`；
- **实际启动**：`PYTHONPATH=server .venv/bin/python -m wiseflow_server --host 0.0.0.0 --port 8078`；
- `server/README.md`、`docs/running.md`、`server/deploy/*.example` 描述的是已废弃的 `osbi_server`（8780、Supabase、动态 channel），**不要照搬**；
- `pyproject.toml` 的 `osbi-server = "osbi_server.cli:main"` 是坏的；不要用 `osbi-server serve`；
- `.venv` 当前可 import `server`（需 `PYTHONPATH=server`），server 依赖 `fastapi/uvicorn/pyoxigraph/rdflib` 已安装；
- 当前协议 `material-extract/v3` 仍含 focus；部署必须等开发 Agent 完成 v4（无 focus、channel 必填）。

部署时以开发文档 + 实际代码为准；如 README 与代码冲突，以代码/开发文档为准。

---

## 4. 初始部署配置（第一版直接使用）

### 4.1 全局 channel（7 个，全部开放）

| id | title | schema | 日期窗口 |
|---|---|---|---|
| party-building | 基层党建 | policy | 无 |
| economy | 经济动态 | economy | 无 |
| community | 社区治理 | policy | 无 |
| digital | 数字社区 | policy | 无 |
| policy-latest | 政策速递 | policy | **3 天（发布日期）** |
| stability | 平安维稳 | policy | 无 |
| production-safety | 安全生产 | policy | 无 |

**stability 与 production-safety 是两个独立 channel，不能合并。**

初始 statement 见开发文档 4.1；部署时原样写入 `runtime.yaml`。要点：
- 全部是**跨地市、话题级宽口径**，不写上海/成都；
- 具体属地/子主题由 node 的 report board query/local rules 收窄；
- `policy-latest` 的 statement 写“最近三天内发布…”，同时 `dateWindowDays: 3` 由 server 确定性执行；过滤字段是 `publishedDate`，缺失/无法从正文判断则拒绝。

### 4.2 存储

- policy 家族（party/community/digital/policy-latest/stability/production-safety）**共享一个 policy 图存储**；
- economy 独立存储；
- channel 是 provenance/查询过滤键，不是独立库；
- node_id 只做溯源，不做数据隔离。

### 4.3 node

```yaml
nodes:
  - id: linfen-shanghai
    name: 临汾路街道
  - id: shizishan-chengdu
    name: 成都狮子山街道
```

- 每个 node 发一个 Bearer key；key→node_id 映射存 `nodes.keys.json`（哈希、权限 600、仓外）。
- 全部 channel 开放；不做 channel 授权。
- 迁移时先给两个 node 建 key，客户端 Agent 负责填入各自 client 配置。

### 4.4 模型

server env / 配置：

```text
OSBI_LLM_BASE_URL=https://api.siliconflow.cn/v1
OSBI_LLM_API_KEY=<从运维密钥库获取，不写仓库>
OSBI_SELECTED_MODEL=deepseek-ai/DeepSeek-V3.2
OSBI_ONTOLOGY_MODEL=Qwen/Qwen3.5-27B
OSBI_FALLBACK_MODEL=deepseek-ai/DeepSeek-V3.2
OSBI_MODEL_CONCURRENCY=3
OSBI_MODEL_TIMEOUT=240
ONTOLOGY_CHUNK_CHARS=3000
ONTOLOGY_CHUNK_OVERLAP=200
```

当前代码仍读取 `SELECTED_MODEL`/`ONTOLOGY_MODEL`/`FALLBACK_MODEL`、`WISEFLOW_LLM_API_BASE`、`WISEFLOW_LLM_API_KEY` 或 `SF_KEY`、`WISEFLOW_WORK_DIR` 等旧名；开发 Agent 会改为 `runtime.yaml` + `OSBI_*`。部署时以最终代码的 `runtime.yaml` 为准，但**模型值必须与生产影子一致**，否则只能功能等价、不能内容 parity。

### 4.5 数据目录

建议：

```text
/var/lib/osbi-server/
  data.db
  wis_cache/
  policy/                  # policy Oxigraph
  economy/                 # economy Oxigraph
```

- `WISEFLOW_WORK_DIR` / `OSBI_SERVER_DATA` 指向该目录；
- `ONTOLOGY_LIBRARIES_CONFIG` 指向仓外 config；
- 目录属主 `osbi`，权限 700/600；
- **不要复用** `~/.osbi/server-data` 或 `~/midplatform/projects_data/wiseflow_dir`（零历史）。

### 4.6 schema 与 seeds

- schema：`policy.ttl`（v3.2/v12 冻结口径，56 类）与 `economy.ttl`（v1.5）；从仓库 example 复制到仓外 config，**不要为每个 channel fork schema**。
- seeds：
  - `seeds_shanghai.ttl`（已有）；
  - **需新增 `seeds_chengdu.ttl`**：四川省 → 成都市 → 锦江区 → 狮子山街道（及同级区/街道）；URI 命名与现有 `uri_for` 一致；写入 policy 库的独立 seed 图。
- schema/seeds 变更会进入 fingerprint；变更后需重启并评估 reprocess。

### 4.7 批处理

```yaml
batch:
  enabled: true
  windowStart: "01:18"
  windowEnd: "06:18"
  timezone: Asia/Shanghai
```

- 新 server 自己开批处理；不再由本地 `schedule_tasks` 通过 HTTP 触发。
- 超限 FIFO 顺延次日；失败重试。

---

## 5. 两个 node 的初始业务配置

### 5.1 临汾 node

- channel：`party-building` / `economy` / `community` / `digital` / `policy-latest`
- board：5 个 section × 2 个任务 = 10 个运行实例（topnews 5 + weeklyreport 5；生产影子默认也是这 5 个板块）
- selector：topnews 全部 `candidates`；weekly 党建/社区/数字=`action`，经济/政策=`policy`
- 属地偏好：`["上海市","静安区","临汾路街道"]`，`territoryMode=prefer`
- 下游任务：topnews 每日 08:40、weekly 周一 07:50（Asia/Shanghai）；切流前 dry-run
- 发送目标：MimicWX 测试群（`政务osbi-net测试体验群`）+ Awada 群 `10854594337574110`；企微 hook 预留未启用
- 信源映射：
  - 13 党建公众号 → party-building
  - 22 经济公众号 → economy
  - 33 社区治理公众号 → community
  - 11 数字社区公众号 → digital
  - 22 政策网站 → policy-latest
- 目标：稳态后与当前影子 topnews/weekly 对齐；首周有零历史预热差异。

### 5.2 成都狮子山 node

- channel：`party-building` / `digital` / `stability` / `community` / `production-safety` / `policy-latest`
- board：7 个 section × 2 个任务 = 14 个运行实例（topnews 7 + weeklyreport 7）
- selector：topnews 全部 `candidates`；weekly 顶层主线=`policy`，其余=`action`
- board 列表：
  1. 党建引领基层治理 → party-building
  2. 智慧数字治理 → digital
  3. 平安维稳风险化解 → stability
  4. 民生服务 → community
  5. 文明城市与环境 → community
  6. 安全生产底线 → production-safety
  7. 顶层主线 → policy-latest
- 属地偏好：`["四川省","成都市","锦江区","狮子山街道"]`，`territoryMode=prefer`
- 下游任务：topnews 每日 08:40、weekly 周一 07:50（Asia/Shanghai）；切流前 dry-run
- 发送目标由成都 node 本地配置（MimicWX/Awada；企微 hook 预留）；server 不参与推送
- 客户 7 大主题（原文）：
  1. 党建引领基层治理：新时代枫桥经验、街乡吹哨部门报到、网格精细化治理、全过程人民民主、两新党建、新就业群体服务、基层减负
  2. 智慧数字治理：一网统管、城运体系、数字吹哨、接诉即办/12345 提质、AI 赋能政务、智慧社区场景、数据赋能减负增效
  3. 平安维稳风险化解：矛盾前端化解、出租房/群租房安全、流动人口管理、重点人群关怀干预、消防燃气安全、隐患排查、舆情稳控
  4. 民生服务：一老一小、居家养老、助老食堂、普惠托育、困难群众兜底、老旧小区改造、加装电梯、物业三方协同、就业帮扶
  5. 文明城市与环境：文明城市巩固、新时代文明实践、志愿服务、移风易俗、垃圾分类、市容提质、爱国卫生
  6. 安全生产底线：安全生产、防灾减灾、食品安全、节假日安保、重大活动保障
  7. 顶层主线：高质量发展、共同富裕、基层政权建设、全会精神、五年规划相关部署
- 信源：
  - 国家：中国政府网、新华网、人民网、共产党员网(12371)、中央社会工作部官网、《求是》、人民日报、学习时报
  - 部委：民政部、中央政法委、应急管理部、住建部、消防、市监局 官方公众号
  - 地方：四川省政府网、成都市政府官网、锦江区政府官网、四川发布、锦江发布
  - 成都所属街道公众号（几十个）
- 初版映射建议见开发文档 6.4；综合门户限制 1–2 个主 channel，控制抽取成本。
- **部署前需客户补齐**：消防/市监局/各街道公众号的**精确显示名**；成都领导/属地口径（是否启用 leader boost）。

---

## 6. 部署步骤

以下命令以 `/opt/osbi-net` 为例；实际路径按运维规范调整。

### 6.1 安装

```bash
cd /opt/osbi-net
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
```

- 不需要 Node/browser 资产（那是 client 侧）。
- 当前 `osbi-server` entry 不可用；启动方式：
  ```bash
  PYTHONPATH=/opt/osbi-net/server \
  /opt/osbi-net/.venv/bin/python -m wiseflow_server --host 0.0.0.0 --port 8078
  ```
  或：
  ```bash
  PYTHONPATH=/opt/osbi-net/server \
  /opt/osbi-net/.venv/bin/uvicorn wiseflow_server.app:app --host 0.0.0.0 --port 8078
  ```

### 6.2 配置（仓外）

```text
/etc/osbi-net/
  runtime.yaml          # 7 channels、stores、models、batch、seeds
  nodes.keys.json       # key→node_id 哈希映射，600
  osbi-server.env       # 模型密钥等 secret，600
  schemas/policy.ttl
  schemas/economy.ttl
  seeds/seeds_shanghai.ttl
  seeds/seeds_chengdu.ttl
```

- `runtime.yaml` 以 `server/config/example/runtime.yaml` 为模板；
- 密钥不写 YAML/仓库；用 env file 或 secret manager；
- 配置变更后重启；statement/schema/rules 变更会改变 fingerprint。

### 6.3 生成 node key

按实现后的 CLI（如 `python -m wiseflow_server keys issue --node linfen-shanghai`）生成两个 key：
- 输出一次性明文，交给对应客户端 Agent；
- 只保存哈希到 `nodes.keys.json`；
- 全部 channel 开放，不设 channel 授权。

如果实现尚未提供 CLI，可先用环境变量/临时映射，但必须在交接记录中登记 key id 与 node_id。

### 6.4 启动与 TLS

- server 只监听内网/回环，前置 Nginx HTTPS；不要把 8078 直接暴露公网。
- 示例 systemd（按实际路径修改）：
  ```ini
  [Service]
  WorkingDirectory=/opt/osbi-net
  EnvironmentFile=/etc/osbi-net/osbi-server.env
  Environment=OSBI_CONFIG_FILE=/etc/osbi-net/runtime.yaml
  Environment=PYTHONPATH=/opt/osbi-net/server
  ExecStart=/opt/osbi-net/.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
  Restart=on-failure
  User=osbi
  ```
- Nginx：只信任 127.0.0.1 upstream，启用 TLS、合理请求体大小（material 总文本 ≤240k）。

### 6.5 首次启动检查

```bash
curl -s http://127.0.0.1:8078/health
```

确认：
- protocolVersion 为 v4；
- 7 个 channel 加载成功；
- schema/seeds 路径可读；
- 模型配置字段与生产一致；
- data 目录可写、权限正确。

---

## 7. Smoke test（部署完成必须逐项做）

### 7.1 基本提交

用临汾 key 提交一条 `community` 材料（含 blocks + 一条 link）：
- 返回 200/202，`selection.blockIds/linkIds` 合法；
- `ontologyJobs` 有 pending；
- provenance 含 `channel=community`、`nodeId=linfen-shanghai`、`publishedDate`。

用成都 key 提交一条 `stability` + 一条 `production-safety`：
- 两个 channel 分别处理，不串；
- 同一内容同 channel 用另一个 node key 再提交一次 → 只产生一个 job，多 node provenance（需实现支持）。

### 7.2 日期窗口（policy-latest）

| 用例 | 期望 |
|---|---|
| meta.publishedAt = 今天 | 入图 |
| meta.publishedAt = 今天-5 天 | 拒绝（date_window） |
| meta 无日期，正文有“发布日期：今天-1 天” | 入图 |
| meta 无日期，正文只有“2021 年印发” | 拒绝 |
| meta 无日期，正文“发布于3天前” | 拒绝（date_window） |
| meta 无日期，正文“2天前发布” | 入图 |
| meta 无日期，正文“24小时前发布” | 换算日期后入图 |
| meta 无日期，正文“72小时前发布” | 拒绝 |
| meta 无日期，正文“昨天/前天发布” | 入图；“大前天”拒绝 |
| meta 无日期，正文“一周前/上个月/去年发布” | 拒绝 |
| meta 无日期，正文“近日/日前/几天前/2天以前” | 拒绝（date_unknown/ambiguous） |
| meta 无日期，正文“刚刚/几小时前发布” | 入图，标 date_estimated |
| 正文只有相对表达且无 capturedAt | 拒绝（date_unknown） |
| meta/标题/正文都无日期 | 拒绝（date_unknown） |
| 正文两个冲突日期 | 拒绝（date_ambiguous） |
| 事件日期在窗口内、发布日期超窗 | 拒绝 |

### 7.3 Evidence 查询

- 必须显式 channel；
- `sourceScope=all` 返回两个 node 的内容；`own` 只返回该 key 的 node；
- `territoryPreference` 只影响排序/偏好，不错误排除其他地区；`only` 才硬过滤；
- 查询 `fromDate/toDate` 能查到窗口期已入库的旧内容（证明入库门禁 ≠ 查询门禁）。

### 7.4 指标

- 提交/抽取后 `node_source_metrics` 有记录；
- 可按 node/source 查询 submissions、selected blocks/links、entities/relations、zero_relation、capture_failures。

### 7.5 批处理

- 触发/等待夜间批次，确认 selected blocks 被提取；
- 重启后 job 不丢、可重提。

---

## 8. 运维交接

### 8.1 配置变更

- 只改 `/etc/osbi-net/runtime.yaml`，不写回代码仓；
- 改 statement/schema/rules/model 后重启，记录 config fingerprint；
- 需要时按 channel/日期范围 reprocess。

### 8.2 备份

- 停止服务后备份 `/var/lib/osbi-server`（SQLite + Oxigraph）；
- 定期校验可恢复；
- 不备份密钥明文；密钥只在运维密钥库。

### 8.3 监控

- `/health`：状态、protocol、模型、数据库/图路径；
- 指标表：每 node/源的提交量、选择率、抽取量、零关系率、抓取失败；
- 批处理进度/失败数；
- 磁盘/WAL/Oxigraph 增长。

### 8.4 安全

- 全部 channel 对所有 key 开放是**产品设计**，不是漏洞；不要给 key 配 channel 限制；
- 但必须保证：HTTPS、Bearer、密钥仓外、data 目录 700、Nginx 只信任 loopback upstream；
- source 准入 v1 不做：任何 node 可把任意源提交到任意 channel，server 只记录指标；后续再评估允许列表。

---

## 9. 切流与回滚

### 9.1 切流顺序

1. 开发 Agent 完成 v4 + client 改造；
2. 本 Agent 部署 server + smoke test 通过；
3. 客户端 Agent 把临汾 node 的 serverUrl/serverKey 指向远端，source→channel 迁移完成；
4. 成都 node 接入同 server（独立 key、独立 source/board 配置）；
5. **双跑**：旧影子（08:40/07:50）与新 osbi 影子同时生成，diff 输出（先 dry-run 不推送）；
6. 连续 7 天无结构性差异后：
   - 停 `~/midplatform/run_all.sh` 中的 `schedule_task` 和 `wiseflow_server`（可保留 query bot/其他进程按需）；
   - osbi client 接管采集与 topnews/weekly；
   - 旧数据只读保留；
7. 客户确认后停老生产内容，正式切影子。

### 9.2 回滚

- 旧系统数据/代码未删除；重启 `run_all.sh` 即可回到旧链路；
- 新 server 数据独立，不影响旧库；
- 回滚时客户端把 serverUrl/key 切回本地旧 8078 或停止提交。

---

## 10. 已知缺口（部署前逐项确认）

| 项 | 状态/动作 |
|---|---|
| 开发 Agent 尚未实现 v4/channel | 部署前确认代码完成 |
| `server/README.md`/deploy 文档过期 | 以开发文档 + 实际代码为准 |
| `pyproject` osbi-server 入口坏 | 用 `PYTHONPATH=server python -m wiseflow_server` |
| `seeds_chengdu.ttl` 尚未创建 | 部署前补：四川省→成都市→锦江区→狮子山街道 |
| 成都公众号精确显示名 | 客户提供后写入 mp 白名单 |
| 成都领导/属地口径 | 确认是否启用 leader boost；否则首周关闭 |
| `policy-latest` 正文日期正则覆盖 | 含绝对日期 + 相对表达（N天前/24小时前/昨天/前天/刚刚），开发 Agent 必须实现并过 smoke test；模糊表达一律拒绝 |
| 零历史 | 首周周报偏薄/惩罚不生效，属预期；按稳态验收 |
| 模型 parity | 远端 server 必须与生产影子同模型/prompt/参数 |
| 溯源指标 | v1 只记录，不做源准入 |
| 全部 channel 开放 | 产品设计；不要加 channel ACL |

---

## 11. 交付验收清单

- [ ] `/health` 正常，7 channel、schema/seeds、模型配置正确；
- [ ] 两个 node key 可提交任意 channel；node_id 正确进入 provenance；
- [ ] material v4 无 focus；channel 必填；同内容同 channel 多 node 去重且多 provenance；
- [ ] `policy-latest` 日期窗口 7 个 smoke 用例全过；
- [ ] evidence 支持 channel、sourceScope、territory prefer/only、日期/文本/实体；
- [ ] 指标表可按 node/source 查询；
- [ ] 夜间批处理跑通；
- [ ] 配置全部在仓外，代码无 channel/地名硬编码；
- [ ] 旧生产进程未被误停；切流由客户端 Agent 协调；
- [ ] 回滚路径演练通过。
