# SimNow 模拟盘运行手册(Phase 4 验收)

> 目的:在**没有真实资金**的 SimNow 环境里,验证交易通道、风控和告警在真实 CTP 行为下是否可靠。
> 当前策略在回测里**没有发现可延续的优势**(见 ADR),所以这里测的是**管道**,不是策略能不能赚钱。
> 代码已在离线假网关上做过端到端测试(`tests/`),但假网关不等于真实 CTP:下面标 ⚠ 的项目只能在 SimNow 上确认。

## 当前进度(2026-11-10)

| 项 | 状态 |
|---|---|
| 主机端 SimNow 启动管道(连接→授权→登录→结算确认→合约表→策略初始化→订阅→优雅停止) | ✅ 已跑通一次(第一套,真实 CTP) |
| 启动各阶段耗时可观测(里程碑日志) | ✅ 已实现 |
| Docker 镜像构建 + 容器内 `vnpy_ctp` 导入 + `run_headless.py --check` | ✅ 已验证 |
| **容器内连接 SimNow 并稳定运行** | ✅ 2026-11-10 完整跑通(连接→授权→登录→结算确认→合约表→策略初始化→订阅→healthy→优雅退出) |
| 第 5 节 10 项实测(废单文本/回报时序/断线/kill -9/熔断/夜盘/断流/撮合差异) | ⚠ 2026-11-12 夜盘演练补做了第 3、6、7、8、9 项（均通过）；第 4 项（断线）已改为外部真实切网并通过；其余待做 |
| 第 5 节实测项的**自动化演练脚本**(`scripts/drill.py`) | ✅ 已写好,待在交易时段/夜盘实跑 |
| 每日证据表 + webhook 连通性自检(`scripts/unattended_report.py`) | ✅ 已写好并跑通离线自检 |
| 连续 ≥ 5 个交易日无人值守 | ❌ 未开始(证据表目前只有 1 天) |
| Webhook 告警实际到达手机 | ❌ 未验证(`.env` 的 `WEBHOOK_URL` 为空) |
| 图形界面 `scripts/run_gui.py`(人工观察,非运行方式) | ✅ 可用,已自动订阅 |

### 5.0 自动化工具(本轮新增)

#### `scripts/drill.py` —— 把第 5 节的实测项做成可重复执行的场景
第 5 节的每一项原本都要人在旁边一步步做,容易漏步骤、也没留下可核对的记录。现在每项对应一个场景,
脚本自己连 SimNow、自己做动作、自己记录结论,结果写进 `docs/simnow-drills.md`。

```bash
# ⚠ 必须在容器里跑:运行时的"是否处于交易时段"判断用 datetime.now()(系统本地时区),
#   宿主机是 UTC-8、容器是 TZ=Asia/Shanghai,时段判断错位会让看门狗不触发、结论失真。
docker compose run --rm --entrypoint python trader scripts/drill.py list
docker compose run --rm --entrypoint python trader scripts/drill.py all --confirm-simnow
docker compose run --rm --entrypoint python trader scripts/drill.py latency --confirm-simnow --symbol c2701.DCE
```

| 场景 | 对应第 5 节 | 前提 |
|---|---|---|
| `overnight` | 第 8 项 夜盘首根 Tick 与交易日归属 | 需要在夜盘时段(20:30 之后) |
| `latency` | 第 3 项 成交→持仓回报延迟 | 需要在交易时段内;会真的开 1 手仓,量完立刻平掉 |
| `tick-stale` | 第 9 项 行情断流 → 看门狗告警 | 需要在交易时段内(看门狗只在时段检查) |
| `disconnect` | 第 4 项 交易连接断开 → 告警与自动重连;另记录重连后连接状态、窗口内发单失败计数与「信号无积压补发」证据 | 需要在交易时段内 |
| `daily-loss` | 第 6 项 当日熔断 → 撤单 → 平仓 | 无时间前提 |
| `hard-lock` | 第 7 项 累计死线 → 平仓 → 退出码 0 | 无时间前提 |

