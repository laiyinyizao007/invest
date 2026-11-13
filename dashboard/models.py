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
MAX_JSON_FILE_BYTES = 2_000_000          # 状态 JSON(风控状态、录制状态、批准记录、运行结果)的读取上限
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


CLOCK_SKEW_TOLERANCE = 60.0          # 允许文件时间戳比本机时钟超前这么多秒(两台机器/NTP 校时的正常误差)


def is_future(ts: float | None, now: float) -> bool:
    """时间戳超出时钟误差地在未来(时钟回拨或文件异常)——不能当作『刚更新』的证据。"""
    return ts is not None and ts - now > CLOCK_SKEW_TOLERANCE


def freshness(as_of: float | None, now: float, stale_after: float) -> str:
    """按年龄判定 ok / stale / corrupt / unknown。

    as_of 为空 -> unknown;**在未来(超出时钟误差)-> corrupt**,而不是按 0 岁算『新鲜』
    (G8 复核:日志文件的未来 mtime 曾被当成刚更新,可长期保持绿色)。未来检查放在这个共用函数里,新增数据来源不会漏。"""
    if as_of is None:
        return "unknown"
    if is_future(as_of, now):
        return "corrupt"
    return "ok" if max(now - as_of, 0.0) <= stale_after else "stale"


def read_json(path: Path, *, retries: int = 1, pause: float = 0.05) -> tuple[Any, str | None]:
    """读 JSON 文件。返回 (数据, 错误类别)。错误类别:None / "missing" / "corrupt"。

    状态文件是原子替换写入的,但 Windows 上读写撞车时可能短暂读到空文件或权限错误,
    所以失败后重试一次再判定为 corrupt。
    """
    last_error = "corrupt"
    for attempt in range(retries + 1):
        try:
            text = read_text_limited(path, MAX_JSON_FILE_BYTES)
        except FileNotFoundError:
            return None, "missing"
        except TooLarge:
            return None, "corrupt"                   # 状态 JSON 都很小;异常增长的文件不读,也不重试
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


class TooLarge(Exception):
    """文件超过读取上限。上限是防止异常增长的文件在单次请求里耗尽内存/CPU(面板与交易系统同机)。"""


def read_text_limited(path: Path, limit: int) -> str:
    """最多读 limit 字节;文件更大就抛 TooLarge,**不截断后继续当作正常数据**。FileNotFoundError / OSError 原样抛出。"""
    with path.open("rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise TooLarge(f"{path.name} 超过 {limit} 字节上限")
    return raw.decode("utf-8", errors="replace")


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
