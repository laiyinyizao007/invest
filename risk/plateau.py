"""参数高原判定(晋级门槛第 8 条 `parameter_on_plateau` 的计算来源)。

纯 Python,不依赖 vn.py。口径承接 `scripts/optimize.py::smoothed` 的 3×3×3 邻域平滑:
最优格子的邻域必须完整、自身有效,并且

1. 邻域平均得分(无效邻居按悲观下限计)> 0——ADR promotion-gate 第 8 条原文「邻域平均得分同样为正」;
2. 邻域内至少 `min_positive_fraction` 的格子自身得分为正——防止「一个巨大的离群点把均值拉正」,
   这是本实现相对 ADR 原文新增的更严一条,见 docs/adr/20261112-research-sandbox-groundwork.md。
"""
from __future__ import annotations

import itertools
from typing import Iterator, Mapping

NEG_INF = float("-inf")
Cell = tuple[int, int, int]


def neighborhood(cell: Cell) -> Iterator[Cell]:
    """含自身在内的 27 个格子(各维 ±1)。"""
    fi, si, mi = cell
    for a, b, c in itertools.product((-1, 0, 1), repeat=3):
        yield fi + a, si + b, mi + c


def parameter_on_plateau(scores: Mapping[Cell, float], cell: Cell, min_positive_fraction: float = 0.5) -> bool:
    if cell not in scores or scores[cell] == NEG_INF:
        return False
    positions = list(neighborhood(cell))
    if any(pos not in scores for pos in positions):
        return False                                   # 边界/空洞:邻域不完整不算高原
    valid = [v for v in scores.values() if v != NEG_INF]
    floor = min(0.0, min(valid)) if valid else 0.0     # 与 optimize.smoothed 相同的悲观下限
    values = [floor if scores[pos] == NEG_INF else scores[pos] for pos in positions]
    positive = sum(1 for v in values if v > 0)
    return sum(values) / len(values) > 0 and positive / len(values) >= min_positive_fraction


def smoothed(scores: Mapping[Cell, float]) -> dict[Cell, float]:
    """每个格子的得分 = 自己及周围 3×3×3 邻域共 27 个格子得分的平均(与 scripts/optimize.py::smoothed 同一算法的纯 Python 版)。

    - 网格内但无效(-inf)的邻居按「悲观下限」计入:下限 = min(0, 全网格有效得分的最小值);
    - 邻域不完整的格子(网格边界或空洞)不参与选参,自身无效的格子也不参与。
    """
    out: dict[Cell, float] = {}
    valid = [v for v in scores.values() if v != NEG_INF]
    floor = min(0.0, min(valid)) if valid else 0.0
    for cell, own in scores.items():
        if own == NEG_INF:
            continue
        positions = list(neighborhood(cell))
        if any(pos not in scores for pos in positions):
            continue
        values = [floor if scores[pos] == NEG_INF else scores[pos] for pos in positions]
        out[cell] = sum(values) / len(values)
    return out
