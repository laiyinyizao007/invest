"""账户级风控规则(挂在 vnpy_riskmanager 的发单检查链上)。

职责:
1. check_allowed:账户被锁定时拦截**新开仓**;平仓永远放行。
2. on_timer(每秒):读取账户权益 → 双层熔断;识别并撤销超时的开仓委托;推进紧急平仓状态机;延迟分类废单。
3. on_order:统计废单。**CTP 先推废单回报、后写错误日志**,所以不能在回报到达时立刻分类,
   而是延迟 reject_classify_delay 秒,在前后 3 秒窗口里取最近的错误日志再分类;未知按硬拒绝。
   硬拒绝或废单风暴 → 当日禁止开仓。开仓单永不自动重试。
4. 单日撤单次数达到 cancel_lock_limit → 当日禁止开仓(内置规则的撤单上限是兜底,这里更早触发)。
5. 阈值预警:vn.py 内置规则触达上限时只写日志并拒单,这里把这类日志转成 WARN 告警(按指标节流),
   满足「达到阈值后预警」(国信程序化测试 3.1)。
6. 手动暂停:`pause_trading.flag` 存在时拒绝新开仓,平仓放行(国信程序化测试 5.1)。

紧急平仓走 risk_engine._send_order(绕过风控规则的原始发单通道):平仓只会降低风险,不能被限流拦住。
紧急平仓状态机的关键约束(防止重复下单):
- 按**成交量**对账,而不是反复读取 OMS 持仓:CTP 的持仓回报约每 2 秒才刷新一次,成交后 OMS 里仍是旧持仓;
- 上一轮每笔平仓委托都被观察到处于终态后才可能重试(最多 3 次,间隔 1/3/5 秒);
- 状态未知(无回报)绝不重发,超时升级人工;成交量已覆盖持仓但持仓回报迟迟不清零,同样不重发,主动查询持仓并升级人工。
"""
import math
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from vnpy.event import Event
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_LOG
from logging import ERROR  # 标准库的级别常量:不要从 vnpy.trader.logger 引,
# 那会把日志模块(在导入时就按 TEMP_DIR 建好文件 sink)提前拉进来,隔离目录就名不副实了
from vnpy.trader.object import LogData, OrderData, OrderRequest
from vnpy.trader.utility import round_to
from vnpy_ctastrategy.base import APP_NAME as CTA_APP_NAME
from vnpy_riskmanager.base import APP_NAME
from vnpy_riskmanager.template import RuleTemplate

from risk.account_guard import AccountGuard, AccountLimits, LockEvent, trading_day
from risk.positions import describe, undirected
from risk.order_policy import (
    RejectKind,
    RejectMonitor,
    RetryPolicy,
    classify_reject,
    extract_ctp_error_code,
    find_stale_orders,
)
from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLOSING_OFFSETS = {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}
ACCOUNT_RULE_NAME = "账户风控"   # 规则在 RiskEngine.rules 里的键名,多处引用,别再写魔法字符串
ERROR_WINDOW = 3.0                      # 废单与其错误日志的最大时间差(秒,前后均可)
MIN_POSITION_REFRESH_TIMEOUT = 3.0     # 等持仓回报的下限(秒)
CANCEL_RETRY_SECONDS = 10.0             # 撤单请求发出后多久仍未终结则允许再次撤单
PAUSE_FLAG_NAME = "pause_trading.flag"  # 与状态文件同目录;存在 = 手动暂停开仓(scripts/pause_trading.py 写/删)
THRESHOLD_LOG_PREFIX = "委托被拦截，"      # vnpy_riskmanager 的 RiskEngine.write_log 统一前缀
THRESHOLD_LOG_MARKERS = ("达到上限", "超过上限")   # 笔数/手数/金额类阈值(含合约级笔数/撤单/成交上限);合约代码不存在、价位不合规不属于阈值
THRESHOLD_ALERT_INTERVAL = 300.0        # 同一指标的阈值预警最短间隔(秒),避免每笔被拒都推一条
FLATTEN_REF = "account_guard_flatten"   # 紧急平仓委托的标记,废单处理据此识别,不依赖事件到达顺序
STRATEGY_REF_PREFIX = f"{CTA_APP_NAME}_"   # CTA 引擎下单的 reference 前缀(vnpy_ctastrategy/engine.py)
MANUAL_CONFIRM_DELAY = 3.0              # 首个事件没有策略标记时,等这么久再认定为非策略委托(见 on_order 的说明)
SEEN_ORDERS_LIMIT = 5000                # 已见委托号的上限,超过则丢弃最旧的一半(只影响去重,不影响风控)
# CTP 把委托失败写成 INFO 级别的日志,这些措辞用来认出"这是一条拒单原因"。
# 实测措辞(2026-11-10 SimNow):"交易委托失败,代码：21,信息：…"、"交易撤单失败,代码：…"。
# **必须用精确措辞**:宽泛的"失败"会把"行情服务器登录失败""行情订阅失败""委托请求发送失败"
# 也收进来,这些不是废单原因;一旦真废单的日志迟到/缺失,就会拿这些无关文本去分类,
# 把可重试的软拒绝误判成硬拒绝,紧急平仓的有限重试就永远用不上。
#
# "状态更新"覆盖**交易所级别**的废单:CTP 收到交易所的拒单回报时走 `onRtnOrder`,
# vnpy_ctp 把状态映射成 REJECTED(源码 ctp_gateway.py:673-684),原因写在 StatusMsg 里,
# 日志措辞是"委托 {委托号} 状态更新，{原因}"(ctp_gateway.py:713-719)。
# 这条路径**不经过** `onRspOrderInsert`,所以上面两个措辞收不到它;漏掉的话,
# 交易所拒单的原因就永远是"未知"→ 一律按硬拒绝 → 平仓单被提前升级为人工处理。
REJECT_LOG_MARKERS = ("交易委托失败", "交易撤单失败", "状态更新")
# 运行时"网关错误"告警用的宽措辞:这里只是给人看,分类错误没有后果,所以可以宽一点。
GATEWAY_ERROR_MARKERS = ("失败", "被拒绝", "拒绝", "不支持", "非法")


