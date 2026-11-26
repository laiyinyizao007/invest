"""候选声明的 AST 解析必须与候选进程里**运行时实际生效**的声明一致(Issue #29 S1,G8 codex 审查发现并经本地复现)。

框架永远不 exec 候选,只读它的 AST;所以任何能让「AST 读到的」与「运行时生效的」不同的写法,
都必须被**拒绝评估**(fail-closed),而不是悄悄取其中一个。典型分歧:
- 类体里重复赋值:框架取第一次、运行时生效最后一次 ⇒ `parameters` 重复时网格轴对候选毫无作用,
  相邻格子信号完全相同,**参数高原被人为做成平坦**;
- 块里(`if`/`try`/`for`/`with`)赋值、`+=`、元组解包、`__init__` 里改写:框架看不见,静默用模板默认值;
- 类体之外改写(`Candidate.param_grid = ...`)、重绑定类名。
`locals()` / `setattr` / `metaclass=` 这几类由静态筛查拦(`research_sandbox.screen_source`),这里不重复。
纯函数测试,不需要交易栈,研究侧 CI 也能真跑。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_candidate_spec as spec_mod

HEAD = "from candidate.signal_template import SignalTemplate\n\n\n"
GRID = '    param_grid = {"fast_window": [5, 8, 10], "slow_window": [20, 30, 40], "atr_mult": [1.5, 2.0, 2.5]}\n'
PARAMS = '    parameters = ["fast_window", "slow_window", "atr_mult"]\n'


def cls(body: str, after: str = "") -> str:
    return HEAD + "class Candidate(SignalTemplate):\n" + body + after


GOOD_BODY = PARAMS + GRID + "    atr_mult = 2.0\n\n    def entry_signal(self):\n        return 0\n"


# ---------------------------------------------------------------- 放行:正常写法照常通过


def test_the_shipped_example_candidate_is_accepted():
    example = Path(__file__).parent / "fixtures" / "candidate_process" / "atr_breakout_15m_2atr_signal.py"
    assert spec_mod.parse_spec(example.read_text(encoding="utf-8")).class_name == "Candidate"


def test_ordinary_shapes_are_accepted():
    source = ('"""模块文档字符串"""\nimport math\n\nTHRESHOLD = 0.1\n\n\ndef helper(x):\n    return x * THRESHOLD\n\n\n'
              + HEAD.replace("\n\n\n", "\n") + "\nclass Candidate(SignalTemplate):\n    \"\"\"类文档字符串\"\"\"\n"
              + PARAMS + GRID + "    atr_mult = 2.0\n    breakout_n: int = 20\n    pass\n\n"
              + "    @property\n    def window(self):\n        return self.breakout_n\n\n"
              + "    def entry_signal(self):\n        return 1 if math.isfinite(helper(1)) else 0\n")
    assert spec_mod.parse_spec(source).defaults["atr_mult"] == 2.0


# ---------------------------------------------------------------- 拦截:类体里能造成「读到的 ≠ 生效的」的写法


@pytest.mark.parametrize("body, why", [
    (PARAMS + GRID + GRID.replace("[5, 8, 10]", "[5, 6, 7, 8, 9, 10]"), "param_grid 重复赋值(框架取第一次,运行时生效最后一次)"),
    (PARAMS + '    parameters = []\n' + GRID, "parameters 重复赋值 ⇒ 网格轴对候选无作用,高原被做平"),
    (PARAMS + GRID + "    atr_mult = 2.0\n    atr_mult = 9.0\n", "默认值重复赋值"),
    (PARAMS + GRID + "    if True:\n        atr_mult = 9.0\n", "if 块里赋值"),
    (PARAMS + GRID + "    try:\n        atr_mult = 9.0\n    except Exception:\n        pass\n", "try 块里赋值"),
    (PARAMS + GRID + "    for _ in range(1):\n        atr_mult = 9.0\n", "for 块里赋值"),
    (PARAMS + GRID + "    atr_mult = 1.0\n    atr_mult += 1.0\n", "增强赋值"),
    (PARAMS + GRID + "    atr_mult, slow_window = 9.0, 99\n", "元组解包目标"),
    (PARAMS + GRID + "    x = y = 1\n    x.z = 2\n", "属性目标"),
    (PARAMS + GRID + "    del parameters\n", "del"),
    (PARAMS + GRID + "    class Inner:\n        pass\n", "类体里嵌套类(__set_name__ 描述符借壳)"),
    (PARAMS + GRID + "    y = (z := 5)\n", "海象运算符"),
])
def test_class_bodies_that_make_ast_and_runtime_disagree_are_refused(body, why):
    with pytest.raises(spec_mod.SpecError):
        spec_mod.parse_spec(cls(body))
    assert why


@pytest.mark.parametrize("dunder", ["__init__", "__init_subclass__", "__set_name__", "__new__", "__setattr__", "__getattr__"])
def test_dunder_methods_are_refused(dunder):
    """`__init__` 里 `self.atr_mult = 5` 会让候选实际用 5、框架止损按类体字面量算,同一份源码两条路径止损不同。"""
    with pytest.raises(spec_mod.SpecError, match="__"):
        spec_mod.parse_spec(cls(PARAMS + GRID + f"\n    def {dunder}(self, *a, **k):\n        pass\n"))


# ---------------------------------------------------------------- 拦截:类体之外改写 / 重绑定类


@pytest.mark.parametrize("after, why", [
    ("\nCandidate.param_grid = {'fast_window': [1, 2, 3]}\n", "类体后改写属性"),
    ("\nCandidate.atr_mult = 9.0\n", "类体后改写默认值"),
    ("\nCandidate = decorate(Candidate)\n", "重绑定类名"),
    ("\nif True:\n    Candidate.atr_mult = 9.0\n", "模块级控制流"),
    ("\nCandidate.atr_mult += 1\n", "模块级增强赋值"),
    ("\ntry:\n    pass\nexcept Exception:\n    pass\n", "模块级 try"),
])
def test_module_level_statements_that_mutate_or_rebind_the_class_are_refused(after, why):
    with pytest.raises(spec_mod.SpecError):
        spec_mod.parse_spec(cls(GOOD_BODY, after))
    assert why


# ---------------------------------------------------------------- 回放:缓存键与信号对齐的防御


def _bars(n, close_offset=0.0, start=datetime(2021, 1, 4, 9, 0)):
    return [SimpleNamespace(datetime=start + timedelta(minutes=15 * i), open_price=100.0 + i, high_price=101.0 + i,
                            low_price=99.0 + i, close_price=100.0 + i + close_offset) for i in range(n)]


def _runner(monkeypatch):
    import research_signal_replay as rsr
    spec = spec_mod.parse_spec(cls(GOOD_BODY))
    proxy = spec_mod.proxy_class(spec)
    calls = []

    def fake_signals(source, class_name, params, bars, **kw):
        calls.append(len(bars))
        return [0] * len(bars)

    monkeypatch.setattr(rsr.rcp, "run_candidate_signals", fake_signals)
    runner = rsr.make_signal_runner(source="x", spec=spec, proxy=proxy, base=type("Base", (), {"parameters": ["fast_window"]}),
                                    engine=lambda c, s, b, m: {"ok": True}, check_setting=lambda s: None,
                                    framework_parameters=["fast_window", "atr_mult"])
    return runner, proxy, calls


def test_cache_does_not_share_signals_between_different_bars_with_the_same_endpoints_and_length(monkeypatch):
    runner, proxy, calls = _runner(monkeypatch)
    one, two = _bars(50), _bars(50)
    two[25].close_price += 7.0                     # 首末时间戳与根数都相同,中间不同
    runner(proxy, {"fast_window": 5}, one, 1.0)
    runner(proxy, {"fast_window": 5}, two, 1.0)
    assert len(calls) == 2, "不同的 K 线共用了同一份信号"
    runner(proxy, {"fast_window": 5}, one, 2.0)    # 同一份 K 线、只换成本倍数 ⇒ 复用
    assert len(calls) == 2


def test_signals_to_map_refuses_misaligned_or_illegal_series():
    import research_signal_replay as rsr
    bars = _bars(4)
    with pytest.raises(rsr.SignalSeriesError):
        rsr.signals_to_map(bars, [0, 1, 0])                   # 长度不符:zip 会静默截断
    with pytest.raises(rsr.SignalSeriesError):
        rsr.signals_to_map(bars, [0, 2, 0, 0])                # 取值域越界
    broken = _bars(4)
    broken[2].datetime = None
    with pytest.raises(rsr.SignalSeriesError):
        rsr.signals_to_map(broken, [0, 0, 0, 0])              # 缺时间戳:信号会存在一个永远取不到的键下
    assert rsr.signals_to_map(bars, [0, 1, 0, -1]) == {bars[1].datetime: 1, bars[3].datetime: -1}
