"""drill 的诊断埋点(默认关闭,由 `drill.py --trace FILE` 开启)。

起因:夜盘演练的 `disconnect` 场景在 `td_api.close()` 之后进程悄悄消失,且当时的管道吞掉了真实退出码,
靠猜无法归因。这里的埋点目标是让**同一次运行**就能区分:原生崩溃、释放后的 API 被再次调用、Python 主动退出、
被外部终止、登录卡住/限流、工具假象(见 docs/plans 与 ~/.claude/plans 里的假设表)。

设计约束:
- 埋点绝不能反过来打断被观察的流程:所有写入都吞掉异常;
- 日志逐行 write→flush→fsync,进程被杀也不丢;写到挂载文件而不是管道;
- 不记录任何凭据:只记事件名、线程名、调用栈、布尔/数字状态。
"""
from __future__ import annotations

import atexit
import faulthandler
import json
import os
import signal
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Sequence

EXIT_CODE_NOTES = {
    0: "0(正常退出)",
    134: "134(SIGABRT:原生 abort,常见于 C++ 异常/断言)",
    137: "137(SIGKILL:被强杀或 OOM)",
    139: "139(SIGSEGV:段错误,原生崩溃)",
    143: "143(SIGTERM:被要求终止)",
}


def describe_exit_code(code: int) -> str:
    if code in EXIT_CODE_NOTES:
        return EXIT_CODE_NOTES[code]
    if code < 0:
        return f"{code}(被信号 {-code} 终止)"
    return str(code)


def _safe(value: Any) -> str:
    try:
        text = repr(value) if not isinstance(value, str) else value
    except Exception:       # noqa: BLE001  埋点不得因为对象的 __repr__ 出错而抛
        return "<unprintable>"
    return text.replace("\n", " | ")[:600]


def stack_summary(skip: int = 2, limit: int = 12) -> str:
    """当前调用栈压成一行:最里层在最后,形如 `outer(file:12) <- inner(file:30)`。"""
    frames = traceback.extract_stack()[:-skip][-limit:]
    return " <- ".join(f"{f.name}({os.path.basename(f.filename)}:{f.lineno})" for f in frames)


class NullTrace:
    """未开启 --trace 时使用:所有写入都是空操作,调用方不必到处判断。"""

    enabled = False
    file = None

    def write(self, event: str, **fields: Any) -> None:
        return None

    def close(self) -> None:
        return None


NULL_TRACE = NullTrace()


