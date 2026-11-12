"""账户风控在真实 vn.py MainEngine 上的离线端到端测试(假网关 + 同步事件引擎,不联网、不依赖 CTP)。"""
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import bootstrap  # noqa: E402,F401  (固定时区/工作目录,必须先于 vnpy 数据库相关导入)
from fakes import FakeGateway, SyncEventEngine  # noqa: E402
from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK  # noqa: E402
from trading.guard_rule import AccountGuardRule  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402
from vnpy.event import Event  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType  # noqa: E402
from vnpy.trader.event import EVENT_TIMER  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.object import CancelRequest, OrderRequest  # noqa: E402


@dataclass
class Clock:
    dt: datetime = datetime(2026, 11, 9, 10, 0)     # 周一日盘
    t: float = 1000.0

    def now(self) -> datetime:
        return self.dt

    def mono(self) -> float:
        return self.t


def make_req(offset=Offset.OPEN, direction=Direction.LONG, price=2200.0, volume=1) -> OrderRequest:
    return OrderRequest(symbol="c2601", exchange=Exchange.DCE, direction=direction, type=OrderType.LIMIT,
                        volume=volume, price=price, offset=offset)


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.clock = Clock()
        self.event_engine = SyncEventEngine()
        self.main = MainEngine(self.event_engine)
        self.gw: FakeGateway = self.main.add_gateway(FakeGateway)
        self.engine = self.main.add_app(SafeRiskManagerApp)
        self.rule: AccountGuardRule = self.engine.rules["账户风控"]
        self.state_file = tmp_path / "guard.json"
        self.rule.update_setting({"state_file": str(self.state_file)})
        self.rule.clock, self.rule.mono = self.clock.now, self.clock.mono
        self.alerts: list[dict] = []
        self.hard_locks: list[dict] = []
        self.event_engine.register(EVENT_GUARD_ALERT, lambda e: self.alerts.append(e.data))
        self.event_engine.register(EVENT_GUARD_HARD_LOCK, lambda e: self.hard_locks.append(e.data))
        self.gw.push_contract()
        self.gw.push_tick()
        self.gw.push_account(10_000)

    def send(self, req: OrderRequest) -> str:
        return self.main.send_order(req, "FAKE")

    def tick(self, seconds: float = 0.0) -> None:
        """像真实运行一样,通过事件引擎触发定时事件(处理函数运行期间产生的回报会排队到其返回之后)。"""
        self.clock.t += seconds
        self.event_engine.put(Event(EVENT_TIMER))

    def set_equity(self, balance: float) -> None:
        self.gw.push_account(balance)

    def alert_titles(self) -> list[str]:
        return [a["title"] for a in self.alerts]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.tick()                                         # 建立当日开盘权益 10000
    yield e
    e.main.close()


# ---------- 配置与基本放行 ----------

def test_settings_come_from_project_config(env):
    assert (env.rule.daily_loss_limit, env.rule.max_drawdown) == (600, 2000)
    assert env.engine.rules["委托规模检查"].order_volume_limit == 2
    assert env.engine.rules["活动委托检查"].active_order_limit == 10


def test_normal_open_order_goes_through(env):
    assert env.send(make_req())
    assert len(env.gw.sent) == 1


def test_builtin_rules_still_enforced(env):
    assert env.send(make_req(volume=3)) == ""                 # 超过单笔手数上限 2
    assert env.send(make_req(price=2200.5)) == ""             # 价格不是最小变动价位(1)的整数倍
    assert env.gw.sent == []


# ---------- 当日熔断 ----------

def test_daily_breaker_blocks_opens_but_allows_closes_and_flattens(env):
    env.gw.mode = "fill"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_390)                                      # 较开盘亏 610 ≥ 600
    env.tick()
    assert "当日熔断" in env.alert_titles()
    assert env.gw.positions[Direction.LONG] == 0               # 已自动平仓
    assert env.gw.sent[-1].offset == Offset.CLOSE and env.gw.sent[-1].direction == Direction.SHORT
    n = len(env.gw.sent)
    assert env.send(make_req()) == ""                          # 新开仓被拦截
    assert len(env.gw.sent) == n
    assert env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))      # 平仓永远放行
    env.tick(seconds=1)                                        # 成交已覆盖持仓:主动查询持仓(而不是重发)
    env.tick(seconds=1)                                        # 持仓回报清零 → 完成
    assert "紧急平仓完成" in env.alert_titles()
    assert env.gw.position_queries >= 1


