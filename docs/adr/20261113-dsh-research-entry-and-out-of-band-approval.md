# ADR: 用 DSH 作为研究入口,批准经「带外确认」完成

- 状态:已采纳(2026-11-13,用户确认;实现尚未开始,实现期验证项见下)
- 日期:2026-11-13
- 关联:`20261110-bisheng-ai-researcher.md`(外层边界)、`20261112-research-sandbox-config-and-approval.md`(哈希绑定批准)、`20261112-status-dashboard.md`(状态页只读)、`20261113-evolution-engine.md`
- 触发:用户反馈「在毕昇里输入研究想法、再回终端敲 `approve` 太麻烦」,希望在 DSH Desktop 里发起研究并能完成批准。

## 背景

- 毕昇工作流(v3/v4)只能在毕昇网页或经 WebSocket 触发;批准要在本机终端执行 `research_api.py approve`。
- 状态页是只读监控页:`20261112-status-dashboard.md` 明确「批准候选策略」任何阶段都不进面板,`tests/test_web_contract.py` 把这条变成了可执行约束。
- DSH Desktop 是通用 Agent 客户端,默认 Preset 带命令行与文件读写工具,权限远宽于毕昇工作流。

## 决策

### 1. DSH 只是对话入口,毕昇仍是编排器

新增一个很薄的 MCP 服务(暂名 `research-mcp`),**只暴露三个工具**:

| 工具 | 作用 | 说明 |
|---|---|---|
| `发起研究(想法)` | 把想法填入毕昇工作流的 `input_idea` 节点并启动 | 优先走毕昇开放 API `POST /api/v2/workflow/invoke`(见第 5 条);不可用时回退到 `tools/bisheng/run_v3.py` 的 WebSocket 调用逻辑 |
| `查进度` | 返回当前节点与最近一次结果摘要 | 只读 |
| `请求批准(候选名)` | 触发带外确认界面,**不写批准记录** | 见第 2 条 |

不暴露:提交源码、回测、门槛、移入 `approved/`、读写任意文件、任意命令。迭代上限(默认 5 次 / 24 小时)与 `max_source_bytes` 仍由网关与毕昇工作流强制,MCP 不得绕过。

### 2. 批准必须带外:模型只能「请求」,不能「授予」

- `请求批准` 在本机拉起**独立的批准进程**(只监听 `127.0.0.1` 的小页面或系统原生窗口),展示完整源码、静态筛查结果、与父代的 diff。
- 批准动作由**人在该界面**完成:输入文件 sha256 前 8 位后点击确认。确认界面发生在 DSH 模型接触不到的位置;一次性 CSRF 令牌、校验 `Origin`、不开 CORS。
- 批准记录仍写入现有 `data/research-control/approvals.json`,机制与 `20261112-research-sandbox-config-and-approval.md` 完全一致:绑定内容哈希,代码一改批准即失效。**写入者只有批准进程**,MCP 与毕昇均无写权限。
- 批准进程**不进入**毕昇所在的 Docker 网络,不与状态页同进程。
- 工作流停在 `input_wait` 后,MCP 只在批准记录出现后才向该节点发「继续」。
- **批准时哈希由后端重算**:批准进程读取候选文件的**当前**内容并自行计算 sha256,与人输入的前 8 位比对;不接受调用方(模型或 MCP)传入的哈希。
- **批准第一版只做带外界面;不做会话内原生批准框。** DSH 有原生批准框(`dsh-user-approval` / `dsh-client-ui-approval`),但目前只有沙箱 bash、`run_code`、子 Agent 等宿主代码会发起审批请求;已查资料中没有「对某个 MCP 工具要求人工批准」的配置。若要在会话内批准,需另写 DSH 插件在函数内调用 `ctx.approval.request`,列为**后续可选升级**(见未决)。升级时批准记录的写入者、哈希重算、负向测试均沿用本条,只替换触发界面。
- **会话 Preset 中禁用 Auto review**(`dsh-experimental-auto-review`,实验功能、默认关闭):它让当前 Agent 的模型自己审查并放行工具调用,属于机器应答者,会使批准退化为模型自批。

这是对「批准只能在命令行」的**有范围的扩展**:仍然是本机、人工、哈希绑定;不是网关端点,也不放进状态页。`research_api.py approve` / `review` 命令保留为等价的命令行入口。

### 3. DSH 侧隔离:自定义 Agent Preset,而不是通用 Preset

- 在 DSH 新建 Preset `research`(新 id,不覆盖内置 `standard`):只含 `persona` 与 `research-mcp` 的 MCP 条目。
- **禁止**在该 Preset 中放置:`tool-pwsh`、`tool-bash`、`tool-fs`、`tool-fs-search`、`tool-jobs`、`tool-skill`、`skill-filesystem`、`dsh-tool-cordis`、`dsh-tool-subagent`、`tool-web`。
- 工作区不指向 `invest/`;MCP 的环境变量只含毕昇登录与网关令牌,不含 CTP 或 `.env` 交易段。令牌用环境变量引用(`!!js process.env.X`),不写明文进配置;DSH 默认不会把名字含 KEY/TOKEN/SECRET/PASSWORD 的环境变量传给子进程,需在 MCP 条目的 `env` 里显式引用。
- **边界声明**:DSH 文档明确「preset 不是安全沙箱」。Preset 只限制**模型可见的工具**,防的是误操作与提示注入,不防恶意插件。真正的硬保障是第 2 条(批准带外)与 `invest/` 不暴露给该 Preset。

### 4. 验证依据(2026-11-13)

