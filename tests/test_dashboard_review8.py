"""成功回测的 stats 校验。

两次教训:
1. stats 只要求『非空对象』时,`{"x": 1}` 也会被当成合法的回测结果;
2. 键集写错更糟——曾按 vnpy 原始统计(`total_trade_count`)校验,而落盘的是投影后的
   `round_trips`,结果**每一个真实回测都被判成损坏、从列表里消失**,而测试全绿。
所以夹具从真实投影路径生成,面板用同一份键集。
"""
import json

import pytest

from dashboard import collectors as col
from risk.report import backtest_stats_row

BASE = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r",
        "ran_at": "2026-11-12T21:00:00", "run_id": "20261112-210000-000000"}
# **夹具从真实投影路径生成**:落盘的 stats 是 risk.report.backtest_stats_row 的产物,
# 不是 vnpy 的原始统计字典。曾经在这里手写一份键集(写成 total_trade_count),
# 面板按那份校验 → 所有真实回测都被判成损坏、从列表里消失,而测试全绿。
REAL_STATS = backtest_stats_row({"total_trade_count": 156, "end_balance": 20000.0, "sharpe_ratio": 1.2,
                                  "total_return": 0.1, "max_ddpercent": -12.5, "max_drawdown": -2500.0,
                                  "total_net_pnl": 200.0, "total_commission": 100.0, "total_slippage": 50.0})


def _ok(body):
    return col._valid_run("20261112-210000-000000", {**BASE, **body})


def test_a_backtest_with_the_real_statistics_fields_is_accepted():
    assert _ok({"kind": "backtest", "stats": REAL_STATS})


@pytest.mark.parametrize("stats", [
    {"x": 1},                       # 非空但不是回测统计
    {"round_trips": "many"},         # 类型不对
    {"round_trips": float("inf")},
    {"end_balance": "20000"},
    {},
    {"total_trade_count": 156, "end_balance": 20000.0},   # vnpy 原始字典:不是落盘形状
])
def test_a_backtest_without_usable_statistics_is_not_accepted(stats):
    assert not _ok({"kind": "backtest", "stats": stats})


def test_a_strategy_error_backtest_still_needs_no_statistics():
    assert _ok({"kind": "backtest", "status": "strategy_error",
                "error": {"type": "X", "message": "m"}})


# ---------------------------------------------------------------- codex 第八次复核的发现
def test_unscanned_research_results_make_the_block_unknown(tmp_path):
    """超出扫描上限的旧结果从未被校验:里面若有坏文件就彻底不可见。这属于『有数据没显示』,state 不能是 ok。"""
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    res = root / "data" / "research"
    res.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "research_sandbox.json").write_text(json.dumps({
        "iteration": {"max_iterations": 5, "window_hours": 24},
        "paths": {"pending_dir": "pending_strategies", "results_dir": "data/research",
                  "approvals_file": "data/research-control/approvals.json"}}), encoding="utf-8")
    for i in range(col.MAX_RUNS_SCANNED + 5):
        rid = f"20261112-210000-{i:06d}"
        (res / f"{rid}.json").write_text(json.dumps({
            "kind": "gate", "strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r",
            "ran_at": "2026-11-12T21:00:00", "run_id": rid, "passed": True, "failures": []}), encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    r = col.collect_research(settings, 1_800_000_000.0)
    assert r.section["state"] == "unknown", "有结果文件没被检查,区块不能显示正常"
    assert "未检查" in (r.section["message"] or "")


def test_a_severe_event_is_still_reported_when_the_latest_day_is_untrustworthy(tmp_path):
    """最新那天不可信(正在写)时,最新的**可信天**上的硬锁定不能被降级成『有日志无法识别』。"""
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    d = root / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True)
    (d / "vt_20261111.log").write_text(
        "2026-11-11 09:00:00 | INFO | R | 已启动\n2026-11-11 10:00:00 | ERROR | R | 硬锁定\n", encoding="utf-8")
    # 11-12 的最后一行停在多字节字符中间(进程正好崩在写入一半的行上)→ 那天不可信
    (d / "vt_20261112.log").write_bytes(
        "2026-11-12 09:00:00 | INFO | R | 已启动\n2026-11-12 10:00:00 | ERROR | R | 硬锁定".encode("utf-8")[:-2])
    settings = appconfig.load(path=root / "none.json", root=root)
    r = col.collect_days(settings, 1_800_000_000.0)
    assert r.level == "critical", f"级别应为 critical,实际 {r.level}({r.headline})"
    assert "2026-11-11" in r.headline


def test_a_historical_severe_event_does_not_pin_the_overview_at_critical(tmp_path):
    """43 天前的一次硬锁定不能把总状态条永久钉在 critical——那样今天真的硬锁定时反而看不出来。

    判据是『最新的可信天』:历史事件仍在表里可见,但不参与当前的严重判定。
    """
    from dashboard import collectors as col
    from dashboard import config as appconfig

    root = tmp_path / "repo"
    d = root / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True)
    (d / "vt_20261001.log").write_text(
        "2026-10-01 09:00:00 | INFO | R | 已启动\n2026-10-01 10:00:00 | ERROR | R | 硬锁定\n", encoding="utf-8")
    (d / "vt_20261002.log").write_text("2026-10-02 09:00:00 | INFO | R | 已启动\n", encoding="utf-8")
    settings = appconfig.load(path=root / "none.json", root=root)
    r = col.collect_days(settings, 1_800_000_000.0)
    assert r.level != "critical", f"历史严重事件不该把当前钉在 critical(实际 {r.level}: {r.headline})"
    # 但它仍然在表里可见
    days = {row["day"]: row for row in r.section["data"]["days"]}
    assert days["2026-10-01"]["has_severe"] is True
