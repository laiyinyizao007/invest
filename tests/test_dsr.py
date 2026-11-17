"""risk/dsr.py:Deflated Sharpe Ratio(Bailey & López de Prado 2014)。"""
import math
import random

import pytest

from risk.dsr import (deflated_sharpe, expected_max_sharpe, moments,
                      required_sharpe_for_dsr)


def noise(n, seed, mean=0.0, sd=0.01):
    rng = random.Random(seed)
    return [rng.gauss(mean, sd) for _ in range(n)]


def test_expected_max_sharpe_known_value():
    # T=2 => V=1/(T-1)=1;N=10:(1-γ)Φ⁻¹(0.9)+γΦ⁻¹(1-1/(10e)) ≈ 1.575
    assert expected_max_sharpe(10, 2) == pytest.approx(1.575, abs=0.01)


def test_expected_max_sharpe_single_trial_is_zero():
    assert expected_max_sharpe(1, 500) == 0.0


def test_expected_max_sharpe_grows_with_trials_and_shrinks_with_samples():
    assert expected_max_sharpe(1000, 500) > expected_max_sharpe(10, 500) > expected_max_sharpe(2, 500)
    assert expected_max_sharpe(100, 100) > expected_max_sharpe(100, 1000)


@pytest.mark.parametrize("n,t", [(0, 500), (-1, 500), (10, 1), (10, 0)])
def test_expected_max_sharpe_rejects_invalid(n, t):
    with pytest.raises(ValueError):
        expected_max_sharpe(n, t)


def test_moments_of_symmetric_series():
    mean, sd, skew, kurt = moments([1.0, -1.0] * 50)
    assert mean == pytest.approx(0.0, abs=1e-12)
    assert skew == pytest.approx(0.0, abs=1e-9)
    assert kurt == pytest.approx(1.0, abs=0.05)       # 两点分布的峰度(非超额)为 1


def test_dsr_decreases_monotonically_with_trials():
    rets = noise(720, seed=1, mean=0.0008)
    values = [deflated_sharpe(rets, n) for n in (1, 5, 50, 500, 5000)]
    assert values == sorted(values, reverse=True)
    assert values[0] > values[-1]


def test_dsr_in_unit_interval():
    for n in (1, 100, 100000):
        assert 0.0 <= deflated_sharpe(noise(500, 2, mean=0.001), n) <= 1.0


def test_strong_edge_survives_moderate_trials():
    assert deflated_sharpe(noise(960, 3, mean=0.0025), 50) > 0.95


def test_pure_noise_does_not_survive_many_trials():
    assert deflated_sharpe(noise(720, 4, mean=0.0), 500) < 0.95


def test_negative_edge_is_low():
    assert deflated_sharpe(noise(720, 5, mean=-0.001), 1) < 0.5


@pytest.mark.parametrize("rets", [[], [0.01], [0.01, 0.01, 0.01, 0.01], [0.0] * 50])
def test_degenerate_series_returns_zero(rets):
    assert deflated_sharpe(rets, 10) == 0.0


def test_matches_manual_formula():
    rets = noise(300, seed=6, mean=0.001)
    mean, sd, skew, kurt = moments(rets)
    sr = mean / sd
    n = 20
    sr0 = expected_max_sharpe(n, len(rets))
    z = (sr - sr0) * math.sqrt(len(rets) - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr**2)
    expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
    assert deflated_sharpe(rets, n) == pytest.approx(expected, rel=1e-9)


def test_invalid_trials_rejected():
    with pytest.raises(ValueError):
        deflated_sharpe(noise(100, 7), 0)


# ---------- 非有限输入必须 fail-closed(NaN 比较恒为 False,会绕过 `dsr < 阈值`) ----------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_returns_give_zero_not_nan(bad):
    rets = noise(200, 8, mean=0.002)
    rets[50] = bad
    assert deflated_sharpe(rets, 10) == 0.0


def test_overflowing_moments_give_zero_not_nan():
    rets = [1e200, -1e200, 1e200, -1e200, 3e200]
    assert deflated_sharpe(rets, 10) == 0.0


