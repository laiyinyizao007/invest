"""算梯子式量化的**汇报步长 η**(Blum & Hardt 2015, arXiv 1502.04585 的无参变体)。

为什么要有 η:阶梯式量化只在"新成绩比历史最佳高出至少 η"时才更新汇报值,
否则重复上次值。没有 η 的话,噪声级别的抖动会被当成进步,汇报值随提交次数随机游走,
误差按提交次数**线性**增长;有了 η,Theorem 3.1 给出误差只按对数增长。
所以 η 不是风格选择,是这套量化能不能用的问题。

η 怎么定(方法已写死在 ADR `20261113-evolution-engine.md`,本脚本只把**数值**算出来):
  η ≈ 1.5 × SE(配对日盈亏差),下限 0.25 夏普。
  配对差 = 两条候选在**同一验证段**上的逐日盈亏之差。用配对差而不是各自的标准差,
  是因为共同的市场波动在差里互相抵消,剩下的才是"候选之间的真实差异 + 各自的噪声"。
  下限 0.25:异常安静的窗口会算出 0.03 这种没意义的步长(那时任何抖动都算"进步")。

怎么估 SE:对配对日盈亏做**分块自助**(block bootstrap,块长 5 个交易日,保留自相关),
每次重采样算一次 ΔSharpe,2000 次重采样的标准差就是 SE(ΔSharpe)。
分块而不是逐日独立重采样:日盈亏是自相关的,逐日重采样会低估方差。
解析式 SE(配对差标准差 / 池化波动)同时打印,作为交叉校验——两者应当同量级。

本脚本用哪个窗口:
  **池化验证段** 2022-07-01 ~ 2023-12-31(W1/W2/W3 三个验证段拼起来),也就是阶梯实际汇报成绩的那一段。
  η 必须与"测量成绩的那段"的样本量与噪声尺度匹配;用训练段估出来的 η 放到验证段上会失配。
  测试段(2024-01-01 起)不碰:它已被消耗,不得复用。

用哪一对候选:
  阶梯比较的是**相邻两代候选**(变异只改一两个参数),所以取网格里"相邻一步"的参数对:
  fast 10→14→…→30、slow 40→50→…→90(atr_mult 固定 1.5,与 `walk_forward_optimizer` 同口径)。
  每对的 SE 单列,取**中位数**作为典型值,再乘 1.5。取中位数而不是均值:
  单对的 SE 受个别参数组合影响很大,中位数对离群组合稳健。

用法: python scripts/calc_ladder_eta.py [--pairs all] [--resamples 2000]
"""
import argparse
import math
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: F401  (保证工作目录与时区设置)

import pandas as pd
from vnpy.trader.constant import Exchange

from backtest_lib import DEFAULT_CAPITAL, load_bars_csv, run_engine_bars
from risk import candidate_eval as ce
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
from walk_forward_optimizer import ATR_MULT, DATA_FILE, FAST, SLOW, build_windows

VALID_START = datetime(2022, 7, 1)
VALID_END = datetime(2023, 12, 31, 23, 59)
TRAIN_START = datetime(2020, 7, 22)
TRAIN_END = datetime(2022, 6, 30, 23, 59)
BLOCK = 5              # 自助块长(交易日)
ETA_FACTOR = 1.5       # η = 1.5 × SE
ETA_FLOOR = 0.25       # η 的下限(夏普)


def adjacent_pairs() -> list[tuple[dict, dict]]:
    """网格里"相邻一步"的参数对:每次只动一个参数,其余不动——这正是变异能造出的最小差异。"""
    pairs = []
    for slow in SLOW:
        for a, b in zip(FAST, FAST[1:]):
            pairs.append(({"fast_window": a, "slow_window": slow}, {"fast_window": b, "slow_window": slow}))
    for fast in FAST:
        for a, b in zip(SLOW, SLOW[1:]):
            pairs.append(({"fast_window": fast, "slow_window": a}, {"fast_window": fast, "slow_window": b}))
    return pairs


