"""试验账本与批准账本的**人工**入口(Issue #31):status / migrate / reset。

    python scripts/research_ledger.py status
    python scripts/research_ledger.py migrate trials
    python scripts/research_ledger.py migrate approvals --confirm-all-approved
    python scripts/research_ledger.py reset trials --reason "账本被截断,按估计补记" --add-trials 2000 --force
    python scripts/research_ledger.py reset approvals --reason "链断,丢弃外部写入的记录" --force

为什么需要它:两本账本都有了哈希链 + 外置锚点(risk/ledger_chain.py),读侧发现链断/回滚/截断/无链记录会 fail-closed。
旧账本(没有链)不会被静默采信,要用 `migrate` 一次性升级;链断之后的人工出路是 `reset`
(保留链上仍然有效的前缀、按此重新定锚、原文件备份;重置不会找回丢失的内容)。

这是**给人用**的命令:网关、MCP、候选都没有任何路径调用它;每次重置/迁移都会在
`data/ledger_anchor/ledger.admin.log` 留一行审计记录(重置要求给出原因)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_ROOT = _SCRIPTS.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import research_config as rc  # noqa: E402
import research_sandbox as rs  # noqa: E402
from risk import ledger_admin as admin  # noqa: E402
from risk import ledger_chain as chain  # noqa: E402
from risk import trial_ledger as tl  # noqa: E402


def _paths() -> tuple[Path, Path]:
    cfg = rc.load()
    paths = rc.resolve_paths(cfg, rc.ROOT)
    return Path(paths["results_dir"]) / "trials.jsonl", Path(paths["approvals_file"])


def _health(which: str, path: Path) -> str | None:
    """None = 健康;否则返回原因。"""
    try:
        if which == "trials":
            tl.total_trials(path)
        else:
            rs._load_approvals(path)
    except (tl.TrialLedgerError, rs.SandboxError) as exc:
        return str(exc)
    return None


def _audit(entry: dict) -> None:
    log = chain.anchor_path_for(Path(_paths()[0])).parent / "ledger.admin.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="试验账本/批准账本的人工入口(status / migrate / reset)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="两本账本是否健康(只读)")
    mig = sub.add_parser("migrate", help="把无链的旧账本一次性升级成哈希链(原文件备份)")
    mig.add_argument("ledger", choices=("trials", "approvals"))
    mig.add_argument("--confirm-all-approved", action="store_true",
                     help="迁移批准记录必须加:表示人已核对过现有记录里每一个 sha 都是自己批准的")
    rst = sub.add_parser("reset", help="链断之后:保留有效前缀、重新定锚(原文件备份;不会找回丢失的内容)")
    rst.add_argument("ledger", choices=("trials", "approvals"))
    rst.add_argument("--reason", required=True, help="原因(必填,写进审计日志)")
    rst.add_argument("--force", action="store_true", help="账本状态正常时也重置")
    rst.add_argument("--add-trials", type=int, default=0, help="(仅 trials)按估计补记的试验次数,宁多勿少")
    args = parser.parse_args(argv)
    trials, approvals = _paths()
    targets = {"trials": trials, "approvals": approvals}
    try:
        if args.command == "status":
            out = {name: (_health(name, path) or "ok") for name, path in targets.items()}
        elif args.command == "migrate":
            if args.ledger == "approvals":
                if not args.confirm_all_approved:
                    print("拒绝: 迁移批准记录要加 --confirm-all-approved(人已核对现有每个 sha)", file=sys.stderr)
                    return 1
                out = admin.migrate_approvals(approvals)
            else:
                out = admin.migrate_trials(trials)
            _audit({"action": "migrate", "ledger": args.ledger, "result": out})
        else:
            if not args.reason.strip():
                print("拒绝: --reason 不能为空", file=sys.stderr)
                return 1
            if _health(args.ledger, targets[args.ledger]) is None and not args.force:
                print("拒绝: 账本状态正常,重置没有必要;确需重置请加 --force", file=sys.stderr)
                return 1
            if args.ledger == "approvals":
                if args.add_trials:
                    print("拒绝: --add-trials 只用于 trials", file=sys.stderr)
                    return 1
                out = admin.reset_approvals(approvals)
            else:
                out = admin.reset_trials(trials, add_trials=args.add_trials)
            _audit({"action": "reset", "ledger": args.ledger, "reason": args.reason[:200], "result": out})
    except (admin.AdminError, tl.TrialLedgerError, chain.LedgerChainError) as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
