"""15 分钟基准策略的网格优化 + 三段样本外验证(方案见 docs/adr/20261110-baseline-15m.md,结果出来之前已提交)。

用法(项目根目录):
    python scripts/optimize_15m.py             # 训练段选参数,在验证段检查(含成本翻倍压力测试)
    python scripts/optimize_15m.py --test      # 参数定下后,最后一次性看测试段

纪律同 optimize.py:参数只在训练段选;验证段只用来判断过拟合;测试段只看一次,看后不得据此改动。
"""
import argparse
import itertools
import math
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import bootstrap

import pandas as pd
from vnpy.trader.constant import Exchange

from backtest_lib import load_bars_csv, run_backtest_bars
from optimize import smoothed
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

DATA_FILE = Path(r"C:\Dev\data\invest\derived\C_15m_adj.csv")
KEY = "C15"
WINDOWS = {
    "train": (datetime(2020, 7, 22), datetime(2022, 12, 31, 23, 59)),
    "valid": (datetime(2023, 1, 1), datetime(2023, 12, 31, 23, 59)),
    "test": (datetime(2024, 1, 1), datetime(2026, 6, 15, 23, 59)),
}
FAST = [8, 12, 16, 24, 32]
SLOW = [48, 72, 96, 144, 192, 288]
MULT = [1.5, 2.0, 2.5, 3.0, 4.0]
MIN_FILLS = 100            # 训练段成交笔数(开+平)少于此值不参与排名
MIN_ROUND_TRIPS_NOTE = 150  # 与晋级门槛一致:往返少于此值夏普没有统计意义


@lru_cache(maxsize=1)
def all_bars():
    return load_bars_csv(DATA_FILE, "CADJ15", Exchange.DCE)


def window_bars(window: str):
    start, end = WINDOWS[window]
    return [b for b in all_bars() if start <= b.datetime <= end]


def evaluate(params: dict, window: str, cost_multiplier: float = 1.0) -> dict:
    stats, _, trades = run_backtest_bars(TrendAtr15mStrategy, params, KEY, window_bars(window), cost_multiplier=cost_multiplier)
    stats = stats or {}
    return {
        "return_pct": stats.get("total_return", 0.0),
        "maxdd_pct": stats.get("max_ddpercent", 0.0),
        "maxdd_yuan": stats.get("max_drawdown", 0.0),
        "sharpe": stats.get("sharpe_ratio", 0.0),
        "round_trips": stats.get("total_trade_count", 0) // 2,
        "fills": stats.get("total_trade_count", 0),
        "end_balance": stats.get("end_balance", 0.0),
        "net_pnl": stats.get("total_net_pnl", 0.0),
        "total_cost": stats.get("total_commission", 0.0) + stats.get("total_slippage", 0.0),
    }


def valid_score(row: dict) -> float:
    s = row["sharpe"]
    if row["fills"] < MIN_FILLS or s is None or (isinstance(s, float) and math.isnan(s)):
        return float("-inf")
    return float(s)


def grid():
    combos = []
    for (fi, f), (si, s), (mi, m) in itertools.product(enumerate(FAST), enumerate(SLOW), enumerate(MULT)):
        if s >= 2 * f:
            combos.append((fi, si, mi, f, s, m))
    return combos


def report(label: str, window: str, res: dict, note: str = "") -> None:
    start, end = WINDOWS[window]
    flag = ""
    if window != "train" and res["round_trips"] < MIN_ROUND_TRIPS_NOTE:
        flag = f"  [!] 往返仅 {res['round_trips']} 笔(<{MIN_ROUND_TRIPS_NOTE}),夏普无统计意义,只能说'无法证明优势'"
    print(f"  {label:12s} {start:%Y-%m}~{end:%Y-%m}: 收益 {res['return_pct']:7.1f}%  最大回撤 {res['maxdd_pct']:6.1f}% ({res['maxdd_yuan']:7.0f}元)  "
          f"夏普 {res['sharpe']:5.2f}  往返 {res['round_trips']:4d} 笔  净盈亏 {res['net_pnl']:8.0f}元  成本 {res['total_cost']:7.0f}元{note}{flag}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--test", action="store_true", help="最终确认:查看测试段(只应运行一次)")
    args = parser.parse_args(argv)

    combos = grid()
    print(f"[{KEY}] 数据 {DATA_FILE.name};训练段 {WINDOWS['train'][0]:%Y-%m-%d}~{WINDOWS['train'][1]:%Y-%m-%d},共 {len(combos)} 组参数")
    rows, scores = [], {}
    for i, (fi, si, mi, f, s, m) in enumerate(combos, 1):
        params = {"fast_window": f, "slow_window": s, "atr_mult": m}
        res = evaluate(params, "train")
        scores[(fi, si, mi)] = valid_score(res)
        rows.append({**params, **res, "score": scores[(fi, si, mi)], "grid": (fi, si, mi)})
        if i % 25 == 0:
            print(f"  ... {i}/{len(combos)}", flush=True)

    smooth = smoothed(scores)
    if not smooth:
        print("没有任何参数组合同时满足最少成交笔数与完整邻域,放弃优化。")
        return 1
    for r in rows:
        r["smoothed"] = smooth.get(r.pop("grid"), float("-inf"))

    df = pd.DataFrame(rows).sort_values("smoothed", ascending=False)
    out_dir = bootstrap.ROOT / "data" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / "optimize_C15_train.csv", index=False)

    pd.set_option("display.width", 220)
    print("\n训练段 Top 8(按邻域平均夏普排序):")
    print(df.head(8)[["fast_window", "slow_window", "atr_mult", "sharpe", "smoothed", "return_pct", "maxdd_pct", "round_trips", "total_cost"]].to_string(index=False))
    ok = df[df["score"] != float("-inf")]
    print(f"\n有效组合 {len(ok)}/{len(df)};训练段夏普为正的占 {(ok['sharpe'] > 0).mean():.0%},中位数 {ok['sharpe'].median():.2f};"
          f"净盈亏为正的占 {(ok['net_pnl'] > 0).mean():.0%},成本合计中位数 {ok['total_cost'].median():.0f} 元")

    best = df.iloc[0]
    params = {"fast_window": int(best.fast_window), "slow_window": int(best.slow_window), "atr_mult": float(best.atr_mult)}
    print(f"\n选定参数(训练段邻域平均最优): {params}")

    windows = ["train", "valid"] + (["test"] if args.test else [])
    for w in windows:
        tag = "  <-- 最终确认,只看这一次" if w == "test" else ""
        report(w, w, evaluate(params, w), tag)
        if w != "train":
            report(w + " 成本x2", w, evaluate(params, w, cost_multiplier=2.0))
    if not args.test:
        print("\n(测试段未查看。参数确认无误后再加 --test 运行一次。)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
