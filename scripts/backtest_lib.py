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
    # 行情在候选的 `__init__` 跑**之前**读成引擎里的局部数据(第 38 轮:与 gate/evaluate 入口的"先取定再让候选运行"同一模式)。
    # `load_data` 只依赖 set_parameters 设好的区间/品种,不依赖策略实例,所以前置不改变回测语义。
    engine.load_data()
    if not engine.history_data:
        raise RuntimeError(f"{key} 在 {start:%Y-%m-%d}~{end:%Y-%m-%d} 没有日线数据,请先运行 fetch_data.py")
    # 第四层的"回测前"基准要在**构造实例之前**拍:
    # 实例构造(__init__)也是候选能碰到的时机,构造期改写的东西如果发生在拍快照之后,
    # 就成了"基线",指纹再也看不出来(第 10 轮 codex 的 Medium 3)。
    _protected = _protected_state_snapshot()
    engine.add_strategy(strategy_class, setting)
    _snapshot = _risk_floor_snapshot(engine.strategy)
    _market = _install_market_baseline(engine)

    engine.run_backtesting()
    _raise_if_strategy_failed(messages)
    _assert_risk_floor_intact(engine.strategy, _snapshot, inst.size, _protected,
                               bars=_market_feed(_market, engine),
                               market_baseline=_market.get("market"),
                               market_findings=_market["findings"])
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
    _protected = _protected_state_snapshot()          # 同 run_engine:基准拍在构造实例之前
    engine.add_strategy(strategy_class, setting)
    _snapshot = _risk_floor_snapshot(engine.strategy)
    _market = _install_market_baseline(engine)
    engine.history_data = list(bars)
    engine.run_backtesting()
    _raise_if_strategy_failed(messages)
    _assert_risk_floor_intact(engine.strategy, _snapshot, inst.size, _protected,
                               bars=_market_feed(_market, engine),
                               market_baseline=_market.get("market"),
                               market_findings=_market["findings"])
    return engine


def _install_market_baseline(engine) -> dict:
    """行情容器的基准与"预热喂入"的记录(运行时第五层的输入)。

    基准必须拍在 **`on_init` 之前**(构造完成时,此时容器是空的):
    若拍在预热之后,候选在 `on_init` 里往容器塞的假 K 线就成了"基线",第五层再也看不见
    (第 13 轮自查实测:预热期塞 40 根假 K 线,`am.count=160` 而引擎只喂了 120,第一版照样放行)。

    日线族的 `on_init` 会经 `engine.load_bar` 加载一段历史日线做预热,那些根数也记在
    `am.count` 里——不能把它们误判成"凭空多出来的 K 线"。所以包住 `engine.load_bar`,
    把**引擎实际交出去的**预热 K 线记下来,与回放的 K 线一起当作"合法喂入"。"""
    from risk import constitution as const

    holder: dict = {"warmup": [], "market": const.snapshot_market_data(engine.strategy),
                    "findings": [], "watch": None}
    original = engine.load_bar

    def load_bar_recording(*args, **kwargs):
        result = original(*args, **kwargs)
        try:
            holder["warmup"].extend(result or [])
        except TypeError:                                   # noqa: BLE001 - 返回值不是可迭代就不记
            pass
        return result

    engine.load_bar = load_bar_recording

    # 逐根核对(第 13 轮 codex 的 Critical 1):包住 `new_bar`,每处理完一根 K 线核对一次行情容器。
    # 只在结束时核对末态,"改 -> 用 -> 改回"的瞬时改写就看不见。
    # 观察器第一次回放时才建:此时预热已结束、`history_data` 已就绪。
    original_new_bar = engine.new_bar

    def new_bar_watched(data):
        if holder["watch"] is None:
            holder["watch"] = const.MarketDataWatch(
                engine.strategy, holder["market"], holder["warmup"], engine.history_data)
        original_new_bar(data)
        holder["findings"].extend(holder["watch"].step())

    engine.new_bar = new_bar_watched
    return holder


def _market_feed(holder: dict, engine) -> list:
    """第五层眼里"引擎合法喂过的 K 线":预热喂入 + 回放喂入。"""
    return list(holder.get("warmup") or []) + list(engine.history_data)


def _risk_floor_snapshot(strategy) -> dict:
    """回测开始前取一份风控状态快照(运行时第三层的比对基准)。"""
    from risk import constitution as const

    return const.snapshot_instance_state(strategy)


def _protected_state_snapshot() -> dict:
    """回测开始前拍一张"受保护对象身份指纹"(运行时第四层的比对基准)。"""
    from risk import constitution as const

    return const.snapshot_protected_state()


def _assert_risk_floor_intact(strategy, snapshot: dict, expected_size: float,
                               protected_before: dict | None = None,
                               bars=None, market_baseline: dict | None = None,
                               market_findings: list | None = None) -> None:
    """回测跑完后核对风控底线还在(不可变宪法的运行时第三层)。

    静态筛查拦的是"写法",类校验拦的是"类字典"——两者都看不见**回测途中**
    才被改写的实例状态:属性是在 `entry_signal` 里被赋值的,类字典里根本没有它。
    第 7 轮 codex 实测:`self.cta_engine.size = 0.001` 让止损距离算出来的风险金额
    接近 0,`promotion_gate` 的两道回撤门槛随之失效,而三层校验全绿。
    这里在回测结束时核对:熔断阈值、合约乘数、风险预算必须与**回测开始时**一致。

    比的是快照而不是模板常量:研究脚本会用更大的 risk_budget 做敏感性实验
    (`tests/test_strategy_backtest.py` 里就是),那是合法的;被改写的是"回测途中"。

    第四层查的是另一件事:候选在回测途中把**框架对象**换掉了(第 9 轮 codex 实测的
    模块级 monkey-patch)——那不是实例状态,是整个风控模块被架空。"""
    from risk import constitution as const

    findings = const.check_instance_state(strategy, expected_size=expected_size, snapshot=snapshot)
    if protected_before is not None:
        findings.extend(const.check_protected_state(protected_before, const.snapshot_protected_state()))
    if bars is not None:
        # 第五层:行情容器必须还是引擎喂进去的那些 K 线。
        # 静态层按名字封,总有封不到的写法(视图、dunder 写入口、别名);
        # 第 12 轮 codex 的 High 1 实测那三条能把 ¥300 单笔风险预算架空。
        findings.extend(const.check_market_data_integrity(strategy, bars, market_baseline))
    findings.extend(market_findings or [])           # 逐根核对攒下的发现
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
