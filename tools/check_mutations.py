#!/usr/bin/env python
"""变异验证脚手架:对 `tools/check_domain.py` 的清单做定点变异,断言每个变异都被抓住。

**为什么要有这个脚本**:ADR 里那句「N 种变异全部被抓住,条数实测」——
如果靠人手记,数字会过期(第 1 轮 codex 复核就发现 ADR 表里三行数字对不上:
测试文件从 82 条长到 90 条,红的用例集自然变了)。本脚本把这件事变成**可复算**的:
每次改清单后跑一遍,数字当场出来。

**为什么在副本里做**:变异要改的是**工作树里的文件**。在真工作树上改来改去
风险太大(中途出错就留下半个改坏的清单)。所以先复制一份仓库到临时目录,
在副本上做变异,真工作树全程不动。

用法:
    python tools/check_mutations.py            # 跑全部变异,打印条数
    python tools/check_mutations.py --verify   # 额外核对 ADR 表格里的数字

退出码:
    0 全部变异都被抓住(且 --verify 时 ADR 数字对得上);
    1 有变异没被抓住,或 ADR 数字对不上;
    2 环境不可用(fail-closed)。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CD_REL = "tools/check_domain.py"
TEST_REL = "tests/test_check_domain.py"

# 变异规格: (说明, 目标文件, 锚点行(整行匹配), 替换成什么)
MUTATIONS = [
    ("删掉 trading/ 的不可降级条目",
     CD_REL, '    "trading/",', '    # (moved)'),
    ("把 risk/live_admission.py 移出不可降级",
     CD_REL, '    "risk/live_admission.py",       # 生产 API 前置检查', '    # (moved)'),
    ("把 risk/candidate_eval.py 移出不可降级",
     CD_REL, '    "risk/candidate_eval.py",', '    # (moved)'),
    ("把 scripts/research_sandbox.py 移出不可降级",
     CD_REL, '    "scripts/research_sandbox.py",        # 批准账本唯一写入者(record_approval)',
     '    # (moved)'),
    ("把 scripts/backtest_lib.py 移出不可降级",
     CD_REL, '    "scripts/backtest_lib.py",', '    # (moved)'),
    ("把 scripts/walk_forward_optimizer.py 移出不可降级",
     CD_REL, '    "scripts/walk_forward_optimizer.py",', '    # (moved)'),
    ("把 scripts/research_grid.py 移出不可降级",
     CD_REL, '    "scripts/research_grid.py",', '    # (moved)'),
    ("把 research_api.py 移出不可降级",
     CD_REL, '    "scripts/research_api.py",            # approve/review CLI 入口 + 候选装载',
     '    # (moved)'),
    ("把 scripts/optimize.py 移出研究白名单(落回默认拒绝)",
     CD_REL, '    "scripts/optimize.py",', '    # (moved)'),
    ("把整个 scripts/ 放进研究白名单",
     CD_REL, '    "scripts/optimize.py",', '    "scripts/",'),
]

# 额外场景:新建一个拥有判定点的文件(未入库)。它不在任何清单里,
# 默认拒绝会让它落进「执行域」——但全仓不变量要求它必须被明确列为不可降级,
# 所以这条场景期望**测试变红**(即探针有反应)。
EXTRA_PROBE = '''from risk.promotion_gate import check_gate


def go(metrics):
    return check_gate(metrics)
'''


def _run_tests(cwd: Path) -> tuple[int, str]:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_REL, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(cwd), env=env,
    )
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    return len(failed), proc.stdout


def _copy_repo(dest: Path) -> None:
    """复制一份够用的仓库副本。

    **必须建一个 git 仓库**:测试里有几条探针是走 git 的
    (`git ls-files` / `git status --porcelain -uall`),
    没有 .git 时它们会直接报错——那不是探针在工作,是环境坏了,
    两者在「红了」这件事上无法区分。所以这里 git init + 全量 add + commit。
    """
    for rel in ("tools", "tests", "risk", "scripts", "config", "docs", "strategies",
                "trading", "ctp_tools", "notify", "dashboard", "evidence",
                "pending_strategies", "pyproject.toml",
                # 清单里点名的这些也必须复制过去,否则存在性探针会误报
                "Dockerfile", "docker-compose.yml", "docker-compose.research.yml",
                "requirements.txt", "requirements-research.txt", "requirements-dashboard.txt",
                "CLAUDE.md", "CHANGELOG.md", "PROJECTWIKI.md", "README.md",
                # .gitignore 必须一起复制:目录前缀的「是 gitignore 的运行时目录吗」
                # 这条判据靠它(缺了它,pending_strategies/ 就既不存在也不被忽略)
                ".gitignore"):
        src = ROOT / rel
        if not src.exists():
            continue
        dst = dest / rel
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    # 建一个 git 仓库并全量提交:探针要用 git ls-files / git status
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(cmd, cwd=str(dest), check=True, env=env,
                       capture_output=True)


def _apply(repo: Path, rel: str, anchor: str, replacement: str) -> str:
    """按**整行**替换;返回 'ok' 或错误说明。"""
    path = repo / rel
    if not path.exists():
        return f"文件不存在:{rel}"
    lines = path.read_text(encoding="utf-8").split("\n")
    hits = [i for i, line in enumerate(lines) if line.strip() == anchor.strip()]
    if len(hits) != 1:
        return f"锚点命中 {len(hits)} 次(应为 1):{anchor!r}"
    lines[hits[0]] = replacement
    path.write_text("\n".join(lines), encoding="utf-8")
    return "ok"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="变异验证:每个清单改动都必须被测试抓住")
    parser.add_argument("--verify", action="store_true",
                        help="额外核对 ADR 表格里的数字与实测是否一致")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="domain_mutations_"))
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    try:
        _copy_repo(repo)

        baseline, out = _run_tests(repo)
        if baseline != 0:
            print("基线就有测试失败,变异验证无从谈起:", file=sys.stderr)
            print(out[-1500:], file=sys.stderr)
            return 2
        print("基线(未变异):全绿\n")

        results: list[tuple[str, int]] = []
        for label, rel, anchor, repl in MUTATIONS:
            orig = (repo / rel).read_text(encoding="utf-8")
            msg = _apply(repo, rel, anchor, repl)
            if msg != "ok":
                (repo / rel).write_text(orig, encoding="utf-8")
                print(f"  [跳过] {label}:{msg}", file=sys.stderr)
                return 2
            n, _ = _run_tests(repo)
            (repo / rel).write_text(orig, encoding="utf-8")
            results.append((label, n))
            mark = "OK " if n > 0 else "!! "
            print(f"  [{mark}{n:2d} 条红] {label}")

        # 额外场景:新建一个拥有判定点的文件(未入库)
        new_file = repo / "risk" / "zz_new_gate_probe.py"
        new_file.write_text(EXTRA_PROBE, encoding="utf-8")
        n, _ = _run_tests(repo)
        new_file.unlink()
        results.append(("新建一个直接调 check_gate 的文件(不入库)", n))
        print(f"  [{'OK ' if n > 0 else '!! '}{n:2d} 条红] 新建一个直接调 check_gate 的文件(不入库)")

        uncaught = [label for label, n in results if n == 0]
        print()
        if uncaught:
            print("以下变异**没有被抓住**(探针有洞):", file=sys.stderr)
            for label in uncaught:
                print(f"  - {label}", file=sys.stderr)
            return 1
        print(f"{len(results)} 种变异全部被抓住。")

        if args.verify:
            return _verify_adr(results)
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _verify_adr(results: list[tuple[str, int]]) -> int:
    """核对 ADR 里的变异表与实测是否一致。"""
    adr = ROOT / "docs" / "adr" / "20261114-consequence-domain-tiers.md"
    if not adr.exists():
        print("ADR 不存在,跳过核对", file=sys.stderr)
        return 2
    text = adr.read_text(encoding="utf-8")
    bad: list[str] = []
    for label, n in results:
        # ADR 表里的行:「| 变异描述 | N |」
        key = label.split("(「")[0].strip()
        if key and key in text:
            # 找出这一行里的数字
            for line in text.splitlines():
                if key in line and line.strip().startswith("|"):
                    nums = re.findall(r"\|\s*(\d+)\s*\|", line)
                    if nums and int(nums[0]) != n:
                        bad.append(f"ADR: 「{key}」写 {nums[0]},实测 {n}")
    if bad:
        print("ADR 里的变异条数与实测不符:", file=sys.stderr)
        for line in bad:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print("ADR 里的变异条数与实测一致。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
