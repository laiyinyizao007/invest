"""门槛评估编排:滚动验证段上跑候选 / 成本翻倍 / 随机入场对照,汇总后调用晋级门槛。

回测执行器(runner)由调用方注入,便于用假执行器单测;真实执行器见 `engine_runner`。
决议见 docs/adr/20261112-candidate-evaluation.md:样本外 = 滚动验证段,测试段不可达,门槛数值不放宽。
随机对照限制:出场逻辑取自随机对照类(目前是 TrendAtr 家族的 ATR 移动止损),候选的出场参数(如 atr_mult)会同样传入;
候选若使用完全不同的出场机制,这条对照衡量的仍是「相同出场下入场信号的贡献」,解读时须知道这一点。
"""
from __future__ import annotations

import functools
import math
from datetime import datetime
from typing import Callable, Sequence

from research_grid import GridError, select_params, MIN_GRID_CELLS
from risk import candidate_eval as ce
from risk.heatmap import build_heatmap
from risk.dsr import MAX_TRIAL_SHARPE, run_variance
from risk.behavior import describe_behavior
from risk.diagnosis import diagnose
from risk.structured_diagnosis import structure

TEST_SEGMENT_START = datetime(2024, 1, 1)   # 与 walk_forward_optimizer.TEST_SEGMENT_START 保持一致(有测试锁定)
MIN_ENTRY_PROB = 1e-4
MAX_ENTRY_PROB = 0.5
STRESS_COST_MULTIPLIER = 2.0

Window = tuple[str, datetime, datetime]
Runner = Callable[[type, dict, list, float], dict]


# 阶段决定外层(进化回路)能不能看到报错原文——见 risk/outward.py:
# - `declaration`:声明期(网格声明、类校验)——我们自己写的消息,放开;
# - `selection`:选参窗口回放——进化回路放开的那一层,放开;
# - `validation`:验证窗口回放——**原文不出去**。候选的普通 bug(`assert pnl > 0, f"pnl {pnl}"`、
#   `raise ValueError(f"bad price {close}")`)就会把验证窗口的原始数字塞进消息,而梯子一次都不扣
#   (第 15 轮 codex 的 Critical,端到端复现:消息里带出了验证窗口首日的收盘价)。
# 没有阶段的错误按验证窗口处理(fail-closed:phase 不在 OPEN_PHASES 里,原文不放行)。
PHASE_DECLARATION = "declaration"
PHASE_SELECTION = "selection"
PHASE_VALIDATION = "validation"


def _error_result(phase: str, exc: BaseException, trials: dict | None = None) -> dict:
    """把异常变成一份**框架写的**错误结果:阶段由调用点(框架自己)决定,写进结果字典。

    这里**不碰异常对象**:以前是把阶段挂在异常的 `invest_phase` 属性上,而那个属性候选自己也能挂——
    候选只要 `e.invest_phase = "selection"`,框架"只补不改"的打标记逻辑就会保留它,
    于是验证窗口的报错原文被当成选参期的消息放出去,而梯子一次都没扣
    (第 22 轮 codex 的 High,实测:消息里带出了验证窗口的收盘价)。
    阶段现在只存在于**框架自己写的这份结果字典**里,候选没有通道能改它。"""
    from research_api import describe_error          # 延迟导入:research_api 与本模块互相引用
    result = {"error": describe_error(exc, phase)}
    if trials is not None:
        # 失败的一轮也要**记账**:DSR 的试验次数按 prior + 扫过的格子数算,
        # 失败的一轮少记格子数 = 悄悄放松 DSR(ADR 第 9 条:漏记格子数会放松已锁定的 DSR ≥ 0.95)。
        result["trials"] = trials
    return result


