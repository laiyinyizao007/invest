"""账户级风控规则(挂在 vnpy_riskmanager 的发单检查链上)。

职责:
1. check_allowed:账户被锁定时拦截**新开仓**;平仓永远放行。
2. on_timer(每秒):读取账户权益 → 双层熔断;识别并撤销超时的开仓委托;推进紧急平仓状态机;延迟分类废单。
3. on_order:统计废单。**CTP 先推废单回报、后写错误日志**,所以不能在回报到达时立刻分类,
   而是延迟 reject_classify_delay 秒,在前后 3 秒窗口里取最近的错误日志再分类;未知按硬拒绝。
   硬拒绝或废单风暴 → 当日禁止开仓。开仓单永不自动重试。
4. 单日撤单次数达到 cancel_lock_limit → 当日禁止开仓(内置规则的撤单上限是兜底,这里更早触发)。

紧急平仓走 risk_engine._send_order(绕过风控规则的原始发单通道):平仓只会降低风险,不能被限流拦住。
紧急平仓状态机的关键约束(防止重复下单):
- 按**成交量**对账,而不是反复读取 OMS 持仓:CTP 的持仓回报约每 2 秒才刷新一次,成交后 OMS 里仍是旧持仓;
- 上一轮每笔平仓委托都被观察到处于终态后才可能重试(最多 3 次,间隔 1/3/5 秒);
- 状态未知(无回报)绝不重发,超时升级人工;成交量已覆盖持仓但持仓回报迟迟不清零,同样不重发,主动查询持仓并升级人工。
"""
import math
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from vnpy.event import Event
from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.logger import ERROR
from vnpy.trader.object import LogData, OrderData, OrderRequest
from vnpy.trader.utility import round_to
from vnpy_riskmanager.base import APP_NAME
from vnpy_riskmanager.template import RuleTemplate

from risk.account_guard import AccountGuard, AccountLimits, LockEvent, trading_day
from risk.order_policy import RejectKind, RejectMonitor, RetryPolicy, classify_reject, find_stale_orders
from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLOSING_OFFSETS = {Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY}
ERROR_WINDOW = 3.0                      # 废单与其错误日志的最大时间差(秒,前后均可)
MIN_POSITION_REFRESH_TIMEOUT = 3.0     # 等持仓回报的下限(秒)
CANCEL_RETRY_SECONDS = 10.0             # 撤单请求发出后多久仍未终结则允许再次撤单
FLATTEN_REF = "account_guard_flatten"   # 紧急平仓委托的标记,废单处理据此识别,不依赖事件到达顺序


class AccountGuardRule(RuleTemplate):
    """账户风控规则"""

    name: str = "账户风控"

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
        self._errors: deque[tuple[float, str]] = deque(maxlen=50)
        self._pending_rejects: dict[str, tuple[float, OrderData]] = {}
        self._flatten: dict | None = None
        self._hard_emitted: set[bool] = set()    # 已发出的硬锁定事件(flat=False / flat=True 各最多一次)
        self._save_error_alerted = False

        self.risk_engine.event_engine.register(EVENT_LOG, self._on_log)

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

    # ---------- 发单检查 ----------

    def check_allowed(self, req: OrderRequest, gateway_name: str) -> bool:
        if req.offset in CLOSING_OFFSETS:
            return True                                           # 平仓永远放行
        today = trading_day(self.clock())
        if not self.guard.can_open(today):
            self.write_log(f"账户处于{self.guard.status(today)},禁止新开仓:{req}")
            return False
        return True

    # ---------- 事件 ----------

    def on_order(self, order: OrderData) -> None:
        now = self.mono()
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

    def _on_log(self, event: Event) -> None:
        log: LogData = event.data
        # 只接受已注册网关(如 CTP)的错误日志;运行时/引擎自身的 ERROR 日志(如"通知发送异常")不能被当成废单原因
        if log.level >= ERROR and log.gateway_name in self.risk_engine.main_engine.gateways:
            self._errors.append((self.mono(), log.msg))

    def _error_text_near(self, ts: float) -> str | None:
        """取与废单时刻最接近(前后 ERROR_WINDOW 秒内)的网关错误日志。"""
        best = min(self._errors, key=lambda e: abs(e[0] - ts), default=None)
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
            self._handle_reject(order, self._error_text_near(t), t)

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
            for ev in self.guard.update(account.balance, now_dt):
                self._on_lock_event(ev)
            self.peak_equity, self.day_start_equity = self.guard.peak_equity, self.guard.day_start_equity
        if self.guard.save_error and not self._save_error_alerted:
            self._save_error_alerted = True
            self._alert("ERROR", "风控状态写盘失败", f"{self.guard.save_error}。熔断仍在内存中生效,但重启后可能丢失,请检查磁盘")
        self.lock_status = self.guard.status(trading_day(now_dt))

        self._process_pending_rejects(now)
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
            selling = pos.direction == Direction.LONG
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
