"""策略晋级门槛:候选策略要进入 SimNow 长期挂机(更别说实盘)前必须全部通过。

纯 Python,不依赖 vn.py。门槛来源与理由见 docs/adr/20261109-promotion-gate.md。
注意:通过门槛只代表"有资格进入下一阶段接受检验",不代表策略有效。
"""
import math
from dataclasses import dataclass

from risk.dsr import required_sharpe_for_dsr

# 年化口径:日夏普 → 年化夏普的换算因子。与 risk/candidate_eval.py **同一份**
# (candidate_eval 从这里 import,不许各写各的 ⇒ 同一个问题两个答案 ⇒ 迟早只改一边)。
TRADING_DAYS_PER_YEAR = 240


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
    suspicious_sharpe_floor: float = 3.0  # 「可疑夏普」上限的**兜底下限**:实际上限 = max(本值, DSR 认证线年化值)
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


def suspicious_sharpe_cap(n_trials: int, n_obs: int,
                          t: GateThresholds = GateThresholds()) -> float:
    """「可疑夏普」上限(年化):**与 DSR 自洽**,且**只放宽、不收紧**。

    **为什么不再是一个常数**(第 55 轮之十六,用户裁决;ADR 见
    `docs/adr/20261117-dsr-consistent-suspicious-sharpe.md`):
    旧上限是写死的 3.0,**与试验次数无关**。可 DSR 是**去通胀**判据:试验次数
    N 越多,「纯靠运气能挑到的最高夏普」越高 ⇒ 同一个年化夏普在 N 大时**通过**
    DSR、在 N 小时**不通过**。于是两条规则**互相打架**:N 大时,一个 DSR 认证过
    的候选会被那条**与 N 无关**的固定上限**先一步**毙掉,报出来的是
    「先排查前视偏差」——而 DSR 那一关**根本没走**。

    **改法**:上限由 **DSR 的认证线**反解出来(`risk/dsr.required_sharpe_for_dsr`:
    DSR 恰好等于 `min_dsr` 的那个日夏普),再年化 ⇒ **两条规则同一个统计模型**。
    兜底仍留 3.0(`suspicious_sharpe_floor`)⇒ 任何 (N, T) 下限值 **≥ 3.0**
    ⇒ **只放宽不收紧**:旧规则放行的候选,新规则**一个都不会被毙掉**。

    **口径**:认证线按**正态收益**假设(偏度 0、峰度 3)反解。真实收益若更厚尾
    (峰度 > 3),实际 DSR 更低 ⇒ 认证线只会**更高** ⇒ 正态假设给出的是**偏保守**
    (更严)的一侧 ⇒ 安全。

    **诚实的量(别把这条改动的效果写得比实测更好)**:「放宽」打开的窗口 =
    {夏普 ≤ 上限} ∩ {实测 DSR ≥ 阈值};而 DSR 阈值本身要求夏普**不低于同一条**
    认证线 ⇒ 窗口很窄(实测 (N=1000, T=600):上沿 3.1121、下沿 3.1013,宽约 0.011)。
    这条改动的主要价值是**口径统一**(上限不再是与 N 无关的魔法数字,诊断层随之
    N 化 ⇒ 同一个问题只有一个答案),而不是让更多候选通过。详见
    `docs/adr/20261117-dsr-consistent-suspicious-sharpe.md`。
    """
    floor = t.suspicious_sharpe_floor
    if (isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1
            or isinstance(n_obs, bool) or not isinstance(n_obs, int)):
        return floor
    daily = required_sharpe_for_dsr(n_trials, n_obs, t.min_dsr)
    if daily <= 0.0:
        return floor
    annualized = daily * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not math.isfinite(annualized):
        return floor
    return max(floor, annualized)


def cap_for_metrics(m: CandidateMetrics, t: GateThresholds = GateThresholds()) -> float:
    """候选自己的「可疑夏普」上限(年化)。样本外长度按 `oos_years` 折回日收益个数。"""
    n_obs = 0
    if (isinstance(m.oos_years, (int, float)) and not isinstance(m.oos_years, bool)
            and math.isfinite(m.oos_years) and m.oos_years > 0):
        n_obs = int(round(m.oos_years * TRADING_DAYS_PER_YEAR))
    return suspicious_sharpe_cap(m.n_trials, n_obs, t)


def check_gate(m: CandidateMetrics, t: GateThresholds = GateThresholds()) -> list[str]:
    """返回未通过的条目(中文说明);空列表表示全部通过。"""
    fails: list[str] = []

    cap = cap_for_metrics(m, t)
    if m.oos_sharpe > cap:
        fails.append(f"样本外夏普 {m.oos_sharpe:.2f} 高于 {cap:.2f}:先排查前视偏差/过拟合,不予通过")
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
