"""穿透测试演练脚本的离线测试(不连 CTP、不联网):目标闸门、脱敏、里程碑判断、步骤编排。"""
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import penetration_check as pc  # noqa: E402  (内部会先 import bootstrap)
from collect_reject_texts import Collector  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.object import OrderData, TickData  # noqa: E402

SIMNOW = "182.254.243.31:30001"
ALL_LOGS = [text for _key, text in pc.MILESTONES]


def test_gate_requires_confirm_flag():
    ok, note = pc.check_target("测试", "10.0.0.1:41205", confirmed=False)
    assert not ok and "--confirm-broker-test" in note


def test_gate_allows_broker_test_environment():
    ok, note = pc.check_target("测试", "tcp://10.0.0.1:41205", confirmed=True)
    assert ok and "测试环境" in note


def test_gate_allows_simnow_rehearsal_even_in_production_mode():
    ok, note = pc.check_target("实盘", SIMNOW, confirmed=True)
    assert ok and "SimNow" in note


def test_gate_refuses_production_api_with_no_override():
    ok, note = pc.check_target("实盘", "10.0.0.1:41205", confirmed=True)
    assert not ok and "生产 API" in note


def test_redact_replaces_all_secrets_and_ignores_short_values():
    text = "登录 user123 密码 hunter22 授权 AUTHCODE9 完成 ab"
    out = pc.redact(text, ["hunter22", "AUTHCODE9", "user123", "ab", ""])
    assert "hunter22" not in out and "AUTHCODE9" not in out and "user123" not in out
    assert out.endswith("完成 ab")  # 太短的值不替换,避免误伤


def test_redact_longer_secret_wins_over_its_prefix():
    out = pc.redact("token=abcdef", ["abc", "abcdef"])
    assert out == "token=***"


@pytest.mark.parametrize("account,expected", [("123456789", "12*****89"), ("1234", "****"), ("", "")])
def test_mask_account(account, expected):
    assert pc.mask_account(account) == expected


def test_milestone_status_and_failures():
    status = pc.milestone_status(["交易服务器连接成功", "交易服务器授权验证成功"])
    assert status["td_connected"] and status["td_auth"] and not status["td_login"]
    assert pc.failure_lines(["交易服务器授权验证失败，代码：63，信息：x", "交易服务器连接成功"]) == [
        "交易服务器授权验证失败，代码：63，信息：x"]


def test_limit_down_price_alignment_and_missing():
    assert pc.limit_down_price(2037.0, 1.0) == 2037.0
    assert pc.limit_down_price(2037.3, 1.0) == 2037.0
    assert pc.limit_down_price(0, 1.0) == 0.0


def make_collector(logs, with_contract=True, with_tick=True):
    collector = Collector()
    collector.log_lines.extend(logs)
    if with_contract:
        collector.contracts["c2701.DCE"] = SimpleNamespace(
            symbol="c2701", exchange=Exchange.DCE, pricetick=1.0, max_volume=1000)
    if with_tick:
        collector.ticks["c2701.DCE"] = TickData(
            gateway_name="CTP", symbol="c2701", exchange=Exchange.DCE, datetime=datetime.now(),
            last_price=2200.0, limit_down=2100.0)
    return collector


class StubGateway:
    def __init__(self, connected=True):
        # 与真实 vnpy_ctp 一致:断线只清 login_status,connect_status 一直是 True
        self.td_api = SimpleNamespace(connect_status=True, login_status=connected)

    def query_account(self): ...
    def query_position(self): ...


class StubEngine:
    """order_mode: nottraded(挂单后可撤) / rejected / silent(无回报)"""

    def __init__(self, collector, order_mode="nottraded", accounts=True, connected=True):
        self.connected = connected
        self.collector = collector
        self.order_mode = order_mode
        self.accounts = accounts
        self.sent = []
        self.cancelled = []

    def subscribe(self, req, gateway): ...
    def get_gateway(self, name): return StubGateway(self.connected)
    def get_all_accounts(self): return [object()] if self.accounts else []
    def get_all_positions(self): return []

    def send_order(self, req, gateway):
        self.sent.append(req)
        oid = "CTP.1"
        status = {"nottraded": Status.NOTTRADED, "rejected": Status.REJECTED,
                  "partial": Status.PARTTRADED}.get(self.order_mode)
        if status is not None:
            self.collector.orders[oid] = OrderData(
                gateway_name="CTP", symbol=req.symbol, exchange=req.exchange, orderid="1",
                direction=req.direction, offset=req.offset, price=req.price, volume=req.volume, status=status,
                traded=1 if self.order_mode == "partial" else 0)
        return oid

    def cancel_order(self, req, gateway):
        self.cancelled.append(req)
        order = self.collector.orders.get("CTP.1")
        if order is not None:
            order.status = Status.CANCELLED


