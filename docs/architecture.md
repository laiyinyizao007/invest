# 架构总图:分层、状态归属、隔离机制与已知局限台账

> 本页是 `PROJECTWIKI.md` §2 的补充:wiki §2.1 画"数据怎么流",这里画**代码怎么分层、磁盘状态归谁、哪些东西不能碰、哪些局限还没解决**。
> 事实来源:2026-11-11 的架构审视(import 图逐文件核对)。目标架构与规划位见 `docs/roadmap.md`。

## 1. 分层与允许的 import 方向

```mermaid
flowchart TB
  subgraph core[核心包:单向依赖,无循环]
    RISK[risk/<br/>纯逻辑:账户熔断、下单策略、晋级门槛、准入、持仓方向]
    STRAT[strategies/<br/>策略与品种表]
    TRADING[trading/<br/>运行时、守卫规则、优雅登出]
    NOTIFY[notify/<br/>Webhook 通知与脱敏]
  end
  SCRIPTS[scripts/<br/>入口与工具:运行、演练、取证、研究、数据]
  STRAT --> RISK
  TRADING --> RISK
  TRADING --> STRAT
  SCRIPTS --> TRADING
  SCRIPTS --> RISK
  SCRIPTS --> STRAT
  SCRIPTS --> NOTIFY
```

规则:**只向下依赖**——`risk` 不依赖任何同级以上的包;`trading` 可用 `risk`、`strategies`;`scripts` 可用全部;核心包**不得** import `scripts/`。
现状(2026-11-11 核对):核心包符合,无循环、无反向依赖。

**已知违例(都在 `scripts/` 内,不影响实盘路径)**:

| 违例 | 位置 | 处置 |
|---|---|---|
| 脚本把另一个脚本当库用 | `broker_test`→`penetration_check`→`collect_reject_texts`(`Collector`/`close_engine`/`check_positions`/`in_trading_session`/`redact`/`check_target`) | 迁到共享包,见 `docs/roadmap.md` §2.6「ctp_tools 共享层」 |
| 脱敏实现三份 | `notify/webhook.py`(最短 4 位)、`penetration_check.redact`(最短 3 位)、`broker_evidence._redact`(同前) | 统一出处,见路线图 |
| `TEMP_DIR` 隔离与 `.env` 加载复制粘贴 | `drill.py`、`broker_test.py`(后者有 `ISOLATED` 自检,前者没有);`load_dotenv` 散在 7 个入口 | 同上 |
| 测试靠 `sys.path.insert(0, 'scripts')` | 约 25 个测试文件 | 改为 `pyproject.toml`/`conftest.py` 的 `pythonpath` |
| 交易时段定义 4 处且语义各异 | `strategies/trend_atr_strategy.py::ENTRY_SESSIONS`、`collect_reject_texts.py`、`drill.py`、`risk/account_guard.py`(`hour >= 20`) | 等 SimNow 实测后并入交易日历(路线图规划位 CAL) |

## 2. 磁盘状态归属表

| 路径 | 谁写 | 谁读 | 谁**不得**碰 |
|---|---|---|---|
| `data/state/account_guard.json` | `trading/guard_rule`(实盘/SimNow 运行时)、`scripts/unlock_account.py`(解锁,运行中拒绝) | 运行时启动恢复 | 演练、取证、测试(它们必须用隔离目录) |
| `data/state/pause_trading.flag` | `scripts/pause_trading.py` | 守卫规则每次 `check_allowed` | — |
| `data/state/promotion_approval.json` | `scripts/record_promotion.py`(仅晋级门槛全过) | `scripts/run_headless.py` 连接生产 API 前 | 任何自动流程不得代写 |
| `data/state/heartbeat` | 运行时(无头) | 容器健康检查 | — |
| `.vntrader/` | vn.py 引擎、`bootstrap.py` 建目录 | 回测、CTA | — |
| `.vntrader_drill/` | `scripts/drill.py` | 演练自身 | 实盘 |
| `.vntrader_brokertest/` | `scripts/broker_test.py` | 取证自身 | 实盘 |
| `data/broker_test/<时间戳>/` | `scripts/broker_test.py`、`broker_report.py` | 报告生成 | 不入库(含测试账号);国信模板同样不入库 |
| `docs/simnow-drills.md`、`docs/reject-texts-simnow.md`、`docs/unattended-log.md` | `drill.py`/`collect_reject_texts.py`/`unattended_report.py` 按标记区重写 | 人读 | 手工改标记区内的内容;`simnow-drills.md` **尚不存在**——演练还没实跑,首次运行时由脚本生成 |
| `data/`(行情/CSV/derived)、`runtime/` | 数据脚本、运行时 | 研究与回测 | 不入库 |

