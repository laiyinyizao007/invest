"""进化引擎的只读读取侧:谱系台账、参数热力图、代码与差异。

数据由**引擎侧**(毕昇工作流与研究网关)写入,面板只读:
- 谱系台账 `data/research/evolution/<campaign_id>.jsonl`:每行一条代记录;
- 参数热力图 `data/research/heatmaps/<run_id>.json`:某个评估运行的三维网格得分。

核心原则与其它区块一致:**缺失/过期/损坏绝不显示成正常**。可选子系统(进化)未部署时不影响总体级别。
"""
from __future__ import annotations

import difflib
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dashboard import models as m

MAX_LEDGER_BYTES = 8 * 1024 * 1024        # 谱系台账总上限
MAX_LEDGER_LINES = 20_000          # 有效记录数上限
MAX_LEDGER_TOTAL_LINES = 200_000    # 总行数上限(含空行):挡住『几百万空行』这种文件
MAX_CODE_BYTES = 256 * 1024               # 单份候选源码上限(与待审候选一致)
MAX_HEATMAP_BYTES = 4 * 1024 * 1024
MAX_HEATMAP_CELLS = 200_000
CAMPAIGN_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
SHA256 = r"[0-9a-f]{64}"

class EvolutionError(Exception):
    """读取失败。消息里不含本机路径。"""


@dataclass
class Record:
    """一条代记录。字段按引擎侧约定(campaign_id / generation / …)。"""

    generation: int
    candidate_sha256: str
    parent_sha256: str | None
    run_id: str | None
    status: str | None
    sharpe: float | None
    total_return: float | None
    max_drawdown: float | None
    dsr: float | None
    n_trials: int | None
    fail_reason: str | None
    reflection: str | None
    created_at: str | None
    cell: str | None
    diagnosis: dict | None

    def brief(self) -> dict:
        """给前端的精简视图。**不含**反思原文以外的自由文本;reflection 单独字段、限长。"""
        return {
            "generation": self.generation,
            "candidate_sha256": self.candidate_sha256,
            "parent_sha256": self.parent_sha256,
            "run_id": self.run_id,
            "status": self.status,
            "sharpe": self.sharpe,
            "total_return": self.total_return,
            "max_drawdown": self.max_drawdown,
            "dsr": self.dsr,
            "n_trials": self.n_trials,
            "fail_reason": self.fail_reason,
            "cell": self.cell,
            "diagnosis": self.diagnosis,
        }


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _text(value: Any, limit: int = 2000) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def parse_record(row: Any) -> Record:
    """校验一行台账。不符合约定的行**直接拒绝**(不是尽量显示):台账是引擎侧写的,形状不对说明写坏了。"""
    if not isinstance(row, dict):
        raise EvolutionError("台账记录不是对象")
    generation = row.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise EvolutionError("台账记录的 generation 非法")
    sha = row.get("candidate_sha256")
    if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise EvolutionError("台账记录的 candidate_sha256 非法")
    n_trials = row.get("n_trials")
    if n_trials is not None and (not isinstance(n_trials, int) or isinstance(n_trials, bool) or n_trials < 0):
        raise EvolutionError("台账记录的 n_trials 非法")
    diagnosis = row.get("diagnosis")
    parent = row.get("parent_sha256")
    if parent is not None:
        # 父代哈希要么没有,要么是合法的 64 位十六进制。**不截断**:截断出来的哈希是废的,
        # 前端拿它去请求差异会得到 404,看起来像『父代源码不在』。
        if not isinstance(parent, str) or len(parent) != 64 or any(c not in "0123456789abcdef" for c in parent):
            raise EvolutionError("台账记录的 parent_sha256 非法")
    return Record(
        generation=generation,
        candidate_sha256=sha,
        parent_sha256=parent,
        run_id=_text(row.get("run_id"), 32),
        status=_text(row.get("status"), 64),
        sharpe=_num(row.get("sharpe")),
        total_return=_num(row.get("total_return")),
        max_drawdown=_num(row.get("max_drawdown")),
        dsr=_num(row.get("dsr")),
        n_trials=n_trials,
        fail_reason=_text(row.get("fail_reason")),
        reflection=_text(row.get("reflection")),
        created_at=_text(row.get("created_at"), 40),
        cell=_text(row.get("cell"), 64),
        diagnosis=diagnosis if isinstance(diagnosis, dict) else None,
    )


