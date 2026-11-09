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
"""
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData, TradeData
from vnpy.trader.utility import ArrayManager, ceil_to, floor_to
from vnpy_ctastrategy import CtaTemplate

from risk.risk_guard import DailyLossGuard, TrailingStop, stop_distance, within_budget


class TrendAtrStrategy(CtaTemplate):
    """"""

    author = "invest"

    fast_window = 10
    slow_window = 40
    atr_mult = 1.5
    atr_window = 14           # 固定,不参与优化
    risk_budget = 300.0       # 单笔最大亏损预算(元)
    take_profit_r = 0.0       # >0 启用固定止盈(R 倍数,如 3 即 1:3);0 = 只用移动止损
    max_daily_losses = 2      # 当日连亏熔断笔数(注意:日线策略同一天几乎不会连亏两笔,该熔断只对未来的日内策略有意义)

    atr_value = 0.0
    stop_price = 0.0
    entry_price = 0.0
    skipped_by_budget = 0

    parameters = [
        "fast_window", "slow_window", "atr_mult", "atr_window",
        "risk_budget", "take_profit_r", "max_daily_losses",
    ]
    variables = ["atr_value", "stop_price", "entry_price", "skipped_by_budget"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window 必须小于 slow_window")
        self.am = ArrayManager(self.slow_window + self.atr_window + 5)
        self.trail = TrailingStop()
        self.guard = DailyLossGuard(self.max_daily_losses)
        self.pending_dist = 0.0   # 本次入场信号对应的止损距离,成交后用于建立移动止损
        self.entry_dist = 0.0
        self.prev_pos = 0

    # ---------- 生命周期 ----------

    def on_init(self) -> None:
        # load_bar 的单位是日历天,且默认周期是 1 分钟,日线策略必须显式传 DAILY
        self.load_bar(int(self.am.size * 1.7) + 10, Interval.DAILY)

    def on_start(self) -> None:
        pass

    def on_stop(self) -> None:
        pass

    # ---------- 行情 ----------

    def on_bar(self, bar: BarData) -> None:
        self.cancel_all()
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
        if side > 0:
            self.sell(price, volume, stop=True)
        else:
            self.cover(price, volume, stop=True)

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

        # 以"收盘价 ± 1 个 ATR"的限价单保证下一根 K 线开盘附近成交(回测中按开盘价成交)。
        # 价格须是 pricetick 的整数倍,否则实盘会被交易所拒单;买入向上取整、卖出向下取整(更容易成交)。
        tick = self.get_pricetick()
        if signal > 0:
            ids = self.buy(ceil_to(bar.close_price + self.atr_value, tick), 1)
        else:
            ids = self.short(floor_to(bar.close_price - self.atr_value, tick), 1)
        if ids:  # 预热阶段 trading=False 不会真正下单
            self.pending_dist = dist

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
            self.entry_dist = self.pending_dist
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
            self.cancel_all()