- **前提不满足的场景标记为「跳过(⏭)」而不是失败** —— 例如收盘后跑 `tick-stale` 等不到告警是前提问题,不是系统有问题。
- **状态与实盘完全隔离**:演练用独立的 `.vntrader_drill/` 目录与独立的风控状态文件,熔断/硬锁定不会污染实盘。
- **只在模拟盘上跑**:交易服务器地址必须在 SimNow 官方白名单内(`scripts/simnow_guard.py`),否则拒绝连接。
  白名单只收官网公布过的:第一套 `182.254.243.31:30001/30002/30003`,第二套 `:40001`(官网只公布了这一套)。
- **⚠ 二次确认**:如果地址不在白名单(用了 `--allow-any-address` 逃生舱),而场景又会**真的发委托**
  (`latency`/`daily-loss`/`hard-lock`),必须再加 `--confirm-any-address-trades`,否则脚本拒绝执行(退出码 2)。
  用了逃生舱的警告会写进 `docs/simnow-drills.md` 当审计记录。
- 演练用**空策略**,不会自己下单,避免和演练动作互相干扰。
- `latency` 场景开的那 1 手仓**平不掉就是失败**:会发 FATAL 告警并以非零码退出 —— "演练通过"却把仓位留在
  模拟账户里,后面的对账与无人值守证据全部失真。跑完请核对模拟账户确实没有持仓。

#### 演练实测记录（SimNow 第一套前置，2026-11-12 夜盘）

| 场景 | 结果 |
|---|---|
| `overnight` | ✅ 21:00 之后的首根 Tick 归属下一交易日 |
| `latency` | ✅ 成交→持仓回报 0.177 秒，1 手仓已平。**首次运行失败**：`drill.py` 读了不存在的 `TickData.ask_price`（应为 `ask_price_1`），单测的假 Tick 犯了同样的错，所以全绿却从未发现；已修并改用真实 `TickData` |
| `latency`（最后一次完整 `all`） | ❌ 30 秒没成交（夜盘后段行情清淡，按卖一价挂单）；已撤单、账户无仓位。该场景对行情活跃度敏感，不能视为稳定通过 |
| `tick-stale` | ✅ 行情断流 60 秒后看门狗告警 |
| `disconnect` | ✅ 切网 240 秒：行情停更 61 秒告警；CTP 约 121 秒报断线（8193）；恢复后约 2 秒自动重连。首个版本因释放 API 段错误，改为外部真实切网后通过，详见下 |
| `daily-loss` | ✅ 注入权益下降 700 元（阈值 600）→ `DAILY_LOCKED`、禁止新开仓告警；场景开平 1 手无残留 |
| `hard-lock` | ✅ 注入回撤 2100 元（死线 2000）→ FATAL、紧急平仓、账户空仓、退出码 0、**重启后拒绝交易** |

完整合并表见 `docs/simnow-drills.md`。

**`disconnect` 的根因（用 `--trace` 埋点一次运行定位，退出码 139 / SIGSEGV）**：场景用 `td_api.close()` 模拟断线，而 vnpy_ctp 的 `close()` 是原生 `exit()`——**释放整个 API 对象**，不是「断开连接」。`close()` 本身 17 毫秒内正常返回；约 1.5 秒后 vn.py 事件引擎的定时器线程调用 `gateway.process_timer_event → query_position → reqQryInvestorPosition`，打到已释放的原生对象上，进程段错误。此前整套 `all` 运行里「进程在 disconnect 标题后悄悄消失」就是它（当时管道吞掉了退出码）。

