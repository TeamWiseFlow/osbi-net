# OSBI-net 共享 Server 部署交接 v1（2026-11-23）

交接对象：负责服务器部署的 Agent / 运维同学
配套文档：`docs/OSBI-net开发文档_v1_2026-11-23.md`

---

## 0. 交接边界

本文件**只覆盖共享 server 的部署与运维**。

- 部署主机与任何客户端机器、生产机器相互独立；本文件不依赖、也不引用任何特定机器的目录、进程或本地配置。
- **不负责**客户端安装、浏览器资产、信源配置、报告板块、UI、推送目标；这些属于 node/client 侧，由客户端 Agent 处理。
- server 只做：鉴权（node key）、相关性判断、本体提取、图谱存储、evidence 查询、node 源与质量指标。
- 代码仓的 `docs/` + `server/config/example/` 是唯一依据；不要假设部署者能看到任何其他机器的运行目录。

**前置条件**：开发 Agent 已完成 `material-extract/v4`（无 focus、channel 必填）与配置外置实现。若代码仍是 v3，不要部署为本版本 server。

---

## 1. 部署目标与拓扑

```
                      HTTPS / Bearer node key
node: linfen-shanghai ───────────┐
                                  ▼
                         OSBI-net Server（单实例）
                          - 7 个全局共享 channel
                          - policy 图 + economy 图
                          - 相关性 / 本体提取 / evidence 查询
                          - node 源与质量指标
                                  ▲
node: shizishan-chengdu ─────────┘
```

- **一个 server**，全部 channel 对所有已认证 node 开放。
- **没有 tenant、没有订阅、没有按 channel 授权**；node key 只标识 node 身份。
- node 只把自己的材料提交到指定 channel；查询时显式指定 channel。
- 同一内容同 channel 多 node 提交 → 一个处理 job + 多 node provenance。
- **零历史起步**：新建数据目录，不导入任何历史图/历史账本。

---

## 2. 代码仓真实状态（部署前必读）

代码仓（分支 `dev`）处于“生产 wiseflow 引擎替换旧 osbi_server”的过渡期：

- **实际 server 包**：`server/wiseflow_server/` + `server/core/`
- **实际启动方式**：
  ```bash
  cd /opt/osbi-net
  PYTHONPATH=/opt/osbi-net/server \
  .venv/bin/python -m wiseflow_server --host 0.0.0.0 --port 8078
  ```
- `server/README.md`、`docs/running.md`、`server/deploy/*.example` 仍描述已废弃的 `osbi_server`（8780 端口、Supabase、动态 channel），**不要照搬**。
- `pyproject.toml` 的 `osbi-server = "osbi_server.cli:main"` 当前是坏入口；不要依赖 `osbi-server serve`。
- `osbi-client` / `osbi-mcp` 入口属于客户端，与本部署无关。
- 部署以**开发文档 + 实际代码 + `server/config/example/runtime.yaml`** 为准；README 与代码冲突时以代码为准。

---

## 3. 初始 server 配置

### 3.1 全局 channel（7 个，全部开放）

| id | title | schemaRef | dateWindowDays |
|---|---|---|---|
| `party-building` | 基层党建 | policy | null |
| `economy` | 经济动态 | economy | null |
| `community` | 社区治理 | policy | null |
| `digital` | 数字社区 | policy | null |
| `policy-latest` | 政策速递 | policy | **3** |
| `stability` | 平安维稳 | policy | null |
| `production-safety` | 安全生产 | policy | null |

`stability` 与 `production-safety` 是两个独立 channel，不能合并。

初始 statement（作为 `runtime.yaml` 的值；全部为跨地市、话题级宽口径，不含具体城市）：

**party-building**
> 基层党建与党建引领基层治理：基层党组织建设、党员教育管理、党群服务阵地、主题党日、物业党建、两企三新党建、新兴就业群体党建、党建引领网格化治理、街乡吹哨部门报到、新时代枫桥经验、全过程人民民主、基层减负、党建引领社区治理创新。

