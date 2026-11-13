"""codex 第四次复核(43.7/50)的 3 个 Medium:尾行前缀过宽、研究结果矛盾字段、编码路径规则未组合。"""
import json

import pytest

from dashboard import collectors as col
from risk.report import backtest_stats_row
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
    {"kind": "backtest", "stats": backtest_stats_row({"total_trade_count": 156, "end_balance": 20000.0, "sharpe_ratio": 1.2, "total_return": 0.1, "max_ddpercent": -12.5, "max_drawdown": -2500.0, "total_net_pnl": 200.0, "total_commission": 100.0, "total_slippage": 50.0})},
    {"kind": "backtest", "status": "strategy_error", "error": {"type": "X", "message": "m"}},
])
def test_real_result_shapes_are_still_accepted(body):
    assert _ok(body)


def test_passed_is_not_exposed_for_a_backtest():
    """backtest 没有『通过』这个概念,展示层也不该给出这个字段。"""
    brief = col._brief_run("20261112-210000-000000",
                           {**BASE, "kind": "backtest", "stats": backtest_stats_row({"total_trade_count": 156, "end_balance": 20000.0, "sharpe_ratio": 1.2, "total_return": 0.1, "max_ddpercent": -12.5, "max_drawdown": -2500.0, "total_net_pnl": 200.0, "total_commission": 100.0, "total_slippage": 50.0})})
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


def test_a_message_containing_a_pipe_still_hides_a_truncated_marker():
    """消息里带竖线时,只取最后一段会丢掉前半段——整行也要检查。"""
    line = "2026-11-13 09:00:00 | ERROR | Runtime | 见 a | 硬锁".encode("utf-8")
    assert _parse(PREFIX + line).trusted is False


def test_real_log_lines_are_not_mistaken_for_a_truncated_event():
    """用仓库里的真实日志做语料:每一条当作未终止的尾行,都不该被判为『藏着事件』。

    (前缀检测曾把『当前权益 10000』误判;真实语料是最直接的回归防线。)
    """
    import pathlib

    log_dir = pathlib.Path(__file__).resolve().parents[1] / "runtime" / "vntrader" / "log"
    if not log_dir.is_dir():
        pytest.skip("仓库里没有真实日志")
    samples: list[str] = []
    for f in sorted(log_dir.glob("vt_*.log")):
        samples.extend(f.read_text(encoding="utf-8", errors="replace").splitlines())
        if len(samples) > 3000:
            break
    if not samples:
        pytest.skip("没有可用的真实日志样本")
    false_positives = []
    for line in samples:
        parser = ls.DayLogParser("2026-11-13")
        parser.feed(b"2026-11-13 08:00:00 | INFO | Runtime | x\n" + line.encode("utf-8"))
        if not parser.snapshot("x").trusted:
            false_positives.append(line)
    assert not false_positives, false_positives[:5]


def test_no_marker_prefix_is_missed_when_the_tail_is_truncated():
    """所有关键字的所有前缀、放在不同前缀文本之后,都要被判为不可信。"""
    for marker in ls.EVENT_MARKERS:
        for cut in range(1, len(marker)):
            for prefix in ("", "前文 ", "a | b "):
                line = f"2026-11-13 09:00:00 | ERROR | Runtime | {prefix}{marker[:cut]}".encode("utf-8")
                assert _parse(PREFIX + line).trusted is False, (marker, cut, prefix)


# ---------------------------------------------------------------- 对抗性审查发现
@pytest.mark.parametrize("text,leak", [
    ('payload={"password": "hunter2"}', "hunter2"),          # 带引号的 JSON 键曾被漏掉
    ('{"api_key": "AKIA1234567890"}', "AKIA1234567890"),
    ("'token': 'abcdef123456'", "abcdef123456"),
    ('cfg = {"secret": "s3cr3tvalue"}', "s3cr3tvalue"),
])
def test_a_quoted_json_key_does_not_leak_its_value(text, leak):
    """日志与候选代码里的 JSON 字面量:键带引号时,赋值规则曾匹配不到。"""
    assert leak not in safe.scrub_text(text), safe.scrub_text(text)


def test_the_campaign_list_does_not_reread_every_ledger_on_every_request(tmp_path):
    """列表接口每次轮询都要读完所有台账(实测 8 个战役 × 2 万条 = 2.9 秒)。摘要要缓存。"""
    import time

    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    row = {"campaign_id": "c", "generation": 1, "candidate_sha256": "a" * 64, "parent_sha256": None,
           "run_id": "20261112-210000-000001", "status": "evaluated", "sharpe": 0.4, "total_return": 0.03,
           "max_drawdown": 0.19, "dsr": 0.3, "n_trials": 4, "fail_reason": None, "reflection": "x",
           "created_at": "2026-11-12T21:00:00", "cell": "c", "diagnosis": None}
    for i in range(4):
        (d / f"camp{i}.jsonl").write_text("".join(json.dumps({**row, "generation": g}) + "\n"
                                                 for g in range(1, 5001)), encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    col.collect_evolution(settings, 1_800_000_000.0)            # 第一次:读盘
    t0 = time.perf_counter()
    col.collect_evolution(settings, 1_800_000_000.0)            # 第二次:应命中缓存
    assert time.perf_counter() - t0 < 0.3


def test_the_campaign_list_says_honestly_when_it_did_not_scan_every_campaign(tmp_path, monkeypatch):
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    d = root / "data" / "research" / "evolution"
    d.mkdir(parents=True)
    for i in range(5):
        (d / f"camp{i}.jsonl").write_text(json.dumps({
            "campaign_id": f"camp{i}", "generation": 1, "candidate_sha256": "a" * 64, "parent_sha256": None,
            "run_id": "20261112-210000-000001", "status": "evaluated", "sharpe": 0.4, "total_return": 0.03,
            "max_drawdown": 0.19, "dsr": 0.3, "n_trials": 4, "fail_reason": None, "reflection": "x",
            "created_at": "2026-11-12T21:00:00", "cell": "c", "diagnosis": None}) + "\n", encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    monkeypatch.setattr(col, "MAX_CAMPAIGNS_SUMMARIZED", 2)
    r = col.collect_evolution(settings, 1_800_000_000.0)
    assert r.section["data"]["unscanned"] == 3
    assert "未检查" in (r.section["message"] or "")
