"""双均线趋势 + ATR 移动止损的 15 分钟基准版(仅研究/回测)。

逻辑与日线版 TrendAtrStrategy 完全相同,只是把"一根日线"换成"一根 15 分钟线":
均线与 ATR 的窗口以 15 分钟线根数计;信号在一根 K 线收盘后产生,下一根开盘成交。
目的是做**零分线**:任何新想法都必须先赢过它(见 docs/adr)。

与日线版的差异(都是时间粒度带来的,不是新增逻辑):
- 当日连亏熔断(max_daily_losses)在 15 分钟下才真正起作用,日期按期货交易日(夜盘归下一交易日)计;
- 无预热:回放从窗口起点开始,前 slow_window + atr_window + 5 根 K 线只用来热身、不会交易;
- 尚不支持实盘(实盘需要 Tick 合成 15 分钟线、收盘/休市时段的待发信号等,基准没有证据之前不做)。
"""
from vnpy.trader.object import BarData, TickData

from strategies.trend_atr_strategy import TrendAtrStrategy


class TrendAtr15mStrategy(TrendAtrStrategy):
    """"""

    def on_init(self) -> None:
        pass                                    # 回放数据由引擎直接给出,不做数据库预热

    def on_tick(self, tick: TickData) -> None:
        raise RuntimeError("TrendAtr15mStrategy 仅用于研究回测,不支持实盘/模拟盘")

    def on_bar(self, bar: BarData) -> None:
        self._process_daily_bar(bar)            # 这里的"bar"就是 15 分钟线
        self.sync_data()
