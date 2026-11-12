"""scripts/run_headless.py 与 scripts/unlock_account.py 测试。"""
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import run_headless  # noqa: E402  (内部会先 import bootstrap)
import unlock_account  # noqa: E402
from fakes import FakeGateway  # noqa: E402
from risk.account_guard import AccountGuard, AccountLimits  # noqa: E402

ENV = {"CTP_USERNAME": "user_x", "CTP_PASSWORD": "pw_secret_123", "CTP_BROKER_ID": "9999",
       "CTP_TD_ADDRESS": "td:1", "CTP_MD_ADDRESS": "md:1", "CTP_APP_ID": "app", "CTP_AUTH_CODE": "auth_secret_456",
       "LIVE_VT_SYMBOL": "c2701.DCE", "CTP_ENVIRONMENT": "测试"}


class StubRuntime:
    created = []

    def __init__(self, config, gateway_class, notifier=None, **kw):
        self.config, self.gateway_class, self.notifier = config, gateway_class, notifier
        self.stopped = None
        StubRuntime.created.append(self)

    def run(self):
        return 4

    def request_stop(self, code=0, reason=""):
        self.stopped = (code, reason)


@pytest.fixture(autouse=True)
def reset_stub():
    StubRuntime.created.clear()


# ---------- run_headless ----------

def test_check_mode_validates_config_without_connecting_or_leaking_secrets(capsys):
    code = run_headless.main(["--check"], env=ENV, gateway_loader=lambda: FakeGateway, runtime_factory=StubRuntime)
    out = capsys.readouterr().out
    assert code == 0 and StubRuntime.created == []
    assert "c2701.DCE" in out
    assert "pw_secret_123" not in out and "auth_secret_456" not in out


def test_missing_environment_reports_names_not_values(capsys):
    env = {k: v for k, v in ENV.items() if k != "CTP_PASSWORD"}
    code = run_headless.main(["--check"], env=env, gateway_loader=lambda: FakeGateway)
    err = capsys.readouterr().err
    assert code == 2 and "CTP_PASSWORD" in err and "user_x" not in err and "auth_secret_456" not in err


def test_missing_symbol_is_a_config_error(capsys):
    env = {k: v for k, v in ENV.items() if k != "LIVE_VT_SYMBOL"}
    assert run_headless.main(["--check"], env=env, gateway_loader=lambda: FakeGateway) == 2
    assert "LIVE_VT_SYMBOL" in capsys.readouterr().err


def test_invalid_setting_json_is_a_config_error(capsys):
    assert run_headless.main(["--check", "--setting", "{oops"], env=ENV, gateway_loader=lambda: FakeGateway) == 2


def test_run_passes_configuration_and_propagates_exit_code():
    code = run_headless.main(
        ["--history", "CADJ.DCE", "--setting", '{"fast_window": 5, "slow_window": 20}', "--heartbeat", "300"],
        env={**ENV, "WEBHOOK_URL": "http://example.invalid/hook", "WEBHOOK_KIND": "feishu"},
        gateway_loader=lambda: FakeGateway, runtime_factory=StubRuntime,
    )
    assert code == 4                                            # 运行时的退出码原样返回
    rt = StubRuntime.created[0]
    assert rt.config.vt_symbol == "c2701.DCE" and rt.config.heartbeat_seconds == 300
    assert rt.config.strategy_setting == {"fast_window": 5, "slow_window": 20, "history_vt_symbol": "CADJ.DCE"}
    assert rt.config.gateway_setting["用户名"] == "user_x" and rt.config.gateway_setting["经纪商代码"] == "9999"
    assert rt.gateway_class is FakeGateway
    assert rt.notifier.kind == "feishu" and rt.notifier.enabled


def test_history_and_symbol_can_come_from_environment():
    run_headless.main([], env={**ENV, "HISTORY_VT_SYMBOL": "CADJ.DCE"}, gateway_loader=lambda: FakeGateway, runtime_factory=StubRuntime)
    assert StubRuntime.created[0].config.strategy_setting["history_vt_symbol"] == "CADJ.DCE"


