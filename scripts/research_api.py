"""研究沙盒 CLI(阶段四 4a:人工驱动)。外层(人或将来的 Bisheng)与确定性内核之间唯一的通道。

子命令即端点白名单的 CLI 形态(docs/roadmap.md §2.2):
    data       列出可用数据段及其「测试段已消费」状态
    backtest   对 pending_strategies/ 内的策略在训练/验证段做沙盒回测
    results    读取结果摘要与诊断书(不带 RUN_ID 则列出全部)

明确不存在:下单、查账户、改风控参数、连 CTP、访问测试段。输出一律是 JSON,便于程序化消费。
威胁模型见 scripts/research_sandbox.py 文件头:这是纵深防御而非沙盒,运行前须由人读过代码。
退出码:0 成功;2 被拒绝/参数错误;3 回测失败;124 超时。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import bootstrap
import research_sandbox as rs

RESULTS_DIR = bootstrap.ROOT / "data" / "research"
CHILD_MARK = "RESEARCH_RESULT_JSON:"
EXIT_OK, EXIT_REFUSED, EXIT_FAILED, EXIT_TIMEOUT = 0, 2, 3, 124


def emit(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def refuse(message: str) -> int:
    print(f"拒绝: {message}", file=sys.stderr)
    return EXIT_REFUSED


# ---------- data ----------

def cmd_data(_args) -> int:
    import optimize
    import segment_ledger
    out = []
    for key in rs.KEYS:
        windows = optimize.windows_for(key)
        t_start, t_end = windows["test"]
        ledger_key = f"optimize:{key}:{t_start:%Y-%m-%d}~{t_end:%Y-%m-%d}"
        try:
            segment_ledger.check(ledger_key)
            consumed = False
        except segment_ledger.SegmentAlreadyUsed:
            consumed = True
        out.append({
            "key": key,
            "segments": {seg: [f"{windows[seg][0]:%Y-%m-%d}", f"{windows[seg][1]:%Y-%m-%d}"] for seg in rs.SEGMENTS},
            "test_segment_accessible_via_sandbox": False,
            "test_segment_consumed": consumed,
        })
    emit(out)
    return EXIT_OK


# ---------- backtest ----------

def kill_tree(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":      # venv 的 python.exe 是启动器,须杀整棵进程树
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def cmd_backtest(args) -> int:
    if not args.confirm_reviewed:
        return refuse("需要 --confirm-reviewed:策略代码运行前必须由人读过(见 research_sandbox.py 威胁模型)")
    try:
        path = rs.resolve_pending(args.strategy, bootstrap.ROOT)
        start, end = rs.segment_window(args.key, args.segment)
        params = json.loads(args.params)
        if not isinstance(params, dict):
            raise rs.SandboxError("--params 必须是 JSON 对象")
    except (rs.SandboxError, json.JSONDecodeError) as exc:
        return refuse(str(exc))
    source = path.read_text(encoding="utf-8")
    findings = rs.screen_source(source)
    if findings:
        print("拒绝: 静态筛查未通过:\n  " + "\n  ".join(findings), file=sys.stderr)
        return EXIT_REFUSED

    cmd = [sys.executable, str(Path(__file__).resolve()), "_child", "--file", str(path), "--cls", args.cls,
           "--key", args.key, "--segment", args.segment, "--params", json.dumps(params)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", cwd=bootstrap.ROOT, env=rs.sandbox_env(os.environ))
    try:
        stdout, stderr = proc.communicate(timeout=args.timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.communicate()
        print(f"回测超过 {args.timeout}s,已终止", file=sys.stderr)
        return EXIT_TIMEOUT
    line = next((ln for ln in reversed(stdout.splitlines()) if ln.startswith(CHILD_MARK)), None)
    if proc.returncode != 0 or line is None:
        print("回测失败:\n" + (stderr or stdout)[-1500:], file=sys.stderr)
        return EXIT_FAILED
    body = json.loads(line[len(CHILD_MARK):])

    from risk.diagnosis import diagnose
    result = {
        "strategy_file": path.name, "strategy_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "class": args.cls, "key": args.key, "segment": args.segment, "params": params,
        "window": [f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"], "ran_at": datetime.now().isoformat(timespec="seconds"),
        "stats": body["stats"], "diagnosis": diagnose(body["stats"], body["pnls"]),
    }
    result["run_id"] = rs.save_result(result, RESULTS_DIR)
    emit(result)
    return EXIT_OK


def run_child(args) -> int:
    """子进程:加载策略并回测。只在受限环境(sandbox_env)里由 cmd_backtest 拉起。"""
    from backtest_lib import run_backtest
    from strategies.instruments import INSTRUMENTS
    start, end = rs.segment_window(args.key, args.segment)
    if rs.screen_source(Path(args.file).read_text(encoding="utf-8")):         # 子进程再筛一次,防止文件在两步之间被替换
        print("静态筛查未通过", file=sys.stderr)
        return EXIT_REFUSED
    spec = importlib.util.spec_from_file_location("pending_strategy", args.file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_class = getattr(module, args.cls, None)
    from vnpy_ctastrategy import CtaTemplate
    if not (isinstance(strategy_class, type) and issubclass(strategy_class, CtaTemplate)):
        print(f"{args.cls} 不存在或不是 CtaTemplate 子类", file=sys.stderr)
        return EXIT_FAILED
    stats, _, trades = run_backtest(strategy_class, json.loads(args.params), args.key, start, end)
    stats = stats or {}
    row = {
        "return_pct": stats.get("total_return", 0.0), "maxdd_pct": stats.get("max_ddpercent", 0.0),
        "maxdd_yuan": stats.get("max_drawdown", 0.0), "sharpe": stats.get("sharpe_ratio", 0.0),
        "round_trips": int(stats.get("total_trade_count", 0)) // 2, "end_balance": stats.get("end_balance", 0.0),
        "net_pnl": stats.get("total_net_pnl", 0.0),
        "total_cost": stats.get("total_commission", 0.0) + stats.get("total_slippage", 0.0),
    }
    pnls = rs.round_trip_pnls(trades, INSTRUMENTS[args.key].size)
    print(CHILD_MARK + json.dumps({"stats": row, "pnls": pnls}))
    return EXIT_OK


# ---------- results ----------

def cmd_results(args) -> int:
    try:
        emit(rs.load_result(args.run_id, RESULTS_DIR) if args.run_id else rs.list_results(RESULTS_DIR))
    except rs.SandboxError as exc:
        return refuse(str(exc))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("data", help="列出数据段与测试段消费状态").set_defaults(func=cmd_data)
    bt = sub.add_parser("backtest", help="沙盒回测(仅训练/验证段)")
    bt.add_argument("--strategy", required=True, help="pending_strategies/ 内的 .py 文件")
    bt.add_argument("--class", dest="cls", required=True, help="策略类名")
    bt.add_argument("--key", required=True, choices=rs.KEYS)
    bt.add_argument("--segment", required=True, choices=rs.SEGMENTS)
    bt.add_argument("--params", default="{}", help="策略参数 JSON 对象")
    bt.add_argument("--timeout", type=float, default=600.0)
    bt.add_argument("--confirm-reviewed", action="store_true", help="确认策略代码已由人读过")
    bt.set_defaults(func=cmd_backtest)
    rr = sub.add_parser("results", help="读取结果与诊断书")
    rr.add_argument("run_id", nargs="?", default="")
    rr.set_defaults(func=cmd_results)
    child = sub.add_parser("_child")           # 内部使用
    for name in ("file", "cls", "key", "segment", "params"):
        child.add_argument(f"--{name}", required=True)
    child.set_defaults(func=run_child)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
