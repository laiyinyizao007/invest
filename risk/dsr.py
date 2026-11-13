"""Deflated Sharpe Ratio(Bailey & López de Prado, 2014):在 N 次试验中挑出的最优夏普,要扣掉"纯靠运气能挑到多高"。

纯 Python(statistics.NormalDist),不依赖 numpy/vn.py。阈值与口径见 docs/adr/20261113-evolution-engine.md:
- 一切在**日**收益口径下计算(未年化),T = 日收益个数;
- 试验间夏普方差 V 取零假设下夏普估计量的方差 1/(T-1)(偏度/峰度修正式在 SR=0 处退化为该值),不另设经验系数;
- 返回的是"真实夏普大于去通胀后基准"的概率(0~1),门槛为 ≥ 0.95。
退化输入(样本太短、零波动、修正项非正)一律返回 0.0,保证门槛不会因数据异常而被放过。

经验方差(阶段 3,ADR「阶段 3 前置」):公开资料里 V 是"N 次试验各自夏普的方差(经验值)",而阶段 1 用的是零假设下的
理论值 1/(T-1)——真实试验的夏普分散度通常更大,所以阶段 1 偏宽松。现在账本在记次数之外同时记录每次评估扫过的格子夏普,
`V = max(V_经验, 1/(T-1))`:
- **取 max 而不是替换**:这次改动只可能让 DSR 更严,不会比阶段 1 更松——"阈值不得放宽"从口头承诺变成机制;
- **V_经验 取各次评估内部格子夏普方差的中位数**(第 15 轮 codex 的 Critical):样本由候选自己的网格产生,
  若把所有格子夏普混在一起算一个样本方差,一次"网格极端分裂"的评估(限幅后方差 0.28)就能把 V 拉到让此后**每个**候选的 DSR 恒为 0,
  而账本只增不减、无法恢复。中位数让单次评估进不了结果——投毒需要占**多数**评估;
- **评估次数不足 3 次、或某次评估的有效格子少于 5 个时,退回理论值**,并把"样本不足"这件事本身写进结果(`basis`),不得静默;
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


MIN_EMPIRICAL_RUNS = 3        # 少于这么多次评估,经验方差不可靠(也扛不住单次投毒),退回理论值(并显式记录)
MIN_CELLS_PER_RUN = 5         # 一次评估里有效格子少于这么多个,它的方差没有意义,不入样本
MAX_RUNS_USED = 200           # 只取最近这么多次评估的方差(命令行参数的长度也受它约束)
MAX_TRIAL_SHARPE = 1.0        # 单条试验的日夏普上限(年化约 15):更大的值视为无效,防止伪造的样本把 V 拉到离谱


def run_variance(values: Sequence[float]) -> float | None:
    """一次评估里各格子日夏普的样本方差(无偏)。有效格子少于 MIN_CELLS_PER_RUN 个返回 None。"""
    n = len(values)
    if n < MIN_CELLS_PER_RUN:
        return None
    mean = sum(values) / n
    value = sum((x - mean) ** 2 for x in values) / (n - 1)
    return value if math.isfinite(value) and value >= 0 else None


def variance_of_sharpes(n_obs: int, run_variances: Sequence[float] | None) -> dict:
    """DSR 用的 V 及其依据。返回 {"v_used", "v_theory", "v_empirical", "n_evaluations", "basis"}。

    `run_variances` 是历次评估各自的格子夏普方差(最近 MAX_RUNS_USED 次);V_经验 取它们的**中位数**。
    basis:
    - "empirical":评估次数够(≥ MIN_EMPIRICAL_RUNS)且 V_经验 ≥ 理论值,用经验值;
    - "theory":次数够,但 V_经验 小于理论值,理论值作为下限生效(V 只能更大,不能更小);
    - "insufficient_samples":次数不足或没提供,退回理论值——**这件事本身要被记录**,不得静默。"""
    if n_obs < 2:
        raise ValueError("n_obs 必须 ≥ 2")
    theory = 1.0 / (n_obs - 1)
    clean = sorted(v for v in (run_variances or ()) if isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) and v >= 0)[:]
    n = len(clean)
    empirical = None
    if n:
        mid = n // 2
        empirical = clean[mid] if n % 2 else (clean[mid - 1] + clean[mid]) / 2
    if n < MIN_EMPIRICAL_RUNS or empirical is None:
        return {"v_used": theory, "v_theory": theory, "v_empirical": empirical, "n_evaluations": n,
                "basis": "insufficient_samples"}
    return {"v_used": max(empirical, theory), "v_theory": theory, "v_empirical": empirical, "n_evaluations": n,
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
                           run_variances: Sequence[float] | None = None) -> dict:
    """样本外日收益序列的 DSR(0~1)及 V 的依据。n_trials 必须包含被淘汰的全部试验。

    `run_variances` 是历次评估各自的格子日夏普方差;不给或不足则按"样本不足"退回理论值(并在 `variance.basis` 里写明)。
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
        info = variance_of_sharpes(t, run_variances)
        z = (sr - expected_max_sharpe(n_trials, t, info["v_used"])) * math.sqrt(t - 1) / math.sqrt(denom)
        result = _NORM.cdf(z)
    except (OverflowError, ValueError, ZeroDivisionError):
        return {"dsr": 0.0, "variance": None}
    return {"dsr": result if math.isfinite(result) else 0.0, "variance": info}


def deflated_sharpe(daily_returns: Sequence[float], n_trials: int,
                    run_variances: Sequence[float] | None = None) -> float:
    """样本外日收益序列的 DSR(0~1)。n_trials 必须包含被淘汰的全部试验。详见 `deflated_sharpe_detail`。"""
    return deflated_sharpe_detail(daily_returns, n_trials, run_variances)["dsr"]
