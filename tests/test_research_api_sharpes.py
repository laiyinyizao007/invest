"""research_api 里与逐试验夏普有关的几处:子入口自己复查参数、作业结束后写账本、样本整批校验。"""
import math
from types import SimpleNamespace

import pytest

import research_api as api
from risk.trial_ledger import record_trials, total_trials, trial_sharpe_stats


def args(**kw):
    base = {"prior_sharpe_n": None, "prior_sharpe_mean": None, "prior_sharpe_m2": None}
    base.update(kw)
    return SimpleNamespace(**base)


# ---------- 子入口自己复查 ----------

def test_no_prior_statistics_means_none():
    assert api.child_sharpe_stats(args()) is None


def test_valid_statistics_pass_through():
    assert api.child_sharpe_stats(args(prior_sharpe_n=30, prior_sharpe_mean=0.01, prior_sharpe_m2=0.5)) == (30, 0.01, 0.5)


@pytest.mark.parametrize("kw", [
    {"prior_sharpe_n": 30},                                                              # 只给一个
    {"prior_sharpe_n": 30, "prior_sharpe_mean": 0.0},                                    # 只给两个
    {"prior_sharpe_n": -1, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": 0.0},
    {"prior_sharpe_n": True, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": 0.0},
    {"prior_sharpe_n": 10**9, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": 0.0},
    {"prior_sharpe_n": 30, "prior_sharpe_mean": float("nan"), "prior_sharpe_m2": 0.5},
    {"prior_sharpe_n": 30, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": float("inf")},
    {"prior_sharpe_n": 30, "prior_sharpe_mean": 5.0, "prior_sharpe_m2": 0.5},            # |mean| 超上限
    {"prior_sharpe_n": 30, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": -0.1},           # 离差平方和不能为负
    {"prior_sharpe_n": 30, "prior_sharpe_mean": 0.0, "prior_sharpe_m2": 1e9},            # 离差平方和不可能这么大
    {"prior_sharpe_n": 0, "prior_sharpe_mean": 0.1, "prior_sharpe_m2": 0.0},             # n=0 时必须全零
])
def test_invalid_statistics_are_refused_by_the_child_itself(kw):
    """子进程入口可被直接调用,不能假设父进程查过。"""
    with pytest.raises(api.JobError) as exc:
        api.child_sharpe_stats(args(**kw))
    assert exc.value.code == api.EXIT_REFUSED


def test_the_argv_round_trips_floats_without_loss():
    stats = (37, 0.1 + 0.2, 1 / 3)
    argv = api.sharpe_stats_argv(stats)
    ns = SimpleNamespace(prior_sharpe_n=int(argv[1]), prior_sharpe_mean=float(argv[3]), prior_sharpe_m2=float(argv[5]))
    assert api.child_sharpe_stats(ns) == stats


def test_the_internal_subcommands_accept_the_new_arguments():
    parser = api.build_parser()
    stats_args = ["--prior-sharpe-n", "20", "--prior-sharpe-mean", "0.01", "--prior-sharpe-m2", "0.3"]
    gate = parser.parse_args(["_gate_child", "--file", "f.py", "--cls", "C", "--params", "{}", "--sha256", "0" * 64,
                              "--on-plateau", "0", "--random-runs", "5", "--prior-trials", "3", *stats_args])
    evaluate = parser.parse_args(["_evaluate_child", "--file", "f.py", "--cls", "C", "--sha256", "0" * 64,
                                  "--random-runs", "5", "--prior-trials", "3", *stats_args])
    for ns in (gate, evaluate):
        assert (ns.prior_sharpe_n, ns.prior_sharpe_mean, ns.prior_sharpe_m2) == (20, 0.01, 0.3)


# ---------- 作业结束后写账本 ----------

@pytest.fixture
def paths(tmp_path):
    return {"results_dir": str(tmp_path)}


def body(cells=(0.01, -0.02, 0.03), oos=1.5):
    return {"trials": {"prior": 10, "grid_cells": 27, "total": 37, "cell_sharpes": list(cells)},
            "metrics": {"oos_sharpe": oos}}


def test_a_job_writes_cell_and_gate_sharpes_without_changing_the_trial_count(paths):
    record_trials(api.trials_path(paths), "grid_cell", 27, at=1.0)
    b = body()
    api.record_job_sharpes(paths, b)
    n, mean, _ = trial_sharpe_stats(api.trials_path(paths))
    assert n == 4                                                        # 3 个格子 + 1 个门槛评估
    assert total_trials(api.trials_path(paths)) == 27                    # 夏普记录不是试验
    assert "cell_sharpes" not in b["trials"]                             # 落盘的结果里不留样本
    assert math.isclose(mean, (0.01 - 0.02 + 0.03 + 1.5 / math.sqrt(240)) / 4)


def test_the_gate_sharpe_is_recorded_as_daily_not_annualized(paths):
    api.record_job_sharpes(paths, {"metrics": {"oos_sharpe": 2.4}})
    n, mean, _ = trial_sharpe_stats(api.trials_path(paths))
    assert n == 1 and math.isclose(mean, 2.4 / math.sqrt(240))


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
        api.prior_sharpe_stats(paths)


def test_prior_statistics_come_from_the_ledger(paths):
    assert api.prior_sharpe_stats(paths) == (0, 0.0, 0.0)
    api.record_job_sharpes(paths, body(cells=(0.1, 0.2)))
    assert api.prior_sharpe_stats(paths)[0] == 3
