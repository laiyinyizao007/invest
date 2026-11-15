# ADR: 闭合沙盒状态机——`approved/` 目录、收菜动作与运行时装载的 fail-closed

- 状态:已采纳(2026-11-15,随 Issue [#34](https://github.com/laiyinyizao007/invest/issues/34) 落地)
- 方案:`plan.md`(分支 `feat/34-approved-sandbox`)
- 关联:`docs/roadmap.md` §2.2 / §2.4 / §2.5;`docs/adr/20261112-research-sandbox-config-and-approval.md`;
  `docs/adr/20261110-bisheng-ai-researcher.md`;`docs/adr/20261113-dsh-research-entry-and-out-of-band-approval.md`

## 背景

阶段四的沙盒状态机只闭合了一半。roadmap §2.2 写的是:

> 目录即状态:`pending_strategies/`(LLM 唯一可写)→ walk-forward + gate 全过 + **人工批准** → `approved/`(runtime 只认);**装载永远人工**,fail-closed

而 2026-11-14 实测确认:

| 缺口 | 证据 |
|---|---|
| `approved/` 不存在 | `ls approved` → No such file or directory |
| 批准只写账本,没有收菜动作 | `research_sandbox.record_approval` 只写 `data/research-control/approvals.json` |
| 运行时完全不认 `approved/` | `trading/runtime.py` 用 `cfg.strategy_class` 从 `strategies/` 装载 |
| roadmap 已登记该缺口 | §2.5「pending 已落地,approved/ 与骨架模板未做」 |

问题的实质:**批准机制没有兑现它唯一的存在意义**。批准当时只是一张「人看过这份代码」的收据,
没有任何下游消费它——批准过的代码与没批准的代码在运行时受到**同样的对待**(都进不了,
也都进得去,只要人自己把文件拷进 `strategies/`)。于是「批准」是流程仪式,不是授权。

## 决策

### 1. 批准 = 一份字节被授权进入运行时唯一认的目录

状态机补上最后一段:

```
pending_strategies/  ──(人批准,写账本)──>  批准账本
                                              │
                                    (本机收菜,本脚本)
                                              ↓
                                          approved/  ──(人工装载)──>  runtime
```

批准仍然**只绑定字节**(sha256)。这不变——变的只是它终于有了下游:
**批准记录是运行时装载的唯一授权来源。**

### 2. 收菜是独立脚本,不是 `research_api.py` 的子命令

`scripts/research_gateway.py` 会 import `research_api`,两者同进程。收菜是**改变「运行时
唯一认的目录」内容**的动作,是整个沙盒里最危险的一步,所以它不能放在网关可达的模块里。
`scripts/research_collect.py` 是独立文件,网关 import 不到它,端点白名单里也没有它。

### 3. 收菜的四道校验(缺一不可)

1. 源文件当前字节的 sha256 **必须在批准账本里**——收菜不产生批准,只搬运已批准的字节;
2. 人确认之后**再算一次** sha256,必须仍等于账本里的那份——防「批准后文件被换掉」(TOCTOU);
3. 写入 `approved/` 之后**复算**目标文件的 sha256,必须仍等于账本里的那份——复制必须逐字节一致;
4. 目标路径固定为 `approved/<原文件名>`,**不接受**任何路径参数——防路径穿越。

写盘用「唯一临时名( pid + uuid)+ `os.replace` + Windows 重试」,与 `record_approval` 同款。

### 4. 人的门槛与批准一致:TTY + 完整 64 位哈希

收菜只能在本机交互式终端里执行,且要粘贴完整哈希。理由与批准完全相同:敲 `y` 不算,
因为任何程序都能喂一个 `y`;非交互调用一律拒绝。**模型只能请求,不能授予**——
这条边界对「批准」和「收菜」同样成立。

### 5. 运行时装载 fail-closed,绝不降级

`risk/approved_guard.py::load_approved_strategy` 做四件事:位置(必须在 `approved/` 内)、
授权(账本命中)、合规(宪法静态筛查 + 设置里不得夹带受保护参数)、同一性(哈希、筛查、
编译、执行基于**同一份字节**)。

任何一项不过 → `ApprovedLoadError` → `TradingRuntime.start()` 返回 `EXIT_STARTUP_FAILED`(3)
并告警,**不回退**到 `strategies/` 里的默认策略。回退等于「装不上就换一个照样跑」,
那批准就白批了。

装载点选在**连网关之前**:没过批准复核的策略不该登录 CTP、更不该下单。

### 6. 第四层(受保护对象的身份指纹)跟着新执行入口走

宪法历史守卫(`tools/check_constitution_history.py`)要求:任何执行候选代码的入口,
必须在**同一个函数体**里自己成对取快照并比对(`snapshot_protected_state` 前后)。
`approved_guard` 因此在 `exec` 前后各拍一次指纹。这条不是新加的规矩,
是既有守卫对本 PR 的判定——它第一次拦住了新代码,按要求补齐。

### 7. 账本的「读」抽成共享只读模块

`risk/approval_ledger.py`:研究侧(`scripts/research_sandbox.py`)写,运行侧(`risk/approved_guard.py`)读。
两侧不能共用同一个模块(研究侧依赖重,运行侧刻意不 import 研究侧)。
写入口仍然**唯一**在 `research_sandbox.record_approval`;共享模块里没有任何写入口
(`tests/test_approval_ledger.py` 用 AST 锁住这一点)。

## 实现

- `risk/approval_ledger.py`:只读账本访问(`load_approvals` / `is_approved_sha` / `file_sha256`)。
- `risk/approved_guard.py`:从 `approved/` 装载策略,四道校验 + 第四层指纹比对。
- `scripts/research_collect.py`:收菜 CLI(`collect` / `list`),本机人工触发。
- `trading/runtime.py`:`RuntimeConfig` 新增 `approved_file` / `approved_dir` / `approvals_file` /
  `approved_strategy_class`;`start()` 在连网关前装载,失败即拒绝启动。
  **全部为 None 时行为与从前完全一致。**
- `scripts/run_headless.py`:新增 `--approved` / `--approved-dir` / `--approvals-file` / `--approved-class`。
- `scripts/research_config.py`:`paths.approved_dir` **固定**为 `approved`(不可配置到别处)。
- `scripts/decoupling_drill.py`:断耦演练(外层宕机时内层照跑),证据写进 `docs/decoupling-drill.md`。
- `docker-compose.yml`:挂载 `approved/` 与只读的批准账本。

## 不变

- 批准**只绑定字节**,流程(TTY + 完整 64 位哈希)一个字没改。
- 网关端点白名单不变,**没有**任何收菜/批准端点。
- 宪法不因本 ADR 放宽;收菜不是绕过宪法的后门(收菜时再筛一遍,装载时再筛一遍)。
- 风控底线不可由调用方指定:设置里夹带 `risk_budget` 之类 → 拒绝装载。
- 实盘准入主链不变:`docs/live-readiness.md` A 段的晋级门槛仍是准入条件,
  本 ADR **不**放宽门槛,也不构成任何策略已通过的证据。

## 已知取舍(如实记录)

1. **宪法第三层(运行中实例级复检)在运行时装载里没有完整实现**。静态层已禁止候选引用
   `guard` / `trail` / `cta_engine`(连读都不许),运行中改写路径已被堵死;本 PR 只在
   **装载期**做了完整的指纹比对(第四层)。运行**途中**的持续核对留待有真实实盘实例后评估。
2. **`approved/` 不入库**。未经审查的代码不进版本库;代价是容器镜像里没有它,
   靠卷挂载(`docker-compose.yml`)或运行机上的目录提供。实盘按 live-readiness C2 是
   在 Windows 运行机上直接用 Python 跑(本就非容器),不受影响。
3. **收菜需要人再确认一次哈希**,虽然批准时已经确认过。这是刻意的冗余:收菜是另一步
   不可逆的状态变更,值得再要一次「你确认的是这一份」。
4. **断耦演练不连真 CTP**。它证明的是「外层宕机不阻断内层」,不是实盘可用性;
   实盘证据另有其物(SimNow 演练与国信穿透测试)。

## 遗留问题

- 批准账本无限增长(本 ADR 不做清理)。
- 运行时**途中**的风控底线持续核对(见「已知取舍」第 1 条)。
- 4c 的「容器隔离放开」需另行评审,不在本 Issue 范围。
