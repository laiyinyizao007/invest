"""Walk-Forward 滚动优化器:严格时序切分下的训练段网格选参 + 验证段样本外外推。

与 optimize_15m.py 的分工:
- optimize_15m.py 是单窗口三段切分(训练/验证/测试),测试段已被基线消费过一次;
- 本脚本只在测试段起点(见 TEST_SEGMENT_START 常量)之前的区间做滚动窗口,
  **脚本内不存在任何触碰测试段数据的代码路径**,连 --test 之类的开关也不提供。
  三层隔离护栏:TEST_SEGMENT_START 硬断言、窗口反例测试、源码扫描测试。

方法学纪律:
1. 参数只在训练段选,取训练夏普 Top5 自动代入同一窗口的验证段;验证段不参与任何选参;
2. risk_budget=300 是策略类默认值(TrendAtrStrategy),参数字典里不传该键,预算永远生效;
   每次回测报告 skipped_by_budget(预算弃单笔数)与弃单率;
3. atr_mult 固定类默认 1.5,不进网格(网格只扫 fast/slow 两个维度,见 docs/plans/20261110-walk-forward-optimizer-plan.md D2);
4. 夏普按 vn.py 自然日分组 ×240 年化口径,15 分钟数据上只能做同口径排序,不可解释为年化收益。

用法(项目根目录):
    python scripts/walk_forward_optimizer.py                    # 多进程跑全部滚动窗口
    python scripts/walk_forward_optimizer.py --workers 1        # 串行(调试/测试降级路径)
    python scripts/walk_forward_optimizer.py --data <CSV 路径>  # 换数据文件
"""
import argparse
import math
import os
import pickle
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import bootstrap  # noqa: F401  必须先于 vnpy 导入

import pandas as pd
from vnpy.trader.constant import Exchange

from backtest_lib import load_bars_csv, run_engine_bars
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

# ---------- 测试段隔离护栏(第 1 层:常量 + 硬断言) ----------
# 本常量是全脚本唯一允许出现测试段年份字面量的位置(源码扫描测试锁定此约束);
# 任何窗口的结束日期必须严格早于它。
TEST_SEGMENT_START = datetime(2024, 1, 1)

DATA_FILE = Path(r"C:\Dev\data\invest\derived\C_15m_adj.csv")
KEY = "C15"
SYMBOL = "CADJ15"

# 网格只扫均线快慢两维;atr_mult 固定 1.5(策略类默认),risk_budget 不传键、固定 300。
FAST = [10, 14, 18, 22, 26, 30]
SLOW = [40, 50, 60, 70, 80, 90]
ATR_MULT = 1.5
MIN_FILLS = 100        # 与 optimize_15m.py 同口径:成交笔数(开+平)少于此值不参与排名
TOP_N = 5
DEFAULT_OUTPUT = "data/results/walk_forward_C15.csv"


@dataclass(frozen=True)
class Window:
    """一个 Walk-Forward 窗口:训练段 + 紧随其后、互不重叠的验证段。"""

    name: str
    train_start: datetime
    train_end: datetime
    valid_start: datetime
    valid_end: datetime

    def segment(self, kind: str) -> tuple[datetime, datetime]:
        if kind == "train":
            return self.train_start, self.train_end
        if kind == "valid":
            return self.valid_start, self.valid_end
        raise ValueError(f"未知区段 {kind!r},只接受 train/valid")

    def __post_init__(self) -> None:
        # 纵深防御收口到数据类本身:外部直接构造的 Window 也无法绕过顺序与隔离校验。
        _validate_window(self)


def _assert_pre_test(start: datetime, end: datetime) -> None:
    """隔离断言:任何区段都必须完全落在测试段之前,且起点不晚于终点。"""
    if not (start < end < TEST_SEGMENT_START):
        raise ValueError(
            f"窗口越界或方向错误:{start:%Y-%m-%d}~{end:%Y-%m-%d},"
            f"所有区段必须严格早于测试段起点 {TEST_SEGMENT_START:%Y-%m-%d}"
        )


