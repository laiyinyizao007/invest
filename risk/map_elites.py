"""简化的 MAP-Elites:按行为格子(risk/behavior.py)分档,从已占格子里选父代。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」「用户裁决」:
- 种群用简化 MAP-Elites 分格;**父代从已占格子中均匀随机选**(Mouret & Clune 2015 原文如此);
- **加权只用于同格内选父代**;
- **DSR 不用于选父代**:选父代只用**选参窗口**的适应度(`fitness`)。DSR 来自验证窗口,若用它选父代,
  验证信息会经由选择回到种群,抵消梯子的保护。`dsr`/`n_trials` 仍记录并展示给人看,但**本模块从不读取它们**
  (测试里有一条不变量守着:打乱它们不会改变选择结果)。

代价(须如实承认,同 ADR):选参窗口内的过拟合只能靠质量多样性、结构化反馈和既有门槛约束,这一层没有多重检验校正。

状态**不另存**:格子档案由谱系台账(risk/lineage.py)的记录推出来,没有第二份会与台账不一致的状态。
纯逻辑;随机性由调用方注入的 `random.Random` 提供,同一种子同一结果。
"""
from __future__ import annotations

import math
import random
from typing import Sequence

from risk.behavior import ALL_CELLS, CELL_PATTERN


def eligible(record: dict) -> bool:
    """能入档的记录:程序给出了格子、给出了有限的选参适应度,且不是候选自己报错(没有成绩可言)。"""
    fitness = record.get("fitness")
    cell = record.get("cell")
    return (record.get("status") != "strategy_error"
            and isinstance(cell, str) and bool(CELL_PATTERN.fullmatch(cell))
            and isinstance(fitness, (int, float)) and not isinstance(fitness, bool) and math.isfinite(fitness))


def _order(record: dict) -> tuple:
    """同一格子里的排序:适应度高者在前;并列时先入档者在前,再按哈希——结果与输入顺序无关。"""
    return (-float(record["fitness"]), record.get("created_at") or "", record["candidate_sha256"])


def archive(records: Sequence[dict]) -> dict[str, list[dict]]:
    """{格子: 该格子里全部合格记录(适应度从高到低)}。"""
    cells: dict[str, list[dict]] = {}
    for record in records:
        if eligible(record):
            cells.setdefault(record["cell"], []).append(record)
    for members in cells.values():
        members.sort(key=_order)
    return cells


def elites(records: Sequence[dict]) -> dict[str, dict]:
    """每个已占格子的精英(格子里适应度最高者)。"""
    return {cell: members[0] for cell, members in archive(records).items()}


def would_enter(records: Sequence[dict], new: dict) -> dict:
    """新个体能否成为所在格子的精英(MAP-Elites 的入格规则:格子为空,或优于占位者)。"""
    if not eligible(new):
        return {"enters": False, "reason": "no_cell"}
    current = elites(records).get(new["cell"])
    if current is None:
        return {"enters": True, "reason": "empty_cell"}
    if float(new["fitness"]) > float(current["fitness"]):
        return {"enters": True, "reason": "better"}
    return {"enters": False, "reason": "not_better"}


def select_parent(records: Sequence[dict], rng: random.Random) -> dict | None:
    """选父代:先从**已占格子**里**均匀**选一个,再在格子内按名次加权选(名次 r 的权重 1/(1+r))。

    没有任何合格记录返回 None(调用方应当从种子候选起步)。只读 `cell`、`fitness`、`status`、哈希与时间——
    **不读 `dsr`、`n_trials`、`sharpe` 等任何验证窗口派生的字段**。"""
    cells = archive(records)
    if not cells:
        return None
    cell = rng.choice(sorted(cells))
    members = cells[cell]
    weights = [1.0 / (1 + rank) for rank in range(len(members))]
    return rng.choices(members, weights=weights, k=1)[0]


def coverage(records: Sequence[dict]) -> dict:
    """格子占用情况(给人看):已占格子数 / 总格子数,以及每个已占格子的精英与个数。"""
    cells = archive(records)
    return {"occupied": len(cells), "total": len(ALL_CELLS),
            "cells": [{"cell": cell, "count": len(cells[cell]), "elite_sha256": cells[cell][0]["candidate_sha256"],
                       "elite_fitness": round(float(cells[cell][0]["fitness"]), 4)} for cell in sorted(cells)]}
