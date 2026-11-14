"""候选在 `entry_signal` 里拿到的**只读行情视图**(收能力,而不是再堵一种写法)。

背景见 docs/adr/20261113-read-only-market-view.md:宪法 G8 第 12–14 轮 codex 为 38.5 / 38.0 / 38.5,
逐条堵"改写行情容器"的**写法**没有收敛——视图、dunder 写入口、别名、`[:, None]`、改 `count`/`size`、
预热期塞假 K 线……每轮修一个,下一轮总能找到下一个。这些攻击的共同前提是:候选手里拿着**真实的容器**。
所以这一层不去枚举写法,而是让候选**拿不到真实容器**:

- 模板在调用 `entry_signal()` 的那一刻把 `self.am` 换成本模块给出的视图,调用结束(含异常)立刻还原;
  候选只覆盖 `entry_signal`(其余成员受保护),所以这是它碰得到行情容器的唯一时机;
- 视图里**没有任何变更入口**(`update_bar` 等),也不暴露真实容器的引用;
- 所有数组(`close`/`high`/…、`*_array`)与指标结果都是**拷贝**。不能返回"只读视图"(`flags.writeable = False`):
  numpy 允许对基础数组可写的视图重新打开可写,那样写回去照样改到引擎的数据;拷贝则写了也只是改自己的副本;
- 视图本身不可变(`__slots__ = ()` + 拒绝 `setattr`/`delattr`),也不能往上挂新属性;
- `self.bg`(分钟线合成器,与 `am` 是同一类容器)在这段时间里换成"禁止访问"的哨兵:候选没有任何理由碰它。

真实容器的引用只存在于视图方法的**闭包**里(没有 `_am` 之类的属性可读);要从闭包里拿出来,
需要 `__closure__`/`__globals__`/`__code__` 这些受禁 dunder——静态层本来就拦它们(见 BUILTIN_FORBIDDEN_DUNDERS)。
这仍是纵深防御而不是沙盒(与 research_sandbox.py 文件头一致),但它把"改写行情容器"这一整类
从"靠静态层与运行期逐条检测"变成"候选手里根本没有可改的东西"。

静态层与运行期第五层**保留**,不放宽:它们是这一层之外的纵深防御(模板自己的代码路径、将来别的入口)。
对诚实候选完全透明:同样的方法、同样的数值,只是返回的是副本。
"""
from __future__ import annotations

import weakref
from typing import Any

import numpy as np

# 行情数组(属性形式与 `*_array` 形式);读到的都是拷贝
_ARRAY_PROPS = ("open", "high", "low", "close", "volume", "turnover", "open_interest")
_ARRAYS = tuple(f"{name}_array" for name in _ARRAY_PROPS)
_SCALARS = ("count", "size", "inited")            # int / bool:不可变,直接给
# 只读指标方法的**显式白名单**(vnpy 4.5.0 的 ArrayManager,除 update_bar 之外的全部公开方法)。
# 第 2 轮 G8 复核的 Medium 2:原来是"黑名单一个 update_bar、其余全部转发",vnpy 升级新增写入口就会静默失守。
# 现在不在表里的公开可调用对象一律不转发(候选访问时 AttributeError)——新增指标要先被人看过再加进来。
_INDICATORS = frozenset({
    "ad", "adosc", "adx", "adxr", "apo", "aroon", "aroonosc", "atr", "boll", "bop", "cci", "cmo", "donchian", "dx",
    "ema", "kama", "keltner", "macd", "mfi", "minus_di", "minus_dm", "mom", "natr", "obv", "plus_di", "plus_dm",
    "ppo", "roc", "rocp", "rocr", "rocr_100", "rsi", "sar", "sma", "std", "stoch", "trange", "trix", "ultosc",
    "willr", "wma",
})


class ReadOnlyViewError(AttributeError):
    """试图改写只读行情视图,或访问被隐藏的容器。"""


def _detached(value: Any) -> Any:
    """把返回值里的 numpy 数组换成拷贝(元组/列表逐个处理);标量与其它对象原样给。"""
    if isinstance(value, np.ndarray):
        return value.copy()
    if isinstance(value, tuple):
        return tuple(_detached(item) for item in value)
    if isinstance(value, list):
        return [_detached(item) for item in value]
    return value


