"""国信取证运行器的步骤测试:假网关 + 同步事件引擎 + **真实**风控规则(不联网、不连 CTP)。"""
import os
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

import broker_evidence as be  # noqa: E402
import broker_test as bt  # noqa: E402
from collect_reject_texts import Collector  # noqa: E402
from fakes import FakeGateway, SyncEventEngine  # noqa: E402
from trading.events import EVENT_GUARD_ALERT  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402
from vnpy.event import Event  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import (  # noqa: E402
    EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_TIMER, EVENT_TRADE,
)


class Env:
    """假网关命名为 CTP(取证运行器里网关名写死为 CTP),挂真实的 SafeRiskManagerApp 与 Collector。"""

    def __init__(self, tmp_path: Path, in_session: bool = True) -> None:
        self.event_engine = SyncEventEngine()
        self.main = MainEngine(self.event_engine)
        self.gw: FakeGateway = self.main.add_gateway(FakeGateway, "CTP")
        self.gw.td_api = SimpleNamespace(connect_status=True, login_status=True)
        self.risk = self.main.add_app(SafeRiskManagerApp)
        self.rule = self.risk.rules["账户风控"]
        self.state_file = tmp_path / "state" / "guard.json"
        self.rule.update_setting({"state_file": str(self.state_file)})
        self.clock_t = 1000.0
        self.rule.mono = lambda: self.clock_t
        self.collector = Collector()
        self.alerts: list[dict] = []
        self.trades: list = []
        for ev, fn in ((EVENT_LOG, self.collector.on_log), (EVENT_ORDER, self.collector.on_order),
                       (EVENT_TICK, self.collector.on_tick), (EVENT_CONTRACT, self.collector.on_contract),
                       (EVENT_POSITION, self.collector.on_position),
                       (EVENT_GUARD_ALERT, lambda e: self.alerts.append(e.data)), (EVENT_TRADE, lambda e: self.trades.append(e.data))):
            self.event_engine.register(ev, fn)
        self.gw.push_contract()
        self.gw.push_tick()
        self.gw.push_account(10_000)
        self.ctx = bt.Ctx(main_engine=self.main, risk_engine=self.risk, collector=self.collector, symbol="c2601.DCE",
                          gateway_name="CTP", alerts=self.alerts, trades=self.trades, state_dir=tmp_path / "state",
                          pump=self.pump, sleep=lambda s: None, in_session=in_session, position_wait=0.2)
        self.ctx.contract = self.collector.contract_of("c2601.DCE")
        self.ctx.tick = self.collector.tick_of("c2601.DCE")
        self.book = bt.Book()
        self.pump()

    def pump(self) -> None:
        self.clock_t += 1.0
        self.event_engine.put(Event(EVENT_TIMER))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.main.close()



def _profile_from_os():
    """让 main() 按**当前进程环境变量**解析配置(绕开真实的 .env 文件)。

    配置解析现在只走 ctp_tools.profile.load_profile,不再走 bootstrap.load_env。
    """
    from ctp_tools.profile import Profile

    return Profile(env=dict(os.environ), hard_conflicts=[], notes=[])

def item(env, name) -> be.Evidence:
    return env.book.items[name]


# ---------- P4.1 指令检查 ----------

def test_validity_checks_block_bad_contract_price_and_volume(env):
    bt.step_validity(env.ctx, env.book)
    ev = item(env, "P4.1")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["合约代码", "合约最小变动价位", "单笔委托最大手数"]
    assert all("本地风控已拒绝" in s.lines[1] for s in ev.shots)
    assert env.gw.sent == []                                   # 三个错误指令一个都没发往柜台


# ---------- P2.3 重复报单 / P3.1 阈值 / T6 ----------

def test_duplicate_detection_blocks_and_alerts_and_restores_limit(env):
    original = env.risk.rules["重复报单检查"].duplicate_order_limit
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["ok"] and item(env, "P2.3").status == be.PASS
    assert env.risk.rules["重复报单检查"].duplicate_order_limit == original
    assert any(a["title"] == "风控阈值触发" and "重复报单" in a["message"] for a in env.alerts)
    assert all(env.collector.order_of(oid) is None or not env.collector.order_of(oid).is_active() for oid in list(env.gw.orders))


