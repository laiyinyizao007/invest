"""`engine_runner` 用**真实回测引擎**回传行为描述符所需的字段(持仓根数、逐笔盈亏)。

假执行器的单测(test_research_gate.py)证明了编排逻辑;这里证明真引擎的输出确实对得上——
否则编排对着一个永远不回传这些字段的执行器,格子永远是 None 而没人发现。
"""
import math
from datetime import datetime, timedelta

import pytest

import research_gate as rg
from risk.behavior import describe_behavior
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData

KEY = "C15"
SETTING = {"risk_budget": 100_000.0, "max_daily_losses": 3, "atr_window": 14, "history_vt_symbol": "C15",
           "fast_window": 5, "slow_window": 20, "atr_mult": 1.5}


def wavy_bars(n=400):
    """合成 15 分钟 K 线:一段上涨、一段下跌地来回,保证均线会交叉、会有几个往返。"""
    start = datetime(2020, 1, 2, 9, 0)
    bars = []
    for i in range(n):
        wave = 60.0 * math.sin(i / 25.0)
        price = 2000.0 + wave
        bars.append(BarData(symbol="C15", exchange=Exchange.DCE, datetime=start + timedelta(minutes=15 * i),
                            interval=Interval.MINUTE, open_price=price, high_price=price + 4.0, low_price=price - 4.0,
                            close_price=price + 0.5, volume=100, turnover=0.0, open_interest=0.0, gateway_name="TEST"))
    return bars


@pytest.fixture(scope="module")
def outcome():
    bars = wavy_bars()
    return bars, rg.engine_runner(KEY)(TrendAtr15mStrategy, dict(SETTING), bars, 1.0)


def test_the_real_engine_reports_holding_bars_and_trade_pnls(outcome):
    bars, result = outcome
    assert result["round_trips"] >= 2, "合成行情应当产生往返,否则这条测试什么也没证明"
    assert all(isinstance(h, int) and 0 <= h < len(bars) for h in result["hold_bars"])
    assert all(isinstance(p, float) and math.isfinite(p) for p in result["trade_pnls"])


def test_holding_bars_and_trade_pnls_pair_up_with_the_round_trips(outcome):
    _, result = outcome
    assert len(result["hold_bars"]) == len(result["trade_pnls"]) == result["round_trips"]


def test_the_real_result_maps_to_a_cell(outcome):
    from risk.behavior import ALL_CELLS
    bars, result = outcome
    behavior = describe_behavior(result["round_trips"], len(bars), result["hold_bars"])
    assert behavior["cell"] in ALL_CELLS and behavior["avg_hold_bars"] > 0