class TraceLog:
    enabled = True

    def __init__(self, path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(self.path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    @property
    def file(self):
        return self._file

    def write(self, event: str, **fields: Any) -> None:
        try:
            parts = [f"+{time.monotonic() - self._t0:9.3f}s", time.strftime("%H:%M:%S"),
                     f"thread={threading.current_thread().name}", event]
            parts += [f"{k}={_safe(v)}" for k, v in fields.items()]
            line = " ".join(parts) + "\n"
            with self._lock:
                if self._file.closed:
                    return
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
        except Exception:       # noqa: BLE001  埋点绝不打断被观察的流程
            pass

    def close(self) -> None:
        with self._lock:
            try:
                self._file.close()
            except Exception:   # noqa: BLE001
                pass


# ---------- 退出与信号 ----------

def install_exit_hooks(trace: TraceLog, os_module=os, sys_module=sys) -> Callable[[], None]:
    """包装 os._exit / sys.exit / os.kill:先把「谁在什么栈上调用」落盘,再执行原函数。返回卸载函数。"""
    originals = {("os", "_exit"): os_module._exit, ("sys", "exit"): sys_module.exit, ("os", "kill"): os_module.kill}

    def wrapped_os_exit(code=0):
        trace.write("exit_call", via="os._exit", code=code, stack=stack_summary())
        return originals[("os", "_exit")](code)

    def wrapped_sys_exit(code=0):
        trace.write("exit_call", via="sys.exit", code=code, stack=stack_summary())
        return originals[("sys", "exit")](code)

    def wrapped_kill(pid, sig):
        trace.write("kill_call", pid=pid, sig=sig, stack=stack_summary())
        return originals[("os", "kill")](pid, sig)

    os_module._exit, sys_module.exit, os_module.kill = wrapped_os_exit, wrapped_sys_exit, wrapped_kill

    def uninstall() -> None:
        if os_module._exit is wrapped_os_exit:
            os_module._exit = originals[("os", "_exit")]
        if sys_module.exit is wrapped_sys_exit:
            sys_module.exit = originals[("sys", "exit")]
        if os_module.kill is wrapped_kill:
            os_module.kill = originals[("os", "kill")]

    return uninstall


def install_signal_logging(trace: TraceLog, signal_module=signal) -> None:
    """SIGTERM/SIGINT 到达时先记一行再交给原处理器(原来是默认行为则恢复默认并重发)。非主线程无法设置信号,静默跳过。"""
    for name in ("SIGTERM", "SIGINT"):
        signum = getattr(signal_module, name, None)
        if signum is None:
            continue
        holder: dict = {}

        def handler(received, frame, _holder=holder):
            trace.write("signal_received", signum=received, stack=stack_summary())
            prev = _holder.get("prev")
            if callable(prev):
                return prev(received, frame)
            default = getattr(signal_module, "SIG_DFL", None)
            try:
                signal_module.signal(received, default)
                os.kill(os.getpid(), received)
            except Exception:       # noqa: BLE001
                pass

        try:
            holder["prev"] = signal_module.signal(signum, handler)
        except (ValueError, OSError):
            trace.write("signal_hook_unavailable", signal=name)


def install_fault_dumps(trace: TraceLog) -> Callable[[], None]:
    """致命信号(SIGSEGV/SIGABRT/…)时把所有线程的 Python 栈写进 trace 文件。"""
    try:
        faulthandler.enable(file=trace.file, all_threads=True)
    except Exception as exc:        # noqa: BLE001
        trace.write("fault_dump_unavailable", error=repr(exc))
        return lambda: None

    def uninstall() -> None:
        try:
            faulthandler.cancel_dump_traceback_later()
            faulthandler.disable()
        except Exception:           # noqa: BLE001
            pass

    return uninstall


def dump_threads_later(trace: TraceLog, seconds: float, repeat: bool = True) -> None:
    """`seconds` 秒后(可重复)转储所有线程的栈,用来抓「挂住」的位置。"""
    try:
        faulthandler.dump_traceback_later(seconds, repeat=repeat, file=trace.file)
    except Exception as exc:        # noqa: BLE001
        trace.write("dump_later_unavailable", error=repr(exc))


def cancel_thread_dumps() -> None:
    try:
        faulthandler.cancel_dump_traceback_later()
    except Exception:               # noqa: BLE001
        pass


# ---------- 已释放 API 探针 ----------

class ReleasedApiProbe:
    """包装 API 对象的若干方法:`mark_released()` 之后再被调用就记录调用栈;`close` 无论何时被调用都记录(抓重复关闭)。

    先落盘再转发:若转发进去的是已释放的原生对象而崩溃,崩溃前的那一行已经在文件里。"""

    def __init__(self, trace: TraceLog, api: Any, name: str, methods: Sequence[str]) -> None:
        self.trace, self.name = trace, name
        self._released = False
        self._close_calls = 0
        for method in methods:
            try:
                original = getattr(api, method)
            except AttributeError:
                continue
            try:
                setattr(api, method, self._wrap(method, original))
            except (AttributeError, TypeError):
                trace.write("probe_unavailable", api=name, method=method)

    def mark_released(self) -> None:
        self._released = True
        self.trace.write("api_marked_released", api=self.name)

    def _wrap(self, method: str, original: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            if method == "close":
                self._close_calls += 1
                self.trace.write("api_close_called", api=self.name, released=self._released,
                                 count=self._close_calls, stack=stack_summary())
            if self._released:
                self.trace.write("call_after_release", api=self.name, method=method, stack=stack_summary())
            return original(*args, **kwargs)

        return wrapper


# ---------- 周期快照 / 登录间隔 ----------

class PeriodicSnapshot:
    def __init__(self, trace: TraceLog, label: str, sampler: Callable[[], dict], interval: float = 10.0) -> None:
        self.trace, self.label, self.sampler, self.interval = trace, label, sampler, interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"snapshot-{label}", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.trace.write("snapshot", label=self.label, **self.sampler())
            except Exception as exc:    # noqa: BLE001  采样器出错只记一行,不杀线程
                self.trace.write("snapshot_error", label=self.label, error=repr(exc))


class LoginGap:
    """记录上一次登出的时刻,下次启动时算出「距上次登出多久」,用来把登录限流和别的问题分开。"""

    def __init__(self, path) -> None:
        self.path = Path(path)

    def mark_logout(self, now: float | None = None) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"logout_at": time.time() if now is None else now}), encoding="utf-8")
        except OSError:
            pass

    def seconds_since_last_logout(self, now: float | None = None) -> float | None:
        try:
            logout_at = float(json.loads(self.path.read_text(encoding="utf-8"))["logout_at"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return (time.time() if now is None else now) - logout_at


def thread_names() -> list[str]:
    return sorted(t.name for t in threading.enumerate())


# ---------- 装配 ----------

class Session:
    def __init__(self, trace: TraceLog, uninstallers: list[Callable[[], None]]) -> None:
        self.trace, self._uninstallers = trace, uninstallers

    def close(self) -> None:
        self.trace.write("trace_end")
        for uninstall in reversed(self._uninstallers):
            try:
                uninstall()
            except Exception:       # noqa: BLE001
                pass
        self.trace.close()


def setup(path: str | None) -> Session | None:
    """没有 trace 路径就什么都不装(返回 None);有则安装退出钩子、致命信号转储、信号日志与 atexit 记录。"""
    if not path:
        return None
    trace = TraceLog(path)
    uninstallers: list[Callable[[], None]] = []
    for installer in (install_exit_hooks, install_fault_dumps):
        undo = installer(trace)
        if callable(undo):
            uninstallers.append(undo)
    install_signal_logging(trace)
    atexit.register(lambda: trace.write("atexit"))
    trace.write("trace_start", pid=os.getpid(), python=sys.version.split()[0], argv=" ".join(sys.argv[1:]))
    return Session(trace, uninstallers)
