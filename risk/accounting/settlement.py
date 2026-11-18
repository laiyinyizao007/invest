"""结算单核对(路线图:结算单与查询到的持仓比对,不一致告警)。

输入是**已解析**的结算单持仓与查询到的持仓,键为(合约, long/short),值为手数;合约代码大小写不敏感,0 手视为不存在。
取结算单内容(vn.py 的 CTP 接口目前只会确认、不会查询结算单)属 F-b 的网关扩展。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionDiff:
    symbol: str
    side: str
    statement: int
    queried: int


def _normalize(rows: dict) -> dict:
    out: dict = {}
    for (symbol, side), volume in rows.items():
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise ValueError(f"手数必须是非负整数:{symbol} {side} {volume!r}(小数会被截断、负数无意义,一律拒绝)")
        if side not in ("long", "short"):
            raise ValueError(f"持仓方向必须是 long/short:{side!r}")
        if volume:
            key = (symbol.strip().lower(), side)
            out[key] = out.get(key, 0) + volume
    return out


def reconcile(statement: dict, queried: dict) -> list[PositionDiff]:
    a, b = _normalize(statement), _normalize(queried)
    keys = sorted(set(a) | set(b))
    return [PositionDiff(sym, side, a.get((sym, side), 0), b.get((sym, side), 0))
            for sym, side in keys if a.get((sym, side), 0) != b.get((sym, side), 0)]