def _phase_result(phase: str):
    """把阶段内抛出的异常变成**框架写的**错误结果(阶段由装饰器决定,候选碰不到)。

    与旧的 `_tag_phase` 的区别只有一处,但那处是致命的:旧的往**异常对象**上打标记(候选能自己挂),
    新的直接产出结果字典(候选碰不到)。最内层优先:内层已经变成结果字典了,外层就不会再改它。"""
    def decorate(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except GateFrameworkError:
                raise                                    # 框架自己的错:响亮地抛出去,不当成"候选失败了"
            except Exception as exc:
                return _error_result(phase, exc)
        return wrapper
    return decorate


def entry_prob(trips: int, n_bars: int) -> float:
    """随机对照的每根 K 线入场概率 = 候选在该窗口的往返数 / K 线数,限幅到合法概率。"""
    if n_bars <= 0:
        return MIN_ENTRY_PROB
    return min(MAX_ENTRY_PROB, max(MIN_ENTRY_PROB, trips / n_bars))


class GateFrameworkError(ValueError):
    """框架自己的不变量被破坏(窗口里混进了测试段 K 线、验证窗口没有 K 线……)。

    与候选的错**分开**:候选的错变成一条框架写的错误结果(阶段由框架决定);
    框架的错照旧抛出来——那不是候选的问题,必须让人看见,不能被当成"候选失败了"吞掉。
    继承 ValueError 是为了不改变既有调用方与用例的预期类型。"""


def slice_bars(bars: Sequence, start: datetime, end: datetime) -> list:
    chosen = [b for b in bars if start <= b.datetime <= end]
    if chosen and max(b.datetime for b in chosen) >= TEST_SEGMENT_START:
        raise GateFrameworkError("窗口包含测试段 K 线:门槛评估不得触碰测试段")
    return chosen


def _random_params(random_cls: type, params: dict) -> dict:
    allowed = set(getattr(random_cls, "parameters", []))
    return {k: v for k, v in params.items() if k in allowed and k not in ("entry_prob", "seed")}


@_phase_result(PHASE_VALIDATION)
def run_gate(*, candidate_cls: type, params: dict, windows: Sequence[Window], bars: Sequence, runner: Runner,
             random_cls: type, random_runs: int = ce.DEFAULT_RANDOM_RUNS, data_adjusted: bool, on_plateau: bool,
             capital: float, prior_trials: int | None = None,
             run_variances: Sequence[float] | None = None) -> dict:
    """prior_trials = 已累计的试验次数(含被淘汰者,由调用方从 risk/trial_ledger.py 读取),作为 DSR 的去通胀次数。
    不提供时 DSR 记为未计算,门槛按未通过处理(fail-closed)。"""
    slices = [(name, slice_bars(bars, start, end)) for name, start, end in windows]
    if any(not chunk for _, chunk in slices):
        raise GateFrameworkError("存在没有 K 线的验证窗口")

    base = [runner(candidate_cls, dict(params), chunk, 1.0) for _, chunk in slices]
    stress = [runner(candidate_cls, dict(params), chunk, STRESS_COST_MULTIPLIER) for _, chunk in slices]
    pooled = ce.pool_oos(base, capital)
    stress_sharpe = ce.pool_oos(stress, capital)["sharpe"]

    random_base = _random_params(random_cls, params)
    probs = [entry_prob(res["round_trips"], len(chunk)) for res, (_, chunk) in zip(base, slices)]
    random_sharpes = []
    for seed in ce.random_seeds(random_runs):
        runs = [runner(random_cls, {**random_base, "entry_prob": p, "seed": seed}, chunk, 1.0)
                for p, (_, chunk) in zip(probs, slices)]
        random_sharpes.append(ce.pool_oos(runs, capital)["sharpe"])

    result = ce.evaluate(pooled, stress_sharpe, random_sharpes, data_adjusted, on_plateau, n_trials=prior_trials,
                         run_variances=run_variances)
    result["windows"] = [
        {"name": name, "bars": len(chunk), "round_trips": res["round_trips"], "maxdd_pct": res["maxdd_pct"],
         "maxdd_yuan": res["maxdd_yuan"], "sharpe": ce.annualized_sharpe(res["daily_pnls"], capital)}
        for (name, chunk), res in zip(slices, base)
    ]
    result["random_sharpe_summary"] = {
        "runs": len(random_sharpes), "median": sorted(random_sharpes)[len(random_sharpes) // 2] if random_sharpes else None,
        "max": max(random_sharpes) if random_sharpes else None,
    }
    result["diagnosis"] = diagnose(
        {"sharpe": pooled["sharpe"], "maxdd_pct": pooled["max_drawdown_pct"], "maxdd_yuan": pooled["max_drawdown_yuan"],
         "round_trips": pooled["round_trips"]},
        [p for res in base for p in res.get("trade_pnls", [])] or None,
    )
    return result


def selection_facts(selection: dict) -> dict:
    """从选参结果里取出**进化回路放开的那一层**的事实:

    - `behavior`:行为描述符(持仓周期/交易频率,程序从回测结果算出);`cell`:MAP-Elites 格子名;
    - `selection_diagnosis`:选参窗口最佳格子的**结构化诊断**(枚举 + 0–1 数值,不含自由文本与原始数值);
    - `fitness`:选父代用的适应度(选参窗口的邻域平滑夏普)。DSR 不用于选父代。

    全部来自**选参窗口**(2020-07 ~ 2022-06),不是验证窗口,在这里过拟合不影响闸门。
    选不出最佳格子、或执行器没给行为信息时对应字段为 None(不猜)。"""
    detail = selection.get("best_detail")
    if not isinstance(detail, dict):
        return {"cell": None, "behavior": None, "selection_diagnosis": None, "fitness": None,
                "selection_metrics": None}
    hold_bars = detail.get("hold_bars")
    behavior = describe_behavior(detail["round_trips"], detail["n_bars"],
                                 hold_bars if isinstance(hold_bars, list) else None)
    row = {"sharpe": detail.get("sharpe"), "maxdd_pct": detail.get("maxdd_pct"),
           "maxdd_yuan": detail.get("maxdd_yuan"), "round_trips": detail["round_trips"]}
    trade_pnls = detail.get("trade_pnls")
    diagnosis = structure(diagnose(row, trade_pnls if isinstance(trade_pnls, list) and trade_pnls else None))
    drawdown = detail.get("maxdd_pct")
    return {"cell": behavior["cell"], "behavior": behavior, "selection_diagnosis": diagnosis,
            "fitness": selection.get("best_smoothed"),
            # 选参窗口最佳格子的收益(%)与最大回撤(%,取绝对值):谱系台账里的 total_return / max_drawdown
            "selection_metrics": {"total_return": detail.get("total_return_pct"),
                                  "max_drawdown": abs(drawdown) if isinstance(drawdown, (int, float)) else None}}


def _declared_grid_cells(grid: Any) -> int:
    """网格**声明**的格子数(拿不到就当 0)。

    失败的一轮也可能已经扫过这么多格子,所以失败记账按这个数取最坏情况——
    少记格子数会悄悄放松已锁定的 DSR(ADR 第 9 条)。"""
    if not isinstance(grid, dict):
        return 0
    total = 1
    for values in grid.values():
        if not isinstance(values, list):
            return 0
        total *= len(values)
    return total


def _failed_trials(prior_trials: int | None, cells: int) -> dict | None:
    """失败的一轮的试验记账:格子数取最坏情况,至少不低于预扣的最小网格数。"""
    if prior_trials is None:
        return None
    charged = max(MIN_GRID_CELLS, int(cells))
    return {"prior": prior_trials, "grid_cells": charged, "total": prior_trials + charged, "cell_sharpes": []}


def run_evaluation(*, candidate_cls: type, bars: Sequence, runner: Runner, random_cls: type, random_runs: int, capital: float,
                   max_cells: int, min_round_trips: int, select_window: tuple[datetime, datetime],
                   validation_windows: Sequence[Window], data_adjusted: bool = True, prior_trials: int | None = None,
                   run_variances: Sequence[float] | None = None) -> dict:
    """选参 + 门槛评估:候选在类上声明 `param_grid`,先在**选参窗口**(止于第一个验证窗口之前)扫网格选参并判定参数高原,
    再用选出的参数在**滚动验证窗口**上做完整门槛评估。选参数据与评估数据不重叠,样本外因此成立。

    选不出参数时 fail-closed:不跑成本翻倍与随机对照,直接给出未通过及原因。

    试验记账:结果的 `trials` = {prior, grid_cells, total}。本次扫过的格子数无论成败都在 `grid_cells` 里,
    调用方须据此写账本(失败的轮次同样消耗试验)。DSR 用 total = prior + grid_cells。"""
    grid = getattr(candidate_cls, "param_grid", None)
    declared_cells = _declared_grid_cells(grid)
    if grid is None:
        missing = GridError("候选类缺少 param_grid 属性:须声明三个参数及各自至少 3 个递增取值")
        return _error_result(PHASE_DECLARATION, missing, _failed_trials(prior_trials, 0))
    try:
        selection = select_params(candidate_cls=candidate_cls, grid=grid, bars=bars, runner=runner, capital=capital,
                                  max_cells=max_cells, min_round_trips=min_round_trips, window=select_window)
    except GridError as exc:
        # 网格声明有问题:我们自己写的消息,一个格子都没扫
        return _error_result(PHASE_DECLARATION, exc, _failed_trials(prior_trials, 0))
    except Exception as exc:
        # 选参窗口回放里的错:进化回路放开的那一层。
        # 可能已经扫过一部分格子 → 按**声明的格子数**记(最坏情况,只会更严不会更松)
        return _error_result(PHASE_SELECTION, exc, _failed_trials(prior_trials, declared_cells))
    summary = {k: v for k, v in selection.items() if k not in ("plateau", "cell_sharpes", "best_detail")}   # 完整得分表留在服务端,不回传给外层
    engine_side = selection_facts(selection)           # 选参窗口的行为描述符/结构化诊断/适应度(进化回路放开的那一层)
    # 本次扫过的每个有效格子的**日**夏普(= 年化 / √240,与 DSR 同一口径):入账本,并并入本次 DSR 的经验方差
    # 限幅到 ±MAX_TRIAL_SHARPE:真实网格里偶有极端格子(几笔全盈利之类)夏普极大,不限幅会让整批样本因越界被父进程丢弃;
    # 父进程的校验仍然拒绝越界值——那才是伪造。限幅只会让 V 略小于"不限幅",而 V 有理论值作下限,不会更松。
    cell_sharpes = [max(-MAX_TRIAL_SHARPE, min(MAX_TRIAL_SHARPE, s / math.sqrt(ce.TRADING_DAYS_PER_YEAR)))
                    for s in selection.get("cell_sharpes", [])]
    summary["plateau_cell"] = selection["plateau"]["cell"]
    cells_run = int(selection["n_cells"])
    trials = None if prior_trials is None else {"prior": prior_trials, "grid_cells": cells_run, "total": prior_trials + cells_run,
                                                "cell_sharpes": cell_sharpes}
    own = run_variance(cell_sharpes)                  # 本次评估自己的格子夏普方差(有效格子 < 5 个则不入样本)
    merged_variances = list(run_variances or ()) + ([own] if own is not None else [])
    if selection["best_params"] is None:
        return {"passed": False, "failures": ["无法选参: " + selection["note"]], "params": None,
                "selection": summary, "kind_detail": "selection-only", "trials": trials,
                "heatmap": build_heatmap(selection), **engine_side}
    result = run_gate(candidate_cls=candidate_cls, params=selection["best_params"], windows=validation_windows, bars=bars,
                      runner=runner, random_cls=random_cls, random_runs=random_runs, data_adjusted=data_adjusted,
                      on_plateau=selection["on_plateau"], capital=capital,
                      prior_trials=None if trials is None else trials["total"], run_variances=merged_variances)
    if "error" in result:                                # 验证期出错:run_gate 自己写的错误结果(阶段=validation)
        if trials is not None:
            # 选参阶段的格子数**照样记账**:验证期失败不代表那些格子没扫过,
            # 少记格子数会让后续的 DSR 少算试验次数 = 悄悄放松已锁定的 DSR 门槛(ADR 第 9 条)
            result["trials"] = trials
        return result
    result["trials"] = trials
    result["params"] = selection["best_params"]
    result["selection"] = summary
    # 完整得分表(选参窗口)交给父进程单独落盘给面板看;父进程在把结果交出去之前把它拿掉,**不并入**外层响应
    result["heatmap"] = build_heatmap(selection)
    result.update(engine_side)
    result["kind_detail"] = "selection+gate"
    return result


def engine_runner(key: str):
    """真实执行器:在给定 K 线上用 vn.py 引擎回放。"""
    def runner(strategy_class: type, setting: dict, bars: list, cost_multiplier: float) -> dict:
        from backtest_lib import run_engine_bars
        engine = run_engine_bars(strategy_class, setting, key, bars, cost_multiplier=cost_multiplier)
        daily = engine.calculate_result()
        stats = engine.calculate_statistics(daily, output=False) or {}
        pnls = [] if daily is None or len(daily) == 0 else [float(x) for x in daily["net_pnl"].tolist()]
        from research_sandbox import round_trip_pnls            # 与落盘结果同一份配对口径(FIFO)
        from risk.behavior import round_trip_holding_bars
        from strategies.instruments import INSTRUMENTS
        trades = engine.get_all_trades()
        return {
            "daily_pnls": pnls,
            "round_trips": int(stats.get("total_trade_count", 0)) // 2,
            "maxdd_pct": float(stats.get("max_ddpercent", 0.0)),
            "maxdd_yuan": float(stats.get("max_drawdown", 0.0)),
            "hold_bars": round_trip_holding_bars(trades, bars),                 # 行为描述符:每个往返持有的 K 线根数
            "trade_pnls": round_trip_pnls(trades, INSTRUMENTS[key].size),       # 结构化诊断:最长连亏
        }
    return runner
