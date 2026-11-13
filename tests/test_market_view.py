"""候选在 entry_signal 里拿到的只读行情视图(收能力)。

单元:视图返回拷贝、没有变更入口、不可变、拿不到真实容器的引用。
端到端:用真实回测引擎、**不经过静态筛查**,第 12–14 轮 codex 的"改写行情容器"攻击现在没有可改的对象——
这正是这一层与此前逐条堵写法的区别:它不依赖静态层认得出写法。
"""
import numpy as np
import pytest

import backtest_lib
from risk.market_view import NO_ACCESS, ReadOnlyViewError, read_only_view
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
from test_constitution_round12 import _TRANSIENT_SETTING, _synth_bars
from vnpy.trader.utility import ArrayManager

from datetime import datetime, timedelta

from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData


def filled_am(n=80, size=40):
    am = ArrayManager(size)
    start = datetime(2020, 1, 2, 9, 0)
    for i in range(n):
        price = 100.0 + 5.0 * np.sin(i / 7.0)
        am.update_bar(BarData(symbol="X", exchange=Exchange.DCE, datetime=start + timedelta(minutes=15 * i),
                              interval=Interval.MINUTE, open_price=price, high_price=price + 1.0, low_price=price - 1.0,
                              close_price=price + 0.3, volume=100.0 + i, turnover=1000.0 + i, open_interest=50.0 + i,
                              gateway_name="TEST"))
    return am


# ---------- 拷贝:写了也只是改自己的副本 ----------

@pytest.mark.parametrize("name", ["open", "high", "low", "close", "volume", "turnover", "open_interest",
                                  "open_array", "high_array", "low_array", "close_array", "volume_array",
                                  "turnover_array", "open_interest_array"])
def test_every_array_is_a_copy_so_writing_it_cannot_reach_the_engines_data(name):
    am = filled_am()
    view = read_only_view(am)
    before = np.array(getattr(am, name), copy=True)
    out = getattr(view, name)
    out[:] = -999.0
    out[-1] = 12345.0
    assert np.array_equal(getattr(am, name), before)
    assert not np.shares_memory(getattr(view, name), getattr(am, name))


def test_turning_the_writeable_flag_back_on_does_not_help_because_it_is_a_copy():
    """只读视图(`flags.writeable = False`)不够:基础数组可写时 numpy 允许重新打开可写。拷贝没有这个问题。"""
    am = filled_am()
    before = am.close_array.copy()
    out = read_only_view(am).close_array
    out.flags.writeable = False
    out.flags.writeable = True
    out[:] = 0.0
    assert np.array_equal(am.close_array, before)


def test_slices_and_views_of_what_the_view_returns_do_not_reach_the_data_either():
    am = filled_am()
    before = am.high_array.copy()
    out = read_only_view(am).high
    out[:, None][...] = 0.0                       # 第 13 轮的 `[:, None]`
    out.reshape(-1)[0] = 1.0                       # reshape 视图
    out.view()[1] = 2.0
    out.__setitem__(slice(None), 3.0)              # dunder 写入口
    out.T[0] = 4.0
    assert np.array_equal(am.high_array, before)


@pytest.mark.parametrize("call", [
    lambda v: v.sma(10, array=True), lambda v: v.ema(10, array=True), lambda v: v.atr(14, array=True),
    lambda v: v.rsi(14, array=True), lambda v: v.macd(12, 26, 9, array=True), lambda v: v.boll(20, 2.0, array=True),
    lambda v: v.donchian(20, array=True), lambda v: v.std(10, array=True),
])
def test_indicator_results_are_the_same_numbers_the_real_container_gives(call):
    am = filled_am()
    real, seen = call(am), call(read_only_view(am))
    for a, b in zip(np.atleast_1d(real) if not isinstance(real, tuple) else real,
                    np.atleast_1d(seen) if not isinstance(seen, tuple) else seen):
        assert np.array_equal(np.asarray(a), np.asarray(b), equal_nan=True)


def test_every_indicator_the_real_container_has_is_available_and_equal_for_honest_candidates():
    am = filled_am()
    view = read_only_view(am)
    for name in dir(am):
        if name.startswith("_") or name in ("update_bar",) or not callable(getattr(am, name)):
            continue
        assert callable(getattr(view, name)), name
    assert view.sma(10) == am.sma(10) and view.atr(14) == am.atr(14)       # 标量:同一个数


def test_the_scalars_match_the_real_container():
    am = filled_am()
    view = read_only_view(am)
    assert (view.count, view.size, view.inited) == (am.count, am.size, am.inited)


