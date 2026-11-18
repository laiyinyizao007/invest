"""权益历史:按时间只追加的 jsonl(路线图「权益曲线」的数据源;交易进程定期写入属 F-b)。

每行一条 {"ts": ISO 时间, "equity": 数值}。写入要求时间严格递增、数值有限;
读取时跳过损坏或乱序的行并**计数**(调用方据此提示「有 N 行读不出来」,而不是当作完整)。

两种写法,接受规则完全相同(都以 read() 为准,保证写进去的每一行 read() 都会接受):
- `append(path, ...)`:每次整文件读取再追加。简单、无状态,适合低频或一次性写入;
- `EquityWriter(path)`:打开时整文件读**一次**,之后在内存里记住最后时间,每次追加 O(1)。
  **单写者约定**:同一文件只能由一个进程写(F-b 接线时由交易进程独占,进程锁保证)。
  G8 复核曾指出「只读文件尾部」的快路径在旧位置藏着坏行时与 read() 结论不一致,所以不再用尾部推断。
"""
from __future__ import annotations

import json
import math
import os
import threading
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


def last_point(path: Path | str) -> EquityPoint | None:
    """最后一条被 read() 接受的记录(整文件读,与 read() 同一规则)。"""
    records = read(path).records
    return records[-1] if records else None


def _check(ts: datetime, equity: float, last: EquityPoint | None) -> None:
    if not _finite_number(equity):
        raise ValueError(f"权益必须是有限数值:{equity!r}")
    if last is not None and ts <= last.ts:
        raise ValueError(f"时间必须严格递增:{ts} <= 上一条 {last.ts}")


def _write_line(path: Path, ts: datetime, equity: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": ts.isoformat(timespec="seconds"), "equity": float(equity)}) + "\n"
    with open(path, "a", encoding="utf-8") as fh:          # 单次写入一整行并落盘
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())


def append(path: Path | str, ts: datetime, equity: float) -> None:
    path = Path(path)
    _check(ts, equity, last_point(path))
    _write_line(path, ts, equity)


class EquityWriter:
    """高频追加用:打开时整文件读一次,之后内存缓存最后时间。

    跨进程靠单写者约定(F-b 由交易进程独占并持进程锁);同进程多线程由内部锁保证「检查 → 写入 → 更新」不交错。
    `skipped` 是打开时已有文件里被 read() 跳过的行数:大于 0 说明文件曾被损坏或手工改动,调用方应告警。
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        hist = read(self.path)
        self.last = hist.records[-1] if hist.records else None
        self.skipped = hist.skipped
        self._lock = threading.Lock()

    def append(self, ts: datetime, equity: float) -> None:
        with self._lock:
            _check(ts, equity, self.last)
            _write_line(self.path, ts, equity)
            self.last = EquityPoint(ts, float(equity))
