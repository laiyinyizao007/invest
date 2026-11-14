"""候选参数网格寻优(研究沙盒):在**选参窗口**上扫参数网格、邻域平滑选参、判定是否在参数高原上。

为什么需要它:晋级门槛第 8 条「参数位于高原」需要网格得分,而 LLM 写的候选没有网格——此前这一项对候选恒为未通过。
这里让候选**声明**一个三维网格,服务端扫描并产出高原数据。

样本外纪律:选参窗口必须止于 Walk-Forward 第一个验证窗口之前(2022-07-01 之前),之后的滚动验证窗口才是真正的样本外。
只做选参,不看验证段;网格上限、最小往返笔数等来自 config/research_sandbox.json。

风控底线永不优化(roadmap §2.4 阶段三铁律):风险预算、连亏熔断笔数、ATR 周期、历史代码不得出现在网格里。
纯逻辑:回测执行器由调用方注入,便于用假执行器单测。
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Callable, Iterator, Sequence

from risk import candidate_eval as ce
from risk.plateau import NEG_INF, parameter_on_plateau, smoothed

# 与 walk_forward_optimizer.build_windows()[0].valid_start 相同(有测试锁定)
FIRST_VALIDATION_START = datetime(2022, 7, 1)
AXES = 3
MIN_AXIS_VALUES = 3
MAX_AXIS_VALUES = 10
# 风控/固定参数:不得进入网格(风控底线永不优化)。取自不可变宪法清单,避免两处各写一份而漂移。
# 清单不可用时退回内置集合:宁可少拦也不让优化器整体不可用(真正的兜底在 check_params 与运行时类校验)。
#
# 尺子必须与 `check_params` **同一把**:那一处查的是「成员 ∪ 参数」且大小写不敏感,
# 这里原来只查 `protected_parameters` 且大小写敏感——同一把尺子两处量纲不同,
# 候选把参数名写成 `Risk_Budget` 并把网格键拼出来,网格层就放行了(第 7 轮 codex 实测)。
try:
    from risk.constitution import load as _load_constitution
    from risk.constitution import protected_lower_all as _protected_lower_all
    _CONSTITUTION = _load_constitution()
    FORBIDDEN_AXES = set(_CONSTITUTION.protected_parameters)
    PROTECTED_LOWER = _protected_lower_all(_CONSTITUTION)
except Exception:                                    # noqa: BLE001 - 清单缺失/非法时的兜底
    # 兜底取内置集合本身(不是另抄一份):否则内置清单加了项而这里没同步,会静默少拦。
    from risk.constitution import default as _default_constitution
    from risk.constitution import protected_lower_all as _protected_lower_all
    _CONSTITUTION = _default_constitution()
    FORBIDDEN_AXES = set(_CONSTITUTION.protected_parameters)
    PROTECTED_LOWER = _protected_lower_all(_CONSTITUTION)

Runner = Callable[[type, dict, list, float], dict]


class GridError(ValueError):
    pass


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_grid(grid: Any, candidate_cls: type, max_cells: int) -> list[tuple[str, list]]:
    """校验声明的网格,返回按声明顺序的 [(参数名, 取值列表)]。非法一律抛 GridError 并说明原因。"""
    if not isinstance(grid, dict) or len(grid) != AXES:
        raise GridError(f"param_grid 必须恰好 {AXES} 个参数(邻域平滑与高原判定是三维的)")
    allowed = set(getattr(candidate_cls, "parameters", []) or [])
    axes: list[tuple[str, list]] = []
    cells = 1
    for name, values in grid.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise GridError(f"参数名非法: {name!r}")
        if name in FORBIDDEN_AXES or name.lower() in PROTECTED_LOWER:
            raise GridError(f"{name} 属于风控/固定参数,不得进入网格(风控底线永不优化)")
        if name not in allowed:
            raise GridError(f"{name} 不是策略参数:须出现在策略类的 parameters 列表里,否则引擎不会把它传给策略")
        if not isinstance(values, list):
            raise GridError(f"{name} 的取值必须是列表")
        if len(values) < MIN_AXIS_VALUES:
            raise GridError(f"{name} 至少 {MIN_AXIS_VALUES} 个取值(邻域是 ±1 格,太短永远没有完整邻域)")
        if len(values) > MAX_AXIS_VALUES:
            raise GridError(f"{name} 最多 {MAX_AXIS_VALUES} 个取值")
        if not all(_is_number(v) for v in values):
            raise GridError(f"{name} 的取值必须全是有限数值")
        if any(b <= a for a, b in zip(values, values[1:])):
            raise GridError(f"{name} 的取值必须严格递增(相邻格才有「邻域」的含义)")
        axes.append((name, list(values)))
        cells *= len(values)
    if cells > max_cells:
        raise GridError(f"网格共 {cells} 个单元格,超过上限 {max_cells}(config grid.max_cells)")
    return axes


def cells(axes: Sequence[tuple[str, list]]) -> Iterator[tuple[tuple[int, int, int], dict]]:
    (n0, v0), (n1, v1), (n2, v2) = axes
    for i, a in enumerate(v0):
        for j, b in enumerate(v1):
            for k, c in enumerate(v2):
                yield (i, j, k), {n0: a, n1: b, n2: c}


def select_params(*, candidate_cls: type, grid: Any, bars: Sequence, runner: Runner, capital: float, max_cells: int,
                  min_round_trips: int, window: tuple[datetime, datetime]) -> dict:
    start, end = window
    if end >= FIRST_VALIDATION_START:
        raise GridError(f"选参窗口须止于第一个滚动验证窗口({FIRST_VALIDATION_START:%Y-%m-%d})之前,否则验证窗口不再是样本外")
    axes = validate_grid(grid, candidate_cls, max_cells)
    from research_gate import slice_bars
    chunk = slice_bars(bars, start, end)
    if not chunk:
        raise GridError("选参窗口内没有 K 线")

    scores: dict[tuple[int, int, int], float] = {}
    sharpes: dict[tuple[int, int, int], float] = {}
    for idx, params in cells(axes):
        result = runner(candidate_cls, dict(params), chunk, 1.0)
        sharpe = ce.annualized_sharpe(result["daily_pnls"], capital)
        sharpes[idx] = sharpe
        scores[idx] = sharpe if int(result["round_trips"]) >= min_round_trips else NEG_INF

    valid = sum(1 for v in scores.values() if v != NEG_INF)
    smooth = smoothed(scores)
    out: dict = {
        "window": [f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"], "axes": {name: values for name, values in axes},
        "n_cells": len(scores), "valid_cells": valid, "min_round_trips": min_round_trips,
        "best_cell": None, "best_params": None, "best_smoothed": None, "best_sharpe": None, "on_plateau": False,
        "plateau": {"cell": None, "scores": [[*idx, "-inf" if v == NEG_INF else v] for idx, v in sorted(scores.items())]},
        "note": "",
    }
    if not smooth:
        out["note"] = "没有任何参数组合同时满足最少往返笔数与完整邻域,无法选参"
        return out
    best = max(sorted(smooth.items()), key=lambda kv: kv[1])[0]
    params = dict(next(p for i, p in cells(axes) if i == best))
    out.update({"best_cell": list(best), "best_params": params, "best_smoothed": smooth[best], "best_sharpe": sharpes[best],
                "on_plateau": parameter_on_plateau(scores, best)})
    out["plateau"]["cell"] = list(best)
    return out
