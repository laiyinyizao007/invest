"""清场工具的平仓计划测试:方向、手数拆分、价格取法。不联网。"""
from types import SimpleNamespace

from vnpy.trader.constant import Direction, Exchange, Offset
from vnpy.trader.object import PositionData

from flatten_positions import plan_closes


def _pos(symbol: str, exchange: Exchange, direction: Direction, volume: int, yd: int = 0) -> PositionData:
    pos = PositionData(symbol=symbol, exchange=exchange, direction=direction, gateway_name="CTP")
    pos.volume = volume
    pos.yd_volume = yd
    pos.contract = SimpleNamespace(pricetick=1.0)
    return pos


def test_long_position_is_closed_by_selling_at_bid():
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    plan = plans[0]
    assert plan.direction == Direction.SHORT      # 平多 = 卖出
    assert plan.offset == Offset.CLOSE            # 大商所不分今昨
    assert plan.volume == 3
    assert plan.price == 2185.0                    # 挂买一,主动打买盘


def test_short_position_is_closed_by_buying_at_ask():
    pos = _pos("c2701", Exchange.DCE, Direction.SHORT, 2)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].direction == Direction.LONG    # 平空 = 买入
    assert plans[0].price == 2186.0                # 挂卖一
    assert plans[0].volume == 2


def test_shfe_splits_today_and_yesterday_positions():
    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 5, yd=2)
    plans = plan_closes([pos], {"rb2701.SHFE": (3000.0, 3001.0, 3000.0)})
    offsets = {p.offset: p.volume for p in plans}
    assert offsets == {Offset.CLOSETODAY: 3, Offset.CLOSEYESTERDAY: 2}
    assert all(p.direction == Direction.SHORT for p in plans)


def test_net_direction_is_never_guessed():
    """净持仓回报里没有多空之分,猜错方向等于把平仓做成开仓。"""
    pos = _pos("c2701", Exchange.DCE, Direction.NET, 3)
    assert plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)}) == []


def test_zero_volume_is_skipped_and_missing_quote_falls_back_to_last():
    empty = _pos("c2701", Exchange.DCE, Direction.LONG, 0)
    assert plan_closes([empty], {}) == []
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    plans = plan_closes([pos], {"c2701.DCE": (0.0, 0.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].price == 2185.0
    assert "最新价" in plans[0].reason
