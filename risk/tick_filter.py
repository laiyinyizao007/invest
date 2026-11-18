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

_EPS = 1e-9


@dataclass(frozen=True)
class TickRules:
    max_jump_pct: float              # 相对上一笔有效 tick 的最大价格变动比例(如 0.03 = 3%)
    max_clock_skew_seconds: float    # 交易所时间与本地接收时间的最大偏差

    def __post_init__(self):
        if not self.max_jump_pct > 0 or not self.max_clock_skew_seconds > 0:
            raise ValueError("阈值必须为正数")


@dataclass(frozen=True)
class TickVerdict:
    ok: bool
    reasons: tuple[str, ...]


def classify(tick, prev, *, pricetick: float, rules: TickRules) -> TickVerdict:
    if not pricetick > 0:
        raise ValueError("pricetick 必须为正数")
    reasons: list[str] = []
    price = tick.last_price
    if not isinstance(price, (int, float)) or math.isnan(price) or price <= 0:
        reasons.append(f"价格非正或非数:{price!r}")
        return TickVerdict(False, tuple(reasons))            # 后续检查都依赖有效价格

    up, down = getattr(tick, "limit_up", 0) or 0, getattr(tick, "limit_down", 0) or 0
    if (up > 0 and price > up + _EPS) or (down > 0 and price < down - _EPS):
        reasons.append(f"价格 {price} 越过涨跌停 [{down}, {up}]")

    steps = price / pricetick
    if abs(steps - round(steps)) > 1e-6:
        reasons.append(f"价格 {price} 与最小变动价位 {pricetick} 不对齐")

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
