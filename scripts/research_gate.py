"""门槛评估编排:滚动验证段上跑候选 / 成本翻倍 / 随机入场对照,汇总后调用晋级门槛。

回测执行器(runner)由调用方注入,便于用假执行器单测;真实执行器见 `engine_runner`。
决议见 docs/adr/20261112-candidate-evaluation.md:样本外 = 滚动验证段,测试段不可达,门槛数值不放宽。
随机对照限制:出场逻辑取自随机对照类(目前是 TrendAtr 家族的 ATR 移动止损),候选的出场参数(如 atr_mult)会同样传入;
候选若使用完全不同的出场机制,这条对照衡量的仍是「相同出场下入场信号的贡献」,解读时须知道这一点。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Sequence

from research_grid import GridError, select_params
from risk import candidate_eval as ce
from risk.diagnosis import diagnose

TEST_SEGMENT_START = datetime(2024, 1, 1)   # 与 walk_forward_optimizer.TEST_SEGMENT_START 保持一致(有测试锁定)
MIN_ENTRY_PROB = 1e-4
MAX_ENTRY_PROB = 0.5
STRESS_COST_MULTIPLIER = 2.0

Window = tuple[str, datetime, datetime]
Runner = Callable[[type, dict, list, float], dict]


def entry_prob(trips: int, n_bars: int) -> float:
    """随机对照的每根 K 线入场概率 = 候选在该窗口的往返数 / K 线数,限幅到合法概率。"""
    if n_bars <= 0:
        return MIN_ENTRY_PROB
    return min(MAX_ENTRY_PROB, max(MIN_ENTRY_PROB, trips / n_bars))


def slice_bars(bars: Sequence, start: datetime, end: datetime) -> list:
    chosen = [b for b in bars if start <= b.datetime <= end]
    if chosen and max(b.datetime for b in chosen) >= TEST_SEGMENT_START:
        raise ValueError("窗口包含测试段 K 线:门槛评估不得触碰测试段")
    return chosen


def _random_params(random_cls: type, params: dict) -> dict:
    allowed = set(getattr(random_cls, "parameters", []))
    return {k: v for k, v in params.items() if k in allowed and k not in ("entry_prob", "seed")}


def run_gate(*, candidate_cls: type, params: dict, windows: Sequence[Window], bars: Sequence, runner: Runner,
             random_cls: type, random_runs: int = ce.DEFAULT_RANDOM_RUNS, data_adjusted: bool, on_plateau: bool,
             capital: float, prior_trials: int | None = None) -> dict:
    """prior_trials = 已累计的试验次数(含被淘汰者,由调用方从 risk/trial_ledger.py 读取),作为 DSR 的去通胀次数。
    不提供时 DSR 记为未计算,门槛按未通过处理(fail-closed)。"""
    slices = [(name, slice_bars(bars, start, end)) for name, start, end in windows]
    if any(not chunk for _, chunk in slices):
        raise ValueError("存在没有 K 线的验证窗口")

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

    result = ce.evaluate(pooled, stress_sharpe, random_sharpes, data_adjusted, on_plateau, n_trials=prior_trials)
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
         "round_trips": pooled["round_trips"],
         # 第 55 轮之十六:诊断层的「可疑夏普」上限与晋级门槛**同一份口径**
         # ⇒ 这里必须把门槛用的那两个输入**原样**带过去(从 metrics 里取,
         # 不另算一份 ⇒ 同一个问题两个答案)。
         "n_trials": result["metrics"]["n_trials"],
         "oos_years": result["metrics"]["oos_years"]},
        [p for res in base for p in res.get("trade_pnls", [])] or None,
    )
    return result


def run_evaluation(*, candidate_cls: type, bars: Sequence, runner: Runner, random_cls: type, random_runs: int, capital: float,
                   max_cells: int, min_round_trips: int, select_window: tuple[datetime, datetime],
                   validation_windows: Sequence[Window], data_adjusted: bool = True, prior_trials: int | None = None) -> dict:
    """选参 + 门槛评估:候选在类上声明 `param_grid`,先在**选参窗口**(止于第一个验证窗口之前)扫网格选参并判定参数高原,
    再用选出的参数在**滚动验证窗口**上做完整门槛评估。选参数据与评估数据不重叠,样本外因此成立。

    选不出参数时 fail-closed:不跑成本翻倍与随机对照,直接给出未通过及原因。

    试验记账:结果的 `trials` = {prior, grid_cells, total}。本次扫过的格子数无论成败都在 `grid_cells` 里,
    调用方须据此写账本(失败的轮次同样消耗试验)。DSR 用 total = prior + grid_cells。"""
    grid = getattr(candidate_cls, "param_grid", None)
    if grid is None:
        raise GridError("候选类缺少 param_grid 属性:须声明三个参数及各自至少 3 个递增取值")
    selection = select_params(candidate_cls=candidate_cls, grid=grid, bars=bars, runner=runner, capital=capital,
                              max_cells=max_cells, min_round_trips=min_round_trips, window=select_window)
    summary = {k: v for k, v in selection.items() if k != "plateau"}      # 完整得分表留在服务端,不回传给外层
    summary["plateau_cell"] = selection["plateau"]["cell"]
    cells_run = int(selection["n_cells"])
    trials = None if prior_trials is None else {"prior": prior_trials, "grid_cells": cells_run, "total": prior_trials + cells_run}
    if selection["best_params"] is None:
        return {"passed": False, "failures": ["无法选参: " + selection["note"]], "params": None,
                "selection": summary, "kind_detail": "selection-only", "trials": trials}
    result = run_gate(candidate_cls=candidate_cls, params=selection["best_params"], windows=validation_windows, bars=bars,
                      runner=runner, random_cls=random_cls, random_runs=random_runs, data_adjusted=data_adjusted,
                      on_plateau=selection["on_plateau"], capital=capital,
                      prior_trials=None if trials is None else trials["total"])
    result["trials"] = trials
    result["params"] = selection["best_params"]
    result["selection"] = summary
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
        return {
            "daily_pnls": pnls,
            "round_trips": int(stats.get("total_trade_count", 0)) // 2,
            "maxdd_pct": float(stats.get("max_ddpercent", 0.0)),
            "maxdd_yuan": float(stats.get("max_drawdown", 0.0)),
        }
    return runner
