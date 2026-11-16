"""行为描述符(MAP-Elites 分格)与结构化诊断。纯逻辑,不依赖 vn.py。"""
import json
import math
from types import SimpleNamespace

import pytest

from risk.behavior import (ALL_CELLS, CELL_PATTERN, FREQ_EDGES, HOLD_EDGES, behavior_cell, describe_behavior,
                           round_trip_holding_bars, trips_per_100_bars)
from risk.diagnosis import diagnose
from risk.structured_diagnosis import DIMENSIONS, LEVELS, structure, validate


# ---------- 持仓周期 ----------

def bars(n):
    return [SimpleNamespace(datetime=i) for i in range(n)]            # 时间戳直接用下标,便于核对


def trade(at, kind):
    return SimpleNamespace(datetime=at, offset=SimpleNamespace(name=kind))


def test_holding_bars_pair_each_open_with_the_next_close():
    trades = [trade(2, "OPEN"), trade(7, "CLOSE"), trade(10, "OPEN"), trade(11, "CLOSE")]
    assert round_trip_holding_bars(trades, bars(20)) == [5, 1]


@pytest.mark.parametrize("close_kind", ["CLOSE", "CLOSETODAY", "CLOSEYESTERDAY"])
def test_every_kind_of_close_ends_a_round_trip(close_kind):
    assert round_trip_holding_bars([trade(1, "OPEN"), trade(4, close_kind)], bars(10)) == [3]


def test_unmatched_trades_are_not_invented_into_round_trips():
    """只有平仓没有开仓、或者结尾还没平的开仓:都不计入,不凭空造数。"""
    assert round_trip_holding_bars([trade(3, "CLOSE")], bars(10)) == []
    assert round_trip_holding_bars([trade(3, "OPEN")], bars(10)) == []
    assert round_trip_holding_bars([trade(1, "OPEN"), trade(2, "OPEN"), trade(5, "CLOSE")], bars(10)) == [3]   # 后一次开仓为准


def test_a_trade_whose_timestamp_is_not_in_the_bars_is_dropped_rather_than_guessed():
    assert round_trip_holding_bars([trade(1, "OPEN"), trade(999, "CLOSE")], bars(10)) == []
    assert round_trip_holding_bars([trade(None, "OPEN"), trade(3, "CLOSE")], bars(10)) == []


def test_a_close_before_its_open_in_bar_order_is_dropped():
    assert round_trip_holding_bars([trade(8, "OPEN"), trade(2, "CLOSE")], bars(10)) == []


def test_trades_with_an_unknown_offset_are_ignored():
    assert round_trip_holding_bars([trade(1, "OPEN"), trade(2, "WHATEVER"), trade(4, "CLOSE")], bars(10)) == [3]


# ---------- 分格 ----------

def test_there_are_exactly_sixteen_cells_and_all_match_the_pattern():
    assert len(ALL_CELLS) == 16 and len(set(ALL_CELLS)) == 16
    assert all(CELL_PATTERN.fullmatch(c) for c in ALL_CELLS)


def test_the_cell_edges_are_the_ones_registered_in_the_code():
    """边界事先定死(看到候选之后再调,等于挑一个让某个候选独占一格的口径)。"""
    assert HOLD_EDGES == (16.0, 64.0, 192.0) and FREQ_EDGES == (0.5, 1.5, 4.0)


@pytest.mark.parametrize("hold, expected_bin", [(0, 0), (16, 0), (16.01, 1), (64, 1), (64.01, 2), (192, 2), (192.01, 3), (5000, 3)])
def test_holding_period_bins_are_inclusive_on_the_upper_edge(hold, expected_bin):
    assert behavior_cell(hold, 1.0) == f"H{expected_bin}F1"


@pytest.mark.parametrize("freq, expected_bin", [(0.0, 0), (0.49, 0), (0.5, 1), (1.49, 1), (1.5, 2), (3.99, 2), (4.0, 3), (50.0, 3)])
def test_frequency_bins_are_exclusive_on_the_upper_edge(freq, expected_bin):
    assert behavior_cell(10, freq) == f"H0F{expected_bin}"


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), -1.0, True, "5"])
def test_a_missing_or_invalid_descriptor_gives_no_cell(bad):
    assert behavior_cell(bad, 1.0) is None
    assert behavior_cell(10.0, bad) is None


def test_every_pair_of_descriptors_lands_in_one_of_the_sixteen_cells():
    for hold in (0, 8, 16, 40, 64, 100, 192, 400):
        for freq in (0, 0.2, 0.5, 1.0, 1.5, 3.0, 4.0, 9.0):
            assert behavior_cell(hold, freq) in ALL_CELLS


def test_trips_per_100_bars_is_window_length_independent():
    assert trips_per_100_bars(10, 1000) == trips_per_100_bars(20, 2000) == 1.0
    assert trips_per_100_bars(5, 0) is None and trips_per_100_bars(-1, 100) is None and trips_per_100_bars(True, 100) is None


def test_describe_behavior_reports_the_cell_and_the_raw_descriptors():
    d = describe_behavior(12, 1200, [10, 20, 30])
    assert d == {"round_trips": 12, "n_bars": 1200, "avg_hold_bars": 20.0, "trips_per_100_bars": 1.0, "cell": "H1F1"}


