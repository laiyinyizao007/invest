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
