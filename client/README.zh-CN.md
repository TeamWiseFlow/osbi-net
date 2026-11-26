# OSBI Client：部署与使用手册

[English](README.md) | 简体中文

Client 由常驻本地 backend、托管浏览器、dashboard、CLI 和 MCP 接口组成，负责采集信源和消费共享证据。
独立部署的 Server 负责相关性选择、频道定义、本体提取及图存储；Client 不维护持久化图谱副本。

## 安装与启动

每个 Client 部署使用独立的 checkout 和数据目录。已验证环境为 Linux x86_64。
Bash 安装脚本包含 Linux／macOS 的 x86_64／arm64 路径，原生 Windows 和 macOS 运行尚未验证。
需要 Bash、curl、tar、SHA-256 工具和浏览器系统依赖；有头浏览器还需要图形会话。
安装过程需要下载 Python／npm 依赖及浏览器资源。

在仓库根目录执行：

```bash
git clone --branch dev https://github.com/TeamWiseFlow/osbi-net.git
cd osbi-net
bash client/scripts/install.sh
bash client/scripts/run.sh
```

安装脚本安装 `.[client]`，构建 `client/backend/browser`，并校验浏览器、指纹、插件和 GeoIP 资源。
复用合格的 Python 3.12+、Node 24.9+；缺少时在 `.osbi/runtime` 中安装锁定版本。
Python 包位于 `.venv`，不修改系统运行时和 shell profile；Client 无需安装 Server 图存储依赖或 Agent。

默认入口为 **http://127.0.0.1:8781**。Backend 应保持常驻，不依赖网页标签页或 Agent 会话。
指定其他数据目录和端口：

```bash
bash client/scripts/run.sh --data "$HOME/.osbi/client-demo" serve --port 8782
```

需要长期常驻时，可安装可选的 systemd `--user` 单元：

```bash
bash client/scripts/install.sh --systemd
systemctl --user status osbi-client.service
```

生成的单元执行 `ExecStart=/bin/bash <checkout>/client/scripts/run.sh serve`。
已有单元请一律指向 `client/scripts/run.sh`，不要指向已移除的包装脚本；
升级 checkout 后需重启服务，新代码才会生效。该单元不启动图形会话，
需要可见浏览器时在 `[Service]` 段增加 `Environment=DISPLAY=:0`。

`mp` 信源采集要调用微信容器命令，因此单元会同时固定容器名：`--systemd`
自动识别运行中的 `wechat-linux-container`，或以 `mimicwx` 开头的容器，并写入
`Environment=WECHAT_LINUX_CONTAINER_CONTAINER=<名称>`。其他容器用
`--wechat-container NAME` 指定；`--wechat-container none` 则不写覆盖，回落到内置默认值。

CLI／Agent 进程使用对应的 `OSBI_CLIENT_DATA` 和 `OSBI_CLIENT_URL`。
默认状态目录是 `~/.osbi/client`。控制 API 只监听 loopback；远程访问可使用 SSH 隧道，
例如 `ssh -L 8781:127.0.0.1:8781 CLIENT_HOST`。浏览器人工操作仍需要 Client 主机上的图形会话。

运行时／资源缓存和手动安装说明见[安装细节](../docs/running.md)。
安装程序不会配置共享 Server、登录微信或发送消息。

## 界面语言

Dashboard **默认英文**，即使浏览器语言为中文也如此。通过页头 **Interface language**
选择 **简体中文**。偏好保存在当前浏览器的本地存储中，刷新和切换页面后继续生效。
切换语言会原地更新标签、说明、占位提示和日期显示，保留未保存的表单输入，不提交设置。
浏览器禁用本地存储时仍可使用界面，但刷新后恢复默认英文。

TopNews 的中文 **加油站** 在英文界面中显示为 **Do you Know**。
界面语言不会翻译采集原文、用户自定义名称、报告文案、模型输出或原始诊断消息；
报告的语言和文案通过对应的输出文案及 Prompt 单独配置。

## 首次配置

打开 `/settings`（**信源配置**）：

1. 在“共享服务”填写 Server 的 HTTPS 地址和客户／节点 Key；私有证书才需要“自定义 CA”。保存此段。
2. 添加信源，指定稳定的 ID，选择网页、RSS 或微信公众号。
3. 从 Server 提供的目录选择提交频道。多频道分别提交；不选频道表示只采集、不提交。
4. 设置浏览器 Profile、采集间隔和可选操作流程，保存信源。
5. 点击“运行”，在“消息通知”中查看进度及需要人工登录／验证的操作。

各段设置可独立保存。Key 只写不读，输入框留空保留原值。
采集偏好支持全局／按域名并发、URL 排除片段、超时、并发提交和缓存 TTL。