def test_threshold_covers_all_three_metrics_in_session(env):
    daily = env.risk.rules["每日上限检查"]
    limits = (daily.total_order_limit, daily.total_cancel_limit)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.PASS, p31.summary
    assert [s.label for s in p31.shots] == ["报单总笔数", "撤单总笔数", "重复报单总笔数"]
    assert item(env, "T6").status == be.PASS
    assert (daily.total_order_limit, daily.total_cancel_limit) == limits         # 阈值已还原
    titles = [a["message"] for a in env.alerts if a["title"] == "风控阈值触发"]
    assert any("汇总委托笔数" in t for t in titles) and any("汇总撤单笔数" in t for t in titles)


def test_threshold_cancel_metric_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        dup = bt.step_duplicate(e.ctx, e.book)
        bt.step_threshold(e.ctx, e.book, dup)
        p31 = e.book.items["P3.1"]
        assert p31.status == be.SKIP and "交易时段" in p31.summary
        assert e.book.items["T6"].status == be.PASS                              # 报单阈值照样可演示
    finally:
        e.main.close()


# ---------- P1.3 稳定运行(模板 2.1)----------

def test_stability_sends_all_orders_records_metrics_and_cleans_up(env):
    daily = env.risk.rules["每日上限检查"]
    dup, active = env.risk.rules["重复报单检查"], env.risk.rules["活动委托检查"]
    before = (daily.total_order_limit, daily.total_cancel_limit, dup.duplicate_order_limit, active.active_order_limit,
              env.rule.cancel_lock_limit, env.rule.cancel_count)
    env.ctx.stability_interval = 0
    bt.step_stability(env.ctx, env.book, total=30)
    ev = item(env, "P1.3")
    assert ev.status == be.PASS, ev.summary
    assert ev.metrics["orders"] == 30 and ev.metrics["accepted"] == 30 and ev.metrics["rejected"] == 0
    assert [s.label for s in ev.shots] == ["30笔"]
    assert len(env.gw.sent) == 30                                   # 30 笔全部经风控放行到了柜台(超过活动委托上限 10 也没被拦)
    assert all(not o.is_active() for o in map(env.collector.order_of, env.gw.orders) if o is not None)    # 清场:不留挂单
    after = (daily.total_order_limit, daily.total_cancel_limit, dup.duplicate_order_limit, active.active_order_limit,
             env.rule.cancel_lock_limit, env.rule.cancel_count)
    assert after == before                                          # 临时抬高的上限与撤单锁定计数都已还原


def test_stability_fails_when_the_counter_rejects_orders_and_still_restores_limits(env):
    env.gw.mode = "reject"
    env.ctx.stability_interval = 0
    limit = env.risk.rules["活动委托检查"].active_order_limit
    bt.step_stability(env.ctx, env.book, total=5)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and ev.metrics["rejected"] == 5
    assert env.risk.rules["活动委托检查"].active_order_limit == limit


def test_stability_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        bt.step_stability(e.ctx, e.book)
        ev = e.book.items["P1.3"]
        assert ev.status == be.SKIP and "交易时段" in ev.summary and e.gw.sent == []
    finally:
        e.main.close()


def test_stability_send_exception_becomes_failed_evidence_and_restores_limits(env, monkeypatch):
    limit = env.risk.rules["每日上限检查"].total_order_limit
    monkeypatch.setattr(bt, "send", lambda ctx, req: (_ for _ in ()).throw(RuntimeError("boom")))
    bt.step_stability(env.ctx, env.book, total=3)                    # 不再把异常抛出去:否则整次运行的证据都丢了
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "boom" in ev.summary
    assert env.risk.rules["每日上限检查"].total_order_limit == limit


def test_stability_cleanup_exception_still_restores_limits_and_cancel_count(env, monkeypatch):
    daily, guard = env.risk.rules["每日上限检查"], env.rule
    before = (daily.total_order_limit, guard.cancel_lock_limit, guard.cancel_count)
    env.ctx.stability_interval = 0
    monkeypatch.setattr(bt, "cancel_active", lambda ctx, ids: (_ for _ in ()).throw(RuntimeError("cancel boom")))
    bt.step_stability(env.ctx, env.book, total=3)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "cancel boom" in ev.summary
    assert (daily.total_order_limit, guard.cancel_lock_limit, guard.cancel_count) == before


