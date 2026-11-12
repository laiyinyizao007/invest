"""scripts/walk_forward_optimizer.py 测试:窗口纪律、隔离护栏、排名、worker 与 CLI 全流程。

真实回测只用合成迷你 CSV(进程内串行 workers=1);并行路径用注入的假 executor 验证调度,
避免 Windows spawn 在测试里反复启动进程。
"""
import pickle
import re
import sys
from concurrent.futures import Future
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


import walk_forward_optimizer as wf  # noqa: E402  (内部会先 import bootstrap)


# ---------- 合成数据 ----------

def synthetic_csv(tmp_path, n_days=40, drift=0.6, seed=7):
    """每个交易日 8 根 15 分钟线(2022 年,全部早于测试段),写成与真实数据同构的 CSV。"""
    rng = np.random.default_rng(seed)
    price, rows = 2000.0, []
    day = datetime(2022, 1, 4)
    for _ in range(n_days):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        for i in range(8):
            o_ = round(price)
            price += drift + rng.normal(0, 2.0)
            c = round(price)
            hi = max(o_, c) + int(abs(rng.normal(0, 1)))
            lo = min(o_, c) - int(abs(rng.normal(0, 1)))
            rows.append({
                "datetime": (day.replace(hour=9) + timedelta(minutes=15 * i)).isoformat(),
                "open": o_, "high": hi, "low": lo, "close": c,
                "volume": 100.0, "open_interest": 1000.0,
            })
        day += timedelta(days=1)
    path = tmp_path / "mini_15m.csv"
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")
    return path


def tiny_window(name="T1"):
    return wf.Window(
        name,
        datetime(2022, 1, 4), datetime(2022, 3, 31, 23, 59),
        datetime(2022, 4, 1), datetime(2022, 6, 30, 23, 59),
    )


# ---------- 阶段 A:窗口与隔离 ----------

def test_build_windows_three_windows_strictly_before_test_segment():
    windows = wf.build_windows()
    assert [w.name for w in windows] == ["W1", "W2", "W3"]
    for w in windows:
        assert w.train_end < w.valid_start
        assert w.train_start < w.train_end < wf.TEST_SEGMENT_START
        assert w.valid_start < w.valid_end < wf.TEST_SEGMENT_START
    # 三个验证段按时间排序且互不重叠
    valids = [(w.valid_start, w.valid_end) for w in windows]
    assert all(valids[i][1] < valids[i + 1][0] for i in range(len(valids) - 1))


def test_window_segment_rejects_unknown_kind():
    with pytest.raises(ValueError):
        tiny_window().segment("test")


def test_pre_test_assertion_rejects_test_segment_dates_and_bad_direction():
    with pytest.raises(ValueError, match="越界"):
        wf._assert_pre_test(datetime(2023, 6, 1), wf.TEST_SEGMENT_START)
    with pytest.raises(ValueError, match="越界"):
        wf._assert_pre_test(datetime(2024, 6, 1), datetime(2024, 12, 31))
    with pytest.raises(ValueError, match="越界"):
        wf._assert_pre_test(datetime(2023, 6, 1), datetime(2023, 1, 1))
    # 边界:恰好止于测试段起点前一刻是允许的
    wf._assert_pre_test(datetime(2023, 1, 1), datetime(2023, 12, 31, 23, 59))


# ---------- 阶段 A:网格与排名 ----------

def test_grid_is_two_dimensional_with_fixed_atr_mult_and_no_risk_budget_key():
    combos = wf.grid()
    assert len(combos) == 31
    assert all(c["slow_window"] >= 2 * c["fast_window"] for c in combos)
    assert all(c["atr_mult"] == 1.5 for c in combos)
    assert all("risk_budget" not in c for c in combos)          # 300 元预算走策略类默认,不允许被参数覆盖


def _row(params, sharpe, maxdd_yuan=-500.0, fills=200):
    # vn.py 的 max_drawdown 带负号:-300 比 -900 回撤更小
    return {"params": params, "sharpe": sharpe, "maxdd_yuan": maxdd_yuan, "fills": fills}


