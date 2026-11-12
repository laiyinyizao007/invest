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
    "CTP_APP_ID": "app",
    "CTP_AUTH_CODE": "auth",
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


# ---- 方案 A:受限连接交易前置,只做认证 + 合约查询 ----

def test_trading_front_credentials_are_required(monkeypatch):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))
    for name in ("CTP_TD_ADDRESS", "CTP_APP_ID", "CTP_AUTH_CODE"):
        env = {k: v for k, v in BASE_ENV.items() if k != name}
        with pytest.raises(ValueError, match=name):
            recorder.validate_config(env)


GUOXIN_ENV = {
    **BASE_ENV,
    "CTP_RECORD_PROFILE": "guoxin-test",
    "CTP_ENVIRONMENT": "测试",
    "CTP_MD_ADDRESS": "tcp://203.0.113.10:41293",
    "CTP_TD_ADDRESS": "tcp://203.0.113.10:41285",
}


def test_guoxin_profile_requires_explicit_address_allowlist():
    with pytest.raises(ValueError, match="CTP_RECORD_ALLOWED_ADDRESSES"):
        recorder.validate_config(GUOXIN_ENV)


def test_guoxin_profile_rejects_address_outside_allowlist():
    env = {**GUOXIN_ENV, "CTP_RECORD_ALLOWED_ADDRESSES": "203.0.113.10:41285"}  # 缺行情端口

    with pytest.raises(ValueError, match="不在允许列表"):
        recorder.validate_config(env)


def test_guoxin_profile_accepts_addresses_in_allowlist():
    env = {**GUOXIN_ENV, "CTP_RECORD_ALLOWED_ADDRESSES": " tcp://203.0.113.10:41285 , 203.0.113.10:41293"}

    assert recorder.validate_config(env)["profile"] == "guoxin-test"


@pytest.fixture
def locked_gateway():
    from vnpy.event import EventEngine

    gateway_cls = recorder.build_contract_only_gateway()
    return gateway_cls(EventEngine(), "CTP")


def _order_request():
    from vnpy.trader.constant import Direction, Exchange, OrderType
    from vnpy.trader.object import OrderRequest

    return OrderRequest(
        symbol="c2701", exchange=Exchange.DCE, direction=Direction.LONG,
        type=OrderType.LIMIT, volume=1, price=1.0,
    )


def test_contract_only_gateway_refuses_orders_without_touching_the_front(locked_gateway, monkeypatch):
    touched = []
    for name in ("reqOrderInsert", "reqOrderAction", "reqQryTradingAccount", "reqQryInvestorPosition"):
        monkeypatch.setattr(type(locked_gateway.td_api).__mro__[1], name, lambda *a, **k: touched.append(name), raising=False)

    assert locked_gateway.send_order(_order_request()) == ""
    assert locked_gateway.td_api.send_order(_order_request()) == ""
    locked_gateway.cancel_order(object())
    locked_gateway.query_account()
    locked_gateway.query_position()

    assert touched == []


@pytest.mark.parametrize("name", ["reqOrderInsert", "reqOrderAction", "reqQryTradingAccount", "reqQryInvestorPosition"])
def test_contract_only_trading_api_blocks_raw_requests(locked_gateway, name):
    """纵深防御:即使有人绕过网关方法直接调底层请求,也必须被拒绝。"""
    with pytest.raises(PermissionError, match="录制入口"):
        getattr(locked_gateway.td_api, name)({}, 1)


def test_contract_only_gateway_timer_only_refreshes_md_date(locked_gateway):
    from vnpy.trader.event import EVENT_TIMER

    calls = []
    locked_gateway.query_account = lambda: calls.append("account")
    locked_gateway.query_position = lambda: calls.append("position")
    locked_gateway.md_api.update_date = lambda: calls.append("date")

    locked_gateway.init_query()
    assert not hasattr(locked_gateway, "query_functions")
    for _ in range(6):
        locked_gateway.process_timer_event(None)

    assert calls and set(calls) == {"date"}  # 夜盘跨午夜需要刷新日期,但绝不查账户/持仓


# ---- 时间偏差可见性(SimNow 实测发现:偏差 >= 60 秒时 DataRecorder 会静默丢光所有 tick) ----

