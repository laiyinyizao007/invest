"""真实滑点对比(资金与成本 ADR 的假设 H1:滑点是主要成本,需逐笔核实)。

比较「下单时的对手盘口价」与成交均价,以最小变动价位计,**对我不利为正**;按成交量加权汇总。
下单时没有记录盘口价的委托跳过并计数,不猜参照价。
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class OrderRef:
    order_id: str
    side: str                    # buy / sell
    reference_price: float | None
    pricetick: float

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side 必须是 buy/sell:{self.side!r}")
        if not (math.isfinite(self.pricetick) and self.pricetick > 0):
            raise ValueError(f"最小变动价位必须为正的有限数:{self.pricetick!r}")
        if self.reference_price is not None and not (math.isfinite(self.reference_price) and self.reference_price > 0):
            raise ValueError(f"参照价必须为正的有限数或 None:{self.reference_price!r}")


@dataclass(frozen=True)
class SlippageRow:
    order_id: str
    ticks: float
    volume: int


@dataclass(frozen=True)
class SlippageReport:
    rows: list[SlippageRow]
    skipped: int
    mean_ticks: float | None


def compare(orders: list[OrderRef], fills: dict[str, list[tuple[float, int]]]) -> SlippageReport:
    rows: list[SlippageRow] = []
    skipped = 0
    for o in orders:
        done = fills.get(o.order_id) or []
        for price, vol in done:
            if isinstance(vol, bool) or not isinstance(vol, int) or vol <= 0:
                raise ValueError(f"委托 {o.order_id} 的成交量必须是正整数:{vol!r}")
            if not (isinstance(price, (int, float)) and math.isfinite(price) and price > 0):
                raise ValueError(f"委托 {o.order_id} 的成交价必须是正的有限数:{price!r}")
        volume = sum(v for _, v in done)
        if not volume:
            continue
        if o.reference_price is None:
            skipped += 1
            continue
        avg = sum(p * v for p, v in done) / volume
        diff = avg - o.reference_price if o.side == "buy" else o.reference_price - avg
        rows.append(SlippageRow(o.order_id, diff / o.pricetick, volume))
    total = sum(r.volume for r in rows)
    mean = sum(r.ticks * r.volume for r in rows) / total if total else None
    return SlippageReport(rows, skipped, mean)
