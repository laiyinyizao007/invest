"""权益历史:按时间只追加的 jsonl(路线图「权益曲线」的数据源;交易进程定期写入属 F-b)。

每行一条 {"ts": ISO 时间, "equity": 数值}。写入要求时间严格递增、数值有限;
读取时跳过损坏的行并**计数**(调用方据此提示「有 N 行读不出来」,而不是当作完整)。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class EquityPoint:
    ts: datetime
    equity: float


@dataclass(frozen=True)
class EquityHistory:
    records: list[EquityPoint]
    skipped: int


def _finite_number(value) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _parse(line: str) -> EquityPoint | None:
    try:
        raw = json.loads(line)
        ts, eq = datetime.fromisoformat(raw["ts"]), raw["equity"]
    except (ValueError, TypeError, KeyError):
        return None
    return EquityPoint(ts, float(eq)) if _finite_number(eq) else None


def read(path: Path | str) -> EquityHistory:
    path = Path(path)
    if not path.exists():
        return EquityHistory([], 0)
    records: list[EquityPoint] = []
    skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        point = _parse(line)
        if point is None or (records and point.ts <= records[-1].ts):
            skipped += 1
            continue
        records.append(point)
    return EquityHistory(records, skipped)


def append(path: Path | str, ts: datetime, equity: float) -> None:
    if not _finite_number(equity):
        raise ValueError(f"权益必须是有限数值:{equity!r}")
    path = Path(path)
    hist = read(path)
    if hist.records and ts <= hist.records[-1].ts:
        raise ValueError(f"时间必须严格递增:{ts} <= 上一条 {hist.records[-1].ts}")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": ts.isoformat(timespec="seconds"), "equity": float(equity)}) + "\n"
    with open(path, "a", encoding="utf-8") as fh:          # 单次写入一整行并落盘
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