def build_windows() -> list[Window]:
    """生成 3 个滚动窗口:训练 24 个月 / 验证 6 个月 / 步长 6 个月,全部止于 2023-12-31。"""
    windows = [
        Window(
            "W1",
            datetime(2020, 7, 22), datetime(2022, 6, 30, 23, 59),
            datetime(2022, 7, 1), datetime(2022, 12, 31, 23, 59),
        ),
        Window(
            "W2",
            datetime(2021, 1, 1), datetime(2022, 12, 31, 23, 59),
            datetime(2023, 1, 1), datetime(2023, 6, 30, 23, 59),
        ),
        Window(
            "W3",
            datetime(2021, 7, 1), datetime(2023, 6, 30, 23, 59),
            datetime(2023, 7, 1), datetime(2023, 12, 31, 23, 59),
        ),
    ]
    for w in windows:
        _validate_window(w)
    return windows


def _validate_window(w: "Window") -> None:
    _assert_pre_test(w.train_start, w.train_end)
    _assert_pre_test(w.valid_start, w.valid_end)
    if not w.train_end < w.valid_start:
        raise ValueError(f"{w.name} 训练段必须严格早于验证段,杜绝未来函数")


def _naive(value: datetime) -> datetime:
    """剥掉 tzinfo:worker 入参正常是 naive isoformat,带时区时归一化为 naive 再比较,避免 TypeError 绕过隔离断言。"""
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def grid() -> list[dict]:
    """fast/slow 二维网格,保留 slow >= 2*fast 过滤(与 optimize_15m.py 同口径)。"""
    combos = []
    for fast in FAST:
        for slow in SLOW:
            if slow >= 2 * fast:
                combos.append({"fast_window": fast, "slow_window": slow, "atr_mult": ATR_MULT})
    return combos


def rank(rows: list[dict]) -> list[dict]:
    """训练段排名:先过成交笔数门槛,再按确定性多级次序排序。

    次序:sharpe 降序 -> 回撤金额降序(vn.py 的 max_drawdown 带负号,越接近 0 回撤越小、越靠前)
    -> fills 降序 -> fast_window 升序 -> slow_window 升序。
    末两级保证完全平局时结果仍然可复现。
    """
    eligible = [
        r for r in rows
        if r["fills"] >= MIN_FILLS
        and r["sharpe"] is not None
        and not (isinstance(r["sharpe"], float) and math.isnan(r["sharpe"]))
    ]
    return sorted(
        eligible,
        key=lambda r: (
            -float(r["sharpe"]),
            -float(r["maxdd_yuan"]),
            -int(r["fills"]),
            int(r["params"]["fast_window"]),
            int(r["params"]["slow_window"]),
        ),
    )


@lru_cache(maxsize=4)
def _load_cached(data_file: str) -> list:
    """每个进程独立的 K 线缓存(按 CSV 路径),spawn 后新进程自行加载,不依赖父进程状态。"""
    return load_bars_csv(Path(data_file), SYMBOL, Exchange.DCE)


def _stats_to_result(params: dict, stats: dict, skipped: int) -> dict:
    """把 vn.py 统计字典映射为结果行;独立成函数以便单测奇数 fills 等边界。

    注意:段末仍持仓时 total_trade_count 为奇数,//2 向下取整,该笔开仓不计入往返,
    因此弃单率分母会少 1、弃单率被轻微高估——fills 原始值同时保留在结果里供核对。
    """
    round_trips = int(stats.get("total_trade_count", 0)) // 2
    # 弃单率分母 = 预算弃单 + 实际成功入场(每个往返对应一次入场);熔断/无信号的拦截不计入。
    entry_attempts = skipped + round_trips
    return {
        "params": params,
        "return_pct": stats.get("total_return", 0.0),
        "maxdd_pct": stats.get("max_ddpercent", 0.0),
        "maxdd_yuan": stats.get("max_drawdown", 0.0),
        "sharpe": stats.get("sharpe_ratio", 0.0),
        "round_trips": round_trips,
        "fills": stats.get("total_trade_count", 0),
        "end_balance": stats.get("end_balance", 0.0),
        "net_pnl": stats.get("total_net_pnl", 0.0),
        "total_cost": stats.get("total_commission", 0.0) + stats.get("total_slippage", 0.0),
        "skipped_by_budget": skipped,
        "budget_skip_rate": skipped / entry_attempts if entry_attempts else 0.0,
    }


