"""国信期货测试环境与 SimNow 预演环境的「允许连的前置地址表」。

单独放一个模块,是为了让**同一个白名单**同时被这几处使用:
- scripts/guoxin_oneclick.py(一键流程的档案校验);
- scripts/broker_test.py(运行期校验,BROKER_TEST_ALLOWED_TD/MD);
- scripts/broker_report.py(生成正式报告名前核对证据里的连接信息)。

只要这些地方各写一份,迟早会漂移;漂移的后果是「校验通过但连到别处」。

地址来源:
- 国信:客户提供的《CTP仿真互联网.txt》(brokerid 8030,电信/联通两套前置);
- SimNow:官方公布的前置机地址(SIMNOW_TD_ADDRESSES 见 ctp_tools/simnow.py)。
"""
from __future__ import annotations

# 国信期货看穿式测试环境
GUOXIN_TEST_BROKER_ID = "8030"
GUOXIN_TEST_TD = frozenset({"121.13.218.19:41285", "120.86.114.57:41285"})
GUOXIN_TEST_MD = frozenset({"121.13.218.19:41293", "120.86.114.57:41293"})

# SimNow 预演环境(模拟资金,不是国信环境)
SIMNOW_BROKER_ID = "9999"
SIMNOW_TD = frozenset({"182.254.243.31:30001"})
SIMNOW_MD = frozenset({"182.254.243.31:30011"})
