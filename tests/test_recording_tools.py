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

    def fake_run_bounded(command, *, cwd, env, timeout):
        calls["command"] = command
        calls["env"] = env
        calls["timeout"] = timeout
        return incremental.BoundedResult(returncode=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(incremental, "run_bounded", fake_run_bounded)
    monkeypatch.setenv("CTP_PASSWORD", "secret")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    assert incremental.run_incremental(("C",), ("1",), tmp_path, 30) == 0

    result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert calls["timeout"] == 30
    assert not (tmp_path / "incremental.lock").exists()
    # 最小权限:下载任务不需要柜台凭证,代理等普通环境保留
    assert not any(k.startswith("CTP_") for k in calls["env"])
    assert calls["env"].get("HTTP_PROXY") == "http://127.0.0.1:7890"


def test_incremental_timeout_kills_process_tree_and_returns_on_time(tmp_path, monkeypatch):
    """回归:venv 启动器 + 孙进程持有管道时,subprocess.run(timeout=) 会等到孙进程退出才返回。"""
    import time

    marker = tmp_path / "grandchild.pid"
    code = "; ".join([
        "import subprocess, sys, time, pathlib",
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
        f"pathlib.Path(r'{marker}').write_text(str(g.pid))",
        "time.sleep(60)",  # 单行语句用分号连接,避免依赖换行转义
    ])
    monkeypatch.setattr(incremental, "build_command", lambda symbols, periods: [sys.executable, "-c", code])
    started = time.monotonic()
    rc = incremental.run_incremental(("C",), ("1",), tmp_path, 3)
    elapsed = time.monotonic() - started

    assert rc == 124
    assert elapsed < 20, f"有界超时失效,实际耗时 {elapsed:.1f}s"
    result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
    assert result["status"] == "timeout"
    assert not (tmp_path / "incremental.lock").exists()
    gpid = int(marker.read_text())
    deadline = time.monotonic() + 5
    while wrapper.pid_alive(gpid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert wrapper.pid_alive(gpid) is False, "孙进程在超时后仍存活"


def test_incremental_recovers_stale_lock_from_dead_process(tmp_path, monkeypatch):
    import subprocess

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock = tmp_path / "incremental.lock"
    lock.write_text(json.dumps({"pid": dead.pid, "started_at": incremental.now()}), encoding="utf-8")
    monkeypatch.setattr(
        incremental, "run_bounded",
        lambda command, *, cwd, env, timeout: incremental.BoundedResult(0, "", "", False),
    )

    assert incremental.run_incremental(("C",), ("1",), tmp_path, 30) == 0
    result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
    assert result["status"] == "succeeded"
    assert result["stale_lock_recovered"] is True


def test_incremental_does_not_reenter_when_locked(tmp_path):
    lock = tmp_path / "incremental.lock"
    assert incremental.acquire_lock(lock) is True

    try:
        assert incremental.run_incremental(("C",), ("1",), tmp_path, 30) == 2
        result = json.loads((tmp_path / "incremental.json").read_text(encoding="utf-8"))
        assert result["status"] == "locked"
    finally:
        incremental.release_lock(lock)


# ---- record_market_data.py 的启动/关闭语义(codex 审查后补) ----

def test_simnow_md_host_must_match_whitelisted_td_host(monkeypatch):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))
    env = {**BASE_ENV, "CTP_MD_ADDRESS": "tcp://203.0.113.9:30011", "CTP_TD_ADDRESS": "tcp://127.0.0.1:30001"}

    with pytest.raises(ValueError, match="行情前置主机"):
        recorder.validate_config(env)


def test_create_engines_starts_event_engine_exactly_once():
    """回归:MainEngine.__init__ 已启动 EventEngine,重复 start() 会抛 RuntimeError。"""
    event_engine, main_engine = recorder.create_engines()
    try:
        assert event_engine is main_engine.event_engine
        assert event_engine._active is True
    finally:
        main_engine.close()


class FakeRecorder:
    def __init__(self):
        self.tick_recordings = {"c2701.DCE": {}, "OLD2601.CZCE": {}}
        self.bar_recordings = {"c2701.DCE": {}, "OLD2601.CZCE": {}}
        self.removed = []
        import queue

        self.queue = queue.Queue()
        self.ticks = {"c2701.DCE": [1, 2]}
        self.bars = {"c2701.DCE": [3]}

    def remove_tick_recording(self, vt_symbol):
        self.tick_recordings.pop(vt_symbol)
        self.removed.append(("tick", vt_symbol))

    def remove_bar_recording(self, vt_symbol):
        self.bar_recordings.pop(vt_symbol)
        self.removed.append(("bar", vt_symbol))


def test_sync_recordings_drops_symbols_outside_current_config():
    fake = FakeRecorder()

    recorder.sync_recordings(fake, ("C2701.DCE", "MA701.CZCE"), record_tick=True, record_1m=False)

    assert "OLD2601.CZCE" not in fake.tick_recordings
    assert "c2701.DCE" in fake.tick_recordings  # 大小写不敏感匹配,保留原始键
    assert fake.bar_recordings == {}  # 1m 关闭时清空所有 bar 录制


