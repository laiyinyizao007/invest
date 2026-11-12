"""risk/risk_guard.py 单元测试(纯 Python,不依赖 vn.py)。"""
import sys
from datetime import date
from pathlib import Path

import pytest


from risk.risk_guard import (  # noqa: E402
    DailyLossGuard,
    TrailingStop,
    risk_per_trade,
    stop_distance,
    within_budget,
)


# ---------- 预算 ----------

def test_stop_distance_and_risk_per_trade():
    assert stop_distance(20, 1.5) == 30
    assert risk_per_trade(30, 10) == 300          # 30 点 × 10 吨 = 300 元
    assert risk_per_trade(30, 10, volume=2) == 600


@pytest.mark.parametrize("dist, expected", [(29.9, True), (30.0, True), (30.1, False)])
def test_within_budget_boundary(dist, expected):
    """恰好等于预算允许;超过一点点就不做。"""
    assert within_budget(dist, size=10, budget=300) is expected


# ---------- 移动止损 ----------

def test_long_initial_stop_and_ratchet_up():
    t = TrailingStop()
    assert t.open(1, 100, 10) == 90
    assert t.update(high=105, low=99, dist=10) == 95    # 最高价抬到 105 → 止损 95
    assert t.update(high=103, low=96, dist=10) == 95    # 最高价没创新高 → 止损不动


def test_short_initial_stop_and_ratchet_down():
    t = TrailingStop()
    assert t.open(-1, 100, 10) == 110
    assert t.update(high=101, low=95, dist=10) == 105
    assert t.update(high=104, low=97, dist=10) == 105


def test_stop_never_loosens_when_atr_grows():
    """纪律:止损永远不往亏损方向移动,即使波动变大(distance 变大)。"""
    t = TrailingStop()
    t.open(1, 100, 10)
    t.update(high=110, low=100, dist=10)                 # 止损 100
    assert t.update(high=110, low=100, dist=30) == 100   # 候选 80 更差,保持 100
    s = TrailingStop()
    s.open(-1, 100, 10)
    s.update(high=100, low=90, dist=10)                  # 止损 100
    assert s.update(high=100, low=90, dist=30) == 100


def test_trailing_stop_invalid_usage():
    t = TrailingStop()
    with pytest.raises(RuntimeError):
        t.update(1, 1, 1)                                # 未开仓
    with pytest.raises(ValueError):
        t.open(0, 100, 10)
    with pytest.raises(ValueError):
        t.open(1, 100, 0)


def test_reset_clears_state():
    t = TrailingStop()
    t.open(1, 100, 10)
    t.reset()
    assert (t.side, t.stop, t.extreme) == (0, 0.0, 0.0)


# ---------- 连亏熔断 ----------

def test_guard_blocks_after_consecutive_losses_same_day():
    g = DailyLossGuard(max_losses=2)
    d = date(2026, 11, 9)
    assert g.can_open(d)
    g.on_trade_closed(d, -100)
    assert g.can_open(d)
    g.on_trade_closed(d, -50)
    assert not g.can_open(d)                              # 连亏 2 笔,当日停手


def test_guard_win_resets_streak():
    g = DailyLossGuard(max_losses=2)
    d = date(2026, 11, 9)
    g.on_trade_closed(d, -100)
    g.on_trade_closed(d, +200)
    g.on_trade_closed(d, -100)
    assert g.can_open(d)                                  # 中间赢过一笔,计数已清零


def test_guard_breakeven_counts_as_loss():
    g = DailyLossGuard(max_losses=1)
    d = date(2026, 11, 9)
    g.on_trade_closed(d, 0)
    assert not g.can_open(d)


def test_guard_resets_next_day():
    g = DailyLossGuard(max_losses=1)
    g.on_trade_closed(date(2026, 11, 9), -100)
    assert not g.can_open(date(2026, 11, 9))
    assert g.can_open(date(2026, 11, 10))


def test_guard_rejects_invalid_limit():
    with pytest.raises(ValueError):
        DailyLossGuard(max_losses=0)
