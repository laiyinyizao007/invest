"""risk/promotion_gate.py 单元测试(纯 Python)。"""
import math
from dataclasses import replace

import pytest


from risk.promotion_gate import (  # noqa: E402
    CandidateMetrics,
    GateThresholds,
    check_gate,
    sharpe_ci,
    sharpe_standard_error,
    suspicious_sharpe_cap,
)

GOOD = CandidateMetrics(
    oos_sharpe=2.0, oos_years=5.0, max_drawdown_pct=-12.0, max_drawdown_yuan=-1200,
    round_trips=200, random_entry_percentile=98.0, stress_sharpe=1.4,
    data_adjusted=True, parameter_on_plateau=True, dsr=0.99, n_trials=50,
)


# ---------- 夏普的统计误差 ----------

def test_standard_error_matches_formula():
    assert sharpe_standard_error(1.5, 3) == pytest.approx(((1 + 0.5 * 1.5**2) / 3) ** 0.5)
    assert sharpe_standard_error(1.5, 3) == pytest.approx(0.8416, abs=1e-3)


def test_shorter_sample_means_larger_error():
    assert sharpe_standard_error(1.0, 1) > sharpe_standard_error(1.0, 10)


def test_three_year_sharpe_1_5_confidence_interval_includes_zero():
    """文档中的例子:3 年样本外夏普 1.5,95% 区间下界 < 0,不能排除没有优势。"""
    low, high = sharpe_ci(1.5, 3)
    assert low < 0 < high
    assert low == pytest.approx(-0.15, abs=0.02)


def test_standard_error_rejects_non_positive_years():
    with pytest.raises(ValueError):
        sharpe_standard_error(1.0, 0)


# ---------- 门槛 ----------

def test_good_candidate_passes():
    assert check_gate(GOOD) == []


@pytest.mark.parametrize(
    "change, keyword",
    [
        ({"oos_sharpe": 1.2}, "样本外夏普"),
        ({"max_drawdown_pct": -25.0}, "最大回撤"),
        ({"max_drawdown_yuan": -2500}, "元"),
        ({"round_trips": 80}, "往返"),
        ({"random_entry_percentile": 80.0}, "随机入场"),
        ({"stress_sharpe": 0.4}, "成本翻倍"),
        ({"data_adjusted": False}, "未复权"),
        ({"parameter_on_plateau": False}, "参数高原"),
        ({"dsr": 0.80}, "DSR"),
    ],
)
def test_each_criterion_fails_independently(change, keyword):
    fails = check_gate(replace(GOOD, **change))
    assert any(keyword in f for f in fails), fails


def test_suspiciously_high_sharpe_is_rejected_not_celebrated():
    fails = check_gate(replace(GOOD, oos_sharpe=3.5))
    assert any("前视偏差" in f for f in fails)


def test_short_sample_fails_on_confidence_interval_even_with_high_point_estimate():
    fails = check_gate(replace(GOOD, oos_sharpe=1.6, oos_years=2.0))
    assert any("置信区间" in f for f in fails)


def test_drawdown_sign_does_not_matter():
    assert check_gate(replace(GOOD, max_drawdown_pct=12.0, max_drawdown_yuan=1200)) == []


def test_non_positive_years_is_reported():
    fails = check_gate(replace(GOOD, oos_years=0))
    assert any("年数" in f for f in fails)


def test_thresholds_are_configurable():
    loose = GateThresholds(min_round_trips=50)
    assert check_gate(replace(GOOD, round_trips=80), loose) == []


def test_dsr_threshold_is_locked_at_0_95():
    assert GateThresholds().min_dsr == 0.95


def test_dsr_boundary_is_inclusive():
    assert check_gate(replace(GOOD, dsr=0.95)) == []
    assert any("DSR" in f for f in check_gate(replace(GOOD, dsr=0.9499)))


def test_missing_dsr_fails_closed():
    """没有算 DSR(例如旧格式的指标文件)不能放行。"""
    fails = check_gate(replace(GOOD, dsr=None))
    assert any("DSR" in f and "未计算" in f for f in fails)


