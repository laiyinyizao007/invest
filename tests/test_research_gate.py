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
    """没有声明 param_grid 是**声明期**的问题:结果里带阶段=声明期,原文可见。"""
    class NoGrid:
        parameters = ["a"]

    result = evaluate(candidate_cls=NoGrid)
    assert result["error"]["phase"] == "declaration"
    assert "param_grid" in result["error"]["message"]


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
    assert {k: result["trials"][k] for k in ("prior", "grid_cells", "total")} == {"prior": 10, "grid_cells": 27, "total": 37}
    assert len(result["trials"]["cell_sharpes"]) <= 27          # 只有往返笔数够的有效格子入样本


def test_evaluation_without_prior_trials_fails_closed():
    result = evaluate()

    assert result["metrics"]["dsr"] is None
    assert any("DSR" in f for f in result["failures"])


def test_selection_only_failure_still_reports_the_cells_it_consumed():
    """选不出参数也消耗了试验:调用方必须据此记账,否则失败的进化轮次会被免费送出。"""
    result = evaluate(runner=make_runner(trips=3), prior_trials=10)

    assert result["passed"] is False
    assert {k: result["trials"][k] for k in ("prior", "grid_cells", "total")} == {"prior": 10, "grid_cells": 27, "total": 37}
    assert len(result["trials"]["cell_sharpes"]) <= 27          # 只有往返笔数够的有效格子入样本


# ---------- 阶段 3:DSR 的经验方差(V = max(V_经验, 1/(T-1)),只能更严) ----------

def _wide_variances(n=6, level=0.006):
    """偏大的各次评估格子夏普方差(明显大于这套假数据的理论值 1/(T-1) ≈ 0.0042,但又不至于让 DSR 下溢成 0)。"""
    return [level * (1 + 0.1 * i) for i in range(n)]


def test_gate_without_prior_history_reports_insufficient_samples():
    """没有账本样本时 DSR 退回理论值——这件事要写进结果,不得静默。"""
    result = run(prior_trials=300)
    assert result["dsr_variance"]["basis"] == "insufficient_samples"
    assert result["dsr_variance"]["n_evaluations"] == 0


def test_gate_with_a_dispersed_history_is_stricter_and_says_so():
    # 温和的候选(日夏普约 0.12):DSR 不会被饱和在 1.0,才看得出 V 变大之后的差别
    mild = make_runner(candidate_pnl=-0.9)
    loose = run(prior_trials=300, runner=mild)["metrics"]["dsr"]
    result = run(prior_trials=300, runner=mild, run_variances=_wide_variances())
    assert result["dsr_variance"]["basis"] == "empirical"
    assert 0.0 < result["metrics"]["dsr"] < loose < 1.0


def test_gate_with_too_few_prior_evaluations_falls_back_to_theory_and_equals_the_old_dsr():
    result = run(prior_trials=300, run_variances=_wide_variances(n=2))
    assert result["dsr_variance"]["basis"] == "insufficient_samples"
    assert result["metrics"]["dsr"] == run(prior_trials=300)["metrics"]["dsr"]


def test_evaluation_folds_this_runs_own_variance_into_the_history():
    """本次评估自己扫过的格子也是一次评估:它的方差并入先验之后再取中位数。"""
    result = evaluate(prior_trials=10, run_variances=_wide_variances(n=2))           # 先验 2 次 + 本次 = 3 次
    assert result["dsr_variance"]["n_evaluations"] == 3
    assert result["dsr_variance"]["basis"] in ("empirical", "theory")


def test_evaluation_with_a_history_never_has_a_higher_dsr_than_without():
    base = evaluate(prior_trials=10)["metrics"]["dsr"]
    with_history = evaluate(prior_trials=10, run_variances=_wide_variances())["metrics"]["dsr"]
    assert with_history <= base + 1e-12


def test_one_poisoned_evaluation_in_the_history_does_not_zero_the_dsr():
    """第 15 轮 codex 的 Critical:一次网格极端分裂的评估(方差 0.279)混进历史,不得让此后每个候选的 DSR 恒为 0。"""
    mild = make_runner(candidate_pnl=-0.9)
    honest = [0.0004, 0.0005, 0.0003, 0.0006, 0.0004]
    clean = run(prior_trials=300, runner=mild, run_variances=honest)["metrics"]["dsr"]
    poisoned = run(prior_trials=300, runner=mild, run_variances=honest + [0.279])["metrics"]["dsr"]
    assert clean > 0.0 and poisoned == pytest.approx(clean, rel=0.2)


