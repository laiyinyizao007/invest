"""离线生成入库的交易日历 config/trading_calendar.json(运行时只读该文件,不联网、不依赖第三方库)。

数据来源:AKShare 的新浪 A 股交易日历(`tool_trade_date_hist_sina`)。国内期货交易所与证券交易所的休市安排一致
(法定节假日休市、调休的周末不开市),所以 A 股交易日即期货交易日;夜盘有无由 risk/sessions.py 从日历推导。

用法(项目根目录,需要 akshare 与网络):
    python tools/build_trading_calendar.py                  # 拉取并更新(只允许延长覆盖区间)
    python tools/build_trading_calendar.py --allow-changes  # 数据源改动了已有日期时,人工核对后才用
    python tools/build_trading_calendar.py --check          # 只校验现有文件并显示覆盖告警(不联网)
    python tools/build_trading_calendar.py --check --fail-on-warning   # 进入到期前 30 天窗口时以退出码 3 返回

退出码:0 成功(内容没变时不写文件)/ 1 与上一版冲突(未写入)/ 2 数据或文件不合法 / 3 覆盖即将到期(仅 --fail-on-warning)。

每年交易所公布来年休市安排后需更新一次。`.github/workflows/trading-calendar.yml` 每天自动拉取:
出现新数据就开 PR 由人确认合并,与旧日期冲突或覆盖即将到期就开 Issue 提醒——**从不直接改写入库文件**。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from risk import sessions  # noqa: E402

DEFAULT_OUT = sessions.DEFAULT_CALENDAR
DEFAULT_START = date(2018, 1, 1)
SOURCE = "akshare.tool_trade_date_hist_sina(新浪 A 股交易日历;国内期货交易所休市安排与之一致)"
YEAR_RANGE = (230, 255)            # 一个完整年份的交易日数合理区间(近年为 242~244)


class CalendarBuildError(Exception):
    pass


def build(trading_days: Iterable[date], start: date, end: date, *, source: str, fetched_at: str) -> dict:
    days = {d for d in trading_days if start <= d <= end}
    if not days:
        raise CalendarBuildError(f"数据源在 {start}~{end} 内没有任何交易日")
    weekend = sorted(d for d in days if d.weekday() >= 5)
    if weekend:
        raise CalendarBuildError(f"数据源把周末当成交易日:{[d.isoformat() for d in weekend[:5]]}")
    per_year = Counter(d.year for d in days)
    for year in range(start.year, end.year + 1):
        if date(year, 1, 1) >= start and date(year, 12, 31) <= end:          # 只检查完整覆盖的年份
            n = per_year.get(year, 0)
            if not YEAR_RANGE[0] <= n <= YEAR_RANGE[1]:
                raise CalendarBuildError(f"{year} 年只有 {n} 个交易日,不在合理区间 {YEAR_RANGE} 内:数据源可能残缺")
    closed, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in days:
            closed.append(d.isoformat())
        d += timedelta(days=1)
    out = {"source": source, "fetched_at": fetched_at,
           "coverage": {"start": start.isoformat(), "end": end.isoformat()}, "closed_weekdays": closed}
    try:
        sessions.calendar_from_dict(out)              # 生成与读取用同一套校验
    except sessions.CalendarError as exc:
        raise CalendarBuildError(str(exc)) from exc
    return out


def conflicts(old: dict, new: dict) -> list[str]:
    """与上一版比较:只允许延长覆盖区间;重叠区间里的休市日必须完全相同。"""
    o, n = sessions.calendar_from_dict(old), sessions.calendar_from_dict(new)
    found = []
    if n.start > o.start or n.end < o.end:
        found.append(f"覆盖区间缩短:{o.start}~{o.end} → {n.start}~{n.end}")
    lo, hi = max(o.start, n.start), min(o.end, n.end)
    for d in sorted(o.closed ^ n.closed):
        if lo <= d <= hi:
            found.append(f"{d.isoformat()} 的休市状态变了:{'休市→交易' if d in o.closed else '交易→休市'}")
    return found


def _fetch_akshare() -> list[date]:
    import akshare as ak                           # 只在生成时需要,运行时不依赖
    return [d if isinstance(d, date) else date.fromisoformat(str(d)[:10]) for d in ak.tool_trade_date_hist_sina()["trade_date"]]


def _git_baseline(path: Path) -> dict | None:
    """入库版本(git HEAD 里的同一路径)。文件被删掉或改名时用它做比较基线,堵住「删文件绕过只许延长」;取不到返回 None。"""
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
        out = subprocess.run(["git", "-C", str(ROOT), "show", f"HEAD:{rel}"], capture_output=True, timeout=30)
        return json.loads(out.stdout.decode("utf-8")) if out.returncode == 0 else None
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _reconfigure_stdio() -> None:
    """Windows 的 cp1252 控制台打印中文会直接抛 UnicodeEncodeError(做法同 tools/check_domain.py)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None, fetch: Callable[[], Iterable[date]] | None = _fetch_akshare,
         today: str | None = None) -> int:
    _reconfigure_stdio()
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    ap.add_argument("--end", type=date.fromisoformat, default=None, help="默认取数据源的最后一个交易日")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--allow-changes", action="store_true", help="允许改动已有日期(须先人工核对官方公告)")
    ap.add_argument("--check", action="store_true", help="只校验现有文件,不联网")
    ap.add_argument("--fail-on-warning", action="store_true", help="与 --check 合用:覆盖即将到期时以退出码 3 返回")
    args = ap.parse_args(argv)
    today_s = today or date.today().isoformat()

    if args.check:
        try:
            cal = sessions.load_calendar(args.out)
        except sessions.CalendarError as exc:
            print(f"[错误] {exc}")
            return 2
        print(f"[OK] {args.out}:覆盖 {cal.start}~{cal.end},休市工作日 {len(cal.closed)} 个,来源:{cal.source}")
        warning = cal.coverage_warning(date.fromisoformat(today_s))
        if warning:
            print(f"[告警] {warning}")
            if args.fail_on_warning:
                return 3
        return 0

    try:
        days = list(fetch())
    except Exception as exc:                      # noqa: BLE001  数据源不可用(网络等)是退出码 2,不能被当成「冲突」
        print(f"[错误] 数据源不可用:{type(exc).__name__}: {exc}")
        return 2
    try:
        end = args.end or max(days)
        new = build(days, args.start, end, source=SOURCE, fetched_at=today_s)
    except (CalendarBuildError, ValueError) as exc:
        print(f"[错误] {exc}")
        return 2
    old = None
    if args.out.exists():
        try:
            old = json.loads(args.out.read_text(encoding="utf-8"))
        except ValueError as exc:
            print(f"[错误] 现有文件不合法,无法比较:{exc}")
            return 2
    else:
        old = _git_baseline(args.out)
        if old is not None:
            print(f"[提示] {args.out} 不存在,以 git 里的入库版本为比较基线")
    if old is not None:
        if old.get("manual_closed"):
            new["manual_closed"] = old["manual_closed"]          # 人工追加的休市日原样保留
        try:
            sessions.calendar_from_dict(new)
            found = conflicts(old, new)
        except sessions.CalendarError as exc:
            print(f"[错误] 无法与上一版比较:{exc}")
            return 2
        unchanged = (old.get("coverage") == new["coverage"] and old.get("closed_weekdays") == new["closed_weekdays"]
                     and old.get("manual_closed", []) == new.get("manual_closed", []))
        if not found and args.out.exists() and unchanged:
            print(f"[无变化] {args.out}:覆盖 {new['coverage']['start']}~{new['coverage']['end']},未写入")
            return 0
        if found and not args.allow_changes:
            print("[拒绝] 新数据改动了已有日期,未写入。核对官方公告后用 --allow-changes 重跑:")
            for line in found:
                print(f"  - {line}")
            return 1
    _write_atomic(args.out, new)
    print(f"[OK] 已写入 {args.out}:覆盖 {new['coverage']['start']}~{new['coverage']['end']},休市工作日 {len(new['closed_weekdays'])} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
