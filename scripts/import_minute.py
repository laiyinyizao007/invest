"""把购买的分合约 1 分钟线导入为"差额复权的主力连续合约",并合成 15 分钟线。

数据目录(放在仓库之外,不入库):<数据根>/<交易所>/<品种>/月份连续/<月>/<品种>_<月>_1m.csv
每个文件含若干合约(symbol 形如 DCE.c2101)的 1 分钟线,时间戳是 K 线**开始**时间(已用新浪日线交叉验证:
收盘/最高/最低 99% 以上一致,成交量总和比值 1.000),与 vn.py 的约定一致,无需转换。

流程:
  1. 读入各合约分钟线,按交易日归属(夜盘 → 下一个有日盘的交易日),汇总成每个合约的日线;
  2. 数据只到某一天时,用新浪日线补齐其后的交易日(缺口用日线,不伪造分钟线);
  3. 复用 build_continuous 的换月规则(前一日持仓量、只向远月换、无前视)得到主力序列与换月价差;
  4. 用同一套换月价差对分钟线做差额复权,再合成 N 分钟线(默认 15)。分钟线只覆盖购买数据的时间范围。

输出(默认 <数据根>/derived/):{品种}_{N}m_adj.csv、{品种}_1d_adj.csv(含新浪补齐部分)、{品种}_rolls.csv。
用法: python scripts/import_minute.py --root C:/Dev/data/invest --exchange DCE_大连商品交易所 --folder C_玉米 --product C [--no-sina]
"""
import argparse
import os
import re
import sys
import warnings
from pathlib import Path

import bootstrap  # noqa: F401  (保证工作目录与时区设置)

import numpy as np
import pandas as pd

from build_continuous import build_continuous, contract_expiry, fetch_contract

USECOLS = ["datetime", "symbol", "open", "high", "low", "close", "volume", "open_interest", "close_interest"]
DAY_START_HOUR = 8     # 早于此小时的时间戳(跨午夜的夜盘)归属下一个交易日
NIGHT_START_HOUR = 20  # 不早于此小时的时间戳是夜盘,归属下一个交易日


def contract_code(symbol: str, product: str) -> str:
    """DCE.c2101 / c2101 -> C2101。"""
    m = re.search(r"([A-Za-z]+)(\d{3,4})$", symbol)
    if not m or m.group(1).upper() != product.upper():
        raise ValueError(f"无法识别合约代码: {symbol}(期望品种 {product})")
    return f"{product.upper()}{m.group(2)}"


def load_minutes(folder: Path, product: str) -> dict[str, pd.DataFrame]:
    """读入 <folder>/月份连续/*/*.csv,返回 合约代码 -> 分钟线。同一合约出现在多个文件里时合并去重。"""
    files = sorted(folder.glob("月份连续/*/*.csv"))
    if not files:
        raise FileNotFoundError(f"没有找到分钟线文件: {folder}/月份连续/*/*.csv")
    parts: dict[str, list[pd.DataFrame]] = {}
    for f in files:
        header = pd.read_csv(f, encoding="utf-8-sig", nrows=0).columns
        lacking = [c for c in USECOLS if c not in header]
        if lacking:
            raise ValueError(f"{f.name} 缺少必需列: {', '.join(lacking)}(需要 {', '.join(USECOLS)})")
        df = pd.read_csv(f, encoding="utf-8-sig", usecols=USECOLS, parse_dates=["datetime"])
        for symbol, g in df.groupby("symbol"):
            parts.setdefault(contract_code(str(symbol), product), []).append(g.drop(columns="symbol"))
    out = {}
    for code, ps in parts.items():
        d = pd.concat(ps).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
        d = d[(d[["open", "high", "low", "close"]] > 0).all(axis=1)]
        out[code] = d.reset_index(drop=True)
    return out


def trading_calendar(minutes: dict[str, pd.DataFrame]) -> np.ndarray:
    """交易日历 = 所有合约出现过日盘的日期(节假日自然缺席)。"""
    days = set()
    for d in minutes.values():
        h = d["datetime"].dt.hour
        days.update(d.loc[(h >= DAY_START_HOUR) & (h < NIGHT_START_HOUR), "datetime"].dt.normalize().unique())
    return np.array(sorted(days), dtype="datetime64[ns]")


def assign_trading_day(ts: pd.Series, calendar: np.ndarray) -> pd.Series:
    """夜盘(及跨午夜时段)归属其后第一个交易日,日盘归属当天。"""
    day = ts.dt.normalize()
    hour = ts.dt.hour
    after = (day + pd.Timedelta(days=1)).values.astype("datetime64[ns]")
    same = day.values.astype("datetime64[ns]")
    padded = np.append(calendar, np.datetime64("NaT", "ns"))         # 越界下标落在末尾的 NaT 上
    nxt_after = padded[np.searchsorted(calendar, after, side="left")]
    nxt_same = padded[np.searchsorted(calendar, same, side="left")]
    # 日历之后的夜盘:所属交易日未知(日历只含已出现过日盘的日期),返回 NaT,由调用方丢弃,不能硬塞进最后一天
    td = np.where(hour >= NIGHT_START_HOUR, nxt_after, np.where(hour < DAY_START_HOUR, nxt_same, same))
    return pd.Series(pd.to_datetime(td), index=ts.index)