**economy**
> 经济形势、稳定增长、促进就业、货币政策、稳定物价、提振经济、降准降息、稳定楼市股市、高质量发展、新质生产力、蓝色生产力、协作生产力、绿色经济。

**community**
> 社区治理与居民服务：社区治理、基层治理创新、先进工作经验/案例/人物/事迹、居民自治、物业管理与三方协同、老旧小区改造、加装电梯、一老一小、居家养老、助老食堂、普惠托育、困难群众兜底、就业帮扶、文明实践、志愿服务、移风易俗、垃圾分类、市容环境、爱国卫生。

**digital**
> 数字治理与智慧社区：公共数据、数据流通、AI政务、数字居委会、数字社工、一网统管、一网通办、随申办、城市运行体系、城运中心、12345接诉即办、数字吹哨、网格化数字平台、智慧社区场景、数据赋能基层减负。

**policy-latest**
> 最近三天内发布的政策、政策公告、政策公示、政策解读、规范性文件、实施意见、规划纲要及配套文件。

**stability**
> 平安建设与社会稳定风险化解：矛盾纠纷排查化解、信访与投诉办理、群体性事件和突发事件处置、社会稳定风险评估、社会治安综合治理、流动人口和出租房管理、重点人群服务管理、舆情监测与稳控、基层平安创建。

**production-safety**
> 安全生产与应急管理：生产安全、消防安全、燃气安全、食品安全、特种设备安全、防灾减灾、隐患排查治理、节假日安保、重大活动安全保障、应急演练与救援。

### 3.2 存储、schema 与 seeds

- policy 家族 channel（party/community/digital/policy-latest/stability/production-safety）**共享一个 policy 图存储**；economy 独立存储。
- channel 是 provenance/查询过滤键，不是独立库。
- schema 使用仓库提供的 `policy.ttl`、`economy.ttl`；**不要为每个 channel fork schema**。
- seeds 需要至少：
  - `seeds_shanghai.ttl`（仓库现有）；
  - `seeds_chengdu.ttl`（部署前补建：四川省 → 成都市 → 锦江区 → 狮子山街道，及同级区/街道；URI 命名与现有 `uri_for` 一致）。
- seeds 写入独立 seed 图，幂等；schema/seeds 变更进入 fingerprint，重启后评估 reprocess。

### 3.3 node 身份

server 初始需要两个 node 身份（只用于 key、provenance、指标；不是 ACL）：

| node id | 名称 |
|---|---|
| `linfen-shanghai` | 临汾路街道 |
| `shizishan-chengdu` | 成都狮子山街道 |

- 每个 node 一个 Bearer key；key→node_id 映射只存哈希，文件权限 600，仓外。
- **全部 channel 对所有 key 开放**；不要实现 channel 授权或订阅。
- node 实际使用哪些 channel 由客户端决定；server 不限制、不需要知道其信源/板块配置。

### 3.4 模型与批处理

`runtime.yaml` 初始值（由项目负责人确认；部署时以实际 provider 可用模型为准）：

```yaml
models:
  baseUrl: https://api.siliconflow.cn/v1
  apiKeyEnv: OSBI_LLM_API_KEY
  selected: deepseek-ai/DeepSeek-V3.2
  ontology: Qwen/Qwen3.5-27B
  fallback: deepseek-ai/DeepSeek-V3.2
  concurrency: 3
  timeoutSeconds: 240
  ontologyChunkChars: 3000
  ontologyChunkOverlap: 200

batch:
  enabled: true
  windowStart: "01:18"
  windowEnd: "06:18"
  timezone: Asia/Shanghai
```

- 密钥只放私有 env / secret manager，不写 YAML、不进仓库。
- 抽取模型与 prompt/参数必须与项目约定的影子口径一致；否则只能功能等价，不能内容 parity。

### 3.5 配置外置

