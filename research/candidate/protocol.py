"""父子进程之间的**帧协议**(候选进程隔离的传输入口)。

形态(ADR `20261115-candidate-process-isolation.md` 第 3.3 节定稿):
- **控制面**:JSON(UTF-8),一行一帧,带固定 `magic`;
- **数据面**:长度前缀二进制帧(4 字节大端长度 + 载荷)。

**为什么不是 pickle**:反序列化即执行,是 Issue 点名的攻击面 ⇒ 绝对不用。
**为什么不是"一行 JSON"**:网格全跑时序列总量约 11 MB,一行 JSON 不现实(ADR 9.1 R1)。

请求方向(框架 → 候选),两帧:
1. 控制帧 JSON:`{magic, class, params, interval, n_bars, source, runtime}`
   - `source`:候选源码**文本**(父进程读过并算过哈希的那一份,不再二次读文件 ⇒ 消灭 TOCTOU);
   - `runtime`:候选侧运行时包的文件名 → 源码文本(父进程把 `research/candidate/` 逐个文件送来,
     候选进程在自己的临时目录里落盘,于是**候选进程的 `sys.path` 里没有任何仓库路径**);
2. 数据帧:`n_bars` 根 K 线的紧凑二进制(每根 66 字节)。

响应方向(候选 → 框架),两帧:
1. 控制帧 JSON:`{magic, status, error, n_bars}`(`status` 为 `ok` / `error`);
2. 数据帧:`n_bars` 个 `int8` 的入场意图(+1 / 0 / -1)。

**候选进程只产出信号**:`sharpe`、`dsr`、`maxdd` 这类数字**不在协议里**,
框架收到也无处可取(ADR 验收标准 A3)。
"""
from __future__ import annotations

import json
import struct
from datetime import datetime

from .bars import Bar

MAGIC_REQUEST = "CANDIDATE_REQUEST_V1"
MAGIC_RESPONSE = "CANDIDATE_SIGNALS_V1"

# 单帧上限:候选进程返回的东西不该有几 MB 那么大(信号序列 32k 根 × 1 字节 ≈ 32 KB)。
# 超限一律拒绝(fail-closed),而不是"截断后继续"。
MAX_FRAME_BYTES = 64 * 1024 * 1024

# 每根 K 线:7 个 float64 + 时间戳(年 uint32 + 月/日/时/分/秒/微秒 各 uint8)
BAR_STRUCT = struct.Struct("<7dI6B")
BAR_SIZE = BAR_STRUCT.size          # 66

# 信号取值域:多 / 空 / 无
SIGNAL_VALUES = (-1, 0, 1)


class ProtocolError(RuntimeError):
    """帧格式错误(长度越界、截断、magic 不符)。一律 fail-closed。"""


# ---------- 帧 ----------

def encode_frame(payload: bytes) -> bytes:
    """一帧 = 4 字节大端长度 + 载荷。"""
    if len(payload) > MAX_FRAME_BYTES:
        raise ProtocolError(f"帧长度 {len(payload)} 超过上限 {MAX_FRAME_BYTES}")
    return len(payload).to_bytes(4, "big") + payload


def read_frame(stream) -> bytes:
    """从二进制流读一帧。截断或超限一律抛 ProtocolError。"""
    head = stream.read(4)
    if len(head) != 4:
        raise ProtocolError("帧头不完整(进程提前退出?)")
    size = int.from_bytes(head, "big")
    if size > MAX_FRAME_BYTES:
        raise ProtocolError(f"帧长度 {size} 超过上限 {MAX_FRAME_BYTES}")
    payload = stream.read(size)
    if len(payload) != size:
        raise ProtocolError(f"帧载荷不完整(期望 {size} 字节,实得 {len(payload)})")
    return payload


def write_frame(stream, payload: bytes) -> None:
    stream.write(encode_frame(payload))
    stream.flush()


# ---------- 控制面 ----------

def encode_json(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def decode_json(payload: bytes) -> dict:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"控制帧不是合法 JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("控制帧必须是 JSON 对象")
    return obj


# ---------- 数据面:K 线 ----------

def pack_bars(bars) -> bytes:
    """把 K 线序列压成紧凑二进制。

    时间戳按**墙钟分量**(年/月/日/时/分/秒/微秒)传,不用 epoch:
    行情 K 线在本仓库里是**不带时区**的 naive datetime(`slice_bars` 拿它和
    `datetime(2024,1,1)` 比大小),走 epoch 会把本地时区与夏令时搅进来。
    """
    out = bytearray()
    for bar in bars:
        dt = getattr(bar, "datetime", None)
        if not isinstance(dt, datetime):
            raise ProtocolError("K 线缺少 datetime")
        out += BAR_STRUCT.pack(
            float(bar.open_price), float(bar.high_price), float(bar.low_price), float(bar.close_price),
            float(bar.volume), float(bar.turnover), float(bar.open_interest),
            dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond,
        )
    return bytes(out)


def unpack_bars(payload: bytes, count: int, interval: str = "") -> list[Bar]:
    """把紧凑二进制还原成候选侧的 `Bar` 列表。"""
    if count < 0:
        raise ProtocolError("K 线根数非法")
    if len(payload) != count * BAR_SIZE:
        raise ProtocolError(f"K 线字节数不符(期望 {count * BAR_SIZE},实得 {len(payload)})")
    bars: list[Bar] = []
    for index in range(count):
        chunk = payload[index * BAR_SIZE:(index + 1) * BAR_SIZE]
        (op, hp, lp, cp, vol, turn, oi, year, month, day, hour, minute, second, micro) = BAR_STRUCT.unpack(chunk)
        bars.append(Bar(
            open_price=op, high_price=hp, low_price=lp, close_price=cp,
            volume=vol, turnover=turn, open_interest=oi,
            datetime=datetime(year, month, day, hour, minute, second, micro),
            interval=interval,
        ))
    return bars


# ---------- 数据面:信号 ----------

def pack_signals(signals) -> bytes:
    out = bytearray()
    for value in signals:
        iv = int(value)
        if iv not in SIGNAL_VALUES:
            raise ProtocolError(f"信号取值非法: {iv!r}(只允许 {SIGNAL_VALUES})")
        out.append(iv & 0xFF)              # int8 补码:-1 → 0xFF(与 unpack_signals 的符号扩展配对)
    return bytes(out)


def unpack_signals(payload: bytes, count: int) -> list[int]:
    """还原信号序列。长度或取值域不符一律抛 ProtocolError(整格判失败,不截断)。"""
    if len(payload) != count:
        raise ProtocolError(f"信号序列长度不符(期望 {count},实得 {len(payload)})")
    signals = []
    for raw in payload:
        value = int(raw) - 256 if raw > 127 else int(raw)      # int8 的符号扩展
        if value not in SIGNAL_VALUES:
            raise ProtocolError(f"信号取值非法: {value!r}(只允许 {SIGNAL_VALUES})")
        signals.append(value)
    return signals
