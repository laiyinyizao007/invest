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
- 测试扩充至 124 个,覆盖率 98%。

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