```text
/etc/osbi-net/
  runtime.yaml          # channels / stores / models / batch / seeds
  nodes.keys.json       # key→node_id 哈希映射，600
  osbi-server.env       # 模型密钥等 secret，600
  schemas/policy.ttl
  schemas/economy.ttl
  seeds/seeds_shanghai.ttl
  seeds/seeds_chengdu.ttl
```

- 代码仓只提供 `server/config/example/`；生产配置在仓外，运维修改不回写代码仓。
- 代码中不得硬编码 channel 列表、板块名、具体地名。

---

## 4. 安装与启动

### 4.1 安装

```bash
cd /opt/osbi-net
python3 -m venv .venv
.venv/bin/pip install -c requirements.lock -e '.[server]'
```

- server 不需要 Node/browser 资产。
- 当前 `osbi-server` 入口不可用；用第 2 节的 `PYTHONPATH=server python -m wiseflow_server` 启动。

### 4.2 启动

```bash
PYTHONPATH=/opt/osbi-net/server \
OSBI_CONFIG_FILE=/etc/osbi-net/runtime.yaml \
/opt/osbi-net/.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
```

- 前面放 Nginx/其他 HTTPS 反代；不要把 8078 直接暴露公网。
- 示例 systemd：

```ini
[Unit]
Description=OSBI shared extraction server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=osbi
Group=osbi
WorkingDirectory=/opt/osbi-net
EnvironmentFile=/etc/osbi-net/osbi-server.env
Environment=OSBI_CONFIG_FILE=/etc/osbi-net/runtime.yaml
Environment=PYTHONPATH=/opt/osbi-net/server
ExecStart=/opt/osbi-net/.venv/bin/python -m wiseflow_server --host 127.0.0.1 --port 8078
Restart=on-failure
RestartSec=5
UMask=0077

[Install]
WantedBy=multi-user.target
```

### 4.3 数据目录

```text
/var/lib/osbi-server/
  data.db
  wis_cache/
  policy/
  economy/
```

- 属主 `osbi`，权限 700/600；不要复用任何旧 server 数据目录。
- 备份在停止服务后进行。

---

## 5. Node key 与质量指标

### 5.1 生成与登记

- 按实现后的 CLI 生成两个 key（例如 `python -m wiseflow_server keys issue --node <node_id>`）。
- 明文只在生成时输出一次；交给对应客户端 Agent。
- 只保存哈希与 node_id 到 `nodes.keys.json`。
- 全部 channel 开放；不做 channel 授权。

### 5.2 指标（仅记录，不做准入）

server 记录 `node_source_metrics`：

```text
node_id, source_key, source_url, publisher,
first_seen, last_seen,
submissions, materials,
selected_blocks, selected_links,
extracted_blocks, entities, relations,
zero_relation_blocks, capture_failures
```

- 用途：观察各 node 的源覆盖与质量、为后续源准入评估提供数据。
- v1 不做源允许列表、不做拦截。

---

## 6. Smoke test（部署完成必须逐项做）

### 6.1 基本

- `GET /health`：protocol 为 `material-extract/v4`；7 个 channel 加载成功；schema/seeds 路径可读；模型配置字段存在；data 目录可写。
- 用两个 node key 分别提交材料：
  - channel 必填；无 focus；
  - 返回 selection/ontologyJobs；
  - provenance 含 channel、node_id、published_date。
- 同一内容、同一 channel、两个不同 node key 各提交一次：
  - 只产生一个处理 job；
  - provenance 包含两个 node_id。

### 6.2 日期窗口（policy-latest）