def test_stability_fails_when_orders_are_still_active_after_cleanup(env, monkeypatch):
    env.ctx.stability_interval = 0
    env.ctx.stability_wait = 0.0
    monkeypatch.setattr(bt, "cancel_active", lambda ctx, ids: None)           # 撤单没发出去:订单仍然挂着
    bt.step_stability(env.ctx, env.book, total=3)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "未清场 3" in ev.summary
    assert any("未确认撤销" in line for line in ev.shots[0].lines)


def test_final_safety_reports_orders_that_stay_active(env, monkeypatch):
    env.gw.mode = "accept"
    oid = bt.send(env.ctx, bt.make_req(env.ctx, price=bt.probe_price(env.ctx)))
    assert oid
    monkeypatch.setattr(bt, "FINAL_CANCEL_WAIT", 0.0)
    monkeypatch.setattr(env.main, "cancel_order", lambda *a, **k: None)        # 撤单请求被吞掉
    notes = bt.final_safety(env.ctx)
    assert any("⚠⚠" in n and "未确认撤销" in n for n in notes)


def test_stability_is_gated_by_flat_check_like_other_order_steps():
    assert "P1.3" in bt.ORDER_ITEMS


# ---------- P5.1 暂停交易 ----------

def test_pause_step_blocks_opens_allows_closes_and_restores(env):
    bt.step_pause(env.ctx, env.book)
    ev = item(env, "P5.1")
    assert ev.status == be.PASS, ev.summary
    text = "\n".join(ev.shots[0].lines)
    assert "暂停期间开仓委托:被拒绝" in text and "暂停期间平仓委托:放行" in text and "恢复后开仓委托:通过风控" in text
    assert not (env.state_file.parent / "pause_trading.flag").exists()


# ---------- P2.2 笔数统计 ----------

def test_counts_step_reads_rule_counters(env):
    env.gw.mode = "accept"
    bt.send(env.ctx, bt.make_req(env.ctx, price=2100.0))
    bt.step_counts(env.ctx, env.book)
    ev = item(env, "P2.2")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["报单笔数统计", "撤单笔数统计"]
    assert "汇总报单笔数 1" in ev.shots[0].lines[1]


def test_counts_step_is_skipped_when_nothing_was_sent(env):
    bt.step_counts(env.ctx, env.book)
    assert item(env, "P2.2").status == be.SKIP


# ---------- T3 / T5 / P1.2 / T4 ----------

def test_basic_trade_opens_closes_and_confirms_flat(env):
    env.gw.mode = "fill"
    env.gw.position_lag = False
    bt.step_basic_trade(env.ctx, env.book)
    assert item(env, "T3").status == be.PASS, item(env, "T3").summary
    assert item(env, "T5").status == be.PASS and "成交回报" in item(env, "T5").shots[0].lines[0]
    assert [s.label for s in item(env, "P1.2").shots] == ["开仓", "平仓"]
    assert [r.direction for r in env.gw.sent] == [Direction.LONG, Direction.SHORT]
    assert env.gw.sent[1].offset == Offset.CLOSE


def test_basic_trade_aborts_when_account_already_holds_a_position(env):
    env.gw.set_position(Direction.LONG, 1)
    bt.step_basic_trade(env.ctx, env.book)
    assert item(env, "T3").status == be.FAIL and "已中止开平仓演示" in item(env, "T3").summary
    assert env.gw.sent == []


def test_basic_trade_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        bt.step_basic_trade(e.ctx, e.book)
        assert {k: v.status for k, v in e.book.items.items()} == {"T3": be.SKIP, "T5": be.SKIP, "P1.2": be.SKIP}
        assert e.gw.sent == []
    finally:
        e.main.close()


def test_cancel_step_feeds_t4_and_the_p12_cancel_shot(env):
    env.gw.mode = "accept"
    bt.step_cancel(env.ctx, env.book)
    assert item(env, "T4").status == be.PASS and item(env, "P1.2").shots[0].label == "撤单"
    assert len(env.gw.cancel_requests) == 1


