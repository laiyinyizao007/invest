"""梯子步长 η 的标定脚本(阶段 3 前置:η 的数值必须登记在第一份候选生成之前)。

这里只测**纯逻辑**(自助估计、参数对构造),不依赖仓库之外的数据文件——
数据相关的部分(`--data`)是标定时的输入,不是逻辑。
"""
import random
from datetime import date, timedelta

import calc_ladder_eta as eta


def _days(n: int, start: date = date(2022, 7, 1)):
    return [start + timedelta(days=i) for i in range(n)]


def _series(seed: int, n: int, scale: float = 1000.0) -> dict:
    rng = random.Random(seed)
    return {d: rng.gauss(0.0, scale) for d in _days(n)}


def test_the_bootstrap_estimate_is_positive_and_matches_the_analytic_one_in_order():
    """自助 SE 与解析式 SE 应当同量级:两者算的是同一个量,只是算法不同。

    解析式算错量纲时(元 vs 收益率)两者会差好几个数量级,这条用例能抓住那种错。"""
    a = _series(1, 300)
    b = _series(2, 300)
    boot, analytic = eta.paired_bootstrap_se(a, b, 100000.0, 400, 5, 7)
    assert boot > 0 and analytic > 0
    assert 0.1 < boot / analytic < 10


def test_the_standard_error_shrinks_as_the_sample_grows():
    """样本翻几倍,SE 应当按 1/√n 缩小——这是"步长必须随窗口样本量变化"的根据。"""
    _, se_small = eta.paired_bootstrap_se(_series(3, 120), _series(4, 120), 100000.0, 400, 5, 7)
    _, se_big = eta.paired_bootstrap_se(_series(3, 480), _series(4, 480), 100000.0, 400, 5, 7)
    assert se_big < se_small


def test_a_pair_that_trades_nothing_does_not_crash_the_estimator():
    """全零盈亏(没有成交)不能让标定崩掉:夏普是 0,SE 也是 0。"""
    a = {d: 0.0 for d in _days(60)}
    b = {d: 0.0 for d in _days(60)}
    boot, analytic = eta.paired_bootstrap_se(a, b, 100000.0, 50, 5, 7)
    assert boot == 0.0 and analytic == 0.0


def test_the_pairs_are_adjacent_steps_that_change_exactly_one_parameter():
    """参数对必须是"只动一个参数、且只动一步"——这正是变异能造出的最小差异。"""
    pairs = eta.adjacent_pairs()
    assert pairs
    for left, right in pairs:
        assert set(left) == set(right) == {"fast_window", "slow_window"}
        changed = [k for k in left if left[k] != right[k]]
        assert len(changed) == 1, f"{left} vs {right} 动了不止一个参数"
        step = abs(left[changed[0]] - right[changed[0]])
        assert step in (4, 10), f"{changed[0]} 的步长 {step} 不是网格里的一步"


def test_the_calibration_uses_the_project_baseline_parameters():
    """标定用的必须是项目基准策略自己的参数(尤其风控底线),不能另设一套。

    风控底线是受保护项:标定阶段也不许动它,否则算出来的 η 对应的是另一个策略。"""
    setting = eta.setting_of({"fast_window": 10, "slow_window": 40})
    assert setting["risk_budget"] == 300.0
    assert setting["atr_window"] == 14
    assert setting["atr_mult"] == eta.ATR_MULT
    assert setting["fast_window"] == 10 and setting["slow_window"] == 40


def test_the_floor_is_applied_before_the_factor():
    """下限先于系数生效:噪声极小的时候 η 至少是 0.25,不能因为乘了 1.5 反而更小。"""
    assert max(eta.ETA_FLOOR, eta.ETA_FACTOR * 0.01) == eta.ETA_FLOOR
    assert max(eta.ETA_FLOOR, eta.ETA_FACTOR * 0.73) > eta.ETA_FLOOR