| 用例 | 期望 |
|---|---|
| meta.publishedAt = 今天 | 入图 |
| meta.publishedAt = 今天-5 天 | 拒绝（date_window） |
| meta 无日期，正文“发布日期：今天-1 天” | 入图 |
| meta 无日期，正文“2021 年印发” | 拒绝 |
| meta 无日期，正文“发布于3天前” | 拒绝 |
| meta 无日期，正文“2天前发布” | 入图 |
| meta 无日期，正文“24小时前发布” | 换算日期后入图 |
| meta 无日期，正文“72小时前发布” | 拒绝 |
| meta 无日期，正文“昨天/前天发布” | 入图；“大前天”拒绝 |
| meta 无日期，正文“一周前/上个月/去年发布” | 拒绝 |
| meta 无日期，正文“近日/日前/几天前/2天以前” | 拒绝（unknown/ambiguous） |
| meta 无日期，正文“刚刚/几小时前发布” | 入图，标 date_estimated |
| 正文只有相对表达且无 capturedAt | 拒绝 |
| meta/标题/正文都无日期 | 拒绝（date_unknown） |
| 正文两个冲突日期 | 拒绝（date_ambiguous） |
| 事件日期在窗口内、发布日期超窗 | 拒绝 |
| 查询 fromDate 覆盖窗口期 | 能查到已入图的旧内容（入库门禁 ≠ 查询门禁） |

### 6.3 Evidence

- 必须显式 channel；
- `sourceScope=all` 返回两个 node 的内容；`own` 只返回本 key 的 node；
- `territoryPreference` 只影响排序/偏好；`territoryMode=only` 才硬过滤；
- 能按日期、文本、实体、contentType 过滤。

### 6.4 指标与批处理

- 提交/抽取后指标表有累加；
- 夜间批处理可运行、可重试、重启后 job 不丢。

---

## 7. 运维

### 7.1 配置变更

- 只改仓外 `runtime.yaml`；重启后记录 config fingerprint。
- statement/schema/rules/model 变化触发 reprocess 评估。
- 不把运维配置提交回代码仓。

### 7.2 备份与恢复

- 停止服务后备份 `/var/lib/osbi-server`。
- 定期校验可恢复。
- 密钥只存运维密钥库，不备份明文。

### 7.3 监控

- `/health`：protocol、模型、数据库/图路径。
- `node_source_metrics`：node/source 提交量、选择率、抽取量、零关系率、抓取失败。
- 批处理进度/失败数；磁盘/WAL/Oxigraph 增长。

### 7.4 安全

- 全部 channel 对所有 key 开放是产品设计；不要加 channel ACL。
- 必须：HTTPS、Bearer、密钥仓外、data 目录 700、Nginx 只信任 loopback upstream。
- v1 无源准入：任何 node 可向任意 channel 提交；server 只记录指标。

---

## 8. 验收清单

- [ ] `/health` 正常，7 channel、schema/seeds、模型配置正确。
- [ ] 两个 node key 可提交任意 channel；node_id 正确进入 provenance。
- [ ] material v4 无 focus；channel 必填；同内容同 channel 多 node 去重且多 provenance。
- [ ] `policy-latest` 日期窗口 smoke 用例全过（含相对日期）。
- [ ] evidence 支持 channel、sourceScope、territory prefer/only、日期/文本/实体。
- [ ] 指标表可按 node/source 查询。
- [ ] 夜间批处理跑通。
- [ ] 配置全部在仓外，代码无 channel/地名硬编码。
- [ ] 回滚/重建环境可复现（仅依赖代码仓 + 仓外配置）。

---

## 9. 已知缺口

| 项 | 动作 |
|---|---|
| 开发 Agent 尚未完成 v4/channel | 部署前确认代码完成 |
| `server/README.md`/deploy 文档过期 | 以开发文档 + 实际代码为准 |
| `pyproject` osbi-server 入口坏 | 用 `PYTHONPATH=server python -m wiseflow_server` |
| `seeds_chengdu.ttl` 尚未创建 | 部署前补建四川省/成都市/锦江区/狮子山街道 seed |
| 零历史 | 首周数据偏薄，属预期 |
| 模型 parity | 抽取模型/prompt/参数需与项目约定一致 |
| 源准入 | v1 只记录指标，不做拦截 |
| 全部 channel 开放 | 产品设计，不加 ACL |
