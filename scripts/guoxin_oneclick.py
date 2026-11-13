"""国信测试一键流程:检查 → 切国信档案 → 取证运行 → 生成报告 → 列出还要人工做的事。

用法(项目根目录或双击 scripts/guoxin_oneclick.cmd):
    python scripts/guoxin_oneclick.py                 # 完整流程(含 3.1 断网演练,需要你动手)
    python scripts/guoxin_oneclick.py --no-interactive   # 不做断网演练(3.1 标「待人工」)
    python scripts/guoxin_oneclick.py --resume data/broker_test/<目录>   # 在已有证据上补测并重新出报告
    python scripts/guoxin_oneclick.py --check-only    # 只做检查,不登录、不发单

安全:只放行国信测试环境(CTP_ENVIRONMENT=测试、BrokerID 8030、前置在国信给的地址表内);
不通过就退出,不会连接。一次流程只登录一次(国信会检查频繁登录),所以不自动重试。
不打印账号、密码、授权码。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
BEIJING = ZoneInfo("Asia/Shanghai")
EXPECTED_BROKER_ID = "8030"
# 国信提供的仿真互联网前置(CTP仿真互联网.txt):电信/联通
ALLOWED_TD = {"121.13.218.19:41285", "120.86.114.57:41285"}
ALLOWED_MD = {"121.13.218.19:41293", "120.86.114.57:41293"}
TEMPLATE_NAME = "国信期货有限责任公司外部接入系统技术测试报告20260717.docx"
# 当前交易时段(北京时间,分钟):日盘 9:00-10:15 / 10:30-11:30 / 13:30-15:00,夜盘 21:00-23:00(玉米)
SESSIONS = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60 + 30, 15 * 60), (21 * 60, 23 * 60))


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            out[key.strip()] = value.strip()
    return out


def check_env(env: dict[str, str]) -> list[str]:
    """返回不满足「国信测试环境」的问题列表;空 = 通过。"""
    problems = []
    if env.get("CTP_ENVIRONMENT") != "测试":
        problems.append(f"CTP_ENVIRONMENT 应为「测试」,当前 {env.get('CTP_ENVIRONMENT')!r}")
    if env.get("CTP_BROKER_ID") != EXPECTED_BROKER_ID:
        problems.append(f"CTP_BROKER_ID 应为 {EXPECTED_BROKER_ID},当前 {env.get('CTP_BROKER_ID')!r}")
    if env.get("CTP_TD_ADDRESS", "").replace("tcp://", "") not in ALLOWED_TD:
        problems.append(f"交易前置 {env.get('CTP_TD_ADDRESS')!r} 不在国信测试地址表 {sorted(ALLOWED_TD)} 内")
    if env.get("CTP_MD_ADDRESS", "").replace("tcp://", "") not in ALLOWED_MD:
        problems.append(f"行情前置 {env.get('CTP_MD_ADDRESS')!r} 不在国信测试地址表 {sorted(ALLOWED_MD)} 内")
    for key in ("CTP_USERNAME", "CTP_PASSWORD", "CTP_APP_ID", "CTP_AUTH_CODE", "LIVE_VT_SYMBOL"):
        if not env.get(key):
            problems.append(f"缺少 {key}")
    return problems


def in_session(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in SESSIONS)


def main_repo() -> Path | None:
    """worktree 里没有 .env/.env.guoxin/模板时,回主仓库(同级 invest 目录)找。"""
    candidate = ROOT.parent / "invest"
    return candidate if candidate.is_dir() and candidate.resolve() != ROOT.resolve() else None


def find_file(*relative: str) -> Path | None:
    for base in (ROOT, main_repo()):
        if base is not None:
            for rel in relative:
                if (base / rel).exists():
                    return base / rel
    return None


def python_exe() -> str:
    """优先项目 venv(worktree 没有就用主仓库的),否则当前解释器。"""
    for base in (ROOT, main_repo()):
        if base is not None:
            exe = base / ".venv" / "Scripts" / "python.exe"
            if exe.exists():
                return str(exe)
    return sys.executable


def step(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-interactive", action="store_true", help="不做 3.1 断网演练")
    parser.add_argument("--resume", type=Path, default=None, help="在已有证据目录上补测")
    parser.add_argument("--check-only", action="store_true", help="只检查,不登录不发单")
    parser.add_argument("--yes", action="store_true", help="跳过「前置地址确认」提示(非交互环境用)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")

    step("1/5 检查")
    now = datetime.now(BEIJING)
    print(f"北京时间 {now:%Y-%m-%d %H:%M}(本机时间可能不同,以北京时间为准)")
    if not in_session(now.replace(tzinfo=None)):
        print("⚠ 现在不在交易时段:发单类测试项(稳定运行、开平撤、撤单阈值、错误提示、暂停)会标「未测试」。"
              "建议北京时间 21:10–22:30(夜盘)或日盘开盘后再跑;如只是演练可继续。")
        if not args.check_only and not args.yes and input("仍要继续?输入 y 继续:").strip().lower() != "y":
            return 1
    source = find_file(".env.guoxin")
    if source is None:
        print("找不到 .env.guoxin(国信测试档案)。", file=sys.stderr)
        return 2
    env = read_env(source)
    problems = check_env(env)
    if problems:
        print("国信档案检查未通过,已退出(没有连接任何环境):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 2
    print(f"国信档案 {source} 检查通过:环境=测试 BrokerID={env['CTP_BROKER_ID']} 交易={env['CTP_TD_ADDRESS']} 行情={env['CTP_MD_ADDRESS']}")
    template = find_file(f"docs/{TEMPLATE_NAME}")
    if template is None:
        print(f"找不到报告模板 docs/{TEMPLATE_NAME}", file=sys.stderr)
        return 2
    print(f"报告模板 {template}")
    if args.check_only:
        print("--check-only:到此为止。")
        return 0

    step("2/5 切换到国信档案")
    target = ROOT / ".env"
    if target.exists() and target.read_bytes() != source.read_bytes():
        shutil.copy2(target, ROOT / ".env.prev")
        print("当前 .env 与国信档案不同,已另存为 .env.prev")
    shutil.copy2(source, target)
    print(f"已写入 {target}(.env 被 gitignore,不会提交)")
    if not args.yes:
        answer = input(f"请确认 {env['CTP_TD_ADDRESS']} 是国信给的测试前置(不是 SimNow/生产)。确认输入 y:").strip().lower()
        if answer != "y":
            print("未确认,已退出。")
            return 1

    step("3/5 取证运行(会向国信测试环境发委托;脚本先确认空仓,有持仓则一笔不发)")
    out_dir = args.resume or (ROOT / "data" / "broker_test" / now.strftime("%Y%m%d-%H%M%S"))
    cmd = [python_exe(), str(ROOT / "scripts" / "broker_test.py"), "--confirm-broker-test", "--out-dir", str(out_dir)]
    if args.resume:
        cmd = [python_exe(), str(ROOT / "scripts" / "broker_test.py"), "--confirm-broker-test", "--resume", str(args.resume)]
    if not args.no_interactive:
        cmd.append("--interactive")
        print("提示:运行到「3.1 连接断开监测」会让你按回车,然后请断开本机网络、看到提示后恢复。")
    code = subprocess.call(cmd, cwd=ROOT)
    print(f"\n取证运行退出码 {code}(0=全过 1=有失败 2=配置错 3=登录失败)")
    if code in (2, 3):
        print("没有登录成功或配置有误,不生成报告。把上面的输出贴给我。", file=sys.stderr)
        return code

    step("4/5 生成报告")
    rcode = subprocess.call([python_exe(), str(ROOT / "scripts" / "broker_report.py"), "--evidence", str(out_dir),
                             "--template", str(template)], cwd=ROOT)
    if rcode != 0:
        print("报告生成失败,证据仍在:", out_dir, file=sys.stderr)
        return rcode

    step("5/5 还需要你手动完成")
    print(f"""证据与报告目录:{out_dir}
  1. 看上面「报告里还缺什么」;FAIL 项不要重跑(会多一次登录),把输出贴给我分析。
  2. 「未测试/待人工」项在交易时段用:python scripts/guoxin_oneclick.py --resume "{out_dir}"
  3. 国信原版采集自检工具(datacollectforWindows-1.2.0.exe,24576 字节,SHA-256 以 2b49e154 开头):
     在采集信息齐全的机器上运行并登录,然后通知国信期货技术部。
  4. 报告签字(第五页)+ 盖四处章(第五页、两份报告首页、骑缝章)。""")
    try:
        import os
        os.startfile(out_dir)           # type: ignore[attr-defined]  Windows:打开结果目录
    except (AttributeError, OSError):
        pass
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
