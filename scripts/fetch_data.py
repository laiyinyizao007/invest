"""用 AKShare(新浪源)下载期货主力连续数据,清洗后写入 vn.py 数据库。

用法(项目根目录下):
    python scripts/fetch_data.py                      # PP/L/EB/C/MA,日线+1/5/15 分钟
    python scripts/fetch_data.py --symbols PP --periods d 15

存储:
    d  -> vn.py 数据库 (Interval.DAILY)
    1  -> vn.py 数据库 (Interval.MINUTE);vn.py 的 CTA 引擎用 BarGenerator 从 1 分钟线合成 15 分钟线
    5/15 -> data/csv/{品种}_{周期}m.csv (vn.py 没有对应的 Interval,仅作历史积累)

数据局限(2026-11 实测,PP0):
    日线约 12 年;分钟线每个周期都只返回最近约 1023 根
    (1 分钟≈5 个交易日,5 分钟≈3 周,15 分钟≈2.5 个月)。
    要积累长期分钟历史,必须每天(或至少每周)增量运行本脚本。重复运行安全:按时间戳去重/覆盖。

注意:新浪 "XX0" 是主力连续合约,换月处价格有跳空且未复权,回测时需要留意。

限流处理:每次请求间隔 REQUEST_INTERVAL 秒;失败按指数退避重试 (2s, 4s, 8s...);
单个品种/周期失败不影响其余任务,最终以非零退出码提示。
"""
import argparse
import os
import sys
import time

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401  必须先于 vnpy.trader.database 导入

import akshare as ak
import pandas as pd
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.database import DB_TZ, get_database
from vnpy.trader.object import BarData

SYMBOLS: dict[str, Exchange] = {
    "PP": Exchange.DCE,  # 聚丙烯
    "L": Exchange.DCE,   # 塑料
    "EB": Exchange.DCE,  # 苯乙烯
    "C": Exchange.DCE,   # 玉米(1 万元账户的主测品种)
    "MA": Exchange.CZCE,  # 甲醇(对照品种)
}
PERIODS = ("d", "1", "5", "15")
COLUMNS = ["datetime", "open", "high", "low", "close", "volume", "open_interest"]
CSV_DIR = bootstrap.ROOT / "data" / "csv"
CSV_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # 显式指定,不依赖 pandas 默认输出格式
GATEWAY_NAME = "AKSHARE"

REQUEST_INTERVAL = 1.0  # 两次请求之间的间隔(秒)
MAX_RETRIES = 4
BACKOFF_BASE = 2.0


def fetch_with_retry(func, **kwargs) -> pd.DataFrame:
    """调用 AKShare 接口,失败时指数退避重试。"""
    for attempt in range(MAX_RETRIES):
        try:
            return func(**kwargs)
        except (TypeError, KeyError, AttributeError):
            raise  # 编程错误或接口结构变化,重试无意义,立即失败
        except Exception as exc:  # AKShare 底层网络/解析异常类型不统一,统一重试
            if attempt == MAX_RETRIES - 1:
                raise
            delay = BACKOFF_BASE * 2**attempt
            print(f"  请求失败({exc!r}),{delay:.0f}s 后重试 {attempt + 1}/{MAX_RETRIES - 1}")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def normalize(raw: pd.DataFrame, period: str) -> pd.DataFrame:
    """统一列名/时间语义并做基本校验。

    新浪分钟线的时间戳是 K 线**结束**时间(1 分钟线首根为 09:01、夜盘末根为 23:00),
    而 vn.py 使用 K 线**开始**时间,因此要往前推一个周期。日线日期保持不变。
    """
    time_col = "date" if period == "d" else "datetime"
    df = raw.rename(columns={time_col: "datetime", "hold": "open_interest"})[COLUMNS].copy()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")  # 坏时间戳变 NaT,由 dropna 清除
    if period != "d":
        df["datetime"] = df["datetime"] - pd.Timedelta(minutes=int(period))

    df = df.dropna()
    price_cols = ["open", "high", "low", "close"]
    invalid = (df[price_cols] <= 0).any(axis=1)
    if invalid.any():
        print(f"  丢弃 {int(invalid.sum())} 行非正价格数据")
        df = df[~invalid].copy()

    # 数据源偶有 OHLC 自相矛盾的行(实测:PP 2020-08-06 收盘低于最低价,L 2007-07-31 开盘高于最高价)。
    # 丢弃会在序列里留下缺口、扰乱均线,所以用四个价格的极值修复 high/low。
    true_high = df[price_cols].max(axis=1)
    true_low = df[price_cols].min(axis=1)
    inconsistent = (df["high"] != true_high) | (df["low"] != true_low)
    if inconsistent.any():
        print(f"  修复 {int(inconsistent.sum())} 行 OHLC 不一致数据")
        df["high"], df["low"] = true_high, true_low
    df = df.drop_duplicates("datetime").sort_values("datetime")
    return df.reset_index(drop=True)


