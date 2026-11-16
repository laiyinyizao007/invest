#!/usr/bin/env python
"""变异验证脚手架:对 `tools/check_domain.py` 的清单做定点变异,断言每个变异都被抓住。

**为什么要有这个脚本**:ADR 里那句「N 种变异全部被抓住,条数实测」——
如果靠人手记,数字会过期(第 1 轮 codex 复核就发现 ADR 表里三行数字对不上:
测试文件从 82 条长到 90 条,红的用例集自然变了)。本脚本把这件事变成**可复算**的:
每次改清单后跑一遍,数字当场出来。

**三条防「假绿」的要求**(第 3 轮 codex 复核的 Critical-2 与 High-1):
1. **变异必须真的改变判定**:每个变异都带一个「见证路径」(witness),
   变异前后它的**分档结果必须不同**。否则「测试变红」可能只是别的原因,
   而某些变异是**空操作**(例:把 `"scripts/"` 塞进 `RESEARCH_FILES` —— 那个清单用
   精确成员判断,目录前缀语义只在 `RESEARCH_DIRS` 里生效,于是这一行等于
   「把 optimize.py 移出白名单」,与上一行重复)。
2. **`--verify` 必须真的核对**:按**去掉反引号后的文本**匹配 ADR 行,
   并断言**行数齐全**。早期版本拿脚本里的裸标签去匹配 ADR 的 Markdown 行,
   11 个标签**全部匹配不上** → 恒打印「一致」→ 纯假绿灯。
3. **退出码要分清**「有变异没被抓住」(1)与「环境不可用」(2)。

**为什么在副本里做**:变异要改的是**工作树里的文件**。在真工作树上改来改去
风险太大(中途出错就留下半个改坏的清单)。所以先复制一份仓库到临时目录,
在副本上做变异,真工作树全程不动。

用法:
    python tools/check_mutations.py            # 跑全部变异,打印条数
    python tools/check_mutations.py --verify   # 额外核对 ADR 表格里的数字

退出码:
    0 全部变异都被抓住(且 --verify 时 ADR 数字对得上);
    1 有变异没被抓住 / 变异是空操作 / ADR 数字对不上;
    2 环境不可用(fail-closed)。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CD_REL = "tools/check_domain.py"
TEST_REL = "tests/test_check_domain.py"

# 变异规格:
#   label    —— 人类可读的说明(也要出现在 ADR 表里,--verify 靠它匹配)
#   rel      —— 要改的文件
#   token    —— 在该文件里定位这一行的**引号内路径 token**(不含注释),
#               这样改注释不会让脚本找不到锚点(第 3 轮的 Low-1)
#   new_line —— 替换成的那一行
#   witness  —— 见证路径:变异前后它的分档结果必须不同(否则这一变异是空操作)
#   before   —— 变异**前**见证路径应有的档位(写进规格,免得测试只能靠猜)
Mutation = tuple[str, str, str, str, str, str]

MUTATIONS: tuple[Mutation, ...] = (
    ("删掉 trading/ 的不可降级条目",
     CD_REL, '"trading/",', '    # (moved by mutation)', "trading/runtime.py", "never_downgrade"),
    ("把 risk/live_admission.py 移出不可降级",
     CD_REL, '"risk/live_admission.py",', '    # (moved by mutation)',
     "risk/live_admission.py", "never_downgrade"),
    ("把 risk/candidate_eval.py 移出不可降级",
     CD_REL, '"risk/candidate_eval.py",', '    # (moved by mutation)',
     "risk/candidate_eval.py", "never_downgrade"),
    ("把 scripts/research_sandbox.py 移出不可降级",
     CD_REL, '"scripts/research_sandbox.py",', '    # (moved by mutation)',
     "scripts/research_sandbox.py", "never_downgrade"),
    ("把 scripts/backtest_lib.py 移出不可降级",
     CD_REL, '"scripts/backtest_lib.py",', '    # (moved by mutation)',
     "scripts/backtest_lib.py", "never_downgrade"),
    ("把 scripts/walk_forward_optimizer.py 移出不可降级",
     CD_REL, '"scripts/walk_forward_optimizer.py",', '    # (moved by mutation)',
     "scripts/walk_forward_optimizer.py", "never_downgrade"),
    ("把 scripts/research_grid.py 移出不可降级",
     CD_REL, '"scripts/research_grid.py",', '    # (moved by mutation)',
     "scripts/research_grid.py", "never_downgrade"),
    ("把 scripts/research_api.py 移出不可降级",
     CD_REL, '"scripts/research_api.py",', '    # (moved by mutation)',
     "scripts/research_api.py", "never_downgrade"),
    ("把 scripts/optimize.py 移出研究白名单(落回默认拒绝)",
     CD_REL, '"scripts/optimize.py",', '    # (moved by mutation)',
     "scripts/optimize.py", "research"),
    # 这一条必须动 RESEARCH_DIRS(目录前缀语义在那儿):
    # RESEARCH_FILES 用的是**精确成员判断**,往里塞 "scripts/" 是空操作。
    ("把整个 scripts/ 放进研究目录白名单",
     CD_REL, '"evidence/",', '    "scripts/",\n    "evidence/",',
     "scripts/some_brand_new_thing.py", "execution"),
)

# 额外场景:新建一个拥有判定点的文件(未入库)。它不在任何清单里,
# 默认拒绝会让它落进「执行域」——但全仓不变量要求它必须被明确列为不可降级,
# 所以这条场景期望**测试变红**(即探针有反应)。
EXTRA_PROBE_LABEL = "新建一个直接调 check_gate 的文件(不入库)"
EXTRA_PROBE = '''from risk.promotion_gate import check_gate


def go(metrics):
    return check_gate(metrics)
'''


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 设成 utf-8 + errors="replace"。

    **为什么必须做**:Windows 控制台默认 cp1252,打印中文会抛
    `UnicodeEncodeError`——而本仓库的主力开发环境就是 Windows
    (CLAUDE.md §6 把这个脚本写成可跑命令)。
    这是第 1 轮 codex 的 M5 在**新脚本**上的原样复发:同样的缺陷,
    在 `check_domain.py` 里修过,新写的脚本里又带进来一次。
    口径与 `tools/check_domain.py`、`scripts/research_console.py` 一致。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _run_tests(cwd: Path) -> tuple[int, str]:
    """跑本文件的测试,返回 (FAILED 行数, 完整输出)。

    **必须显式 encoding="utf-8"**:子进程被 PYTHONUTF8=1 逼着吐 UTF-8,
    而 `text=True` 默认按 locale(cp1252)解码 —— 解码失败时 stdout 变 None,
    紧接着 `.splitlines()` 就崩(第 3 轮的 Critical-1 第二处)。

    **关于「subprocess 实参不是静态字符串」**:这里的实参全部来自**本地常量**
    (`sys.executable` + 固定的测试路径),且**不经 shell**(`shell=False` 是默认),
    外部输入进不来。`_tier_of` 里的路径经 `%r` 转成 Python 字面量后作为 `-c`
    的参数传入,同样不经 shell。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TEST_REL, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(cwd), env=env,
        encoding="utf-8", errors="replace",
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

    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "baseline"]):
        subprocess.run(cmd, cwd=str(dest), check=True, env=env,
                       capture_output=True)


