"""门槛评估编排:用假的回测执行器验证窗口切片、成本翻倍、随机对照与 fail-closed 行为。"""
from datetime import datetime
from types import SimpleNamespace

import importlib.util

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


# 这两条用例要 import 交易栈(walk_forward_optimizer / strategies.*)才能核对常量:
# CI 的研究侧闸门不装交易环境,那里**跳过**而不是失败(缺依赖 ≠ 行为不对)。
def _trading_stack_missing() -> bool:
    try:
        return importlib.util.find_spec("vnpy_ctastrategy") is None
    except (ImportError, ValueError):
        return True


requires_trading_stack = pytest.mark.skipif(
    _trading_stack_missing(),
    reason="需要交易栈:CI 的研究侧闸门不装交易环境")


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


@requires_trading_stack
def test_test_segment_start_is_locked_to_walk_forward_constant():
    import walk_forward_optimizer as wf
    assert rg.TEST_SEGMENT_START == wf.TEST_SEGMENT_START


@requires_trading_stack
def test_random_entry_15m_strategy_exposes_seed_and_entry_prob_parameters():
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    assert {"entry_prob", "seed"} <= set(RandomEntry15mStrategy.parameters)
    assert issubclass(RandomEntry15mStrategy, TrendAtr15mStrategy)


# ---------- 选参 + 门槛评估(候选声明 param_grid) ----------

SELECT = (datetime(2020, 7, 22), datetime(2022, 6, 30, 23, 59))
EVAL_BARS = [bar(datetime(2021, 3, 1, 9, 0)), bar(datetime(2021, 9, 1, 9, 0)), bar(datetime(2022, 3, 1, 9, 0)),
             bar(datetime(2022, 8, 1, 9, 0)), bar(datetime(2023, 2, 1, 9, 0)), bar(datetime(2023, 3, 1, 9, 0))]


class GridCand:
    parameters = ["a", "b", "c"]
    param_grid = {"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]}


def evaluate(**kw):
    args = dict(candidate_cls=GridCand, bars=EVAL_BARS, runner=make_runner(), random_cls=Rand, random_runs=3, capital=10_000,
                max_cells=100, min_round_trips=10, select_window=SELECT, validation_windows=[W1, W2])
    args.update(kw)
    return rg.run_evaluation(**args)


def test_evaluation_selects_on_the_early_window_then_gates_on_validation_windows_only():
    calls = []
    result = evaluate(runner=make_runner(calls=calls))

    select_calls = [c for c in calls if c[0] == "GridCand" and c[3] == 1.0 and set(c[1]) == {"a", "b", "c"} and c[2] == 3]
    assert len(select_calls) >= 27                                         # 3x3x3 网格都在选参窗口(3 根 K 线)上跑过
    gate_candidate_calls = [c for c in calls if c[0] == "GridCand" and c[2] == 2 and c[1] == result["params"]]    # W2 有 2 根 K 线
    assert gate_candidate_calls, "门槛评估必须用选出的参数"
    assert result["selection"]["window"] == ["2020-07-22", "2022-06-30"]
    assert result["kind_detail"] == "selection+gate"


def test_evaluation_without_valid_params_fails_closed_and_skips_the_gate():
    calls = []
    result = evaluate(runner=make_runner(calls=calls, trips=3))            # 往返不足 -> 无有效格子

    assert result["passed"] is False and result["params"] is None
    assert any("无法选参" in f for f in result["failures"])
    assert all(c[3] == 1.0 for c in calls) and not any(c[0] == "Rand" for c in calls)      # 没有跑成本翻倍与随机对照


def test_evaluation_passes_the_plateau_verdict_into_the_gate():
    result = evaluate()

    assert result["selection"]["on_plateau"] is True
    assert not any("参数高原" in f for f in result["failures"])            # 平坦得分 -> 在高原上,不应再因此失败


def test_evaluation_requires_a_declared_grid():
    class NoGrid:
        parameters = ["a"]

    with pytest.raises(rg.GridError, match="param_grid"):
        evaluate(candidate_cls=NoGrid)


def test_evaluation_result_trims_the_score_table_but_keeps_the_verdict():
    import json
    result = evaluate()

    assert "scores" not in result["selection"].get("plateau", {})
    assert result["selection"]["n_cells"] == 27
    json.dumps(result, ensure_ascii=False)


# ---------- 试验记账 / DSR ----------

def test_gate_without_prior_trials_fails_closed_on_dsr():
    result = run()

    assert result["metrics"]["dsr"] is None and result["metrics"]["n_trials"] == 0
    assert any("DSR" in f for f in result["failures"])
    assert result["passed"] is False


def test_gate_uses_prior_trials_as_the_deflation_count():
    result = run(prior_trials=40)

    assert result["metrics"]["n_trials"] == 40
    assert result["metrics"]["dsr"] is not None


def test_gate_more_prior_trials_never_raise_dsr():
    low = run(prior_trials=2)["metrics"]["dsr"]
    high = run(prior_trials=2000)["metrics"]["dsr"]
    assert high <= low


def test_evaluation_counts_this_runs_grid_cells_on_top_of_prior_trials():
    result = evaluate(prior_trials=10)

    assert result["metrics"]["n_trials"] == 10 + 27                       # 3x3x3 = 27 个格子
    assert result["trials"] == {"prior": 10, "grid_cells": 27, "total": 37}


def test_evaluation_without_prior_trials_fails_closed():
    result = evaluate()

    assert result["metrics"]["dsr"] is None
    assert any("DSR" in f for f in result["failures"])


def test_selection_only_failure_still_reports_the_cells_it_consumed():
    """选不出参数也消耗了试验:调用方必须据此记账,否则失败的进化轮次会被免费送出。"""
    result = evaluate(runner=make_runner(trips=3), prior_trials=10)

    assert result["passed"] is False
    assert result["trials"] == {"prior": 10, "grid_cells": 27, "total": 37}
