"""run_gui:用 Qt 的 offscreen 平台组装界面(不连接、不进入事件循环)。"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import run_gui  # noqa: E402  (内部会先 import bootstrap)

ENV = {"CTP_USERNAME": "user_x", "CTP_PASSWORD": "pw_secret_123", "CTP_BROKER_ID": "9999",
       "CTP_TD_ADDRESS": "td:1", "CTP_MD_ADDRESS": "md:1", "CTP_APP_ID": "app", "CTP_AUTH_CODE": "auth_secret_456"}


@pytest.fixture(autouse=True)
def single_qapp(monkeypatch):
    """同一进程里只能有一个 QApplication:复用已有的。"""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    import vnpy.trader.ui as ui
    original = ui.create_qapp
    monkeypatch.setattr(ui, "create_qapp", lambda name="": QApplication.instance() or original(name))


def test_check_mode_validates_config_without_opening_a_window(capsys):
    assert run_gui.main(["--check"], env=ENV) == 0
    out = capsys.readouterr().out
    assert "配置检查通过" in out and "pw_secret_123" not in out


def test_missing_environment_reports_names_not_values(capsys):
    assert run_gui.main(["--check"], env={"CTP_USERNAME": "u"}) == 2
    err = capsys.readouterr().err
    assert "CTP_PASSWORD" in err


def test_gui_assembles_with_account_guard_and_does_not_connect(monkeypatch):
    pytest.importorskip("PySide6")
    connected = []
    from vnpy.trader.engine import MainEngine
    monkeypatch.setattr(MainEngine, "connect", lambda self, setting, name: connected.append(name))
    qapp, window, engine = run_gui.build_gui(ENV, auto_connect=False)
    try:
        assert not connected                                                    # --no-connect 不连接
        risk = engine.get_engine("RiskManager")
        assert risk is not None and "账户风控" in risk.rules                       # 手动下单同样受账户风控约束
        cta = engine.get_engine("CtaStrategy")
        cta.init_engine() if hasattr(cta, "init_engine") else None
        assert "TrendAtrStrategy" in cta.classes                                # 策略类能在界面里被发现
    finally:
        engine.close()                                                          # 不调 window.close():它会弹出模态确认框并阻塞


def test_gui_subscribes_to_the_symbol_once_its_contract_arrives(monkeypatch):
    pytest.importorskip("PySide6")
    from vnpy.event import Event
    from vnpy.trader.constant import Exchange, Product
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.event import EVENT_CONTRACT
    from vnpy.trader.object import ContractData
    subs = []
    monkeypatch.setattr(MainEngine, "subscribe", lambda self, req, name: subs.append((req.vt_symbol, name)))
    qapp, window, engine = run_gui.build_gui(ENV, auto_connect=False, subscribe="c2701.DCE")
    try:
        def contract(symbol):
            return ContractData(symbol=symbol, exchange=Exchange.DCE, name=symbol, product=Product.FUTURES,
                                size=10, pricetick=1, gateway_name="CTP")
        engine.event_engine.put(Event(EVENT_CONTRACT, contract("c2703")))      # 别的合约不订阅
        engine.event_engine.put(Event(EVENT_CONTRACT, contract("c2701")))
        import time
        for _ in range(50):
            if subs:
                break
            time.sleep(0.05)
        assert subs == [("c2701.DCE", "CTP")]
    finally:
        engine.close()


def test_repeated_contract_events_do_not_subscribe_twice(monkeypatch):
    """CTP 重连会重发合约表;重复订阅没有意义,也不能无限发。"""
    pytest.importorskip("PySide6")
    from vnpy.event import Event
    from vnpy.trader.constant import Exchange, Product
    from vnpy.trader.engine import MainEngine
    from vnpy.trader.event import EVENT_CONTRACT
    from vnpy.trader.object import ContractData
    subs = []
    monkeypatch.setattr(MainEngine, "subscribe", lambda self, req, name: subs.append((req.vt_symbol, name)))
    qapp, window, engine = run_gui.build_gui(ENV, auto_connect=False, subscribe="c2701.DCE")
    try:
        contract = ContractData(symbol="c2701", exchange=Exchange.DCE, name="c2701", product=Product.FUTURES,
                                size=10, pricetick=1, gateway_name="CTP")
        for _ in range(3):
            engine.event_engine.put(Event(EVENT_CONTRACT, contract))
        import time
        for _ in range(50):
            if subs:
                break
            time.sleep(0.05)
        time.sleep(0.3)                                    # 多等一会儿,确认没有第二次
        assert subs == [("c2701.DCE", "CTP")]
    finally:
        engine.close()


def test_symbol_without_exchange_suffix_is_reported(monkeypatch):
    """少写交易所后缀会导致永远匹配不上合约,必须给出提示而不是静默不订阅。"""
    pytest.importorskip("PySide6")
    logs = []
    from vnpy.trader.engine import MainEngine
    monkeypatch.setattr(MainEngine, "write_log", lambda self, msg, *a, **kw: logs.append(msg))
    qapp, window, engine = run_gui.build_gui(ENV, auto_connect=False, subscribe="c2701")
    try:
        assert any("缺少交易所后缀" in m for m in logs)
    finally:
        engine.close()


def test_subscribe_default_comes_from_the_given_env():
    args = run_gui.parse_args([], env={"LIVE_VT_SYMBOL": "c2701.DCE"})
    assert args.subscribe == "c2701.DCE"
    args = run_gui.parse_args([], env={})
    assert args.subscribe == ""
