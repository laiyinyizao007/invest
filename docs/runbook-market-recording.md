# 行情录制与增量运行手册

> 适用范围：Windows 本机、SimNow 默认 profile。本文不承诺 DSH Desktop 关闭期间持续录制，也不包含任何账号、密码或前置地址。

## 1. 安全边界

- `record_market_data.py` 连接行情前置，并受限地连接交易前置（仅认证 + 查询合约清单，原因见 ADR），订阅明确的 C/MA 具体合约，调用官方 `vnpy_datarecorder`。
- 录制数据写入独立的 `.vntrader_record/database.db`，不使用交易和回测主库。
- 录制入口不加载策略；发单、撤单、查询资金与持仓在网关层和底层请求层都被硬性拒绝（有单元测试覆盖）；不读取风控状态。
- SimNow 必须通过地址白名单校验；国信测试必须显式使用 `guoxin-test` profile 和 `CTP_ENVIRONMENT=测试`。
- 不把真实凭证写入仓库；运行时从 `.env` 或受控环境变量注入。

## 2. 离线配置检查

先在项目根目录准备 `.env`，至少填写 `CTP_USERNAME`、`CTP_PASSWORD`、`CTP_BROKER_ID`、`CTP_MD_ADDRESS`、`CTP_TD_ADDRESS`、`CTP_APP_ID`、`CTP_AUTH_CODE` 和具体合约列表。国信测试 profile 还必须填写 `CTP_RECORD_ALLOWED_ADDRESSES`（逗号分隔的 host:port，取自期货公司书面测试环境参数，只放本机 `.env`，不入库），行情与交易地址都必须在其中。然后运行：

```text
python scripts/record_market_data.py --check
```

配置必须满足：

- `CTP_RECORD_SYMBOLS` 同时包含一个 `Cxxxx.DCE` 和一个 `MAxxxx.CZCE`；不接受 `C0` 或 `MA0`。
- `CTP_RECORD_TICK` 与 `CTP_RECORD_1M` 不能同时为 `false`。
- SimNow 使用 `simnow` profile；国信测试使用 `guoxin-test` 且环境必须为 `测试`。
- `--check` 只做配置校验，不连接柜台、不创建录制数据库。

## 3. 启停与状态

DSH 只调用短任务，不直接承载长期 DataRecorder：

```text
python scripts/record_market_data_wrapper.py start
python scripts/record_market_data_wrapper.py ensure
python scripts/record_market_data_wrapper.py watchdog
python scripts/record_market_data_wrapper.py status
python scripts/record_market_data_wrapper.py stop
```

wrapper 使用 `.ops/recording/wrapper.json` 保存子进程身份，使用 `wrapper.lock` 防止并发启动，使用 `status.json` 暴露心跳和最近 Tick/Bar 时间，使用 `events.ndjson` 记录生命周期事件。`stop` 只会在 PID 仍能被证明属于本录制入口时发送停止信号；无法证明身份时 fail-closed 返回非零。

## 4. 有界增量任务

增量任务只运行 `fetch_data.py` 的有限品种和周期，默认超时 900 秒，不是常驻进程：

```text
python scripts/record_market_incremental.py --symbols C MA --periods 1 15 --timeout 900
```

任务使用 `incremental.lock` 防止重入，把最近一次结果写入 `incremental.json`，并保留 stdout/stderr 尾部用于诊断。返回码：

- `0`：下载任务成功；
- `2`：任务被锁或参数非法；
- `124`：超过有界超时；
- 其他非零值：底层 `fetch_data.py` 失败。

重复执行安全依赖 `fetch_data.py` 的时间戳去重、数据库 upsert 和 CSV 原子替换；DSH 应按交易日周期调用，不应设置“无限 timeout”。

## 5. 验收顺序

