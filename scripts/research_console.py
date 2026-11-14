"""研究侧脚本的 stdout/stderr 编码修正。

**为什么需要它**:`scripts/bootstrap.py` 在导入时会把 stdout/stderr 重配成
`errors="replace"`——这是 Windows 控制台(cp1252)与子进程管道里打印中文不崩的关键。
研究侧不 import bootstrap(那会把 vnpy 交易栈与 .env 拖进来,见 ADR
`20261113-dsh-research-entry-and-out-of-band-approval` 的安全约束),就得自己补上这一条,
否则网关/CLI/批准进程一打印中文就 `UnicodeEncodeError` 退出。

**为什么不在库模块里做**:只在**进程入口**(`__main__`)调用,不放在 import 期。
放在 import 期会连 pytest 的捕获流一起改,干扰测试自身的编码处理。
"""
from __future__ import annotations

import sys


def reconfigure_stdio() -> None:
    """把 stdout/stderr 重配成 utf-8 + errors="replace"(无法重配时静默跳过)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
