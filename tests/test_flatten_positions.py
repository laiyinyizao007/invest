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


def test_pricetick_comes_from_contract_not_from_position_object():
    """最小变动价位必须来自合约信息。

    PositionData 上没有 .contract(vnpy 不填这个属性),按 1 计价的话,
    国债(0.001)、股指(0.2)这类品种的价格不是整数倍,柜台会以「价格非法」拒单,
    平仓就平不掉——还会误以为是柜台问题。
    """
    pos = _pos("T2601", Exchange.CFFEX, Direction.LONG, 1)
    plans = plan_closes([pos], {"T2601.CFFEX": (3900.123, 3900.222, 3900.1)}, {"T2601.CFFEX": 0.2})
    assert len(plans) == 1
    assert plans[0].price == 3900.2          # 3900.123 → 3900.2(0.2 的整数倍)
    assert "没有合约信息" not in plans[0].reason


def test_missing_pricetick_falls_back_to_one_and_says_so():
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert plans[0].price == 2185.0
    assert "没有合约信息" in plans[0].reason


def test_position_without_vt_symbol_suffix_is_skipped():
    """vt_symbol 形如 c2701.DCE;没有交易所后缀时拼不出合约代码,宁可不发单。"""
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701"
    assert plan_closes([pos], {"c2701": (2185.0, 2186.0, 2185.0)}) == []
