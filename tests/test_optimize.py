"""scripts/optimize.py 测试:不跑真实回测,用假的 evaluate 验证选参逻辑与"测试段纪律"。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import optimize  # noqa: E402  (内部会先 import bootstrap)


def row(sharpe, fills=40):
    return {"sharpe": sharpe, "fills": fills}


# ---------- valid_score ----------

def test_score_is_sharpe_when_enough_trades():
    assert optimize.valid_score(row(0.8)) == 0.8


def test_score_excludes_too_few_trades():
    assert optimize.valid_score(row(3.0, fills=optimize.MIN_TRADES - 1)) == float("-inf")


def test_score_excludes_nan_sharpe():
    assert optimize.valid_score(row(float("nan"))) == float("-inf")
    assert optimize.valid_score(row(None)) == float("-inf")


# ---------- grid ----------

def test_grid_requires_slow_at_least_twice_fast():
    combos = optimize.grid()
    assert combos
    assert all(slow >= 2 * fast for _, _, _, fast, slow, _ in combos)
    assert not any(fast == 20 and slow == 30 for _, _, _, fast, slow, _ in combos)


# ---------- smoothed ----------

def dense(n, func):
    """n x n x n 的完整网格,得分由 func(i, j, k) 给出。"""
    return {(i, j, k): func(i, j, k) for i in range(n) for j in range(n) for k in range(n)}


def test_only_cells_with_complete_neighbourhood_are_eligible():
    out = optimize.smoothed(dense(4, lambda i, j, k: 1.0))
    assert set(out) == {(i, j, k) for i in (1, 2) for j in (1, 2) for k in (1, 2)}   # 边界层全部排除
    assert all(v == pytest.approx(1.0) for v in out.values())


def test_invalid_neighbours_count_as_zero():
    scores = dense(3, lambda i, j, k: 1.0)
    scores[(0, 0, 0)] = float("-inf")
    out = optimize.smoothed(scores)
    assert list(out) == [(1, 1, 1)]
    assert out[(1, 1, 1)] == pytest.approx(26 / 27)      # 26 个有效 1.0 + 1 个无效按 0


def test_invalid_neighbour_is_never_better_than_any_valid_score():
    """全是负分的区域:无效邻居不能按 0 分计(那会比有效的负分更'好'),应按最差有效分计。"""
    scores = dense(3, lambda i, j, k: -1.0)
    scores[(0, 0, 0)] = float("-inf")
    out = optimize.smoothed(scores)
    assert out[(1, 1, 1)] == pytest.approx(-1.0)          # 下限 = min(0, -1) = -1,不会被抬高到 > -1


def test_invalid_cell_itself_is_never_selected():
    scores = dense(3, lambda i, j, k: 1.0)
    scores[(1, 1, 1)] = float("-inf")
    assert optimize.smoothed(scores) == {}


def test_hole_in_grid_blocks_neighbours():
    """被过滤掉的组合(空洞)旁边的格子邻域不完整,不参与选参。"""
    scores = dense(3, lambda i, j, k: 1.0)
    del scores[(0, 0, 0)]
    assert optimize.smoothed(scores) == {}


def test_isolated_spike_loses_to_stable_plateau():
    """孤立的高分尖峰(过拟合特征)应当输给周围都不错的参数高原。"""
    def f(i, j, k):
        if (i, j, k) == (1, 1, 1):
            return 2.0                                    # 尖峰,四周都是 0 分
        return 0.6 if min(i, j, k) >= 2 else 0.0          # 高原:2..4 区域

    scores = dense(5, f)
    out = optimize.smoothed(scores)
    assert scores[(1, 1, 1)] > scores[(3, 3, 3)]          # 原始分尖峰更高
    assert out[(1, 1, 1)] < 0.2
    assert max(out, key=out.get) == (3, 3, 3)             # 平滑后选的是高原
    assert out[(3, 3, 3)] == pytest.approx(0.6)


def test_symmetric_peak_selects_centre_not_edge():
    """对称凹函数:最优应是中心格子;不得因边缘邻居更少而偏向边缘。"""
    scores = dense(5, lambda i, j, k: -((i - 2) ** 2 + (j - 2) ** 2 + (k - 2) ** 2))
    out = optimize.smoothed(scores)
    assert max(out, key=out.get) == (2, 2, 2)


# ---------- 切分 ----------

def test_adjusted_symbols_use_their_own_windows_and_test_window_is_unchanged_after_2024():
    assert optimize.windows_for("C") is optimize.WINDOWS
    assert optimize.windows_for("MA") is optimize.WINDOWS
    adj = optimize.windows_for("CADJ")
    assert adj is optimize.windows_for("MAADJ") and adj is optimize.WINDOWS_ADJ
    assert adj["train"][0] == datetime(2018, 5, 1)                       # 复权数据 2018-05 起
    # 三段首尾相接、互不重叠,按时间顺序排列
    assert adj["train"][1] < adj["valid"][0] and adj["valid"][1] < adj["test"][0]


def test_evaluate_uses_the_windows_of_the_symbol(monkeypatch):
    seen = {}

    def fake_run(cls, params, key, start, end):
        seen["range"] = (start, end)
        return {}, None, []

    monkeypatch.setattr(optimize, "run_backtest", fake_run)
    optimize.evaluate("CADJ", {}, "valid")
    assert seen["range"] == optimize.WINDOWS_ADJ["valid"]
    optimize.evaluate("C", {}, "valid")
    assert seen["range"] == optimize.WINDOWS["valid"]


# ---------- evaluate ----------

def test_evaluate_maps_stats_and_handles_empty(monkeypatch):
    stats = {"total_return": 12.5, "max_ddpercent": -8.0, "sharpe_ratio": 0.7,
             "total_trade_count": 30, "end_balance": 11250.0}
    monkeypatch.setattr(optimize, "run_backtest", lambda *a, **k: (stats, None, []))
    res = optimize.evaluate("C", {}, "train")
    assert (res["return_pct"], res["round_trips"], res["fills"]) == (12.5, 15, 30)

    monkeypatch.setattr(optimize, "run_backtest", lambda *a, **k: ({}, None, []))
    empty = optimize.evaluate("C", {}, "train")
    assert empty["fills"] == 0 and empty["round_trips"] == 0


# ---------- main:选参只用训练段,测试段需显式开启 ----------

@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    calls = []

    def fake_evaluate(key, params, window):
        calls.append(window)
        # 让 fast=10, slow=60 附近得分最高,便于断言选参结果
        peak = -abs(params["fast_window"] - 10) - abs(params["slow_window"] - 60) / 10
        return {"return_pct": 5.0, "maxdd_pct": -3.0, "sharpe": 1.0 + peak / 10,
                "round_trips": 20, "end_balance": 10500.0, "fills": 40}

    monkeypatch.setattr(optimize, "evaluate", fake_evaluate)
    monkeypatch.setattr(optimize.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(optimize, "FAST", [8, 10, 13])
    monkeypatch.setattr(optimize, "SLOW", [40, 60, 80])
    monkeypatch.setattr(optimize, "MULT", [1.0, 1.25, 1.5])
    return calls, tmp_path


def test_main_never_touches_test_window_by_default(fake_env, monkeypatch, capsys):
    calls, tmp_path = fake_env
    monkeypatch.setattr(sys, "argv", ["optimize.py", "--symbol", "C"])
    optimize.main()
    assert "test" not in calls
    out = capsys.readouterr().out
    assert "'slow_window': 60" in out
    assert "测试段未查看" in out
    assert (tmp_path / "data" / "results" / "optimize_C_train.csv").exists()


def test_main_warns_when_validation_has_too_few_round_trips(fake_env, monkeypatch, capsys):
    calls, _ = fake_env
    original = optimize.evaluate

    def few_trips_in_valid(key, params, window):
        res = original(key, params, window)
        if window == "valid":
            res = {**res, "round_trips": 3}
        return res

    monkeypatch.setattr(optimize, "evaluate", few_trips_in_valid)
    monkeypatch.setattr(sys, "argv", ["optimize.py", "--symbol", "C"])
    optimize.main()
    assert "统计无意义" in capsys.readouterr().out


def test_main_looks_at_test_window_once_when_asked(fake_env, monkeypatch, capsys):
    calls, _ = fake_env
    monkeypatch.setattr(sys, "argv", ["optimize.py", "--symbol", "C", "--test"])
    optimize.main()
    assert calls.count("test") == 1
    assert "只看这一次" in capsys.readouterr().out


def test_main_gives_up_when_no_combo_has_enough_trades(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(optimize, "evaluate", lambda *a: {
        "return_pct": 0.0, "maxdd_pct": 0.0, "sharpe": 0.0, "round_trips": 0, "end_balance": 1e4, "fills": 0})
    monkeypatch.setattr(optimize.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["optimize.py"])
    optimize.main()
    assert "放弃优化" in capsys.readouterr().out
    