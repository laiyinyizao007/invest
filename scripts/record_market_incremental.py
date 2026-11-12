"""运行有界、可重入的行情增量任务。

该入口供 DSH cron 调用，不承载常驻进程。每次运行只下载指定品种和周期，
通过原子锁避免重入，并把最近一次结果写入状态文件。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bootstrap

FETCH_SCRIPT = Path(__file__).with_name("fetch_data.py")
DEFAULT_STATE_DIR = bootstrap.ROOT / ".ops" / "recording"
DEFAULT_SYMBOLS = ("C", "MA")
DEFAULT_PERIODS = ("1", "15")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def acquire_lock(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"pid": os.getpid(), "started_at": now()}, stream)
    return True


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


def run_incremental(
    symbols: Sequence[str],
    periods: Sequence[str],
    state_dir: Path,
    timeout: int,
) -> int:
    state_dir = state_dir.resolve()
    lock = state_dir / "incremental.lock"
    result_path = state_dir / "incremental.json"
    if not acquire_lock(lock):
        write_json(result_path, {"at": now(), "status": "locked"})
        return 2

    command = build_command(symbols, periods)
    started_at = now()
    try:
        completed = subprocess.run(
            command,
            cwd=bootstrap.ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        status = "succeeded" if completed.returncode == 0 else "failed"
        write_json(
            result_path,
            {
                "started_at": started_at,
                "finished_at": now(),
                "status": status,
                "returncode": completed.returncode,
                "symbols": list(symbols),
                "periods": list(periods),
                "command": command,
                "stdout_tail": completed.stdout[-2000:],
                "stderr_tail": completed.stderr[-2000:],
            },
        )
        return completed.returncode
    except subprocess.TimeoutExpired as exc:
        write_json(
            result_path,
            {
                "started_at": started_at,
                "finished_at": now(),
                "status": "timeout",
                "symbols": list(symbols),
                "periods": list(periods),
                "command": command,
                "timeout_seconds": timeout,
                "stderr_tail": str(exc),
            },
        )
        return 124
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