含义：
1. 这个场景**永远无法回答**它想问的问题（断线是否告警、CTP 客户端是否自动重连）——API 已被释放，不会再有回调，更不会重连。
2. 生产路径不受影响：`runtime.shutdown()` 是先停事件引擎再关网关，没有「释放后继续跑定时器」的情形。
3. 要真正验证断线与重连，需要**真的切断网络**而不是释放 API。可行做法：演练在容器里跑，由宿主机对该容器执行 `docker network disconnect` → 等待 → `docker network connect`，原生 API 会看到真实的 TCP 断开并自行重连。**该改造已完成并通过**（2026-11-12 夜盘）：`disconnect` 现在要求 `--external-cut FILE`（演练就绪后写出请求文件），由宿主机对容器执行 `docker network disconnect` / `connect`；没给该参数时场景自动跳过，所以 `all` 不会再崩溃。结果：切断 240 秒——行情停更看门狗 61 秒告警，CTP 心跳超时约 121 秒报断线（原因 8193），网络恢复后约 2 秒两个通道自动重连。**切断只有 30 秒时 CTP 完全无感知**（TCP 重传补回了数据，恢复后涌入约 108 笔积压 Tick），所以真实检测延迟取决于中断时长，短中断只有行情停更看门狗能发现。

另一次失败（`disconnect` 单独重跑，登录后 300 秒收不到合约表，退出码 3）是**重复登录限流**：上一次登出后立即重新登录会卡住，间隔 ≥5 分钟正常；与行情录制里发现的同一现象。

**排查工具**：`drill.py --trace FILE`（默认关闭）逐行 fsync 记录退出钩子、致命信号转储、释放后 API 调用探针、周期快照与登录间隔；`--trace-skip-close` 为控制运行。

#### `scripts/status_server.py` —— 只读状态页(Web 监控面板)后端
```bash
pip install -r requirements-dashboard.txt     # Web 依赖,与交易环境隔离;交易机不需要装
python scripts/status_server.py --check       # 只校验配置
python scripts/status_server.py               # 启动,默认 127.0.0.1:8770
```
- **只读、仅回环**:全部接口是 GET;监听 `0.0.0.0` 或局域网地址会被配置层拒绝启动(经局域网访问属于阶段⑤,须另写 ADR)。
- **令牌**:首次启动在 `data/dashboard/status.token`(面板专属目录,不在交易侧目录树里)自动生成(不入库,内容不会被打印);浏览器里粘贴它。令牌放 `Authorization: Bearer` 头,不要放 URL。
- **环境标签**:在 `config/status_dashboard.json` 里设 `environment`(`simnow` / `guoxin-test` / `live`)。不设则页面横幅显示『环境未知』——面板不读 `.env`,不会猜。
- **状态目录**:默认 `data/state`(直接用 Python 运行的机器);Docker 运行则在配置里把 `state_dir` 指向挂载出来的 `runtime/data/state`,`log_dirs` 默认 `runtime/vntrader/log`。**不要把 `.vntrader/log` 加进去**——单元测试会污染它。
- **面板必须与交易运行时在同一台机器**(它读同一批文件)。杀掉面板不影响交易;它不写任何被读取的文件(测试强制)。
- **进化视图(阶段④)默认关闭**:在 `config/status_dashboard.json` 里设 `"evolution": {"enabled": true}` 才会注册那 6 个接口;关闭时总览里也没有进化区块(一个用户打不开的子系统不该出现在总览里)。数据由引擎侧写进 `data/research/evolution/` 与 `data/research/heatmaps/`,面板只读。
- 前端未构建时 `/` 显示占位页;API 可直接用(带令牌)。前端构建与入库见 `docs/web/05-lovable-guide.md`。

#### `scripts/unattended_report.py` —— 无人值守验收的证据
```bash
python scripts/unattended_report.py --report          # 生成每日证据表 → docs/unattended-log.md
python scripts/unattended_report.py --probe-webhook    # webhook 连通性自检
```

