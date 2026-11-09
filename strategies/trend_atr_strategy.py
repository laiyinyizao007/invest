"""双均线趋势 + ATR 移动止损(日线,单手)。

设计原则(对应"最佳实践"):
- 入场极简:快慢均线金叉做多、死叉做空,仅空仓时入场;
- 出场是重点:按 ATR 设初始止损,随持仓期极值只收紧不放松的移动止损;
- 风险预算:止损距离 × 每手吨数 > risk_budget(元)时放弃这笔交易,而不是缩小止损去迁就预算;
- 可优化的参数只有 3 个:fast_window / slow_window / atr_mult(atr_window 固定为 14)。

回测/实盘一致性:
- 信号在收盘后的 on_bar 里产生,委托在下一根 K 线成交(无前视偏差);
- 保护性止损用 stop=True 的停止单。注意 vn.py CTA 的停止单是引擎在本地按行情触发,
  **不是挂在交易所的委托**:程序断线时止损不会执行,实盘必须运行在稳定的服务器上。

日线的固有近似:同一根 K 线内高低点先后顺序未知,移动止损用当根 K 线的极值收紧、下一根生效。

两种运行方式(同一份逻辑):
- 回测:引擎直接推送日线,收盘后产生信号并立即挂单,下一根 K 线开盘成交。
- 实盘/模拟盘:引擎推送 Tick,由 BarGenerator 合成日线(夜盘并入次日)。收盘时已休市,此时下单会被交易所拒绝,
  所以**信号先存起来,等下一个交易时段的第一个 Tick 再发委托**。局限:
  * BarGenerator 的 K 线时间是该分钟的开始时间,15:00 收盘的最后一根是 14:59,日线在收到它时完成;
  * 进程在盘中启动时,当天的日线缺前半段,识别后丢弃这根不完整的日线(日志提示),避免用错误的高低价;
  * 待发信号只在内存里,进程在收盘与下一次开盘之间重启会丢失该次信号。
- 重启恢复:持仓、入场价、移动止损的内部状态在每次成交与每根日线后通过 sync_data() 保存;重启后由 CTA 引擎恢复,
  本地止损单(不会随进程保存)在恢复后的第一个 Tick 重新挂出。恢复出的持仓是否与账户一致,由运行时核对。
"""
from datetime import time

from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData, TickData, TradeData
from vnpy.trader.utility import ArrayManager, BarGenerator, ceil_to, floor_to
from vnpy_ctastrategy import CtaTemplate
from vnpy_ctastrategy.base import EngineType

from risk.account_guard import trading_day
from risk.risk_guard import DailyLossGuard, TrailingStop, stop_distance, within_budget


# 允许发出入场委托的交易时段(开始含、结束不含)。玉米/甲醇:日盘 9:00-10:15、10:30-11:30、13:30-15:00,夜盘 21:00-23:00
ENTRY_SESSIONS = (
    (time(9, 0), time(10, 15)), (time(10, 30), time(11, 30)),
    (time(13, 30), time(15, 0)), (time(21, 0), time(23, 0)),
)
# 一根完整日线的第一根分钟线应出现在这些时刻(有夜盘从 21:00 起,节后首日无夜盘从 9:00 起)
FULL_DAY_OPEN_TIMES = {time(21, 0), time(9, 0)}


