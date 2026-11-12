# 行情录制与增量运行手册

> 适用范围：Windows 本机、SimNow 默认 profile。本文不承诺 DSH Desktop 关闭期间持续录制，也不包含任何账号、密码或前置地址。

## 1. 安全边界

- `record_market_data.py` 只连接行情前置，订阅明确的 C/MA 具体合约，并调用官方 `vnpy_datarecorder`。
- 录制数据写入独立的 `.vntrader_record/database.db`，不使用交易和回测主库。
- 录制入口不加载策略、不发单、不查询账户、持仓或风控状态。
- SimNow 必须通过地址白名单校验；国信测试必须显式使用 `guoxin-test` profile 和 `CTP_ENVIRONMENT=测试`。
- 不把真实凭证写入仓库；运行时从 `.env` 或受控环境变量注入。

## 2. 离线配置检查

先在项目根目录准备 `.env`，至少填写 `CTP_USERNAME`、`CTP_PASSWORD`、`CTP_BROKER_ID`、`CTP_MD_ADDRESS`、`CTP_TD_ADDRESS` 和具体合约列表。然后运行：

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
  - **阻断（未修复，待决）**：CTP 的合约（`ContractData`）只来自交易前置的 `onRspQryInstrument`；行情前置收到 tick 时若无合约会直接丢弃（`ctp_gateway.py` 的 `onRtnDepthMarketData`）。本入口只连行情前置，所以不会订阅、也录不到数据。可选方案：(a) 受限地连交易前置，仅做认证与合约查询，并在 gateway 层禁用发单/撤单/查账户/查持仓；(b) 从可信的版本化本地合约元数据构造 `ContractData`。(a) 突破“不连交易前置”的原定边界，(b) 要维护乘数、最小变动价位等元数据。**这是安全边界决策，需用户确认后再改。**
  - **已修复**：DCE 合约被转大写后传给 DataRecorder 导致“找不到合约”；启动时不同步持久化录制列表，旧合约会继续被录；关闭时不 flush，丢尾部约 10 秒 tick/bar；每个 tick 同步写状态文件且写失败会终止事件线程；SimNow 白名单只校验交易前置而录制连的是行情前置（现要求 MD 主机与已通过白名单的 TD 主机一致）；wrapper 子进程继承调用方管道；`start` 不确认子进程存活；`watchdog` 与 `ensure` 相同、不查心跳；增量任务陈旧锁永久残留、超时不杀进程树、向子进程传递 CTP 凭证；PowerShell 身份查询无超时。
  - **未修复，待决或后续**：国信测试 profile 对地址没有任何校验（需要期货公司提供测试环境地址后建白名单）；DataRecorder 写线程异常后只置 `active=False`，状态文件不反映（需监听 recorder 日志事件）；PID 身份校验未核验 profile/state 目录/实例 token，POSIX 无命令行校验；`wrapper.lock` 无陈旧锁恢复；`last_bar` 字段从不更新；`database_dir` 允许任意路径且隔离只取目录名，未拒绝交易/回测主库。
  - 注：codex 对“venv 双层进程下 `subprocess.run(timeout)` 必然无限阻塞”未能复现；仅当后代进程持有输出管道时才会，本机用显式孙进程复现过（3 秒超时实际 30.2 秒返回）。`fetch_data.py` 自身不派生子进程，已改为树级终止作为加固。
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