# ---------- 第 55 轮之十六:认证线(DSR 恰好等于 min_dsr 的那个日夏普) ----------
#
# 认证线是晋级门槛里「样本外夏普高得可疑」上限的**来源** ⇒ 它算错,
# 门槛就跟着算错 ⇒ 下面每一条都在钉它的数学与它的 fail-closed 边界。

def test_the_certification_line_solves_the_dsr_equation():
    """认证线回代 DSR 公式 ⇒ 恰好等于阈值(正态矩假设:偏度 0、峰度 3)。"""
    from statistics import NormalDist
    for n_trials, n_obs in ((1, 240), (10, 320), (50, 320), (1000, 600), (5000, 480)):
        sr = required_sharpe_for_dsr(n_trials, n_obs, 0.95)
        denom = math.sqrt(1.0 + sr * sr / 2.0)
        z = (sr - expected_max_sharpe(n_trials, n_obs)) * math.sqrt(n_obs - 1) / denom
        assert NormalDist().cdf(z) == pytest.approx(0.95, abs=1e-9), (n_trials, n_obs)


def test_the_certification_line_is_above_the_luck_ceiling():
    """认证线必须高于「运气基准」SR0——否则 DSR 判据自相矛盾。"""
    for n_trials, n_obs in ((1, 240), (10, 320), (50, 320), (1000, 600)):
        assert required_sharpe_for_dsr(n_trials, n_obs, 0.95) > expected_max_sharpe(n_trials, n_obs)


def test_the_certification_line_grows_with_trials():
    """试验次数越多 ⇒ 认证线越高(与去通胀的方向一致)。"""
    assert required_sharpe_for_dsr(5000, 600) > required_sharpe_for_dsr(1000, 600) > required_sharpe_for_dsr(50, 600)


@pytest.mark.parametrize("bad", [0, -1, True, 1.5, "50"])
def test_the_certification_line_rejects_invalid_trial_counts(bad):
    assert required_sharpe_for_dsr(bad, 600) == 0.0


@pytest.mark.parametrize("bad", [0, 1, 2, -5, True, 1.5, "600"])
def test_the_certification_line_rejects_invalid_sample_lengths(bad):
    assert required_sharpe_for_dsr(50, bad) == 0.0


@pytest.mark.parametrize("bad", [0.0, 1.0, 1.5, -0.1, float("nan"), float("inf"), True, "0.95"])
def test_the_certification_line_rejects_invalid_thresholds(bad):
    assert required_sharpe_for_dsr(50, 600, bad) == 0.0


def test_the_certification_line_fails_closed_when_the_equation_has_no_root():
    """方程无实根(A ≤ 0)⇒ 返回 0.0(调用方必须理解为「用兜底下限」)。"""
    assert required_sharpe_for_dsr(1, 3, 0.01) == 0.0


def test_the_certification_line_fails_closed_when_the_root_is_not_positive():
    """解出来的夏普不是正的 ⇒ 返回 0.0(不得拿它去放宽任何东西)。"""
    assert required_sharpe_for_dsr(1, 3, 0.1) == 0.0


def test_the_certification_line_survives_a_broken_derivation(monkeypatch):
    """推导内部抛异常 ⇒ 返回 0.0(fail-closed),不是把异常甩给调用方。"""
    import risk.dsr as dsr

    def boom(*args, **kwargs):
        raise ValueError("boom")

    monkeypatch.setattr(dsr, "expected_max_sharpe", boom)
    assert required_sharpe_for_dsr(50, 600) == 0.0

# ---------- 第 55 轮之十九之补:退化输入必须 fail-closed,不许把异常抛出去 ----------

def test_moments_rejects_a_sample_shorter_than_two():
    """样本少于 2 个观测 ⇒ 明确报错(而不是算出 NaN 矩)。"""
    with pytest.raises(ValueError):
        moments([0.01])


def test_an_absurd_return_series_fails_closed_instead_of_raising():
    """荒谬数值(1e200)⇒ 矩计算溢出 ⇒ 必须返回 0.0,不许把异常抛给门槛。

    门槛是「决策的最后一道」:它自己抛异常 = 整个批次中断(而不是把这条毙掉),
    所以这里钉的是「异常不许冒出去」。
    """
    assert deflated_sharpe([1e200, -1e200, 1e200], 50) == 0.0
