"""日内演示策略:尾盘强平成交路径、夜盘/尾盘禁开仓、反向跳空时止损接管。全部用合成数据。"""
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
from vnpy.trader.constant import Exchange, Interval, Offset
from vnpy.trader.object import BarData

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backtest_lib import run_backtest_bars, run_engine_bars  # noqa: E402
from strategies.intraday_demo_strategy import IntradayDemoStrategy  # noqa: E402

KEY = "C15"
SETTING = {"fast_window": 5, "slow_window": 20, "atr_mult": 2.0}


def synthetic_day_bars(n_days=10, drift=0.6, seed=3, start=datetime(2024, 3, 4)):
    """每交易日日盘(09:00-11:15、13:30-14:45)与夜盘(21:00-22:45)的 15 分钟线,带漂移随机游走。"""
    rng = np.random.default_rng(seed)
    price, bars = 2000.0, []
    day = start
    for _ in range(n_days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for base_h, base_m, count in [(9, 0, 9), (13, 30, 6), (21, 0, 8)]:
            for i in range(count):
                o_ = round(price)
                price += drift + rng.normal(0, 2.0)
                c = round(price)
                hi, lo = max(o_, c) + int(abs(rng.normal(0, 1))), min(o_, c) - int(abs(rng.normal(0, 1)))
                dt = day.replace(hour=base_h, minute=base_m) + timedelta(minutes=15 * i)
                bars.append(BarData(symbol="CADJ15", exchange=Exchange.DCE, datetime=dt, interval=Interval.MINUTE,
                                    open_price=o_, high_price=hi, low_price=lo, close_price=c,
                                    volume=100.0, turnover=0.0, open_interest=1000.0, gateway_name="TEST"))
        day += timedelta(days=1)
    return bars


class AlwaysLong(IntradayDemoStrategy):
    """测试替身:预热完成后永远看多,让"每天开仓 → 尾盘强平"的循环确定性发生。"""

    def entry_signal(self) -> int:
        return 1 if self.am.inited else 0


def test_flatten_fills_before_the_day_close_and_no_position_survives_the_day():
    engine = run_engine_bars(AlwaysLong, SETTING, KEY, synthetic_day_bars())
    trades = engine.get_all_trades()
    assert trades, "强趋势下应当有成交"
    assert engine.strategy.pos == 0                                    # 收盘后不持仓
    # 成对配对:每笔开仓在同一交易日、15:00 之前被平掉(日盘强平成交的正常路径)
    opens = [t for t in trades if t.offset == Offset.OPEN]
    closes = [t for t in trades if t.offset == Offset.CLOSE]
    assert len(opens) == len(closes) > 0
    for o_, c_ in zip(opens, closes):
        assert o_.datetime.date() == c_.datetime.date(), "存在隔夜仓"
        assert c_.datetime.time() < time(15, 0), "平仓发生在 15:00 之后"
    # 至少一笔平仓发生在 14:45(尾盘强平单成交),证明强平路径真实执行过
    assert any(t.datetime.time() == time(14, 45) for t in closes), "尾盘强平单从未成交"


def test_night_session_never_opens_a_position():
    engine = run_engine_bars(AlwaysLong, SETTING, KEY, synthetic_day_bars())
    for t in engine.get_all_trades():
        assert not (time(21, 0) <= t.datetime.time() <= time(22, 45)), \
            "夜盘不应有任何成交(开仓被禁止;正常数据下强平日盘已成交,止损接管不应触发)"


def test_adverse_gap_hands_over_to_the_protective_stop():
    """尾盘强平限价单遇反向跳空不成交时:不重发被动单,保护性止损重新挂出并在夜盘接管离场。"""
    bars = synthetic_day_bars(6)
    day = bars[-1].datetime.date()
    for b in bars:
        if b.datetime.date() == day and b.datetime.time() >= time(14, 45):
            # 最后一天 14:45 起向下跳空并持续走低:14:30 收盘价发出的多头平仓限价单无法成交
            price = 1500.0 - 10 * (b.datetime.hour * 60 + b.datetime.minute - 14 * 60 - 45) / 15
            b.open_price = b.high_price = b.low_price = b.close_price = round(price)
    engine = run_engine_bars(AlwaysLong, SETTING, KEY, bars)
    trades = engine.get_all_trades()
    assert engine.strategy.pos == 0, "止损接管后最终必须离场"
    closes = [t for t in trades if t.offset == Offset.CLOSE]
    assert any(t.datetime.time() >= time(21, 0) for t in closes), \
        "强平未成交时应当由保护性止损在夜盘接管离场,而不是让持仓裸奔回夜盘之后"
    # 跳空之后不得再开新仓(夜盘/次日禁开),离场只能来自止损
    gap_opens = [t for t in trades if t.offset == Offset.OPEN and t.datetime.date() == day and t.datetime.time() > time(14, 45)]
    assert not gap_opens


def test_class_runs_on_trending_series_with_default_signal():
    """不用测试替身、用默认双均线信号也能正常回测(类本身可独立使用)。"""
    stats, _, trades = run_backtest_bars(IntradayDemoStrategy, SETTING, KEY, synthetic_day_bars(12, drift=0.8))
    assert stats["total_trade_count"] > 0
    offsets = {t.offset for t in trades}
    assert Offset.OPEN in offsets and Offset.CLOSE in offsets
