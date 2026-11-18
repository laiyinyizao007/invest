"""交易日历与统一交易时段(risk/sessions.py)的测试。

口径(见 docs/plans/20261117-live-readiness-modules-plan.md「关键设计决定」):
- 交易日 = 覆盖区间内、周一至周五、不在休市表里的日子;区间外一律「未知」(fail-closed);
- D 日有夜盘 ⇔ D 是交易日,且 D 与下一个交易日之间只隔普通周末(或紧邻);
- 20:00 及以后归入下一个日历日,再顺延到第一个交易日;
- 时段表只认有依据的品种,表外品种报错。
"""
import json
from datetime import date, datetime

import pytest

from risk import sessions as s

# 测试用日历:2026-09-21(周一) ~ 2026-10-18(周日),休市日取自真实安排(中秋 9/25、国庆 10/1~10/7 的工作日)
CLOSED = {date(2026, 9, 25), date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)}


@pytest.fixture
def cal():
    return s.TradingCalendar(start=date(2026, 9, 21), end=date(2026, 10, 18), closed=frozenset(CLOSED),
                             source="test", fetched_at="2026-11-17")


# ---------------------------------------------------------------- 日期状态

@pytest.mark.parametrize("d,expected", [
    (date(2026, 9, 21), s.TRADING),     # 普通周一
    (date(2026, 9, 25), s.CLOSED),      # 中秋(工作日休市)
    (date(2026, 9, 26), s.CLOSED),      # 周六
    (date(2026, 10, 7), s.CLOSED),      # 国庆
    (date(2026, 10, 8), s.TRADING),     # 节后首日
    (date(2026, 9, 20), s.UNKNOWN),     # 覆盖区间之前
    (date(2026, 10, 19), s.UNKNOWN),    # 覆盖区间之后:不猜
])
def test_day_status(cal, d, expected):
    assert cal.status(d) == expected


def test_next_and_prev_trading_day_skip_holidays(cal):
    assert cal.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 8)
    assert cal.prev_trading_day(date(2026, 10, 8)) == date(2026, 9, 30)
    assert cal.next_trading_day(date(2026, 9, 24)) == date(2026, 9, 28)


def test_next_trading_day_is_unknown_past_coverage(cal):
    assert cal.next_trading_day(date(2026, 10, 16)) is None     # 下一个交易日在区间外


# ---------------------------------------------------------------- 夜盘

@pytest.mark.parametrize("d,expected", [
    (date(2026, 9, 21), True),     # 普通周一:次日即交易日
    (date(2026, 10, 9), True),     # 普通周五:只隔周末
    (date(2026, 9, 24), False),    # 中秋前:隔着周五节假日
    (date(2026, 9, 30), False),    # 国庆前最后一个交易日
    (date(2026, 9, 26), False),    # 周六本身不是交易日
    (date(2026, 10, 16), None),    # 下一个交易日未知 ⇒ 未知
])
def test_has_night_session(cal, d, expected):
    assert cal.has_night_session(d) is expected


# ---------------------------------------------------------------- 时段判定

@pytest.mark.parametrize("t,expected", [
    (datetime(2026, 9, 21, 9, 0), s.OPEN),          # 开盘(含起点)
    (datetime(2026, 9, 21, 10, 15), s.CLOSED),      # 小节休息(不含终点)
    (datetime(2026, 9, 21, 10, 30), s.OPEN),
    (datetime(2026, 9, 21, 13, 0), s.CLOSED),       # 商品期货下午 13:30 才开
    (datetime(2026, 9, 21, 13, 30), s.OPEN),
    (datetime(2026, 9, 21, 15, 0), s.CLOSED),
    (datetime(2026, 9, 21, 21, 0), s.OPEN),         # 普通夜盘
    (datetime(2026, 9, 21, 23, 0), s.CLOSED),
    (datetime(2026, 9, 30, 21, 30), s.CLOSED),      # 节前无夜盘
    (datetime(2026, 9, 26, 21, 30), s.CLOSED),      # 周六晚
    (datetime(2026, 10, 7, 10, 0), s.CLOSED),       # 节假日白天
    (datetime(2026, 10, 19, 10, 0), s.UNKNOWN),     # 区间外的盘中时间:未知
    (datetime(2026, 10, 16, 21, 30), s.UNKNOWN),    # 夜盘归属未知
])
def test_session_state_for_c(cal, t, expected):
    assert s.session_state("C", t, cal) == expected