def test_rank_filters_by_min_fills_and_sorts_deterministically():
    p1 = {"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}
    p2 = {"fast_window": 14, "slow_window": 50, "atr_mult": 1.5}
    p3 = {"fast_window": 18, "slow_window": 60, "atr_mult": 1.5}
    rows = [
        _row(p1, 1.0, fills=wf.MIN_FILLS - 1),                  # 笔数不足,淘汰
        _row(p2, 0.8, maxdd_yuan=-900.0),
        _row(p3, 0.8, maxdd_yuan=-300.0),                       # 夏普平局,回撤小者(-300)靠前
    ]
    ranked = wf.rank(rows)
    assert [r["params"] for r in ranked] == [p3, p2]


def test_rank_breaks_full_ties_by_params_and_excludes_nan():
    p1 = {"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}
    p2 = {"fast_window": 14, "slow_window": 50, "atr_mult": 1.5}
    rows = [_row(p2, 0.5), _row(p1, 0.5), _row(p1, float("nan"))]
    ranked = wf.rank(rows)
    assert [r["params"] for r in ranked] == [p1, p2]            # 完全平局时 fast 小者靠前且可复现


# ---------- 阶段 B:worker 与多进程调度 ----------

def test_backtest_one_runs_on_synthetic_csv_and_reports_budget_counter(tmp_path):
    data = str(synthetic_csv(tmp_path))
    res = wf._backtest_one(
        data, {"fast_window": 3, "slow_window": 12, "atr_mult": 1.5},
        datetime(2022, 1, 4).isoformat(), datetime(2022, 6, 30, 23, 59).isoformat(),
    )
    for key in ("sharpe", "maxdd_yuan", "maxdd_pct", "fills", "round_trips",
                "skipped_by_budget", "budget_skip_rate", "total_cost", "net_pnl"):
        assert key in res
    assert res["skipped_by_budget"] >= 0
    assert 0.0 <= res["budget_skip_rate"] <= 1.0
    assert res["fills"] == res["round_trips"] * 2


def test_backtest_one_refuses_test_segment_dates(tmp_path):
    data = str(synthetic_csv(tmp_path))
    with pytest.raises(ValueError, match="越界"):
        wf._backtest_one(
            data, {"fast_window": 3, "slow_window": 12, "atr_mult": 1.5},
            datetime(2024, 1, 1).isoformat(), datetime(2024, 6, 30).isoformat(),
        )


def test_worker_args_are_pickle_safe_for_windows_spawn():
    pickle.loads(wf.pickle_safe_worker_args())                    # 不抛即兼容 spawn


class FakeFuture:
    def __init__(self, value):
        self._value = value

    def result(self):
        return self._value


class FakeExecutor:
    """记录提交内容的假进程池:验证并行调度路径,不真正起进程。"""

    def __init__(self):
        self.submitted = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        self.submitted.append((fn, args))
        return FakeFuture(fn(*args))


def test_map_backtests_serial_path_aligns_results(tmp_path, monkeypatch):
    data = str(synthetic_csv(tmp_path))
    calls = []

    def fake_worker(data_file, params, start_iso, end_iso, cost_multiplier=1.0):
        calls.append(params["fast_window"])
        return {"params": params, "fills": 0}

    monkeypatch.setattr(wf, "_backtest_one", fake_worker)
    jobs = wf._jobs(
        [{"fast_window": f, "slow_window": 40, "atr_mult": 1.5} for f in (10, 14)],
        datetime(2022, 1, 1), datetime(2022, 6, 30),
    )
    out = wf.map_backtests(data, jobs, workers=1)
    assert [r["params"]["fast_window"] for r in out] == [10, 14]  # 顺序对齐
    assert calls == [10, 14]


def test_map_backtests_parallel_path_uses_executor(tmp_path, monkeypatch):
    data = str(synthetic_csv(tmp_path))
    fake_pool = FakeExecutor()
    monkeypatch.setattr(wf, "_backtest_one", lambda *a, **k: {"ok": True})
    jobs = wf._jobs(
        [{"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}],
        datetime(2022, 1, 1), datetime(2022, 6, 30),
    )
    out = wf.map_backtests(data, jobs, workers=4, executor=fake_pool)
    assert out == [{"ok": True}]
    assert len(fake_pool.submitted) == 1
    assert fake_pool.submitted[0][0] is wf._backtest_one          # 提交的是模块级 worker(spawn 可 pickle)


def test_map_backtests_builds_real_process_pool_when_workers_gt_one(monkeypatch):
    created = {}

    class RecordingPool:
        def __init__(self, max_workers):
            created["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, *args):
            fut = Future()
            fut.set_result({"params": args[1]})
            return fut

    monkeypatch.setattr(wf, "ProcessPoolExecutor", RecordingPool)
    jobs = wf._jobs([{"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}],
                    datetime(2022, 1, 1), datetime(2022, 6, 30))
    out = wf.map_backtests("dummy.csv", jobs, workers=3)
    assert created["max_workers"] == 3 and out[0]["params"]["fast_window"] == 10


# ---------- 阶段 C:单窗口流程、CLI 与源码扫描 ----------

def test_run_window_top5_and_insufficient_flag(monkeypatch):
    combos = [{"fast_window": f, "slow_window": 40, "atr_mult": 1.5} for f in (10, 14)]
    monkeypatch.setattr(wf, "grid", lambda: combos)

    def fake_worker(data_file, params, start_iso, end_iso, cost_multiplier=1.0):
        is_train = start_iso.startswith("2022-01")
        sharpe = 2.0 - params["fast_window"] / 100 if is_train else 0.1
        return {"params": params, "sharpe": sharpe, "maxdd_yuan": -100.0,
                "maxdd_pct": -1.0, "fills": 200, "round_trips": 100,
                "skipped_by_budget": 0, "budget_skip_rate": 0.0,
                "return_pct": 1.0, "end_balance": 1.0, "net_pnl": 1.0, "total_cost": 1.0}

    monkeypatch.setattr(wf, "_backtest_one", fake_worker)
    result = wf.run_window("dummy.csv", tiny_window(), workers=1)
    assert len(result["top"]) == 2
    assert result["insufficient"] is True                        # 达标组合 2 < Top5
    assert len(result["valid_rows"]) == 2
    assert result["top"][0]["params"]["fast_window"] == 10       # 训练夏普高者居前
    assert all(r["sharpe"] == 0.1 for r in result["valid_rows"]) # 验证段独立回测,不看训练分


def test_run_window_reports_all_insufficient_when_no_combo_qualifies(monkeypatch):
    monkeypatch.setattr(wf, "grid", lambda: [
        {"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}])
    monkeypatch.setattr(wf, "_backtest_one", lambda *a, **k: {
        "params": a[1], "sharpe": 0.0, "maxdd_yuan": 0.0, "maxdd_pct": 0.0,
        "fills": 0, "round_trips": 0, "skipped_by_budget": 0, "budget_skip_rate": 0.0,
        "return_pct": 0.0, "end_balance": 1e4, "net_pnl": 0.0, "total_cost": 0.0})
    result = wf.run_window("dummy.csv", tiny_window(), workers=1)
    assert result["top"] == [] and result["valid_rows"] == []
    assert result["insufficient"] is True


def test_main_end_to_end_with_fake_worker(monkeypatch, tmp_path, capsys):
    combos = [{"fast_window": f, "slow_window": 40, "atr_mult": 1.5} for f in (10, 14)]
    monkeypatch.setattr(wf, "grid", lambda: combos)
    monkeypatch.setattr(wf, "build_windows", lambda: [tiny_window()])
    monkeypatch.setattr(wf.bootstrap, "ROOT", tmp_path)

    def fake_worker(data_file, params, start_iso, end_iso, cost_multiplier=1.0):
        is_train = start_iso.startswith("2022-01")
        return {"params": params,
                "sharpe": (1.5 if is_train else 0.4) - params["fast_window"] / 1000,
                "maxdd_yuan": -320.0, "maxdd_pct": -3.2, "fills": 200, "round_trips": 100,
                "skipped_by_budget": 5, "budget_skip_rate": 5 / 105,
                "return_pct": 4.0, "end_balance": 10400.0, "net_pnl": 400.0, "total_cost": 80.0}

    monkeypatch.setattr(wf, "_backtest_one", fake_worker)
    out_csv = tmp_path / "wf.csv"
    rc = wf.main(["--workers", "1", "--data", str(tmp_path / "x.csv"), "--output", str(out_csv)])
    assert rc == 0
    stdout = capsys.readouterr().out
    assert "T1" in stdout and "跨窗口汇总" in stdout and "INSUFFICIENT" in stdout
    assert "2024" not in stdout and "2025" not in stdout and "2026" not in stdout
    df = pd.read_csv(out_csv)
    assert set(df["segment"]) == {"train", "valid"}
    assert len(df) == 4                                           # 2 组合 × 2 区段
    assert (df["atr_mult"] == 1.5).all() and "risk_budget" not in df.columns


def test_source_code_contains_no_test_segment_access():
    """隔离护栏第 3 层:测试段年份字面量只能出现在 TEST_SEGMENT_START 定义行。"""
    source = Path(wf.__file__).read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(source.splitlines(), 1):
        for year in re.findall(r"20\d{2}", line):
            if int(year) >= 2024 and "TEST_SEGMENT_START = datetime" not in line:
                offenders.append((lineno, line.strip()))
    assert offenders == []


def test_validate_window_rejects_validation_before_train():
    # __post_init__ 让构造本身就抛:外部直接构造的越序 Window 在创建时即被拦截
    with pytest.raises(ValueError, match="严格早于验证段"):
        wf.Window(
            "BAD",
            datetime(2022, 7, 1), datetime(2023, 6, 30, 23, 59),
            datetime(2022, 1, 1), datetime(2022, 6, 30, 23, 59),
        )


def test_print_window_and_summary_survive_all_insufficient(capsys):
    window = tiny_window()
    wf._print_window({"window": window, "top": [], "valid_rows": [], "insufficient": True})
    out = capsys.readouterr().out
    assert "INSUFFICIENT" in out and "跳过验证" in out
    wf._print_summary(wf.summarize([{"valid_rows": []}]))
    assert "全部 INSUFFICIENT" in capsys.readouterr().out


def test_main_relative_output_anchors_at_bootstrap_root(monkeypatch, tmp_path, capsys):
    combos = [{"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}]
    monkeypatch.setattr(wf, "grid", lambda: combos)
    monkeypatch.setattr(wf, "build_windows", lambda: [tiny_window()])
    monkeypatch.setattr(wf.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(wf, "_backtest_one", lambda *a, **k: {
        "params": a[1], "sharpe": 0.5, "maxdd_yuan": -100.0, "maxdd_pct": -1.0,
        "fills": 200, "round_trips": 100, "skipped_by_budget": 0, "budget_skip_rate": 0.0,
        "return_pct": 1.0, "end_balance": 1.0, "net_pnl": 1.0, "total_cost": 1.0})
    wf.main(["--workers", "1", "--data", str(tmp_path / "x.csv"), "--output", "wf_rel.csv"])
    assert (tmp_path / "wf_rel.csv").exists()
    capsys.readouterr()


def test_summarize_handles_empty_and_aggregates():
    empty = wf.summarize([{"valid_rows": []}])
    assert empty["windows_validated"] == 0
    assert empty["worst_validation_maxdd_yuan"] != empty["worst_validation_maxdd_yuan"]  # NaN,不是 inf
    summary = wf.summarize([{
        "valid_rows": [
            {"sharpe": 0.8, "maxdd_yuan": -300.0, "skipped_by_budget": 2, "round_trips": 8},
            {"sharpe": -0.2, "maxdd_yuan": -900.0, "skipped_by_budget": 0, "round_trips": 10},
        ],
    }])
    assert summary["windows_validated"] == 1
    assert summary["top1_sharpe_mean"] == pytest.approx(0.8)      # 只取每窗口第一名
    assert summary["positive_sharpe_window_rate"] == 1.0
    assert summary["worst_validation_maxdd_yuan"] == -900.0       # 带负号口径下"最差"= 负得最多
    assert summary["validation_budget_skip_rate"] == pytest.approx(2 / 20)


def test_summarize_filters_nonfinite_sharpe_and_counts_insufficient():
    summary = wf.summarize([
        {"valid_rows": [{"sharpe": float("nan"), "maxdd_yuan": -100.0,
                         "skipped_by_budget": 0, "round_trips": 5}],
         "insufficient": True},
        {"valid_rows": [{"sharpe": 0.8, "maxdd_yuan": -300.0,
                         "skipped_by_budget": 0, "round_trips": 10}], "insufficient": False},
    ])
    assert summary["windows_validated"] == 1                       # NaN 毒化被剔除,不能进均值
    assert summary["nonfinite_sharpes_dropped"] == 1
    assert summary["insufficient_windows"] == 1
    assert summary["positive_sharpe_window_rate"] == 1.0


# ---------- 阶段 B 补充:统计映射与真实 spawn ----------

def test_stats_mapping_handles_odd_fills_by_floor_division():
    """段末仍持仓时 fills 为奇数://2 向下取整,弃单率分母少 1(轻微高估弃单率),原值保留核对。"""
    res = wf._stats_to_result(
        {}, {"total_trade_count": 101, "total_net_pnl": -1.0}, skipped=3)
    assert res["round_trips"] == 50 and res["fills"] == 101
    assert res["budget_skip_rate"] == pytest.approx(3 / 53)


def test_stats_mapping_all_zero_trades_reports_zero_rate():
    res = wf._stats_to_result({}, {}, skipped=0)
    assert res["round_trips"] == 0 and res["budget_skip_rate"] == 0.0


def test_backtest_one_normalizes_tz_aware_dates_before_assertion(tmp_path):
    """tz-aware 入参先归一化为 naive 再过隔离断言,不得以 TypeError 绕开 ValueError 语义。"""
    data = str(synthetic_csv(tmp_path))
    with pytest.raises(ValueError, match="越界"):
        wf._backtest_one(
            data, {"fast_window": 3, "slow_window": 12, "atr_mult": 1.5},
            datetime(2022, 1, 4).isoformat(),
            datetime(2024, 6, 30).isoformat() + "+08:00",
        )


def test_real_spawn_produces_same_results_as_serial(tmp_path):
    """真实 Windows spawn 冒烟:CI 若永远只测假池,worker 改成闭包/不可 pickle 时无人发现。"""
    data = str(synthetic_csv(tmp_path))
    params = {"fast_window": 3, "slow_window": 12, "atr_mult": 1.5}
    start, end = datetime(2022, 1, 4).isoformat(), datetime(2022, 6, 30, 23, 59).isoformat()
    serial = wf._backtest_one(data, params, start, end)
    with wf.ProcessPoolExecutor(max_workers=2) as pool:
        fut = pool.submit(wf._backtest_one, data, params, start, end)
        spawned = fut.result()
    assert spawned["fills"] == serial["fills"] and spawned["net_pnl"] == serial["net_pnl"]


# ---------- CLI 校验与空结果 ----------

def test_main_parallel_branch_shares_one_pool(monkeypatch, tmp_path, capsys):
    """main 的并行分支应创建单个进程池贯穿全部窗口,而不是每窗口一个池。"""
    monkeypatch.setattr(wf, "grid", lambda: [
        {"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}])
    monkeypatch.setattr(wf, "build_windows", lambda: [tiny_window(), tiny_window("T2")])
    monkeypatch.setattr(wf.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(wf, "_backtest_one", lambda *a, **k: {
        "params": a[1], "sharpe": 0.5, "maxdd_yuan": -100.0, "maxdd_pct": -1.0,
        "fills": 200, "round_trips": 100, "skipped_by_budget": 0, "budget_skip_rate": 0.0,
        "return_pct": 1.0, "end_balance": 1.0, "net_pnl": 1.0, "total_cost": 1.0})
    created = []

    class RecordingPool(FakeExecutor):
        def __init__(self, max_workers):
            created.append(max_workers)
            super().__init__()

    monkeypatch.setattr(wf, "ProcessPoolExecutor", RecordingPool)
    rc = wf.main(["--workers", "2", "--data", str(tmp_path / "x.csv"),
                  "--output", str(tmp_path / "wf.csv")])
    assert rc == 0 and created == [2]                             # 单池,不是每窗口各一个
    capsys.readouterr()


def test_print_summary_reports_insufficient_and_nonfinite(capsys):
    summary = wf.summarize([
        {"valid_rows": [{"sharpe": 0.5, "maxdd_yuan": -100.0,
                         "skipped_by_budget": 0, "round_trips": 5}], "insufficient": False},
        {"valid_rows": [{"sharpe": float("nan"), "maxdd_yuan": -100.0,
                         "skipped_by_budget": 0, "round_trips": 5}], "insufficient": True},
    ])
    wf._print_summary(summary)
    out = capsys.readouterr().out
    assert "INSUFFICIENT" in out and "非有限夏普" in out


def test_main_rejects_zero_or_negative_workers():
    with pytest.raises(SystemExit):                              # argparse 校验失败 exit(2)
        wf.main(["--workers", "0"])
    with pytest.raises(SystemExit):
        wf.main(["--workers", "-2"])


def test_main_skips_csv_when_every_window_is_empty(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(wf, "grid", lambda: [
        {"fast_window": 10, "slow_window": 40, "atr_mult": 1.5}])
    monkeypatch.setattr(wf, "build_windows", lambda: [tiny_window()])
    monkeypatch.setattr(wf.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(wf, "map_backtests", lambda *a, **k: [])  # 全部回测无结果(极端边界)
    out_csv = tmp_path / "empty.csv"
    assert wf.main(["--workers", "1", "--data", str(tmp_path / "x.csv"), "--output", str(out_csv)]) == 0
    assert "未写出 CSV" in capsys.readouterr().out
    assert not out_csv.exists()                                   # 不写只有换行符的空文件
