"""回测封装:统一成本口径,并把 vn.py 静默吞掉的策略异常变成真正的报错。"""
from datetime import datetime

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401  必须先于 vnpy 导入

import pandas as pd
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData
from vnpy_ctastrategy.backtesting import BacktestingEngine

from strategies.instruments import INSTRUMENTS

DEFAULT_CAPITAL = 10_000

# vn.py 在策略抛异常时会把 traceback 写进 engine.output("触发异常，回测终止" + traceback)。
# 只认这两个精确哨兵:策略自己的日志走 BacktestingEngine.logs(不进 output),但用宽泛的
# "异常"二字判断,一旦 vn.py 改动就会误报(策略日志含"异常")或漏报(文案变化)。
EXCEPTION_SENTINELS = ("Traceback", "触发异常")


def _raise_if_strategy_failed(messages: list[str]) -> None:
    """vn.py 回测时遇到策略异常只打印日志就返回,结果是半截的,必须当错误处理。"""
    for index, msg in enumerate(messages):
        if any(s in msg for s in EXCEPTION_SENTINELS):
            # vn.py 先打印「触发异常」再单独打印 traceback:把哨兵之后的几条一并带上,否则只剩一句没有信息的话
            detail = "\n".join(messages[index:index + 3])[-2000:]
            raise RuntimeError("回测中策略抛出异常:\n" + detail)


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
    _snapshot = _risk_floor_snapshot(engine.strategy)
    engine.load_data()
    if not engine.history_data:
        raise RuntimeError(f"{key} 在 {start:%Y-%m-%d}~{end:%Y-%m-%d} 没有日线数据,请先运行 fetch_data.py")

    engine.run_backtesting()
    _raise_if_strategy_failed(messages)
    _assert_risk_floor_intact(engine.strategy, _snapshot, inst.size)
    return engine


def load_bars_csv(path, symbol: str, exchange: Exchange, interval: Interval = Interval.MINUTE) -> list[BarData]:
    """读 import_minute.py 输出的 K 线 CSV(时间戳为开始时间,已复权),转成 vn.py BarData 列表。

    注意:vn.py 没有 15 分钟的 Interval 枚举,这里用 MINUTE **占位**。这些 bar 只用于内存回放,
    **不要把它们写进 vn.py 数据库**——落库后会被当成 1 分钟线读回,周期全错。
    """
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
    """直接用给定的 K 线回放(不经过数据库),用于数据库放不下的周期(如 15 分钟)。成本口径同 run_engine。

    注意:绩效统计沿用 vn.py 的口径——逐日盈亏按**日历日**分组、年化因子固定 240(日线口径)。
    对日内周期(夜盘与次日日盘同属一个交易日)来说,夏普的数值不能与日线回测直接比大小;
    收益率、净盈亏、成本、回撤金额不受影响。详见 docs/adr/20261110-baseline-15m.md。
    """
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
    _snapshot = _risk_floor_snapshot(engine.strategy)
    engine.history_data = list(bars)
    engine.run_backtesting()
    _raise_if_strategy_failed(messages)
    _assert_risk_floor_intact(engine.strategy, _snapshot, inst.size)
    return engine


def _risk_floor_snapshot(strategy) -> dict:
    """回测开始前取一份风控状态快照(运行时第三层的比对基准)。"""
    from risk import constitution as const

    return const.snapshot_instance_state(strategy)


def _assert_risk_floor_intact(strategy, snapshot: dict, expected_size: float) -> None:
    """回测跑完后核对风控底线还在(不可变宪法的运行时第三层)。

    静态筛查拦的是"写法",类校验拦的是"类字典"——两者都看不见**回测途中**
    才被改写的实例状态:属性是在 `entry_signal` 里被赋值的,类字典里根本没有它。
    第 7 轮 codex 实测:`self.cta_engine.size = 0.001` 让止损距离算出来的风险金额
    接近 0,`promotion_gate` 的两道回撤门槛随之失效,而三层校验全绿。
    这里在回测结束时核对:熔断阈值、合约乘数、风险预算必须与**回测开始时**一致。

    比的是快照而不是模板常量:研究脚本会用更大的 risk_budget 做敏感性实验
    (`tests/test_strategy_backtest.py` 里就是),那是合法的;被改写的是"回测途中"。"""
    from risk import constitution as const

    findings = const.check_instance_state(strategy, expected_size=expected_size, snapshot=snapshot)
    if findings:
        raise RuntimeError("风控底线在回测途中被改写(fail-closed):" + "; ".join(findings))


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
