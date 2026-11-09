"""用分合约日线构建"复权主力连续合约"(差价法向前复权),并量化换月跳空。

为什么要做:新浪的 XX0 主力连续在换月处价格跳空且未复权,会污染 ATR、均线交叉与止损(见 ADR)。
这里改用各合约自己的日线:
  1. 主力合约 = 持仓量最大的合约,且只允许向更远月合约换月(不回头),避免来回抖动;
  2. **用前一个交易日的持仓量决定当日主力**(持仓量是收盘后才公布的统计量,用当日值决定当日合约属于前视):
     即"t-1 日收盘后发现远月持仓量反超 → t 日起持有远月合约",换月发生在反超的次日;
  3. 换月价差 = 新旧合约在换月前最后一个共同交易日的收盘价之差;没有共同交易日时退化为
     "新合约首个收盘价 - 旧合约最后收盘价"的近似,并在换月记录里标记 gap_exact=False;
  4. 差价法向前复权:把换月日之前的所有价格整体平移,使序列在换月处连续。点数波动(ATR)因此保持真实,
     1 万元账户的"止损点数 × 每手吨数"预算规则可直接使用。最新价格保持真实,越早的价格偏离越大,
     累计价差为大负数时早期价格可能变成非正数(会发出警告)。
  5. 当前主力当天没有成交但之后还会交易(停牌/无量)时,该日不产生 K 线,不换月;只有合约不再有数据(到期)才强制换月。

数据局限:新浪分合约日线只覆盖约 2019 年之后(实测 C1905 可取、C1801~C1809 取不到)。
用法(项目根目录): python scripts/build_continuous.py --symbol C [--save]
"""
import argparse
import os
import re
import sys
import time
import warnings
from datetime import date

import bootstrap

import pandas as pd

import akshare as ak
from fetch_data import SYMBOLS, fetch_with_retry, normalize, to_bars
from strategies.instruments import INSTRUMENTS
from vnpy.trader.constant import Interval
from vnpy.trader.database import get_database

CACHE_DIR = bootstrap.ROOT / "data" / "contracts"
ROLL_MONTHS = (1, 5, 9)  # 玉米、甲醇的主力合约集中在 1/5/9 月
REQUEST_INTERVAL = 1.0
CACHE_TTL_SECONDS = 24 * 3600  # 未到期合约的缓存有效期;已到期合约的数据不会再变,永久有效


def contract_expiry(code: str) -> int:
    """合约代码末四位 YYMM -> 可比较的整数(如 C2509 -> 2509)。"""
    m = re.search(r"(\d{4})$", code)
    if not m:
        raise ValueError(f"无法解析合约月份: {code}")
    return int(m.group(1))


def _choose_main(current, avail, prev_oi, expiry, last_day, day):
    """决定 day 日使用哪个合约;返回 None 表示该日跳过(不产生 K 线)。只使用截至前一交易日的持仓量。"""
    cands = [c for c in avail if current is None or expiry[c] >= expiry[current]]
    if not cands:
        return None

    def key(c):
        return (prev_oi.get(c, -1.0), expiry[c])

    if current is None:
        known = [c for c in cands if c in prev_oi]
        if known:
            return max(known, key=key)
        # 序列的第一天没有"前一日"可参考,只能用当日持仓量(仅影响起点)
        return max(cands, key=lambda c: (avail[c]["open_interest"], expiry[c]))

    if current in avail:
        best = max(cands, key=key)
        if expiry[best] > expiry[current] and prev_oi.get(best, -1.0) > prev_oi.get(current, -1.0):
            return best
        return current

    # 当前主力今天没有成交:之后还会有数据 → 停牌/无量,跳过当日;不再有数据(到期) → 强制换月
    if last_day[current] > day:
        return None
    return max(cands, key=key)


