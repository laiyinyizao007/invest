"""阶段 3:逐试验夏普 + DSR 的经验方差 V。纯逻辑 + tmp_path,不依赖 vn.py。

规则见 risk/dsr.py 文件头与 ADR「阶段 3 前置:DSR 的方差 V」:V = max(V_经验, 1/(T-1)),只能更严;
试验数 < 20 退回理论值并显式记录"样本不足"。
"""
import json
import math
import random

import pytest

from risk.dsr import (MAX_TRIAL_SHARPE, MIN_EMPIRICAL_TRIALS, deflated_sharpe, deflated_sharpe_detail,
                      expected_max_sharpe, merge_stats, sharpe_stats, variance_of_sharpes)
from risk.trial_ledger import (TrialLedgerError, record_sharpes, record_trials, total_trials, trial_sharpe_stats,
                               valid_trial_sharpes)

T = 367                                            # 池化验证段的交易日数(与 η 标定同一窗口)


def _returns(seed=1, n=T, mu=0.0012, sd=0.01):
    rng = random.Random(seed)
    return [rng.gauss(mu, sd) for _ in range(n)]


def _stats(values):
    return sharpe_stats(values)


# ---------- 在线统计量 ----------

def test_sharpe_stats_matches_the_textbook_sample_variance():
    values = [0.01, -0.02, 0.03, 0.005, -0.004]
    n, mean, m2 = sharpe_stats(values)
    assert n == 5 and math.isclose(mean, sum(values) / 5)
    assert math.isclose(m2 / (n - 1), sum((x - mean) ** 2 for x in values) / (n - 1))


def test_merging_two_batches_equals_the_statistics_of_their_concatenation():
    rng = random.Random(7)
    a = [rng.gauss(0.0, 0.05) for _ in range(40)]
    b = [rng.gauss(0.01, 0.08) for _ in range(25)]
    merged = merge_stats(sharpe_stats(a), sharpe_stats(b))
    whole = sharpe_stats(a + b)
    assert merged[0] == whole[0]
    assert math.isclose(merged[1], whole[1], abs_tol=1e-12)
    assert math.isclose(merged[2], whole[2], rel_tol=1e-9)


def test_merging_with_an_empty_batch_is_the_identity():
    s = sharpe_stats([0.1, 0.2, 0.3])
    assert merge_stats(s, (0, 0.0, 0.0)) == s and merge_stats((0, 0.0, 0.0), s) == s
    assert merge_stats((0, 0.0, 0.0), (0, 0.0, 0.0)) == (0, 0.0, 0.0)


# ---------- V = max(V_经验, 1/(T-1)) ----------

def test_with_too_few_trials_it_falls_back_to_theory_and_says_so():
    """试验数 < 20:经验方差不可靠。退回理论值,并且**显式记录**(不得静默)。"""
    info = variance_of_sharpes(T, _stats([0.5, -0.5, 0.4] * 6))            # 18 条 < 20
    assert info["basis"] == "insufficient_samples"
    assert info["v_used"] == info["v_theory"] == 1.0 / (T - 1)
    assert info["n_sharpes"] == 18
    assert info["v_empirical"] is not None                                   # 算出来了,只是不采信


def test_without_any_statistics_it_also_reports_insufficient_samples():
    info = variance_of_sharpes(T, None)
    assert info["basis"] == "insufficient_samples" and info["n_sharpes"] == 0
    assert info["v_used"] == 1.0 / (T - 1) and info["v_empirical"] is None


def test_a_large_empirical_variance_is_used_when_there_are_enough_trials():
    rng = random.Random(3)
    values = [rng.gauss(0.0, 0.2) for _ in range(MIN_EMPIRICAL_TRIALS + 10)]   # 方差 ≈ 0.04 ≫ 1/366
    info = variance_of_sharpes(T, _stats(values))
    assert info["basis"] == "empirical"
    assert info["v_used"] == info["v_empirical"] > info["v_theory"]


def test_a_tiny_empirical_variance_cannot_go_below_the_theoretical_floor():
    """V 只能更大不能更小:经验方差比理论值小时,理论值作为下限生效。"""
    values = [0.001 * (i % 3) for i in range(60)]                              # 方差 ≈ 6.7e-7 ≪ 1/366
    info = variance_of_sharpes(T, _stats(values))
    assert info["basis"] == "theory" and info["v_used"] == info["v_theory"]
    assert info["v_empirical"] < info["v_theory"]


@pytest.mark.parametrize("seed", range(25))
def test_v_used_is_never_below_the_theoretical_value(seed):
    rng = random.Random(seed)
    n = rng.randint(0, 80)
    values = [rng.gauss(0, rng.choice([1e-6, 1e-3, 0.05, 0.5])) for _ in range(n)]
    info = variance_of_sharpes(T, _stats(values) if n else None)
    assert info["v_used"] >= info["v_theory"] - 1e-18


def test_expected_max_sharpe_refuses_a_variance_below_theory():
    with pytest.raises(ValueError):
        expected_max_sharpe(100, T, variance=0.5 / (T - 1))
    with pytest.raises(ValueError):
        expected_max_sharpe(100, T, variance=float("nan"))
    assert expected_max_sharpe(100, T, variance=1.0 / (T - 1)) == expected_max_sharpe(100, T)


