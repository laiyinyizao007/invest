"""策略层风控:纯 Python,不依赖 vn.py,便于单元测试。

vnpy_riskmanager 只管流控类限制(单笔手数、撤单数等);
"单笔亏损预算 / 止损只收紧不放松 / 当日连亏熔断"必须在策略层自己实现,由本模块提供。
"""
from collections.abc import Hashable


def stop_distance(atr: float, atr_mult: float) -> float:
    """止损距离(价格点数)= ATR × 倍数。"""
    return atr * atr_mult


def risk_per_trade(stop_dist: float, size: float, volume: float = 1) -> float:
    """触及止损时单笔的价格亏损金额(元),不含手续费与滑点。"""
    return stop_dist * size * volume


def within_budget(stop_dist: float, size: float, budget: float, volume: float = 1) -> bool:
    """单笔最大亏损是否在预算内。超预算则这笔交易不做(而不是缩小止损去迁就预算)。"""
    return risk_per_trade(stop_dist, size, volume) <= budget


class TrailingStop:
    """只收紧、不放松的移动止损(chandelier 型):多头 = 持仓期最高价 - 距离。

    "止损永远不允许往亏损方向移动"是这里的硬约束:
    即使 ATR 变大使候选止损价更差,也保持原有止损价不变。
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.side: int = 0          # 1 多 / -1 空 / 0 空仓
        self.extreme: float = 0.0   # 持仓以来的最高价(多)/最低价(空)
        self.stop: float = 0.0

    def open(self, side: int, entry_price: float, dist: float) -> float:
        if side not in (1, -1):
            raise ValueError("side 必须是 1(多)或 -1(空)")
        if dist <= 0:
            raise ValueError("止损距离必须为正")
        self.side = side
        self.extreme = entry_price
        self.stop = entry_price - side * dist
        return self.stop

    def update(self, high: float, low: float, dist: float) -> float:
        """用新 K 线的极值收紧止损,返回当前止损价。"""
        if self.side == 0:
            raise RuntimeError("未开仓,无法更新止损")
        if self.side > 0:
            self.extreme = max(self.extreme, high)
            self.stop = max(self.stop, self.extreme - dist)
        else:
            self.extreme = min(self.extreme, low)
            self.stop = min(self.stop, self.extreme + dist)
        return self.stop


class DailyLossGuard:
    """当日连亏熔断:连续亏损达到 max_losses 笔后,当日禁止开仓;换日自动重置。"""

    def __init__(self, max_losses: int = 2) -> None:
        if max_losses < 1:
            raise ValueError("max_losses 必须 >= 1")
        self.max_losses = max_losses
        self._day: Hashable | None = None
        self._losses = 0

    def _roll(self, day: Hashable) -> None:
        if day != self._day:
            self._day = day
            self._losses = 0

    def on_trade_closed(self, day: Hashable, pnl: float) -> None:
        """记录一笔平仓结果。盈利(pnl > 0)清零连亏计数,亏损或持平不清零。"""
        self._roll(day)
        self._losses = 0 if pnl > 0 else self._losses + 1

    def can_open(self, day: Hashable) -> bool:
        self._roll(day)
        return self._losses < self.max_losses
