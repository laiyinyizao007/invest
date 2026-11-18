"""交易日历与统一交易时段:全仓库唯一的「哪天交易、几点开盘、某时刻算哪个交易日」来源。

为什么要有它:此前仓库里有 7 份互相矛盾的时段/交易日定义(下午 13:00 还是 13:30、夜盘到 23:00 还是 02:30、
日界线 20 点还是 21 点、周末/节假日各自处理或不处理),而且**没有任何节假日表**——节前那天没有夜盘也无人知晓。
本模块只建立单一来源;把现有定义改为调用它是阶段 F-b 的工作(见 docs/plans/20261117-live-readiness-modules-plan.md)。

口径:
- 交易日 = 覆盖区间内、周一至周五、且不在休市表里的日子。区间外一律「未知」,**不猜**(fail-closed,调用方据此拒绝)。
  日历数据由 tools/build_trading_calendar.py 离线生成并入库(config/trading_calendar.json),运行时不联网、不依赖第三方库。
- 夜盘:D 日有夜盘 ⇔ D 是交易日,且 D 与下一个交易日之间只隔普通周末(或紧邻)。节前最后一个交易日没有夜盘由此推出。
- 交易日归属:20:00 及以后归入下一个日历日,再顺延到第一个交易日(与 account_guard / import_minute 的 20 点口径一致)。
- 时段:只录有依据的品种,表外品种报错。时段区间含起点、不含终点(与 strategies 里 ENTRY_SESSIONS 一致)。

只依赖标准库:研究侧也会用到它(研究栈不得 import 交易栈)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

DEFAULT_CALENDAR = Path(__file__).resolve().parents[1] / "config" / "trading_calendar.json"

TRADING, CLOSED, UNKNOWN = "trading", "closed", "unknown"      # 日期状态
OPEN = "open"                                                  # 时段状态:OPEN / CLOSED / UNKNOWN

NIGHT_CUTOFF = time(20, 0)          # 20:00 起归入下一个日历日
COVERAGE_LEAD_DAYS = 30             # 覆盖结束前多少天开始告警
_MAX_SCAN_DAYS = 40                 # 顺延找交易日的上限(最长长假也远小于它)


class CalendarError(Exception):
    """日历文件缺失、损坏或内容不合法。调用方应按 fail-closed 处理(拒绝,而不是当作正常)。"""


class UnknownProductError(KeyError):
    """时段表里没有这个品种:不猜,直接报错。"""


@dataclass(frozen=True)
class ProductSessions:
    exchange: str
    day: tuple[tuple[time, time], ...]
    night: tuple[tuple[time, time], ...]


_COMMODITY_DAY = ((time(9, 0), time(10, 15)), (time(10, 30), time(11, 30)), (time(13, 30), time(15, 0)))
_NIGHT_21_23 = ((time(21, 0), time(23, 0)),)

# 只录有依据的品种。新增品种前须对照交易所公告核对日盘/夜盘时间,并在 CHANGELOG 记录依据。
SESSIONS: dict[str, ProductSessions] = {
    "C": ProductSessions("DCE", _COMMODITY_DAY, _NIGHT_21_23),     # 大商所玉米
    "MA": ProductSessions("CZCE", _COMMODITY_DAY, _NIGHT_21_23),   # 郑商所甲醇
}

_PRODUCT_RE = re.compile(r"^([A-Za-z]+)")


def product_of(symbol: str) -> str:
    """'c2701.DCE' / 'MA701.CZCE' / 'ma' → 'C' / 'MA'(大写品种代码)。"""
    m = _PRODUCT_RE.match(symbol.strip())
    if not m:
        raise UnknownProductError(symbol)
    return m.group(1).upper()


@dataclass(frozen=True)
class TradingCalendar:
    start: date
    end: date
    closed: frozenset[date]          # 覆盖区间内、工作日里的休市日
    source: str
    fetched_at: str

    def status(self, d: date) -> str:
        if d < self.start or d > self.end:
            return UNKNOWN
        if d.weekday() >= 5 or d in self.closed:
            return CLOSED
        return TRADING

    def _scan(self, d: date, step: int) -> date | None:
        for _ in range(_MAX_SCAN_DAYS):
            d += timedelta(days=step)
            st = self.status(d)
            if st == TRADING:
                return d
            if st == UNKNOWN:
                return None
        return None

    def next_trading_day(self, d: date) -> date | None:
        """严格晚于 d 的第一个交易日;落到覆盖区间外则为 None(未知)。"""
        return self._scan(d, 1)

    def prev_trading_day(self, d: date) -> date | None:
        return self._scan(d, -1)

    def has_night_session(self, d: date) -> bool | None:
        st = self.status(d)
        if st != TRADING:
            return None if st == UNKNOWN else False
        nxt = self.next_trading_day(d)
        if nxt is None:
            return None
        gap = d + timedelta(days=1)
        while gap < nxt:                      # 中间只允许是普通周末
            if gap.weekday() < 5:
                return False
            gap += timedelta(days=1)
        return True

    def coverage_warning(self, today: date, lead_days: int = COVERAGE_LEAD_DAYS) -> str | None:
        left = (self.end - today).days
        if left > lead_days:
            return None
        if left < 0:
            return f"交易日历覆盖已于 {self.end.isoformat()} 结束:此后日期一律判为未知,依赖日历的功能会拒绝运行。请运行 tools/build_trading_calendar.py 更新"
        return f"交易日历覆盖将于 {self.end.isoformat()} 结束(剩 {left} 天):交易所公布来年休市安排后请运行 tools/build_trading_calendar.py 更新"


def _parse_date(value, field: str) -> date:
    if not isinstance(value, str):
        raise CalendarError(f"{field} 不是日期字符串:{value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CalendarError(f"{field} 不是合法日期:{value!r}") from exc


def calendar_from_dict(raw) -> TradingCalendar:
    """校验并构造日历;任何不合法都抛 CalendarError(由 tools/build_trading_calendar.py 复用,保证生成与读取同一套规则)。"""
    if not isinstance(raw, dict):
        raise CalendarError("日历文件顶层必须是对象")
    cov = raw.get("coverage")
    if not isinstance(cov, dict):
        raise CalendarError("缺少 coverage")
    start, end = _parse_date(cov.get("start"), "coverage.start"), _parse_date(cov.get("end"), "coverage.end")
    if start > end:
        raise CalendarError(f"覆盖区间倒置:{start} > {end}")
    items = raw.get("closed_weekdays")
    if not isinstance(items, list):
        raise CalendarError("closed_weekdays 必须是列表")
    closed = set()
    for item in items:
        d = _parse_date(item, "closed_weekdays")
        if d.weekday() >= 5:
            raise CalendarError(f"休市表里出现周末 {d}:周末本来就不交易,出现在这里说明数据或生成逻辑有误")
        if not start <= d <= end:
            raise CalendarError(f"休市日 {d} 不在覆盖区间 {start}~{end} 内")
        closed.add(d)
    return TradingCalendar(start, end, frozenset(closed), str(raw.get("source", "")), str(raw.get("fetched_at", "")))


def load_calendar(path: Path | str = DEFAULT_CALENDAR) -> TradingCalendar:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CalendarError(f"读不了交易日历 {path}:{exc}") from exc
    return calendar_from_dict(raw)


def session_state(product: str, t: datetime, cal: TradingCalendar) -> str:
    """某品种在时刻 t 是否处于交易时段:OPEN / CLOSED / UNKNOWN。

    时段之外一律 CLOSED(无论日历是否覆盖);时段之内看日历:日盘看当天,夜盘看当天是否有夜盘。
    """
    code = product_of(product)
    ps = SESSIONS.get(code)
    if ps is None:
        raise UnknownProductError(code)
    clock = t.time()
    if any(a <= clock < b for a, b in ps.day):
        st = cal.status(t.date())
        return {TRADING: OPEN, CLOSED: CLOSED}.get(st, UNKNOWN)
    if any(a <= clock < b for a, b in ps.night):
        night = cal.has_night_session(t.date())
        return UNKNOWN if night is None else (OPEN if night else CLOSED)
    return CLOSED


def trading_day_of(t: datetime, cal: TradingCalendar) -> date | None:
    """时刻 t 归属的交易日;顺延出覆盖区间则为 None(未知)。"""
    base = t.date() + timedelta(days=1) if t.time() >= NIGHT_CUTOFF else t.date()
    st = cal.status(base)
    if st == TRADING:
        return base
    if st == UNKNOWN:
        return None
    return cal.next_trading_day(base)
