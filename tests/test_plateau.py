"""参数高原判定:晋级门槛第 8 条 `parameter_on_plateau` 的计算来源。"""
import itertools

from risk.plateau import NEG_INF, neighborhood, parameter_on_plateau


def grid_scores(fn, size=5):
    return {c: fn(c) for c in itertools.product(range(size), repeat=3)}


def test_flat_positive_region_is_a_plateau():
    scores = grid_scores(lambda c: 1.0)

    assert parameter_on_plateau(scores, (2, 2, 2)) is True


def test_isolated_spike_in_a_losing_region_is_not_a_plateau():
    scores = grid_scores(lambda c: 5.0 if c == (2, 2, 2) else -0.5)

    assert parameter_on_plateau(scores, (2, 2, 2)) is False


def test_one_huge_outlier_cannot_drag_the_mean_positive():
    # 邻域均值为正,但只有 1 个格子赚钱,其余全亏:不是高原
    scores = grid_scores(lambda c: 40.0 if c == (2, 2, 2) else -0.5)
    assert sum(scores[c] for c in neighborhood((2, 2, 2))) / 27 > 0

    assert parameter_on_plateau(scores, (2, 2, 2)) is False


def test_incomplete_neighbourhood_is_not_a_plateau():
    scores = grid_scores(lambda c: 1.0)

    assert parameter_on_plateau(scores, (0, 2, 2)) is False        # 网格边界
    del scores[(1, 2, 2)]
    assert parameter_on_plateau(scores, (2, 2, 2)) is False        # 邻居缺失(过滤出的空洞)


def test_invalid_neighbours_count_at_pessimistic_floor():
    scores = grid_scores(lambda c: 1.0)
    for c in list(neighborhood((2, 2, 2)))[:20]:
        if c != (2, 2, 2):
            scores[c] = NEG_INF

    assert parameter_on_plateau(scores, (2, 2, 2)) is False


def test_invalid_own_cell_is_not_a_plateau_and_unknown_cell_too():
    scores = grid_scores(lambda c: 1.0)
    scores[(2, 2, 2)] = NEG_INF

    assert parameter_on_plateau(scores, (2, 2, 2)) is False
    assert parameter_on_plateau(scores, (9, 9, 9)) is False


def test_feeds_the_promotion_gate_field():
    from risk.promotion_gate import CandidateMetrics, check_gate

    plateau = parameter_on_plateau(grid_scores(lambda c: -1.0), (2, 2, 2))
    m = CandidateMetrics(oos_sharpe=2.0, oos_years=5, max_drawdown_pct=5, max_drawdown_yuan=500, round_trips=200,
                         random_entry_percentile=99, stress_sharpe=1.5, data_adjusted=True, parameter_on_plateau=plateau,
                         dsr=0.99, n_trials=50)

    assert any("参数高原" in f for f in check_gate(m))


def test_smoothed_matches_optimize_smoothed_on_the_same_scores():
    import optimize
    from risk.plateau import smoothed
    scores = {c: ((c[0] * 7 + c[1] * 3 + c[2]) % 5) - 1.0 for c in itertools.product(range(5), repeat=3)}
    scores[(2, 2, 2)] = NEG_INF
    scores[(1, 3, 2)] = NEG_INF

    assert smoothed(scores) == optimize.smoothed(scores)


def test_smoothed_skips_incomplete_neighbourhoods_and_invalid_cells():
    from risk.plateau import smoothed
    scores = {c: 1.0 for c in itertools.product(range(3), repeat=3)}

    assert set(smoothed(scores)) == {(1, 1, 1)}                         # 只有中心格的 27 邻域完整
    scores[(1, 1, 1)] = NEG_INF
    assert smoothed(scores) == {}
