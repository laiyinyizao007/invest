# ADR: 纪律机制补缺——解锁问责、非策略委托检测、实盘准入检查

- 状态:已采纳
- 日期:2026-11-11
- 跟踪:Issue [#6](https://github.com/laiyinyizao007/invest/issues/6)
- 依据:`docs/期货交易复盘与人性反思-要点梳理.md`(「飘了被教育」链条、人工干预次数 = 0、没有样本外优势不得上实盘)
- 实现:`risk/account_guard.py`、`scripts/unlock_account.py`、`trading/guard_rule.py`、`trading/runtime.py`、
  `risk/live_admission.py`、`scripts/record_promotion.py`、`scripts/run_headless.py`、`scripts/unattended_report.py`

## 背景
账户级风控(双层熔断、废单分类、看门狗、对账)已经把"系统自己出错"的路堵住,但研究资料反复强调的另一类风险是
**人**:亏损后手痒、嫌赚少而手动干预、被熔断后立刻解锁"翻本"。盘点代码发现三个缺口:

1. `AccountGuard.unlock_hard()` 把 `peak_equity` 清零——**每次人工解锁都重新获得完整的 2000 元预算**,
   而解锁脚本没有冷却、没有记录,等于"死线可以被无限次重置"。
2. "人工干预次数 = 0"是模拟盘验收的硬条件,但除了启动对账,运行中没有任何检测。
3. `risk/promotion_gate.py::check_gate` 没有任何运行时消费者,"策略没通过样本外门槛不得上实盘"只存在于文档里。

## 决策

### 1. 解锁问责(不改变"解锁后峰值重置"的语义,只加问责)
- 状态文件新增 `hard_locked_at`、`unlock_history`(最近 20 条:解锁时间、原锁定时间与原因、是否强制)。读旧状态文件缺字段取默认值。
- **冷却期**:硬锁定发生的交易日内拒绝解锁(夜盘按下一交易日算),退出码 3。理由:研究资料的「无感」要求之一就是触发熔断后关机、隔夜冷静。
- `--force` 可越过冷却,但必须**交互输入 `FORCE`**,且**不能与 `--yes` 同用**(脚本化场景不允许越过);强制解锁在历史里标记 `forced`。
- 解锁时打印最近几次解锁记录,让操作者看见"这是第几次"。
- **触发时间未知**(旧状态文件、状态文件损坏)时无法证明已过冷却期,**按冷却期内处理**——必须 `--force`;否则"弄坏状态文件"就成了绕过冷却的办法(codex 审查 M1)。
- **运行时仍在运行时拒绝解锁**(退出码 4):运行时每次权益变化/每 60 秒用内存态整体覆盖状态文件,此时解锁会被回写成"仍硬锁定"且历史丢失;以心跳文件(`data/state/heartbeat`,10 秒刷新)是否在 60 秒内判断(codex 审查 H2)。
- 写盘失败时内存状态回滚,与磁盘一致;stdin 关闭(EOF)按"未确认"处理。
- 周五夜盘触发的硬锁定,"下一个交易日"按 `trading_day` 规则要到下周一夜盘才算过冷却;`--force` 兜底,不是永久锁定。

### 2. 非策略委托检测(只告警 + 计数,不锁定、不拦截)
- 本系统发出的委托:CTA 策略单 `reference` 以 `CtaStrategy_` 开头(`vnpy_ctastrategy/engine.py:307`),紧急平仓为 `FLATTEN_REF`。
  vnpy_ctp 只在 `send_order` 返回时那次事件里带 `reference`,之后的 CTP 回报 `reference` 为空;**别的终端下的单只会以 reference 为空的回报出现**。
- 判定规则(`AccountGuardRule._track_manual_order`):
  - **启动对账通过后才开始计数**(`TradingRuntime.start()` 末尾调用 `arm_manual_detection()`),否则 CTP 登录后回放的当日历史委托会被误认成人工下单;
  - 首次出现且 reference 为空的委托先挂起 3 秒(`MANUAL_CONFIRM_DELAY`),期间若见到带策略标记的事件则放行——CTP 回报线程可能抢在 `send_order` 的事件之前到达;
  - 已见过的委托号不再判定(断线重连回放不重复计数)。
- 动作:`ERROR` 告警「检测到非策略委托」+ 按交易日计数写入状态文件 `manual_orders`;`unattended_report.py` 把该告警记为**严重事件**,当天不算干净、连续无人值守天数重新计。
- **被否方案:检测到就锁定/拦截。** 理由:人工下的可能恰恰是离场单,锁定与「平仓永远放行」原则冲突;而且误判(如时序竞态)的代价是锁死账户。告警+验收作废已足以把"人工干预"变成可见、可追责的事件。
- 图形界面(`run_gui.py`)不 arm 检测:它本来就是人工操作入口,不属于无人值守路径。

### 3. 实盘准入检查
- `CTP_ENVIRONMENT` 严格校验:strip 后只允许「实盘」「测试」,其余写法(`live`、`PROD`、带空格)直接配置错误(退出码 2),不会被悄悄当成非生产环境绕过准入(codex 审查 M2)。
  「测试」环境不看地址:看穿式测试版 API 连生产前置在 CTP 层面本就无法握手,因此不再叠加测试地址白名单。
- `risk/live_admission.py::check_live_admission`:目标是**生产 API**(`CTP_ENVIRONMENT=实盘` 且交易前置不是 SimNow 白名单地址)时,
  `scripts/run_headless.py` 要求存在 `data/state/promotion_approval.json`,否则退出码 2、拒绝启动(`--check` 同样检查,便于提前暴露)。
- 批准文件只能由 `scripts/record_promotion.py` 写出:读候选指标 JSON → `check_gate` → **仅当失败列表为空**才写,记录策略名、批准人、时间与指标。
- SimNow 与期货公司看穿式测试环境(`CTP_ENVIRONMENT=测试`)豁免——它们是用来验证管道的。
- 当前两条基线(日线、15 分钟)都未通过门槛,所以此刻任何生产连接都会被拒绝,这是预期行为。

## 已知局限
- 准入文件是**防误操作**的闸门,不是防恶意:有写权限的人可以手写批准文件。
- `CandidateMetrics.parameter_on_plateau` 仍由调用方提供,仓库里没有代码计算它(参数平原卷积属规划位,见路线图);在计算落地前该项依赖人工判断。
- **检测前提未实测(codex 审查 H1)**:"别的终端下的单会以 reference 为空的 `onRtnOrder` 回报出现"是本 ADR 的假设;codex 审阅 vnpy_ctp 源码后认为私有流只推本会话委托、网关也从不调用 `ReqQryOrder`,外部终端的单可能根本收不到。两种说法都没有在 SimNow 上验证过(runbook 第 5 节第 11 项)。**若实测收不到,则本机制只覆盖同会话内的干预(GUI/算法模块),外部终端的人工操作只能靠启动对账与持仓漂移发现**,届时需要登记路线图:周期性 `ReqQryOrder`(遵守 CTP 查询流控)与本会话委托对账。
- 已计数的非策略委托号单独保存(`_manual_counted`),不随已见委托号淘汰,避免断线回放重复计数。
- 升级 vnpy_ctp 后需用 SimNow 手动下一单复验 reference 行为。
- 无头进程不加载算法单/价差应用(`vnpy_algotrading`/`vnpy_spreadtrading` 只挂在 GUI);若将来在无头路径启用它们,其子单 reference 不是 `CtaStrategy_` 前缀,会被判为非策略委托,届时需扩展白名单。
- 非策略委托只在**当前进程存活期间**检测;进程停机期间发生的人工操作只能靠下次启动的对账发现。
- 手机 App 等别的终端下的单是否一定表现为 reference 为空,尚未在 SimNow 实测(登记进 `docs/runbook-simnow.md` 第 5 节待办)。

## 验证
`tests/test_discipline.py`(冷却/强制/历史/旧状态兼容、五种检测时序)、`tests/test_live_admission.py`(准入四种组合、`record_promotion` 对负面基线拒绝写文件)、`tests/test_unattended_report.py`(证据表把非策略委托记为严重事件)。
