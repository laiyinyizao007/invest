# SimNow 模拟盘运行手册(Phase 4 验收)

> 目的:在**没有真实资金**的 SimNow 环境里,验证交易通道、风控和告警在真实 CTP 行为下是否可靠。
> 当前策略在回测里**没有发现可延续的优势**(见 ADR),所以这里测的是**管道**,不是策略能不能赚钱。
> 代码已在离线假网关上做过端到端测试(`tests/`),但假网关不等于真实 CTP:下面标 ⚠ 的项目只能在 SimNow 上确认。

## 0. 你需要准备的(我无法代办)
1. 注册 SimNow 账号(上期技术官网 simnow.com.cn),拿到投资者代码与密码。
2. 在官网"服务器信息"页查到**当前**的 BrokerID、交易前置机与行情前置机地址(7x24 与交易时段两套)。
   SimNow 的 AppID / 授权编码也以官网公布为准(常见为 `simnow_client_test` / `0000000000000000`,**请以官网为准**)。
3. 一个 x86_64 的运行环境:Windows 本机、或 Linux VPS(Docker)。**树莓派(ARM64)不行**:PyPI 上 `vnpy_ctp` 没有 aarch64 包。
4. (可选)一个接收告警的 webhook:飞书机器人或 Telegram Bot。

## 1. 配置
```bash
cp config/.env.example .env      # .env 不入库(.gitignore 已忽略)
```
编辑 `.env`:
- `CTP_USERNAME / CTP_PASSWORD / CTP_BROKER_ID / CTP_TD_ADDRESS / CTP_MD_ADDRESS / CTP_APP_ID / CTP_AUTH_CODE`
- `CTP_ENVIRONMENT`:SimNow 与实盘用 `实盘`(生产版 API);`测试` 仅用于期货公司的看穿式监管测试环境。⚠ 请在 SimNow 上确认哪个能连通。
- `LIVE_VT_SYMBOL`:要交易的**具体合约**,如玉米主力 `c2701.DCE`(主力合约会换月,需要定期更新)。
- `HISTORY_VT_SYMBOL`:预热用的历史日线代码,填 `CADJ.DCE`(复权连续合约)。
- `WEBHOOK_URL / WEBHOOK_KIND`(`generic` / `feishu` / `telegram`)。

## 2. 准备历史数据(策略预热需要)
```bash
python scripts/build_continuous.py --symbol C --save   # 写入 CADJ.DCE 复权日线到 .vntrader 数据库
```
策略实盘模式从本地数据库读取历史日线预热;**每个交易日收盘后重新运行一次**,让数据库保持最新(否则重启时历史会缺最近几天)。

## 3. 配置自检(不连接、不交易)
```bash
python scripts/run_headless.py --check
```
会校验环境变量、依赖、风控配置;缺变量时只报变量**名**,不报值。

## 4. 启动
- 本机:`python scripts/run_headless.py`
- Docker(Linux x86_64 VPS):`docker compose up -d --build`,`docker compose logs -f trader`

启动流程(任何一步失败都不会进入交易):硬锁定检查 → 连接 → 等合约与账户 → 查询持仓 → 初始化策略并强制停止 →
**对账**(账户有其他合约持仓 / 策略持仓与账户不一致 / 策略状态矛盾 → 拒绝启动并告警)→ 启动策略。

退出码:`0` 正常停止或硬锁定;`3` 启动失败(可重试);`4` 对账失败(需人工处理);`2` 配置错误。

## 5. ⚠ 必须在 SimNow 上实测并回填的事项
这些是离线测试覆盖不了的真实行为,**做完后把观察结果补进代码与 ADR**:

| # | 实测内容 | 为什么 | 回填位置 |
|---|---|---|---|
| 1 | **采集真实的 CTP 废单错误文本**:故意触发 ① 非交易时段下单 ② 价格超出涨跌停 ③ 不存在的合约 ④ 超大手数 ⑤ 无持仓时平仓 ⑥ 资金不足 | 废单分类靠关键词,未知按硬拒绝;不采集会把可恢复的错误也当成硬故障 | `risk/order_policy.py` 的 `HARD_KEYWORDS/SOFT_KEYWORDS` + 测试 |
| 2 | 废单回报与错误日志的实际先后和间隔 | 当前假设错误日志在回报之后 3 秒内 | `trading/guard_rule.py` 的 `ERROR_WINDOW`、`reject_classify_delay` |
| 3 | 成交后持仓回报多久刷新 | 紧急平仓按成交量对账,等待 `position_refresh_timeout`(默认 6 秒) | `config/risk_manager_setting.json` |
| 4 | **断网 30 秒再恢复**:是否收到"连接断开/登录成功"告警、行情是否恢复、持仓是否对得上 | 验证 CTP 自动重连与我们的告警 | — |
| 5 | **持仓中 `kill -9` 进程再启动**:对账是否通过、本地止损是否在第一个 Tick 重新挂出 | 验证状态持久化与重启恢复 | — |
| 6 | 把 `daily_loss_limit` 临时调小(如 20 元),手动制造亏损 | 验证当日熔断 → 撤单 → 平仓 → 次日解除的完整链路 | — |
| 7 | 把 `max_drawdown` 临时调小,触发硬锁定 | 验证:平仓 → 确认空仓 → 退出码 0 → 重启后拒绝交易 → `unlock_account.py` | — |
| 8 | 夜盘跨日:21:00 开盘第一根 Tick 到来时的信号发单、交易日归属 | 验证日线合成与 `trading_day` | — |
| 9 | 行情断流:交易时段内停掉行情,是否收到"行情停更"告警 | 本地止损依赖 Tick | — |
| 10 | SimNow 的撮合与真实交易所的差异(排队、滑点、成交回报) | **SimNow 不能代表真实滑点**,回测成本假设需用实盘小额再校准 | `strategies/instruments.py` |

## 6. SimNow 阶段验收
- 连续无人工干预运行 ≥ 5 个交易日,含夜盘与至少一次周末;
- 上表 4–9 项均实测通过并留有记录;
- 告警(webhook)全部到达手机;
- 没有出现:重复下单、止损未触发、持仓与策略不一致、进程静默死亡。

## 7. 故障处理
| 现象 | 处理 |
|---|---|
| 启动即退出,告警"硬锁定生效中" | 核对账户权益与持仓,确认原因后 `python scripts/unlock_account.py` |
| 退出码 4 / "对账失败" | 核对账户持仓与 `.vntrader/cta_strategy_data.json`;必要时人工平仓后删除该策略的保存状态再启动 |
| "紧急平仓失败" | **立即人工平仓**;进程会保持存活并持续告警 |
| "退出时仍有持仓" | 本地止损已随进程失效,立即人工处理 |
| 容器反复重启 | 看 `docker compose logs`;`on-failure:3` 最多重试 3 次 |
