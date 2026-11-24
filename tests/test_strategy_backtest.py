"""策略在合成行情上的行为测试:不读真实数据库,走势完全由测试构造,结果可手算。"""
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import backtest_lib  # noqa: E402  (内部会先 import bootstrap)
import vnpy_ctastrategy.backtesting as vt_backtesting  # noqa: E402
from strategies.instruments import INSTRUMENTS, Instrument  # noqa: E402
from strategies.random_entry_strategy import RandomEntryStrategy  # noqa: E402
from strategies.trend_atr_strategy import TrendAtrStrategy  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Interval, Offset  # noqa: E402
from vnpy.trader.object import BarData  # noqa: E402

DAY0 = datetime(2020, 1, 1)
FAST = {"fast_window": 5, "slow_window": 20, "atr_mult": 1.5}


def make_bars(closes, rng=0.5):
    """开盘 = 上一日收盘;最高/最低 = 开收极值 ± rng。"""
    bars, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        bars.append(BarData(
            symbol="C0", exchange=Exchange.DCE, interval=Interval.DAILY,
            datetime=datetime.combine((DAY0 + timedelta(days=i)).date(), time()),
            open_price=o, high_price=max(o, c) + rng, low_price=min(o, c) - rng, close_price=c,
            volume=1000, open_interest=1000, gateway_name="TEST",
        ))
        prev = c
    return bars


def up_then_crash():
    """平盘 80 天 → 上涨 60 天(+1/天)→ 暴跌 30 天(-2/天)→ 平盘 40 天。"""
    closes = [100.0] * 80
    closes += [100.0 + i for i in range(1, 61)]
    closes += [160.0 - 2 * i for i in range(1, 31)]
    closes += [100.0] * 40
    return closes


def down_then_rally():
    closes = [100.0] * 80
    closes += [100.0 - i for i in range(1, 61)]
    closes += [40.0 + 2 * i for i in range(1, 31)]
    closes += [100.0] * 40
    return closes


@pytest.fixture
def feed(monkeypatch):
    """把回测引擎的数据读取替换成内存里的合成行情(按起止时间过滤,与真实数据库行为一致)。"""
    holder = {}

    def fake_load(symbol, exchange, interval, start, end):
        return [b for b in holder["bars"] if start <= b.datetime <= end]

    monkeypatch.setattr(vt_backtesting, "load_bar_data", fake_load)

    def set_bars(bars):
        holder["bars"] = bars
        return bars

    return set_bars


def run(feed_bars, strategy=TrendAtrStrategy, setting=None, start_idx=70):
    start = feed_bars[start_idx].datetime
    end = feed_bars[-1].datetime
    return backtest_lib.run_engine(strategy, {**FAST, **(setting or {})}, "C", start, end)


def roundtrips(trades):
    out, cur = [], None
    for t in trades:
        if t.offset is Offset.OPEN:
            cur = t
        else:
            out.append((cur, t))
    return out


# ---------- 方向与进出场 ----------

def test_uptrend_opens_long_then_trailing_stop_exits_in_crash(feed):
    bars = feed(make_bars(up_then_crash()))
    engine = run(bars)
    trades = engine.get_all_trades()
    trips = roundtrips(trades)

    assert len(trips) >= 1
    first_open, first_close = trips[0]
    assert first_open.direction is Direction.LONG
    assert first_close.price > first_open.price      # 趋势中止损被移动抬高,最终盈利离场
    if len(trips) > 1 or len(trades) > 2:
        assert trades[2].direction is Direction.SHORT  # 暴跌形成死叉 → 平多之后顺势转空


def test_downtrend_opens_short(feed):
    bars = feed(make_bars(down_then_rally()))
    trades = run(bars).get_all_trades()
    trips = roundtrips(trades)

    assert trips and trips[0][0].direction is Direction.SHORT
    assert trips[0][1].price < trips[0][0].price


