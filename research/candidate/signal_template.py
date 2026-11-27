"""候选可继承的基类(`SignalTemplate`)。

**这里没有风控**——这是本模块存在的全部理由:
`risk_budget`、`stop_distance`、`within_budget`、`DailyLossGuard`、`TrailingStop`、
`cta_engine`、`send_order`、`sync_data` 一律**不在**这里 ⇒ 候选进程里**物理上不存在**风控底线,
候选想改也改不到(比"检测到改写"强一个量级,与 ADR `20261111-risk-floor-immutability` 一致)。

候选进程**只产出一样东西**(协议见 `protocol.py`):
每根 K 线的**入场意图**(`entry_signal` 的取值:{+1, -1, 0})。

为什么是「入场意图」而不是 ADR 3.2 节写的「目标仓位序列」:
默认模板的出场是**移动止损**,而移动止损是风控、住在框架进程里 ⇒ 候选**不可能**知道
自己哪一根会被止损出场 ⇒ 「目标仓位序列」在默认策略上无法表达其语义。
入场意图恰好等价于旧路径(入场由信号决定、出场由风控决定),见 ADR 第 5.4 节之补。

**ATR 不外传**:风控用的 ATR 由框架自己用同一段行情重算一遍——
候选提供的 ATR 不得进入风控(否则候选就能通过篡改 ATR 放宽止损距离)。
"""
from __future__ import annotations

from .bars import Bar
from .indicators import ArrayManager


class SignalTemplate:
    """候选基类:维护指标窗口,产出入场意图与 ATR。

    候选通常只需要重写 `entry_signal()`(以及声明 `parameters` / `param_grid`)。
    """

    author = "invest"

    # 与框架侧模板同名的默认参数(注意:**不含任何风控参数**)
    fast_window = 10
    slow_window = 40
    atr_mult = 1.5
    atr_window = 14

    # 候选可优化的参数清单(框架侧按这份清单校验网格轴)
    parameters = [
        "fast_window", "slow_window", "atr_mult", "atr_window",
    ]

    # 周期口径:"daily" 或 "15m"(框架据此选对应的信号驱动模板)
    mode = "daily"

    def __init__(self, params: dict | None = None) -> None:
        # **先应用参数再建指标窗口**——顺序与框架侧模板一致
        # (框架侧:CtaTemplate.__init__ 里 update_setting 先跑,TrendAtrStrategy.__init__ 随后才建 ArrayManager)
        # 顺序反了,窗口大小就会用错 slow_window ⇒ ATR 数值不同 ⇒ 门槛数字漂。
        for name, value in (params or {}).items():
            if name in self.parameters:
                setattr(self, name, value)
        if self.fast_window >= self.slow_window:
            raise ValueError("fast_window 必须小于 slow_window")
        self.am = ArrayManager(self.slow_window + self.atr_window + 5)
        self.atr_value = 0.0
        self.signal = 0

    # ---------- 行情 ----------

    def on_bar(self, bar: Bar) -> None:
        """推进一根 K 线:更新窗口 → 算 ATR → 算入场意图。

        时序契约:**收盘后出信号,下一根开盘成交**(与框架侧模板同一条)。
        """
        self.am.update_bar(bar)
        if not self.am.inited:
            return
        self.atr_value = self.am.atr(self.atr_window)
        if self.atr_value <= 0:
            return
        self.signal = self.entry_signal()

    def entry_signal(self) -> int:
        """入场信号:1 做多 / -1 做空 / 0 无。子类可重写(如随机入场对照)。"""
        fast = self.am.sma(self.fast_window, array=True)
        slow = self.am.sma(self.slow_window, array=True)
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            return 1
        if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            return -1
        return 0

    # ---------- 跑一遍 ----------

    def run(self, bars) -> list[int]:
        """把整段 K 线跑完,返回**每根的入场意图**。

        只返回信号,**不返回任何绩效数字**:候选进程产出的数字一律不可信,
        框架只把它当作"要执行的交易意图"(ADR 第 3.2 节)。
        ATR 由候选自己在 `entry_signal` 里用(需要时),**不外传**——
        风控用的 ATR 由框架自己用行情重算一遍(候选提供的 ATR 不得进入风控)。
        """
        signals: list[int] = []
        for bar in bars:
            self.on_bar(bar)
            signals.append(int(self.signal))
        return signals
