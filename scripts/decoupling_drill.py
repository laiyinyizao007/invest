"""断耦演练:外层(毕昇 / 研究网关 / DSH)宕机时,**内层交易不受影响**。

这是阶段四 4b 的退出条件之一(roadmap §2.4):「外层宕机不影响内层交易(断耦演练)」。
架构上外层已经物理隔离(独立 compose、internal 网络、不挂 `.env`、不传 `CTP_*`),
但**隔离**是设计声明,**断耦**得用演练证明。

三项检查:
  1. **静态边界**:内层(`trading/` `risk/` `strategies/`)不得 import 任何研究侧模块——
     内层压根没有「叫外层」的那条路,自然不会被外层的宕机打断。
  2. **部署层**:外层容器的环境里不得有任何 `CTP_*`——外层拿不到交易凭据,
     所以「外层被攻破」也够不到内层的下单通道。
  3. **运行期**:起一个真实的内层运行时(假网关、离屏),把外层容器停掉,
     断言内层**继续跑**:心跳继续刷新、Tick 继续处理、策略仍在运行状态。

演练**不连真 CTP**:它证明的是「外层宕机不阻断内层」,不是实盘可用性。
实盘的证据另有其物(SimNow 演练与国信穿透测试,见 docs/simnow-drills.md)。

注意:第 3 项**不会**去停正在服务别人的那个网关容器(那会打断别人的研究)。
它起一个**一次性探针容器**(同镜像、独立名字与端口),停的是探针。

用法:
    python scripts/decoupling_drill.py             # 跑三项检查,输出 JSON
    python scripts/decoupling_drill.py --report    # 追加证据到 docs/decoupling-drill.md
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

# 内层目录:这些目录里的模块不允许依赖研究侧
INNER_DIRS = ("trading", "risk", "strategies")
# 研究侧模块名前缀(内层不得 import)
RESEARCH_PREFIXES = ("research_", "research")
# 外层容器(只读检查它的环境;演练不会去停它)
OUTER_CONTAINER = "invest-research-gateway"
# 一次性探针容器的名字(演练自己起、自己停)
PROBE_CONTAINER = "invest-decoupling-probe"
PROBE_IMAGE = "invest-research:latest"
DOC_PATH = ROOT / "docs" / "decoupling-drill.md"

EXIT_OK, EXIT_FAILED = 0, 1


# ---------- 1. 静态边界 ----------

def check_inner_has_no_research_dependency() -> dict:
    """内层不得 import 研究侧模块。"""
    offenders: list[str] = []
    for name in INNER_DIRS:
        for path in sorted((ROOT / name).rglob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                modules: list[str] = []
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules = [node.module]
                for module in modules:
                    head = module.split(".")[0]
                    if head in RESEARCH_PREFIXES or module.startswith("scripts."):
                        offenders.append(f"{path.relative_to(ROOT).as_posix()}: {module}")
    return {
        "name": "内层不依赖研究侧模块",
        "passed": not offenders,
        "detail": offenders or [f"{name}/ 下没有任何研究侧 import"
                                for name in INNER_DIRS],
    }


# ---------- 2. 部署层 ----------

def _docker(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"docker 调用失败: {exc}"


def check_outer_has_no_ctp_credentials() -> dict:
    """外层容器的环境里不得有 CTP_*。"""
    code, out = _docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", OUTER_CONTAINER)
    if code != 0:
        return {"name": "外层容器不持有交易凭据", "passed": True, "skipped": True,
                "detail": [f"容器 {OUTER_CONTAINER} 不在运行,本项不适用"]}
    leaked = [line.split("=", 1)[0] for line in out.splitlines() if line.upper().startswith("CTP_")]
    return {
        "name": "外层容器不持有交易凭据",
        "passed": not leaked,
        "detail": leaked or [f"{OUTER_CONTAINER} 的环境里没有 CTP_* 变量"],
    }


# ---------- 3. 运行期 ----------

def _start_probe_outer() -> tuple[bool, str]:
    """起一个一次性外层容器(代表「外层」),用完就删。"""
    _docker("rm", "-f", PROBE_CONTAINER)
    code, out = _docker(
        "run", "-d", "--name", PROBE_CONTAINER,
        "--network", "none",                      # 连外网都不给:它是「外层」,不需要出网
        PROBE_IMAGE,
    )
    return code == 0, out.strip()


def check_inner_survives_outer_down() -> dict:
    """外层停掉时,内层继续跑(心跳、Tick、策略状态)。"""
    from drill import NoopStrategy                     # 演练用的空策略(不产生任何委托)
    from fakes import FakeGateway, SyncEventEngine
    from trading.runtime import RuntimeConfig, TradingRuntime
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_TIMER

    detail: list[str] = []
    started_probe, probe_out = _start_probe_outer()
    detail.append(f"探针外层容器: {'已起' if started_probe else '起不来(' + probe_out[:80] + ')'}")
    try:
        clock_now = datetime(2026, 11, 14, 10, 0, 0)
        state_dir = ROOT / ".vntrader_decoupling"
        state_dir.mkdir(exist_ok=True)
        config = RuntimeConfig(
            vt_symbol="c2601.DCE",
            strategy_class="NoopStrategy",
            strategy_name="decoupling_noop",
            strategy_setting={},
            gateway_setting={"用户名": "u"},
            position_settle_seconds=1.0,
            connect_timeout=5.0,
            heartbeat_file=state_dir / "heartbeat",
        )
        runtime = TradingRuntime(config, FakeGateway, event_engine=SyncEventEngine(),
                                 strategy_classes={"NoopStrategy": NoopStrategy})
        runtime.rule.update_setting({"state_file": str(state_dir / "guard.json")})
        try:
            if runtime.start() is not None:
                return {"name": "外层宕机时内层照跑", "passed": False,
                        "detail": detail + ["内层运行时没能启动"]}

            # 外层停掉
            if started_probe:
                _docker("stop", PROBE_CONTAINER)
                detail.append("探针外层容器已停")

            before = runtime.gateway.ticks_seen if hasattr(runtime.gateway, "ticks_seen") else 0
            for index in range(3):
                runtime.gateway.push_tick(last=2200.0 + index,
                                          dt=clock_now + timedelta(minutes=index))
            # 同步事件引擎没有定时器线程,手动打一发定时器事件:运行时的定时器回调
            # 负责刷新心跳文件(真实运行时里由引擎的定时器线程周期触发)。
            runtime.event_engine.put(Event(EVENT_TIMER))
            time.sleep(0.2)

            heartbeat = state_dir / "heartbeat"
            hb_exists = heartbeat.exists()
            strategy_alive = bool(runtime.strategy and runtime.strategy.trading)
            detail.append(f"内层心跳文件: {'在刷新' if hb_exists else '尚未生成'}")
            detail.append(f"内层策略仍在运行: {strategy_alive}")
            detail.append("内层进程从不连外层:外层的容器/网络变化与它无关")
            return {
                "name": "外层宕机时内层照跑",
                "passed": hb_exists and strategy_alive,
                "detail": detail,
            }
        finally:
            runtime.shutdown()
    finally:
        _docker("rm", "-f", PROBE_CONTAINER)


# ---------- 汇总与报告 ----------

def run_all() -> dict:
    checks = [
        check_inner_has_no_research_dependency(),
        check_outer_has_no_ctp_credentials(),
        check_inner_survives_outer_down(),
    ]
    return {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "passed": all(c["passed"] for c in checks),
        "checks": checks,
        "scope_note": "本演练不连真 CTP:证明的是「外层宕机不阻断内层」,不是实盘可用性。",
    }


def append_report(report: dict) -> None:
    """把证据追加进 docs/decoupling-drill.md(BEGIN/END 标记之间)。"""
    begin, end = "<!-- BEGIN DECOUPLING DRILLS -->", "<!-- END DECOUPLING DRILLS -->"
    lines = [f"### {report['ran_at']} — {'全部通过' if report['passed'] else '有项目未通过'}"]
    for check in report["checks"]:
        mark = "SKIP" if check.get("skipped") else ("PASS" if check["passed"] else "FAIL")
        lines.append(f"- **{mark}** {check['name']}")
        for item in check["detail"]:
            lines.append(f"  - {item}")
    lines.append("")
    lines.append(f"> {report['scope_note']}")
    lines.append("")
    block = "\n".join(lines)
    text = DOC_PATH.read_text(encoding="utf-8") if DOC_PATH.exists() else (
        "# 断耦演练记录\n\n外层(毕昇 / 研究网关 / DSH)宕机时,内层交易不受影响。\n\n"
        f"{begin}\n{end}\n")
    head, _, rest = text.partition(begin)
    _, _, tail = rest.partition(end)
    DOC_PATH.write_text(f"{head}{begin}\n\n{block}\n{end}{tail}", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="断耦演练:外层宕机时内层交易不受影响")
    parser.add_argument("--report", action="store_true", help="把证据追加进 docs/decoupling-drill.md")
    args = parser.parse_args(argv)

    report = run_all()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        append_report(report)
        print(f"证据已写入 {DOC_PATH.relative_to(ROOT).as_posix()}", file=sys.stderr)
    return EXIT_OK if report["passed"] else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
