"""成功回测的 stats 校验。

两次教训:
1. stats 只要求『非空对象』时,`{"x": 1}` 也会被当成合法的回测结果;
2. 键集写错更糟——曾按 vnpy 原始统计(`total_trade_count`)校验,而落盘的是投影后的
   `round_trips`,结果**每一个真实回测都被判成损坏、从列表里消失**,而测试全绿。
所以夹具从真实投影路径生成,面板用同一份键集。
"""
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