def test_the_view_follows_the_container_as_it_grows():
    """视图造一次反复用:容器继续喂 K 线之后,读到的是最新的数据。"""
    am = filled_am(60)
    view = read_only_view(am)
    first = view.close[-1]
    am.update_bar(BarData(symbol="X", exchange=Exchange.DCE, datetime=datetime(2021, 1, 1), interval=Interval.MINUTE,
                          open_price=1.0, high_price=2.0, low_price=0.5, close_price=1.5, volume=1.0, turnover=1.0,
                          open_interest=1.0, gateway_name="TEST"))
    assert view.close[-1] == 1.5 and view.close[-1] != first and view.count == am.count


# ---------- 没有变更入口、不可变、拿不到真容器 ----------

def test_there_is_no_way_to_feed_a_bar_into_the_view():
    view = read_only_view(filled_am())
    assert not hasattr(view, "update_bar")
    with pytest.raises(AttributeError):
        view.update_bar(None)


@pytest.mark.parametrize("name, value", [("count", 0), ("size", 5), ("inited", False), ("close", [1.0]), ("x", 1)])
def test_the_view_refuses_attribute_assignment(name, value):
    view = read_only_view(filled_am())
    with pytest.raises(ReadOnlyViewError):
        setattr(view, name, value)


def test_the_view_refuses_attribute_deletion_and_has_no_dict_to_stash_things_in():
    view = read_only_view(filled_am())
    with pytest.raises(ReadOnlyViewError):
        delattr(view, "close")
    with pytest.raises(AttributeError):
        view.__dict__                                              # noqa: B018  (__slots__ = ():没有实例字典)
    with pytest.raises(AttributeError):
        object.__setattr__(view, "stash", 1)


def test_the_real_container_is_not_an_attribute_of_the_view():
    """真实容器只存在于方法的闭包里(要取出它需要受禁的 __closure__):属性里一个都没有。"""
    am = filled_am()
    view = read_only_view(am)
    for name in dir(view):
        if name.startswith("__") and name.endswith("__"):
            continue
        try:
            value = getattr(view, name)
        except Exception:                                          # noqa: BLE001
            continue
        assert value is not am, name


def test_the_no_access_sentinel_refuses_everything():
    with pytest.raises(ReadOnlyViewError):
        NO_ACCESS.update_bar
    with pytest.raises(ReadOnlyViewError):
        NO_ACCESS.anything = 1
    with pytest.raises(ReadOnlyViewError):
        getattr(NO_ACCESS, "daily_bar")


def test_views_are_independent_per_container():
    a, b = filled_am(80), filled_am(50)
    va, vb = read_only_view(a), read_only_view(b)
    assert va.count == a.count != vb.count == b.count


# ---------- 模板:换入/还原 ----------

def bare_strategy(cls):
    """不跑 __init__ 的最小实例:只用来检验 `_call_entry_signal` 的换入与还原。"""
    strategy = object.__new__(cls)
    strategy.am = filled_am()
    strategy.bg = object()
    strategy._am_view = read_only_view(strategy.am)
    return strategy


def test_during_entry_signal_the_candidate_sees_the_view_and_afterwards_the_real_objects_are_back():
    seen = {}

    class Probe(TrendAtr15mStrategy):
        def entry_signal(self):
            seen["am"], seen["bg"] = type(self.am).__name__, repr(self.bg)
            return 0

    strategy = bare_strategy(Probe)
    real_am, real_bg = strategy.am, strategy.bg
    assert strategy._call_entry_signal() == 0
    assert seen == {"am": "ReadOnlyMarketView", "bg": "<禁止访问>"}
    assert strategy.am is real_am and strategy.bg is real_bg


def test_the_real_objects_are_restored_even_when_the_candidate_raises():
    class Boom(TrendAtr15mStrategy):
        def entry_signal(self):
            raise ValueError("候选自己的 bug")

    strategy = bare_strategy(Boom)
    real_am, real_bg = strategy.am, strategy.bg
    with pytest.raises(ValueError):
        strategy._call_entry_signal()
    assert strategy.am is real_am and strategy.bg is real_bg


def test_the_template_never_calls_entry_signal_without_the_view():
    """模板里只有 `_call_entry_signal` 调 entry_signal:换入点是唯一的。"""
    import ast
    import inspect

    from strategies import trend_atr_strategy
    tree = ast.parse(inspect.getsource(trend_atr_strategy))
    callers = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "entry_signal"
                       for n in ast.walk(node))]
    assert callers == ["_call_entry_signal"], callers