def test_dsr_without_trial_count_fails_closed():
    """DSR 必须带着试验次数:n_trials 缺失(0)意味着无法核对去通胀是否到位。"""
    fails = check_gate(replace(GOOD, n_trials=0))
    assert any("试验次数" in f for f in fails)


def test_dsr_failure_message_names_trials():
    fails = check_gate(replace(GOOD, dsr=0.4, n_trials=300))
    assert any("300" in f for f in fails)


def test_all_failures_are_reported_together():
    bad = CandidateMetrics(0.5, 1.0, -40.0, -4000, 10, 50.0, 0.0, False, False)
    assert len(check_gate(bad)) >= 7


# ---------- check_gate 对审计字段严格 fail-closed ----------

@pytest.mark.parametrize("bad_dsr", [float("nan"), float("inf"), -0.1, 1.5, True])
def test_non_finite_or_out_of_range_dsr_fails_closed(bad_dsr):
    fails = check_gate(replace(GOOD, dsr=bad_dsr))
    assert any("DSR" in f for f in fails), bad_dsr


@pytest.mark.parametrize("bad_n", [True, 1.5, float("nan"), -3, 0, "50"])
def test_invalid_trial_counts_fail_closed(bad_n):
    fails = check_gate(replace(GOOD, n_trials=bad_n))
    assert any("试验次数" in f for f in fails), bad_n


# ---------- 第 55 轮之十六:「可疑夏普」上限与 DSR 自洽 ----------
#
# **来历**:上限原先是写死的 3.0,**与试验次数无关**;而 DSR 是去通胀判据,
# 试验次数 N 越多,「纯靠运气能挑到的最高夏普」越高 ⇒ 同一个年化夏普在 N 大时
# **通过** DSR、在 N 小时**不通过** ⇒ 两条规则互相打架:N 大时那条与 N 无关的
# 固定上限会**先一步**把 DSR 认证过的候选毙掉(而 DSR 那一关根本没走)。
# **改法**:上限 = max(兜底下限 3.0, 由 DSR 认证线年化得到的值)(见 ADR
# `docs/adr/20261117-dsr-consistent-suspicious-sharpe.md`)。
#
# **口径要点**:上限**只放宽不收紧** ⇒ 旧规则放行的候选,新规则一个都不会被毙掉。
# 而「放宽」真正生效的场景是 **N 大**(认证线被抬到 3.0 之上)——
# N 小的时候认证线低于 3.0 ⇒ 兜底生效 ⇒ 行为与旧规则完全一致。

# 放行侧的那只候选:夏普 3.05(刚过旧的 3.0)、样本外 2.5 年(T = 600 个交易日)、
# 试验 1000 次。
# - 上限:认证线(N=1000, T=600)年化 = 3.1121 ⇒ 3.05 在上限之内(旧规则会毙掉它);
# - 置信区间下界 3.05 - 1.96·√((1+0.5·3.05²)/2.5) ≈ 0.103 > 0 ⇒ 过;
# - DSR 0.99 ≥ 0.95 ⇒ 过。
RELAXED = replace(GOOD, oos_sharpe=3.05, oos_years=2.5, n_trials=1000, dsr=0.99)


def test_the_suspicious_cap_is_relaxed_when_there_are_many_trials():
    """**放行侧**:试验次数多时,夏普 3.05 不再被「可疑」这条**抢先**毙掉。

    旧规则(固定 3.0)会把这只候选判成「先排查前视偏差」⇒ 与 DSR(0.99)打架。
    """
    assert check_gate(RELAXED) == []
    assert not any("前视偏差" in f for f in check_gate(RELAXED))


def test_the_suspicious_cap_still_rejects_when_there_are_few_trials():
    """**拦截侧**:同样的夏普,试验次数少 ⇒ 上限回到兜底下限 3.0 ⇒ 仍然拒绝。

    这是「放宽不是无条件放弃」的那一侧:DSR 认证不通过的高夏普,照样被毙。
    """
    fails = check_gate(replace(RELAXED, n_trials=50))
    assert any("前视偏差" in f for f in fails)
    assert any("3.00" in f for f in fails), fails       # 报出来的是兜底下限,不是别处的数


