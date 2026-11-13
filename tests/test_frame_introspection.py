"""栈帧/回溯内省:候选沿栈帧走到可信代码的局部变量(回测引擎、研究编排、模板),与 `self.cta_engine`(引用即拦)是同一个能力。

自查发现(先于只读视图、在已合并的宪法里就存在):受限导入器放行了 `traceback`,
`traceback.walk_stack(None)` 不需要写任何被禁的属性名就能拿到调用栈上每个栈帧的 `f_locals`;
`e.__traceback__.tb_frame.f_back.f_locals` 这条链上的名字也都不在原来的禁用清单里
(第 7 轮的注释写过这条路径,当时只把 `setattr` 从候选的内建里拿掉,没有拿掉"沿栈帧走到可信代码"这个能力)。

这里的结论要说准:**静态层是这条路的门**。进程内的 Python 没有办法在运行期完全封死栈帧——
所以下面分两层测:静态层要拦住每个名字和每个模块;运行期只测"换入期间,调用链上没有任何一个栈帧的局部变量里放着真容器"。
"""
import sys

import pytest

import bootstrap  # noqa: F401  (必须先于 vnpy)
import research_sandbox as rs
from risk import constitution as const
from risk.market_view import ReadOnlyViewError  # noqa: F401
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
from vnpy.trader.utility import ArrayManager, BarGenerator

import backtest_lib
from test_constitution_round12 import _TRANSIENT_SETTING, _synth_bars


def flagged(source: str) -> bool:
    return bool(rs.screen_source(source)) or bool(const.screen_source(source))


FRAME_NAMES = ["__traceback__", "tb_frame", "tb_next", "f_back", "f_locals", "f_globals", "f_builtins", "f_code",
               "f_trace", "gi_frame", "gi_code", "gi_yieldfrom", "cr_frame", "cr_code", "cr_await", "ag_frame",
               "ag_code", "ag_await", "with_traceback"]


# ---------- 静态层:每个名字 ----------

@pytest.mark.parametrize("name", FRAME_NAMES)
def test_every_frame_and_traceback_attribute_is_refused_by_the_static_layer(name):
    source = f"def entry_signal(self):\n    x = something.{name}\n    return 0\n"
    assert flagged(source), f"{name} 没被拦"


@pytest.mark.parametrize("name", FRAME_NAMES)
def test_the_names_are_refused_as_strings_too(name):
    """属性名作为字符串常量出现同样拒(防 `format`/`operator` 之类用字符串走反射)。"""
    assert flagged(f'def entry_signal(self):\n    k = "{name}"\n    return 0\n'), name


def test_the_exact_chain_from_the_round_7_comment_is_refused():
    source = ("def entry_signal(self):\n    try:\n        raise ValueError\n    except ValueError as e:\n"
              "        f = e.__traceback__.tb_frame.f_back\n        v = f.f_locals\n    return 0\n")
    assert flagged(source)


def test_the_generator_frame_chain_is_refused():
    assert flagged("def entry_signal(self):\n    g = (i for i in range(3))\n    f = g.gi_frame.f_back\n    return 0\n")


# ---------- 静态层:每个模块 ----------

MODULES = ["traceback", "linecache", "code", "codeop", "pdb", "bdb", "dis", "faulthandler", "tracemalloc", "runpy",
           "pkgutil", "zipimport"]


@pytest.mark.parametrize("module", MODULES)
def test_the_introspection_modules_are_refused_by_the_static_layer(module):
    assert flagged(f"import {module}\n\ndef entry_signal(self):\n    return 0\n"), module
    assert flagged(f"from {module} import *\n\ndef entry_signal(self):\n    return 0\n"), module
    assert flagged(f"import {module} as m\n\ndef entry_signal(self):\n    return 0\n"), module


@pytest.mark.parametrize("module", MODULES)
def test_the_restricted_importer_refuses_them_at_run_time_too(module):
    with pytest.raises(ImportError):
        rs.safe_builtins()["__import__"](module)


