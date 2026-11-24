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


def _anchor_ledgers() -> dict:
    trials, approvals = _paths()
    import research_attempts as ra
    return {
        "trials": (trials, lambda p: tl.total_trials(p)),
        "attempts": (trials.parent / "attempts.jsonl", lambda p: ra.read(p)),
        "approvals": (approvals, lambda p: rs._load_approvals(p)),
        "auto_approvals": (approvals.parent / "auto_approvals.json", lambda p: rs._load_approvals(p)),
    }


def _describe_anchor(path: Path, anchor: Path) -> str:
    got = chain.read_anchor(anchor)
    rows = 0
    if path.exists():
        rows = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if got is None:
        return f"锚点不可读;账本 {rows} 行"
    seq, head, total = got
    return f"账本 {rows} 行;锚点 seq={seq} head={head[:12]}… 累计={total}"


def _rename_anchors(apply: bool) -> dict:
    """Issue #73:把旧口径(绝对路径哈希)命名的锚点换成新口径名,内容不动。

    **默认只预演(不改任何文件)**,把每本账本"账本多少行 / 锚点 seq、头哈希、累计次数"列给人核对;
    加 `--apply` 才执行。原因(codex 复审,已实测):本命令只能证明"账本与这份锚点自洽",证明不了
    "这份锚点是真的"——被一并改过的账本 + 植入的旧名锚点会通过校验。所以必须由人对照自己记得的数字确认。
    执行时**复制**而不是改名:旧锚点原样保留到新锚点核对通过之后才改成 `.migrated`;任何失败(含 Ctrl-C)
    都只删掉刚写的新锚点,回到执行前的状态。新名已存在的也要核对,不再无条件报"已迁移"。"""
    out: dict = {"apply": apply}
    failed = False
    for name, (path, check) in _anchor_ledgers().items():
        new, old = chain.anchor_path_for(path), chain.legacy_anchor_path_for(path)
        if new.exists():
            try:
                check(path)
                out[name] = "新口径锚点已存在,核对通过:" + _describe_anchor(path, new)
            except Exception as exc:
                out[name] = f"新口径锚点已存在但核对不过(请人工核对,必要时 reset): {str(exc)[:160]}"
                failed = True
            continue
        if not old.exists():
            out[name] = "无旧锚点(账本为空或从未建过)" if not path.exists() else "没有旧锚点,账本有记录:需人工 reset"
            failed = failed or path.exists()
            continue
        if not apply:
            out[name] = "待迁移:" + _describe_anchor(path, old)
            continue
        try:
            new.write_bytes(old.read_bytes())
            check(path)
        except BaseException as exc:                  # 含 KeyboardInterrupt:一律回到执行前
            try:
                new.unlink(missing_ok=True)
            except OSError:
                pass
            out[name] = f"核对失败,已撤销新锚点: {str(exc)[:160]}"
            failed = True
            if not isinstance(exc, Exception):
                out["ok"] = False
                _audit({"action": "rename-anchors", "result": out})
                raise
            continue
        try:
            old.rename(old.with_name(old.name + ".migrated"))
        except OSError as exc:
            out[name] = f"已迁移并核对通过,但旧锚点没能改名({str(exc)[:80]});可手工删除旧文件"
        else:
            out[name] = "已迁移并核对通过:" + _describe_anchor(path, new)
    out["ok"] = not failed
    return out


def _audit(entry: dict) -> None:
    log = chain.anchor_path_for(Path(_paths()[0])).parent / "ledger.admin.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="试验账本/批准账本的人工入口(status / migrate / reset)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="两本账本是否健康(只读)")
    ren = sub.add_parser("rename-anchors", help="把旧口径(绝对路径哈希)的锚点换成新口径名(默认只预演;加 --apply 才执行)")
    ren.add_argument("--apply", action="store_true", help="执行迁移(先看预演输出,对照你记得的次数/行数)")
    mig = sub.add_parser("migrate", help="把无链的旧账本一次性升级成哈希链(原文件备份)")
    mig.add_argument("ledger", choices=("trials", "approvals"))
    mig.add_argument("--confirm-all-approved", action="store_true",
                     help="迁移批准记录必须加:表示人已核对过现有记录里每一个 sha 都是自己批准的")
    rst = sub.add_parser("reset", help="链断之后:保留有效前缀、重新定锚(原文件备份;不会找回丢失的内容)")
    rst.add_argument("ledger", choices=("trials", "approvals"))
    rst.add_argument("--reason", required=True, help="原因(必填,写进审计日志)")
    rst.add_argument("--force", action="store_true", help="账本状态正常时也重置")
    rst.add_argument("--add-trials", type=int, default=0, help="(仅 trials)按估计补记的试验次数,宁多勿少")
    rst.add_argument("--allow-reduce-n", action="store_true",
                     help="(仅 trials)确认允许重置后累计试验次数变小(N 变小会让 DSR 的分母变小、去通胀失效;"
                          "默认拒绝,必须显式确认)")
    args = parser.parse_args(argv)
    trials, approvals = _paths()
    targets = {"trials": trials, "approvals": approvals}
    try:
        if args.command == "status":
            out = {name: (_health(name, path) or "ok") for name, path in targets.items()}
        elif args.command == "rename-anchors":
            out = _rename_anchors(args.apply)
            if args.apply:
                _audit({"action": "rename-anchors", "result": out})
            if not out["ok"]:
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 1
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
                if args.allow_reduce_n:
                    print("拒绝: --allow-reduce-n 只用于 trials", file=sys.stderr)
                    return 1
                out = admin.reset_approvals(approvals)
            else:
                out = admin.reset_trials(trials, add_trials=args.add_trials,
                                         allow_reduce_n=args.allow_reduce_n, reason=args.reason)
            _audit({"action": "reset", "ledger": args.ledger, "reason": args.reason[:200], "result": out})
    except (admin.AdminError, tl.TrialLedgerError, chain.LedgerChainError) as exc:
        print(f"拒绝: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