- `--report`:解析 vn.py 每日日志与心跳文件,列出每天的启动次数、成交笔数、异常事件、心跳新鲜度。
  **只统计日志里真实出现的内容,不推断"没问题"**。
  默认只统计容器目录 `runtime/vntrader/log` —— 宿主机 `.vntrader/log` 会被单元测试污染,要加 `--include-host` 才统计。
- `--probe-webhook`:配了 `WEBHOOK_URL` → 用真实通知器发一条"【连通性自检】"消息并报告对端是否接受(**会真的发消息**);
  没配 → 起本地接收端离线验证"送达 + 业务失败可辨识",并明确指出手机端尚未验证。
- **注意**:飞书机器人**失败时也返回 HTTP 200**,错误信息在 JSON 体的 `code` 字段里。只看状态码会把"没送达"当成成功,
  所以通知器现在会解析响应体,业务码非 0 一律算失败并重试。

## 0. 你需要准备的(我无法代办)
1. 注册 SimNow 账号(上期技术官网 simnow.com.cn),拿到投资者代码与密码。
2. 前置机地址以官网 `https://www.simnow.com.cn/product.action` 当前公布的为准。**网上教程里的地址很多已经过期**
   (实测:教程里的 `180.168.146.187:10201` 等整个 IP 已无任何响应,导致我们花了很长时间排查代理)。
   2026-11 官网公布(看穿式前置,使用监控中心生产秘钥):BrokerID `9999`;
   第一套(与实盘交易时间一致,行情为真实行情):交易 `182.254.243.31:30001` / 行情 `182.254.243.31:30011`(第二组 `30002/30012`,第三组 `30003/30013`);
   第二套 7x24(仅供 API 测试,新注册账号要等到第三个交易日才能用):交易 `182.254.243.31:40001` / 行情 `182.254.243.31:40011`;
   AppID `simnow_client_test`,认证码 16 个 `0`。**每次使用前请对照官网页面,地址可能再次调整。**
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

**实测(2026-11-10,第一套环境,真实 CTP)**:连接成功 → 授权验证成功 → 登录成功 → 行情登录成功 → 结算信息确认成功 →
策略用数据库里的 `CADJ` 复权日线初始化 → 订阅 `c2701` → 已启动(SimNow 初始权益 2000 万)→ 优雅停止,退出码 0。
**SimNow 的"结算信息确认"耗时波动很大**(同一天三次启动分别约 4 秒、28 秒、超过 60 秒),且对**短时间内反复登录**似乎会变慢:
不要连续反复启动测试;`connect_timeout` 默认 300 秒。
**慢的原因**:vn.py 的 CTP 网关在结算确认后会查询**全市场合约表**(含期权,不可过滤),账户/持仓查询与行情订阅都要等它完成;SimNow 返回这张表耗时 20 秒~3 分钟以上。重启恢复因此最多要几分钟,期间若有持仓,本地止损尚未生效;实盘前须在期货公司前置上实测此耗时。

启动流程(任何一步失败都不会进入交易):硬锁定检查 → 连接 → 等合约与账户 → 查询持仓 → 初始化策略并强制停止 →
**对账**(账户有其他合约持仓 / 策略持仓与账户不一致 / 策略状态矛盾 → 拒绝启动并告警)→ 启动策略。

退出码:`0` 正常停止或硬锁定;`3` 启动失败(可重试);`4` 对账失败(需人工处理);`2` 配置错误。

## 5. ⚠ 必须在 SimNow 上实测并回填的事项
这些是离线测试覆盖不了的真实行为,**做完后把观察结果补进代码与 ADR**:

