"""新格式候选示例(候选独立进程路径,Issue #29 S1):`pending_strategies/atr_breakout_15m_2atr.py` 的等价移植。

与旧格式的差别(都是候选独立进程化带来的,逻辑一字不改):
- 基类换成 `candidate.signal_template.SignalTemplate`(候选侧模板:只有指标窗口与入场意图,**没有风控**);
- `parameters` 必须是**字面量列表**(框架用 AST 读它,读不出就拒绝评估);
- `param_grid` 同样必须是字面量;
- 出场(ATR 移动止损)、1 手、单笔风险预算、当日连亏熔断都不在这里:它们由框架固定,候选改不了也看不见。
"""
from candidate.signal_template import SignalTemplate


class Candidate(SignalTemplate):
    fast_window = 8
    slow_window = 20
    atr_mult = 2.0
    breakout_n = 10

    parameters = ["fast_window", "slow_window", "atr_mult", "atr_window", "breakout_n"]

    param_grid = {
        "breakout_n": [6, 8, 10, 12, 16],
        "slow_window": [12, 16, 20, 24, 32],
        "atr_mult": [1.5, 1.75, 2.0, 2.25, 2.5],
    }

    def entry_signal(self) -> int:
        n = self.breakout_n
        if self.am.count < max(n + 3, self.slow_window + 1, self.fast_window + 1):
            return 0

        # donchian 返回 (up, down) 两个数组,且窗口包含当前 K 线:
        # 当前收盘价不可能高于含自身最高价的上轨,所以突破要对「上一根为止」的通道判定。
        upper, lower = self.am.donchian(n, array=True)
        atr_now = self.am.atr(self.fast_window)
        if atr_now <= 0:
            return 0

        fast_ma = self.am.sma(self.fast_window, array=True)[-1]
        slow_ma = self.am.sma(self.slow_window, array=True)[-1]
        close_now = self.am.close[-1]
        close_prev = self.am.close[-2]

        # upper[-2]/lower[-2]:截至上一根 K 线的 n 根通道(不含当前根);[-3] 是再前一根的通道
        if close_now > upper[-2] + 0.10 * atr_now and close_prev <= upper[-3] and fast_ma > slow_ma:
            return 1
        if close_now < lower[-2] - 0.10 * atr_now and close_prev >= lower[-3] and fast_ma < slow_ma:
            return -1
        return 0