def read_campaign(directory: Path, campaign_id: str) -> list[Record]:
    """读一个战役的台账。文件不存在 → 空列表(调用方按 missing 处理);坏行 → 抛错(按 corrupt 处理)。"""
    path = directory / f"{campaign_id}.jsonl"
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise EvolutionError("台账无法读取") from exc
    if size > MAX_LEDGER_BYTES:
        raise EvolutionError("台账超过大小上限")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvolutionError("台账无法读取") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvolutionError("台账不是合法 UTF-8") from exc
    rows: list[Record] = []
    lines_seen = 0
    start = 0
    while start <= len(text):                      # 逐行扫描:一次性 splitlines() 会把 8 MB 的空行文件变成八百万个列表元素
        end = text.find("\n", start)
        if end < 0:
            end = len(text)
        line = text[start:end]
        start = end + 1
        lines_seen += 1
        if lines_seen > MAX_LEDGER_TOTAL_LINES:
            raise EvolutionError("台账总行数超过上限")
        if not line.strip():
            continue
        if len(rows) >= MAX_LEDGER_LINES:
            raise EvolutionError("台账行数超过上限")
        try:
            rows.append(parse_record(m.loads(line)))
        except (ValueError, RecursionError) as exc:
            # RecursionError:深层嵌套的 JSON 会抛它(不是 ValueError)——不该漏成未处理异常
            raise EvolutionError("台账里有无法解析的行") from exc
    rows.sort(key=lambda r: (r.generation, r.created_at or ""))
    return rows


def campaigns(directory: Path) -> list[str]:
    """战役列表(文件名即战役号)。只取直接位于目录内、名字合规的文件。

    **按修改时间倒序**(新的在前):超出一次统计上限时被略过的是**最旧**的那些,
    而不是按名字排在后面的那些——否则『未统计』可能正好落在最新的战役上。
    """
    if not directory.is_dir():
        return []
    out: list[tuple[float, str]] = []
    for path in directory.glob("*.jsonl"):
        if path.is_symlink():
            continue
        name = path.stem
        if not name or len(name) > 64 or not all(c.isalnum() or c in "._-" for c in name):
            continue
        try:
            if path.resolve(strict=True).parent != directory.resolve():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        out.append((mtime, name))
    return [name for _, name in sorted(out, key=lambda x: (-x[0], x[1]))]


