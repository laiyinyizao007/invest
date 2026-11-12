"""门槛评估编排:用假的回测执行器验证窗口切片、成本翻倍、随机对照与 fail-closed 行为。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import research_gate as rg


def bar(ts):
    return SimpleNamespace(datetime=ts)


W1 = ("W1", datetime(2022, 7, 1), datetime(2022, 12, 31, 23, 59))
W2 = ("W2", datetime(2023, 1, 1), datetime(2023, 6, 30, 23, 59))
BARS = [bar(datetime(2022, 7, 1, 9, 0)), bar(datetime(2022, 8, 1, 9, 0)), bar(datetime(2023, 2, 1, 9, 0)),
        bar(datetime(2023, 3, 1, 9, 0)), bar(datetime(2023, 4, 1, 9, 0))]


class Cand:
    parameters = ["fast_window", "atr_mult"]


class Rand:
    parameters = ["fast_window", "atr_mult", "entry_prob", "seed"]


def make_runner(candidate_pnl=3.0, random_pnl=-1.0, trips=100, calls=None):
    def runner(strategy_class, setting, bars, cost_multiplier):
        if calls is not None:
            calls.append((strategy_class.__name__ if hasattr(strategy_class, "__name__") else strategy_class, dict(setting), len(bars), cost_multiplier))
        is_random = strategy_class is Rand
        pnl = random_pnl if is_random else (candidate_pnl / cost_multiplier)
        # 每日盈亏带一点起伏,避免方差为 0
        return {"daily_pnls": [pnl + (i % 3) for i in range(120)], "round_trips": trips, "maxdd_pct": -3.0, "maxdd_yuan": -300.0}
    return runner


def run(**kw):
    args = dict(candidate_cls=Cand, params={"fast_window": 10, "atr_mult": 1.5}, windows=[W1, W2], bars=BARS,
                runner=make_runner(), random_cls=Rand, random_runs=5, data_adjusted=True, on_plateau=True, capital=10_000)
    args.update(kw)
    return rg.run_gate(**args)


def test_windows_slice_bars_and_never_pass_bars_past_test_start():
    calls = []
    run(runner=make_runner(calls=calls))

    first_window_calls = [c for c in calls if c[1].get("fast_window") == 10 and c[3] == 1.0 and c[0] == "Cand"]
    assert [c[2] for c in first_window_calls] == [2, 3]                  # W1 两根、W2 三根


def test_test_segment_bars_are_rejected_loudly():
    leaked = BARS + [bar(datetime(2024, 1, 2, 9, 0))]

    with pytest.raises(ValueError, match="测试段"):
        run(bars=leaked, windows=[("W9", datetime(2023, 7, 1), datetime(2024, 3, 1))])


def test_stress_run_uses_double_cost_and_random_runs_are_parameterised():
    calls = []
    result = run(runner=make_runner(calls=calls), random_runs=7)

    assert {c[3] for c in calls} == {1.0, 2.0}
    random_calls = [c for c in calls if c[0] == "Rand"]
    assert len(random_calls) == 7 * 2                                    # 7 个种子 × 2 个窗口
    assert sorted({c[1]["seed"] for c in random_calls}) == list(range(7))
    assert result["metrics"]["random_runs"] == 7


def test_random_control_matches_candidate_activity_and_keeps_candidate_exit_params():
    calls = []
    run(runner=make_runner(calls=calls, trips=40))

    rc = [c for c in calls if c[0] == "Rand"][0]
    assert rc[1]["atr_mult"] == 1.5 and "fast_window" in rc[1]
    assert rc[1]["entry_prob"] == rg.entry_prob(trips=40, n_bars=rc[2])  # 与候选在该窗口的活跃度对齐


def test_entry_prob_is_clamped_to_a_probability():
    assert rg.entry_prob(trips=0, n_bars=1000) == rg.MIN_ENTRY_PROB
    assert rg.entry_prob(trips=10_000, n_bars=100) == rg.MAX_ENTRY_PROB
    assert rg.entry_prob(trips=50, n_bars=1000) == pytest.approx(0.05)


def test_good_candidate_vs_bad_random_can_pass_percentile_but_gate_still_checks_everything():
    result = run(runner=make_runner(candidate_pnl=3.0, random_pnl=-1.0, trips=100))

    assert result["metrics"]["random_entry_percentile"] == 100.0
    assert result["passed"] is False                                      # 往返 200 够,但年数/置信区间等仍由门槛判断
    assert isinstance(result["failures"], list)


def test_without_plateau_evidence_fails_closed():
    result = run(on_plateau=False)

    assert any("参数高原" in f for f in result["failures"])


def test_output_includes_windows_and_diagnosis_and_is_json_serialisable():
    import json
    result = run()

    assert [w["name"] for w in result["windows"]] == ["W1", "W2"]
    assert "dimensions" in result["diagnosis"]
    json.dumps(result, ensure_ascii=False)


def test_test_segment_start_is_locked_to_walk_forward_constant():
    import walk_forward_optimizer as wf
    assert rg.TEST_SEGMENT_START == wf.TEST_SEGMENT_START


def test_random_entry_15m_strategy_exposes_seed_and_entry_prob_parameters():
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    assert {"entry_prob", "seed"} <= set(RandomEntry15mStrategy.parameters)
    assert issubclass(RandomEntry15mStrategy, TrendAtr15mStrategy)
