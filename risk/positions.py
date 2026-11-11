"""持仓方向的统一判读(纯逻辑,无 vn.py 运行时依赖)。

风控与对账只认两种方向:LONG(多)、SHORT(空)。vn.py 在净持仓账户上会回报 `Direction.NET`
(方向不明,只有净量)——此前代码里 `direction == LONG else 空` 会把它当成空头,净额与平仓方向都会算反。
这里的原则是 **fail-closed:方向不明就报出来,绝不猜方向**。是否真会出现 NET 取决于账户形态,需要在
SimNow/国信测试环境实测(见 docs/adr/20261109-account-risk-lock.md「净持仓」)。
"""
from collections.abc import Iterable

from vnpy.trader.constant import Direction

DIRECTED = (Direction.LONG, Direction.SHORT)


def is_directed(direction) -> bool:
    return direction in DIRECTED


def undirected(positions: Iterable) -> list:
    """方向不是多/空的持仓(净持仓账户的 NET 等)。"""
    return [p for p in positions if not is_directed(p.direction)]


def signed_net(positions: Iterable, vt_symbol: str) -> float:
    """指定合约的净持仓(多 − 空)。方向不明的持仓**不计入**——调用方必须另用 `undirected` 判定并拒绝/告警。"""
    total = 0.0
    for p in positions:
        if p.vt_symbol != vt_symbol:
            continue
        if p.direction == Direction.LONG:
            total += p.volume
        elif p.direction == Direction.SHORT:
            total -= p.volume
    return total


def describe(positions: Iterable) -> str:
    return ", ".join(f"{p.vt_symbol} {getattr(p.direction, 'value', p.direction)}{p.volume}" for p in positions)
