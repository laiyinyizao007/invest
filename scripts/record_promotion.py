"""记录晋级门槛的批准:**全部通过**才写出批准文件。

本脚本写**两份独立的记录**(Issue #37)——它们问的不是一件事:

| 记录 | 问的问题 | 谁消费 |
|---|---|---|
| `data/state/promotion_approval.json` | 「这个账号/这套系统能不能连生产 API」 | `run_headless.py::check_live_admission` |
| `data/state/approved_promotion.json` | 「这份**代码**配不配被装载执行」 | `risk/approved_guard.py`(运行时装载) |

装载门那份额外绑定**品种 / 字节 / 那一次评估**(key + sha256 + run_id):
它只授权**它被评估过的那一份代码、那个品种**。换个文件、改个品种,都不算数。

用法(项目根目录):
    # 只写准入门(连生产 API 的前置):
    python scripts/record_promotion.py --metrics candidate.json --strategy TrendAtr15m --approver 你的名字

    # 同时写装载门(运行时装载的前置):要给出字节、品种与那次评估的 run_id
    python scripts/record_promotion.py --metrics candidate.json --strategy my_candidate \
        --approver 你的名字 --sha256 <64位> --run-id 20261115-091234-abcdef \
        --key C15

`--key` 可以不给:脚本会从 `--run-id` 指向的评估结果文件(`data/research/<run_id>.json`)里取;
两者都给了却不一致 → 拒绝(fail-closed)。

candidate.json 的字段与 `risk/promotion_gate.py::CandidateMetrics` 一致:
    oos_sharpe, oos_years, max_drawdown_pct, max_drawdown_yuan, round_trips,
    random_entry_percentile, stress_sharpe, data_adjusted, parameter_on_plateau

未通过时不写任何文件,逐条打印失败原因,退出码 1。
注意:通过门槛只代表"有资格上小额实盘",不代表策略有效;切换实盘仍需另行明确授权。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from risk.live_admission import (
    APPROVAL_RELATIVE,
    LOAD_APPROVAL_RELATIVE,
    write_approval,
    write_load_approval,
)
from risk.promotion_gate import CandidateMetrics

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_BAD_INPUT = 2


def _key_from_run(run_id: str, root: Path) -> str:
    """从那次评估的结果文件里取品种键(`data/research/<run_id>.json` 的 "key" 字段)。

    为什么要从评估结果取而不是让人手填:那份记录绑定的是「**哪一次评估、哪个品种**」,
    手填就可能填错(而这正是它要防的事)。
    """
    if not run_id:
        return ""
    path = root / "data" / "research" / f"{run_id}.json"
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    key = data.get("key") if isinstance(data, dict) else None
    return str(key).strip() if isinstance(key, str) else ""


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--metrics", type=Path, required=True, help="候选策略样本外指标 JSON")
    parser.add_argument("--strategy", required=True, help="策略名称(写入批准记录)")
    parser.add_argument("--approver", required=True, help="批准人(写入批准记录)")
    parser.add_argument("--out", type=Path, default=bootstrap.ROOT / APPROVAL_RELATIVE,
                        help="准入门批准文件路径")
    parser.add_argument("--load-out", type=Path, default=bootstrap.ROOT / LOAD_APPROVAL_RELATIVE,
                        help="装载门批准文件路径")
    parser.add_argument("--sha256", default="",
                        help="这份晋级通过对应**哪一段字节**(sha256);"
                             "运行时装载会核对它与实际要跑的文件是不是同一份")
    parser.add_argument("--run-id", default="",
                        help="**哪一次评估**(评估结果的 run_id);装载门要靠它追回来这次通过")
    parser.add_argument("--key", default="",
                        help="**哪个品种**(如 C15 / CADJ);不给就从 --run-id 的评估结果里取")
    args = parser.parse_args(argv)

    try:
        metrics = CandidateMetrics(**json.loads(args.metrics.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        print(f"指标文件无效:{exc!r}。字段必须与 CandidateMetrics 一致。", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not args.strategy.strip() or not args.approver.strip():
        print("--strategy 与 --approver 不能为空。", file=sys.stderr)
        return EXIT_BAD_INPUT

    stamp = now or datetime.now()
    failures = write_approval(args.out, args.strategy.strip(), metrics, args.approver.strip(),
                               stamp, sha256=args.sha256 or None)
    if failures:
        print("未通过晋级门槛,没有写出批准文件:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_GATE_FAILED
    print(f"已通过全部晋级门槛,准入门批准记录写入 {args.out}。切换实盘仍需另行明确授权。")

    # 装载门:只有给了字节才写(它必须绑定字节才有意义)
    if args.sha256.strip():
        key = args.key.strip() or _key_from_run(args.run_id.strip(), bootstrap.ROOT)
        if args.key.strip() and args.run_id.strip():
            from_run = _key_from_run(args.run_id.strip(), bootstrap.ROOT)
            if from_run and from_run != args.key.strip():
                print(f"拒绝: --key({args.key.strip()}) 与那次评估的品种({from_run})不一致。",
                      file=sys.stderr)
                return EXIT_BAD_INPUT
        if not key:
            print("拒绝: 写装载门批准记录需要品种 key(--key),"
                  "或一个能查到品种的 --run-id。", file=sys.stderr)
            return EXIT_BAD_INPUT
        if not args.run_id.strip():
            print("拒绝: 写装载门批准记录需要 --run-id(它绑定「哪一次评估」)。", file=sys.stderr)
            return EXIT_BAD_INPUT
        load_failures = write_load_approval(
            args.load_out, args.strategy.strip(), key, args.sha256.strip(), args.run_id.strip(),
            metrics, args.approver.strip(), stamp)
        if load_failures:
            print("装载门批准记录没有写出:", file=sys.stderr)
            for line in load_failures:
                print(f"  - {line}", file=sys.stderr)
            return EXIT_GATE_FAILED
        print(f"装载门批准记录写入 {args.load_out}(品种 {key},绑定这一次评估)。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