def test_the_walk_stack_attack_that_names_nothing_forbidden_is_refused_because_of_the_module():
    """这条攻击不写任何被禁的属性名——只靠 `import traceback`。原来两层都放行。"""
    source = ("import traceback\n\ndef entry_signal(self):\n    for frame, _ in traceback.walk_stack(None):\n"
              "        data = frame.f_locals\n    return 0\n")
    assert flagged(source)


@pytest.mark.parametrize("module", ["math", "statistics", "datetime", "numpy", "collections", "itertools", "functools",
                                    "enum", "typing", "dataclasses"])
def test_the_modules_an_honest_candidate_needs_are_still_allowed(module):
    assert not rs.screen_source(f"import {module}\n\ndef entry_signal(self):\n    return 0\n")
    rs.safe_builtins()["__import__"](module)


def test_an_honest_candidate_is_not_flagged():
    source = ("import math\nimport numpy as np\n\nclass C(Base):\n    param_grid = {'fast_window': [5, 10, 20]}\n"
              "    def entry_signal(self):\n        fast = self.am.sma(self.fast_window, array=True)\n"
              "        slow = self.am.sma(self.slow_window, array=True)\n"
              "        if fast[-1] > slow[-1] and fast[-2] <= slow[-2]:\n            return 1\n        return 0\n")
    assert not flagged(source), (rs.screen_source(source), const.screen_source(source))


# ---------- 运行期纵深防御:调用链上没有栈帧的局部变量里放着真容器 ----------

def test_while_the_candidate_runs_no_frame_on_the_call_chain_holds_the_real_container():
    """即使有人绕过静态层真的沿栈帧往上走:`_call_entry_signal` 的局部变量里不再放真实的 am/bg
    (原先写成 `real_am, real_bg = self.am, self.bg`,等于把真容器放在候选一步就走得到的地方)。
    这里在候选里用 `sys._getframe` 逐帧检查(测试代码不受沙盒限制)。"""
    held = []

    class Walker(TrendAtr15mStrategy):
        def entry_signal(self):
            frame = sys._getframe(0)
            while frame is not None:
                for name, value in frame.f_locals.items():
                    if isinstance(value, (ArrayManager, BarGenerator)):
                        held.append((frame.f_code.co_name, name, type(value).__name__))
                frame = frame.f_back
            return 0

    backtest_lib.run_engine_bars(Walker, dict(_TRANSIENT_SETTING), "C15", _synth_bars())
    assert held == [], f"调用链上有栈帧的局部变量里放着真容器: {held}"


def test_the_call_entry_signal_frame_holds_only_self():
    seen = {}

    class Probe(TrendAtr15mStrategy):
        def entry_signal(self):
            caller = sys._getframe(1)
            seen["function"], seen["locals"] = caller.f_code.co_name, sorted(caller.f_locals)
            return 0

    backtest_lib.run_engine_bars(Probe, dict(_TRANSIENT_SETTING), "C15", _synth_bars())
    assert seen["function"] == "_call_entry_signal" and seen["locals"] == ["self"]


def test_the_real_objects_are_still_restored_after_the_call_and_after_an_exception():
    class Boom(TrendAtr15mStrategy):
        def entry_signal(self):
            raise ValueError("候选自己的 bug")

    from risk.market_view import _REAL, read_only_view
    from test_market_view import bare_strategy
    strategy = bare_strategy(Boom)
    real_am, real_bg = strategy.am, strategy.bg
    with pytest.raises(ValueError):
        strategy._call_entry_signal()
    assert strategy.am is real_am and strategy.bg is real_bg and strategy not in _REAL


def test_a_second_call_does_not_leak_the_previous_entry():
    from risk.market_view import _REAL
    from test_market_view import bare_strategy

    class Quiet(TrendAtr15mStrategy):
        def entry_signal(self):
            return 0

    strategy = bare_strategy(Quiet)
    for _ in range(3):
        strategy._call_entry_signal()
        assert strategy not in _REAL
