"""批量撤单(应急处置;国信程序化测试 6.2「批量撤单功能」)。

用法(项目根目录,在运行机上):
    python scripts/cancel_all_orders.py [--reason 原因]

机制:在状态目录(默认 data/state/,与 account_guard.json 同目录)写一次性请求文件 `cancel_all.request`;
运行中的 `AccountGuardRule` 在下一次定时器周期(约 1 秒)撤销全部**开仓**挂单、删除请求并发一次告警。
平仓委托不撤(「平仓永远放行」:批量撤单不能让持仓失去离场委托)。
与暂停交易相同,不开远程入口:谁能写这台机器的文件,谁才能批量撤单(见 ADR 20261111-broker-test-compliance)。
"""
import argparse
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from trading.guard_rule import CANCEL_ALL_REASON_CHARS, CANCEL_ALL_REQUEST_NAME

DEFAULT_STATE_DIR = bootstrap.ROOT / "data" / "state"


def request(state_dir: Path, reason: str = "", now: datetime | None = None) -> Path:
    """写批量撤单请求;已有未处理的请求时覆盖(效果相同:都是撤销全部开仓挂单)。

    先写同目录临时文件再 `os.replace`:运行时要么看到完整的旧请求、要么看到完整的新请求,不会读到半截文件。
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / CANCEL_ALL_REQUEST_NAME
    reason = " ".join(reason.split())[:CANCEL_ALL_REASON_CHARS]
    text = f"{(now or datetime.now()).isoformat(timespec='seconds')} {reason}".strip() + "\n"
    fd, tmp = tempfile.mkstemp(prefix=CANCEL_ALL_REQUEST_NAME + ".", suffix=".tmp", dir=state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())                 # 应急请求:返回「已提交」前先落盘
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--reason", default="", help="撤单原因(写入请求文件与告警,便于事后追溯)")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)
    try:
        path = request(args.state_dir, args.reason)
    except OSError as exc:
        print(f"操作失败:{exc!r}。请求文件 {args.state_dir / CANCEL_ALL_REQUEST_NAME} 可能被占用或没有权限。", file=sys.stderr)
        return 1
    print(f"已提交批量撤单请求({path})。运行中的进程会在下一次定时器周期撤销全部开仓挂单(平仓委托不撤),"
          "结果见告警与委托回报。(前提:运行中的进程配置了状态文件 state_file,否则它看不到这个请求)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