def daily_from_minutes(df: pd.DataFrame, calendar: np.ndarray) -> pd.DataFrame:
    d = df.assign(trading_day=assign_trading_day(df["datetime"], calendar))
    g = d.groupby("trading_day")
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "open_interest": g["close_interest"].last(),
    })
    out.index.name = "datetime"
    return out.reset_index()


def extend_with_sina(frames: dict[str, pd.DataFrame], product: str, fetch=fetch_contract,
                     months: tuple[int, ...] = tuple(range(1, 13)), failures: list | None = None) -> dict[str, int]:
    """数据结束日之后的交易日用新浪日线补齐(原地修改 frames)。返回 合约 -> 补入的天数。

    只补每个合约"已有数据最后一天"之后的日期(从未出现过的合约则只补整体结束日之后),成交量为 0 的日子不补。
    候选月份默认 1-12 全月(不假设品种只有奇数月合约);不存在的合约新浪返回空,无副作用。
    取数失败的合约会追加到 failures(调用方据此判定缺口不完整)。
    """
    added: dict[str, int] = {}
    end = max(f["datetime"].max() for f in frames.values())
    floor = (end.year % 100) * 100 + end.month                 # 结束日之前就已到期的合约补不出新数据
    last_year = max(contract_expiry(c) for c in frames) // 100 + 1
    candidates = set(frames) | {f"{product}{yy:02d}{mm:02d}" for yy in range(end.year % 100, last_year + 1) for mm in months}
    for code in sorted(candidates, key=contract_expiry):
        if contract_expiry(code) < floor:
            continue
        try:
            sina = fetch(code)
        except Exception as exc:
            warnings.warn(f"{code}: 新浪取数失败,缺口可能不完整: {exc!r}", stacklevel=2)
            if failures is not None:
                failures.append(code)
            continue
        if sina is None or sina.empty:
            continue
        cutoff = frames[code]["datetime"].max() if code in frames else end
        keep = sina[(sina["datetime"] > cutoff) & (sina["volume"] > 0)]
        if keep.empty:
            continue
        keep = keep[["datetime", "open", "high", "low", "close", "volume", "open_interest"]]
        frames[code] = pd.concat([frames[code], keep]).sort_values("datetime").reset_index(drop=True) if code in frames else keep.reset_index(drop=True)
        added[code] = len(keep)
    return added


def trim_illiquid_start(series: pd.DataFrame, rolls: pd.DataFrame, fraction: float = 0.5, window: int = 5):
    """去掉序列开头"主力合约还很冷清"的日子。

    购买的数据从某个合约上市日起算,起点附近数据里只有远月合约,真正的主力(更近月)不在数据里,
    此时挑出的"主力"成交很少,不能代表可交易的行情。规则:从第一段"连续 window 个交易日成交量都不低于
    全序列中位数的 fraction 倍"的第一天开始。返回 (序列, 换月记录)。
    """
    liquid = series["volume"] >= series["volume"].median() * fraction
    run = liquid.rolling(window, min_periods=window).min() == 1
    if not run.any():
        raise ValueError("整个序列都没有连续 %d 个成交充足的交易日,数据不可用" % window)
    first_ok = int(np.argmax(run.to_numpy()))                         # 位置而非标签:不依赖索引是否从 0 开始
    start = series["datetime"].iloc[first_ok - (window - 1)]
    return (series[series["datetime"] >= start].reset_index(drop=True),
            rolls[rolls["date"] > start].reset_index(drop=True))


def offsets_by_day(series: pd.DataFrame, rolls: pd.DataFrame) -> pd.Series:
    """每个交易日的复权偏移量 = 该日之后所有换月价差之和(与 build_continuous 对价格的处理一致)。"""
    off = pd.Series(0.0, index=pd.DatetimeIndex(series["datetime"]))
    for _, r in rolls.iterrows():
        off.loc[off.index < r["date"]] += float(r["gap"])
    return off


def build_intraday(minutes: dict[str, pd.DataFrame], series: pd.DataFrame, rolls: pd.DataFrame,
                   calendar: np.ndarray, minutes_per_bar: int = 15) -> pd.DataFrame:
    """按主力序列挑出每个交易日对应合约的分钟线,做差额复权,再合成 N 分钟线(时间戳 = 开始时间)。"""
    last_minute_day = max(d["datetime"].max() for d in minutes.values()).normalize()
    off = offsets_by_day(series, rolls)
    chunks = []
    for code, g in series[series["datetime"] <= last_minute_day].groupby("contract"):
        if code not in minutes:        # 新浪补入的合约没有分钟线(数据恰在夜盘中途结束时可能成为主力),跳过
            continue
        d = minutes[code]
        d = d.assign(trading_day=assign_trading_day(d["datetime"], calendar))
        days = pd.DatetimeIndex(g["datetime"])
        d = d[d["trading_day"].isin(days)].copy()
        shift = off.reindex(d["trading_day"]).to_numpy()
        for col in ("open", "high", "low", "close"):
            d[col] = d[col] + shift
        d["contract"] = code
        chunks.append(d)
    m = pd.concat(chunks).sort_values("datetime").reset_index(drop=True)
    m["bucket"] = m["datetime"].dt.floor(f"{minutes_per_bar}min")
    # 同一时刻只能属于一个交易日;按 (交易日, 桶) 分组,避免夜盘与次日日盘因整点对齐被并到一起
    g = m.groupby(["trading_day", "bucket"], sort=True)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last(),
        "volume": g["volume"].sum(), "open_interest": g["close_interest"].last(), "contract": g["contract"].first(),
    }).reset_index()
    out = out.rename(columns={"bucket": "datetime"}).sort_values("datetime").reset_index(drop=True)
    return out[["datetime", "trading_day", "open", "high", "low", "close", "volume", "open_interest", "contract"]]