def setting_of(params: dict) -> dict:
    """策略参数表:可优化的三个 + 固定的几个,与基准策略的类属性一致。"""
    return {
        "fast_window": params["fast_window"],
        "slow_window": params["slow_window"],
        "atr_mult": ATR_MULT,
        "atr_window": 14,
        "risk_budget": 300.0,
        "take_profit_r": 0.0,
        "max_daily_losses": 3,
        "history_vt_symbol": "CADJ15.DCE",
    }


def run_daily_pnls(bars, params: dict) -> dict:
    """跑一条策略,返回 {日期: 当日净盈亏}。没有成交的交易日不在结果里(配对时按 0 补)。"""
    engine = run_engine_bars(TrendAtr15mStrategy, setting_of(params), "C15", bars)
    daily = engine.calculate_result()
    if daily is None or len(daily) == 0:
        return {}
    # vn.py 的逐日结果把日期放在**索引**上(不是列)
    out = {}
    for day, row in daily.iterrows():
        out[pd.Timestamp(day).date()] = float(row["net_pnl"])
    return out


def run_pooled_daily_pnls(bars, params: dict, windows) -> dict:
    """按**三个验证段分别回放**再拼成一条序列,与 `research_gate.run_gate` 的口径一致。

    为什么不能连着跑一段:真实链路里每个验证段是一次独立的回测(段与段之间不持仓、
    指标也不跨段归并)。连着跑会让持仓跨过 2023-01-01 的段边界,那就不是同一件事了。
    """
    merged: dict = {}
    for start, end in windows:
        segment = [b for b in bars if start <= b.datetime <= end]
        if not segment:
            continue
        merged.update(run_daily_pnls(segment, params))
    return merged


def sharpe(pnls: list[float], capital: float) -> float:
    return ce.annualized_sharpe(pnls, capital)