def test_daily_lock_cancels_pending_open_orders_but_not_close_orders(env):
    open_id = env.send(make_req())
    close_id = env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))
    env.set_equity(9_300)
    env.tick()
    cancelled = {r.orderid for r in env.gw.cancel_requests}
    assert open_id.split(".")[-1] in cancelled
    assert close_id.split(".")[-1] not in cancelled


def test_flatten_can_be_disabled_for_daily_lock(env):
    env.rule.flatten_on_daily_lock = False
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert env.gw.sent == []                                   # 只禁止开仓,不平仓
    assert env.send(make_req()) == ""


def test_zero_equity_sample_does_not_trip_breaker(env):
    env.set_equity(0)
    env.tick()
    assert env.send(make_req())


def test_daily_lock_clears_next_trading_day(env):
    env.set_equity(9_300)
    env.tick()
    assert env.send(make_req()) == ""
    env.clock.dt = datetime(2026, 11, 10, 10, 0)
    env.set_equity(9_300)
    env.tick()
    assert env.send(make_req())


# ---------- 累计死线 ----------

def test_hard_lock_flattens_then_emits_event_and_persists(env):
    env.gw.mode = "fill"
    env.gw.set_position(Direction.SHORT, 1)
    env.set_equity(7_900)                                      # 回撤 2100 ≥ 2000
    env.tick()
    assert "累计死线触发" in env.alert_titles()
    assert env.hard_locks == []                                # 还没确认空仓
    env.tick(seconds=1)
    env.tick(seconds=1)
    assert env.hard_locks == [{"flat": True, "reason": env.rule.guard.hard_reason}]
    assert env.gw.sent[-1].direction == Direction.LONG and env.gw.sent[-1].offset == Offset.CLOSE
    assert env.state_file.exists()
    env.tick()
    assert len(env.hard_locks) == 1                            # 事件只发一次


def test_hard_lock_survives_restart(env):
    env.set_equity(7_900)
    env.tick()
    restarted = AccountGuardRule(env.engine, {"state_file": str(env.state_file)})
    restarted.clock = env.clock.now
    assert restarted.check_allowed(make_req(), "FAKE") is False
    assert restarted.check_allowed(make_req(offset=Offset.CLOSE, direction=Direction.SHORT), "FAKE") is True


# ---------- 紧急平仓的重试安全 ----------

def test_flatten_never_resends_while_order_is_active_then_cancels_after_timeout(env):
    env.gw.mode = "accept"                                     # 平仓委托挂着不成交
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert len(env.gw.sent) == 1
    env.tick(seconds=5)                                        # 订单仍在途,未超时
    assert len(env.gw.sent) == 1 and env.gw.cancel_requests == []
    env.tick(seconds=10)                                       # 超过平仓回报超时:撤掉自己的在途平仓单
    assert len(env.gw.cancel_requests) == 1 and len(env.gw.sent) == 1
    env.tick(seconds=1)                                        # 已终态 → 按节奏重试
    env.tick(seconds=10)
    assert len(env.gw.sent) >= 2


def test_flatten_retries_are_bounded_then_escalate(env):
    env.gw.mode = "accept"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(7_900)                                      # 硬锁定,平仓一直失败
    for _ in range(60):
        env.tick(seconds=11)                                   # 每轮都超过回报超时 → 撤单 → 终态 → 下一次重试
        env.tick(seconds=6)
    assert len(env.gw.sent) == 4                               # 首次 + 3 次重试,不多不少
    assert "紧急平仓失败" in env.alert_titles()
    assert env.hard_locks and env.hard_locks[0]["flat"] is False