def test_entry_fills_at_next_bar_open_not_signal_bar(feed):
    """无前视偏差:入场成交价是信号日之后某根 K 线的开盘价,不会早于信号日。"""
    bars = feed(make_bars(up_then_crash()))
    by_day = {b.datetime.date(): b for b in bars}
    for t in run(bars).get_all_trades():
        bar = by_day[t.datetime.date()]
        assert bar.low_price <= t.price <= bar.high_price       # 成交价必须落在当日区间内
        if t.offset is Offset.OPEN:
            assert t.price == bar.open_price                    # 平滑行情下无跳空,按开盘价成交


def test_positions_alternate_open_close_never_double_up(feed):
    bars = feed(make_bars(up_then_crash() + down_then_rally()))
    trades = run(bars).get_all_trades()
    offsets = [t.offset for t in trades]
    for i, off in enumerate(offsets):
        assert off is (Offset.OPEN if i % 2 == 0 else Offset.CLOSE)       # 只会 开-平-开-平,不会重复开仓


# ---------- 风险预算 ----------

def test_budget_rule_skips_trades_when_volatility_too_high(feed):
    """波动极大时,止损距离 × 吨数超过 300 元预算 → 这笔不做,而不是缩小止损。"""
    bars = feed(make_bars(up_then_crash(), rng=50))
    engine = run(bars)
    assert engine.get_all_trades() == []
    assert engine.strategy.skipped_by_budget > 0


def test_larger_budget_allows_the_same_trades(feed):
    bars = feed(make_bars(up_then_crash(), rng=50))
    engine = run(bars, setting={"risk_budget": 1e9})
    assert len(engine.get_all_trades()) > 0


def test_open_position_carries_atr_trailing_stop(feed):
    """持仓中:止损价 = 持仓期最高价 - atr_mult × ATR;且低于当前价(保护性)。"""
    closes = [100.0] * 80 + [100.0 + i for i in range(1, 61)]
    engine = run(feed(make_bars(closes)))
    s = engine.strategy
    assert s.pos == 1 and s.entry_price > 0
    assert s.stop_price == pytest.approx(s.trail.extreme - FAST["atr_mult"] * s.atr_value)
    assert s.stop_price < closes[-1]


def test_state_is_cleared_after_position_closes(feed):
    """平仓后移动止损、入场价等状态必须清空,不能带到下一笔交易。"""
    closes = [100.0] * 80 + [100.0 + i for i in range(1, 61)] + [160.0 - 4 * i for i in range(1, 31)]
    closes += [40.0] * 20     # 暴跌后价格贴着底部不动,不再出现新的入场信号
    engine = run(feed(make_bars(closes)))
    s = engine.strategy
    if s.pos == 0:
        assert s.trail.side == 0 and s.entry_price == 0 and s.entry_dist == 0 and s.stop_price == 0
        assert not engine.active_stop_orders and not engine.active_limit_orders   # 无残留委托


# ---------- 价格取整 ----------

def test_all_order_prices_are_multiples_of_pricetick(feed):
    """实盘委托价必须是最小变动价位的整数倍;ATR 是小数,下单前必须取整。"""
    bars = feed(make_bars(up_then_crash() + down_then_rally()))
    engine = run(bars, setting={"risk_budget": 1e9})
    prices = [o.price for o in engine.limit_orders.values()] + [o.price for o in engine.stop_orders.values()]
    assert prices
    assert all(p == round(p) for p in prices)           # pricetick = 1


def test_protective_stop_is_rounded_to_the_tighter_side(feed):
    """多头止损向上取整、空头向下取整:取整不得放松止损。"""
    closes = [100.0] * 80 + [100.0 + i for i in range(1, 61)]
    engine = run(feed(make_bars(closes)))
    s = engine.strategy
    placed = [o for o in engine.stop_orders.values() if o.direction is Direction.SHORT]   # 多头平仓的 sell stop
    assert placed
    assert placed[-1].price >= s.stop_price             # 向上取整 → 不低于理论止损价
    assert placed[-1].price - s.stop_price < 1.0


# ---------- 固定止盈 ----------

