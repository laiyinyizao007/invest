"""risk/promotion_gate.py 单元测试(纯 Python)。"""
import math
from dataclasses import replace

import pytest


from risk.promotion_gate import (  # noqa: E402
    MAX_OOS_YEARS,
    CandidateMetrics,
    GateThresholds,
    cap_for_metrics,
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


def test_the_normal_model_sub_window_is_narrow_but_is_not_the_whole_relaxation():
    """**正态模型内**的那一小段确实很窄(≈0.011),但它**不是**这次改动的全部效果。

    更正(codex 双轮复核):ADR 原先把这个 0.011 当成「放宽打开的窗口」,
    那是**错的** —— 真正的打开区间是 (旧上限 3.0, 新上限] ⇒ 宽约 0.1121。
    0.011 是「候选的收益接近正态时,DSR 自己也在 3.1013 处就顶到 0.95」的那一小段。
    这条探针把**两个数字**都钉住,免得文档再写错。
    """
    from statistics import NormalDist
    from risk.dsr import expected_max_sharpe

    n_trials, n_obs = 1000, 600
    upper = suspicious_sharpe_cap(n_trials, n_obs)
    lower = (expected_max_sharpe(n_trials, n_obs)
             + NormalDist().inv_cdf(0.95) / math.sqrt(n_obs - 1)) * math.sqrt(240)
    assert lower < upper, (lower, upper)
    assert upper - lower == pytest.approx(0.0108, abs=2e-3)   # 正态模型内的窄段
    assert upper - 3.0 == pytest.approx(0.1121, abs=1e-3)     # 实际打开的区间


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


# ---------- 第 55 轮之十七:codex 双轮复核后的更正与加固 ----------
#
# **来历**:第 55 轮之十六那版把「放宽打开的窗口」写成「宽约 0.011」,并且
# 在 docstring/ADR 里写了「正态假设偏保守(更严)的一侧 ⇒ 安全」。
# codex 双轮复核(第 55 轮之十七)指出这两句都**只在正态模型内成立**:
# - 实际 DSR 用**样本自己的**偏度/峰度(`denom = 1 − 偏度·sr + (峰度−1)·sr²/4`),
#   **正偏度会让分母 < 1** ⇒ 实测 DSR 更高 ⇒ 能过 DSR 的夏普**更低**
#   ⇒ 实际打开的区间比正态模型宽(下沿更低);
# - 而**这次改动**打开的区间是 (旧上限 3.0, 新上限] ⇒ 宽约 0.1121,不是 0.011。
# 本节把更正后的口径钉成探针(放行侧 + 拦截侧),并补上「输入非法值」的加固。


def _right_skewed_returns(n_obs=600, p=0.018, sd=0.01, sr_daily=0.19685):
    """两点分布:小概率(1.8%)取一个高值、其余取低值 ⇒ 强右偏(skew ≈ 7.2)。"""
    mean = sr_daily * sd
    gap = sd / math.sqrt(p * (1 - p))
    high = mean + (1 - p) * gap
    low = high - gap
    n_high = round(p * n_obs)
    return [high] * n_high + [low] * (n_obs - n_high)


def test_the_newly_opened_band_is_measured_from_the_old_constant():
    """**更正后的口径**:这次改动打开的区间 = (旧上限 3.0, 新上限] ⇒ 宽约 0.1121。

    「宽约 0.011」是**正态模型内**的那一小段,不是这次改动的实际效果。
    """
    upper = suspicious_sharpe_cap(1000, 600)
    assert upper == pytest.approx(3.1121, abs=1e-3)
    assert upper - 3.0 == pytest.approx(0.1121, abs=1e-3)


def test_a_right_skewed_candidate_can_land_in_the_newly_opened_band():
    """**放行侧**:右偏的候选能落进 (3.0, 3.1121] 这段新打开的区间 ⇒ 通过全部门槛。

    这条把「0.011 不是全局结论」钉成事实:实测这条序列年化夏普 **3.0582**、
    实测 DSR **0.99999997**(偏度 7.16 ⇒ DSR 分母被压到 0.0877)⇒
    旧规则(固定 3.0)会毙掉它,新规则放行。
    """
    from risk.dsr import deflated_sharpe, moments

    rets = _right_skewed_returns()
    mean, sd, skew, _kurt = moments(rets)
    annual = mean / sd * math.sqrt(240)
    dsr_value = deflated_sharpe(rets, n_trials=1000)
    assert annual == pytest.approx(3.0582, abs=1e-3)
    assert 3.0 < annual <= suspicious_sharpe_cap(1000, 600)
    assert dsr_value >= 0.95
    candidate = replace(GOOD, oos_sharpe=annual, oos_years=600 / 240,
                        n_trials=1000, dsr=dsr_value)
    assert check_gate(candidate) == []


def test_the_same_sharpe_with_normal_moments_fails_the_dsr_gate():
    """**拦截侧**:同样的年化夏普,若矩接近正态 ⇒ DSR 顶不到 0.95 ⇒ 被毙。

    ⇒ 「能不能落进新打开的区间」取决于**候选自己的矩**,不是这条改动的常量。
    (也说明:DSR 不是前视检测器 —— 它按矩去通胀,不识别前视。)
    """
    from statistics import NormalDist
    from risk.dsr import expected_max_sharpe

    annual = 3.0582
    sr = annual / math.sqrt(240)
    denom = math.sqrt(1.0 + sr * sr / 2.0)          # 正态收益 ⇒ DSR 分母退化
    z = (sr - expected_max_sharpe(1000, 600)) * math.sqrt(599) / denom
    normal_dsr = NormalDist().cdf(z)
    assert normal_dsr < 0.95, normal_dsr
    fails = check_gate(replace(GOOD, oos_sharpe=annual, oos_years=600 / 240,
                               n_trials=1000, dsr=normal_dsr))
    assert any("DSR" in f for f in fails), fails


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "2.0", None])
def test_a_non_finite_or_non_numeric_sharpe_fails_closed(bad):
    """非法样本外夏普 ⇒ **不予通过**(而不是被 NaN 的比较静默放过)。"""
    fails = check_gate(replace(GOOD, oos_sharpe=bad))
    assert any("样本外夏普" in f and "非法" in f for f in fails), (bad, fails)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "5.0", None])
