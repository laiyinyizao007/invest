"""行情录制与增量调度工具的离线单元测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import record_market_data as recorder
import record_market_data_wrapper as wrapper
import record_market_incremental as incremental


BASE_ENV = {
    "CTP_RECORD_PROFILE": "simnow",
    "CTP_RECORD_SYMBOLS": "c2701.DCE,MA701.CZCE",
    "CTP_USERNAME": "user",
    "CTP_PASSWORD": "password",
    "CTP_BROKER_ID": "9999",
    "CTP_MD_ADDRESS": "tcp://127.0.0.1:30001",
    "CTP_TD_ADDRESS": "tcp://127.0.0.1:30000",
    "CTP_ENVIRONMENT": "测试",
}


def test_validate_config_accepts_market_only_symbols(monkeypatch):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))

    config = recorder.validate_config(BASE_ENV)

    assert config["symbols"] == ("C2701.DCE", "MA701.CZCE")
    assert config["record_tick"] is True
    assert config["record_1m"] is True


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"CTP_RECORD_SYMBOLS": "C0,MA0"}, "具体合约"),
        ({"CTP_RECORD_SYMBOLS": "C2701.DCE"}, "必须包含 MA"),
        ({"CTP_RECORD_SYMBOLS": "MA701.CZCE"}, "必须包含 C"),
        ({"CTP_RECORD_TICK": "false", "CTP_RECORD_1M": "false"}, "不能同时关闭"),
    ],
)
def test_validate_config_rejects_unsafe_values(monkeypatch, changes, message):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))
    env = {**BASE_ENV, **changes}

    with pytest.raises(ValueError, match=message):
        recorder.validate_config(env)


def test_wrapper_lock_is_exclusive(tmp_path):
    lock = tmp_path / "recording.lock"

    assert wrapper.acquire_lock(lock) is True
    assert wrapper.acquire_lock(lock) is False
    wrapper.release_lock(lock)
    assert wrapper.acquire_lock(lock) is True


def test_pid_alive_reports_real_process_state():
    """回归:Windows 上 os.kill(pid, 0) 抛 WinError 87,曾让存活进程被判为已退出,
    导致 ensure/watchdog 重复拉起、stop 永远拒绝。必须用真实子进程验证。"""
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert wrapper.pid_alive(child.pid) is True
        assert wrapper.pid_alive(0) is False
        assert wrapper.pid_alive(-1) is False
    finally:
        child.kill()
        child.wait()
    assert wrapper.pid_alive(child.pid) is False


def test_wrapper_status_reads_recorder_status(tmp_path, capsys):
    state = tmp_path / "state"
    state.mkdir()
    (state / "wrapper.json").write_text(
        json.dumps({"pid": 0, "profile": "simnow"}), encoding="utf-8"
    )
    (state / "status.json").write_text(
        json.dumps({"status": "recording", "heartbeat_at": "now"}), encoding="utf-8"
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(wrapper, "process_identity", lambda pid, profile=None: True)
    try:
        assert wrapper.status(state) == 0
    finally:
        monkeypatch.undo()

    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "recording"
    assert output["wrapper_running"] is True


def test_incremental_build_command_is_bounded():
    command = incremental.build_command(("C", "MA"), ("1", "15"))

    assert command[0] == sys.executable
    assert command[-3:] == ["--periods", "1", "15"]
    assert "fetch_data.py" in command[1]


def test_incremental_records_subprocess_result(tmp_path, monkeypatch):
    calls = {}

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(incremental.subprocess, "run", fake_run)
    assert incremental.run_incremental(("C",), ("1",), tmp_path, 30) == 0

    result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert calls["kwargs"]["timeout"] == 30
    assert not (tmp_path / "incremental.lock").exists()


def test_incremental_does_not_reenter_when_locked(tmp_path):
    lock = tmp_path / "incremental.lock"
    assert incremental.acquire_lock(lock) is True

    try:
        assert incremental.run_incremental(("C",), ("1",), tmp_path, 30) == 2
        result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
        assert result["status"] == "locked"
    finally:
        incremental.release_lock(lock)
