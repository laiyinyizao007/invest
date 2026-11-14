"""已登记阈值之间的冲突(第 15 轮 codex 的 C3):DSR≥0.95 与可疑夏普上限 3.0 在 T=367 时无法同时满足。

这里**不放宽任何阈值**,只把冲突变成一个会自己提醒的测试(strict xfail):
一旦裁决落地(按家族计试验次数/更长 OOS/调整可疑上限)使它可达,本测试变 XPASS 并令套件变红,提示移除 xfail。
"""
import math

import pytest

from risk.dsr import deflated_sharpe
from risk.promotion_gate import GateThresholds

T = 367
N_FIRST_ROUND = 28


def returns_with_annual_sharpe(target: float, n: int = T) -> list[float]:
    base = [(-1) ** i * 0.01 for i in range(n)]            # 均值≈0 的对称序列
    mean = sum(base) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in base) / (n - 1))
    daily = target / math.sqrt(252)
    return [daily + (x - mean) / sd for x in base]         # 样本夏普恰为 target/√252 的日夏普单位


def test_the_helper_hits_the_requested_sharpe():
    r = returns_with_annual_sharpe(2.9)
    m = sum(r) / len(r)
    sd = math.sqrt(sum((x - m) ** 2 for x in r) / (len(r) - 1))
    assert m / sd * math.sqrt(252) == pytest.approx(2.9, rel=1e-6)


def test_a_sharpe_just_under_the_suspicious_cap_does_not_clear_dsr_today():
    """事实陈述(通过):在上限之下的任何夏普都过不了 DSR——冲突的实证。"""
    cap = GateThresholds().max_suspicious_sharpe
    assert deflated_sharpe(returns_with_annual_sharpe(cap - 0.01), N_FIRST_ROUND) < 0.95


@pytest.mark.xfail(strict=True, reason="C3:DSR≥0.95 与 max_suspicious_sharpe=3.0 在 T=367、N=28 时互斥;待用户裁决,不放宽阈值")
def test_dsr_gate_is_reachable_under_registered_limits():
    cap = GateThresholds().max_suspicious_sharpe
    assert deflated_sharpe(returns_with_annual_sharpe(cap - 0.01), N_FIRST_ROUND) >= 0.95
