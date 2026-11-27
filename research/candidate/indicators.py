"""候选侧的指标容器(`ArrayManager` 的等价实现)。

**为什么是副本而不是直接用 vnpy 的**:
候选进程里 `import vnpy.trader.utility` 虽然技术上可行(它不 import `bootstrap`、不读 `.env`),
但那会让候选进程**依赖整套交易栈的安装形态**(容器镜像、版本),而且 vnpy 的模块链随时可能
拉进 `vnpy.trader.object` / `locale` 之类的东西 ⇒ 候选侧的依赖面必须**自己封死**:
只允许 numpy + ta-lib。

**数值口径必须与框架侧一致**:这是硬要求,不是风格问题。
候选进程算出来的 `atr` 与框架进程算出来的 `atr` 只要有一根 K 线不同,
门槛数字就会漂(而且漂多少不可预测)。所以这里的实现**逐行照抄** vnpy `ArrayManager` 的算法
(`talib.SMA(self.close, n)` / `talib.ATR(self.high, self.low, self.close, n)`),
并由一条测试把两边**逐根数值比对**钉住(`tests/test_candidate_process_isolation.py`)。

刻意**只抄候选用得到的那几个方法**:`sma` / `ema` / `std` / `rsi` / `atr` / `donchian`,
外加 `open` / `high` / `low` / `close` / `volume` / `turnover` / `open_interest` 七个数组属性。
候选若要用别的指标,在这里加,并且**同时**在框架侧验证等价性(测试逐根比对两边数值)。
"""
from __future__ import annotations

import numpy as np
import talib


class ArrayManager:
    """时间序列容器 + 指标计算(与 vnpy `ArrayManager` 同口径)。"""

    def __init__(self, size: int = 100) -> None:
        self.count: int = 0
        self.size: int = size
        self.inited: bool = False

        self.open_array: np.ndarray = np.zeros(size)
        self.high_array: np.ndarray = np.zeros(size)
        self.low_array: np.ndarray = np.zeros(size)
        self.close_array: np.ndarray = np.zeros(size)
        self.volume_array: np.ndarray = np.zeros(size)
        self.turnover_array: np.ndarray = np.zeros(size)
        self.open_interest_array: np.ndarray = np.zeros(size)

    def update_bar(self, bar) -> None:
        """把一根 K 线推进窗口(字段名与 vnpy 的 `BarData` 对齐)。"""
        self.count += 1
        if not self.inited and self.count >= self.size:
            self.inited = True

        self.open_array[:-1] = self.open_array[1:]
        self.high_array[:-1] = self.high_array[1:]
        self.low_array[:-1] = self.low_array[1:]
        self.close_array[:-1] = self.close_array[1:]
        self.volume_array[:-1] = self.volume_array[1:]
        self.turnover_array[:-1] = self.turnover_array[1:]
        self.open_interest_array[:-1] = self.open_interest_array[1:]

        self.open_array[-1] = bar.open_price
        self.high_array[-1] = bar.high_price
        self.low_array[-1] = bar.low_price
        self.close_array[-1] = bar.close_price
        self.volume_array[-1] = bar.volume
        self.turnover_array[-1] = bar.turnover
        self.open_interest_array[-1] = bar.open_interest

    @property
    def open(self) -> np.ndarray:
        return self.open_array

    @property
    def high(self) -> np.ndarray:
        return self.high_array

    @property
    def low(self) -> np.ndarray:
        return self.low_array

    @property
    def close(self) -> np.ndarray:
        return self.close_array

    @property
    def volume(self) -> np.ndarray:
        return self.volume_array

    @property
    def turnover(self) -> np.ndarray:
        return self.turnover_array

    @property
    def open_interest(self) -> np.ndarray:
        return self.open_interest_array

    def sma(self, n: int, array: bool = False):
        """简单移动平均(与 vnpy 同口径:`talib.SMA(self.close, n)`)。"""
        result_array: np.ndarray = talib.SMA(self.close, n)
        if array:
            return result_array
        return float(result_array[-1])

    def ema(self, n: int, array: bool = False):
        """指数移动平均(与 vnpy 同口径:`talib.EMA(self.close, n)`)。"""
        result_array: np.ndarray = talib.EMA(self.close, n)
        if array:
            return result_array
        return float(result_array[-1])

    def std(self, n: int, nbdev: int = 1, array: bool = False):
        """标准差(与 vnpy 同口径:`talib.STDDEV(self.close, n, nbdev)`)。"""
        result_array: np.ndarray = talib.STDDEV(self.close, n, nbdev)
        if array:
            return result_array
        return float(result_array[-1])

    def rsi(self, n: int, array: bool = False):
        """相对强弱指标(与 vnpy 同口径:`talib.RSI(self.close, n)`)。"""
        result_array: np.ndarray = talib.RSI(self.close, n)
        if array:
            return result_array
        return float(result_array[-1])

    def atr(self, n: int, array: bool = False):
        """平均真实波幅(与 vnpy 同口径:`talib.ATR(self.high, self.low, self.close, n)`)。"""
        result_array: np.ndarray = talib.ATR(self.high, self.low, self.close, n)
        if array:
            return result_array
        return float(result_array[-1])

    def donchian(self, n: int, array: bool = False):
        """唐奇安通道(与 vnpy 同口径:`talib.MAX(self.high, n)` / `talib.MIN(self.low, n)`)。"""
        up: np.ndarray = talib.MAX(self.high, n)
        down: np.ndarray = talib.MIN(self.low, n)
        if array:
            return up, down
        return up[-1], down[-1]
