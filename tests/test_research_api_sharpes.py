"""research_api 里与逐试验夏普有关的几处:子入口自己复查参数、作业结束后写账本、样本整批校验、报错带阶段。"""
import json
import math
from types import SimpleNamespace

import pytest

import research_api as api
from risk.dsr import MAX_RUNS_USED, run_variance
from risk.trial_ledger import grid_run_variances, record_trials, total_trials


def args(**kw):
    return SimpleNamespace(prior_run_variances=kw.get("prior_run_variances"))


# ---------- 子入口自己复查 ----------

def test_no_prior_history_means_none():
    assert api.child_run_variances(args()) is None


def test_a_valid_history_passes_through():
    assert api.child_run_variances(args(prior_run_variances="[0.01, 0.2, 0.0]")) == [0.01, 0.2, 0.0]
    assert api.child_run_variances(args(prior_run_variances="[]")) == []


@pytest.mark.parametrize("raw", [
    "not json", "{}", '"0.1"', "5", "null", "[0.1, NaN]", "[0.1, Infinity]", "[0.1, -0.5]", "[0.1, 1.5]",
    "[true]", '["0.1"]', "[null]", "[[0.1]]", json.dumps([0.1] * (MAX_RUNS_USED + 1)), 5, b"[0.1]",
])
def test_an_invalid_history_is_refused_by_the_child_itself(raw):
    """子进程入口可被直接调用,不能假设父进程查过。"""
    with pytest.raises(api.JobError) as exc:
        api.child_run_variances(args(prior_run_variances=raw))
    assert exc.value.code == api.EXIT_REFUSED


def test_the_upper_bound_on_the_length_is_inclusive():
    assert len(api.child_run_variances(args(prior_run_variances=json.dumps([0.1] * MAX_RUNS_USED)))) == MAX_RUNS_USED


def test_the_argv_round_trips_floats_without_loss():
    variances = [0.1 + 0.2, 1 / 3, 1e-9, 0.0]
    argv = api.run_variances_argv(variances)
    assert argv[0] == "--prior-run-variances"
    assert api.child_run_variances(args(prior_run_variances=argv[1])) == variances


def test_the_argv_keeps_only_the_most_recent_evaluations():
    argv = api.run_variances_argv([0.001 * i for i in range(MAX_RUNS_USED + 30)])
    values = json.loads(argv[1])
    assert len(values) == MAX_RUNS_USED and values[-1] == pytest.approx(0.001 * (MAX_RUNS_USED + 29))


def test_the_internal_subcommands_accept_the_new_argument():
    parser = api.build_parser()
    gate = parser.parse_args(["_gate_child", "--file", "f.py", "--cls", "C", "--params", "{}", "--sha256", "0" * 64,
                              "--on-plateau", "0", "--random-runs", "5", "--prior-trials", "3",
                              "--prior-run-variances", "[0.01]"])
    evaluate = parser.parse_args(["_evaluate_child", "--file", "f.py", "--cls", "C", "--sha256", "0" * 64,
                                  "--random-runs", "5", "--prior-trials", "3", "--prior-run-variances", "[0.01]"])
    assert gate.prior_run_variances == evaluate.prior_run_variances == "[0.01]"


# ---------- 作业结束后写账本 ----------

@pytest.fixture
def paths(tmp_path):
    return {"results_dir": str(tmp_path)}


CELLS = [0.01, -0.02, 0.03, 0.0, 0.02, -0.01]            # ≥ 5 个有效格子


def body(cells=CELLS, oos=1.5):
    return {"trials": {"prior": 10, "grid_cells": 27, "total": 37, "cell_sharpes": list(cells)},
            "metrics": {"oos_sharpe": oos}}