def _apply(repo: Path, rel: str, token: str, new_line: str) -> str:
    """把含 `token` 的那一行替换成 `new_line`;返回 'ok' 或错误说明。

    定位靠**引号内的路径 token**,不靠整行相等——所以改行尾注释不会让脚本
    找不到锚点(第 3 轮的 Low-1:早期版本整行匹配,注释改一个字就 CI 变红,
    报错信息还 cryptic)。
    """
    path = repo / rel
    if not path.exists():
        return f"文件不存在:{rel}"
    lines = path.read_text(encoding="utf-8").split("\n")
    hits = [i for i, line in enumerate(lines) if token in line]
    if len(hits) != 1:
        return f"锚点 {token!r} 命中 {len(hits)} 次(应为 1)"
    lines[hits[0]] = new_line
    path.write_text("\n".join(lines), encoding="utf-8")
    return "ok"


def _tier_of(repo: Path, rel: str) -> str:
    """问副本里的分档器:这个路径现在判成什么档。"""
    code = (
        "import sys; sys.path.insert(0, 'tools'); "
        "import check_domain as cd; print(cd.classify(%r)[0])" % rel
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          cwd=str(repo), env={**os.environ, "PYTHONUTF8": "1"},
                          encoding="utf-8", errors="replace")
    return proc.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="变异验证:每个清单改动都必须被抓住")
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
        print("基线(未变异):全绿")
        print()

        results: list[tuple[str, int]] = []
        for label, rel, token, new_line, witness, expected_before in MUTATIONS:
            orig = (repo / rel).read_text(encoding="utf-8")
            before = _tier_of(repo, witness)
            if before != expected_before:
                print(f"  [跳过] {label}:见证路径 {witness} 现在的档位是 {before},"
                      f"规格写的是 {expected_before}(规格与仓库现状对不上)", file=sys.stderr)
                return 2
            msg = _apply(repo, rel, token, new_line)
            if msg != "ok":
                (repo / rel).write_text(orig, encoding="utf-8")
                print(f"  [跳过] {label}:{msg}", file=sys.stderr)
                return 2
            after = _tier_of(repo, witness)
            n, _ = _run_tests(repo)
            (repo / rel).write_text(orig, encoding="utf-8")

            if before == after:
                # **空操作变异**:判定结果没变 → 这一行等于没测。
                # (第 3 轮的 High-1:ADR 表里有一行把 `"scripts/"` 塞进
                #  RESEARCH_FILES,而那个清单用精确成员判断 → 整行是空操作)
                print(f"  [!! 空操作] {label}:见证路径 {witness} 的分档前后都是 {before}")
                return 1
            mark = "OK " if n > 0 else "!! "
            print(f"  [{mark}{n:2d} 条红] {label}"
                  f"(见证 {witness}:{before} → {after})")
            results.append((label, n))

        # 额外场景:新建一个拥有判定点的文件(未入库)
        new_file = repo / "risk" / "zz_new_gate_probe.py"
        new_file.write_text(EXTRA_PROBE, encoding="utf-8")
        n, _ = _run_tests(repo)
        new_file.unlink()
        results.append((EXTRA_PROBE_LABEL, n))
        print(f"  [{'OK ' if n > 0 else '!! '}{n:2d} 条红] {EXTRA_PROBE_LABEL}")

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