1. 先运行离线配置检查和单元测试：`pytest -q tests/test_recording_tools.py`。
2. 使用 dummy worker 验证 Windows 子进程生存、进程树停止、PID 重用保护、锁释放和状态文件原子写入。
3. 验证 DSH 重启后的 `ensure`/`watchdog` 行为；DSH 关闭期间的录制缺口必须如实记录。
4. 取得明确的行情连接授权后，才使用 SimNow 或国信测试 profile 做行情验收。
5. 检查 `.vntrader_record/database.db`、`status.json` 和事件日志；不得把测试账号或连接细节复制进提交、日志或文档。

### 验收记录

- **第 2 步（dummy worker，2026-11-12，Windows 11 + venv Python 3.13，已通过）**：用不连接任何行情的 dummy worker（带孙进程、响应 `CTRL_BREAK`）验证了：启动；重复 `ensure` 识别已有进程而不重复拉起且不伤及它；锁被占用时启动返回 2；状态文件指向无关存活 PID 时 `stop` 返回 2 且不动该进程；`stop` 优雅退出（worker 收到 `CTRL_BREAK`）、孙进程随之退出、状态文件清除；无进程时 `stop` 返回 0。
- **实测发现的缺陷（已修复）**：`pid_alive()` 原先用 `os.kill(pid, 0)`，在 Windows 上对存活进程抛 `WinError 87` 被判为已退出，且对已退出进程可能误判存活。后果是 `ensure`/`watchdog` 每次都重复拉起、`stop` 永远拒绝。现改用 `OpenProcess` + `GetExitCodeProcess`，回归测试 `test_pid_alive_reports_real_process_state`。
- **注意**：venv 下 `python.exe` 是启动器，wrapper 记录的是外层启动器 PID，真实解释器是其子进程；身份校验依赖命令行含录制脚本路径，两层都满足。
- **codex 深度审查（2026-11-12，G8 第 2 轮；第 1 轮 `codereview` 的外部专家因 Gemini 免费额度用尽未能验证，仅有本地逐行核查）**，对照已安装 vnpy 源码核实后的结论：
  - **阻断（已修复）**：`MainEngine` 已启动事件引擎，原代码再次 `start()` 必抛 `RuntimeError`，录制进程在连接前就会退出。
  - **阻断（已按用户选择的方案 A 修复，2026-11-12；仍待 SimNow 实测）**：CTP 的合约（`ContractData`）只来自交易前置的 `onRspQryInstrument`；行情前置收到 tick 时若无合约会直接丢弃（`ctp_gateway.py` 的 `onRtnDepthMarketData`）。本入口只连行情前置，所以不会订阅、也录不到数据。可选方案：(a) 受限地连交易前置，仅做认证与合约查询，并在 gateway 层禁用发单/撤单/查账户/查持仓；(b) 从可信的版本化本地合约元数据构造 `ContractData`。(a) 突破“不连交易前置”的原定边界，(b) 要维护乘数、最小变动价位等元数据。用户选择 (a)：已实现受限交易前置连接（见 ADR 修订），离线单元测试与真实 `MainEngine` 冒烟（不连任何前置）通过；**真实前置上的认证、结算单确认、合约查询与 tick 入库从未验证**，需明确授权后在 SimNow 上验收。
  - **已修复**：DCE 合约被转大写后传给 DataRecorder 导致“找不到合约”；启动时不同步持久化录制列表，旧合约会继续被录；关闭时不 flush，丢尾部约 10 秒 tick/bar；每个 tick 同步写状态文件且写失败会终止事件线程；SimNow 白名单只校验交易前置而录制连的是行情前置（现要求 MD 主机与已通过白名单的 TD 主机一致）；wrapper 子进程继承调用方管道；`start` 不确认子进程存活；`watchdog` 与 `ensure` 相同、不查心跳；增量任务陈旧锁永久残留、超时不杀进程树、向子进程传递 CTP 凭证；PowerShell 身份查询无超时。
  - **未修复，待决或后续**：国信测试 profile 对地址没有任何校验（需要期货公司提供测试环境地址后建白名单）；DataRecorder 写线程异常后只置 `active=False`，状态文件不反映（需监听 recorder 日志事件）；PID 身份校验未核验 profile/state 目录/实例 token，POSIX 无命令行校验；`last_bar` 字段从不更新。
  - **后续已修复（2026-11-12，待 G8 复审）**：`wrapper.lock` 改为 OS 文件锁，持有者退出即释放（无陈旧锁问题）；`CTP_RECORD_DATABASE_DIR` 必须是项目根下的单层独立目录，否则配置检查拒绝。`pid_state` 三态化（alive/dead/unknown，只有进程确定不存在才算 dead），记录的 PID 状态未知时 `start` 拒绝拉起（返回 2，事件 `pid_state_unknown`），避免重复录制。**已知保留**：校验后到使用前被替换为 symlink/junction 的本地对抗场景未防护。
  - 注：codex 对“venv 双层进程下 `subprocess.run(timeout)` 必然无限阻塞”未能复现；仅当后代进程持有输出管道时才会，本机用显式孙进程复现过（3 秒超时实际 30.2 秒返回）。`fetch_data.py` 自身不派生子进程，已改为树级终止作为加固。
