"""Deflated Sharpe Ratio(Bailey & López de Prado, 2014):在 N 次试验中挑出的最优夏普,要扣掉"纯靠运气能挑到多高"。

纯 Python(statistics.NormalDist),不依赖 numpy/vn.py。阈值与口径见 docs/adr/20261113-evolution-engine.md:
- 一切在**日**收益口径下计算(未年化),T = 日收益个数;
- 试验间夏普方差 V 取零假设下夏普估计量的方差 1/(T-1)(偏度/峰度修正式在 SR=0 处退化为该值),不另设经验系数;
- 返回的是"真实夏普大于去通胀后基准"的概率(0~1),门槛为 ≥ 0.95。
退化输入(样本太短、零波动、修正项非正)一律返回 0.0,保证门槛不会因数据异常而被放过。

经验方差(阶段 3,ADR「阶段 3 前置」):公开资料里 V 是"N 次试验各自夏普的方差(经验值)",而阶段 1 用的是零假设下的
理论值 1/(T-1)——真实试验的夏普分散度通常更大,所以阶段 1 偏宽松。现在账本在记次数之外同时记录每次试验的夏普,
`V = max(V_经验, 1/(T-1))`:
- **取 max 而不是替换**:这次改动只可能让 DSR 更严,不会更松——"阈值不得放宽"从口头承诺变成机制,没人能靠挑一个更松的 V 来放水;
- **试验数 < 20 时 `V_经验` 不可靠**,退回理论值,并把"样本不足"这件事本身写进结果(`basis`),不得静默;
- 试验夏普与 DSR 同一口径:**非年化逐期(日)夏普**。
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


MIN_EMPIRICAL_TRIALS = 20     # 少于这么多条试验夏普,经验方差不可靠,退回理论值(并显式记录)
MAX_TRIAL_SHARPE = 1.0        # 单条试验的日夏普上限(年化约 15):更大的值视为无效,防止伪造的样本把 V 拉到离谱


def sharpe_stats(values: Sequence[float]) -> tuple[int, float, float]:
    """(个数, 均值, 离差平方和 M2)。用 Welford 在线算法;可与别的批次用 `merge_stats` 合并而不需要原始样本。"""
    n, mean, m2 = 0, 0.0, 0.0
    for x in values:
        n += 1
        delta = x - mean
        mean += delta / n
        m2 += delta * (x - mean)
    return n, mean, m2


def merge_stats(a: tuple[int, float, float], b: tuple[int, float, float]) -> tuple[int, float, float]:
    """合并两批样本的 (n, mean, M2)(Chan 等的并行方差公式)。"""
    na, ma, m2a = a
    nb, mb, m2b = b
    if na == 0:
        return b if nb else (0, 0.0, 0.0)
    if nb == 0:
        return a
    n = na + nb
    delta = mb - ma
    mean = ma + delta * nb / n
    m2 = m2a + m2b + delta * delta * na * nb / n
    return n, mean, m2


def variance_of_sharpes(n_obs: int, stats: tuple[int, float, float] | None) -> dict:
    """DSR 用的 V 及其依据。返回 {"v_used", "v_theory", "v_empirical", "n_sharpes", "basis"}。

    basis:
    - "empirical":样本够(≥ MIN_EMPIRICAL_TRIALS)且经验方差 ≥ 理论值,用经验值;
    - "theory":样本够,但经验方差小于理论值,理论值作为下限生效(V 只能更大,不能更小);
    - "insufficient_samples":样本不足或没提供,退回理论值——**这件事本身要被记录**,不得静默。"""
    if n_obs < 2:
        raise ValueError("n_obs 必须 ≥ 2")
    theory = 1.0 / (n_obs - 1)
    n = 0 if stats is None else int(stats[0])
    empirical = None
    if stats is not None and n >= 2:
        value = stats[2] / (n - 1)                      # 样本方差(无偏)
        empirical = value if math.isfinite(value) and value >= 0 else None
    if n < MIN_EMPIRICAL_TRIALS or empirical is None:
        return {"v_used": theory, "v_theory": theory, "v_empirical": empirical, "n_sharpes": n,
                "basis": "insufficient_samples"}
    return {"v_used": max(empirical, theory), "v_theory": theory, "v_empirical": empirical, "n_sharpes": n,
            "basis": "empirical" if empirical >= theory else "theory"}


def expected_max_sharpe(n_trials: int, n_obs: int, variance: float | None = None) -> float:
    """N 次独立试验、零真实优势时,日夏普最大值的期望(DSR 的基准 SR0)。N=1 时无通胀,返回 0。

    variance 为 None 时取理论值 1/(n_obs-1)。传入的方差不得小于理论值(只能更严)。"""
    if n_trials < 1:
        raise ValueError("n_trials 必须 ≥ 1")
    if n_obs < 2:
        raise ValueError("n_obs 必须 ≥ 2")
    if n_trials == 1:
        return 0.0
    theory = 1.0 / (n_obs - 1)
    v = theory if variance is None else variance
    if not math.isfinite(v) or v < theory - 1e-15:
        raise ValueError("V 不得小于零假设下的理论值 1/(T-1)(阈值只能更严,不能放宽)")
    return math.sqrt(v) * ((1 - EULER_GAMMA) * _NORM.inv_cdf(1 - 1 / n_trials)
                           + EULER_GAMMA * _NORM.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe_detail(daily_returns: Sequence[float], n_trials: int,
                           stats: tuple[int, float, float] | None = None) -> dict:
    """样本外日收益序列的 DSR(0~1)及 V 的依据。n_trials 必须包含被淘汰的全部试验。

    `stats` 是历次试验日夏普的 (n, mean, M2);不给则按"样本不足"退回理论值(并在 `variance.basis` 里写明)。
    返回 {"dsr": float, "variance": {...}};`variance` 在输入退化(DSR 记 0.0)时为 None。"""
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        raise ValueError("n_trials 必须是 ≥ 1 的整数")
    t = len(daily_returns)
    if t < 3:
        return {"dsr": 0.0, "variance": None}
    # NaN/inf 比较恒为 False,会让 `dsr < 阈值` 放过候选,所以任何非有限的输入或中间量都按 0.0 处理(fail-closed)
    if not all(isinstance(r, (int, float)) and not isinstance(r, bool) and math.isfinite(r) for r in daily_returns):
        return {"dsr": 0.0, "variance": None}
    try:
        mean, sd, skew, kurt = moments(daily_returns)
        if not all(math.isfinite(x) for x in (mean, sd, skew, kurt)) or sd <= 0:
            return {"dsr": 0.0, "variance": None}
        sr = mean / sd
        denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
        if not math.isfinite(denom) or denom <= 0:
            return {"dsr": 0.0, "variance": None}
        info = variance_of_sharpes(t, stats)
        z = (sr - expected_max_sharpe(n_trials, t, info["v_used"])) * math.sqrt(t - 1) / math.sqrt(denom)
        result = _NORM.cdf(z)
    except (OverflowError, ValueError, ZeroDivisionError):
        return {"dsr": 0.0, "variance": None}
    return {"dsr": result if math.isfinite(result) else 0.0, "variance": info}


def deflated_sharpe(daily_returns: Sequence[float], n_trials: int,
                    stats: tuple[int, float, float] | None = None) -> float:
    """样本外日收益序列的 DSR(0~1)。n_trials 必须包含被淘汰的全部试验。详见 `deflated_sharpe_detail`。"""
    return deflated_sharpe_detail(daily_returns, n_trials, stats)["dsr"]