def test_flatten_with_no_acknowledgement_never_resends(env):
    """网络抖动、委托回报丢失:状态未知时绝不重发(可能已被接受),超时后升级人工处理。"""
    env.gw.mode = "silent"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert len(env.gw.sent) == 1
    env.tick(seconds=11)
    env.tick(seconds=100)
    assert len(env.gw.sent) == 1
    assert "紧急平仓失败" in env.alert_titles()


def test_flatten_hard_reject_escalates_immediately_without_retry(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    env.tick(seconds=30)
    assert len(env.gw.sent) == 1
    assert "紧急平仓失败" in env.alert_titles()


def test_flatten_without_tick_data_is_reported_not_silent(env):
    env.main.engines["oms"].ticks.clear()                      # 没有行情,无法定价
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert "平仓缺少行情" in env.alert_titles() and env.gw.sent == []


# ---------- 废单与超时委托 ----------

def test_unknown_reject_text_locks_opening_for_the_day(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "某个从未见过的错误"
    env.send(make_req())
    env.tick(seconds=2)                                        # 等分类延迟过去(CTP 的错误日志晚于废单回报)
    assert "委托被拒" in env.alert_titles()
    env.gw.mode = "accept"
    assert env.send(make_req()) == ""                          # 当日禁止开仓


def test_open_orders_are_never_retried_after_reject(env):
    env.gw.mode = "reject"
    env.send(make_req())
    env.tick(seconds=30)
    assert len(env.gw.sent) == 1


def test_soft_rejects_lock_only_after_a_storm(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "网络超时,请稍后重试"
    env.send(make_req(price=2200)), env.send(make_req(price=2201))
    env.tick(seconds=2)
    env.gw.mode = "accept"
    assert env.send(make_req(price=2202))                      # 2 次软拒绝:仍可开仓
    env.gw.mode = "reject"
    env.send(make_req(price=2203))                             # 第 3 次 → 风暴
    env.tick(seconds=2)
    env.gw.mode = "accept"
    assert env.send(make_req(price=2204)) == ""


def test_close_order_reject_does_not_lock_opening(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "未知错误"
    env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))
    env.gw.mode = "accept"
    assert env.send(make_req())


def test_hard_reject_emits_fatal_alert_with_error_code_and_locks(env):
    """硬拒绝要给出「严重错误,已停止策略开仓」的 FATAL 告警并带 CTP 错误码,不淹没在普通拒单告警里。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足，代码：21，信息：投资者资金不足"
    env.send(make_req())
    env.tick(seconds=2)
    fatal = [a for a in env.alerts if a["level"] == "FATAL" and a["title"] == "严重错误,已停止策略开仓"]
    assert len(fatal) == 1
    assert "21" in fatal[0]["message"] and "资金不足" in fatal[0]["message"]
    env.gw.mode = "accept"
    assert env.send(make_req()) == ""                          # 当日已禁止开仓


def test_hard_reject_without_error_code_still_emits_fatal(env):
    """没有数字错误码时 fail-closed 仍按硬拒绝,FATAL 告警照发,只是不带错误码段。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "某个从未见过的硬错误"
    env.send(make_req())
    env.tick(seconds=2)
    fatal = [a for a in env.alerts if a["title"] == "严重错误,已停止策略开仓"]
    assert len(fatal) == 1 and "错误码" not in fatal[0]["message"]


def test_repeated_hard_rejects_after_lock_emit_a_single_fatal(env):
    """锁定转换只发生一次:分类延迟窗口内多笔在途单陆续被判 HARD 时,FATAL 与撤单措辞只能出现一次。

    codex 第二轮审查动态复现:旧实现把 FATAL 放在 _lock_daily 之前无条件发射,
    已锁定后第二笔硬拒绝会再刷一条声称「已撤销全部挂单」的 FATAL,但实际并无撤单动作。
    """
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足，代码：21，信息：投资者资金不足"
    env.send(make_req(price=2200)), env.send(make_req(price=2201))       # 两笔在锁定前都已被柜台拒绝
    env.tick(seconds=2)                                                   # 同一个定时周期完成两笔分类
    fatal = [a for a in env.alerts if a["title"] == "严重错误,已停止策略开仓"]
    assert len(fatal) == 1
    assert env.alert_titles().count("当日禁止开仓") == 1
    env.gw.mode = "accept"
    assert env.send(make_req(price=2202)) == ""                          # 锁定本身仍然有效


def test_soft_reject_storm_does_not_emit_severe_fatal(env):
    """软拒绝攒成风暴只触发「当日禁止开仓」,不能误报「严重错误,已停止策略开仓」。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "网络超时,请稍后重试"
    for price in (2200, 2201, 2202):
        env.send(make_req(price=price))
    env.tick(seconds=2)
    assert "严重错误,已停止策略开仓" not in env.alert_titles()
    assert "当日禁止开仓" in env.alert_titles()


def test_close_order_reject_never_says_strategy_stopped(env):
    """平仓委托被拒永远不锁离场,也不得出现「已停止」措辞(离场通道不能被误报成策略停手)。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足，代码：21，信息：投资者资金不足"
    env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))
    env.tick(seconds=2)
    assert all("已停止" not in a["title"] for a in env.alerts)
    env.gw.mode = "accept"
    assert env.send(make_req())                               # 开仓能力不受影响