- **第 4 步部分完成（SimNow，用户授权，2026-11-12 约 16:30–16:50 中国时间）**：
  - 第一套前置（30001/30011）：当时不在交易时段，反复 `OnSessionDisconnected`，未连上；配置检查、wrapper 启动、优雅停止均正常，无数据。
  - 第二套 7x24 前置（40001/40011）：认证、登录、合约查询成功；`c2701.DCE`、`MA701.CZCE` 被识别并订阅；tick 持续到达，进程由 wrapper 拉起并优雅停止（返回码 0）。
  - **但第二套的行情时间不可信，DataRecorder 默认丢光所有 tick**：本机时钟经与百度/微软服务器核对误差 <1s，而 `c2701` 的 tick 时间比本机快约 417 秒，`MA701` 的 tick 日期停在 2026-11-10（郑商所报文日期未更新）。DataRecorder 会丢弃与本机时间相差 ≥60s 的 tick（`filter_window` 默认 60），所以正式录制库为 0 行。**不得为了凑数而放宽这个过滤。**
  - 入库链路验证：在一次性临时库中把过滤临时放宽，录制 40 秒得到 115 条 tick、2 根 1 分钟 K 线，价格合理（c2701≈2189），说明 DataRecorder→SQLite 写入与关闭刷盘是通的；临时库已删除。
  - 由此新增的保护：状态文件新增 `data_warning`，当任一合约的行情时间与本机相差 ≥ `filter_window` 时，状态由 `recording` 变为 `degraded` 并列出各合约偏差，避免“进程健康、心跳正常、实际一条没录”。
  - **仍待验收**：在第一套前置的真实交易时段（日盘 09:00–15:00、夜盘 21:00 起，中国时间）重做一次，那时行情时间应当真实，才能证明正式录制库有数据、1 分钟 K 线完整、合约换月处理。已知不足：状态值 `connected` 在前置实际未连通时也会出现（只表示进程在运行且尚无已订阅合约）。
- **尚未验证**：第 3 步（DSH 重启后的 `ensure`/`watchdog`、DSH 关闭期间的录制缺口）与第 4 步（授权行情端到端）。

在第 3、4 步完成前，只能称为“实验/开发配置”，不能宣称常驻录制或自愈已验收。

## 6. 故障处理

- `配置错误`：先修正环境变量，不绕过 profile、地址白名单或具体合约校验。
- `start_locked`：确认是否已有合法录制进程；不要直接删除 lock。确认无进程且锁为陈旧文件后，保留事件记录再人工清理。
- `stale_or_unknown_pid`：wrapper 拒绝停止，先人工确认 PID 和命令行，避免误杀其他进程。
- `status=starting/connecting` 长时间不变：检查行情前置可达性、交易时段和脱敏后的日志；不得因此自动切换到交易入口。
- 增量任务超时：保留 `incremental.json`，缩小 symbols/periods 或调整调度频率；不要取消超时限制。

