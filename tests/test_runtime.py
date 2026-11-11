"""trading/runtime.py 离线测试:真实 MainEngine + 风控链 + CtaEngine,假网关/假通知,不联网。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bootstrap  # noqa: E402,F401
import vnpy_ctastrategy.engine as cta_engine_module  # noqa: E402
from fakes import FakeDatabase, FakeGateway, SyncEventEngine, history_bars  # noqa: E402
from risk.account_guard import AccountGuard, AccountLimits  # noqa: E402
from trading.events import EVENT_GUARD_HARD_LOCK  # noqa: E402
from trading.runtime import (  # noqa: E402
    EXIT_OK,
    EXIT_RECONCILE_REFUSED,
    EXIT_STARTUP_FAILED,
    RuntimeConfig,
    TradingRuntime,
    build_ctp_setting,
)
from vnpy.event import Event  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType  # noqa: E402
from vnpy.trader.event import EVENT_LOG, EVENT_TIMER  # noqa: E402
from vnpy.trader.object import LogData, OrderRequest  # noqa: E402

SETTING = {"fast_window": 2, "slow_window": 3, "atr_mult": 1.5, "risk_budget": 1e9}
VT = "c2601.DCE"


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.dt = datetime(2026, 11, 10, 10, 0, 0)       # 交易时段内

    def mono(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def now(self):
        return self.dt


class RecordingNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []
        self.closed = False
        self.close_budget: float | None = None      # close() 收到的排空预算

    def send(self, level, title, message, extra=None):
        self.messages.append((level, title, message))
        return True

    def close(self, drain_timeout=0.0):
        self.closed = True
        self.close_budget = drain_timeout

    def titles(self):
        return [t for _, t, _ in self.messages]

    def find(self, title):
        return [m for _, t, m in self.messages if t == title]


class RaisingNotifier(RecordingNotifier):
    def send(self, *a, **k):
        raise RuntimeError("webhook down")


@pytest.fixture
def make(monkeypatch, tmp_path):
    monkeypatch.setattr(cta_engine_module, "load_json", lambda filename: {})
    monkeypatch.setattr(cta_engine_module, "save_json", lambda *a, **k: None)
    created = []

    def factory(notifier=None, config_overrides=None, state=None, **kw):
        clock = Clock()
        cfg = RuntimeConfig(vt_symbol=VT, strategy_setting=SETTING, gateway_setting={"用户名": "u"},
                            position_settle_seconds=1.0, connect_timeout=5.0, **(config_overrides or {}))
        rt = TradingRuntime(cfg, FakeGateway, notifier=notifier or RecordingNotifier(), event_engine=SyncEventEngine(),
                            sleep=clock.sleep, monotonic=clock.mono, now=clock.now, **kw)
        rt.clock = clock
        rt.rule.update_setting({"state_file": str(tmp_path / "guard.json")})       # 隔离:不读写真实状态文件
        rt.rule.clock, rt.rule.mono = clock.now, clock.mono
        rt.cta.database = FakeDatabase(history_bars())
        rt.gw = rt.gateway
        rt.gw.mode = "fill"
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        rt.shutdown()


def timer(rt):
    rt.event_engine.put(Event(EVENT_TIMER))


# ---------- 启动 ----------

def test_clean_start_runs_the_strategy(make):
    rt = make()
    assert rt.start() is None
    assert rt.strategy.trading and rt.strategy.inited
    assert rt.gw.connect_count == 1 and rt.gw.connected_with == {"用户名": "u"}
    assert "已启动" in rt.notifier.titles()


def test_persisted_hard_lock_prevents_connecting_and_trading(make, tmp_path):
    rt = make()
    g = AccountGuard(AccountLimits(600, 2000), tmp_path / "guard.json")
    g.update(10_000, datetime(2026, 11, 9, 10, 0))
    g.update(7_900, datetime(2026, 11, 9, 10, 0))                 # 触发硬锁定并落盘
    rt.rule.update_setting({"state_file": str(tmp_path / "guard.json")})
    assert rt.start() == EXIT_OK
    assert getattr(rt.gw, "connect_count", 0) == 0                 # 根本不连接
    assert rt.strategy is None
    assert "硬锁定生效中,不启动" in rt.notifier.titles()


def test_startup_fails_when_contract_never_arrives(make):
    rt = make()
    rt.gw.connect_pushes = False
    assert rt.start() == EXIT_STARTUP_FAILED
    assert rt.strategy is None and "启动失败" in rt.notifier.titles()


def test_startup_fails_when_connect_raises(make):
    rt = make()

    def boom(setting):
        raise ConnectionError("no route")

    rt.gw.connect = boom
    assert rt.start() == EXIT_STARTUP_FAILED
    assert "网关连接失败" in rt.notifier.titles()


# ---------- 对账 ----------

def test_refuses_to_start_with_foreign_positions(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1, symbol="c2605")         # 账户里有别的合约的持仓
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert not rt.strategy.trading
    assert any("不属于本策略合约" in m for m in rt.notifier.find("对账失败,拒绝启动"))


def test_refuses_when_account_has_position_but_strategy_thinks_flat(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1)
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert any("不一致" in m for m in rt.notifier.find("对账失败,拒绝启动"))


def test_refuses_when_strategy_restored_position_but_account_is_flat(make):
    rt = make()
    rt.cta.strategy_data["trend_atr"] = {"pos": 1, "prev_pos": 1, "trail_side": 1, "trail_stop": 2200.0,
                                         "trail_extreme": 2230.0, "entry_price": 2231.0, "stop_price": 2200.0}
    assert rt.start() == EXIT_RECONCILE_REFUSED


def test_refuses_when_restored_state_is_self_contradictory(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1)
    rt.cta.strategy_data["trend_atr"] = {"pos": 1, "prev_pos": 1, "trail_side": 0}      # 有持仓却没有止损状态
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert any("自相矛盾" in m for m in rt.notifier.find("对账失败,拒绝启动"))


def test_resumes_with_restored_position_when_account_matches(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1)
    rt.cta.strategy_data["trend_atr"] = {"pos": 1, "prev_pos": 1, "trail_side": 1, "trail_stop": 2200.0,
                                         "trail_extreme": 2230.0, "entry_price": 2231.0, "stop_price": 2200.0,
                                         "trading": True}                       # 引擎会把保存的 trading=True 恢复出来
    assert rt.start() is None
    assert rt.strategy.pos == 1 and rt.strategy.trading
    assert "对账失败,拒绝启动" not in rt.notifier.titles()


def test_trading_flag_restored_by_engine_cannot_bypass_reconciliation(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1)
    rt.cta.strategy_data["trend_atr"] = {"pos": 0, "trading": True}              # 持久化数据说在交易、持仓为 0,账户却有仓
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert rt.strategy.trading is False                                           # 仍被强制停止


def test_leftover_active_orders_are_cancelled_at_start(make):
    rt = make()
    rt.gw.mode = "accept"
    rt.gw.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                  type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN))
    assert rt.start() is None
    assert len(rt.gw.cancel_requests) == 1 and "启动时撤销遗留委托" in rt.notifier.titles()


# ---------- 硬锁定与退出 ----------

def test_hard_lock_with_confirmed_flat_stops_the_runtime(make):
    rt = make()
    rt.start()
    rt.event_engine.put(Event(EVENT_GUARD_HARD_LOCK, {"flat": True, "reason": "回撤"}))
    assert rt.exit_code == EXIT_OK and rt.wait() == EXIT_OK                       # wait 立即返回


def test_hard_lock_without_flat_keeps_the_process_alive(make):
    rt = make()
    rt.start()
    rt.event_engine.put(Event(EVENT_GUARD_HARD_LOCK, {"flat": False, "reason": "平仓失败"}))
    assert not rt._stop_event.is_set() and rt.exit_code is None                    # 带着持仓不能退出(止损是本地的)
    assert "硬锁定但平仓未完成" in rt.notifier.titles()


def test_shutdown_stops_strategy_closes_gateway_and_notifier(make):
    rt = make()
    rt.start()
    rt.shutdown()
    rt.shutdown()                                                                  # 幂等
    assert not rt.strategy.trading and rt.gw.closed and rt.notifier.closed


def test_shutdown_with_open_position_raises_a_fatal_alert(make):
    rt = make()
    rt.start()
    rt.gw.set_position(Direction.LONG, 1)
    rt.shutdown()
    assert any("本地止损将随进程退出而失效" in m for m in rt.notifier.find("退出时仍有持仓"))


def test_shutdown_gives_the_notifier_time_to_deliver_the_fatal_alert(make):
    """退出时仍有持仓的 FATAL 是无人值守最后的兜底,不能随进程退出一起丢掉:
    关闭通知器的预算必须按通知器自己的排空预算来给,而不是默认的 10 秒。"""
    class _BudgetedNotifier(RecordingNotifier):
        def __init__(self):
            super().__init__()
            self.drain_timeout = 42.0

    rt = make(notifier=_BudgetedNotifier())
    rt.start()
    rt.shutdown()
    assert rt.notifier.close_budget == 42.0


def test_shutdown_survives_a_notifier_that_fails_to_close(make):
    """通知器关闭时抛异常不能打断 shutdown。

    退出码、对账、后续清理都还在这条路径上 —— 通知器是"顺带关一下",
    不能因为它出错就把整个退出流程炸掉。
    """
    class _BrokenNotifier(RecordingNotifier):
        def close(self, drain_timeout: float = 10.0) -> None:
            raise RuntimeError("通知器炸了")

    rt = make(notifier=_BrokenNotifier())
    rt.start()
    rt.shutdown()               # 不抛异常
    assert rt.gw.closed


def test_shutdown_survives_a_notifier_whose_close_signature_is_different(make):
    """签名对不上(TypeError)时退回无参关闭,而不是把退出流程打断。"""
    class _OldNotifier(RecordingNotifier):
        def close(self) -> None:                    # 旧签名:没有 drain_timeout
            super().close()

    rt = make(notifier=_OldNotifier())
    rt.start()
    rt.shutdown()
    assert rt.notifier.closed


def test_run_returns_refusal_code_and_always_shuts_down(make):
    rt = make()
    rt.gw.set_position(Direction.LONG, 1)
    assert rt.run() == EXIT_RECONCILE_REFUSED
    assert rt.notifier.closed


# ---------- 看门狗 ----------

def test_strategy_crash_triggers_alert_and_emergency_flatten_once(make):
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.gw.set_position(Direction.LONG, 1)
    rt.strategy.trading = False                                                    # CtaEngine 捕获策略异常后的状态
    timer(rt)
    assert "策略已停止运行" in rt.notifier.titles()
    assert rt.gw.sent and rt.gw.sent[-1].offset == Offset.CLOSE                    # 立即紧急平仓
    timer(rt)
    assert rt.notifier.titles().count("策略已停止运行") == 1                        # 只报一次


def test_stale_tick_alert_in_session_only_and_rate_limited(make):
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.clock.t += 61
    timer(rt)
    assert "行情停更" in rt.notifier.titles()
    timer(rt)
    assert rt.notifier.titles().count("行情停更") == 1                              # 5 分钟内不重复
    rt.clock.t += 301
    timer(rt)
    assert rt.notifier.titles().count("行情停更") == 2


def test_no_stale_alert_outside_trading_session(make):
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)                                # 午休
    rt.clock.t += 600
    timer(rt)
    assert "行情停更" not in rt.notifier.titles()


def test_no_stale_alert_before_the_first_tick(make):
    rt = make()
    rt.start()
    rt.clock.t += 600
    timer(rt)
    assert "行情停更" not in rt.notifier.titles()


# ---------- 假死看门狗:首次越限强撤开仓单(演练③) ----------

def _open_order_req(price=2200.0):
    return OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                        type=OrderType.LIMIT, volume=1, price=price, offset=Offset.OPEN)


def _close_order_req(price=2190.0):
    return OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.SHORT,
                        type=OrderType.LIMIT, volume=1, price=price, offset=Offset.CLOSE)


def test_stale_tick_forcibly_cancels_open_orders_only_once_and_rearms_on_new_tick(make):
    rt = make()
    rt.start()
    rt.gw.mode = "accept"                                                  # 挂单保持活动
    rt.gw.push_tick(last=2230.0)
    open_id = rt.main.send_order(_open_order_req(), "CTP")
    close_id = rt.main.send_order(_close_order_req(), "CTP")
    rt.clock.t += 61
    timer(rt)
    cancelled = {r.orderid for r in rt.gw.cancel_requests}
    assert open_id.split(".")[-1] in cancelled                            # 开仓挂单被强撤
    assert close_id.split(".")[-1] not in cancelled                       # 平仓单不动
    assert "行情假死" in rt.notifier.titles()
    n_cancels = len(rt.gw.cancel_requests)
    timer(rt)                                                             # 连续停更:边沿触发,不重复撤
    assert len(rt.gw.cancel_requests) == n_cancels
    rt.gw.push_tick(last=2231.0)                                          # 行情恢复,复位边沿
    rt.clock.t += 61
    timer(rt)
    assert rt.notifier.titles().count("行情假死") == 2                    # 再次停更可二次触发


def test_stale_cancel_switch_off_keeps_alert_only_behavior(make):
    rt = make(config_overrides={"stale_cancel_open_orders": False})
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    rt.main.send_order(_open_order_req(price=2201.0), "CTP")
    rt.clock.t += 61
    timer(rt)
    assert rt.gw.cancel_requests == []                                    # 开关关闭:只告警不撤单
    assert "行情假死" in rt.notifier.titles() and "行情停更" in rt.notifier.titles()


def test_stale_cancel_never_fires_outside_trading_session(make):
    rt = make()
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    rt.main.send_order(_open_order_req(price=2202.0), "CTP")
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)                        # 午休
    rt.clock.t += 61                                                       # 61s:越过假死阈值但低于规则 120s 超时撤单
    timer(rt)
    assert "行情假死" not in rt.notifier.titles() and rt.gw.cancel_requests == []


def test_stale_watchdog_rearms_at_session_reentry_instead_of_counting_the_break(make):
    """午休/夜盘休市后时段重入:定时器先于本时段首 Tick 触发时,不得把整个休市时间误算成假死。

    codex 第二轮审查动态复现:单调时钟的 _last_tick 不按时段边界重新武装时,
    13:30 开盘瞬间会报「7261s 没有 Tick」并误撤年龄 0 秒的新挂单。
    """
    rt = make()
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    rt.main.send_order(_open_order_req(price=2203.0), "CTP")
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)                       # 进入午休
    rt.clock.t += 7200
    timer(rt)
    assert "行情假死" not in rt.notifier.titles() and rt.gw.cancel_requests == []
    rt.clock.dt = datetime(2026, 11, 10, 13, 30, 1)                      # 开盘重入,定时器先于首 Tick
    timer(rt)
    assert "行情假死" not in rt.notifier.titles() and rt.gw.cancel_requests == []
    rt.gw.push_tick(last=2231.0)                                         # 本时段首 Tick 到达,从此刻才开始计静默
    rt.clock.t += 61
    timer(rt)
    assert "行情假死" in rt.notifier.titles() and rt.gw.cancel_requests  # 真假死仍然报、仍然撤


def test_stale_watchdog_rearms_at_night_session_reentry(make):
    """15:00 日盘收市 → 21:00 夜盘重入:trading_day 已滚动但时段外不动作,夜盘开盘不误报假死。"""
    rt = make()
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    rt.main.send_order(_open_order_req(price=2205.0), "CTP")
    rt.clock.dt = datetime(2026, 11, 10, 15, 0, 0)                        # 日盘收市
    rt.clock.t += 21600
    timer(rt)
    assert "行情假死" not in rt.notifier.titles()
    rt.clock.dt = datetime(2026, 11, 10, 21, 0, 1)                        # 夜盘重入,定时器先于首 Tick
    timer(rt)
    assert "行情假死" not in rt.notifier.titles() and rt.gw.cancel_requests == []
    rt.gw.push_tick(last=2231.0)
    rt.clock.t += 61
    timer(rt)
    assert "行情假死" in rt.notifier.titles() and rt.gw.cancel_requests  # 夜盘内的真假死照常处置


def test_start_during_a_break_does_not_fire_on_the_first_timer(make):
    """休市时段启动:初始基线即为"时段外",13:30 重入时才发生一次边沿,首定时器不误判。"""
    rt = make()
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)
    assert rt.start() is None
    rt.gw.mode = "accept"
    timer(rt)                                                             # 休市中:无任何假死告警
    assert "行情假死" not in rt.notifier.titles()
    rt.clock.dt = datetime(2026, 11, 10, 13, 30, 1)
    rt.main.send_order(_open_order_req(price=2206.0), "CTP")
    timer(rt)                                                             # 重入瞬间:年龄 0 秒的单不能被撤
    assert "行情假死" not in rt.notifier.titles() and rt.gw.cancel_requests == []


def test_session_reentry_clears_only_open_order_cancel_throttle(make):
    """时段边沿只拨开仓单的撤单节流时间戳,平仓单的 10s 节流必须保留(codex Low1 收窄)。"""
    rt = make()
    rt.clock.dt = datetime(2026, 11, 10, 10, 0, 0)
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.cancel_works = False                                           # 撤单无回报:订单保持活动,节流时间戳才会留存
    rt.gw.push_tick(last=2230.0)
    open_id = rt.main.send_order(_open_order_req(price=2207.0), "CTP")
    close_id = rt.main.send_order(_close_order_req(price=2190.0), "CTP")
    rt.rule._cancel_order(open_id, rt.clock.t)                            # 各发一次撤单请求,建立节流时间戳
    rt.rule._cancel_order(close_id, rt.clock.t)
    assert set(rt.rule._cancel_requested) == {open_id, close_id}
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)                        # 进入午休
    rt.clock.t += 7200
    timer(rt)
    rt.clock.dt = datetime(2026, 11, 10, 13, 30, 0)                       # 重入:开仓单节流被拨、平仓单保留
    timer(rt)
    assert open_id not in rt.rule._cancel_requested
    assert close_id in rt.rule._cancel_requested


def test_rule_stale_order_timeout_excludes_the_lunch_break(make):
    """规则自身的 120s 超时撤单同样按交易时段计时:休市两小时不能让旧挂单在开盘瞬间被撤。"""
    rt = make()
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    oid = rt.main.send_order(_open_order_req(price=2204.0), "CTP")
    rt.clock.dt = datetime(2026, 11, 10, 12, 0, 0)                       # 午休
    rt.clock.t += 7200
    timer(rt)                                                            # 休市期间不做超时撤单
    rt.clock.dt = datetime(2026, 11, 10, 13, 30, 0)                      # 重入:挂单年龄被拨回 0
    timer(rt)
    rt.clock.t += 61                                                     # 时段内仅 61s < 120s(墙钟年龄已超 2 小时)
    timer(rt)
    cancelled = {r.orderid for r in rt.gw.cancel_requests}
    assert oid.split(".")[-1] not in cancelled
    rt.clock.t += 60                                                     # 时段内满 121s:超时撤单恢复生效
    timer(rt)
    assert oid.split(".")[-1] in {r.orderid for r in rt.gw.cancel_requests}


def _gateway_log(rt, msg, level=20):
    rt.event_engine.put(Event(EVENT_LOG, LogData(msg=msg, level=level, gateway_name=rt.config.gateway_name)))


# ---------- 断线连接状态机(演练①) ----------

def test_connection_state_tracks_td_and_md_channels_separately(make):
    rt = make()
    rt.start()
    _gateway_log(rt, "交易服务器连接断开，原因1")
    assert rt.connection_state == {"交易": False}
    _gateway_log(rt, "行情服务器连接断开，原因1")
    assert rt.connection_state == {"交易": False, "行情": False}
    assert rt.notifier.titles().count("网关连接断开") == 2
    _gateway_log(rt, "交易服务器登录成功")
    assert rt.connection_state == {"交易": True, "行情": False}
    _gateway_log(rt, "行情服务器登录成功")
    assert rt.connection_state == {"交易": True, "行情": True}
    assert rt.notifier.titles().count("网关已连接") == 2


def test_repeated_disconnect_logs_alert_only_once_per_channel(make):
    rt = make()
    rt.start()
    for _ in range(3):
        _gateway_log(rt, "交易服务器连接断开，原因1")
    assert rt.notifier.titles().count("网关连接断开") == 1
    _gateway_log(rt, "行情服务器连接断开，原因1")
    assert rt.notifier.titles().count("网关连接断开") == 2


def test_intermediate_connect_success_does_not_mark_connected(make):
    """「连接成功」只是 TCP 连上、尚未认证登录,不能作为恢复凭据,否则告警会抖动。"""
    rt = make()
    rt.start()
    _gateway_log(rt, "交易服务器连接断开，原因1")
    _gateway_log(rt, "交易服务器连接成功")
    assert rt.connection_state == {"交易": False}
    assert "网关已连接" not in rt.notifier.titles()
    _gateway_log(rt, "交易服务器登录成功")
    assert rt.connection_state == {"交易": True}


def test_reconnected_channel_does_not_realert_without_a_new_disconnect(make):
    rt = make()
    rt.start()
    _gateway_log(rt, "交易服务器登录成功")
    _gateway_log(rt, "交易服务器登录成功")                    # 重复的登录成功日志
    assert rt.notifier.titles().count("网关已连接") == 1


def test_other_gateway_logs_do_not_change_connection_state(make):
    rt = make()
    rt.start()
    rt.event_engine.put(Event(EVENT_LOG, LogData(msg="交易服务器连接断开", level=20, gateway_name="SomethingElse")))
    assert rt.connection_state == {}


def test_stale_cancel_does_not_touch_emergency_flatten_orders(make):
    """假死撤单与紧急平仓状态机互斥:FLATTEN_REF 平仓单绝不能被看门狗撤掉。"""
    rt = make(config_overrides={"tick_stale_seconds": 5.0})               # 缩短阈值,在平仓 ack 超时(10s)内完成验证
    rt.start()
    rt.gw.mode = "accept"
    rt.gw.push_tick(last=2230.0)
    rt.gw.set_position(Direction.LONG, 1)
    rt.rule.emergency_flatten("测试用紧急平仓")
    assert rt.rule.flatten_active
    flatten_ids = {oid for oid, o in rt.gw.orders.items() if o.reference == "account_guard_flatten"}
    assert flatten_ids
    rt.clock.t += 6
    timer(rt)
    assert "行情假死" in rt.notifier.titles()                             # 假死确实触发了
    cancelled = {r.orderid for r in rt.gw.cancel_requests}
    assert not (flatten_ids & cancelled)                                  # 紧急平仓单未被波及


def test_heartbeat(make):
    rt = make(config_overrides={"heartbeat_seconds": 30.0})
    rt.start()
    rt.clock.t += 20
    timer(rt)
    assert "心跳" not in rt.notifier.titles()
    rt.clock.t += 11
    timer(rt)
    assert any("权益" in m and "风控状态" in m for m in rt.notifier.find("心跳"))


def test_heartbeat_file_is_refreshed_every_ten_seconds_once_running(make, tmp_path):
    hb = tmp_path / "state" / "heartbeat"
    rt = make(config_overrides={"heartbeat_file": hb})
    timer(rt)
    assert not hb.exists()                                                         # 启动前不写,避免健康检查误判
    rt.start()
    timer(rt)
    assert hb.exists()
    first = hb.read_text()
    rt.clock.t += 5
    hb.write_text("old")
    timer(rt)
    assert hb.read_text() == "old"                                                 # 10 秒内不重复写
    rt.clock.t += 6
    timer(rt)
    assert hb.read_text() != "old" and first.strip().isdigit()


def test_unwritable_state_directory_refuses_to_start(make, tmp_path):
    """Docker 卷默认属 root、容器内 uid 1000 写不进:风控状态(尤其硬锁定)会写盘失败、重启后被洗掉。必须拒绝启动。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                                                        # 目录位置被文件占用,mkdir 必然失败
    rt = make(config_overrides={"heartbeat_file": blocker / "heartbeat"})
    assert rt.start() == EXIT_STARTUP_FAILED
    assert any("状态目录不可写" in m for m in rt.notifier.find("启动失败"))
    assert getattr(rt.gw, "connect_count", 0) == 0                                 # 连网关之前就拒绝