# ---------- DSR 只会更严 ----------

def test_a_big_empirical_variance_makes_the_dsr_stricter_never_looser():
    returns = _returns()
    base = deflated_sharpe(returns, 300)
    rng = random.Random(11)
    wide = _stats([rng.gauss(0.0, 0.15) for _ in range(50)])
    assert deflated_sharpe(returns, 300, wide) < base


def test_a_small_empirical_variance_leaves_the_dsr_exactly_at_the_theoretical_value():
    returns = _returns()
    tight = _stats([0.0005 * (i % 4) for i in range(50)])
    assert deflated_sharpe(returns, 300, tight) == deflated_sharpe(returns, 300)


@pytest.mark.parametrize("seed", range(15))
def test_the_dsr_with_statistics_never_exceeds_the_dsr_without(seed):
    """不变量:不管账本里是什么样本,带上它之后的 DSR 都不会比不带更高。"""
    rng = random.Random(seed)
    returns = _returns(seed=seed, mu=rng.uniform(0.0005, 0.003))
    stats = _stats([rng.gauss(rng.uniform(-0.1, 0.1), rng.choice([1e-4, 0.01, 0.1, 0.3]))
                    for _ in range(rng.randint(0, 80))])
    n_trials = rng.choice([5, 80, 400, 5000])
    assert deflated_sharpe(returns, n_trials, stats) <= deflated_sharpe(returns, n_trials) + 1e-12


def test_the_detail_reports_why_it_used_that_variance():
    detail = deflated_sharpe_detail(_returns(), 300, _stats([0.2, -0.1, 0.05] * 5))     # 15 条
    assert detail["variance"]["basis"] == "insufficient_samples"
    assert detail["dsr"] == deflated_sharpe(_returns(), 300)


def test_a_degenerate_input_has_no_variance_info_and_zero_dsr():
    detail = deflated_sharpe_detail([0.01] * 50, 10, _stats([0.1] * 30))               # 零波动
    assert detail == {"dsr": 0.0, "variance": None}
    assert deflated_sharpe_detail([0.01, 0.02], 10)["variance"] is None


# ---------- 账本:夏普记录不计入试验次数 ----------

def test_sharpe_records_do_not_change_the_trial_count(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_trials(path, "grid_cell", 27, at=1.0)
    record_sharpes(path, "grid", [0.01, -0.02, 0.03], at=2.0)
    record_trials(path, "gate_run", 1, at=3.0)
    assert total_trials(path) == 28                                           # 夏普记录不是试验


def test_trial_sharpe_stats_equal_the_statistics_of_everything_recorded(tmp_path):
    path = tmp_path / "trials.jsonl"
    first, second = [0.01, -0.02, 0.03], [0.05, 0.0]
    record_sharpes(path, "grid", first, at=1.0)
    record_trials(path, "grid_cell", 5, at=1.5)
    record_sharpes(path, "gate", second, at=2.0)
    n, mean, m2 = trial_sharpe_stats(path)
    whole = sharpe_stats(first + second)
    assert n == 5 and math.isclose(mean, whole[1]) and math.isclose(m2, whole[2], rel_tol=1e-9)


def test_an_absent_ledger_has_no_sharpe_statistics(tmp_path):
    assert trial_sharpe_stats(tmp_path / "none.jsonl") == (0, 0.0, 0.0)


def test_a_ledger_written_before_this_change_is_still_readable(tmp_path):
    """旧账本(只有三种计数记录)原样可读,不需要迁移。"""
    path = tmp_path / "trials.jsonl"
    path.write_text('{"kind": "submission", "count": 1, "at": 1.0}\n{"kind": "grid_cell", "count": 27, "at": 2.0}\n',
                    encoding="utf-8")
    assert total_trials(path) == 28 and trial_sharpe_stats(path) == (0, 0.0, 0.0)


@pytest.mark.parametrize("bad", [
    [float("nan")], [float("inf")], [float("-inf")], [MAX_TRIAL_SHARPE + 0.01], [-MAX_TRIAL_SHARPE - 0.01],
    [True], ["0.1"], [None], [], "0.1", None, 5,
])
def test_invalid_sharpes_are_rejected_as_a_whole(tmp_path, bad):
    """样本可能来自候选进程:混进一个坏值就整批丢弃,而不是只信剩下的。"""
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
    with pytest.raises(TrialLedgerError):
        total_trials(path)
    with pytest.raises(TrialLedgerError):
        trial_sharpe_stats(path)
    with pytest.raises(TrialLedgerError):
        record_trials(path, "gate_run", 1, at=2.0)             # 损坏的账本上不继续累计
    with pytest.raises(TrialLedgerError):
        record_sharpes(path, "gate", [0.1], at=2.0)


def test_the_ledger_file_never_contains_non_finite_numbers(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_sharpes(path, "grid", [0.1, 0.2], at=1.0)
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    for line in text.splitlines():
        json.loads(line)
