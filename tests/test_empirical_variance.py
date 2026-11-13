"""阶段 3:DSR 的经验方差 V(稳健版)+ 逐试验夏普账本。纯逻辑 + tmp_path,不依赖 vn.py。

规则见 risk/dsr.py 文件头与 ADR「阶段 3 前置:DSR 的方差 V」:V = max(V_经验, 1/(T-1)),只能更严;
V_经验 取**各次评估内部格子夏普方差的中位数**(第 15 轮 codex 的 Critical:样本由候选自己的网格产生,
一次极端评估不得把后续所有候选的 DSR 打成 0);评估次数 < 3 退回理论值并显式记录"样本不足"。
"""
import json
import math
import random

import pytest

from risk.dsr import (MAX_RUNS_USED, MAX_TRIAL_SHARPE, MIN_CELLS_PER_RUN, MIN_EMPIRICAL_RUNS, deflated_sharpe,
                      deflated_sharpe_detail, expected_max_sharpe, run_variance, variance_of_sharpes)
from risk.trial_ledger import (TrialLedgerError, grid_run_variances, record_sharpes, record_trials, total_trials,
                               valid_trial_sharpes)

T = 367                                            # 池化验证段的交易日数(与 η 标定同一窗口)
THEORY = 1.0 / (T - 1)


def _returns(seed=1, n=T, mu=0.0012, sd=0.01):
    rng = random.Random(seed)
    return [rng.gauss(mu, sd) for _ in range(n)]


# ---------- 一次评估的方差 ----------

def test_run_variance_matches_the_textbook_sample_variance():
    values = [0.01, -0.02, 0.03, 0.005, -0.004, 0.02]
    mean = sum(values) / len(values)
    assert run_variance(values) == pytest.approx(sum((x - mean) ** 2 for x in values) / (len(values) - 1))


def test_an_evaluation_with_too_few_valid_cells_has_no_variance():
    assert run_variance([0.1] * (MIN_CELLS_PER_RUN - 1)) is None and run_variance([]) is None
    assert run_variance([0.1] * MIN_CELLS_PER_RUN) == 0.0


# ---------- V = max(中位数, 1/(T-1)) ----------

def test_with_too_few_evaluations_it_falls_back_to_theory_and_says_so():
    info = variance_of_sharpes(T, [0.04] * (MIN_EMPIRICAL_RUNS - 1))
    assert info["basis"] == "insufficient_samples"
    assert info["v_used"] == info["v_theory"] == THEORY and info["n_evaluations"] == MIN_EMPIRICAL_RUNS - 1
    assert info["v_empirical"] is not None                                  # 算出来了,只是不采信


def test_without_any_history_it_reports_insufficient_samples():
    for empty in (None, []):
        info = variance_of_sharpes(T, empty)
        assert info["basis"] == "insufficient_samples" and info["n_evaluations"] == 0
        assert info["v_used"] == THEORY and info["v_empirical"] is None


def test_enough_evaluations_with_a_large_median_use_the_empirical_value():
    info = variance_of_sharpes(T, [0.04, 0.05, 0.03, 0.06])
    assert info["basis"] == "empirical" and info["v_empirical"] == pytest.approx(0.045)      # 偶数个:中间两个的均值
    assert info["v_used"] == info["v_empirical"] > THEORY


def test_a_small_median_cannot_go_below_the_theoretical_floor():
    info = variance_of_sharpes(T, [1e-6, 2e-6, 3e-6])
    assert info["basis"] == "theory" and info["v_used"] == THEORY and info["v_empirical"] < THEORY


@pytest.mark.parametrize("seed", range(25))
def test_v_used_is_never_below_the_theoretical_value(seed):
    rng = random.Random(seed)
    variances = [rng.choice([0.0, 1e-9, 1e-4, 0.01, 0.3, 1.0]) for _ in range(rng.randint(0, 40))]
    assert variance_of_sharpes(T, variances)["v_used"] >= THEORY - 1e-18


def test_the_median_is_not_moved_by_a_minority_of_poisoned_evaluations():
    """第 15 轮 codex 的 Critical:一次"网格极端分裂"的评估(限幅后方差 0.28)混进来,
    把所有格子夏普并成一个样本时 V 被拉到让此后每个候选的 DSR 恒为 0;取中位数,单次评估进不了结果。"""
    honest = [0.0004, 0.0005, 0.0003, 0.0006, 0.0004]
    poisoned = variance_of_sharpes(T, honest + [0.279])
    clean = variance_of_sharpes(T, honest + [0.0004])                       # 同样 6 次评估,最后一次是诚实的
    assert poisoned["v_empirical"] == pytest.approx(clean["v_empirical"], rel=0.3)
    assert poisoned["v_used"] == clean["v_used"]                            # 对门槛的影响:一点都没有
    assert poisoned["v_empirical"] < 0.001                                  # 中位数没被 0.279 拉动


