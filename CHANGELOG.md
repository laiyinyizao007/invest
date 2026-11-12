# 变更日志（Changelog）
所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。
与 [`PROJECTWIKI.md`](PROJECTWIKI.md) 互相链接;相关 Issue:[#1](https://github.com/laiyinyizao007/invest/issues/1)。

## [Unreleased]
### Changed（变更）
- Batch B 脚本层整理(Issue [#12](https://github.com/laiyinyizao007/invest/issues/12)):统一 `notify.webhook.redact` 脱敏实现，新增 `bootstrap.isolate_state()`/`load_env()`，并将 CTP 采集与穿透测试的可复用逻辑真正下沉至 `ctp_tools/`，两个 CLI 现在直接使用共享实现；用 `pyproject.toml` 统一测试导入路径。交易日历仍保留在后续 Batch D。

- PROJECTWIKI 拆分与瘦身(Issue [#7](https://github.com/laiyinyizao007/invest/issues/7)):876 行 → 299 行。
  原 §2.2/§2.4/§2.5 迁至 `docs/roadmap.md`(编号沿用,另增 §2.6 新增规划位与 §2.7 已处理技术债归档);原 §2.3 迁至
  `docs/methodology-mapping.md`(25 行扩充到 34 行,新增「研究文档的矛盾与证据强度」一节给出本项目采纳口径);
  原 §5 整体迁至 `docs/modules.md`(原文未删改),§5 改为模块索引;§4 只留未解决条目并新增 3 条技术债;
  修正过期事实(运行环境改为远程 Windows 直接运行、不再写死测试数、阶段命名统一)。迁移经逐行校验:除有意改写的 19 行外无丢失。
  Bisheng ADR 里的章节引用改指 `docs/roadmap.md`。

### Added（新增）
- 研究网关**提交额度账本**（`scripts/research_gateway.py`，配置 `iteration.window_hours`，默认 24，范围 1–720）：滚动窗口内最多提交 `iteration.max_iterations` 个候选，由服务端强制，LLM 无法绕过；额度检查、写文件、记账在同一把锁内完成，记账失败则撤回候选文件；账本损坏 fail-closed（拒绝提交）；`/research/data` 回报已用额度与最早释放时间。请求可带 `view:"report"` 只取精炼回报（run_id/passed/status/report），非法取值在运行作业前即拒绝。
- 确定性中文报告 `risk/report.py::render_report`：把 backtest/gate/evaluate 结果压成几行文字，纯函数、输入残缺也不抛异常、不做策略建议；每个作业结果附带 `report` 并落盘。此前 `research_api.py` 已导入该模块而文件未入库，现补齐。
- 通知器**落盘待补发队列**（`notify/webhook.py`，环境变量 `WEBHOOK_OUTBOX`，默认 `data/state/webhook_outbox.jsonl`，`off` 关闭）：发送最终失败（如本机断网）的告警不再丢弃，写入 JSONL（临时文件 + fsync + 原子替换），网络恢复后由后台线程补发——FATAL/ERROR/WARN 按发生先后逐条补发并加「【补发】」前缀、保留原始时间；INFO 积压 ≥3 条合并成一条摘要（最多列 20 行）；条数上限（FATAL 优先保留）与存活期（默认 24 小时）；已知断网时新告警只试一次而不是空等约 34 秒；退出时内存队列里没发完的也落盘；进程重启后继续补发。起因：夜盘切断网络 240 秒的演练里，断网期间产生的「行情假死」「网关连接断开」告警恢复后也没有补发。已用真实 Telegram 验证。演练的送达统计改为如实报告实际发送/暂存/放弃/已补发/待补发条数；`drill.py` 场景跑完先落一份结果再进入退出阶段。
- 记录夜盘完整 `all` 的两个新发现：`latency` 在行情清淡时 30 秒没成交（对活跃度敏感）；`disconnect` 重连后退出登出（`logout_ctp`）出现一次原生段错误（139），待复现。
- `disconnect` 演练通过：改为外部真实切断容器网络（`drill.py --external-cut FILE` + 宿主机 `docker network disconnect/connect`），旧版 `td_api.close()` 会释放整个原生 API 导致段错误，已移除；没给 `--external-cut` 时场景自动跳过。实测：切断 240 秒——行情停更看门狗 61 秒告警，CTP 心跳超时约 121 秒报断线（8193），恢复后约 2 秒自动重连；切断 30 秒无断线。至此六个演练场景全部通过。
- 沙盒 `evaluate`（CLI `research_api.py evaluate`、网关 `POST /research/evaluate`、毕昇工具 `run_evaluate`）：候选在类上声明 `param_grid`（恰好三个参数、各 3–10 个严格递增数值，参数名须登记在 `parameters`；`risk_budget`/`max_daily_losses`/`atr_window`/`history_vt_symbol` 不得入网格）→ 服务端在**选参窗口（止于 2022-06-30）**扫网格、邻域平滑选参并判定参数高原 → 用选出的参数在其后的**滚动验证窗口**做完整晋级门槛评估。选参数据与评估数据不重叠，样本外因此由服务端保证；解决「LLM 候选没有参数网格、参数高原恒为未通过」的限制。新增 `scripts/research_grid.py`、`risk/plateau.py::smoothed`（与 `optimize.smoothed` 等价，有对照测试）、配置 `grid.{min_round_trips,max_cells,timeout_seconds}`。实测（毕昇容器 → 沙盒容器，手写候选）：36 格、15 个有效，选出 breakout_n=72/slow_window=96/atr_mult=2.0，不在高原；样本外夏普 -1.84、往返 156、对随机入场 65.5 分位，7 项门槛未过，总耗时 45 秒。毕昇工作流 v3 改用该工具，提示词要求 LLM 声明 `param_grid`。
- SimNow 第一套前置夜盘演练（2026-11-12）：`overnight`、`latency`（0.177 秒）、`tick-stale`、`daily-loss`、`hard-lock` 五个场景通过，`disconnect` 因场景设计缺陷未验证（见 `docs/simnow-drills.md`、`docs/runbook-simnow.md`）。新增 `docs/simnow-drills.md` 合并记录。
- 毕昇工作流 v3（`tools/bisheng/build_v3.py`、`run_v3.py`）：LLM 写继承 `TrendAtr15mStrategy` 的 15 分钟候选，评估节点由日线回测换成 `run_gate`（15 分钟玉米滚动验证段上的晋级门槛评估），解决 v2 里日线候选基类与 15 分钟门槛评估不一致的问题。联调时 MiniMax 返回 429（Token Plan 用量上限），LLM 节点未能实跑；不依赖 LLM 的部分用手写 15 分钟唐奇安候选从毕昇容器一侧实测通过（提交→本机批准→门槛评估，28 秒）：样本外夏普 -5.47、往返 240 笔、对随机入场仅 6 分位，该想法不成立。注意：LLM 候选没有参数网格，`plateau` 恒为空，所以「参数高原」一项永远判未通过——在为候选做参数网格寻优之前，LLM 候选无法通过全部门槛，这是如实的限制而非缺陷。
- `drill.py` 诊断埋点 `--trace FILE` / `--trace-skip-close`（新增 `scripts/drill_trace.py`，默认关闭）：逐行 fsync 的 trace、退出钩子、致命信号转储与周期线程栈转储、释放后 API 调用探针、启动与断线阶段周期快照、距上次登出间隔。用它在夜盘一次运行定位了 `disconnect` 场景静默退出：退出码 139，根因是 `td_api.close()` 释放了整个原生 API，事件引擎定时器线程随后调用 `query_position` 触发段错误；场景本身无法验证「断线与自动重连」，需改为真实切网（`docker network disconnect`），改造前该场景视为未验证。
- 修复 `drill.py` 的 `latency` 场景在真实 SimNow 上必然失败的缺陷：代码读 `tick.ask_price` / `tick.bid_price`，而 vn.py 的 `TickData` 只有 `ask_price_1` / `bid_price_1`。此前单测用手写假 Tick 同样写成 `ask_price`，与代码互相印证，所以 1000+ 个测试全绿却从未发现。测试改用真实 `TickData` 并加一条断言防止假字段回流。夜盘实测 `latency` 通过：成交→持仓回报 0.177 秒，1 手仓已平；`overnight`、`tick-stale` 通过。
- 行情录制「卡住」检测：启动后超时未订阅 → `stalled`，`watchdog` 据此受控重启（忽略上次运行遗留状态）；订阅后长时间无任何 Tick → `data_warning`（只告警）。起因：SimNow 第一套前置夜盘实测，距上次停止仅约 16 秒再启动时，进程连接登录成功却 3 分钟不订阅，状态一直是 `connected`、心跳正常，旧版 watchdog 看不出。
- 策略自身报错作为研究结果回传,而不是网关故障:子进程捕获策略运行异常 → `{type, message}`(取 traceback 末行,抹掉本机路径,限 200 字符)→ 记为 `status: strategy_error` 的结果(HTTP 200)→ `feedback` 的失败 Trace 中给出「策略运行报错: 类型: 信息」;`backtest_lib` 的异常信息现带上 vn.py 随后打印的 traceback(此前只剩一句「触发异常」)。毕昇工作流 v2(多轮循环)实测:布林带想法第 1 轮因 `am.boll` 返回值个数用错而报错,反馈回流后第 2 轮 LLM 自行改写并跑通(夏普 -1.02,该想法不成立)。注意:反馈里的报错文本可能来自已批准代码的异常信息,属进入 LLM 上下文的外部文本,故限长且只保留可打印字符。
- 毕昇工作流 v2 构建与联调脚本 `tools/bisheng/build_v2.py`、`run_v2.py`:想法→读失败反馈→LLM 起名→LLM 写 `entry_signal`(带反馈)→提交→本机批准→回测→输出→回到想法输入(每轮都需人批准,轮数天然由人把控)。
- 研究沙盒容器化 `docker-compose.research.yml` + `docker/research/`:网关与回测在独立容器内运行,内部网络 `research_net`(无外网)、只读根文件系统、丢弃 capability、非 root、资源限额、控制目录只读、数据只读、不含 `.env`/CTP;毕昇后端经该网络调用。镜像约 248 MB(去掉 vnpy 的 GUI 依赖),依赖由宿主机预下载轮子后离线安装(`fetch_wheels.sh`)。子进程运行前重算代码哈希(堵批准后替换文件);批准记录与令牌移入控制目录 `data/research-control/`;`RESEARCH_SANDBOX_CONFIG`、`INVEST_C15_DATA` 环境变量。随附毕昇可导入的接口描述 `docs/research-gateway.openapi.json`(有测试保证与路由白名单一致)。
- 研究沙盒 HTTP 网关 `scripts/research_gateway.py`(标准库实现,无新增依赖):路由白名单(data/candidates/backtest/gate/feedback/results)、Bearer 令牌(独立文件,`init-token` 生成且不覆盖)、先鉴权再路由、全局限流、请求体上限、重作业串行(忙则 409)、提交通道只写 pending 且绝不覆盖并先过静态筛查、请求体里的 `confirm` 被忽略(批准只能由本机 `approve` 触发)、反馈最多返回配置的 N 条且只含固定字段(不回传源码/路径/调用方自由文本)、启动自检拒绝带 CTP_* 环境变量的进程。`research_api` 抽出 CLI 与网关共用的作业函数(JobError)。实测:真实 socket 上 401/404/403 行为正确,提交→本机批准→回测→反馈全链路通;**未解决:毕昇容器访问不到宿主 127.0.0.1 上的网关**(`host.docker.internal` 解析到过期地址 172.20.10.5,连接超时),见 ADR 待决项。
- 研究沙盒参数配置化 `config/research_sandbox.json` + `scripts/research_config.py`:迭代轮数、反馈条数、随机对照次数、各类超时、候选大小/命名规则、网关监听/限流/令牌路径、各目录、筛查追加清单;严格校验(未知键/越界/路径越界即拒绝运行),安全边界不可被配置调没。人工批准绑定代码哈希:新增 `research_api.py approve`,`backtest`/`gate` 只运行已批准的代码(代码一改即失效),`--confirm-reviewed` 变为「现在批准」的快捷方式。ADR `20261112-research-sandbox-config-and-approval`。
- `optimize_15m.py --select-only`:样本外安全选参——训练段止于 2022-06-30(与 Walk-Forward W1 训练段末一致,有测试锁定),不评估验证/测试段,与 `--test` 互斥;输出的参数与 `--plateau-out` 文件交给 `gate`,滚动验证窗口才是真正的样本外。实测该流程选出 16/192/3.0,在 W1~W3 上样本外夏普 0.31(此前用重叠窗口选参的 24/192/3.0 为 1.39,其中含样本内成分),对随机对照 93.5 分位,往返 58 笔,门槛 6 项未过。
- `optimize_15m.py --plateau-out FILE`:把训练段参数网格得分写成沙盒 `gate --plateau-file` 可读的 JSON(只含训练段)。`gate` 结果新增 `protocol_notice`:候选参数若参考过滚动验证窗口(2022-07 ~ 2023-12)就不再是样本外——`optimize_15m.py` 的训练/验证段与这些窗口重叠,用它的输出做 gate 只能算参考(实测其选出的 24/192/3.0 在这些窗口上夏普 1.39、对随机对照 100 分位,但往返仅 48 笔、不在高原,门槛 5 项未过)。
- 沙盒 `gate` 子命令与 `scripts/research_gate.py`:15 分钟玉米滚动验证段(W1~W3,不触碰测试段)上跑候选 / 成本翻倍 / 随机入场对照(新增 `RandomEntry15mStrategy`,入场概率按候选活跃度对齐,`--random-runs` 参数化默认 200),汇总后调用晋级门槛;缺 `--plateau-file` 时参数高原按未通过处理。实测 200 次对照约 23 秒。示例:基准类 fast=14/slow=60 的结果与既有 ADR 一致(样本外夏普 -3.96,门槛 7 项未过)。
- 候选评估纯逻辑 `risk/candidate_eval.py`(ADR `20261112-candidate-evaluation`):滚动验证段拼接成样本外序列、随机入场分位数(种子数参数化,默认 200)、组装 `CandidateMetrics` 并调用 `check_gate`;门槛数值不放宽,无随机对照样本时分位数记 0。尚未接入沙盒 `gate` 子命令。
- 研究沙盒 CLI `scripts/research_api.py`(`data`/`backtest`/`results`)与底座 `scripts/research_sandbox.py`:仅 `pending_strategies/` 内策略、仅训练/验证段、子进程 + 环境白名单 + 静态筛查 + 需 `--confirm-reviewed`;结果附诊断书与代码 sha256。纵深防御而非沙盒,威胁模型写在 `research_sandbox.py` 文件头与 ADR。
- 研究沙盒纯逻辑底座(ADR `20261112-research-sandbox-groundwork`):`risk/diagnosis.py` 确定性诊断书(回撤/次数/连亏/夏普四维,阈值与晋级门槛同源);`risk/plateau.py` 的 `parameter_on_plateau` 计算(比门槛原文更严:邻域内至少一半格子得分为正)。毕昇先于 API 固化部署,仅作知识库问答,不接工具。
- 行情录制 wrapper 的 PID 探测改为三态（alive/dead/unknown）：Windows 访问被拒或查询失败不再被当成「已死」；记录的 PID 状态未知时 `start` 拒绝拉起第二个录制进程。新增 4 组测试，全量 867 通过。
- 测试段使用登记 `scripts/segment_ledger.py` + `config/test_segment_usage.json`:`optimize.py`/`optimize_15m.py` 的 `--test` 先查后登记再运行,重复使用即拒绝(无强制开关,重置须手工改登记文件并补 ADR);15 分钟基准测试段补登记为已使用。`tests/conftest.py` 让所有测试使用临时登记文件。
- 国信取证运行器每次运行向 `connection.json` 追加实际连接的环境、前置地址、BrokerID 与 AppID 掩码(不含账号密码授权码)。
- 行情录制二次加固(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):`wrapper.lock` 改为 OS 文件锁(Windows `msvcrt.locking` / POSIX `flock`),持有进程退出即自动释放,不再依赖 PID 探测与 unlink;`CTP_RECORD_DATABASE_DIR` 必须是项目根下的单层独立目录(拒绝 `.vntrader`/`data`/项目根/多层/绝对路径——codex 审查发现 `archive/.vntrader` 经 `isolate_state(name)` 只取目录名会把 TEMP_DIR 重映射回主库)。新增测试含真实子进程并发持锁,全量 853 通过。G8:codex 轮已审并修复 1 Critical + 2 High;`codereview` 轮待做。
- 个人信息防线 `tests/test_no_personal_data.py`:本机 `.env` 中的姓名/电话/邮箱/账号取值不得出现在任何受版本控制的文件里(失败信息只给位置,不回显取值);测试数据里的姓名一律改为虚构占位。真实个人信息只放本机 `.env`。
- 行情录制 codex 审查后的加固(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):修复 `EventEngine` 重复启动、DCE 小写合约被大写后找不到、关闭时不 flush 丢尾部数据、每 tick 同步写状态文件、旧录制列表不同步;SimNow 要求行情前置主机与已通过白名单的交易前置主机一致;wrapper 子进程不再继承调用方管道、启动后确认存活、`watchdog` 检查心跳;增量任务超时杀整棵进程树、陈旧锁自动恢复、不向下载子进程传递 `CTP_*` 凭证。合约来源问题按用户确认的方案 A 解决:受限连接交易前置,只做认证+查合约,发单/撤单/查资金/查持仓在网关层与底层请求层双重硬性禁止(ADR 已修订边界);国信 profile 新增 `CTP_RECORD_ALLOWED_ADDRESSES` 显式地址允许列表。SimNow 第二套 7x24 前置上已验证认证/登录/合约查询/订阅/tick 到达/优雅停止,以及临时库中 DataRecorder 入库(115 tick + 2 根 1 分钟 K 线);但该环境行情时间不可信(比本机快 417s、郑商所日期停在两天前),DataRecorder 的 60s 保护会丢光 tick,故正式库为 0 行。新增 `data_warning`/`degraded` 状态,时间偏差 ≥60s 时显式告警。**交易时段内的真实录制仍待验收。**
- `scripts/broker_report.py` 在国信报告签字栏旁打印提测方姓名（仅提示，不代替手写签字/盖章；姓名为空时不改动模板）。
- 补齐行情录制与增量调度的有界任务 `scripts/record_market_incremental.py`、离线生命周期测试 `tests/test_recording_tools.py` 和运行手册 `docs/runbook-market-recording.md`；**真实前置上的录制尚未验证**（合约来源已改为受限交易前置连接，见 ADR 修订）；Windows dummy worker 实测已通过并修复 `pid_alive` 在 Windows 上的误判（旧实现使 `ensure`/`watchdog` 重复拉起、`stop` 永远拒绝）；DSH 重启行为与授权行情端到端实测仍待完成（Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)）。
- 行情录制与 DSH 调度生命周期闸门(Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)):核对 `@goodandready/dsh-cron@0.3.43` 的有限超时、Windows 进程树终止和非 detached spawn 语义，锁定官方 `vnpy_datarecorder` + 独立 `.vntrader_record/database.db` + 短任务 `ensure`/`watchdog` + wrapper 方案；生命周期 ADR 见 [`docs/adr/20261111-market-recording-and-dsh-orchestration.md`](docs/adr/20261111-market-recording-and-dsh-orchestration.md)，本机 dummy worker 实测仍待完成。
- 文档状态对齐(Issue [#11](https://github.com/laiyinyizao007/invest/issues/11)):新增 `docs/architecture.md` §0「目标 vs 现状」逐节点实现矩阵
  (IMPL/PART/PLAN + 证据文件路径 + 实现比例 + 依赖顺序),作为"实现了多少"的单一出处;
  roadmap 目标图的 `DRDOC` 改回规划位(演练从未实跑、该文档尚不存在)、`PG`(晋级门槛)三条边改回规划位(门槛**未接入优化脚本**),
  `BS` 依赖边改回实线;§2.5 换月派生指向改回 `scripts/build_continuous.py`(实际在它那里,不在 `import_minute.py`);
  wiki §2.1 状态列改为区分"代码完成"与"已验证"(`RT/GR` 仅假网关测试、`CTP` 只有启动管道实测、`SG/DR` 从未实跑、`UR` 证据未攒齐),
  图例注明实线只表示代码存在。
- 净持仓方向不明 fail-closed(Issue [#10](https://github.com/laiyinyizao007/invest/issues/10)):新增 `risk/positions.py`(`signed_net`/`undirected`/`describe`);
  `trading/runtime.py` 的启动对账与运行中漂移检测遇到 `Direction.NET` 等方向不明的持仓时拒绝启动/告警,不再按"非多即空"算净额;
  `trading/guard_rule.py` 紧急平仓遇方向不明持仓时**不发任何平仓单**、直接升级人工(FATAL,硬锁定时 flat=False 保持进程存活)。
  此前 NET 会被当空头算反、平反。账户是否真会回报 NET 尚未实测(SimNow/国信测试环境待验证,结论补记进 ADR)。
- 架构总图 `docs/architecture.md`:分层依赖图与允许的 import 方向、磁盘状态归属表、隔离机制摘要、ADR 已知局限台账;
  roadmap §2.6 登记脚本层去重/统一交易时段表/脚本分目录三个规划位;修正 `modules.md` 写死的测试数。
- 国信测试报告的取证与自动生成(Issue [#9](https://github.com/laiyinyizao007/invest/issues/9)):
  `scripts/broker_test.py` 在国信测试环境的同一条真实连接上挂载真实风控,逐项演示 18 个测试项(穿透 T1–T7、程序化 P1.1–P6.1),
  产出脱敏证据 JSON 与渲染成图片的"截图"(`--resume` 增量补测,新的 SKIP 不覆盖旧的 PASS/FAIL);
  `scripts/broker_report.py` 用 lxml 直接填国信原模板 docx(不新增依赖):以文字定位单元格、先精确再前缀勾选、**不默认勾通过**(模板预勾选一律重置)、
  不碰国信填写区、列出"报告里还缺什么";模板含国信测试环境 IP 不入库(`--template`/`BROKER_REPORT_TEMPLATE`)。
  已用真实模板填写验证,Word 可正常打开。`.env.example` 增 `BROKER_TEST_*`;`docs/live-readiness.md` 增 C2b;`docs/broker-filing.md` 增 11 项对照表。
  经 codereview + codex 两轮外部审查后固化的运行约束:发单类测试项前**统一过空仓闸门**(有持仓或查不准持仓则一笔不发、全部记 FAIL)、
  演示委托用**唯一价格**(不同步骤不落进重复报单的同一个价格键)、测试前置地址**由操作者人工确认**(脚本无法用握手判别)、
  暂停/阈值步骤**异常也清场**(finally 解除暂停标志并撤遗留委托)、撤单要等终态确认才算数、日志只取本次运行之后的行；
  报告侧:选测项 FAIL 同样不勾「测试结论:通过」,证据图文件缺失进 warnings 与缺失清单(不静默交缺图报告)。
- 国信程序化测试合规的功能缺口(Issue [#8](https://github.com/laiyinyizao007/invest/issues/8),依据国信《外部接入系统技术测试报告》模板):
  ① 阈值预警——风控规则触达笔数/手数/金额上限时发 WARN「风控阈值触发」告警(走既有 Webhook,按指标 300 秒节流;校验失败类不预警);
  ② 暂停交易——`scripts/pause_trading.py on|off|status` 写/删 `data/state/pause_trading.flag`,标志存在时拒绝新开仓、平仓放行,切换发告警;
  ③ `collect_terminal_info.py` 增整机厂商/型号/内存;`penetration_check.py` 报告记录「关闭连接」步骤(`close_engine` 返回是否限时内关闭)。
  ④ 优雅登出——运行时 `shutdown()` 与脚本共用的 `close_engine` 在关闭引擎前先向已登录的交易/行情通道各发 `ReqUserLogout`(vnpy_ctp 的 `close()` 只断开连接、不登出),失败不打断退出。
  新增 ADR `20261111-broker-test-compliance.md`。
- 纪律机制补缺(Issue [#6](https://github.com/laiyinyizao007/invest/issues/6),依据《期货交易复盘与人性反思》):
  ① 解锁问责——`AccountGuard` 记录硬锁定时间与解锁历史,`unlock_account.py` 增加冷却期(硬锁定当个交易日内拒绝,退出码 3)
  与 `--force`(须交互输入 FORCE、不能与 `--yes` 同用);② 非策略委托检测——运行中发现 reference 不属于 CTA 策略/紧急平仓的新委托,
  告警并按交易日计数(只告警不锁定;启动对账通过后才开始计数),每日证据表记为严重事件、让"人工干预 = 0"可验收;
  ③ 实盘准入——`run_headless` 连接生产 API(`实盘` 且非 SimNow)前要求 `data/state/promotion_approval.json`,
  由新增的 `scripts/record_promotion.py` 在晋级门槛全部通过时才写出(SimNow 与看穿式测试环境豁免)。
  codex 审查后加固:运行时仍在运行(心跳 60 秒内)时拒绝解锁、触发时间未知须 `--force`、写盘失败回滚、`CTP_ENVIRONMENT` 严格校验(只允许「实盘」「测试」)、已计数委托号防回放重复。
  新增 ADR `20261111-discipline-enforcement.md`、`20261111-risk-floor-immutability.md`(风控底线不可优化、明确不做清单、本地止损已接受风险)。
- 穿透式监管接入工具(Issue [#5](https://github.com/laiyinyizao007/invest/issues/5)):新增 `scripts/collect_terminal_info.py`
  (Windows 终端信息采集,标出出网网卡与虚拟网卡)、`scripts/penetration_check.py`(看穿式测试环境演练:
  逐里程碑判定登录全流程、订阅/账户/持仓、可选跌停价买单+撤单,生成脱敏报告;生产 API 无条件拒绝);
  新增 `docs/broker-filing.md`(报备说明草稿,事实取自仓库)、`docs/live-readiness.md` C2/C3(Windows 直接运行的部署方式与要向期货公司确认的问题);
  `.env.example` 补测试环境说明。详见 PROJECTWIKI §5。
- 报备能力面补齐(Issue #4):安装 vn.py 官方模块 `vnpy_algotrading`(算法单/拆单:TWAP、冰山等)与
  `vnpy_spreadtrading`(价差/组合套利),注册到 GUI 入口 `run_gui.py`——平台能力面的演示与手动试验用,
  无人值守路径(`run_headless`/`runtime`)零改动;多账户能力(vn.py `add_gateway` 原生支持多 CTP 网关)
  在 PROJECTWIKI §5 补如实说明(项目默认单账户,`AccountGuardRule` 当前按单账户监控);
  新增日内演示策略 `strategies/intraday_demo_strategy.py`(继承 15 分钟基准,尾盘 14:30 K 线收盘
  发出强平限价单、力争 15:00 前离场,反向跳空未成交时保护性止损接管而非裸奔过夜;夜盘/尾盘不开新仓,
  仅研究/回测)。全量 647 个测试通过,总覆盖率 90%(门禁 85%)。
- 目标架构设计(PROJECTWIKI.md §2.2–2.5,依据两份研究资料):两层架构——
  **内层 = vn.py 流程引擎(确定性内核,现有 trading/risk/strategies/研究脚本/notify/Docker)**,
  **外层 = Bisheng 流程编排(AI 策略研究员,规划位)**;边界铁律 = 外层只经研究沙盒 API
  白名单端点(5 个 `/research/*` 端点)交互,`promotion_gate` 是候选策略唯一闸门,LLM 产出
  只落 `pending_strategies/`、经 walk-forward + gate 全过 + 人工批准才进 `approved/`、
  装载永远人工。附:研究方法论→系统映射表 25 条、四阶段演进路线(SimNow 验收闭环 →
  多品种与调度 → 月度滚动引擎化+研究 API 固化 → Bisheng 接入与 Alpha Factory)、
  技术债去向表、「明确不做」清单(CPCV/每日自动优化/自建前端)、规划位登记表
  (不建空目录,触发条件满足时「新 ADR + 代码同一原子提交」)。
- 新 ADR `docs/adr/20261110-bisheng-ai-researcher.md`:外层 AI 策略研究员采用 Bisheng 编排;
  含备选方案对比(Dify/LangGraph/Coze)、可替换性条款(全部业务逻辑在内层,外层只是编排壳)、
  安全约束(版本 ≥2.6.0 因 CVE-2026-82278 Code 节点 RCE、仅内网、禁用 Code 节点、与交易
  compose 物理分离);未决:部署机器(阶段四 4b 前决定)。
- 研究资料《期货交易的复盘与人性反思》(`docs/期货交易的复盘与人性反思.md`):
  由用户提供 PDF(Gemini 对话导出,51 页)全文忠实转写而来,未改写删减,仅做排版规范化;
  原始 PDF 不入仓库。内容为一次人机协作量化复盘对话——交易纪律反思、1 万元本金约束下的
  系统路线(模拟盘/风控基建先行)、样本内外切分与寻优方法、混沌工程演练等,与本仓库
  既有 ADR 的方法论互相印证。
- 同源要点梳理《期货交易复盘与人性反思-要点梳理》(`docs/期货交易复盘与人性反思-要点梳理.md`):
  按七个正交章节(心性/资金/研究方法论/风控工程/系统基建/AI 自动化/学习资源)逐条归档原文全部
  要点,每条只归一章并以 `(提问 N)` 回溯忠实转写文档,交叉关联用链接不复述。
- Walk-Forward 滚动优化器 `scripts/walk_forward_optimizer.py`(方法学见 ADR
  `docs/adr/20261110-walk-forward-optimizer.md`,在看到结果前写定):
  - 3 个滚动窗口(训练 24 月 / 验证 6 月 / 步长 6 月,验证段互斥),全部止于 2023-12-31;
    fast ∈ {10..30} × slow ∈ {40..90} 二维网格 31 组,`atr_mult=1.5` 与 `risk_budget=300` 固定
    (参数字典不传 risk_budget 键,无法覆盖),按原始夏普确定性多级排序取 Top5 自动外推验证段。
  - 测试段三层隔离护栏:`TEST_SEGMENT_START` 硬断言、API 反例测试、源码扫描测试
    (2024+ 年份字面量只允许出现在常量定义行),不提供任何 `--test` 开关。
  - 多进程:模块级可 pickle worker + 单个 `ProcessPoolExecutor` 贯穿全部窗口(避免每窗口重建池
    导致 lru_cache 失效与重复 spawn),`--workers 1` 串行降级,`--workers` 拒绝 0/负数;
    每进程独立 K 线缓存 + 回测切片后断言不含测试段 K 线;经 `run_engine_bars` 读取
    `skipped_by_budget` 报告预算弃单率,`backtest_lib.py` 零改动;明细输出
    `data/results/walk_forward_C15.csv`(不纳入版本控制)。
  - 一次性演练结果(108 次回测):训练段最优夏普本身全为负,15 组验证参数夏普全部为负、
    正夏普窗口占比 0%,预算弃单率全程 0%(空仓并非预算造成),与基线 ADR 负面结论互证;
    串行/并行两次运行数值完全一致。演练中修复 vn.py 回撤金额带负号导致的两个统计 bug
    (汇总最差回撤被 0 吞掉、排名平局键方向反),复跑确认排名与数值不变。
  - codex 双轮审查(第 1 轮工作流 + 第 2 轮 clink codex CLI 833 秒含真实 spawn 动态探针):
    无 Critical/High;隔离护栏经伪造 2024 窗口实测不可绕过。采纳修复:单池复用、
    真实 spawn 冒烟测试、summarize 非有限夏普防御(当前 vn.py 的 nan_to_num 使其暂不可达,
    属防升级缺口)、Window `__post_init__` 校验、tz-aware 入参归一化、奇数 fills 截断测试、
    空结果不写 CSV;源码扫描层"防误改提示网"定位与弃单率三类误读场景写入 ADR。
  - 新增 `tests/test_walk_forward_optimizer.py` 29 个测试,新模块覆盖率 99%;
    全量 643 个测试通过,总覆盖率 88%(门禁 85%)。
- 极限压力测试前置改造(混沌工程四演练,详见 ADR `docs/adr/20261110-ctp-error-code-and-watchdog-policy.md`):
  - `risk/order_policy.py`:`extract_ctp_error_code()` + `CTP_ERROR_KIND` 码表,废单分类升级为
    「错误码优先 → 关键词回退 → 未知 fail-closed 硬拒绝」三级决策;码表只收录实测/官方背书的 8 个码
    (21/81/63/32/33/14/30/31),软码表留空待 SimNow 实测;模块保持 100% 覆盖率。
  - `trading/guard_rule.py`:开仓单硬拒绝时发 FATAL「严重错误,已停止策略开仓」(带 CTP 错误码与原因);
    新增看门狗撤单唯一公开入口 `cancel_open_orders_on_stale_tick()`(复用 offset 过滤,平仓/紧急平仓单不撤)。
  - `trading/runtime.py`:交易/行情分通道连接状态机(精确措辞驱动、边沿告警、只认「登录成功」为恢复凭据,
    只读属性 `connection_state`);假死看门狗首次越限强撤全部开仓挂单,新 Tick 复位可再次触发,
    `stale_cancel_open_orders` 开关可回退旧的只告警行为。
  - `tests/fakes.py`:`FakeGateway` 新增 `disconnected` 模式(发单本地快速失败返回空串、不产生 OrderData,
    复现 `CtpTdApi.send_order` 断连行为);新增 `tests/test_disconnect_signal.py` 锁定
    「断线信号一次性消费、重连无一窝蜂补发、新信号不受影响」三条语义。
  - `scripts/drill.py`:`disconnect` 场景增加重连后连接状态、窗口内发单失败告警计数、「信号积压补发:无补发」证据项。
  - 测试由 572 个增至 614 个,全量离线通过,总覆盖率 89%(门禁 85%)。
  - G11 偏离说明:Gemini CLI 仍报 `ProjectIdRequiredError`(缺 `GOOGLE_CLOUD_PROJECT`),simple-gemini
    不可用,本 ADR/测试/文档由主模型撰写;plan-down 的方法澄清由 gemini-2.5-flash 与 planner 完成,
    codex CLI 的设计复核经 clink 后台完成(结论与本方案一致:同一组错误码、同样 fail-closed)。
- `README.md`:项目上手入口 —— 说明"这个项目在干嘛、怎么用"。含项目定位与状态提示(策略结论为负面、只用于管道测试、当前不连实盘)、
  Mermaid 架构速览、目录结构、环境准备与 `.env` 配置、按场景分组的命令(数据下载/主力连续/优化/无头运行/Docker/图形界面/
  SimNow 演练/废单采集/无人值守日报)、账户级风控阈值表、测试命令、已知限制表、相关文档索引。
  文档生成偏离说明:按 G11 应由 `simple-gemini` skill(Gemini CLI)生成,但本机 Gemini CLI 报
  `ProjectIdRequiredError`(缺 `GOOGLE_CLOUD_PROJECT` 环境变量),故由主模型撰写;内容已按仓库脚本的 argparse 实际参数逐条核对。
- `scripts/simnow_guard.py`:SimNow 前置机地址白名单(`SIMNOW_TD_ADDRESSES`)+ `check_simnow_address()` + `normalize_address()`。
  `--confirm-simnow` 只是布尔开关,挡不住"配置写错连上真券商";CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,
  **只能靠前置机地址区分**,地址不在表内默认拒绝连接。`collect_reject_texts.py` 与 `drill.py` 共用这一套约束。
- `scripts/drill.py`:把运行手册第 5 节的实测项做成**可重复执行**的演练场景 —— 成交→持仓回报延迟、行情断流看门狗、
  交易连接断开与自动重连、当日熔断链路、累计死线链路、夜盘首根 Tick 的交易日归属。
  演练状态与实盘状态**完全隔离**(独立的 `.vntrader_drill/` 目录与独立的风控状态文件),熔断/硬锁定不会污染实盘;
  熔断场景用"注入权益来源"的方式模拟亏损,只改本进程内的取值,真实账户权益不受影响。
  前提不满足(如收盘后跑看门狗场景)的场景标记为**跳过**而不是判失败。⚠ 必须在容器里跑(时段判断用系统本地时区)。
  非白名单地址上跑会**真的发委托**的场景(`latency`/`daily-loss`/`hard-lock`)需要再加 `--confirm-any-address-trades`。
- `scripts/unattended_report.py`:无人值守验收的**证据**工具 —— `--report` 解析 vn.py 每日日志与心跳文件生成
  "每日证据表"(启动次数/成交笔数/异常事件/心跳新鲜度)+ 验收清单;`--probe-webhook` 做连通性自检
  (有 `WEBHOOK_URL` 就真发一条,没有就起本地接收端离线验证"送达 + 业务失败可辨识")。结果写进 `docs/unattended-log.md`。
- `docs/unattended-log.md`:无人值守运行记录(由脚本生成,标记之间的内容会被覆盖)。
- `scripts/collect_reject_texts.py`:在 SimNow 模拟盘上故意触发几个**必定被拒**的委托,采集 CTP 的真实错误文本,
  用于校准 `risk/order_policy.py` 的 `HARD_KEYWORDS/SOFT_KEYWORDS`;结果记入 `docs/reject-texts-simnow.md`。
  安全约束:必须 `--confirm-simnow`,且交易服务器地址必须在 SimNow 官方地址白名单内;
  交易时段内运行自动改用越界价(不可能成交)并跳过时段场景;
  发单前先查持仓(已有持仓或交易连接断开就中止,退出码 5);
  跑完主动查持仓,发现非零持仓即告警并以非零码退出;所有价格按 `price_tick` 对齐。
- 15 分钟基准策略(`strategies/trend_atr_15m_strategy.py`)、`scripts/optimize_15m.py` 与实验方案 ADR;结果:测试段 -20.2%、夏普 -1.08,成本吞掉全部毛利,**无可延续的优势**(详见 `docs/adr/20261110-baseline-15m.md`)。测试段已被基准使用一次。
- `scripts/import_minute.py`:把购买的分合约 1 分钟线导入为差额复权主力连续合约并合成 15 分钟线(新浪日线补齐缺口、起点成交冷清段裁剪、交易日归属);玉米实测与新浪日线交叉验证通过。
- `scripts/fetch_data.py`:AKShare(新浪源)下载 PP/L/EB 日线与 1/5/15 分钟线,清洗后写入 vn.py 数据库(日线、1 分钟)或 CSV(5、15 分钟);指数退避重试、失败隔离、重复运行幂等。
- `scripts/bootstrap.py`:统一启动入口,固定中国时区与项目内 `.vntrader/` 数据目录。
- `tests/test_fetch_data.py`:32 个单元测试,覆盖率 97%。
- `PROJECTWIKI.md`、`docs/adr/`(vn.py 选型、中国时区两条 ADR)。
- 项目骨架(`0981db3`):`requirements.txt`、`.gitignore`、`config/.env.example`、`strategies/` `risk/` `notify/` `tests/` 空包。
- Phase 2(策略与回测):`risk/risk_guard.py`(风险预算、只收紧的移动止损、连亏熔断)、`risk/promotion_gate.py`(8 条晋级门槛与夏普置信区间)、
  `strategies/trend_atr_strategy.py` 与随机入场对照、`scripts/backtest_lib.py`、`scripts/optimize.py`(三段样本外 + 邻域平滑)。
- `scripts/fetch_data.py` 新增玉米(C)、甲醇(MA)。
- ADR:账户风险预算、负面回测结果、晋级门槛。
- `scripts/build_continuous.py`:分合约日线 → 差价法复权主力连续合约,并量化换月跳空(玉米中位 44 点、甲醇 75 点)。
- 晋级门槛 ADR 增补"样本外年数与置信区间的张力"表;负面结果 ADR 增补"回测结论作废待重做"与样本切分纪律。
- 测试扩充至 320 个(英文/中文 vn.py 语言环境均通过),覆盖率 97%。

- Phase 3 账户级风控:`risk/account_guard.py`(当日 600 / 累计 2000 元双层熔断、原子持久化、日初基准取上次采样)、
  `risk/order_policy.py`(废单分类/重试节奏/风暴检测)、`trading/guard_rule.py`(挂在 vnpy_riskmanager 发单链上的规则:
  拦截开仓放行平仓、紧急平仓状态机)、`trading/safe_risk.py`(平仓单跳过计数类规则)。
- Phase 4 运行时:`trading/runtime.py`(启动对账、看门狗、退出码语义)、`scripts/run_headless.py`、`scripts/unlock_account.py`;
  策略新增实盘模式(Tick→日线、信号存到下一交易时段发单、状态持久化与重启恢复)。
- Phase 5:`notify/webhook.py`(非阻塞通知)、`Dockerfile`、`docker-compose.yml`、健康检查心跳。
- `scripts/build_continuous.py --save` 与 `CADJ/MAADJ` 品种;`optimize.py` 为复权数据使用事先确定的新切分。
- 文档:`docs/runbook-simnow.md`、`docs/live-readiness.md`,ADR 增 3 条(账户风控、部署与实盘模式、负面结果更新)。

### Changed（变更）
- `trading/runtime.py::_on_log`:原合并式 `_login_notified` 单次通知替换为交易/行情两通道各自的边沿状态迁移,
  告警标题(「网关连接断开」/「网关已连接」)与消息子串保持不变,依赖它们的 drill 与无人值守日报不受影响。
- `Dockerfile`:把 `locales` 层挪到 `pip install` **之后**。改动前面的层会让 pip 重新下载全部依赖
  (容器→PyPI 实测 12.6 MB 的 sdist 要 400 秒以上),挪到后面则能命中已有构建缓存,只需重新拉取 `locales` 这一个小包。
- 测试扩充至 417 个,覆盖率 92%(目标 85%)。
- 测试扩充至 543 个,覆盖率 89%(目标 85%);新增用例覆盖:结构级脱敏、飞书业务码 fail-closed、
  队列排空超时、撤单与成交竞态、只平演练自己开的仓、告警"发出/送达"区分、缺天打断连续段、熔断类告警计数。
- 测试扩充至 559 个,覆盖率 89%(目标 85%);新增用例覆盖:周末日志不计入交易日连续段、周末严重事件仍打断连续段、
  键名脱敏、非 JSON 标量脱敏、演练拒绝在锁仓账户上运行、净持仓账户方向单列、平仓价挂在交易的那一侧、
  退出时按通知器排空预算关闭。
- 测试扩充至 565 个,覆盖率 89%(目标 85%);新增用例覆盖:演练结果文件连续两次重写仍更新主记录、
  采集文档的 BEGIN 标记不被丢掉、排空预算计入正在发送中的消息、FATAL 被提到队首、
  对端卡死时 `close()` 按上限返回。
- 测试扩充至 572 个,覆盖率 89%(目标 85%);新增用例覆盖:净持仓账户上平掉演练自己开的仓、
  平仓没成交时撤单、队列满时 `close()` 仍能停住后台线程、关闭后拒绝再收消息、
  数字键等于密钥时被脱敏、通知器关闭抛异常时 shutdown 仍能走完。
- 分钟线时间戳由新浪的"K 线结束时间"前推一个周期,统一为 vn.py 的"开始时间"。
- **文档同步(2026-11-10)**:PROJECTWIKI 补充 15 分钟基准模块、购买数据的派生数据流、GUI 入口、容器内连通 SimNow 未验证等条目;
  `docs/runbook-simnow.md` 与 `docs/live-readiness.md` 刷新 Phase 3–5 的实际状态与剩余实测项;Issue #1 的阶段勾选同步。
- 明确记录:**15 分钟基准的测试段(2024-01 ~ 2026-06)已被使用一次**,新候选策略不能再用它做最终确认。

### Fixed（修复）
- **第八轮 codex 审查(2026-11-10,clink + codex CLI 动态复现)追加修复** —— 1 个 High / 1 个 Medium,
  均为本轮看门狗/废单改造引入或暴露的问题,修复与新增回归测试在同一原子提交内:
  - **[High] 跨交易时段重入时看门狗误报「行情假死」并误撤新挂单**:`_last_tick` 是单调时钟时间戳且从不按
    交易时段边界重新武装,午休/夜盘休市后定时器若先于本时段首 Tick 触发,会把整个休市时间(实测复现 7261s)
    计为静默,对年龄 0 秒的新开仓单也强撤,而策略 `pending_entry` 一次性消费不重发,等于静默丢失入场。
    → 运行时记录上一次定时器的时段状态,在「非交易→交易」边沿把 `_last_tick` 置 None、解除假死边沿,
    等本时段首 Tick 后才开始计 60s 静默;初始基线在 `start()` 记录,首个定时器不被误判为边沿。
  - **[Medium] 已当日锁定后每笔硬拒绝重复发 FATAL,且「已撤销全部挂单」与实际未撤单不符**:FATAL 原在
    `_lock_daily()` 之前无条件发射。→ 仅在锁定状态真实转换(`guard.can_open()` 为真)时发 FATAL,
    已锁定后的后续硬拒绝只保留 ERROR「委托被拒」留痕。
  - **同类既有问题一并修复**:规则自身的 120s 开仓委托超时也按含休市的单调时间计时,午休两小时后旧挂单会在
    开盘瞬间被撤。→ 运行时把交易时段判定注入 `AccountGuardRule.session_active`,休市期间不做超时撤单,
    时段重入时把在跟踪挂单的计时起点拨到开盘时刻(休市时间不计入挂单年龄)。
  - 新增 3 个回归测试:时段重入不误报(运行时)、规则超时剔除午休(运行时夹具)、锁定后重复硬拒绝只发一次 FATAL(集成)。
  - 第二轮 clink codex CLI 复核(动态验证夜盘 21:00 重入、休市中启动、启动失败早退三条路径,无 High/Medium),
    其 4 条 Low 建议全部处置:① 时段边沿的 `_cancel_requested` 清理收窄为只拨开仓单 id,平仓/紧急平仓撤单的
    10s 节流不被休市边沿清掉;② 规则 `_was_in_session` 基线在运行时注入时同步,与运行时 `_in_session` 同一初始认知;
    ③ 补仓库级测试 3 个(夜盘重入、休市中启动、节流清理范围);④ 值班手册说明「日锁后硬拒绝只有 ERROR 属预期」。
    测试由 611 个增至 614 个。
- **第五轮 codex 审查(2026-11-10)追加修复** —— 2 个 Medium / 4 个 Low:
  - **[Medium] 有日志的周末被当成交易日,"连续 5 个交易日"凭空多出两天**:容器是常驻的,周末照样有日志文件
    (周五夜盘跨过零点还会写进周六的文件 —— `trading_day()` 把 20 点后的夜盘归到下一个工作日)。
    `longest_clean_streak()` 原先"有日志就算一天",周末的日志文件于是被算成交易日,验收结论虚高。
    → 周末**既不计入也不打断**;但周末日志里的**严重**事件(紧急平仓、熔断、硬锁定、对账失败…)
    仍然打断连续段(夜盘跨零点的事故不能因为落在周六就被放过),周末的**警告**(空闲期连接抖动)不打断
    (否则验收标准永远达不成)。
  - **[Medium] 演练只看净持仓,多空锁仓(净额 0)时可能误平既有仓**:`latency` 场景进场前只检查净持仓 ≠ 0,
    而多空锁仓(各 1 手)的净持仓是 0 → 演练照跑,收尾平仓时会动到演练之前就存在的仓位。
    → 进场前按**总手数**判断(`gross_position()` > 0 就拒绝演练),进场时的总手数记进 `gross_baseline`,
    收尾用 `at_baseline()` 同时核对净额**与**总手数;`net_position()` 里 `Direction.NET`(净持仓账户)
    单列处理,既不算多也不算空 —— 不能当成空头去平。
  - **[Low] 演练的平仓价一律用买一,平空时永远成交不了**:`_close_position()` 平多(卖出)与平空(买入)
    都用 `bid_price`(买一)。平空是**买入**,挂在买一(比卖一低)上等不到成交 → 演练变成"平仓超时",
    还容易被误判成"平不掉"。→ 平多(卖出)挂**买一** `bid_price`,平空(买入)挂**卖一** `ask_price`。
  - **[Low] `extra` 的脱敏漏了两处,密钥照样能发出去**:① dict 的**键**没脱敏 —— 键名是密码时只脱敏值等于没脱敏;
    ② 非 JSON 标量(datetime、Decimal、自定义对象)最终会被 `json.dumps(default=str)` 转成字符串,
    **先序列化再脱敏就来不及了** —— `str(obj)` 里的密码会原样发出去。
    → 键名同样走 `redact()`;非 JSON 标量在 `redact_obj()` 里**先转成字符串再脱敏**(数字/布尔/None 原样保留)。
  - **[Low] `close()` 的形参名与"单条上限"同名,极易误用**:形参 `flush_timeout` 与 `flush_timeout` 属性
    (单条消息的最坏耗时)同名,但两者含义不同(一个是**排空整队列的总预算**)。
    → 形参统一改名为 `drain_timeout`(与 `drain_timeout` 属性同名同义),并同步 `Notifier` 协议、
    `NullNotifier`、`RecordingNotifier` 与全部调用点。
  - **[Low] 退出时的"仍有持仓" FATAL 可能随进程退出一起丢掉**:`shutdown()` 在关闭通知器前发这条 FATAL,
    而关闭的默认预算是 10 秒 —— 一条消息的最坏耗时是 (1+重试次数) × 单次超时 + 全部退避(默认 34 秒),
    慢一点的对端就收不到这条兜底告警。→ 按通知器自己的 `drain_timeout` 关闭。
  - **[Low] `EVENT_MARKERS` 一行日志可能命中多个标记**(措辞有重叠),"异常事件"列计数偏大 ——
    **只影响展示**,判断"这一天有没有异常"只看有没有事件,多算几次不改变结论。已在代码里注明,不改动行为。
- **第六轮 codex 审查(2026-11-10)追加修复** —— 2 个 Medium / 2 个 Low(主逻辑全部确认正确,以下是审查中新发现的缺陷):
  - **[Medium] 演练结果文件重写一次后丢掉 `END_MARK`,之后再跑就不再更新主记录**:`drill.write_results()`
    重写时只写正文、不写回标记 → 第二次运行 `old.find(END_MARK)` 返回 -1 → 走"没有标记"的分支,
    结果只落到 `simnow-drills-raw.md`,主演练记录停在旧结果上**且不报错**。
    → 重写时把 `BEGIN_MARK` / `END_MARK` 一起写回;补"连续跑两次仍更新主文件"的回归用例。
    同一类缺陷在采集脚本里也存在(`write_collected()` 重写时丢 `BEGIN_MARK`,连"重新采集会替换这一段"这句
    人话一起丢),一并修掉。
  - **[Medium] 排空预算没算"正在发送中的那条"**:`drain_timeout` 只按队列长度(`qsize()`)算,
    而 worker 已经取走、还没发完的那条**不在队列里** → 预算少一条 → 退出时最后那条 FATAL 告警
    随进程一起消失(这正是要兜底的场景)。→ 新增 in-flight 计数,`drain_timeout` 按"队列剩余 + 正在发送"计算。
  - **[Low] 排空预算没有上限**:队列上限 200、单条最坏耗时约 34 秒 → 对端持续超时且队列塞满时,
    排空预算接近 2 小时,交易进程会被吊在退出阶段。→ 等待时间封顶 `MAX_DRAIN_TIMEOUT`(300 秒),
    并在关闭前把 FATAL 提到队首 —— 宁可丢一部分非 FATAL 告警,也不能让退出流程拖住。
  - **[Low] 平仓价的注释把买一/卖一写反了**:vn.py 的 `bid_price` 是**买一**、`ask_price` 是**卖一**。
    行为一直是对的(卖出挂买一、买入挂卖一),但注释与变更日志写成"平多用卖一"会误导后续维护者 → 改正术语。
- **第七轮 codex 审查(2026-11-10)追加修复** —— 2 个 High / 1 个 Medium / 2 个 Low:
  - **[High] 队列满时 `close()` 停不住后台线程**:停止哨兵是**追加**到队列末尾的,
    队列满(`queue.Full`)时根本放不进去,而这个异常被吞掉 → worker 永远收不到停止信号,
    `close()` 只能干等到排空预算耗尽(最坏 200 条 × 单条最坏耗时)。
    → 改用独立的 `_closed` 标志:后台线程取队列超时后检查该标志,排空即退出;关闭后再 `send()` 一律拒绝
    (消息只会躺在队列里没人发,不能让调用方以为收下了)。
  - **[High] 净持仓账户上,演练会把自己的仓位留在账户里**:CTP 的净持仓账户回报的是 `Direction.NET`,
    净额算出来是 0 → `_close_position()` 认为"没有仓位要平",**不发平仓单**,
    留仓手数还被误报成 0 手。演练其实知道自己开了 1 手多仓,却没记下来。
    → 演练记录**自己开出的方向与手数**(`owned_volume` / `owned_direction`),净额看不出来时按它平
    (开多仓就用卖出平);留仓手数改用**总手数**统计,不再按净额报 0。
  - **[Medium] 平仓单没成交时没有撤单**:平仓单还挂在交易所上,过一会儿成交就会留下一笔"晚到的"持仓,
    账户状态与演练结论不一致 → 平仓失败路径补撤单。
  - **[Medium] 通知器关闭抛异常会打断 shutdown**:只捕获 `TypeError`,其它异常会沿着 `finally` 冒出去,
    把退出码与后续清理一起打断 → 兜底捕获并记日志(写日志本身也不再抛)。
  - **[Low] 数字键等于密钥时仍会泄漏**:`{12345: "值"}` 原样保留数字键,
    而 `json.dumps` 会把它变成字符串 `"12345"` 发出去 → 非字符串键按 JSON 形态先脱敏,命中就换成脱敏后的字符串。
- **飞书机器人"失败也返回 HTTP 200",告警会静默丢失**:`notify/webhook.py` 只看 HTTP 状态码,把
  `{"code":19001,"msg":"invalid token"}` 这种"根本没送达"算成发送成功(计入 `sent`)。运行手册的验收标准恰恰是
  "告警全部到达手机",这个缺陷会让验收结论失真。现新增 `_body_error()` 解析响应体,业务码(`code` / 旧版 `StatusCode`)非 0
  一律算失败并走重试,`last_error` 脱敏后保留;业务码可能是字符串 `"0"`,比较时用 `int()` 安全转换,不会把成功误判成失败。
  **响应体不是 JSON(代理/网关回 HTML 2xx)、不是 JSON 对象、或没有业务码 → 同样判为失败**:
  真实的飞书一定回 JSON 并给业务码,拿不到就说明消息没被接收,按成功处理就是静默丢告警。
- **webhook 自检的等待时间短于最坏重试耗时**:慢一点的对端会被误判成"没送达"。等待时间改按
  "重试次数 × 单次超时 + 全部退避"计算(新增 `flush_timeout` 属性)。
- **webhook 计数器在多线程下丢数**:`sent`/`failed`/`dropped` 由发送线程与后台线程同时写,
  `+=` 不是原子操作 → 用独立的 `_counter_lock` 保护。
- **第四轮 codex 审查(2026-11-10)追加修复** —— 2 个 High / 4 个 Medium / 5 个 Low:
  - **[High] 演练的撤单与成交竞态会留下仓位且没有告警**:`scripts/drill.py` 的 `latency` 场景在
    `wait_for_trade()` 超时后直接撤单返回。CTP 的撤单是**异步**的,撤单请求发出前后订单仍可能成交,
    此时仓位留在账户上,却走不到"留仓 FATAL"分支。→ 撤单后用 `wait_flat(close_timeout, baseline)`
    确认净持仓真的回到 baseline;仍有残留(撞车)就平掉它、发 FATAL 告警、场景判失败。
  - **[High] 演练按账户净持仓平仓,会平掉演练之前就有的仓位**:`_close_position()` 用的是**账户级**净持仓,
    目标账户上本来就有同品种持仓的话,演练会连别人的仓一起平掉。→ 进场前记 `baseline = net_position()`,
    平仓量 = **当前净持仓 − baseline**,方向按 delta 正负取反;`latency` 进场前净持仓 ≠ 0 时**直接拒绝演练**
    (分不清哪一手是演练开的,强行平仓就是误伤)。
  - **[Medium] 告警"发出"与"送达"混为一谈**:`RecordingNotifier.send()` 无条件把告警记成已记录,
    底层 `send()` 返回 False(去重/级别过滤/队列满/渠道未配置)时手机端其实没响。→ 每条告警带 `delivered` 标记,
    新增 `delivery_summary()` 区分「发出/底层接受/未送达」,并把**渠道没配**与**送达失败**分开报。
  - **[Medium] 进程静默死亡会让"连续无异常天数"虚高**:`longest_clean_streak()` 只遍历日志里有的天,
    静默死亡那天**根本没有日志文件** → 被悄悄跳过 → 连续段被拉长。→ 按日历逐天走,
    **工作日缺日志就打断连续段**;周末不是交易日,缺日志不打断。新增 `missing_weekdays()` 把缺口列进验收清单。
  - **[Medium] `--help` 在 cp1252 控制台上直接崩**:`_setup_stdout()` 原先在 `parse_args()` 之后调用,
    而 argparse 的用法说明里有中文 → `UnicodeEncodeError`,连用法说明都看不到。→ 提到 `main()` 最前面。
  - **[Medium] `close()` 的等待时间只够一条消息**:`flush_timeout` 是**单条**上限,队列里排着 N 条时
    `close()` 会提前返回,最后几条随进程退出丢掉(`sent` 偏小 → 慢端被误判成"没送达")。→
    拆成 `flush_timeout`(单条)与 `drain_timeout`(单条 × 队列剩余条数),`close()` 的参数改为**总预算**。
  - **[Low] `extra` 的脱敏在错误的层次上做**:先序列化成 JSON 字符串再 `str.replace` ——
    序列化时引号/换行已被转义,`replace` 找不到原样的密钥 → 密钥留在 `extra` 里发出去;
    密钥本身含 JSON 语法字符时,替换还会把整条 JSON 弄坏。→ 新增 `redact_obj()` 在**结构上**逐个字符串脱敏。
  - **[Low] 飞书业务码为 `None` 时被当成成功**:`bool(None)` 是 False,原来的写法把"无法确认"判成"已送达"。→
    业务码解析改为 fail-closed:`None` / 空串 / 非数字一律判失败。
  - **[Low] `EVENT_MARKERS` 漏了熔断类措辞**:守卫与运行时的熔断告警(当日熔断、累计死线触发等)不在表里,
    熔断日会被当成"干净的一天"。→ 补齐措辞;「紧急平仓」用**带级别前缀**的 `"[FATAL] 紧急平仓:"`,
    既不误伤正常的 "[INFO] 紧急平仓完成",也不与 "紧急平仓失败" 重复计数。
  - **[Low] `render_table` 的 `heartbeat_day` 形参遮蔽同名模块函数**;`print(text[:0], end="")` 是死代码。→ 均已清理。
  - **[Low] `risky_scenarios()` 依赖调用方先归一化地址**:调用方忘了归一化,白名单判断就失效
    (同一个地址的两种写法会得到不同结论)→ 地址在函数内部自己归一化。
- **演练的"本次场景"判定有两个方向的错**(2026-11-10):
  - *假阳性*:断线/停更这类告警可能在场景动作之前就发过,不设下界会把旧告警当成本次场景的结果。
    `RecordingNotifier` 新增 `mark()`(记下当前告警条数)与 `find/wait_for(..., since)`。
  - *假阴性*:熔断场景原来是**先注入权益、再打标记**,而守卫的定时器线程随时可能在注入的瞬间就发出告警 ——
    那条告警落在标记之前,被 `since` 过滤掉 → 场景明明触发了熔断却报"没有触发"。已改为**先打标记再注入**。
- **演练开出去的单没成交就撤单**:否则那张单一直挂着,演练结束后才成交,仓位留到下一次运行里(演练污染实盘状态)。
- **演练的平仓单被交易所拒单**:上期所/能源中心要用 `Offset.CLOSETODAY`(演练开的是当日仓),
  用普通平仓会被拒 → 留仓 → 演练"通过"却把仓位留在模拟账户里。现按交易所选 offset,
  并且**平不掉就判失败并发 FATAL 告警**。
- **演练的"硬锁定已落盘"只是文档里的说法**:现在会读风控状态文件确认 `hard_locked` 已写盘,不落盘则场景判失败。
- **演练的时段窗口与运行时不一致**:演练原来硬编码交易时段,而运行时的看门狗用的是策略的 `ENTRY_SESSIONS`。
  两处不一致 → 演练说"在交易时段"而运行时说"不在",看门狗不检查,场景必然假失败。现直接复用同一份。
- **逃生舱只有一道**:地址不在 SimNow 白名单时,`--allow-any-address` 只是"我确认过"的一次声明。
  而 `latency`/`daily-loss`/`hard-lock` **会真的发委托**,后果是真金白银 → 在非白名单地址上跑这类场景
  必须再加 `--confirm-any-address-trades`;用了逃生舱的警告会写进结果文件当审计记录。
- **SimNow 地址白名单按编号规律外推**:`40002`/`40003` 是按第一套的编号规律外推出来的,官网第二套**只公布了 `40001`**,
  没有官方背书 → 已从白名单移除。另外地址先经 `normalize_address()` 归一化(去 `tcp://` 前缀、空格、路径),
  否则"长得不一样"会被误判成非 SimNow,反而逼人用逃生舱。
- **每日证据表的心跳日期算错**:`heartbeat_day()` 用 `datetime.fromtimestamp`(本机时区,宿主机 UTC-8),
  而容器跑的是 Asia/Shanghai,差 16 小时 → 容器 11-10 08:00 的心跳被算成 11-09,挂到错误的交易日那一行。
  现显式用 `ZoneInfo("Asia/Shanghai")` 换算。
- **心跳可能整列从证据表里消失**:心跳文件不存在、或它最后刷新的那天没有日志时,心跳完全不出现 ——
  而"进程静默死亡"这条证据恰恰靠这一列,验收项"没有进程静默死亡"就看它。现改为挂到**最后一行**。
- **日志时间戳带毫秒,按秒解析全部失败**:真实日志行首是 `2026-11-10 15:36:11.123 | INFO | ...`,
  按 `%Y-%m-%d %H:%M:%S` 解析会抛 `ValueError` → 整天的起止时间全丢。现先砍掉小数部分再解析。
- **"总共干净了几天"不能顶替"连续几天"**:验收要求连续 ≥ 5 个交易日,中间夹一个有异常的日子连续性就断了。
  新增 `longest_clean_streak()` 算最长连续段。
- **空的证据表不是证据**:一个 `vt_*.log` 都没有时静默生成空表并退出 0,会让人误以为"当天没问题"。
  现改为警告 + 退出码 1。
- **Windows 控制台编码会让脚本"假死"**:默认 GBK/cp1252 打印中文与 ✅ 会抛 `UnicodeEncodeError`,
  文件已写好但进程带异常退出(码 1)。新增 `_setup_stdout()` 统一按 utf-8 + errors="replace" 重配 stdout/stderr。
- **CTP 的拒单日志是 INFO 级别,原来永远收不到**(2026-11-10 SimNow 实测):`vnpy_ctp` 的 `write_error()` 实际调用
  `write_log()`,所以"交易委托失败,代码：21,信息：…"以 INFO 级别写出。`trading/guard_rule.py` 只认 `log.level >= ERROR`,
  导致废单分类永远拿不到原因(一律按硬拒绝),紧急平仓单的有限重试形同虚设。
  现按**精确措辞** `REJECT_LOG_MARKERS = ("交易委托失败", "交易撤单失败")` 识别,`trading/runtime.py` 的网关错误告警同步修复
  (运行时告警另用宽措辞 `GATEWAY_ERROR_MARKERS`,只给人看、分类错误没有后果);
  `tests/fakes.py` 的假网关也改成 INFO 级别,免得掩盖这个缺陷。措辞收窄的理由:宽泛的"失败"会把
  "行情服务器登录失败""行情订阅失败"也收进来,真废单日志迟到时就拿这些无关文本去分类,把可重试的软拒绝误判成硬拒绝。
- **废单关键词冲突**:`该交易席位未连接到交易所`(SimNow 非交易时段的真实拒单文本)含软关键词"连接",
  被误判成可重试的软拒绝;已显式加入硬关键词表。
- `scripts/collect_reject_texts.py`:① 竞态——CTP 先推订单回报、后写错误日志,脚本拿到拒单回报就翻日志,
  必然抓不到(2026-11-10 首次采集白跑一趟);已加 `wait_error` 等错误日志到达。② `close()` 会 join CTP 原生线程,
  4097 断连风暴下不返回,脚本"跑完不退出";改为限时关闭 + `os._exit` 兜底。
  ③ 交易时段内跑采集时,"非交易时段开仓""超大手数"两个场景用的是合法价格,**可能成交留下持仓**;
  现按 `in_trading_session()` 判定,时段内改用越界价并跳过时段场景。④ 价格未按 `price_tick` 对齐,
  会被 CTP 按"价格非法"拒单,测到的不是想测的场景;已统一 `round_to`。
- **第二轮 codex 审查追加修复**(2026-11-10):
  - 采集脚本只靠 `--confirm-simnow` 布尔开关挡不住"配置写错连上真券商"→ 新增 SimNow 官方地址白名单,
    不在表内直接拒绝连接(`--allow-any-address` 为强制逃生口)。
  - 场景之间的错误会串味(上一个场景的旧错误离本次发单更近)→ `Collector.error_index()` + `since` 参数。
  - **"无持仓时平仓"场景可能真的平掉账户上的持仓** → 发单前 `symbol_has_position()` 查持仓,
    账户上有任何持仓都中止采集(退出码 5);查询必须等满整个窗口(默认 15 秒),
    否则 CTP 异步到达的持仓回报会被漏掉、误判成空仓。交易连接断开同样中止。
  - 早退路径不关引擎 → `main()` 用 `try/finally` 统一走限时 `close_engine()`,采集主体拆到 `_collect()`。
  - 事件线程写、主线程读列表 → `Collector` 加锁,读取走快照。
  - 多网关时错误张冠李戴 → `_errors` 记来源网关,`_error_text_near(ts, gateway_name)` 按网关过滤,
    并优先取废单回报**之后**到达的日志。
- **第三轮 codex 审查追加修复**(2026-11-10):
  - **交易所级废单的原因拿不到**:`REJECT_LOG_MARKERS` 只认"交易委托失败""交易撤单失败",
    但 CTP 的交易所拒单走 `onRtnOrder`(`vnpy_ctp/gateway/ctp_gateway.py:673-684` 把状态映射成 REJECTED),
    原因在 StatusMsg 里、日志措辞是"委托 {委托号} 状态更新，{原因}"(同文件 713-719),
    **不经过** `onRspOrderInsert`。漏掉它 → 交易所拒单原因永远是"未知"→ 一律按硬拒绝 →
    平仓单被提前升级为人工处理、丧失重试机会。现已加入 `"状态更新"` 措辞。
  - 发单前查持仓的等待窗口可能不够 → 查询改为**等满整个窗口且查两轮**(默认 15 秒 + 7.5 秒):
    CTP 的持仓回报是异步的,而且对空账户**不推送任何持仓事件**,
    "没收到回报"既可能是真空仓也可能是回报没到。
  - `Collector` 的 `orders/ticks/contracts` 字典未加锁(与 `errors` 的保护方式不一致)→ 统一加锁,
    读取改走 `contract_of/tick_of/order_of` 快照方法。
  - 交易时段窗口未覆盖 CFFEX 的 13:00 下午盘 → 下午盘起点由 13:30 提前到 13:00(保守取并集)。
  - `--allow-any-address` 的帮助文本与警告改为明确提示"可能连上真实资金账户"。
- **SimNow 第二套对新注册账号不可用**(2026-11-10 实测):连 `182.254.243.31:40001` 时行情登录成功、
  交易登录回 `代码：3,信息：CTP:不合法的登录`;新账号要到第三个交易日才能用第二套。
  采集脚本现在检测到登录失败会**立刻退出(退出码 6)**并说明原因,不再白等 180 秒超时。
- **Docker 容器连不上 SimNow 的根因**:`vnpy_ctp` 的 `toUtf()` 硬编码 `std::locale("zh_CN.GB18030")`,Debian slim 镜像默认无此 locale,CTP 回调线程抛异常导致进程 abort(无 CTP 错误日志)。Dockerfile 已加 `locales` + `localedef -i zh_CN -f GB18030 zh_CN.GB18030`。
- **Docker 容器内完整连上 SimNow**(2026-11-10 实测):连接→授权→登录→结算确认→合约表→策略初始化→订阅→容器 `healthy`→`docker compose stop` 优雅退出(码 0)。
- `scripts/backtest_lib.py`:回测异常检测改用精确哨兵(`Traceback` / `触发异常`),避免普通日志含"异常"二字被误报;`run_engine_bars` 补充统计口径说明(按自然日分组,与日线基准的夏普不可直接比较)。
- `scripts/run_gui.py`:合约事件去重,CTP 重连重发合约表时不会重复订阅;自动订阅的合约缺交易所后缀时给出提示而不是静默不订阅。
- `scripts/optimize_15m.py`:`--data` 换文件后 K 线缓存按路径取键,不会继续用上一份数据。
- `trading/runtime.py`:网关连接只通知一次(行情/交易各报一次"登录成功"不再重复告警)。
- 修正 `DB_TZ` 与系统时区不一致导致 vn.py 读回 K 线整体偏移 16 小时的问题(根因:`vnpy_sqlite` 读取时按系统本地时区解释无时区时间)。
- 数据源 OHLC 自相矛盾的行改为修复而非丢弃(PP 2020-08-06、L 2007-07-31 各 1 行)。
- CSV 改为先写临时文件再原子替换,避免写入中断损坏已积累的分钟历史。
- 邻域平滑选参的偏差:边缘格子邻居更少导致得分偏高、无效邻居被忽略使孤立尖峰胜出、负分区域里按 0 计无效邻居偏乐观;
  现在只有完整 3×3×3 邻域的格子可选,无效邻居按悲观下限计。
- 测试不再依赖 vn.py 枚举的本地化字符串(原先在中文环境下 4 个测试会失败)。
- 策略委托价按 `pricetick` 取整(止损取更紧的一侧),避免实盘因价格不是最小变动价位的整数倍被拒单。
- `backtest_lib` 的异常检测改为精确哨兵,不再因策略日志里出现"异常"二字而误报。

- 复权连续合约经 codex 独立审查与外部模型验证后修复:换月改用前一交易日持仓量(消除前视)、主力停牌不再触发虚假换月、
  无共同交易日时价差近似并告警(原先静默为 0)、取数缓存不再把"暂时取不到"永久缓存成"不存在"。

- 通知脱敏:`requests` 异常文本只含 URL 路径,Telegram token 曾因此可能进入 `last_error`;现在 URL 的路径、查询串和各路径段都参与脱敏。
- 紧急平仓不再依赖陈旧的 OMS 持仓:CTP 持仓回报滞后会导致成交后重复发平仓单;现在按成交量对账,持仓没清零时主动查询并等待,绝不重发。
- 当日熔断的平仓失败后,随后的硬锁定可以重新开一轮平仓(原先会被卡死的状态机吞掉)。

- 优雅停机会误触发紧急平仓:`stop_all_strategies` 把 `trading` 置假,看门狗把它当成"策略崩溃"。现在停机先关看门狗,收到停止请求后看门狗也不再触发。
- 启动安全:启动时撤销遗留委托后等待其终结(否则拒绝启动)并重新确认持仓;持仓回报连续两次一致才对账;状态目录(含风控状态、心跳、CTA 策略数据)不可写时拒绝启动,
  避免硬锁定写不进磁盘、重启后被"洗掉"。CtaEngine 恢复变量前先删除持久化的 `trading/inited`,消除可交易时间窗。
- 新增运行中持仓漂移看门狗:账户持仓与策略持仓不一致持续超过容忍时间 → FATAL 告警并紧急平仓。
- 紧急平仓:没发过平仓单就看到空持仓时先主动查询并等待(启动早期持仓回报可能未到);重开一轮前先撤掉旧的活动平仓单;完成时撤掉残留平仓单;
  硬锁定事件 flat=False 与 flat=True 各发一次(平仓先失败后成功仍能让进程退出)。
- 启动阶段收到停止信号能及时响应并以退出码 0 退出;`wait()` 改为分段等待(Windows 下 Ctrl+C 可中断)。
- 超时类参数加下限(持仓刷新 ≥3s、平仓回报 ≥3s、持仓稳定等待 ≥2s),配成 0 不会让检查形同虚设。
- Webhook 队列满时优先挤掉最旧的非 FATAL 消息,且不会挤掉停止哨兵。

- 风控状态与账户 ID 绑定:连接到不同账户时重置权益基准并告警(SimNow 2000 万的"最高点"不会把 1 万元账户误判为大幅回撤);硬锁定不因换账户而解除。
- `scripts/run_gui.py`:VeighNa Trader 图形界面入口(挂载账户风控,不含对账与看门狗,合约表到位后自动订阅 LIVE_VT_SYMBOL),用于人工观察与试验。
- 启动各阶段耗时(登录/结算确认/合约表)写入日志,启动超时告警里带上已到达的里程碑,便于判断慢在哪一步。
- `connect_timeout` 默认 60 → 300 秒(网关登录后需下载全市场合约表,SimNow 上实测 20 秒~3 分钟以上),超时告警写明原因。
- 首次在真实 SimNow(第一套)上跑通启动管道:连接/授权/登录/结算确认/策略初始化/订阅/优雅停止。

### Security（安全）
- 账号、密码、AuthCode 仅放 `.env`(已 `.gitignore`),仓库只提供 `config/.env.example` 模板。
- **演练脚本与实盘状态彻底隔离**:`scripts/drill.py` 用独立的 `.vntrader_drill/` 目录(策略数据、日志、心跳)与独立的
  `account_guard.json`,演练触发的熔断/硬锁定**不会污染实盘的 `data/state/account_guard.json`**。
- **演练只能在模拟盘上跑**:复用 `scripts/simnow_guard.py` 的官方前置机地址白名单,地址不在表内默认拒绝连接
  (CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,只有地址能区分)。
- **演练的熔断场景不碰真实账户权益**:用"注入权益来源"的方式模拟亏损,只改本进程内的取值,
  守卫判定、撤单、告警、状态落盘照常跑,场景结束即换回真实查询。

<!-- 比对链接 -->
[Unreleased]: https://github.com/laiyinyizao007/invest/compare/0981db3...HEAD

<!-- 归类指引（Conventional Commits → Changelog 分区）
feat: Added（新增）
fix: Fixed（修复）
perf / refactor / style / chore / docs / test: Changed（变更）或按需归类
deprecate: Deprecated（弃用）
remove / breaking: Removed（移除）并标注 BREAKING
security: Security（安全）
-->
