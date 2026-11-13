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
import os
import threading
from pathlib import Path

KINDS = ("submission", "grid_cell", "gate_run")
# 上限:count 过大时 DSR 的尾概率 1-1/N 会舍入成 1.0 并抛 StatisticsError,账本在语法上有效却让评估永久失败
MAX_ENTRY_COUNT = 10**9
MAX_TOTAL = 10**12
_lock = threading.Lock()


class TrialLedgerError(Exception):
    """试验账本损坏、不可读或写入参数非法。"""


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
            count = data.get("count")
            if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
                raise ValueError(f"count 必须是 1..{MAX_ENTRY_COUNT} 的整数")
            if data.get("kind") not in KINDS:
                raise ValueError("未知的 kind")
            entries.append(data)
        if sum(e["count"] for e in entries) > MAX_TOTAL:
            raise ValueError(f"累计试验次数超过上限 {MAX_TOTAL}")
    except (OSError, ValueError) as exc:          # JSONDecodeError 是 ValueError 的子类
        raise TrialLedgerError(f"试验账本损坏或无法读取(fail-closed): {exc}") from exc
    return entries


def total_trials(path: Path) -> int:
    """累计试验次数;账本不存在视为 0,损坏则抛 TrialLedgerError。"""
    return sum(e["count"] for e in _entries(Path(path)))


def record_trials(path: Path, kind: str, count: int, *, at: float) -> int:
    """追加一条试验记录并返回追加后的累计次数。"""
    if kind not in KINDS:
        raise TrialLedgerError(f"未知的试验类型: {kind!r}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
        raise TrialLedgerError(f"试验次数必须是 1..{MAX_ENTRY_COUNT} 的整数")
    path = Path(path)
    with _lock:
        existing = sum(e["count"] for e in _entries(path))      # 先校验,损坏则不追加
        if existing + count > MAX_TOTAL:
            raise TrialLedgerError(f"累计试验次数将超过上限 {MAX_TOTAL}")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "count": count, "at": at}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise TrialLedgerError(f"试验账本写入失败(fail-closed): {exc}") from exc
    return existing + count