# ---------- 端到端:真实引擎、不经静态筛查,攻击没有可改的对象 ----------

SETTING = dict(_TRANSIENT_SETTING)
# 一根**合法**的假 K 线:喂给真容器它是能被接受的(不是 None 那种怎么都会抛错的东西),
# 所以只有"根本碰不到真容器"才能让攻击失败
FAKE_BAR = BarData(symbol="C15", exchange=Exchange.DCE, datetime=datetime(2020, 1, 1), interval=Interval.MINUTE,
                   open_price=1.0, high_price=1.0, low_price=1.0, close_price=1.0, volume=1.0, turnover=0.0,
                   open_interest=0.0, gateway_name="TEST")


def run_candidate(cls, bars=None):
    bars = bars or _synth_bars()
    return backtest_lib.run_engine_bars(cls, dict(SETTING), "C15", bars)


def fingerprint(engine):
    trades = [(t.datetime, t.direction.name, t.offset.name, float(t.price), float(t.volume)) for t in engine.get_all_trades()]
    return {"trades": trades, "atr": float(engine.strategy.atr_value), "skipped": engine.strategy.skipped_by_budget}


@pytest.fixture(scope="module")
def honest():
    return fingerprint(run_candidate(TrendAtr15mStrategy))


def test_the_honest_template_is_unchanged_by_the_view_it_just_reads(honest):
    assert honest["atr"] > 0                                                          # 跑出了真东西


def test_writing_every_array_the_view_gives_changes_nothing(honest):
    """第 12–14 轮的写回类攻击:`close[:] = ..`、`[:, None]`、`__setitem__`、reshape/view/T。全部写到副本上。"""
    class WriteBack(TrendAtr15mStrategy):
        def entry_signal(self):
            c = self.am.close
            c[:] = 1.0
            self.am.high_array[-20:] = 0.0
            self.am.low[:, None][...] = 9e9
            self.am.close.__setitem__(slice(None), 5.0)
            self.am.open.reshape(-1)[0] = 7.0
            self.am.high.view()[1] = 8.0
            self.am.close_array.T[0] = 9.0
            return super().entry_signal()

    assert fingerprint(run_candidate(WriteBack)) == honest


def test_the_codex_round_13_atr_collapse_attack_has_nothing_to_change(honest):
    """第 13 轮 Critical:把最近 45 根的高/低/收改成同一个值让 ATR 塌陷(跨 K 线改写再还原)。
    现在每次拿到的都是副本,改了也不会留在容器里——下一根 K 线的 ATR 与诚实模板逐位相同。"""
    class Collapse(TrendAtr15mStrategy):
        def entry_signal(self):
            hc, lc, cc = self.am.high[:, None], self.am.low[:, None], self.am.close[:, None]
            c = cc[-45:, 0].copy()
            hc[-45:, 0], lc[-45:, 0], cc[-45:, 0] = c, c, c
            return super().entry_signal()

    assert fingerprint(run_candidate(Collapse)) == honest


@pytest.mark.parametrize("body", [
    "self.am.update_bar(FAKE_BAR)",                                        # 往容器里插一根合法的假 K 线
    "self.am.count -= 1",                                                  # 第 14 轮 High 1:让计数落后一根
    "self.am.size = 5",                                                    # 第 14 轮 Medium 2:改容量
    "self.am.count = 10**6",
    "self.am.inited = True",
    "del self.am.close",
    "self.am.stash = 1",
    "self.bg.update_bar(FAKE_BAR)",                                        # 第 11 轮:往 BarGenerator 里插假 K 线
    "self.bg.daily_bar = None",
])
def test_every_attempt_to_mutate_the_containers_is_refused_with_no_effect_on_the_data(body):
    ns: dict = {}
    exec(f"def entry_signal(self):\n    {body}\n    return 0\n", {"FAKE_BAR": FAKE_BAR}, ns)                 # noqa: S102  (测试里造候选)
    Cand = type("Cand", (TrendAtr15mStrategy,), {"entry_signal": ns["entry_signal"]})
    with pytest.raises(RuntimeError, match="回测中策略抛出异常"):
        run_candidate(Cand)


def test_the_candidate_cannot_reach_the_real_container_through_the_template_attributes(honest):
    """`self._am_view` 给的还是视图;`self.am` 在 entry_signal 里就是视图——候选手里没有第二条路。"""
    class Probe(TrendAtr15mStrategy):
        def entry_signal(self):
            assert type(self.am).__name__ == "ReadOnlyMarketView"
            assert type(self._am_view).__name__ == "ReadOnlyMarketView"
            self._am_view.close[:] = 0.0
            return super().entry_signal()

    assert fingerprint(run_candidate(Probe)) == honest