def test_stale_open_order_is_cancelled_but_close_order_is_not(env):
    open_id = env.send(make_req())
    close_id = env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))
    env.tick(seconds=60)
    assert env.gw.cancel_requests == []
    env.tick(seconds=61)                                       # 累计 121s ≥ 120s
    cancelled = [r.orderid for r in env.gw.cancel_requests]
    assert cancelled == [open_id.split(".")[-1]]
    assert close_id.split(".")[-1] not in cancelled


def test_excessive_cancels_lock_opening(env):
    env.rule.cancel_lock_limit = 2
    ids = [env.send(make_req(price=2200 + i)) for i in range(2)]
    for vt in ids:
        env.main.cancel_order(CancelRequest(orderid=vt.split(".")[-1], symbol="c2601", exchange=Exchange.DCE), "FAKE")
    assert env.rule.cancel_count == 2
    assert env.send(make_req(price=2300)) == ""


# ---------- 审查后补充:CTP 真实行为与卡死路径 ----------

def test_reject_classification_uses_error_log_that_arrives_after_the_reject_event(env):
    """CTP 先推废单回报、后写错误日志:分类必须用到后到的日志,而不是回报到达时的"最近错误"。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    env.send(make_req())
    assert env.alerts == []                                    # 刚收到废单,还在等错误日志,不下结论
    env.tick(seconds=2)
    msgs = [a["message"] for a in env.alerts if a["title"] == "委托被拒"]
    assert msgs and "资金不足" in msgs[0] and "HARD" in msgs[0]


def test_flatten_does_not_resend_when_oms_position_stays_stale(env):
    """成交已覆盖持仓、但持仓回报一直不清零(CTP 刷新滞后/丢失):绝不重发平仓单,主动查询后超时升级人工。"""
    env.gw.mode = "fill"
    env.gw.set_position(Direction.LONG, 1)
    env.gw.query_position = lambda: None                       # 持仓查询也得不到刷新
    env.set_equity(9_300)
    env.tick()
    for _ in range(12):
        env.tick(seconds=1)
    assert len(env.gw.sent) == 1                               # 只发过一笔平仓单
    assert "紧急平仓失败" in env.alert_titles()
    assert any("持仓回报" in a["message"] for a in env.alerts if a["title"] == "紧急平仓失败")


def test_failed_daily_flatten_is_revived_by_hard_lock(env):
    """当日熔断的平仓失败后卡死的状态机,不能吞掉随后的硬锁定:硬锁定要重新开一轮平仓。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_390)
    env.tick()
    env.tick(seconds=2)                                        # 平仓单被硬拒绝 → 升级失败
    assert "紧急平仓失败" in env.alert_titles() and env.hard_locks == []

    env.gw.mode = "fill"
    env.set_equity(7_900)                                      # 硬锁定
    env.tick()
    env.tick(seconds=1)
    env.tick(seconds=1)
    assert env.gw.positions[Direction.LONG] == 0               # 新一轮平仓成功
    assert env.hard_locks and env.hard_locks[0]["flat"] is True