def test_a_non_finite_or_non_numeric_sample_length_fails_closed(bad):
    """非法样本外年数 ⇒ **不予通过**。"""
    fails = check_gate(replace(GOOD, oos_years=bad))
    assert any("样本外年数" in f and "非法" in f for f in fails), (bad, fails)


@pytest.mark.parametrize("field", ["max_drawdown_pct", "max_drawdown_yuan",
                                   "round_trips", "random_entry_percentile",
                                   "stress_sharpe"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_the_other_metric_fields_do_not_silently_pass_nan(field, bad):
    """**其余指标字段**同样不许被 NaN/inf 静默放过(逐个查,不是只查夏普)。

    只查夏普是**半条规则**:回撤、往返笔数、随机分位、成本翻倍夏普的比较
    在 NaN 下同样恒为 False ⇒ 都会变成「通过」。
    """
    fails = check_gate(replace(GOOD, **{field: bad}))
    assert any("非法" in f for f in fails), (field, bad, fails)


@pytest.mark.parametrize("field", ["data_adjusted", "parameter_on_plateau"])
@pytest.mark.parametrize("bad", ["False", "yes", 2, None])
def test_a_boolean_field_given_a_non_boolean_is_rejected(field, bad):
    """布尔字段只认 bool / 0 / 1;字符串 "False" 在 Python 里是**真** ⇒ 必须拦。"""
    fails = check_gate(replace(GOOD, **{field: bad}))
    assert any("非法" in f for f in fails), (field, bad, fails)


@pytest.mark.parametrize("bad_floor", [float("nan"), float("inf"), float("-inf"),
                                       -1.0, 0.0, "3.0", None])
def test_an_illegal_floor_falls_back_to_the_constant(bad_floor):
    """兜底下限被传成非法值 ⇒ 退回常量 3.0,**不许**让上限变成 NaN。

    NaN 的危害:上限变 NaN ⇒ 「夏普 > 上限」恒为 False ⇒ 「高得可疑」这条
    **永远不触发** ⇒ 防线静默失效(fail-open)。
    """
    thresholds = GateThresholds(suspicious_sharpe_floor=bad_floor)
    cap = suspicious_sharpe_cap(1000, 600, thresholds)
    assert math.isfinite(cap), bad_floor
    assert cap == pytest.approx(3.1121, abs=1e-3), bad_floor


def test_an_absurd_sample_length_does_not_crash_the_gate():
    """样本外年数大到折算溢出(1e308 年)⇒ **不抛异常**,并且被明确拒掉。

    旧实现会抛 OverflowError(inf → int)⇒ 整个评估流程炸掉,而不是 fail-closed。
    """
    candidate = replace(GOOD, oos_years=1e308)
    assert cap_for_metrics(candidate) == 3.0
    fails = check_gate(candidate)
    assert any("不合理" in f for f in fails), fails


def test_the_gate_never_raises_on_hostile_metrics():
    """一批怪输入 ⇒ 门槛**既不抛异常**,也**不放行**(fail-closed)。"""
    hostile = [
        {"oos_sharpe": float("nan")},
        {"oos_sharpe": "2.0"},
        {"oos_years": float("inf")},
        {"oos_years": 1e308},
        {"oos_years": -3.0},
        {"max_drawdown_pct": float("nan")},
        {"max_drawdown_yuan": float("inf")},
        {"round_trips": float("nan")},
        {"random_entry_percentile": float("nan")},
        {"stress_sharpe": float("nan")},
        {"data_adjusted": "False"},
        {"parameter_on_plateau": "False"},
        {"dsr": float("nan")},
        {"n_trials": 0},
    ]
    for change in hostile:
        fails = check_gate(replace(GOOD, **change))
        assert fails, change           # 不许出现「静默全过」


@pytest.mark.parametrize("field,change", [
    ("min_oos_sharpe", {"oos_sharpe": 1.2}),
    ("max_drawdown_pct", {"max_drawdown_pct": -25.0}),
    ("max_drawdown_yuan", {"max_drawdown_yuan": -2500}),
    ("min_round_trips", {"round_trips": 80}),
    ("min_random_percentile", {"random_entry_percentile": 80.0}),
    ("min_stress_sharpe", {"stress_sharpe": 0.4}),
    ("min_sharpe_ci_lower", {"oos_years": 2.0}),
    ("min_dsr", {"dsr": 0.80}),
])
def test_a_non_finite_threshold_still_rejects_what_the_default_rejects(field, change):
    """**阈值本身**是 NaN ⇒ 退回该字段的默认值 ⇒ **该拦的仍然拦得住**。

    NaN 阈值的危害和 NaN 指标一样:「夏普 < NaN」恒为 False ⇒ 下限那条
    **永远不触发** ⇒ 等于把这一关悄悄关掉。
    """
    thresholds = GateThresholds(**{field: float("nan")})
    candidate = replace(GOOD, **change)
    fails = check_gate(candidate, thresholds)
    assert fails, (field, change)               # 默认阈值会拒 ⇒ 退回默认后也必须拒
    assert fails == check_gate(candidate), (field, change)


# ---------- 第 55 轮之十八:外部复核追问的两点,补成探针 ----------

def test_the_cap_stays_the_floor_when_the_derivation_returns_nan(monkeypatch):
    """认证线解出 **NaN** ⇒ 上限必须仍是兜底 3.0(不许变成 NaN)。

    为什么单独钉这一条:Python 的 `max(a, b)` 在 NaN 上**依赖参数顺序**
    (`max(3.0, nan)` → 3.0,`max(nan, 3.0)` → nan)⇒ 若哪天把 `max(floor, x)`
    写成 `max(x, floor)`,上限就会变成 NaN ⇒ 「夏普 > 上限」恒为 False
    ⇒ 「高得可疑」这条防线**静默失效**。
    """
    import risk.promotion_gate as pg

    monkeypatch.setattr(pg, "required_sharpe_for_dsr", lambda *a, **k: float("nan"))
    cap = suspicious_sharpe_cap(500, 600)
    assert math.isfinite(cap)
    assert cap == 3.0


def test_a_sample_length_above_the_physical_limit_only_tightens_the_gate():
    """样本外年数**超过物理上界**(`MAX_OOS_YEARS`)⇒ 上限落回兜底 3.0 且被明确拒掉。

    这一条钉住「新增的年数上界只会更严」:年数学不出时上限**不升反降**
    (3.0 < 该 (N,T) 下的认证线)⇒ 不会因为多写一个 1e308 就换到更大的放行区间。
    """
    candidate = replace(GOOD, oos_years=MAX_OOS_YEARS + 10.0)
    assert cap_for_metrics(candidate) == 3.0
    # 对照组:同一批试验次数(N=1000)、合法样本外长度 ⇒ 上限才是 3.1121
    assert cap_for_metrics(candidate) < cap_for_metrics(
        replace(GOOD, oos_years=2.5, n_trials=1000))
    fails = check_gate(candidate)
    assert any("不合理" in f for f in fails), fails


@pytest.mark.parametrize("n_trials,n_obs", [(1, 3), (2, 10), (1000, 600),
                                             (10 ** 6, 3), (10 ** 6, 10 ** 6),
                                             (500, 3)])
def test_the_cap_is_always_finite_and_never_below_the_floor(n_trials, n_obs):
    """极端 (N, T) 组合 ⇒ 上限恒为有限数且 **≥ 3.0**。

    这是「只放宽、不收紧」的**穷举式**钉法:任何 (N, T) 下上限都不得低于
    旧常量 3.0(否则旧规则放行的候选会被新规则毙掉)。
    """
    cap = suspicious_sharpe_cap(n_trials, n_obs)
    assert math.isfinite(cap), (n_trials, n_obs)
    assert cap >= 3.0, (n_trials, n_obs, cap)

