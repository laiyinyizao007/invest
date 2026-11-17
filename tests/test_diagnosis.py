"""确定性诊断书:回撤 / 次数 / 连亏 / 夏普四维,相同输入恒得相同输出。"""
import json

import pytest

from risk.diagnosis import DiagnosisThresholds, diagnose, max_losing_streak

GOOD = {"sharpe": 2.0, "maxdd_pct": -5.0, "maxdd_yuan": -500.0, "round_trips": 200}


def levels(d):
    return {k: v["level"] for k, v in d["dimensions"].items()}


def test_all_ok_when_every_dimension_is_within_limits():
    d = diagnose(GOOD, [1, -1, 1, 1, -1])

    assert levels(d) == {"drawdown": "ok", "trades": "ok", "losing_streak": "ok", "sharpe": "ok"}
    assert d["weakest"] is None


@pytest.mark.parametrize("pnls, expected", [([], 0), ([1, 1], 0), ([-1, -1, 1, -1], 2), ([0, 0, -1, 5], 3), ([-1, -1, -1], 3)])
def test_max_losing_streak_counts_flat_as_loss_like_risk_guard(pnls, expected):
    assert max_losing_streak(pnls) == expected


def test_drawdown_fails_on_either_limit_and_warns_near_it():
    assert levels(diagnose({**GOOD, "maxdd_pct": -25.0}))["drawdown"] == "fail"
    assert levels(diagnose({**GOOD, "maxdd_yuan": -2500.0}))["drawdown"] == "fail"
    assert levels(diagnose({**GOOD, "maxdd_pct": -15.0}))["drawdown"] == "warn"   # ≥70% 上限
    assert levels(diagnose({**GOOD, "maxdd_pct": 5.0}))["drawdown"] == "ok"        # 正负号都按绝对值


def test_trades_below_gate_minimum_warns_and_below_half_fails():
    assert levels(diagnose({**GOOD, "round_trips": 100}))["trades"] == "warn"
    assert levels(diagnose({**GOOD, "round_trips": 10}))["trades"] == "fail"


def test_losing_streak_levels_and_unknown_without_pnls():
    t = DiagnosisThresholds(max_losing_streak=4)
    assert levels(diagnose(GOOD, [-1] * 3, t))["losing_streak"] == "warn"
    assert levels(diagnose(GOOD, [-1] * 4, t))["losing_streak"] == "fail"
    assert levels(diagnose(GOOD, None))["losing_streak"] == "unknown"


def test_sharpe_levels_including_suspiciously_high():
    assert levels(diagnose({**GOOD, "sharpe": 1.0}))["sharpe"] == "warn"
    assert levels(diagnose({**GOOD, "sharpe": -0.5}))["sharpe"] == "fail"
    d = diagnose({**GOOD, "sharpe": 3.5})
    assert levels(d)["sharpe"] == "fail" and "前视" in d["dimensions"]["sharpe"]["note"]


def test_weakest_is_most_severe_then_first_in_fixed_order():
    d = diagnose({**GOOD, "round_trips": 10, "sharpe": 1.0, "maxdd_pct": -25.0}, [-1] * 20)

    assert d["weakest"] == "drawdown"                 # 全是 fail 时按固定顺序取第一个
    assert diagnose({**GOOD, "sharpe": 1.0})["weakest"] == "sharpe"


def test_output_is_deterministic_and_json_serialisable():
    a = diagnose(GOOD, [1, -1, -1])
    b = diagnose(GOOD, [1, -1, -1])

    assert a == b
    assert json.loads(json.dumps(a, ensure_ascii=False)) == a


def test_missing_fields_are_reported_not_guessed():
    d = diagnose({"sharpe": 2.0}, None)

    assert levels(d)["drawdown"] == "unknown" and levels(d)["trades"] == "unknown"
    assert d["weakest"] is None                      # unknown 不冒充问题,也不算通过
    assert "unknown" in d["summary"] or "缺失" in d["summary"]


# ---------- 第 55 轮之十六:诊断层的「可疑夏普」上限与晋级门槛同源 ----------

def test_the_diagnosis_cap_follows_the_trial_count():
    """诊断层用**同一份**上限口径:试验次数多时,夏普 3.05 不再被标成「可疑」。

    缺 `n_trials` / `oos_years` 时用兜底下限(旧口径)⇒ 诊断偏严 ⇒ 保守。
    """
    row = {**GOOD, "sharpe": 3.05, "n_trials": 1000, "oos_years": 2.5}
    d = diagnose(row)
    assert levels(d)["sharpe"] == "ok"                # 上限 3.1121 ⇒ 3.05 不算可疑

    # 同样的夏普,缺输入 ⇒ 落回兜底下限 3.0 ⇒ 判成「可疑」(偏严,保守)
    d_strict = diagnose({**GOOD, "sharpe": 3.05})
    assert levels(d_strict)["sharpe"] == "fail"
    assert d_strict["dimensions"]["sharpe"]["limit"] == 3.0


def test_the_diagnosis_reports_the_cap_it_used():
    """诊断书报出来的 limit 必须就是它**实际用的**那个上限(不是一个写死的 3.0)。"""
    d = diagnose({**GOOD, "sharpe": 3.5, "n_trials": 1000, "oos_years": 2.5})
    assert levels(d)["sharpe"] == "fail"             # 3.5 > 上限 3.1121
    assert d["dimensions"]["sharpe"]["limit"] == pytest.approx(3.1121, abs=1e-3)

    d_few = diagnose({**GOOD, "sharpe": 3.5, "n_trials": 50, "oos_years": 2.5})
    assert d_few["dimensions"]["sharpe"]["limit"] == 3.0


def test_the_diagnosis_reports_a_missing_sharpe_as_unknown():
    """缺 sharpe ⇒ unknown(不猜),而不是当成 0 或当成可疑。"""
    d = diagnose({"maxdd_pct": -5.0, "maxdd_yuan": -500.0, "round_trips": 200})
    assert levels(d)["sharpe"] == "unknown"
    assert d["dimensions"]["sharpe"]["value"] is None


# ---------- 第 55 轮之十七:诊断层与门槛层同款的 fail-closed ----------

@pytest.mark.parametrize("bad_years", [1e308, float("inf"), float("nan"), 10 ** 400])
def test_the_diagnosis_does_not_crash_on_an_absurd_sample_length(bad_years):
    """样本外年数荒谬(1e308 / inf / NaN / 10**400)⇒ 诊断**不抛异常**,上限落回兜底。

    修之前:`int(round(inf * 240))` 抛 OverflowError;`float(10**400)` 也抛
    ⇒ 诊断整个炸掉(而不是报「未知」)。
    """
    row = {**GOOD, "sharpe": 3.5, "n_trials": 1000, "oos_years": bad_years}
    d = diagnose(row)
    assert d["dimensions"]["sharpe"]["limit"] == 3.0


@pytest.mark.parametrize("bad_sharpe", [float("nan"), float("inf"), float("-inf"), "2.0"])
def test_the_diagnosis_reports_a_non_finite_sharpe_as_unknown(bad_sharpe):
    """非有限夏普 ⇒ 报 **unknown**,不许当成「没超限」(NaN 比较恒为 False)。"""
    d = diagnose({**GOOD, "sharpe": bad_sharpe})
    assert levels(d)["sharpe"] == "unknown", bad_sharpe
    assert d["dimensions"]["sharpe"]["value"] is None
