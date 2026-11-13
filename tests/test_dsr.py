"""risk/dsr.py:Deflated Sharpe Ratio(Bailey & López de Prado 2014)。"""
import math
import random

import pytest

from risk.dsr import deflated_sharpe, expected_max_sharpe, moments


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
