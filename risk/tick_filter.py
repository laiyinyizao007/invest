"""行情异常值判定:把明显的坏 tick 标记出来,不喂给策略(路线图「实时 tick 异常值过滤」)。

本模块只提供**纯判定函数**;接入运行时的行情入口、具体阈值与「坏数据如何处理」(丢弃/告警/计数)涉及实盘路径,
按路线图要求另写 ADR,随阶段 F-b 一起定。所以阈值没有默认值,调用方必须显式给出。

约定:`prev` 是**同一交易日内上一笔判为正常的 tick**(由调用方维护;跨交易日传 None),
否则累计成交量在日切时的正常归零会被误判为回退。持仓量不做单调检查:有人平仓时它本来就会减少。

只依赖标准库;tick 按鸭子类型读取(vn.py 的 TickData 即可)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

_EPS = 1e-9


@dataclass(frozen=True)
class TickRules:
    max_jump_pct: float              # 相对上一笔有效 tick 的最大价格变动比例(如 0.03 = 3%)
    max_clock_skew_seconds: float    # 交易所时间与本地接收时间的最大偏差

    def __post_init__(self):
        for v in (self.max_jump_pct, self.max_clock_skew_seconds):
            if not (isinstance(v, (int, float)) and math.isfinite(v) and v > 0):
                raise ValueError(f"阈值必须是正的有限数(无穷大等于关掉检查):{v!r}")


@dataclass(frozen=True)
class TickVerdict:
    ok: bool
    reasons: tuple[str, ...]


def classify(tick, prev, *, pricetick: float, rules: TickRules) -> TickVerdict:
    if not (isinstance(pricetick, (int, float)) and math.isfinite(pricetick) and pricetick > 0):
        raise ValueError(f"pricetick 必须是正的有限数:{pricetick!r}")
    reasons: list[str] = []
    price = tick.last_price
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price <= 0:
        reasons.append(f"价格非正或非数:{price!r}")
        return TickVerdict(False, tuple(reasons))            # 后续检查都依赖有效价格

    up, down = getattr(tick, "limit_up", 0) or 0, getattr(tick, "limit_down", 0) or 0
    # 0/None 表示交易所没给涨跌停,跳过;给了就必须是有限数且 跌停 <= 涨停,否则涨跌停数据本身就坏了(NaN 比较恒为假会静默放行)
    limits_given = [x for x in (up, down) if x]
    if any(not math.isfinite(x) for x in limits_given) or (up and down and down > up):
        reasons.append(f"涨跌停数据异常:跌停 {down!r},涨停 {up!r}")
    elif (up > 0 and price > up + _EPS) or (down > 0 and price < down - _EPS):
        reasons.append(f"价格 {price} 越过涨跌停 [{down}, {up}]")

    steps = price / pricetick
    if abs(steps - round(steps)) > 1e-6:
        reasons.append(f"价格 {price} 与最小变动价位 {pricetick} 不对齐")

    vol, ts = getattr(tick, "volume", None), getattr(tick, "datetime", None)
    if isinstance(vol, bool) or not isinstance(vol, (int, float)) or not math.isfinite(vol) or not isinstance(ts, datetime):
        reasons.append(f"成交量或时间字段缺失或类型异常:volume={vol!r},datetime={ts!r}")
        return TickVerdict(False, tuple(reasons))             # 下面的比较都依赖这两个字段

    if prev is not None:
        base = prev.last_price
        if base and base > 0 and abs(price - base) / base > rules.max_jump_pct + _EPS:
            reasons.append(f"价格跳变 {base} → {price}({(price - base) / base:+.2%})超过 {rules.max_jump_pct:.2%}")
        if tick.volume < prev.volume:
            reasons.append(f"累计成交量回退:{prev.volume} → {tick.volume}")
        if tick.datetime < prev.datetime:
            reasons.append(f"时间倒退:{prev.datetime} → {tick.datetime}")

    local = getattr(tick, "localtime", None)
    if local is not None:
        skew = abs((tick.datetime - local).total_seconds())
        if skew > rules.max_clock_skew_seconds:
            reasons.append(f"交易所时间与本地接收时间偏差 {skew:.1f} 秒,超过 {rules.max_clock_skew_seconds} 秒(时钟偏差)")

    return TickVerdict(not reasons, tuple(reasons))