| # | 实测内容 | 为什么 | 回填位置 |
|---|---|---|---|
| 1 | **采集真实的 CTP 废单错误文本**:故意触发 ① 非交易时段下单 ② 价格超出涨跌停 ③ 不存在的合约 ④ 超大手数 ⑤ 无持仓时平仓 ⑥ 资金不足 | 废单分类靠关键词,未知按硬拒绝;不采集会把可恢复的错误也当成硬故障 | `risk/order_policy.py` 的 `HARD_KEYWORDS/SOFT_KEYWORDS` + 测试 |
| 1 ⚠ | **已采集 2/6**(2026-11-10 第一套 15:53):"不存在的合约"(走 vnpy_ctp 自己的 KeyError,不是 CTP 文本)、"非交易时段开仓"(`交易委托失败,代码：21,信息：CTP:该交易席位未连接到交易所`,硬拒绝)。其余 4 类待重跑 —— 第一套环境 16:00 左右关闭,要么 16:00 前跑,要么 21:00 后跑夜盘。脚本会打印目标地址,**确认是 SimNow 地址**再让它发单 | 已知缺陷已修:CTP 把拒单写成 INFO 级(`write_error` 实为 `write_log`)、"该交易席位未连接到交易所"含软关键词"连接"被误判、假网关级别失真、采集脚本竞态/挂死/时段内可能成交/价格未对齐 tick、场景间错误串味、平仓场景可能误平用户持仓、多网关错误张冠李戴 | `docs/reject-texts-simnow.md` |
| 2 | 废单回报与错误日志的实际先后和间隔 | 当前假设错误日志在回报之后 3 秒内 | `trading/guard_rule.py` 的 `ERROR_WINDOW`、`reject_classify_delay` |
| 2 ✅ | **已实测(2026-11-10)**:CTP 的 `onRspOrderInsert` 是**先** `gateway.on_order()`(订单回报)**后** `write_error()`(错误日志),事件引擎按 put 顺序派发 → **错误日志一定在订单回报之后**;实测间隔:发单 15:54:12.973 → 错误日志 15:54:13.000,约 **27 毫秒**。`ERROR_WINDOW=3s` / `reject_classify_delay=1.5s` 都绰绰有余 | — | 已回填 `trading/guard_rule.py`(并据此修正采集脚本的竞态) |
| 3 | 成交后持仓回报多久刷新 | 紧急平仓按成交量对账,等待 `position_refresh_timeout`(默认 6 秒) | `config/risk_manager_setting.json` |
| 4 | **断网 30 秒再恢复**:是否收到"连接断开/登录成功"告警、行情是否恢复、持仓是否对得上 | 验证 CTP 自动重连与我们的告警 | — |
| 5 | **持仓中 `kill -9` 进程再启动**:对账是否通过、本地止损是否在第一个 Tick 重新挂出 | 验证状态持久化与重启恢复 | — |
| 6 | 把 `daily_loss_limit` 临时调小(如 20 元),手动制造亏损 | 验证当日熔断 → 撤单 → 平仓 → 次日解除的完整链路 | — |
| 7 | 把 `max_drawdown` 临时调小,触发硬锁定 | 验证:平仓 → 确认空仓 → 退出码 0 → 重启后拒绝交易 → `unlock_account.py` | — |
| 8 | 夜盘跨日:21:00 开盘第一根 Tick 到来时的信号发单、交易日归属 | 验证日线合成与 `trading_day` | — |
| 9 | 行情断流:交易时段内停掉行情,是否收到"行情停更"告警 | 本地止损依赖 Tick | — |
| 10 | SimNow 的撮合与真实交易所的差异(排队、滑点、成交回报) | **SimNow 不能代表真实滑点**,回测成本假设需用实盘小额再校准 | `strategies/instruments.py` |
| 11 | **非策略委托检测的 reference 假设**:在 SimNow 上用手机 App/另一终端手动下一单,**两种结果都要记录**:① 收到 reference 为空的回报并触发「检测到非策略委托」告警 → 假设成立;② 本进程完全收不到该委托的事件 → 检测只覆盖同会话干预,需按 ADR 登记周期性 `ReqQryOrder` 对账;升级 vnpy_ctp 后同样复验 | 检测依赖 vnpy_ctp 只在 send_order 事件里带 reference 的行为(源码推断,未实测) | `docs/adr/20261111-discipline-enforcement.md` 已知局限 |