class AccountGuardRule(RuleTemplate):
    """账户风控规则"""

    name: str = ACCOUNT_RULE_NAME

    parameters: dict[str, str] = {
        "daily_loss_limit": "当日熔断(元)",
        "max_drawdown": "累计死线(元)",
        "flatten_on_daily_lock": "当日熔断时平仓",
        "stale_order_seconds": "开仓委托超时(秒)",
        "reject_storm_limit": "废单风暴笔数",
        "reject_storm_window": "废单风暴窗口(秒)",
        "reject_classify_delay": "废单分类延迟(秒)",
        "cancel_lock_limit": "单日撤单锁定次数",
        "flatten_ack_timeout": "平仓回报超时(秒)",
        "position_refresh_timeout": "持仓刷新超时(秒)",
        "account_id": "账户ID(空=第一个)",
        "state_file": "状态文件",
    }

    variables: dict[str, str] = {
        "lock_status": "锁定状态",
        "peak_equity": "权益最高点",
        "day_start_equity": "当日开盘权益",
        "cancel_count": "今日撤单次数",
    }

    def on_init(self) -> None:
        self.daily_loss_limit: float = 600.0
        self.max_drawdown: float = 2000.0
        self.flatten_on_daily_lock: bool = True
        self.stale_order_seconds: float = 120.0
        self.reject_storm_limit: int = 3
        self.reject_storm_window: float = 600.0
        self.reject_classify_delay: float = 1.5
        self.cancel_lock_limit: int = 50
        self.flatten_ack_timeout: float = 10.0
        self.position_refresh_timeout: float = 6.0
        self.account_id: str = ""
        self.state_file: str = ""

        self.lock_status: str = "OK"
        self.peak_equity: float = 0.0
        self.day_start_equity: float = 0.0
        self.cancel_count: int = 0

        self.clock = datetime.now             # 测试可替换
        self.mono = time.monotonic            # 测试可替换
        self.retry_policy = RetryPolicy()
        self.guard = AccountGuard()
        self.reject_monitor = RejectMonitor()
        self.close_reject_monitor = RejectMonitor(5, 600.0)   # 平仓委托频繁被拒只告警,不锁定(不能阻止离场)
        self._cancel_day = None
        self._open_orders: dict[str, tuple[float, bool]] = {}   # vt_orderid -> (创建时间, 是否开仓单)
        self._cancel_requested: dict[str, float] = {}
        self._errors: deque[tuple[float, str, str]] = deque(maxlen=50)   # (时刻, 文本, 来源网关)
        self._pending_rejects: dict[str, tuple[float, OrderData]] = {}
        self._flatten: dict | None = None
        # 非策略委托检测(docs/adr/20261111-discipline-enforcement.md):启动对账完成后由运行时 arm 才开始计数,
        # 否则 CTP 登录后回放的当日历史委托(reference 为空)会被误认成人工下单。
        self._manual_armed = False
        self._threshold_alerted: dict[str, float] = {}   # 指标 -> 上次预警的单调时刻
        self._pause_active = False
        self._seen_orders: dict[str, None] = {}      # 保持插入顺序,超限时丢最旧的
        self._own_orders: set[str] = set()
        self._manual_counted: set[str] = set()      # 已计数的非策略委托号:不随 _seen_orders 淘汰,防回放重复计数
        self._manual_pending: dict[str, tuple[float, OrderData]] = {}
        self._hard_emitted: set[bool] = set()    # 已发出的硬锁定事件(flat=False / flat=True 各最多一次)
        self._save_error_alerted = False
        # 交易时段判定:由运行时注入(默认始终在时段内,保持脱离运行时的单测原有行为)。
        # 超时撤单只在交易时段内计时,午休/夜盘休市时间不计入挂单年龄。
        self.session_active = lambda: True
        self._was_in_session = True

        self._account_events: dict[str, datetime] = {}    # 账户号 -> 最近一次该账户资金回报到达的时刻(只供状态页判断权益新鲜度)
        self.risk_engine.event_engine.register(EVENT_LOG, self._on_log)
        self.risk_engine.event_engine.register(EVENT_ACCOUNT, self._on_account_event)

    def update_setting(self, rule_setting: dict) -> None:
        super().update_setting(rule_setting)
        path = Path(self.state_file) if self.state_file else None
        if path is not None and not path.is_absolute():
            path = PROJECT_ROOT / path          # 锚定到项目根:换启动目录不能让硬锁定状态"丢失"
        # 超时参数设下限:配成 0 会让"等持仓回报"形同虚设(一个 tick 就宣布空仓/升级)
        self.position_refresh_timeout = max(float(self.position_refresh_timeout), MIN_POSITION_REFRESH_TIMEOUT)
        self.flatten_ack_timeout = max(float(self.flatten_ack_timeout), 3.0)
        self.guard = AccountGuard(AccountLimits(self.daily_loss_limit, self.max_drawdown), path)
        self.reject_monitor = RejectMonitor(self.reject_storm_limit, self.reject_storm_window)
        self.peak_equity, self.day_start_equity = self.guard.peak_equity, self.guard.day_start_equity
        if path is not None:
            self.risk_engine.main_engine.write_log(f"账户风控状态文件: {path}", source="AccountGuard")
        else:
            # 没有状态文件 = 熔断状态不落盘,手动暂停标志也无处可放:不能让操作员以为暂停生效了
            self.risk_engine.main_engine.write_log(
                "未配置账户风控状态文件(state_file):熔断状态不落盘,手动暂停(pause_trading.flag)不可用", source="AccountGuard")

    # ---------- 发单检查 ----------

    def check_allowed(self, req: OrderRequest, gateway_name: str) -> bool:
        if req.offset in CLOSING_OFFSETS:
            return True                                           # 平仓永远放行
        today = trading_day(self.clock())
        if not self.guard.can_open(today):
            self.write_log(f"账户处于{self.guard.status(today)},禁止新开仓:{req}")
            return False
        if self._pause_flag_exists():
            self.write_log(f"交易已手动暂停({PAUSE_FLAG_NAME}),禁止新开仓:{req}")
            return False
        return True

    # ---------- 手动暂停 ----------

    def _pause_flag_path(self) -> Path | None:
        state_path = self.guard.state_path
        return state_path.parent / PAUSE_FLAG_NAME if state_path is not None else None

    def _pause_flag_exists(self) -> bool:
        path = self._pause_flag_path()
        return path is not None and path.exists()

    def _sync_pause_state(self) -> None:
        """暂停状态变化时发一次告警(边沿触发)。"""
        active = self._pause_flag_exists()
        if active == self._pause_active:
            return
        self._pause_active = active
        if active:
            self._alert("WARN", "交易已暂停", f"检测到 {PAUSE_FLAG_NAME}:新开仓一律拒绝,平仓与持仓管理照常。解除请运行 scripts/pause_trading.py off")
        else:
            self._alert("WARN", "交易已恢复", "暂停标志已解除,允许新开仓")

    # ---------- 事件 ----------

    def arm_manual_detection(self) -> None:
        """启动对账完成后调用:此后首次出现、且不是本系统发出的委托才算"非策略委托"。"""
        self._manual_armed = True

    def _track_manual_order(self, order: OrderData, now: float) -> None:
        """识别非策略委托(只告警+计数,不锁定、不拦截)。

        本系统发出的委托:CTA 策略单 reference 以 `CtaStrategy_` 开头,紧急平仓是 FLATTEN_REF。
        vnpy_ctp 只在 send_order 返回时的那次事件里带 reference,之后 CTP 回报(onRtnOrder)的 reference 为空;
        别的终端(手机 App、柜台)下的单只会以"reference 为空"的回报出现。所以:
        - 首次出现且 reference 为空的委托先挂起 MANUAL_CONFIRM_DELAY 秒,期间若见到带策略标记的事件则放行
          (CTP 回报线程可能抢在 send_order 的事件之前到达);
        - 已见过的委托号不再判定,避免断线重连后的回放造成重复计数。
        """
        oid = order.vt_orderid
        is_own = order.reference == FLATTEN_REF or order.reference.startswith(STRATEGY_REF_PREFIX)
        first = oid not in self._seen_orders
        if first:
            if len(self._seen_orders) >= SEEN_ORDERS_LIMIT:
                for old in list(self._seen_orders)[:SEEN_ORDERS_LIMIT // 2]:
                    del self._seen_orders[old]
            self._seen_orders[oid] = None
        if is_own:
            self._own_orders.add(oid)
            self._manual_pending.pop(oid, None)
        elif oid not in self._manual_counted and first and self._manual_armed and oid not in self._own_orders:
            self._manual_pending[oid] = (now, order)

    def _process_manual_pending(self, now: float) -> None:
        for oid, (t, order) in list(self._manual_pending.items()):
            if now - t < MANUAL_CONFIRM_DELAY:
                continue
            del self._manual_pending[oid]
            if oid in self._own_orders:
                continue
            if len(self._manual_counted) >= SEEN_ORDERS_LIMIT:
                self._manual_counted.clear()      # 上限兜底:人工委托要累计到上千笔才会触及,清空只可能让回放重复计一次
            self._manual_counted.add(oid)
            today = self.guard.record_manual_order(self.clock())
            self._alert("ERROR", "检测到非策略委托",
                        f"{order.vt_symbol} {order.direction.value if order.direction else ''}{order.offset.value} "
                        f"{order.volume}@{order.price}(委托号 {oid},reference={order.reference!r})。"
                        f"今日第 {today} 笔。这不是本系统发出的单:人工干预会使无人值守验收作废,请核对账户与持仓。")

    def on_order(self, order: OrderData) -> None:
        now = self.mono()
        self._track_manual_order(order, now)
        is_open = order.offset not in CLOSING_OFFSETS
        if order.is_active():
            self._open_orders.setdefault(order.vt_orderid, (now, is_open))
        else:
            self._open_orders.pop(order.vt_orderid, None)
            self._cancel_requested.pop(order.vt_orderid, None)

        if order.status == Status.CANCELLED:
            self._count_cancel()
        elif order.status == Status.REJECTED:
            self._pending_rejects[order.vt_orderid] = (now, order)   # 稍后(等错误日志到达)再分类

    def _on_threshold_log(self, msg: str) -> None:
        """风控规则触达阈值的日志 → WARN 告警(同一指标 THRESHOLD_ALERT_INTERVAL 秒内只发一次)。"""
        body = msg[len(THRESHOLD_LOG_PREFIX):]
        metric = body.split("：", 1)[0]
        metric = "".join(ch for ch in metric if not (ch.isdigit() or ch == "."))   # "汇总委托笔数201达到上限200" / 整数浮点手数 → 同一指标
        if metric.startswith("合约"):
            # 合约级阈值按合约分别节流:A 合约刚预警过,B 合约的突破不能被吞掉
            found = re.search(r"symbol='([^']+)'", body)
            metric += "@" + (found.group(1) if found else "")
        now = self.mono()
        last = self._threshold_alerted.get(metric)
        if last is not None and now - last < THRESHOLD_ALERT_INTERVAL:
            return
        self._threshold_alerted[metric] = now
        self._alert("WARN", "风控阈值触发", body)

    def _on_account_event(self, event: Event) -> None:
        """记录账户资金回报的到达时间。**不参与任何熔断判断**:vn.py 在网关断开后仍缓存旧账户对象,
        只看定时器读到的数据会误以为权益是新的;状态页据此区分『风控进程活着』与『权益还在被刷新』。"""
        account_id = getattr(event.data, "accountid", None)
        if isinstance(account_id, str) and account_id:
            self._account_events[account_id] = self.clock()          # 按账户记:别的账户的回报不能让当前展示的账户显得新鲜

    def _on_log(self, event: Event) -> None:
        log: LogData = event.data
        if (log.gateway_name == APP_NAME and log.msg.startswith(THRESHOLD_LOG_PREFIX)
                and any(m in log.msg for m in THRESHOLD_LOG_MARKERS)):
            self._on_threshold_log(log.msg)
            return
        # 只接受已注册网关(如 CTP)的错误日志;运行时/引擎自身的 ERROR 日志(如"通知发送异常")不能被当成废单原因
        if log.gateway_name not in self.risk_engine.main_engine.gateways:
            return
        # vnpy_ctp 的 write_error() 实际调用 write_log(),也就是**以 INFO 级别**输出
        # "交易委托失败，代码：21，信息：..."。只认 log.level >= ERROR 会漏掉全部委托失败文本,
        # 废单分类就永远拿不到原因(2026-11-10 在 SimNow 上实测确认)。所以按文本特征识别。
        # 只收**明确的废单措辞**:宽泛的"失败"会把"行情订阅失败""登录失败"也收进来,
        # 它们不是废单原因,拿来分类会把软拒绝误判成硬拒绝。
        if any(k in log.msg for k in REJECT_LOG_MARKERS):
            # 记下来源网关:多网关时不能拿 A 网关的错误去解释 B 网关的废单
            self._errors.append((self.mono(), log.msg, log.gateway_name))

    def _error_text_near(self, ts: float, gateway_name: str = "") -> str | None:
        """取与废单时刻最接近(前后 ERROR_WINDOW 秒内)的废单日志。

        优先取**废单回报之后**到达的那条:实测 CTP 的 onRspOrderInsert 是先 `on_order()`
        (回报)后 `write_error()`(日志),所以真废单的原因一定在回报之后;
        回报之前的那条多半是别的事件(比如上一次废单)留下的。
        """
        candidates = [e for e in self._errors if not gateway_name or e[2] == gateway_name]
        after = [e for e in candidates if e[0] >= ts]
        for pool in (after, candidates):
            best = min(pool, key=lambda e: abs(e[0] - ts), default=None)
            if best is not None and abs(best[0] - ts) <= ERROR_WINDOW:
                return best[1]
        return None

    def _count_cancel(self) -> None:
        today = trading_day(self.clock())
        if self._cancel_day != today:
            self._cancel_day, self.cancel_count = today, 0
        self.cancel_count += 1
        if self.cancel_count >= self.cancel_lock_limit:
            self._lock_daily(f"今日撤单 {self.cancel_count} 次 >= {self.cancel_lock_limit}", flatten=False)

    def _process_pending_rejects(self, now: float) -> None:
        for oid, (t, order) in list(self._pending_rejects.items()):
            if now - t < self.reject_classify_delay:
                continue
            del self._pending_rejects[oid]
            self._handle_reject(order, self._error_text_near(t, order.gateway_name), t)

    def _handle_reject(self, order: OrderData, text: str | None, ts: float) -> None:
        kind = classify_reject(text)
        self._alert("ERROR", "委托被拒", f"{order.vt_symbol} {order.direction.value}{order.offset.value} "
                                         f"{order.volume}@{order.price} 原因:{text or '未知'}({kind.value})")
        if order.reference == FLATTEN_REF:
            if kind is RejectKind.HARD:                           # 平仓单被硬拒绝:不再重试,升级
                self._escalate_flatten(f"平仓委托被硬拒绝:{text or '未知原因'}")
            return
        if order.offset in CLOSING_OFFSETS:
            if self.close_reject_monitor.add(ts):                 # 平仓单被拒不能锁定(离场不能被阻止),只告警
                self._alert("ERROR", "平仓委托频繁被拒", f"{self.close_reject_monitor.count} 笔,请检查策略与持仓是否一致")
            return
        storm = self.reject_monitor.add(ts)
        if kind is RejectKind.HARD or storm:
            # FATAL 只在锁定状态真实转换时发:分类延迟窗口内可能有多笔在途单陆续被判 HARD,
            # 第一笔已经锁定并撤单后,后续硬拒绝再发 FATAL 既造成告警疲劳,
            # 其「已撤销全部挂单」措辞也与实际动作(已无单可撤)不符。已锁定时上面的 ERROR「委托被拒」已留痕。
            if kind is RejectKind.HARD and self.guard.can_open(trading_day(self.clock())):
                # 硬拒绝 = 重试不会成功的确定性失败(资金不足/涨跌停/权限等):用 FATAL 明确告知
                # 「策略已被停止开仓」,并带上 CTP 错误码,避免无人值守时淹没在普通「委托被拒」告警里。
                code = extract_ctp_error_code(text)
                code_part = f"[CTP 错误码 {code}] " if code is not None else ""
                self._alert("FATAL", "严重错误,已停止策略开仓",
                            f"开仓委托被硬拒绝,{code_part}原因:{text or '未知原因'}。已撤销全部开仓挂单,当日禁止新开仓。")
            self._lock_daily(f"开仓委托被{'硬' if kind is RejectKind.HARD else '频繁'}拒绝:{text or '未知原因'}", flatten=False)

    # ---------- 每秒定时 ----------

    def on_timer(self) -> None:
        now_dt = self.clock()
        now = self.mono()
        main = self.risk_engine.main_engine

        for order in main.get_all_active_orders():               # 启动前已存在的活跃委托也要纳入超时管理
            self._open_orders.setdefault(order.vt_orderid, (now, order.offset not in CLOSING_OFFSETS))

        account = self._account()
        if account is not None:
            note = self.guard.bind_account(account.accountid)
            if note:
                self._alert("WARN", "账户变更", note)
            for ev in self.guard.update(account.balance, now_dt, sampled_at=self._account_events.get(account.accountid)):
                self._on_lock_event(ev)
            self.peak_equity, self.day_start_equity = self.guard.peak_equity, self.guard.day_start_equity
        if self.guard.save_error and not self._save_error_alerted:
            self._save_error_alerted = True
            self._alert("ERROR", "风控状态写盘失败", f"{self.guard.save_error}。熔断仍在内存中生效,但重启后可能丢失,请检查磁盘")
        self.lock_status = self.guard.status(trading_day(now_dt))

        self._process_pending_rejects(now)
        self._process_manual_pending(now)
        self._sync_pause_state()
        in_session = self.session_active()
        if in_session and not self._was_in_session:
            # 非交易→交易时段边沿:休市期间没有成交/撤单回报是正常的,挂单年龄要从开盘时刻重新计。
            # 不做这一步,午休两小时后所有旧挂单会在开盘瞬间被判 120s 超时撤掉(单调时钟含休市时间)。
            self._open_orders = {oid: (now, is_open) for oid, (_, is_open) in self._open_orders.items()}
            # 只拨开仓单的撤单节流时间戳:平仓单(含紧急平仓)的 10s 节流不能被休市边沿无差别清掉,
            # 否则未来若出现短于 10s 的时段间隙,平仓撤单可能绕过节流重复发请求。
            open_ids = {oid for oid, (_, is_open) in self._open_orders.items() if is_open}
            self._cancel_requested = {oid: ts for oid, ts in self._cancel_requested.items() if oid not in open_ids}
        self._was_in_session = in_session
        if in_session:
            stale = find_stale_orders(
                [(oid, created, is_open) for oid, (created, is_open) in self._open_orders.items()],
                now, self.stale_order_seconds,
            )
            for oid in stale:
                self._cancel_order(oid, now)
        self._advance_flatten(now)
        self.put_event()

    def _account(self):
        accounts = self.risk_engine.main_engine.get_all_accounts()
        if self.account_id:
            accounts = [a for a in accounts if a.accountid == self.account_id]
        return accounts[0] if accounts else None

    # ---------- 锁定处理 ----------

    def _on_lock_event(self, ev: LockEvent) -> None:
        """处理 AccountGuard.update 产生的熔断事件(守卫内部已完成锁定,这里负责告警、撤单、平仓)。"""
        if ev.level == "HARD":
            self._alert("FATAL", "累计死线触发", ev.reason + "。撤单并平仓后停止交易,需人工解锁。")
            self._cancel_all_open_orders()
            self._begin_flatten(hard=True)
        else:
            self._alert("ERROR", "当日熔断", ev.reason + "。今日禁止新开仓。")
            self._cancel_all_open_orders()
            if self.flatten_on_daily_lock:
                self._begin_flatten(hard=False)

    def emergency_flatten(self, reason: str) -> None:
        """运行时在发现策略已异常停止(其本地止损随之失效)等情况时调用:告警、撤销开仓委托、当日禁止开仓并平仓。"""
        self._alert("FATAL", "紧急平仓", reason)
        self.guard.lock_daily(self.clock(), reason)
        self._cancel_all_open_orders()
        self._begin_flatten(hard=False)
        self._advance_flatten(self.mono())      # 不必等下一次定时事件:紧急情况立刻动作

    def _lock_daily(self, reason: str, flatten: bool) -> None:
        """外部原因(废单、撤单过多)触发的当日锁定;已锁定则什么都不做。"""
        if self.guard.lock_daily(self.clock(), reason) is None:
            return
        self._alert("ERROR", "当日禁止开仓", reason)
        self._cancel_all_open_orders()
        if flatten:
            self._begin_flatten(hard=False)

    def _cancel_all_open_orders(self) -> None:
        now = self.mono()
        for order in self.risk_engine.main_engine.get_all_active_orders():
            if order.offset not in CLOSING_OFFSETS:
                self._cancel_order(order.vt_orderid, now)

    def cancel_open_orders_on_stale_tick(self) -> None:
        """行情假死看门狗(运行时定时器)撤单的**唯一公开入口**。

        直接复用 _cancel_all_open_orders 的 offset 过滤:平仓单与 FLATTEN_REF 紧急平仓单
        永远不在撤销集合内,因而不会与紧急平仓状态机竞争——不会撤掉刚重发的平仓委托。
        """
        self._cancel_all_open_orders()

    def _cancel_order(self, vt_orderid: str, now: float) -> None:
        """撤单;撤单请求发出后超过 CANCEL_RETRY_SECONDS 仍未终结(CTP 撤单失败只有错误日志、没有终态)则允许再撤。"""
        last = self._cancel_requested.get(vt_orderid)
        if last is not None and now - last < CANCEL_RETRY_SECONDS:
            return
        main = self.risk_engine.main_engine
        order = main.get_order(vt_orderid)
        if order is None or not order.is_active():
            return
        self._cancel_requested[vt_orderid] = now
        main.cancel_order(order.create_cancel_request(), order.gateway_name)

    # ---------- 紧急平仓状态机 ----------

    @property
    def flatten_active(self) -> bool:
        """是否有未完成(且未失败)的紧急平仓在进行。"""
        return self._flatten is not None and not self._flatten["failed"]

    def _begin_flatten(self, hard: bool) -> None:
        st = self._flatten
        if st is not None and not st["failed"]:
            if hard:
                st["hard"] = True
            return
        # 没有进行中的平仓,或上一轮已经失败(卡死):任何新的平仓请求都重开一轮,不能被旧的失败状态吞掉。
        # 硬锁定标记继承下来:之前的硬锁定平仓失败后,即使后来由别的触发(如策略崩溃)平仓成功,也要通知运行时退出。
        self._flatten = {"hard": hard or bool(st and st["hard"]), "targets": None, "orders": {}, "accounted": set(),
                         "sent": 0, "sent_at": 0.0, "due": 0.0, "failed": False, "refresh_since": None,
                         "empty_since": None}

    def _oms_positions(self) -> dict:
        return {(p.vt_symbol, p.direction): p for p in self.risk_engine.main_engine.get_all_positions() if p.volume > 0}

    def _advance_flatten(self, now: float) -> None:
        st = self._flatten
        if st is None or st["failed"]:
            return
        main = self.risk_engine.main_engine
        positions = self._oms_positions()
        if not positions:
            if st["targets"] is None:
                # 一笔平仓单都没发过就看到空仓,不可信:启动早期持仓回报可能还没到达。CTP 对空账户不会推送任何持仓事件,
                # 无法用"收到事件"来确认,只能主动查询后等待足够长的时间。
                if st["empty_since"] is None:
                    st["empty_since"] = now
                    self._query_positions()
                    return
                if now - st["empty_since"] < self.position_refresh_timeout:
                    return
            self._finish_flatten(st)
            return
        st["empty_since"] = None
        if st["targets"] is None:                                 # 首次:以当时的持仓作为需要平掉的目标量
            st["targets"] = {key: p.volume for key, p in positions.items()}

        blind = undirected(positions.values())
        if blind:                                                 # 方向不明(净持仓账户)时按多/空猜方向会把仓位越平越大:不发任何平仓单,直接升级人工
            for order in main.get_all_active_orders():            # 升级后状态机不再观察:此前已挂出的平仓单必须先撤,不能留在交易所无人监督
                if order.reference == FLATTEN_REF:
                    self._cancel_order(order.vt_orderid, now)
            self._escalate_flatten("持仓方向不明(净持仓账户?),无法判定平仓方向,不自动平仓: " + describe(blind))
            return

        if any(o.reference == FLATTEN_REF for _, o in self._pending_rejects.values()):
            return                                                # 有平仓废单还没分类完,先不动作

        # 重试前必须"观察到"上一轮每笔平仓委托都已是终态,否则可能重复下单:
        # - 状态未知(没有任何回报,可能网络抖动导致委托已被接受但回报丢失):不能重发,超时后升级为人工处理;
        # - 仍在活动(状态已知):超时后撤掉,等它进入终态再决定是否重试。
        unknown, active = [], []
        for oid in st["orders"]:
            order = main.get_order(oid)
            if order is None:
                unknown.append(oid)
            elif order.is_active():
                active.append(order)
        if unknown or active:
            if now - st["sent_at"] > self.flatten_ack_timeout:
                if unknown:
                    self._escalate_flatten(f"平仓委托 {len(unknown)} 笔状态未知(超过 {self.flatten_ack_timeout:.0f}s 无回报),为防重复下单不再重发")
                else:
                    for order in active:
                        self._cancel_order(order.vt_orderid, now)
            return

        for oid, meta in st["orders"].items():                    # 全部终态:按成交量对账
            if oid in st["accounted"]:
                continue
            st["accounted"].add(oid)
            order = main.get_order(oid)
            st["targets"][meta["key"]] = max(0.0, st["targets"][meta["key"]] - order.traded)

        remaining = {key: vol for key, vol in st["targets"].items() if vol > 0}
        if not remaining:
            # 成交已覆盖目标持仓,但 OMS 持仓还没刷新(CTP 约每 2 秒才查询一次):主动查询并等待,绝不重复发平仓单
            if st["refresh_since"] is None:
                st["refresh_since"] = now
                self._query_positions()
            elif now - st["refresh_since"] > self.position_refresh_timeout:
                self._escalate_flatten(f"平仓成交已覆盖持仓,但持仓回报超过 {self.position_refresh_timeout:.0f}s 仍未清零,"
                                       "为防重复下单不再重发,请人工核对")
            return
        st["refresh_since"] = None

        # 上一轮(已失败、被丢弃的状态)可能还留着挂在交易所的平仓单:重开一轮前必须先撤掉并等它终结,否则两轮平仓单可能都成交
        leftovers = [o for o in main.get_all_active_orders() if o.reference == FLATTEN_REF and o.vt_orderid not in st["orders"]]
        if leftovers:
            for order in leftovers:
                self._cancel_order(order.vt_orderid, now)
            return

        if st["sent"] > 0 and now < st["due"]:
            return
        if st["sent"] > self.retry_policy.max_attempts:           # 首次 + 3 次重试之后仍有持仓
            self._escalate_flatten("平仓重试次数用尽仍有持仓")
            return
        st["orders"].update(self._send_close_orders(remaining, positions))
        st["sent"] += 1
        st["sent_at"] = now
        delay = self.retry_policy.delay_for(st["sent"])
        st["due"] = now + (delay if delay is not None else 0.0)

    def _query_positions(self) -> None:
        main = self.risk_engine.main_engine
        for gateway in list(main.gateways.values()):
            gateway.query_position()

    def _send_close_orders(self, remaining: dict, positions: dict) -> dict[str, dict]:
        main = self.risk_engine.main_engine
        sent: dict[str, dict] = {}
        for key, volume in remaining.items():
            pos = positions.get(key)
            if pos is None:
                continue
            contract = main.get_contract(pos.vt_symbol)
            tick = main.get_tick(pos.vt_symbol)
            last = getattr(tick, "last_price", 0.0) if tick is not None else 0.0
            if contract is None or not (last and math.isfinite(last) and last > 0):
                self._alert("ERROR", "平仓缺少行情", f"{pos.vt_symbol} 无合约或有效最新价,本轮无法平仓")
                continue
            if pos.direction == Direction.LONG:
                selling = True
            elif pos.direction == Direction.SHORT:
                selling = False
            else:                                                 # 纵深防御:调用方已拦截方向不明,这里仍绝不推导"非多即空"
                self._alert("ERROR", "平仓方向不明", f"{pos.vt_symbol} 持仓方向 {getattr(pos.direction, 'value', pos.direction)},不发平仓单")
                continue
            limit = tick.limit_down if selling else tick.limit_up
            price = limit if (limit and math.isfinite(limit) and limit > 0) else last * (0.95 if selling else 1.05)
            req = OrderRequest(
                symbol=pos.symbol, exchange=pos.exchange,
                direction=Direction.SHORT if selling else Direction.LONG,
                type=OrderType.LIMIT, volume=min(volume, pos.volume),
                price=round_to(price, contract.pricetick), offset=Offset.CLOSE,
                reference=FLATTEN_REF,
            )
            converted = main.convert_order_request(req, pos.gateway_name, False, False)
            if not converted:
                # 上海/能源中心合约:有活跃平仓单冻结了仓位时,拆分器可能返回空。不能当作"已发出"
                self._alert("ERROR", "平仓请求被拆分器拒绝", f"{pos.vt_symbol} 可用持仓不足(可能被未终结的平仓单冻结)")
                continue
            for r in converted:
                vt_orderid = self.risk_engine._send_order(r, pos.gateway_name)   # 绕过风控规则的原始通道
                if vt_orderid:
                    sent[vt_orderid] = {"key": key}
        return sent

    def _finish_flatten(self, st: dict) -> None:
        self._flatten = None
        now = self.mono()
        for order in self.risk_engine.main_engine.get_all_active_orders():     # OMS 已空但本轮平仓单还挂着:撤掉,不留给交易所兜底
            if order.reference == FLATTEN_REF:
                self._cancel_order(order.vt_orderid, now)
        self._alert("INFO", "紧急平仓完成", "账户已空仓。")
        if st["hard"] or self.guard.hard_locked:
            self._emit_hard_lock(flat=True, reason=self.guard.hard_reason)

    def _escalate_flatten(self, reason: str) -> None:
        st = self._flatten
        if st is None or st["failed"]:
            return
        st["failed"] = True
        self._alert("FATAL", "紧急平仓失败", f"{reason}。账户仍有持仓,请立即人工处理!")
        if st["hard"]:
            self._emit_hard_lock(flat=False, reason=reason)

    def _emit_hard_lock(self, flat: bool, reason: str) -> None:
        """硬锁定事件只发一次。运行时仅在 flat=True(已确认空仓)时才退出进程;flat=False 时保持进程存活并持续告警,
        因为 vn.py 的止损单是本地触发的,带着持仓杀进程会让持仓失去止损。"""
        if flat in self._hard_emitted:
            return
        self._hard_emitted.add(flat)
        self.risk_engine.event_engine.put(Event(EVENT_GUARD_HARD_LOCK, {"flat": flat, "reason": reason}))

    # ---------- 告警 ----------

    def _alert(self, level: str, title: str, message: str) -> None:
        self.risk_engine.event_engine.put(Event(EVENT_GUARD_ALERT, {"level": level, "title": title, "message": message}))
        self.risk_engine.event_engine.put(Event(EVENT_LOG, LogData(
            msg=f"[{level}] {title}:{message}", level=ERROR, gateway_name=APP_NAME)))