## 7. 相关记录

- 架构决策：`docs/adr/20261111-market-recording-and-dsh-orchestration.md`
- 项目知识库：`PROJECTWIKI.md`
- 变更日志：`CHANGELOG.md`
- 跟踪 Issue：[#13](https://github.com/laiyinyizao007/invest/issues/13)

- **第 4 步（SimNow 第一套前置，交易时段，用户授权，2026-11-12 夜盘 21:01–21:09 中国时间，已通过）**：
  - 连接：交易/行情前置连接成功、授权验证成功、登录成功、结算信息确认成功（约 10 秒）、合约查询后 `c2701.DCE`、`MA701.CZCE` 被识别并订阅；状态 `connected` → `recording`，`last_tick` 持续推进，`data_warning` 始终为空（本机与前置时间一致，未出现第二套 7x24 那种时间偏差）。
  - 数据：一次 3 分钟连续录制，两个合约各约 400 条 Tick 入正式库 `.vntrader_record`（重复 Tick（symbol, datetime）0 组）；1 分钟 K 线连续无缺口（21:05–21:08），OHLC 自洽（0 条不一致）；两个合约都有数据。进行中的当前分钟不落库（下一分钟到达才完成），属 DataRecorder 正常行为。
  - 停止：`stop` 返回 0，停止后行数不再变化。
  - 自动重连：录制中交易前置断开（原因 8193）后约 1 秒自动重连、重新确认结算并重新订阅两个合约，无需人工。
  - **新发现的缺陷（未修）**：同一天第二次启动距上一次停止仅约 16 秒时，进程连接并登录成功，但**始终没有出现结算确认、没有订阅、没有 Tick，约 3 分钟内状态一直是 `connected`、心跳正常**——对 `watchdog` 来说是健康的，不会重启。再隔数分钟重启则正常。疑为 SimNow 对快速重复登录的服务端限制，但无论原因，「已连接却长时间未订阅/无 Tick」必须被状态机识别（建议：启动后超过阈值仍未订阅则置 `stalled`，`watchdog` 据此重启）。
  - **未完成**：DSH 重启实测（runbook §5 第 3 步）、国信测试环境；因此 Issue #13 仍不能关闭。

## 6. 交易时段验收清单（第一套前置）

> 前置条件：中国时间交易时段内（日盘 09:00–15:00，夜盘 21:00 起）。非交易时段第一套前置会反复 `OnSessionDisconnected`（原因 4097），属预期，不算失败。本机 `date` 的时区可能不是中国时区，以 `[DateTime]::UtcNow.AddHours(8)` 为准。

1. **预检**：`python scripts/record_market_data.py --check` 通过；确认 `.vntrader_record/database.db` 当前行数（记基线）。
2. **连接**：用第一套前置（30001/30011）启动 wrapper，5–10 秒内 `status.json` 应出现 `connected` → `recording`，`last_tick` 非空。
3. **数据**：持续 ≥3 分钟后停止；`dbtickdata` 行数 > 0，`dbbardata` 至少含 2 个合约各若干根完整 1 分钟 K 线（时间连续、无整分钟缺口）。
4. **时间偏差**：`status.json` 的 `data_warning` 应为空；若出现偏差告警，**不要放宽 `filter_window` 凑数**，记录偏差并如实写入验收记录。
5. **优雅停止**：`stop` 返回 0；日志里无 flush 超时告警；停止后行数不再变化。
6. **DSH 重启实测**：让 DSH 触发 `ensure`/`watchdog` 后重启 DSH，确认不重复拉起、不误杀，并如实记录 DSH 关闭期间的录制缺口。
7. **回填**：把结果写入上方「验收记录」（不写账号、密码与前置地址）；全部通过才关闭 Issue #13。

