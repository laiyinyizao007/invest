"""封存前向留出集防护:2026-06-15 之后的数据对研究回路永远不可见,只在最终确认时由人触发使用一次。

依据 docs/adr/20261113-evolution-engine.md「封存前向留出集」(用户 2026-11-13 确认):
- 15 分钟复权序列停在 2026-06-15 14:45;但日线文件(`*_1d_adj.csv`)已补到 11 月,**越过了封存线**。
  ADR 约定「日线要么截到 2026-06-15,要么在读它的入口显式拒绝越界区间」——本模块提供这个入口检查。
- 交易日口径:20:00 及以后归入下一交易日,所以 2026-06-15 21:00 起的夜盘属于 2026-06-16(封存段)。

一次性使用台账(`config/holdout_usage.json`,入库):按**日期区间**记账,新的使用区间不得与任何已用区间重叠。
不按「每个候选一次」记账——那样 N 个候选就看了 N 次,正是多重检验偏差。录入新数据后,只有之后的全新区间才能再用。
仿照 scripts/segment_ledger.py:先登记后使用、损坏即拒绝(fail-closed)、没有「强制重用」开关。

本模块是方法学闸门的输入,只依赖标准库(研究栈不得 import 交易栈)。
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path

LAST_VISIBLE_TRADING_DAY = date(2026, 6, 15)          # 唯一一处定义
FIRST_SEALED_TRADING_DAY = LAST_VISIBLE_TRADING_DAY + timedelta(days=1)
_NIGHT_CUTOFF = time(20, 0)                            # 与 risk/sessions.NIGHT_CUTOFF 同口径

LEDGER = Path(__file__).resolve().parents[1] / "config" / "holdout_usage.json"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class HoldoutViolation(RuntimeError):
    """研究回路试图读取封存段。"""


class HoldoutAlreadyUsed(RuntimeError):
    """请求的留出区间与已用区间重叠。"""


class LedgerError(RuntimeError):
    """台账无法读取或格式不对:拒绝继续(fail-closed)。"""


def is_sealed(value: date | datetime) -> bool:
    """datetime 按 20:00 日界线归属交易日;date 视为交易日本身。"""
    if isinstance(value, datetime):
        day = value.date() + timedelta(days=1) if value.time() >= _NIGHT_CUTOFF else value.date()
        return day >= FIRST_SEALED_TRADING_DAY
    return value >= FIRST_SEALED_TRADING_DAY


def check_range(start: date, end: date, *, purpose: str) -> None:
    if start > end:
        raise ValueError(f"区间倒置:{start} > {end}")
    if is_sealed(end):
        raise HoldoutViolation(f"{purpose}请求的区间 {start}~{end} 越过了封存线(最后可见交易日 {LAST_VISIBLE_TRADING_DAY}):"
                               "2026-06-15 之后的数据是封存前向留出集,研究回路不可见")


def assert_visible(rows: Iterable, *, key: Callable, purpose: str) -> None:
    sealed = sum(1 for r in rows if is_sealed(key(r)))
    if sealed:
        raise HoldoutViolation(f"{purpose}的数据里有 {sealed} 根落在封存段(最后可见交易日 {LAST_VISIBLE_TRADING_DAY}):拒绝读取")


def split(rows: Iterable, *, key: Callable) -> tuple[list, int]:
    """显式截断:返回 (可见部分, 被截掉的数量)。调用方必须自己决定如何报告被截掉的部分,不许悄悄丢弃。"""
    visible, dropped = [], 0
    for r in rows:
        if is_sealed(key(r)):
            dropped += 1
        else:
            visible.append(r)
    return visible, dropped


def _load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("顶层必须是列表")
        for e in data:
            start, end = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
            # 语义也要对:倒置、起点不在封存段、sha 不完整的记录说明台账被人改坏了
            if start > end or not is_sealed(start) or not _SHA_RE.match(str(e.get("sha256", ""))):
                raise ValueError(f"记录语义无效:{e}")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise LedgerError(f"留出段台账无法读取,拒绝继续(fail-closed):{path}:{exc}") from exc
    return data


def check_unused(start: date, end: date, path: Path | None = None) -> None:
    path = path or LEDGER
    for e in _load(path):
        a, b = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
        if start <= b and a <= end:
            raise HoldoutAlreadyUsed(f"留出区间 {start}~{end} 与已用区间 {a}~{b}({e.get('at', '?')},候选 {e.get('sha256', '?')[:8]})"
                                     "重叠:封存段只能使用一次。录入新数据后,只能使用此后的全新区间")


def record_use(start: date, end: date, *, sha256: str, note: str = "", path: Path | None = None) -> None:
    """先登记、后使用:即使随后的确认流程崩溃,这段数据也算已被看过。"""
    path = path or LEDGER
    if start > end:
        raise ValueError(f"区间倒置:{start} > {end}")
    if not is_sealed(start):
        raise ValueError(f"起点 {start} 不在封存段内(首个封存交易日 {FIRST_SEALED_TRADING_DAY})")
    if not isinstance(sha256, str) or not _SHA_RE.match(sha256):
        raise ValueError("sha256 必须是完整的 64 位小写十六进制")
    check_unused(start, end, path)
    data = _load(path)
    data.append({"start": start.isoformat(), "end": end.isoformat(), "sha256": sha256, "note": note,
                 "at": datetime.now().isoformat(timespec="seconds")})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
