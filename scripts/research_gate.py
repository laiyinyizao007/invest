"""门槛评估编排:滚动验证段上跑候选 / 成本翻倍 / 随机入场对照,汇总后调用晋级门槛。

回测执行器(runner)由调用方注入,便于用假执行器单测;真实执行器见 `engine_runner`。
决议见 docs/adr/20261112-candidate-evaluation.md:样本外 = 滚动验证段,测试段不可达,门槛数值不放宽。
随机对照限制:出场逻辑取自随机对照类(目前是 TrendAtr 家族的 ATR 移动止损),候选的出场参数(如 atr_mult)会同样传入;
候选若使用完全不同的出场机制,这条对照衡量的仍是「相同出场下入场信号的贡献」,解读时须知道这一点。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Sequence

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
             capital: float) -> dict:
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

    result = ce.evaluate(pooled, stress_sharpe, random_sharpes, data_adjusted, on_plateau)
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