def test_guard_state_directory_is_checked_too(make, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    rt = make()
    rt.rule.update_setting({"state_file": str(blocker / "guard.json")})
    assert rt.start() == EXIT_STARTUP_FAILED


def test_heartbeat_write_failure_after_start_never_breaks_trading(make, tmp_path, monkeypatch):
    rt = make(config_overrides={"heartbeat_file": tmp_path / "hb" / "heartbeat"})
    assert rt.start() is None
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rt.clock.t += 30
    timer(rt)                                                                      # 不抛异常
    assert rt.strategy.trading


def test_timer_before_start_does_nothing(make):
    rt = make()
    timer(rt)
    assert rt.notifier.messages == []


# ---------- 通知 ----------

def test_trades_and_gateway_logs_are_forwarded(make):
    rt = make()
    rt.start()
    rt.main.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                    type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN), "CTP")
    assert any("c2601.DCE" in m and "2200" in m for m in rt.notifier.find("成交"))

    def gateway_log(msg, level=20):
        rt.event_engine.put(Event(EVENT_LOG, LogData(msg=msg, level=level, gateway_name="CTP")))

    gateway_log("交易服务器连接断开，原因0x1001")
    gateway_log("交易服务器登录成功")
    gateway_log("交易委托失败，信息：资金不足", level=40)
    assert {"网关连接断开", "网关已连接", "网关错误"} <= set(rt.notifier.titles())