未配置每日抓取时间、且采集间隔为 `0` 时只手动运行；非零间隔由常驻 backend 调度。
浏览器 Profile 保留会话状态，无头任务遇到登录或验证码时也可能需要切换到有头环境处理。
信源支持 navigate／click／fill／select／wait／scroll／switch／capture／wait_for_user 等操作步骤；
复杂流程可以交给管理技能生成。

微信公众号信源需要每行填写一个与本机微信中完全一致的公众号显示名，固定入口为
`https://mp.weixin.qq.com/`。目录 hunter 依赖另外部署并登录的 wechat-linux-container 及其微信数据库／密钥访问环境；
可用 `WECHAT_LINUX_CONTAINER_CONTAINER`、`WECHAT_LINUX_CONTAINER_WX_USER_DIR`、`WECHAT_LINUX_CONTAINER_KEYS_FILE` 选择该环境。
Client 安装程序不提供这套环境。Hunter 发现文章 URL，正文由浏览器抓取，公众号信源不使用可编程步骤。

### 页面并发与人工验证

共用浏览器 Profile 的信源使用同一个浏览器实例，各个采集页面独立运行。
新一轮默认无头；同一信源的多篇文章、不同信源的页面都可以并发。
`browserConcurrency` 是总页面容量（1–100，默认 2），实际容量不低于活跃 Profile 数。
域名限额跨所有 Profile 统一计算，因此增加 Profile 不能绕过域名限额，实际并发也可能低于容量。
需要共享登录／验证状态的信源可使用同一个 Profile；不同 Profile 的浏览器身份和 Cookie 独立。

某个页面需要验证时，暂停向对应 Profile 派发新页面，关闭实例，再用原 Profile 有头单页面运行。
未完成的 URL 抓取重新入列，已经采完的材料保留。完成验证后点击“完成并继续”，
客户端先采完当前页面，再切回无头并发。如果 10 分钟内再次触发验证，
该 Profile 本轮保持有头串行，其他 Profile 继续运行。
同 Profile 之前的信源都结束后，新一轮才重置为无头；新启动的信源不会重置其他信源正在处理的验证。
有头模式需要图形会话或虚拟桌面。

模式切换复用 Profile 目录、固定浏览器指纹和会话 Cookie，但会重建标签页，不能保证保留临时页面状态。
结果不明的交互操作需要用户明确确认后才能继续，不会自动重放。
首次升级到页面级采集时，尚未结束的旧任务会标为中断，请取消后重新启动信源；
已经保存的材料、完成的提交和 Profile 文件保留。

每个信源单次执行从创建起最多 **3 小时**，排队、重试、人工等待和等待提交均计入。
每次人工提示最多等待 **30 分钟**，独立于页面缓存 TTL；心跳、恢复和重启后台都不会续期。
到期会取消该信源待执行／执行中的本地工作，释放其页面；其他信源仍在使用的浏览器不会被关闭。
服务端已接收的材料保留。

提交与页面采集独立：`submissionConcurrency` 范围为 1–32，默认 4；
`maxPendingSubmissions` 范围为 1–2000，默认 100。达到待提交上限时暂停派发新抓取，已有页面继续收尾。
已知 URL 不必等待上一页筛选完成；依赖筛选结果发现的链接仍需等待结果。
两类队列均持久化，可在“信源配置 → 采集偏好”设置。
旧配置中的 `headless` 字段仍可读取，但会统一转为自动管理模式。


## 信源定时抓取

在“信源配置”中，为每个信源设置“每日抓取时间”，可以添加多个 24 小时制 `HH:MM` 时刻，
例如 `08:00`、`12:30`、`18:00`。选择“抓取时区”（IANA 名称，默认 `Asia/Shanghai`）后保存信源。
“下次定时抓取”按已保存计划和该时区显示；重新加载或保存后刷新。每个信源最多配置 48 个不同时间。

Backend 常驻时会自动将到期信源加入采集队列。每日时间表优先于“采集间隔／分钟”；
移除全部每日时间后恢复间隔策略，间隔也为零时仅手动运行。
停用或删除信源不会自动运行。同一信源仍有活动任务时跳过该时刻并记录通知，不创建重叠抓取；
不同信源仍受浏览器 Profile 和域名并发限制。

计划持久化保存，重启不会丢失；保存其他设置不会重置未修改的计划。
停机期间错过的多个时刻合并补抓一次，随后等待下一个未来时刻，不逐次补跑积压任务。
新增或修改计划从下一个未来时刻开始。夏令时不存在的时刻跳过，重复时刻只按第一次出现执行。
信源抓取时间与 TopNews／Weeklyreport 的报告生成时间相互独立。

