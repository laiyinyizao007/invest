"""候选评估:把滚动验证段的回测结果聚合成晋级门槛的输入,并调用 `check_gate`。

纯 Python,不依赖 vn.py 与回测引擎;回测由调用方(研究沙盒)负责跑,这里只做聚合与判定。
决议见 docs/adr/20261112-candidate-evaluation.md:样本外 = 滚动验证段(不触碰测试段),门槛数值不放宽。

夏普口径:按日净盈亏 / 本金得到日收益率,年化因子取 240(与 vn.py 日线口径一致)。这是对 vn.py 统计的近似复算,
用于跨窗口拼接,数值不要求与单窗口的 `sharpe_ratio` 逐位相等。
"""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from risk.dsr import deflated_sharpe, deflated_sharpe_detail
from risk.promotion_gate import CandidateMetrics, GateThresholds, check_gate, check_gate_ids

TRADING_DAYS_PER_YEAR = 240
DEFAULT_RANDOM_RUNS = 200


def annualized_sharpe(daily_pnls: Sequence[float], capital: float) -> float:
    n = len(daily_pnls)
    if n < 2 or capital <= 0:
        return 0.0
    rets = [p / capital for p in daily_pnls]
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(TRADING_DAYS_PER_YEAR)


def pool_oos(windows: Sequence[dict], capital: float) -> dict:
    """把多个滚动验证段拼接成一条样本外序列。回撤取各窗口最差值(保守,不假装窗口之间的回撤可以抵消)。"""
    if not windows:
        raise ValueError("至少需要一个验证窗口")
    pnls: list[float] = [p for w in windows for p in w["daily_pnls"]]
    return {
        "sharpe": annualized_sharpe(pnls, capital),
        "years": len(pnls) / TRADING_DAYS_PER_YEAR,
        "round_trips": sum(int(w["round_trips"]) for w in windows),
        "max_drawdown_pct": min(w["maxdd_pct"] for w in windows),
        "max_drawdown_yuan": min(w["maxdd_yuan"] for w in windows),
        "daily_returns": [p / capital for p in pnls] if capital > 0 else [],      # DSR 用,不进入落盘的 metrics
    }


def percentile_rank(value: float, samples: Iterable[float]) -> float:
    """value 严格超过样本的百分比(0~100);平手不算超过;没有样本时返回 0,保证门槛不会因缺对照而被放过。"""
    data = list(samples)
    if not data:
        return 0.0
    return 100.0 * sum(1 for s in data if s < value) / len(data)


def random_seeds(n: int = DEFAULT_RANDOM_RUNS) -> range:
    """随机入场对照的固定种子序列 0..n-1,保证同一候选的评估可复现。"""
    if n <= 0:
        raise ValueError("随机对照次数必须为正")
    return range(n)


def build_metrics(pooled: dict, stress_sharpe: float, random_sharpes: Sequence[float],
                  data_adjusted: bool, on_plateau: bool, n_trials: int | None = None,
                  sharpe_stats: tuple[int, float, float] | None = None) -> CandidateMetrics:
    """n_trials 为累计试验次数(含被淘汰者,见 risk/trial_ledger.py)。缺省或缺日收益序列时 DSR 记为 None,门槛按未通过处理。"""
    if n_trials is not None and (isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1):
        raise ValueError("n_trials 必须是 ≥ 1 的整数")
    returns = pooled.get("daily_returns")
    dsr = deflated_sharpe(returns, n_trials, sharpe_stats) if n_trials is not None and returns else None
    return CandidateMetrics(
        oos_sharpe=pooled["sharpe"],
        oos_years=pooled["years"],
        max_drawdown_pct=pooled["max_drawdown_pct"],
        max_drawdown_yuan=pooled["max_drawdown_yuan"],
        round_trips=pooled["round_trips"],
        random_entry_percentile=percentile_rank(pooled["sharpe"], random_sharpes),
        stress_sharpe=stress_sharpe,
        data_adjusted=data_adjusted,
        parameter_on_plateau=on_plateau,
        dsr=dsr,
        n_trials=n_trials if dsr is not None else 0,
    )


def evaluate(pooled: dict, stress_sharpe: float, random_sharpes: Sequence[float],
             data_adjusted: bool, on_plateau: bool, thresholds: GateThresholds = GateThresholds(),
             n_trials: int | None = None, sharpe_stats: tuple[int, float, float] | None = None) -> dict:
    """`sharpe_stats` 是历次试验日夏普的 (n, mean, M2),供 DSR 用经验方差(V = max(V_经验, 1/(T-1)),只能更严)。
    结果里的 `dsr_variance` 记录了 V 用的是什么、依据是什么(含"样本不足"退回理论值这件事)。"""
    metrics = build_metrics(pooled, stress_sharpe, random_sharpes, data_adjusted, on_plateau, n_trials, sharpe_stats)
    failures = check_gate(metrics, thresholds)
    failed_check_ids = check_gate_ids(metrics, thresholds)
    returns = pooled.get("daily_returns")
    variance = (deflated_sharpe_detail(returns, n_trials, sharpe_stats)["variance"]
                if n_trials is not None and returns else None)
    return {"passed": not failures, "failures": failures, "failed_check_ids": failed_check_ids,
            "metrics": metrics.__dict__ | {"random_runs": len(random_sharpes)}, "dsr_variance": variance}
