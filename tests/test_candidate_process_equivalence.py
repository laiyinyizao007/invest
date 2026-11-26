"""A4:新旧两条路径对同一份(无状态)候选给出相同的绩效(Issue #29 S1 · ADR §7 验收标准 4)。

旧路径:候选类继承 `TrendAtr15mStrategy`,在**框架进程里**被引擎驱动。
新路径:候选写成 `SignalTemplate` 子类,在**独立进程**里算信号,框架用回放策略驱动同一台引擎。

K 线是**确定性合成的随机游走**,不依赖本机行情文件,CI 能跑(需要交易栈,没有就跳过)。
比较的是逐日盈亏序列、往返笔数、回撤——不是「夏普差不多」。
"""
from __future__ import annotations

import importlib.util
import random
from datetime import datetime, timedelta

import pytest

pytest.importorskip("vnpy_ctastrategy", reason="需要交易栈:CI 的研究侧闸门不装交易环境")
pytest.importorskip("talib", reason="候选进程里的指标用 ta-lib")
import bootstrap  # noqa: E402,F401  必须先于 vnpy.trader.database 导入(时区与状态目录设置)
import research_candidate_process as rcp
import research_candidate_spec as spec_mod
import research_signal_replay as replay

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("vnpy_ctastrategy") is None,
    reason="需要交易栈(vnpy_ctastrategy):CI 的研究侧闸门不装交易环境")

OLD_BODY = '''
    breakout_n = 20

    def entry_signal(self):
        up, dn = self.am.donchian(self.breakout_n, array=True)
        close = self.am.close[-1]
        if close > up[-2]:
            return 1
        if close < dn[-2]:
            return -1
        return 0
'''
NEW_SOURCE = '''
from candidate.signal_template import SignalTemplate


class Candidate(SignalTemplate):
    parameters = ["fast_window", "slow_window", "atr_mult", "atr_window", "breakout_n"]
    param_grid = {"breakout_n": [10, 20, 30], "fast_window": [5, 8, 10], "atr_mult": [1.5, 2.0, 2.5]}
''' + OLD_BODY


def _vnpy_bars(n: int, seed: int = 7):
    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData
    rng = random.Random(seed)
    price, out, start = 2500.0, [], datetime(2021, 3, 1, 9, 0)
    for i in range(n):
        o = price
        c = o + rng.uniform(-9, 9)
        out.append(BarData(symbol="CADJ15", exchange=Exchange.DCE, datetime=start + timedelta(minutes=15 * i),
                           interval=Interval.MINUTE, volume=float(rng.randint(100, 5000)), turnover=0.0,
                           open_interest=float(rng.randint(10000, 20000)), open_price=o,
                           high_price=max(o, c) + rng.uniform(0, 4), low_price=min(o, c) - rng.uniform(0, 4),
                           close_price=c, gateway_name="T"))
        price = c
    return out


@pytest.fixture(scope="module")
def stack():
    from research_gate import engine_runner
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    old_cls = type("OldCandidate", (TrendAtr15mStrategy,), {
        "parameters": TrendAtr15mStrategy.parameters + ["breakout_n"], "breakout_n": 20,
        "entry_signal": lambda self: _old_signal(self)})
    spec = spec_mod.parse_spec(NEW_SOURCE)
    proxy = spec_mod.proxy_class(spec)
    engine = engine_runner("C15")
    runner = replay.make_signal_runner(source=NEW_SOURCE, spec=spec, proxy=proxy, base=TrendAtr15mStrategy,
                                       engine=engine, check_setting=lambda s: None,
                                       framework_parameters=TrendAtr15mStrategy.parameters)
    return old_cls, proxy, runner, engine, spec, TrendAtr15mStrategy


def _old_signal(self):
    up, dn = self.am.donchian(self.breakout_n, array=True)
    close = self.am.close[-1]
    if close > up[-2]:
        return 1
    if close < dn[-2]:
        return -1
    return 0


@pytest.fixture(scope="module")
def bars():
    return _vnpy_bars(2400)


@pytest.mark.parametrize("setting", [
    {"breakout_n": 20},
    {"breakout_n": 10, "fast_window": 5, "slow_window": 30, "atr_mult": 2.0},
    {"breakout_n": 30, "atr_mult": 2.5},
])
@pytest.mark.parametrize("cost_multiplier", [1.0, 2.0])
def test_a4_new_path_matches_old_path_exactly(stack, bars, setting, cost_multiplier):
    old_cls, proxy, runner, engine, *_ = stack
    old = engine(old_cls, dict(setting), bars, cost_multiplier)
    new = runner(proxy, dict(setting), bars, cost_multiplier)
    assert old["round_trips"] > 0, "探针前提:合成行情上这套参数应当有交易,否则等价性没有被检验到"
    assert new["round_trips"] == old["round_trips"]
    assert new["daily_pnls"] == old["daily_pnls"]
    assert new["maxdd_pct"] == old["maxdd_pct"] and new["maxdd_yuan"] == old["maxdd_yuan"]


