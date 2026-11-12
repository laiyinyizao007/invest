"""候选评估:滚动验证段聚合 -> CandidateMetrics -> check_gate(纯函数,与回测引擎解耦)。"""
import math

import pytest

from risk import candidate_eval as ce
from risk.promotion_gate import check_gate


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
    pooled = {"sharpe": 2.0, "years": 4.0, "round_trips": 200, "max_drawdown_pct": -5.0, "max_drawdown_yuan": -500.0}

    result = ce.evaluate(pooled, stress_sharpe=1.5, random_sharpes=[0.0] * 100, data_adjusted=True, on_plateau=True)

    assert result["passed"] is True and result["failures"] == []
    assert check_gate(ce.build_metrics(pooled, 1.5, [0.0] * 100, True, True)) == []


def test_unadjusted_data_never_passes():
    pooled = {"sharpe": 2.0, "years": 4.0, "round_trips": 200, "max_drawdown_pct": -5.0, "max_drawdown_yuan": -500.0}

    result = ce.evaluate(pooled, 1.5, [0.0] * 100, data_adjusted=False, on_plateau=True)

    assert result["passed"] is False and any("复权" in f for f in result["failures"])