def _backtest_one(data_file: str, params: dict, start_iso: str, end_iso: str,
                  cost_multiplier: float = 1.0) -> dict:
    """模块级 worker:入参全是基础类型(Windows spawn 可 pickle),内部自行加载 K 线。

    回测内核仍是 vn.py BacktestingEngine(经 backtest_lib.run_engine_bars 封装),
    用 run_engine_bars 而非 run_backtest_bars,是为了从 engine.strategy 读取预算弃单计数。
    """
    start, end = _naive(datetime.fromisoformat(start_iso)), _naive(datetime.fromisoformat(end_iso))
    _assert_pre_test(start, end)
    bars = [b for b in _load_cached(data_file) if start <= b.datetime <= end]
    # 物理读入的 CSV 可能含测试段行(整文件加载后再按窗口切),断言切片结果不越界,
    # 保证引擎/策略对象永远收不到测试段 K 线。
    if bars:
        assert max(b.datetime for b in bars) < TEST_SEGMENT_START, "回测切片泄漏测试段 K 线"
    engine = run_engine_bars(TrendAtr15mStrategy, params, KEY, bars, cost_multiplier=cost_multiplier)
    daily = engine.calculate_result()
    stats = engine.calculate_statistics(daily, output=False) or {}
    skipped = int(getattr(engine.strategy, "skipped_by_budget", 0))
    return _stats_to_result(params, stats, skipped)


def map_backtests(data_file: str, jobs: list[tuple[dict, datetime, datetime]],
                  workers: int, executor=None) -> list[dict]:
    """按 jobs 顺序回测并对齐返回。workers=1 进程内串行;workers>1 用进程池(可注入 executor 测试)。"""
    serialized = [(p, s.isoformat(), e.isoformat()) for p, s, e in jobs]
    if executor is not None:
        futures = [executor.submit(_backtest_one, data_file, p, s, e) for p, s, e in serialized]
        return [f.result() for f in futures]
    if workers <= 1:
        return [_backtest_one(data_file, p, s, e) for p, s, e in serialized]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_backtest_one, data_file, p, s, e) for p, s, e in serialized]
        return [f.result() for f in futures]


def _jobs(combos: list[dict], start: datetime, end: datetime) -> list[tuple[dict, datetime, datetime]]:
    return [(params, start, end) for params in combos]


def run_window(data_file: str, window: Window, workers: int, executor=None) -> dict:
    """单窗口全流程:训练段网格 -> Top5 -> 验证段外推。"""
    train_start, train_end = window.segment("train")
    valid_start, valid_end = window.segment("valid")
    combos = grid()
    train_rows = map_backtests(data_file, _jobs(combos, train_start, train_end), workers, executor)
    ranked = rank(train_rows)
    insufficient = len(ranked) < TOP_N
    top = ranked[:TOP_N]

    valid_rows = []
    if top:
        valid_rows = map_backtests(data_file, _jobs([r["params"] for r in top], valid_start, valid_end),
                                   workers, executor)
    return {
        "window": window,
        "train_rows": train_rows,
        "top": top,
        "valid_rows": valid_rows,
        "insufficient": insufficient,
    }


