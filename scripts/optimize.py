"""网格优化 + 三段样本外验证。

用法(项目根目录):
    python scripts/optimize.py --symbol C            # 只用训练段选参数,在验证段检查
    python scripts/optimize.py --symbol C --test     # 参数定下后,最后一次性看测试段

纪律:
- 参数只在【训练段】上选;验证段用来判断是否过拟合;测试段(2023 至今)只在最终确认时看**一次**,
  看过之后不得再据此调整参数,否则测试段就变成了训练段。
- 选参数用"邻域平均得分"而非单点最高分:最优点周围的参数组合也应该表现不差(参数高原),
  孤立的尖峰基本是过拟合。
- 网格搜索试得越多,越容易挑到"运气好"的组合,所以还要配合 random_control.py 做随机入场对照。
"""
import argparse
import itertools
import math

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

import pandas as pd

import segment_ledger
from backtest_lib import run_backtest
from strategies.trend_atr_strategy import TrendAtrStrategy

# 窗口常量放在 segment_windows.py:那里是纯数据,不 import bootstrap/vnpy。
# 研究侧(沙盒、网关)只需要窗口,不该为了查一段日期把整个交易栈拖进来
# (第 7 轮 codex 的 C1/H1:CI 的研究侧闸门不装交易环境,拖进来就收集失败)。
# 这里仍然导出同名常量,别处的引用(`optimize.WINDOWS` 等)不受影响。
from segment_windows import WINDOWS, WINDOWS_ADJ, segment_key, windows_for  # noqa: E402,F401

FAST = [5, 8, 10, 13, 16, 20]
SLOW = [30, 40, 60, 80, 100, 120]
MULT = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0]  # 含更窄的止损:1 万元/300 元预算下,近年只有窄止损才开得了仓
MIN_TRADES = 20  # 成交笔数(开+平各算一笔)少于此值的组合不参与排名
MIN_ROUND_TRIPS_NOTE = 10  # 验证/测试段往返笔数少于此值时,夏普等指标没有统计意义,输出时醒目提示


def evaluate(key: str, params: dict, window: str) -> dict:
    start, end = windows_for(key)[window]
    stats, _, _ = run_backtest(TrendAtrStrategy, params, key, start, end)
    stats = stats or {}
    return {
        "return_pct": stats.get("total_return", 0.0),
        "maxdd_pct": stats.get("max_ddpercent", 0.0),
        "sharpe": stats.get("sharpe_ratio", 0.0),
        "round_trips": stats.get("total_trade_count", 0) // 2,
        "end_balance": stats.get("end_balance", 0.0),
        "fills": stats.get("total_trade_count", 0),
    }


def valid_score(row: dict) -> float:
    s = row["sharpe"]
    if row["fills"] < MIN_TRADES or s is None or (isinstance(s, float) and math.isnan(s)):
        return float("-inf")
    return float(s)


def grid() -> list[tuple[int, int, int, float]]:
    """(快线序号, 慢线序号, 倍数序号) -> 参数;要求慢线 >= 2 × 快线,避免两条线几乎重合。"""
    combos = []
    for (fi, f), (si, s), (mi, m) in itertools.product(enumerate(FAST), enumerate(SLOW), enumerate(MULT)):
        if s >= 2 * f:
            combos.append((fi, si, mi, f, s, m))
    return combos


