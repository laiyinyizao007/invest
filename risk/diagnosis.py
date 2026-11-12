"""确定性「诊断书」:回撤 / 次数 / 连亏 / 夏普四维。

纯 Python,不依赖 vn.py,同一输入恒得同一输出,可单测。研究沙盒的 `results` 端点以此为底座:
外层(LLM)只拿到这份结构化诊断去定向改进,而不是自己去读原始回测输出。
阈值与晋级门槛(`promotion_gate.GateThresholds`)同源,避免两套口径。
诊断只描述弱点,不替代门槛;通过诊断也不代表策略有效。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from risk.promotion_gate import GateThresholds

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
    value = row.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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


def _sharpe(row: Mapping[str, Any], t: DiagnosisThresholds) -> dict:
    s = _num(row, "sharpe")
    if s is None:
        return _unknown("缺失 sharpe")
    g = t.gate
    if s > g.max_suspicious_sharpe:
        return {"level": "fail", "value": s, "limit": g.max_suspicious_sharpe,
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
