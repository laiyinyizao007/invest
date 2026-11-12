"""人工解除账户的累计死线硬锁定。

硬锁定表示权益从最高点回撤 >= 2000 元,系统已平仓并停止交易;需要人来判断原因后才能恢复。
解锁前请先核对:账户实际权益与持仓、策略是否在回测之外的行情里失效、是否有数据/代码问题。

用法(项目根目录):
    python scripts/unlock_account.py            # 显示当前状态,交互确认后解锁
    python scripts/unlock_account.py --yes      # 跳过交互确认(仅用于脚本化场景)
    python scripts/unlock_account.py --force    # 越过冷却期强制解锁(必须交互输入 FORCE,不能与 --yes 同用)

解锁会把"权益最高点"重置为下一次采样的权益,所以 2000 元的回撤是从解锁后重新计算的——
等于重新获得完整的预算。因此:硬锁定当个交易日内拒绝解锁(冷却期,隔夜冷静),每次解锁都写入解锁历史。
"""
import argparse
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401

from risk.account_guard import AccountGuard, AccountLimits

DEFAULT_STATE = bootstrap.ROOT / "data" / "state" / "account_guard.json"


EXIT_COOLDOWN = 3
EXIT_RUNNING = 4
HEARTBEAT_FRESH_SECONDS = 60.0     # 运行时每 10 秒刷新心跳;更新鲜说明进程还活着


def ask(confirm: Callable[[str], str] | None, prompt: str) -> str:
    """交互提问;stdin 已关闭(EOF)时按"未确认"处理,不抛栈回溯。"""
    try:
        return (confirm or input)(prompt)
    except EOFError:
        return ""


def unlock(state_path: Path, confirm: Callable[[str], str] | None = None, assume_yes: bool = False,
           force: bool = False, now: datetime | None = None, heartbeat_file: Path | None = None) -> int:
    if not state_path.exists():
        print(f"没有找到状态文件 {state_path},无需解锁。")
        return 0
    guard = AccountGuard(AccountLimits(), state_path)
    if not guard.hard_locked:
        print("账户没有处于硬锁定状态,无需解锁。")
        return 0
    # 运行中的进程每次权益变化/每 60 秒会用内存态整体覆盖状态文件:此时解锁会被回写成"仍硬锁定",
    # 解锁历史也会丢。硬锁定后进程若仍活着(例如平仓失败持续告警),更要先停进程再解锁。
    hb = heartbeat_file if heartbeat_file is not None else state_path.parent / "heartbeat"
    try:
        age = time.time() - hb.stat().st_mtime
    except OSError:
        age = None
    if age is not None and age < HEARTBEAT_FRESH_SECONDS:
        print(f"检测到运行时仍在运行(心跳 {age:.0f} 秒前刷新:{hb})。请先停止进程再解锁,"
              "否则解锁会被运行时的状态回写覆盖。", file=sys.stderr)
        return EXIT_RUNNING
    print(f"当前硬锁定原因: {guard.hard_reason}")
    print(f"记录的权益最高点: {guard.peak_equity:.0f}  最近一次权益: {guard.last_equity:.0f}")
    if guard.unlock_history:
        print(f"历次解锁(共 {len(guard.unlock_history)} 条,最近在前):")
        for h in reversed(guard.unlock_history[-5:]):
            print(f"  - {h['at']}{'(强制)' if h.get('forced') else ''}  原锁定: {h.get('reason', '')}")
    now = now or datetime.now()
    cooldown = guard.unlock_cooldown(now)
    forced = False
    if cooldown:
        if not force:
            print(f"冷却期内拒绝解锁:{cooldown}。", file=sys.stderr)
            return EXIT_COOLDOWN
        if assume_yes:
            print("强制解锁不能与 --yes 同用:越过冷却期必须由人交互输入 FORCE。", file=sys.stderr)
            return EXIT_COOLDOWN
        answer = ask(confirm, "⚠ 仍在冷却期内。确认越过冷却期强制解锁?输入 FORCE 继续: ")
        if answer.strip() != "FORCE":
            print("已取消,未解锁。")
            return 1
        forced = True
    if not assume_yes:
        answer = ask(confirm, "已核对账户并确认要解锁?输入 UNLOCK 继续: ")
        if answer.strip() != "UNLOCK":
            print("已取消,未解锁。")
            return 1
    guard.unlock_hard(now, forced=forced)
    if guard.save_error:
        print(f"解锁失败(写文件出错): {guard.save_error}", file=sys.stderr)
        return 2
    print("已解锁。权益最高点将在下次采样时重置。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--force", action="store_true", help="越过冷却期强制解锁(需交互输入 FORCE)")
    args = parser.parse_args(argv)
    return unlock(args.state, assume_yes=args.yes, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