信源的对应 JSON 片段（合并到该信源其他字段）：

```json
{
  "schedule": {
    "times": ["08:00", "12:30", "18:00"],
    "timezone": "Asia/Shanghai"
  }
}
```

## 页面说明

| 页面 | 用途 |
| --- | --- |
| 情报图谱 `/graph` | 切换频道、搜索实体、查看关系路径和原文证据；同 Schema 的频道可能展示同一张图。 |
| 信源配置 `/settings` | 共享服务／Key、信源、采集偏好和可选视觉模型。 |
| 查询设置 `/query` | 默认来源范围、可选 Jev 后置过滤和本地报告／查询模型。 |
| 任务设定 `/tasks` | 报告方案、共享 Sections、任务板块、Prompt、调度、输出文案及 DOCX 文件名。 |
| 任务产出 `/reports` | 按窗口生成报告，查看最近运行日志／文件，维护发送目标。 |
| 消息通知 `/notifications` | 采集事件、连接状态和人工待办。 |

来源范围 `all` 使用授权范围内其他节点的共享贡献，`own` 只使用本节点信源。
共享 Schema 图谱的实体／关系总数不是单频道采集计数；证据仍受 Server 日期门禁约束。

## 配置报告任务

在“查询设置”配置本地报告模型，默认地址 `http://127.0.0.1:9997/v1`、模型 `xin_local`。
需要自行运行该模型服务，或改用带独立 Key 的远程 HTTPS 模型。OSBI 安装程序不安装模型服务器。
它与 Server 的提取模型独立；调用失败时回退到确定性选材／格式化，并在输出元数据中标明。

“任务设定”中，每个方案包含：

- 共享 Sections：标题、一个或多个频道、查询过滤、属地及本地规则。
- TopNews 板块：候选池选材、条数和可选编辑 Prompt。
- Weeklyreport 板块：action／policy／evidence 选材、关系阈值和 Prompt。
- 各自的调度时间、发送目标 ID、启用和 Dry-run 开关。
- 输出文案：TopNews 导语／结尾语及 Weekly 导语模板。

任务板块引用 Section 并覆盖本任务的参数，不修改共享模板。
通过本地 Section 和 Prompt 配置关注点，不向 Server 提交频道路由指令。
内置预设包含中文地域和板块名称，其他部署应修改这些配置值；界面语言切换不会改写它们。

TopNews 默认窗口为昨日至今；Weeklyreport 默认从七天前到今天，即八个日历日。
“任务产出”中指定日期的运行使用该窗口；“任务设定”的“手动运行”使用 backend 默认窗口。
调度按配置时区执行，依赖 backend 常驻。启用任务必须至少配置一个板块。
审阅产出时保留 **Dry-run**：仍生成并保存文件，但不发送。

### Weekly DOCX 文件名

在“任务设定 → Weeklyreport → DOCX 文件名”填写基础名称并保存。
每次生成时固定追加生成当天的日期和扩展名：

```text
Council weekly → Council weekly_20261125.docx
部门周报        → 部门周报_20261125.docx
```

格式固定为 `{基础名称}_{YYYYMMDD}.docx`，日期来自生成当天，与取材窗口无关。
粘贴带 `.docx` 的名称时会先去掉该扩展名再追加后缀。
不接受目录路径、控制字符及保留文件名字符；名称上限为 120 个字符、240 个 UTF-8 字节。
原有配置继续使用之前的默认基础名称。

此项按方案保存，适用于手动和定时运行，并影响本地 DOCX、最近文件列表、下载文件名和文件发送。
Markdown 和导语 TXT 文件名保持原规则。同一天用同一基础名称生成会覆盖当天文件；
不同方案需要分别留存时应使用不同名称。“发送 docx 文件”只控制发送，不控制生成。
DOCX 导出失败时仍保留 Markdown／TXT 及相应诊断。

### 发送与日志

在“任务产出”维护发送目标，再在任务中勾选对应目标：

- wechat-linux-container：支持文本，配置地址、群名、Token 或私有配置文件；文件发送返回 `file_not_supported`。
- Awada：支持文本及 DOCX，配置私有配置路径和频道 ID；文件可使用独立频道。
- 企业微信 hook：预留，发送返回 `not_implemented`。