class TrendAtrStrategy(CtaTemplate):
    """"""

    author = "invest"
    daily_end = time(14, 59)  # 15:00 收盘 → 最后一根分钟线的开始时间为 14:59

    fast_window = 10
    slow_window = 40
    atr_mult = 1.5
    atr_window = 14           # 固定,不参与优化
    risk_budget = 300.0       # 单笔最大亏损预算(元)
    history_vt_symbol = ""    # 实盘预热用的历史日线代码(如 CADJ.DCE);实盘交易的是具体合约,历史存在连续合约代码下
    take_profit_r = 0.0       # >0 启用固定止盈(R 倍数,如 3 即 1:3);0 = 只用移动止损
    max_daily_losses = 2      # 当日连亏熔断笔数(注意:日线策略同一天几乎不会连亏两笔,该熔断只对未来的日内策略有意义)

    atr_value = 0.0
    stop_price = 0.0
    entry_price = 0.0
    entry_dist = 0.0
    prev_pos = 0
    skipped_by_budget = 0

    parameters = [
        "fast_window", "slow_window", "atr_mult", "atr_window",
        "risk_budget", "take_profit_r", "max_daily_losses", "history_vt_symbol",
    ]
    # 这些变量会被 sync_data() 保存、重启后由引擎恢复;trail_* 映射到 TrailingStop 内部状态
    variables = [
        "atr_value", "stop_price", "entry_price", "entry_dist", "prev_pos", "skipped_by_budget",
        "trail_side", "trail_extreme", "trail_stop",
    ]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window 必须小于 slow_window")
        self.am = ArrayManager(self.slow_window + self.atr_window + 5)
        self.trail = TrailingStop()
        self.guard = DailyLossGuard(self.max_daily_losses)
        self.pending_dist = 0.0   # 本次入场信号对应的止损距离,成交后用于建立移动止损
        self._stop_armed = False  # 本地保护性止损单是否已挂出(重启后需重新挂出)
        self.pending_entry: tuple[int, float] | None = None   # 实盘:收盘后产生、待下一交易时段发出的 (方向, 止损距离)
        self.bg = BarGenerator(self.on_bar, window=1, on_window_bar=self.on_daily_bar,
                               interval=Interval.DAILY, daily_end=self.daily_end)
        self._daily_open_dt = None

    # ---- TrailingStop 内部状态的变量映射(供 sync_data 保存 / 引擎恢复) ----
    @property
    def trail_side(self) -> int:
        return self.trail.side

    @trail_side.setter
    def trail_side(self, value: int) -> None:
        self.trail.side = int(value)

    @property
    def trail_extreme(self) -> float:
        return self.trail.extreme

    @trail_extreme.setter
    def trail_extreme(self, value: float) -> None:
        self.trail.extreme = float(value)

    @property
    def trail_stop(self) -> float:
        return self.trail.stop

    @trail_stop.setter
    def trail_stop(self, value: float) -> None:
        self.trail.stop = float(value)

    def state_consistent(self) -> bool:
        """恢复出的状态是否自洽:有持仓时必须有对应方向的移动止损;空仓时不应残留止损状态。"""
        if self.pos == 0:
            return self.trail.side == 0
        return self.trail.side == (1 if self.pos > 0 else -1) and self.trail.stop > 0

    @property
    def is_live(self) -> bool:
        return bool(self.cta_engine.get_engine_type() == EngineType.LIVE)

    # ---------- 生命周期 ----------

    def on_init(self) -> None:
        # load_bar 的单位是日历天,且默认周期是 1 分钟,日线策略必须显式传 DAILY;
        # 实盘时从本地数据库读取(use_database=True),历史日线由 scripts/fetch_data.py 维护
        days = int(self.am.size * 1.7) + 10
        if self.history_vt_symbol:
            for bar in self.cta_engine.load_bar(self.history_vt_symbol, days, Interval.DAILY, self.on_bar, True):
                self.on_bar(bar)
        else:
            self.load_bar(days, Interval.DAILY, use_database=True)

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    # ---------- 行情 ----------

    def on_tick(self, tick: TickData) -> None:
        """实盘:合成 K 线,并在交易时段内发出待发的入场委托。回测引擎不会调用本方法。"""
        self.bg.update_tick(tick)
        self._ensure_protective_stop(tick)
        self._place_pending_entry(tick)

    def _ensure_protective_stop(self, tick: TickData) -> None:
        """重启后本地止损单已丢失:恢复出持仓和止损状态后,在第一个 Tick 重新挂出。

        引擎是先检查本地止损单、再调用策略的 on_tick,所以刚挂出的止损单要等下一个 Tick 才会被检查;
        重启后价格已跌破止损正是最需要立刻离场的场景,因此挂出后让引擎立刻检查一次。
        """
        if self.pos != 0 and not self._stop_armed and self.trading and self.trail.side != 0:
            self._place_protective_stop(1 if self.pos > 0 else -1, abs(self.pos))
            if self._stop_armed:
                self.cta_engine.check_stop_order(tick)

    def on_bar(self, bar: BarData) -> None:
        """日线(回测引擎直接推送)或分钟线(实盘由 on_tick 合成,再聚合成日线)。"""
        if bar.interval == Interval.DAILY:
            self.on_daily_bar(bar)
            return
        self._complete_stale_daily_bar(bar)
        if self.bg.daily_bar is None:           # 这是一根新日线的第一根分钟线
            self._daily_open_dt = bar.datetime
        self.bg.update_bar(bar)

    def _complete_stale_daily_bar(self, bar: BarData) -> None:
        """BarGenerator 只在收到 daily_end(14:59)那根分钟线时才完成日线。若当天最后一分钟没有成交,日线不会完成,
        下一交易日的数据会被静默并进同一根日线。这里发现分钟线已跨到新的交易日时,强制完成上一根。"""
        pending = self.bg.daily_bar
        if pending is None or self._daily_open_dt is None:
            return
        if trading_day(bar.datetime) == trading_day(self._daily_open_dt):
            return
        self.write_log(f"{pending.datetime:%Y-%m-%d} 日线缺少 {self.daily_end:%H:%M} 的分钟线(该分钟无成交),强制完成")
        self.bg.daily_bar = None
        pending.datetime = pending.datetime.replace(hour=0, minute=0, second=0, microsecond=0)
        self.on_daily_bar(pending)

    def on_daily_bar(self, bar: BarData) -> None:
        if self._daily_open_dt is not None and self._daily_open_dt.time() not in FULL_DAY_OPEN_TIMES:
            self.write_log(f"{bar.datetime:%Y-%m-%d} 日线从 {self._daily_open_dt:%H:%M} 才开始合成(盘中启动),"
                           "缺前半段数据,丢弃这根不完整的日线")
            return
        self._process_daily_bar(bar)
        self.sync_data()                        # 保存持仓/止损状态,进程重启后可恢复

    def _process_daily_bar(self, bar: BarData) -> None:
        self.pending_entry = None               # 上一次没发出的信号作废,以最新的日线为准
        self.cancel_all()
        self._stop_armed = False
        self.am.update_bar(bar)
        if not self.am.inited:
            return
        self.atr_value = self.am.atr(self.atr_window)
        if self.atr_value <= 0:
            return

        if self.pos == 0:
            self._try_enter(bar)
        else:
            self._manage_position(bar)

    def entry_signal(self) -> int:
        """入场信号:1 做多 / -1 做空 / 0 无。子类可重写(如随机入场对照)。"""
        fast = self.am.sma(self.fast_window, array=True)
        slow = self.am.sma(self.slow_window, array=True)
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            return 1
        if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            return -1
        return 0

    def _stop_order_price(self, side: int, stop: float) -> float:
        """止损委托价按 pricetick 取整,方向取"更紧"的一侧:多头向上、空头向下,绝不放松止损。"""
        tick = self.get_pricetick()
        return ceil_to(stop, tick) if side > 0 else floor_to(stop, tick)

    def _place_protective_stop(self, side: int, volume: float) -> None:
        """挂出保护性停止单(多头平仓用 sell,空头平仓用 cover)。"""
        price = self._stop_order_price(side, self.stop_price)
        ids = self.sell(price, volume, stop=True) if side > 0 else self.cover(price, volume, stop=True)
        if ids:
            self._stop_armed = True

    def _try_enter(self, bar: BarData) -> None:
        signal = self.entry_signal()
        if signal == 0:
            return
        if not self.guard.can_open(bar.datetime.date()):
            return

        dist = stop_distance(self.atr_value, self.atr_mult)
        if not within_budget(dist, self.get_size(), self.risk_budget):
            self.skipped_by_budget += 1
            return

        if self.is_live:                        # 收盘时已休市,不能立刻下单:存起来,下一交易时段的第一个 Tick 再发
            if self.trading:
                self.pending_entry = (signal, dist)
            return
        self._send_entry(signal, dist, bar.close_price)

    def _send_entry(self, signal: int, dist: float, ref_price: float, limit_up: float = 0.0, limit_down: float = 0.0) -> bool:
        """以"参考价 ± 1 个 ATR"的限价单保证开盘附近成交(回测中按开盘价成交)。

        价格须是 pricetick 的整数倍,否则实盘会被交易所拒单(内置风控规则也会拦下);买入向上取整、卖出向下取整。
        实盘时价格不得越过涨跌停板。
        """
        tick = self.get_pricetick()
        if signal > 0:
            price = ref_price + self.atr_value
            if limit_up:
                price = min(price, limit_up)
            ids = self.buy(ceil_to(price, tick), 1)
        else:
            price = ref_price - self.atr_value
            if limit_down:
                price = max(price, limit_down)
            ids = self.short(floor_to(price, tick), 1)
        if ids:  # 预热阶段 trading=False 不会真正下单
            self.pending_dist = dist
        return bool(ids)

    def _place_pending_entry(self, tick: TickData) -> None:
        if self.pending_entry is None or not self.trading or self.pos != 0 or not tick.last_price:
            return
        t = tick.datetime.time()
        if not any(start <= t < end for start, end in ENTRY_SESSIONS):
            return
        signal, dist = self.pending_entry
        self.pending_entry = None               # 只发一次;发不出去(被风控拦下、被拒)也不重试,等下一根日线重新判断
        self._send_entry(signal, dist, tick.last_price, tick.limit_up, tick.limit_down)

    def _manage_position(self, bar: BarData) -> None:
        dist = stop_distance(self.atr_value, self.atr_mult)
        self.stop_price = self.trail.update(bar.high_price, bar.low_price, dist)
        volume = abs(self.pos)
        tick = self.get_pricetick()

        self._place_protective_stop(1 if self.pos > 0 else -1, volume)

        # 可选固定止盈:日线无法确认盘中先后顺序,采取保守做法——
        # 收盘后发现当根 K 线触及目标价,则在下一根 K 线开盘附近离场(不假设按目标价成交)
        if self.take_profit_r > 0 and self.entry_dist > 0:
            side = 1 if self.pos > 0 else -1
            target = self.entry_price + side * self.take_profit_r * self.entry_dist
            hit = bar.high_price >= target if side > 0 else bar.low_price <= target
            if hit:
                # 离场单与入场单同理:挂在市价之外一个 ATR 处,下一根 K 线开盘附近成交,
                # 在时间顺序上先于盘中触发的保护性止损,因此不存在"同根 K 线先后顺序未知"的乐观偏差
                if side > 0:
                    self.sell(floor_to(bar.close_price - self.atr_value, tick), volume)
                else:
                    self.cover(ceil_to(bar.close_price + self.atr_value, tick), volume)

    # ---------- 成交 ----------

    def on_trade(self, trade: TradeData) -> None:
        prev, self.prev_pos = self.prev_pos, self.pos

        if prev == 0 and self.pos != 0:
            # 新开仓:建立初始止损并立即挂出保护单
            side = 1 if self.pos > 0 else -1
            self.entry_price = trade.price
            # 正常情况下 pending_dist 来自入场信号;若信号状态丢失(如发单后进程重启再成交),退回当前 ATR 重新计算
            self.entry_dist = self.pending_dist or stop_distance(self.atr_value, self.atr_mult)
            if self.entry_dist <= 0:
                self.write_log("警告:开仓成交但无法确定止损距离(无 ATR),无法建立保护性止损,请立即人工处理")
            else:
                self.stop_price = self.trail.open(side, trade.price, self.entry_dist)
                self._place_protective_stop(side, abs(self.pos))

        elif prev != 0 and self.pos == 0:
            # 平仓:登记盈亏(用于连亏熔断)并清理残留委托
            side = 1 if prev > 0 else -1
            pnl = side * (trade.price - self.entry_price) * self.get_size() * abs(prev)
            self.guard.on_trade_closed(trade.datetime.date(), pnl)
            self.trail.reset()
            self.entry_price = 0.0
            self.entry_dist = 0.0
            self.stop_price = 0.0
            self.pending_dist = 0.0                 # 清掉陈旧的止损距离,避免极端路径(如发单后重启再成交)误用
            self._stop_armed = False
            self.cancel_all()
        self.sync_data()