def paired_bootstrap_se(a: dict, b: dict, capital: float, resamples: int, block: int, seed: int) -> tuple[float, float]:
    """返回 (自助 SE(ΔSharpe), 解析式 SE)。

    自助:按块重采样交易日(同一组日期索引同时用于两条策略,保持配对),每次算 ΔSharpe。
    解析式:SE ≈ sqrt(240)·std(配对差)/sqrt(n)/σ_池化,σ_池化取两条策略日收益合并后的标准差。
    """
    days = sorted(set(a) | set(b))
    pa = [a.get(d, 0.0) for d in days]
    pb = [b.get(d, 0.0) for d in days]
    n = len(days)
    if n < 2 * block:
        return float("nan"), float("nan")

    rng = random.Random(seed)
    diffs = []
    nblocks = (n + block - 1) // block
    for _ in range(resamples):
        idx: list[int] = []
        for _ in range(nblocks):
            start = rng.randrange(n)
            idx.extend((start + k) % n for k in range(block))
        idx = idx[:n]
        sa = sharpe([pa[i] for i in idx], capital)
        sb = sharpe([pb[i] for i in idx], capital)
        diffs.append(sa - sb)
    boot_se = statistics.pstdev(diffs) if len(diffs) > 1 else float("nan")

    d = [x - y for x, y in zip(pa, pb)]
    m = sum(d) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in d) / (n - 1)) / capital   # 折成收益率口径
    pooled = [x / capital for x in pa] + [x / capital for x in pb]
    mp = sum(pooled) / len(pooled)
    varp = sum((x - mp) ** 2 for x in pooled) / (len(pooled) - 1)
    sigma = math.sqrt(varp)
    # sigma == 0:两条策略的日收益都是常数(没有成交),夏普本身无定义,噪声也是 0
    analytic = (math.sqrt(ce.TRADING_DAYS_PER_YEAR) * sd / math.sqrt(n) / sigma) if sigma > 0 else 0.0
    return boot_se, analytic


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--resamples", type=int, default=2000)
    p.add_argument("--seed", type=int, default=20261113)
    p.add_argument("--data", default=str(DATA_FILE))
    p.add_argument("--window", choices=["valid", "train"], default="valid",
                   help="valid=池化验证段(2022-07~2023-12,η 的正式标定窗口);"
                        "train=W1 训练段(2020-07~2022-06,只作敏感性对照)")
    a = p.parse_args(argv)

    if a.window == "valid":
        win_start, win_end = VALID_START, VALID_END
        # 窗口自检:三个验证段拼起来必须正好是 2022-07-01 ~ 2023-12-31
        seg_start = min(w.valid_start for w in build_windows())
        seg_end = max(w.valid_end for w in build_windows())
        if (seg_start, seg_end) != (VALID_START, VALID_END):
            print(f"窗口定义变了:验证段现在是 {seg_start:%Y-%m-%d}~{seg_end:%Y-%m-%d},"
                  f"与本脚本硬编码的 {VALID_START:%Y-%m-%d}~{VALID_END:%Y-%m-%d} 不一致;先改脚本再改结论")
            return 2
        # 与真实链路一致:三个验证段**分别**回放再拼接(段间不持仓)
        seg_windows = [(w.valid_start, w.valid_end) for w in build_windows()]
    else:
        win_start, win_end = TRAIN_START, TRAIN_END
        seg_windows = [(TRAIN_START, TRAIN_END)]

    df = pd.read_csv(a.data, parse_dates=["datetime"])
    df = df[(df["datetime"] >= win_start) & (df["datetime"] <= win_end)]
    bars = load_bars_csv(a.data, "CADJ15", Exchange.DCE)
    bars = [b for b in bars if win_start <= b.datetime <= win_end]
    print(f"[{a.window}] {win_start:%Y-%m-%d}~{win_end:%Y-%m-%d}:{len(bars)} 根 15 分钟 K 线"
          f"({df['datetime'].min():%Y-%m-%d %H:%M}~{df['datetime'].max():%Y-%m-%d %H:%M})")

    pairs = adjacent_pairs()
    results = []
    for i, (pa, pb) in enumerate(pairs):
        a_pnls = run_pooled_daily_pnls(bars, pa, seg_windows)
        b_pnls = run_pooled_daily_pnls(bars, pb, seg_windows)
        if not a_pnls or not b_pnls:
            print(f"第 {i + 1} 对 {pa} vs {pb}:无成交,跳过")
            continue
        boot_se, analytic = paired_bootstrap_se(a_pnls, b_pnls, DEFAULT_CAPITAL, a.resamples, BLOCK, a.seed + i)
        sa = sharpe(list(a_pnls.values()), DEFAULT_CAPITAL)
        sb = sharpe(list(b_pnls.values()), DEFAULT_CAPITAL)
        days = len(set(a_pnls) | set(b_pnls))
        results.append((pa, pb, sa, sb, boot_se, analytic, days))
        print(f"{i + 1:2d}/{len(pairs)} {pa['fast_window']}/{pa['slow_window']} vs "
              f"{pb['fast_window']}/{pb['slow_window']}: ΔSharpe={sa - sb:+.3f} "
              f"(自助 SE={boot_se:.3f}, 解析 SE={analytic:.3f}, n={days} 天)")

    if not results:
        print("没有可用的参数对")
        return 1
    ses = sorted(r[4] for r in results)
    median_se = statistics.median(ses)
    eta = max(ETA_FLOOR, ETA_FACTOR * median_se)
    print(f"\n配对日盈亏差的 SE(ΔSharpe):中位数 {median_se:.4f},最小 {ses[0]:.4f},最大 {ses[-1]:.4f}"
          f"({len(ses)} 对)")
    print(f"η = max({ETA_FLOOR}, {ETA_FACTOR} × {median_se:.4f}) = **{eta:.3f}** 夏普")
    print("注:这是**汇报阈值**(阶梯什么时候更新汇报值),不是晋级门槛,与 DSR 阈值互相独立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
