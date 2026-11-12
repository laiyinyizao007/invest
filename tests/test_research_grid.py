"""候选参数网格寻优:声明校验、邻域平滑选参、高原判定。纯逻辑,回测执行器由调用方注入。"""
from datetime import datetime
from types import SimpleNamespace

import pytest

import research_grid as rg

GRID = {"a": [1, 2, 3, 4], "b": [10, 20, 30], "c": [0.5, 1.0, 1.5]}


class Cand:
    parameters = ["a", "b", "c"]


def bar(ts):
    return SimpleNamespace(datetime=ts)


BARS = [bar(datetime(2021, 1, 4, 9, 0)), bar(datetime(2021, 6, 1, 9, 0)), bar(datetime(2022, 6, 30, 14, 45)), bar(datetime(2022, 7, 1, 9, 0))]


# ---------- 声明校验 ----------

def test_valid_grid_returns_axes_in_declared_order():
    axes = rg.validate_grid(GRID, Cand, max_cells=100)

    assert [name for name, _ in axes] == ["a", "b", "c"] and axes[0][1] == [1, 2, 3, 4]


@pytest.mark.parametrize("grid, message", [
    ({"a": [1, 2, 3], "b": [1, 2, 3]}, "恰好 3 个"),
    ({"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3], "d": [1, 2, 3]}, "恰好 3 个"),
    ({"a": [1, 2], "b": [1, 2, 3], "c": [1, 2, 3]}, "至少 3 个"),
    ({"a": [3, 2, 1], "b": [1, 2, 3], "c": [1, 2, 3]}, "递增"),
    ({"a": [1, 1, 2], "b": [1, 2, 3], "c": [1, 2, 3]}, "递增"),
    ({"a": [1, "x", 3], "b": [1, 2, 3], "c": [1, 2, 3]}, "数值"),
    ({"a": [True, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]}, "数值"),
    ({"a": [1, float("nan"), 3], "b": [1, 2, 3], "c": [1, 2, 3]}, "数值"),
    ({"a": "123", "b": [1, 2, 3], "c": [1, 2, 3]}, "列表"),
    ({"a": list(range(1, 12)), "b": [1, 2, 3], "c": [1, 2, 3]}, "最多"),
    ({"a": [1, 2, 3], "b": [1, 2, 3], "zzz": [1, 2, 3]}, "不是策略参数"),
])
def test_invalid_grids_are_rejected_with_a_reason(grid, message):
    with pytest.raises(rg.GridError, match=message):
        rg.validate_grid(grid, Cand, max_cells=100)


def test_total_cells_are_capped_by_configuration():
    big = {"a": list(range(1, 8)), "b": list(range(1, 8)), "c": list(range(1, 8))}        # 343

    assert rg.validate_grid(big, SimpleNamespace(parameters=["a", "b", "c"]), max_cells=343)
    with pytest.raises(rg.GridError, match="单元格"):
        rg.validate_grid(big, SimpleNamespace(parameters=["a", "b", "c"]), max_cells=342)


def test_grid_cannot_set_risk_or_budget_parameters():
    for forbidden in ("risk_budget", "max_daily_losses", "atr_window", "history_vt_symbol"):
        with pytest.raises(rg.GridError, match="风控"):
            rg.validate_grid({"a": [1, 2, 3], "b": [1, 2, 3], forbidden: [1, 2, 3]}, SimpleNamespace(parameters=["a", "b", forbidden]), max_cells=100)


# ---------- 网格展开 ----------

def test_cells_enumerate_index_tuples_with_params():
    axes = rg.validate_grid(GRID, Cand, max_cells=100)

    cells = list(rg.cells(axes))

    assert len(cells) == 4 * 3 * 3 and cells[0] == ((0, 0, 0), {"a": 1, "b": 10, "c": 0.5})
    assert cells[-1] == ((3, 2, 2), {"a": 4, "b": 30, "c": 1.5})


# ---------- 选参 ----------

def run_with(score_fn, trips=40):
    def runner(strategy_class, setting, bars, cost_multiplier):
        sharpe = score_fn(setting)
        # 构造一条日收益序列,使 annualized_sharpe 的符号与 score_fn 一致
        base = [1.0 if sharpe > 0 else -1.0] * 60
        base[::7] = [(-1.0 if sharpe > 0 else 1.0)] * len(base[::7])
        return {"daily_pnls": [x * (abs(sharpe) + 0.1) for x in base], "round_trips": trips, "maxdd_pct": -1.0, "maxdd_yuan": -100.0}
    return runner


def test_select_picks_the_cell_with_best_smoothed_score_and_reports_plateau():
    result = rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=run_with(lambda s: 2.0), capital=10_000, max_cells=100,
                              min_round_trips=10, window=(datetime(2021, 1, 1), datetime(2022, 6, 30, 23, 59)))

    assert result["n_cells"] == 36 and result["valid_cells"] == 36
    assert result["on_plateau"] is True
    assert set(result["best_params"]) == {"a", "b", "c"}
    assert len(result["plateau"]["scores"]) == 36 and len(result["plateau"]["cell"]) == 3


def test_spike_in_a_losing_region_is_selected_but_not_on_plateau():
    def score(setting):
        return 5.0 if (setting["a"], setting["b"], setting["c"]) == (2, 20, 1.0) else -1.0

    result = rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=run_with(score), capital=10_000, max_cells=100,
                              min_round_trips=10, window=(datetime(2021, 1, 1), datetime(2022, 6, 30, 23, 59)))

    assert result["on_plateau"] is False


def test_cells_with_too_few_round_trips_are_invalid_and_count_at_the_floor():
    result = rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=run_with(lambda s: 2.0, trips=3), capital=10_000,
                              max_cells=100, min_round_trips=10, window=(datetime(2021, 1, 1), datetime(2022, 6, 30, 23, 59)))

    assert result["valid_cells"] == 0 and result["best_params"] is None and result["on_plateau"] is False
    assert "没有任何" in result["note"]


def test_selection_window_must_end_before_the_first_validation_window():
    with pytest.raises(rg.GridError, match="验证窗口"):
        rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=run_with(lambda s: 1.0), capital=10_000, max_cells=100,
                         min_round_trips=10, window=(datetime(2021, 1, 1), datetime(2022, 7, 1)))


def test_selection_never_sees_bars_after_the_window():
    seen = []

    def runner(strategy_class, setting, bars, cost_multiplier):
        seen.append(max(b.datetime for b in bars))
        return {"daily_pnls": [1.0, -1.0] * 30, "round_trips": 40, "maxdd_pct": -1.0, "maxdd_yuan": -1.0}

    rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=runner, capital=10_000, max_cells=100, min_round_trips=10,
                     window=(datetime(2021, 1, 1), datetime(2022, 6, 30, 23, 59)))

    assert max(seen) <= datetime(2022, 6, 30, 23, 59)


def test_result_is_json_serialisable():
    import json
    result = rg.select_params(candidate_cls=Cand, grid=GRID, bars=BARS, runner=run_with(lambda s: 2.0), capital=10_000, max_cells=100,
                              min_round_trips=10, window=(datetime(2021, 1, 1), datetime(2022, 6, 30, 23, 59)))

    json.dumps(result, ensure_ascii=False)


def test_first_validation_start_is_locked_to_the_walk_forward_windows():
    import walk_forward_optimizer as wf
    assert rg.FIRST_VALIDATION_START == wf.build_windows()[0].valid_start