def validate(bars: pd.DataFrame) -> list[str]:
    """基本一致性检查,返回问题列表(空 = 通过)。"""
    problems = []
    if bars["datetime"].duplicated().any():
        problems.append(f"重复时间戳 {int(bars['datetime'].duplicated().sum())} 个")
    if not bars["datetime"].is_monotonic_increasing:
        problems.append("时间未单调递增")
    bad = (bars["high"] < bars[["open", "close"]].max(axis=1)) | (bars["low"] > bars[["open", "close"]].min(axis=1))
    if bad.any():
        problems.append(f"OHLC 不自洽 {int(bad.sum())} 根")
    if (bars[["open", "high", "low", "close"]] <= 0).any(axis=None):
        problems.append("复权后出现非正价格")
    return problems


def days_without_bars(bars: pd.DataFrame, series: pd.DataFrame) -> list:
    """日线序列里本应有分钟线(不晚于分钟线结束日)却一根都没有的交易日。"""
    have = set(bars["trading_day"].unique())
    return [d for d in series["datetime"] if d <= bars["trading_day"].max() and d not in have]


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--root", required=True, help="数据根目录(仓库之外),如 C:/Dev/data/invest")
    p.add_argument("--exchange", required=True, help="交易所目录名,如 DCE_大连商品交易所")
    p.add_argument("--folder", required=True, help="品种目录名,如 C_玉米")
    p.add_argument("--product", required=True, help="品种代码,如 C")
    p.add_argument("--minutes", type=int, default=15, help="合成的分钟线周期")
    p.add_argument("--no-sina", action="store_true", help="不用新浪日线补齐数据结束日之后的缺口")
    p.add_argument("--out", default=None, help="输出目录(默认 <数据根>/derived)")
    a = p.parse_args(argv)

    root = Path(a.root)
    out = Path(a.out) if a.out else root / "derived"
    minutes = load_minutes(root / a.exchange / a.folder, a.product)
    calendar = trading_calendar(minutes)
    frames = {c: daily_from_minutes(d, calendar) for c, d in minutes.items()}
    print(f"读入 {len(minutes)} 个合约 {min(minutes, key=contract_expiry)}~{max(minutes, key=contract_expiry)},"
          f"分钟线 {sum(len(d) for d in minutes.values())} 根,交易日 {len(calendar)} 天")

    sina_failed: list[str] = []
    if not a.no_sina:
        added = extend_with_sina(frames, a.product.upper(), failures=sina_failed)
        print("新浪日线补齐: " + (", ".join(f"{c}+{n}" for c, n in added.items()) or "无新增"))

    series, rolls = build_continuous(frames)
    total = len(series)
    series, rolls = trim_illiquid_start(series, rolls)
    print(f"去掉起点附近成交冷清的 {total - len(series)} 个交易日,序列从 {series['datetime'].min():%Y-%m-%d} 开始")
    bars = build_intraday(minutes, series, rolls, calendar, a.minutes)
    problems = validate(bars) + [f"日线: {x}" for x in validate(series)]
    if sina_failed:
        problems.append(f"新浪取数失败,缺口可能不完整: {', '.join(sina_failed)}")
    missing = days_without_bars(bars, series)
    if missing:
        problems.append(f"{len(missing)} 个交易日有日线却没有分钟线(如 {missing[0]:%Y-%m-%d}):该日主力合约的分钟数据缺失")
    tag = a.product.upper()
    _write_atomic(bars, out / f"{tag}_{a.minutes}m_adj.csv")
    _write_atomic(series, out / f"{tag}_1d_adj.csv")
    _write_atomic(rolls, out / f"{tag}_rolls.csv")

    print(f"{a.minutes} 分钟线 {len(bars)} 根 {bars.datetime.min()}~{bars.datetime.max()};"
          f"日线 {len(series)} 根 {series.datetime.min():%Y-%m-%d}~{series.datetime.max():%Y-%m-%d};换月 {len(rolls)} 次")
    if len(rolls):
        gaps = rolls["gap"].abs()
        print(f"换月价差(点): 中位 {gaps.median():.1f} 最大 {gaps.max():.1f}; 不精确换月 {int((~rolls['gap_exact']).sum())} 次")
    print("检查: " + ("通过" if not problems else "; ".join(problems)))
    print(f"输出: {out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
