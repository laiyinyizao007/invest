"""run_gui:用 Qt 的 offscreen 平台组装界面(不连接、不进入事件循环)。"""
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import run_gui  # noqa: E402  (内部会先 import bootstrap)

ENV = {"CTP_USERNAME": "user_x", "CTP_PASSWORD": "pw_secret_123", "CTP_BROKER_ID": "9999",
       "CTP_TD_ADDRESS": "td:1", "CTP_MD_ADDRESS": "md:1", "CTP_APP_ID": "app", "CTP_AUTH_CODE": "auth_secret_456"}


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
