# 变更日志（Changelog）
所有重要变更均记录于此文件。

本文件格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，并遵循 [语义化版本号](https://semver.org/lang/zh-CN/) 规范。
与 [`PROJECTWIKI.md`](PROJECTWIKI.md) 互相链接;相关 Issue:[#1](https://github.com/laiyinyizao007/invest/issues/1)。

## [Unreleased]
### Added（新增）
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
- 分钟线时间戳由新浪的"K 线结束时间"前推一个周期,统一为 vn.py 的"开始时间"。

### Fixed（修复）
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

### Security（安全）
- 账号、密码、AuthCode 仅放 `.env`(已 `.gitignore`),仓库只提供 `config/.env.example` 模板。

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
