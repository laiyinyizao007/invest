"""15 分钟基准:K 线回放封装、策略、优化器的方案约束。全部用合成数据。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import optimize_15m as o  # noqa: E402  (内部会先 import bootstrap)
from backtest_lib import load_bars_csv, run_backtest_bars, run_engine_bars  # noqa: E402
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy  # noqa: E402

KEY = "C15"
SETTING = {"fast_window": 5, "slow_window": 20, "atr_mult": 2.0}


def synthetic_bars(n_days=30, drift=0.15, seed=1, start=datetime(2024, 3, 4)):
    """每个交易日 8 根 15 分钟线(09:00 起),带漂移的随机游走;价格取整数,保证 OHLC 自洽。"""
    rng = np.random.default_rng(seed)
    price, bars = 2000.0, []
    day = start
    for _ in range(n_days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for i in range(8):
            o_ = round(price)
            price += drift + rng.normal(0, 2.0)
            c = round(price)
            hi, lo = max(o_, c) + int(abs(rng.normal(0, 1))), min(o_, c) - int(abs(rng.normal(0, 1)))
            bars.append(BarData(symbol="CADJ15", exchange=Exchange.DCE, datetime=day.replace(hour=9) + timedelta(minutes=15 * i),
                                interval=Interval.MINUTE, open_price=o_, high_price=hi, low_price=lo, close_price=c,
                                volume=100.0, turnover=0.0, open_interest=1000.0, gateway_name="TEST"))
        day += timedelta(days=1)
    return bars


def test_strategy_trades_on_a_trending_series_and_reports_costs():
    stats, df, trades = run_backtest_bars(TrendAtr15mStrategy, SETTING, KEY, synthetic_bars(60, drift=0.6))
    assert stats["total_trade_count"] > 0 and len(trades) == stats["total_trade_count"]
    assert stats["total_commission"] + stats["total_slippage"] > 0


def test_cost_multiplier_scales_costs_but_not_the_trades():
    bars = synthetic_bars(60, drift=0.6)
    s1, _, t1 = run_backtest_bars(TrendAtr15mStrategy, SETTING, KEY, bars)
    s2, _, t2 = run_backtest_bars(TrendAtr15mStrategy, SETTING, KEY, bars, cost_multiplier=2.0)
    assert [(t.price, t.volume) for t in t1] == [(t.price, t.volume) for t in t2]
    assert s2["total_slippage"] == pytest.approx(2 * s1["total_slippage"])


def test_no_bars_is_an_error():
    with pytest.raises(RuntimeError, match="没有可回放"):
        run_engine_bars(TrendAtr15mStrategy, SETTING, KEY, [])


def test_strategy_exception_is_not_swallowed():
    class Boom(TrendAtr15mStrategy):
        def on_bar(self, bar):
            raise ValueError("boom")

    with pytest.raises(RuntimeError, match="异常"):
        run_engine_bars(Boom, SETTING, KEY, synthetic_bars(5))


def test_strategy_refuses_live_ticks():
    strategy = TrendAtr15mStrategy.__new__(TrendAtr15mStrategy)
    with pytest.raises(RuntimeError, match="仅用于研究回测"):
        strategy.on_tick(None)


def test_fast_must_be_below_slow():
    with pytest.raises(ValueError):
        run_engine_bars(TrendAtr15mStrategy, {**SETTING, "fast_window": 30}, KEY, synthetic_bars(5))


def test_budget_blocks_entries_when_the_stop_is_too_wide():
    stats, _, _ = run_backtest_bars(TrendAtr15mStrategy, {**SETTING, "atr_mult": 200.0}, KEY, synthetic_bars(60, drift=0.6))
    assert stats["total_trade_count"] == 0                                  # 止损距离 × 10 吨 > 300 元,全部放弃


def test_load_bars_csv_roundtrip(tmp_path):
    bars = synthetic_bars(3)
    pd.DataFrame({"datetime": [b.datetime for b in bars], "open": [b.open_price for b in bars], "high": [b.high_price for b in bars],
                  "low": [b.low_price for b in bars], "close": [b.close_price for b in bars], "volume": [b.volume for b in bars],
                  "open_interest": [b.open_interest for b in bars]}).to_csv(tmp_path / "b.csv", index=False)
    loaded = load_bars_csv(tmp_path / "b.csv", "CADJ15", Exchange.DCE)
    assert len(loaded) == len(bars) and loaded[0].datetime == bars[0].datetime and loaded[-1].close_price == bars[-1].close_price
    assert all(b.interval == Interval.MINUTE for b in loaded)


# ---------- 实验方案的硬约束(见 docs/adr/20261110-baseline-15m.md) ----------

def test_windows_are_ordered_non_overlapping_and_test_is_last():
    w = o.WINDOWS
    assert w["train"][1] < w["valid"][0] and w["valid"][1] < w["test"][0]
    assert w["train"][0] < w["train"][1] and w["test"][0] < w["test"][1]
    assert w["test"][1] <= datetime(2026, 6, 16)                              # 分钟线只到 2026-06-15


def test_grid_matches_the_preregistered_plan():
    combos = o.grid()
    assert all(s >= 2 * f for _, _, _, f, s, _ in combos)
    assert (o.FAST, o.SLOW, o.MULT) == ([8, 12, 16, 24, 32], [48, 72, 96, 144, 192, 288], [1.5, 2.0, 2.5, 3.0, 4.0])
    assert len(combos) == 5 * 6 * 5 - 5                                         # 只有 (32, 48) 被 slow >= 2 x fast 过滤,共 5 个倍数


def test_valid_score_requires_enough_fills_and_finite_sharpe():
    assert o.valid_score({"sharpe": 1.0, "fills": o.MIN_FILLS - 1}) == float("-inf")
    assert o.valid_score({"sharpe": float("nan"), "fills": 500}) == float("-inf")
    assert o.valid_score({"sharpe": 0.7, "fills": o.MIN_FILLS}) == 0.7


def test_test_window_is_not_evaluated_unless_requested(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(o.bootstrap, "ROOT", tmp_path)                         # 不要把假数据写进真实的结果目录
    calls = []
    monkeypatch.setattr(o, "grid", lambda: [(i, j, k, o.FAST[i], o.SLOW[j], o.MULT[k]) for i in range(5) for j in range(6) for k in range(5)
                                           if o.SLOW[j] >= 2 * o.FAST[i]])

    def fake_eval(params, window, cost_multiplier=1.0):
        calls.append(window)
        return {"return_pct": 1.0, "maxdd_pct": -1.0, "maxdd_yuan": -100.0, "sharpe": 1.0 - 0.001 * params["atr_mult"], "round_trips": 200,
                "fills": 400, "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 50.0}

    monkeypatch.setattr(o, "evaluate", fake_eval)
    assert o.main([]) == 0
    assert "test" not in calls and "valid" in calls
    assert "测试段未查看" in capsys.readouterr().out
    calls.clear()
    assert o.main(["--test"]) == 0
    assert calls.count("test") == 2                                             # 常规成本 + 成本翻倍,各一次
