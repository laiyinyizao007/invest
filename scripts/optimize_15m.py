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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

import pandas as pd
from vnpy.trader.constant import Exchange

import segment_ledger
from backtest_lib import load_bars_csv, run_backtest_bars
from optimize import smoothed
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

DATA_FILE = Path(r"C:\Dev\data\invest\derived\C_15m_adj.csv")
# 购买数据与派生产物都在仓库之外,换机器/换目录时用 --data 覆盖(见 main)。
KEY = "C15"
WINDOWS = {
    "train": (datetime(2020, 7, 22), datetime(2022, 12, 31, 23, 59)),
    "valid": (datetime(2023, 1, 1), datetime(2023, 12, 31, 23, 59)),
    # 样本外安全选参:训练段止于 Walk-Forward 第一个验证窗口之前(2022-07-01 起的验证窗口因此保持真正的样本外)
    "select": (datetime(2020, 7, 22), datetime(2022, 6, 30, 23, 59)),
    "test": (datetime(2024, 1, 1), datetime(2026, 6, 15, 23, 59)),
}
FAST = [8, 12, 16, 24, 32]
SLOW = [48, 72, 96, 144, 192, 288]
MULT = [1.5, 2.0, 2.5, 3.0, 4.0]
MIN_FILLS = 100            # 训练段成交笔数(开+平)少于此值不参与排名
MIN_ROUND_TRIPS_NOTE = 150  # 与晋级门槛一致:往返少于此值夏普没有统计意义


@lru_cache(maxsize=4)
def all_bars(data_file: Path):
    """按文件路径缓存,换 --data 时缓存键自然变化,不会误用上一份 K 线。"""
    return load_bars_csv(data_file, "CADJ15", Exchange.DCE)


def window_bars(window: str):
    start, end = WINDOWS[window]
    return [b for b in all_bars(DATA_FILE) if start <= b.datetime <= end]


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
    print("           注:夏普按 vn.py 的自然日分组计算(夜盘与次日日盘分属两天),与日线基准的夏普数值不可直接比较;"
          "收益/净盈亏/成本/回撤的绝对值不受影响。")


def plateau_json(scores: dict, cell: tuple) -> str:
    """把训练段参数网格得分写成沙盒 `gate --plateau-file` 能读的 JSON。无效格子写成 "-inf" 字符串(JSON 没有无穷大)。"""
    import json
    rows = [[*key, "-inf" if value == float("-inf") else value] for key, value in sorted(scores.items())]
    return json.dumps({"cell": list(cell), "scores": rows}, ensure_ascii=False)


def segment_key() -> str:
    start, end = WINDOWS["test"]
    return f"optimize_15m:{KEY}:{start:%Y-%m-%d}~{end:%Y-%m-%d}"


def main(argv: list[str] | None = None) -> int:
    global DATA_FILE
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--test", action="store_true", help="最终确认:查看测试段(只应运行一次)")
    parser.add_argument("--select-only", action="store_true",
                        help="样本外安全选参:只用 2022-06-30 之前的数据选参并输出,不评估验证/测试段(供研究沙盒 gate 使用)")
    parser.add_argument("--plateau-out", default=None, help="把训练段参数网格得分写到此 JSON,供研究沙盒 gate --plateau-file 使用")
    parser.add_argument("--data", default=str(DATA_FILE), help=f"15 分钟复权 K 线 CSV(默认 {DATA_FILE})")
    args = parser.parse_args(argv)
    DATA_FILE = Path(args.data)
    if args.test:
        try:
            segment_ledger.check(segment_key())
        except segment_ledger.SegmentAlreadyUsed as exc:
            print(f"拒绝运行 --test: {exc}", file=sys.stderr)
            return 2

    if args.select_only and args.test:
        print("拒绝: --select-only 与 --test 不能同时使用", file=sys.stderr)
        return 2
    train_w = "select" if args.select_only else "train"
    combos = grid()
    print(f"[{KEY}] 数据 {DATA_FILE.name};训练段 {WINDOWS[train_w][0]:%Y-%m-%d}~{WINDOWS[train_w][1]:%Y-%m-%d},共 {len(combos)} 组参数")
    rows, scores = [], {}
    for i, (fi, si, mi, f, s, m) in enumerate(combos, 1):
        params = {"fast_window": f, "slow_window": s, "atr_mult": m}
        res = evaluate(params, train_w)
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
    if args.plateau_out:     # 只含训练段得分,不触碰验证/测试段
        cell = (FAST.index(params["fast_window"]), SLOW.index(params["slow_window"]), MULT.index(params["atr_mult"]))
        Path(args.plateau_out).write_text(plateau_json(scores, cell), encoding="utf-8")
        print(f"参数网格得分已写入 {args.plateau_out}")

    if args.select_only:     # 到此为止:验证/测试段一律不看,保持 gate 的滚动验证窗口为真正的样本外
        print("(--select-only:未评估验证/测试段。把上面的参数和 --plateau-out 文件交给 research_api.py gate。)")
        return 0
    if args.test:  # 先登记后运行
        segment_ledger.record(segment_key(), note=f"optimize_15m.py --test 参数 {params}")
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