def test_other_sources_logs_are_not_forwarded_as_gateway_events(make):
    rt = make()
    rt.start()
    before = len(rt.notifier.messages)
    rt.event_engine.put(Event(EVENT_LOG, LogData(msg="交易服务器连接断开", level=20, gateway_name="SomethingElse")))
    assert len(rt.notifier.messages) == before


def test_notifier_failures_never_break_trading(make):
    rt = make(notifier=RaisingNotifier())
    assert rt.start() is None and rt.strategy.trading
    rt.gw.push_tick(last=2230.0)
    timer(rt)                                                                      # 不抛异常


# ---------- 连接参数 ----------

ENV = {"CTP_USERNAME": "u1", "CTP_PASSWORD": "p1", "CTP_BROKER_ID": "9999", "CTP_TD_ADDRESS": "td:1",
       "CTP_MD_ADDRESS": "md:1", "CTP_APP_ID": "app", "CTP_AUTH_CODE": "code"}


def test_build_ctp_setting_maps_environment():
    s = build_ctp_setting(ENV)
    assert s["用户名"] == "u1" and s["经纪商代码"] == "9999" and s["授权编码"] == "code" and s["柜台环境"] == "实盘"
    assert build_ctp_setting({**ENV, "CTP_ENVIRONMENT": "测试"})["柜台环境"] == "测试"