def test_flatten_reports_when_order_splitter_returns_nothing(env):
    """上海/能源中心合约:仓位被冻结时拆分器返回空,不能当作已发出。"""
    env.main.convert_order_request = lambda *a, **k: []
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert "平仓请求被拆分器拒绝" in env.alert_titles() and env.gw.sent == []


@pytest.mark.parametrize("bad_price", [float("nan"), 0.0])
def test_flatten_with_invalid_last_price_is_reported(env, bad_price):
    env.gw.push_tick(last=bad_price)
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(9_300)
    env.tick()
    assert "平仓缺少行情" in env.alert_titles() and env.gw.sent == []


def test_cancel_is_retried_when_exchange_never_confirms(env):
    env.gw.cancel_works = False                                # 撤单失败:只有错误日志,委托一直是活动状态
    oid = env.send(make_req())
    env.rule._cancel_order(oid, 1000.0)
    env.rule._cancel_order(oid, 1005.0)                        # 10 秒内不重复请求
    assert len(env.gw.cancel_requests) == 1
    env.rule._cancel_order(oid, 1011.0)                        # 超过 10 秒仍未终结 → 再撤一次
    assert len(env.gw.cancel_requests) == 2


def test_orders_existing_before_tracking_start_get_timeout_management(env):
    oid = env.send(make_req())
    env.rule._open_orders.clear()                              # 模拟:规则启动前就已存在的活跃委托
    env.tick(seconds=1)                                        # 被纳入跟踪(从此刻计时)
    env.tick(seconds=121)
    assert [r.orderid for r in env.gw.cancel_requests] == [oid.split(".")[-1]]


def test_state_file_is_anchored_to_project_root_not_cwd(env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                                # 换一个启动目录
    env.rule.update_setting({"state_file": "data/state/x.json"})
    assert env.rule.guard.state_path == ROOT / "data" / "state" / "x.json"


def test_state_write_failure_is_alerted_not_raised(env, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                                    # 把"目录"位置占成文件,mkdir 必然失败
    env.rule.update_setting({"state_file": str(blocker / "g.json")})
    env.set_equity(9_800)
    env.tick()                                                 # 不能抛出异常中断定时器
    assert "风控状态写盘失败" in env.alert_titles()
    assert env.send(make_req())                                # 熔断逻辑仍在内存中正常工作


def test_equity_is_read_from_the_configured_account_only(env):
    from vnpy.trader.object import AccountData
    env.gw.on_account(AccountData(accountid="OTHER", balance=100.0, frozen=0.0, gateway_name="FAKE"))
    env.rule.update_setting({"account_id": "SIM", "state_file": str(env.state_file)})
    env.tick()
    assert env.send(make_req())                                # 另一个账户的低余额不会触发熔断


def test_close_reject_storm_alerts_but_never_locks(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "平仓量超过持仓量"
    for i in range(5):
        env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT, price=2200 + i))
    env.tick(seconds=2)
    assert "平仓委托频繁被拒" in env.alert_titles()
    env.gw.mode = "accept"
    assert env.send(make_req())                                # 平仓被拒不影响开仓,也绝不会锁住离场


# ---------- codex 第二轮审查后补充 ----------

def test_empty_positions_at_hard_lock_are_not_trusted_until_a_position_query_has_had_time(env):
    """启动早期持仓回报还没到:OMS 为空不能当作已空仓(否则会带着真实持仓以退出码 0 退出)。"""
    env.gw.query_position = lambda: None                       # 持仓回报还没到
    env.set_equity(7_900)                                      # 硬锁定,此时 OMS 里没有任何持仓
    env.tick()
    env.tick(seconds=2)
    assert env.hard_locks == []                                # 还不能宣布空仓
    env.gw.set_position(Direction.LONG, 1)                     # 持仓回报迟到了:账户其实有 1 手
    del env.gw.query_position                                  # 恢复真实的持仓查询(平仓成交后要靠它刷新持仓)
    env.gw.mode = "fill"
    env.tick(seconds=1)
    env.tick(seconds=1)
    env.tick(seconds=1)
    assert env.gw.positions[Direction.LONG] == 0               # 迟到的持仓被平掉
    assert env.hard_locks and env.hard_locks[0]["flat"] is True


def test_genuinely_empty_account_is_declared_flat_after_the_refresh_wait(env):
    env.set_equity(7_900)
    env.tick()
    assert env.hard_locks == [] and env.gw.position_queries >= 1               # 先主动查询
    env.tick(seconds=3)
    assert env.hard_locks == []                                                # 等待期内不宣布
    env.tick(seconds=4)                                                        # 超过 position_refresh_timeout(6s)
    assert env.hard_locks and env.hard_locks[0]["flat"] is True


def test_emergency_flatten_can_restart_after_a_failed_hard_lock_flatten(env):
    """硬锁定的平仓失败后,之后的紧急平仓(如策略崩溃)必须能重开一轮;成功后要补发 flat=True 让进程退出。"""
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    env.gw.set_position(Direction.LONG, 1)
    env.set_equity(7_900)
    env.tick()
    env.tick(seconds=2)
    assert [h["flat"] for h in env.hard_locks] == [False]                      # 平仓失败:进程保持存活

    env.gw.mode = "fill"
    env.rule.emergency_flatten("策略崩溃")                                      # 新的紧急平仓请求
    env.tick(seconds=1)
    env.tick(seconds=1)
    assert env.gw.positions[Direction.LONG] == 0
    assert [h["flat"] for h in env.hard_locks] == [False, True]                # 之后成功:补发 flat=True


def test_errors_from_non_gateway_sources_are_not_used_to_classify_rejects(env):
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import ERROR
    from vnpy.trader.object import LogData
    env.gw.mode = "reject"
    env.gw.reject_text = "网络超时"                                             # 这是网关的真实错误(软)
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="通知发送异常: 资金不足", level=ERROR, gateway_name="Runtime")))
    env.send(make_req())
    env.tick(seconds=2)
    msg = [a["message"] for a in env.alerts if a["title"] == "委托被拒"][0]
    assert "网络超时" in msg and "SOFT" in msg                                  # 用的是网关日志,没有被 Runtime 的日志污染


