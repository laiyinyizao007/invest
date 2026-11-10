"""回测封装:统一成本口径,并把 vn.py 静默吞掉的策略异常变成真正的报错。"""
from datetime import datetime

import bootstrap  # noqa: F401  必须先于 vnpy 导入

import pandas as pd
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy_ctastrategy.backtesting import BacktestingEngine

from strategies.instruments import INSTRUMENTS

DEFAULT_CAPITAL = 10_000


def run_engine(
    strategy_class,
    setting: dict,
    key: str,
    start: datetime,
    end: datetime,
    capital: float = DEFAULT_CAPITAL,
) -> BacktestingEngine:
    """在日线上回放一个品种并返回引擎(可通过 engine.strategy 查看策略内部状态)。

    成本:rate=0,把每手固定手续费折成点数并入 slippage(见 Instrument.slippage_points)。
    """
    inst = INSTRUMENTS[key]
    engine = BacktestingEngine()
    messages: list[str] = []
    engine.output = messages.append  # 静默 vn.py 的进度输出,同时保留以便检查错误

    engine.set_parameters(
        vt_symbol=inst.vt_symbol,
        interval=Interval.DAILY,
        start=start,
        end=end,
        rate=0.0,
        slippage=inst.slippage_points,
        size=inst.size,
        pricetick=inst.pricetick,
        capital=capital,
    )
    engine.add_strategy(strategy_class, setting)
    engine.load_data()
    if not engine.history_data:
        raise RuntimeError(f"{key} 在 {start:%Y-%m-%d}~{end:%Y-%m-%d} 没有日线数据,请先运行 fetch_data.py")

    engine.run_backtesting()
    # vn.py 在策略抛异常时只打印"回测终止"并返回,结果会是半截的,必须当错误处理
    for msg in messages:
        if "Traceback" in msg or "触发异常" in msg or "异常" in msg:
            raise RuntimeError(f"回测中策略抛出异常:\n{msg}")
    return engine


def load_bars_csv(path, symbol: str, exchange: Exchange, interval: Interval = Interval.MINUTE) -> list[BarData]:
    """读 import_minute.py 输出的 K 线 CSV(时间戳为开始时间,已复权),转成 vn.py BarData 列表。"""
    df = pd.read_csv(path, parse_dates=["datetime"])
    return [
        BarData(symbol=symbol, exchange=exchange, datetime=row.datetime, interval=interval,
                open_price=row.open, high_price=row.high, low_price=row.low, close_price=row.close,
                volume=float(row.volume), turnover=0.0, open_interest=float(row.open_interest), gateway_name="CSV")
        for row in df.itertuples(index=False)
    ]


def run_engine_bars(
    strategy_class,
    setting: dict,
    key: str,
    bars: list[BarData],
    capital: float = DEFAULT_CAPITAL,
    interval: Interval = Interval.MINUTE,
    cost_multiplier: float = 1.0,
) -> BacktestingEngine:
    """直接用给定的 K 线回放(不经过数据库),用于数据库放不下的周期(如 15 分钟)。成本口径同 run_engine。"""
    if not bars:
        raise RuntimeError(f"{key} 没有可回放的 K 线")
    inst = INSTRUMENTS[key]
    engine = BacktestingEngine()
    messages: list[str] = []
    engine.output = messages.append
    engine.set_parameters(
        vt_symbol=inst.vt_symbol,
        interval=interval,
        start=bars[0].datetime,
        end=bars[-1].datetime,
        rate=0.0,
        slippage=inst.slippage_points * cost_multiplier,
        size=inst.size,
        pricetick=inst.pricetick,
        capital=capital,
    )
    engine.add_strategy(strategy_class, setting)
    engine.history_data = list(bars)
    engine.run_backtesting()
    for msg in messages:
        if "Traceback" in msg or "触发异常" in msg or "异常" in msg:
            raise RuntimeError(f"回测中策略抛出异常:\n{msg}")
    return engine


def run_backtest_bars(strategy_class, setting: dict, key: str, bars: list[BarData], capital: float = DEFAULT_CAPITAL,
                      interval: Interval = Interval.MINUTE, cost_multiplier: float = 1.0) -> tuple[dict, pd.DataFrame, list]:
    engine = run_engine_bars(strategy_class, setting, key, bars, capital, interval, cost_multiplier)
    df = engine.calculate_result()
    stats = engine.calculate_statistics(df, output=False)
    return stats, df, engine.get_all_trades()


def run_backtest(
    strategy_class,
    setting: dict,
    key: str,
    start: datetime,
    end: datetime,
    capital: float = DEFAULT_CAPITAL,
) -> tuple[dict, pd.DataFrame, list]:
    """回测并计算绩效,返回 (统计指标, 逐日结果, 成交列表)。"""
    engine = run_engine(strategy_class, setting, key, start, end, capital)
    df = engine.calculate_result()
    stats = engine.calculate_statistics(df, output=False)
    return stats, df, engine.get_all_trades()
