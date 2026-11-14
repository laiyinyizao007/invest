"""样本外窗口常量(纯数据,**不依赖交易栈**)。

为什么单独成文件(第 7 轮 codex 的 C1/H1):
研究沙盒只需要「哪段时间是哪一段」这一件事,而 optimize.py 为了跑回测要
`import bootstrap` → vnpy。窗口常量留在 optimize.py 里,研究侧查一次窗口就得把
整个交易栈拖进来——CI 的研究侧闸门(刻意不装交易环境)连**收集**都过不去,
整轮闸门一次都没跑过。

常量仍然只有一份:optimize.py 从这里导入,行为不变;研究侧也只从这里取。
新增 15 分钟那套窗口时,请一并放到这里(或另起同类的纯数据模块)。
"""
from __future__ import annotations

from datetime import datetime

WINDOWS = {
    "train": (datetime(2005, 1, 1), datetime(2018, 12, 31)),
    "valid": (datetime(2019, 1, 1), datetime(2022, 12, 31)),
    "test": (datetime(2023, 1, 1), datetime(2026, 11, 9)),
}
# 复权连续合约(CADJ/MAADJ)只有 2018-05 之后的数据,不能使用上面的切分。以下切分在看到任何复权数据上的结果之前就已确定
# (见 docs/adr/20261109-negative-backtest-result.md),测试段此前从未被查看。
WINDOWS_ADJ = {
    "train": (datetime(2018, 5, 1), datetime(2021, 12, 31)),
    "valid": (datetime(2022, 1, 1), datetime(2023, 12, 31)),
    "test": (datetime(2024, 1, 1), datetime(2026, 11, 9)),
}


def windows_for(key: str) -> dict:
    return WINDOWS_ADJ if key.endswith("ADJ") else WINDOWS


def segment_key(key: str) -> str:
    start, end = windows_for(key)["test"]
    return f"optimize:{key}:{start:%Y-%m-%d}~{end:%Y-%m-%d}"
