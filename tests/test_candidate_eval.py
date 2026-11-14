"""候选评估:滚动验证段聚合 -> CandidateMetrics -> check_gate(纯函数,与回测引擎解耦)。"""
import math
import random

import pytest

from risk import candidate_eval as ce
from risk.promotion_gate import check_gate


def edge_returns(n=960, seed=3, mean=0.0025, sd=0.01):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def good_pooled():
    return {"sharpe": 2.0, "years": 4.0, "round_trips": 200, "max_drawdown_pct": -5.0, "max_drawdown_yuan": -500.0,
            "daily_returns": edge_returns()}


def window(pnls, round_trips=60, dd_pct=-3.0, dd_yuan=-300.0):
    return {"daily_pnls": pnls, "round_trips": round_trips, "maxdd_pct": dd_pct, "maxdd_yuan": dd_yuan}


def test_annualized_sharpe_matches_manual_formula():
    pnls = [10.0, -5.0, 20.0, 0.0, 5.0]
    rets = [p / 10_000 for p in pnls]
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))

    assert ce.annualized_sharpe(pnls, 10_000) == pytest.approx(mean / std * math.sqrt(240))


def test_annualized_sharpe_degenerate_series_is_zero_not_nan():
    assert ce.annualized_sharpe([], 10_000) == 0.0
    assert ce.annualized_sharpe([5.0], 10_000) == 0.0
    assert ce.annualized_sharpe([5.0, 5.0, 5.0], 10_000) == 0.0


def test_pool_oos_concatenates_windows_and_takes_worst_drawdown():
    w1 = window([1.0] * 120, round_trips=50, dd_pct=-2.0, dd_yuan=-200.0)
    w2 = window([-1.0] * 120, round_trips=60, dd_pct=-6.0, dd_yuan=-600.0)

    pooled = ce.pool_oos([w1, w2], capital=10_000)

    assert pooled["round_trips"] == 110
    assert pooled["years"] == pytest.approx(240 / 240)            # 240 个交易日 / 240 = 1 年
    assert pooled["max_drawdown_pct"] == -6.0 and pooled["max_drawdown_yuan"] == -600.0   # 取最差,保守
    assert pooled["sharpe"] == pytest.approx(ce.annualized_sharpe([1.0] * 120 + [-1.0] * 120, 10_000))
    assert pooled["daily_returns"] == [1.0 / 10_000] * 120 + [-1.0 / 10_000] * 120   # DSR 需要日收益序列


def test_pool_oos_requires_windows():
    with pytest.raises(ValueError):
        ce.pool_oos([], capital=10_000)


@pytest.mark.parametrize("value, samples, expected", [
    (1.0, [0.0, 0.5, 2.0, 3.0], 50.0),
    (5.0, [0.0, 1.0], 100.0),
    (-1.0, [0.0, 1.0], 0.0),
    (1.0, [1.0, 1.0], 0.0),              # 平手不算超过
])
def test_percentile_rank_counts_strictly_below(value, samples, expected):
    assert ce.percentile_rank(value, samples) == expected


def test_percentile_rank_without_samples_is_zero_so_gate_cannot_pass_by_accident():
    assert ce.percentile_rank(9.0, []) == 0.0


def test_random_seeds_are_fixed_and_parameterised():
    assert list(ce.random_seeds(5)) == [0, 1, 2, 3, 4]
    assert len(list(ce.random_seeds())) == ce.DEFAULT_RANDOM_RUNS == 200
    with pytest.raises(ValueError):
        ce.random_seeds(0)


def test_evaluate_reports_gate_failures_without_loosening_anything():
    pooled = ce.pool_oos([window([2.0, -1.0, 3.0] * 40, round_trips=40)], capital=10_000)

    result = ce.evaluate(pooled, stress_sharpe=0.2, random_sharpes=[500.0] * 10, data_adjusted=True, on_plateau=False)

    assert result["passed"] is False
    assert any("往返" in f for f in result["failures"])
    assert any("随机入场" in f for f in result["failures"])
    assert any("参数高原" in f for f in result["failures"])
    assert result["metrics"]["round_trips"] == 40


def test_evaluate_passes_only_when_every_gate_item_is_met():
    # 人工构造一条各项都达标的序列,验证组装没有丢字段
    pooled = good_pooled()

    result = ce.evaluate(pooled, stress_sharpe=1.5, random_sharpes=[0.0] * 100, data_adjusted=True, on_plateau=True,
                         n_trials=50)

    assert result["passed"] is True and result["failures"] == []
    assert result["metrics"]["n_trials"] == 50 and result["metrics"]["dsr"] > 0.95
    assert check_gate(ce.build_metrics(pooled, 1.5, [0.0] * 100, True, True, n_trials=50)) == []


def test_unadjusted_data_never_passes():
    result = ce.evaluate(good_pooled(), 1.5, [0.0] * 100, data_adjusted=False, on_plateau=True, n_trials=50)

    assert result["passed"] is False and any("复权" in f for f in result["failures"])


# ---------- DSR 接线 ----------

def test_more_trials_lower_dsr_and_can_fail_the_gate():
    pooled = good_pooled() | {"daily_returns": edge_returns(mean=0.0012)}      # 优势适中:试验一多就扛不住
    few = ce.build_metrics(pooled, 1.5, [0.0] * 100, True, True, n_trials=5)
    many = ce.build_metrics(pooled, 1.5, [0.0] * 100, True, True, n_trials=5_000_000)
    assert many.dsr < few.dsr
    assert any("DSR" in f for f in check_gate(many))


def test_missing_trial_count_fails_closed():
    metrics = ce.build_metrics(good_pooled(), 1.5, [0.0] * 100, True, True)           # n_trials 未提供
    assert metrics.dsr is None and metrics.n_trials == 0
    assert any("DSR" in f for f in check_gate(metrics))


def test_pooled_without_daily_returns_fails_closed():
    pooled = {k: v for k, v in good_pooled().items() if k != "daily_returns"}
    metrics = ce.build_metrics(pooled, 1.5, [0.0] * 100, True, True, n_trials=50)
    assert metrics.dsr is None
    assert any("DSR" in f for f in check_gate(metrics))


def test_invalid_trial_count_is_rejected():
    with pytest.raises(ValueError):
        ce.build_metrics(good_pooled(), 1.5, [0.0] * 100, True, True, n_trials=0)
