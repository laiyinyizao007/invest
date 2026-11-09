"""scripts/build_continuous.py 测试:合成合约数据,结果可手算。"""
import os
import sys
import time
import warnings
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import build_continuous as bc  # noqa: E402  (内部会先 import bootstrap)


def contract(start, closes, holds, volume=100):
    """closes/holds 逐日列表;OHLC 简化为 开=收=close,高低 ±1。"""
    idx = pd.bdate_range(start, periods=len(closes))
    return pd.DataFrame({
        "datetime": idx, "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes],
        "close": closes, "volume": volume, "open_interest": holds,
    })


def two_contracts(basis=10.0, flat=100.0):
    """旧合约 2501 价格恒为 flat,新合约 2505 = 旧合约 + basis;
    第 6 个交易日(下标 5)起新合约持仓量反超 → 按"前一日持仓量"规则,第 7 个交易日(下标 6)才换月。"""
    n = 10
    old = contract("2024-01-01", [flat] * n, [1000] * 5 + [500] * 5)
    new = contract("2024-01-01", [flat + basis] * n, [100] * 5 + [900] * 5)
    return {"C2501": old, "C2505": new}


def test_contract_expiry_parsing():
    assert bc.contract_expiry("C2509") == 2509
    assert bc.contract_expiry("MA1905") == 1905
    with pytest.raises(ValueError):
        bc.contract_expiry("CXX")


# ---------- 换月规则 ----------

def test_roll_uses_previous_day_open_interest_no_lookahead():
    """持仓量在下标 5 反超;下标 5 当天仍用旧合约(那天的持仓量收盘后才知道),下标 6 起才换新合约。"""
    series, rolls = bc.build_continuous(two_contracts())
    assert series["contract"].tolist() == ["C2501"] * 6 + ["C2505"] * 4
    assert len(rolls) == 1
    assert (rolls.iloc[0]["from"], rolls.iloc[0]["to"]) == ("C2501", "C2505")
    assert rolls.iloc[0]["date"] == pd.bdate_range("2024-01-01", periods=10)[6]


def test_never_rolls_back_to_an_earlier_contract():
    old = contract("2024-01-01", [100.0] * 10, [1000, 400, 400, 400, 400, 400, 1200, 1200, 1200, 1200])
    new = contract("2024-01-01", [110.0] * 10, [100, 500, 500, 500, 500, 500, 100, 100, 100, 100])
    series, rolls = bc.build_continuous({"C2501": old, "C2505": new})
    assert len(rolls) == 1                                              # 旧合约持仓量回升也不换回
    assert series["contract"].iloc[-1] == "C2505"


def test_halted_current_contract_does_not_trigger_a_false_roll():
    """主力某天没有成交(volume=0)但之后还会交易,而新合约持仓量很小:不得换月(审查发现的 bug)。"""
    old = contract("2024-01-01", [100.0] * 8, [1000] * 8)
    old.loc[3, "volume"] = 0
    new = contract("2024-01-01", [110.0] * 8, [50] * 8)
    series, rolls = bc.build_continuous({"C2501": old, "C2505": new})
    assert rolls.empty
    assert set(series["contract"]) == {"C2501"}
    assert len(series) == 7                                             # 停牌日不产生 K 线
    assert (series["close"] == 100.0).all()                             # 没有凭空出现的 10 点跳空


def test_forced_roll_when_current_contract_expires():
    """旧合约不再有数据(到期)时强制换到可用的更远月合约。"""
    old = contract("2024-01-01", [100.0] * 4, [1000] * 4)
    new = contract("2024-01-01", [110.0] * 8, [100] * 8)
    series, rolls = bc.build_continuous({"C2501": old, "C2505": new})
    assert series["contract"].tolist() == ["C2501"] * 4 + ["C2505"] * 4
    assert rolls.iloc[0]["gap"] == pytest.approx(10.0)
    assert bool(rolls.iloc[0]["gap_exact"]) is True


