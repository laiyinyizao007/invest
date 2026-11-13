# 模块文档

> 本文件由 `PROJECTWIKI.md` §5 整体迁出(2026-11-11,Issue #7),**原文未删改**,包含各模块的详细职责、约束与修复经过。
> `PROJECTWIKI.md` §5 只保留每个模块一行的索引。命令与参数以脚本的 argparse 定义为准。

### `README.md`
- **职责**:面向使用者的上手入口 —— "这个项目在干嘛、怎么用"。包含项目定位与状态提示(策略结论为负面、只用于管道测试、当前不连实盘)、
  Mermaid 架构速览、目录结构说明、环境准备、按场景分组的命令(数据/复权/优化/无头运行/容器/图形界面/演练/废单采集/无人值守报告)、
  配置项表格(`.env` 与 `config/risk_manager_setting.json`)、测试命令、已知限制表、相关文档索引。
- **与本文件的关系**:README 面向"怎么用",本文件面向"怎么实现的";命令与参数以脚本的 argparse 定义为准,两者必须一致。
- **纪律**:README 里不写任何真实账号/密码/token,`.env` 的值一律写占位符。

### `scripts/bootstrap.py`
- **职责**:所有脚本的统一启动入口,须在导入 `vnpy.trader.database` 之前 `import bootstrap`(否则抛 `RuntimeError`)。
- **做的事**:`chdir` 到项目根并确保 `.vntrader/` 存在(vn.py 据此把数据库放在项目内);设置 `TZ=CST-8` 与 `database.timezone=Asia/Shanghai`;`NO_PROXY` 默认 `*`(新浪为国内源,绕开系统代理)。
- **风险**:导入顺序是隐式约定,由运行时检查兜底。

### `scripts/fetch_data.py`
- **职责**:AKShare(新浪源)→ 清洗 → vn.py 数据库(日线/1 分钟)或 CSV(5/15 分钟)。
- **入口**:`python scripts/fetch_data.py [--symbols PP L EB] [--periods d 1 5 15]`,在项目根目录运行。
- **关键函数**:`fetch_with_retry`、`normalize`、`to_bars`、`save_csv`、`run_one`、`main`。
- **幂等**:数据库 upsert;CSV 按时间戳去重,新数据覆盖旧数据;重复运行安全。
- **失败隔离**:单个品种/周期失败不影响其余,最终退出码为 1。
- **测试**:`tests/test_fetch_data.py`,32 个用例。全仓库测试数与覆盖率以 `pytest -q --cov=.` 的实时结果为准(不在此写死,避免过期;门禁 ≥85%),在英文/中文 vn.py 语言环境下均通过。

### `scripts/import_minute.py`(购买的分钟线 → 复权主力连续)
- **职责**:读取仓库之外的分合约 1 分钟线(`<数据根>/<交易所>/<品种>/月份连续/*/*.csv`),按交易日归属汇总成日线,用新浪日线补齐数据结束日之后的缺口,复用 `build_continuous` 的换月规则与价差,对分钟线做差额复权,合成 N 分钟线(默认 15,时间戳为开始时间)。
- **入口**:`python scripts/import_minute.py --root C:/Dev/data/invest --exchange DCE_大连商品交易所 --folder C_玉米 --product C`,输出到 `<数据根>/derived/`(`C_15m_adj.csv`、`C_1d_adj.csv`、`C_rolls.csv`,**不入库**——购买数据与派生产物都不进仓库)。
- **交易日归属**:日盘归当天;夜盘(≥20 时)归下一个有日盘的交易日;日历之外的夜盘所属日未知,返回 NaT 并丢弃,绝不硬塞进最后一天。
- **起点裁剪**:购买数据从合约上市日起算,起点附近只有远月合约,挑出的"主力"成交冷清。`trim_illiquid_start` 从第一段"连续 5 个交易日成交量不低于全序列中位数 50%"的日子开始。
- **缺口**:分钟线只到购买数据的最后一天;其后的交易日只有日线(新浪),不伪造分钟线。
- **验证**(玉米实测):分钟线合成日线与新浪日线对比,收盘/最高/最低 99% 以上一致,成交量总和比值 1.000;输出的分钟线逐日收盘/最高/最低与日线序列完全一致。
- **测试**:`tests/test_import_minute.py`,21 个用例(合成数据)。

### `risk/risk_guard.py`(纯 Python,覆盖率 100%)
- `stop_distance` / `risk_per_trade` / `within_budget`:单笔风险预算,超预算则放弃交易(不缩小止损)。
- `TrailingStop`:只收紧不放松的移动止损(多头 max、空头 min),ATR 变大也不放松。
- `DailyLossGuard`:当日连亏熔断,换日重置。

### `risk/promotion_gate.py`(纯 Python,覆盖率 100%)
- `sharpe_standard_error` / `sharpe_ci`:夏普的统计误差(Lo 2002 近似)。
- `check_gate(CandidateMetrics, GateThresholds)`:9 条晋级门槛,返回未通过项(前 8 条在代码里是 11 个失败原因——夏普过高/过低、回撤百分比/金额各算一条门槛的两个方向;第 9 条 DSR ≥ 0.95,`dsr` 为 None 或 `n_trials` < 1 按未通过处理)。
  **未接入优化脚本**:目前只有 `scripts/record_promotion.py` 与 `risk/live_admission.py` 消费它,优化器不会自动过闸。

### `strategies/trend_atr_strategy.py`
- 日线双均线(金叉做多/死叉做空,仅空仓入场)+ ATR 移动止损,1 手;可选固定止盈(保守:收盘后触及目标则下一根开盘附近离场)。
- 可优化参数 3 个:`fast_window` / `slow_window` / `atr_mult`;`atr_window=14`、`risk_budget=300` 固定。
- 所有委托价按 `pricetick` 取整(止损取"更紧"一侧);信号收盘后产生、下一根成交,无前视偏差。
- `random_entry_strategy.py`:出场与预算相同、入场随机的对照策略。`instruments.py`:品种参数与成本假设。

### `scripts/build_continuous.py`
- 用分合约日线构建差价法向前复权的主力连续合约(`python scripts/build_continuous.py --symbol C|MA`),输出 `data/results/{品种}_continuous_adj.csv` 与 `_rolls.csv`。
- 主力 = 前一交易日持仓量最大且只向更远月换月(无前视、不回头);主力停牌不换月,到期强制换月;换月价差取换月前最后共同交易日收盘价之差,无共同日时近似并告警。
- 取数缓存:已到期合约永久缓存(含确认不存在),未到期合约缓存 24 小时,网络错误不缓存,原子写入;单合约失败不输出残缺序列。
- 局限:仅 1/5/9 月合约;新浪分合约日线只覆盖 2018-05 之后;差价法可能产生非正价格(会告警)。

### `trading/`(账户风控与运行时)
- `guard_rule.py`:挂在 vnpy_riskmanager 发单链上的 `AccountGuardRule`。拦截锁定期间的**开仓**,平仓永远放行;每秒读权益推进双层熔断
  (当日 600 元 / 距最高点 2000 元);撤超时开仓单;废单延迟分类(CTP 先推回报后写错误日志);紧急平仓状态机(按成交量对账、
  终态后才重试、状态未知不重发、持仓回报滞后时主动查询);`emergency_flatten()` 供运行时看门狗调用。
- `safe_risk.py`:`SafeRiskEngine` 让平仓单跳过计数类内置规则(每日上限/活动委托/重复报单),并加载上面的规则与项目默认配置。
- `runtime.py`:`TradingRuntime` 串起网关、风控、CTA 引擎与通知。启动顺序与对账、看门狗(策略被异常停止/行情停更/心跳)、退出码语义见 ADR。
  **2026-11-10 极限压力测试前置改造**(ADR `20261110-ctp-error-code-and-watchdog-policy.md`):
  - 连接状态机:按精确措辞分别跟踪交易/行情两条通道(「服务器连接断开」→ 断开、「服务器登录成功」→ 恢复),
    边沿告警不重复;「连接成功」的未登录中间态不驱动状态。只读属性 `connection_state` 供演练与巡检读取。
  - 假死看门狗:交易时段 60s 无 Tick 从「只告警」升级为**首次越限强撤全部活动开仓委托**
    (经规则唯一入口 `cancel_open_orders_on_stale_tick()`,平仓单与紧急平仓单天然排除),
    新 Tick 复位边沿;`RuntimeConfig.stale_cancel_open_orders=False` 可回退旧行为,周期 WARN 保留。
    **时段边沿重新武装**(codex 第八轮修复):午休/夜盘休市后重入交易时段时,`_last_tick` 置 None、
    边沿解除,等本时段首 Tick 后才开始计静默,避免把休市时间误算成假死、误撤年龄 0 秒的新挂单;
    初始基线在 `start()` 记录,首个定时器不被当成边沿。
  - 规则侧同类修复:运行时把 `_in_trading_session` 注入 `AccountGuardRule.session_active`,
    规则的 120s 开仓委托超时只在交易时段内计时,时段重入时挂单年龄拨回零点,休市时间不再触发超时撤单。
- **退出时的 FATAL 要留出发送时间**(2026-11-10 codex 三轮修复):`shutdown()` 在关闭通知器前会发
  "退出时仍有持仓"的 FATAL —— 那是无人值守最后的兜底,慢一点的对端就会把它一起丢掉。
  关闭通知器的默认预算是 10 秒,而一条消息的最坏耗时是 (1+重试次数) × 单次超时 + 全部退避(默认 34 秒),
  所以现在按通知器自己的 `drain_timeout`(排空整个队列的总预算)来关,而不是默认的 10 秒。
  `Notifier` 协议里的 `close()` 形参统一叫 `drain_timeout`,与"单条上限"的 `flush_timeout` 属性区分开。
- `events.py`:自定义事件(账户告警、硬锁定)。
- **废单日志级别(2026-11-10 SimNow 实测)**:`vnpy_ctp` 的 `write_error()` 实际调用 `write_log()`,
  所以 CTP 的"交易委托失败,代码：21,信息：…"是以 **INFO 级别**写出来的。只认 `log.level >= ERROR`
  会漏掉全部拒单文本 → 废单分类永远拿不到原因(一律按硬拒绝)→ 紧急平仓单的有限重试永远用不上。
  现在按**精确措辞** `REJECT_LOG_MARKERS = ("交易委托失败", "交易撤单失败", "状态更新")` 识别;
  措辞必须精确——宽泛的"失败"会把"行情订阅失败""登录失败"也收进来,真废单日志迟到时
  就会拿这些无关文本去分类,把可重试的软拒绝误判成硬拒绝。
  第三个措辞"状态更新"覆盖**交易所级拒单**:CTP 的交易所拒单走 `onRtnOrder`
  (`vnpy_ctp/gateway/ctp_gateway.py:673-684` 把状态映射成 REJECTED),原因在 StatusMsg 里,
  日志措辞是"委托 {委托号} 状态更新，{原因}"(同文件 713-719);这条路径**不经过** `onRspOrderInsert`,
  少了它交易所拒单的原因永远是"未知"→ 一律硬拒 → 平仓单被提前升级为人工处理。
  `trading/runtime.py` 的告警另用宽措辞 `GATEWAY_ERROR_MARKERS`(只是给人看,分类错误没有后果)。
  `tests/fakes.py` 的假网关也改成 INFO 级别,免得掩盖这个缺陷。关键词表以**实测文本**为准,
  见 `docs/reject-texts-simnow.md`。
- **废单原因按网关归属**(2026-11-10):`_errors` 记下来源网关,`_error_text_near(ts, gateway_name)`
  只在同网关的日志里找,并且**优先取废单回报之后**到达的那条(实测 CTP 顺序就是先回报后日志)。

### `risk/account_guard.py` 与 `risk/order_policy.py`(纯 Python,100% 覆盖)
- `AccountGuard`:权益驱动的双层熔断状态机,原子持久化,损坏即硬锁定,权益 ≤0/NaN 视为无效采样,日初基准取上次采样。
- `trading_day()`:夜盘归属下一交易日,周末顺延(不处理法定节假日)。
- `order_policy`:废单**三级分类**(2026-11-10 起):`extract_ctp_error_code()` 先抽数字错误码 →
  `CTP_ERROR_KIND` 码表命中直接定论(21/81/63/32/33/14/30/31 均 HARD,软码表留空待 SimNow 实测)→
  未命中回退关键词 → 仍未知 fail-closed 按硬拒绝;正则锚定「代码」前缀且兼容全/半角冒号。
  另有紧急平仓重试节奏 1/3/5 秒、废单风暴检测、超时开仓委托识别。硬拒绝的 FATAL「严重错误,已停止策略开仓」
  只在锁定状态真实转换时发一次:分类延迟窗口内多笔在途单陆续被判 HARD 时,已锁定后的后续硬拒绝只留 ERROR「委托被拒」,
  不重复 FATAL、不声称执行并未发生的撤单(codex 第八轮修复)。**值班须知**:当日熔断/撤单锁定等
  其他原因已锁定后再来硬拒绝,只有 ERROR「委托被拒」留痕、不再有 FATAL——这不代表锁定失效,
  开仓仍在规则链上被物理拦截。硬拒绝锁定时规则发
  FATAL「严重错误,已停止策略开仓」并带错误码;平仓拒单永不锁离场。

### `scripts/collect_reject_texts.py`(SimNow 真实废单文本采集)
- **只连已知 SimNow 地址**:白名单 `SIMNOW_TD_ADDRESSES`(官网公布的第一套 30001/30002/30003 / 第二套 40001)。
  `--confirm-simnow` 只是布尔开关,挡不住"配置写错连上真券商";地址不在表内直接拒绝连接,
  确有别的模拟环境才用 `--allow-any-address` 强制放行(会打警告)。
  第二套官网只公布了 40001(行情 40011),**40002/40003 没有官方背书** —— 按第一套的编号规律外推出来的地址不收。
  地址先经 `normalize_address()` 归一化(去掉 `tcp://` 前缀、首尾空格、路径),避免"长得不一样"被误判成非 SimNow。
- 故意触发几个**必定被拒**的委托(不存在的合约、越界价格、无持仓平仓、超大手数、非交易时段开仓),
  把 CTP 的真实错误文本收进 `docs/reject-texts-simnow.md`,用于校准 `HARD_KEYWORDS/SOFT_KEYWORDS`。
- 安全:
  - 交易时段内运行时,所有"价格合法"的场景改用越界价(不可能成交)并跳过时段场景;
  - **发单前先查持仓**(`symbol_has_position`):"无持仓时平仓"场景发的是平仓单,账户上本来有持仓
    的话那张单会真的把仓位平掉 → 账户上有任何持仓都中止(退出码 5)。
    查询**必须等满整个窗口并查两轮**(默认 15 秒 + 7.5 秒):CTP 的持仓回报是异步的,
    看到别的合约有持仓就早退会漏掉本合约还没到达的回报;而且 CTP 对空账户**不推送任何持仓事件**,
    "没收到回报"既可能是真空仓也可能是回报没到,只查一轮在 SimNow 高负载下会误判。
    交易连接断开(查不准)同样中止;
  - 跑完主动查持仓,发现非零持仓就告警并以非零码退出;
  - 所有价格按 `price_tick` 对齐。
- 已知约束:
  - CTP 先推订单回报、后写错误日志,采集脚本必须**等**错误日志到达再取(`Collector.wait_error`);
  - 每个场景用 `Collector.error_index()` 记下起始条数,只在本次新增的错误里找,避免场景间串味;
  - 交易时段按"所有品种的并集"保守判断(含 CFFEX 的 13:00 下午盘),宁可少采一个场景;
    法定节假日判断不了,假日休市委托只会被拒不会成交,误判方向是安全的;
  - 引擎关闭走 `close_engine()` 限时关闭(CTP 原生线程在断连风暴下不返回),`__main__` 用 `os._exit` 兜底;
  - SimNow 对短时间反复登录敏感(4097),不要连续重跑;第一套环境 16:00 左右关闭。
  - **第二套(7x24)对新注册账号不可用**(2026-11-10 实测):交易登录回"代码：3,信息：CTP:不合法的登录",
    新账号要到第三个交易日才能用。脚本检测到登录失败会立刻退出(退出码 6),不再白等超时。

### `notify/webhook.py`
- 非阻塞(有界队列 + 后台线程)、失败重试 1/3/5 秒、60 秒去重(FATAL 不去重)、发送前脱敏(含 URL 路径里的 token)、generic/飞书/Telegram 三种格式。
- **飞书的"失败也返回 HTTP 200"**:机器人接口出错时状态码仍是 200,错误信息在 JSON 体的 `code`(旧版 `StatusCode`)字段里。
  只看状态码会把"根本没送达"算成发送成功 → 告警静默丢失,而验收标准恰恰是"告警全部到达手机"。
  `_body_error()` 解析响应体,业务码非 0 一律算失败并重试;业务码可能是字符串 `"0"`,比较时用 `int()` 安全转换。
  **响应体不是 JSON(代理/网关回 HTML 2xx)、不是 JSON 对象、或没有业务码 → 同样判为失败**:
  真实的飞书一定回 JSON 并给业务码,拿不到就说明消息没被接收,按成功处理就是静默丢告警。
  业务码**无法确认**时(`None` / 空串 / 非数字)同样判失败(fail-closed)——
  `bool(None)` 是 False,按成功处理就是把"没送达"记成"已送达"。
- **脱敏在结构上做**(2026-11-10 codex 二轮修复):`redact_obj()` 逐个字符串脱敏 dict/list/标量。
  不能"先序列化成 JSON 字符串再 `str.replace`"—— 序列化时引号、换行已经被转义,`replace` 找不到原样的密钥,
  密钥会留在 `extra` 里发出去;而密钥本身含 JSON 语法字符时,替换还可能把整条 JSON 弄坏。
- **脱敏要覆盖键与非 JSON 标量**(2026-11-10 codex 三轮修复):两处漏洞都会让密钥原样发出去 ——
  ① dict 的**键**也可能是密码(`{"pwd": 1}` 的键名),只脱敏值等于没脱敏,现在键名同样走 `redact()`;
  ② 非 JSON 标量(datetime、Decimal、自定义对象)最终会被 `json.dumps(default=str)` 转成字符串,
  **先序列化再脱敏**就来不及了 —— `str(obj)` 里带的密码会原样发出去,
  所以现在在 `redact_obj()` 里**先转成字符串再脱敏**(数字/布尔/None 原样保留,转成字符串会改变语义)。
  非字符串键也要看:数字键在 `json.dumps` 里会变成字符串(`{12345: "值"}` → `{"12345": "值"}`),
  键恰好等于密钥时原样保留就等于漏出去 → 数字/布尔键按 JSON 形态先脱敏,命中就换成脱敏后的字符串。
- **等队列排空的时间**:`flush_timeout` 是**单条**消息的最坏耗时(重试次数 × 单次超时 + 全部退避);
  `drain_timeout` = 单条 × (队列剩余条数 + **正在发送中的那条**)。`close(drain_timeout=...)` 的参数是**总预算**
  (2026-11-10 codex 三轮修复:参数原名 `flush_timeout` 与"单条上限"的属性同名,极易被误当成单条预算,已改名):
  停止哨兵追加在队列末尾,后台线程退出时队列必然已排空,所以只要给够时间就能把最后几条也发出去;
  只按单条算,队列里有多条时 close() 会提前返回,`sent` 仍偏小 —— 慢端被误判成"没送达"。
  **正在发送中的那条不在 `qsize()` 里**(worker 已经把它取走了),漏掉它就会少等一条 ——
  退出时最后那条 FATAL 告警会随进程一起消失,所以单独维护 in-flight 计数。
- **排空有上限,FATAL 优先**:`close()` 的实际等待封顶 `MAX_DRAIN_TIMEOUT`(300 秒)——
  队列上限 200、单条最坏耗时约 34 秒,不设上限时对端持续超时会把交易进程吊在退出阶段接近两小时;
  关闭前把 FATAL 提到队首,退出时最重要的告警("还有持仓")不该排在几十条 INFO 后面。
  取舍:宁可丢一部分非 FATAL 告警,也不能让退出流程拖住。
- **停止信号不只靠队列里的哨兵**:哨兵是**追加**到队列末尾的,队列满时根本放不进去,
  而异常被吞掉 → 后台线程永远收不到停止信号,`close()` 只能干等到预算耗尽。
  所以另用独立的 `_closed` 标志:后台线程取队列超时(0.2 秒)后检查它,排空即退出。
  关闭之后再 `send()` 一律拒绝 —— 消息只会躺在队列里没人发,不能让调用方以为收下了。
- 计数器(`sent`/`failed`/`dropped`)由发送线程与后台线程同时写,`+=` 不是原子操作 → 用独立的 `_counter_lock` 保护。

### `scripts/simnow_guard.py`(SimNow 前置机地址白名单)
- `SIMNOW_TD_ADDRESSES`:官网公布的第一套 `182.254.243.31:30001` / `:30002` / `:30003` 与第二套 `:40001`。
  **第二套官网只公布了 40001**(行情 40011)—— 40002/40003 是按第一套编号规律外推出来的,没有官方背书,不收。
- `normalize_address(td_address)`:去掉 `tcp://` 前缀、首尾空格、路径部分,归一化成 `host:port`。
  不归一化就会因为"长得不一样"被误判成非 SimNow,逼人用逃生舱,反而削弱了防线。
- `check_simnow_address(td_address, allow_any)`:地址不在表内**默认拒绝连接** —— `--confirm-simnow` 只是布尔开关,
  挡不住"配置写错"；CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,**只能靠前置机地址区分**。
  `--allow-any-address` 是逃生舱,会打出"可能连上真实资金账户"的醒目警告。
- 被 `scripts/collect_reject_texts.py` 与 `scripts/drill.py` 共用。

### `scripts/drill.py`(SimNow 实测演练:把运行手册第 5 节做成可重复执行)
- 场景:`latency`(成交→持仓回报延迟)、`tick-stale`(行情断流→看门狗告警)、`disconnect`(交易连接断开→告警与自动重连)、
  `daily-loss`(当日熔断链路)、`hard-lock`(累计死线→平仓→退出码 0)、`overnight`(夜盘首根 Tick 的交易日归属)。
  `disconnect` 场景另记录重连后 `connection_state`、窗口内「委托请求发送失败」告警计数与「信号积压补发:无补发」证据项;
  断连期信号一次性消费、重连不补发的确定性语义由离线测试 `tests/test_disconnect_signal.py` 锁定。
- **状态隔离**:演练用独立的 `.vntrader_drill/` 目录(策略数据、日志、心跳)与独立的 `account_guard.json`,
  演练里的熔断/硬锁定**不会污染实盘状态** `data/state/account_guard.json`。
  vn.py 4.5 没有 `SETTINGS["data_path"]`,真正决定落盘位置的是 `vnpy.trader.utility.TEMP_DIR`,演练改的就是它。
- **只在模拟盘上跑**:复用 `simnow_guard` 的地址白名单 + `--confirm-simnow`。
  逃生舱再加一道:`--allow-any-address` 只表示"我确认过地址",而 `latency`/`daily-loss`/`hard-lock`
  **会真的发委托**(`REAL_ORDER_SCENARIOS`),在非白名单地址上跑必须再加 `--confirm-any-address-trades`;
  用了逃生舱的警告会写进结果文件当审计记录,不能只打在终端上。
- **告警的"本次场景"语义**:`RecordingNotifier` 提供 `mark()`(记下当前告警条数)与 `find/wait_for(..., since)`。
  断线/停更这类告警可能在场景动作之前就发过,不设下界会把旧告警当成本次场景的结果(假阳性)。
  **标记必须在动作之前取** —— 熔断场景的守卫定时器线程随时可能在注入权益的瞬间就发出告警,
  顺序反了会让那条告警落在标记之前被过滤掉(假阴性)。
- **"发出"与"送达"必须分开记**(2026-11-10 codex 二轮修复):底层 `send()` 返回 False(去重/级别过滤/队列满/渠道未配置)
  时手机端并没有响,却会被原实现记成"已送达"。现在每条告警带 `delivered` 标记,
  `delivery_summary()` 区分「发出 / 底层接受 / 未送达」,并把**渠道没配**(`enabled == False`)与
  **送达失败**分开报 —— 前者是"没地方可送",后者是"告警丢了",混在一起会误判。
- **权益注入**:`inject_equity()` 只替换本进程内权益的**来源**,守卫判定、撤单、告警、状态落盘全都照常跑,
  真实账户权益不受影响,场景结束 `restore_equity()` 换回真实查询。
- **未成交立刻撤单**:否则那张单会一直挂着,演练结束后才成交,仓位留到下一次运行里(演练污染实盘状态)。
- **撤单是异步的,撤单后必须确认账户确实干净**(2026-11-10 codex 二轮修复):CTP 的撤单请求发出前后订单仍然可能成交,
  "没等到成交回报"≠"没有仓位"。撤单后用 `wait_flat(close_timeout, baseline)` 确认净持仓回到 baseline;
  仍有残留(撤单与成交撞车)→ 平掉它 + 发 FATAL 告警 + 场景判失败,不能当作"没成交"轻轻放过。
- **只动演练自己开的仓**:`latency` 进场前先记 `baseline = net_position()`,
  平仓量 = **当前净持仓 − baseline**(不是按账户净持仓一刀切),平仓方向按 delta 正负取反(空头用 BUY 平)。
  进场前净持仓 ≠ 0 → **直接拒绝演练**:分不清哪一手是演练开的,强行平仓会误伤别人的仓。
- **进场前按"总手数"拒绝,不能只看净额**(2026-11-10 codex 三轮修复):多空锁仓(各 1 手)的**净持仓是 0**,
  只看净额会被骗过去 —— 收尾平仓时会动到演练之前就存在的仓位。
  现在进场前要求该合约**一手都不能有**(`gross_position()` > 0 就拒绝演练),
  并把进场时的总手数记进 `gross_baseline`,收尾用 `at_baseline()` 同时核对净额**与**总手数:
  方向不明的仓位(`Direction.NET`,净持仓账户)与锁仓都不体现在净额里,
  只看净额会把"还留着仓"当成"已经平干净"。
  `net_position()` 里 `Direction.NET` **单列处理**(既不算多也不算空),不能当成空头去平。
- **净持仓账户上必须按"演练自己开了多少"来平**(2026-11-10 codex 第七轮):CTP 的净持仓账户回报的是
  `Direction.NET`,净额算出来是 0 —— 只看净额会以为"没有仓位要平",于是**不发平仓单**,
  仓位留在账户上,留仓手数还被误报成 0 手。
  所以演练记录**自己开出的方向与手数**(`owned_volume` / `owned_direction`):开的是多仓就用**卖出**平,
  净额看不出来时按这个手数平;留仓手数一律按**总手数**统计。
  这条对 SimNow 尤其关键 —— 模拟账户默认就是净持仓账户。
- **平仓单没成交要撤单**:平仓单还挂在交易所上,过一会儿成交就会留下一笔"晚到的"持仓,
  账户状态与演练结论不一致 → 平仓失败路径补一次撤单。
- **平仓价要挂在交易的那一侧**(2026-11-10 codex 三轮修复):平多(卖出)挂**买一** `bid_price`,
  平空(买入)挂**卖一** `ask_price`。原先一律用 `bid_price`,
  平空时挂在对手价上永远等不到成交 —— 演练会变成"平仓超时",还容易误判成"平不掉"。
- **平仓用 `Offset.CLOSETODAY`**:上期所/能源中心用普通平仓会被交易所拒单(演练开的是当日仓)。
- **平不掉就是失败**:`latency` 场景平仓失败会发 FATAL 告警并让场景判失败 —— "演练通过"却把仓位留在模拟账户里,
  后面的对账与无人值守证据全部失真。
- **硬锁定必须落盘**:`hard-lock` 场景会读风控状态文件确认 `hard_locked` 已写盘,不落盘的话"重启后拒绝交易"只是文档里的说法。
- **前置条件**:看门狗只在交易时段检查行情停更,夜盘首根 Tick 只在夜盘出现 → 前提不满足时场景标记为**跳过(⏭)**而不是判失败。
  时段窗口直接取自策略的 `ENTRY_SESSIONS`(与运行时的 `_in_trading_session` 同一份),两处不一致会让看门狗不触发、场景假失败。
- **⚠ 必须在容器里跑**:运行时的"是否处于交易时段"判断用 `datetime.now()`(系统本地时区),宿主机是 UTC-8、容器是 `TZ=Asia/Shanghai`。
- `latency` 场景会真的开 1 手仓,量完延迟立刻平掉;平不掉就以非零码退出并大声告警。演练用**空策略**(`NoopStrategy`),不会自己下单。

### `scripts/unattended_report.py`(无人值守验收的证据)
- `--report`:解析 vn.py 每日日志(`vt_YYYYMMDD.log`)与心跳文件,生成"每日证据表"(启动次数、成交笔数、异常事件、心跳新鲜度)+ 验收清单。
  **只统计日志里真实出现的内容,不推断"没问题"**。默认只统计容器目录 `runtime/vntrader/log` —— 宿主机 `.vntrader/log` 会被单元测试污染
  (曾出现"一天启动 771 次"的荒唐数字),宿主机日志要用 `--include-host` 显式加上。
  启动次数取 `max(已启动, 启动耗时)`,避免同一次启动被数成两次。
- **时间戳解析**:真实日志行首是 `2026-11-10 15:36:11.123 | INFO | ...`(**带毫秒**),
  按 `%Y-%m-%d %H:%M:%S` 直接解析会失败 → 整天的起止时间全丢。
- **心跳按交易所时区换算**:`heartbeat_day()` 用 `ZoneInfo("Asia/Shanghai")` 把 mtime(epoch 秒)换算成日期。
  `datetime.fromtimestamp` 用的是**本机**时区,宿主机 UTC-8 与容器 Asia/Shanghai 差 16 小时,
  会把 11-10 08:00 的心跳算成 11-09 16:00 → 挂到错误的交易日那一行。
  心跳是"当前进程"的状态,只属于**当天**那一行;不知道属于哪一天时挂到**最后一行**,
  否则"进程已经死了"这条最重要的证据会从表里消失,而验收项"没有进程静默死亡"恰恰看这一列。
- **连续无异常天数**:`longest_clean_streak()` 算**最长连续**段,不是"总共干净了几天"。
  验收要求的是"连续 ≥ 5 个交易日",中间夹一个有异常的日子连续性就断了。
- **"缺天"必须打断连续段**(2026-11-10 codex 二轮修复):进程**静默死亡**的那天根本没有日志文件,
  只按"日志里有的天"遍历会把这一天悄悄跳过 → 连续段被拉长 → 验收结论虚高。
  现在按日历逐天走:**工作日缺日志就打断连续段**;周末不是交易日,缺日志是正常的,既不计入也不打断。
  `missing_weekdays()` 把缺口列进验收清单(法定节假日判断不了,只能"待人工核对",方向是保守的)。
- **周末有日志也不计入交易日**(2026-11-10 codex 三轮修复):容器是**常驻**的,周末照样有日志文件
  (周五夜盘跨过零点还会写进周六的文件 —— `risk/account_guard.py::trading_day()` 把 20 点后的夜盘归到下一个工作日)。
  原实现"有日志就算一天",于是周末被当成交易日,验收标准凭空多出两天 —— 正是这轮审查要消除的"结论虚高"。
  现在周末**既不计入也不打断**;但周末日志里出现**严重**事件(紧急平仓、熔断、硬锁定、对账失败…)
  仍然**打断**连续段 —— 夜盘跨零点的事故不能因为落在周六就被放过;
  周末的**警告**(空闲期的连接抖动)不打断,否则"连续 5 个交易日"永远达不成。
- **熔断类告警必须算成异常**:守卫(`RiskManager`)与运行时(`Runtime`)发出的告警都落在同一个日志文件里,
  `EVENT_MARKERS` 收录了「当日熔断 / 累计死线触发 / 当日禁止开仓 / 账户变更 / 委托被拒 / 平仓委托频繁被拒 /
  风控状态写盘失败 / 平仓缺少行情 / 平仓请求被拆分器拒绝 / 策略初始化失败」。
  「紧急平仓」用**带级别前缀**的写法 `"[FATAL] 紧急平仓:"` —— "[INFO] 紧急平仓完成"是**正常**收尾不能算异常,
  "[FATAL] 紧急平仓失败"又有自己的标记,写成 `"[FATAL] 紧急平仓:"` 才不会重复计数。
- **stdout 编码必须在解析参数之前修好**:`_setup_stdout()` 提到 `main()` 最前面 —— argparse 的用法说明里有中文,
  cp1252 控制台上 `--help` 会抛 `UnicodeEncodeError`,连用法说明都看不到。
- **空的证据表不是证据**:一个 `vt_*.log` 都没有时,警告并以退出码 1 结束 ——
  静默生成一张空表并退出 0 会让人误以为"当天没问题"。
- `--probe-webhook`:连通性自检。配了 `WEBHOOK_URL` → 用真实通知器发一条"【连通性自检】"消息并报告对端是否接受;
  没配 → 起本地 HTTP 接收端离线验证"送达 + 飞书式业务失败可辨识",并明确指出**手机端尚未验证**。
  等待时间按"重试次数 × 单次超时 + 全部退避"计算,否则慢端会被误判成没送达。
- 结果写进 `docs/unattended-log.md`,只覆盖 `<!-- BEGIN UNATTENDED -->`/`<!-- END UNATTENDED -->` 之间的内容,人工段落保留。

### 标记区重写的共同约定(演练 / 采集 / 无人值守报告)
- 三个脚本都往 Markdown 文档的 `<!-- BEGIN … -->` / `<!-- END … -->` 之间写自动生成的内容,标记之外是人工段落,不动。
- **重写时必须把标记原样写回去**。只写正文的话标记会消失,下一次运行 `find(END_MARK)` 返回 -1 →
  走"文档没有标记"的分支,结果只落到 `-raw.md`,主文档从此不再更新**而且不报错**
  (演练审计记录停在旧结果、采集表格停在旧表格,是"静默失效")。
- 三个实现里 `unattended_report.replace_section()` 用切分实现,天然保留两个标记;
  `drill.write_results()` 与 `collect_reject_texts.write_collected()` 手工拼字符串,
  已在 2026-11-10 codex 第六轮修复,并各配了"连续跑两次仍更新主文件"的回归用例。

### `scripts/run_headless.py` 与 `scripts/unlock_account.py`
- `run_headless.py --symbol c2701.DCE --history CADJ.DCE`:无头运行;`--check` 只校验配置不连接。配置来自环境变量/`.env`,凭据不出现在命令行或日志。
- `unlock_account.py`:人工解除累计死线硬锁定(需输入 `UNLOCK` 确认)。

### `scripts/build_continuous.py`(补充)
- `--save` 把复权序列写入 vn.py 数据库(`CADJ.DCE` / `MAADJ.CZCE`),供回测(`optimize.py --symbol CADJ`)和实盘预热使用。

### `scripts/backtest_lib.py` 与 `scripts/optimize.py`
- `run_engine` / `run_backtest`:统一成本口径(rate=0,固定手续费折成点数并入滑点);把 vn.py 静默吞掉的策略异常转成 `RuntimeError`。
- `load_bars_csv` / `run_engine_bars` / `run_backtest_bars`:直接回放内存中的 K 线列表(绕过 vn.py 数据库,vn.py 没有 15 分钟 `Interval`);`cost_multiplier` 用来做成本翻倍压力测试;同样检测被吞掉的策略异常。
- `optimize.py`:三段切分(训练 2005–2018 / 验证 2019–2022 / 测试 2023–),测试段仅 `--test` 才看;
  `--test` 经 `segment_ledger.py` 先查后登记再运行，已登记则在计算前拒绝（登记文件 `config/test_segment_usage.json` 入库）；
  邻域平滑选参(仅完整 3×3×3 邻域的格子可选,无效邻居按悲观下限计);验证/测试往返笔数 <10 时输出醒目警告。

### `strategies/trend_atr_15m_strategy.py`(15 分钟基准,仅研究用)
- 继承 `TrendAtrStrategy`,**只把"一根日线"换成"一根 15 分钟线"**,不引入任何新的入场/出场逻辑(控制变量)。
- `on_tick` 直接抛 `RuntimeError`:不支持实盘/模拟盘,防止误用。
- 归属的日亏熔断按**期货交易日**计算(夜盘归下一交易日),与 `trading_day()` 一致。
- 品种参数 `C15`(`CADJ15.DCE`,size=10,pricetick=1.0,滑点 1.3 点/边)只用于回测。

### `strategies/intraday_demo_strategy.py`(日内演示,仅研究用)
- 继承 15 分钟基准 `TrendAtr15mStrategy`,加两条日内约束:**尾盘强制平仓**(14:30 K 线收盘发出限价平仓委托,
  下一根开盘成交,力争 15:00 收盘前离场;先撤保护性停止单再发平仓单,避免重复执行。限价单遇反向跳空可能不成交,
  此时**不重发被动单**,转入基准逻辑重新挂出保护性停止单——极值只收紧不放松、夜盘继续有效,由止损接管离场,
  离场可能发生在夜盘。这是「尽力平仓 + 止损兜底」的如实语义,回测引擎只撮合限价单,跳空日无法模拟市价单)与
  **只在日盘白天开新仓**(夜盘与尾盘 14:30 之后一律不开,避免刚开就平的空转)。
- 定位是平台能力面的「日内短线」形态示例,不是策略准入对象;任何策略仍须先赢过 15 分钟基准零分线。
- 同样 `on_tick` 抛 `RuntimeError`(不支持实盘,防误用);无新增可优化参数(`flat_time` 为常量,不进 parameters)。

### `scripts/optimize_15m.py`(15 分钟基准的网格优化与三段验证)
- 方案见 `docs/adr/20261110-baseline-15m.md`(**在看到任何结果之前就已提交**):训练 2020-07~2022-12、验证 2023、测试 2024-01~2026-06。
- 网格一次定死:快线 {8,12,16,24,32} × 慢线 {48,72,96,144,192,288}(要求 `slow ≥ 2×fast`)× ATR 倍数 {1.5,2,2.5,3,4},共 145 组。
- 选参:训练段"邻域平均夏普"(复用 `optimize.smoothed`),成交笔数 < 100 的组合不参与排名。
- **测试段只有加 `--test` 才会被评估**,且只应运行一次(已由 `segment_ledger.py` 强制:该测试段已登记为使用过,再跑会被拒绝);验证/测试段同时输出"成本×2"压力测试行。
- **结果为负面**(测试段 -20.2%、夏普 -1.08,成本吞掉全部毛利),详见 ADR;**测试段已被使用一次**。

### `scripts/walk_forward_optimizer.py`(Walk-Forward 滚动优化,测试段物理隔离)
- 方案见 `docs/adr/20261110-walk-forward-optimizer.md`(**方法学在看到任何结果之前写定**):只在 2020-07~2023-12 区间滚动,
  3 个窗口(训练 24 月 / 验证 6 月 / 步长 6 月,验证段互斥),测试段起点之后的数据没有任何代码路径可以触达。
- 网格只扫 fast ∈ {10,14,18,22,26,30} × slow ∈ {40,50,60,70,80,90}(仍要求 `slow ≥ 2×fast`,31 组);
  `atr_mult=1.5` 与 `risk_budget=300` 固定(参数字典不传 risk_budget 键,无法被覆盖),不使用邻域平滑,按原始夏普多级排序取 Top5。
- 隔离三层护栏:`TEST_SEGMENT_START` 硬断言(越界 `ValueError`)、API 反例测试、源码扫描测试(2024+ 年份字面量只能出现在常量定义行);不提供 `--test` 开关。
- 多进程:模块级 `_backtest_one`(入参全基础类型,Windows spawn 可 pickle)+ 单个 `ProcessPoolExecutor`
  贯穿全部窗口;`--workers 1` 为进程内串行降级路径;每进程独立 `lru_cache`;回测内核仍是 vn.py `BacktestingEngine`(经 `backtest_lib.run_engine_bars`),
  借此从 `engine.strategy.skipped_by_budget` 读取 300 元预算弃单计数,`backtest_lib.py` 零改动;
  弃单率分母不含熔断拦截与段末持仓笔数(ADR 有误读边界说明)。
- 输出:逐窗口 Top5 训练/验证对照 + 跨窗口汇总(第一名验证夏普均值/中位数、正夏普窗口占比、最差验证回撤、预算弃单率),
  明细写 `data/results/walk_forward_C15.csv`(不纳入版本控制);vn.py 夏普为自然日 ×240 口径,只做同口径排序。
- 结果(2026-11-10 一次性运行):三个窗口训练段 Top5 夏普全部为负,验证段 15 组参数夏普全部为负、正夏普窗口占比 0%,
  预算弃单率 0%(atr_mult=1.5 下止损距离都在 300 元内,空仓并非预算造成);与基线 ADR 的负面结论互相印证,详见 ADR 结果章节。

### 纪律机制补缺(Issue #6,详见 `docs/adr/20261111-discipline-enforcement.md`)
- **解锁问责**:`AccountGuard` 记录 `hard_locked_at` 与 `unlock_history`;`scripts/unlock_account.py` 在硬锁定当个交易日内拒绝解锁(退出码 3),`--force` 须交互输入 `FORCE` 且不能与 `--yes` 同用;解锁后峰值重置的语义不变。
- **非策略委托检测**:`AccountGuardRule._track_manual_order`——reference 不是 `CtaStrategy_*`/`FLATTEN_REF` 的新委托,经 3 秒确认窗口后告警并按交易日计数(`guard.manual_orders`);只告警不锁定;启动对账通过后才 arm;每日证据表记为严重事件。
- **实盘准入**:`risk/live_admission.py` + `scripts/record_promotion.py`——`run_headless` 连接生产 API(`实盘` 且非 SimNow)前要求 `data/state/promotion_approval.json`,该文件仅在 `check_gate` 全部通过时写出;SimNow 与 `测试` 环境豁免。

### `scripts/collect_terminal_info.py` 与 `scripts/penetration_check.py`(穿透式监管接入,Issue #5)
- 运行机是**另一台 Windows**,直接用 Python 运行(不用 Docker:容器内采到的 IP/MAC 与宿主机不符;树莓派 ARM64 无 CTP 库)。
- `collect_terminal_info.py`:仅 Windows,PowerShell `Get-CimInstance` 采 OS/CPU/主板/BIOS/硬盘序列号/网卡 MAC 与 IP;
  用 UDP connect(不发包)判断出网网卡并标出虚拟网卡(VPN/TUN/虚拟机);`--public-ip` 才访问第三方查公网 IP。`--target <前置地址>` 可按该目标的路由判断出网网卡。
- `penetration_check.py`:`--confirm-broker-test` 必加;目标闸门只放行 `CTP_ENVIRONMENT=测试` 或 SimNow 地址预演,
  `实盘`+非 SimNow = 生产 API **无条件拒绝**。按里程碑日志(连接→授权→登录→结算→合约→行情)逐步判定,再订阅 Tick、查账户/持仓;
  `--with-order` 在交易时段报跌停价买 1 手并立即撤销,意外成交以退出码 5 告警。报告写 `data/penetration/`(不入库),账号头尾保留、密钥全部 `***`。
- 报备材料草稿见 `docs/broker-filing.md`;向期货公司确认的问题与部署方式见 `docs/live-readiness.md` C2/C3。

### `scripts/run_gui.py`(图形界面,人工观察用)
- 打开 VeighNa Trader 界面,按 `.env` 自动连接 CTP;`--subscribe` 让合约表到位后自动订阅行情;`--no-connect` 手动连接;`--check` 只校验配置。
- 同样挂载 `SafeRiskManagerApp`:界面里手动下单也受账户风控约束;硬锁定生效时拒绝连接。
- 另挂载 vn.py 官方 **AlgoTrading**(算法单/拆单:TWAP、冰山、狙击手等)与 **SpreadTrading**(价差/组合套利)两个应用页:
  平台能力面的演示与手动试验用,同样**不进无人值守路径**。
- 多账户:vn.py `MainEngine.add_gateway` 原生支持添加多个 CTP 网关(界面「系统 → 连接 CTP」可按不同账号分别连接)。
  本项目默认单账户(`.env` 一套配置),且 `AccountGuardRule` 当前按**单账户**监控(可配 `account_id`,空 = 第一个账户)——
  多账户属于平台能力面,未在项目配置中启用,启用前需另行评估账户风控的多账户覆盖。
- **没有**启动对账、看门狗和 Webhook 告警,**不得用于无人值守**,也不要与无头版同时运行。


### 研究沙盒(2026-11-12,ADR `20261112-research-sandbox-*`)
- `scripts/research_api.py`:CLI 入口(`data`/`approve`/`review`/`backtest`/`gate`/`evaluate`/`results`);`review` 先展示带行号源码与静态筛查再问 y/N;批准绑定代码 sha256。
- `scripts/research_sandbox.py`:路径限制、静态筛查(纵深防御,非完整隔离)、子进程环境白名单、批准账本 `data/research-control/approvals.json`(容器内只读)。
- `scripts/research_gate.py` / `research_grid.py`:服务端按候选声明的 `param_grid` 在选参窗口(止于 2022-06-30)寻优,再对验证窗口做晋级门槛评估(3×3×3 邻域参数平原、随机入场对照、成本翻倍)。
- `scripts/research_config.py` + `config/research_sandbox.json`:全部可调参数,严格校验;`iteration.max_iterations`/`window_hours`(滚动窗口内提交上限,服务端强制,超限 429)。
- `scripts/research_gateway.py`:标准库 HTTP 网关,白名单路由与 `docs/research-gateway.openapi.json` 一致;`view=report|text` 返回精简中文报告;反馈只返回最近 N 条失败 Trace。容器见 `docker-compose.research.yml`、`docker/research/`。
- `risk/diagnosis.py`、`risk/plateau.py`、`risk/candidate_eval.py`、`risk/report.py`:纯函数;`strategies/random_entry_15m_strategy.py`:随机入场对照。

### `scripts/drill_trace.py` 与 `tools/bisheng/`
- `drill_trace.py`:`drill.py --trace` 的 faulthandler 诊断埋点(定位 `td_api.close()` 段错误)。
- `tools/bisheng/`:毕昇工作流构建/运行脚本(`bs.py` API 封装、`bsflow.py` 节点 helper、`build_v3.py` LLM 版、`build_v3_manual.py` 无 LLM 联调版、`run_*.py`);用法见该目录 README。`templates.json` 需用 `extract_templates.js` 重新生成,不入库。

### `notify/webhook.py` 落盘待补发队列
发送最终失败的告警写入 `WEBHOOK_OUTBOX`(JSONL),网络恢复后补发(【补发】前缀、保留原时间、积压 INFO≥3 条合并摘要),重启后继续;容量与最大年龄有上限。