def test_build_ctp_setting_reports_missing_names_without_leaking_values():
    env = {k: v for k, v in ENV.items() if k not in ("CTP_PASSWORD", "CTP_AUTH_CODE")}
    with pytest.raises(ValueError) as exc:
        build_ctp_setting(env)
    msg = str(exc.value)
    assert "CTP_PASSWORD" in msg and "CTP_AUTH_CODE" in msg and "u1" not in msg and "9999" not in msg
def test_timer_before_start_does_nothing(make):
    rt = make()
    timer(rt)
    assert rt.notifier.messages == []


# ---------- 通知 ----------

def test_trades_and_gateway_logs_are_forwarded(make):
    rt = make()
    rt.start()
    rt.main.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                    type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN), "CTP")
    assert any("c2601.DCE" in m and "2200" in m for m in rt.notifier.find("成交"))

    def gateway_log(msg, level=20):
        rt.event_engine.put(Event(EVENT_LOG, LogData(msg=msg, level=level, gateway_name="CTP")))

    gateway_log("交易服务器连接断开，原因0x1001")
    gateway_log("交易服务器登录成功")
    gateway_log("交易委托失败，信息：资金不足", level=40)
    assert {"网关连接断开", "网关已连接", "网关错误"} <= set(rt.notifier.titles())


