"""risk/promotion_gate.py 单元测试(纯 Python)。"""
import sys
from dataclasses import replace
from pathlib import Path

import pytest


from risk.promotion_gate import (  # noqa: E402
    CandidateMetrics,
    GateThresholds,
    check_gate,
    sharpe_ci,
    sharpe_standard_error,
)

GOOD = CandidateMetrics(
    oos_sharpe=2.0, oos_years=5.0, max_drawdown_pct=-12.0, max_drawdown_yuan=-1200,
    round_trips=200, random_entry_percentile=98.0, stress_sharpe=1.4,
    data_adjusted=True, parameter_on_plateau=True, dsr=0.99, n_trials=50,
)


# ---------- 夏普的统计误差 ----------

def test_standard_error_matches_formula():
    assert sharpe_standard_error(1.5, 3) == pytest.approx(((1 + 0.5 * 1.5**2) / 3) ** 0.5)
    assert sharpe_standard_error(1.5, 3) == pytest.approx(0.8416, abs=1e-3)


def test_shorter_sample_means_larger_error():
    assert sharpe_standard_error(1.0, 1) > sharpe_standard_error(1.0, 10)


def test_three_year_sharpe_1_5_confidence_interval_includes_zero():
    """文档中的例子:3 年样本外夏普 1.5,95% 区间下界 < 0,不能排除没有优势。"""
    low, high = sharpe_ci(1.5, 3)
    assert low < 0 < high
    assert low == pytest.approx(-0.15, abs=0.02)


def test_standard_error_rejects_non_positive_years():
    with pytest.raises(ValueError):
        sharpe_standard_error(1.0, 0)


# ---------- 门槛 ----------

def test_good_candidate_passes():
    assert check_gate(GOOD) == []


@pytest.mark.parametrize(
    "change, keyword",
    [
        ({"oos_sharpe": 1.2}, "样本外夏普"),
        ({"max_drawdown_pct": -25.0}, "最大回撤"),
        ({"max_drawdown_yuan": -2500}, "元"),
        ({"round_trips": 80}, "往返"),
        ({"random_entry_percentile": 80.0}, "随机入场"),
        ({"stress_sharpe": 0.4}, "成本翻倍"),
        ({"data_adjusted": False}, "未复权"),
        ({"parameter_on_plateau": False}, "参数高原"),
        ({"dsr": 0.80}, "DSR"),
    ],
)
def test_each_criterion_fails_independently(change, keyword):
    fails = check_gate(replace(GOOD, **change))
    assert any(keyword in f for f in fails), fails


def test_suspiciously_high_sharpe_is_rejected_not_celebrated():
    fails = check_gate(replace(GOOD, oos_sharpe=3.5))
    assert any("前视偏差" in f for f in fails)


def test_short_sample_fails_on_confidence_interval_even_with_high_point_estimate():
    fails = check_gate(replace(GOOD, oos_sharpe=1.6, oos_years=2.0))
    assert any("置信区间" in f for f in fails)


def test_drawdown_sign_does_not_matter():
    assert check_gate(replace(GOOD, max_drawdown_pct=12.0, max_drawdown_yuan=1200)) == []


def test_non_positive_years_is_reported():
    fails = check_gate(replace(GOOD, oos_years=0))
    assert any("年数" in f for f in fails)


def test_thresholds_are_configurable():
    loose = GateThresholds(min_round_trips=50)
    assert check_gate(replace(GOOD, round_trips=80), loose) == []


def test_dsr_threshold_is_locked_at_0_95():
    assert GateThresholds().min_dsr == 0.95


def test_dsr_boundary_is_inclusive():
    assert check_gate(replace(GOOD, dsr=0.95)) == []
    assert any("DSR" in f for f in check_gate(replace(GOOD, dsr=0.9499)))


def test_missing_dsr_fails_closed():
    """没有算 DSR(例如旧格式的指标文件)不能放行。"""
    fails = check_gate(replace(GOOD, dsr=None))
    assert any("DSR" in f and "未计算" in f for f in fails)


def test_dsr_without_trial_count_fails_closed():
    """DSR 必须带着试验次数:n_trials 缺失(0)意味着无法核对去通胀是否到位。"""
    fails = check_gate(replace(GOOD, n_trials=0))
    assert any("试验次数" in f for f in fails)


def test_dsr_failure_message_names_trials():
    fails = check_gate(replace(GOOD, dsr=0.4, n_trials=300))
    assert any("300" in f for f in fails)


def test_all_failures_are_reported_together():
    bad = CandidateMetrics(0.5, 1.0, -40.0, -4000, 10, 50.0, 0.0, False, False)
    assert len(check_gate(bad)) >= 7


# ---------- check_gate 对审计字段严格 fail-closed ----------

@pytest.mark.parametrize("bad_dsr", [float("nan"), float("inf"), -0.1, 1.5, True])
def test_non_finite_or_out_of_range_dsr_fails_closed(bad_dsr):
    fails = check_gate(replace(GOOD, dsr=bad_dsr))
    assert any("DSR" in f for f in fails), bad_dsr


@pytest.mark.parametrize("bad_n", [True, 1.5, float("nan"), -3, 0, "50"])
def test_invalid_trial_counts_fail_closed(bad_n):
    fails = check_gate(replace(GOOD, n_trials=bad_n))
    assert any("试验次数" in f for f in fails), bad_n