def test_the_poisoned_campaign_does_not_collapse_every_candidates_dsr_to_zero():
    returns = _returns(mu=0.0016)
    honest = [0.0004, 0.0005, 0.0003, 0.0006, 0.0004]
    clean_dsr = deflated_sharpe(returns, 100, honest)
    poisoned_dsr = deflated_sharpe(returns, 100, honest + [0.279])
    assert clean_dsr > 0.0 and poisoned_dsr == pytest.approx(clean_dsr, rel=0.1)


def test_poisoning_needs_a_majority_of_evaluations_and_that_is_stated_not_hidden():
    """如实记录残余:投毒者若占**多数**评估,中位数仍会被拉高(V 只会更严,也不会比"全部混在一起"更糟)。"""
    assert variance_of_sharpes(T, [0.279, 0.279, 0.279, 0.0004])["v_empirical"] == pytest.approx(0.279)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, True, None, "0.1"])
def test_invalid_entries_in_the_history_are_ignored_not_trusted(bad):
    info = variance_of_sharpes(T, [0.04, 0.05, 0.03, bad])
    assert info["n_evaluations"] == 3 and math.isfinite(info["v_used"])


def test_expected_max_sharpe_refuses_a_variance_below_theory():
    with pytest.raises(ValueError):
        expected_max_sharpe(100, T, variance=0.5 * THEORY)
    with pytest.raises(ValueError):
        expected_max_sharpe(100, T, variance=float("nan"))
    assert expected_max_sharpe(100, T, variance=THEORY) == expected_max_sharpe(100, T)


# ---------- DSR 只会更严 ----------

def test_a_big_empirical_variance_makes_the_dsr_stricter_never_looser():
    returns = _returns()
    assert deflated_sharpe(returns, 300, [0.04, 0.05, 0.03]) < deflated_sharpe(returns, 300)


def test_a_small_empirical_variance_leaves_the_dsr_exactly_at_the_theoretical_value():
    returns = _returns()
    assert deflated_sharpe(returns, 300, [1e-7, 2e-7, 1e-7, 3e-7]) == deflated_sharpe(returns, 300)


@pytest.mark.parametrize("seed", range(15))
def test_the_dsr_with_history_never_exceeds_the_dsr_without(seed):
    rng = random.Random(seed)
    returns = _returns(seed=seed, mu=rng.uniform(0.0005, 0.003))
    history = [rng.choice([0.0, 1e-5, 0.001, 0.05, 0.4]) for _ in range(rng.randint(0, 30))]
    n_trials = rng.choice([5, 80, 400, 5000])
    assert deflated_sharpe(returns, n_trials, history) <= deflated_sharpe(returns, n_trials) + 1e-12


def test_the_detail_reports_why_it_used_that_variance():
    detail = deflated_sharpe_detail(_returns(), 300, [0.04, 0.05])                        # 2 次 < 3
    assert detail["variance"]["basis"] == "insufficient_samples" and detail["variance"]["n_evaluations"] == 2
    assert detail["dsr"] == deflated_sharpe(_returns(), 300)


def test_a_degenerate_input_has_no_variance_info_and_zero_dsr():
    assert deflated_sharpe_detail([0.01] * 50, 10, [0.04] * 5) == {"dsr": 0.0, "variance": None}
    assert deflated_sharpe_detail([0.01, 0.02], 10)["variance"] is None


# ---------- 账本:夏普记录不计入试验次数,按评估取方差 ----------

