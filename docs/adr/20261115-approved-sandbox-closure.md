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

### 15. 所有闸门在**exec 之前**绑成局部引用,装载前后逐一核对(第 2、3 轮 codex 复核)

第 2 轮 codex 实测出一条比第 1 轮更深的洞:

> 一次**被拒**的候选可以把全部闸门永久毒化,同进程第二次装载完全裸奔。
> 候选 #1 一行就关掉了五道闸:`check_protected_state`、`snapshot_protected_state`、
> `screen_source`、`check_class`、`check_params`,外加账本读取。
> 载入 #2(真正的攻击载荷):**LOADED** ——第四层死了,300 元预算被永久换掉。

第 3 轮又实测出**更彻底**的一条(「换属性 → 调用 → 复原」):
候选把 `check_protected_state` 换成「**被调用时把原函数装回去**并返回 `[]`」的假函数——
指纹结论恒为空,而事后身份核对看到的是「真函数」→ 两条检查**同时**被骗过,装载成功。

修法三条一起上:
1. **闸门函数在 exec 之前**绑成局部引用(`snapshot_protected_state = const.snapshot_protected_state` …),
   候选的代码在另一个 module dict 里,改不到这些引用;
   装载时调的始终是「导入时那个真函数」,不是「调用时模块上的那个属性」。
2. 装载**前后**逐一核对「模块上的属性还是不是那个真函数」
   (`_gates_intact()`;清单是**不可变 tuple**,并用**默认参数**绑死——
   否则候选一句 `_g._CANONICAL_GATES = {...}` 就把它换掉了)。
3. **静态层先封**:新开 `BUILTIN_PROTECTED_GATE_ATTRS`(闸门函数名),
   候选只要写这些名字(或 `setattr(obj, "名字", …)`)就在 exec **之前**被拒。

### 16. 被投毒之后,本进程不再允许装载

发现闸门或受保护对象被改写 → 进程标记为**已中毒** → 之后任何装载一律拒绝。
理由与 ADR「已知取舍」第 3 条同源:被拒的候选可能已经改坏了进程,
留着这个进程继续装载下一个候选,等于把下一个候选交给一个被拆了防线的进程。
运行时本来就是「一进程一次装载、失败即退出」,这里把那条约定**变成代码强制**。

⚠️ **这条是症状的补丁,不是根因的补丁**:中毒标记只在「篡改被**发现**」时才置位。
下面「已知取舍」第 8 条说明了什么情况下篡改**发现不了**。

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

### 19. 候选的 `exec` 在**独立线程**里跑(第 4 轮 codex 实测的 P13)

在同线程里跑,候选可以用
`getattr(sys._getframe(1), 'f_' + 'locals')['check_protected_state'] = fake`
**直接改装载器的局部变量**(Python 3.13 的 PEP 667 写穿)——
连「exec 之前绑好的局部引用」都拦不住,第四层能被**完全中和**。

独立线程里 `sys._getframe(1)` 拿到的是**那个线程**的帧,不是装载器的帧:
主线程的局部变量够不着(第 4 轮 codex 实测:线程里的候选改不到主线程的局部变量)。

线程**不是**安全边界:候选仍然能改写**共享的**模块状态(例如 `risk.risk_guard`),
那一路由第四层的指纹比对兜着。线程隔离消灭的是「够到装载器自身」那一类
(帧劫持、闭包单元、`__code__` 改写)。

### 20. 三道**互相独立**的记录:批准 / 已收菜 / 晋级通过

第 4 轮 codex 指出两处「人能绕过机器承诺的底线」:

1. **收菜这道关口可以被人工 `cp` 绕过**——运行时只查「sha256 在账本里」,
   不查「这份文件是怎么进 `approved/` 的」。
   → 修法:收菜成功时写一条**收菜记录**(`data/research-control/collected.json`),
   运行时要求这条记录存在。人工 `cp` 就绕不过收菜(收菜时会把源码打给人看、
   并要求粘贴完整哈希)。
   批准账本的写入口仍然**唯一**(`research_sandbox.record_approval`,有 AST 测试锁住);
   收菜记录是**另一个文件**,写入口也只有收菜脚本一个。

2. **人工审阅挡不住「这策略没有统计优势」**——那是机器门槛的事。
   → 修法:装载时要求**晋级门槛通过记录**,且该记录里的 `sha256` 与要跑的那份字节**相同**
   (`scripts/record_promotion.py --sha256 …` 写入)。

**今天没有任何候选通过过晋级门槛** → 这道门实际上把「从 `approved/` 装载」这条路**关着**。
这是刻意的:第 4 轮 codex 测出「宪法风控底线可被已批准的候选用 `setattr`/`getattr`/`vars`
绕过」(H1),而把这条路关着,那个洞就**到不了实盘**——等 #37 落地、
以及运行侧的能力隔离做完,再把它打开。

### 21. 静态层把闸门函数名的**所有**写入形态都拒掉

第 4 轮 codex 实测:只挡 `x.attr = v` 会被绕开——`AnnAssign`、元组解包、`AugAssign`、
`del x.attr`、`setattr(obj, "名字", …)`、`delattr`、`vars(obj)["名字"] = …` 全都过。
修法:`_gate_attr_violations()` 一次把这七种形态都收进来。

### 22. 准入门与装载门是**两份独立的记录**(Issue #37)

两道门问的不是一件事:

