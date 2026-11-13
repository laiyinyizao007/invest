"""G8 第 3 次 codex 复核(42.5/50)的发现:尾行截断、研究结果矛盾、路径净化残余、Python 版本兼容。"""
import json

import pytest

from dashboard import collectors as col
from dashboard import safe
from evidence import logstats as ls

NOW = 1_800_000_000.0


# ---------------------------------------------------------------- High:尾行停在多字节字符中间
def _tail_log(tmp_path, severe_cut):
    prefix = "2026-11-13 08:00:00 | INFO | Runtime | 已启动\n".encode("utf-8")
    critical = "2026-11-13 09:00:00 | ERROR | Runtime | 硬锁定".encode("utf-8")
    p = tmp_path / "vt_20261113.log"
    p.write_bytes(prefix + (critical[:-1] if severe_cut else critical))
    return p


def test_a_tail_cut_inside_a_multibyte_character_is_not_reported_as_a_clean_day(tmp_path):
    """被截断的可能正是『硬锁定』的最后一个字:面板无法证明它不是关键字的一部分,不能把这一天算作无异常。"""
    stats = ls.parse_day_log(_tail_log(tmp_path, severe_cut=True))
    assert stats.trusted is False


def test_the_same_line_completed_is_recognised_as_severe(tmp_path):
    stats = ls.parse_day_log(_tail_log(tmp_path, severe_cut=False))
    assert stats.trusted is True and stats.has_severe is True


def test_a_tail_that_is_completed_later_recovers_trust_in_the_incremental_parser():
    """正在写入时的瞬间状态不能永久污染:后续追加使字符完整后,可信度应当恢复。"""
    prefix = "2026-11-13 08:00:00 | INFO | Runtime | 已启动\n".encode("utf-8")
    critical = "2026-11-13 09:00:00 | ERROR | Runtime | 硬锁定\n".encode("utf-8")
    parser = ls.DayLogParser("2026-11-13")
    parser.feed(prefix + critical[:-2])
    assert parser.snapshot("x").trusted is False
    parser.feed(critical[-2:])
    snap = parser.snapshot("x")
    assert snap.trusted is True and snap.has_severe is True


# ---------------------------------------------------------------- Medium:研究结果的矛盾组合
BASE = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r", "ran_at": "2026-11-12T21:00:00"}
RID = "20261112-210000-000000"


def _ok(body):
    return col._valid_run(RID, {**BASE, "run_id": RID, **body})


@pytest.mark.parametrize("body", [
    {"kind": "gate", "status": "strategy_error", "passed": True, "error": {"type": "X", "message": "m"}},
    {"kind": "evaluate", "status": "complete", "passed": True, "failures": []},
    {"kind": "gate", "status": "ok", "passed": False, "failures": ["f"]},
    {"kind": "gate", "passed": True, "failures": ["不应该有失败项却通过"]},
    {"kind": "gate", "passed": False, "failures": []},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {}},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {"type": 1, "message": "m"}},
    {"kind": "backtest", "status": "complete", "stats": {"x": 1}},
    {"kind": "gate", "passed": False, "failures": ["f"], "ran_at": "not-a-date"},
])
def test_contradictory_or_unknown_result_shapes_are_rejected(body):
    assert not _ok(body)


@pytest.mark.parametrize("body", [
    {"kind": "gate", "passed": True, "failures": []},
    {"kind": "gate", "passed": False, "failures": ["f"]},
    {"kind": "evaluate", "passed": False, "failures": ["无法选参: x"], "kind_detail": "selection-only", "params": None},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {"type": "ValueError", "message": "m"}},
    {"kind": "backtest", "status": "strategy_error", "error": {"type": "X", "message": "m"}},
    {"kind": "backtest", "stats": {"sharpe_ratio": 1.2}},
])
def test_every_real_result_shape_is_still_accepted(body):
    assert _ok(body)


# ---------------------------------------------------------------- Medium:路径净化残余
@pytest.mark.parametrize("text,leak", [
    (r"读取 C:\Users\Alice Smith\secret.txt 失败", "Smith"),
    (r"读取 C:\Users\Alice Smith\secret.txt 失败", "secret"),
    (r"at C:\Program Files\Vendor Tool\bin\x.exe line 3", "Vendor"),
    ("Ｃ:\\Users\\alice\\x", "alice"),
    ("Ｚ:\\Users\\alice\\x", "alice"),
    ("C\u200b:\\Users\\alice\\x", "alice"),
    ("C:\u200b\\Users\\alice\\x", "alice"),
    ("C:\\Users\\al\u200bice\\x", "alice"),
    ("C%252525253A%252525255CUsers%252525255Calice", "alice"),
])
def test_remaining_leaks_from_the_third_review_are_scrubbed(text, leak):
    assert leak not in safe.scrub_text(text), safe.scrub_text(text)


@pytest.mark.parametrize("text", [
    "url https://example.com/a:/b", "time 10:30 ratio 3:4", "see http://host/x:/y/z", "a plain sentence: nothing here",
])
def test_the_path_rules_do_not_rewrite_urls_or_ordinary_text(text):
    assert safe.scrub_text(text) == text


# ---------------------------------------------------------------- Low:不依赖 Path.is_junction
def test_link_detection_works_without_path_is_junction(monkeypatch, tmp_path):
    from pathlib import Path

    from dashboard import config
    monkeypatch.delattr(Path, "is_junction", raising=False)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert config._is_link(plain) is False
    assert config._is_link(tmp_path / "missing") is False
