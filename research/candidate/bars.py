"""候选侧的 K 线载体。

**不是 vn.py 的 `BarData`**:候选进程不 import vnpy(依赖面必须自己封死),
但字段名与语义必须与 vnpy 的 `BarData` 对齐 ⇒ 框架侧喂数据时做一次转换,
并由一条测试逐字段比对(`tests/test_candidate_process_isolation.py`)。

候选拿到的 K 线**只有值**:没有文件路径、没有文件句柄、没有数据库连接。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Bar:
    """一根 K 线(字段名对齐 vnpy `BarData` 的价格/成交字段)。"""

    open_price: float = 0.0
    high_price: float = 0.0
    low_price: float = 0.0
    close_price: float = 0.0
    volume: float = 0.0
    turnover: float = 0.0
    open_interest: float = 0.0
    datetime: datetime | None = None
    interval: str = ""
    extra: dict = field(default_factory=dict)
