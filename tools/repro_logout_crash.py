"""复现「重连之后退出登出段错误(退出码 139)」的宿主机侧循环脚本。

背景:docs/simnow-drills.md 记过一次——`drill.py all` 在 disconnect 重连成功约 7 秒后退出,
`graceful_exit.logout_ctp` 发 ReqUserLogout 时原生段错误。证据只有一次,单独跑 disconnect / hard-lock 退出都正常。
本脚本反复做「容器里跑演练 → 宿主机真实切断容器网络 → 恢复 → 演练结束退出」,逐轮记录退出码和 trace,
用来回答:这个崩溃能不能稳定复现、需要什么前提(只 disconnect?还是 all 里 hard-lock 之后?)。

用法(在仓库根目录;先 `docker compose build trader` 让镜像带上最新 scripts):
    python tools/repro_logout_crash.py --dry-run                       # 只打印计划,不碰 docker、不读凭据
    python tools/repro_logout_crash.py --front second --scenario disconnect --rounds 1   # 第二套 7x24,先试脚本本身
    python tools/repro_logout_crash.py --front first  --scenario all --rounds 3          # 第一套,交易时段(日盘 09:00 起)

约束:
- 只连 SimNow(drill.py 自带地址白名单,不在白名单直接拒绝)。凭据运行时从 .env.simnow.bak 读,只放进子进程环境变量,
  不出现在命令行/日志;不改 .env。
- `--scenario all` 会真的发 SimNow 委托(latency 开平 1 手、daily-loss、hard-lock),需要交易时段。
- 两次登录之间要隔 >=5 分钟,否则会被重复登录限流卡住(默认轮间隔 360 秒)。
- 不要和别的 drill/行情录制同时跑同一套前置机。
退出码:0 = 所有轮次完成且无崩溃;1 = 至少一轮崩溃(139/-11);2 = 参数/环境问题;3 = 有轮次异常结束(非崩溃)。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.simnow.bak"
HOST_DATA = ROOT / "runtime" / "data" / "repro"           # 挂载为容器内 /app/data/repro
CONTAINER_DATA = "/app/data/repro"
PORTS = {"first": (30001, 30011), "second": (40001, 40011)}
ENV_KEY = re.compile(r"^(CTP_|LIVE_VT_SYMBOL$|HISTORY_VT_SYMBOL$)")
CRASH_CODES = {139, -11, 3221225477}                       # SIGSEGV(shell/POSIX)与 Windows 访问冲突
SCENARIOS = ("disconnect", "all")


def load_simnow_env(path: Path = ENV_FILE) -> dict[str, str]:
    """读取 .env 风格文件里的 CTP_* / 合约变量。只返回需要的键;缺文件抛 FileNotFoundError。"""
    env: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if ENV_KEY.match(key):
            env[key] = value.strip().strip('"').strip("'")
    return env


def apply_front(env: dict[str, str], front: str) -> dict[str, str]:
    """把交易/行情地址的端口换成指定前置机(与 tools/simnow_gui.ps1 同一规则)。"""
    td_port, md_port = PORTS[front]
    out = dict(env)
    if "CTP_TD_ADDRESS" in out:
        out["CTP_TD_ADDRESS"] = re.sub(r":\d+\s*$", f":{td_port}", out["CTP_TD_ADDRESS"])
    if "CTP_MD_ADDRESS" in out:
        out["CTP_MD_ADDRESS"] = re.sub(r":\d+\s*$", f":{md_port}", out["CTP_MD_ADDRESS"])
    return out


def classify(exit_code: int | None) -> str:
    if exit_code is None:
        return "timeout"
    if exit_code in CRASH_CODES:
        return "CRASH"
    return "ok" if exit_code == 0 else "nonzero"


def build_command(container: str, env_keys: list[str], scenario: str, symbol: str, round_no: int, tag: str) -> list[str]:
    """docker compose run 命令。凭据只以「-e KEY」(不带值)传入,值从子进程环境继承,命令行里看不到。"""
    cmd = ["docker", "compose", "run", "--rm", "--no-deps", "--name", container]
    for key in sorted(env_keys):
        cmd += ["-e", key]
    cmd += ["--entrypoint", "python", "trader", "scripts/drill.py", scenario, "--confirm-simnow",
            "--external-cut", f"{CONTAINER_DATA}/{tag}_cut_{round_no}.json",
            "--trace", f"{CONTAINER_DATA}/{tag}_trace_{round_no}.log",
            "--out", f"{CONTAINER_DATA}/{tag}_result_{round_no}.md"]
    if symbol:
        cmd += ["--symbol", symbol]
    return cmd


def _run(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


def container_networks(container: str) -> list[str]:
    out = _run(["docker", "inspect", "-f", "{{range $k, $_ := .NetworkSettings.Networks}}{{$k}} {{end}}", container])
    return out.stdout.split() if out.returncode == 0 else []


class Cutter(threading.Thread):
    """等演练写出「请求切网」文件 → 断开容器网络 → 等 cut_seconds → 恢复。恢复放在 finally,出错也一定恢复。"""

    def __init__(self, request_file: Path, container: str, cut_seconds: float, give_up_after: float):
        super().__init__(daemon=True)
        self.request_file, self.container = request_file, container
        self.cut_seconds, self.give_up_after = cut_seconds, give_up_after
        self.cut_at: float | None = None
        self.restored_at: float | None = None
        self.error = ""
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        deadline = time.monotonic() + self.give_up_after
        while not self.request_file.exists():
            if self._halt.is_set() or time.monotonic() > deadline:
                self.error = "未等到切网请求" if not self._halt.is_set() else ""
                return
            time.sleep(0.5)
        nets = container_networks(self.container)
        if not nets:
            self.error = "找不到容器网络"
            return
        try:
            for net in nets:
                _run(["docker", "network", "disconnect", net, self.container])
            self.cut_at = time.time()
            print(f"  [{datetime.now():%H:%M:%S}] 已切断网络 {nets},{self.cut_seconds:.0f} 秒后恢复", flush=True)
            self._halt.wait(self.cut_seconds)
        finally:
            for net in nets:
                result = _run(["docker", "network", "connect", net, self.container])
                if result.returncode != 0 and "already exists" not in result.stderr:
                    self.error += f" 恢复 {net} 失败: {result.stderr.strip()[:200]}"
            self.restored_at = time.time()
            print(f"  [{datetime.now():%H:%M:%S}] 已恢复网络", flush=True)


def run_round(round_no: int, args, env: dict[str, str], tag: str) -> dict:
    container = f"invest-repro-{tag}-{round_no}"
    cmd = build_command(container, list(env), args.scenario, env.get("LIVE_VT_SYMBOL", ""), round_no, tag)
    request = HOST_DATA / f"{tag}_cut_{round_no}.json"
    request.unlink(missing_ok=True)
    child_env = {**os.environ, **env}
    started = time.time()
    print(f"\n=== 第 {round_no}/{args.rounds} 轮({args.scenario}) {datetime.now():%H:%M:%S} ===", flush=True)
    proc = subprocess.Popen(cmd, cwd=ROOT, env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    cutter = Cutter(request, container, args.cut_seconds, give_up_after=args.connect_wait)
    cutter.start()
    lines: list[str] = []
    reader = threading.Thread(target=lambda: [lines.append(l.rstrip("\n")) for l in proc.stdout], daemon=True)
    reader.start()
    try:
        exit_code: int | None = proc.wait(timeout=args.round_timeout)
    except subprocess.TimeoutExpired:
        _run(["docker", "kill", container])
        proc.kill()
        exit_code = None
    finally:
        cutter.stop()
        cutter.join(timeout=args.cut_seconds + 30)
    reader.join(timeout=5)
    # 容器若被崩溃带走,compose run --rm 会返回容器退出码;139 即 SIGSEGV
    record = {
        "round": round_no, "scenario": args.scenario, "front": args.front, "exit_code": exit_code, "verdict": classify(exit_code),
        "seconds": round(time.time() - started, 1),
        "cut_at": cutter.cut_at, "restored_at": cutter.restored_at, "cutter_error": cutter.error.strip(),
        "after_restore_seconds_to_exit": None if cutter.restored_at is None else round(time.time() - cutter.restored_at, 1),
        "trace": str(HOST_DATA / f"{tag}_trace_{round_no}.log"), "tail": lines[-12:],
    }
    print(f"  退出码 {exit_code} → {record['verdict']};恢复后 {record['after_restore_seconds_to_exit']} 秒退出", flush=True)
    return record


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):                  # Windows 控制台默认 cp1252,中文会抛 UnicodeEncodeError
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--front", choices=sorted(PORTS), default="second", help="SimNow 前置机:first(交易时段) / second(7x24)")
    parser.add_argument("--scenario", choices=SCENARIOS, default="disconnect")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--cut-seconds", type=float, default=240.0, help="切断网络的时长(<约 120 秒 CTP 无感知,不会触发断线)")
    parser.add_argument("--gap-seconds", type=float, default=360.0, help="轮间隔;两次登录需 >=300 秒")
    parser.add_argument("--connect-wait", type=float, default=600.0, help="等演练就绪并写出切网请求的最长时间")
    parser.add_argument("--round-timeout", type=float, default=2400.0, help="单轮总超时,超时 docker kill")
    parser.add_argument("--tag", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--dry-run", action="store_true", help="只打印计划;不读凭据、不碰 docker")
    args = parser.parse_args(argv)

    if args.rounds < 1 or args.cut_seconds < 130 or args.gap_seconds < 300:
        print("参数不合理:rounds>=1、cut-seconds>=130(否则触发不了断线)、gap-seconds>=300(否则登录被限流)", file=sys.stderr)
        return 2
    HOST_DATA.mkdir(parents=True, exist_ok=True)
    summary = HOST_DATA / "summary.jsonl"

    if args.dry_run:
        demo = build_command(f"invest-repro-{args.tag}-1", ["CTP_USER_ID", "CTP_PASSWORD"], args.scenario, "c2701.DCE", 1, args.tag)
        print(f"计划:{args.rounds} 轮 × {args.scenario},前置机 {args.front},每轮切网 {args.cut_seconds:.0f} 秒,轮间隔 {args.gap_seconds:.0f} 秒")
        print("命令(凭据只以 -e KEY 传入):\n  " + " ".join(demo))
        print(f"记录:{summary};trace/结果:{HOST_DATA}")
        return 0

    try:
        env = apply_front(load_simnow_env(), args.front)
    except FileNotFoundError:
        print(f"缺少 {ENV_FILE}", file=sys.stderr)
        return 2
    if not env.get("CTP_TD_ADDRESS"):
        print(".env.simnow.bak 里没有 CTP_TD_ADDRESS", file=sys.stderr)
        return 2
    if _run(["docker", "info"]).returncode != 0:
        print("docker 不可用", file=sys.stderr)
        return 2

    records: list[dict] = []
    for i in range(1, args.rounds + 1):
        record = run_round(i, args, env, args.tag)
        records.append(record)
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({**record, "tag": args.tag}, ensure_ascii=False) + "\n")
        if i < args.rounds:
            print(f"  等待 {args.gap_seconds:.0f} 秒(避开重复登录限流)...", flush=True)
            time.sleep(args.gap_seconds)

    crashes = sum(r["verdict"] == "CRASH" for r in records)
    print(f"\n汇总:{len(records)} 轮,崩溃 {crashes} 次,退出码 {[r['exit_code'] for r in records]}")
    if crashes:
        return 1
    return 0 if all(r["verdict"] == "ok" for r in records) else 3


if __name__ == "__main__":
    sys.exit(main())
