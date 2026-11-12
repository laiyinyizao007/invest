"""测试段使用登记:`--test` 只许消费一次,重复使用即拒绝(fail-closed)。"""
from __future__ import annotations

import json
import sys

import pytest

import segment_ledger as sl


def test_unused_segment_passes_check_and_record_marks_it_used(tmp_path):
    ledger = tmp_path / "usage.json"

    sl.check("optimize:CADJ:2024-01-01~2026-11-09", ledger)
    sl.record("optimize:CADJ:2024-01-01~2026-11-09", ledger, note="首次")

    with pytest.raises(sl.SegmentAlreadyUsed, match="已被使用"):
        sl.check("optimize:CADJ:2024-01-01~2026-11-09", ledger)


def test_other_segments_are_unaffected(tmp_path):
    ledger = tmp_path / "usage.json"
    sl.record("a", ledger)

    sl.check("b", ledger)


def test_record_twice_is_refused_and_keeps_first_entry(tmp_path):
    ledger = tmp_path / "usage.json"
    sl.record("a", ledger, note="first")

    with pytest.raises(sl.SegmentAlreadyUsed):
        sl.record("a", ledger, note="second")
    assert json.loads(ledger.read_text(encoding="utf-8"))["a"]["note"] == "first"


def test_corrupt_ledger_fails_closed(tmp_path):
    ledger = tmp_path / "usage.json"
    ledger.write_text("{not json", encoding="utf-8")

    with pytest.raises(sl.LedgerError):
        sl.check("a", ledger)


def test_shipped_ledger_marks_15m_baseline_test_segment_as_used():
    # ADR baseline-15m:15 分钟基准测试段已被 optimize_15m.py --test 使用过一次
    import optimize_15m as o15

    with pytest.raises(sl.SegmentAlreadyUsed):
        sl.check(o15.segment_key(), sl.bootstrap.ROOT / "config" / "test_segment_usage.json")


def test_optimize_15m_refuses_second_test_run_before_any_work(monkeypatch, capsys):
    import optimize_15m as o15
    sl.record(o15.segment_key())
    monkeypatch.setattr(o15, "grid", lambda: pytest.fail("被拒绝后不应开始任何计算"))

    assert o15.main(["--test"]) == 2
    assert "拒绝运行" in capsys.readouterr().err


def test_optimize_daily_refuses_second_test_run(monkeypatch):
    import optimize
    sl.record(optimize.segment_key("CADJ"))
    monkeypatch.setattr(sys, "argv", ["optimize.py", "--symbol", "CADJ", "--test"])
    monkeypatch.setattr(optimize, "grid", lambda: pytest.fail("被拒绝后不应开始任何计算"))

    with pytest.raises(SystemExit, match="拒绝运行"):
        optimize.main()


def test_test_run_registers_segment_before_evaluating_test_window(monkeypatch, tmp_path):
    import optimize_15m as o15
    monkeypatch.setattr(o15.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(o15, "grid", lambda: [(0, 0, 0, o15.FAST[0], o15.SLOW[0], o15.MULT[0])])
    seen = {}

    def fake_eval(params, window, cost_multiplier=1.0):
        if window == "test":
            seen["registered"] = sl.LEDGER.exists() and o15.segment_key() in json.loads(sl.LEDGER.read_text(encoding="utf-8"))
        return {"return_pct": 1.0, "maxdd_pct": -1.0, "maxdd_yuan": -100.0, "sharpe": 1.0, "round_trips": 200,
                "fills": 400, "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 50.0}

    monkeypatch.setattr(o15, "evaluate", fake_eval)
    monkeypatch.setattr(o15, "smoothed", lambda scores: {k: 1.0 for k in scores})

    assert o15.main(["--test"]) == 0
    assert seen["registered"] is True