def test_other_sources_logs_are_not_forwarded_as_gateway_events(make):
    rt = make()
    rt.start()
    before = len(rt.notifier.messages)
    rt.event_engine.put(Event(EVENT_LOG, LogData(msg="交易服务器连接断开", level=20, gateway_name="SomethingElse")))
    assert len(rt.notifier.messages) == before


def test_notifier_failures_never_break_trading(make):
    rt = make(notifier=RaisingNotifier())
    assert rt.start() is None and rt.strategy.trading
    rt.gw.push_tick(last=2230.0)
    timer(rt)                                                                      # 不抛异常


# ---------- 连接参数 ----------

ENV = {"CTP_USERNAME": "u1", "CTP_PASSWORD": "p1", "CTP_BROKER_ID": "9999", "CTP_TD_ADDRESS": "td:1",
       "CTP_MD_ADDRESS": "md:1", "CTP_APP_ID": "app", "CTP_AUTH_CODE": "code"}


def test_build_ctp_setting_maps_environment():
    s = build_ctp_setting(ENV)
    assert s["用户名"] == "u1" and s["经纪商代码"] == "9999" and s["授权编码"] == "code" and s["柜台环境"] == "实盘"
    assert build_ctp_setting({**ENV, "CTP_ENVIRONMENT": "测试"})["柜台环境"] == "测试"


