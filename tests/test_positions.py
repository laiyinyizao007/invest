"""持仓方向判读:净持仓(NET)方向不明时 fail-closed,不当成空头。"""
from types import SimpleNamespace

from risk.positions import describe, is_directed, signed_net, undirected
from vnpy.trader.constant import Direction


def pos(direction, volume, symbol="c2601.DCE"):
    return SimpleNamespace(vt_symbol=symbol, direction=direction, volume=volume)


def test_signed_net_counts_long_minus_short_for_the_symbol_only():
    ps = [pos(Direction.LONG, 3), pos(Direction.SHORT, 1), pos(Direction.LONG, 9, "x.DCE")]
    assert signed_net(ps, "c2601.DCE") == 2


def test_net_direction_is_not_counted_and_is_reported_as_undirected():
    net = pos(Direction.NET, 2)
    assert signed_net([net], "c2601.DCE") == 0                 # 不被当成空头算成 -2
    assert undirected([pos(Direction.LONG, 1), net]) == [net]
    assert not is_directed(Direction.NET) and is_directed(Direction.SHORT)


def test_describe_lists_symbol_direction_volume():
    assert "c2601.DCE" in describe([pos(Direction.NET, 2)])


def test_describe_tolerates_missing_direction_and_lock_positions_net_to_zero():
    assert "None" in describe([pos(None, 1)])                   # direction=None 不能让 fail-closed 路径自己抛异常
    assert undirected([pos(None, 1)]) != []
    assert signed_net([pos(Direction.LONG, 1), pos(Direction.SHORT, 1)], "c2601.DCE") == 0   # 锁仓净额为 0(既有口径)
    assert signed_net([pos(Direction.LONG, 0)], "c2601.DCE") == 0