def _verify_adr(results: list[tuple[str, int]], adr_path: Path | None = None) -> int:
    """核对 ADR 里的**变异集合**与实测是否一致。

    **只核对「集合」,不核对「每种变异红了几条」**:那个数字随本文件的用例数
    变化(每加一条用例就变),写进文档必然过期——CI 上已经因此红过两次
    (一次数字对不上、一次标签对不上)。有意义且稳定的只有两件事,
    都由脚本在 CI 上每次实跑:每种变异都被抓住、每种变异都真的翻转了判定。

    **匹配前必须去掉 Markdown 反引号与强调号**:ADR 的行里写的是
    `| 把整个 scripts/ 放进研究**目录**白名单 | ... |`,脚本里的标签没有那些标记——
    直接 `in` 匹配**一个都命中不了**,于是恒打印「一致」→ 纯假绿灯
    (第 3 轮的 Critical-2,CI 上真的发生过)。

    **不能**去掉下划线:`risk/live_admission.py` 这类路径里有它,
    去掉就把路径改了(实测:去掉后八行全部对不上)。

    **还必须断言行数齐全**:找不到标签不能只跳过——那正是「守护静默失效」的样子。

    **按整格比对,不用子串**:`in` 会让「某条变异了」匹配上标签「某条变异」——
    那样「标签写错一个字」这种失准就检测不出来(实测踩过)。
    """
    adr = adr_path or (ROOT / "docs" / "adr" / "20261114-consequence-domain-tiers.md")
    if not adr.exists():
        print("ADR 不存在,跳过核对", file=sys.stderr)
        return 2
    # 归一化:去掉反引号与 Markdown 强调号。
    # **必须**去掉强调号——ADR 的行里有「研究**目录**白名单」这种加粗,
    # 留着它就与脚本标签差两个字符,又变成「匹配不上 → 静默跳过 → 假绿灯」。
    # **不能**去掉下划线:`risk/live_admission.py` 这类路径里有它,
    # 去掉就把路径改了(实测:去掉 `_` 后八行全部对不上)。
    normalized = adr.read_text(encoding="utf-8")
    for mark in ("`", "*"):
        normalized = normalized.replace(mark, "")
    bad: list[str] = []
    matched = 0
    # 按**整格**比对,不用子串:`in` 会让「某条变异了」匹配上标签「某条变异」——
    # 那样「标签写错了一个字」这种失准就检测不出来(实测踩过)。
    listed: set[str] = set()
    for line in normalized.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells:
            listed.add(cells[0])
    for label, _n in results:
        if label in listed:
            matched += 1
        else:
            bad.append(f"ADR 表里没有「{label}」这一行")
    if matched != len(results):
        bad.append(f"ADR 表只匹配到 {matched} 行,实测有 {len(results)} 种变异")
    if bad:
        print("ADR 里的变异表与实测不符:", file=sys.stderr)
        for line in bad:
            print(f"  - {line}", file=sys.stderr)
        return 1
    print(f"ADR 里列出的变异集合与脚本一致({matched} 行全部对上)。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