# ---------- P4.2 柜台错误 ----------

def test_counter_errors_bypass_local_risk_and_capture_broker_text(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        e.gw.mode = "reject"
        e.gw.reject_text = "CTP:资金不足"
        bt.step_counter_errors(e.ctx, e.book)
        ev = e.book.items["P4.2"]
        assert [s.label for s in ev.shots] == ["无足够资金开仓", "无仓位可平", "市场状态不允许"]
        assert ev.status == be.PASS, ev.summary
        assert "资金不足" in ev.shots[0].lines[2]
        assert len(e.gw.sent) == 3                                              # 直发,没有经过本地风控拦截
    finally:
        e.main.close()


def test_counter_errors_in_session_cannot_demo_market_state(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    bt.step_counter_errors(env.ctx, env.book)
    ev = item(env, "P4.2")
    assert ev.status == be.SKIP and "非交易时段补测" in ev.summary and "非交易时段" in ev.shots[2].lines[0]


def test_counter_errors_fail_when_broker_does_not_complain_about_funds(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:别的错误"
    bt.step_counter_errors(env.ctx, env.book)
    assert item(env, "P4.2").status == be.FAIL and "资金" in item(env, "P4.2").summary


# ---------- 其他步骤 ----------

def test_connection_step_is_manual_without_interactive(env):
    bt.step_connection(env.ctx, env.book)
    assert item(env, "P2.1").status == be.MANUAL


def test_batch_cancel_is_recorded_as_not_implemented(env):
    bt.step_batch_cancel(env.ctx, env.book)
    assert item(env, "P5.2").status == be.SKIP and "未实现" in item(env, "P5.2").shots[0].lines[0]


def test_logs_step_needs_all_four_categories(env, monkeypatch):
    monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["Send new order -> CTP", "交易服务器登录成功", "[WARN] 风控阈值触发:x", "交易委托失败,代码:21"])
    bt.step_logs(env.ctx, env.book)
    ev = item(env, "P6.1")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["交易日志", "系统运行记录", "监测记录", "错误提示记录"]
    monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["交易服务器登录成功"])
    env.book.items.clear()
    bt.step_logs(env.ctx, env.book)
    assert item(env, "P6.1").status == be.FAIL and "监测记录" in item(env, "P6.1").summary


def test_exit_step_runs_the_runtime_exit_tests(env, monkeypatch):
    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="12 passed in 3s\n".encode()))
    bt.step_exit(env.ctx, env.book)
    ev = item(env, "T7")
    assert ev.status == be.PASS and any("12 passed" in ln for ln in ev.shots[0].lines) and "代码级验证" in ev.shots[0].lines[0]
    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"1 failed"))
    env.book.items.clear()
    bt.step_exit(env.ctx, env.book)
    assert item(env, "T7").status == be.FAIL


def test_login_step_reports_milestones_and_failures(tmp_path):
    e = Env(tmp_path)
    try:
        e.ctx.connect_timeout = 0.5
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        assert bt.step_login(e.ctx, e.book) is True
        assert e.book.items["T1"].status == be.PASS and e.book.items["P1.1"].status == be.PASS
        e.book.items.clear()
        e.collector.log_lines.clear()
        e.collector.log_lines.extend(["交易服务器连接成功", "交易服务器授权验证失败，代码：63，信息：x"])
        assert bt.step_login(e.ctx, e.book) is False
        assert e.book.items["T1"].status == be.FAIL and "代码：63" in e.book.items["T1"].summary
    finally:
        e.main.close()


def test_run_steps_stops_after_failed_login_and_runs_everything_otherwise(tmp_path, monkeypatch):
    e = Env(tmp_path)
    try:
        e.ctx.connect_timeout = 0.3
        assert bt.run_steps(e.ctx, e.book) is False                              # 没有里程碑日志 → 登录失败 → 后续不执行
        assert set(e.book.items) == {"T1", "P1.1"}
        e.book.items.clear()
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"ok"))
        monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["Send new order", "登录成功", "[WARN] 风控阈值触发", "委托被拦截"])
        e.gw.mode = "fill"
        e.gw.position_lag = False
        assert bt.run_steps(e.ctx, e.book) is True
        assert set(e.book.items) == set(be.ITEMS)                               # 全部测试项都有结果
        assert e.book.items["P2.1"].status == be.MANUAL
    finally:
        e.main.close()