def _print_window(result: dict) -> None:
    window: Window = result["window"]
    print(f"\n=== {window.name}: 训练 {window.train_start:%Y-%m-%d}~{window.train_end:%Y-%m-%d}"
          f" | 验证 {window.valid_start:%Y-%m-%d}~{window.valid_end:%Y-%m-%d} ===")
    eligible = len(result["top"])
    if result["insufficient"]:
        print(f"  [!] INSUFFICIENT:满足成交笔数门槛(>={MIN_FILLS})的组合仅 {eligible} 个(<{TOP_N}),")
    if not result["top"]:
        print("      没有任何组合达标,本窗口跳过验证,不凑数、不放宽门槛。")
        return
    print(f"  训练段 Top{eligible}(按原始夏普多级排序):")
    for i, r in enumerate(result["top"], 1):
        p = r["params"]
        print(f"    {i}. fast={p['fast_window']:3d} slow={p['slow_window']:3d}  "
              f"训练夏普 {r['sharpe']:6.2f}  最大回撤 {r['maxdd_yuan']:8.0f} 元  成交 {r['fills']:4d} 笔"
              f"  预算弃单 {r['skipped_by_budget']:3d} 笔({r['budget_skip_rate']:.0%})")
    print("  验证段样本外表现:")
    for i, r in enumerate(result["valid_rows"], 1):
        p = r["params"]
        print(f"    {i}. fast={p['fast_window']:3d} slow={p['slow_window']:3d}  "
              f"验证夏普 {r['sharpe']:6.2f}  最大回撤 {r['maxdd_pct']:6.1f}%({r['maxdd_yuan']:8.0f}元)"
              f"  往返 {r['round_trips']:4d} 笔  预算弃单率 {r['budget_skip_rate']:.0%}")


def summarize(results: list[dict]) -> dict:
    """跨窗口聚合:只统计真正产生验证结果的窗口。

    当前钉住的 vn.py 版本在 calculate_statistics 内对统计量做 np.nan_to_num,
    真实引擎产不出 NaN;这里仍做 isfinite 过滤,防升级/口径变化后 NaN 毒化均值
    并静默压低正夏普占比。INSUFFICIENT 窗口(达标组合 < Top5)单独计数。
    """
    # vn.py 的 max_drawdown 带负号,"最差"= 数值最小(负得最多);用 inf 初始化避免全负时被 0 吞掉。
    top1_sharpes, worst_dd, skip_num, skip_den = [], float("inf"), 0, 0
    positive_windows = nonfinite_sharpes = insufficient_windows = 0
    for result in results:
        if not result["valid_rows"]:
            continue
        if result.get("insufficient"):
            insufficient_windows += 1
        top1 = result["valid_rows"][0]
        sharpe = float(top1["sharpe"])
        if not math.isfinite(sharpe):
            nonfinite_sharpes += 1
        else:
            top1_sharpes.append(sharpe)
            if sharpe > 0:
                positive_windows += 1
        for r in result["valid_rows"]:
            worst_dd = min(worst_dd, float(r["maxdd_yuan"]))
            skip_num += int(r["skipped_by_budget"])
            skip_den += int(r["skipped_by_budget"]) + int(r["round_trips"])
    n = len(top1_sharpes)
    series = pd.Series(top1_sharpes) if top1_sharpes else pd.Series(dtype=float)
    return {
        "windows_validated": n,
        "insufficient_windows": insufficient_windows,
        "nonfinite_sharpes_dropped": nonfinite_sharpes,
        "top1_sharpe_mean": float(series.mean()) if n else float("nan"),
        "top1_sharpe_median": float(series.median()) if n else float("nan"),
        "positive_sharpe_window_rate": positive_windows / n if n else float("nan"),
        "worst_validation_maxdd_yuan": worst_dd if math.isfinite(worst_dd) else float("nan"),
        "validation_budget_skip_rate": skip_num / skip_den if skip_den else 0.0,
    }