def test_cell_sharpes_are_daily_not_annualized():
    """入账本的是日夏普(= 年化 / √240),与 DSR 同一口径;否则 V 会被放大 240 倍而把一切卡死。"""
    import math

    import research_grid as rgrid
    from risk import candidate_eval as ce
    mild = make_runner(candidate_pnl=-0.9)
    result = evaluate(prior_trials=10, runner=mild)
    selection = rgrid.select_params(candidate_cls=GridCand, grid=GridCand.param_grid, bars=EVAL_BARS, runner=mild,
                                    capital=10_000, max_cells=100, min_round_trips=10, window=SELECT)
    expected = [s / math.sqrt(ce.TRADING_DAYS_PER_YEAR) for s in selection["cell_sharpes"]]
    assert result["trials"]["cell_sharpes"] == pytest.approx(expected)
    assert expected and all(abs(s) < 1.0 for s in expected)


def test_extreme_cell_sharpes_are_clamped_so_one_outlier_cannot_void_the_whole_batch():
    """真实网格里偶有极端格子(几笔全盈利)夏普极大:限幅到上限,整批样本才不会被父进程当成越界丢掉。"""
    from risk.dsr import MAX_TRIAL_SHARPE
    result = evaluate(prior_trials=10)                                   # 这个假执行器的夏普极大(日夏普远超 1)
    sharpes = result["trials"]["cell_sharpes"]
    assert sharpes and all(abs(s) <= MAX_TRIAL_SHARPE for s in sharpes)
    assert max(sharpes) == MAX_TRIAL_SHARPE


# ---------- 阶段 3:热力图随评估一起产出,但不进摘要 ----------

def test_the_evaluation_carries_a_valid_heatmap_for_the_dashboard():
    from risk.heatmap import validate_heatmap
    result = evaluate(prior_trials=10)
    validate_heatmap(result["heatmap"])
    assert result["heatmap"]["source"] == "selection"
    assert len(result["heatmap"]["scores"]) == 27


def test_the_full_score_table_stays_out_of_the_selection_summary():
    """摘要是给外层看的:完整得分表只在 heatmap 里(父进程会把它拿出来单独落盘)。"""
    result = evaluate(prior_trials=10)
    assert "plateau" not in result["selection"] and "cell_sharpes" not in result["selection"]
    assert "scores" not in result["selection"]


def test_a_selection_only_failure_still_carries_the_heatmap():
    result = evaluate(runner=make_runner(trips=3), prior_trials=10)
    assert result["kind_detail"] == "selection-only" and result["heatmap"]["on_plateau"] is False
    assert all(row[3] == "-inf" for row in result["heatmap"]["scores"])        # 往返笔数不够:每个格子都无效


# ---------- 阶段 3:选参窗口的行为描述符、格子、结构化诊断、适应度 ----------

