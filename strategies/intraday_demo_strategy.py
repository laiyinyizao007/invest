"""日内短线演示策略(仅研究/回测):平台能力面的"日内"形态示例,不是基准策略。

继承 15 分钟基准 TrendAtr15mStrategy 的双均线 + ATR 移动止损骨架,加上两条日内约束:
- **尾盘强制平仓**:14:30 那根 15 分钟线收盘(14:45)发出限价平仓委托,下一根开盘(14:45)成交,
  目标是 15:00 收盘前离场。限价单在反向跳空时可能不成交——此时**不再重发被动限价单**,
  而是转入基准逻辑:更新指标、收紧移动止损、重新挂出保护性停止单(尾盘极值只收紧不放松,
  夜盘继续有效),由止损接管离场——离场可能发生在夜盘。这是"尽力平仓 + 止损兜底"的如实语义,
  不假装保证 15:00 前必成交(回测引擎只撮合限价单,跳空日无法模拟市价单);
- **只在日盘白天开新仓**:夜盘与尾盘(14:30 之后开始的 K 线)一律不开仓,避免刚开就平的空转。

定位与纪律:
- 目的是把"日内短线"作为平台具备的能力形态落到一个真实可回测的示例上,不是策略准入对象;
  任何策略仍必须先赢过 TrendAtr15mStrategy 零分线(见 docs/adr/20261110-baseline-15m.md);
- 与基准一样只吃 15 分钟线、不支持实盘(on_tick 抛 RuntimeError),防误用;
- 默认参数与基准相同,除 flat_time 外没有新增可优化参数。
"""
from datetime import time

from vnpy.trader.object import BarData
from vnpy.trader.utility import ceil_to, floor_to

from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

NIGHT_START = time(21, 0)   # 夜盘第一根 15 分钟线的开始时刻(判断"还没到夜盘"用)
DAY_OPEN = time(9, 0)       # 日盘第一根 K 线的开始时刻


class IntradayDemoStrategy(TrendAtr15mStrategy):
    """"""

    author = "invest"
    flat_time = time(14, 30)  # 这根 K 线收盘(14:45)发出平仓委托,下一根开盘成交,力争 15:00 收盘前离场

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self._flat_pending = False  # 尾盘强平单已发出但尚未确认成交(仅回测内存状态,该策略不支持实盘)

    def _try_enter(self, bar: BarData) -> None:
        t = bar.datetime.time()
        if t < DAY_OPEN or t >= self.flat_time:  # 夜盘与尾盘不开新仓
            return
        super()._try_enter(bar)

    def _process_daily_bar(self, bar: BarData) -> None:
        if self.pos == 0:
            self._flat_pending = False            # 已离场(强平成交或止损触发),恢复正常的尾盘强平资格
        t = bar.datetime.time()
        if self.pos != 0 and self.flat_time <= t < NIGHT_START:
            if not self._flat_pending:
                self._flat_pending = True
                self.pending_entry = None
                self.cancel_all()                 # 先撤保护性停止单,避免与平仓单重复执行
                self._stop_armed = False
                volume = abs(self.pos)
                tick = self.get_pricetick()
                if self.pos > 0:
                    self.sell(floor_to(bar.close_price, tick), volume)
                else:
                    self.cover(ceil_to(bar.close_price, tick), volume)
                self.write_log(f"日内策略尾盘强平({t:%H:%M} K 线收盘发出,下一根开盘成交)")
                return
            # 强平单未成交(反向跳空):不再重发被动限价单,转入基准逻辑——
            # 更新指标/移动止损并重新挂出保护性停止单(极值只收紧不放松,夜盘继续有效),由止损接管离场
            self._flat_pending = False
        super()._process_daily_bar(bar)
