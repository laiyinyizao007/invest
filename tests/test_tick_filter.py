"""行情异常值判定(risk/tick_filter.py):纯函数,阈值由调用方显式给出(启用策略与取值随 F-b 的 ADR 一起定)。

判定项:价格非正/非数、越过涨跌停、与最小变动价位不对齐、相对上一笔有效 tick 跳变过大、
累计成交量回退、时间戳倒退、交易所时间与本地接收时间偏差过大。
持仓量**不**做单调检查:有人平仓时持仓量本来就会减少。
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from datetime import datetime as DateTime  # dataclass 字段名叫 datetime,类型注解用别名免得被遮住

import pytest

from risk import tick_filter as tf

RULES = tf.TickRules(max_jump_pct=0.03, max_clock_skew_seconds=30.0)
T0 = datetime(2026, 11, 16, 10, 0, 0)


@dataclass
class Tick:
    last_price: float = 2300.0
    volume: float = 1000
    open_interest: float = 50000
    datetime: DateTime = T0
    localtime: DateTime | None = T0
    limit_up: float = 2480.0
    limit_down: float = 2120.0


PREV = Tick(datetime=T0 - timedelta(seconds=1), localtime=T0 - timedelta(seconds=1), volume=990)


def test_normal_tick_passes():
    v = tf.classify(Tick(), PREV, pricetick=1.0, rules=RULES)
    assert v.ok and v.reasons == ()


def test_first_tick_without_previous_passes():
    assert tf.classify(Tick(), None, pricetick=1.0, rules=RULES).ok


@pytest.mark.parametrize("change,needle", [
    (dict(last_price=0.0), "非正"),
    (dict(last_price=float("nan")), "非正"),
    (dict(last_price=2481.0), "涨跌停"),
    (dict(last_price=2119.0), "涨跌停"),
    (dict(last_price=2300.5), "最小变动价位"),
    (dict(last_price=2400.0), "跳变"),                   # 相对上一笔 +4.3% > 3%
    (dict(volume=980), "成交量回退"),
    (dict(datetime=T0 - timedelta(seconds=5)), "时间倒退"),
    (dict(localtime=T0 + timedelta(seconds=31)), "时钟偏差"),
])
def test_bad_ticks_are_flagged_with_reason(change, needle):
    v = tf.classify(replace(Tick(), **change), PREV, pricetick=1.0, rules=RULES)
    assert not v.ok and any(needle in r for r in v.reasons), v.reasons


def test_open_interest_decrease_is_normal():
    assert tf.classify(replace(Tick(), open_interest=40000), PREV, pricetick=1.0, rules=RULES).ok


def test_limit_check_is_skipped_when_exchange_sends_no_limits():
    assert tf.classify(replace(Tick(), limit_up=0.0, limit_down=0.0, last_price=2310.0), PREV, pricetick=1.0, rules=RULES).ok


def test_missing_localtime_skips_only_the_skew_check():
    assert tf.classify(replace(Tick(), localtime=None), PREV, pricetick=1.0, rules=RULES).ok


def test_all_reasons_are_reported_together():
    v = tf.classify(replace(Tick(), last_price=2500.5, volume=1), PREV, pricetick=1.0, rules=RULES)
    assert len(v.reasons) >= 3


def test_jump_is_measured_against_previous_valid_tick_exactly_at_threshold():
    assert tf.classify(replace(Tick(), last_price=2369.0), PREV, pricetick=1.0, rules=RULES).ok          # +3.0% 恰好在阈值上
    assert not tf.classify(replace(Tick(), last_price=2370.0), PREV, pricetick=1.0, rules=RULES).ok     # +3.04%


@pytest.mark.parametrize("kwargs", [
    dict(max_jump_pct=0.0, max_clock_skew_seconds=30.0),
    dict(max_jump_pct=0.03, max_clock_skew_seconds=-1.0),
])
def test_rules_must_be_positive(kwargs):
    with pytest.raises(ValueError):
        tf.TickRules(**kwargs)


def test_pricetick_must_be_positive():
    with pytest.raises(ValueError):
        tf.classify(Tick(), PREV, pricetick=0.0, rules=RULES)


# ---------------------------------------------------------------- G8 第 2 轮(codex 39/50)复现后补的回归

@pytest.mark.parametrize("kwargs", [
    dict(max_jump_pct=float("inf"), max_clock_skew_seconds=30.0),     # 无穷大阈值等于关掉检查
    dict(max_jump_pct=0.03, max_clock_skew_seconds=float("nan")),
])
def test_rules_must_be_finite(kwargs):
    with pytest.raises(ValueError):
        tf.TickRules(**kwargs)


def test_infinite_price_is_bad_not_an_exception():
    v = tf.classify(replace(Tick(), last_price=float("inf")), PREV, pricetick=1.0, rules=RULES)
    assert not v.ok and any("非正" in r for r in v.reasons)


@pytest.mark.parametrize("up,down", [(float("nan"), 2120.0), (2480.0, float("inf")), (2120.0, 2480.0)])
def test_broken_limit_data_is_bad_not_silently_skipped(up, down):
    v = tf.classify(replace(Tick(), limit_up=up, limit_down=down), PREV, pricetick=1.0, rules=RULES)
    assert not v.ok and any("涨跌停数据异常" in r for r in v.reasons)


def test_pricetick_must_be_finite():
    with pytest.raises(ValueError):
        tf.classify(Tick(), PREV, pricetick=float("inf"), rules=RULES)