def test_the_suspicious_cap_is_never_below_the_old_constant():
    """上限**只放宽不收紧**:任何 (N, T) 下限值都 ≥ 3.0 ⇒ 旧规则放行的不会被毙掉。"""
    for n_trials in (1, 2, 5, 20, 50, 500, 5000):
        for n_obs in (3, 60, 120, 240, 480, 600, 1200):
            cap = suspicious_sharpe_cap(n_trials, n_obs)
            assert cap >= 3.0, (n_trials, n_obs, cap)


def test_the_suspicious_cap_grows_with_the_number_of_trials():
    """试验次数越多 ⇒ 上限越高(认证线被抬高)。

    口径要点:上限 = max(兜底下限 3.0, 认证线) ⇒ 在认证线还低于 3.0 的区间里,
    上限**贴住 3.0 不动**(只放宽不收紧 ⇒ 老规则放行的候选一个都不会被毙掉);
    越过 3.0 之后才随试验次数严格上升。
    """
    caps = [suspicious_sharpe_cap(n, 320) for n in (10, 50, 500, 1000)]
    assert caps == sorted(caps)
    assert all(b > a for a, b in zip(caps, caps[1:])), caps
    assert caps[0] == 3.0, caps                      # N=10 时认证线还够不到 3.0 ⇒ 贴住兜底
    assert caps[-1] == pytest.approx(4.2771, abs=1e-3), caps


def test_the_suspicious_cap_is_flat_at_the_floor_until_the_line_crosses_it():
    """**兜底是夹紧**:认证线低于 3.0 时上限恒为 3.0(不随试验次数变)。

    ⇒ 「上限只放宽不收紧」这句承诺在这条探针里是可测的:把 N 从 1 加到 500,
    上限一动不动 ⇒ 旧规则(3.0)在该区间内放行/毙掉的候选,新规则**判得一样**。
    """
    assert [suspicious_sharpe_cap(n, 600) for n in (1, 10, 50, 200, 500)] == [3.0] * 5
    assert suspicious_sharpe_cap(1000, 600) == pytest.approx(3.1121, abs=1e-3)


def test_the_suspicious_cap_falls_back_to_the_floor_on_degenerate_input():
    """退化输入 ⇒ 用兜底下限(3.0),**不放宽**(fail-closed)。"""
    for bad in (0, -1, True, 1.5, "50"):
        assert suspicious_sharpe_cap(bad, 600) == 3.0
    for bad_obs in (0, 1, -5, True, 1.5, "600"):
        assert suspicious_sharpe_cap(50, bad_obs) == 3.0


def test_the_suspicious_cap_needs_both_a_trial_count_and_a_long_enough_sample():
    """缺试验次数 ⇒ 兜底(3.0);样本太短 ⇒ 兜底(3.0)。"""
    assert suspicious_sharpe_cap(0, 600) == 3.0
    assert suspicious_sharpe_cap(50, 2) == 3.0


def test_the_suspicious_cap_is_derived_from_the_dsr_threshold():
    """上限的来源是**门槛自己的 DSR 阈值**:阈值抬到 0.99 ⇒ 认证线更高 ⇒ 上限更高。

    ⇒ 上限不是拍脑袋的第二个数字,而是同一个 DSR 判据的两个面。
    """
    loose = GateThresholds(min_dsr=0.99)
    assert suspicious_sharpe_cap(500, 600, loose) > suspicious_sharpe_cap(500, 600)


def test_the_cap_is_the_dsr_certification_line_not_a_second_magic_number():
    """上限就是 **DSR 认证线** 本身:正态收益假设下,认证线处的 DSR 恰好等于阈值。

    这条把「上限」与「DSR 阈值」钉在**同一个统计模型**上:
    上限不是第二个拍脑袋的数字,而是 DSR 判据的另一面(闭式解与 DSR 公式自洽)。
    """
    from statistics import NormalDist
    from risk.dsr import expected_max_sharpe, required_sharpe_for_dsr

    for n_trials, n_obs in ((1, 240), (10, 320), (50, 320), (1000, 600), (5000, 480)):
        sr = required_sharpe_for_dsr(n_trials, n_obs, 0.95)
        denom = math.sqrt(1.0 + sr * sr / 2.0)          # 正态收益 ⇒ DSR 分母退化
        z = (sr - expected_max_sharpe(n_trials, n_obs)) * math.sqrt(n_obs - 1) / denom
        assert NormalDist().cdf(z) == pytest.approx(0.95, abs=1e-9), (n_trials, n_obs, z)