def test_zero_volume_days_are_ignored():
    old = contract("2024-01-01", [100.0] * 6, [1000] * 6)
    old.loc[2, "volume"] = 0
    series, _ = bc.build_continuous({"C2501": old})
    assert len(series) == 5


# ---------- 复权 ----------

def test_back_adjustment_removes_the_roll_gap():
    series, rolls = bc.build_continuous(two_contracts(basis=10.0))
    assert rolls.iloc[0]["gap"] == pytest.approx(10.0)                  # 新 - 旧 = +10
    assert series["raw_close"].diff().abs().max() == pytest.approx(10.0)    # 未复权序列在换月处跳 10 点
    assert series["close"].diff().abs().max() == pytest.approx(0.0)     # 复权后连续
    assert series["close"].iloc[-1] == pytest.approx(110.0)             # 最新价格保持真实
    assert series["close"].iloc[0] == pytest.approx(110.0)              # 早期价格被平移到同一水平


def test_point_ranges_are_preserved_by_difference_method():
    """差价法只平移价格,每根 K 线的点数波幅(ATR 的来源)不变。"""
    series, _ = bc.build_continuous(two_contracts())
    assert ((series["high"] - series["low"]) == 2.0).all()


def test_three_contracts_accumulate_adjustments():
    a = contract("2024-01-01", [100.0] * 10, [1000] * 4 + [100] * 6)
    b = contract("2024-01-01", [110.0] * 10, [100] * 4 + [1000] * 3 + [100] * 3)
    c = contract("2024-01-01", [125.0] * 10, [10] * 7 + [1000] * 3)
    series, rolls = bc.build_continuous({"C2501": a, "C2505": b, "C2509": c})
    assert rolls["gap"].tolist() == pytest.approx([10.0, 15.0])
    assert series["close"].diff().abs().max() == pytest.approx(0.0)
    assert series["close"].iloc[-1] == pytest.approx(125.0)
    assert series["close"].iloc[0] == pytest.approx(125.0)


def test_no_overlap_gap_is_approximated_and_flagged_not_silently_zero():
    """新旧合约没有共同交易日:价差用"新首个收盘 - 旧最后收盘"近似,标记 gap_exact=False 并告警(审查发现的 bug)。"""
    old = contract("2024-01-01", [100.0] * 4, [1000] * 4)
    new = contract("2024-01-08", [110.0] * 4, [900] * 4)
    with pytest.warns(UserWarning, match="没有共同交易日"):
        series, rolls = bc.build_continuous({"C2501": old, "C2505": new})
    assert rolls.iloc[0]["gap"] == pytest.approx(10.0)
    assert bool(rolls.iloc[0]["gap_exact"]) is False
    assert series["close"].diff().abs().max() == pytest.approx(0.0)


def test_degenerate_roll_gap_warns_instead_of_silently_returning_zero():
    """新旧合约数据完全不衔接(旧合约在换月日之前没有任何数据):单独警告,不静默。"""
    day = pd.Timestamp("2024-01-10")
    data = {
        "C2501": contract("2024-01-15", [100.0] * 3, [10] * 3).set_index("datetime"),   # 全部晚于换月日
        "C2505": contract("2024-01-01", [110.0] * 3, [10] * 3).set_index("datetime"),   # 全部早于换月日
    }
    with pytest.warns(UserWarning, match="无法计算价差"):
        assert bc._roll_gap(data, "C2501", "C2505", day) == (0.0, False)