def test_build_ctp_setting_reports_missing_names_without_leaking_values():
    env = {k: v for k, v in ENV.items() if k not in ("CTP_PASSWORD", "CTP_AUTH_CODE")}
    with pytest.raises(ValueError) as exc:
        build_ctp_setting(env)
    msg = str(exc.value)
    assert "CTP_PASSWORD" in msg and "CTP_AUTH_CODE" in msg and "u1" not in msg and "9999" not in msg


# ---------- codex 第二轮审查后补充 ----------

def test_graceful_shutdown_never_triggers_the_crash_watchdog(make):
    """stop_all_strategies 会把 trading 置假;若此时恰有定时事件被处理,不能被误判为"策略崩溃"而紧急平仓。"""
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.gw.set_position(Direction.LONG, 1)
    original = rt.cta.stop_all_strategies

    def stop_with_timer_in_between():
        original()
        timer(rt)                                                                  # 模拟竞态:停策略之后、关引擎之前的定时事件

    rt.cta.stop_all_strategies = stop_with_timer_in_between
    rt.shutdown()
    assert "策略已停止运行" not in rt.notifier.titles()
    assert rt.gw.sent == []                                                        # 没有任何紧急平仓单


def test_persisted_trading_and_inited_flags_are_dropped_before_init(make):
    rt = make()
    rt.cta.strategy_data["trend_atr"] = {"pos": 0, "trading": True, "inited": True}
    seen = {}
    original = rt.cta.init_strategy

    def spy(name):
        seen.update(rt.cta.strategy_data[name])
        return original(name)

    rt.cta.init_strategy = spy
    assert rt.start() is None
    assert "trading" not in seen and "inited" not in seen                          # init 之前就删掉,没有可交易的时间窗


