"""人工解除账户的累计死线硬锁定。

硬锁定表示权益从最高点回撤 >= 2000 元,系统已平仓并停止交易;需要人来判断原因后才能恢复。
解锁前请先核对:账户实际权益与持仓、策略是否在回测之外的行情里失效、是否有数据/代码问题。

用法(项目根目录):
    python scripts/unlock_account.py            # 显示当前状态,交互确认后解锁
    python scripts/unlock_account.py --yes      # 跳过交互确认(仅用于脚本化场景)

解锁会把"权益最高点"重置为下一次采样的权益,所以 2000 元的回撤是从解锁后重新计算的。
"""
import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import bootstrap  # noqa: F401

from risk.account_guard import AccountGuard, AccountLimits

DEFAULT_STATE = bootstrap.ROOT / "data" / "state" / "account_guard.json"


def unlock(state_path: Path, confirm: Callable[[str], str] | None = None, assume_yes: bool = False) -> int:
    if not state_path.exists():
        print(f"没有找到状态文件 {state_path},无需解锁。")
        return 0
    guard = AccountGuard(AccountLimits(), state_path)
    if not guard.hard_locked:
        print("账户没有处于硬锁定状态,无需解锁。")
        return 0
    print(f"当前硬锁定原因: {guard.hard_reason}")
    print(f"记录的权益最高点: {guard.peak_equity:.0f}  最近一次权益: {guard.last_equity:.0f}")
    if not assume_yes:
        answer = (confirm or input)("已核对账户并确认要解锁?输入 UNLOCK 继续: ")
        if answer.strip() != "UNLOCK":
            print("已取消,未解锁。")
            return 1
    guard.unlock_hard()
    if guard.save_error:
        print(f"解锁失败(写文件出错): {guard.save_error}", file=sys.stderr)
        return 2
    print("已解锁。权益最高点将在下次采样时重置。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    return unlock(args.state, assume_yes=args.yes)


if __name__ == "__main__":
    sys.exit(main())
