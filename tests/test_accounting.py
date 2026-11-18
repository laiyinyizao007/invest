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


# ---------------------------------------------------------------- G8 第 2 轮(codex 39/50)复现后补的回归

def test_append_after_an_out_of_order_tail_line_is_refused(tmp_path):
    """High:last_point 原先只找最后一条可解析的行、不管它是否递增,于是乱序行之后还能追加,
    而 read() 会把新记录当乱序跳过——新写的权益悄悄丢了。现在 last_point 与 read() 同一套接受规则。"""
    p = tmp_path / "equity.jsonl"
    eh.append(p, datetime(2026, 1, 2), 1.0)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-01-01T00:00:00", "equity": 2.0}) + "\n")
    assert eh.last_point(p).ts == datetime(2026, 1, 2)
    with pytest.raises(ValueError):
        eh.append(p, datetime(2026, 1, 1, 12), 3.0)
    eh.append(p, datetime(2026, 1, 3), 3.0)
    assert eh.read(p).records[-1].equity == 3.0


@pytest.mark.parametrize("kwargs", [
    dict(fee_per_lot=float("nan"), size=10, pricetick=1.0),
    dict(fee_per_lot=-1.0, size=10, pricetick=1.0),
    dict(fee_per_lot=1.0, size=0, pricetick=1.0),
    dict(fee_per_lot=1.0, size=10, pricetick=0.0),
])
def test_product_cost_validation(kwargs):
    with pytest.raises(ValueError):
        at.ProductCost(**kwargs)


@pytest.mark.parametrize("kwargs", [
    dict(order_id="o", side="hold", reference_price=1.0, pricetick=1.0),
    dict(order_id="o", side="buy", reference_price=1.0, pricetick=0.0),
    dict(order_id="o", side="buy", reference_price=float("nan"), pricetick=1.0),
])
def test_order_ref_validation(kwargs):
    with pytest.raises(ValueError):
        sl.OrderRef(**kwargs)


@pytest.mark.parametrize("volume", [1.9, -1, "2", True])
def test_settlement_rejects_non_integer_or_negative_volumes(volume):
    with pytest.raises(ValueError):
        st.reconcile({("c2701.DCE", "long"): volume}, {})


# ---------------------------------------------------------------- G8 第 2 轮复核(codex 44/50)复现后补的回归

def test_last_point_matches_read_even_with_old_future_corruption(tmp_path):
    """Low:尾部窗口之前藏着一条「未来时间」的坏行、尾部又严格递增时,原先的尾部快路径与 read() 结论不同。
    现在 last_point 整文件读(与 read 同一规则);高频追加用 EquityWriter(打开时读一次,之后内存缓存)。"""
    p = tmp_path / "equity.jsonl"
    lines = [json.dumps({"ts": "2030-01-01T00:00:00", "equity": 9.0})]
    lines += [json.dumps({"ts": datetime(2026, 1, 1).replace(hour=i // 3600 % 24, minute=i // 60 % 60, second=i % 60).isoformat(),
                          "equity": 1.0}) for i in range(3000)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert eh.last_point(p) == eh.read(p).records[-1]
    with pytest.raises(ValueError):
        eh.append(p, datetime(2026, 6, 1), 1.0)              # 会被 read() 当乱序跳过的写入,一律拒绝


def test_equity_writer_caches_last_timestamp_after_one_full_read(tmp_path):
    p = tmp_path / "equity.jsonl"
    eh.append(p, T, 10000.0)
    w = eh.EquityWriter(p)
    assert w.last is not None and w.last.ts == T
    w.append(datetime(2026, 11, 16, 10, 1), 10001.0)
    with pytest.raises(ValueError):
        w.append(datetime(2026, 11, 16, 10, 1), 10002.0)
    w.append(datetime(2026, 11, 16, 10, 2), 10002.0)
    assert [r.equity for r in eh.read(p).records] == [10000.0, 10001.0, 10002.0]


def test_slippage_rejects_bad_fills():
    o = [sl.OrderRef("o", "buy", 1.0, 1.0)]
    for bad in ([(2.0, -1)], [(float("nan"), 1)], [(2.0, 1.5)], [(0.0, 1)]):
        with pytest.raises(ValueError):
            sl.compare(o, {"o": bad})
