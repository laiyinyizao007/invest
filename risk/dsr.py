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


def required_sharpe_for_dsr(n_trials: int, n_obs: int, min_dsr: float = 0.95) -> float:
    """**认证线**:DSR 恰好等于 `min_dsr` 的那个**日**夏普(0~1 的 DSR,0~1 的夏普不在此列)。

    **口径**:正态收益假设(偏度 0、峰度 3)⇒ DSR 的分母退化成 `√(1 + sr²/2)`:

        DSR(sr) = Φ( (sr - SR0(N,T)) · √(T-1) / √(1 + sr²/2) )

    解 `DSR(sr) = min_dsr` 得闭式解(取**大**根;小根落在 `sr < SR0` 处,那里 DSR < 0.5):

        u2 = T-1 ,  q = Φ⁻¹(min_dsr) ,  A = u2 - q²/2
        sr = ( SR0·u2 + q·√( SR0²·u2/2 + A ) ) / A

    **为什么要有它**:晋级门槛里那条「样本外夏普高得可疑」的上限,原先是一个**常数**
    (3.0),**与试验次数无关** ⇒ 试验次数一多,它会比 DSR **更早**把候选毙掉
    (报出来的是「先排查前视偏差」,而 DSR 那一关**根本没走**)⇒ 两套口径打架。
    把上限改成**由本函数反解出来的认证线**,两条规则就**同一个统计模型**。

    **退化输入一律返回 0.0**(fail-closed:解不出来 ⇒ 不得用来**放宽**任何东西;
    调用方必须把 0.0 理解为「用兜底下限」而不是「可以放宽到 0」)。
    """
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        return 0.0
    if isinstance(n_obs, bool) or not isinstance(n_obs, int) or n_obs < 3:
        return 0.0
    if (isinstance(min_dsr, bool) or not isinstance(min_dsr, (int, float))
            or not math.isfinite(min_dsr) or not 0.0 < min_dsr < 1.0):
        return 0.0
    try:
        q = _NORM.inv_cdf(min_dsr)
        c = expected_max_sharpe(n_trials, n_obs)
        u2 = float(n_obs - 1)
        a = u2 - q * q / 2.0
        if a <= 0:
            return 0.0
        sr = (c * u2 + q * math.sqrt(c * c * u2 / 2.0 + a)) / a
    except (OverflowError, ValueError, ZeroDivisionError):
        return 0.0
    if not math.isfinite(sr) or sr <= 0.0:
        return 0.0
    return sr


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