def _percentile(values: list[float], q: float) -> float | None:
    """分位数(线性插值)。空样本返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return round(ordered[int(position)], 4)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight, 4)


def curve(records: list[Record], *, stagnant_generations: int, stagnant_epsilon: float,
          overfit_mean_gap: float) -> dict:
    """进化曲线:按代聚合种群的夏普分布与收益。

    每代给出:精英(最高夏普)、均值、P25、P75、最好收益与最大回撤,以及该代的候选数。
    **提示标志由服务端算**(前端不重算):
    - `stagnant`:连续 N 代精英夏普没有超过历史最好值 + 一点点余量;
    - `overfit_suspect`:某一代的精英夏普与种群均值差距超过阈值(只是**提示**,不是结论)。
    """
    by_generation: dict[int, list[Record]] = {}
    for record in records:
        by_generation.setdefault(record.generation, []).append(record)
    points = []
    for generation in sorted(by_generation):
        group = by_generation[generation]
        sharpes = [r.sharpe for r in group if r.sharpe is not None]
        returns = [r.total_return for r in group if r.total_return is not None]
        drawdowns = [r.max_drawdown for r in group if r.max_drawdown is not None]
        elite = max(sharpes) if sharpes else None
        mean = round(sum(sharpes) / len(sharpes), 4) if sharpes else None
        points.append({
            "generation": generation,
            "count": len(group),
            "elite_sharpe": None if elite is None else round(elite, 4),
            "mean_sharpe": mean,
            "p25_sharpe": _percentile(sharpes, 0.25),
            "p75_sharpe": _percentile(sharpes, 0.75),
            "best_total_return": None if not returns else round(max(returns), 4),
            "worst_max_drawdown": None if not drawdowns else round(max(drawdowns), 4),
            "cells": sorted({r.cell for r in group if r.cell}),
        })
    flags: list[dict] = []
    if len(points) >= stagnant_generations:
        tail = points[-stagnant_generations:]
        # 基准取**窗口之前**的历史最好。拿全局最好当基准是错的:窗口内的最好值恰好等于全局最好时,
        # 『窗口内每代都没超过全局最好』恒成立 —— 一个正在进步的战役也会被判成停滞(文案与事实相反)。
        before = points[:-stagnant_generations]
        baseline = max((p["elite_sharpe"] for p in before if p["elite_sharpe"] is not None), default=None)
        window_best = max((p["elite_sharpe"] for p in tail if p["elite_sharpe"] is not None), default=None)
        if baseline is None:
            # 战役开头就没有更早的代可比较:用窗口第一代当基准(从头到尾一样平,也是停滞)
            baseline = tail[0]["elite_sharpe"]
        # 『连续 N 代』要求代号是**连续的**:代号跳号(1、7、30)说明中间有代没跑,
        # 那不是连续 N 代停滞,只是记录稀疏。稀疏时不给停滞提示(宁可不说,不说错)。
        contiguous = all(tail[i]["generation"] == tail[i - 1]["generation"] + 1 for i in range(1, len(tail)))
        if (baseline is not None and window_best is not None and contiguous
                and window_best <= baseline + stagnant_epsilon
                and all(p["elite_sharpe"] is not None for p in tail)):
            flags.append({"kind": "stagnant", "text": f"连续 {stagnant_generations} 代精英夏普没有提升(窗口前最高 {baseline})",
                          "level": "warn"})
    for point in points:
        if point["elite_sharpe"] is not None and point["mean_sharpe"] is not None:
            gap = point["elite_sharpe"] - point["mean_sharpe"]
            if gap >= overfit_mean_gap:
                flags.append({"kind": "overfit_suspect", "level": "warn",
                              "text": f"第 {point['generation']} 代精英夏普比种群均值高 {round(gap, 2)}(≥ {overfit_mean_gap}):可能是个别候选的偶然高分,请看热力图判断是否稳健"})
                break
    return {"points": points, "flags": flags, "generations": len(points),
            "latest_generation": points[-1]["generation"] if points else None}


def _really_inside(path: Path, directory: Path) -> bool:
    """文件解析后的真实位置必须直接位于 directory 内。符号链接/junction 指向目录外的一律不算。"""
    try:
        return path.resolve(strict=True).parent == directory.resolve()
    except OSError:
        return False


def read_heatmap(directory: Path, run_id: str) -> dict | None:
    """读某个运行的参数网格得分表,并切成二维(第三轴取 best_cell)。文件不存在返回 None。"""
    path = directory / f"{run_id}.json"
    if not path.exists():
        return None
    if not _really_inside(path, directory):
        raise EvolutionError("热力图文件是链接,指向目录之外")     # 与研究结果文件同一口径:不读目录外的文件
    try:
        if path.stat().st_size > MAX_HEATMAP_BYTES:
            raise EvolutionError("热力图文件超过大小上限")
        raw = path.read_bytes()
    except OSError as exc:
        raise EvolutionError("热力图无法读取") from exc
    try:
        body = m.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EvolutionError("热力图文件不是合法 JSON") from exc
    if not isinstance(body, dict):
        raise EvolutionError("热力图文件不是对象")
    axes = body.get("axes")
    scores = body.get("scores")
    if not isinstance(axes, dict) or not isinstance(scores, list):
        raise EvolutionError("热力图缺少 axes 或 scores")
    if len(axes) != 3 or len(scores) > MAX_HEATMAP_CELLS:
        raise EvolutionError("热力图的形状不符")
    names = sorted(axes)
    for name in names:
        values = axes[name]
        if not isinstance(values, list) or not values or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in values):
            raise EvolutionError("热力图的坐标轴取值非法")
    limits = [len(axes[name]) for name in names]        # 每个轴的下标范围
    cells: dict[tuple[int, ...], float | None] = {}
    for item in scores:
        if not isinstance(item, list) or len(item) != 4:
            raise EvolutionError("热力图的得分表形状不符")
        index = item[:3]
        if any(not isinstance(i, int) or isinstance(i, bool) for i in index):
            raise EvolutionError("热力图的得分表下标非法")
        if any(i < 0 or i >= limit for i, limit in zip(index, limits)):
            raise EvolutionError("热力图的得分表下标越界(与坐标轴长度不符)")
        value = item[3]
        if isinstance(value, str) and value == "-inf":
            cells[tuple(index)] = None               # 无效格子:显式为空,不画成 0
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            cells[tuple(index)] = float(value)
        else:
            raise EvolutionError("热力图的得分非法")
    best_cell = body.get("best_cell")
    if not (isinstance(best_cell, list) and len(best_cell) == 3):
        best_cell = None
    elif any(not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= limit for i, limit in zip(best_cell, limits)):
        raise EvolutionError("热力图的 best_cell 越界")
    # 三维 → 二维:第三轴固定在 best_cell 的取值上(没有 best_cell 时取第一格)
    third = 0 if best_cell is None else int(best_cell[2])
    if len(axes[names[0]]) * len(axes[names[1]]) * len(axes[names[2]]) != len(cells):
        raise EvolutionError("热力图的格子数与坐标轴长度对不上")
    x_name, y_name = names[0], names[1]
    x_values, y_values = axes[x_name], axes[y_name]
    grid = []
    for yi in range(len(y_values)):
        row = []
        for xi in range(len(x_values)):
            row.append(cells.get((xi, yi, third)))
        grid.append(row)
    valid = sum(1 for v in cells.values() if v is not None)
    return {
        "run_id": run_id,
        "axes": {"x": {"name": x_name, "values": x_values}, "y": {"name": y_name, "values": y_values},
                 "z": {"name": names[2], "values": axes[names[2]], "selected": third}},
        "grid": grid,
        "best_cell": best_cell,
        "on_plateau": body.get("on_plateau") if isinstance(body.get("on_plateau"), bool) else None,
        "valid_cells": valid,
        "n_cells": len(cells),
        "source_window": body.get("window") if isinstance(body.get("window"), list) else None,
        "note": "分数来自选参窗口,不是样本外表现" if body.get("source") != "validation" else None,
    }


def candidate_source(directory: Path, sha256: str) -> str | None:
    """按哈希取候选源码。目录里找不到就返回 None(不猜、不按名字找)。

    **不做缓存**:候选只有几十个、每个 ≤256 KB,读一遍算哈希也就几十毫秒;
    而按元数据(大小/mtime)做缓存会在『同长度改写并恢复 mtime』后返回旧源码——审阅接口给出
    磁盘上已经不存在的代码,比慢更糟。
    """
    return _scan_candidate_source(directory, sha256)


def _scan_candidate_source(directory: Path, sha256: str) -> str | None:
    """按哈希找候选源码:读遍目录里的 .py,比对内容哈希。**不按名字找**。"""
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        return None
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.py")):
        if path.is_symlink():
            continue
        try:
            if path.resolve(strict=True).parent != directory.resolve():
                continue
            if path.stat().st_size > MAX_CODE_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() == sha256:
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
    return None


def diff_text(before: str, after: str, *, context: int = 3) -> str:
    """统一 diff(服务端算,前端只显示)。"""
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile="parent", tofile="candidate", n=context))