## 6. SimNow 阶段验收
- 连续无人工干预运行 ≥ 5 个交易日,含夜盘与至少一次周末;
  **"交易日"的算法**(`scripts/unattended_report.py::longest_clean_streak()`):只有**工作日(周一~周五)**计入连续段,
  **周末即使有日志文件也不计入**(容器常驻,周末照样有日志,夜盘跨零点还会写进周六的文件 ——
  把它们算成交易日会让验收结论凭空多出两天)。周末的**严重**事件(紧急平仓、熔断、硬锁定、对账失败)仍然**打断**连续段;
  周末的警告(空闲期的连接抖动)不打断。周五到周一是连续的**两个**交易日,中间的周末不打断也不占名额。
  工作日**缺日志**(进程静默死亡)打断连续段,并列进验收清单待人工核对是否节假日。
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

## 8. 排障:错误码 4097("网络读失败")反复出现,且从未出现"服务器连接成功"
**含义**:CTP 连不上前置机,还没走到登录。与账号、密码无关。按下面的顺序排查,**不要先怀疑代理**:

1. **地址是否过期(这次的真实原因)**:去官网 `product.action` 页核对前置机地址。教程里的旧地址整个 IP 都不再响应。
   判断方法:`socket.create_connection((ip, 80), timeout=5)` 这类探测连 80 端口也超时,说明该 IP 本身已不可达。
2. **本机代理(TUN 模式)是否拦截**:默认路由经过 `198.18.0.2`(虚拟网卡)时,所有 TCP 连接都会被代理在本机"瞬间接受",
   所以**端口探测成功不代表真的连上了对端**(探测"连接耗时 2 ms"就是本机应答)。真实情况看代理内核日志:
   Clash Verge 的 `%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\logs\service\service_latest.log`,
   里面有 `[TCP] dial DIRECT (match IPCIDR/…) … error: i/o timeout` 这样的记录,能看到规则命中了谁、拨号结果如何。
   如需让前置机直连,在代理规则最前面加 `IP-CIDR,<前置机IP>/32,DIRECT,no-resolve`。
3. 在非开放时段连接:第一套环境与实盘交易时间一致;第二套 7x24 的服务时间是交易日 16:00~次日 09:00、非交易日 16:00~次日 12:00。
4. 账号问题:新注册账号需先改一次密码;一个手机号一个账号。
   **第二套(7x24)对新注册账号不可用**:2026-11-10 实测,连 `182.254.243.31:40001` 时
   行情登录成功、交易登录回 `代码：3,信息：CTP:不合法的登录`,新账号要到第三个交易日才能用第二套。
   在此之前只能用第一套,受实盘交易时间限制(约 16:00 关闭、21:00 开盘)。

**生产环境(VPS)不要在运行交易的机器上开 TUN 代理。**
同类问题:本机走代理下载 PyPI / Docker 镜像这类大文件只有约 20 KB/s,直连约 4.7 MB/s。

## 9. 换账户前必须知道
风控状态文件(`data/state/account_guard.json`)记录了账户的权益最高点。SimNow 默认 2000 万,若不处理,换成 1 万元账户会被判为回撤 1999 万而硬锁定。
现在状态与账户 ID 绑定:连接到不同账户时会自动重置权益基准并告警"账户变更"(**硬锁定不会因换账户而解除**,仍需 `unlock_account.py`)。

## 10. Docker 镜像(2026-11-10 实测)
`docker build` 在 x86_64 上成功:`vnpy_ctp` 在 Linux 下从源码编译并可导入,容器内 `run_headless.py --check` 通过。
Docker 拉取镜像曾只有约 10 KB/s,后来恢复到正常(`python:3.12-slim` 约 30 秒),原因未查明,可能与代理/网络波动有关;宿主机直连与走代理下载都约 4 MB/s。

