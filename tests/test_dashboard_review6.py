"""codex 第四次复核(43.7/50)的 3 个 Medium:尾行前缀过宽、研究结果矛盾字段、编码路径规则未组合。"""
import json

import pytest

from dashboard import collectors as col
from dashboard import safe
from evidence import logstats as ls

PREFIX = "2026-11-13 08:00:00 | INFO | Runtime | 已启动\n".encode("utf-8")


def _parse(data: bytes) -> ls.DayStats:
    parser = ls.DayLogParser("2026-11-13")
    parser.feed(data)
    return parser.snapshot("x")


# ---------------------------------------------------------------- Medium 1:尾行前缀过宽
@pytest.mark.parametrize("message", [
    "当前权益 10000",            # 『当』是『当日熔断』的前缀,但整行不是
    "当前", "普通日志:没有事件", "行情正常",
])
def test_an_ordinary_unterminated_line_is_not_treated_as_hiding_an_event(message):
    """前缀检测曾在整个尾行里搜:『当前权益』里的『当』让普通日志被判不可信。"""
    line = f"2026-11-13 09:00:00 | INFO | Runtime | {message}".encode("utf-8")
    stats = _parse(PREFIX + line)
    assert stats.trusted is True, message


def test_a_complete_severe_event_in_the_last_line_is_still_recognised():
    """已完整识别的严重事件不该因为共享前缀而被降级。"""
    line = "2026-11-13 09:00:00 | ERROR | Risk | 当日熔断".encode("utf-8")
    stats = _parse(PREFIX + line)
    assert stats.trusted is True
    assert stats.has_severe is True and stats.events[("严重", "当日熔断")] == 1


def test_a_severe_event_truncated_at_the_end_is_still_not_trusted():
    line = "2026-11-13 09:00:00 | ERROR | Risk | 当日熔".encode("utf-8")
    assert _parse(PREFIX + line).trusted is False


def test_a_tail_ending_exactly_with_a_marker_prefix_is_still_suspicious():
    """尾行正好以某个关键字的前缀结尾——它确实可能是那个事件写到一半。"""
    line = "2026-11-13 09:00:00 | ERROR | Runtime | 当".encode("utf-8")
    assert _parse(PREFIX + line).trusted is False


def test_a_prefix_in_the_middle_of_the_line_does_not_count():
    """截断只能发生在行尾:中间出现的前缀不是『被藏起来的事件』。"""
    line = "2026-11-13 09:00:00 | ERROR | Runtime | 当 普通信息".encode("utf-8")
    assert _parse(PREFIX + line).trusted is True


# ---------------------------------------------------------------- Medium 2:研究结果的矛盾字段
BASE = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r",
        "ran_at": "2026-11-12T21:00:00", "run_id": "20261112-210000-000000"}


def _ok(body):
    return col._valid_run("20261112-210000-000000", {**BASE, **body})


@pytest.mark.parametrize("body", [
    {"kind": "gate", "passed": True, "failures": [], "error": {"type": "X", "message": "boom"}},
    {"kind": "evaluate", "passed": True, "failures": [], "error": {"type": "X", "message": "boom"}},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {"type": "", "message": ""}},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {"type": "X", "message": "  "}},
    {"kind": "backtest", "stats": {"sharpe": 1.0}, "passed": True},
    {"kind": "backtest", "status": "strategy_error", "error": {"type": "X", "message": "m"}, "passed": False},
])
def test_contradictory_result_shapes_are_rejected(body):
    assert not _ok(body)


@pytest.mark.parametrize("body", [
    {"kind": "gate", "passed": True, "failures": []},
    {"kind": "gate", "passed": False, "failures": ["f"]},
    {"kind": "evaluate", "passed": False, "failures": ["无法选参: x"], "kind_detail": "selection-only"},
    {"kind": "gate", "status": "strategy_error", "passed": False, "error": {"type": "ValueError", "message": "m"}},
    {"kind": "backtest", "stats": {"sharpe_ratio": 1.2}},
    {"kind": "backtest", "status": "strategy_error", "error": {"type": "X", "message": "m"}},
])
def test_real_result_shapes_are_still_accepted(body):
    assert _ok(body)


def test_passed_is_not_exposed_for_a_backtest():
    """backtest 没有『通过』这个概念,展示层也不该给出这个字段。"""
    brief = col._brief_run("20261112-210000-000000", {**BASE, "kind": "backtest", "stats": {"sharpe": 1.0}})
    assert brief["passed"] is None


# ---------------------------------------------------------------- Medium 3:编码路径的规则要组合
@pytest.mark.parametrize("text,leak", [
    ("%5CUsers%5CAlice%5Csecret.env", "Alice"),
    ("%255CUsers%255CAlice%255Csecret.env", "Alice"),
    ("C%3A%25u005cUsers%25u005calice", "alice"),
    ("%EF%BC%A3%EF%BC%9A%EF%BC%BCUsers%EF%BC%BCalice", "alice"),
])
def test_encoded_forms_of_every_path_shape_are_scrubbed(text, leak):
    """编码检测必须用**同一套**路径规则:曾漏掉『没有盘符的根路径』与『全角写法』。"""
    out = safe.scrub_text(text)
    assert leak not in out, out


@pytest.mark.parametrize("text", [
    "https://example.com/a%2Fb?q=1%262",
    "http://host/x",
    "折扣 50% off",
    "https://example.com/%E4%B8%AD%E6%96%87",
])
def test_urls_and_ordinary_percent_text_are_still_left_alone(text):
    assert safe.scrub_text(text) == text