def build_continuous(frames: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """frames: 合约代码 -> 标准日线(列 datetime/open/high/low/close/volume/open_interest)。

    返回 (复权连续序列, 换月记录)。序列含 contract(当日主力)与 raw_close(未复权收盘价);
    换月记录含 date/from/to/gap/gap_exact。
    """
    if not frames:
        raise ValueError("没有任何合约数据")
    data = {c: f.set_index("datetime") for c, f in frames.items()}
    expiry = {c: contract_expiry(c) for c in data}
    last_day = {c: f.index.max() for c, f in data.items()}
    dates = sorted(set().union(*[set(f.index) for f in data.values()]))

    current: str | None = None
    prev_oi: dict[str, float] = {}  # 截至"前一个交易日"各合约的持仓量
    rows, rolls = [], []
    for day in dates:
        avail = {c: f.loc[day] for c, f in data.items() if day in f.index and f.loc[day, "volume"] > 0}
        if not avail:
            continue
        chosen = _choose_main(current, avail, prev_oi, expiry, last_day, day)
        if chosen is not None:
            if current is not None and chosen != current:
                gap, exact = _roll_gap(data, current, chosen, day)
                rolls.append({"date": day, "from": current, "to": chosen, "gap": gap, "gap_exact": exact, "row": len(rows)})
            current = chosen
            r = avail[chosen]
            rows.append({
                "datetime": day, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                "volume": r["volume"], "open_interest": r["open_interest"], "contract": chosen, "raw_close": r["close"],
            })
        for c, r in avail.items():  # 当日收盘后才更新,供下一个交易日决策使用
            prev_oi[c] = r["open_interest"]

    series = pd.DataFrame(rows)
    rolls_df = pd.DataFrame(rolls, columns=["date", "from", "to", "gap", "gap_exact", "row"])
    # 差价法向前复权:换月日之前的所有价格 += 之后各次换月价差之和
    offset = pd.Series(0.0, index=series.index)
    for _, roll in rolls_df.iterrows():
        offset.iloc[: int(roll["row"])] += roll["gap"]
    for col in ("open", "high", "low", "close"):
        series[col] = series[col] + offset
    if (series[["open", "high", "low", "close"]] <= 0).any(axis=None):
        warnings.warn("复权后出现非正价格:累计换月价差过大,下游止损/保证金计算请勿直接使用,考虑改用比例复权", stacklevel=2)
    if len(rolls_df) and not rolls_df["gap_exact"].all():
        warnings.warn(f"{int((~rolls_df['gap_exact']).sum())} 次换月新旧合约没有共同交易日,价差为近似值", stacklevel=2)
    return series, rolls_df.drop(columns="row")


def _roll_gap(data: dict[str, pd.DataFrame], old: str, new: str, day: pd.Timestamp) -> tuple[float, bool]:
    """返回 (价差, 是否精确)。精确 = 换月日之前存在新旧合约的共同交易日。"""
    common = data[old].index.intersection(data[new].index)
    prior = common[common < day]
    if len(prior):
        ref = prior[-1]
        return float(data[new].loc[ref, "close"] - data[old].loc[ref, "close"]), True
    if day in common:  # 退而求其次:用换月日当天的收盘价之差
        return float(data[new].loc[day, "close"] - data[old].loc[day, "close"]), False
    old_before = data[old].index[data[old].index < day]
    new_from = data[new].index[data[new].index >= day]
    if len(old_before) and len(new_from):  # 完全没有重叠:新合约首个收盘 - 旧合约最后收盘
        return float(data[new].loc[new_from[0], "close"] - data[old].loc[old_before[-1], "close"]), False
    warnings.warn(f"{old}->{new} 换月日 {day:%Y-%m-%d} 无法计算价差(新旧合约数据完全不衔接),按 0 处理,请检查数据", stacklevel=2)
    return 0.0, False


def _contract_is_past(code: str, today: date) -> bool:
    return contract_expiry(code) < (today.year % 100) * 100 + today.month


def _write_csv_atomic(df: pd.DataFrame, path) -> None:
    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def fetch_contract(code: str, today: date | None = None) -> pd.DataFrame | None:
    """取一个合约的日线(带本地缓存)。合约确实不存在返回 None;其它取数失败抛出异常(不缓存)。

    缓存规则:已到期合约永久有效(含"确认不存在"的结果);未到期合约缓存 24 小时,避免永远停在首次抓取日,
    也避免把尚未上市合约的"暂时取不到"永久缓存成"不存在"。
    """
    today = today or date.today()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{code}.csv"
    expired = _contract_is_past(code, today)

    if path.exists() and (expired or time.time() - path.stat().st_mtime < CACHE_TTL_SECONDS):
        try:
            cached = pd.read_csv(path, parse_dates=["datetime"])
            return cached if len(cached) else None
        except Exception:  # 缓存文件损坏:删除后重新抓取
            path.unlink(missing_ok=True)

    try:
        raw = fetch_with_retry(ak.futures_zh_daily_sina, symbol=code)
        df = normalize(raw, "d")
    except KeyError:  # 新浪没有该合约
        if expired:
            _write_csv_atomic(pd.DataFrame(columns=["datetime"]), path)
        return None
    if df.empty:
        return None
    _write_csv_atomic(df, path)
    time.sleep(REQUEST_INTERVAL)
    return df


def save_to_database(series: pd.DataFrame, code: str) -> str:
    """把复权序列写入 vn.py 数据库(日线),代码为 {品种}ADJ,如 CADJ.DCE。重复写入会覆盖同日数据。返回 vt_symbol。"""
    exchange = SYMBOLS[code]
    symbol = f"{code}ADJ"
    bars = to_bars(series[["datetime", "open", "high", "low", "close", "volume", "open_interest"]],
                   code, exchange, Interval.DAILY, symbol=symbol)
    get_database().save_bar_data(bars)
    return f"{symbol}.{exchange.value}"


def candidate_codes(product: str, first_year: int, last_year: int) -> list[str]:
    return [f"{product}{yy:02d}{m:02d}" for yy in range(first_year % 100, last_year % 100 + 1) for m in ROLL_MONTHS]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="C", choices=["C", "MA"])
    parser.add_argument("--first-year", type=int, default=2018)
    parser.add_argument("--last-year", type=int, default=2027)
    parser.add_argument("--save", action="store_true", help="把复权序列写入 vn.py 数据库(代码 {品种}ADJ),供回测与实盘预热使用")
    args = parser.parse_args()

    frames, failed = {}, []
    for code in candidate_codes(args.symbol, args.first_year, args.last_year):
        try:
            df = fetch_contract(code)
        except Exception as exc:  # 单个合约取数失败不中断其它合约,最后统一报错
            print(f"{code}: 取数失败 {exc!r}")
            failed.append(code)
            continue
        if df is not None:
            print(f"{code}: {len(df)} 行 {df.datetime.iloc[0]:%Y-%m-%d}~{df.datetime.iloc[-1]:%Y-%m-%d}")
            frames[code] = df
    if failed:
        print(f"\n以下合约取数失败,为避免产生缺合约的错误序列,不生成结果: {', '.join(failed)}")
        return 1
    if not frames:
        print("没有取到任何合约数据")
        return 1

    series, rolls = build_continuous(frames)
    out = bootstrap.ROOT / "data" / "results"
    out.mkdir(parents=True, exist_ok=True)
    series.to_csv(out / f"{args.symbol}_continuous_adj.csv", index=False)
    rolls.to_csv(out / f"{args.symbol}_rolls.csv", index=False)

    if args.save:
        print(f"已写入 vn.py 数据库: {save_to_database(series, args.symbol)}({len(series)} 根日线)")

    size = INSTRUMENTS[args.symbol].size
    print(f"\n[{args.symbol}] 复权连续序列 {len(series)} 根 {series.datetime.iloc[0]:%Y-%m-%d}~{series.datetime.iloc[-1]:%Y-%m-%d},换月 {len(rolls)} 次")
    if len(rolls):
        gaps = rolls["gap"]
        print(f"换月价差(点): 平均 {gaps.mean():.1f}  绝对值中位 {gaps.abs().median():.1f}  最大 {gaps.abs().max():.1f}"
              f"  (折合每手 {gaps.abs().median() * size:.0f} 元 / 最大 {gaps.abs().max() * size:.0f} 元)")
        print(rolls.assign(date=rolls["date"].dt.strftime("%Y-%m-%d")).to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
