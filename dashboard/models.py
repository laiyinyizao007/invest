"""区块(Section)构造与共用文件读取。契约见 docs/web/02-api-contract.md §3。

核心原则:**缺失、过期、损坏永远不是 ok。** 每个 collector 都经由这里产出 Section,
`data` 在 missing / corrupt 时恒为 None。
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 固定 UTC+8,**不用** `ZoneInfo("Asia/Shanghai")`:
# ZoneInfo 要读时区数据库,Windows 上常常只有用户目录里那份 `tzdata`,
# 而以 `-I` 隔离模式启动的进程(仪表盘冒烟 `scripts/status_server.py`)拿不到用户目录 ——
# 一 import 就 `ZoneInfoNotFoundError`,整个仪表盘起不来
# (2026-11-16 codex 复核 H1:`python -I -c "from dashboard import collectors"` 复现)。
# 中国最后一段夏令时 1991-09-15 才结束(1991-04-14 是**开始**日),自那以后固定偏移与
# `Asia/Shanghai` 等价,
# 而且不依赖任何外部数据。
BEIJING = timezone(timedelta(hours=8), "北京时间")
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


# state 决定**内容能不能看**(missing/corrupt 时 data 为 null),level 决定**徽标长什么样**。
# 两者分开,前端才能区分『数据是坏的』(state=unknown)和『有些内容没列出来/没检查』(state=ok + level=warn)。
_STATE_LEVEL = {"ok": "ok", "stale": "warn", "missing": "unknown", "corrupt": "unknown", "unknown": "unknown"}


def section(state: str, data: Any = None, *, as_of: float | None = None, now: float, stale_after: float | None = None,
            message: str | None = None, level: str | None = None) -> dict:
    """构造区块外壳。as_of / now 为 epoch 秒;age 由服务器计算。

    `level` 缺省按 state 推导;需要区分『没检查/没列出』与『读不出来』时显式给。
    """
    age = None if as_of is None else round(max(now - as_of, 0.0), 1)
    return {
        "state": state,
        "level": level if level is not None else _STATE_LEVEL.get(state, "unknown"),
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
        except (PermissionError, OSError, BadEncoding):          # 读写撞车可能读到被截断的多字节字符,所以重试一次
            last_error = "corrupt"
        else:
            try:
                return loads(text), None
            except (ValueError, RecursionError):                 # 深层嵌套会触发 RecursionError(不是 ValueError)
                last_error = "corrupt"
        if attempt < retries:
            time.sleep(pause)
    return None, last_error


class Untrusted(Exception):
    """文件读到了,但内容不可信(过大、编码非法)。调用方一律按 corrupt 处理,不能『尽量读』后当作正常数据。"""


class TooLarge(Untrusted):
    """文件超过读取上限。上限是防止异常增长的文件在单次请求里耗尽内存/CPU(面板与交易系统同机)。"""


class BadEncoding(Untrusted):
    """不是合法 UTF-8。曾用 errors='replace' 把非法字节换成 U+FFFD 继续解析,文件被『修复』后可能仍然合法并被判正常。"""


def loads(text: str) -> Any:
    """面板里所有 JSON 解析的唯一入口:拒绝 NaN/Infinity 常量与溢出为 inf 的数字。深层嵌套抛 RecursionError,由调用方按损坏处理。"""
    return json.loads(text, parse_constant=_reject_constant, parse_float=_finite_float)


def _finite_float(text: str) -> float:
    """`1e999` 是合法数字语法,Python 把它解析成 inf,不经过 parse_constant。"""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"数值溢出: {text[:20]}")
    return value


def _reject_constant(name: str):
    """JSON 标准里没有 NaN / Infinity,Python 默认却接受。它们说明写入方出了问题,不能当作合法数据。"""
    raise ValueError(f"JSON 不允许常量 {name}")


def read_text_limited(path: Path, limit: int) -> str:
    """最多读 limit 字节;文件更大就抛 TooLarge,**不截断后继续当作正常数据**。FileNotFoundError / OSError 原样抛出。"""
    with path.open("rb") as fh:
        raw = fh.read(limit + 1)
    if len(raw) > limit:
        raise TooLarge(f"{path.name} 超过 {limit} 字节上限")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BadEncoding(f"{path.name} 不是合法 UTF-8") from exc


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
