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


_TAIL_BYTES = 64 * 1024


def last_point(path: Path | str) -> EquityPoint | None:
    """最后一条被 read() 接受的记录。只读文件末尾一段:按分钟写一年约 50 万行,每次追加都整文件重读会越来越慢。

    接受规则与 read() 相同(可解析且时间严格递增)。尾部里的有效行只要不是严格递增(说明有乱序或手工改动)、
    或者一条有效行都没有,就退回整文件读取——宁可慢,也不能与 read() 的结论不一致
    (否则会在乱序行之后追加,而 read() 又把新记录当乱序跳过,新写的权益悄悄丢失)。
    残余:乱序行若藏在尾部窗口之前且时间比尾部全部记录都晚,这里看不出来;append 本身保证递增,
    只有人工改文件或损坏才会出现,read() 的 skipped 计数会把它暴露出来。
    """
    path = Path(path)
    if not path.exists():
        return None
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - _TAIL_BYTES))
        tail = fh.read().decode("utf-8", errors="replace")
    lines = tail.splitlines()
    if size > _TAIL_BYTES:
        lines = lines[1:]                       # 第一行可能被截断
    points = [pt for pt in (_parse(line) for line in lines if line.strip()) if pt is not None]
    if points and all(a.ts < b.ts for a, b in zip(points, points[1:])):
        return points[-1]
    full = read(path).records
    return full[-1] if full else None


def append(path: Path | str, ts: datetime, equity: float) -> None:
    """追加一条。**单写者约定**:同一文件只能由一个进程写(F-b 接线时由交易进程独占,进程锁保证),
    否则两个进程可能同时通过递增检查。"""
    if not _finite_number(equity):
        raise ValueError(f"权益必须是有限数值:{equity!r}")
    path = Path(path)
    last = last_point(path)
    if last is not None and ts <= last.ts:
        raise ValueError(f"时间必须严格递增:{ts} <= 上一条 {last.ts}")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"ts": ts.isoformat(timespec="seconds"), "equity": float(equity)}) + "\n"
    with open(path, "a", encoding="utf-8") as fh:          # 单次写入一整行并落盘
        fh.write(line)
        fh.flush()
        os.fsync(fh.fileno())
