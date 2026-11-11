"""手动暂停 / 恢复新开仓(应急处置;国信程序化测试 5.1「暂停交易功能」)。

用法(项目根目录,在运行机上):
    python scripts/pause_trading.py on [--reason 原因]   # 暂停:新开仓一律拒绝,平仓与持仓管理照常
    python scripts/pause_trading.py off                   # 恢复
    python scripts/pause_trading.py status                # 查看当前是否暂停

机制:在状态目录(默认 data/state/,与 account_guard.json 同目录)写/删 `pause_trading.flag`;运行中的
`AccountGuardRule` 在每次发单检查时读取它,切换时发一次告警。不停进程(停进程会让持仓失去本地止损),
也不开远程入口:谁能写这台机器的文件,谁才能暂停——权限边界就是本机文件权限(见 ADR 20261111-broker-test-compliance)。
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

import bootstrap

from trading.guard_rule import PAUSE_FLAG_NAME

DEFAULT_STATE_DIR = bootstrap.ROOT / "data" / "state"


def pause(state_dir: Path, reason: str = "", now: datetime | None = None) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    flag = state_dir / PAUSE_FLAG_NAME
    flag.write_text(f"{(now or datetime.now()).isoformat(timespec='seconds')} {reason}".strip() + "\n", encoding="utf-8")
    return flag


def resume(state_dir: Path) -> bool:
    flag = state_dir / PAUSE_FLAG_NAME
    if not flag.exists():
        return False
    flag.unlink()
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("on", "off", "status"))
    parser.add_argument("--reason", default="", help="暂停原因(写入标志文件,便于事后追溯)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)

    flag = args.state_dir / PAUSE_FLAG_NAME
    try:
        if args.action == "on":
            pause(args.state_dir, args.reason)
            print(f"已暂停新开仓({flag})。运行中的进程会在下一次发单检查/定时器周期内生效;平仓与持仓管理照常。"
                  "(前提:运行中的进程配置了状态文件 state_file,否则它看不到这个标志)")
        elif args.action == "off":
            print("已恢复。" if resume(args.state_dir) else "当前没有暂停标志,无需恢复。")
        else:
            print(f"状态:{'暂停中 — ' + flag.read_text(encoding='utf-8').strip() if flag.exists() else '未暂停'}")
    except OSError as exc:
        # 应急操作失败时给清楚的提示和非零退出码,不要把栈回溯甩给操作员(Windows 上文件被占用会 PermissionError)
        print(f"操作失败({args.action}):{exc!r}。标志文件 {flag} 可能被其它程序占用或没有权限,请关闭占用后重试。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
