"""核算(risk/accounting/):权益历史、盈亏归因、结算单核对、真实滑点对比。

都是只依赖标准库的纯逻辑;接入运行时(定期写权益、把成交喂给归因、查询结算单)属阶段 F-b。
费率与合约乘数必须由调用方给出(来自配置),表外品种报错——不猜费率。
"""
import json
from datetime import date, datetime

import pytest

from risk.accounting import attribution as at
from risk.accounting import equity_history as eh
from risk.accounting import settlement as st
from risk.accounting import slippage as sl
from risk import sessions

# ---------------------------------------------------------------- 权益历史

T = datetime(2026, 11, 16, 10, 0, 0)


def test_equity_append_and_read(tmp_path):
    p = tmp_path / "equity.jsonl"
    eh.append(p, T, 10000.0)
    eh.append(p, datetime(2026, 11, 16, 10, 5), 10012.5)
    hist = eh.read(p)
    assert [r.equity for r in hist.records] == [10000.0, 10012.5] and hist.skipped == 0


def test_equity_append_rejects_non_increasing_time_and_bad_values(tmp_path):
    p = tmp_path / "equity.jsonl"
    eh.append(p, T, 10000.0)
    with pytest.raises(ValueError):
        eh.append(p, T, 10001.0)                            # 时间不递增
    with pytest.raises(ValueError):
        eh.append(p, datetime(2026, 11, 16, 11), float("nan"))


def test_equity_read_skips_corrupt_lines_and_counts_them(tmp_path):
    p = tmp_path / "equity.jsonl"
    eh.append(p, T, 10000.0)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("{broken\n")
        fh.write(json.dumps({"ts": "2026-11-16T10:10:00", "equity": "x"}) + "\n")
    eh.append(p, datetime(2026, 11, 16, 10, 20), 10020.0)
    hist = eh.read(p)
    assert len(hist.records) == 2 and hist.skipped == 2