def ledger_rows(paths):
    return [json.loads(line) for line in api.trials_path(paths).read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_job_writes_cell_and_gate_sharpes_without_changing_the_trial_count(paths):
    record_trials(api.trials_path(paths), "grid_cell", 27, at=1.0)
    b = body()
    api.record_job_sharpes(paths, b)
    rows = [r for r in ledger_rows(paths) if r["kind"] == "sharpes"]
    assert [(r["source"], len(r["values"])) for r in rows] == [("grid", 6), ("gate", 1)]
    assert total_trials(api.trials_path(paths)) == 27                    # 夏普记录不是试验
    assert "cell_sharpes" not in b["trials"]                             # 落盘的结果里不留样本
    assert grid_run_variances(api.trials_path(paths)) == [pytest.approx(run_variance(CELLS))]


def test_the_gate_sharpe_is_recorded_as_daily_not_annualized(paths):
    api.record_job_sharpes(paths, {"metrics": {"oos_sharpe": 2.4}})
    rows = ledger_rows(paths)
    assert len(rows) == 1 and rows[0]["source"] == "gate" and math.isclose(rows[0]["values"][0], 2.4 / math.sqrt(240))


@pytest.mark.parametrize("bad", [[0.01, float("nan")], [0.01, 9.0], [0.01, True], ["0.1"], [float("inf")]])
def test_a_batch_with_any_bad_value_is_dropped_whole_and_the_job_says_so(paths, bad):
    """样本可能来自候选进程:一个坏值就整批丢弃,不是只信剩下的。"""
    b = {"trials": {"cell_sharpes": bad}}
    api.record_job_sharpes(paths, b)
    assert "cell_sharpes_dropped" in b["trials"] and "cell_sharpes" not in b["trials"]
    assert not api.trials_path(paths).exists()


def test_a_forged_oversized_batch_is_dropped(paths):
    b = {"trials": {"cell_sharpes": [0.01] * 25_000}}
    api.record_job_sharpes(paths, b)
    assert "cell_sharpes_dropped" in b["trials"] and not api.trials_path(paths).exists()


def test_a_non_finite_gate_sharpe_is_not_recorded(paths):
    api.record_job_sharpes(paths, {"metrics": {"oos_sharpe": float("nan")}})
    api.record_job_sharpes(paths, {"metrics": {"oos_sharpe": "1.5"}})
    assert not api.trials_path(paths).exists()


def test_a_corrupted_ledger_refuses_the_job_instead_of_silently_skipping(paths):
    api.trials_path(paths).write_text("not json\n", encoding="utf-8")
    with pytest.raises(api.JobError) as exc:
        api.record_job_sharpes(paths, body())
    assert exc.value.code == api.EXIT_REFUSED
    with pytest.raises(api.JobError):
        api.prior_run_variances(paths)


def test_the_prior_history_comes_from_the_ledger(paths):
    assert api.prior_run_variances(paths) == []
    api.record_job_sharpes(paths, body(cells=[0.1, 0.2, 0.0, -0.1, 0.05]))
    api.record_job_sharpes(paths, body(cells=[0.0, 0.01, 0.02, -0.01, 0.005]))
    assert len(api.prior_run_variances(paths)) == 2


# ---------- describe_error 带阶段(第 15 轮 codex 的 Critical) ----------

def test_describe_error_carries_the_phase_the_orchestrator_tagged():
    exc = ValueError("close=2728.0")
    exc.invest_phase = "validation"
    out = api.describe_error(exc)
    assert out["phase"] == "validation" and out["type"] == "ValueError"


def test_an_explicit_phase_wins_over_the_tag():
    exc = ValueError("x")
    exc.invest_phase = "validation"
    assert api.describe_error(exc, "declaration")["phase"] == "declaration"


def test_a_grid_error_is_a_declaration_error_even_without_a_tag():
    from research_grid import GridError
    assert api.describe_error(GridError("param_grid 必须恰好 3 个参数"))["phase"] == "declaration"


def test_an_untagged_error_has_no_phase_and_is_therefore_treated_as_validation_downstream():
    assert api.describe_error(ValueError("x"))["phase"] is None