def to_bars(df: pd.DataFrame, code: str, exchange: Exchange, interval: Interval, symbol: str | None = None) -> list[BarData]:
    """DataFrame -> vn.py BarData 列表(时间带 DB_TZ 时区)。symbol 默认是新浪主力连续代码 {code}0。"""
    return [
        BarData(
            symbol=symbol or f"{code}0",
            exchange=exchange,
            datetime=row.datetime.to_pydatetime().replace(tzinfo=DB_TZ),
            interval=interval,
            volume=float(row.volume),
            turnover=0.0,  # 新浪不提供成交额,策略不要依赖该字段
            open_interest=float(row.open_interest),
            open_price=float(row.open),
            high_price=float(row.high),
            low_price=float(row.low),
            close_price=float(row.close),
            gateway_name=GATEWAY_NAME,
        )
        for row in df.itertuples(index=False)
    ]


def save_csv(df: pd.DataFrame, code: str, period: str) -> pd.DataFrame:
    """与已有 CSV 合并后落盘(新数据覆盖同时间戳的旧数据),返回合并后的完整数据。

    先写临时文件再 os.replace 原子替换:分钟历史无法回补,写到一半被中断不能毁掉已积累的数据。
    """
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    path = CSV_DIR / f"{code}_{period}m.csv"
    if path.exists():
        old = pd.read_csv(path)
        old["datetime"] = pd.to_datetime(old["datetime"], format=CSV_DATE_FORMAT)
        df = pd.concat([old, df]).drop_duplicates("datetime", keep="last")
    df = df.sort_values("datetime").reset_index(drop=True)

    tmp = path.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, date_format=CSV_DATE_FORMAT)
    os.replace(tmp, path)
    return df


def run_one(code: str, exchange: Exchange, period: str) -> None:
    sina_symbol = f"{code}0"
    if period == "d":
        raw = fetch_with_retry(ak.futures_zh_daily_sina, symbol=sina_symbol)
    else:
        raw = fetch_with_retry(ak.futures_zh_minute_sina, symbol=sina_symbol, period=period)
    df = normalize(raw, period)
    if df.empty:
        raise ValueError("清洗后无有效数据")

    if period in ("d", "1"):
        interval = Interval.DAILY if period == "d" else Interval.MINUTE
        get_database().save_bar_data(to_bars(df, code, exchange, interval))
        target = f"vn.py 数据库 ({interval.value})"
    else:
        df = save_csv(df, code, period)
        target = f"CSV ({code}_{period}m.csv, 累计)"
    print(f"  {len(df)} 根 {df['datetime'].iloc[0]} -> {df['datetime'].iloc[-1]} => {target}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbols", nargs="+", default=list(SYMBOLS), choices=list(SYMBOLS))
    parser.add_argument("--periods", nargs="+", default=list(PERIODS), choices=PERIODS)
    args = parser.parse_args()

    failed: list[str] = []
    for code in args.symbols:
        for period in args.periods:
            print(f"[{code} {period}]")
            try:
                run_one(code, SYMBOLS[code], period)
            except Exception as exc:
                print(f"  失败: {exc!r}")
                failed.append(f"{code}/{period}")
            time.sleep(REQUEST_INTERVAL)

    if failed:
        print(f"\n以下任务失败: {', '.join(failed)}")
        return 1
    print("\n全部完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
