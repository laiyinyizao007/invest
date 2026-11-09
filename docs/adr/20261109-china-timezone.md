# ADR: 进程统一使用中国时区(TZ=CST-8 + database.timezone=Asia/Shanghai)

- 状态:已采纳
- 日期:2026-11-09
- 跟踪:Issue [#1](https://github.com/laiyinyizao007/invest/issues/1)

## 背景
开发机系统时区为 `America/Los_Angeles`。`vnpy_sqlite` 写入时把时间转成 `DB_TZ` 的无时区墙上时间,
读取时却用 `naive_dt.timestamp()` 还原,而该调用按**系统本地时区**解释。
只有 `DB_TZ == 系统时区` 才能往返一致。实测 `DB_TZ=Asia/Shanghai`、系统为洛杉矶时,读回的 K 线整体偏移 +16 小时。

## 备选方案
1. 不覆盖,使用系统时区作为 `DB_TZ`(时间点正确,但 K 线带洛杉矶时区,日线日期与夜盘日期会错位,依赖盘中时间的逻辑易出错)
2. 在每个读数据的地方手工 `astimezone` 转换(分散、易漏,且回测引擎内部的日期分组仍会错)
3. **进程内统一设为中国时区**:`os.environ["TZ"] = "CST-8"` 并设置 `SETTINGS["database.timezone"] = "Asia/Shanghai"`

## 决策
采用方案 3,集中在 `scripts/bootstrap.py`,所有脚本先 `import bootstrap`。中国无夏令时,`CST-8`(POSIX 写法,UTC+8)在 Windows CRT 与 Linux 上均有效(均已实测)。

## 影响
- 必须先于 `vnpy.trader.database` 导入;`bootstrap.py` 在顺序错误时抛 `RuntimeError`。
- Docker/VPS 部署时设置 `TZ=Asia/Shanghai`,与本机行为一致。

## 验证
入库后读回:PP 11-09 的 1 分钟线 225 根,首根 09:00、末根 14:59;`datetime(2026,11,9,21,12).timestamp()` 与真实上海时间戳一致。

## 回滚
删除 `bootstrap.py` 中两行设置即可回到 vn.py 默认行为(同时需重新核对已入库数据的时间语义)。