def test_main_uses_instrument_size_for_yuan_conversion(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bc.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(bc, "INSTRUMENTS", {"C": type("I", (), {"size": 5})()})
    monkeypatch.setattr(sys, "argv", ["build_continuous.py", "--symbol", "C", "--first-year", "2025", "--last-year", "2025"])
    frames = two_contracts(basis=10.0)
    monkeypatch.setattr(bc, "fetch_contract", lambda code: frames.get(code))
    assert bc.main() == 0
    assert "折合每手 50 元" in capsys.readouterr().out        # 价差 10 点 × 5 吨


def test_non_positive_adjusted_prices_emit_a_warning():
    """旧合约早期价格很低、换月前涨得很高,而远月贴水很深:早期价格被平移后会变成负数。"""
    holds_old, holds_new = [1000] * 3 + [100] * 3, [100] * 3 + [1000] * 3
    normal_old = contract("2024-01-01", [100.0] * 6, holds_old)
    normal_new = contract("2024-01-01", [110.0] * 6, holds_new)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        bc.build_continuous({"C2501": normal_old, "C2505": normal_new})      # 正常情形不告警

    old = contract("2024-01-01", [10.0, 10.0, 10.0, 300.0, 300.0, 300.0], holds_old)
    new = contract("2024-01-01", [100.0] * 6, holds_new)                      # 换月时价差 = 100 - 300 = -200
    with pytest.warns(UserWarning, match="非正价格"):
        series, _ = bc.build_continuous({"C2501": old, "C2505": new})
    assert series["close"].min() < 0


def test_single_contract_has_no_rolls_and_no_adjustment():
    series, rolls = bc.build_continuous({"C2501": contract("2024-01-01", [100.0, 101.0, 102.0], [10, 10, 10])})
    assert rolls.empty
    assert series["close"].tolist() == series["raw_close"].tolist() == [100.0, 101.0, 102.0]


def test_empty_input_is_rejected():
    with pytest.raises(ValueError):
        bc.build_continuous({})


def test_candidate_codes():
    assert bc.candidate_codes("C", 2024, 2025) == ["C2401", "C2405", "C2409", "C2501", "C2505", "C2509"]


# ---------- 取数与缓存 ----------

TODAY = date(2026, 11, 9)


@pytest.fixture
def api(monkeypatch, tmp_path):
    """假的新浪接口 + 临时缓存目录;记录调用次数,不联网、不真实等待。"""
    monkeypatch.setattr(bc, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(bc.time, "sleep", lambda s: None)
    state = {"calls": [], "mode": "ok"}

    def fake(symbol):
        state["calls"].append(symbol)
        if state["mode"] == "keyerror":
            raise KeyError("none")
        if state["mode"] == "boom":
            raise ConnectionError("down")
        return pd.DataFrame({"date": ["2025-01-02", "2025-01-03"], "open": [1.0, 1.0], "high": [2.0, 2.0],
                             "low": [0.5, 0.5], "close": [1.5, 1.5], "volume": [10, 10], "hold": [5, 5], "settle": [1.0, 1.0]})

    monkeypatch.setattr(bc.ak, "futures_zh_daily_sina", fake)
    monkeypatch.setattr(bc, "fetch_with_retry", lambda func, **kw: func(**kw))
    return state


def test_expired_contract_is_cached_forever(api, tmp_path):
    df = bc.fetch_contract("C2505", today=TODAY)                          # 2505 早于 2026-11,已到期
    assert len(df) == 2 and api["calls"] == ["C2505"]
    old = time.time() - 30 * 24 * 3600
    os.utime(tmp_path / "C2505.csv", (old, old))                          # 即使缓存很旧
    assert len(bc.fetch_contract("C2505", today=TODAY)) == 2
    assert api["calls"] == ["C2505"]                                      # 也不再请求
    assert [p.name for p in tmp_path.iterdir()] == ["C2505.csv"]          # 原子写,不残留 .tmp


def test_unexpired_contract_cache_expires_after_a_day(api, tmp_path):
    bc.fetch_contract("C2701", today=TODAY)
    bc.fetch_contract("C2701", today=TODAY)
    assert api["calls"] == ["C2701"]                                      # 24 小时内走缓存
    old = time.time() - 2 * 24 * 3600
    os.utime(tmp_path / "C2701.csv", (old, old))
    bc.fetch_contract("C2701", today=TODAY)
    assert api["calls"] == ["C2701", "C2701"]                             # 过期后刷新


def test_missing_expired_contract_is_negative_cached(api):
    api["mode"] = "keyerror"
    assert bc.fetch_contract("C1801", today=TODAY) is None
    assert bc.fetch_contract("C1801", today=TODAY) is None
    assert api["calls"] == ["C1801"]                                      # 确认不存在的老合约不重复请求


def test_missing_future_contract_is_not_cached(api, tmp_path):
    """尚未上市合约的"取不到"不能永久缓存成"不存在"(审查发现的 bug)。"""
    api["mode"] = "keyerror"
    assert bc.fetch_contract("C2709", today=TODAY) is None
    assert list(tmp_path.iterdir()) == []
    api["mode"] = "ok"
    assert len(bc.fetch_contract("C2709", today=TODAY)) == 2              # 之后上市了能取到


def test_network_errors_propagate_and_are_not_cached(api, tmp_path):
    api["mode"] = "boom"
    with pytest.raises(ConnectionError):
        bc.fetch_contract("C2505", today=TODAY)
    assert list(tmp_path.iterdir()) == []


def test_corrupted_cache_is_refetched(api, tmp_path):
    (tmp_path / "C2505.csv").write_bytes(b"\x00\xff not a csv \x00")
    assert len(bc.fetch_contract("C2505", today=TODAY)) == 2
    assert api["calls"] == ["C2505"]


# ---------- 写入 vn.py 数据库 ----------

def test_save_to_database_writes_adjusted_daily_bars_under_adj_symbol(monkeypatch):
    saved = []

    class FakeDb:
        def save_bar_data(self, bars):
            saved.extend(bars)

    monkeypatch.setattr(bc, "get_database", lambda: FakeDb())
    series, _ = bc.build_continuous(two_contracts(basis=10.0))
    vt = bc.save_to_database(series, "C")
    assert vt == "CADJ.DCE"
    assert len(saved) == len(series)
    assert {b.symbol for b in saved} == {"CADJ"} and {b.interval.name for b in saved} == {"DAILY"}
    assert saved[0].close_price == pytest.approx(series["close"].iloc[0])        # 存的是复权价,不是原始价
    assert saved[0].close_price != series["raw_close"].iloc[0]


def test_main_save_flag_calls_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bc.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_continuous.py", "--symbol", "C", "--first-year", "2025", "--last-year", "2025", "--save"])
    frames = two_contracts()
    monkeypatch.setattr(bc, "fetch_contract", lambda code: frames.get(code))
    calls = []
    monkeypatch.setattr(bc, "save_to_database", lambda series, code: calls.append((len(series), code)) or "CADJ.DCE")
    assert bc.main() == 0
    assert calls == [(10, "C")] and "已写入 vn.py 数据库: CADJ.DCE" in capsys.readouterr().out


# ---------- main ----------

def test_main_isolates_failures_and_refuses_to_write_partial_series(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bc.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_continuous.py", "--symbol", "C", "--first-year", "2024", "--last-year", "2024"])

    def fake_fetch(code):
        if code == "C2405":
            raise ConnectionError("down")
        return contract("2024-01-01", [100.0] * 5, [10] * 5)

    monkeypatch.setattr(bc, "fetch_contract", fake_fetch)
    assert bc.main() == 1
    out = capsys.readouterr().out
    assert "C2405" in out and "不生成结果" in out
    assert not (tmp_path / "data" / "results").exists()


def test_main_writes_series_and_rolls(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bc.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_continuous.py", "--symbol", "C", "--first-year", "2025", "--last-year", "2025"])
    frames = two_contracts()
    monkeypatch.setattr(bc, "fetch_contract", lambda code: {"C2501": frames["C2501"], "C2505": frames["C2505"]}.get(code))
    assert bc.main() == 0
    assert (tmp_path / "data" / "results" / "C_continuous_adj.csv").exists()
    assert (tmp_path / "data" / "results" / "C_rolls.csv").exists()
    assert "换月价差" in capsys.readouterr().out


def test_main_with_no_data_returns_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(bc.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["build_continuous.py", "--symbol", "C", "--first-year", "2025", "--last-year", "2025"])
    monkeypatch.setattr(bc, "fetch_contract", lambda code: None)
    assert bc.main() == 1
    assert "没有取到任何合约数据" in capsys.readouterr().out
