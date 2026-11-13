"""试验次数账本:记录进化/选参一共"试"过多少次,供 DSR 去通胀使用。

规则见 docs/adr/20261113-evolution-engine.md:
- 每个 param_grid 格子、每次候选提交各计 1 次试验;被淘汰的候选同样计数(幸存者偏差正是 DSR 要惩罚的对象)。
- 只增不减:本模块不提供删除/回退接口。
- 账本损坏或无法读取一律抛 TrialLedgerError(fail-closed):少记等于放水,宁可拒绝评估。
- 追加前先校验整个账本,不在已损坏的账本上继续累计。

阶段 1 只做全局累计(保守:家族划分随阶段 3 的谱系再引入)。同一进程内用锁串行写;跨进程不加锁,
网关与评估作业由父进程串行触发,CLI 并发使用时可能漏记极少量,方向偏向"少记",在 ADR 的已知限制中登记。
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

from risk.dsr import MAX_RUNS_USED, MAX_TRIAL_SHARPE, run_variance

KINDS = ("submission", "grid_cell", "gate_run")
# 阶段 3:账本在记次数之外同时记录每次试验的夏普,供 DSR 用**经验方差**(见 risk/dsr.py)。
# 夏普记录是**独立的一种记录**,不带 count、**不计入试验次数**——次数仍只由上面三种记录决定,
# 这样旧账本原样可读,"次数只增不减"的口径也不受影响。
SHARPES_KIND = "sharpes"
SHARPE_SOURCES = ("grid", "gate")
MAX_SHARPES_PER_ENTRY = 20_000     # 单条记录的夏普个数上限(网格上限远小于它),挡住伪造的巨型样本
MAX_TOTAL_SHARPES = 2_000_000
# 上限:count 过大时 DSR 的尾概率 1-1/N 会舍入成 1.0 并抛 StatisticsError,账本在语法上有效却让评估永久失败
MAX_ENTRY_COUNT = 10**9
MAX_TOTAL = 10**12
_lock = threading.Lock()


class TrialLedgerError(Exception):
    """试验账本损坏、不可读或写入参数非法。"""


def _why(exc: BaseException) -> str:
    """错误的可外传说明:`OSError` 的原文里有本机绝对路径(G7:不得泄露),只给异常类型;别的异常原文照给。"""
    return type(exc).__name__ if isinstance(exc, OSError) else str(exc)


def _check_sharpes_entry(data: dict) -> None:
    values = data.get("values")
    if data.get("source") not in SHARPE_SOURCES:
        raise ValueError("夏普记录的 source 非法")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SHARPES_PER_ENTRY:
        raise ValueError(f"夏普记录的 values 必须是 1..{MAX_SHARPES_PER_ENTRY} 个数")
    for x in values:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > MAX_TRIAL_SHARPE:
            raise ValueError("夏普记录里有非有限数或超出上限的值")


def _entries(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if not isinstance(data, dict):
                raise ValueError("记录不是对象")
            if data.get("kind") == SHARPES_KIND:
                _check_sharpes_entry(data)
                entries.append(data)
                continue
            count = data.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
                raise ValueError(f"count 必须是 1..{MAX_ENTRY_COUNT} 的整数")
            if data.get("kind") not in KINDS:
                raise ValueError("未知的 kind")
            entries.append(data)
        if sum(e["count"] for e in entries if e["kind"] != SHARPES_KIND) > MAX_TOTAL:
            raise ValueError(f"累计试验次数超过上限 {MAX_TOTAL}")
        if sum(len(e["values"]) for e in entries if e["kind"] == SHARPES_KIND) > MAX_TOTAL_SHARPES:
            raise ValueError(f"累计夏普样本超过上限 {MAX_TOTAL_SHARPES}")
    except (OSError, ValueError) as exc:          # JSONDecodeError 是 ValueError 的子类
        raise TrialLedgerError(f"试验账本损坏或无法读取(fail-closed): {_why(exc)}") from exc
    return entries


def _count_of(entries: list[dict]) -> int:
    return sum(e["count"] for e in entries if e["kind"] != SHARPES_KIND)


def total_trials(path: Path) -> int:
    """累计试验次数;账本不存在视为 0,损坏则抛 TrialLedgerError。"""
    return _count_of(_entries(Path(path)))


def record_trials(path: Path, kind: str, count: int, *, at: float) -> int:
    """追加一条试验记录并返回追加后的累计次数。"""
    if kind not in KINDS:
        raise TrialLedgerError(f"未知的试验类型: {kind!r}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
        raise TrialLedgerError(f"试验次数必须是 1..{MAX_ENTRY_COUNT} 的整数")
    path = Path(path)
    with _lock:
        existing = _count_of(_entries(path))      # 先校验,损坏则不追加
        if existing + count > MAX_TOTAL:
            raise TrialLedgerError(f"累计试验次数将超过上限 {MAX_TOTAL}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "count": count, "at": at}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise TrialLedgerError(f"试验账本写入失败(fail-closed): {_why(exc)}") from exc
    return existing + count


def valid_trial_sharpes(values) -> list[float] | None:
    """校验一批要入账的试验夏普(日、非年化):全部是有限数且 |x| ≤ MAX_TRIAL_SHARPE,个数在上限内。
    不合法返回 None——调用方**整批丢弃**而不是部分采纳(样本可能来自候选进程,不能因为混进一个坏值就信剩下的)。"""
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_SHARPES_PER_ENTRY:
        return None
    for x in values:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or abs(x) > MAX_TRIAL_SHARPE:
            return None
    return [float(x) for x in values]


def record_sharpes(path: Path, source: str, values, *, at: float) -> int:
    """追加一条夏普记录(不计入试验次数),返回账本里累计的夏普样本数。非法输入抛 TrialLedgerError。"""
    if source not in SHARPE_SOURCES:
        raise TrialLedgerError(f"未知的夏普来源: {source!r}")
    clean = valid_trial_sharpes(values)
    if clean is None:
        raise TrialLedgerError("试验夏普必须是有限数(|x| ≤ %s),个数 1..%d" % (MAX_TRIAL_SHARPE, MAX_SHARPES_PER_ENTRY))
    path = Path(path)
    with _lock:
        entries = _entries(path)                                 # 先校验,损坏则不追加
        existing = sum(len(e["values"]) for e in entries if e["kind"] == SHARPES_KIND)
        if existing + len(clean) > MAX_TOTAL_SHARPES:
            raise TrialLedgerError(f"累计夏普样本将超过上限 {MAX_TOTAL_SHARPES}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": SHARPES_KIND, "source": source, "values": clean, "at": at},
                                        ensure_ascii=False, allow_nan=False) + chr(10))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise TrialLedgerError(f"试验账本写入失败(fail-closed): {_why(exc)}") from exc
    return existing + len(clean)


def grid_run_variances(path: Path, limit: int = MAX_RUNS_USED) -> list[float]:
    """最近 `limit` 次评估各自的格子日夏普方差,供 DSR 的经验方差(取中位数)用。

    只看 source = "grid" 的夏普记录(门槛评估的单个池化夏普不入——一次评估只有一个数,没有"内部分散度");
    有效格子少于 `MIN_CELLS_PER_RUN` 的评估不入样本。账本不存在视为空列表;损坏抛 TrialLedgerError。"""
    out: list[float] = []
    for entry in _entries(Path(path)):
        if entry["kind"] == SHARPES_KIND and entry["source"] == "grid":
            value = run_variance(entry["values"])
            if value is not None:
                out.append(value)
    return out[-limit:]
