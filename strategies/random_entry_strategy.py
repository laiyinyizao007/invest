"""随机入场对照策略:出场逻辑、风险预算与 TrendAtrStrategy 完全相同,只有入场方向由随机数决定。

用途:检验均线信号相对"随机入场 + 同样出场"有没有真实贡献。
若趋势策略的绩效落在随机入场分布的中间,说明收益主要来自出场规则/行情漂移,而不是入场信号。
"""
import random

from strategies.trend_atr_strategy import TrendAtrStrategy


class RandomEntryStrategy(TrendAtrStrategy):
    """"""

    entry_prob = 0.02   # 空仓时每根 K 线产生入场信号的概率
    seed = 0

    parameters = TrendAtrStrategy.parameters + ["entry_prob", "seed"]

    def __init__(self, cta_engine, strategy_name, vt_symbol, setting) -> None:
        super().__init__(cta_engine, strategy_name, vt_symbol, setting)
        self.rng = random.Random(self.seed)

    def entry_signal(self) -> int:
        if self.rng.random() < self.entry_prob:
            return self.rng.choice((1, -1))
        return 0