def _runner_with_behavior(hold=10, **kw):
    base = make_runner(**kw)

    def runner(strategy_class, setting, bars, cost_multiplier):
        out = base(strategy_class, setting, bars, cost_multiplier)
        out["hold_bars"] = [hold] * out["round_trips"]
        out["trade_pnls"] = [3.0, -1.0, 2.0, -2.0] * (out["round_trips"] // 4)
        return out
    return runner


def test_the_evaluation_reports_the_cell_from_the_selection_window_behavior():
    from risk.behavior import ALL_CELLS
    result = evaluate(prior_trials=10, runner=_runner_with_behavior(hold=10))
    assert result["cell"] in ALL_CELLS
    assert result["behavior"]["avg_hold_bars"] == 10.0 and result["behavior"]["round_trips"] == 100
    assert result["behavior"]["n_bars"] == 3                                     # 选参窗口里的 K 线根数,不是验证窗口的
    assert result["cell"] == "H0F3"                                              # 平均持仓 10 根 ≤ 16;100 笔/3 根 K 线 ≥ 4/100 根


def test_a_different_holding_period_lands_in_a_different_cell():
    short = evaluate(prior_trials=10, runner=_runner_with_behavior(hold=5))["cell"]
    long = evaluate(prior_trials=10, runner=_runner_with_behavior(hold=300))["cell"]
    assert short.startswith("H0") and long.startswith("H3") and short[2:] == long[2:]


def test_without_holding_information_the_cell_is_none_rather_than_a_guess():
    """旧执行器/假执行器不回传持仓根数:不猜格子,但适应度与结构化诊断照常给出。"""
    result = evaluate(prior_trials=10)
    assert result["cell"] is None and result["behavior"]["avg_hold_bars"] is None
    assert isinstance(result["fitness"], float)


def test_the_selection_diagnosis_is_structured_and_contains_no_free_text():
    import json

    from risk.structured_diagnosis import validate
    result = evaluate(prior_trials=10, runner=_runner_with_behavior())
    assert validate(result["selection_diagnosis"])
    text = json.dumps(result["selection_diagnosis"], ensure_ascii=False)
    assert not any("一" <= ch <= "鿿" for ch in text) and "note" not in text


def test_the_fitness_is_the_selection_window_smoothed_sharpe_not_a_validation_number():
    result = evaluate(prior_trials=10, runner=_runner_with_behavior())
    assert result["fitness"] == result["selection"]["best_smoothed"]


def test_a_selection_failure_has_no_cell_and_no_fitness():
    result = evaluate(runner=make_runner(trips=3), prior_trials=10)
    assert result["kind_detail"] == "selection-only"
    assert result["cell"] is None and result["fitness"] is None and result["selection_diagnosis"] is None


def test_the_best_cell_detail_stays_out_of_the_selection_summary():
    """摘要是给外层看的:最佳格子的逐笔盈亏等明细只在引擎一侧用,不进摘要。"""
    result = evaluate(prior_trials=10, runner=_runner_with_behavior())
    assert "best_detail" not in result["selection"]


# ---------- 阶段 3(第 15 轮 codex 的 Critical):报错带"阶段"标记,验证窗口回放里的报错原文不外传 ----------

def _raising_runner(where, message="close=2728.0"):
    """在指定的窗口里抛错的执行器:`select` 在选参窗口(3 根 K 线)里抛,`validate` 在验证窗口里抛。"""
    base = make_runner()

    def runner(strategy_class, setting, bars, cost_multiplier):
        in_select = len(bars) == 3 and strategy_class.__name__ == "GridCand"
        if (where == "select" and in_select) or (where == "validate" and not in_select):
            raise ValueError(message)
        return base(strategy_class, setting, bars, cost_multiplier)
    return runner


def test_an_error_in_the_selection_window_is_reported_as_selection():
    """选参窗口里的错:阶段=选参期(进化回路放开的那一层)。"""
    result = evaluate(prior_trials=10, runner=_raising_runner("select"))
    assert result["error"]["phase"] == "selection"
    assert result["error"]["type"] == "ValueError"


def test_an_error_while_replaying_the_validation_windows_is_reported_as_validation():
    """验证窗口里的错:阶段=验证期(原文不外放)。"""
    result = evaluate(prior_trials=10, runner=_raising_runner("validate"))
    assert result["error"]["phase"] == "validation"


def test_a_grid_declaration_error_is_reported_as_declaration():
    class NoGrid:
        parameters = ["a", "b", "c"]

    result = rg.run_evaluation(candidate_cls=NoGrid, bars=EVAL_BARS, runner=make_runner(), random_cls=Rand, random_runs=3,
                               capital=10_000, max_cells=100, min_round_trips=10, select_window=SELECT,
                               validation_windows=[W1, W2], prior_trials=1)
    assert result["error"]["phase"] == "declaration"


def test_run_gate_alone_reports_its_errors_as_validation_because_its_whole_run_is_the_validation_window():
    result = run(runner=_raising_runner("validate"), prior_trials=5)
    assert result["error"]["phase"] == "validation"


def test_the_innermost_phase_wins():
    """从 run_gate 里抛出的错是验证期,不会被外层的 run_evaluation 改标成选参期。"""
    result = evaluate(prior_trials=10, runner=_raising_runner("validate"))
    assert result["error"]["phase"] == "validation"


def test_reporting_the_phase_does_not_change_the_error_type_or_message():
    result = evaluate(prior_trials=10, runner=_raising_runner("validate"))
    assert result["error"]["type"] == "ValueError"
    assert "close=2728.0" in result["error"]["message"]


def test_a_candidate_cannot_forge_the_phase_by_tagging_its_own_exception():
    """**伪造阶段**无效:候选给异常挂 `invest_phase="selection"`,阶段仍由框架决定。

    旧协议把阶段挂在异常对象上,而打标记的逻辑"只补不改",候选自己挂的标记会被保留——
    于是验证窗口的报错原文被当成选参期的消息放出去,而梯子一次都没扣
    (第 22 轮 codex 的 High,实测消息里带出了验证窗口的收盘价)。"""
    base = make_runner()

    def runner(strategy_class, setting, bars, cost_multiplier):
        if len(bars) != 3 and strategy_class.__name__ == "GridCand":
            exc = ValueError("close=2728.0")
            exc.invest_phase = "selection"              # 候选伪造:把自己说成选参期
            raise exc
        return base(strategy_class, setting, bars, cost_multiplier)

    result = evaluate(prior_trials=10, runner=runner)
    assert result["error"]["phase"] == "validation"    # 框架说了算,不是候选
    assert result["error"]["message"].startswith("close=")


def test_the_framework_own_invariant_failures_are_still_raised_loudly():
    """框架自己的错(窗口混进测试段 K 线)照旧抛异常——那不是候选的问题,不能被吞成"候选失败了"。"""
    leaked = BARS + [bar(datetime(2024, 1, 2, 9, 0))]
    with pytest.raises(ValueError, match="测试段"):
        run(bars=leaked, windows=[("W9", datetime(2023, 7, 1), datetime(2024, 3, 1))])
