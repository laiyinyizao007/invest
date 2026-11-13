"""结构化诊断:只含**枚举 + 0–1 数值**,没有任何自由文本。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:反思节点只输出程序校验过的结构化字段
(枚举 + 0–1 数值),自由文本只存档给人看,**不进变异提示词**。

为什么要从 `risk/diagnosis.py` 的诊断书再抽一层:那份诊断书的 `note` 是给人读的中文句子,里面带着具体数字
("夏普 1.23""最大回撤占上限 85%")——如果数字来自**验证窗口**,这等于把验证窗口的信息绕过梯子直接交给了 LLM。
这里的输出:
- `levels`:每个维度一个枚举(ok / warn / fail / unknown);
- `ratios`:每个维度一个 [0, 1] 的比例(越接近 1 越"满"或越"健康",见下),没有原始数值;
- `weakest`:最弱维度的枚举名,或 None。

比例的含义(统一成"越大越好",便于程序比较,不是给人读的):
- drawdown:1 − 回撤占上限的比例(回撤用得越多越接近 0);
- trades:往返笔数 / 门槛笔数;
- losing_streak:1 − 最长连亏 / 上限;
- sharpe:夏普 / 门槛夏普(负数记 0)。
全部夹到 [0, 1];缺失的维度(unknown)比例为 None。
调用方决定这份诊断来自哪个窗口——进入进化回路的只能是**选参窗口**的诊断(放开的那一层)。
"""
from __future__ import annotations

import math
from typing import Any

DIMENSIONS = ("drawdown", "trades", "losing_streak", "sharpe")
LEVELS = ("ok", "warn", "fail", "unknown")


def _clip01(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 4)


def _ratio(name: str, dim: dict) -> float | None:
    level = dim.get("level")
    value, limit = dim.get("value"), dim.get("limit")
    if level == "unknown":
        return None
    try:
        if name == "drawdown":
            used = []
            for key in ("pct", "yuan"):
                v = value.get(key) if isinstance(value, dict) else None
                lim = limit.get(key) if isinstance(limit, dict) else None
                if isinstance(v, (int, float)) and isinstance(lim, (int, float)) and lim > 0:
                    used.append(abs(v) / lim)
            return _clip01(1.0 - max(used)) if used else None
        if name == "trades":
            return _clip01(value / limit) if limit else None
        if name == "losing_streak":
            return _clip01(1.0 - value / limit) if limit else None
        if name == "sharpe":
            return _clip01(value / limit) if limit else None
    except (TypeError, ZeroDivisionError):
        return None
    return None


def structure(diagnosis: dict) -> dict:
    """把 `risk.diagnosis.diagnose` 的输出变成结构化诊断(丢掉全部 note/summary 自由文本)。"""
    dims = diagnosis.get("dimensions") if isinstance(diagnosis, dict) else None
    if not isinstance(dims, dict):
        raise ValueError("不是诊断书")
    levels, ratios = {}, {}
    for name in DIMENSIONS:
        dim = dims.get(name)
        level = dim.get("level") if isinstance(dim, dict) else None
        level = level if level in LEVELS else "unknown"
        ratio = _ratio(name, dim) if isinstance(dim, dict) and level != "unknown" else None
        if ratio is None:
            level = "unknown"                      # 算不出比例的维度一律记为 unknown,保证输出自己过得了 validate
        levels[name], ratios[name] = level, ratio
    weakest = diagnosis.get("weakest")
    weakest = weakest if weakest in DIMENSIONS and levels[weakest] != "unknown" else None
    return {"weakest": weakest, "levels": levels, "ratios": ratios}


def validate(obj: Any) -> bool:
    """这是不是一份合法的结构化诊断:键集精确、枚举合法、比例在 [0, 1] 或 None。不合法返回 False。

    ledger/外层响应在写出前必须过这一关:`diagnosis` 来自程序,但经过子进程(候选所在的进程)传回,不可信。"""
    if not isinstance(obj, dict) or set(obj) != {"weakest", "levels", "ratios"}:
        return False
    if obj["weakest"] is not None and obj["weakest"] not in DIMENSIONS:
        return False
    levels, ratios = obj["levels"], obj["ratios"]
    if not isinstance(levels, dict) or set(levels) != set(DIMENSIONS):
        return False
    if not isinstance(ratios, dict) or set(ratios) != set(DIMENSIONS):
        return False
    if any(levels[n] not in LEVELS for n in DIMENSIONS):
        return False
    for name in DIMENSIONS:
        r = ratios[name]
        if r is None:
            continue
        if isinstance(r, bool) or not isinstance(r, (int, float)) or not math.isfinite(r) or not 0.0 <= r <= 1.0:
            return False
    # 一致性:unknown 的维度没有比例,有比例的维度不能是 unknown
    if any((levels[n] == "unknown") != (ratios[n] is None) for n in DIMENSIONS):
        return False
    return True
