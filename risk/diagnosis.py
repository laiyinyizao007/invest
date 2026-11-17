"""确定性「诊断书」:回撤 / 次数 / 连亏 / 夏普四维。

纯 Python,不依赖 vn.py,同一输入恒得同一输出,可单测。研究沙盒的 `results` 端点以此为底座:
外层(LLM)只拿到这份结构化诊断去定向改进,而不是自己去读原始回测输出。
阈值与晋级门槛(`promotion_gate.GateThresholds`)同源,避免两套口径。
诊断只描述弱点,不替代门槛;通过诊断也不代表策略有效。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from risk.promotion_gate import (MAX_OOS_YEARS, TRADING_DAYS_PER_YEAR, GateThresholds,
                                 suspicious_sharpe_cap)

DIMENSIONS = ("drawdown", "trades", "losing_streak", "sharpe")   # 固定顺序,同等严重度时取靠前者
_SEVERITY = {"fail": 2, "warn": 1}
WARN_RATIO = 0.7        # 回撤/连亏达到上限的 70% 起预警


@dataclass(frozen=True)
class DiagnosisThresholds:
    gate: GateThresholds = GateThresholds()
    max_losing_streak: int = 8      # 与日内连亏熔断无关:这里衡量的是整段回测里最长的连亏


def max_losing_streak(pnls: Sequence[float]) -> int:
    """最长连续亏损笔数。口径同 risk_guard:盈利清零,亏损或持平累加。"""
    best = run = 0
    for pnl in pnls:
        run = 0 if pnl > 0 else run + 1
        best = max(best, run)
    return best


def _num(row: Mapping[str, Any], key: str) -> float | None:
    """取一个**有限数**;取不到(或不是有限数)一律返回 None ⇒ 那一维报「未知」。

    为什么连 NaN/inf 也当「取不到」:NaN 与任何数比较恒为 False ⇒ 会被静默当成
    「没超限」(看着像合格);inf 会让折算溢出成 OverflowError 把诊断整个炸掉。
    """
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        v = float(value)
    except (OverflowError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _unknown(note: str) -> dict:
    return {"level": "unknown", "value": None, "limit": None, "note": note}


def _drawdown(row: Mapping[str, Any], t: DiagnosisThresholds) -> dict:
    pct, yuan = _num(row, "maxdd_pct"), _num(row, "maxdd_yuan")
    if pct is None and yuan is None:
        return _unknown("缺失 maxdd_pct/maxdd_yuan")
    g = t.gate
    ratios = []
    if pct is not None:
        ratios.append(abs(pct) / g.max_drawdown_pct)
    if yuan is not None:
        ratios.append(abs(yuan) / g.max_drawdown_yuan)
    worst = max(ratios)
    level = "fail" if worst >= 1.0 else "warn" if worst >= WARN_RATIO else "ok"
    return {"level": level, "value": {"pct": pct and abs(pct), "yuan": yuan and abs(yuan)},
            "limit": {"pct": g.max_drawdown_pct, "yuan": g.max_drawdown_yuan},
            "note": f"最大回撤占上限 {worst:.0%}"}


def _trades(row: Mapping[str, Any], t: DiagnosisThresholds) -> dict:
    n = _num(row, "round_trips")
    if n is None:
        return _unknown("缺失 round_trips")
    need = t.gate.min_round_trips
    level = "fail" if n < need / 2 else "warn" if n < need else "ok"
    note = "往返笔数充足" if level == "ok" else f"往返 {int(n)} 笔 < {need}:夏普等指标统计意义不足" + ("(不足一半,基本不可信)" if level == "fail" else "")
    return {"level": level, "value": int(n), "limit": need, "note": note}


def _streak(pnls: Sequence[float] | None, t: DiagnosisThresholds) -> dict:
    if pnls is None:
        return _unknown("未提供逐笔盈亏,无法计算连亏")
    streak, limit = max_losing_streak(pnls), t.max_losing_streak
    level = "fail" if streak >= limit else "warn" if streak >= limit * WARN_RATIO else "ok"
    return {"level": level, "value": streak, "limit": limit, "note": f"最长连亏 {streak} 笔(上限 {limit})"}


def _cap_of(row: Mapping[str, Any], g: GateThresholds) -> float:
    """「可疑夏普」上限:**与晋级门槛同一份**(`risk.promotion_gate.suspicious_sharpe_cap`)。

    第 55 轮之十六:上限不再是与试验次数无关的常数 ⇒ 诊断层若还用老口径,
    就会与门槛**报出两个不同的答案**(同一个问题两个答案 ⇒ 迟早只改一边)。
    行里缺 `n_trials` / `oos_years` 时用**兜底下限**(旧口径)⇒ 诊断偏严 ⇒ 保守。
    """
    n_trials = row.get("n_trials")
    if isinstance(n_trials, bool) or not isinstance(n_trials, int):
        n_trials = 0
    years = _num(row, "oos_years")
    n_obs = 0
    # 与门槛同一套退化处理:折算溢出(年数大到乘 240 变成 inf)或年数超过物理上界
    # ⇒ 一律按「样本太短」处理 ⇒ 落回兜底下限 ⇒ **不许抛异常**。
    if years is not None and 0 < years <= MAX_OOS_YEARS:
        scaled = years * TRADING_DAYS_PER_YEAR
        if math.isfinite(scaled):
            n_obs = int(round(scaled))
    return suspicious_sharpe_cap(n_trials, n_obs, g)


def _sharpe(row: Mapping[str, Any], t: DiagnosisThresholds) -> dict:
    s = _num(row, "sharpe")
    if s is None:
        return _unknown("缺失 sharpe")
    g = t.gate
    cap = _cap_of(row, g)
    if s > cap:
        return {"level": "fail", "value": s, "limit": cap,
                "note": f"夏普 {s:.2f} 高得可疑:先排查前视偏差/过拟合,而不是庆祝"}
    if s < 0:
        return {"level": "fail", "value": s, "limit": g.min_oos_sharpe, "note": f"夏普 {s:.2f} 为负:当前参数亏钱"}
    level = "ok" if s >= g.min_oos_sharpe else "warn"
    return {"level": level, "value": s, "limit": g.min_oos_sharpe,
            "note": f"夏普 {s:.2f}" + ("" if level == "ok" else f" < {g.min_oos_sharpe}")}


def diagnose(row: Mapping[str, Any], pnls: Sequence[float] | None = None,
             t: DiagnosisThresholds = DiagnosisThresholds()) -> dict:
    """生成诊断书。`row` 是回测结果行(sharpe/maxdd_pct/maxdd_yuan/round_trips),`pnls` 是逐笔往返盈亏。"""
    dims = {
        "drawdown": _drawdown(row, t),
        "trades": _trades(row, t),
        "losing_streak": _streak(pnls, t),
        "sharpe": _sharpe(row, t),
    }
    ranked = [(name, _SEVERITY.get(dims[name]["level"], 0)) for name in DIMENSIONS]
    top = max(sev for _, sev in ranked)
    weakest = next(name for name, sev in ranked if sev == top) if top > 0 else None
    missing = [n for n in DIMENSIONS if dims[n]["level"] == "unknown"]
    parts = [f"最弱维度: {weakest}({dims[weakest]['note']})" if weakest else "四维均无告警(不代表策略有效)"]
    if missing:
        parts.append("数据缺失(unknown): " + ", ".join(missing))
    return {"dimensions": dims, "weakest": weakest, "summary": ";".join(parts)}
