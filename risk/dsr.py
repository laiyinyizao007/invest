"""Deflated Sharpe Ratio(Bailey & López de Prado, 2014):在 N 次试验中挑出的最优夏普,要扣掉"纯靠运气能挑到多高"。

纯 Python(statistics.NormalDist),不依赖 numpy/vn.py。阈值与口径见 docs/adr/20261113-evolution-engine.md:
- 一切在**日**收益口径下计算(未年化),T = 日收益个数;
- 试验间夏普方差 V 取零假设下夏普估计量的方差 1/(T-1)(偏度/峰度修正式在 SR=0 处退化为该值),不另设经验系数;
- 返回的是"真实夏普大于去通胀后基准"的概率(0~1),门槛为 ≥ 0.95。
退化输入(样本太短、零波动、修正项非正)一律返回 0.0,保证门槛不会因数据异常而被放过。
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Sequence

EULER_GAMMA = 0.5772156649015329
_NORM = NormalDist()


def moments(returns: Sequence[float]) -> tuple[float, float, float, float]:
    """(均值, 样本标准差, 偏度, 峰度)。偏度/峰度用总体矩,峰度为非超额(正态 = 3)。"""
    n = len(returns)
    if n < 2:
        raise ValueError("至少需要 2 个观测")
    mean = sum(returns) / n
    m2 = sum((r - mean) ** 2 for r in returns) / n
    sd = math.sqrt(m2 * n / (n - 1))
    if m2 <= 0:
        return mean, 0.0, 0.0, 0.0
    m3 = sum((r - mean) ** 3 for r in returns) / n
    m4 = sum((r - mean) ** 4 for r in returns) / n
    return mean, sd, m3 / m2**1.5, m4 / m2**2


def expected_max_sharpe(n_trials: int, n_obs: int) -> float:
    """N 次独立试验、零真实优势时,日夏普最大值的期望(DSR 的基准 SR0)。N=1 时无通胀,返回 0。"""
    if n_trials < 1:
        raise ValueError("n_trials 必须 ≥ 1")
    if n_obs < 2:
        raise ValueError("n_obs 必须 ≥ 2")
    if n_trials == 1:
        return 0.0
    v = 1.0 / (n_obs - 1)
    return math.sqrt(v) * ((1 - EULER_GAMMA) * _NORM.inv_cdf(1 - 1 / n_trials)
                           + EULER_GAMMA * _NORM.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(daily_returns: Sequence[float], n_trials: int) -> float:
    """样本外日收益序列的 DSR(0~1)。n_trials 必须包含被淘汰的全部试验。"""
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError("n_trials 必须是 ≥ 1 的整数")
    t = len(daily_returns)
    if t < 3:
        return 0.0
    # NaN/inf 比较恒为 False,会让 `dsr < 阈值` 放过候选,所以任何非有限的输入或中间量都按 0.0 处理(fail-closed)
    if not all(isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r) for r in daily_returns):
        return 0.0
    try:
        mean, sd, skew, kurt = moments(daily_returns)
        if not all(math.isfinite(x) for x in (mean, sd, skew, kurt)) or sd <= 0:
            return 0.0
        sr = mean / sd
        denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
        if not math.isfinite(denom) or denom <= 0:
            return 0.0
        z = (sr - expected_max_sharpe(n_trials, t)) * math.sqrt(t - 1) / math.sqrt(denom)
        result = _NORM.cdf(z)
    except (OverflowError, ValueError, ZeroDivisionError):
        return 0.0
    return result if math.isfinite(result) else 0.0
