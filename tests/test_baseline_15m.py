"""15 分钟基准:K 线回放封装、策略、优化器的方案约束。全部用合成数据。"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


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


def test_exception_detection_uses_exact_sentinels():
    """只用精确哨兵:含"异常"二字的普通日志不能误报,真正的 traceback 必须报出来。"""
    from backtest_lib import _raise_if_strategy_failed

    _raise_if_strategy_failed(["数据异常提示(仅日志)"])          # 不抛
    _raise_if_strategy_failed(["回放进度：100%"])                # 不抛
    with pytest.raises(RuntimeError):
        _raise_if_strategy_failed(["触发异常，回测终止"])
    with pytest.raises(RuntimeError):
        _raise_if_strategy_failed(["Traceback (most recent call last): ..."])


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
    assert o.MIN_FILLS == 100
    assert o.valid_score({"sharpe": 1.0, "fills": o.MIN_FILLS - 1}) == float("-inf")
    assert o.valid_score({"sharpe": float("nan"), "fills": 500}) == float("-inf")
    assert o.valid_score({"sharpe": 0.7, "fills": o.MIN_FILLS}) == 0.7


def test_bar_cache_is_keyed_by_the_data_file(monkeypatch, tmp_path):
    """换 --data 必须重新读盘,不能继续用上一份 K 线。"""
    calls = []

    def fake_load(path, symbol, exchange):
        calls.append(path)
        return []

    monkeypatch.setattr(o, "load_bars_csv", fake_load)
    o.all_bars(tmp_path / "a.csv")
    o.all_bars(tmp_path / "a.csv")
    assert calls == [tmp_path / "a.csv"], "同一路径应命中缓存"
    o.all_bars(tmp_path / "b.csv")
    assert calls == [tmp_path / "a.csv", tmp_path / "b.csv"], "换了 --data 必须重新读盘"


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


def test_plateau_file_is_written_from_training_scores_only(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(o.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(o, "grid", lambda: [(i, j, k, o.FAST[i], o.SLOW[j], o.MULT[k]) for i in range(5) for j in range(6) for k in range(5)
                                           if o.SLOW[j] >= 2 * o.FAST[i]])
    windows = []

    def fake_eval(params, window, cost_multiplier=1.0):
        windows.append(window)
        return {"return_pct": 1.0, "maxdd_pct": -1.0, "maxdd_yuan": -100.0, "sharpe": 1.0, "round_trips": 200,
                "fills": 400, "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 50.0}

    monkeypatch.setattr(o, "evaluate", fake_eval)
    out = tmp_path / "plateau.json"

    assert o.main(["--plateau-out", str(out)]) == 0

    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data["cell"]) == 3 and data["scores"]
    assert all(len(row) == 4 for row in data["scores"])
    assert "test" not in windows


def test_plateau_file_roundtrips_through_the_gate_loader(monkeypatch, tmp_path):
    import research_api as api
    monkeypatch.setattr(o.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(o, "grid", lambda: [(i, j, k, o.FAST[i], o.SLOW[j], o.MULT[k]) for i in range(5) for j in range(6) for k in range(5)
                                           if o.SLOW[j] >= 2 * o.FAST[i]])
    monkeypatch.setattr(o, "evaluate", lambda params, window, cost_multiplier=1.0: {
        "return_pct": 1.0, "maxdd_pct": -1.0, "maxdd_yuan": -100.0, "sharpe": 1.0, "round_trips": 200,
        "fills": 400, "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 50.0})
    out = tmp_path / "plateau.json"
    o.main(["--plateau-out", str(out)])

    ok, note = api.load_plateau(str(out))

    assert ok is True and "plateau-file" in note                        # 全平面均为正得分的网格,选中格必在高原上


def test_invalid_cells_are_serialised_so_the_loader_counts_them_at_the_floor():
    import json
    text = o.plateau_json({(0, 0, 0): 1.0, (0, 0, 1): float("-inf")}, (0, 0, 0))

    rows = json.loads(text)["scores"]
    assert [0, 0, 1, "-inf"] in rows and [0, 0, 0, 1.0] in rows


def _stub_grid_and_eval(monkeypatch, tmp_path, windows_seen):
    monkeypatch.setattr(o.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(o, "grid", lambda: [(i, j, k, o.FAST[i], o.SLOW[j], o.MULT[k]) for i in range(5) for j in range(6) for k in range(5)
                                           if o.SLOW[j] >= 2 * o.FAST[i]])

    def fake_eval(params, window, cost_multiplier=1.0):
        windows_seen.append(window)
        return {"return_pct": 1.0, "maxdd_pct": -1.0, "maxdd_yuan": -100.0, "sharpe": 1.0, "round_trips": 200,
                "fills": 400, "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 50.0}

    monkeypatch.setattr(o, "evaluate", fake_eval)


def test_select_only_touches_only_the_early_selection_window(monkeypatch, tmp_path, capsys):
    seen = []
    _stub_grid_and_eval(monkeypatch, tmp_path, seen)
    out = tmp_path / "p.json"

    assert o.main(["--select-only", "--plateau-out", str(out)]) == 0

    assert set(seen) == {"select"}                       # 既不看 train(到 2022-12)也不看 valid/test
    assert out.exists() and "select-only" in capsys.readouterr().out


def test_select_only_cannot_be_combined_with_test(monkeypatch, tmp_path, capsys):
    seen = []
    _stub_grid_and_eval(monkeypatch, tmp_path, seen)

    assert o.main(["--select-only", "--test"]) == 2 and seen == []


def test_select_window_ends_exactly_where_the_first_walk_forward_validation_begins():
    import walk_forward_optimizer as wf
    first = wf.build_windows()[0]

    assert o.WINDOWS["select"][0] == first.train_start
    assert o.WINDOWS["select"][1] == first.train_end           # 选参数据止于 W1 训练段末,W1~W3 验证窗口保持样本外
    assert o.WINDOWS["select"][1] < first.valid_start


def test_strategy_failure_message_carries_the_traceback_that_vnpy_prints_next():
    from backtest_lib import _raise_if_strategy_failed
    messages = ["回放进度", "触发异常，回测终止", "Traceback (most recent call last):" + chr(10) + "  File x" + chr(10) + "ValueError: too many values to unpack (expected 3)"]

    with pytest.raises(RuntimeError, match="too many values to unpack"):
        _raise_if_strategy_failed(messages)


# ---------- 不可变宪法的运行时第三层:回测途中被改写要 fail-closed ----------

def test_backtest_fails_closed_when_the_candidate_tamfers_with_the_contract_size():
    """候选在回测途中改小合约乘数 → 止损距离算出来的风险金额接近 0。

    静态层看不见这种改写(属性是运行期才被赋值的,类字典里根本没有它),
    所以回测结束时必须核对一次:合约乘数必须还是回测设置给的那个。
    第 7 轮 codex 实测:不核对的话,回撤从 -4272 元/-39.97% 缩成 -0 元/-0.00%,
    promotion_gate 的 2000 元与 20% 两道门槛同时失效,而三层校验全绿。"""
    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            self.cta_engine.size = 0.001
            return super().entry_signal()

    with pytest.raises(RuntimeError, match="风控底线"):
        run_engine_bars(Evil, SETTING, KEY, synthetic_bars(60, drift=0.6))


def test_backtest_fails_closed_when_the_candidate_tampers_with_the_loss_breaker():
    """熔断器阈值与策略用的阈值不一致 → 当日连亏熔断被架空。"""
    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            self.guard.max_losses = 999
            return super().entry_signal()

    with pytest.raises(RuntimeError, match="风控底线"):
        run_engine_bars(Evil, SETTING, KEY, synthetic_bars(60, drift=0.6))


def test_backtest_fails_closed_when_the_candidate_rewrites_a_framework_object():
    """候选在回测途中把**框架对象**换掉 → 整个风控模块被架空。

    这是第 9 轮 codex 实测的 Critical 1 的收口:模块级 monkey-patch 不是实例状态,
    运行期第三层(实例快照)看不见,要靠第四层(受保护对象的身份指纹)。"""
    import risk.risk_guard as rg

    original = rg.risk_per_trade

    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            rg.risk_per_trade = lambda stop_dist, size, volume=1: 0.0
            return super().entry_signal()

    try:
        with pytest.raises(RuntimeError, match="风控底线"):
            run_engine_bars(Evil, SETTING, KEY, synthetic_bars(60, drift=0.6))
    finally:
        rg.risk_per_trade = original


def test_an_untouched_strategy_passes_the_runtime_floor_check():
    """反向用例:没动过底线的策略必须照常跑完(别把正常研究也拦了)。"""
    engine = run_engine_bars(TrendAtr15mStrategy, SETTING, KEY, synthetic_bars(60, drift=0.6))
    assert engine.strategy is not None