def test_notifier_redacts_ctp_credentials():
    n = run_headless.build_notifier({**ENV, "WEBHOOK_URL": "http://x/y"})
    assert "pw_secret_123" in n.secrets and "auth_secret_456" in n.secrets
    assert not run_headless.build_notifier({}).enabled


def test_signal_handlers_request_a_graceful_stop():
    rt = StubRuntime(None, None)
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM) if hasattr(signal, "SIGTERM")}
    try:
        installed = run_headless.install_signal_handlers(rt)
        assert int(signal.SIGINT) in installed
        signal.getsignal(signal.SIGINT)(int(signal.SIGINT), None)
        assert rt.stopped is not None and rt.stopped[0] == 0 and "终止信号" in rt.stopped[1]
    finally:
        for s, h in saved.items():
            signal.signal(s, h)


# ---------- unlock_account ----------

def locked_state(tmp_path) -> Path:
    path = tmp_path / "guard.json"
    g = AccountGuard(AccountLimits(600, 2000), path)
    g.update(10_000, datetime(2026, 11, 9, 10, 0))
    g.update(7_900, datetime(2026, 11, 9, 10, 0))
    assert AccountGuard(AccountLimits(), path).hard_locked
    return path


def test_unlock_requires_exact_confirmation(tmp_path):
    path = locked_state(tmp_path)
    assert unlock_account.unlock(path, confirm=lambda prompt: "yes") == 1
    assert AccountGuard(AccountLimits(), path).hard_locked                 # 答错就不能解锁
    assert unlock_account.unlock(path, confirm=lambda prompt: "UNLOCK") == 0
    after = AccountGuard(AccountLimits(), path)
    assert not after.hard_locked and after.peak_equity == 0.0              # 最高点已重置,不会立刻再次触发


def test_unlock_assume_yes(tmp_path):
    path = locked_state(tmp_path)
    assert unlock_account.unlock(path, assume_yes=True) == 0
    assert not AccountGuard(AccountLimits(), path).hard_locked


def test_unlock_when_not_locked_or_missing(tmp_path, capsys):
    assert unlock_account.unlock(tmp_path / "nope.json") == 0
    path = tmp_path / "ok.json"
    AccountGuard(AccountLimits(), path).update(10_000, datetime(2026, 11, 9, 10, 0))
    assert unlock_account.unlock(path, assume_yes=True) == 0
    assert "无需解锁" in capsys.readouterr().out


def test_unlock_reports_write_failure(tmp_path, monkeypatch):
    path = locked_state(tmp_path)
    monkeypatch.setattr(AccountGuard, "_save", lambda self: setattr(self, "save_error", "disk full"))
    assert unlock_account.unlock(path, assume_yes=True) == 2


def test_unlock_main_parses_arguments(tmp_path):
    path = locked_state(tmp_path)
    assert unlock_account.main(["--state", str(path), "--yes"]) == 0
    assert not json.loads(path.read_text(encoding="utf-8"))["hard_locked"]


def test_notifier_outbox_is_on_by_default_under_data_state():
    n = run_headless.build_notifier({**ENV, "WEBHOOK_URL": "http://x/y"})

    assert n._outbox_path is not None and n._outbox_path.name == "webhook_outbox.jsonl"
    assert n._outbox_path.parent.name == "state" and n._outbox_path.parent.parent.name == "data"


def test_notifier_outbox_can_be_redirected_or_switched_off():
    custom = run_headless.build_notifier({**ENV, "WEBHOOK_URL": "http://x/y", "WEBHOOK_OUTBOX": "/tmp/my_outbox.jsonl"})
    off = run_headless.build_notifier({**ENV, "WEBHOOK_URL": "http://x/y", "WEBHOOK_OUTBOX": "off"})

    assert str(custom._outbox_path).replace("\\", "/").endswith("/tmp/my_outbox.jsonl")
    assert off._outbox_path is None