def test_sharpe_records_do_not_change_the_trial_count(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_trials(path, "grid_cell", 27, at=1.0)
    record_sharpes(path, "grid", [0.01, -0.02, 0.03], at=2.0)
    record_trials(path, "gate_run", 1, at=3.0)
    assert total_trials(path) == 28


def test_the_ledger_gives_one_variance_per_evaluation_most_recent_last(tmp_path):
    path = tmp_path / "trials.jsonl"
    first, second = [0.01, -0.02, 0.03, 0.0, 0.02], [0.1, -0.1, 0.2, 0.0, 0.05, -0.05]
    record_sharpes(path, "grid", first, at=1.0)
    record_trials(path, "grid_cell", 5, at=1.5)
    record_sharpes(path, "grid", second, at=2.0)
    assert grid_run_variances(path) == [pytest.approx(run_variance(first)), pytest.approx(run_variance(second))]


def test_the_gate_sharpes_and_tiny_evaluations_are_not_in_the_variance_sample(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_sharpes(path, "gate", [0.05], at=1.0)                                         # 门槛评估的单个数:没有内部分散度
    record_sharpes(path, "grid", [0.01, 0.02], at=2.0)                                   # 有效格子 < 5
    record_sharpes(path, "grid", [0.0, 0.1, -0.1, 0.05, -0.05], at=3.0)
    assert len(grid_run_variances(path)) == 1


def test_only_the_most_recent_evaluations_are_used(tmp_path):
    path = tmp_path / "trials.jsonl"
    scale = 0.001                                                          # 夏普值要在单条试验的上限(±1)以内
    for i in range(MAX_RUNS_USED + 7):
        record_sharpes(path, "grid", [0.0, scale * (i + 1), -scale * (i + 1), 0.005, -0.005], at=float(i))
    out = grid_run_variances(path)
    assert len(out) == MAX_RUNS_USED
    last = scale * (MAX_RUNS_USED + 7)
    assert out[-1] == pytest.approx(run_variance([0.0, last, -last, 0.005, -0.005]))


def test_an_absent_ledger_has_no_history(tmp_path):
    assert grid_run_variances(tmp_path / "none.jsonl") == []


def test_a_ledger_written_before_this_change_is_still_readable(tmp_path):
    path = tmp_path / "trials.jsonl"
    path.write_text('{"kind": "submission", "count": 1, "at": 1.0}\n{"kind": "grid_cell", "count": 27, "at": 2.0}\n',
                    encoding="utf-8")
    assert total_trials(path) == 28 and grid_run_variances(path) == []


@pytest.mark.parametrize("bad", [
    [float("nan")], [float("inf")], [float("-inf")], [MAX_TRIAL_SHARPE + 0.01], [-MAX_TRIAL_SHARPE - 0.01],
    [True], ["0.1"], [None], [], "0.1", None, 5,
])
def test_invalid_sharpes_are_rejected_as_a_whole(tmp_path, bad):
    path = tmp_path / "trials.jsonl"
    assert valid_trial_sharpes(bad) is None
    with pytest.raises(TrialLedgerError):
        record_sharpes(path, "grid", bad, at=1.0)
    assert not path.exists()


def test_a_batch_with_one_bad_value_among_good_ones_is_rejected_entirely(tmp_path):
    path = tmp_path / "trials.jsonl"
    with pytest.raises(TrialLedgerError):
        record_sharpes(path, "grid", [0.01, 0.02, float("nan"), 0.03], at=1.0)
    assert not path.exists()


def test_an_unknown_source_is_rejected(tmp_path):
    with pytest.raises(TrialLedgerError):
        record_sharpes(tmp_path / "t.jsonl", "candidate", [0.1], at=1.0)


@pytest.mark.parametrize("line", [
    '{"kind": "sharpes", "source": "grid", "values": [0.1, NaN], "at": 1}',
    '{"kind": "sharpes", "source": "grid", "values": [], "at": 1}',
    '{"kind": "sharpes", "source": "grid", "values": "0.1", "at": 1}',
    '{"kind": "sharpes", "source": "evil", "values": [0.1], "at": 1}',
    '{"kind": "sharpes", "source": "grid", "values": [99.0], "at": 1}',
    '{"kind": "sharpes", "source": "grid", "at": 1}',
])
def test_a_corrupted_sharpe_record_makes_the_whole_ledger_fail_closed(tmp_path, line):
    path = tmp_path / "trials.jsonl"
    path.write_text('{"kind": "grid_cell", "count": 27, "at": 1}\n' + line + "\n", encoding="utf-8")
    for call in (lambda: total_trials(path), lambda: grid_run_variances(path),
                 lambda: record_trials(path, "gate_run", 1, at=2.0), lambda: record_sharpes(path, "gate", [0.1], at=2.0)):
        with pytest.raises(TrialLedgerError):
            call()


def test_the_ledger_file_never_contains_non_finite_numbers(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_sharpes(path, "grid", [0.1, 0.2, 0.0, 0.05, 0.3], at=1.0)
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    for line in text.splitlines():
        json.loads(line)