def test_the_runtime_layers_still_work_as_defense_in_depth_when_something_else_reaches_the_real_container():
    """视图是新增的一层,不是替换:假设将来某条别的路径拿到了真实容器,第五层仍然逐根核对。
    这里用一个覆盖了换入点的类模拟"视图被绕过"(静态层与 check_class 会拦这种类;此处直接绕过它们来测运行期)。"""
    class ViewBypassed(TrendAtr15mStrategy):
        def _call_entry_signal(self):
            self.am.count -= 1                                             # 直接改真实容器
            return super()._call_entry_signal()

    with pytest.raises(RuntimeError, match="累计根数"):
        run_candidate(ViewBypassed)


def test_overriding_the_swap_point_is_refused_by_the_static_layer_and_the_class_check():
    """候选想覆盖 `_call_entry_signal` 来绕开视图:静态层与运行时类校验都拦。"""
    from risk import constitution as const
    findings = const.screen_source("class C:\n    def _call_entry_signal(self):\n        return 1\n")
    assert findings, "静态层没拦住对换入点的覆盖"
    assert any("_call_entry_signal" in f for f in findings)
    findings = const.screen_source("def entry_signal(self):\n    self._call_entry_signal()\n    return 0\n")
    assert findings, "静态层没拦住对换入点的引用"
    problems = const.check_class(type("Cand", (TrendAtr15mStrategy,), {"_call_entry_signal": lambda self: 1}),
                                 const.load_template())
    assert problems


def test_a_candidate_may_still_read_everything_it_legitimately_needs(honest):
    """诚实候选的正常读取(各种指标、数组切片、标量)全部照常工作。"""
    class Reader(TrendAtr15mStrategy):
        def entry_signal(self):
            fast = self.am.sma(self.fast_window, array=True)
            slow = self.am.sma(self.slow_window, array=True)
            _ = (self.am.close[-1], self.am.high[-5:].max(), self.am.low_array[-10:].min(), self.am.count, self.am.size,
                 self.am.atr(14), self.am.rsi(14), self.am.boll(20, 2.0), self.am.macd(12, 26, 9))
            if fast[-1] > slow[-1] and fast[-2] <= slow[-2]:
                return 1
            if fast[-1] < slow[-1] and fast[-2] >= slow[-2]:
                return -1
            return 0

    run_candidate(Reader)                                                  # 不抛错即可


# ---------- 对诚实策略完全透明:有真实成交的行情上,有视图与无视图逐笔一致 ----------

def _wavy_bars(n=400):
    """合成 15 分钟 K 线:一段上涨、一段下跌地来回,保证均线交叉、有若干往返。"""
    import math
    start = datetime(2020, 1, 2, 9, 0)
    bars = []
    for i in range(n):
        price = 2000.0 + 60.0 * math.sin(i / 25.0)
        bars.append(BarData(symbol="C15", exchange=Exchange.DCE, datetime=start + timedelta(minutes=15 * i),
                            interval=Interval.MINUTE, open_price=price, high_price=price + 4.0, low_price=price - 4.0,
                            close_price=price + 0.5, volume=100, turnover=0.0, open_interest=0.0, gateway_name="TEST"))
    return bars


def test_the_view_is_transparent_to_an_honest_strategy_trade_for_trade():
    """视图对诚实候选必须完全透明:同样的方法、同样的数值。用有真实成交的行情逐笔对照,
    (合成的 120 根行情上诚实模板 0 成交,"一致"是平凡成立的,不能当证据)。"""
    class NoView(TrendAtr15mStrategy):
        def _call_entry_signal(self):                       # 对照:不换入视图,直接调
            return self.entry_signal()

    setting = {**SETTING, "risk_budget": 100_000.0, "fast_window": 5, "slow_window": 20}
    bars = _wavy_bars()
    with_view = backtest_lib.run_engine_bars(TrendAtr15mStrategy, dict(setting), "C15", bars)
    without = backtest_lib.run_engine_bars(NoView, dict(setting), "C15", bars)
    key = lambda e: [(t.datetime, t.direction.name, t.offset.name, float(t.price), float(t.volume)) for t in e.get_all_trades()]
    assert len(key(with_view)) >= 4, "合成行情应当产生成交,否则这条测试什么也没证明"
    assert key(with_view) == key(without)
    assert float(with_view.strategy.atr_value) == float(without.strategy.atr_value)
