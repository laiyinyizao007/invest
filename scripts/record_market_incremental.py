"""运行有界、可重入的行情增量任务。

该入口供 DSH cron 调用，不承载常驻进程。每次运行只下载指定品种和周期，
通过原子锁避免重入，并把最近一次结果写入状态文件。
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap
from record_market_data_wrapper import pid_alive

FETCH_SCRIPT = Path(__file__).with_name("fetch_data.py")
DEFAULT_STATE_DIR = bootstrap.ROOT / ".ops" / "recording"
DEFAULT_SYMBOLS = ("C", "MA")
DEFAULT_PERIODS = ("1", "15")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lock_is_stale(path: Path, max_age_seconds: float) -> bool:
    """锁属于已退出的进程,或存在时间超过任务上限(PID 可能已被复用)时视为陈旧。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data.get("pid", 0))
        started = datetime.fromisoformat(data["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        # 内容损坏的锁无法证明属于存活进程;按文件修改时间兜底
        try:
            return time.time() - path.stat().st_mtime > max_age_seconds
        except OSError:
            return False
    if not pid_alive(pid):
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > max_age_seconds


def acquire_lock(path: Path, stale_after: float | None = None) -> bool | str:
    """原子加锁。返回 True=拿到锁,False=被占用,"recovered"=清理陈旧锁后拿到。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    recovered = False
    for _attempt in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if stale_after is None or recovered or not lock_is_stale(path, stale_after):
                return False
            release_lock(path)
            recovered = True
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "started_at": now()}, stream)
        return "recovered" if recovered else True
    return False


def release_lock(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def build_command(symbols: Sequence[str], periods: Sequence[str]) -> list[str]:
    return [
        sys.executable,
        str(FETCH_SCRIPT),
        "--symbols",
        *symbols,
        "--periods",
        *periods,
    ]


@dataclass
class BoundedResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/pid", str(pid), "/T", "/F"],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def minimal_env(source: dict[str, str]) -> dict[str, str]:
    """下载任务不需要柜台凭证:去掉 CTP_* 后其余环境(代理等)原样保留。"""
    return {k: v for k, v in source.items() if not k.upper().startswith("CTP_")}


def run_bounded(command: Sequence[str], *, cwd: Path, env: dict[str, str], timeout: int) -> BoundedResult:
    """有界运行:输出写临时文件而不是管道,超时后杀整棵进程树。

    不用 subprocess.run(capture_output=True, timeout=):Windows venv 下 python.exe 是启动器,
    超时只会杀外层,孙进程仍握着管道,communicate() 会一直等到孙进程退出才返回。
    """
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        child = subprocess.Popen(
            list(command), cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **kwargs
        )
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_tree(child.pid)
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        returncode = child.returncode if child.returncode is not None else -1
        out.seek(0)
        err.seek(0)
        return BoundedResult(
            returncode=returncode,
            stdout=out.read().decode("utf-8", errors="replace"),
            stderr=err.read().decode("utf-8", errors="replace"),
            timed_out=timed_out,
        )


def run_incremental(
    symbols: Sequence[str],
    periods: Sequence[str],
    state_dir: Path,
    timeout: int,
) -> int:
    state_dir = state_dir.resolve()
    lock = state_dir / "incremental.lock"
    result_path = state_dir / "incremental.json"
    # 锁的最长合法存活期 = 任务超时 + 杀进程树/写结果的余量
    acquired = acquire_lock(lock, stale_after=timeout + 60)
    if not acquired:
        write_json(result_path, {"at": now(), "status": "locked"})
        return 2
    recovered = acquired == "recovered"

    command = build_command(symbols, periods)
    started_at = now()
    try:
        outcome = run_bounded(command, cwd=bootstrap.ROOT, env=minimal_env(dict(os.environ)), timeout=timeout)
        payload = {
            "started_at": started_at,
            "finished_at": now(),
            "symbols": list(symbols),
            "periods": list(periods),
            "command": command,
            "stale_lock_recovered": recovered,
            "stdout_tail": outcome.stdout[-2000:],
            "stderr_tail": outcome.stderr[-2000:],
        }
        if outcome.timed_out:
            payload.update(status="timeout", timeout_seconds=timeout)
            write_json(result_path, payload)
            return 124
        payload.update(status="succeeded" if outcome.returncode == 0 else "failed", returncode=outcome.returncode)
        write_json(result_path, payload)
        return outcome.returncode
    finally:
        release_lock(lock)


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--symbols", nargs="+", choices=("C", "MA"), default=DEFAULT_SYMBOLS)
    parser.add_argument("--periods", nargs="+", choices=("1", "5", "15"), default=DEFAULT_PERIODS)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--timeout", type=int, default=900)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.timeout <= 0:
        print("--timeout 必须为正整数", file=sys.stderr)
        return 2
    state_dir = (bootstrap.ROOT / args.state_dir).resolve() if not args.state_dir.is_absolute() else args.state_dir
    return run_incremental(args.symbols, args.periods, state_dir, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