## 3. 隔离机制(为什么演练/取证不会污染实盘)

1. **`TEMP_DIR` 改写**:`drill.py`、`broker_test.py` 在导入 vn.py 日志模块**之前**把 `vnpy.trader.utility.TEMP_DIR` 指向各自目录,日志与引擎临时文件随之隔离。
   导入顺序是硬约束:`bootstrap` → 改写 `TEMP_DIR` → 其余 vn.py 导入。`broker_test` 带 `ISOLATED` 自检并有"全新进程"测试;`drill` 暂无自检(路线图登记)。
2. **`state_file` 重定向**:`rule.update_setting({"state_file": ...})` 让熔断/硬锁定状态落到隔离目录,不写真实 `data/state/account_guard.json`。
3. **环境门禁**:`broker_test`/`penetration_check` 只认 `CTP_ENVIRONMENT=测试`、**无条件拒绝生产 API**;`drill`/`collect_reject_texts` 用 `--confirm-simnow` 并校验 SimNow 地址。
4. **晋级闸门**:生产 API 需要 `promotion_approval.json`,只在晋级门槛全部通过时由 `record_promotion.py` 写出。

## 4. 已知局限台账(逐条链接回 ADR)

| 局限 | 来源 | 状态 | 触发条件 / 去向 |
|---|---|---|---|
| **净持仓(`Direction.NET`)方向不明** | `adr/20261109-account-risk-lock.md` | **已做 fail-closed**:对账/漂移检测拒绝启动或告警,紧急平仓不猜方向直接升级人工(`risk/positions.py`);**局限**:空仓启动证明不了账户形态,失败的紧急平仓不自动恢复(见 ADR,已登记路线图) | 账户是否真会回报 NET **未实测**——SimNow/国信测试环境验证后在 ADR 记录结论;若会出现再做真正的净持仓支持 |
| 交易日按周末顺延,不处理法定节假日 | `adr/20261109-account-risk-lock.md` | 未解决 | 交易日历(路线图规划位 CAL) |
| 回撤口径含浮动盈亏,出入金会被当成盈亏 | 同上 | 未解决 | 运维约定:出入金前后先停机 |
| 废单文本/持仓回报时序/撤单失败/断线重连/夜盘跨日未在真实 CTP 验证 | `adr/20261109-deployment-and-live-mode.md` §6 | 待 SimNow 实测 | `docs/runbook-simnow.md` 第 5 节 |
| 主力换月需人工更新 `LIVE_VT_SYMBOL` | 同上 | 未解决 | 换月移仓(roadmap) |
| 晋级门槛夏普下界在现有数据上几乎不可能通过 | `adr/20261110-baseline-15m.md` | 事先承认 | 更长样本外/多品种/小额实盘记录,**不放宽门槛** |
| 单品种、一套复权方式、成交模型无排队/盘口冲击 | `adr/20261110-baseline-15m.md`、`adr/20261110-walk-forward-optimizer.md` | 事先承认 | 多品种数据(需补购郑商所数据) |
| `parameter_on_plateau` 由调用方提供 | `adr/20261111-discipline-enforcement.md` | 人工判断 | 参数平原卷积(roadmap §2.6) |
| 别的终端下的单是否以 reference 为空回报**未实测** | 同上(codex H1) | 待实测 | runbook 第 5 节第 11 项;周期性 `ReqQryOrder` 对账(roadmap §2.6) |
| 非策略委托仅进程存活期间检测;算法单/价差子单 reference 前缀不同 | 同上 | 未解决 | 无头启用算法单时扩白名单(roadmap §2.6) |
| 准入文件防误操作、不防恶意 | 同上 | 设计取舍 | — |
| 5.2 批量撤单、2.1 全自动断网演练未做;证据图是渲染图非界面截图 | `adr/20261111-broker-test-compliance.md` | 待国信确认 | roadmap §2.6;国信书面确认(`docs/live-readiness.md` C3) |