def test_describe_behavior_without_round_trips_has_no_cell():
    assert describe_behavior(0, 1200, [])["cell"] is None
    assert describe_behavior(5, 1200, None)["cell"] is None                  # 有往返但没有持仓记录:不猜
    assert describe_behavior(5, 0, [3])["cell"] is None                      # 窗口无效


# ---------- 结构化诊断 ----------

def row(sharpe=1.2, dd_pct=-5.0, dd_yuan=-500.0, trips=80):
    return {"sharpe": sharpe, "maxdd_pct": dd_pct, "maxdd_yuan": dd_yuan, "round_trips": trips}


def test_a_structured_diagnosis_validates():
    out = structure(diagnose(row(), [10.0, -5.0, 3.0]))
    assert validate(out)
    assert set(out["levels"]) == set(out["ratios"]) == set(DIMENSIONS)


def test_a_structured_diagnosis_contains_no_free_text_and_no_raw_numbers():
    """规格要求只含枚举 + 0–1 数值:不得带 note/summary 之类的中文句子,也不得带原始夏普/回撤数值。"""
    out = structure(diagnose(row(sharpe=1.2345, dd_pct=-7.77, dd_yuan=-1234.5, trips=137), [1.0, -2.0]))
    text = json.dumps(out, ensure_ascii=False)
    assert not any("一" <= ch <= "鿿" for ch in text)                 # 没有中文句子
    for secret in ("1.2345", "7.77", "1234.5", "137"):
        assert secret not in text
    assert "note" not in text and "summary" not in text


def test_every_ratio_is_within_zero_and_one_whatever_the_input():
    for sharpe in (-3, 0, 0.5, 2, 99):
        for dd in (0, -1, -50, -500):
            for trips in (0, 5, 30, 10_000):
                out = structure(diagnose(row(sharpe=sharpe, dd_pct=dd, dd_yuan=dd * 100, trips=trips), [1.0]))
                assert validate(out)
                assert all(r is None or 0.0 <= r <= 1.0 for r in out["ratios"].values())


def test_a_drawdown_at_the_limit_has_ratio_zero_and_no_drawdown_has_ratio_one():
    assert structure(diagnose(row(dd_pct=-0.0, dd_yuan=-0.0), [1.0]))["ratios"]["drawdown"] == 1.0
    assert structure(diagnose(row(dd_pct=-99.0, dd_yuan=-99999.0), [1.0]))["ratios"]["drawdown"] == 0.0


def test_a_negative_sharpe_has_ratio_zero():
    assert structure(diagnose(row(sharpe=-2.0), [1.0]))["ratios"]["sharpe"] == 0.0


def test_a_missing_dimension_is_unknown_with_no_ratio():
    out = structure(diagnose({"sharpe": 1.0}, None))
    assert out["levels"]["drawdown"] == "unknown" and out["ratios"]["drawdown"] is None
    assert out["levels"]["losing_streak"] == "unknown" and out["ratios"]["losing_streak"] is None
    assert validate(out)


def test_the_weakest_dimension_is_an_enum_name_or_none():
    assert structure(diagnose(row(sharpe=-1.0), [1.0]))["weakest"] == "sharpe"
    healthy = structure(diagnose(row(sharpe=2.0, dd_pct=-1.0, dd_yuan=-50.0, trips=500), [5.0, 6.0]))
    assert healthy["weakest"] is None or healthy["weakest"] in DIMENSIONS


def test_structure_refuses_something_that_is_not_a_diagnosis():
    with pytest.raises(ValueError):
        structure({"nope": 1})
    with pytest.raises(ValueError):
        structure(None)


@pytest.mark.parametrize("mutate", [
    lambda d: d.__setitem__("weakest", "unknown_dim"),
    lambda d: d.__setitem__("extra", 1),
    lambda d: d["levels"].__setitem__("sharpe", "excellent"),
    lambda d: d["levels"].pop("sharpe"),
    lambda d: d["ratios"].__setitem__("sharpe", 1.5),
    lambda d: d["ratios"].__setitem__("sharpe", -0.1),
    lambda d: d["ratios"].__setitem__("sharpe", float("nan")),
    lambda d: d["ratios"].__setitem__("sharpe", True),
    lambda d: d["ratios"].__setitem__("sharpe", "0.5"),
    lambda d: d["ratios"].__setitem__("note", 0.5),
    lambda d: d.__setitem__("levels", "ok"),
    lambda d: d["levels"].__setitem__("sharpe", "unknown"),                  # unknown 却有比例:不一致
])
def test_validate_rejects_anything_that_is_not_a_clean_structured_diagnosis(mutate):
    """诊断经过子进程(候选所在的进程)传回,不可信:写出前必须过 validate。"""
    out = structure(diagnose(row(), [1.0, -1.0]))
    assert validate(out)
    mutate(out)
    assert not validate(out)


@pytest.mark.parametrize("junk", [None, [], "x", 5, {}, {"weakest": None}])
def test_validate_rejects_non_diagnoses(junk):
    assert not validate(junk)


def test_levels_are_drawn_from_the_closed_set():
    out = structure(diagnose(row(), [1.0]))
    assert set(out["levels"].values()) <= set(LEVELS)
    assert math.isfinite(sum(r for r in out["ratios"].values() if r is not None))
