"""纪律机制补缺的测试:解锁问责(冷却/历史/向后兼容)、非策略委托检测。
见 docs/adr/20261111-discipline-enforcement.md。"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import unlock_account  # noqa: E402
from risk.account_guard import UNLOCK_HISTORY_LIMIT, AccountGuard, AccountLimits  # noqa: E402
from test_guard_integration import Env, make_req  # noqa: E402,F401
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.object import OrderData  # noqa: E402

MON_10AM = datetime(2026, 11, 9, 10, 0)
MON_11AM = datetime(2026, 11, 9, 11, 0)
TUE_10AM = datetime(2026, 11, 10, 10, 0)


def hard_locked_guard(path: Path) -> AccountGuard:
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, MON_10AM)
    g.update(7_900, MON_11AM)
    assert g.hard_locked
    return g


# ---------- 解锁问责 ----------

def test_hard_lock_records_trigger_time_and_survives_restart(tmp_path):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    assert AccountGuard(AccountLimits(), path).hard_locked_at == "2026-11-09T11:00:00"


def test_cooldown_blocks_same_trading_day_and_allows_next_day(tmp_path):
    g = hard_locked_guard(tmp_path / "g.json")
    assert "下一个交易日" in g.unlock_cooldown(datetime(2026, 11, 9, 16, 0))
    assert g.unlock_cooldown(TUE_10AM) == ""


def test_night_session_lock_belongs_to_next_trading_day(tmp_path):
    g = AccountGuard(AccountLimits(600, 2000), tmp_path / "g.json")
    g.update(10_000, datetime(2026, 11, 9, 21, 0))
    g.update(7_900, datetime(2026, 11, 9, 21, 30))             # 周一夜盘 → 归属周二交易日
    assert g.unlock_cooldown(datetime(2026, 11, 10, 9, 30)) != ""   # 周二白天仍在同一交易日内
    assert g.unlock_cooldown(datetime(2026, 11, 11, 9, 30)) == ""


def test_unlock_appends_history_and_caps_it(tmp_path):
    path = tmp_path / "g.json"
    g = hard_locked_guard(path)
    g.unlock_hard(TUE_10AM, forced=True)
    entry = AccountGuard(AccountLimits(), path).unlock_history[-1]
    assert entry["forced"] is True and entry["locked_at"] == "2026-11-09T11:00:00" and "回撤" in entry["reason"]
    for _ in range(UNLOCK_HISTORY_LIMIT + 5):
        g.hard_locked = True
        g.unlock_hard(TUE_10AM)
    assert len(g.unlock_history) == UNLOCK_HISTORY_LIMIT


def test_old_state_file_without_new_fields_still_loads(tmp_path):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    state = json.loads(path.read_text(encoding="utf-8"))
    for key in ("hard_locked_at", "unlock_history", "manual_orders"):
        state.pop(key)
    path.write_text(json.dumps(state), encoding="utf-8")
    g = AccountGuard(AccountLimits(), path)
    assert g.hard_locked and g.hard_locked_at == "" and g.unlock_history == [] and g.manual_orders == {}
    assert "触发时间未知" in g.unlock_cooldown(MON_11AM)      # 无法证明已过冷却 → 按冷却期内处理,必须 --force


def test_failed_unlock_write_rolls_back_memory_state(tmp_path, monkeypatch):
    path = tmp_path / "g.json"
    g = hard_locked_guard(path)
    monkeypatch.setattr(AccountGuard, "_save", lambda self: setattr(self, "save_error", "disk full"))
    g.unlock_hard(TUE_10AM)
    assert g.hard_locked and g.hard_reason and g.hard_locked_at and g.peak_equity > 0 and g.unlock_history == []


def test_unlock_script_refuses_inside_cooldown(tmp_path, capsys):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    assert unlock_account.unlock(path, assume_yes=True, now=datetime(2026, 11, 9, 16, 0)) == unlock_account.EXIT_COOLDOWN
    assert AccountGuard(AccountLimits(), path).hard_locked
    assert "冷却期内拒绝解锁" in capsys.readouterr().err


def test_force_requires_typed_force_and_is_recorded(tmp_path):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    now = datetime(2026, 11, 9, 16, 0)
    assert unlock_account.unlock(path, confirm=lambda p: "yes", force=True, now=now) == 1
    answers = iter(["FORCE", "UNLOCK"])
    assert unlock_account.unlock(path, confirm=lambda p: next(answers), force=True, now=now) == 0
    g = AccountGuard(AccountLimits(), path)
    assert not g.hard_locked and g.unlock_history[-1]["forced"] is True


def test_force_cannot_be_combined_with_yes(tmp_path):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    code = unlock_account.unlock(path, assume_yes=True, force=True, now=datetime(2026, 11, 9, 16, 0))
    assert code == unlock_account.EXIT_COOLDOWN and AccountGuard(AccountLimits(), path).hard_locked


def test_unlock_shows_previous_unlocks(tmp_path, capsys):
    path = tmp_path / "g.json"
    g = hard_locked_guard(path)
    g.unlock_hard(TUE_10AM)
    g2 = AccountGuard(AccountLimits(600, 2000), path)
    g2.update(10_000, datetime(2026, 11, 10, 11, 0))
    g2.update(7_900, datetime(2026, 11, 10, 12, 0))
    assert unlock_account.unlock(path, assume_yes=True, now=datetime(2026, 11, 11, 10, 0)) == 0
    assert "历次解锁" in capsys.readouterr().out


# ---------- 非策略委托检测 ----------

def foreign_order(oid="1_1_9", reference="", status=Status.NOTTRADED) -> OrderData:
    return OrderData(gateway_name="FAKE", symbol="c2601", exchange=Exchange.DCE, orderid=oid,
                     direction=Direction.LONG, offset=Offset.OPEN, price=2200.0, volume=1,
                     type=OrderType.LIMIT, status=status, reference=reference)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.tick()
    yield e
    e.main.close()


def test_unarmed_rule_ignores_foreign_orders(env):
    env.gw.on_order(foreign_order())
    env.tick(5)
    assert "检测到非策略委托" not in env.alert_titles()


def test_armed_rule_alerts_and_counts_but_does_not_lock(env):
    env.rule.arm_manual_detection()
    env.gw.on_order(foreign_order())
    env.tick(1)
    assert "检测到非策略委托" not in env.alert_titles()          # 还在确认窗口内
    env.tick(3)
    assert env.alert_titles().count("检测到非策略委托") == 1
    assert sum(env.rule.guard.manual_orders.values()) == 1
    assert env.rule.guard.can_open(env.clock.dt.date())          # 只告警计数,不锁定
    assert json.loads(env.state_file.read_text(encoding="utf-8"))["manual_orders"]


def test_strategy_and_flatten_references_are_not_foreign(env):
    env.rule.arm_manual_detection()
    env.gw.on_order(foreign_order("1_1_1", "CtaStrategy_trend"))
    env.gw.on_order(foreign_order("1_1_2", "account_guard_flatten"))
    env.tick(5)
    assert "检测到非策略委托" not in env.alert_titles()


def test_callback_arriving_before_send_event_is_not_misjudged(env):
    """CTP 回报线程可能抢在 send_order 的事件之前:先来 reference 为空的回报,随后带策略标记的事件到达。"""
    env.rule.arm_manual_detection()
    env.gw.on_order(foreign_order("1_1_5", ""))
    env.tick(1)
    env.gw.on_order(foreign_order("1_1_5", "CtaStrategy_trend", status=Status.SUBMITTING))
    env.tick(5)
    assert "检测到非策略委托" not in env.alert_titles()


def test_replayed_order_is_counted_once(env):
    env.rule.arm_manual_detection()
    for _ in range(3):
        env.gw.on_order(foreign_order("1_1_7"))
    env.tick(5)
    env.gw.on_order(foreign_order("1_1_7"))                      # 断线重连后的回放
    env.tick(5)
    assert sum(env.rule.guard.manual_orders.values()) == 1


def test_orders_seen_before_arming_are_not_counted_later(env):
    """启动期 CTP 回放的当日历史委托(reference 为空)在 arm 之前就见过,之后再来更新也不算人工干预。"""
    env.gw.on_order(foreign_order("1_1_8"))
    env.rule.arm_manual_detection()
    env.gw.on_order(foreign_order("1_1_8", status=Status.CANCELLED))
    env.tick(5)
    assert "检测到非策略委托" not in env.alert_titles()


def test_unknown_trigger_time_cannot_be_unlocked_without_force(tmp_path, capsys):
    """损坏/旧状态文件不能成为绕过冷却的办法。"""
    path = tmp_path / "g.json"
    path.write_text("{truncated", encoding="utf-8")
    assert AccountGuard(AccountLimits(), path).hard_locked
    assert unlock_account.unlock(path, assume_yes=True, now=TUE_10AM) == unlock_account.EXIT_COOLDOWN
    assert "触发时间未知" in capsys.readouterr().err


def test_unlock_refused_while_runtime_heartbeat_is_fresh(tmp_path, capsys):
    path = tmp_path / "g.json"
    hard_locked_guard(path)
    hb = tmp_path / "heartbeat"
    hb.write_text("x", encoding="utf-8")
    code = unlock_account.unlock(path, assume_yes=True, now=TUE_10AM, heartbeat_file=hb)
    assert code == unlock_account.EXIT_RUNNING and AccountGuard(AccountLimits(), path).hard_locked
    assert "请先停止进程" in capsys.readouterr().err
    old = hb.stat().st_mtime - 3600
    os.utime(hb, (old, old))                                   # 心跳已陈旧:进程已停
    assert unlock_account.unlock(path, assume_yes=True, now=TUE_10AM, heartbeat_file=hb) == 0


def test_closed_stdin_counts_as_not_confirmed(tmp_path):
    path = tmp_path / "g.json"
    hard_locked_guard(path)

    def eof(prompt):
        raise EOFError

    assert unlock_account.unlock(path, confirm=eof, now=TUE_10AM) == 1
    assert AccountGuard(AccountLimits(), path).hard_locked


def test_counted_manual_order_is_not_counted_again_after_seen_eviction(env, monkeypatch):
    import trading.guard_rule as gr
    monkeypatch.setattr(gr, "SEEN_ORDERS_LIMIT", 4)
    env.rule.arm_manual_detection()
    env.gw.on_order(foreign_order("1_1_100"))
    env.tick(5)
    for i in range(10):                                          # 把 1_1_100 从 seen 里挤出去
        env.gw.on_order(foreign_order(f"1_1_2{i}", "CtaStrategy_x"))
    env.gw.on_order(foreign_order("1_1_100"))                    # 断线重连后的回放
    env.tick(5)
    assert sum(env.rule.guard.manual_orders.values()) == 1