def test_wait_returns_promptly_when_stop_is_requested_from_another_thread(make):
    import threading
    import time
    rt = make()
    threading.Timer(0.2, rt.request_stop).start()
    t0 = time.time()
    assert rt.wait() == EXIT_OK
    assert time.time() - t0 < 3


def test_start_aborts_quickly_when_stop_is_requested_during_connect(make):
    rt = make()
    rt.gw.connect_pushes = False                                                   # 一直等不到合约
    rt.request_stop()                                                              # 启动阶段收到 SIGTERM
    assert rt.start() == EXIT_OK
    assert rt.clock.t < 1000.0 + 5                                                 # 没有傻等完 connect_timeout


def test_leftover_orders_that_cannot_be_cancelled_refuse_start(make):
    """撤单失败或竞态成交:不能带着"可能已变成无主持仓"的状态启动。"""
    rt = make()
    rt.gw.mode = "accept"
    rt.gw.cancel_works = False
    rt.gw.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                  type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN))
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert any("未能撤销" in m for m in rt.notifier.find("对账失败,拒绝启动"))
    assert not rt.strategy.trading


def test_positions_are_rechecked_after_cancelling_leftover_orders(make):
    rt = make()
    rt.gw.mode = "accept"
    rt.gw.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                  type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN))
    queries_before = rt.gw.position_queries
    assert rt.start() is None
    assert rt.gw.position_queries > queries_before + 2                             # 撤单后又重新查询并确认持仓


def test_unstable_position_reports_refuse_start(make):
    rt = make()
    counter = {"n": 0}

    def flapping_query():
        counter["n"] += 1
        rt.gw.positions[Direction.LONG] = float(counter["n"])                      # 每次查询持仓都不同
        rt.gw.push_positions()

    rt.gw.query_position = flapping_query
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert any("反复变化" in m for m in rt.notifier.find("对账失败,拒绝启动"))


def test_position_drift_while_running_triggers_alert_and_emergency_flatten(make):
    """运行中账户出现策略不知道的持仓(人工下单、回报丢失…):没有本地止损保护,容忍一小会儿后紧急平仓。"""
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.gw.set_position(Direction.LONG, 1)                                          # 策略 pos 仍是 0
    timer(rt)
    assert "持仓漂移" not in rt.notifier.titles()                                    # 刚发现,先容忍(持仓回报可能只是滞后)
    rt.clock.t += 16
    timer(rt)
    assert "持仓漂移" in rt.notifier.titles()
    assert rt.gw.sent and rt.gw.sent[-1].offset == Offset.CLOSE
    rt.clock.t += 30
    timer(rt)
    assert rt.notifier.titles().count("持仓漂移") == 1                              # 只报一次


def test_transient_position_mismatch_does_not_trigger_flatten(make):
    rt = make()
    rt.start()
    rt.gw.set_position(Direction.LONG, 1)
    timer(rt)
    rt.clock.t += 10
    rt.gw.set_position(Direction.LONG, 0)                                          # 容忍期内恢复一致
    timer(rt)
    rt.clock.t += 30
    timer(rt)
    assert "持仓漂移" not in rt.notifier.titles() and rt.gw.sent == []


def test_drift_check_is_skipped_while_an_emergency_flatten_is_running(make):
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.gw.mode = "accept"                                                          # 平仓单挂着不成交,平仓状态机保持"进行中"
    rt.gw.set_position(Direction.LONG, 1)
    rt.rule.emergency_flatten("test")
    assert rt.rule.flatten_active
    rt.clock.t += 100
    timer(rt)
    assert "持仓漂移" not in rt.notifier.titles()


# ---------- codex 第三轮复核后补充 ----------

def test_stop_request_while_waiting_for_leftover_orders_exits_cleanly_not_as_a_reconcile_failure(make):
    rt = make()
    rt.gw.mode = "accept"
    rt.gw.cancel_works = False
    rt.gw.send_order(OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=Direction.LONG,
                                  type=OrderType.LIMIT, volume=1, price=2200.0, offset=Offset.OPEN))
    original_sleep = rt._sleep
    state = {"n": 0}

    def sleep_then_stop(seconds):
        state["n"] += 1
        if state["n"] > 30:                                                        # 已经进入"等遗留委托撤销"阶段
            rt.request_stop()
        original_sleep(seconds)

    rt._sleep = sleep_then_stop
    assert rt.start() == EXIT_OK
    assert "对账失败,拒绝启动" not in rt.notifier.titles()                          # 不能把"收到停止信号"误报成对账失败


def test_stop_request_interrupts_a_slow_strategy_init(make):
    from concurrent.futures import Future
    rt = make()
    rt.cta.init_strategy = lambda name: Future()                                   # 永远不会完成的初始化
    rt.request_stop()
    assert rt.start() == EXIT_OK