def test_run_steps_only_filter(tmp_path):
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        assert bt.run_steps(e.ctx, e.book, only={"P4.1"}) is True
        assert set(e.book.items) == {"T1", "P1.1", "P4.1"}
    finally:
        e.main.close()


# ---------- Book / 入口 ----------

def test_book_combines_statuses_and_shots():
    book = bt.Book()
    book.add("P1.2", be.PASS, "", [be.Shot("开仓", ["a"])], "t")
    book.add("P1.2", be.SKIP, "撤单需交易时段", [be.Shot("撤单", ["b"])], "t")
    ev = book.items["P1.2"]
    assert ev.status == be.SKIP and [s.label for s in ev.shots] == ["开仓", "撤单"] and "撤单需交易时段" in ev.summary
    book.add("P1.2", be.FAIL, "x", [], "t")
    assert book.items["P1.2"].status == be.FAIL


def test_main_refuses_without_confirmation_and_for_production(monkeypatch, capsys):
    env = {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "10.0.0.1:1",
           "CTP_MD_ADDRESS": "10.0.0.1:2", "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2", "LIVE_VT_SYMBOL": "c2701.DCE",
           "CTP_ENVIRONMENT": "测试"}
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert bt.main([]) == bt.EXIT_CONFIG and "--confirm-broker-test" in capsys.readouterr().err
    # 白名单为空 → 拒绝(「柜台环境=测试」不说明连的是哪台机器)
    monkeypatch.setenv("CTP_ENVIRONMENT", "实盘")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "白名单" in capsys.readouterr().err
    # 地址在可信表内、但柜台环境是「实盘」→ 生产 API,拒绝
    monkeypatch.setenv("CTP_TD_ADDRESS", "121.13.218.19:41285")
    monkeypatch.setenv("CTP_MD_ADDRESS", "121.13.218.19:41293")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "121.13.218.19:41285")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_MD", "121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "生产 API" in capsys.readouterr().err
    monkeypatch.setenv("CTP_ENVIRONMENT", "测试")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    monkeypatch.delenv("LIVE_VT_SYMBOL")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG


def test_pause_flag_helper_is_shared_with_the_cli(tmp_path):
    import pause_trading
    flag = pause_trading.pause(tmp_path, "x", now=datetime(2026, 11, 11))
    assert flag.exists() and pause_trading.resume(tmp_path)


def _guoxin_env(monkeypatch, **over):
    env = {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030",
           "CTP_TD_ADDRESS": "121.13.218.19:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293",
           "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2", "LIVE_VT_SYMBOL": "c2701.DCE",
           "CTP_ENVIRONMENT": "测试"}
    env.update(over)
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def test_market_data_front_must_be_whitelisted_too(monkeypatch, capsys):
    """行情前置喂回来的盘口价会直接变成委托价,只校验交易前置等于留了半个口子。"""
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="121.13.218.19:41285")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "行情前置" in err and "BROKER_TEST_ALLOWED_MD" in err