- 用 `dsh --profile <临时> --patch <research 补丁> --dump-config` 在临时 `DSH_HOME` 中组装配置:含 MCP 条目的 `research` Preset 合法组装,Preset 列表为 `standard, ptc, minimal, cordis, research`。
- 全局树 184 个条目中,命令行、文件读写、文件搜索、后台任务、技能、子 Agent、网页、待办等工具插件在全局层均为 disabled,仅在各 Preset 内启用。
- **未验证**:模型实际拿到的工具目录。接入时必须补一次实测:开 `research` 会话,让它列出可用工具,应只有三个 `mcp__research__*`(见验证)。

### 5. 毕昇侧调用方式与官方「企业接入」的边界(2026-11-13 查证)

- 毕昇 3.0 源码有开放 API `POST /api/v2/workflow/invoke`(`open_endpoints/api/endpoints/workflow.py`):请求体含 `workflow_id`、`input`、`session_id`、`message_id`、`override`、`stream`;非流式一次返回事件列表,另有 `/stop`;需 `workflow:invoke` 权限与对该工作流的「使用」权限;鉴权为服务账号密钥或个人访问令牌。
- 给 `research-mcp` 使用的服务账号**只授予 `workflow:invoke`,且只对研究工作流有「使用」权限**;服务账号权限单独授予,不继承任何自然人。密钥放环境变量,不入库。
- **官方「DSH 企业接入」只覆盖登录、模型、额度与席位(商业版网关),不覆盖调用工作流**;毕昇也没有把工作流发布为 MCP(PRD 中「MCP 复用同一密钥体系」属 P2,本期不做)。因此本 ADR 的 `research-mcp` 不能被官方集成替代,也不依赖商业版网关。
- **未实测**:本机 `v3.0.0-beta5` 是否启用该开放 API、服务账号怎么建、停在 `input_wait` 后用同一 `session_id` 再发 `input` 能否续跑。实现期第一步先实测,不通过则用 WebSocket 回退。

### 6. DSH 版本说明

- 本节验证用的是 `bisheng-rs/dsh-desktop` 源码目录(`0.1.1`,`dsh@0.2.0-rc.2`),与实际使用的开发版基线(v0.13.0+9,`dsh@0.2.1-rc.3`)不一致;GitHub 最新为 v0.13.2。
- 第 3、4 条的配置格式与审批机制结论,**须在实际使用的版本上复验**(`--dump-config` 与工具目录实测)后才算定稿。不为本任务升级 DSH。

## 备选方案

| 方案 | 结论 | 原因 |
|---|---|---|
| MCP 直接带 `批准` 工具 | 否决 | 模型能自己审自己写的代码,「人读过」失效 |
| 让用户在对话里回复「批准」由模型转写 | 否决 | 模型可伪造或代发 |
| 把批准放进状态页 | 否决 | 违反 `20261112-status-dashboard.md` 与其契约测试 |
| DSH 绕开毕昇直接调研究网关 | 暂不做 | 网关目前只对毕昇容器网络开放;且会让 DSH 的模型成为编排器,边界更难守 |
| 独立 `DSH_HOME` 实例 | 备选 | 隔离更彻底,但要重配模型与密钥;若第 3 条实测的工具目录有泄漏则改用 |

## 安全约束(决策的一部分)

1. 批准写入者只有批准进程;MCP、毕昇、网关均无 `approvals.json` 写权限(负向测试)。
2. `research-mcp` 的工具集合固定为三个;新增任何写类工具须新 ADR(契约测试枚举断言)。
3. 无人值守连续迭代(4c)仍不开;本决策不改变「4c 须先做容器隔离并单独评审」的结论。
4. 批准进程只读源码,不执行候选代码;展示内容经 `dashboard/safe.py::sanitize` 脱敏,不显示本机路径与令牌。
5. 提交给毕昇与 DSH 的任何内容按「会外发给模型提供方」处理。

## 影响

- 新增:`research-mcp`(薄 MCP)、批准进程、DSH `research` Preset 配置文件(随仓库只存不含密钥的模板)。
- 不改:交易内核、网关端点白名单、状态页、`20261112-research-sandbox-config-and-approval.md` 的哈希机制。
- 文档:`docs/roadmap.md` §2.5 登记表追加「DSH 研究入口与带外批准」行(阶段四 4b);`docs/web/` 无需改动。

## 验证(实现期)

- 负向测试:`research-mcp` 的工具枚举恰为三项;任何路径都无法写 `approvals.json`;批准进程拒绝非本机 `Origin` 与缺失 CSRF 令牌的请求。
- 断耦演练:停掉毕昇与 DSH,交易与风控不受影响。
- 带外性实测:在 DSH 里让模型尝试「直接批准」,应无任何可调用路径;批准界面只响应人的点击。
- 工具目录实测:`research` 会话只列出三个 `mcp__research__*` 工具。

## 未决

- 批准界面形态:`127.0.0.1` 网页 vs 系统原生窗口(倾向网页,便于展示 diff)。
- 会话内原生批准框(自写 DSH 插件调用 `ctx.approval.request`)是否升级:第一版稳定、用户仍嫌切窗口麻烦时再评审;须先实测该调用在实际版本里能弹框,且批准框只显示理由文字、源码仍需在会话里读原文。
- MCP 条目放在 `research` Preset 内部还是 Profile 级:倾向 Preset 内部(依据 MCP 插件的 scope 隔离设计),实测确认。
- 模型额度恢复前,`发起研究` 只能用 `build_v3_manual.py` 的手工联调版验证链路。
