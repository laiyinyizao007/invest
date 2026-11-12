"""drill 诊断埋点:耐久日志、退出钩子、已释放 API 探针、周期快照、登录间隔。默认关闭时不得安装任何东西。"""
import os
import time
from types import SimpleNamespace

import pytest

import drill_trace as dt


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


# ---------- TraceLog ----------

def test_trace_log_writes_one_durable_line_per_event(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(dt.os, "fsync", lambda fd: synced.append(fd))
    trace = dt.TraceLog(tmp_path / "t.log")

    trace.write("step_a", n=1, note="x")
    trace.write("step_b")

    out = lines(tmp_path / "t.log")
    assert "step_a" in out[0] and "n=1" in out[0] and "thread=" in out[0]
    assert "step_b" in out[1]
    assert len(synced) == 2                    # 每行都 fsync:进程被杀也不丢
    trace.close()


def test_trace_log_survives_unprintable_values_and_never_raises(tmp_path):
    trace = dt.TraceLog(tmp_path / "t.log")

    class Bad:
        def __repr__(self):
            raise RuntimeError("boom")

    trace.write("weird", value=Bad())
    trace.close()
    trace.write("after_close")                 # 关闭后写入也不抛(埋点绝不能反过来打断被观察的流程)

    assert "weird" in lines(tmp_path / "t.log")[0]


# ---------- 退出码解释 ----------

@pytest.mark.parametrize("code, word", [(139, "SIGSEGV"), (134, "SIGABRT"), (137, "SIGKILL"), (143, "SIGTERM"), (0, "正常"), (3, "3")])
def test_describe_exit_code(code, word):
    assert word in dt.describe_exit_code(code)


# ---------- 退出钩子 ----------

def test_exit_hooks_log_caller_stack_before_exiting_and_can_be_removed(tmp_path):
    exits = []
    fake_os = SimpleNamespace(_exit=lambda code: exits.append(("os._exit", code)), kill=lambda pid, sig: exits.append(("kill", pid, sig)))
    fake_sys = SimpleNamespace(exit=lambda code=0: exits.append(("sys.exit", code)))
    trace = dt.TraceLog(tmp_path / "t.log")

    uninstall = dt.install_exit_hooks(trace, os_module=fake_os, sys_module=fake_sys)
    fake_os._exit(7)
    fake_sys.exit(2)
    fake_os.kill(123, 15)

    text = "\n".join(lines(tmp_path / "t.log"))
    assert exits == [("os._exit", 7), ("sys.exit", 2), ("kill", 123, 15)]       # 原函数照常执行
    assert "exit_call" in text and "os._exit" in text and "code=7" in text
    assert "test_exit_hooks_log_caller_stack" in text                           # 调用栈里能看到谁调的
    assert "kill_call" in text and "pid=123" in text

    uninstall()
    before = len(lines(tmp_path / "t.log"))
    fake_os._exit(9)
    assert len(lines(tmp_path / "t.log")) == before                              # 卸载后不再记录


def test_signal_handlers_log_and_chain_to_previous(tmp_path):
    seen = []
    registry = {}

    def fake_signal(sig, handler):
        old = registry.get(sig, "default")
        registry[sig] = handler
        return old

    fake_signal_module = SimpleNamespace(signal=fake_signal, SIGTERM=15, SIGINT=2)
    trace = dt.TraceLog(tmp_path / "t.log")
    registry[15] = lambda s, f: seen.append(("prev", s))

    dt.install_signal_logging(trace, signal_module=fake_signal_module)
    registry[15](15, None)

    assert "signal_received" in "\n".join(lines(tmp_path / "t.log")) and "signum=15" in "\n".join(lines(tmp_path / "t.log"))
    assert seen == [("prev", 15)]                                                # 原处理器仍被调用


# ---------- 已释放 API 探针 ----------

class _Api:
    def __init__(self):
        self.calls = []

    def query_account(self):
        self.calls.append("query_account")
        return "acct"

    def send_order(self, req):
        self.calls.append(("send_order", req))
        return "oid"

    def close(self):
        self.calls.append("close")


def test_probe_logs_calls_only_after_marked_closed_and_still_forwards_them(tmp_path):
    api = _Api()
    trace = dt.TraceLog(tmp_path / "t.log")
    probe = dt.ReleasedApiProbe(trace, api, "td", ("query_account", "send_order", "close", "reqNotThere"))

    assert api.query_account() == "acct"                       # 关闭前:转发但不记录
    assert "call_after_release" not in "\n".join(lines(tmp_path / "t.log"))

    probe.mark_released()
    assert api.query_account() == "acct"
    assert api.send_order("r") == "oid"
    api.close()

    text = "\n".join(lines(tmp_path / "t.log"))
    assert text.count("call_after_release") == 3
    assert "method=query_account" in text and "method=send_order" in text and "method=close" in text
    assert "test_probe_logs_calls_only_after" in text           # 带调用栈
    assert api.calls == ["query_account", "query_account", ("send_order", "r"), "close"]


def test_probe_logs_every_close_call_even_before_release_to_catch_double_close(tmp_path):
    api = _Api()
    trace = dt.TraceLog(tmp_path / "t.log")
    dt.ReleasedApiProbe(trace, api, "td", ("close",))

    api.close()
    api.close()

    assert "api_close_called" in "\n".join(lines(tmp_path / "t.log"))
    assert "\n".join(lines(tmp_path / "t.log")).count("api_close_called") == 2


def test_probe_skips_methods_that_cannot_be_wrapped(tmp_path):
    class Slots:
        __slots__ = ()

        def close(self):
            pass

    trace = dt.TraceLog(tmp_path / "t.log")
    probe = dt.ReleasedApiProbe(trace, Slots(), "td", ("close",))     # 不可设置属性:只记录「无法包装」,不抛

    assert "probe_unavailable" in "\n".join(lines(tmp_path / "t.log"))
    probe.mark_released()


# ---------- 周期快照 ----------

def test_periodic_snapshot_runs_until_stopped_and_survives_errors(tmp_path):
    trace = dt.TraceLog(tmp_path / "t.log")
    n = {"i": 0}

    def sample():
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("sampler broke")
        return {"i": n["i"]}

    snap = dt.PeriodicSnapshot(trace, "tick", sample, interval=0.05)
    snap.start()
    time.sleep(0.4)
    snap.stop()
    count = n["i"]
    time.sleep(0.2)

    text = "\n".join(lines(tmp_path / "t.log"))
    assert count >= 3 and n["i"] == count                       # 停止后不再采样
    assert "snapshot_error" in text and "label=tick" in text    # 采样器异常被记录而不是杀死线程


# ---------- 登录间隔 ----------

def test_login_gap_reports_seconds_since_last_logout_and_handles_missing_file(tmp_path):
    gap = dt.LoginGap(tmp_path / "gap.json")

    assert gap.seconds_since_last_logout() is None
    gap.mark_logout(now=1000.0)
    assert gap.seconds_since_last_logout(now=1016.0) == 16.0
    (tmp_path / "gap.json").write_text("{broken", encoding="utf-8")
    assert gap.seconds_since_last_logout(now=2000.0) is None


# ---------- 默认关闭 ----------

def test_setup_returns_none_and_installs_nothing_without_a_trace_path(monkeypatch):
    called = []
    monkeypatch.setattr(dt, "install_exit_hooks", lambda *a, **k: called.append("hooks"))
    monkeypatch.setattr(dt, "install_fault_dumps", lambda *a, **k: called.append("fault"))

    assert dt.setup(None) is None and dt.setup("") is None
    assert called == []


def test_setup_with_a_path_installs_hooks_and_returns_a_session(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(dt, "install_exit_hooks", lambda *a, **k: called.append("hooks") or (lambda: None))
    monkeypatch.setattr(dt, "install_fault_dumps", lambda *a, **k: called.append("fault") or (lambda: None))
    monkeypatch.setattr(dt, "install_signal_logging", lambda *a, **k: called.append("signal"))

    session = dt.setup(str(tmp_path / "t.log"))

    assert session is not None and called == ["hooks", "fault", "signal"]
    assert "trace_start" in "\n".join(lines(tmp_path / "t.log"))
    session.close()