def test_flush_recorder_moves_buffers_to_queue_and_waits_for_drain():
    import threading

    fake = FakeRecorder()
    drained = threading.Thread(target=lambda: [fake.queue.get() for _ in range(2)], daemon=True)
    drained.start()

    assert recorder.flush_recorder(fake, timeout=5) is True
    drained.join(timeout=5)
    assert fake.ticks == {} and fake.bars == {}


def test_flush_recorder_reports_timeout_when_queue_not_drained():
    fake = FakeRecorder()  # 没有消费者

    assert recorder.flush_recorder(fake, timeout=0.3) is False


def test_status_write_failure_never_raises_into_event_thread(tmp_path, monkeypatch):
    config = {"profile": "simnow", "symbols": ("C2701.DCE",), "database_dir": tmp_path}
    status = recorder.RecorderStatus(tmp_path / "status.json", config)

    def deny(*args, **kwargs):
        raise PermissionError("locked by reader")

    monkeypatch.setattr(recorder.os, "replace", deny)
    status.update(status="recording")  # 不得抛出
    assert "locked by reader" in status.data["last_error"]


def test_status_note_tick_does_not_write_to_disk(tmp_path):
    config = {"profile": "simnow", "symbols": ("C2701.DCE",), "database_dir": tmp_path}
    status = recorder.RecorderStatus(tmp_path / "status.json", config)
    before = (tmp_path / "status.json").read_text(encoding="utf-8")

    for i in range(200):
        status.note(last_tick=f"t{i}")

    assert (tmp_path / "status.json").read_text(encoding="utf-8") == before
    assert status.data["last_tick"] == "t199"


# ---- wrapper 生命周期语义(codex 审查后补) ----

def _write_script(path, body):
    path.write_text(body, encoding="utf-8")
    return path


def test_wrapper_start_does_not_hold_callers_pipes(tmp_path):
    """回归:子进程继承调用方 stdout/stderr 句柄时,捕获输出的 DSH 短任务会被常驻进程拖到超时。"""
    import subprocess

    dummy = _write_script(tmp_path / "dummy_recorder.py", "import time\ntime.sleep(60)\n")
    scripts_dir = Path(wrapper.__file__).resolve().parent
    state = tmp_path / "state"
    driver = "; ".join([
        "import sys",
        f"sys.path.insert(0, r'{scripts_dir}')",
        "import record_market_data_wrapper as w",
        "from pathlib import Path",
        f"w.SCRIPT = Path(r'{dummy}')",
        f"sys.exit(w.start(Path(r'{state}')))",
    ])
    pid = None
    try:
        done = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, timeout=30)
        assert done.returncode == 0, done.stderr
        pid = json.loads((state / "wrapper.json").read_text(encoding="utf-8"))["pid"]
    finally:
        if pid is None and (state / "wrapper.json").exists():
            pid = json.loads((state / "wrapper.json").read_text(encoding="utf-8"))["pid"]
        if pid:
            incremental.kill_tree(pid)


def test_wrapper_start_reports_failure_when_child_exits_immediately(tmp_path, monkeypatch):
    dummy = _write_script(tmp_path / "dying_recorder.py", "import sys\nsys.exit(3)\n")
    monkeypatch.setattr(wrapper, "SCRIPT", dummy)
    monkeypatch.setattr(wrapper, "STARTUP_GRACE", 5.0)
    state = tmp_path / "state"

    assert wrapper.start(state) == 3
    assert not (state / "wrapper.json").exists()
    events = [json.loads(line)["event"] for line in (state / "events.ndjson").read_text(encoding="utf-8").splitlines()]
    assert "start_failed" in events


def _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds, started_age_seconds):
    from datetime import datetime, timedelta, timezone

    state = tmp_path / "state"
    state.mkdir()
    now = datetime.now(timezone.utc)
    (state / "wrapper.json").write_text(json.dumps({
        "pid": 4242, "profile": "simnow",
        "started_at": (now - timedelta(seconds=started_age_seconds)).isoformat(timespec="seconds"),
    }), encoding="utf-8")
    (state / "status.json").write_text(json.dumps({
        "heartbeat_at": (now - timedelta(seconds=heartbeat_age_seconds)).isoformat(timespec="seconds"),
    }), encoding="utf-8")
    calls = []
    monkeypatch.setattr(wrapper, "process_identity", lambda pid, profile=None: True)
    monkeypatch.setattr(wrapper, "stop", lambda state_dir, timeout=15.0: calls.append("stop") or 0)
    monkeypatch.setattr(wrapper, "start", lambda state_dir, profile=None: calls.append("start") or 0)
    return state, calls


def test_watchdog_restarts_recorder_with_stale_heartbeat(tmp_path, monkeypatch):
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=3600, started_age_seconds=7200)

    assert wrapper.watchdog(state) == 0
    assert calls == ["stop", "start"]


def test_watchdog_leaves_healthy_recorder_alone(tmp_path, monkeypatch):
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=5, started_age_seconds=7200)

    assert wrapper.watchdog(state) == 0
    assert calls == []


def test_watchdog_gives_new_process_time_to_publish_heartbeat(tmp_path, monkeypatch):
    # status.json 还是上一次运行遗留的旧文件,但进程刚启动 10 秒:不能据此重启
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=3600, started_age_seconds=10)

    assert wrapper.watchdog(state) == 0
    assert calls == []