def test_whitelist_cannot_widen_beyond_the_trusted_addresses(monkeypatch, capsys):
    """白名单与地址同在 .env 里,采信它等于自己给自己发通行证。

    所以白名单只能**收窄**:不在可信地址表内的地址,哪怕同时写进
    CTP_TD_ADDRESS 和 BROKER_TEST_ALLOWED_TD,也不放行。
    """
    _guoxin_env(monkeypatch, CTP_TD_ADDRESS="10.0.0.1:41285",
                BROKER_TEST_ALLOWED_TD="10.0.0.1:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "可信地址表" in err and "已丢弃" in err and "拒绝连接" in err


def test_a_whitelist_outside_the_trusted_table_is_dropped_and_reported(monkeypatch, capsys):
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="10.0.0.1:41285,121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "10.0.0.1:41285" in err and "已丢弃" in err


def test_market_data_front_outside_the_whitelist_is_refused(monkeypatch, capsys):
    _guoxin_env(monkeypatch, CTP_MD_ADDRESS="8.8.8.8:1",
                BROKER_TEST_ALLOWED_TD="121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    assert "行情前置" in capsys.readouterr().err


def test_both_fronts_whitelisted_pass_the_gate(monkeypatch, capsys):
    """两个白名单都配好时,闸门放行(这里靠「未知测试项」提前收住,不会真的去连)。"""
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "未知测试项" in err and "行情前置" not in err


# ---------- 收尾兜底 ----------

def test_final_safety_cancels_leftover_orders_and_flags_positions(env):
    env.gw.mode = "accept"
    oid = bt.send(env.ctx, bt.make_req(env.ctx, price=2100.0))
    assert env.collector.order_of(oid).is_active()
    env.gw.set_position(Direction.LONG, 1)
    notes = bt.final_safety(env.ctx)
    assert not env.collector.order_of(oid).is_active()
    assert any("撤销了 1 笔遗留委托" in n for n in notes) and any("⚠⚠ 收尾发现账户有持仓" in n for n in notes)


def test_final_safety_is_quiet_when_clean_and_never_raises(env):
    assert bt.final_safety(env.ctx) == []
    env.ctx.main_engine = SimpleNamespace(get_all_active_orders=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "收尾检查出错" in bt.final_safety(env.ctx)[0]


def test_main_refuses_to_connect_when_state_isolation_did_not_take_effect(monkeypatch, capsys):
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "10.0.0.1:1",
                 "CTP_MD_ADDRESS": "10.0.0.1:2", "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2",
                 "LIVE_VT_SYMBOL": "c2701.DCE", "CTP_ENVIRONMENT": "测试"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bt, "ISOLATED", False)
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "隔离未生效" in capsys.readouterr().err


def test_isolation_holds_when_run_as_a_fresh_script():
    import subprocess
    code = ("import sys; sys.path.insert(0, 'scripts'); import broker_test; print('ISOLATED=' + str(broker_test.ISOLATED))")
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT, capture_output=True, timeout=120)
    assert "ISOLATED=True" in proc.stdout.decode("utf-8", errors="replace")


# ---------- codex 第二轮审查补的用例 ----------

def test_probe_price_gives_each_probe_order_a_unique_price(env):
    prices = [bt.probe_price(env.ctx) for _ in range(5)]
    assert len(set(prices)) == 5                                       # 不同价格,重复报单计数不跨步骤污染
    tick = abs(prices[1] - prices[0])
    assert tick == env.ctx.contract.pricetick


def test_cancel_then_duplicate_orders_share_no_price_key(env):
    """真实运行顺序 T4 → P2.3:撤单演示的委托不能计入重复报单的同一价格键。"""
    env.gw.mode = "accept"
    bt.step_cancel(env.ctx, env.book)
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["ok"] and item(env, "P2.3").status == be.PASS, item(env, "P2.3").summary


def test_run_steps_only_order_items_with_a_position_gates_every_order_step(tmp_path):
    """--only P4.2(或 P5.1)也必须先过空仓闸门:有持仓时所有会发单的演示都记 FAIL,一笔都不发。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        e.gw.set_position(Direction.LONG, 1)
        assert bt.run_steps(e.ctx, e.book, only={"P4.2"}) is True
        assert e.gw.sent == []
        item = e.book.items.get("P4.2")
        assert item is not None and item.status == be.FAIL and "已有持仓" in item.summary
    finally:
        e.main.close()


def test_flat_gate_says_which_failure_it_was(tmp_path):
    """闸门失败必须说清是「有持仓」还是「查不到持仓」——两种原因的处理方式完全不同。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        e.gw.set_position(Direction.LONG, 1)
        assert bt.run_steps(e.ctx, e.book, only={"P4.2"}) is True
        summary = e.book.items["P4.2"].summary
        assert "已有持仓" in summary and "查不到持仓" not in summary      # 有持仓:不说查不到
        assert e.ctx.flat_reason.startswith("账户已有持仓")
    finally:
        e.main.close()

    e2 = Env(tmp_path / "b")
    try:
        e2.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        # 断线:vnpy_ctp 只清 login_status,connect_status 一直是 True
        # (ctp_gateway.py 的 onFrontDisconnected)。以前这里只改 connect_status,
        # 于是「断线」这个场景根本没被测到——闸门照样放行。
        e2.gw.td_api.login_status = False
        assert e2.gw.td_api.connect_status is True                      # 陷阱:连接标志不会变
        assert bt.run_steps(e2.ctx, e2.book, only={"P4.2"}) is True
        summary = e2.book.items["P4.2"].summary
        assert "查不到持仓" in summary and "已有持仓" not in summary
        assert e2.ctx.flat_reason.startswith("交易连接断开")
    finally:
        e2.main.close()


def test_pause_step_cleans_flag_and_orders_when_sending_raises(env, monkeypatch):
    def boom(ctx, req=None):
        raise RuntimeError("模拟发送中途崩溃")
    monkeypatch.setattr(bt, "send", boom)
    with pytest.raises(RuntimeError):
        bt.step_pause(env.ctx, env.book)
    assert not (env.state_file.parent / "pause_trading.flag").exists()   # finally 解除暂停标志


def test_book_redacts_summary_and_shot_lines_at_add_time():
    book = bt.Book(clean=lambda t: t.replace("pw_secret_1", "***"))
    book.add("P1.1", be.PASS, "密码 pw_secret_1 泄露了", [be.Shot("", ["auth pw_secret_1 ok"])], "t")
    ev = book.items["P1.1"]
    assert ev.summary == "密码 *** 泄露了" and ev.shots[0].lines == ["auth *** ok"]


# ---------- 连接环境留痕 ----------

SETTING = {"用户名": "user_secret_9", "密码": "pw_secret_1", "经纪商代码": "8030", "交易服务器": "10.0.0.1:41205",
           "行情服务器": "10.0.0.1:41213", "产品名称": "client_product_1.0", "授权编码": "auth_secret_2", "柜台环境": "测试"}


def test_connection_info_records_fronts_and_broker_but_no_secrets():
    info = bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0))
    assert info["trade_front"] == "10.0.0.1:41205" and info["market_front"] == "10.0.0.1:41213"
    assert info["broker_id"] == "8030" and info["environment"] == "测试" and info["at"] == "2026-11-12T21:00:00"
    dumped = str(info)
    for secret in ("user_secret_9", "pw_secret_1", "auth_secret_2", "client_product_1.0"):
        assert secret not in dumped
    assert info["app_id"] and info["app_id"] != SETTING["产品名称"]       # 只留掩码,足以区分不同产品名