def test_reject_reason_is_found_even_though_ctp_logs_it_at_info_level(env):
    """真实 vnpy_ctp 把拒单写成 INFO 级别(vnpy_ctp 的 write_error 实际调用 write_log)。

    2026-11-10 在 SimNow 上实测确认:拒单文本是"交易委托失败，代码：21，信息：...",
    级别 INFO。若只认 ERROR 级别,废单分类永远拿不到原因(一律按硬拒绝),
    紧急平仓单的有限重试也就永远用不上。
    """
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import INFO
    from vnpy.trader.object import LogData
    env.gw.mode = "accept"
    env.send(make_req())
    env.tick(seconds=1)
    env.gw.mode = "reject"
    env.gw.reject_text = "网络超时"
    # 手工补一条 INFO 级别的拒单日志,模拟 vnpy_ctp 的行为
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="交易委托失败,信息:网络超时", level=INFO, gateway_name="FAKE")))
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="其它无关的 INFO 日志", level=INFO, gateway_name="FAKE")))
    env.gw.mode = "reject"
    env.send(make_req())
    env.tick(seconds=2)
    rejects = [a for a in env.alerts if a["title"] == "委托被拒"]
    assert rejects, "应该收到拒单告警"
    assert "网络超时" in rejects[-1]["message"] and "SOFT" in rejects[-1]["message"]


def test_unrelated_gateway_info_logs_are_not_used_as_reject_reasons(env):
    """网关的 INFO 级日志里混着"行情订阅失败""行情服务器登录失败"这类无关文本。

    它们不是废单原因:真废单的日志迟到或缺失时,拿这些文本去分类会把可恢复的软拒绝
    误判成硬拒绝,紧急平仓的有限重试就永远用不上。
    """
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import INFO
    from vnpy.trader.object import LogData
    env.gw.mode = "reject"
    env.gw.reject_text = "网络超时"                                       # 网关的真实废单原因(软)
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="行情订阅失败", level=INFO, gateway_name="FAKE")))
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="行情服务器登录失败", level=INFO, gateway_name="FAKE")))
    env.send(make_req())
    env.tick(seconds=2)
    msg = [a["message"] for a in env.alerts if a["title"] == "委托被拒"][0]
    assert "行情订阅失败" not in msg and "行情服务器登录失败" not in msg


