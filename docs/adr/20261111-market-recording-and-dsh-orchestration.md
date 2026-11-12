# ADR 20261111：行情录制与 DSH 调度编排

## Status

Accepted（生命周期闸门已完成源码核对；wrapper 独立子进程行为仍待本机实测）

## Context

阶段一 SimNow 验收尚未退出，但分钟数据存在增量断档风险，且后续研究需要新的样本外行情。因此，行情录制与定时增量需要作为阶段一并行数据基础设施提前建设。

本任务必须使用官方 `vnpy_datarecorder` 录制 C/MA 的 Tick 与 1 分钟 Bar，并将数据写入独立的 `.vntrader_record/database.db`，不得污染回测主库 `.vntrader/database.db`。DSH 侧采用 `@goodandready/dsh-cron` 与薄插件，但录制入口不得发单、加载交易策略、访问账户或风控状态。

关键约束是不能把未经验证的长期前台进程能力写成可用能力。已核对 `@goodandready/dsh-cron@0.3.43` 的 npm 包和源码：

- 任务级 `timeoutSeconds` 缺省为 1800 秒（30 分钟）；
- script/python 等外部运行器缺省 `executionTimeoutMs` 为 900000 毫秒（15 分钟）；可用任务字段 `executionTimeoutMs` 或 `timeoutSeconds` 覆盖；
- 运行器始终设置有限超时；设置 0 或非正数不会表示无限，而会回退默认值；
- Windows 外部运行器超时会调用 `taskkill /pid <pid> /T /F` 终止进程树，并在 3 秒后再次强制终止；停止信号还会触发更短的强制终止路径；
- Windows `spawn` 选项不设置 `detached: true`；`detached: true` 只在 POSIX 分支启用。

上述事实足以证明：不能直接让 dsh-cron 的一次前台任务承载长期运行的 DataRecorder。它可以调度短任务，但不能通过填写一个“无限 timeout”值获得常驻语义。

## Decision

采用方案 B：使用短任务 `ensure`/`watchdog` 加上 invest 自己的生命周期 wrapper 管理独立 DataRecorder 子进程。dsh-cron 只负责周期触发、重试、心跳和自愈检查；wrapper 负责单实例、PID 身份校验、优雅停止、状态文件和事件日志；官方 DataRecorder 负责行情连接和数据落盘。

DSH Desktop 关闭期间不承诺继续录制。只有在后续本机实测证明 wrapper 的独立子进程在 DSH 重启/关闭后仍符合预期时，才能把“DSH 任务拉起并自愈”作为已验证能力对外描述。

### Decision Details

| Item | Content |
|------|---------|
| **Decision** | 短任务 `ensure`/`watchdog` + 独立 wrapper child，不用 dsh-cron 前台长期任务承载 DataRecorder |
| **Why now** | 阶段一期间就需要积累新的 C/MA 样本外数据，并降低分钟增量断档风险 |
| **Why this** | 适配 dsh-cron 的有限超时和 Windows 进程树终止语义，同时把录制生命周期控制留在可测试的本地 wrapper |
| **Known unknowns** | wrapper 的 detached child、生存、停止、PID 重用以及 DSH 重启/关闭后的实际行为尚未在本机完成端到端实测 |
| **Kill criteria** | 本机实测发现 wrapper 无法可靠识别/停止旧进程、出现 PID 重用误杀，或 DSH 重启后不能安全恢复时，撤回“由 DSH 拉起并自愈”的能力声明 |

## Rationale

### Options Considered

1. **方案 A：dsh-cron 前台长期任务**
   - 优点：结构简单，任务状态直接反映 DataRecorder 状态。
   - 缺点：dsh-cron 外部运行器具有有限超时；Windows 进程未 detached；超时会杀整个进程树，不适合常驻录制。

2. **方案 C：Windows 服务或任务计划程序**
   - 优点：适合 Windows 常驻进程，生命周期可脱离 DSH Desktop。
   - 缺点：超出本次已确认的 DSH 方案边界；若 dsh-cron 方案未完成验证就切换，会掩盖生命周期闸门结论。当前不隐式采用，需另行确认。

3. **方案 B（选定）：短任务 `ensure`/`watchdog` + wrapper 独立 child**
   - 优点：dsh-cron 只运行短任务，避免超时杀死录制器；wrapper 可集中实现单实例、PID 校验、优雅停止和健康检查。
   - 缺点：需要额外验证 Windows 子进程、孤儿进程、PID 重用和 DSH 重启语义；DSH Desktop 关闭期间不保证调度继续。

## Consequences

### Positive Consequences

- 官方 DataRecorder 与交易运行时隔离，录制路径可以独立测试和回滚。
- dsh-cron 的有限超时不会直接成为长期录制进程的生命周期上限。
- wrapper 可对状态、心跳、PID 和数据库路径实施 fail-closed 校验。
- DSH 插件保持薄层，不复制 cron、重试或进程管理逻辑。

### Negative Consequences

- “自愈”不是单个 dsh-cron 前台任务自动获得的能力，必须由短任务周期检查并经本机实测证明。
- DSH Desktop 关闭时不承诺持续录制；这会形成明确的数据覆盖窗口。
- wrapper 的错误处理和 Windows 进程语义增加了实现与测试成本。

### Neutral Consequences

- 录制数据始终不作为策略晋级或实盘准入证据；是否可用于研究仍需按数据质量和样本外纪律单独判定。
- `dsh-cron@0.3.43` 版本和上述生命周期事实必须在后续升级时重新核对。

## Implementation Guidance

- `record_market_data.py` 只连接行情、订阅具体合约并调用官方 DataRecorder；不得导入交易策略、下单模块、账户查询或风控状态。
- 独立 bootstrap 必须在导入 vn.py database/DataRecorder 之前固定中国时区、`.vntrader_record/` 临时目录和日志路径。
- wrapper 必须使用不可猜测的进程身份校验（PID + 命令行/profile/状态签名），并在停止前确认目标仍是本任务创建的进程；不能仅凭 PID 操作。
- `ensure`、`watchdog` 和增量任务必须是有界、可重入的短任务；不得配置所谓无限 timeout。
- 在本机 dummy worker 实测完成前，DSH 配置只作为实验/开发配置，不宣称常驻录制已验收。

## Related Information

- GitHub Issue [#13](https://github.com/laiyinyizao007/invest/issues/13)
- [`docs/roadmap.md`](../roadmap.md)
- [`docs/architecture.md`](../architecture.md)
- [`docs/runbook-market-recording.md`](../runbook-market-recording.md)（实现后新增）
- [`PROJECTWIKI.md`](../../PROJECTWIKI.md)
- [`CHANGELOG.md`](../../CHANGELOG.md)
- `@goodandready/dsh-cron@0.3.43` npm 包及其 `lib/runtimes/runner-external.js`、`lib/runtimes/runner.js`