### 10.1 必须安装 `zh_CN.GB18030` locale(踩过的坑)
容器第一次连 SimNow 时进程在 CTP 回调线程里直接 abort,日志只有一行:
```
terminate called after throwing an instance of 'std::runtime_error'
  what():  locale::facet::_S_create_c_locale name not valid
```
原因:`vnpy_ctp` 的 `toUtf()` 里硬编码了 `std::locale("zh_CN.GB18030")`。Debian slim 镜像默认只带 `C` / `C.utf8` / `POSIX`,
构造这个 locale 抛异常 → CTP 回调线程 `std::terminate` → 整个进程挂掉,**没有任何 CTP 错误日志**。
定位办法:`objdump -s` 看 `vnctpmd.so` 的 .rodata,能看到字面量 `zh_CN.GB18030`。
修法(Dockerfile 里已加,别删):
```dockerfile
apt-get install -y --no-install-recommends locales
localedef -i zh_CN -f GB18030 zh_CN.GB18030
```
只装 `locales` 不生成 `zh_CN.GB18030` 无效;只生成 `zh_CN.UTF-8` 也无效(名字必须完全一致)。
`LANG=C LC_ALL=C` 不能绕过——失败路径是 `std::locale(name)`,不是环境变量。

### 10.2 当前状态
| 项 | 状态 |
|---|---|
| 镜像构建 + `vnpy_ctp` 导入 + `--check` | ✅ |
| **容器内完整连上 SimNow** | ✅ 2026-11-10 实测通过(见下) |
| ≥5 个交易日无人值守 | ❌ 未开始 |

2026-11-10 用 `docker compose up -d` 实测的完整启动序列(容器内,locale 由 Dockerfile 自动生成):
```
15:36:11  连接登录 -> CTP
15:36:12  行情服务器连接成功 / 行情服务器登录成功
15:36:12  交易服务器连接成功 / 交易服务器授权验证成功 / 交易服务器登录成功
15:36:14  结算信息确认成功
15:36:15  启动耗时(自发起连接): 行情登录=0.5s, 交易登录=1.2s, 结算确认=2.5s, 合约与账户到位=3.9s
15:36:19  合约信息查询成功
15:36:20  策略初始化完成 → 订阅 c2701.DCE → 已启动(权益 20000000)
```
容器状态 `healthy`(心跳文件 `runtime/data/state/heartbeat` 每 10 秒刷新),
风控状态写进 `runtime/data/state/account_guard.json`,`docker compose stop` 优雅退出、退出码 0。
**这次比宿主机那次快得多**(结算确认 2.5 秒,宿主机上曾耗时 4 秒~60 秒以上),因为这次没有反复登录。

下一步做法:
```bash
docker compose up -d --build
docker compose logs -f trader     # 观察是否出现"合约信息查询成功"
```
容器内没有 SimNow 的网络问题(前置机在公网),若连接失败,先按第 8 节排查宿主机到前置机的连通性,
再确认 `.env` 已挂载进容器(`docker-compose.yml` 的 `env_file`)。**不要**在宿主机和无头版同时连同一个模拟盘账号。

## 11. 图形界面(人工观察与试验)
`python scripts/run_gui.py` 打开 VeighNa Trader 界面并按 `.env` 自动连接(`--no-connect` 则手动连接)。
- 手动下单/撤单同样受账户风控约束(日亏 600 元 / 累计回撤 2000 元硬锁定,平仓放行)。
- **没有**启动对账、行情停更/持仓漂移/策略崩溃看门狗和 Webhook 告警,这些只在无头版里。**不要用它做无人值守运行。**
- 不要与无头版同时运行(同一账号、同一份策略状态文件)。
- 界面里点"微信"二维码对话框需要 Pillow(`pip install pillow`),缺它会让整个界面崩溃退出。
- 登录后同样要等全市场合约表下载完成,界面里"日志"页会显示"合约信息查询成功"。
