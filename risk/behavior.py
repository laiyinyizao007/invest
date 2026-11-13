"""策略的行为描述符与 MAP-Elites 分格:持仓周期 × 交易频率。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:种群用简化 MAP-Elites 分格(持仓周期、交易频率等),
**格子从小做起**。这里是 4 × 4 = 16 格;格子边界写在代码常量里,是**事先定死**的:
看到候选之后再调边界,等于挑一个让某个候选"独占一格"的口径。

描述符**由程序从回测结果算出来**,不来自候选自报、更不来自 LLM 的自由文本:
- 持仓周期 = 每个往返从开仓到平仓经过的 K 线根数的平均值;
- 交易频率 = 每 100 根 K 线的往返笔数(与窗口长度无关,不同窗口之间可比)。

取自**选参窗口**(进化回路放开的那一层),不碰验证窗口。

纯逻辑,不依赖 vn.py:成交按鸭子类型读 `datetime`/`offset`(`offset.name` 为 OPEN 或以 CLOSE 开头)。
"""
from __future__ import annotations

import bisect
import math
import re
from typing import Any, Sequence

# 持仓周期(K 线根数,15 分钟线;约 4 小时 / 1.5 个交易日 / 一周)。bin 0: ≤16;1: ≤64;2: ≤192;3: >192
HOLD_EDGES = (16.0, 64.0, 192.0)
# 交易频率(每 100 根 K 线的往返笔数)。bin 0: <0.5;1: <1.5;2: <4.0;3: ≥4.0
FREQ_EDGES = (0.5, 1.5, 4.0)
CELL_PATTERN = re.compile(r"H[0-3]F[0-3]")
ALL_CELLS = tuple(f"H{h}F{f}" for h in range(4) for f in range(4))


def _kind(offset: Any) -> str | None:
    name = getattr(offset, "name", None)
    name = name if isinstance(name, str) else str(offset)
    upper = name.upper()
    if upper == "OPEN":
        return "open"
    if upper.startswith("CLOSE"):
        return "close"
    return None


def round_trip_holding_bars(trades: Sequence[Any], bars: Sequence[Any]) -> list[int]:
    """每个往返(开仓→平仓)持有的 K 线根数。

    按成交顺序配对:遇到开仓记下根数下标,遇到平仓与最近一次开仓配成一对;
    没有对应开仓的平仓、末尾还没平掉的开仓都不计入(不凭空造数)。
    成交时间不在 `bars` 里(时间戳对不上)的往返丢弃,而不是猜一个下标。"""
    index = {getattr(b, "datetime", None): i for i, b in enumerate(bars)}
    holds: list[int] = []
    opened: int | None = None
    for trade in trades:
        kind = _kind(getattr(trade, "offset", None))
        position = index.get(getattr(trade, "datetime", None))
        if kind == "open":
            opened = position
        elif kind == "close":
            if opened is not None and position is not None and position >= opened:
                holds.append(position - opened)
            opened = None
    return holds


def trips_per_100_bars(round_trips: int, n_bars: int) -> float | None:
    if isinstance(round_trips, bool) or not isinstance(round_trips, int) or round_trips < 0:
        return None
    if isinstance(n_bars, bool) or not isinstance(n_bars, int) or n_bars <= 0:
        return None
    return round_trips * 100.0 / n_bars


def behavior_cell(avg_hold_bars: float | None, trips_per_100: float | None) -> str | None:
    """MAP-Elites 格子名 `H<持仓档>F<频率档>`。任一描述符缺失/非有限时返回 None(不归任何格子,不猜)。"""
    for value in (avg_hold_bars, trips_per_100):
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
    if avg_hold_bars < 0 or trips_per_100 < 0:
        return None
    hold_bin = bisect.bisect_left(HOLD_EDGES, avg_hold_bars)           # ≤ 边界 归入该档
    freq_bin = bisect.bisect_right(FREQ_EDGES, trips_per_100)          # < 边界 归入该档
    return f"H{hold_bin}F{freq_bin}"


def describe_behavior(round_trips: int, n_bars: int, hold_bars: Sequence[int] | None) -> dict:
    """由回测结果算出行为描述符。没有往返、没有持仓记录、窗口无效时对应字段为 None,cell 也是 None。"""
    freq = trips_per_100_bars(round_trips, n_bars)
    holds = [h for h in (hold_bars or []) if isinstance(h, int) and not isinstance(h, bool) and h >= 0]
    avg = sum(holds) / len(holds) if holds else None
    return {"round_trips": round_trips if isinstance(round_trips, int) and not isinstance(round_trips, bool) else None,
            "n_bars": n_bars if isinstance(n_bars, int) and not isinstance(n_bars, bool) else None,
            "avg_hold_bars": None if avg is None else round(avg, 4),
            "trips_per_100_bars": None if freq is None else round(freq, 4),
            "cell": behavior_cell(avg, freq) if round_trips else None}