| 记录 | 问的问题 | 谁消费 | 绑定什么 |
|---|---|---|---|
| `data/state/promotion_approval.json` | 「这个账号/这套系统能不能连生产 API」 | `run_headless.check_live_admission` | 策略名 + 批准人 + 指标 |
| `data/state/approved_promotion.json` | 「这份**代码**配不配被装载执行」 | `risk/approved_guard` | **品种 + 字节 + 那一次评估** |

共用一份记录会有两个毛病:①「过了一次门槛」同时满足两件事;②没法单独撤销其中一件。
拆开之后,装载门那份额外绑定三样东西:

- **`key`**(品种,如 C15):同一份代码换个品种不通用——那份通过授权的是「它被评估过的那个品种」。
  写入时**从那次评估的结果文件里取**(`data/research/<run_id>.json` 的 `key`),
  人手填的 `--key` 与评估结果不一致时**拒绝**;
- **`sha256`**(字节):换个文件不能蹭别人的通过记录;
- **`run_id`**(哪一次评估):数据一更新,同一次通过的含义就变了,记下 run_id 才追得回来。

装载时四样都要对上:记录存在 → `gate_failures == []` → sha256 与要跑的字节一致 →
key 与运行时声明的品种(可选 `--approved-key`)一致。

**今天没有任何候选通过过晋级门槛 → 这份记录不存在 → `approved/` 装载这条路是关着的。**
这是刻意的:第 4 轮 codex 测出的 H1(宪法风控底线可被已批准的候选用 `setattr`/`getattr`/`vars`
绕过)因此到不了实盘;要打开这条路,先做完 §20 的能力隔离与 #36(15 分钟实盘骨架)。

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
   现在用的是规范的调用形状 + 事后身份核对(§15);等这个文件进了主干、
   有了「合规形态」的历史版本之后再登记(见「遗留问题」)。
   ⚠️ 缺的那一维只是「跨历史版本的引用次数只增不减」;**形态维度已经由
   `check_executor_completeness` 覆盖**——实测把第四层删掉 / 改成自己跟自己比 / 丢弃结论,
   三种改法守卫都报错(第 3 轮 codex 实测)。

8. **在运行侧(同进程、完整真实内建)执行任意 Python 代码,做不到安全**(第 3、4 轮 codex 实测,
   本 ADR 最重要的保留意见)。上面 §15 那族修法(提前绑定 + 事后核对 + 静态层封名字)
   在「候选只改模块属性」这个威胁模型下成立;在「候选是任意 Python 代码」这个**真实**模型下**不成立**:
   - 候选可以用 `getattr(sys._getframe(1), 'f_' + 'locals')['check'] = fake`
     **直接改装载器的局部变量**(Python 3.13 的 PEP 667 写穿)。
     ⚠️ 第 4 轮 codex 实测的细节:**字面写法** `sys._getframe(1).f_locals[...]`
     会被静态层挡住(「禁止引用 f_locals」),**藏名字**的写法才复现得出来——
     本 ADR 早先给的例子是字面写法,方向对、例子错,已按实测更正;
     → §19 的**线程隔离**让这一整类够不到主线程的帧(实测:线程里的候选改不到主线程的局部变量);
   - 候选可以改写函数对象自身(`__code__`/`__defaults__`),而身份核对只看对象的**身份**,
     不看它的内容——这一条**线程隔离挡不住**,只能靠「不在同进程里跑任意代码」;
   - 静态层看不见 `setattr(obj, "名字", …)` 这种**按字符串**的写法:
     §21 已把这类形态补齐(AnnAssign / 元组解包 / AugAssign / del / setattr / delattr / vars()),
     但**按字符串**的写法天然可以继续藏(比如把名字拼起来)。

   → **真正的边界是「人工审批 + 隔离」**,而**运行侧至今没有隔离**:
   研究侧有受限内建(`safe_builtins`:拿掉 `setattr`/`getattr`/`eval`/`exec`/`open`/`sys`/`importlib`)
   + 容器只读根文件系统 + 按路径的导入裁决;运行侧是**完整真实内建、同一进程、还持着 CTP 凭据**。
   本 PR 把能封的都封了(§15 的三条 + §19 线程隔离 + §21 静态层 + 中毒标记 + fail-closed),
   但**不宣称「运行侧已安全」**。而且——
   **§20 把「从 `approved/` 装载」这条路**默认关着**(要求晋级门槛通过记录,
   今天没有任何候选通过过)——所以 H1 那个洞**到不了实盘**;
   晋级门本身已在本 PR 里做完(§22),但**能力隔离还没做**,所以这条路仍然关着。
   终局修法有两条,都还没做:
   ① 把候选的**执行**放进子进程/受限环境(研究侧的做法),只把「校验结论」带回主进程;
   ② 给运行时装载也套上受限内建(需要先确认候选的正当需求不依赖那些能力)。

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
- **运行时装载的能力隔离**(见「已知取舍」第 8 条):这是让 `approved/` 在实盘上真正安全的
  结构性前提——要么把候选的执行挪进子进程,要么给运行时装载套上受限内建。
- 批准账本无限增长(本 ADR 不做清理)。
- 运行时**途中**的风控底线持续核对(见「已知取舍」第 1 条)。
- **`risk/approved_guard.py` 登记进宪法的 `CALL_SITE_REGISTRY`**(见「已知取舍」第 7 条):
  等它进了主干、有「合规形态」的历史版本之后再登记。
- 4c 的「容器隔离放开」需另行评审,不在本 Issue 范围。