def test_assess_skew_is_silent_when_clocks_agree():
    assert recorder.assess_skew({}, 60) is None
    assert recorder.assess_skew({"c2701.DCE": 1.5, "MA701.CZCE": -2.0}, 60) is None


def test_assess_skew_warns_when_every_symbol_will_be_dropped():
    message = recorder.assess_skew({"c2701.DCE": 417.0, "MA701.CZCE": -172383.0}, 60)

    assert "全部" in message and "c2701.DCE" in message and "MA701.CZCE" in message


def test_assess_skew_warns_when_only_some_symbols_are_dropped():
    message = recorder.assess_skew({"c2701.DCE": 1.0, "MA701.CZCE": 500.0}, 60)

    assert "部分" in message and "MA701.CZCE" in message and "c2701.DCE" not in message.split("部分")[0]


@pytest.mark.parametrize("path", [".vntrader", ".vntrader/", ".", "data", "./.vntrader/../.vntrader"])
def test_database_dir_must_not_be_trading_or_backtest_store(monkeypatch, path):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))

    with pytest.raises(ValueError, match="CTP_RECORD_DATABASE_DIR"):
        recorder.validate_config({**BASE_ENV, "CTP_RECORD_DATABASE_DIR": path})


def test_database_dir_default_and_custom_isolated_dir_are_accepted(monkeypatch):
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))

    assert recorder.validate_config(BASE_ENV)["database_dir"] == Path(".vntrader_record")
    assert recorder.validate_config({**BASE_ENV, "CTP_RECORD_DATABASE_DIR": ".vntrader_record2"})


def test_wrapper_lock_leftover_file_without_holder_is_acquirable(tmp_path):
    lock = tmp_path / "wrapper.lock"
    lock.write_text(json.dumps({"pid": 999999, "at": "x"}), encoding="utf-8")  # 持有者早已退出遗留

    assert wrapper.acquire_lock(lock) is True
    wrapper.release_lock(lock)


def test_wrapper_lock_is_released_by_os_when_holder_process_dies(tmp_path):
    import subprocess
    import sys
    lock = tmp_path / "wrapper.lock"
    code = (
        "import sys, time; sys.path.insert(0, r'%s'); import record_market_data_wrapper as w; "
        "from pathlib import Path; assert w.acquire_lock(Path(sys.argv[1])); print('held', flush=True); time.sleep(60)"
    ) % str(Path(wrapper.__file__).parent)
    holder = subprocess.Popen([sys.executable, "-c", code, str(lock)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert wrapper.acquire_lock(lock) is False  # 真实并发持有者:拒绝
    finally:
        if sys.platform == "win32":  # venv 的 python.exe 是启动器,须杀整棵进程树
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(holder.pid)], capture_output=True)
        else:
            holder.kill()
        holder.wait()
    assert wrapper.acquire_lock(lock) is True  # 持有者被杀后由 OS 释放,无需 PID 探测
    wrapper.release_lock(lock)


@pytest.mark.parametrize("path", ["archive/.vntrader", "archive/data", "a/b", "../x", "C:/recording/.vntrader"])
def test_database_dir_must_be_single_level_under_project_root(monkeypatch, path):
    # isolate_state 只收目录名并拼到项目根下;多层/绝对路径会让 TEMP_DIR 落到别处(甚至主库)
    monkeypatch.setattr(recorder, "check_simnow_address", lambda address: (True, "ok"))

    with pytest.raises(ValueError, match="CTP_RECORD_DATABASE_DIR"):
        recorder.validate_config({**BASE_ENV, "CTP_RECORD_DATABASE_DIR": path})


def test_pid_state_is_three_valued_on_real_processes():
    import subprocess
    import sys
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert wrapper.pid_state(child.pid) == wrapper.ALIVE
    finally:
        child.kill()
        child.wait()
    assert wrapper.pid_state(child.pid) == wrapper.DEAD
    assert wrapper.pid_state(0) == wrapper.DEAD


