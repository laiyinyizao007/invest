"""codex 第五次复核指出但未列为阻断的一项:成功 backtest 的 stats 只要求『非空对象』。

`{"x": 1}` 也会被当成合法的回测结果。真实产出里 stats 一定含这几个字段
(vnpy 的 calculate_statistics,以及研究代码自己读的那几个),所以可以按它们校验。
"""
import pytest

from dashboard import collectors as col

BASE = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r",
        "ran_at": "2026-11-12T21:00:00", "run_id": "20261112-210000-000000"}
REAL_STATS = {"total_trade_count": 156, "end_balance": 20000.0, "max_ddpercent": -12.5,
              "total_net_pnl": 200.0, "start_date": "2020-07-22", "end_date": "2022-06-30"}


def _ok(body):
    return col._valid_run("20261112-210000-000000", {**BASE, **body})


def test_a_backtest_with_the_real_statistics_fields_is_accepted():
    assert _ok({"kind": "backtest", "stats": REAL_STATS})


@pytest.mark.parametrize("stats", [
    {"x": 1},                      # 非空但不是回测统计
    {"total_trade_count": "many"},  # 类型不对
    {"total_trade_count": float("inf")},
    {"end_balance": "20000"},
    {},
])
def test_a_backtest_without_usable_statistics_is_not_accepted(stats):
    assert not _ok({"kind": "backtest", "stats": stats})


def test_a_strategy_error_backtest_still_needs_no_statistics():
    assert _ok({"kind": "backtest", "status": "strategy_error",
                "error": {"type": "X", "message": "m"}})
