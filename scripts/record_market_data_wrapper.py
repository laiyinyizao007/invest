"""管理行情录制子进程的短任务 wrapper。

DSH 只调用本模块的 start/ensure/watchdog/stop，不直接承载长期 DataRecorder。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap

SCRIPT = Path(__file__).with_name("record_market_data.py")
DEFAULT_STATE = bootstrap.ROOT / ".ops" / "recording"
STARTUP_GRACE = 3.0              # 拉起后观察多久,期间退出视为启动失败
HEARTBEAT_STALE_SECONDS = 120.0  # 录制进程每 5 秒写心跳;超过此值视为卡死


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def paths(state_dir: Path) -> dict[str, Path]:
    return {
        "state": state_dir / "wrapper.json",
        "lock": state_dir / "wrapper.lock",
        "events": state_dir / "events.ndjson",
        "status": state_dir / "status.json",
    }


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def event(state_dir: Path, kind: str, **values: Any) -> None:
    record = {"at": now(), "event": kind, **values}
    path = paths(state_dir)["events"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def _pid_alive_windows(pid: int) -> bool:
    # os.kill(pid, 0) 在 Windows 上不是存活探测(会抛 WinError 87,且对已退出进程可能"成功"),
    # 必须用 OpenProcess + GetExitCodeProcess 判断。
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    return True


def process_identity(pid: int, expected_profile: str | None = None) -> bool:
    """Fail closed when the PID cannot be proven to be our recorder."""
    if not pid_alive(pid):
        return False
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                [
                    "powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Process -Filter 'ProcessId=%d').CommandLine" % pid,
                ],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=10,  # WMI/CIM 假死时 ensure/watchdog 仍须是有界短任务
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if str(SCRIPT).lower() not in output.lower():
            return False
    return True


def stop_signal(pid: int) -> None:
    if os.name == "nt":
        # CTRL_BREAK_EVENT only reaches a process group created with CREATE_NEW_PROCESS_GROUP.
        # The wrapper creates that group so graceful stop is deterministic on Windows.
        os.kill(pid, signal.CTRL_BREAK_EVENT)
    else:
        os.kill(pid, signal.SIGTERM)


def acquire_lock(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(json.dumps({"pid": os.getpid(), "at": now()}))
    return True


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def current(state_dir: Path) -> dict[str, Any] | None:
    data = read_json(paths(state_dir)["state"])
    if not data:
        return None
    pid = int(data.get("pid", 0))
    if not process_identity(pid, data.get("profile")):
        return None
    return data


def start(state_dir: Path, profile: str | None = None) -> int:
    state_dir.mkdir(parents=True, exist_ok=True)
    files = paths(state_dir)
    if current(state_dir):
        event(state_dir, "already_running", pid=read_json(files["state"])["pid"])
        return 0
    if not acquire_lock(files["lock"]):
        event(state_dir, "start_locked")
        return 2
    try:
        env = os.environ.copy()
        env["CTP_RECORD_STATE_DIR"] = str(state_dir)
        if profile:
            env["CTP_RECORD_PROFILE"] = profile
        command = [sys.executable, str(SCRIPT)]
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        # 不能继承调用方的 stdout/stderr:DSH 以捕获输出方式调用短任务时,
        # 常驻录制进程握着管道会让调用一直等不到 EOF。输出落到专用日志文件。
        with (state_dir / "recorder.log").open("ab") as log:
            child = subprocess.Popen(
                command,
                cwd=bootstrap.ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        data = {"pid": child.pid, "started_at": now(), "profile": profile or env.get("CTP_RECORD_PROFILE", "simnow"), "command": command}
        write_json(files["state"], data)
        event(state_dir, "started", pid=child.pid, profile=data["profile"])
        # 启动后短暂观察:配置错误等会让录制进程立即退出,不能报告"启动成功"
        deadline = time.monotonic() + STARTUP_GRACE
        while time.monotonic() < deadline:
            returncode = child.poll()
            if returncode is not None:
                event(state_dir, "start_failed", pid=child.pid, returncode=returncode)
                try:
                    files["state"].unlink()
                except FileNotFoundError:
                    pass
                return returncode or 3
            time.sleep(0.1)
        return 0
    finally:
        release_lock(files["lock"])


def heartbeat_age(state_dir: Path) -> float | None:
    """status.json 心跳距今秒数;文件缺失或无法解析返回 None。"""
    stamp = (read_json(paths(state_dir)["status"]) or {}).get("heartbeat_at")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()
    except (TypeError, ValueError):
        return None


def watchdog(state_dir: Path, profile: str | None = None) -> int:
    """进程不在 → 启动;进程在但心跳陈旧(卡死) → 受控重启;健康 → 不动。"""
    data = current(state_dir)
    if not data:
        return start(state_dir, profile)
    try:
        started = datetime.fromisoformat(data["started_at"])
        uptime = (datetime.now(timezone.utc) - started).total_seconds()
    except (KeyError, TypeError, ValueError):
        uptime = float("inf")
    # 新进程还没来得及写心跳时,status.json 可能是上次运行遗留的旧文件,不据此判断
    if uptime < HEARTBEAT_STALE_SECONDS:
        return 0
    age = heartbeat_age(state_dir)
    if age is not None and age <= HEARTBEAT_STALE_SECONDS:
        return 0
    event(state_dir, "heartbeat_stale", pid=data.get("pid"), age_seconds=age)
    stop(state_dir)
    return start(state_dir, profile)


def stop(state_dir: Path, timeout: float = 15.0) -> int:
    data = read_json(paths(state_dir)["state"])
    if not data:
        event(state_dir, "not_running")
        return 0
    pid = int(data.get("pid", 0))
    if not process_identity(pid, data.get("profile")):
        event(state_dir, "stale_or_unknown_pid", pid=pid)
        return 2
    try:
        stop_signal(pid)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.2)
    if pid_alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"], check=False)
        else:
            os.kill(pid, signal.SIGKILL)
        event(state_dir, "force_stopped", pid=pid)
    else:
        event(state_dir, "stopped", pid=pid)
    try:
        paths(state_dir)["state"].unlink()
    except FileNotFoundError:
        pass
    return 0


def status(state_dir: Path) -> int:
    data = current(state_dir)
    output = read_json(paths(state_dir)["status"]) or {}
    output["wrapper_running"] = bool(data)
    if data:
        output["wrapper_pid"] = data["pid"]
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("command", choices=("start", "ensure", "watchdog", "stop", "restart", "status"))
    p.add_argument("--state-dir", type=Path, default=DEFAULT_STATE)
    p.add_argument("--profile", choices=("simnow", "guoxin-test"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    state_dir = (bootstrap.ROOT / args.state_dir).resolve() if not args.state_dir.is_absolute() else args.state_dir
    if args.command in {"start", "ensure"}:
        return start(state_dir, args.profile)
    if args.command == "watchdog":
        return watchdog(state_dir, args.profile)
    if args.command == "stop":
        return stop(state_dir)
    if args.command == "restart":
        stop(state_dir)
        return start(state_dir, args.profile)
    return status(state_dir)


if __name__ == "__main__":
    raise SystemExit(main())
