#!/usr/bin/env python
"""CI 闸门的**一条命令**(第 55 轮):跑 pytest → 核对名单 → 用**退出码**说话。

**为什么要有这个工具(第 55 轮,codex 第 2 轮的 C1 与本轮实测的「真空绿」)**

第 48~54 轮那套是「pytest 一步 + 闸门一步」**两段** shell,中间夹着 `rm -f`、
续行(`\\`)、for 循环……每多一段,就有多一种「闸门看上去配好了、实际没跑」的形态。
第 55 轮**实测**出来的那一条最狠:

    bash -c "python -m pytest ... tests/test_research_boundary.py"
    bash -c "python tools/check_ci_ran.py ... || true"

⇒ 七条纪律守卫**全部真空绿**(红=0、异常=0)——因为守卫只认**它认得**的命令,
认不出来的就当「没有 pytest、没有闸门」。守卫是**按已知的漏洞形态**枚举的,
漏一种形态就漏一条防线(仓库自己的教训:「静态推理必漏,每条规则都配双向探针」)。

现在把两段合成**一条**:

  1. 报告由本工具**自己**跑 pytest 生成——中间没有别的命令能插进来;
  2. 生成后**立刻**核对名单——不可能被「pytest 与闸门之间」的动作改写
     (codex M4:报告可以在两者之间被覆写/伪造);
  3. 对不上 ⇒ **退出码非 0** ⇒ 这一步红。

于是「闸门能不能让 CI 红」不再取决于 shell 的写法,而取决于**这一条命令的退出码**。

**用法**(这一条就是 CI 里那一步的**全部**内容——规范形由
`tools/check_ci_wiring.py` 强制):

    python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml" \\
        --roster tests/guard_roster.json --suite tests/test_research_boundary.py

退出码:
    0  pytest 跑过、报告对得上(名单点名的用例都真跑了);
    1  有失败 / 名单对不上 / 报告缺失或读不了;
    2  环境不可用(fail-closed)。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import check_ci_ran  # noqa: E402  (同目录的工具,故意用文件路径导入)

DEFAULT_ROSTER = ROOT / "tests" / "guard_roster.json"


def _reconfigure_stdio() -> None:
    """stdout/stderr 设成 utf-8(Windows 控制台默认 cp1252,打印中文会抛异常)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_roster(roster: Path) -> tuple[dict, list[str]]:
    """读名单 ⇒ (数据, 结构问题)。

    **结构问题一律 fail-closed**(第 55 轮之五,codex 第 2 轮的 H2):
    此前 `_roster_suites()` 读不出就返回**空集** ⇒ 每份套件都被当成
    「不在名单里」⇒ 自动加 `--allow-unlisted-suite`
    ⇒ **逐条点名核对整个消失** ⇒ 闸门照样打印「[OK] 闸门通过」。
    ⇒ 现在:名单结构不对 ⇒ **退出码 2**,压根不跑 pytest。
    """
    import json

    try:
        data = json.loads(roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return ({}, ["名单读不了:%s(%s)" % (roster, exc)])
    problems = check_ci_ran.validate_roster(data)
    if not isinstance(data, dict):
        return ({}, problems)
    return (data, problems)


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="跑研究侧测试并核对名单(一步到底)")
    parser.add_argument("--junitxml", required=True, type=Path, help="pytest 报告的落盘位置")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER,
                        help="机器可读的名单(默认 tests/guard_roster.json)")
    parser.add_argument("--suite", action="append", default=[], dest="suites",
                        help="要跑并核对的套件(可重复给多个)")
    args = parser.parse_args(argv)

    if not args.suites:
        print("[FAIL] 一份套件都没给:这一步什么都跑不了 ⇒ 判红(fail-closed)")
        return 2

    missing = [s for s in args.suites if not Path(s).exists()]
    if missing:
        print("[FAIL] 这些套件文件不存在:%s" % ", ".join(missing))
        return 2

    # --- 名单先验**:结构不对 ⇒ **压根不跑 pytest**(退出码 2 = 环境不可用) ---
    #
    # **为什么放在跑 pytest 之前**:名单坏掉时若还往下走,
    # 「逐条点名核对」会**静默变成空集**(每份套件都被当成「不在名单里」
    # ⇒ 自动 `--allow-unlisted-suite`)⇒ 闸门打印「[OK] 闸门通过」⇒
    # 而**一条用例都没点名核对过**。这是 codex 第 2 轮 H2 里最严重的一条。
    roster_data, roster_problems = _load_roster(args.roster)
    if roster_problems:
        for problem in roster_problems:
            print("[FAIL] 名单结构不对:%s" % problem)
        print("[FAIL] 名单结构不对 ⇒ 点名核对做不了 ⇒ 退出码 2(fail-closed)")
        return 2
    known = set(roster_data.get("must_run", {}))

    # --- 第 1 步:先把上一轮可能残留的报告删掉 ---
    #
    # **为什么必须删**:自托管 runner 的临时目录不一定每次都清干净;pytest 崩在
    # 写文件之前时,核对会去读**上一轮的陈旧报告**,看着全绿其实什么都没跑。
    # 本工具**自己**生成报告,所以删在自己的进程里做,不给别人插手的窗口。
    report = args.junitxml
    try:
        if report.exists():
            report.unlink()
    except OSError as exc:
        print("[FAIL] 删不掉上一轮残留的报告 %s:%s(fail-closed)" % (report, exc))
        return 2

    # --- 第 2 步:跑 pytest ---
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    cmd = [
        sys.executable, "-m", "pytest",
        "-p", "no:cacheprovider",
        "-q",
        # 收集错误**不许**吞掉整轮:一份文件收不起来 ⇒ 那份套件的用例不在报告里
        # ⇒ 下面的名单核对会点名报红(第 55 轮:`--continue-on-collection-errors`
        # 单独用是安全的,因为「缺了用例」这一层由名单兜底)。
        "--continue-on-collection-errors",
        "--junitxml=%s" % report,
        *args.suites,
    ]
    print("[gate] 跑测试:%s" % " ".join(cmd))
    # **在当前工作目录里跑**(CI 上就是仓库根):套件路径按 cwd 解析,
    # 与命令行上写的字面一致 ⇒ 命令行看到什么就跑什么,不多不少。
    proc = subprocess.run(cmd, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    output = (proc.stdout or "") + (proc.stderr or "")
    # 摘要行照抄 pytest 的(读者要看到「跑了多少条」),但**整段**输出太长时只留尾部。
    tail = output.strip().splitlines()[-25:]
    for line in tail:
        print("    %s" % line)

    pytest_failed = proc.returncode != 0
    if pytest_failed:
        print("[FAIL] pytest 自己就红了(退出码 %d)" % proc.returncode)

    # --- 第 3 步:核对名单(逐份套件) ---
    #
    # 名单里的套件 ⇒ **逐条点名**核对(没出现 / 出现却是 skipped ⇒ 红);
    # 不在名单里的套件 ⇒ `--allow-unlisted-suite`(只做全局核对 + 这一份自己的核对:
    # 必须出现在报告里、且不能整份被跳过)。
    known_suites = known
    problems = 0
    for suite in args.suites:
        gate_argv = ["--junitxml", str(report), "--roster", str(args.roster),
                     "--suite", suite]
        if suite not in known_suites:
            gate_argv.append("--allow-unlisted-suite")
        rc = check_ci_ran.main(gate_argv)
        if rc != 0:
            problems += 1
        print("[gate] %s 核对结果:%s" % (suite, "OK" if rc == 0 else "红"))

    if pytest_failed or problems:
        print("[FAIL] 闸门判红:pytest=%s 名单核对失败 %d 份 ⇒ 这一步必须红"
              % ("红" if pytest_failed else "绿", problems))
        return 1
    print("[OK ] 闸门通过:该跑的跑了,名单点名的都真跑了。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