微信目标的 `type` 为 `wechat-linux-container`。可选的私有 JSON 使用
`wechat-linux-container` 节，字段为 `base_url`、`token`、`window`。
环境变量默认值来自 `WECHAT_LINUX_CONTAINER_BASE_URL`、
`WECHAT_LINUX_CONTAINER_TOKEN`、`WECHAT_LINUX_CONTAINER_WINDOW`，目标中的显式配置优先。
已有安装在启动此版本前，需要迁移已保存的目标类型、私有 JSON 节名和环境变量名，
同时保留方案／目标 ID 及密钥。公众号 hunter 的默认 Docker 名称也为
`wechat-linux-container`；容器名称不同时使用 `WECHAT_LINUX_CONTAINER_CONTAINER` 指定。

保存目标不会发送测试消息。实际发送需要关闭任务 Dry-run，并启用选中的目标。
“任务产出”显示最近 10 次运行、回退原因、文件路径及发送结果。
全部生成文件位于 `<client-state>/reports/`。

## 抓取与提交缓存

本地不同任务默认共享六小时内成功抓取的页面，支持按域名覆盖 TTL；命中不刷新实际抓取时间。
提交结果缓存独立，默认七天，仅在对应的服务／内容／配置范围内复用。
两者都不代表 Server 全局 URL 保鲜状态；对应 TTL 设为 `0` 可关闭缓存。

排除片段按 URL 子串匹配，信源起始 URL 豁免。重测页面时可显式刷新抓取：

```bash
.venv/bin/osbi-client run source-1 --refresh
```

频道由 Client 选择。v4 每个链接始终携带 `content`／`unknown`／`navigation` role；
Server 限制选中导航链接的数量，但不改变正文日期门禁，详见[链接选择](../docs/link-selection.md)。

## CLI 与 Agent

以下命令依赖常驻 backend：

```bash
.venv/bin/osbi-client status
.venv/bin/osbi-client run source-1
.venv/bin/osbi-client runs
.venv/bin/osbi-client channels
.venv/bin/osbi-client query economy ENTITY_NAME
```

`configure` 从标准输入读取 JSON patch。替换 `reportProfiles` 前先读取现有配置，保留其他方案，
因为数组不是逐元素 patch。[示例配置](config/example/client.yaml) 用于说明结构，backend
通过 API／CLI 配置，不会自动加载该 YAML。

DeepSeek Harness 已有适配器，其他 Agent 平台目录目前为占位：

```bash
bash client/scripts/install.sh --agent dsh
# With the backend already running, in another terminal:
bash client/scripts/deepseek-harness/run.sh
```

适配步骤安装共享管理／查询技能；DSH 主机在首次显式启动时才下载。
会话模型需要单独配置，详见[DSH 设置](scripts/deepseek-harness/README.md)。
同一部署的不同 Agent 共用一个 Client backend 和浏览器。

## 升级、备份与排障

等待采集／报告任务完成，停止 backend 及其拥有的浏览器会话，备份完整 Client 状态，
包括 `client.db`、浏览器 Profile、报告和台账。更新到审阅后的 `dev` 版本；
依赖、浏览器运行时代码或资源变化时重新运行安装脚本，再用原数据目录启动。
浏览器资源及模型服务与界面语言独立。

代码同步后，已安装 systemd 用户服务的部署可在仓库根目录执行：

```bash
systemctl --user stop osbi-client.service
bash client/scripts/install.sh
systemctl --user restart osbi-client.service
systemctl --user status osbi-client.service --no-pager
```

安装脚本会重新构建浏览器运行时；不加 `--systemd` 时保留已有单元的显示、容器及数据目录设置。
安装命令加 `--agent dsh` 可同时刷新已安装的 DSH 技能；使用自定义目录时沿用原来的 `--dsh-home`。
如果 backend 是手动启动的，停止原前台进程，安装后重新执行 `bash client/scripts/run.sh serve`。

| 现象 | 检查项 |
| --- | --- |
| 没有频道 | Server 地址／Key、有效期、net 启用状态及 CA 信任。 |
| 采集后图谱无变化 | 信源是否选了频道，Server 日期门禁及夜间本体队列。 |
| 等待人工操作 | 在 Client 浏览器完成登录／验证，然后在通知中继续。 |
| 报告为空或只有材料概览 | 日期窗口、范围／属地／查询过滤、关系阈值和证据数量。 |
| 模型回退 | 本地模型地址／名称／Key、服务可用性及超时。 |
| 消息／文件未发出 | Dry-run、目标启用、目标 ID 和适配器文件支持情况。 |
| 重测仍得到旧结果 | 刷新抓取，并分别检查提交缓存与 Server 状态。 |
| 语言选择刷新后丢失 | 浏览器本地存储是否可用；偏好按浏览器 origin 保存。 |

使用前阅读[原文存储与责任说明](../README.zh-CN.md#部署前须知原文存储知识产权与责任声明)，
妥善保管 Key、登录状态、原文证据和备份。
