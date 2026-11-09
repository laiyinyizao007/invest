"""scripts/fetch_data.py 纯函数单元测试:不联网、不写真实 vn.py 数据库。"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fetch_data  # noqa: E402  (内部会先 import bootstrap)
from vnpy.trader.constant import Exchange, Interval  # noqa: E402

MIN_COLS = ["datetime", "open", "high", "low", "close", "volume", "hold"]
DAY_COLS = ["date", "open", "high", "low", "close", "volume", "hold", "settle"]
OUT_COLS = ["datetime", "open", "high", "low", "close", "volume", "open_interest"]


def minute_df(rows):
    return pd.DataFrame(rows, columns=MIN_COLS)


def day_df(rows):
    return pd.DataFrame(rows, columns=DAY_COLS)


# ---------- normalize ----------

@pytest.mark.parametrize(
    "period, raw_ts, start_ts",
    [
        ("1", "2026-11-09 09:01:00", "2026-11-09 09:00:00"),   # 日盘首根
        ("1", "2026-11-09 23:00:00", "2026-11-09 22:59:00"),   # 夜盘末根
        ("5", "2026-11-09 09:05:00", "2026-11-09 09:00:00"),
        ("15", "2026-11-09 10:15:00", "2026-11-09 10:00:00"),
        ("15", "2026-11-09 13:45:00", "2026-11-09 13:30:00"),   # 午休后首根
    ],
)
def test_minute_bars_shift_end_time_to_start_time(period, raw_ts, start_ts):
    out = fetch_data.normalize(minute_df([[raw_ts, 10, 12, 9, 11, 100, 500]]), period)
    assert out["datetime"].iloc[0] == pd.Timestamp(start_ts)


def test_daily_bars_keep_date_and_rename_columns():
    raw = day_df([["2026-11-09", 10, 12, 9, 11, 100, 500, 10.5]])
    out = fetch_data.normalize(raw, "d")
    assert list(out.columns) == OUT_COLS
    assert out["datetime"].iloc[0] == pd.Timestamp("2026-11-09")
    assert out["open_interest"].iloc[0] == 500


def test_sorted_deduplicated_and_index_reset():
    raw = minute_df([
        ["2026-11-09 09:03:00", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:01:00", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:01:00", 10, 12, 9, 11, 7, 1],  # 重复时间戳,volume 不同
    ])
    out = fetch_data.normalize(raw, "1")
    assert out["datetime"].is_monotonic_increasing
    assert len(out) == 2
    assert list(out.index) == [0, 1]
    kept = out[out["datetime"] == pd.Timestamp("2026-11-09 09:00:00")]
    assert kept["volume"].iloc[0] == 1  # 同一次抓取内的重复行保留第一条


def test_rows_with_nan_are_dropped():
    raw = minute_df([
        ["2026-11-09 09:01:00", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:02:00", None, 12, 9, 11, 1, 1],
    ])
    assert len(fetch_data.normalize(raw, "1")) == 1


def test_non_positive_prices_are_dropped():
    raw = minute_df([
        ["2026-11-09 09:01:00", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:02:00", 0, 12, 9, 11, 1, 1],
        ["2026-11-09 09:03:00", 10, 12, -1, 11, 1, 1],
    ])
    assert len(fetch_data.normalize(raw, "1")) == 1


def test_open_above_high_is_repaired_not_dropped():
    """L 2007-07-31 上市首日:开盘价高于最高价。"""
    raw = day_df([["2007-07-31", 12780, 12625, 12450, 12580, 21556, 2428, 0]])
    out = fetch_data.normalize(raw, "d")
    assert len(out) == 1
    assert out["high"].iloc[0] == 12780
    assert out["low"].iloc[0] == 12450


def test_close_below_low_is_repaired_not_dropped():
    """PP 2020-08-06:收盘价低于最低价。"""
    raw = day_df([["2020-08-06", 7774, 7814, 7737, 7727, 320263, 242096, 0]])
    out = fetch_data.normalize(raw, "d")
    assert out["high"].iloc[0] == 7814
    assert out["low"].iloc[0] == 7727


def test_consistent_row_is_unchanged():
    raw = day_df([["2026-11-09", 10, 12, 9, 11, 100, 500, 10.5]])
    out = fetch_data.normalize(raw, "d")
    assert (out["high"].iloc[0], out["low"].iloc[0]) == (12, 9)


def test_bad_timestamp_is_dropped_without_error():
    raw = minute_df([
        ["not-a-date", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:01:00", 10, 12, 9, 11, 1, 1],
    ])
    assert len(fetch_data.normalize(raw, "1")) == 1


# ---------- to_bars ----------

def test_to_bars_maps_fields_and_timezone():
    df = fetch_data.normalize(
        minute_df([["2026-11-09 09:01:00", 10, 12, 9, 11, 100, 500]]), "1"
    )
    bars = fetch_data.to_bars(df, "PP", Exchange.DCE, Interval.MINUTE)
    assert len(bars) == 1
    bar = bars[0]
    assert bar.symbol == "PP0"
    assert bar.exchange == Exchange.DCE
    assert bar.interval == Interval.MINUTE
    assert bar.gateway_name == "AKSHARE"
    assert bar.datetime.tzinfo == fetch_data.DB_TZ
    assert bar.datetime.replace(tzinfo=None) == pd.Timestamp("2026-11-09 09:00:00")
    assert (bar.open_price, bar.high_price, bar.low_price, bar.close_price) == (10, 12, 9, 11)
    assert (bar.volume, bar.open_interest, bar.turnover) == (100, 500, 0.0)


# ---------- save_csv ----------

@pytest.fixture
def csv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch_data, "CSV_DIR", tmp_path)
    return tmp_path


def make_norm(rows):
    return fetch_data.normalize(minute_df(rows), "15")


def test_save_csv_first_write_and_format(csv_dir):
    df = make_norm([["2026-11-09 09:15:00", 10, 12, 9, 11, 1, 1]])
    fetch_data.save_csv(df, "PP", "15")
    path = csv_dir / "PP_15m.csv"
    assert path.exists()
    assert "2026-11-09 09:00:00" in path.read_text(encoding="utf-8")


def test_save_csv_merge_keeps_latest_and_sorted(csv_dir):
    first = make_norm([
        ["2026-11-09 09:15:00", 10, 12, 9, 11, 1, 1],
        ["2026-11-09 09:30:00", 10, 12, 9, 11, 2, 2],
    ])
    fetch_data.save_csv(first, "PP", "15")
    second = make_norm([
        ["2026-11-09 09:30:00", 20, 22, 19, 21, 9, 9],   # 与旧数据同时间戳
        ["2026-11-09 09:45:00", 10, 12, 9, 11, 3, 3],
    ])
    merged = fetch_data.save_csv(second, "PP", "15")

    assert len(merged) == 3
    assert merged["datetime"].is_monotonic_increasing
    overwritten = merged[merged["datetime"] == pd.Timestamp("2026-11-09 09:15:00")]
    assert overwritten["volume"].iloc[0] == 9           # 新数据覆盖旧数据
    on_disk = pd.read_csv(csv_dir / "PP_15m.csv")
    assert len(on_disk) == 3


def test_save_csv_writes_tmp_then_replaces(csv_dir, monkeypatch):
    """原子写:先写 .csv.tmp,再 os.replace 到目标;替换发生时临时文件内容已完整。"""
    df = make_norm([["2026-11-09 09:15:00", 10, 12, 9, 11, 1, 1]])
    calls = []
    real_replace = fetch_data.os.replace

    def spy(src, dst):
        calls.append((Path(src).name, Path(dst).name, Path(src).read_text(encoding="utf-8")))
        real_replace(src, dst)

    monkeypatch.setattr(fetch_data.os, "replace", spy)
    fetch_data.save_csv(df, "PP", "15")
    assert len(calls) == 1
    src_name, dst_name, content = calls[0]
    assert (src_name, dst_name) == ("PP_15m.csv.tmp", "PP_15m.csv")
    assert "2026-11-09 09:00:00" in content


def test_save_csv_failure_keeps_existing_file(csv_dir, monkeypatch):
    """写临时文件中途失败时,已积累的旧 CSV 必须原样保留。"""
    df = make_norm([["2026-11-09 09:15:00", 10, 12, 9, 11, 1, 1]])
    fetch_data.save_csv(df, "PP", "15")
    path = csv_dir / "PP_15m.csv"
    before = path.read_text(encoding="utf-8")

    def broken_to_csv(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_csv", broken_to_csv)
    new = make_norm([["2026-11-09 09:30:00", 10, 12, 9, 11, 2, 2]])
    with pytest.raises(OSError):
        fetch_data.save_csv(new, "PP", "15")
    assert path.read_text(encoding="utf-8") == before


def test_save_csv_leaves_no_tmp_file(csv_dir):
    df = make_norm([["2026-11-09 09:15:00", 10, 12, 9, 11, 1, 1]])
    fetch_data.save_csv(df, "PP", "15")
    fetch_data.save_csv(df, "PP", "15")
    assert [p.name for p in csv_dir.iterdir()] == ["PP_15m.csv"]


# ---------- fetch_with_retry ----------

@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """不真实等待,但记录每次退避延迟,用于验证退避节奏。"""
    recorded = []
    monkeypatch.setattr(fetch_data.time, "sleep", recorded.append)
    return recorded


def flaky(failures, exc):
    calls = {"n": 0}

    def func(**kwargs):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc
        return "ok"

    return func, calls


def test_retry_success_first_try():
    func, calls = flaky(0, ConnectionError())
    assert fetch_data.fetch_with_retry(func) == "ok"
    assert calls["n"] == 1


def test_retry_recovers_after_transient_errors(sleeps):
    func, calls = flaky(2, ConnectionError("boom"))
    assert fetch_data.fetch_with_retry(func) == "ok"
    assert calls["n"] == 3
    assert sleeps == [2.0, 4.0]  # 指数退避 BACKOFF_BASE * 2**attempt


def test_retry_gives_up_after_max_retries(sleeps):
    func, calls = flaky(99, ConnectionError("down"))
    with pytest.raises(ConnectionError):
        fetch_data.fetch_with_retry(func)
    assert calls["n"] == fetch_data.MAX_RETRIES
    assert sleeps == [2.0, 4.0, 8.0]  # 最后一次失败不再等待


@pytest.mark.parametrize("exc", [KeyError("col"), TypeError("x"), AttributeError("y")])
def test_non_transient_errors_fail_fast(exc):
    func, calls = flaky(99, exc)
    with pytest.raises(type(exc)):
        fetch_data.fetch_with_retry(func)
    assert calls["n"] == 1


def test_value_error_is_retried():
    func, calls = flaky(1, ValueError("bad json"))
    assert fetch_data.fetch_with_retry(func) == "ok"
    assert calls["n"] == 2


# ---------- run_one / main(用假的 AKShare 与数据库,不联网、不写真实库) ----------

class FakeDB:
    def __init__(self):
        self.saved = []

    def save_bar_data(self, bars):
        self.saved.append(bars)
        return True


@pytest.fixture
def fake_db(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(fetch_data, "get_database", lambda: db)
    return db


def test_run_one_daily_goes_to_database(monkeypatch, fake_db):
    raw = day_df([["2026-11-09", 10, 12, 9, 11, 100, 500, 10.5]])
    seen = []
    monkeypatch.setattr(
        fetch_data.ak, "futures_zh_daily_sina", lambda symbol: seen.append(symbol) or raw
    )
    fetch_data.run_one("PP", Exchange.DCE, "d")
    assert seen == ["PP0"]
    assert len(fake_db.saved) == 1
    assert fake_db.saved[0][0].interval == Interval.DAILY


def test_run_one_1m_goes_to_database(monkeypatch, fake_db):
    raw = minute_df([["2026-11-09 09:01:00", 10, 12, 9, 11, 100, 500]])
    seen = []
    monkeypatch.setattr(
        fetch_data.ak, "futures_zh_minute_sina",
        lambda symbol, period: seen.append((symbol, period)) or raw,
    )
    fetch_data.run_one("PP", Exchange.DCE, "1")
    assert seen == [("PP0", "1")]
    assert fake_db.saved[0][0].interval == Interval.MINUTE


def test_run_one_15m_goes_to_csv_not_database(monkeypatch, fake_db, csv_dir):
    raw = minute_df([["2026-11-09 09:15:00", 10, 12, 9, 11, 100, 500]])
    seen = []
    monkeypatch.setattr(
        fetch_data.ak, "futures_zh_minute_sina",
        lambda symbol, period: seen.append((symbol, period)) or raw,
    )
    fetch_data.run_one("PP", Exchange.DCE, "15")
    assert seen == [("PP0", "15")]  # period 原样透传
    assert fake_db.saved == []
    assert (csv_dir / "PP_15m.csv").exists()


def test_run_one_raises_when_no_valid_rows(monkeypatch, fake_db):
    raw = minute_df([["2026-11-09 09:01:00", 0, 0, 0, 0, 0, 0]])  # 全是非正价格
    monkeypatch.setattr(fetch_data.ak, "futures_zh_minute_sina", lambda symbol, period: raw)
    with pytest.raises(ValueError):
        fetch_data.run_one("PP", Exchange.DCE, "1")


def test_main_returns_zero_when_all_succeed(monkeypatch):
    monkeypatch.setattr(fetch_data, "run_one", lambda *a: None)
    monkeypatch.setattr(sys, "argv", ["fetch_data.py", "--symbols", "PP", "--periods", "d"])
    assert fetch_data.main() == 0


def test_main_isolates_failures_and_returns_one(monkeypatch, capsys):
    ran = []

    def fake_run_one(code, exchange, period):
        ran.append((code, period))
        if (code, period) == ("PP", "d"):
            raise RuntimeError("boom")

    monkeypatch.setattr(fetch_data, "run_one", fake_run_one)
    monkeypatch.setattr(sys, "argv", ["fetch_data.py", "--symbols", "PP", "L", "--periods", "d"])
    assert fetch_data.main() == 1
    assert ran == [("PP", "d"), ("L", "d")]          # 一个失败不影响其余任务
    assert "PP/d" in capsys.readouterr().out