def test_save_connection_info_appends_one_entry_per_run(tmp_path):
    be.save_connection_info(tmp_path, {"at": "a"})
    be.save_connection_info(tmp_path, {"at": "b"})                       # --resume 的多次运行都要留痕

    import json
    assert [r["at"] for r in json.loads((tmp_path / "connection.json").read_text(encoding="utf-8"))] == ["a", "b"]


def test_session_and_evidence_timestamps_use_beijing_time_not_host_local_time(monkeypatch):
    """宿主机可能是 UTC-8:北京周五 10:00 在本机是周四 18:00,按本地时间会把交易时段误判为休市。"""
    from datetime import datetime as real_datetime, timezone
    real_now = real_datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)          # = 北京 2026-11-13 10:00 周五

    class Fake(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now.astimezone(tz) if tz else real_now.astimezone(timezone.utc).replace(tzinfo=None) - __import__("datetime").timedelta(hours=8)

    monkeypatch.setattr(bt, "datetime", Fake)
    now = bt.beijing_now()
    assert (now.hour, now.weekday()) == (10, 4)
    assert bt.in_trading_session(now) is True


def test_the_evidence_runner_refuses_a_non_guoxin_target_without_the_rehearsal_flag(monkeypatch, capsys):
    """这个取证运行器出的是**国信**的报告:连着别的环境生成的证据,
    目录名和内容都长得像正式证据,事后很容易被当成国信环境的结果交上去。
    """
    _guoxin_env(monkeypatch,
                CTP_BROKER_ID="9999",
                CTP_ENVIRONMENT="实盘",
                CTP_TD_ADDRESS="182.254.243.31:30001",
                CTP_MD_ADDRESS="182.254.243.31:30011",
                BROKER_TEST_ALLOWED_TD="182.254.243.31:30001",
                BROKER_TEST_ALLOWED_MD="182.254.243.31:30011")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "--rehearsal" in err, "非国信目标必须要求显式声明预演"


def test_the_rehearsal_flag_passes_the_gate_but_still_stops_before_connecting(monkeypatch, capsys):
    """显式声明预演后不再被那道闸门挡住;后面仍然要过别的闸门。

    这里用「状态目录隔离未生效」把它挡在连接之前 —— 测试绝不能真的去连柜台。
    """
    _guoxin_env(monkeypatch,
                CTP_BROKER_ID="9999",
                CTP_ENVIRONMENT="实盘",
                CTP_TD_ADDRESS="182.254.243.31:30001",
                CTP_MD_ADDRESS="182.254.243.31:30011",
                BROKER_TEST_ALLOWED_TD="182.254.243.31:30001",
                BROKER_TEST_ALLOWED_MD="182.254.243.31:30011")
    monkeypatch.setattr(bt, "ISOLATED", False)
    assert bt.main(["--confirm-broker-test", "--rehearsal"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "--rehearsal" not in err and "隔离" in err


def test_the_connection_record_states_which_environment_it_was(monkeypatch):
    """证据目录要能自证是在哪台机器上跑出来的,不能靠人记得。"""
    _guoxin_env(monkeypatch)
    guoxin = bt.connection_info({"经纪商代码": "8030", "柜台环境": "测试",
                                  "交易服务器": "121.13.218.19:41285", "行情服务器": "121.13.218.19:41293",
                                  "产品名称": "client_x_1.0"})
    assert guoxin["mode"] == "guoxin"
    rehearsal = bt.connection_info({"经纪商代码": "9999", "柜台环境": "实盘",
                                    "交易服务器": "182.254.243.31:30001", "行情服务器": "182.254.243.31:30011",
                                    "产品名称": "client_x_1.0"})
    assert rehearsal["mode"] == "rehearsal"


def test_connection_mode_follows_the_rehearsal_flag_even_for_the_guoxin_broker():
    """对 8030 跑预演也必须标成预演(mode 以 --rehearsal 为准,并与经纪商代码交叉校验)。"""
    assert bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0))["mode"] == "guoxin"
    assert bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0), rehearsal=True)["mode"] == "rehearsal"
    other = {**SETTING, "经纪商代码": "9999"}
    assert bt.connection_info(other, now=datetime(2026, 11, 12, 21, 0))["mode"] == "rehearsal"


