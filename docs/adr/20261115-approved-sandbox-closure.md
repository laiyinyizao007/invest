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

### 8. 收菜默认**拒绝**覆盖已存在的另一版本

批准目录里已有同名文件、内容不同 → **拒绝**,要换版本必须显式 `--replace`,
且旧版本会被留成 `<名>.prev`(字节一致,便于回溯)。

理由:覆盖等于换掉「运行时正在认的策略」,这是另一件需要显式授权的事;
而且运行中的进程可能正持有那个文件。默认拒绝把「换版本」变成一个**有意的动作**,
而不是收菜时的顺手副作用。

### 9. 换批准版本必须同时换 `strategy_name`

`cta.strategy_data` 按 `strategy_name` 存(持仓、止损、入场价等)。
换 `approved/` 里的文件却沿用同一个名字 → 旧版本的持久化状态会被新版本继承,
而 `state_consistent()` 只覆盖「持仓与止损方向」这一层,不覆盖「这份状态属于哪个版本」。

**不靠操作者记得改**:装载批准策略时,状态命名空间默认由**批准哈希**派生
(`approved_<sha 前 8 位>`)——换版本自动换命名空间,旧状态留在旧名字下(可回溯,不串)。
`--strategy-name` 仍然保留,给需要固定名字的场景用。

### 10. 第四层:比较器与快照器提前绑定 + 用后自检

第 1 轮 codex 复核**实测**出一条绕过(本 ADR 最重要的一条修正):

```python
# 候选在自己的模块体里:
import risk.constitution as _c
_c.check_protected_state = lambda *a, **k: []
```

如果装载器在 `exec` **之后**才经 `const.check_protected_state` 去取比较器,
取到的就是候选自己换上去的那个空函数 → 结论恒为「没被改写」→ **第四层静默失效**。
实测还发现:静态层(`screen_source`)对这条载荷**返回 `[]`**——它只按属性名查黑名单。

同一个载荷在研究侧过不去(受限内建 + 导入白名单 + 按路径的导入裁决),
在运行侧一路绿灯(完整真实内建)——**这就是「能力不对称」的实际后果,不是表面差异**。

修法(已实施):
- 比较器与快照器在 `exec` **之前**绑定成局部引用(`real_snapshot` / `real_check`);
- `exec` 之后用**这两个引用**拍快照并比对;
- 再核对「模块上的那两个属性还是不是原来那两个」,被换过就拒绝装载。

### 11. 类级校验(`check_class`)补齐到运行侧

静态层只按**名字**查黑名单。「候选是否重写了模板成员」要到运行时才知道——
那一层由 `constitution.check_class` 负责。研究侧有(`research_api.import_candidate`),
**运行侧原先漏了** → 研究侧拒的候选,运行侧会收。

漏掉的具体成员:`on_daily_bar`(日线处理的唯一入口:撤单、ATR 更新、
`_try_enter`(预算+连亏熔断)、`_manage_position`(移动止损)、`sync_data` 全在它下面)、
`variables`(持久化/恢复的状态集)、`daily_end`(收盘时点)。
它们的名字**不在** `BUILTIN_PROTECTED_NAMES` 里,所以静态层看不见。

修法:`getattr(cls, ...)` 之后、`issubclass` 之前补 `const.check_class(cls, template)`。

### 12. 「仅研究」的骨架不许在交易运行时装载;实盘骨架要显式选

宪法指定的默认模板是 `strategies.trend_atr_15m_strategy:TrendAtr15mStrategy`,
而那个骨架的 `on_tick` 直接抛异常(`仅用于研究回测,不支持实盘/模拟盘`)。
候选必然继承它、又不许重写 `on_tick`(它在受保护名单里)→
**在实盘第一根 tick 上抛异常、被引擎静默停掉**:失败方向安全(不下单),但信息极具误导性。

修法:
- 骨架加 `research_only = True` 标记;装载器看到就**拒绝**,并说清楚原因;
- 运行时装载可用 `--approved-template` 显式选**实盘可用**的骨架
  (目前是日线的 `strategies.trend_atr_strategy:TrendAtrStrategy`,在宪法的内置白名单里)。
  缺省仍用宪法指定的默认骨架 → 默认就是拒绝。

### 13. 风控底线参数 ≠ 全部受保护参数

宪法的 `BUILTIN_PROTECTED_PARAMETERS` 含 `history_vt_symbol`(预热用哪个历史合约)。
它**不是**风控底线,而运行时会从 `--history` 传它——若一并禁掉,
**实盘的正常配置(带历史预热)就永远装不上**。

