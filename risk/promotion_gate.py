"""策略晋级门槛:候选策略要进入 SimNow 长期挂机(更别说实盘)前必须全部通过。

纯 Python,不依赖 vn.py。门槛来源与理由见 docs/adr/20261109-promotion-gate.md。
注意:通过门槛只代表"有资格进入下一阶段接受检验",不代表策略有效。
"""
import math
from dataclasses import dataclass


def sharpe_standard_error(sharpe: float, years: float) -> float:
    """年化夏普比率的标准误(Lo 2002,假设收益独立同分布):sqrt((1 + SR²/2) / 年数)。

    忽略了收益的自相关与厚尾,实际误差通常更大;样本年数越短误差越大。
    """
    if years <= 0:
        raise ValueError("years 必须为正")
    return math.sqrt((1 + 0.5 * sharpe**2) / years)


def sharpe_ci(sharpe: float, years: float, z: float = 1.96) -> tuple[float, float]:
    """夏普比率的近似置信区间(默认 95%)。"""
    se = sharpe_standard_error(sharpe, years)
    return sharpe - z * se, sharpe + z * se


@dataclass(frozen=True)
class GateThresholds:
    min_oos_sharpe: float = 1.5          # 样本外夏普点估计下限
    max_suspicious_sharpe: float = 3.0   # 高于此值先怀疑代码有前视偏差/过拟合,而非通过
    max_drawdown_pct: float = 20.0       # 最大回撤(%)上限
    max_drawdown_yuan: float = 2000.0    # 最大回撤绝对金额(元)上限,1 万元账户
    min_round_trips: int = 150           # 样本外往返笔数下限(统计意义)
    min_sharpe_ci_lower: float = 0.0     # 夏普 95% 置信区间下界必须大于此值
    min_random_percentile: float = 95.0  # 相对随机入场对照的分位数(%)
    min_stress_sharpe: float = 1.0       # 成本(滑点+手续费)翻倍后夏普下限
    min_dsr: float = 0.95                # Deflated Sharpe Ratio 下限;ADR 20261113 锁定,不得为让候选通过而放宽


@dataclass(frozen=True)
class CandidateMetrics:
    oos_sharpe: float
    oos_years: float
    max_drawdown_pct: float          # 取绝对值比较,传入负数也可以
    max_drawdown_yuan: float
    round_trips: int
    random_entry_percentile: float   # 候选夏普超过随机入场分布的百分比(0~100)
    stress_sharpe: float             # 成本翻倍后的样本外夏普
    data_adjusted: bool              # 回测使用的是复权后的连续合约
    parameter_on_plateau: bool       # 最优参数位于参数高原(邻域平均得分同样为正)
    dsr: float | None = None         # Deflated Sharpe Ratio(0~1);None = 未计算,按未通过处理(fail-closed)
    n_trials: int = 0                # 计算 DSR 所用的累计试验次数;0 = 未记账,按未通过处理


def check_gate(m: CandidateMetrics, t: GateThresholds = GateThresholds()) -> list[str]:
    """返回未通过的条目(中文说明);空列表表示全部通过。"""
    fails: list[str] = []

    if m.oos_sharpe > t.max_suspicious_sharpe:
        fails.append(f"样本外夏普 {m.oos_sharpe:.2f} 高于 {t.max_suspicious_sharpe}:先排查前视偏差/过拟合,不予通过")
    elif m.oos_sharpe < t.min_oos_sharpe:
        fails.append(f"样本外夏普 {m.oos_sharpe:.2f} < {t.min_oos_sharpe}")

    if m.oos_years > 0:
        low, high = sharpe_ci(m.oos_sharpe, m.oos_years)
        if low <= t.min_sharpe_ci_lower:
            fails.append(f"夏普 95% 置信区间 [{low:.2f}, {high:.2f}] 下界未超过 {t.min_sharpe_ci_lower}:不能排除没有优势")
    else:
        fails.append("样本外年数必须为正")

    dd_pct = abs(m.max_drawdown_pct)
    if dd_pct >= t.max_drawdown_pct:
        fails.append(f"最大回撤 {dd_pct:.1f}% ≥ {t.max_drawdown_pct}%")
    dd_yuan = abs(m.max_drawdown_yuan)
    if dd_yuan > t.max_drawdown_yuan:
        fails.append(f"最大回撤 {dd_yuan:.0f} 元 > {t.max_drawdown_yuan:.0f} 元")

    if m.round_trips < t.min_round_trips:
        fails.append(f"样本外往返 {m.round_trips} 笔 < {t.min_round_trips}:统计意义不足")

    if m.random_entry_percentile < t.min_random_percentile:
        fails.append(f"仅超过随机入场对照的 {m.random_entry_percentile:.0f}% < {t.min_random_percentile:.0f}%:入场信号无明显贡献")

    if m.stress_sharpe < t.min_stress_sharpe:
        fails.append(f"成本翻倍后夏普 {m.stress_sharpe:.2f} < {t.min_stress_sharpe}:对成本过于敏感")

    if not m.data_adjusted:
        fails.append("回测数据未复权:主力连续换月跳空会污染 ATR/均线/止损")

    if not m.parameter_on_plateau:
        fails.append("最优参数不在参数高原:邻域表现不稳定,疑似过拟合")

    if m.dsr is None:
        fails.append("DSR 未计算:缺少去通胀检验,不予通过")
    else:
        # 审计字段严格校验:NaN 比较恒为 False、bool 是 int 的子类,宽松比较会把它们放过
        if isinstance(m.n_trials, bool) or not isinstance(m.n_trials, int) or m.n_trials < 1:
            fails.append("DSR 缺少有效的试验次数:无法核对多重检验的去通胀是否到位,不予通过")
        if isinstance(m.dsr, bool) or not isinstance(m.dsr, (int, float)) or not math.isfinite(m.dsr) or not 0.0 <= m.dsr <= 1.0:
            fails.append(f"DSR 取值非法({m.dsr!r}):必须是 [0, 1] 内的有限数,不予通过")
        elif m.dsr < t.min_dsr:
            fails.append(f"DSR {m.dsr:.3f} < {t.min_dsr}(累计试验 {m.n_trials} 次):扣除多重检验后不能排除是运气")

    return fails
