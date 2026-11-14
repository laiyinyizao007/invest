"""梯子的人工入口:`status`(只读)、`renew`(显式续期,留痕)与 `reset`(从头开始,只留给人)。

用法:
    python scripts/research_ladder.py status
    python scripts/research_ladder.py renew --reason "第 1 批候选已复核,续 60 次"
    python scripts/research_ladder.py reset --reason "结果目录只恢复了一半,梯子对不上账" --force

`reset` 会把梯子日志、头指针、锚点**与全部历史揭示存档**一起归档(改名加 `.bak-<序号>`),
`best` 与查询数归零——**已经泄漏出去的信息收不回来**,所以它是比 `renew` 更重的操作:
梯子状态正常时默认拒绝,必须显式 `--force`;被拒绝的尝试也会写进 `ladder.reset.log` 留痕。
(第 20 轮 codex 的 L6:此前这段用法只列了 `status` 与 `renew`,`reset --force` 的存在与代价都没写。)

为什么需要它(第 15 轮 codex 的 High):梯子的总查询预算用尽后,续期原先只能手工编辑状态文件——
续期与回滚无法区分,而且没有审计钩子。现在状态是带哈希链的日志(risk/ladder.py),手工编辑会被当成篡改而 fail-closed;
续期只能走这里,每次追加一条 `renew` 事件:`queries` 归零、**`best` 保留**(已经泄漏的信息收不回来)、必须给出非空原因。

这是**给人用**的命令:进化回路与网关没有任何路径可以调用它(网关只有 /research/* 白名单路由)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import bootstrap  # noqa: F401  (必须先于 vnpy;这里不用 vnpy,但与同目录脚本保持同一入口约定)
import research_api as api
from risk.ladder import Ladder, LadderError, anchor_path_for


def ladder_for(paths: dict) -> Ladder:
    return Ladder(Path(paths["results_dir"]) / "ladder" / "ladder.json", anchor=anchor_path_for(paths["results_dir"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="梯子的人工入口(只读 status;显式 renew,留痕;reset 从头开始——最重的一项,只留给人)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="当前汇报值与剩余查询数(不消耗查询)")
    renew = sub.add_parser("renew", help="续期:queries 归零、保留 best,留审计记录")
    renew.add_argument("--reason", required=True, help="续期原因(必填,写进审计日志,至多 200 字符)")
    renew.add_argument("--force", action="store_true", help="预算还没用尽也续期(审计日志里标注 [强制])")
    reset = sub.add_parser("reset", help="从头开始:归档日志/头指针/锚点/揭示存档(best 与查询数都归零,只留给人)")
    reset.add_argument("--reason", required=True, help="原因(必填,写进 ladder.reset.log,至多 200 字符)")
    reset.add_argument("--force", action="store_true",
                       help="梯子状态正常时也重置(重置会清零 best 与查询数,并作废全部历史揭示存档)")
    args = parser.parse_args(argv)
    _, paths = api.settings()
    ladder = ladder_for(paths)
    try:
        if args.command == "status":
            out = ladder.status()
        elif args.command == "reset":
            out = ladder.reset(args.reason, force=args.force,
                               outward=Path(paths["results_dir"]) / "outward")
        else:
            out = ladder.renew(args.reason, force=args.force)
    except LadderError as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
