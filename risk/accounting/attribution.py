"""盈亏归因(路线图:净盈亏 = 毛利 − 手续费 − 估计滑点,平仓后拆解)。

成交按(合约, 持仓方向)先进先出配对成往返交易。费率与合约乘数由调用方给出(来自配置;拿到券商真实费率后替换),
表外品种报错——不猜费率。平仓找不到对应开仓时记入 unmatched,不臆造开仓价。
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime

from risk.sessions import TradingCalendar, product_of, trading_day_of


class UnknownCostError(KeyError):
    """费率表里没有这个品种。"""


@dataclass(frozen=True)
class ProductCost:
    fee_per_lot: float      # 每手每边手续费(元)
    size: float             # 合约乘数(吨/手)
    pricetick: float        # 最小变动价位

    def __post_init__(self):
        values = (self.fee_per_lot, self.size, self.pricetick)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values):
            raise ValueError(f"费率参数必须是有限数值:{values}")
        if self.fee_per_lot < 0 or self.size <= 0 or self.pricetick <= 0:
            raise ValueError(f"手续费不得为负,合约乘数与最小变动价位必须为正:{values}")


@dataclass(frozen=True)
class Fill:
    dt: datetime
    symbol: str
    side: str               # 受影响的持仓方向:long / short
    action: str             # open / close
    price: float
    volume: int

    def __post_init__(self):
        if self.side not in ("long", "short") or self.action not in ("open", "close"):
            raise ValueError(f"side 必须是 long/short、action 必须是 open/close:{self.side}/{self.action}")
        if not (isinstance(self.volume, int) and not isinstance(self.volume, bool) and self.volume > 0):
            raise ValueError(f"成交量必须是正整数:{self.volume!r}")
        if not (math.isfinite(self.price) and self.price > 0):
            raise ValueError(f"成交价必须为正:{self.price!r}")


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    side: str
    volume: int
    open_price: float
    close_price: float
    close_dt: datetime
    trading_day: date | None
    gross: float
    fees: float
    est_slippage: float

    @property
    def net(self) -> float:
        return self.gross - self.fees - self.est_slippage


@dataclass
class Attribution:
    round_trips: list[RoundTrip] = field(default_factory=list)
    open_positions: dict = field(default_factory=dict)      # (symbol, side) -> [(price, volume), ...]
    unmatched: list[Fill] = field(default_factory=list)


def _cost(symbol: str, costs: dict[str, ProductCost]) -> ProductCost:
    code = product_of(symbol)
    if code not in costs:
        raise UnknownCostError(f"费率表里没有品种 {code}(合约 {symbol}):不猜费率")
    return costs[code]


def attribute(fills: list[Fill], costs: dict[str, ProductCost], *, est_slippage_ticks: float,
              calendar: TradingCalendar | None = None) -> Attribution:
    if not (math.isfinite(est_slippage_ticks) and est_slippage_ticks >= 0):
        raise ValueError(f"估计滑点必须是非负有限数(负数会把成本变成利润):{est_slippage_ticks!r}")
    out = Attribution()
    books: dict = defaultdict(deque)          # (symbol, side) -> deque[[price, remaining]]
    for f in sorted(fills, key=lambda x: x.dt):
        cost = _cost(f.symbol, costs)
        key = (f.symbol, f.side)
        if f.action == "open":
            books[key].append([f.price, f.volume])
            continue
        remaining = f.volume
        sign = 1 if f.side == "long" else -1
        while remaining and books[key]:
            lot = books[key][0]
            n = min(remaining, lot[1])
            out.round_trips.append(RoundTrip(
                symbol=f.symbol, side=f.side, volume=n, open_price=lot[0], close_price=f.price, close_dt=f.dt,
                trading_day=trading_day_of(f.dt, calendar) if calendar else None,
                gross=sign * (f.price - lot[0]) * cost.size * n,
                fees=cost.fee_per_lot * n * 2,
                est_slippage=est_slippage_ticks * cost.pricetick * cost.size * n * 2))
            lot[1] -= n
            remaining -= n
            if lot[1] == 0:
                books[key].popleft()
        if remaining:
            out.unmatched.append(Fill(f.dt, f.symbol, f.side, f.action, f.price, remaining))
    out.open_positions = {k: [(p, v) for p, v in q] for k, q in books.items() if q}
    return out