def test_slow_strategy_init_times_out_as_a_startup_failure(make, monkeypatch):
    from concurrent.futures import Future
    rt = make(config_overrides={"init_timeout": 0.6})
    rt.cta.init_strategy = lambda name: Future()
    assert rt.start() == EXIT_STARTUP_FAILED
    assert "策略初始化失败" in rt.notifier.titles()


def test_unwritable_cta_data_directory_refuses_to_start(make, tmp_path, monkeypatch):
    """.vntrader(策略数据卷)只读:首笔成交后 sync_data 会抛异常 → 引擎停掉策略 → 看门狗盘中强制平仓。必须启动前发现。"""
    import trading.runtime as runtime_module
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(runtime_module, "get_file_path", lambda name: blocker / "sub" / name)
    rt = make()
    assert rt.start() == EXIT_STARTUP_FAILED
    assert getattr(rt.gw, "connect_count", 0) == 0


def test_timeouts_have_floors_so_zero_cannot_disable_the_checks(make):
    assert RuntimeConfig(vt_symbol=VT, position_settle_seconds=0.0).position_settle_seconds >= 2.0
    rt = make()
    rt.rule.update_setting({"position_refresh_timeout": 0, "flatten_ack_timeout": 0, "state_file": rt.rule.guard.state_path.as_posix()})
    assert rt.rule.position_refresh_timeout >= 3.0 and rt.rule.flatten_ack_timeout >= 3.0


def test_crash_watchdog_is_silent_once_a_stop_was_requested(make):
    rt = make()
    rt.start()
    rt.request_stop()
    rt.strategy.trading = False
    timer(rt)
    assert "策略已停止运行" not in rt.notifier.titles()


def test_drift_emergency_flatten_is_reported_only_once_even_if_strategy_pos_stays_wrong(make):
    rt = make()
    rt.start()
    rt.gw.push_tick(last=2230.0)
    rt.gw.set_position(Direction.LONG, 1)
    timer(rt)
    for _ in range(6):
        rt.clock.t += 20
        timer(rt)
    assert rt.notifier.titles().count("持仓漂移") == 1



def test_startup_timeout_alert_reports_which_milestones_were_reached(make):
    """启动慢时,告警里要能看出卡在哪一步(登录/结算确认/合约表下载)。"""
    from vnpy.trader.object import LogData
    rt = make()
    rt.gw.connect_pushes = False
    orig = rt.gw.connect

    def connect(setting):
        orig(setting)
        rt.clock.t += 2.0
        rt.event_engine.put(Event(EVENT_LOG, LogData(msg="交易服务器登录成功", gateway_name=rt.config.gateway_name)))
        rt.clock.t += 3.0
        rt.event_engine.put(Event(EVENT_LOG, LogData(msg="结算信息确认成功", gateway_name=rt.config.gateway_name)))

    rt.gw.connect = connect
    assert rt.start() == EXIT_STARTUP_FAILED
    msg = rt.notifier.find("启动失败")[0]
    assert "交易服务器登录成功=2.0s" in msg and "结算信息确认成功=5.0s" in msg and "合约信息查询成功" not in msg


def test_successful_startup_logs_total_elapsed_time(make):
    rt = make()
    logs = []
    rt.event_engine.register(EVENT_LOG, lambda e: logs.append(e.data.msg))
    assert rt.start() is None
    assert any("启动耗时" in m for m in logs)


def test_shutdown_logs_out_before_closing_the_gateway(make, monkeypatch):
    """vnpy_ctp 的 close() 只断开连接;退出前必须先发登出请求(国信「退出后是否正常注销」)。"""
    import trading.runtime as runtime_module
    order = []
    monkeypatch.setattr(runtime_module, "logout_ctp", lambda main, name, sleep=None: order.append("logout") or ["交易: 已发送登出请求"])
    rt = make()
    rt.start()
    original_close = rt.main.close
    rt.main.close = lambda: (order.append("close"), original_close())[1]
    rt.shutdown()
    assert order == ["logout", "close"]


def test_shutdown_survives_a_failing_logout(make, monkeypatch):
    import trading.runtime as runtime_module

    def boom(*a, **k):
        raise RuntimeError("logout broke")

    monkeypatch.setattr(runtime_module, "logout_ctp", boom)
    rt = make()
    rt.start()
    rt.shutdown()
    assert rt.gw.closed and rt.notifier.closed                              # 登出失败不能打断退出


def test_refuses_to_start_when_position_direction_is_unknown(make):
    """净持仓账户回报 Direction.NET:方向不明,不能按"非多即空"算净额,必须拒绝启动。"""
    rt = make()
    rt.gw.set_position(Direction.NET, 1)
    assert rt.start() == EXIT_RECONCILE_REFUSED
    assert any("方向不明" in m for m in rt.notifier.find("对账失败,拒绝启动"))


def test_position_drift_reports_unknown_direction_instead_of_miscounting(make):
    rt = make()
    assert rt.start() is None                                   # None = 启动成功,进入运行
    rt.gw.set_position(Direction.NET, 2)
    assert "方向不明" in (rt._position_drift() or "")