def test_session_state_outside_hours_is_closed_even_when_day_unknown(cal):
    assert s.session_state("MA", datetime(2026, 10, 19, 16, 0), cal) == s.CLOSED


def test_unknown_product_is_rejected(cal):
    with pytest.raises(s.UnknownProductError):
        s.session_state("XX", datetime(2026, 9, 21, 10, 0), cal)


def test_product_codes_are_case_insensitive_and_accept_vt_symbols(cal):
    assert s.session_state("ma", datetime(2026, 9, 21, 10, 0), cal) == s.OPEN
    assert s.product_of("MA701.CZCE") == "MA" and s.product_of("c2701.DCE") == "C"


# ---------------------------------------------------------------- 交易日归属

@pytest.mark.parametrize("t,expected", [
    (datetime(2026, 9, 21, 10, 0), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 19, 59), date(2026, 9, 21)),
    (datetime(2026, 9, 21, 20, 0), date(2026, 9, 22)),    # 20:00 起归下一日
    (datetime(2026, 10, 9, 21, 30), date(2026, 10, 12)),  # 周五夜盘归下周一
    (datetime(2026, 9, 26, 10, 0), date(2026, 9, 28)),    # 周六顺延
    (datetime(2026, 9, 30, 21, 0), date(2026, 10, 8)),    # 节前晚上顺延到节后
    (datetime(2026, 10, 16, 21, 0), None),                # 顺延出覆盖区间:未知
])
def test_trading_day_of(cal, t, expected):
    assert s.trading_day_of(t, cal) == expected


# ---------------------------------------------------------------- 覆盖告警

def test_coverage_warning_starts_30_days_before_end(cal):
    assert cal.coverage_warning(date(2026, 9, 17)) is None
    assert "2026-10-18" in cal.coverage_warning(date(2026, 9, 18))
    assert cal.coverage_warning(date(2026, 10, 19)) is not None      # 已过期也要报


# ---------------------------------------------------------------- 加载(fail-closed)

def _write(tmp_path, payload):
    p = tmp_path / "trading_calendar.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


GOOD = {"source": "x", "fetched_at": "2026-11-17", "coverage": {"start": "2026-09-21", "end": "2026-10-18"},
        "closed_weekdays": sorted(d.isoformat() for d in CLOSED)}


def test_load_roundtrip(tmp_path):
    loaded = s.load_calendar(_write(tmp_path, GOOD))
    assert loaded.status(date(2026, 10, 1)) == s.CLOSED and loaded.status(date(2026, 10, 8)) == s.TRADING


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("coverage"),
    lambda p: p.update(coverage={"start": "2026-10-18", "end": "2026-09-21"}),     # 区间倒置
    lambda p: p.update(closed_weekdays=["2026-09-26"]),                            # 周末不该出现在休市表
    lambda p: p.update(closed_weekdays=["2026-12-01"]),                            # 区间外的日期
    lambda p: p.update(closed_weekdays=["not-a-date"]),
    lambda p: p.update(closed_weekdays="2026-10-01"),
])
def test_load_rejects_malformed_files(tmp_path, mutate):
    payload = json.loads(json.dumps(GOOD))
    mutate(payload)
    with pytest.raises(s.CalendarError):
        s.load_calendar(_write(tmp_path, payload))


def test_load_rejects_missing_or_corrupt_file(tmp_path):
    with pytest.raises(s.CalendarError):
        s.load_calendar(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(s.CalendarError):
        s.load_calendar(bad)


# ---------------------------------------------------------------- 入库的真实日历

def test_committed_calendar_is_valid_and_matches_known_closures():
    real = s.load_calendar()
    assert real.start <= date(2018, 1, 2) and real.end >= date(2026, 12, 31)
    assert real.status(date(2025, 10, 1)) == s.CLOSED and real.status(date(2025, 10, 9)) == s.TRADING
    assert real.status(date(2026, 2, 16)) == s.CLOSED                      # 2026 春节
    assert real.has_night_session(date(2026, 9, 30)) is False             # 国庆前无夜盘
    assert real.has_night_session(date(2026, 11, 13)) is True             # 普通周五
