"""参数热力图:评估时把选参窗口的网格得分表另存一份,给面板只读(`data/research/heatmaps/<run_id>.json`)。

规格见 docs/adr/20261113-evolution-engine.md「阶段 3 范围追加」:
- 内容 `axes`、`scores`(`[i,j,k,夏普|"-inf"]`,无效格用字符串 `"-inf"`,**不写** JSON 的 `-Infinity`)、
  `best_cell`、`on_plateau`;
- **数据来自选参窗口**(进化回路放开的那一层),不是验证窗口,所以标 `source = "selection"`,面板会提示"不是样本外表现";
- 得分表**不并入**网关给外层的响应:父进程在把结果交出去之前把它拿出来单独落盘。

读取侧(`dashboard/evolution.py::read_heatmap`)有两个必须对齐的约定,在这里一并满足:
1. 三个轴按**名字排序**后依次作为 x/y/z,得分表里的下标也按这个顺序解释——而 `select_params` 的下标按
   网格**声明顺序**。所以写文件前要把轴与下标一起换成按名字排序的顺序,否则面板画出来是错位的;
2. 无效格必须显式写 `"-inf"`,读取侧把它读成"空"而不是 0。

本模块纯逻辑,不依赖 vn.py;校验口径与读取侧同源(读取侧才是消费者,写出去的东西必须它读得进)。
"""
from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any

MAX_HEATMAP_BYTES = 4 * 1024 * 1024        # 与读取侧一致:超过它读取侧会拒绝
MAX_HEATMAP_CELLS = 200_000
AXES = 3
RUN_ID = re.compile(r"[A-Za-z0-9_-]+")


class HeatmapError(Exception):
    """热力图形状不合法或无法写入。"""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def build_heatmap(selection: dict) -> dict:
    """由 `research_grid.select_params` 的完整输出(含 `plateau.scores`)生成热力图正文。

    `selection["axes"]` 是 {参数名: 取值列表}(声明顺序),`plateau.scores` 是 `[i, j, k, 夏普|"-inf"]`。
    返回的轴与下标已换成**按名字排序**的顺序(见模块文件头)。"""
    axes = selection.get("axes")
    plateau = selection.get("plateau")
    if not isinstance(axes, dict) or len(axes) != AXES or not isinstance(plateau, dict):
        raise HeatmapError("选参结果缺少三维 axes 或 plateau 得分表")
    declared = list(axes)
    order = sorted(declared)
    permutation = [declared.index(name) for name in order]          # 排序后第 n 个轴 = 声明顺序里的第几个

    def reorder(index: list) -> list[int]:
        return [index[p] for p in permutation]

    scores = []
    for item in plateau.get("scores", []):
        if not isinstance(item, (list, tuple)) or len(item) != AXES + 1:
            raise HeatmapError("得分表的行必须是 [i, j, k, 得分]")
        value = item[3]
        if value == "-inf" or value is None or (isinstance(value, float) and not math.isfinite(value)):
            value = "-inf"                                           # 无效格显式为空,不写 JSON 的 -Infinity/NaN
        elif not _is_number(value):
            raise HeatmapError("得分必须是有限数或字符串 \"-inf\"")
        scores.append([*reorder(list(item[:AXES])), value])
    scores.sort(key=lambda row: tuple(row[:AXES]))
    best = selection.get("best_cell")
    body = {
        "axes": {name: list(axes[name]) for name in order},
        "scores": scores,
        "best_cell": None if best is None else reorder(list(best)),
        "on_plateau": bool(selection.get("on_plateau")),
        "window": list(selection["window"]) if isinstance(selection.get("window"), (list, tuple)) else None,
        "source": "selection",
    }
    validate_heatmap(body)
    return body


def validate_heatmap(body: Any) -> None:
    """按读取侧的口径校验(写出去的必须它读得进)。不合法抛 HeatmapError。"""
    if not isinstance(body, dict):
        raise HeatmapError("热力图不是对象")
    axes, scores = body.get("axes"), body.get("scores")
    if not isinstance(axes, dict) or len(axes) != AXES or not isinstance(scores, list):
        raise HeatmapError("热力图缺少三个轴或得分表")
    if len(scores) > MAX_HEATMAP_CELLS:
        raise HeatmapError("热力图格子数超过上限")
    names = sorted(axes)
    if list(axes) != names:
        raise HeatmapError("轴必须按名字排序(读取侧按排序后的名字解释下标)")
    for name in names:
        values = axes[name]
        if not isinstance(values, list) or not values or not all(_is_number(v) for v in values):
            raise HeatmapError("热力图的坐标轴取值非法")
    limits = [len(axes[name]) for name in names]
    seen: set[tuple[int, ...]] = set()
    for item in scores:
        if not isinstance(item, list) or len(item) != AXES + 1:
            raise HeatmapError("得分表的行必须是 [i, j, k, 得分]")
        index = tuple(item[:AXES])
        if any(isinstance(i, bool) or not isinstance(i, int) for i in index):
            raise HeatmapError("得分表下标必须是整数")
        if any(i < 0 or i >= limit for i, limit in zip(index, limits)):
            raise HeatmapError("得分表下标越界")
        if index in seen:
            raise HeatmapError("得分表有重复的格子")
        seen.add(index)
        value = item[3]
        if not (value == "-inf" or _is_number(value)):
            raise HeatmapError("得分必须是有限数或字符串 \"-inf\"")
    if len(seen) != limits[0] * limits[1] * limits[2]:
        raise HeatmapError("得分表的格子数与坐标轴长度对不上(网格必须是完整的)")
    best = body.get("best_cell")
    if best is not None:
        if (not isinstance(best, list) or len(best) != AXES
                or any(isinstance(i, bool) or not isinstance(i, int) or not 0 <= i < limit for i, limit in zip(best, limits))):
            raise HeatmapError("best_cell 非法或越界")
    if not isinstance(body.get("on_plateau"), bool):
        raise HeatmapError("on_plateau 必须是布尔值")


def save_heatmap(directory: Path, run_id: str, body: dict) -> Path:
    """原子写入 `<directory>/<run_id>.json`。run_id 不合法、已存在、超大、写入失败都抛 HeatmapError。

    不覆盖已有文件:同一个 run_id 的热力图是那次评估的记录,事后不能被改写。"""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id) or len(run_id) > 64:
        raise HeatmapError("run_id 不合法")
    validate_heatmap(body)
    try:
        text = json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError as exc:                                        # 含 NaN/inf(allow_nan=False)
        raise HeatmapError(f"热力图含非有限数: {exc}") from exc
    if len(text.encode("utf-8")) > MAX_HEATMAP_BYTES:
        raise HeatmapError("热力图超过大小上限")
    directory = Path(directory)
    target = directory / f"{run_id}.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise HeatmapError("该 run_id 的热力图已存在,不覆盖")
        tmp = directory / f".{run_id}.json.tmp"
        with open(tmp, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, target)
    except OSError as exc:
        raise HeatmapError(f"热力图写入失败: {exc}") from exc
    return target