def args(**kw):
    base = dict(connect_timeout=1.0, tick_timeout=0.5, position_wait=0.5, with_order=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_run_checks_all_pass_without_order(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: True)
    collector = make_collector(ALL_LOGS)
    run, code = pc.run_checks(args(), "c2701.DCE", StubEngine(collector), collector)
    assert code == pc.EXIT_OK and not run.failed()
    assert {s.name: s.status for s in run.steps}["报单/撤单"] == "SKIP"


def test_run_checks_stops_on_auth_failure():
    collector = make_collector(["交易服务器连接成功", "交易服务器授权验证失败，代码：63，信息：bad"], with_contract=False)
    run, code = pc.run_checks(args(), "c2701.DCE", StubEngine(collector), collector)
    assert code == pc.EXIT_LOGIN and run.failed()
    assert any("代码：63" in s.detail for s in run.steps if s.status == "FAIL")


def test_off_session_without_tick_is_a_warning_not_a_failure(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: False)
    collector = make_collector(ALL_LOGS, with_tick=False)
    run, code = pc.run_checks(args(), "c2701.DCE", StubEngine(collector), collector)
    assert {s.name: s.status for s in run.steps}["订阅行情"] == "WARN" and not run.failed()


def test_in_session_without_tick_fails(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: True)
    collector = make_collector(ALL_LOGS, with_tick=False)
    run, _ = pc.run_checks(args(), "c2701.DCE", StubEngine(collector), collector)
    assert {s.name: s.status for s in run.steps}["订阅行情"] == "FAIL"


def test_order_and_cancel_flow_uses_limit_down_buy(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: True)
    collector = make_collector(ALL_LOGS)
    engine = StubEngine(collector)
    run, code = pc.run_checks(args(with_order=True), "c2701.DCE", engine, collector)
    names = {s.name: s.status for s in run.steps}
    assert names["报单"] == "PASS" and names["撤单"] == "PASS" and code == pc.EXIT_OK
    assert engine.sent[0].price == 2100.0 and engine.sent[0].direction == Direction.LONG
    assert engine.sent[0].offset == Offset.OPEN and engine.sent[0].type == OrderType.LIMIT
    assert len(engine.cancelled) == 1


def test_rejected_order_is_reported_as_failure(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: True)
    collector = make_collector(ALL_LOGS)
    run, code = pc.run_checks(args(with_order=True), "c2701.DCE",
                              StubEngine(collector, order_mode="rejected"), collector)
    assert {s.name: s.status for s in run.steps}["报单"] == "FAIL" and code == pc.EXIT_STEP_FAILED


def test_order_skipped_off_session(monkeypatch):
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: False)
    collector = make_collector(ALL_LOGS)
    engine = StubEngine(collector)
    run, _ = pc.run_checks(args(with_order=True), "c2701.DCE", engine, collector)
    assert not engine.sent and {s.name: s.status for s in run.steps}["报单/撤单"] == "SKIP"


def test_report_is_redacted():
    run = pc.Run()
    run.add("交易登录", "FAIL", "用户 user123 登录失败")
    meta = {"time": "t", "target": "x", "account": "us***23", "symbol": "c2701.DCE",
            "vnpy": "4.5.0", "vnpy_ctp": "6.7", "python": "3.13", "host": "H"}
    report = pc.render_report(run, meta, ["密码 hunter22 错误"], ["user123", "hunter22"])
    assert "user123" not in report and "hunter22" not in report and "***" in report


def test_partial_fill_then_cancel_is_a_failure_with_position_exit_code(monkeypatch):
    """部分成交后撤单成功:状态是 CANCELLED,但已留下持仓,不能判撤单 PASS。"""
    monkeypatch.setattr(pc, "in_trading_session", lambda dt: True)
    collector = make_collector(ALL_LOGS)
    run, code = pc.run_checks(args(with_order=True), "c2701.DCE",
                              StubEngine(collector, order_mode="partial"), collector)
    assert {s.name: s.status for s in run.steps}["撤单"] == "FAIL" and code == pc.EXIT_POSITION


def test_submitting_timeout_still_sends_a_cancel(monkeypatch):
    """回报迟迟不来也要发撤单兜底,不能放着一张可能进交易所的委托。"""
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    ticks = iter(range(0, 10000))
    monkeypatch.setattr(pc.time, "monotonic", lambda: next(ticks) * 0.5)
    collector = make_collector(ALL_LOGS)
    engine = StubEngine(collector, order_mode="silent")
    monkeypatch.setattr(collector, "wait_order_final", lambda *a, **k: None)
    run = pc.Run()
    code = pc.do_order_cancel(run, engine, collector, collector.contracts["c2701.DCE"],
                              collector.ticks["c2701.DCE"])
    assert len(engine.cancelled) == 1 and engine.cancelled[0].orderid == "1"
    assert {s.name: s.status for s in run.steps}["报单"] == "FAIL" and code == pc.EXIT_STEP_FAILED


def test_disconnected_trade_channel_fails_even_though_connect_status_is_true():
    collector = make_collector(ALL_LOGS)
    run, code = pc.run_checks(args(), "c2701.DCE", StubEngine(collector, connected=False), collector)
    assert code == pc.EXIT_LOGIN and {s.name: s.status for s in run.steps}["交易通道在线"] == "FAIL"


def test_limit_down_must_be_below_last_price():
    assert pc.limit_down_price(2100.0, 1.0, last_price=2200.0) == 2100.0
    assert pc.limit_down_price(9999999.0, 1.0, last_price=2200.0) == 0.0   # 无涨跌停品种的异常值
    assert pc.limit_down_price(2200.0, 1.0, last_price=2200.0) == 0.0


def test_short_accounts_are_fully_masked():
    assert pc.mask_account("1234567") == "*******"
    assert pc.mask_account("12345678") == "12****78"


def test_console_output_is_redacted(capsys):
    run = pc.Run(secrets=("user123",))
    run.add("交易登录", "FAIL", "用户 user123 登录失败")
    assert "user123" not in capsys.readouterr().out