def test_a_step_that_raises_does_not_discard_the_evidence_already_collected(tmp_path, monkeypatch):
    """任一步抛异常也要保住已采到的证据:这一步记 FAIL,后面的发单演示因状态不明而中止。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        e.gw.mode = "fill"
        e.gw.position_lag = False

        def boom(ctx, book):
            raise RuntimeError("柜台炸了 pw_secret_1")

        monkeypatch.setattr(bt, "step_basic_trade", boom)
        assert bt.run_steps(e.ctx, e.book, only={"T3", "P4.1"}) is True        # 不抛
        assert e.book.items["T3"].status == be.FAIL
        assert "RuntimeError" in e.book.items["T3"].summary
        assert "P4.1" in e.book.items                                         # 不发单的步骤照常出证据
    finally:
        e.main.close()


def test_a_crashed_step_makes_the_run_exit_with_the_crash_code(tmp_path, monkeypatch):
    """步骤抛异常 = 证据不完整:run_steps 记下失败步骤,main 据此返回崩溃码(一键流程不出报告)。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        monkeypatch.setattr(bt, "step_validity", lambda ctx, book: (_ for _ in ()).throw(RuntimeError("x")))
        assert bt.run_steps(e.ctx, e.book, only={"P4.1"}) is True
        assert e.ctx.failed_steps == ["<lambda>"]
    finally:
        e.main.close()
