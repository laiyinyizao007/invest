"""演练①(c)的离线证据:断线期间策略信号不会排队,重连后不会「一窝蜂」补发。

真实链路事实(源码核实,见 docs/adr/20261110-ctp-error-code-and-watchdog-policy.md):
- vnpy_ctp 的 CtpTdApi.send_order 不排队:断连时 reqOrderInsert 本地失败,返回空串并写
  「委托请求发送失败，错误代码：{n}」,不产生 OrderData;
- 策略的 pending_entry 一次性消费:发出前就置 None,发不出去也不重试,等下一根日线重新判断。

本文件用 FakeGateway 的 disconnected 模式在离线环境复现这条链路,作为 SimNow 拔网线演练的
可重复证据(现场演练只能看到现象,这里锁定语义)。
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import bootstrap  # noqa: E402,F401
import vnpy_ctastrategy.engine as cta_engine_module  # noqa: E402
from fakes import FakeDatabase, FakeGateway, SyncEventEngine, history_bars  # noqa: E402
from strategies.trend_atr_strategy import TrendAtrStrategy  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy_ctastrategy import CtaStrategyApp  # noqa: E402

SETTING = {"fast_window": 2, "slow_window": 3, "atr_mult": 1.5, "risk_budget": 1e9}
IN_SESSION = datetime(2026, 11, 10, 10, 0, 0)          # 周二 10:00,在入场时段 9:00-10:15 内


class Harness:
    def __init__(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cta_engine_module, "load_json", lambda filename: {})
        monkeypatch.setattr(cta_engine_module, "save_json", lambda *a, **k: None)
        self.event_engine = SyncEventEngine()
        self.main = MainEngine(self.event_engine)
        self.gw: FakeGateway = self.main.add_gateway(FakeGateway)
        self.risk = self.main.add_app(SafeRiskManagerApp)
        self.risk.rules["账户风控"].update_setting({"state_file": str(tmp_path / "guard.json")})
        self.cta = self.main.add_app(CtaStrategyApp)
        self.cta.database = FakeDatabase(history_bars())
        self.cta.classes["TrendAtrStrategy"] = TrendAtrStrategy
        self.cta.register_event()
        self.gw.push_contract()
        self.gw.mode = "fill"
        self.cta.add_strategy("TrendAtrStrategy", "t1", "c2601.DCE", dict(SETTING))
        self.strategy: TrendAtrStrategy = self.cta.strategies["t1"]
        self.cta._init_strategy("t1")
        self.strategy.trading = False               # 与运行时一致:对账前强制停止
        self.cta.start_strategy("t1")

    def tick(self, dt: datetime, price: float = 2230.0) -> None:
        self.gw.push_tick(dt=dt, last=price)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path)
    yield h
    h.main.close()


def test_signal_during_disconnect_fails_fast_and_is_never_retried_after_reconnect(harness):
    """断连期间到达入场时段:信号被一次性消费,发单本地失败;重连后没有任何积压委托补发。"""
    harness.strategy.pending_entry = (1, 5.0)       # 模拟收盘后产生、待次时段发出的做多信号
    harness.gw.mode = "disconnected"

    harness.tick(IN_SESSION)                         # 触发 _place_pending_entry

    assert harness.strategy.pending_entry is None   # 无论成败只发一次:信号已被消费
    assert harness.gw.sent == []                     # 断连快速失败,没有任何订单到达网关
    assert harness.strategy.pos == 0

    harness.gw.mode = "fill"                         # 模拟自动重连成功
    harness.tick(IN_SESSION.replace(minute=1))       # 重连后继续推 Tick
    harness.tick(IN_SESSION.replace(minute=2))
    assert harness.gw.sent == []                     # 没有积压、没有补发:不会一窝蜂全发出去


def test_disconnected_send_order_leaves_no_order_trace(harness):
    """断连发单不产生 OrderData、不进网关订单表,框架层面无队列可补发。"""
    harness.gw.mode = "disconnected"
    before = dict(harness.gw.orders)
    harness.strategy.pending_entry = (-1, 5.0)
    harness.tick(IN_SESSION)
    assert harness.gw.orders == before               # 没有遗留任何订单对象
    assert harness.gw.cancel_requests == []          # 也没有需要撤销的挂单


def test_new_signal_after_reconnect_still_works(harness):
    """重连后产生的**新**信号不受影响:链路恢复后交易能力正常(证明拦截的只是断线期的旧信号)。"""
    harness.gw.mode = "disconnected"
    harness.strategy.pending_entry = (1, 5.0)
    harness.tick(IN_SESSION)
    assert harness.gw.sent == []

    harness.gw.mode = "fill"
    harness.strategy.pending_entry = (1, 5.0)       # 模拟下一根日线产生的新信号
    harness.tick(IN_SESSION.replace(minute=1))
    assert len(harness.gw.sent) == 1                # 新信号正常发出
