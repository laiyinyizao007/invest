# PROJECTWIKI.md

> 项目知识库(单一可信源),与 `README.md`(怎么用)、`docs/`(ADR / 手册 / 路线图)互链。
> **拆分说明**(2026-11-11):目标架构与路线图 → `docs/roadmap.md`;研究方法论映射 → `docs/methodology-mapping.md`;模块详细文档 → `docs/modules.md`。
> **当前状态**(测试数与覆盖率以 `CHANGELOG.md` 与测试输出为准,不在此处写死):
> - 工程基线(原 Phase 0–5)代码完成并通过离线端到端测试;SimNow 上主机端与 Docker 容器内均已跑通真实 CTP 启动管道。
>   **2026-11-12 夜盘:`scripts/drill.py` 六个演练场景全部通过**(夜盘归属、成交回报延迟、行情停更、**真实切断网络**的断线与自动重连、当日熔断、累计死线硬锁定),见 `docs/simnow-drills.md`。
> - 路线图当前处于**阶段一(SimNow 验收闭环)**,**尚未退出**:演练六场景已通过;行情录制已在交易时段验收并加了「已连接却不订阅」的卡住检测;通知器有落盘待补发队列,Telegram 送达已验证。未完成:连续 ≥5 个交易日无人值守、行情录制的 DSH 重启实测、国信测试环境、废单文本补采。**两个未解决的问题**:退出登出(`logout_ctp`)在重连后出现过一次原生段错误(待复现);`latency` 演练在行情清淡时 30 秒未成交(不稳定)。
> - **策略结论为负面**:日线与 15 分钟两条基准都没有发现可延续的优势(见 ADR),当前策略只用于**管道测试**;15 分钟基准的测试段(2024-01 ~ 2026-06)已被使用一次,
>   不能再用于新候选的最终确认,新候选需要 2026-06 之后的分钟线或模拟盘/小额实盘记录。
> - 连接**生产 API** 需要实盘准入批准(晋级门槛全部通过才会写出,见 `docs/adr/20261111-discipline-enforcement.md`);穿透式认证与实盘准入是人工步骤,见 `docs/live-readiness.md`。
> - **研究沙盒与毕昇外层(已提前部署,偏离原定时序,见 ADR `20261112-research-sandbox-groundwork`)**:沙盒 CLI + HTTP 网关 + 容器隔离(内部网络无外网、只读根文件系统、控制目录只读)、候选声明 `param_grid` 的服务端选参 + 晋级门槛评估(选参窗口与验证窗口不重叠)、滚动 24 小时最多提交 5 个候选的服务端迭代上限、确定性中文报告、先看代码再批准均已实现;毕昇本机部署(仅 127.0.0.1),工作流 v3 已建,**需要 LLM 的节点因模型额度用完待联调**;无人值守的连续迭代(4c)未放开。
> 跟踪 Issue:[#1](https://github.com/laiyinyizao007/invest/issues/1)、Batch B [#12](https://github.com/laiyinyizao007/invest/issues/12)、行情录制与增量副线 [#13](https://github.com/laiyinyizao007/invest/issues/13)
> 阶段一期间已锁定行情基础设施副线：官方 `vnpy_datarecorder` 写入独立 `.vntrader_record/database.db`，DSH 只调度短任务 `ensure`/`watchdog`；dsh-cron 长任务 timeout 与 Windows 进程语义的事实、限制和待实测项见 [`docs/adr/20261111-market-recording-and-dsh-orchestration.md`](docs/adr/20261111-market-recording-and-dsh-orchestration.md)。

## 1. 项目概述
- **目标**:用代码自动交易国内商品期货,把"盈亏比纪律"固化为策略层风控,杜绝盘中手动干预。
- **账户约束**:本金 1 万元,单笔最大亏损 300 元(3%),固定 1 手;主测品种玉米(C),甲醇(MA)作对照。已下载 PP/L/EB 的数据,但 1 万元预算下它们的止损只有日波动的 15% 左右,不适合。
- **背景**:放弃自研底层,采用开源框架 vn.py(CTA 策略 + CTP 网关),先在 SimNow 模拟盘验证,再走期货公司穿透式认证上实盘。
- **范围**:数据下载与存储、CTA 策略与回测、策略层风控、SimNow 模拟盘、Webhook 监控、Docker 部署。
- **非目标**:高频交易;手机 app 自动化(adb)下单(无可靠成交回报、合规风险);美股(Alpaca)暂列后续备选。
- **运行环境**:开发 Windows 11 + Python 3.13;实盘与穿透测试运行在**另一台 Windows 机器**上直接用 Python 运行(不用 Docker,见 `docs/live-readiness.md` C2);Docker + x86_64 Linux 保留为备选。
  树莓派 5(ARM64)不可行:CTP 官方库只有 x86_64,`vnpy_ctp` 在 PyPI 上也只有 win_amd64 wheel 与源码包。
- **实盘切换需用户另行明确授权**;当前不连接任何实盘。
- **上手入口**:`README.md`(面向"这个项目在干嘛、怎么用");本文件是模块级的单一可信源,两者互链。


## 2. 架构设计

> 分层依赖图、磁盘状态归属表、隔离机制与**已知局限台账**见 [`docs/architecture.md`](docs/architecture.md)。

### 2.1 现状数据流(代码已实现;**各节点的真实验证程度见下表**,不要把"代码完成"读成"已验证")
```mermaid
flowchart LR
  SINA[(新浪行情 via AKShare)] --> FD[fetch_data.py]
  BUY[(购买的 DCE 分合约 1 分钟线)] --> IM[import_minute.py]
  IM --> DER[(derived: 复权主力连续<br/>1d / 15m / rolls)]
  BS[bootstrap.py] --> FD
  BS --> IM
  FD --> DB[(.vntrader/database.db)]
  FD --> CSV[(data/csv/*.csv)]
  DB --> BL[backtest_lib.py]
  DER --> BL
  BL --> OPT[optimize.py 日线基准]
  BL --> O15[optimize_15m.py 15 分钟基准]
  ST[trend_atr_strategy.py 日线] --> BL
  ST15[trend_atr_15m_strategy.py 15 分钟] --> BL
  RG[risk_guard.py] --> ST
  PG[promotion_gate.py] -. 晋级检查 .-> OPT
  PG -. 晋级检查 .-> O15
  RT[runtime.py] --> ST
  RT --> GR[guard_rule.py 账户风控]
  RT --> WH[webhook.py]
  RT --> CTP[CTP 网关 / SimNow]
  GUI[run_gui.py 图形界面] --> GR
  GUI --> CTP
  GR -. 拦截开仓/紧急平仓 .-> CTP
  SG[simnow_guard.py 地址白名单] --> DR[drill.py 实测演练]
  DR -. 独立状态目录 .-> CTP
  DR --> DRDOC[(docs/simnow-drills.md)]
  RT --> UR[unattended_report.py 每日证据]
  UR --> URDOC[(docs/unattended-log.md)]
  LOG[(vn.py 每日日志 + 心跳)] --> UR
  WH --> UR
```
| 节点 | 代码路径 | 状态 |
|---|---|---|
| FD | `scripts/fetch_data.py` | 完成 |
| BS | `scripts/bootstrap.py` | 完成 |
| IM / DER | `scripts/import_minute.py` → `C:\Dev\data\invest\derived\` | 完成(玉米实测交叉验证通过) |
| DB / CSV | 运行时生成,均已 `.gitignore` | 完成 |
| BL | `scripts/backtest_lib.py` | 完成 |
| OPT / O15 | `scripts/optimize.py`、`scripts/optimize_15m.py` | 完成(两者结论均为负面) |
| ST / ST15 | `strategies/trend_atr_strategy.py`、`trend_atr_15m_strategy.py`、`random_entry_strategy.py`、`instruments.py` | 完成 |
| RG / PG | `risk/risk_guard.py`、`risk/promotion_gate.py` | 完成 |
| RT / GR | `trading/runtime.py`、`trading/guard_rule.py`、`trading/safe_risk.py` | 完成(仅假网关离线测试;**未在真实 CTP 上验证**) |
| WH | `notify/webhook.py` | 完成 |
| CTP | `scripts/run_headless.py`(无头入口)、`Dockerfile`、`docker-compose.yml` | **部分**:主机端与容器内均跑通完整启动管道(2026-11-10);废单文本、撤单失败、断线重连、夜盘跨日**未在真实 CTP 验证** |
| GUI | `scripts/run_gui.py` | 完成(人工观察用,非无人值守) |
| SG / DR | `scripts/simnow_guard.py`、`scripts/drill.py` | 代码完成,**演练从未实跑**(`docs/simnow-drills.md` 尚不存在,首次实跑时由脚本生成);阶段一,见 §2.4 |
| UR | `scripts/unattended_report.py` | 完成(离线自检通过,真实 webhook 待配,probe 未通过;连续 5 天证据未攒齐);纯逻辑在 `evidence/logstats.py` |
| 状态页 | `dashboard/`(含 `dashboard/evolution.py`)、`scripts/status_server.py`、`requirements-dashboard.txt`、`tools/build_static_manifest.py` | **后端阶段①已实现并有测试**(2026-11-12/13):只读、仅回环、令牌鉴权(令牌在专属目录 `data/dashboard/`,不在交易侧目录树)、全 GET/HEAD、缺失/过期/损坏永不显示为正常。**前端未构建**(占位页)。**G8 双轮审查未通过**:第 1 轮外部专家因 Gemini 免费额度 429 未完成;第 2 轮 codex 首次审查的 12 条发现已全部处理,但其**复核**综合评分仅 6.0/10,又提出 4 条 High / 6 条 Medium 的新问题。其中日志路径的 4 项(增量缓存可被同长度改写欺骗、垃圾日志被判干净、日志未来 mtime、无换行尾行漏报)已修复(`fa7a5cd`),`__pycache__` 写入与令牌位置(白名单)、严格 UTF-8 与 NaN/Infinity、风控文件采样时间(交易侧新增 `sampled_at`,只供可观测)、令牌回退发布 fail-closed、研究结果结构校验、编码形式路径净化也已修改——**10 项均已修改;codex 评分走势 6.0/10 → 36.5/50 → 42.5/50 → 43.7/50 → 44.6/50 → 42.8/50 → 44.5/50 → **44.4/50**(第八次:指定的 3 项全部修好;新发现 research 扫描上限仍只落在 message 等 1 Medium + 3 Low 也已修),尚未第九次复核;令牌创建竞态写明威胁模型而非声称消除****。**在这些修复前,不应把该面板作为唯一的无人值守安全依据**——详见 `docs/plans/20261112-status-dashboard-plan.md`『codex 复核后的未修问题』。启动:`python scripts/status_server.py`;规划与契约见 `docs/web/README.md` |


### 2.2 目标架构与演进路线(已迁出)
- 详见 [`docs/roadmap.md`](docs/roadmap.md)(原 §2.2 目标分层架构、§2.4 差距与路线、§2.5 规划位登记表,编号沿用)。
- **一句话**:两层架构——**内层 = vn.py 流程引擎**(确定性内核:`trading/`、`risk/`、`strategies/`、研究脚本、`notify/`、Docker,即上图全部已实现部分);**外层 = Bisheng 流程编排**(AI 策略研究员,**规划位,零代码**)。边界铁律:外层只经研究沙盒 API 白名单端点交互;`promotion_gate` 是候选策略唯一闸门;LLM 产出只落 `pending_strategies/`,装载永远人工。
- 路线图四个阶段:**一** SimNow 验收闭环(当前)→ **二** 调度/换月/日历/真实费率 → **三** 月度滚动优化引擎化 + 研究沙盒 API(触发条件驱动,不排日期)→ **四** Bisheng 接入与 Alpha Factory(远期)。
- 每个阶段的验收都只是通道/能力验收,**不是策略准入**;实盘切换需用户另行明确授权。

### 2.3 研究方法论 → 系统映射(已迁出)
- 详见 [`docs/methodology-mapping.md`](docs/methodology-mapping.md):研究资料条目 → 状态 → 实现文件的映射表(34 行),以及研究文档自身的矛盾与证据强度——本项目的采纳口径。

## 3. 架构决策记录(ADR)
- 目录:`docs/adr/`,MADR 格式;各 ADR 的「已知局限」汇总与状态见 [`docs/architecture.md`](docs/architecture.md) §4
  - [`20261109-use-vnpy.md`](docs/adr/20261109-use-vnpy.md):采用 vn.py,不自研
  - [`20261109-china-timezone.md`](docs/adr/20261109-china-timezone.md):进程统一为中国时区
  - [`20261109-account-risk-budget.md`](docs/adr/20261109-account-risk-budget.md):1 万元 / 300 元预算,超预算则不交易,主测玉米
  - [`20261109-negative-backtest-result.md`](docs/adr/20261109-negative-backtest-result.md):如实记录"未发现优势"与方法学纪律
  - [`20261109-promotion-gate.md`](docs/adr/20261109-promotion-gate.md):策略晋级门槛(9 条;第 9 条 DSR 见 [`20261113-evolution-engine.md`](docs/adr/20261113-evolution-engine.md))
  - [`20261109-account-risk-lock.md`](docs/adr/20261109-account-risk-lock.md):账户级双层熔断、平仓永远放行、紧急平仓防重复下单
  - [`20261109-deployment-and-live-mode.md`](docs/adr/20261109-deployment-and-live-mode.md):实盘模式策略、运行时对账、通知、容器化
  - [`20261110-baseline-15m.md`](docs/adr/20261110-baseline-15m.md):15 分钟基准的事前方案、成绩单与"测试段已被使用"的约束
  - [`20261110-ctp-error-code-and-watchdog-policy.md`](docs/adr/20261110-ctp-error-code-and-watchdog-policy.md):错误码优先的废单三级分类、假死强撤开仓单、断线连接状态机与无积压补发证据
  - [`20261110-walk-forward-optimizer.md`](docs/adr/20261110-walk-forward-optimizer.md):Walk-Forward 滚动窗口方法学、测试段三层隔离护栏、预算弃单率口径与负面结果记录
  - [`20261110-bisheng-ai-researcher.md`](docs/adr/20261110-bisheng-ai-researcher.md):两层架构——外层 Bisheng AI 策略研究员经研究沙盒 API 与内层 vn.py 确定性内核隔离;LLM 产出只落 pending 目录、装载永远人工、外层可整体替换

  - [`20261111-discipline-enforcement.md`](docs/adr/20261111-discipline-enforcement.md):纪律机制补缺——解锁冷却与历史、非策略委托检测(只告警计数)、实盘准入检查(生产 API 必须有晋级门槛批准)
  - [`20261111-risk-floor-immutability.md`](docs/adr/20261111-risk-floor-immutability.md):风控底线不可优化(预算是约束不是变量)、明确不做清单、本地止损依赖进程存活的已接受风险


  - [`20261111-broker-test-compliance.md`](docs/adr/20261111-broker-test-compliance.md):国信程序化测试合规——阈值触达预警(走 Webhook、按指标节流)、手动暂停开仓(标志文件)、证据与报告
  - [`20261111-market-recording-and-dsh-orchestration.md`](docs/adr/20261111-market-recording-and-dsh-orchestration.md):行情录制采用官方 DataRecorder 与独立数据库；DSH 采用短任务 `ensure`/`watchdog` + wrapper，不直接承载长期前台录制

## 4. 设计决策 & 技术债务
> 只保留**未解决**的条目。已处理或重复的行已归档到 `docs/roadmap.md` §2.7;各条目的阶段归属见 `docs/roadmap.md` §2.4 末尾的去向表。
| 项 | 说明 | 优先级 |
|---|---|---|
| 分钟线历史很浅 | 新浪每个周期只返回最近约 1023 根(1 分钟≈5 个交易日,15 分钟≈2.5 个月),须每天/每周增量运行脚本积累 | 高 |
| **未在真实 CTP/SimNow 上验证** | 启动管道已在真实 SimNow(第一套)跑通一次(2026-11-10);但废单文本、持仓回报时序、撤单失败、断线重连、SHFE/INE 平今平昨、夜盘跨日、kill -9 重启、熔断演练都只在离线假网关上验证;须按 `docs/runbook-simnow.md` 第 5 节实测并回填 | **最高** |
| **演练脚本已就绪但未实跑** | `scripts/drill.py` 把第 5 节的第 3/4/6/7/8/9 项做成了可重复执行的场景,但**还没有在 SimNow 上真跑过**,结论仍为空 | **最高** |
| **飞书的"失败也返回 HTTP 200"** | 通知器只看 HTTP 状态码会把 `{"code":19001}` 当成发送成功 → 告警静默丢失。现已解析响应体,业务码非 0 一律算失败并重试;但**真实飞书 webhook 仍未验证过**(`.env` 的 `WEBHOOK_URL` 是空的) | **高** |
| 无人值守只跑了 1 天 | 第 6 节要求连续 ≥ 5 个交易日,目前证据表(`docs/unattended-log.md`)只有 2026-11-10 一天,且心跳已停 | 高 |
| 废单关键词表只覆盖 2 条实测文本 | 已在 SimNow 实测到"该交易席位未连接到交易所"(硬拒绝);越界价格、无持仓平仓、超大手数、资金不足四类因 SimNow 反复登录触发 4097 冷却尚未采到,见 `docs/reject-texts-simnow.md` | 高 |
| 图形界面不是运行方式 | `run_gui.py` 无对账、无看门狗、无 Webhook,只能人工观察;不得用于无人值守 | 中 |
| 主力合约换月需人工更新 | `LIVE_VT_SYMBOL` 指向具体合约,换月与持仓移仓未实现 | 高 |
| 本地止损依赖进程存活 | vn.py 停止单是引擎本地触发的、不是交易所委托,断线或进程退出时不执行;进程退出时若有持仓会发 FATAL,但无法替代交易所端条件单。实盘须跑在稳定机器上并有行情停更检测。**已接受的风险**,见 `docs/adr/20261111-risk-floor-immutability.md` | 高 |
| 交易日不处理法定节假日 | 长假前后的当日熔断边界需人工留意 | 中 |
| 复权数据历史较短 | 新浪分合约日线只到 2018-05;更早需交易所逐日合约数据(大连 2005 起、郑州 2010-08 起,约几千次请求) | 高 |
| 分钟线历史不足 | 已购买并导入(DCE 玉米分合约 1 分钟线,约 5.9 年);**只到 2026-06-15**,新候选需要更新数据 | 高 |
| **15 分钟基准的测试段已被使用一次** | 2024-01 ~ 2026-06 已被 `optimize_15m.py --test` 使用,再用于新候选会产生多重检验偏差;新候选只能用训练/验证段筛选,最终确认需要 2026-06 之后的分钟线或模拟盘记录 | **高** |
| 15 分钟夏普口径与日线不同 | vn.py 按**日历日**分组逐日盈亏、夜盘与次日日盘被拆成两条记录,年化因子固定 240(日线口径);15 分钟夏普**不可与日线夏普直接比大小**,只有收益率/净盈亏/回撤金额可靠(详见 ADR 的"事后补充") | 高 |
| 回测结论统计效力不足 | 日线选定参数在验证段 10 笔往返(复权数据重做);15 分钟基准测试段 91 笔往返,均低于 150 的门槛;结论只能是"无法证明优势" | 高 |
| 预算约束不了跳空 | 默认参数回测有 4 笔单笔亏损超 300 元,最大 700 元(未计成本) | 中 |
| 日线内高低点先后未知 | 移动止损按当根极值收紧、下一根生效,属日线固有近似 | 中 |
| 测试段"只看一次"登记仅覆盖 optimize*.py | 已由 `config/test_segment_usage.json` 持久化;但登记文件可被手工改写,约束依赖 git 历史可审计 | 低 |
| `DailyLossGuard` 对日线策略无意义 | 同一天几乎不会连亏两笔,仅对未来日内策略有效 | 低 |
| 回测成本是假设值 | 每边 1 tick + 3 元/手,实盘前须换成期货公司真实费率 | 高 |
| 5/15 分钟线只存 CSV | vn.py 无对应 `Interval`;回测时需自行加载,或由 1 分钟线合成 | 中 |
| 数据源 OHLC 偶有矛盾 | 已修复(取 OHLC 极值),实测 PP 2020-08-06、L 2007-07-31 各 1 行 | 低 |
| `turnover` 恒为 0 | 新浪不提供成交额 | 低 |
| 缺少自动化调度 | 增量下载目前需手动运行 | 中 |
| **`parameter_on_plateau` 无计算来源** | 晋级门槛第 8 条由调用方传入布尔值,仓库内没有代码计算它;计算落地前该项靠人工判断(见 `docs/roadmap.md` §2.6) | 高 |
| **非策略委托检测的前提未实测** | 假设"外部终端下的单会以 reference 为空的回报出现";codex 审阅源码认为外部会话的委托可能收不到事件。须按 runbook 第 5 节第 11 项在 SimNow 实测,两种结果都要记录(`docs/adr/20261111-discipline-enforcement.md`) | **高** |
| 实盘准入批准文件可被手写 | 它是防误操作的闸门,不防有写权限的人 | 低 |

## 5. 模块索引
> 每个模块一行;详细职责、约束与修复经过见 [`docs/modules.md`](docs/modules.md)(按下列路径查找小节)。纪律机制与穿透式监管工具的决策见 `docs/adr/20261111-*.md`。

**数据**
- `scripts/bootstrap.py`:所有脚本的统一启动入口(固定工作目录与中国时区,须先于 `vnpy.trader.database` 导入)。
- `scripts/fetch_data.py`:新浪行情下载→清洗→存 vn.py 数据库/CSV(分钟线只返回最近约 1023 根,须定期增量运行)。
- `scripts/record_market_data.py`、`scripts/record_market_data_wrapper.py`:官方 DataRecorder 行情录制与独立子进程生命周期管理；连行情前置，并受限地连交易前置（仅认证+查合约，发单/撤单/查资金/查持仓在代码层硬性禁止）。**验证程度**：SimNow 第二套 7x24 前置已验证认证/登录/合约/订阅/tick/优雅停止，但该环境行情时间不可信，正式库 0 行；SimNow 第一套前置交易时段录制已验收（2026-11-12 夜盘：Tick/1 分钟 K 线入正式库、数据自洽、自动重连正常），**DSH 重启实测与国信测试环境未验证；「连接成功但长时间不订阅」已加 `stalled` 检测与 watchdog 重启**，详见 runbook「验收记录」与 ADR 修订。
- `scripts/record_market_incremental.py`:DSH 调用的有界、可重入增量任务；通过锁、超时和结果状态文件保护定时补数。
- `docs/runbook-market-recording.md`:行情录制、增量任务、离线检查与生命周期验收手册。
- `scripts/import_minute.py`:购买的 DCE 分合约 1 分钟线 → 复权主力连续(日线/15 分钟/换月表,派生文件不入库)。
- `scripts/build_continuous.py`:新浪日线构造主力连续并存库,供运行时预热。

**策略与研究**
- `strategies/trend_atr_strategy.py`:日线趋势 + ATR 止损策略,含实盘模式(Tick→日线合成、信号次日发单、状态持久化)。
- `strategies/trend_atr_15m_strategy.py`、`intraday_demo_strategy.py`、`random_entry_strategy.py`、`instruments.py`:15 分钟基准、日内演示(仅研究/回测)、随机入场对照、品种参数。
- 研究沙盒(阶段四 4a/4b 的内层部分):`scripts/research_api.py`(CLI:`data`/`approve`/`review`/`backtest`/`gate`/`evaluate`/`results`)、`scripts/research_sandbox.py`(路径限制、静态筛查、环境白名单、批准账本)、`scripts/research_gate.py`(门槛评估与选参编排)、`scripts/research_grid.py`(候选参数网格寻优)、`scripts/research_config.py` + `config/research_sandbox.json`(全部可调参数,严格校验)、`scripts/research_gateway.py`(HTTP 网关,标准库实现;容器见 `docker-compose.research.yml`、`docker/research/`);纯逻辑:`risk/diagnosis.py`、`risk/plateau.py`、`risk/candidate_eval.py`、`risk/report.py`。威胁模型与限制见文件头及 ADR `20261112-research-sandbox-*`。毕昇工作流脚本在 `tools/bisheng/`。
- `notify/webhook.py`:非阻塞、带重试、去重、脱敏的 Webhook 通知(generic/feishu/telegram);**落盘待补发队列**(`WEBHOOK_OUTBOX`,默认 `data/state/webhook_outbox.jsonl`):发送最终失败的告警写入 JSONL,网络恢复后补发(【补发】前缀、保留原始时间、积压 INFO 合并成摘要),进程重启后继续。
- `scripts/segment_ledger.py`、`config/test_segment_usage.json`:测试段使用登记，`optimize.py`/`optimize_15m.py` 的 `--test` 先查后登记，重复使用即拒绝（无强制开关）。
- `scripts/backtest_lib.py`:回测公共库(统一成本、内存 K 线回放、异常哨兵)。
- `scripts/optimize.py`、`optimize_15m.py`、`walk_forward_optimizer.py`:日线/15 分钟网格与三段验证;Walk-Forward 滚动优化(测试段物理隔离)。

**风控(纯逻辑在 `risk/`,vn.py 集成在 `trading/`)**
- `risk/risk_guard.py`:单笔预算闸、只收紧的移动止损、策略层连亏熔断。
- `risk/promotion_gate.py`:策略晋级门槛(9 条,含 DSR ≥ 0.95),纯函数;`risk/dsr.py`、`risk/trial_ledger.py`:DSR 与试验次数账本(Issue #15)。
- `scripts/calc_ladder_eta.py`:梯子式量化的**汇报步长 η** 的标定脚本(阶段 3 前置,纯数据 + 纯统计,不碰交易接口)。η = 1.5 × SE(配对日盈亏差),下限 0.25;在**池化验证段**(2022-07-01 ~ 2023-12-31,n=367 交易日)上用分块自助(块长 5 日,2000 次)标定,SE 中位数 0.7297 → **η = 1.10 夏普**(已登记进 ADR,早于任何候选生成);同一方法在 W1 训练段上得 η = 1.010,相差 8%,说明不是单一窗口的偶然。η 是**汇报阈值**(阶梯何时更新汇报值),**不是晋级门槛**,与 DSR 0.95 互相独立。标定**没有**碰测试段——测试段(2024-01-01 起)全程未触碰。
- `risk/constitution.py` + `config/immutable_constitution.json`:不可变宪法(Issue #16)——候选能改什么、不能改什么的机器可校验清单。三层防线:提交时的静态 AST 筛查、执行前的运行时类校验、请求时的参数校验;清单文件只能追加(相对历史提交只增不减,由 `.github/workflows/constitution-guard.yml` 保证:`fetch-depth: 0` 拉全历史后逐个历史版本比对;仓库是浅克隆时该检查直接失败而不是跳过),文件缺失/非法即 fail-closed。三个子进程入口统一走 `research_api.import_candidate()`,一处不漏。G8:第 1 轮外部复核无 Critical/High;codex 已审 5 轮,第 4 轮(31/50)的 6 项发现逐条复现后修复并各配了会在修复前失败的测试;**第 5 轮已跑完**(`gpt-5.6-sol` 40.5/50 与另一轮独立复核 `MiniMax-M3` 33/50,两轮都未通过),阻塞项已逐条复现后修复并配了回归测试:①**框架的"按名 setter"绕过**(`self.update_setting(dict(risk_budget=1e12))` 能在三层防线全绿时把单笔风险预算从 300 改成 1e12——模板 `parameters` 里就有四个受保护参数),现整体禁用按名 setter 并拦受保护参数作关键字参数;②**内置保护的历史守卫形同虚设**(`BUILTIN_PROTECTED_NAMES` 是 BinOp,提取器只认字面量,删掉受保护成员后 CI 照样全绿),现改为常量表达式求值 + fail-closed,并把沙盒的三个 deny set 一并纳入;③隐藏子入口只复查了设置字典,`--random-runs`/`--prior-trials`/`--key`/`--segment`/`--file` 现在各自复查(其中 `--prior-trials` 为负会放松 DSR 门槛)。修复后补跑第 6 轮(独立复核 32/50,仍不通过),它又报出一条前两轮都没报出的活绕过 `__builtins__['setattr']`(静态层与沙盒层当时都返回空列表,底线照样从 300 被改成 1e12)——现已修(受禁属性名的裸名与字符串形式一律拒);同一轮还查出**宪法守卫在 CI 上收集即失败**(根 conftest 依赖 vnpy),守卫已抽成独立脚本 `tools/check_constitution_history.py`(只用标准库 + git),CI 直接跑它;三轮复核的发现均已逐条复现后修复并配了回归测试。第 7 轮(codex `gpt-5.6-sol` 与独立复核各一轮)结论不一致:gpt 指出前面封的是**已知写法**而不是**能力**——实测经异常回溯拿到 `frame.f_builtins.setattr`、以及 `unittest.mock.patch.object` 两条都放行(gpt 是对的);现已改为**执行时能力隔离**:候选执行时拿到的内建集合里没有 `setattr`/`getattr`/`eval`/`open` 这些,`__import__` 换成受限导入器,无论怎么绕都改不了实例属性。容器隔离与"运行前由人读过代码"仍是真正的边界。结果见 `CHANGELOG.md`。
- `risk/account_guard.py`:账户级双层熔断(当日 600 / 累计 2000)、原子落盘、解锁冷却与历史、非策略委托计数。
- `risk/order_policy.py`:CTP 错误码优先的废单三级分类与有限重试策略。
- `risk/live_admission.py`:生产 API 的实盘准入检查(需晋级门槛批准文件)。
- `trading/guard_rule.py`、`trading/safe_risk.py`:挂在 vnpy_riskmanager 发单链上的账户风控规则(拦开仓放平仓、废单分类、紧急平仓状态机、非策略委托检测)。

**运行时与运维**
- `trading/runtime.py`、`scripts/run_headless.py`:无头运行入口——启动对账、看门狗、连接状态机、退出码语义、准入检查。
- `scripts/unlock_account.py`、`scripts/record_promotion.py`、`scripts/pause_trading.py`:人工解锁(冷却/强制/历史)、实盘准入批准记录、手动暂停/恢复新开仓(标志文件,平仓放行)。
- `trading/graceful_exit.py`:退出前向交易/行情前置发 `ReqUserLogout`(vnpy_ctp 的 `close()` 只断开连接),供运行时与脚本共用。
- `notify/webhook.py`:非阻塞 Webhook 通知(通用/飞书/Telegram),发送前脱敏。
- `scripts/run_gui.py`:图形界面(人工观察用,含算法单/价差应用,不得用于无人值守)。

**验证与接入工具**
- `scripts/simnow_guard.py`:SimNow 前置机地址白名单(演练/采集脚本共用)。
- `scripts/drill.py`:SimNow 实测演练(第 5 节做成可重复执行的场景,状态与实盘隔离)。
- `scripts/collect_reject_texts.py`:采集 SimNow 真实废单文本,校准关键词表。
- `scripts/unattended_report.py`:无人值守每日证据表与验收清单、Webhook 连通性自检。
- `scripts/collect_terminal_info.py`、`scripts/penetration_check.py`:穿透式监管的终端信息采集与看穿式测试环境演练(脱敏报告)。
- `scripts/broker_test.py`、`scripts/broker_evidence.py`、`scripts/broker_report.py`:国信测试报告的取证运行器(18 项,证据渲染成图;每次运行向 `connection.json` 留痕实际连接的环境)、证据模型与渲染、docx 报告生成器(lxml 填国信原模板,不默认勾通过,不碰国信填写区)。
  取证运行器的硬约束:**发单前统一过空仓闸门**(有持仓一笔不发)、演示委托用唯一价格(不污染重复报单计数)、测试前置地址靠人工确认(脚本无法判别)、异常也清场(解除暂停标志、还原阈值)。
- 标记区重写约定(演练/采集/无人值守报告共用):见 `docs/modules.md`「标记区重写的共同约定」。

## 6. API 手册(脚本内部接口)
| 函数 | 签名 | 说明 |
|---|---|---|
| `fetch_with_retry` | `(func, **kwargs) -> DataFrame` | 指数退避重试,间隔 2s/4s/8s;`TypeError/KeyError/AttributeError` 立即抛出不重试 |
| `normalize` | `(raw, period) -> DataFrame` | 统一列名;分钟线时间前推一个周期;丢弃 NaN/非正价格;修复 OHLC 矛盾;去重排序 |
| `to_bars` | `(df, code, exchange, interval) -> list[BarData]` | 时间带 `DB_TZ`;`symbol=f"{code}0"`;`turnover=0` |
| `save_csv` | `(df, code, period) -> DataFrame` | 与旧 CSV 合并;先写 `.csv.tmp` 再 `os.replace` 原子替换 |
| `run_one` | `(code, exchange, period) -> None` | 单任务:下载→清洗→存储 |
| `main` | `() -> int` | 命令行入口,全部成功返回 0,否则 1 |

命令行参数:`--symbols`(PP/L/EB/C/MA,默认全部)、`--periods`(d/1/5/15,默认全部)。

`python scripts/optimize.py --symbol {C|MA} [--test]`:日线网格优化;加 `--test` 才查看测试段,且只应运行一次。

`python scripts/optimize_15m.py [--test]`:15 分钟基准(数据在 `C:\Dev\data\invest\derived\C_15m_adj.csv`);加 `--test` 才查看测试段,只应运行一次。

| 函数 | 签名 | 说明 |
|---|---|---|
| `load_bars_csv` | `(path, symbol, exchange, interval=MINUTE) -> list[BarData]` | 读 `import_minute.py` 输出的 K 线 CSV(时间戳为开始时间) |
| `run_engine_bars` | `(strategy_class, setting, key, bars, capital=10000, interval=MINUTE, cost_multiplier=1.0) -> BacktestingEngine` | 直接回放内存 K 线;滑点 × `cost_multiplier`;策略异常转 `RuntimeError` |
| `run_backtest_bars` | `(同上) -> (stats, df, trades)` | 上者 + 绩效统计 |

`python scripts/walk_forward_optimizer.py [--workers N] [--data 路径] [--output 路径]`:Walk-Forward 滚动优化;
无 `--test` 开关,任何越界窗口在 `_assert_pre_test` 直接 `ValueError`。

| 函数 | 签名 | 说明 |
|---|---|---|
| `build_windows` | `() -> list[Window]` | 3 个滚动窗口(训练 24 月/验证 6 月/步长 6 月),构造即过隔离断言 |
| `grid` | `() -> list[dict]` | fast/slow 二维 31 组,固定 `atr_mult=1.5`,无 `risk_budget` 键 |
| `rank` | `(rows) -> list[dict]` | fills≥100 门槛;夏普↓→回撤↓→fills↓→fast↑→slow↑ 确定性排序 |
| `_backtest_one` | `(data_file, params, start_iso, end_iso, cost_multiplier=1.0) -> dict` | 模块级可 pickle worker;含 `skipped_by_budget` 与 `budget_skip_rate` |
| `map_backtests` | `(data_file, jobs, workers, executor=None) -> list[dict]` | workers=1 串行;否则进程池;executor 仅供测试注入 |
| `run_window` | `(data_file, window, workers, executor=None) -> dict` | 训练网格→Top5→验证外推;不足 5 组标 `insufficient` |
| `summarize` | `(results) -> dict` | 第一名验证夏普均值/中位数、正夏普窗口占比、最差回撤、预算弃单率 |

## 7. 数据模型
```mermaid
flowchart LR
  RAW["新浪原始数据<br/>datetime/date, OHLC, volume, hold"] -->|normalize| STD["标准列<br/>datetime, open, high, low, close, volume, open_interest"]
  STD -->|to_bars| BAR["vn.py BarData<br/>symbol=PP0, exchange=DCE, interval=DAILY/MINUTE"]
  STD -->|save_csv| F["data/csv/PP_15m.csv"]
```
- **时间语义**:新浪分钟线时间戳是 K 线**结束**时间,入库统一为**开始**时间(vn.py 约定);日线为交易日 00:00。
- 合约:`PP0` / `L0` / `EB0`(新浪主力连续),交易所 `DCE`。
- CSV 日期格式固定为 `%Y-%m-%d %H:%M:%S`。

**购买数据与派生产物(仓库之外,`C:\Dev\data\invest\`)**
```mermaid
flowchart LR
  RAW["DCE 分合约 1 分钟线<br/>月份连续/*/*.csv"] --> IM[import_minute.py]
  SINA2[(新浪日线)] --> IM
  IM --> D1["derived/C_1d_adj.csv<br/>1526 行"]
  IM --> D15["derived/C_15m_adj.csv<br/>32540 根"]
  IM --> DR["derived/C_rolls.csv<br/>28 次换月"]
```
- 派生文件**不入库**(购买数据有授权限制,派生产物体积大),换机器需重新导入。
- 复权方式:差额法(换月价差取换月前最后共同交易日收盘价之差),主力按**前一交易日**持仓量选,只向更远月换月。
- 分钟线时间戳是**开始**时间;交易日归属:夜盘(≥20 时)归下一交易日,日历之外返回 NaT 并丢弃。

## 8. 核心流程
```mermaid
sequenceDiagram
  autonumber
  participant U as 用户
  participant M as main
  participant R as run_one
  participant A as AKShare
  participant D as vn.py DB / CSV
  U->>M: python scripts/fetch_data.py
  loop 每个 品种 x 周期
    M->>R: run_one(code, exchange, period)
    R->>A: fetch_with_retry(...)
    A-->>R: 原始 DataFrame
    R->>R: normalize
    R->>D: save_bar_data 或 save_csv
    M->>M: sleep(REQUEST_INTERVAL=1s)
  end
  M-->>U: 退出码 0 / 1
```

## 9. 依赖图谱
直接依赖见 `requirements.txt`(未锁定版本,安装时实测版本如下):
| 包 | 实测版本 | 用途 |
|---|---|---|
| vnpy | 4.5.0 | 框架核心(会拉入 PySide6 等 GUI 依赖,体积大) |
| vnpy_ctp | 6.7.11.4 | CTP 网关(仅 win_amd64 wheel) |
| vnpy_ctastrategy / vnpy_ctabacktester / vnpy_riskmanager / vnpy_sqlite | — | CTA、回测、风控、SQLite |
| akshare | 1.20.36 | 行情下载 |
| pandas | 3.1.1 | 数据清洗 |
| pillow | 已安装 | 界面里点"微信"二维码对话框需要,缺它会让整个界面崩溃 |
| pytest / pytest-cov | — | 测试 |
- 许可证:vn.py、akshare 等为宽松开源协议,**尚未逐包核验**;CTP 底层库为上期技术专有二进制,实盘前需确认条款。
- 潜在冲突:Python 3.13 与部分依赖的兼容性已通过导入验证,尚未跑完整回测。

## 10. 维护建议
- **每天/每周运行一次 `fetch_data.py`**,否则分钟线历史会因 1023 根上限而断档且无法回补。
- **购买数据在 `C:\Dev\data\invest\`,派生文件在 `derived\`;两者都不入库**。换机器或数据更新后重新运行 `scripts/import_minute.py` 重建派生文件。
- 所有新脚本第一行业务导入前先 `import bootstrap`。
- 部署到 Docker/VPS 时设置 `TZ=Asia/Shanghai`;`.env` 通过挂载注入,不入库。
- 临时文件:pip 异常中断会在 `%TEMP%` 残留 `pip-unpack-*`,可手动清理。
- **本机网络**:走本地代理(127.0.0.1:7897)下载 PyPI/Docker 镜像这类大文件只有约 20 KB/s,直连约 4.7 MB/s。pip 安装与镜像拉取应绕过代理(`NO_PROXY=*`;WSL 里还要 `unset http_proxy https_proxy` 并忽略 pip 的旧代理配置)。
- 运行与部署:见 `docs/runbook-simnow.md`、`docs/live-readiness.md`、`docker-compose.yml`;宿主机挂载目录需让 uid 1000 可写。
- Windows 控制台输出中文时设置 `PYTHONUTF8=1`。
- **测试不得比较 vn.py 枚举的 `.value` 字符串**(`Direction.LONG.value` 等会随 `LANG` 环境变量在 `'Long'`/`'多'` 间变化),一律用枚举成员比较。
  验证方法:`env -u LANG -u LC_ALL -u LANGUAGE ... pytest tests`。
- 升级 vn.py 后须确认 `backtest_lib.py` 依赖的异常哨兵(`Traceback` / `回测终止`)仍有效(`tests/test_strategy_backtest.py` 覆盖)。
- `vnpy_ctp` 在 PyPI 上近几个版本只有 Windows 预编译包与源码包,Linux/Docker 须从源码编译(需 g++ 与 Python 头文件),容器内编译与导入已于 2026-11-10 实测通过(`docs/runbook-simnow.md` §10)。

## 11. 术语表和缩写
- **CTP**:综合交易平台(上期技术),国内期货事实标准接口。
- **SimNow**:上期技术提供的官方模拟环境。
- **穿透式监管认证**:实盘接入前由期货公司登记程序、发放 AppID/AuthCode 的流程。
- **主力连续**:把各月份主力合约拼接成的连续序列,换月处有价差跳空。
- **CTA**:vn.py 中的单品种策略引擎。
- **MADR**:Markdown 架构决策记录模板。


## 12. 变更日志
- 参见 [`CHANGELOG.md`](CHANGELOG.md)。提交与本文件同步更新,提交信息引用对应 Issue。
- 2026-11-12:**夜盘实测日**:演练六场景全部通过(含真实切网);行情录制第一套前置验收与卡住检测;`drill.py --trace` 诊断埋点定位 `td_api.close()` 段错误并重写 `disconnect` 场景;通知器落盘待补发队列(起因:断网期间的告警恢复后也没送到);研究沙盒 CLI/网关/容器/选参评估/迭代上限/报告/审阅;毕昇本机部署与工作流 v3(LLM 联调待额度)。详见 `CHANGELOG.md`。
- 2026-11-11:国信测试报告取证与自动生成(Issue #9):`broker_test.py`/`broker_evidence.py`/`broker_report.py`,模板不入库;`docs/live-readiness.md` 补 C2b。
- 2026-11-11:**拆分与瘦身**(Issue #7):原 §2.2/§2.4/§2.5 → `docs/roadmap.md`;原 §2.3 → `docs/methodology-mapping.md`(扩充至 34 行并新增矛盾与证据强度一节);原 §5 → `docs/modules.md`(原文未删改),§5 改为模块索引;§4 只留未解决条目(已处理/重复的 4 行归档到 `docs/roadmap.md` §2.7,新增 3 条技术债);修正过期事实(运行环境、测试数、阶段命名);新增 ADR 索引 2 条。
- 2026-11-11:纪律机制补缺(Issue #6):解锁冷却与历史、非策略委托检测、实盘准入检查;新增 ADR `20261111-discipline-enforcement.md`、`20261111-risk-floor-immutability.md`。
- 2026-11-10:穿透式监管接入工具(Issue #5):新增 `collect_terminal_info.py`/`penetration_check.py` 与 `docs/broker-filing.md`;`docs/live-readiness.md` 补 C2/C3。
- 2026-11-10:报备能力面补齐(Issue #4):日内演示策略、算法单/价差应用页与多账户能力说明。
- 2026-11-10:目标分层架构(两层)、研究方法论映射、四阶段路线与规划位登记表、ADR `20261110-bisheng-ai-researcher.md`(现位于 `docs/roadmap.md`/`docs/methodology-mapping.md`)。
