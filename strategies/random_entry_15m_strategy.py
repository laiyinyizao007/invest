"""15 分钟随机入场对照:出场逻辑与 TrendAtr15mStrategy 完全相同,只有入场方向由随机数决定。

用于晋级门槛第 5 条(相对随机入场的分位数)。入场概率由评估编排按候选的活跃度设定,
使随机对照与候选的交易频率大致相当,比较的是「入场信号的质量」而不是「谁交易得多」。
仅研究回测使用,不支持实盘。
"""
import random

from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class RandomEntry15mStrategy(TrendAtr15mStrategy):
    """"""

    entry_prob = 0.002   # 空仓时每根 K 线产生入场信号的概率;评估时由编排覆盖
    seed = 0

    parameters = TrendAtr15mStrategy.parameters + ["entry_prob", "seed"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.rng = random.Random(self.seed)

    def entry_signal(self) -> int:
        if self.rng.random() < self.entry_prob:
            return self.rng.choice((1, -1))
        return 0