def test_exchange_level_reject_reason_is_found(env):
    """交易所级拒单走 onRtnOrder,原因在 StatusMsg 里,日志措辞是"委托 X 状态更新，<原因>"。

    这条路径不经过 onRspOrderInsert,"交易委托失败"收不到它;漏掉的话交易所拒单的原因
    就永远是"未知"→ 一律按硬拒绝 → 平仓单被提前升级为人工处理,丧失重试机会。
    """
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import INFO
    from vnpy.trader.object import LogData
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    env.event_engine.put(Event(EVENT_LOG, LogData(msg="委托 1_1_5 状态更新,网络繁忙",
                                                   level=INFO, gateway_name="FAKE")))
    env.send(make_req())
    env.tick(seconds=2)
    msg = [a["message"] for a in env.alerts if a["title"] == "委托被拒"][0]
    assert "网络繁忙" in msg, "应该拿到交易所级拒单的真实原因"


def test_reject_reason_is_taken_from_the_same_gateway_only(env):
    """多网关时不能拿 A 网关的错误去解释 B 网关的废单。"""
    env.rule._errors.append((env.clock.mono(), "交易委托失败,信息:另一网关的原因", "OTHER"))
    env.rule._errors.append((env.clock.mono(), "交易委托失败,信息:本网关的原因", "FAKE"))
    assert env.rule._error_text_near(env.clock.mono(), "FAKE") == "交易委托失败,信息:本网关的原因"
    assert env.rule._error_text_near(env.clock.mono(), "OTHER") == "交易委托失败,信息:另一网关的原因"


def test_retry_volume_never_exceeds_current_oms_position(env):
    key = ("c2601.DCE", Direction.LONG)
    env.gw.set_position(Direction.LONG, 1)
    positions = env.rule._oms_positions()
    env.rule._send_close_orders({key: 2.0}, positions)                         # 目标量 2,但账户当前只有 1 手(人工平过一部分)
    assert env.gw.sent[-1].volume == 1


# ---------- codex 第三轮复核后补充 ----------

def test_reopened_flatten_round_first_clears_leftover_active_flatten_orders(env):
    """失败的平仓轮被丢弃后,旧的平仓单可能还挂着:重开一轮必须先撤掉并等它终结,否则两轮平仓单可能都成交。"""
    env.gw.mode = "accept"
    env.gw.set_position(Direction.LONG, 1)
    env.rule.emergency_flatten("第一次")
    assert len(env.gw.sent) == 1
    env.rule._flatten["failed"] = True                         # 模拟:第一轮被升级为失败,但委托仍在交易所挂着
    env.gw.mode = "fill"
    env.rule.emergency_flatten("第二次")                       # 重开一轮
    assert len(env.gw.sent) == 1                               # 旧单还活着,不能立刻再发
    assert len(env.gw.cancel_requests) == 1                    # 先撤旧单
    env.tick(seconds=1)                                        # 旧单已终结 → 现在才发新的
    assert len(env.gw.sent) == 2


def test_finishing_a_flatten_cancels_residual_flatten_orders(env):
    env.gw.mode = "accept"
    env.gw.set_position(Direction.LONG, 1)
    env.rule.emergency_flatten("test")
    assert len(env.gw.sent) == 1 and not env.gw.cancel_requests
    env.gw.set_position(Direction.LONG, 0)                     # OMS 先归零(例如人工平掉),平仓单还挂着
    env.tick(seconds=1)
    assert len(env.gw.cancel_requests) == 1                    # 完成时撤掉残留的平仓单,不留给交易所"超平拒单"兜底