def test_take_profit_variant_exits_flat_without_double_exit(feed):
    bars = feed(make_bars(up_then_crash()))
    engine = run(bars, setting={"take_profit_r": 2.0})
    trades = engine.get_all_trades()
    offsets = [t.offset for t in trades]
    assert offsets and all(off is (Offset.OPEN if i % 2 == 0 else Offset.CLOSE) for i, off in enumerate(offsets))
    trips = roundtrips(trades)
    assert trips[0][1].price > trips[0][0].price         # 趋势中触及目标 → 盈利离场
    by_day = {b.datetime.date(): b for b in bars}
    exit_bar = by_day[trips[0][1].datetime.date()]
    assert trips[0][1].price == exit_bar.open_price      # 止盈离场在下一根 K 线开盘成交,不假设按目标价


# ---------- 参数校验与错误处理 ----------

def test_fast_not_smaller_than_slow_is_rejected(feed):
    bars = feed(make_bars(up_then_crash()))
    with pytest.raises(ValueError):
        run(bars, setting={"fast_window": 30, "slow_window": 20})


def test_strategy_exception_is_not_silently_swallowed(feed):
    """vn.py 遇到策略异常只打印日志就返回;封装必须把它变成真正的报错。"""
    class Boom(TrendAtrStrategy):
        def on_bar(self, bar):
            if self.trading:  # 热身阶段(on_init)的异常 vn.py 会直接抛出;被吞掉的是回放阶段
                raise RuntimeError("boom")
            super().on_bar(bar)

    bars = feed(make_bars(up_then_crash()))
    with pytest.raises(RuntimeError, match="回测中策略抛出异常"):
        run(bars, strategy=Boom)


def test_strategy_log_containing_word_exception_is_not_a_false_alarm(feed):
    class Chatty(TrendAtrStrategy):
        def on_bar(self, bar):
            self.write_log("数据异常提示(仅日志,不是错误)")
            super().on_bar(bar)

    bars = feed(make_bars(up_then_crash()))
    run(bars, strategy=Chatty)       # 不应抛出


def test_missing_data_raises_clear_error(feed):
    feed([])
    with pytest.raises(RuntimeError, match="fetch_data"):
        backtest_lib.run_engine(TrendAtrStrategy, FAST, "C", DAY0, DAY0 + timedelta(days=10))


def test_run_backtest_returns_stats_and_costs(feed):
    bars = feed(make_bars(up_then_crash()))
    stats, df, trades = backtest_lib.run_backtest(
        TrendAtrStrategy, FAST, "C", bars[70].datetime, bars[-1].datetime
    )
    assert stats["total_trade_count"] == len(trades) > 0
    # 每笔成交成本 = 10 吨 × (1 tick + 3 元/10 吨) = 13 元
    assert stats["total_slippage"] == pytest.approx(len(trades) * 13.0)
    assert stats["total_commission"] == 0


# ---------- 随机入场对照 ----------

def test_random_entry_is_deterministic_per_seed(feed):
    bars = feed(make_bars(up_then_crash() + down_then_rally()))
    base = {"entry_prob": 0.1, "risk_budget": 1e9}

    def trade_days(seed):
        engine = run(bars, strategy=RandomEntryStrategy, setting={**base, "seed": seed})
        return [(t.datetime, t.direction) for t in engine.get_all_trades()]

    assert trade_days(1) == trade_days(1)
    assert trade_days(1) != trade_days(2)


# ---------- 品种参数 ----------

def test_instrument_slippage_folds_fee_into_points():
    inst = Instrument("C0.DCE", size=10, pricetick=1.0, fee_per_lot=3.0)
    assert inst.slippage_points == pytest.approx(1.3)
    assert set(INSTRUMENTS) == {"C", "MA", "CADJ", "MAADJ", "C15", "V15", "EB15", "EG15", "PP15", "M15"}
    assert INSTRUMENTS["CADJ"].vt_symbol == "CADJ.DCE" and INSTRUMENTS["MAADJ"].vt_symbol == "MAADJ.CZCE"
    assert INSTRUMENTS["MA"].vt_symbol.endswith(".CZCE")
