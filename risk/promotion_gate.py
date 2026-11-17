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

# 「可疑夏普」上限的兜底下限(常量)。字段 `suspicious_sharpe_floor` 若被传成
# 非法值(NaN/inf/≤0/非数),一律退回这个常量 —— 见 `_as_finite_float`。
DEFAULT_SUSPICIOUS_SHARPE_FLOOR = 3.0

# 样本外年数的**物理上限**(不是统计阈值):现有 15 分钟数据自 2010 年前后开始,
# 50 年是任何现实数据集都够不到的数。它的作用是**排除不可能的输入**
# (例如手改的指标文件里写 oos_years=1e308 ⇒ 置信区间检查被架空成恒真),
# 只会让门槛**更严**,不会更松 ⇒ 不违反「阈值须在看到候选之前有依据」。
MAX_OOS_YEARS = 50.0


def _as_finite_float(x: object) -> float | None:
    """把输入变成**有限浮点数**;不合法一律返回 None(fail-closed)。

    为什么这么严:NaN/inf 与任何数比较**恒为 False** ⇒ 门槛里那些
    `if m.oos_sharpe > cap` / `if m.oos_years > 0` 会被**静默放过**
    (实测:oos_sharpe=NaN、oos_years=inf 时 check_gate 返回空列表 = 全部通过)。
    另:超大 int(如 10**400)在 float() / math.isfinite() 上会抛 OverflowError
    ⇒ 一并按「不合法」处理,不许让异常从门槛里冒出去。
    """
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    try:
        v = float(x)
    except (OverflowError, ValueError):
        return None
    return v if math.isfinite(v) else None


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
    suspicious_sharpe_floor: float = DEFAULT_SUSPICIOUS_SHARPE_FLOOR  # 「可疑夏普」上限的**兜底下限**:实际上限 = max(本值, DSR 认证线年化值)
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

    **口径**:认证线按**正态收益**假设(偏度 0、峰度 3)反解。这个假设**只在
    一侧保守**,不能说成「全局偏保守」(第 55 轮之十七,codex 复核后的更正):
    - 厚尾(峰度 > 3)⇒ DSR 分母变大 ⇒ 实测 DSR 更低 ⇒ 实际认证线**更高**
      ⇒ 这一侧正态假设**偏保守(更严)**;
    - **正偏度(偏度 > 0)会让 DSR 分母变小**(`denom = 1 − 偏度·sr + (峰度−1)·sr²/4`
      可以 < 1)⇒ 实测 DSR **更高** ⇒ 能过 DSR 的夏普**更低**
      ⇒ 实际认证线**更低** ⇒ 放行区间在**下沿**方向比正态模型更宽。
    ⇒ 真正的下界永远是 **DSR ≥ 0.95 那一关**,而它用的是**样本自己的**偏度/峰度
      (`risk/dsr.py::deflated_sharpe`),不是这个正态模型。**DSR 不是前视检测器**。

    **诚实的量(别把这条改动的效果写得比实测更好)**:「放宽」打开的区间 =
    (旧上限 3.0, 新上限] ⇒ 实测 (N=1000, T=600) 宽约 **0.1121**(3.0 → 3.1121)。
    能不能真落进去,取决于**候选自己的矩**:矩接近正态时 DSR 在 3.1013 附近就顶到
    0.95(此时实际可落区间只有 3.1013–3.1121,宽约 0.011);**右偏**的候选能把 DSR
    顶得更高 ⇒ 实测有一个年化夏普 **3.0582**、实测 DSR **0.99999997** 的右偏序列
    **通过了全部 check_gate**(旧规则会毙掉它)⇒ **0.011 那个窄数字不是全局结论**。
    这条改动的主要价值是**口径统一**(上限不再是与 N 无关的魔法数字,诊断层随之
    N 化 ⇒ 同一个问题只有一个答案),而不是让更多候选通过。详见
    `docs/adr/20261117-dsr-consistent-suspicious-sharpe.md`。
    """
    floor = _as_finite_float(t.suspicious_sharpe_floor)
    if floor is None or floor <= 0:
        # 兜底下限被传成非法值(NaN/inf/≤0/非数)⇒ 退回常量。
        # 不退回的话:NaN 会让 max(floor, ...) = NaN,而 NaN > cap 恒为 False
        # ⇒ 「夏普高得可疑」这一关**永远不触发** ⇒ 防线静默失效(fail-open)。
        floor = DEFAULT_SUSPICIOUS_SHARPE_FLOOR
    if (isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1
            or isinstance(n_obs, bool) or not isinstance(n_obs, int)):
        return floor
    daily = required_sharpe_for_dsr(n_trials, n_obs, _normalized_min_dsr(t))
    if daily <= 0.0:
        return floor
    annualized = daily * math.sqrt(TRADING_DAYS_PER_YEAR)
    if not math.isfinite(annualized):
        return floor
    return max(floor, annualized)


def cap_for_metrics(m: CandidateMetrics, t: GateThresholds = GateThresholds()) -> float:
    """候选自己的「可疑夏普」上限(年化)。样本外长度按 `oos_years` 折回日收益个数。

    退化处理:年数不是有限正数、或折算出来的日收益个数不是有限数
    (例如 `oos_years = 1e308` ⇒ 折算溢出成 inf)⇒ **一律按「样本太短」处理**
    (n_obs = 0 ⇒ 落回兜底下限)。**不许抛异常**:门槛里抛异常 = 整个评估流程炸掉,
    而不是 fail-closed。
    """
    n_obs = 0
    years = _as_finite_float(m.oos_years)
    if years is not None and 0 < years <= MAX_OOS_YEARS:
        scaled = years * TRADING_DAYS_PER_YEAR
        if math.isfinite(scaled):
            n_obs = int(round(scaled))
    return suspicious_sharpe_cap(m.n_trials, n_obs, t)


def _as_bool(x: object) -> bool | None:
    """布尔字段的合法取值:真正的 bool,或 0/1(人手写的指标 JSON 里常见)。别的**不认**。

    不认的原因是「宽松的真值判断」:字符串 "False" 在 Python 里是**真** ⇒
    `if not m.data_adjusted` 会把 "False" 放过去(该拦的没拦)。
    """
    if isinstance(x, bool):
        return x
    if isinstance(x, int) and x in (0, 1):
        return bool(x)
    return None


def _safe_threshold(value: object, default: float) -> float:
    """阈值本身也可能是非有限值(NaN/inf)⇒ 退回**该字段的默认值**。

    为什么阈值也要查:`夏普 < NaN` 恒为 False ⇒ 下限那条**永远不触发** ⇒
    「夏普太低也不通过」被静默关掉 ⇒ 与「没有下限」同义。
    """
    v = _as_finite_float(value)
    return default if v is None else v


def _normalized_min_dsr(t: GateThresholds) -> float:
    """DSR 阈值归一化:合法域是 **开区间 (0, 1)**——DSR 本身是 [0,1] 的概率。

    非法(`≤0` / `≥1` / NaN / inf / 超大 int / 非数)⇒ 退回**默认 0.95**,
    而不是「取边界值」。为什么这条必须查(codex 第 2 轮 High,本地已复现):
    `min_dsr = 0.0` 时 `dsr < 0.0` 恒为 False ⇒ **DSR 那一关被整体放空**
    ⇒ 一条 `dsr = 0.0` 的候选也能全部通过 ⇒ 防线静默失效(fail-open)。
    `min_dsr = -1.0` 同理;`min_dsr = 0.5` 仍在合法域内 ⇒ 照旧生效
    (阈值可调是 `GateThresholds` 的用途,但**调整须在看到候选之前确定并记录**,
    见 `20261109-promotion-gate.md` 的影响一节)。

    **同一个问题只能有一个答案**:门槛判定与认证线反解都用**这一份**归一化值,
    否则上限按一个阈值算、DSR 按另一个阈值判 ⇒ 两处口径分叉。
    """
    v = _as_finite_float(t.min_dsr)
    if v is None or not (0.0 < v < 1.0):
        return GateThresholds().min_dsr
    return v


def check_gate(m: CandidateMetrics, t: GateThresholds = GateThresholds()) -> list[str]:
    """返回未通过的条目(中文说明);空列表表示全部通过。

    **所有输入字段都先做合法性校验**(fail-closed):非法值**一律不予通过**。
    为什么必须逐个查:NaN/inf 与任何数比较**恒为 False** ⇒ 「夏普 > 上限」
    「年数 > 0」「置信区间下界 ≤ 0」「回撤 ≥ 上限」这类比较会把 NaN **静默放过**
    (实测:oos_sharpe=NaN 时 check_gate 返回空列表 = 全部通过)。
    而指标 JSON 可以带 NaN/Infinity(Python 的 json 接受这两个字面量)⇒ 不是假想输入。

    **阈值本身**也做同样的校验(非法阈值退回该字段的默认值)⇒ 不会因为阈值是 NaN
    而把「这一关」整个关掉。
    """
    fails: list[str] = []

    defaults = GateThresholds()
    min_oos_sharpe = _safe_threshold(t.min_oos_sharpe, defaults.min_oos_sharpe)
    max_dd_pct = _safe_threshold(t.max_drawdown_pct, defaults.max_drawdown_pct)
    max_dd_yuan = _safe_threshold(t.max_drawdown_yuan, defaults.max_drawdown_yuan)
    min_trips = int(_safe_threshold(t.min_round_trips, defaults.min_round_trips))
    min_percentile = _safe_threshold(t.min_random_percentile, defaults.min_random_percentile)
    min_stress = _safe_threshold(t.min_stress_sharpe, defaults.min_stress_sharpe)
    min_ci_lower = _safe_threshold(t.min_sharpe_ci_lower, defaults.min_sharpe_ci_lower)
    min_dsr = _normalized_min_dsr(t)

    sharpe = _as_finite_float(m.oos_sharpe)
    years = _as_finite_float(m.oos_years)
    dd_pct_raw = _as_finite_float(m.max_drawdown_pct)
    dd_yuan_raw = _as_finite_float(m.max_drawdown_yuan)
    trips = _as_finite_float(m.round_trips)
    percentile = _as_finite_float(m.random_entry_percentile)
    stress = _as_finite_float(m.stress_sharpe)
    adjusted = _as_bool(m.data_adjusted)
    on_plateau = _as_bool(m.parameter_on_plateau)

    for label, value in (("样本外夏普", m.oos_sharpe),
                         ("样本外年数", m.oos_years),
                         ("最大回撤(%)", m.max_drawdown_pct),
                         ("最大回撤(元)", m.max_drawdown_yuan),
                         ("样本外往返笔数", m.round_trips),
                         ("随机入场分位", m.random_entry_percentile),
                         ("成本翻倍后夏普", m.stress_sharpe)):
        if _as_finite_float(value) is None:
            fails.append(f"{label}非法({value!r}):必须是有限数,不予通过")
    if adjusted is None:
        fails.append(f"回测数据已复权非法({m.data_adjusted!r}):必须是布尔值,不予通过")
    if on_plateau is None:
        fails.append(f"最优参数在参数高原非法({m.parameter_on_plateau!r}):必须是布尔值,不予通过")

    cap = cap_for_metrics(m, t)
    if sharpe is not None:
        if sharpe > cap:
            fails.append(f"样本外夏普 {sharpe:.2f} 高于 {cap:.2f}:先排查前视偏差/过拟合,不予通过")
        elif sharpe < min_oos_sharpe:
            fails.append(f"样本外夏普 {sharpe:.2f} < {min_oos_sharpe}")

    if years is not None:
        if years > MAX_OOS_YEARS:
            fails.append(f"样本外年数不合理({years:.6g} 年):超过 {MAX_OOS_YEARS:g} 年的物理上限,不予通过")
        elif years <= 0:
            fails.append("样本外年数必须为正")

    if years is not None and 0 < years <= MAX_OOS_YEARS:
        low, high = sharpe_ci(sharpe if sharpe is not None else 0.0, years)
        if low <= min_ci_lower:
            fails.append(f"夏普 95% 置信区间 [{low:.2f}, {high:.2f}] 下界未超过 {min_ci_lower}:不能排除没有优势")

    if dd_pct_raw is not None:
        dd_pct = abs(dd_pct_raw)
        if dd_pct >= max_dd_pct:
            fails.append(f"最大回撤 {dd_pct:.1f}% ≥ {max_dd_pct}%")
    if dd_yuan_raw is not None:
        dd_yuan = abs(dd_yuan_raw)
        if dd_yuan > max_dd_yuan:
            fails.append(f"最大回撤 {dd_yuan:.0f} 元 > {max_dd_yuan:.0f} 元")

    if trips is not None:
        # 业务合法域(不只是有限性):往返笔数是**计数**⇒ 必须是非负整数。
        # `200.5` 笔不是一笔数;`-5` 笔不是发生过的事。
        if trips < 0 or not float(trips).is_integer():
            fails.append(f"样本外往返笔数非法({m.round_trips!r}):必须是非负整数")
        elif trips < min_trips:
            fails.append(f"样本外往返 {m.round_trips} 笔 < {min_trips}:统计意义不足")

    if percentile is not None:
        # 百分位的合法域是 [0, 100];`150%` 不可能发生 ⇒ 不许当成「超过随机对照」。
        if not 0.0 <= percentile <= 100.0:
            fails.append(f"随机入场分位非法({m.random_entry_percentile!r}):必须是 0~100 的百分数")
        elif percentile < min_percentile:
            fails.append(f"仅超过随机入场对照的 {percentile:.0f}% < {min_percentile:.0f}%:入场信号无明显贡献")

    if stress is not None and stress < min_stress:
        fails.append(f"成本翻倍后夏普 {stress:.2f} < {min_stress}:对成本过于敏感")

    if adjusted is False:
        fails.append("回测数据未复权:主力连续换月跳空会污染 ATR/均线/止损")

    if on_plateau is False:
        fails.append("最优参数不在参数高原:邻域表现不稳定,疑似过拟合")

    if m.dsr is None:
        fails.append("DSR 未计算:缺少去通胀检验,不予通过")
    else:
        # 审计字段严格校验:NaN 比较恒为 False、bool 是 int 的子类,宽松比较会把它们放过
        if isinstance(m.n_trials, bool) or not isinstance(m.n_trials, int) or m.n_trials < 1:
            fails.append("DSR 缺少有效的试验次数:无法核对多重检验的去通胀是否到位,不予通过")
        if isinstance(m.dsr, bool) or not isinstance(m.dsr, (int, float)) or not math.isfinite(m.dsr) or not 0.0 <= m.dsr <= 1.0:
            fails.append(f"DSR 取值非法({m.dsr!r}):必须是 [0, 1] 内的有限数,不予通过")
        elif m.dsr < min_dsr:
            fails.append(f"DSR {m.dsr:.3f} < {min_dsr}(累计试验 {m.n_trials} 次):扣除多重检验后不能排除是运气")

    return fails