def _print_summary(summary: dict) -> None:
    print("\n=== 跨窗口汇总 ===")
    if not summary["windows_validated"]:
        print("  没有任何窗口产出有效验证结果(全部 INSUFFICIENT)。")
        return
    print(f"  已验证窗口数: {summary['windows_validated']}")
    print(f"  各窗口第一名验证夏普: 均值 {summary['top1_sharpe_mean']:.2f} / "
          f"中位数 {summary['top1_sharpe_median']:.2f}")
    print(f"  验证夏普为正的窗口占比: {summary['positive_sharpe_window_rate']:.0%}")
    print(f"  验证段最差最大回撤: {summary['worst_validation_maxdd_yuan']:.0f} 元")
    print(f"  验证段预算弃单率(300 元硬预算): {summary['validation_budget_skip_rate']:.0%}")
    if summary["insufficient_windows"]:
        print(f"  其中 {summary['insufficient_windows']} 个窗口达标组合不足 {TOP_N} 个(INSUFFICIENT,结论已打折)")
    if summary["nonfinite_sharpes_dropped"]:
        print(f"  [!] {summary['nonfinite_sharpes_dropped']} 个非有限夏普已从汇总剔除")
    print("  注:夏普为 vn.py 自然日分组 ×240 年化口径,仅作同口径排序,不可解释为年化收益。")


def _write_csv(results: list[dict], output: Path) -> bool:
    """落盘明细;全部窗口都没有有效组合时不写空文件,返回是否写入。"""
    rows = []
    for result in results:
        window: Window = result["window"]
        for segment, group in (("train", result["train_rows"]), ("valid", result["valid_rows"])):
            for r in group:
                rows.append({
                    "window": window.name, "segment": segment,
                    "fast_window": r["params"]["fast_window"],
                    "slow_window": r["params"]["slow_window"],
                    "atr_mult": r["params"]["atr_mult"],
                    **{k: v for k, v in r.items() if k != "params"},
                })
    if not rows:
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(output, index=False, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", default=str(DATA_FILE), help=f"15 分钟复权 K 线 CSV(默认 {DATA_FILE})")
    parser.add_argument("--workers", type=lambda v: positive_int(v, "--workers"),
                        default=min(4, os.cpu_count() or 1),
                        help="并行进程数(>=1);1 = 进程内串行(调试/测试降级路径)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"结果 CSV(默认 {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)

    windows = build_windows()
    combos = grid()
    print(f"[{KEY}] 数据 {Path(args.data).name};{len(windows)} 个滚动窗口,每窗口 {len(combos)} 组参数;"
          f"workers={args.workers};测试段隔离已启用(硬断言 + 源码扫描)")

    # 单进程池贯穿全部窗口与区段:worker 只 spawn 一次、每进程 CSV 只加载一次(lru_cache 跨窗口复用)。
    if args.workers == 1:
        results = [run_window(args.data, w, 1) for w in windows]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            results = [run_window(args.data, w, args.workers, executor=pool) for w in windows]
    for result in results:
        _print_window(result)
    summary = summarize(results)
    _print_summary(summary)

    output = Path(args.output)
    if not output.is_absolute():
        output = bootstrap.ROOT / output
    if _write_csv(results, output):
        print(f"\n明细已写入 {output}")
    else:
        print("\n没有任何有效回测结果,未写出 CSV。")
    return 0


def positive_int(raw: str, flag: str) -> int:
    """argparse 正整数校验:0/负数静默退化到串行属于隐藏行为,显式拒绝。"""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{flag} 必须 >= 1,收到 {raw}")
    return value


if __name__ == "__main__":
    sys.exit(main())


# ---------- 测试段隔离护栏(第 1 层补充:pickle 安全的静态自证) ----------
# Windows spawn 要求 worker 与入参可 pickle;此处供测试直接引用,避免逻辑散落在测试里。
def pickle_safe_worker_args() -> bytes:
    """返回一组标准 worker 入参的 pickle 结果,测试用它锁定 spawn 兼容性。"""
    sample_job = (str(DATA_FILE), grid()[0],
                  datetime(2022, 1, 1).isoformat(), datetime(2022, 6, 30).isoformat())
    return pickle.dumps(sample_job)