def test_the_cap_matches_the_measured_table_in_the_adr():
    """ADR 里那张实测表必须与代码一致(**数字漂移要红**)。"""
    assert suspicious_sharpe_cap(50, 320) == pytest.approx(3.4184, abs=1e-3)
    assert suspicious_sharpe_cap(50, 240) == pytest.approx(3.9560, abs=1e-3)
    assert suspicious_sharpe_cap(1000, 240) == pytest.approx(4.9518, abs=1e-3)
    assert suspicious_sharpe_cap(5000, 600) == pytest.approx(3.3878, abs=1e-3)
    assert suspicious_sharpe_cap(50, 480) == 3.0
    assert suspicious_sharpe_cap(50, 600) == 3.0


def test_the_relaxation_window_is_narrow_and_that_is_measured_not_assumed():
    """**诚实的量**:「放宽」真正打开的窗口很窄,这是**实测**结论,不是推测。

    窗口 = {夏普 ≤ 上限} ∩ {DSR ≥ 阈值}。DSR 阈值要求夏普不低于**同一条**认证线,
    所以窗口的上沿 = 认证线(正态假设),下沿 = 最乐观矩(分母 = 1)下的认证线。
    实测 (N=1000, T=600):上沿 3.1121,下沿 3.1013 ⇒ 窗口宽约 0.011 年化夏普。
    ⇒ 这条改动的实质影响很小(ADR 里写明),但方向是对的:上限不再与试验次数无关。
    """
    from statistics import NormalDist
    from risk.dsr import expected_max_sharpe

    n_trials, n_obs = 1000, 600
    upper = suspicious_sharpe_cap(n_trials, n_obs)
    lower = (expected_max_sharpe(n_trials, n_obs)
             + NormalDist().inv_cdf(0.95) / math.sqrt(n_obs - 1)) * math.sqrt(240)
    assert lower < upper, (lower, upper)
    assert upper - lower == pytest.approx(0.0108, abs=2e-3)


def test_the_relaxation_probe_has_discriminating_power(monkeypatch):
    """**判别力**:把 DSR 推导摘掉(回到固定 3.0)⇒ 放行侧那只候选必须被重新拒绝。

    没有这一步,「放行侧变绿」可能只是**碰巧**(例如门槛坏了、把候选一律放行)。
    """
    import risk.promotion_gate as pg

    monkeypatch.setattr(pg, "required_sharpe_for_dsr", lambda *a, **k: 0.0)
    fails = check_gate(RELAXED)
    assert any("前视偏差" in f for f in fails), fails


def test_the_cap_stays_the_floor_when_the_derivation_is_removed(monkeypatch):
    """反过来:推导摘掉 ⇒ 上限**不许**低于 3.0(不许借「推导坏了」之名收紧)。"""
    import risk.promotion_gate as pg

    monkeypatch.setattr(pg, "required_sharpe_for_dsr", lambda *a, **k: 0.0)
    assert suspicious_sharpe_cap(500, 600) == 3.0


def test_the_cap_stays_the_floor_when_the_derivation_returns_a_nonsense_value(monkeypatch):
    """推导返回非有限值(将来改了公式)⇒ 落回兜底,**不许**变成 inf 上限。"""
    import risk.promotion_gate as pg

    monkeypatch.setattr(pg, "required_sharpe_for_dsr", lambda *a, **k: 1e308)
    assert suspicious_sharpe_cap(500, 600) == 3.0


def test_the_dsr_threshold_is_still_locked_at_0_95_by_the_new_rule():
    """**DSR ≥ 0.95 没有被顺带放宽**:放宽的只是「可疑」那条。"""
    assert GateThresholds().min_dsr == 0.95
    fails = check_gate(replace(RELAXED, dsr=0.949))
    assert any("DSR" in f for f in fails)