def test_rule_resets_baselines_when_the_connected_account_changes(env):
    from vnpy.trader.object import AccountData
    env.gw.on_account(AccountData(accountid="SIM", balance=20_000_000.0, frozen=0.0, gateway_name="FAKE"))
    env.rule.update_setting({"account_id": "SIM", "state_file": str(env.state_file)})
    env.tick()
    assert env.rule.guard.peak_equity == 20_000_000
    env.rule.update_setting({"account_id": "REAL", "state_file": str(env.state_file)})
    env.gw.on_account(AccountData(accountid="REAL", balance=10_000.0, frozen=0.0, gateway_name="FAKE"))
    env.tick()
    assert "账户变更" in env.alert_titles()
    assert env.rule.guard.peak_equity == 10_000 and not env.rule.guard.hard_locked   # 没有被 2000 万的旧最高点误判为大幅回撤
    assert env.send(make_req())


# ---------- 平仓不被计数类规则拦截 ----------

def test_counting_rules_block_opens_but_never_closes(env):
    daily = env.engine.rules["每日上限检查"]
    daily.total_order_count = daily.total_order_limit          # 触顶
    assert env.send(make_req()) == ""
    assert env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT))


def test_closing_orders_still_must_be_valid(env):
    """平仓单只跳过计数类规则,价格不合法等合法性检查仍然生效。"""
    assert env.send(make_req(offset=Offset.CLOSE, direction=Direction.SHORT, price=2200.5)) == ""


def test_status_variable_reflects_lock(env):
    assert env.rule.lock_status == "OK"
    env.set_equity(9_300)
    env.tick()
    assert env.rule.lock_status == "DAILY_LOCKED"
    assert env.rule.peak_equity == 10_000 and env.rule.day_start_equity == 10_000
    _ = date  # noqa


def test_flatten_escalates_instead_of_guessing_when_position_direction_is_unknown(env):
    """方向不明(净持仓)时不能按"非多即空"猜平仓方向:一笔平仓单都不发,直接升级人工并保持存活。"""
    env.gw.mode = "fill"
    env.gw.set_position(Direction.NET, 1)
    env.set_equity(7_900)
    env.tick()
    env.tick(seconds=1)
    env.tick(seconds=1)
    assert env.gw.sent == []
    assert "紧急平仓失败" in env.alert_titles()
    assert env.hard_locks and all(not h["flat"] for h in env.hard_locks)     # flat=False:进程保持存活继续告警,绝不当作已空仓


def test_send_close_orders_refuses_undirected_position_on_its_own(env):
    """纵深防御:即使调用方没拦截,发单函数自己也不能把方向不明的持仓推导成"非多即空"。"""
    env.gw.set_position(Direction.NET, 1)
    positions = env.rule._oms_positions()
    sent = env.rule._send_close_orders({key: 1 for key in positions}, positions)
    assert sent == {} and env.gw.sent == []
    assert "平仓方向不明" in env.alert_titles()


def test_mixed_directed_and_undirected_positions_send_no_close_orders(env):
    """宁可整体升级人工也不对一部分仓位自动平:账户形态不明时,已知方向的仓位也不动(取舍记录在 ADR)。"""
    env.gw.mode = "fill"
    env.gw.set_position(Direction.LONG, 1)
    env.gw.set_position(Direction.NET, 1, symbol="c2605")
    env.set_equity(7_900)
    env.tick()
    env.tick(seconds=1)
    assert env.gw.sent == [] and "紧急平仓失败" in env.alert_titles()


def test_undirected_position_appearing_mid_flatten_cancels_the_resting_close_order(env):
    """平仓进行到一半出现方向不明持仓:状态机升级后不再观察,已挂出的平仓单必须先撤掉。"""
    env.gw.mode = "accept"                                     # 平仓单挂着不成交
    env.gw.set_position(Direction.SHORT, 1)
    env.set_equity(7_900)
    env.tick()
    assert env.gw.sent and env.gw.sent[-1].offset == Offset.CLOSE
    env.gw.set_position(Direction.NET, 1)
    env.tick(seconds=1)
    assert env.gw.cancel_requests                               # 挂着的平仓单被撤
    assert "紧急平仓失败" in env.alert_titles()