def test_equity_append_on_a_large_file_only_needs_the_tail(tmp_path):
    p = tmp_path / "equity.jsonl"
    lines = [json.dumps({"ts": datetime(2026, 1, 1, 0, 0).replace(minute=i % 60, hour=(i // 60) % 24, day=1 + i // 1440).isoformat(),
                         "equity": 10000.0 + i}) for i in range(3000)]           # 约 20 万字节,远超 64 KB 的尾部窗口
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    last = eh.last_point(p)
    assert last is not None and last.equity == 10000.0 + 2999
    with pytest.raises(ValueError):
        eh.append(p, last.ts, 1.0)                           # 不递增仍被拒
    eh.append(p, datetime(2026, 2, 1), 1.0)
    assert eh.read(p).records[-1].equity == 1.0


def test_equity_last_point_falls_back_when_the_tail_is_all_garbage(tmp_path):
    p = tmp_path / "equity.jsonl"
    eh.append(p, T, 10000.0)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(("x" * 100 + "\n") * 1000)                   # 尾部 10 万字节全是坏行
    assert eh.last_point(p).ts == T
    with pytest.raises(ValueError):
        eh.append(p, T, 1.0)


def test_equity_read_missing_file_is_empty(tmp_path):
    hist = eh.read(tmp_path / "none.jsonl")
    assert hist.records == [] and hist.skipped == 0


# ---------------------------------------------------------------- 盈亏归因

FEES = {"C": at.ProductCost(fee_per_lot=1.21, size=10, pricetick=1.0)}


def fill(t, side, action, price, vol=1, symbol="c2701.DCE"):
    return at.Fill(t, symbol, side, action, price, vol)


def test_round_trip_attribution_long():
    fills = [fill(datetime(2026, 11, 16, 9, 30), "long", "open", 2300.0),
             fill(datetime(2026, 11, 16, 14, 0), "long", "close", 2312.0)]
    res = at.attribute(fills, FEES, est_slippage_ticks=1)
    trip = res.round_trips[0]
    assert trip.gross == pytest.approx(120.0)               # (2312-2300) × 10 吨 × 1 手
    assert trip.fees == pytest.approx(2.42)                 # 1.21 × 2 边
    assert trip.est_slippage == pytest.approx(20.0)         # 1 跳 × 1 元 × 10 吨 × 2 边
    assert trip.net == pytest.approx(120.0 - 2.42 - 20.0)
    assert res.open_positions == {} and res.unmatched == []


def test_short_round_trip_and_fifo_partial_close():
    fills = [fill(datetime(2026, 11, 16, 9, 30), "short", "open", 2300.0, vol=2),
             fill(datetime(2026, 11, 16, 9, 40), "short", "open", 2310.0, vol=1),
             fill(datetime(2026, 11, 16, 10, 0), "short", "close", 2290.0, vol=2)]
    res = at.attribute(fills, FEES, est_slippage_ticks=0)
    assert sum(t.gross for t in res.round_trips) == pytest.approx((2300 - 2290) * 10 * 2)     # 先进先出:平掉 2300 的两手
    assert res.open_positions == {("c2701.DCE", "short"): [(2310.0, 1)]}


def test_close_without_open_is_reported_not_guessed():
    res = at.attribute([fill(T, "long", "close", 2300.0)], FEES, est_slippage_ticks=1)
    assert res.round_trips == [] and len(res.unmatched) == 1


@pytest.mark.parametrize("ticks", [-1, float("nan")])
def test_negative_or_nan_slippage_estimate_is_rejected(ticks):
    with pytest.raises(ValueError):
        at.attribute([], FEES, est_slippage_ticks=ticks)


def test_unknown_product_fee_is_an_error():
    with pytest.raises(at.UnknownCostError):
        at.attribute([fill(T, "long", "open", 2300.0, symbol="MA701.CZCE")], FEES, est_slippage_ticks=1)


def test_round_trip_is_tagged_with_trading_day_of_close():
    cal = sessions.TradingCalendar(date(2026, 11, 2), date(2026, 11, 30), frozenset(), "t", "x")
    fills = [fill(datetime(2026, 11, 13, 21, 5), "long", "open", 2300.0),        # 周五夜盘
             fill(datetime(2026, 11, 13, 22, 0), "long", "close", 2305.0)]
    res = at.attribute(fills, FEES, est_slippage_ticks=0, calendar=cal)
    assert res.round_trips[0].trading_day == date(2026, 11, 16)


@pytest.mark.parametrize("bad", [
    dict(side="flat"), dict(action="reverse"), dict(volume=0), dict(price=-1.0),
])
def test_fill_validation(bad):
    kwargs = dict(dt=T, symbol="c2701.DCE", side="long", action="open", price=2300.0, volume=1) | bad
    with pytest.raises(ValueError):
        at.Fill(**kwargs)


# ---------------------------------------------------------------- 结算单核对

def test_settlement_matches_and_reports_differences():
    statement = {("c2701.DCE", "long"): 2, ("MA701.CZCE", "short"): 1}
    queried = {("C2701.DCE", "long"): 2, ("MA701.CZCE", "short"): 2, ("c2705.DCE", "long"): 1}
    diffs = st.reconcile(statement, queried)
    assert {(d.symbol.lower(), d.side, d.statement, d.queried) for d in diffs} == {
        ("ma701.czce", "short", 1, 2), ("c2705.dce", "long", 0, 1)}


def test_settlement_identical_is_empty_and_zero_volume_rows_are_ignored():
    assert st.reconcile({("c2701.DCE", "long"): 2, ("c2705.DCE", "short"): 0}, {("c2701.dce", "long"): 2}) == []


# ---------------------------------------------------------------- 真实滑点对比

def test_slippage_in_ticks_adverse_positive():
    orders = [sl.OrderRef("o1", "buy", 2300.0, pricetick=1.0), sl.OrderRef("o2", "sell", 2300.0, pricetick=1.0),
              sl.OrderRef("o3", "buy", None, pricetick=1.0)]
    fills = {"o1": [(2302.0, 1)], "o2": [(2299.0, 2), (2300.0, 2)], "o3": [(2301.0, 1)]}
    rep = sl.compare(orders, fills)
    by = {r.order_id: r for r in rep.rows}
    assert by["o1"].ticks == pytest.approx(2.0)            # 买贵了 2 跳
    assert by["o2"].ticks == pytest.approx(0.5)            # 卖便宜了:成交均价 2299.5,不利 0.5 跳
    assert rep.skipped == 1                                # o3 没有下单时盘口价:不猜
    assert rep.mean_ticks == pytest.approx((2.0 * 1 + 0.5 * 4) / 5)     # 按成交量加权


def test_slippage_unfilled_orders_are_ignored():
    rep = sl.compare([sl.OrderRef("o1", "buy", 2300.0, pricetick=1.0)], {})
    assert rep.rows == [] and rep.mean_ticks is None