修法:装载时只校验**风控底线**那三个(`risk_budget` / `max_daily_losses` / `atr_window`),
操作参数放行。底线仍然是底线:调用方不得指定它们。

### 14. 授权来源钉死:账本路径固定、批准目录不可改指

- 删掉 `--approvals-file`:账本路径没有理由可配(它固定在 `<root>/data/research-control/approvals.json`)。
  保留它等于把「批准账本是唯一授权来源」降级成「启动命令说了算」。
- `--approved-dir` 解析后必须**等于** `<root>/approved`——把它指到 `strategies/` 就是
  「候选目录与受信目录混为一谈」(研究侧用同一份受保护目录名单堵死了这条路)。
- 装载前先跑一次**批准目录整体自检**:目录里任何一个文件与账本不一致 → 拒绝启动。

### 15. 所有闸门在**模块导入时**绑成常量,装载前后逐一核对(第 2 轮 codex 复核的 F1)

第 2 轮 codex 实测出一条比第 1 轮更深的洞:

> 一次**被拒**的候选可以把全部闸门永久毒化,同进程第二次装载完全裸奔。
> 候选 #1 一行就关掉了五道闸:`check_protected_state`、`snapshot_protected_state`、
> `screen_source`、`check_class`、`check_params`,外加账本读取。
> 载入 #2(真正的攻击载荷):**LOADED** ——第四层死了,300 元预算被永久换掉。

修法两条一起上:
1. **闸门函数在 `risk/approved_guard.py` 模块导入时**绑成常量(`_CANONICAL_GATES` /
   `_CANONICAL_LEDGER`),装载时用的始终是「导入时那个真函数」;
2. 装载**前后**逐一核对「模块上的属性还是不是那个真函数」——
   **比对结论可以被伪造,但属性身份核对不能**:候选换掉 `const.check_protected_state`,
   指纹结论就恒为「没被改写」,但身份核对会当场发现。

### 16. 被投毒之后,本进程不再允许装载

发现闸门或受保护对象被改写 → 进程标记为**已中毒** → 之后任何装载一律拒绝。
理由与 ADR「已知取舍」第 3 条同源:被拒的候选可能已经改坏了进程,
留着这个进程继续装载下一个候选,等于把下一个候选交给一个被拆了防线的进程。
运行时本来就是「一进程一次装载、失败即退出」,这里把那条约定**变成代码强制**。

### 17. 候选不得用 `SystemExit` / `KeyboardInterrupt` 让进程「正常结束」

候选在导入期 `raise SystemExit(0)` 会一路穿过装载器 → `start()` → `run()` → `main()`,
进程以**退出码 0** 结束:`restart: on-failure` 不重启、监控看着像「正常停止」、且**零告警**。
修法:`exec` 的兜底改成 `except BaseException` → 统一转成 `ApprovedLoadError`(研究侧同口径);
运行时装载再兜一层 `except BaseException`,保证任何异常都变成「拒绝 + 告警 + 退出码 3」。

### 18. 装载时用**清单文件**里的那一份宪法

`screen_source` / `check_params` / `check_class` 三道闸原先退回内置默认值
(`c = c or default()`),而 `config/immutable_constitution.json` 的设计是「只能追加」。
不接上的话,将来往 JSON 里加的保护项只对研究侧生效、**对真正下单的运行侧不生效**。
修法:装载时统一用 `const.load()` 的结果,并显式传给三道闸。

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

1. **宪法第三层(运行中实例级复检)在运行时装载里没有完整实现**。静态层禁止候选引用
   `guard` / `trail` / `cta_engine`(连读都不许);本 PR 在**装载期**做了完整的指纹比对
   (第四层),并补上了类级校验(`check_class`)。运行**途中**的持续核对留待有真实实盘实例后评估。

   ⚠️ 但第 1 轮 codex 复核**实测推翻**了「运行中改写路径已被堵死」这句:
   `setattr(sys.modules["risk.risk_guard"], "within_budget", ...)` 这条路**静态层看不见**
   (实测 `screen_source` 返回 `[]`)。研究侧过不去是因为受限内建与导入白名单,
   不是因为静态层拦得住。
