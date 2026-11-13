"""SimNow 前置机地址白名单:采集脚本与演练脚本共用同一套「只在模拟盘上跑」的约束。

**这个模块现在是 `ctp_tools/simnow` 的转发层,不再自带地址表。**

为什么要有白名单:`--confirm-simnow` 只是一个布尔开关,挡不住「配置写错」——
CTP 的「柜台环境=实盘」是 SimNow 与期货公司生产 API 共用的,没法靠环境变量区分两者,
**只能靠前置机地址**。期货公司生产 API 的地址一定不在表里。

为什么改成转发:地址表原来在本文件和 `ctp_tools/simnow.py` 里各存一份,
迟早漂移 —— 历史上就漂移过一次(`broker_targets` 与 `ctp_tools/simnow` 的
SimNow 交易地址不一致,同一台机器一会儿按新表放行、一会儿按旧表拒绝)。
2026-11-13 又发现本文件的副本少了第二套的行情前置 40011,两张表再次不一致。
白名单是「能不能连上那台机器」的唯一判据,必须只有一份。
"""
from __future__ import annotations

import sys
from pathlib import Path

# scripts/ 目录里的模块按同目录导入;项目根也要在 path 上(ctp_tools 是包)。
for _p in (str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ctp_tools.simnow import (  # noqa: E402,F401  (转发:保持原有导入路径可用)
    SIMNOW_MD_ADDRESSES,
    SIMNOW_TD_ADDRESSES,
    check_simnow_address,
    is_simnow_address,
    normalize_address,
)

__all__ = [
    "SIMNOW_MD_ADDRESSES",
    "SIMNOW_TD_ADDRESSES",
    "check_simnow_address",
    "is_simnow_address",
    "normalize_address",
]