def test_pid_alive_is_false_when_state_unknown():
    # 未知不能当成"存活"来放行,也不能当成"已死"来回收
    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(wrapper, "pid_state", lambda pid: wrapper.UNKNOWN)
    try:
        assert wrapper.pid_alive(123) is False
    finally:
        mp.undo()


def test_start_refuses_to_spawn_duplicate_when_recorded_pid_state_is_unknown(tmp_path, monkeypatch):
    wrapper.write_json(wrapper.paths(tmp_path)["state"], {"pid": 4321, "profile": "simnow"})
    monkeypatch.setattr(wrapper, "pid_state", lambda pid: wrapper.UNKNOWN)
    monkeypatch.setattr(wrapper.subprocess, "Popen", lambda *a, **k: pytest.fail("状态未知时不得拉起第二个录制进程"))

    assert wrapper.start(tmp_path) == 2
    assert "pid_state_unknown" in (tmp_path / "events.ndjson").read_text(encoding="utf-8")


def test_start_still_replaces_state_of_a_dead_pid(tmp_path, monkeypatch):
    wrapper.write_json(wrapper.paths(tmp_path)["state"], {"pid": 4321, "profile": "simnow"})
    monkeypatch.setattr(wrapper, "pid_state", lambda pid: wrapper.DEAD)
    spawned = []

    class Child:
        pid = 777
        def poll(self):
            return None

    monkeypatch.setattr(wrapper.subprocess, "Popen", lambda *a, **k: spawned.append(1) or Child())
    monkeypatch.setattr(wrapper, "STARTUP_GRACE", 0.0)

    assert wrapper.start(tmp_path) == 0 and spawned


# ---- 卡住检测:连接成功但迟迟订阅不到合约(实测 SimNow 快速重复登录后出现) ----

def test_assess_stall_only_fires_when_unsubscribed_past_the_limit():
    assert recorder.assess_stall(subscribed=False, elapsed=10, limit=90) is None
    assert "未订阅" in recorder.assess_stall(subscribed=False, elapsed=90, limit=90)
    assert recorder.assess_stall(subscribed=True, elapsed=3600, limit=90) is None      # 已订阅:盘后无 Tick 是正常的


def _write_status(state, status, started_age_seconds, heartbeat_age_seconds=5):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    (state / "status.json").write_text(json.dumps({
        "status": status, "started_at": (now - timedelta(seconds=started_age_seconds)).isoformat(timespec="seconds"),
        "heartbeat_at": (now - timedelta(seconds=heartbeat_age_seconds)).isoformat(timespec="seconds"),
    }), encoding="utf-8")


def test_watchdog_restarts_a_recorder_that_reports_stalled_even_with_a_fresh_heartbeat(tmp_path, monkeypatch):
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=5, started_age_seconds=100)
    _write_status(state, "stalled", started_age_seconds=100)

    assert wrapper.watchdog(state) == 0
    assert calls == ["stop", "start"]
    assert "stalled" in (state / "events.ndjson").read_text(encoding="utf-8")


def test_watchdog_ignores_a_stalled_status_left_by_a_previous_run(tmp_path, monkeypatch):
    # 新进程刚启动 20 秒,status.json 还是上一次运行遗留的 stalled:不能据此重启
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=5, started_age_seconds=20)
    _write_status(state, "stalled", started_age_seconds=3600)

    assert wrapper.watchdog(state) == 0
    assert calls == []


def test_watchdog_leaves_recording_status_alone(tmp_path, monkeypatch):
    state, calls = _stage_running_wrapper(tmp_path, monkeypatch, heartbeat_age_seconds=5, started_age_seconds=100)
    _write_status(state, "recording", started_age_seconds=100)

    assert wrapper.watchdog(state) == 0
    assert calls == []


def test_assess_silence_warns_when_subscribed_but_no_tick_ever_arrived():
    assert recorder.assess_silence(subscribed_for=30, ticks_seen=False, limit=120) is None
    assert "未收到任何 Tick" in recorder.assess_silence(subscribed_for=120, ticks_seen=False, limit=120)
    assert recorder.assess_silence(subscribed_for=3600, ticks_seen=True, limit=120) is None
    assert recorder.assess_silence(subscribed_for=None, ticks_seen=False, limit=120) is None     # 还没订阅:交给 stalled 判断