2. **运行时装载与研究侧存在能力不对称(重要)**。研究侧执行候选用**受限内建**
   (`safe_builtins`:拿掉 `setattr`/`getattr`/`eval`/`exec`/`open`)+ **按路径的导入裁决**;
   而 `risk/approved_guard.py` 的 `exec` 用**完整真实内建**,并且跑在
   **持有 CTP 凭据与交易会话的进程**里。

   仓库 doctrine 说「真正的边界是人工审批 + 容器隔离」,但**交易进程不是容器**
   (live-readiness §C2:实盘与穿透测试在 Windows 运行机上直接用 Python 跑)。
   于是:**LLM 产出的候选一旦被人批准,它在运行时的权限与人工写的 `strategies/` 代码完全相同**,
   而研究侧那套能力隔离对它不生效。

   本 PR 把第四层做成「候选拆不掉」(提前绑定 + 用后自检),但**没有**在运行时复用受限内建:
   一是受限内建的语义是为研究沙盒设计的,套到交易进程里会改变候选的可用能力
   (可能引入新的运行期故障);二是研究侧模块刻意与交易侧隔离,交易进程 import 研究模块
   会把那条边界抹掉。**这是本 ADR 明确接受的残余风险**,不是「已解决」。
3. **被拒的候选可能已经把进程改坏了**。拒绝发生在比对**之后**,而改写发生在 `exec` **期间**:
   一个被拒的候选可能已经把 `risk.risk_guard.within_budget` 换成了恒 0 的函数。
   → 所以**装载失败必须导致进程退出**,不能只是「拒绝这一份策略」而让进程继续跑。
   运行时正是这样做的(`EXIT_STARTUP_FAILED` → 进程退出);装载器本身也把
   「被发现投毒」变成**进程级中毒标记**,之后任何装载一律拒绝(§15/§16)。
   测试里这类载荷放在**子进程**里跑,不污染别的用例。
4. **`approved/` 不入库**。未经审查的代码不进版本库;代价是容器镜像里没有它,
   靠卷挂载(`docker-compose.yml`)或运行机上的目录提供。实盘按 live-readiness C2 是
   在 Windows 运行机上直接用 Python 跑(本就非容器),不受影响。
5. **收菜需要人再确认一次哈希**,虽然批准时已经确认过。这是刻意的冗余:收菜是另一步
   不可逆的状态变更,值得再要一次「你确认的是这一份」——而且收菜时**会把源码打出来**
   (批准那一步只给大小+哈希,`review` 才给源码),让人真正看一眼再决定。
6. **断耦演练不连真 CTP**。它证明的是「外层宕机不阻断内层」,不是实盘可用性;
   实盘证据另有其物(SimNow 演练与国信穿透测试)。
7. **`risk/approved_guard.py` 暂未登记进宪法的 `CALL_SITE_REGISTRY`**(第 2 轮 codex 的 F7)。
   登记表是**历史守卫**用的:它逐个历史版本检查「有没有把第四层摘掉过」。
   本分支早期的那几个提交里,第四层用的是**同名局部引用**的写法(已被「同名遮蔽」规则判违规),
   登记进来会让历史守卫把**那些已提交的版本**判成违规。
   现在用的是规范的属性调用形状 + 事后身份核对(§15);等这个文件进了主干、
   有了「合规形态」的历史版本之后再登记(见「遗留问题」)。

## 遗留问题

- **15 分钟的实盘骨架不存在**(Issue [#36](https://github.com/laiyinyizao007/invest/issues/36))。
  默认骨架是仅研究回测的 15m 骨架(会被拒绝装载),日线骨架是实盘可用的但与 15m 候选不同家族。
  要让 15m 候选上实盘,先得有人维护一个**实盘可用**的 15m 骨架(Tick→15 分钟线合成、
  时段与待发信号处理)——在那之前,`approved/ → 实盘`这条路没有可用的组合。
- **运行时装载前的「晋级门槛通过」闸门**(Issue [#37](https://github.com/laiyinyizao007/invest/issues/37))
  ——这是让 `approved/` 在实盘上真正安全的下一道门。
  按当前证据(门槛 9 条一项未过),加上这道闸门会让 `approved/` 暂时**无法**用于实盘,
  这恰恰是正确的方向,但它是独立的一步。
- **运行侧缺研究侧的两道闸**:「受信目录与仓库提交一致」(`verify_trusted_tree`)与
  「按路径的导入裁决」(`repo_guard`)。研究侧有,运行侧没有:上一轮候选残留在
  `strategies/` 里的辅助模块,在运行侧是可达的。补它需要把研究侧模块引到交易进程,
  与边界声明冲突,需单独设计。
- 批准账本无限增长(本 ADR 不做清理)。
- 运行时**途中**的风控底线持续核对(见「已知取舍」第 1 条)。
- **`risk/approved_guard.py` 登记进宪法的 `CALL_SITE_REGISTRY`**(见「已知取舍」第 7 条):
  等它进了主干、有「合规形态」的历史版本之后再登记。
- 4c 的「容器隔离放开」需另行评审,不在本 Issue 范围。
