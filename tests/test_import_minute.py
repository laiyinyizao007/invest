"""import_minute:分钟线 → 交易日归属 → 日线 → 差额复权分钟线。全部用合成数据,不依赖购买的真实文件。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


import import_minute as im  # noqa: E402  (内部会先 import bootstrap)
from build_continuous import build_continuous  # noqa: E402

DAYS = pd.to_datetime(["2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09"])  # 周一到下周一
CAL = np.array(DAYS, dtype="datetime64[ns]")


def minute_bars(level_by_day: dict, oi_by_day: dict, night=True, symbol="DCE.c2105"):
    """每个交易日:前一晚 21:00-21:14(属本交易日)+ 日盘 09:00-09:29。价格恒等于该日水平。"""
    rows = []
    for day in DAYS:
        if day not in level_by_day:
            continue
        lvl, oi = level_by_day[day], oi_by_day[day]
        prev = day - pd.Timedelta(days=3 if day.weekday() == 0 else 1)
        stamps = list(pd.date_range(day + pd.Timedelta(hours=9), periods=30, freq="1min"))
        if night:
            stamps = list(pd.date_range(prev + pd.Timedelta(hours=21), periods=15, freq="1min")) + stamps
        for t in stamps:
            rows.append({"datetime": t, "symbol": symbol, "open": lvl, "high": lvl, "low": lvl, "close": lvl,
                         "volume": 10, "open_interest": oi, "close_interest": oi})
    return pd.DataFrame(rows)


def two_contracts():
    """A 一直持仓 100;B 持仓从第 3 天起反超 → 第 4 天(前一日持仓量决定)换月到 B。"""
    a = minute_bars({d: 100.0 + i for i, d in enumerate(DAYS)}, {d: 100 for d in DAYS})
    b = minute_bars({d: 120.0 + i for i, d in enumerate(DAYS)}, {d: 50 if i < 2 else 150 for i, d in enumerate(DAYS)},
                    symbol="DCE.c2109")
    return {"C2105": a.drop(columns="symbol"), "C2109": b.drop(columns="symbol")}


# ---------- 代码与交易日 ----------

def test_contract_code_normalizes_symbol_and_rejects_other_products():
    assert im.contract_code("DCE.c2101", "C") == "C2101" and im.contract_code("c2101", "c") == "C2101"
    with pytest.raises(ValueError):
        im.contract_code("DCE.m2101", "C")


def test_night_session_belongs_to_next_trading_day_across_weekend():
    ts = pd.Series(pd.to_datetime(["2026-03-06 21:00", "2026-03-06 09:00", "2026-03-04 21:30"]))
    td = im.assign_trading_day(ts, CAL)
    assert list(td) == [pd.Timestamp("2026-03-09"), pd.Timestamp("2026-03-06"), pd.Timestamp("2026-03-05")]


def test_night_session_after_the_calendar_has_no_trading_day():
    """日历(来自已出现的日盘)之外的夜盘所属交易日未知:必须是 NaT,不能硬塞进最后一天。"""
    td = im.assign_trading_day(pd.Series(pd.to_datetime(["2026-03-09 21:00"])), CAL)
    assert td.isna().all()


def test_after_midnight_bars_belong_to_the_next_trading_day():
    td = im.assign_trading_day(pd.Series(pd.to_datetime(["2026-03-07 01:00"])), CAL)    # 周六凌晨
    assert td.iloc[0] == pd.Timestamp("2026-03-09")


def test_calendar_comes_from_day_session_dates_only():
    m = {"C2105": minute_bars({d: 100.0 for d in DAYS}, {d: 1 for d in DAYS}).drop(columns="symbol")}
    cal = im.trading_calendar(m)
    assert list(pd.to_datetime(cal)) == list(DAYS)                       # 夜盘所在的周五/周日等不会混进日历


def test_daily_aggregation_includes_night_session_in_its_trading_day():
    df = two_contracts()["C2105"]
    daily = im.daily_from_minutes(df, CAL)
    assert len(daily) == len(DAYS) and list(daily["datetime"]) == list(DAYS)
    assert (daily["volume"] == 450).all()                                # 15 个夜盘 + 30 个日盘,每根 10 手
    assert daily["open_interest"].iloc[0] == 100


# ---------- 读入 ----------

def test_load_minutes_merges_files_dedups_and_drops_nonpositive_prices(tmp_path):
    base = tmp_path / "月份连续"
    for month in ("01月", "05月"):
        (base / month).mkdir(parents=True)
    a = minute_bars({DAYS[0]: 100.0, DAYS[1]: 101.0}, {DAYS[0]: 5, DAYS[1]: 5}, night=False)
    bad = a.copy()
    bad.loc[0, ["open", "high", "low", "close"]] = 0.0
    bad.to_csv(base / "01月" / "C_01月_1m.csv", index=False, encoding="utf-8-sig")
    a.iloc[10:].to_csv(base / "05月" / "C_05月_1m.csv", index=False, encoding="utf-8-sig")   # 与上一个文件重叠
    out = im.load_minutes(tmp_path, "C")
    d = out["C2105"]
    assert d["datetime"].is_unique and d["datetime"].is_monotonic_increasing
    assert (d[["open", "close"]] > 0).all(axis=None) and len(d) == len(a) - 1


def test_load_minutes_reports_missing_columns_clearly(tmp_path):
    (tmp_path / "月份连续" / "01月").mkdir(parents=True)
    pd.DataFrame({"datetime": ["2026-03-02 09:00:00"], "symbol": ["DCE.c2105"], "open": [1.0]}).to_csv(
        tmp_path / "月份连续" / "01月" / "C_01月_1m.csv", index=False)
    with pytest.raises(ValueError, match="缺少必需列"):
        im.load_minutes(tmp_path, "C")


def test_intraday_skips_contracts_that_only_exist_in_the_sina_extension():
    mins, series, rolls = adjusted()
    series = series.copy()
    series.loc[series.index[-1], "contract"] = "C2609"            # 最后一天的主力来自新浪补齐,没有分钟线
    bars = im.build_intraday(mins, series, rolls, CAL, 15)       # 不应因 C2609 不在 minutes 里而 KeyError
    assert "C2609" not in set(bars["contract"])


def test_main_fails_when_sina_gap_fill_failed(tmp_path, monkeypatch, capsys):
    base = tmp_path / "X" / "C_玉米" / "月份连续"
    (base / "05月").mkdir(parents=True)
    (base / "09月").mkdir(parents=True)
    mins = two_contracts()
    mins["C2105"].assign(symbol="DCE.c2105").to_csv(base / "05月" / "C_05月_1m.csv", index=False, encoding="utf-8-sig")
    mins["C2109"].assign(symbol="DCE.c2109").to_csv(base / "09月" / "C_09月_1m.csv", index=False, encoding="utf-8-sig")
    monkeypatch.setattr(im, "trim_illiquid_start", lambda s, r, *a, **k: (s, r))

    def fake_extend(frames, product, failures=None, **k):
        failures.append("C2109")
        return {}

    monkeypatch.setattr(im, "extend_with_sina", fake_extend)
    rc = im.main(["--root", str(tmp_path), "--exchange", "X", "--folder", "C_玉米", "--product", "C"])
    assert rc == 1 and "新浪取数失败" in capsys.readouterr().out


def test_load_minutes_without_files_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        im.load_minutes(tmp_path, "C")


# ---------- 新浪补齐 ----------

def test_extend_with_sina_only_adds_days_after_the_data_end_and_skips_zero_volume():
    frames = {"C2609": im.daily_from_minutes(two_contracts()["C2105"], CAL)}     # 数据结束日(2026-03)之后才到期的合约
    end = frames["C2609"]["datetime"].max()

    def fetch(code):
        if code != "C2609":
            return None
        days = pd.to_datetime(["2026-03-05", "2026-03-10", "2026-03-11", "2026-03-12"])
        return pd.DataFrame({"datetime": days, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                             "volume": [9, 7, 0, 5], "open_interest": 3})

    added = im.extend_with_sina(frames, "C", fetch=fetch)
    assert added == {"C2609": 2}
    d = frames["C2609"]
    assert list(d["datetime"].iloc[-2:]) == [pd.Timestamp("2026-03-10"), pd.Timestamp("2026-03-12")]
    assert (d["datetime"] <= end).sum() == len(DAYS) and d["datetime"].is_monotonic_increasing


def test_extend_with_sina_covers_even_months_too():
    frames = {"C2609": im.daily_from_minutes(two_contracts()["C2105"], CAL)}
    asked = []
    im.extend_with_sina(frames, "C", fetch=lambda code: asked.append(code))
    assert "C2612" in asked and "C2610" in asked and "C2603" in asked


def test_extend_with_sina_reports_which_contracts_failed():
    frames = {"C2609": im.daily_from_minutes(two_contracts()["C2105"], CAL)}
    failures = []

    def boom(code):
        raise RuntimeError("network")

    with pytest.warns(UserWarning):
        im.extend_with_sina(frames, "C", fetch=boom, failures=failures)
    assert "C2609" in failures


def test_extend_with_sina_survives_fetch_errors():
    frames = {"C2609": im.daily_from_minutes(two_contracts()["C2105"], CAL)}

    def boom(code):
        raise RuntimeError("network")

    with pytest.warns(UserWarning):
        assert im.extend_with_sina(frames, "C", fetch=boom) == {}


# ---------- 复权与合成 ----------

def adjusted():
    mins = two_contracts()
    frames = {c: im.daily_from_minutes(d, CAL) for c, d in mins.items()}
    series, rolls = build_continuous(frames)
    return mins, series, rolls


def test_roll_happens_the_day_after_open_interest_flips_and_gap_is_exact():
    _, series, rolls = adjusted()
    assert len(rolls) == 1 and rolls.iloc[0]["to"] == "C2109" and rolls.iloc[0]["date"] == DAYS[3]
    assert rolls.iloc[0]["gap"] == (120 + 2) - (100 + 2)                 # 前一个共同交易日(第 3 天)两合约收盘价之差


def test_intraday_bars_are_continuous_across_the_roll():
    mins, series, rolls = adjusted()
    bars = im.build_intraday(mins, series, rolls, CAL, 15)
    day3 = bars[bars["trading_day"] == DAYS[2]]["close"].iloc[-1]
    first_new = bars[bars["trading_day"] == DAYS[3]]["open"].iloc[0]
    # 旧合约第 3 天收盘 102 + 价差 20 = 122;新合约第 4 天水平 123,无缝衔接(只差一天的真实波动 1 点)
    assert day3 == 122.0 and first_new == 123.0
    assert bars[bars["trading_day"] == DAYS[3]]["contract"].eq("C2109").all()
    assert bars[bars["trading_day"] == DAYS[2]]["contract"].eq("C2105").all()
    assert bars["close"].iloc[-1] == 125.0                                # 最新价格不动(未复权)


def test_fifteen_minute_buckets_are_aligned_and_labelled_by_start_time():
    mins, series, rolls = adjusted()
    bars = im.build_intraday(mins, series, rolls, CAL, 15)
    day = bars[bars["trading_day"] == DAYS[1]]
    assert list(day["datetime"].dt.strftime("%H:%M")) == ["21:00", "09:00", "09:15"]
    assert list(day["volume"]) == [150, 150, 150]                         # 每桶 15 根 × 10 手
    assert bars["datetime"].is_monotonic_increasing and not im.validate(bars)


def test_intraday_closes_match_the_daily_series():
    mins, series, rolls = adjusted()
    bars = im.build_intraday(mins, series, rolls, CAL, 15)
    last = bars.groupby("trading_day")["close"].last()
    d = series.set_index("datetime")["close"]
    assert (last.reindex(d.index) == d).all()


def test_minutes_beyond_the_last_known_day_are_not_forced_into_it():
    mins = two_contracts()
    extra = mins["C2105"].iloc[:1].copy()
    extra["datetime"] = pd.Timestamp("2026-03-09 21:00")                 # 最后一个日盘之后的夜盘
    mins["C2105"] = pd.concat([mins["C2105"], extra]).reset_index(drop=True)
    daily = im.daily_from_minutes(mins["C2105"], CAL)
    assert daily["datetime"].max() == DAYS[-1] and daily["volume"].iloc[-1] == 450


# ---------- 起点裁剪 / 检查 ----------

def test_trim_illiquid_start_drops_thin_leading_days():
    days = pd.bdate_range("2026-01-05", periods=30)
    s = pd.DataFrame({"datetime": days, "volume": [10] * 10 + [1000] * 20})
    rolls = pd.DataFrame({"date": [days[3], days[15]], "gap": [1.0, 2.0]})
    s2, r2 = im.trim_illiquid_start(s, rolls, fraction=0.5, window=3)
    assert s2["datetime"].iloc[0] == days[10] and list(r2["date"]) == [days[15]]   # 第一段连续 3 个成交充足的日子从第 11 天开始


def test_trim_illiquid_start_rejects_unusable_data():
    s = pd.DataFrame({"datetime": pd.bdate_range("2026-01-05", periods=3), "volume": [0, 0, 0]})
    with pytest.raises(ValueError):
        im.trim_illiquid_start(s, pd.DataFrame({"date": [], "gap": []}))


def test_trim_works_when_the_index_does_not_start_at_zero():
    days = pd.bdate_range("2026-01-05", periods=30)
    s = pd.DataFrame({"datetime": days, "volume": [10] * 10 + [1000] * 20}, index=range(100, 130))
    s2, _ = im.trim_illiquid_start(s, pd.DataFrame({"date": pd.to_datetime([]), "gap": []}), fraction=0.5, window=3)
    assert s2["datetime"].iloc[0] == days[10]


def test_days_without_bars_reports_series_days_missing_from_the_intraday_data():
    mins, series, rolls = adjusted()
    b = mins["C2109"]
    mins["C2109"] = b[im.assign_trading_day(b["datetime"], CAL) != DAYS[4]]          # 新主力在第 5 个交易日的分钟数据整天缺失
    bars = im.build_intraday(mins, series, rolls, CAL, 15)
    assert im.days_without_bars(bars, series) == [DAYS[4]]


def test_validate_flags_each_kind_of_corruption():
    good = pd.DataFrame({"datetime": pd.to_datetime(["2026-01-01 09:00", "2026-01-01 09:15"]),
                         "open": [1.0, 1.0], "high": [2.0, 2.0], "low": [0.5, 0.5], "close": [1.5, 1.5]})
    assert im.validate(good) == []
    assert any("重复" in p for p in im.validate(pd.concat([good, good.iloc[:1]])))
    assert any("OHLC" in p for p in im.validate(good.assign(high=[0.9, 2.0])))
    assert any("非正" in p for p in im.validate(good.assign(low=[-1.0, 0.5])))


# ---------- 端到端 ----------

def test_main_end_to_end_writes_outputs_atomically(tmp_path, monkeypatch, capsys):
    base = tmp_path / "SHFE_x" / "C_玉米" / "月份连续"
    (base / "05月").mkdir(parents=True)
    (base / "09月").mkdir(parents=True)
    mins = two_contracts()
    mins["C2105"].assign(symbol="DCE.c2105").to_csv(base / "05月" / "C_05月_1m.csv", index=False, encoding="utf-8-sig")
    mins["C2109"].assign(symbol="DCE.c2109").to_csv(base / "09月" / "C_09月_1m.csv", index=False, encoding="utf-8-sig")
    monkeypatch.setattr(im, "trim_illiquid_start", lambda s, r, *a, **k: (s, r))      # 合成数据太短,不裁剪
    rc = im.main(["--root", str(tmp_path), "--exchange", "SHFE_x", "--folder", "C_玉米", "--product", "C", "--no-sina"])
    assert rc == 0 and "检查: 通过" in capsys.readouterr().out
    out = tmp_path / "derived"
    assert {p.name for p in out.iterdir()} == {"C_15m_adj.csv", "C_1d_adj.csv", "C_rolls.csv"}   # 无 .tmp 残留
    bars = pd.read_csv(out / "C_15m_adj.csv", parse_dates=["datetime"])
    assert len(bars) == 3 * len(DAYS) and not im.validate(bars)
