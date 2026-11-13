"""区块(Section)构造与共用文件读取。契约见 docs/web/02-api-contract.md §3。

核心原则:**缺失、过期、损坏永远不是 ok。** 每个 collector 都经由这里产出 Section,
`data` 在 missing / corrupt 时恒为 None。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")
LEVELS = ("ok", "warn", "critical", "unknown")
_RANK = {"ok": 0, "warn": 1, "unknown": 2, "critical": 3}     # unknown 比 warn 更糟:不知道比知道有点问题更危险


def iso(ts: float) -> str:
    """epoch 秒 -> 北京时间 ISO 8601(带偏移)。"""
    return datetime.fromtimestamp(ts, BEIJING).isoformat(timespec="seconds")


def worst(levels: list[str]) -> str:
    """取最差级别;空列表为 unknown(没有任何信息不是 ok)。"""
    known = [l if l in _RANK else "unknown" for l in levels]
    return max(known, key=_RANK.__getitem__) if known else "unknown"


@dataclass
class Clock:
    """可注入的时钟,便于测试。"""
    now: float = field(default_factory=time.time)

    def __call__(self) -> float:
        return self.now


def section(state: str, data: Any = None, *, as_of: float | None = None, now: float, stale_after: float | None = None,
            message: str | None = None) -> dict:
    """构造区块外壳。as_of / now 为 epoch 秒;age 由服务器计算。"""
    age = None if as_of is None else round(max(now - as_of, 0.0), 1)
    return {
        "state": state,
        "as_of": None if as_of is None else iso(as_of),
        "age_seconds": age,
        "stale_after_seconds": stale_after,
        "message": message,
        "data": None if state in ("missing", "corrupt") else data,
    }


def freshness(as_of: float | None, now: float, stale_after: float) -> str:
    """按年龄判定 ok / stale;as_of 为空 -> unknown。未来时间戳(时钟回拨)按 0 岁算,但不视为更新鲜的证据。"""
    if as_of is None:
        return "unknown"
    return "ok" if max(now - as_of, 0.0) <= stale_after else "stale"


def read_json(path: Path, *, retries: int = 1, pause: float = 0.05) -> tuple[Any, str | None]:
    """读 JSON 文件。返回 (数据, 错误类别)。错误类别:None / "missing" / "corrupt"。

    状态文件是原子替换写入的,但 Windows 上读写撞车时可能短暂读到空文件或权限错误,
    所以失败后重试一次再判定为 corrupt。
    """
    last_error = "corrupt"
    for attempt in range(retries + 1):
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, "missing"
        except (PermissionError, OSError):
            last_error = "corrupt"
        else:
            try:
                return json.loads(text), None
            except ValueError:
                last_error = "corrupt"
        if attempt < retries:
            time.sleep(pause)
    return None, last_error


def mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def parse_iso(value: Any) -> float | None:
    """解析 ISO 时间字符串为 epoch 秒;无时区的按 UTC(录制进程写的是 UTC)。解析失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def start_of_beijing_day(now: float) -> float:
    d = datetime.fromtimestamp(now, BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


def days_ago(now: float, n: int) -> float:
    return now - timedelta(days=n).total_seconds()
