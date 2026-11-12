"""记录实盘准入批准:把候选策略的样本外指标送进晋级门槛,**全部通过才**写出批准文件。

用法(项目根目录):
    python scripts/record_promotion.py --metrics candidate.json --strategy TrendAtr15m --approver 你的名字

candidate.json 的字段与 `risk/promotion_gate.py::CandidateMetrics` 一致:
    oos_sharpe, oos_years, max_drawdown_pct, max_drawdown_yuan, round_trips,
    random_entry_percentile, stress_sharpe, data_adjusted, parameter_on_plateau

批准文件 `data/state/promotion_approval.json` 是 `scripts/run_headless.py` 连接生产 API 前的必要条件
(SimNow 与看穿式测试环境不需要)。未通过时不写文件,逐条打印失败原因,退出码 1。
注意:通过门槛只代表"有资格上小额实盘",不代表策略有效;切换实盘仍需另行明确授权。
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from risk.live_admission import APPROVAL_RELATIVE, write_approval
from risk.promotion_gate import CandidateMetrics

EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_BAD_INPUT = 2


def main(argv: list[str] | None = None, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--metrics", type=Path, required=True, help="候选策略样本外指标 JSON")
    parser.add_argument("--strategy", required=True, help="策略名称(写入批准记录)")
    parser.add_argument("--approver", required=True, help="批准人(写入批准记录)")
    parser.add_argument("--out", type=Path, default=bootstrap.ROOT / APPROVAL_RELATIVE, help="批准文件路径")
    args = parser.parse_args(argv)

    try:
        metrics = CandidateMetrics(**json.loads(args.metrics.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError) as exc:
        print(f"指标文件无效:{exc!r}。字段必须与 CandidateMetrics 一致。", file=sys.stderr)
        return EXIT_BAD_INPUT
    if not args.strategy.strip() or not args.approver.strip():
        print("--strategy 与 --approver 不能为空。", file=sys.stderr)
        return EXIT_BAD_INPUT

    failures = write_approval(args.out, args.strategy.strip(), metrics, args.approver.strip(), now or datetime.now())
    if failures:
        print("未通过晋级门槛,没有写出批准文件:", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        return EXIT_GATE_FAILED
    print(f"已通过全部晋级门槛,批准记录写入 {args.out}。切换实盘仍需另行明确授权。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