def test_candidate_process_is_started_once_per_params_and_window_not_once_per_cost_multiplier(stack, bars):
    _, proxy, runner, *_ = stack
    runner.cache.clear()
    runner(proxy, {"breakout_n": 20}, bars, 1.0)
    runner(proxy, {"breakout_n": 20}, bars, 2.0)
    assert len(runner.cache) == 1
    runner(proxy, {"breakout_n": 30}, bars, 1.0)
    assert len(runner.cache) == 2


def test_class_body_default_reaches_the_framework_engine_too(stack, bars):
    """候选类体写 `atr_mult = 2.0` 会改变框架模板的止损距离:新路径框架侧要采用同样的有效参数。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    engine = stack[3]
    src = NEW_SOURCE.replace("    breakout_n = 20\n", "    breakout_n = 20\n    atr_mult = 2.5\n", 1)
    spec = spec_mod.parse_spec(src)
    proxy = spec_mod.proxy_class(spec)
    runner = replay.make_signal_runner(source=src, spec=spec, proxy=proxy, base=TrendAtr15mStrategy, engine=engine,
                                       check_setting=lambda s: None, framework_parameters=TrendAtr15mStrategy.parameters)
    old_cls = type("OldWithDefault", (TrendAtr15mStrategy,), {
        "parameters": TrendAtr15mStrategy.parameters + ["breakout_n"], "breakout_n": 20, "atr_mult": 2.5,
        "entry_signal": lambda self: _old_signal(self)})
    expected = engine(old_cls, {}, bars, 1.0)
    plain = engine(stack[0], {}, bars, 1.0)
    got = runner(proxy, {}, bars, 1.0)
    assert expected["daily_pnls"] != plain["daily_pnls"], "探针前提:atr_mult 2.5 与默认 1.5 的结果应当不同"
    assert got["daily_pnls"] == expected["daily_pnls"]


def test_protected_default_in_class_body_is_refused_by_the_setting_check(stack, bars):
    """类体里写 `risk_budget = 1e9`:框架设置必须过受保护参数检查,不得悄悄生效。"""
    from risk import constitution as const
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    engine = stack[3]
    src = NEW_SOURCE.replace("    breakout_n = 20\n", "    breakout_n = 20\n    risk_budget = 1000000000\n", 1)
    spec = spec_mod.parse_spec(src)
    proxy = spec_mod.proxy_class(spec)
    constitution = const.load()

    def check(setting):
        findings = const.check_params(setting, constitution)
        if findings:
            raise ValueError("; ".join(findings))

    runner = replay.make_signal_runner(source=src, spec=spec, proxy=proxy, base=TrendAtr15mStrategy, engine=engine,
                                       check_setting=check, framework_parameters=TrendAtr15mStrategy.parameters)
    with pytest.raises(ValueError):
        runner(proxy, {}, bars, 1.0)


def test_non_candidate_classes_go_straight_to_the_engine(stack, bars):
    """随机入场对照这类框架自己的类不起候选进程。"""
    from strategies.random_entry_15m_strategy import RandomEntry15mStrategy
    _, _, runner, engine, *_ = stack
    runner.cache.clear()
    setting = {"entry_prob": 0.01, "seed": 3}
    assert runner(RandomEntry15mStrategy, dict(setting), bars, 1.0) == engine(RandomEntry15mStrategy, dict(setting), bars, 1.0)
    assert runner.cache == {}


def test_duplicate_timestamps_are_refused():
    one = _vnpy_bars(3)
    with pytest.raises(replay.SignalSeriesError):
        replay.signals_to_map([one[0], one[0], one[1]], [1, 0, -1])


def test_candidate_failure_surfaces_as_candidate_process_error(stack, bars):
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    engine = stack[3]
    src = NEW_SOURCE.replace("    def entry_signal(self):", "    def entry_signal(self):\n        raise ValueError('boom')\n    def _unused(self):", 1)
    spec = spec_mod.parse_spec(src)
    proxy = spec_mod.proxy_class(spec)
    runner = replay.make_signal_runner(source=src, spec=spec, proxy=proxy, base=TrendAtr15mStrategy, engine=engine,
                                       check_setting=lambda s: None, framework_parameters=TrendAtr15mStrategy.parameters)
    with pytest.raises(rcp.CandidateProcessError) as err:
        runner(proxy, {}, bars, 1.0)
    assert err.value.kind == "candidate_error"
