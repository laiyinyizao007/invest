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

### Changed（变更）
- 分钟线时间戳由新浪的"K 线结束时间"前推一个周期,统一为 vn.py 的"开始时间"。

### Fixed（修复）
- 修正 `DB_TZ` 与系统时区不一致导致 vn.py 读回 K 线整体偏移 16 小时的问题(根因:`vnpy_sqlite` 读取时按系统本地时区解释无时区时间)。
- 数据源 OHLC 自相矛盾的行改为修复而非丢弃(PP 2020-08-06、L 2007-07-31 各 1 行)。
- CSV 改为先写临时文件再原子替换,避免写入中断损坏已积累的分钟历史。

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