def _array_getter(am: Any, name: str):
    def getter(self):                                  # noqa: ANN001 - property 的 self 不用
        return np.array(getattr(am, name), copy=True)
    return getter


def _scalar_getter(am: Any, name: str):
    def getter(self):                                  # noqa: ANN001
        return getattr(am, name)
    return getter


def _method(am: Any, name: str):
    def method(self, *args, **kwargs):                 # noqa: ANN001
        return _detached(getattr(am, name)(*args, **kwargs))
    method.__name__ = name
    return method


def _deny(self, *args, **kwargs):                      # noqa: ANN001
    raise ReadOnlyViewError("行情视图是只读的:不能给它设置或删除任何属性")


def read_only_view(am: Any) -> Any:
    """为一个 ArrayManager 造它的只读视图。每个 ArrayManager 造一次、反复使用(类的构造不便宜)。"""
    namespace: dict[str, Any] = {"__slots__": ("__weakref__",)}
    for name in _ARRAY_PROPS + _ARRAYS:
        namespace[name] = property(_array_getter(am, name))
    for name in _SCALARS:
        namespace[name] = property(_scalar_getter(am, name))
    for name in dir(am):
        if name.startswith("_") or name in namespace or name not in _INDICATORS:
            continue
        if callable(getattr(am, name)):
            namespace[name] = _method(am, name)        # 指标方法(sma/atr/macd/…):结果一律拷贝
    namespace["__setattr__"] = _deny
    namespace["__delattr__"] = _deny
    namespace["__repr__"] = lambda self: "<只读行情视图>"
    view = type("ReadOnlyMarketView", (), namespace)()
    _SOURCE[view] = am                                 # 登记"这个视图包着谁",换入时核对同源
    return view


class _NoAccess:
    """哨兵:在 `entry_signal` 期间代替 `self.bg`,任何访问都抛 ReadOnlyViewError。"""

    __slots__ = ()

    def __getattr__(self, name: str) -> Any:
        raise ReadOnlyViewError("候选在 entry_signal 里不能访问分钟线合成器 bg")

    def __setattr__(self, name: str, value: Any) -> None:
        raise ReadOnlyViewError("候选在 entry_signal 里不能访问分钟线合成器 bg")

    def __repr__(self) -> str:
        return "<禁止访问>"


NO_ACCESS = _NoAccess()

# 换入期间真实容器的存放处。**不放在 `_call_entry_signal` 的局部变量里**:候选若沿栈帧走到调用者
# (`e.__traceback__.tb_frame.f_back.f_locals`),局部变量里放着真容器就等于白给。静态层封了栈帧内省的名字与模块,
# 这里再做一层:即使有人走到那个栈帧,里面只有 `self`(`self.am` 此刻已是视图)。
_REAL: "weakref.WeakKeyDictionary[Any, tuple[Any, Any]]" = weakref.WeakKeyDictionary()
_SOURCE: "weakref.WeakKeyDictionary[Any, Any]" = weakref.WeakKeyDictionary()


def swap_in(strategy: Any) -> None:
    """记下真实的 am/bg,换入只读视图与禁止访问的哨兵。

    fail-closed(第 2 轮 G8 复核的 High 1 / Medium 3):已经换入过、或当前 `am` 不是这个视图所包的那个容器,
    一律拒绝——否则 `_REAL` 里会被写进"视图"而不是真容器,`swap_out` 之后模板自己的行情管道就永久坏了。"""
    if strategy in _REAL:
        raise ReadOnlyViewError("已经换入过只读视图,不可重入")
    if _SOURCE.get(strategy._am_view) is not strategy.am:
        raise ReadOnlyViewError("当前 am 不是只读视图所包的容器(陈旧视图),拒绝换入")
    _REAL[strategy] = (strategy.am, strategy.bg)
    strategy.am, strategy.bg = strategy._am_view, NO_ACCESS


def swap_out(strategy: Any) -> None:
    """还原真实的 am/bg(配合 try/finally:异常时也要还原)。没有换入记录时什么都不做——
    在 `finally` 里抛 KeyError 会把候选自己的异常顶掉(第 2 轮 G8 复核的 High 2)。"""
    real = _REAL.pop(strategy, None)
    if real is not None:
        strategy.am, strategy.bg = real