def smoothed(scores: dict[tuple[int, int, int], float]) -> dict[tuple[int, int, int], float]:
    """每个组合的得分 = 自己及周围 3x3x3 邻域(各维 ±1)共 27 个格子得分的平均。

    - 网格内但无效(成交太少)的邻居按"悲观下限"计入:周围开不了仓说明这片区域不稳定,
      不能因为"无效"就忽略它,否则被无效邻居包围的孤立尖峰反而会胜出。
      下限 = min(0, 全网格有效得分的最小值),保证它永远不优于任何有效得分——
      若简单按 0 分计,在得分为负的区域里无效邻居反而显得"更好"。
    - 邻域不完整的格子(网格边界,或紧挨 slow >= 2 x fast 过滤出的空洞)**不参与选参**:
      边缘格子的邻居更少、更近,平均值会系统性偏高;用自身得分补齐则会让边缘尖峰占便宜。
      最优点落在边界通常也说明搜索范围太窄,不应当被选中。
    """
    out = {}
    valid = [v for v in scores.values() if v != float("-inf")]
    floor = min(0.0, min(valid)) if valid else 0.0
    for (fi, si, mi), own in scores.items():
        if own == float("-inf"):
            continue
        positions = [(fi + a, si + b, mi + c) for a, b, c in itertools.product((-1, 0, 1), repeat=3)]
        if any(pos not in scores for pos in positions):
            continue
        vals = [floor if scores[pos] == float("-inf") else scores[pos] for pos in positions]
        out[(fi, si, mi)] = sum(vals) / len(vals)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="CADJ", choices=["C", "MA", "CADJ", "MAADJ"],
                        help="C/MA = 新浪未复权主力连续(换月跳空会污染结果,仅供对照);CADJ/MAADJ = 复权连续合约")
    parser.add_argument("--test", action="store_true", help="最终确认:查看测试段(只应运行一次)")
    args = parser.parse_args()
    key = args.symbol
    if args.test:
        try:
            segment_ledger.check(segment_key(key))
        except segment_ledger.SegmentAlreadyUsed as exc:
            raise SystemExit(f"拒绝运行 --test: {exc}")

    combos = grid()
    windows = windows_for(key)
    print(f"[{key}] 训练段 {windows['train'][0]:%Y-%m-%d}~{windows['train'][1]:%Y-%m-%d},共 {len(combos)} 组参数")

    rows, scores = [], {}
    for fi, si, mi, f, s, m in combos:
        params = {"fast_window": f, "slow_window": s, "atr_mult": m}
        res = evaluate(key, params, "train")
        scores[(fi, si, mi)] = valid_score(res)
        rows.append({**params, **res, "score": scores[(fi, si, mi)], "grid": (fi, si, mi)})

    smooth = smoothed(scores)
    if not smooth:
        print("没有任何参数组合同时满足最少成交笔数与完整邻域,放弃优化。")
        return
    for r in rows:
        r["smoothed"] = smooth.get(r.pop("grid"), float("-inf"))

    df = pd.DataFrame(rows).sort_values("smoothed", ascending=False)
    out_dir = bootstrap.ROOT / "data" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_dir / f"optimize_{key}_train.csv", index=False)

    pd.set_option("display.width", 200)
    print("\n训练段 Top 8(按邻域平均夏普排序):")
    print(df.head(8)[["fast_window", "slow_window", "atr_mult", "sharpe", "smoothed", "return_pct", "maxdd_pct", "round_trips"]].to_string(index=False))
    valid_rows = df[df["score"] != float("-inf")]
    print(f"\n有效组合 {len(valid_rows)}/{len(df)};训练段夏普为正的占 {(valid_rows['sharpe'] > 0).mean():.0%},"
          f"中位数 {valid_rows['sharpe'].median():.2f}")

    best = df.iloc[0]
    params = {"fast_window": int(best.fast_window), "slow_window": int(best.slow_window), "atr_mult": float(best.atr_mult)}
    print(f"\n选定参数(训练段邻域平均最优): {params}")

    if args.test:  # 先登记后运行:结果一旦被看到,该测试段即算已使用
        segment_ledger.record(segment_key(key), note=f"optimize.py --symbol {key} --test 参数 {params}")
    for window in ("train", "valid") + (("test",) if args.test else ()):
        res = evaluate(key, params, window)
        tag = "  <-- 最终确认,只看这一次" if window == "test" else ""
        if window != "train" and res["round_trips"] < MIN_ROUND_TRIPS_NOTE:
            tag += f"  [!] 往返仅 {res['round_trips']} 笔,统计无意义,只能说'无法证明优势'"
        print(f"  {window:5s} {windows[window][0]:%Y}~{windows[window][1]:%Y}: 收益 {res['return_pct']:7.1f}%  "
              f"最大回撤 {res['maxdd_pct']:7.1f}%  夏普 {res['sharpe']:5.2f}  往返 {res['round_trips']:3d} 笔  "
              f"期末 {res['end_balance']:8.0f}{tag}")
    if not args.test:
        print("\n(测试段未查看。参数确认无误后再加 --test 运行一次。)")


if __name__ == "__main__":
    main()
