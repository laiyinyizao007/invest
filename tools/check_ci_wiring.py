#!/usr/bin/env python
"""CI 接线的**结构**自检(第 55 轮):规范形 + fail-closed。

**为什么不再靠「猜 shell」**

第 48~54 轮的纪律守卫是**静态猜**命令位置:把 workflow 的 `run:` 切成逻辑命令行,
认出「哪一条在跑 pytest」「哪一条在调闸门」,再断言那些命令是**裸命令**。
这套办法有一个**结构性**的漏:它只认**它认得**的命令形态。

第 55 轮**实测**:把 pytest 与闸门**一起**藏进 `bash -c "..."` ⇒
七条纪律守卫**全部真空绿**(红=0、异常=0)——守卫看到的是「没有 pytest、没有闸门」,
于是「这份套件没人跑」这件事**它自己看不见**。
(仓库自己的教训:「静态推理必漏」;「守卫要防的不是**已知的**漏洞形态」,
而是**任何**它判不了的形态。)

**这一轮的做法:不再枚举形态,改成「只允许一种形态」**

闸门步骤必须是**规范形**——**一条**命令:

    python tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml" \\
        --roster tests/guard_roster.json --suite tests/test_a.py [...]

于是:
  - 「闸门能不能让 CI 红」不再取决于 shell 的写法,而取决于**那一条命令的退出码**;
  - `exit 0` / `set +e` / `|| true` / `if:` / `continue-on-error:` / `shell:`
    这些旁路**没有位置可放**(第一步只能有一条命令;第二步不许带那几个键);
  - 「pytest 与闸门之间」没有缝隙可插(报告由 `tools/ci_gate.py` 自己生成并核对)。

**fail-closed**:凡是**判不了**的形态一律判红——
  - 命令里在跑 pytest / 闸门工具,但**不是**规范形 ⇒ 红;
  - 命令**认不出来**(例如 `bash -c "…"`、`eval`、`$( )`、函数定义),
    而它的文本里**提到了** pytest / 闸门 ⇒ 红(「判不了 ⇒ 不放行」)。
  认不出来又**不涉及**危险命令的(比如 `rm -f`、`echo`)不红——否则全是假红。

**它守什么(四条)**:
  1. **规范形**:闸门步骤只有那一条命令,且不许带 `if:` / `continue-on-error:` / `shell:`;
  2. **名单覆盖**:名单 `tests/guard_roster.json` 里点名的**每一份**套件,
     都必须真的被某个规范闸门步骤跑(codex 第 2 轮的 **C1**:
     把那一步删掉,此前**没有任何守卫**会发现);
  3. **这份检查自己得有人跑**:两个 workflow 文件里**各自**都必须有一处
     「跑 `tools/check_ci_wiring.py`」的调用——删掉其中一处,另一处会发现;
  4. **这份检查自己的调用必须「不带参数」**(第 55 轮**实测**出来的洞):
     守卫的自检若能被**加参数缩小范围**,那它就不是守卫了。
     实测:把调用改成 `python tools/check_ci_wiring.py --workflow <某个别处文件>`
     ⇒ 被检查的就只剩那个文件 ⇒ **真实的两个 workflow 压根没被查**,
     而工具**照样报「[OK] CI 接线合规」**。
     ⇒ 现在:接线自检的调用**必须**是 `python tools/check_ci_wiring.py`
     (解释器可以换写法,**不许带任何选项**);命令行上的 `--workflow`
     是**追加**而不是**替换**(不能从命令行把检查范围缩小)。

用法:
    python tools/check_ci_wiring.py [--workflow PATH ...] [--roster PATH]

    `--workflow` 是**追加**:默认要查的那两个文件**永远**会被查,
    给出 `--workflow` 只是**额外**再查一个(调试用)。

退出码:0 接线合规 / 1 有问题 / 2 环境不可用(fail-closed)。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
RESEARCH_WORKFLOW = WORKFLOWS_DIR / "research-guard.yml"
CONSTITUTION_WORKFLOW = WORKFLOWS_DIR / "constitution-guard.yml"
DEFAULT_ROSTER = ROOT / "tests" / "guard_roster.json"

# 规范形里那一条命令的解释器:python / python3 / python3.12 / py
_PYTHON_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|py|pythonw)$")


def is_python_interpreter_name(text: str) -> bool:
    """这个名字是不是**python 解释器**?

    **口径只有这一份**(第 55 轮之二十四,claude 第 1 轮复核 ④):
    `tools/check_constitution_history.py` 的字符串程序扫描**也调这一份**
    ⇒ 两边不会各认各的。
    (此前两边**各有一份**正则 ⇒ `python.exe` / `pythonw` / 带反斜杠的路径
    只被一边认 ⇒ 同一台机器上的同一个解释器,两个守卫结论不同。)

    **Windows 的两个细节**:
      - 路径分隔符可能是 `\\` ⇒ 先归一成 `/` 再取**末段**;
      - 可执行文件常带 `.exe` 后缀 ⇒ **剥掉**再比。
    """
    base = text.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return bool(_PYTHON_RE.match(base))
# 「危险工具」的文本指纹:出现在**认不出来的**命令里 ⇒ 判红。
_DANGEROUS_WORDS = ("pytest", "check_ci_ran", "ci_gate")
# **「列举/说明」类命令**:它们**不会执行** pytest/闸门,只把它们当**参数或文本**
# ⇒ 提到它们**不算**「在跑它们」。
# (第 55 轮之七,codex 第 4 轮的 M3:`grep -rn pytest tests/`、
#  `ruff check tools/ci_gate.py`、`echo "闸门是 ci_gate.py"` 这些**合法写法**
#  此前一律判红 ⇒ 极容易踩的雷。**仓库教训:禁引用,不是禁出现**。)
#
# **第 55 轮之十(codex 第 5 轮的 M-2)**:这个名单原先**混进了真能执行的命令**
# ——`awk 'BEGIN{system("python -m pytest …")}'`、`sed '…e'`、
# `find … -exec python -m pytest … \;`、`git -c alias.x='!python -m pytest' x`
# **都能执行** ⇒ 把它们当「不会执行」⇒ 实测**四条全部漏过**
# ⇒ 现在**移出**这个名单(它们不是「列举/说明」类)。
# ⇒ 留下的每一个都**只读文本**:echo/printf/grep/…/cat/head/tail/ls/wc/
#    sort/uniq/cut/ruff/black/flake8/pylint/mypy。
_NON_EXECUTING_HEADS = {
    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "cat", "head",
    "tail", "ls", "wc", "sort", "uniq", "cut", "true",
    "false", "comment", "ruff", "black", "flake8", "pylint", "mypy",
}
# 运行器(uv run / poetry run …):`run` 之后的那个命令才是真命令。
_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "tox", "nox"}
# **会吃掉下一个参数**的解释器选项(此前按「跳过所有以 - 开头的」处理,
# 于是 `python -W error -m pytest` 会把 `error` 当成脚本 ⇒ codex M2)。
_VALUE_FLAGS = {"-W", "-X", "-c", "--check-hash-based-pycs"}
# **规范形里允许的解释器选项**(第 55 轮之五,codex 第 2 轮的 M1):
# 此前 `canonical_gate`/`canonical_wiring` 要求「解释器后面紧跟脚本」,
# 于是 `python -X utf8 …`、`python -W error …`、`py -3 …` 这些**合法**写法
# 全被判红(误伤)⇒ 而 ADR 的明文却写着「闸门命令允许 -W/-X/-c 等取值选项」
# ⇒ 代码与文档直接矛盾。
#
# **取值选项**(-W / -X):选项**和它的值**一起跳过。
# **不带值的选项**:-3 / -3.12(版本)、-u / -B / -E / -s / -I / -O / -v。
# **禁止**的选项:
#   - `-c`:**它把脚本整个换掉**(`python -c "…"` ⇒ 脚本压根没跑)⇒ 判红;
#   - `-m`:**它把脚本换成模块** ⇒ 跑的压根不是那个脚本 ⇒ 判红。
_VALUE_INTERPRETER_FLAGS = {"-W", "-X"}
_NO_VALUE_INTERPRETER_FLAGS = {
    "-3", "-u", "-B", "-E", "-s", "-I", "-O", "-OO", "-P", "-v",
    "--check-hash-based-pycs",
}
_FORBIDDEN_INTERPRETER_FLAGS = {"-c", "-m"}
# **受保护脚本**(第 55 轮之六,codex 第 3 轮的 H2):
# 这些脚本的**退出码就是证据链**——「142 种变异全部被抓住」这句话,
# 靠的就是 `tools/check_mutations.py --verify` 与
# `tools/check_guard_mutations.py --verify` 这两步**红**。
# 若这两步可以被 `continue-on-error:` / `if: false` / `|| true` /
# 尾部 `&` 轻轻放过 ⇒ 那句话变成一句空话,而 CI 照样绿。
# ⇒ 对它们施加与闸门步骤**同一套**纪律。
_PROTECTED_SCRIPTS = {
    "tools/check_mutations.py",
    "tools/check_guard_mutations.py",
}
# **受保护脚本那一步必须带的选项**(第 55 轮之七):
# `--verify` 才是「**ADR 的变异表与脚本双向核对**」的那一步。
# 少了它 ⇒ 变异**照样**被抓住,但「两边各 N 行、逐条对上」这件事
# **不再有人核对** ⇒ 文档里那句话从此没人保证
# ⇒ 这正是仓库反复吃过的亏:「文档里写下的防线,必须在代码里真的接线」。
# ⇒ 受保护脚本那一步的**规范形**是:`python <脚本> --verify`(**恰好**这一个选项)。
_PROTECTED_REQUIRED_OPTIONS = {
    "tools/check_mutations.py": ("--verify",),
    "tools/check_guard_mutations.py": ("--verify",),
}
# **裁决者自己的冒烟关卡**(第 55 轮之七,claude 第 4 轮的 L-5):
# `tools/check_domain.py` 是「谁拥有判定点」的**裁决者**(项目 CLAUDE.md 明说
# 它属于**不可降级**清单),而它在 CI 里的冒烟那一步此前**不受任何纪律约束**
# ⇒ 把它改成 `|| true` / `continue-on-error: true` / `if: false`,
# **分档器自己的关卡就永远不会红**,而 CI 看上去是绿的。
# ⇒ 对它施加**旁路键**纪律(但**不是**「只有一条命令」——那一步本来就是一个
# 多命令块:`set -e` + 显式 rc 判定 + 结论行自证)。
_SELF_GUARD_SCRIPTS = {
    "tools/check_domain.py",
}
# 「后台执行」的操作符:裸 `&`(不是 `&&`,也不是 `2>&1` / `&>` 这种重定向)。
# `cmd &` ⇒ shell 立刻返回 0 ⇒ 退出码被丢弃(实测:`bash -c 'false &'` → 0)。
_BACKGROUND_RE = re.compile(r"(?<![&>])&(?!&|>)")
# **关掉 errexit**(`set +e` / `set +o errexit`)——第 55 轮之八**实测**的洞。
# 裁决者冒烟那一步是**多命令块**(仓库自己写的:`set -e` + 显式 rc 判定 +
# 结论行自证)⇒ 「只有一条命令」的规范形**套不上它** ⇒ 旁路**键**纪律
# (`if:` / `continue-on-error:` / `shell:` / `env:` / 尾部 `&`)也拦不住
# `set +e`——它不是键,是 shell 内建。
# ⇒ 后果:分档器崩了(退出码 1 = 「判为执行域」,**正常结果**)或返回 2
#    (环境不可用,fail-closed)时,这一步**照样绿** ⇒ 裁决者的关卡等于没有。
# ⇒ 判红。
#
# **第 55 轮之十(codex 第 5 轮的 M-4)**:一条正则**认不全** ——
# `set +eo pipefail` 与 `set +e` **一样**关掉 errexit(实测:
# `bash -c 'set -e; set +eo pipefail; false; echo survived'` ⇒ 打印 survived)
# ⇒ 而 `(?![A-Za-z0-9_])` 的后视把 `+eo` **排除**掉了 ⇒ 实测**漏过**
# ⇒ 改由 `_errexit_disabled()` **按 `set` 的参数逐个解析**。
# `|| true` / `|| :` ⇒ 把失败**吃掉**。**只对「裁决者调用所在的那一行」判**
# (别处的 `|| true` 与这条关卡无关 ⇒ 不误伤)。
# (`|| rc=$?` 是**捕获**退出码,不是吃掉 ⇒ 不匹配。)
_OR_TRUE_RE = re.compile(r"\|\|\s*(?:true|:)\s*(?:#.*)?$")
# **失败被吃掉的其它形态**(第 55 轮之九,实测):`|| true` 只是其中一种写法。
# (`|| rc=$?` 是**捕获**退出码、后面还要拿它判 2 ⇒ 不匹配、不误伤。)
_SWALLOW_RE = re.compile(
    r"\|\|\s*(?:true|:)(?![A-Za-z0-9_])"      # `|| true` / `|| :`(后面还有东西)
    r"|;\s*true(?![A-Za-z0-9_])"              # `; true`
    r"|&&\s*true(?![A-Za-z0-9_])"             # `&& true`
    r"|(?<!\|)\|\s*true(?![A-Za-z0-9_])"       # `| true`(管道给 true)
    r"|\|\|\s*exit\s+0(?![0-9])"              # `|| exit 0`
)
# 分档器调用**被包在条件里**(`if` / `elif` / `while` / `until`)
_CONDITIONAL_RE = re.compile(r"^\s*(?:if|elif|while|until)\b")
_CONDITIONAL_OPEN_RE = re.compile(r"^\s*(?:if|while|until)\b")
_CONDITIONAL_CLOSE_RE = re.compile(r"^\s*(?:fi|done)\b")
# 规范形里允许出现的选项(只有这三个;多一个就是非规范形)。
_CANONICAL_OPTIONS = {"--junitxml", "--roster", "--suite"}
# 接线自检**自己**的规范形调用:`python tools/check_ci_wiring.py`,**不许带任何选项**。
_CANONICAL_WIRING_SCRIPT = "tools/check_ci_wiring.py"
# 宪法 workflow 里的那个落点:宪法层是**纯标准库 + git**,不装 PyYAML ⇒
# 那个落点跑的是 `tools/check_constitution_history.py`
# (它自己用**文本级**核对「研究 workflow 里的接线自检还在」)。
_CONSTITUTION_GUARD_SCRIPT = "tools/check_constitution_history.py"
_SUITE_RE = re.compile(r"^tests/test_[A-Za-z0-9_]+\.py$")


def _reconfigure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# --------------------------------------------------------------------------
# 命令切分与识别
# --------------------------------------------------------------------------
def _strip_shell_comment(line: str) -> str:
    """去掉行尾的 shell 注释(`#` 到行尾),**不动引号里的 `#`**。

    **为什么**(第 55 轮之五,codex 第 2 轮的 M1/L5):
    `run: |` 的块标量里写
        python tools/ci_gate.py --junitxml … --suite tests/test_x.py  # 边界闸门
    是**完全合法**的 shell(注释在 `#` 前有空白时开始)⇒ 运行结果与不带注释
    一模一样 ⇒ 但此前 `# 边界闸门` 被当成命令的一部分 ⇒ 规范形判红(误伤)。
    """
    in_single = False
    in_double = False
    for index, char in enumerate(line):
        if in_double:
            if char == '"':
                in_double = False
            continue
        if in_single:
            if char == "'":
                in_single = False
            continue
        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True
        elif char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index]
    return line


def _remove_escaped(text: str) -> str:
    r"""**转义**的字符**整个去掉**(反斜杠 + 它后面那个字符)。

    **为什么**(第 55 轮之十,codex 第 5 轮的 L-7):shell 里 `\&` 是**字面量**
    `&`(`echo a \& b` 打印 `a & b`)⇒ 但后台检测只看裸 `&`
    ⇒ `\&` 被误判成「丢到后台」⇒ **误伤合法写法**。

    **为什么是「整个去掉」而不是「还原成字面量」**:本函数**只**服务于
    `has_background_operator`(它找的是**操作符**)⇒ 一个被转义出来的
    `&` 是**内容**,不是操作符 ⇒ 把它还原出来就还是会被误判。
    """
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2              # 反斜杠 + 被转义的字符 ⇒ 整个丢掉
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _remove_quoted(text: str) -> str:
    """把**引号里**的内容抹掉(只留引号外的字符)。

    **为什么**(第 55 轮之七,codex 第 4 轮的 L-3):`has_background_operator`
    此前不看引号 ⇒ `--suite "a&b"` 被误判成「后台执行」。
    ⇒ 现在引号里的 `&` / `#` / `>` 都不再是操作符。
    """
    out: list[str] = []
    in_single = False
    in_double = False
    for char in text:
        if in_double:
            if char == '"':
                in_double = False
            out.append(" ")
            continue
        if in_single:
            if char == "'":
                in_single = False
            out.append(" ")
            continue
        if char == "'":
            in_single = True
            out.append(" ")
            continue
        if char == '"':
            in_double = True
            out.append(" ")
            continue
        out.append(char)
    return "".join(out)


def _strip_redirections(line: str) -> str:
    """把**重定向**从命令行里剥掉(`2>&1` / `&>log` / `>out` / `2>/dev/null`)。

    **为什么**(第 55 轮之七,codex 第 4 轮的 M-1 / claude 第 4 轮的 L-1):
    重定向**不影响退出码** ⇒ 它不该让「闸门步骤不是规范形」。
    此前 `2>&1` 被切成**两条**命令 ⇒ 「这一步有两条命令」⇒ 判红;
    `2>/dev/null` 则变成一个多余的词 ⇒ 规范形判红。
    ⇒ 现在:剥掉之后再切分、再判规范形。

    **引号里的 `>` 不算重定向**(例如 `echo "a>b"`)⇒ 逐字符扫,认引号。
    """
    out: list[str] = []
    index = 0
    length = len(line)
    in_single = False
    in_double = False
    while index < length:
        char = line[index]
        if in_double:
            out.append(char)
            if char == '"':
                in_double = False
            index += 1
            continue
        if in_single:
            out.append(char)
            if char == "'":
                in_single = False
            index += 1
            continue
        if char == "'":
            in_single = True
            out.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            out.append(char)
            index += 1
            continue
        if char.isdigit():
            # **文件描述符数字**:`2>&1` / `1>out` ⇒ 那个数字是 fd,**不是参数**
            # (此前只剥掉了 `>` 往后的一段 ⇒ `2` 留在命令行里
            #  ⇒ 闸门命令后面多出一个词 ⇒ 被判「非规范形」⇒ 误伤。)
            end = index
            while end < length and line[end].isdigit():
                end += 1
            if end < length and line[end] == ">":
                index = end          # 数字整个丢掉,交给下面的 ">" 分支
                continue
            out.append(char)
            index += 1
            continue
        if char == ">":
            # `2>&1` / `>&2`:跳过数字、`>`、`&`、后面的数字
            index += 1
            if index < length and line[index] == "&":
                index += 1
                if index < length and line[index].isdigit():
                    index += 1
            elif index < length and line[index].isdigit():
                index += 1
                if index < length and line[index] == ">":
                    index += 1
            # 重定向的**目标**:没有空格就连着,否则到空格为止
            if index < length and line[index] not in " \t":
                while index < length and line[index] not in " \t":
                    index += 1
            out.append(" ")
            continue
        if char == "&" and index + 1 < length and line[index + 1] == ">":
            # `&>log` / `&>>log`
            index += 2
            while index < length and line[index] not in " \t":
                index += 1
            out.append(" ")
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _head_of(tokens: list[str] | None) -> str:
    """命令的**第一个词**(小写、去掉路径)⇒ 判断「这条命令干什么用的」。

    (第 55 轮之七:fail-closed 的判据要问「这条命令**会不会执行** pytest/闸门」,
    而不是「文本里**有没有**这个词」。)
    """
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1].lower()


def _script_identity(script: str) -> str:
    """脚本的**标识**:归一化分隔符 + 去掉 `.py` 后缀。

    **为什么**(第 55 轮之七,codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3):
    `python -m tools.ci_gate` 与 `python tools/ci_gate.py` **是同一个脚本**
    ⇒ 仓库级扫描此前只按**字面**比对 ⇒ 模块形态**完全隐形**
    ⇒ 「别处不许有闸门步骤」这条不变量可以被一步绕过。
    ⇒ 现在两种写法归成**同一个标识**。
    """
    normalized = _normalize_script(script)
    if normalized.endswith(".py"):
        normalized = normalized[:-3]
    return normalized


def _script_base(script: str) -> str:
    """脚本标识的**基名**:`tools/ci_gate` 与 `tools.ci_gate` 都是 `ci_gate`。

    **为什么要这一步**(第 55 轮之七,codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3):
    `python tools/ci_gate.py` 与 `python -m tools.ci_gate` 是**同一个脚本**
    ⇒ 只按**字面**比对 ⇒ 模块形态**完全隐形**
    ⇒ 「别处不许有闸门步骤」这条不变量可以被一步绕过。
    ⇒ 路径分隔符与模块的点号在这里**归一**。
    """
    return _script_identity(script).replace("/", ".").rsplit(".", 1)[-1].lower()


# here-doc(`<<EOF` / `<<'EOF'` / `<<-EOF`)⇒ **正文是数据,不是命令**。
#
# **为什么**(第 55 轮之十四,codex M-2):shell 解析器此前**不认识** here-doc
# ⇒ here-doc 的**正文**被逐行当成**命令**解析
# ⇒ 正文里写一条 `python tools/check_domain.py …`
#   就被当成「裁决者真的跑了」⇒ 工具报「自检本身有人跑」
# ⇒ 而实际上**什么命令都没跑** ⇒ 分档器的关卡等于没有。
_HEREDOC_RE = re.compile(
    r"<<(?P<dash>-?)\s*(?P<quote>['\"]?)(?P<word>[A-Za-z_][A-Za-z0-9_]*)(?P=quote)")


def _heredoc_delimiters(line: str) -> list[tuple[str, str]]:
    """这一行里**引号外**的 here-doc 分隔符(按出现顺序)。

    **为什么要在引号外找**:`echo 'result<<CLAUDE_EOF'`(仓库自己 workflow 里就有)
    ⇒ 引号里的 `<<` **不是** here-doc ⇒ 认成是 ⇒ 后面几行被当正文丢掉
    ⇒ 真命令被漏掉 ⇒ 那是**漏判**,比误伤更糟。
    """
    found: list[tuple[str, str]] = []
    in_single = False
    in_double = False
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if in_single:
            in_single = char != "'"
            index += 1
            continue
        if in_double:
            in_double = char != '"'
            index += 1
            continue
        if char == "\\":
            index += 2
            continue
        if char == "'":
            in_single = True
            index += 1
            continue
        if char == '"':
            in_double = True
            index += 1
            continue
        if char == "<":
            match = _HEREDOC_RE.match(line, index)
            if match is not None:
                found.append((match.group("dash"), match.group("word")))
                index = match.end()
                continue
        index += 1
    return found


def _strip_heredoc_bodies(script: str) -> str:
    """把 **here-doc 的正文**去掉(只去**完整**的块)。

    **为什么「只去完整的块」**:结束符没找到 ⇒ 那个 here-doc 是**坏的**
    ⇒ shell 会报错 ⇒ 那几行**也许**是真的命令
    ⇒ 宁可**多判**(把它们还回来),不可**漏判**。
    """
    lines = script.splitlines()
    kept: list[str] = []
    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        kept.append(line)
        index += 1
        pending = _heredoc_delimiters(line)
        if not pending:
            continue
        body: list[str] = []
        while pending and index < total:
            candidate = lines[index]
            index += 1
            if candidate.strip() == pending[0][1]:
                kept.append(candidate)
                pending.pop(0)
            else:
                body.append(candidate)
        if pending:
            kept.extend(body)              # 没找到结束符 ⇒ 把正文还回来
    return "\n".join(kept)


def _logical_lines(script: str) -> list[str]:
    """把一段 shell 脚本按**续行**接起来 ⇒ **逻辑行**。

    **为什么要有它**(第 55 轮之十一,子代理复核的 C-6):
    `cmd … \\`(换行)`|| true` 在 YAML 的 `run: |` 块里是**两个物理行**
    ⇒ 判据建在物理行上 ⇒ 那条 `|| true` **看不见**
    ⇒ 失败照样被吃掉而守卫报绿。
    ⇒ 现在先把续行接起来 ⇒ 后面按**逻辑行**判。
    """
    lines: list[str] = []
    buf = ""
    # **here-doc 的正文是数据,不是命令**(第 55 轮之十四,codex M-2)
    # ⇒ 先把它们去掉 ⇒ 正文里的裁决者调用不再被当成「真跑了」。
    for raw in _strip_heredoc_bodies(script).splitlines():
        line = _strip_shell_comment(raw.strip()).strip()
        # **重定向先剥掉**(第 55 轮之七):`2>&1` / `2>/dev/null` 不影响退出码
        # ⇒ 不该让「闸门步骤不是规范形」。此前 `2>&1` 被切成两条命令 ⇒ 误伤。
        line = _strip_redirections(line).strip()
        if not line or line.startswith("#"):
            continue
        buf = f"{buf} {line}".strip() if buf else line
        if buf.endswith("\\"):
            buf = buf[:-1].strip()
            continue
        lines.append(buf)
        buf = ""
    if buf:
        lines.append(buf)
    return lines


def logical_commands(script: str) -> list[str]:
    """把一段 shell 脚本切成**逻辑命令行**。

    切点:续行(`\\` 结尾)、换行、`;`、`&&`、`||`、`|`、`&`。

    **不做引号/转义解析**——这是**故意的**:切得比真实 shell **更碎**,
    只会让「危险命令」更容易被认出来(认不出 ⇒ 判红,方向是安全的)。
    (第 54 轮那套按引号配对切分,结果 `bash -c "a; b"` 被当成一条命令 ⇒ 漏。)
    """
    commands: list[str] = []
    for line in _logical_lines(script):
        commands.extend(_split_logical_line(line))
    return commands


def _split_logical_line(line: str) -> list[str]:
    """把一条**逻辑行**切成命令片段。

    **管道给 shell 时不在 `|` 处切**(第 55 轮之十三):
    `awk 'BEGIN{print "python -m pytest …" | "sh"}'`、
    `sed 's/.*/python -m pytest …/' | sh` 这两种写法里
    **真正执行的是管道右侧那个 shell** ⇒ 左侧那几段是它的**输入**
    ⇒ 切在 `|` 上 ⇒ 「管道给 shell」这件事**看不见** ⇒ 左侧那段
    看着像「只读文本」⇒ 被免检 ⇒ **闸门压根没跑而 CI 看上去是绿的**。
    ⇒ 这类行只在 `;` / `&&` / `||` / `&` 处切(管道整体留给同一段)，
    管道右侧那个 shell 与它的输入因此**留在同一段里**。

    **为什么原来漏过**(实测):原先在 `|` 处切 ⇒ 管道右侧那个 shell
    **看不见** ⇒ 判据作用在切完之后的片段上 ⇒ 永远匹配不到
    ⇒ 而 `awk 'BEGIN{print "…" | "sh"}'` 之所以被报出来,
    是因为切开后**引号不配对** ⇒ 分词失败 ⇒ 走的是「认不出来 ⇒ 判红」
    那条**兜底**,**不是**这条判据 ⇒ 见证断言的是**错误的理由**。
    """
    # **管道给 shell 的行** ⇒ `|` **一律不切**(那条 shell 与它的输入留在同一段里)。
    pipe_to_shell = _PIPE_TO_SHELL_RE.search(line) is not None
    parts: list[str] = []
    buf: list[str] = []
    index = 0
    length = len(line)
    in_single = False
    in_double = False
    while index < length:
        char = line[index]
        # **引号状态**只用来判断「这个 `|` 是不是管道」;
        # `;` / `&` / `&&` / `||` 的切点**故意不变**(第 54 轮的教训:
        # 那几处切得比真实 shell 更碎 ⇒ 更碎才安全 ⇒ `bash -c "a; b"` 仍被切开)。
        if char == "'" and not in_double:
            in_single = not in_single
            buf.append(char)
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            buf.append(char)
            index += 1
            continue
        pair = line[index:index + 2]
        if pair in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            index += 2
            continue
        if char == "|":
            # **引号里的 `|` 不是管道**(第 55 轮之十四,codex H-2):
            # `awk 'BEGIN{print "x" | "python -m pytest …"}'` 的 `|` 在**引号里**
            # ⇒ 在那儿切开 ⇒ 两半都**引号不配对** ⇒ 分词失败
            # ⇒ awk 的 co-process(能把**任意命令**执行掉)**完全隐形**。
            if not pipe_to_shell and not in_single and not in_double:
                parts.append("".join(buf))
                buf = []
                index += 1
                continue
            buf.append(char)
            index += 1
            continue
        if char in ";&":
            parts.append("".join(buf))
            buf = []
            index += 1
            continue
        buf.append(char)
        index += 1
    parts.append("".join(buf))
    return [part.strip() for part in parts if part.strip()]


def _tokens(command: str) -> list[str] | None:
    """切词;切不动(引号不配对等)⇒ None(认不出来 ⇒ 交给上层判红)。"""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _normalize_script(path: str) -> str:
    """脚本路径**归一化**:统一分隔符、去掉 `./` 与 `..`。

    **为什么**(第 55 轮之六,codex 第 3 轮的 M3):
    `python ./tools/ci_gate.py`、`python tools/../tools/ci_gate.py`、
    `python .\\tools\\ci_gate.py` 都是**完全等价**的调用
    ⇒ 此前按**字面**比较 ⇒ 误判成「非规范形」⇒ 覆盖塌方 ⇒ 整个工具判红
    ⇒ **误伤合法写法**。
    """
    return os.path.normpath(path.replace("\\", "/")).replace("\\", "/")


def has_background_operator(script: str) -> bool:
    """脚本里有没有「后台执行」的操作符(裸 `&`)。

    **为什么**(第 55 轮之六,codex 第 3 轮的 H1):
    `cmd &` ⇒ shell 把命令丢到后台、**立刻返回 0**
    ⇒ 闸门判红被丢弃(实测:`bash -c 'false &'` 的退出码是 **0**)
    ⇒ 闸门看着绿,而它压根没跑完。

    `&&` 不是后台;`2>&1` / `&>` 是重定向,也不是后台
    ⇒ 正则把它们排除掉(否则会把合法的重定向误判成后台)。
    """
    for raw in script.splitlines():
        line = _strip_shell_comment(raw.strip()).strip()
        # **引号里的 `&` 不是后台执行**(第 55 轮之七,codex 第 4 轮的 L-3):
        # `--suite "a&b"` 此前被误判成「丢到后台」⇒ 误伤。
        if _BACKGROUND_RE.search(_remove_quoted(_remove_escaped(line))):
            return True
    return False


def _errexit_disabled(script: str) -> bool:
    r"""本里有没有**关掉 errexit** 的 `set` 调用。

    **为什么不能只靠一条正则**(第 55 轮之十,codex 第 5 轮的 M-4):
    `set +eo pipefail` 与 `set +e` **一样**关掉 errexit(实测:
    `bash -c 'set -e; set +eo pipefail; false; echo survived'` ⇒ 打印 survived)
    ⇒ 而 `set\s+\+e(?![A-Za-z0-9_])` 的后视把 `+eo` **排除**掉了
    ⇒ 实测**漏过** ⇒ 现在**按 `set` 的参数逐个解析**:

      - `+e` / `+eo` / `+eu` / `+ex` …… ⇒ 关掉 errexit(`+` 后的字母里有 `e`);
      - `+o errexit` ⇒ 关掉 errexit;
      - `-e` / `-o errexit` 是**打开** ⇒ 不算;
      - `+E`(`set +E`)是 **errexit 的继承标志**,**不影响** errexit 本身
        (实测 `bash -c 'set -e; set +E; false'` ⇒ 脚本中止)⇒ 不算。
    """
    for line in _logical_lines(script):
        for _operator, text in _split_segments(line):
            tokens = _tokens(text)
            if not tokens:
                continue
            # ① **词法**:`set` 是某个 token(`if set +e; then …` 的条件里也是 `set`)
            for index, token in enumerate(tokens):
                if token == "set" and _flags_disable_errexit(tokens[index + 1:]):
                    return True
            # ② **字符串**:命令把一整段脚本当参数
            #    (`eval 'set +e'` / `sh -c "set +e"`)⇒ token 切不出来 ⇒ 按文本找。
            #    (第 55 轮之十一,子代理复核的 C-7:此前 `eval` 的参数不逐 token 解析
            #     ⇒ 实测 `eval 'set +e'` **漏过** ⇒ bash 侧 errexit 确实被关掉。)
            if _head_of(tokens) in _SCRIPT_STRING_HEADS and _SET_OFF_RE.search(text):
                return True
    return False


def _protected_call_label(tokens: list[str] | None) -> str | None:
    """这一条命令在跑**受保护脚本**吗?是 ⇒ 返回它的名字;不是 ⇒ None。

    (`python -m check_mutations` 这种**模块形态**也算——它跑的是同一个东西。)
    """
    if not tokens:
        return None
    script = script_of(tokens)
    if script is None:
        return None
    normalized = _normalize_script(script)
    if normalized in _PROTECTED_SCRIPTS:
        return normalized
    # **模块形态也算**(`python -m tools.check_mutations`)——
    # 第 55 轮之七(codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3):
    # 此前只认**基名**,而 `-m` 给的是**点号**名 ⇒ 受保护脚本可以用模块形态
    # 绕过它那一步的纪律。
    base = _script_base(script)
    if base in {"check_mutations", "check_guard_mutations"}:
        return base
    return None


def _self_guard_call_label(command: str) -> str | None:
    """这一条命令在跑**裁决者自己的冒烟关卡**吗?是 ⇒ 返回它的名字。

    **为什么单列一族**(第 55 轮之七,claude 第 4 轮的 L-5):
    `tools/check_domain.py` 决定「哪些文件算研究域、哪些算执行域」
    ⇒ 它是**谁拥有判定点**的裁决者 ⇒ 它自己的冒烟关卡被改坏 ⇒
    **没人发现**。
    ⇒ 但它**不能**套用「受保护脚本」那套「只有一条命令」的规范形——
    仓库里那一步本来就是一个多命令块(`set -e` + 显式 rc 判定 + 结论行自证)
    ⇒ 套上去会**误伤仓库自己**。
    ⇒ 所以只施加**旁路键**纪律:`if:` / `continue-on-error:` / `shell:` / `env:`
    与尾部 `&`(丢后台 ⇒ 退出码被丢弃)。
    """
    script = _command_script(command)
    if script is None:
        return None
    normalized = _normalize_script(script)
    if normalized in _SELF_GUARD_SCRIPTS:
        return normalized
    base = _script_base(script)
    if base in {"check_domain"}:
        return base
    return None


# 前导的**环境赋值**(`PYTHONIOENCODING=cp1252`)不是命令本身。
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _command_script(command: str) -> str | None:
    """这条命令**实际**跑的是哪个脚本(尽力而为;认不出就 None)。

    **为什么要多这一层**(第 55 轮之七,claude 第 4 轮的 L-5):
    仓库里分档器冒烟那一步的命令长这样:
        `PYTHONIOENCODING=cp1252 python tools/check_domain.py scripts/optimize.py`
        `out=$(python tools/check_domain.py --diff "origin/…" 2>&1) || rc=$?`
    ⇒ 首个 token 分别是「环境赋值」与「`out=$(python`」⇒ 直接按
    `tokens[0]` 找解释器**认不出来** ⇒ 那一步**根本不会被当成裁决者关卡**
    ⇒ 规则在真实 workflow 上**形同虚设**(仓库教训:规则必须在**真实**代码上
    生效,不能只在合成的样例上生效)。
    ⇒ 这里把这两层包装剥掉,再交回 `script_of` 定位**命令位置**上的脚本。
    """
    text = command.strip()
    tokens = _tokens(text)
    if not tokens:
        return None
    tokens = list(tokens)
    # 前导的环境赋值 ⇒ 剥掉
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]) and "$(" not in tokens[0]:
        tokens.pop(0)
    if not tokens:
        return None
    # `out=$(python …` ⇒ 取 `$(` 之后的解释器
    if "$(" in tokens[0]:
        tokens[0] = tokens[0].split("$(")[-1].strip()
    if not tokens[0]:
        return None
    return script_of(tokens)


def script_of(tokens: list[str]) -> str | None:
    """命令**位置**上的那个脚本(解释器/运行器后面的)。

    - `python -m pytest` ⇒ `pytest`;`python3.12 -m pytest` ⇒ `pytest`;
    - `python -W error -m pytest` ⇒ `pytest`(**会吃值的选项**要连值一起跳过,
      否则 `error` 被当成脚本——codex 第 2 轮的 M2);
    - `uv run python -m pytest` ⇒ `pytest`;
    - `bash -c "…"` ⇒ `None`(**认不出来**——这正是第 55 轮要判红的那种)。
    """
    if not tokens:
        return None
    head = tokens[0].rsplit("/", 1)[-1].lower()
    # **走共享的那一份**(第 55 轮之二十四):`.exe` 后缀与反斜杠路径也认。
    if is_python_interpreter_name(tokens[0]):
        index = 1
        while index < len(tokens):
            token = tokens[index]
            if token == "-m":
                module = tokens[index + 1] if index + 1 < len(tokens) else None
                return _module_identity(module)
            if token in _VALUE_FLAGS:
                index += 2                       # 跳过选项**和它的值**
                continue
            if token.startswith("-"):
                index += 1
                continue
            return token
        return None
    if head in _RUNNERS:
        for index, token in enumerate(tokens):
            if token in ("run", "exec") and index + 1 < len(tokens):
                rest = []
                for candidate in tokens[index + 1:]:
                    if candidate.startswith("-"):
                        if "=" not in candidate:
                            continue
                        rest.append(candidate.split("=", 1)[1])
                        continue
                    rest.append(candidate)
                return script_of(rest)
        return None
    if head == "pytest":
        return "pytest"
    return None


def _module_identity(module: str | None) -> str | None:
    """模块名归一:`pkg.__main__` ⇒ `pkg`(它跑的就是那个包)。

    **为什么**(第 55 轮之十一,子代理复核的 C-1):
    `python -m pytest.__main__` **真的跑 pytest**(实测:`--version` ⇒ pytest 8.4.2)
    ⇒ 而 `_script_base("pytest.__main__")` 取最后一段得 `__main__`
    ⇒ 既不等于 `pytest` 也不含 `pytest` ⇒ 落到 `other` ⇒ **放行**
    ⇒ 同一族写法:`python -m pytest` 判红、`python -m pytest.__main__` 判绿。
    """
    if not module:
        return None
    if module.endswith(".__main__"):
        return module[: -len(".__main__")]
    return module


def _module_flag_value(tokens: list[str] | None) -> str | None:
    """这条命令是不是**模块形态**(`python -m 包.模块`)?是 ⇒ 返回那个模块名。"""
    if not tokens:
        return None
    if not is_python_interpreter_name(tokens[0]):
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-m":
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if token in _VALUE_FLAGS:
            index += 2                       # 跳过选项**和它的值**
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None                          # 第一个非选项就是脚本 ⇒ 不是模块形态
    return None


# `git` 的**检索类**子命令(只读文本,不会执行任何东西)。
_GIT_INERT_SUBCOMMANDS = {
    "grep", "log", "diff", "show", "status", "ls-files", "rev-parse",
    "blame", "describe", "cat-file", "shortlog", "branch", "ls-tree",
    "for-each-ref", "count-objects", "config",
}
# `git -c <键>=<值>` / `git --git-dir=…` 这类**吃值**的选项。
_GIT_VALUE_OPTIONS = {"-c", "-C", "--git-dir", "--work-tree", "--exec-path"}
# `sed` 的 `e` 标志:它把模式空间当**命令**执行 ⇒ `sed 's/x/y/e'` 能执行。
#
# **第 55 轮之十二(codex H-2)**:原先**只认被引号包住**的那一种形态 ⇒
# `sed -e s/a/pytest/e x.txt`(不引号)、`sed -e s,a,pytest,e x.txt`(逗号分隔)、
# `sed -e e x.txt`(独立的 `e` 命令)这三种**等价**的可执行写法**全部漏过**
# ⇒ 现在按「**标志位里有 e**」判断:分隔符是 `/` 还是 `,`、引号在不在,都认。
_SED_EXEC_RE = re.compile(
    r"""(?x)
    (?: ['"][^'"]* s (?P<q> [^'"] )                                  # 引号里的 s<分隔符>…
      [^'"]* (?P=q) [^'"]* (?P=q) [a-z0-9]* e [a-z0-9]* ['"]        # …<分隔符>…<分隔符>标志位
    | (?: ^ | [\s;&|(] )
      s (?P<d> [^\s] )                                              # `s` 的分隔符(/ 或 ,)
      (?: (?! (?P=d) ) [^\s] )*?                                    # 第一段(正则)
      (?P=d)
      (?: (?! (?P=d) ) [^\s] )*?                                    # 第二段(替换)
      (?P=d)
      [a-z0-9]* e [a-z0-9]*                                          # 标志位里有 `e`
      (?: [\s;&|)] | $ )                                             # 边界
    | \s-e\s+e (?: [\s;&|)] | $ )                                  # 独立的 `e` 命令
    )""")
# **管道给 shell** ⇒ 能执行(第 55 轮之十三):
# `awk 'BEGIN{print "…" | "/bin/sh"}'`(管道在**引号里**)、
# `awk 'BEGIN{print "…"}' | sh`、`sed 's/.*/python -m pytest …/' | sh`
# (管道在**引号外**)——这两种形态里**真正执行的是管道右侧那个 shell**
# ⇒ 左侧那几段是它的**输入** ⇒ 不是「只读文本」。
#
# **为什么改名**:原先叫 `_AWK_PIPE_TO_SHELL_RE` ⇒ 看着只管 awk
# ⇒ 其实这条判据与**命令头无关**:任何「只读命令」被管道给 shell,
# 它就**不再是**只读的(`echo 载荷 | bash` 一样能执行)。
# (原先它被写在 `awk` 分支里 ⇒ 只对 awk 生效 ⇒ `echo … | bash` 漏过。)
_PIPE_TO_SHELL_RE = re.compile(
    r"\|\s*[\"']?(?:/usr/bin/|/bin/)?(?:sh|bash|zsh|dash|ksh)\b")
# `sed` 的 `e` **命令**(不是 `e` **标志**):GNU sed 支持 `e [COMMAND]`
# ⇒ `sed -e 'e python -m pytest …'` 会把**内联的那条命令**执行掉。
#
# **为什么**(第 55 轮之十四,codex M-1):此前只认 `s/x/y/e` 那个**标志位**
# ⇒ `e` **命令**形态漏过 ⇒ 而 ADR 把这条残余写成「载荷必须来自输入文件」
# ⇒ **描述低估了实际可执行形态**(仓库教训㉝:「洞」与「描述」分开验收)。
_SED_E_COMMAND_RE = re.compile(r"(?:^|[;{}])\s*e(?:\s|$)")
# awk 的 **co-process**:`|` 右侧那个**字符串**被当命令执行。
_AWK_COPROCESS_RE = re.compile(r"\|\s*[\"']")


def _sed_executes(tokens: list[str], command: str) -> bool:
    """这条 `sed` **会执行**东西吗?

    - `s/x/y/e`(`e` **标志**)⇒ 会;
    - `e [COMMAND]`(`e` **命令**)⇒ 会;
    - 其余 ⇒ 不会。
    """
    if _SED_EXEC_RE.search(command) is not None:
        return True
    for token in tokens[1:]:
        if token.startswith("-"):
            # `--expression=e cmd` 这种**取值**形态
            if "=" in token:
                value = token.split("=", 1)[1]
                if _SED_E_COMMAND_RE.search(value.strip()):
                    return True
            continue                     # 选项(`-e` 的**值**是下一个 token)
        # 第一个**非选项** token 就是 sed 程序
        return _SED_E_COMMAND_RE.search(token.strip()) is not None
    return False


def _awk_has_coprocess(tokens: list[str]) -> bool:
    """awk 程序里有 **co-process**(`| "命令"`)吗?"""
    for token in tokens[1:]:
        if token.startswith("-"):
            continue
        if "{" in token and _AWK_COPROCESS_RE.search(token):
            return True
    return False
# **第三方动作**(第 55 轮之十二,codex H-4):
# `uses:` 一步可以执行**任意代码** ⇒ 它比 `run:` 更危险:
# YAML 里**连 `pytest` / `ci_gate` 字样都不必有**。
# ⇒ 现在**逐个登记**允许的第三方动作;不在名单里的 ⇒ 判红。
# (本仓库自己的动作(`./.github/actions/…`)不在此列 ⇒ 它们的 `run:` 块另有纪律。)
_ALLOWED_THIRD_PARTY_ACTIONS = {
    "actions/add-to-project@v2.0.0",
    "actions/cache@v4",
    "actions/checkout@v4",
    "actions/github-script@v7",
    "actions/setup-node@v4",
    "actions/setup-python@v5",
    "laiyinyizao007/my-project-management/.github/workflows/reusable-dedup.yml@main",
    "laiyinyizao007/my-project-management/.github/workflows/reusable-tasklist.yml@main",
}


def _git_is_inert(tokens: list[str]) -> bool:
    """这个 `git` 调用**只读文本**(不会执行任何东西)吗?"""
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            if token in _GIT_VALUE_OPTIONS and index + 1 < len(tokens):
                index += 2
                continue
            index += 1
            continue
        return token.lower() in _GIT_INERT_SUBCOMMANDS
    return False


def _is_inert_command(tokens: list[str] | None, command: str) -> bool:
    """这条命令**只读文本/改文本**,不会执行 pytest 或闸门吗?

    **为什么**(第 55 轮之十一,子代理复核的 M-1):
    第 55 轮之十把 `awk`/`sed`/`find`/`git` 从「不执行类命令」白名单里**移出**
    (它们**能**执行 ⇒ 白名单等于给了它们一条免检通道)⇒ 但「**能**」不等于
    「这一条**真的**执行了」⇒ `git grep -n pytest tests/`、
    `sed -i 's/pytest/PYTEST/' x.py`、`pip install pytest==8.3.4`
    这些**不可能执行 pytest** 的写法**一律判红** ⇒ **误伤**
    ⇒ 而且判据取决于「怎么写」而不是「会不会执行」⇒ 与 R5 的立意相反。
    ⇒ 现在**按形态**判断:同一个命令头,「检索/改写」形态放行,
    「能执行」的形态(`git -c alias` / `sed '…e'` / `find -exec` /
    `awk 'BEGIN{system(…)}'`)判红。
    """
    if tokens is None:
        return False
    # **管道给 shell ⇒ 这条命令**不是**「只读文本」(第 55 轮之十三):
    # 管道右侧那个 shell 会把左侧的**输出当命令执行**
    # ⇒ `echo "python -m pytest …" | bash` 与
    #   `awk 'BEGIN{print "python -m pytest …"}' | sh` **都能执行**
    # ⇒ 必须在下面那个「只读名单」**之前**判掉
    # (名单里就有 `echo`/`cat`/`grep` ⇒ 不提前判掉就等于给它们一条
    #  「管道给 shell 就变执行器」的免检通道。)
    if _PIPE_TO_SHELL_RE.search(command):
        return False
    head = _head_of(tokens)
    # **`rg --pre` / `--pre-glob` 会执行预处理器**(第 55 轮之十二,codex H-3A)
    # ⇒ 它**不是**「只读文本」那一类 ⇒ 必须在下面那个名单**之前**判掉。
    # **第 55 轮之十四(codex H-3)**:`--pre=COMMAND` 是 ripgrep 的**等价写法**
    # ⇒ 此前只认**独立的** `--pre` token ⇒ `rg --pre='python -m pytest …'`
    #    被当成「只读检索」⇒ **漏过**。
    if head == "rg" and any(token == "--pre" or token.startswith("--pre=")
                              for token in tokens):
        return False
    if head in _NON_EXECUTING_HEADS:
        return True
    if head == "git":
        # `git -c alias.x='!…'` / `git alias` ⇒ **能执行** ⇒ 不放行
        if re.search(r"\balias\b", command):
            return False
        return _git_is_inert(tokens)
    # `awk -f prog.awk` / `sed -f prog.sed`:程序在**别的文件**里
    # ⇒ 「只读文本」这个保证**看不见内容** ⇒ 不再给免检
    # ⇒ 与 `python x.py` 同等待遇(文本里提到 pytest/闸门 ⇒ 判红)。
    if head in ("awk", "sed") and "-f" in tokens:
        return False
    if head == "sed":
        return not _sed_executes(tokens, command)
    if head == "find":
        return "-exec" not in command and "-execdir" not in command
    if head == "awk":
        # `awk 'BEGIN{system("…")}'` ⇒ 能执行(**直接**调用 shell)
        # (「管道给 shell」那种形态已由**函数最前面**那条判据兜住
        #  ⇒ 这里不需要、也**不能**再判一次。)
        if "system(" in command:
            return False
        # **co-process**(第 55 轮之十四,codex H-2):
        # `awk 'BEGIN{print "x" | "python -m pytest …"}'` ⇒ awk 把 `|` 右侧
        # 那个**字符串**当**命令**执行 ⇒ 与 `| sh` **等价**
        # ⇒ 它**不是**「只读文本」⇒ 不再给它免检通道。
        if _awk_has_coprocess(tokens):
            return False
        return True
    if head == "pip":
        return True                         # `pip install …` 不执行 pytest
    return False


def _split_segments(line: str) -> list[tuple[str, str]]:
    """把一条**逻辑行**切成 `(运算符, 片段)`;**引号里的运算符不算**。

    **为什么要有引号感知**(第 55 轮之十一,子代理复核的 C-6):
    仓库自己的裁决者那一步是
        out=$(python tools/check_domain.py --diff "origin/${{ github.base_ref || 'main' }}" 2>&1) || rc=$?
    ⇒ 那个 `||` 在**双引号里**(GitHub 表达式)⇒ 不切分才不会误伤。

    **与 `logical_commands` 的区别**:那个函数**故意**切得比真实 shell 更碎
    (引号不解析)⇒ 用来「认出危险命令」;这个函数要回答的是
    「**这条逻辑行的退出码由谁决定**」⇒ 必须认引号。
    """
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    op = ""
    index = 0
    length = len(line)
    in_single = False
    in_double = False
    while index < length:
        char = line[index]
        if in_double:
            buf.append(char)
            if char == '"':
                in_double = False
            index += 1
            continue
        if in_single:
            buf.append(char)
            if char == "'":
                in_single = False
            index += 1
            continue
        if char == "'":
            in_single = True
            buf.append(char)
            index += 1
            continue
        if char == '"':
            in_double = True
            buf.append(char)
            index += 1
            continue
        pair = line[index:index + 2]
        if pair == "&&" or pair == "||":
            segments.append((op, "".join(buf).strip()))
            buf = []
            op = pair
            index += 2
            continue
        if char in ";&|":
            segments.append((op, "".join(buf).strip()))
            buf = []
            op = char
            index += 1
            continue
        buf.append(char)
        index += 1
    segments.append((op, "".join(buf).strip()))
    return [(operator, text) for operator, text in segments if text]


# 「捕获退出码」:`rc=$?` ⇒ 失败**被捕获**(后面还要拿它判)⇒ 不是「被吃掉」。
#
# **引号/前缀形态也是捕获**(第 55 轮之十二,codex M-2):
# `rc="$?"` / `rc='$?'` / `export rc=$?` / `local rc=$?` 与 `rc=$?` **同义**
# ⇒ 此前只认最后一种 ⇒ 等价的合法写法被误判成「失败被吃掉」。
# ⇒ 这里**只放宽语法形态**,**不放宽语义**(右操作数仍然必须是 `$?`)。
_CAPTURE_RE = re.compile(
    r"^(?:(?:export|local|readonly|typeset)\s+|declare\s+(?:-\w+\s+)*)?"
    r"[A-Za-z_][A-Za-z0-9_]*=(?:\"\$\?\"|'\$\?'|\$\?)$")
# 把一整段脚本当参数的命令 ⇒ 里面的 `set +e` 之类只能按**文本**找。
_SCRIPT_STRING_HEADS = {"eval", "sh", "bash", "zsh", "dash", "ksh", "source", "."}
# `set +e` / `set +eo pipefail` / `set +o errexit` 的**文本**形态。
_SET_OFF_RE = re.compile(r"\bset\s+\+(?:[A-Za-z]*e[A-Za-z]*|o\s+errexit)\b")
# 单行的 `if …; then …; fi` ⇒ 开与关在**同一行**⇒ 嵌套深度**不**增加。
_CONDITIONAL_CLOSE_ANY_RE = re.compile(r"\b(?:fi|done)\b")
# **函数定义行**(`foo() {` / `function foo {`)⇒ 下一行起在**函数体**里
# ⇒ 里面的调用**未必**会被执行(第 55 轮之十一,子代理复核的 C-5)。
_FUNCTION_DEF_END_RE = re.compile(
    r"^\s*(?:function\s+)?[\w.:-]+\s*(?:\(\s*\))?\s*\{\s*$")
# 函数定义(`run_gate() {` / `function run_gate {`)—— 调用在它**里面**⇒ 未必会被执行。
# (判据落在 `_call_is_live_in_line`:调用**前面**有没有一个没关上的 `{`。)


def _is_capture(text: str) -> bool:
    """这个右操作数是**捕获**退出码(而不是把失败吃掉)吗?"""
    return bool(_CAPTURE_RE.match(text.strip()))


def _flags_disable_errexit(args: list[str]) -> bool:
    """`set` 的这些参数会**关掉 errexit**吗?

    - `+e` / `+eo` / `+eu` …… ⇒ 关掉(`+` 后的字母里有 `e`);
    - `+o errexit` ⇒ 关掉;
    - `-e` / `-o errexit` 是**打开** ⇒ 不算;
    - `+E`(errexit 的继承标志)**不影响** errexit ⇒ 不算。
    """
    expect_errexit = False
    for arg in args:
        if arg == "+o":
            expect_errexit = True
            continue
        if expect_errexit:
            expect_errexit = False
            if arg == "errexit":
                return True
            continue
        if arg.startswith("+") and "e" in arg[1:]:
            return True
        if arg.startswith("-"):
            continue
        break                        # `set` 的位置参数(操作数)⇒ 停
    return False


def _command_script_and_args(command: str) -> tuple[str | None, list[str]]:
    """这条命令跑的脚本 + 它**后面**的参数(去掉解释器选项与脚本本身)。"""
    tokens = _tokens(command.strip())
    if not tokens:
        return (None, [])
    tokens = list(tokens)
    while tokens and _ENV_ASSIGN_RE.match(tokens[0]) and "$(" not in tokens[0]:
        tokens.pop(0)
    if not tokens:
        return (None, [])
    if "$(" in tokens[0]:
        tokens[0] = tokens[0].split("$(")[-1].strip()
    script = script_of(tokens)
    if script is None:
        return (None, [])
    index = None
    for position, token in enumerate(tokens):
        if token == script:
            index = position
            break
    if index is None:
        index = 1 if len(tokens) > 1 else 0
    return (script, tokens[index + 1:])


def _self_guard_call_is_adjudication(command: str) -> bool:
    """这一条裁决者调用**真的在裁决**吗?

    **为什么**(第 55 轮之十一,子代理复核的 C-4):
    `python tools/check_domain.py --help` ⇒ argparse 直接退出 0、**不做任何分档**
    ⇒ 此前它满足「这个 workflow 里有裁决者冒烟调用」⇒ 分档器的关卡
    **等于没有**,而工具照样报「自检本身也有人跑」。
    ⇒ 现在:调用必须带**至少一个**参数(且不是帮助选项)⇒ 才算真的裁决。
    """
    _script, args = _command_script_and_args(command)
    if not args:
        return False
    if all(arg in ("-h", "--help") for arg in args):
        return False
    return True


def _call_is_live_in_line(line: str, call: str) -> bool:
    """这一条调用**真的会被执行**吗?

    **为什么**(第 55 轮之十一,子代理复核的 C-5):
    `never_called() { python tools/check_domain.py …; }` —— 调用在**函数体**里
    而那个函数**从未被调用** ⇒ 它压根不会跑。
    ⇒ 判据:调用**前面**有没有一个没关上的 `{` ⇒ 有 ⇒ 它在函数体里。
    """
    position = line.find(call)
    if position < 0:
        return True
    return "{" not in line[:position]


def _self_guard_commands_in_run(raw_run: str) -> list[str]:
    """这个 run 块里**提到**裁决者冒烟的命令(**引号感知**切段)。

    **为什么引号感知**(第 55 轮之十一,子代理复核的 C-5):
    `logical_commands` **故意**切得比真实 shell 更碎(不认引号)
    ⇒ `out=$(python tools/check_domain.py --diff "origin/${{ a || 'main' }}" 2>&1)`
    会被那个表达式里的 `||` **切成两半** ⇒ 那条调用**认不出来**
    ⇒ 「它到底跑没跑」判不了。
    ⇒ 这里用 `_split_segments`(认引号)⇒ 认得出来。
    """
    commands: list[str] = []
    for line in _logical_lines(raw_run):
        for _operator, text in _split_segments(line):
            if _self_guard_call_label(text) is not None:
                commands.append(text)
    return commands


# `exit` 的**等价写法**:它们**同样终止这个 shell** ⇒ 之后的段是**死代码**。
#
# **为什么**(第 55 轮之十四,codex H-1):此前只认**字面** `exit`
# ⇒ `eval "exit 0"` / `builtin exit 0` / `command exit 0` 这些**等价**写法
#    **漏过** ⇒ 裁决者那一步压根走不到,而守卫照样报「自检本身有人跑」。
_EXIT_WORD_RE = re.compile(r"\bexit\b")


def _segment_terminates_shell(text: str) -> bool:
    """这一段会不会**终止这个 shell**?

    - `exit …` ⇒ 会;
    - `eval "exit 0"` / `builtin exit 0` / `command exit 0` ⇒ **也会**
      (同一个内置命令,只是换了写法);
    - `exec <命令>` ⇒ shell 被**替换** ⇒ 后面的段**永远不会执行**
      (`exec` 后面**没有**命令时它只改重定向 ⇒ 不终止)。

    **为什么不算 `python -c 'sys.exit(0)'`**:那只是**那个 python 进程**退出
    ⇒ shell 继续往下跑 ⇒ 后面的段**仍然会执行** ⇒ 不算死代码。
    """
    tokens = _tokens(text)
    if not tokens:
        return False
    head = _head_of(tokens)
    if head == "exit":
        return True
    if head in ("eval", "builtin", "command"):
        return bool(_EXIT_WORD_RE.search(text))
    if head == "exec":
        return any(not token.startswith(">") for token in tokens[1:])
    return False


def _live_self_guard_calls(raw_run: str) -> list[str]:
    """这个 run 块里**真的会被执行**的裁决者调用。

    **为什么**(第 55 轮之十一,子代理复核的 C-5):
    此前只数「文本里出现了调用」⇒
      - `exit 0` **之后**的调用(死代码)、
      - **从未被调用**的函数体里的调用,
    都算数 ⇒ 分档器一步都没跑,而守卫照样报「自检本身有人跑」。
    ⇒ 现在:调用必须在顶层 `exit` **之前**、且**不在函数体里**。
    """
    calls: list[str] = []
    dead = False
    depth = 0
    in_function = False
    for line in _logical_lines(raw_run):
        stripped = line.strip()
        if (_CONDITIONAL_OPEN_RE.match(stripped)
                and not _CONDITIONAL_CLOSE_ANY_RE.search(stripped)):
            depth += 1
        if not dead and depth == 0 and not in_function:
            # **引号感知的切段** ⇒ GitHub 表达式里的 `||` 不把命令切碎
            # (`out=$(python … "origin/${{ a || 'main' }}" 2>&1)` 要认得出)。
            for _operator, text in _split_segments(line):
                # **同一逻辑行里 `exit` 之后的命令是死代码**
                # (第 55 轮之十二,codex H-1):`exit 0; python tools/check_domain.py …`
                # ⇒ 裁决者那一步**压根走不到** ⇒ 而此前是「先收调用、后看 exit」
                # ⇒ 恒把它当成**活调用** ⇒ 分档器的关卡等于没有。
                #
                # **只 break 掉本行后面的段**:`dead` 是**跨行**的(归下面那个循环管)
                # ⇒ 跨行的 `exit` 语义**一点没变** ⇒ 已有的那条变异仍然有效。
                if _segment_terminates_shell(text):
                    break
                if _self_guard_call_label(text) is None:
                    continue
                if not _call_is_live_in_line(line, text):
                    continue
                calls.append(text)
        if depth == 0:
            for _operator, text in _split_segments(line):
                if _segment_terminates_shell(text):
                    dead = True
        if stripped == "}":
            in_function = False
        elif _FUNCTION_DEF_END_RE.match(stripped):
            in_function = True
        if _CONDITIONAL_CLOSE_RE.match(stripped):
            depth = max(0, depth - 1)
    return calls


def _logical_line_groups(script: str) -> list[tuple[str, list[str]]]:
    """逻辑行 + 它是由哪几**物理行**拼起来的。

    **为什么要有它**(第 55 轮之十一,子代理复核的 C-6):
    新判据要按**逻辑行**判(续行接起来)⇒ 但**旧**判据按**物理行**判
    ⇒ 要避免同一件事**报两遍**,就得知道一条逻辑行由哪些物理行拼成
    ⇒ 旧判据已经报过的形状 ⇒ 新判据**不再重复报**。
    """
    groups: list[tuple[str, list[str]]] = []
    buf = ""
    sources: list[str] = []
    for raw in script.splitlines():
        line = _strip_shell_comment(raw.strip()).strip()
        line = _strip_redirections(line).strip()
        if not line or line.startswith("#"):
            continue
        buf = f"{buf} {line}".strip() if buf else line
        sources.append(line)
        if buf.endswith("\\"):
            buf = buf[:-1].strip()
            continue
        groups.append((buf, sources))
        buf = ""
        sources = []
    if buf:
        groups.append((buf, sources))
    return groups


def _self_guard_swallow_problems(self_guard: str, raw_run: str) -> list[str]:
    """分档器调用的失败会不会被**吃掉**?(捕获 ≠ 吃掉)

    **为什么**(第 55 轮之十一,子代理复核的 C-6 / H-1 / H-2):
    此前这一族判据建在**物理行**上 ⇒ 三条通道漏过(全部实测):
      - `cmd … \\`(换行)`|| true` ⇒ 续行把 `|| true` 推到下一行;
      - `cmd || /bin/true` ⇒ 正则只认字面 `true`;
      - `cmd || echo skipped` ⇒ **任何**「退出码必为 0」的右操作数都等价。
    ⇒ 现在按**逻辑行**切段(引号里的运算符不算)⇒ 判据问的是
    「**失败会不会被吃掉**」,而不是「后缀长什么样」。

    **不误伤**:`out=$(python tools/check_domain.py --diff "…" 2>&1) || rc=$?`
    —— `||` 右边是**捕获**(把退出码存下来,后面还要拿它判 2)⇒ 不是吃掉。
    """
    problems: list[str] = []
    for logical, sources in _logical_line_groups(raw_run):
        # **旧判据已经报过的形状不重复报**:
        # (`|| true` 的行尾形态由 R17 报;`; true` / `|| true && …` 等由
        #  `_self_guard_failure_semantics` 报。
        #  注意:只有**同一行**里既有 `check_domain` 又有那个后缀才算「已报」
        #  ⇒ 续行把后缀推到**下一行**的情形不会被这里漏掉。)
        already = any("check_domain" in source
                      and (_OR_TRUE_RE.search(source) or _SWALLOW_RE.search(source))
                      for source in sources)
        if already:
            continue
        segments = _split_segments(logical)
        for index, (_operator, text) in enumerate(segments):
            if "check_domain" not in text:
                continue
            for next_operator, next_text in segments[index + 1:]:
                if next_operator == "&&":
                    break                     # `cmd && next` ⇒ 失败**照样传播**
                if next_operator in ("||", ";", "|", "&"):
                    if _is_capture(next_text):
                        break                 # 捕获 ⇒ 不是吃掉
                    problems.append(
                        "分档器冒烟(%s)那一步里,分档器调用的失败**被吃掉**"
                        "(后面跟着 `%s %s` ⇒ 分档器说「环境不可用」时这一步照样绿)"
                        % (self_guard, next_operator, next_text[:60]))
                    break
    return problems


def _protected_call_path_problem(tokens: list[str] | None, label: str) -> str | None:
    """这个受保护脚本的调用**路径**是不是规范路径?不是 ⇒ 判红。

    **为什么**(第 55 轮之十一,子代理复核的 H-5):
    此前只按**基名**认 ⇒ 在**仓库根**放一个 4 行的 `check_mutations.py`
    (内容 `print(...); sys.exit(0)`)⇒ `python check_mutations.py --verify`
    **照样合规** ⇒ 「142 种变异全部被抓住」这句话**换成了一个空壳**。
    ⇒ 现在:文件形态必须**正好**是 `tools/<基名>.py`(且文件确实在仓库里),
    模块形态必须**正好**是 `tools.<基名>`。
    """
    expected = _protected_canonical_path(label)
    module = _module_flag_value(tokens)
    if module is not None:
        wanted = expected.replace("/", ".")[:-3]      # tools/x.py ⇒ tools.x
        if module != wanted:
            return ("受保护脚本(%s)被写成了**模块形态 `%s`** ⇒ 它必须正好是 `%s`"
                    "⇒ 仓库根放一个同名空壳就能把这条证据链换掉"
                    % (label, module, wanted))
        return None
    script = script_of(tokens) if tokens else None
    if script is None:
        return None
    if _normalize_script(script) != _normalize_script(expected):
        return ("受保护脚本(%s)的调用路径**不是**规范路径(应当是 %s)⇒ 仓库根放一个"
                "同名空壳就能把这条证据链换掉" % (label, expected))
    if not (ROOT / expected).exists():
        return ("受保护脚本(%s)在仓库里**不存在**(%s)⇒ 这条证据链是空的"
                % (label, expected))
    return None


def command_kind(tokens: list[str] | None, raw: str) -> str:
    """一条命令的类别。

    `pytest` / `gate` / `wiring` / `lint` / `other` / `unknown`。

    **`unknown` = 认不出来**——上层据此 fail-closed(见模块说明)。
    """
    if tokens is None:
        return "unknown"
    script = script_of(tokens)
    if script is None:
        return "unknown"
    # **标识**(`tools/ci_gate.py` / `ci_gate.py` / `tools.ci_gate` 归成同一个)
    # ⇒ 模块形态(`python -m tools.ci_gate`)不再隐形
    # (第 55 轮之七,codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3)。
    base = _script_base(script)
    if base == "pytest":
        return "pytest"
    if base in ("check_ci_ran", "ci_gate"):
        return "gate"
    if base == "check_ci_wiring":
        return "wiring"
    if base in ("ruff", "flake8", "pylint"):
        return "lint"
    return "other"


def _split_interpreter(tokens: list[str] | None) -> tuple[int, str | None]:
    """把「解释器 + 它的选项」与「脚本」分开。

    返回 `(脚本所在的下标, 脚本名)`;认不出 ⇒ `(-1, None)`。

    **允许的选项**(第 55 轮之五,codex 第 2 轮的 M1):
    - 取值的(`-W` / `-X`):选项**和它的值**一起跳过;
    - 不取值的(`-3` / `-u` / `-B` / `-E` / `-s` / `-I` / `-O` …):只跳选项。

    **禁止的选项**:
    - `-c`:**它把脚本整个换掉**(`python -c "…"` ⇒ 脚本压根没跑)⇒ 判红;
    - `-m`:**它把脚本换成模块** ⇒ 跑的压根不是那个脚本 ⇒ 判红。

    **连写的取值选项**(第 55 轮之六,claude 第 3 轮的 L-2):
    CPython **也接受** `-Werror` / `-Xutf8` 这种「选项和值连在一起」的写法
    ⇒ 此前一律判红(误伤)⇒ 现在**认**。
    """
    if not tokens:
        return (-1, None)
    if not is_python_interpreter_name(tokens[0]):
        return (-1, None)
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _FORBIDDEN_INTERPRETER_FLAGS:
            return (-1, None)
        if token in _VALUE_INTERPRETER_FLAGS:
            index += 2                       # 跳过选项**和它的值**
            continue
        if any(token.startswith(flag) and len(token) > len(flag)
               for flag in _VALUE_INTERPRETER_FLAGS):
            index += 1                       # 连写形式:`-Werror` / `-Xutf8`
            continue
        if token in _NO_VALUE_INTERPRETER_FLAGS or re.match(r"^-\d+(\.\d+)*$", token):
            index += 1
            continue
        break
    if index >= len(tokens):
        return (-1, None)
    return (index, tokens[index])


def canonical_wiring(tokens: list[str] | None, script: str = _CANONICAL_WIRING_SCRIPT) -> bool:
    """这一条命令是不是**规范形的守卫调用**?

    规范形:`python <脚本>`,**一个选项都不许带**。

    **为什么不许带选项**(第 55 轮**实测**出来的洞):
    `main()` 里若允许 `--workflow` 缩小被检查的文件集,守卫就**可以被自己收窄**:
    把 workflow 里的调用改成 `python tools/check_ci_wiring.py --workflow <别处文件>`
    ⇒ 真实的两个 workflow 压根没被查 ⇒ 而工具**照样报「[OK]」**。
    这不是「多了一个选项」的小事——它让守卫**可以被一键关掉**。

    `script` 默认是接线自检自己;宪法 workflow 里那个落点是
    `tools/check_constitution_history.py`(纯标准库,不需要装 PyYAML)。

    **解释器选项**(第 55 轮之五):`python -X utf8 …`、`py -3 …` 这类**合法**写法
    现在**算规范形**(此前一律判红 ⇒ 误伤);但 `-c` / `-m` 仍然判红
    (它们把脚本整个换掉 ⇒ 守卫压根没跑)。
    """
    index, found = _split_interpreter(tokens)
    if found is None:
        return False
    if _normalize_script(found) != _normalize_script(script):
        return False
    return index == len(tokens) - 1          # 脚本后面**不许**再有任何词


def _protected_requirement(label: str | None) -> tuple[str, ...] | None:
    """这个受保护脚本**必须**带的选项;不是受保护脚本 ⇒ None。

    `label` 可能是**全路径**(`tools/check_mutations.py`)也可能是**基名**
    (`check_mutations`——`_protected_call_label` 对模块形态也认)。
    """
    if not label:
        return None
    if label in _PROTECTED_REQUIRED_OPTIONS:
        return _PROTECTED_REQUIRED_OPTIONS[label]
    # **模块形态的基名**(`check_guard_mutations`)也要能找到那份要求。
    #
    # **为什么**(第 55 轮之十,codex 第 5 轮的 H-1):此前这里按**带 `.py` 的
    # 基名**比对(`"check_guard_mutations.py" != "check_guard_mutations"`)
    # ⇒ 模块形态**永远查不到** ⇒ `--verify` 那条要求**整条被跳过**
    # ⇒ `python -m tools.check_guard_mutations`(连 `--verify` 都没有)
    # **照样放行**(实测 8 个变体全部漏过)
    # ⇒ 而 `--verify` 才是「ADR 的变异表与脚本**双向核对**」的那一步
    # ⇒ 文档里那句话就没人保证了。
    base = label.rsplit("/", 1)[-1]
    if base.endswith(".py"):
        base = base[:-3]
    for path, required in _PROTECTED_REQUIRED_OPTIONS.items():
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".py"):
            name = name[:-3]
        if name == base:
            return required
    return None


def _protected_canonical_path(label: str) -> str:
    """受保护脚本的**规范路径**(两种形态归到同一个)。

    (模块形态的基名 `check_guard_mutations` 与文件形态
    `tools/check_guard_mutations.py` 是**同一个脚本**
    ⇒ 报出来的规范形必须指向同一个路径。)
    """
    base = label.rsplit("/", 1)[-1]
    if not base.endswith(".py"):
        base = base + ".py"
    for path in _PROTECTED_REQUIRED_OPTIONS:
        if path.rsplit("/", 1)[-1] == base:
            return path
    return "tools/%s" % base


def _protected_identities(expected: str) -> set[str]:
    """受保护脚本**可以**被写成哪些「标识」。

    `tools/check_mutations.py` 与 `check_mutations`(模块形态的基名)
    指同一个脚本 ⇒ 两种写法都要认,否则会出现「模块形态被判非规范形」
    这种自相矛盾的说法(它既是受保护脚本、又永远不合规)。
    """
    identities = {_script_identity(expected)}
    identities.add(_script_identity(expected.rsplit("/", 1)[-1]))
    # **模块形态**:`tools.check_mutations` ⇒ 与 `tools/check_mutations` 是同一个
    identities.add(_script_identity(expected).replace("/", "."))
    return identities


def canonical_protected(tokens: list[str] | None, expected: str) -> bool:
    """受保护脚本那一步的**规范形**:解释器 + 脚本 + **恰好** `--verify`。

    **为什么**(第 55 轮之七):接线自检此前只管**形状**——一条命令、不许带
    `if:` / `continue-on-error:` / `shell:` / `env:`——**不管内容**。
    于是 `python tools/check_mutations.py`(去掉 `--verify`)形状照样合规
    ⇒ 静默放行 ⇒ 而 `--verify` 才是「ADR 的变异表与脚本双向核对」的那一步
    ⇒ 文档里「两边各 N 行,逐条对上」这句话**从此没人保证**。

    **多一个选项也不许**:允许的形态只有一条(`--verify`);
    将来要加选项时**显式**改 `_PROTECTED_REQUIRED_OPTIONS`。
    """
    index, found = _split_interpreter(tokens)
    if found is None:
        return False
    if _script_identity(found) not in _protected_identities(expected):
        return False
    required = _PROTECTED_REQUIRED_OPTIONS.get(expected, ("--verify",))
    return tokens[index + 1:] == list(required)


def canonical_gate(tokens: list[str] | None) -> dict | None:
    """这一条命令是不是**规范形的闸门调用**?是 ⇒ 返回解析结果;不是 ⇒ None。

    规范形(顺序不限,选项**只有**这三个):
        python tools/ci_gate.py --junitxml <报告路径> --roster <名单> --suite <套件> [--suite <套件> …]

    **解释器选项**(第 55 轮之五,codex 第 2 轮的 M1):`python -X utf8 …`、
    `python -W error …`、`py -3 …` 现在**算规范形**(此前一律判红 ⇒ 误伤,
    而且与 ADR「闸门命令允许 -W/-X 等取值选项」的明文直接矛盾);
    `-c` / `-m` 仍然判红(它们把脚本整个换掉 ⇒ 闸门压根没跑)。
    """
    index, script = _split_interpreter(tokens)
    if script is None:
        return None
    if _normalize_script(script) != "tools/ci_gate.py":
        return None
    index += 1
    options: dict[str, list[str]] = {"--junitxml": [], "--roster": [], "--suite": []}
    while index < len(tokens):
        name = tokens[index]
        if name not in _CANONICAL_OPTIONS:
            return None                     # 多一个选项 ⇒ 非规范形
        if index + 1 >= len(tokens):
            return None
        options[name].append(tokens[index + 1])
        index += 2
    if not options["--junitxml"] or not options["--roster"] or not options["--suite"]:
        return None
    return {
        "junitxml": options["--junitxml"][0],
        "roster": options["--roster"][0],
        "suites": list(options["--suite"]),
    }


# --------------------------------------------------------------------------
# workflow 的读取
# --------------------------------------------------------------------------
def _load_yaml(path: Path):
    """读 YAML;读不了 ⇒ 抛(上层 ⇒ 退出码 2,fail-closed)。"""
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(data: dict) -> dict:
    """取 `on:` 段 ⇒ `{事件名: 配置}`(配置可能是 None)。

    **注意**:YAML 1.1 把裸 `on` 解析成**布尔 True**——PyYAML 就是这样,
    所以 `data["on"]` 取不到,得两个键都试。

    **`on:` 不一定是字典**(第 55 轮之五,codex 第 2 轮的 H1):
    `on: pull_request`、`on: [push, pull_request]` 都是**合法**的 GitHub 写法
    ⇒ 此前一律返回 {} ⇒ 被判红「没有 pull_request 触发器」⇒ **误伤合法配置**。
    """
    for key in ("on", True):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            return {value: None}
        if isinstance(value, list):
            return {item: None for item in value if isinstance(item, str)}
        return {}
    return {}


def _steps_of_job(job_name: str, job: dict) -> list[dict]:
    """**一个** job 里带 `run:` 的步骤(附 job 上下文)。"""
    out: list[dict] = []
    for step in job.get("steps") or []:
        if isinstance(step, dict) and "run" in step:
            out.append({"job": job_name, "job_body": job, "step": step,
                        "name": step.get("name", ""), "run": step.get("run", "")})
    return out


def _steps_of(data: dict) -> list[dict]:
    """所有 job 里带 `run:` 的步骤(附 job 上下文)。"""
    out: list[dict] = []
    jobs = data.get("jobs") or {}
    if not isinstance(jobs, dict):
        return out
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        out.extend(_steps_of_job(job_name, job))
    return out


# --------------------------------------------------------------------------
# 检查
# --------------------------------------------------------------------------
def _job_roles(data: dict) -> dict[str, dict]:
    """每个 job 的**角色**:有没有闸门步骤、有没有受保护脚本。

    **为什么**(第 55 轮之七):job 级的那些键(`container:` / `services:` /
    `defaults.run.*` / `if:` / `env:` / `needs:`)此前是**按步骤**报的:
      - 有 5 个闸门步骤的 job ⇒ 同一条 job 级问题**报 5 遍**;
      - job 级 `defaults.run.*` 只在「**这个步骤恰好是闸门步骤**」时才被看见
        ⇒ 换个 job 就没有保护了。
    ⇒ 现在**按 job** 归并:每个 job 的角色先算出来,再一次性报。
    """
    roles: dict[str, dict] = {}
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return roles
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        role = {"gate": None, "protected": None, "self_guard": None}
        for entry in _steps_of_job(job_name, job):
            raw = entry["run"]
            if not isinstance(raw, str):
                continue
            for command in logical_commands(raw):
                tokens = _tokens(command)
                if tokens is None:
                    continue
                if role["gate"] is None:
                    if canonical_gate(tokens) is not None:
                        role["gate"] = entry["name"]
                    elif _gate_step_mentions(command) is not None:
                        # **非规范形**的闸门调用也算「这个 job 在证据链上」
                        # (第 55 轮之十一,子代理复核的 H-4:此前只有**规范形**
                        #  才算 ⇒ 闸门写法被改坏的那个 job,它的 job 级
                        #  `container:` / `env:` / `needs:` **一条都不报**
                        #  ⇒ 真正的攻击面被「非规范形」这条消息**盖住**。)
                        role["gate"] = entry["name"]
                if role["protected"] is None:
                    label = _protected_call_label(tokens)
                    if label is not None:
                        role["protected"] = label
                if role["self_guard"] is None:
                    label = _self_guard_call_label(command)
                    if label is not None:
                        role["self_guard"] = label
        roles[job_name] = role
    return roles


def _job_key_present(job: dict, key: str) -> bool:
    """这个 job 上有没有**这个键**(键名**小写归一**后比对)。

    **为什么**(第 55 轮之十,codex 第 5 轮的 L-6):GitHub 的 job 键是**小写**的
    ⇒ `Container:` 这种大写写法 GitHub 会**忽略**它 ⇒ 但本工具的规则写的是
    「键存在即判红」⇒ 两边口径要对齐 ⇒ 现在按小写归一后比对
    (宁可多报,也不放过「写成大写就不算容器」的写法)。
    """
    return any(str(name).lower() == key for name in job)


def _job_level_problems(workflow_name: str, data: dict,
                        roles: dict[str, dict]) -> list[str]:
    """**job 级**的旁路键与执行环境:每个 job **只报一遍**。

    **管的是哪些键**:
      - `container:` / `services:`(第 55 轮之七,codex 第 4 轮的 H1):
        这一步整个跑在**别人给的镜像**里 ⇒ 装什么、跑什么版本全不由我们说了算
        ⇒ 闸门/守卫压根没跑,而 CI 看上去是绿的;
      - `defaults.run.shell` / `defaults.run.working-directory`:
        job 级也能换掉执行语义与工作目录(此前只在 workflow 级查);
      - `if:` / `continue-on-error:` / `env:` / `needs:`:
        整个 job 可以不出力 / 可以被跳过 / 解释器可以被换掉。
    """
    problems: list[str] = []
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return problems
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        role = roles.get(job_name) or {}
        gate_step = role.get("gate")
        protected = role.get("protected")
        self_guard = role.get("self_guard")
        if gate_step is None and protected is None and self_guard is None:
            # 这个 job 既不跑闸门也不跑受保护脚本、不跑裁决者冒烟
            # ⇒ 不在证据链上
            # ⇒ 它的 job 级键不影响「闸门到底跑没跑」。
            continue

        if _job_key_present(job, "container"):
            problems.append(
                "job「%s」带了 `container:` ⇒ 这一步整个跑在**别人给的镜像**里 "
                "⇒ 装什么、跑哪个版本全不由我们说了算 ⇒ 闸门/守卫压根没跑而 "
                "CI 看上去是绿的" % job_name)
        if _job_key_present(job, "services"):
            problems.append(
                "job「%s」带了 `services:` ⇒ 这一步整个跑在**别人给的容器**里 "
                "⇒ 闸门/守卫压根没跑而 CI 看上去是绿的" % job_name)

        job_defaults = job.get("defaults")
        if isinstance(job_defaults, dict):
            run_defaults = job_defaults.get("run")
            if isinstance(run_defaults, dict):
                if "shell" in run_defaults:
                    problems.append(
                        "job「%s」在 **job 级** defaults.run.shell 上改了 shell(%r)"
                        "⇒ 这一步的执行语义不再由我们自己说了算"
                        % (job_name, run_defaults["shell"]))
                if "working-directory" in run_defaults:
                    problems.append(
                        "job「%s」在 **job 级** defaults.run.working-directory 上"
                        "改了工作目录(%r)⇒ 闸门/守卫那一步不在仓库根下跑"
                        % (job_name, run_defaults["working-directory"]))

        if gate_step is not None:
            # 闸门所在的 job:旁路键 ⇒ 整个 job 可以不出力。
            if "env" in job:
                problems.append(
                    "闸门步骤「%s」所在的 job 带了 `env:` ⇒ 解释器/模块搜索路径"
                    "可以被换掉(实测:提交一个 `exit 0` 的 python 放在 PATH 前面 ⇒ "
                    "闸门没跑而工具照样报「[OK]」)" % gate_step)
            if "needs" in job:
                problems.append(
                    "闸门步骤「%s」所在的 job 带了 `needs:` ⇒ 上游 job 一旦被跳过"
                    "(如 `if: false`),这个 job 也被跳过 ⇒ 闸门压根没跑,"
                    "而 CI 记成 skipped(分支保护视为通过)" % gate_step)
            for key in ("if", "continue-on-error"):
                if key in job:
                    problems.append(
                        "闸门步骤「%s」所在的 job 带了 `%s:` ⇒ 整个 job 可以不出力"
                        % (gate_step, key))
        elif protected is not None:
            for key in ("if", "continue-on-error", "env", "needs"):
                if key in job:
                    problems.append(
                        "受保护脚本(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"
                        % (protected, key))
        elif self_guard is not None:
            # 裁决者冒烟所在的 job(**没有**闸门步骤、**没有**受保护脚本):
            # job 级旁路键 ⇒ 整个 job 可以不出力 ⇒ 分档器的关卡压根没跑。
            # (有闸门步骤的 job 归闸门那条规则管 ⇒ 同一条问题**不报两遍**。)
            for key in ("if", "continue-on-error", "env", "needs"):
                if key in job:
                    problems.append(
                        "分档器冒烟(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"
                        % (self_guard, key))
    return problems


def _check_workflow_shape(workflow: Path, roster_path: Path,
                          roster_info: "_RosterInfo" | None = None
                          ) -> tuple[list[str], set[str], set[str], bool]:
    """检查**一个** workflow 的形状:规范形 + 触发器。

    返回 `(问题, 这一份覆盖到的套件, 名单点名的套件, 这一份里有没有闸门步骤)`。
    **覆盖判定不在这里做**——见 `check_workflows`(第 55 轮之五:覆盖按**单文件**
    算,不再取并集 ⇒ 「把闸门挪到另一个 workflow」不再能糊弄过去)。
    """
    problems: list[str] = []
    try:
        data = _load_yaml(workflow)
    except Exception as exc:                      # noqa: BLE001 - 读不了 ⇒ 判红
        return (["%s 读不了/不是合法 YAML:%s(fail-closed)" % (workflow.name, exc)],
                set(), set(), False)
    if not isinstance(data, dict):
        return (["%s 的顶层不是对象(fail-closed)" % workflow.name], set(), set(), False)

    triggers = _triggers(data)
    if "pull_request" not in triggers:
        problems.append("%s 没有 pull_request 触发器 ⇒ PR 上的改动根本不经这道关卡"
                        % workflow.name)
    else:
        # **触发器不许被过滤**:带 `paths:` / `paths-ignore:` 的 `pull_request`
        # ⇒ 不匹配的改动**根本不会触发这个 workflow** ⇒ 闸门压根没跑,
        # 而 CI 看上去是「没有失败」(第 55 轮补上的洞)。
        #
        # **`types:` 判「效果」而不是判「键」**(第 55 轮之六,codex 第 3 轮的 M2):
        # `types: [opened, closed]` ⇒ PR 后续的 push(`synchronize`)**不触发**
        # ⇒ 闸门不跑、CI 看着绿;
        # 而 `types: [opened, reopened, synchronize]`(GitHub 的默认三件套)是
        # **完全合法且不削弱**的 ⇒ 此前「键存在就判红」是**误伤**。
        # ⇒ 现在:带了 `types:` 就必须**包含** `opened` 与 `synchronize`。
        pull_request = triggers.get("pull_request")
        if isinstance(pull_request, dict):
            for key in ("paths", "paths-ignore", "branches", "branches-ignore"):
                if key in pull_request:
                    problems.append(
                        "%s 的 pull_request 触发器带了 `%s:` ⇒ 不匹配的改动**不会触发**"
                        "这个 workflow ⇒ 闸门压根没跑,而 CI 看上去是绿的"
                        % (workflow.name, key))
            if "types" in pull_request:
                types = pull_request["types"]
                if isinstance(types, str):
                    type_set = {types}
                elif isinstance(types, list):
                    type_set = {t for t in types if isinstance(t, str)}
                else:
                    type_set = set()
                missing = sorted({"opened", "synchronize"} - type_set)
                if missing:
                    problems.append(
                        "%s 的 pull_request 触发器带了 `types:` 但**少了** %s ⇒ "
                        "PR 后续的 push(`synchronize`)/PR 的开启(`opened`)"
                        "**不触发**这个 workflow ⇒ 闸门压根没跑,而 CI 看上去是绿的"
                        % (workflow.name, ", ".join(missing)))

    # **`push:` 触发器上的文件过滤**(第 55 轮之七,codex 第 4 轮的 L-2):
    # `push:` 带了 `paths:` / `paths-ignore:` ⇒ 某些提交**根本不会触发**这个
    # workflow ⇒ 闸门压根没跑,而 CI 看上去是绿的。
    # (`branches:` 在 `push:` 上**是合法的**——本仓库两个 workflow 都用
    #  `push: branches: [main]` ⇒ **不判**它;只有**文件**过滤会让改动跑不到。)
    push = triggers.get("push")
    if isinstance(push, dict):
        for key in ("paths", "paths-ignore"):
            if key in push:
                problems.append(
                    "%s 的 push 触发器带了 `%s:` ⇒ 不匹配的提交**不会触发**"
                    "这个 workflow ⇒ 闸门压根没跑,而 CI 看上去是绿的"
                    % (workflow.name, key))

    # **`$GITHUB_ENV` / `$GITHUB_PATH`**(第 55 轮之七,claude 第 4 轮的 H-1):
    # 这两个是 GitHub 提供的**官方**通道 ⇒ 往里写一行 PATH 就等于
    # **给后续所有步骤**换掉解释器 ⇒ 与本轮禁掉的 `env:` **完全等价**。
    # ⇒ 被守的 workflow 里**任何**步骤提到它们 ⇒ 判红。
    for entry in _steps_of(data):
        raw_run = entry["run"]
        if not isinstance(raw_run, str):
            continue
        for marker in ("GITHUB_ENV", "GITHUB_PATH"):
            if marker in raw_run:
                problems.append(
                    "%s 的步骤「%s」(%s)提到了 `$%s` ⇒ 它能**给后续所有步骤**"
                    "换掉解释器/模块搜索路径(与 `env:` 等价)⇒ 闸门压根没跑而 "
                    "CI 看上去是绿的" % (workflow.name, entry["name"],
                                        entry["job"], marker))
                break

    # **workflow 级 `env:`(第 55 轮之六,claude 第 3 轮的 C-1)**:
    # 它被**所有** job、所有 step 继承 ⇒ 解释器/模块搜索路径可以被换掉。
    # (此前只禁了 step 级与 job 级 ⇒ workflow 级是个洞。)
    if "env" in data:
        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "
                        "解释器/模块搜索路径可以被换掉(实测:提交一个 `exit 0` 的 python "
                        "放在 PATH 前面 ⇒ 闸门没跑而工具照样报「[OK]」)" % workflow.name)

    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        run_defaults = defaults.get("run")
        if isinstance(run_defaults, dict) and "shell" in run_defaults:
            problems.append("%s 在 defaults.run.shell 上改了 shell(%r)⇒ 闸门那一步"
                            "的执行语义不再由我们自己说了算" % (workflow.name, run_defaults["shell"]))
        if isinstance(run_defaults, dict) and "working-directory" in run_defaults:
            # **H-2(claude 第 3 轮)**:换掉工作目录 ⇒ 脚本路径/相对路径全变。
            problems.append("%s 在 **workflow 级** defaults.run.working-directory 上"
                            "改了工作目录(%r)⇒ 闸门那一步跑的不是仓库里的东西"
                            % (workflow.name, run_defaults["working-directory"]))

    # 名单:读一次、结构问题**只报一次**(此前每个 workflow 各解析一次
    # ⇒ 名单坏掉时同一条问题报两遍——codex 第 2 轮的 L2)。
    if roster_info is None:
        roster_info = _read_roster(roster_path)
    roster_suites: set[str] = set(roster_info.suites)
    roster_rel = roster_info.rel

    covered: set[str] = set()
    has_gate = False

    # **job 级**的那些键:按 job 归并,**一个 job 只报一遍**(第 55 轮之七)。
    # (此前是**按步骤**报的 ⇒ 有 5 个闸门步骤的 job 会把同一条问题报 5 遍;
    #  而 job 级 `defaults.run.*` 只在「这一步恰好是闸门步骤」时才看得见。)
    roles = _job_roles(data)
    problems.extend(_job_level_problems(workflow.name, data, roles))

    for entry in _steps_of(data):
        step = entry["step"]
        raw_run = entry["run"]
        if "working-directory" in step:
            # **H-2(claude 第 3 轮)**:步骤级 `working-directory:` 把这一步的工作目录
            # 换掉 ⇒ 相对路径(`tools/…`、`tests/…`)指向别处 ⇒ 跑的东西不是我们的。
            problems.append("步骤「%s」(%s)带了 `working-directory:` ⇒ 这一步不在仓库根下跑"
                            "(相对路径会指向别处)" % (entry["name"], entry["job"]))
        for key in ("container", "services"):
            if any(str(name).lower() == key for name in step):
                # **步骤级**的容器(第 55 轮之十一,子代理复核的 H-3):
                # GitHub 的步骤**也**支持 `container:` ⇒ 这一步整个跑在
                # **别人给的镜像**里 ⇒ 装什么、跑哪个版本全不由我们说了算
                # ⇒ 闸门/守卫压根没跑,而 CI 看上去是绿的
                # (此前只查 **job 级** ⇒ 同一个键写在步骤上就绕过去了。)
                problems.append("步骤「%s」(%s)带了 `%s:` ⇒ 这一步整个跑在"
                                "**别人给的镜像/容器**里 ⇒ 闸门/守卫压根没跑而 "
                                "CI 看上去是绿的" % (entry["name"], entry["job"], key))
        if not isinstance(raw_run, str):
            # **`run:` 不是字符串**(写成列表/映射)⇒ 判不了 ⇒ 判红
            # (第 55 轮之五,codex 第 2 轮的 L4:此前直接抛栈,**没有结论行**)。
            problems.append("步骤「%s」(%s)的 `run:` 不是字符串(是 %s)⇒ "
                            "这一步在跑什么判不了(fail-closed)"
                            % (entry["name"], entry["job"], type(raw_run).__name__))
            continue
        commands = logical_commands(raw_run)
        gate = None
        stray: list[str] = []
        protected: str | None = None
        protected_tokens: list[str] | None = None
        self_guard: str | None = None

        for command in commands:
            tokens = _tokens(command)
            kind = command_kind(tokens, command)
            candidate = canonical_gate(tokens)
            if protected is None:
                protected = _protected_call_label(tokens)
                protected_tokens = tokens
            if self_guard is None:
                self_guard = _self_guard_call_label(command)
            if candidate is not None:
                if gate is not None:
                    stray.append("这一步里**有两条**规范闸门命令")
                gate = candidate
                continue
            if kind in ("pytest", "gate"):
                stray.append("非规范形态的 %s 调用:%s" % (kind, command[:120]))
            elif kind == "unknown":
                # **「列举/说明」类命令不算**(第 55 轮之七,codex 第 4 轮的 M-3):
                # `grep -rn pytest tests/`、`ruff check tools/ci_gate.py`、
                # `echo "闸门是 ci_gate.py"` 这些**合法写法**此前一律判红
                # ⇒ 极容易踩的雷(仓库教训:**禁引用,不是禁出现**)。
                if _is_inert_command(tokens, command):
                    continue
                if any(word in command for word in _DANGEROUS_WORDS):
                    stray.append("认不出来的命令里提到了 pytest/闸门(判不了 ⇒ 判红):%s"
                                 % command[:120])

        # --- **受保护脚本**(证据链):与闸门步骤同一套纪律 ---
        # (第 55 轮之六,codex 第 3 轮的 H2:此前只有闸门步骤与两处接线调用
        #  受约束 ⇒ `check_mutations.py --verify` / `check_guard_mutations.py
        #  --verify` 这两步可以被 `continue-on-error:` 一行废掉 ⇒
        #  「142 种变异全部被抓住」变成空话,而 CI 照样绿。)
        if protected is not None:
            if len(commands) != 1:
                problems.append("受保护脚本(%s)那一步必须**只有一条命令**,现在有 %d 条"
                                "(多出来的命令就是插进来的缝)"
                                % (protected, len(commands)))
            if has_background_operator(raw_run):
                problems.append("受保护脚本(%s)那一步的命令尾部有 `&` ⇒ 它被丢到后台,"
                                "**退出码被丢弃**(实测:`bash -c 'false &'` 的退出码是 0)"
                                % protected)
            for key in ("if", "continue-on-error", "shell", "env"):
                if key in step:
                    problems.append("受保护脚本(%s)那一步带了 `%s:` ⇒ 它可以不算数"
                                    % (protected, key))
            # **必须带 `--verify`**(第 55 轮之七):
            # 少了它 ⇒ 变异照样被抓,但「ADR 的变异表与脚本**双向核对**」
            # 这一步不再被 CI 执行 ⇒ 文档里「两边各 N 行,逐条对上」没人保证。
            path_problem = _protected_call_path_problem(protected_tokens, protected)
            if path_problem is not None:
                problems.append(path_problem)
            if _protected_requirement(protected) is not None:
                expected = _protected_canonical_path(protected)
                if not canonical_protected(protected_tokens, expected):
                    problems.append(
                        "受保护脚本(%s)那一步的命令**不是规范形**(必须恰好是 "
                        "`python %s --verify`)⇒ 「ADR 的变异表与脚本双向核对」"
                        "这一步不再被 CI 执行" % (protected, expected))

        # --- **裁决者自己的冒烟关卡**(第 55 轮之七,claude 第 4 轮的 L-5) ---
        # `tools/check_domain.py` 决定「谁拥有判定点」⇒ 它自己的关卡被改坏
        # ⇒ **没人发现**(项目 CLAUDE.md:分档器在**不可降级**清单里)。
        # ⇒ 施加**旁路键**纪律;但**不**施加「只有一条命令」——
        # 仓库里那一步本来就是一个多命令块(`set -e` + 显式 rc 判定 + 结论行自证)
        # ⇒ 套上去会误伤仓库自己。
        if self_guard is not None:
            for key in ("if", "continue-on-error", "shell", "env"):
                if key in step:
                    problems.append(
                        "分档器冒烟(%s)那一步带了 `%s:` ⇒ 它可以不算数"
                        % (self_guard, key))
            if has_background_operator(raw_run):
                problems.append(
                    "分档器冒烟(%s)那一步的命令尾部有 `&` ⇒ 它被丢到后台,"
                    "**退出码被丢弃**" % self_guard)
            # --- 第 55 轮之八:旁路**键**纪律拦不住的两条通道(实测) ---
            # ① `set +e`:errexit 被关掉 ⇒ 分档器崩了/失败了这一步也不红。
            if _errexit_disabled(raw_run):
                problems.append(
                    "分档器冒烟(%s)那一步里出现了 `set +e`"
                    "(或 `set +o errexit`、`set +eo pipefail` 之类)"
                    "⇒ **errexit 被关掉** ⇒ 分档器崩了、返回 2(fail-closed)了,"
                    "这一步**照样绿** ⇒ 裁决者的关卡等于没有" % self_guard)
            # ② `|| true` / `|| :`:失败被**吃掉**。
            #    (`|| rc=$?` 是**捕获**退出码——后面还要拿它判 2 ⇒ 不是吃掉
            #     ⇒ 不匹配、不误伤。)
            for raw_line in raw_run.splitlines():
                stripped = raw_line.strip()
                if "check_domain" not in stripped or stripped.startswith("#"):
                    continue
                if _OR_TRUE_RE.search(stripped):
                    problems.append(
                        "分档器冒烟(%s)那一步里,分档器调用后面跟着 `|| true`"
                        "⇒ **失败被吃掉** ⇒ 分档器说「环境不可用」时这一步照样绿"
                        % self_guard)
                    break
            # --- 第 55 轮之九:「失败被吃掉」的**其它形态**(实测) ---
            # 第 55 轮之八只堵了「**行尾**的 `|| true`」⇒ 实测还有四种写法
            # 照样能吃掉失败(`|| true;` / `|| true && …` / `; true` /
            # `if ! cmd; then … fi`)⇒ 一族能力,四条通道。
            problems.extend(
                _self_guard_swallow_problems(self_guard, raw_run))
            problems.extend(
                _self_guard_failure_semantics(self_guard, raw_run))

        if gate is None:
            if stray:
                problems.append("步骤「%s」(%s):%s" % (entry["name"], entry["job"], "；".join(stray)))
            continue

        # --- 这一步是闸门步骤:规范形 + 不许带旁路键 ---
        has_gate = True
        if len(commands) != 1:
            problems.append("闸门步骤「%s」必须**只有一条命令**,现在有 %d 条"
                            "(多出来的命令就是插进来的缝)" % (entry["name"], len(commands)))
        if has_background_operator(raw_run):
            # **H1(codex 第 3 轮)**:尾部 `&` ⇒ 闸门被丢到后台、退出码被丢弃
            # ⇒ 闸门判红压根进不了 CI 结论。
            problems.append("闸门步骤「%s」的命令尾部有 `&` ⇒ 它被丢到后台、"
                            "**退出码被丢弃**(实测:`bash -c 'false &'` 的退出码是 0)"
                            % entry["name"])
        if (roster_rel is not None
                and _normalize_script(gate["roster"]) != _normalize_script(roster_rel)):
            problems.append("闸门步骤「%s」核对的是**另一份**名单:%s(应当是 %s)"
                            % (entry["name"], gate["roster"], roster_rel))
        if not gate["junitxml"].startswith("$RUNNER_TEMP/"):
            problems.append("闸门步骤「%s」把报告写到了 %s(应当落在 $RUNNER_TEMP 下,"
                            "别写进仓库)" % (entry["name"], gate["junitxml"]))
        for suite in gate["suites"]:
            if not _SUITE_RE.match(suite):
                problems.append("闸门步骤「%s」点名了不是测试套件的路径:%s" % (entry["name"], suite))
            elif not (ROOT / suite).exists():
                problems.append("闸门步骤「%s」点名的套件文件不存在:%s" % (entry["name"], suite))
            else:
                # 只有**合法且确实存在**的套件才计入「已覆盖」
                # (第 55 轮之六,codex 第 3 轮的 L1:此前 `covered.add` 在
                #  `if/elif` **之外** ⇒ 不存在的套件也被算成「已覆盖」)。
                covered.add(suite)

        for key in ("if", "continue-on-error", "shell"):
            if key in step:
                problems.append("闸门步骤「%s」带了 `%s:` ⇒ 这一步可以不出力/不算失败"
                                % (entry["name"], key))
        if "env" in step:
            # **C2(codex 第 2 轮)**:`env: PATH: …/evil:$PATH` ⇒ 提交一个
            # `evil/python`(内容 `exit 0`)就能把**解释器**换掉 ⇒
            # 闸门「通过」而**什么都没跑** ⇒ 自检照样报绿。
            problems.append("闸门步骤「%s」带了 `env:` ⇒ 解释器/模块搜索路径可以被换掉"
                            "(实测:提交一个 `exit 0` 的 python 放在 PATH 前面 ⇒ "
                            "闸门没跑而工具照样报「[OK]」)" % entry["name"])
        # **job 级**的那几键(`if` / `continue-on-error` / `env` / `needs` /
        #  `defaults.run.*` / `container:` / `services:`)由 `_job_level_problems`
        # **按 job 统一报**(第 55 轮之七)⇒ 这里不再重复报一遍。

    return problems, covered, roster_suites, has_gate


def _roster_relative(roster_path: Path) -> str | None:
    """名单相对仓库根的路径;**不在仓库里** ⇒ None。"""
    try:
        return roster_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return None


def _load_sibling(module_name: str, filename: str):
    """按**文件路径**加载同目录下的另一个工具(不靠 `sys.path`)。"""
    path = ROOT / "tools" / filename
    if not path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:                              # noqa: BLE001 - 拿不到 ⇒ 交给上层判红
        return None
    return module


def _roster_structure_problems(data, roster_path: Path) -> list[str]:
    """名单**结构**校验——调**共享的那一份**实现。

    **为什么共享**(第 55 轮之五,codex 第 2 轮的 H2):
    此前接线自检只校验「`must_run` 是对象」,而闸门工具校验七种坏形态里的
    六种 ⇒ **两边口径不一致** ⇒ 接线自检报「[OK] CI 接线合规」而闸门报红,
    前者是**误导性的绿**。⇒ 现在两边调**同一份** `validate_roster`
    (它在 `tools/check_ci_ran.py` 里)。
    """
    module = _load_sibling("check_ci_ran", "check_ci_ran.py")
    validator = getattr(module, "validate_roster", None) if module is not None else None
    if validator is None:
        return ["共享的名单结构校验拿不到(tools/check_ci_ran.py 里没有 validate_roster)"
                "⇒ 名单结构判不了(fail-closed)"]
    try:
        found = validator(data)
    except Exception as exc:                      # noqa: BLE001 - 崩了 ⇒ 判红
        return ["共享的名单结构校验自己崩了:%r(fail-closed)" % (exc,)]
    if not isinstance(found, list):
        return ["共享的名单结构校验返回的不是清单:%r(fail-closed)" % (found,)]
    return ["名单 %s %s" % (roster_path, item) for item in found]


class _RosterInfo:
    """读好的名单:结构问题 + 点名的套件 + 相对仓库根的路径。"""

    __slots__ = ("problems", "suites", "rel")

    def __init__(self, problems: list[str], suites: set[str], rel: str | None):
        self.problems = problems
        self.suites = suites
        self.rel = rel


def _read_roster(roster_path: Path) -> _RosterInfo:
    """读名单 ⇒ 结构问题 + 点名的套件 + 相对路径。"""
    problems: list[str] = []
    try:
        raw = roster_path.read_text(encoding="utf-8")
    except OSError as exc:
        return _RosterInfo(["名单读不了:%s(%s)" % (roster_path, exc)], set(), None)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return _RosterInfo(["名单不是合法 JSON:%s(%s)" % (roster_path, exc)], set(), None)
    problems.extend(_roster_structure_problems(data, roster_path))
    suites: set[str] = set()
    if isinstance(data, dict) and isinstance(data.get("must_run"), dict):
        suites = set(data["must_run"])
    # 名单**相对仓库根**的路径(闸门步骤里写的就是这个)。
    # 名单在仓库之外 ⇒ 比不出来 ⇒ 判红(而不是崩在 `relative_to` 上:
    # 未捕获的异常退出码也是 1,而仓库的规矩是「崩掉也要给结论行」)。
    rel = _roster_relative(roster_path)
    if rel is None:
        problems.append("名单 %s 不在仓库里 ⇒ 闸门核对的名单与本工具核对的名单"
                        "可能不是同一份(fail-closed)" % roster_path)
    return _RosterInfo(problems, suites, rel)


def _coverage_problems(roster_suites: set[str], covered: set[str]) -> list[str]:
    """名单覆盖:点名的每一份套件都得真的被某个**规范**闸门步骤跑。"""
    missing = sorted(roster_suites - covered)
    return ["名单点名的 %s **没有任何规范闸门步骤在跑它** ⇒ "
            "把那一步删掉也不会有人发现(codex 的 C1)" % suite for suite in missing]


def check_gate_wiring(workflow: Path, roster_path: Path) -> list[str]:
    """检查**一个** workflow:规范形 + 名单覆盖 + 触发器。

    覆盖只按**这一个** workflow 算(单文件语义;用例与探针用这个)。
    """
    roster_info = _read_roster(roster_path)
    problems, covered, roster_suites, _ = _check_workflow_shape(
        workflow, roster_path, roster_info)
    problems = list(roster_info.problems) + problems
    problems.extend(_coverage_problems(roster_suites, covered))
    return problems


def check_workflows(workflows: list[Path], roster_path: Path,
                    primary: Path = RESEARCH_WORKFLOW) -> list[str]:
    """检查**一组** workflow:逐个查形状,覆盖按**单文件**算。

    **为什么不再取并集**(第 55 轮之五,codex 第 2 轮的 L3):
    并集口径下,把所有闸门步骤挪到 `constitution-guard.yml` ⇒ 并集仍覆盖
    ⇒ 工具报绿;但那个 workflow **没有研究侧依赖** ⇒ 闸门压根跑不起来
    ⇒ **CI 看着绿、什么也没跑**。而仓库里的用例按**单文件**口径查
    ⇒ 两处结论相反(「仓库里对覆盖有两个定义」)。

    ⇒ 现在:
      1. **覆盖按 `primary`(research-guard.yml)这一个文件算**;
      2. **别的 workflow 里不许有闸门步骤**(它们没有研究侧依赖,闸门跑不起来)。
    """
    problems: list[str] = []
    roster_info = _read_roster(roster_path)
    problems.extend(roster_info.problems)
    covered: set[str] = set()
    roster_suites: set[str] = set(roster_info.suites)
    seen_primary = False
    for workflow in workflows:
        file_problems, file_covered, _file_roster, has_gate = _check_workflow_shape(
            workflow, roster_path, roster_info)
        problems.extend(file_problems)
        if workflow.resolve() == primary.resolve():
            seen_primary = True
            covered |= file_covered
        elif has_gate:
            problems.append(
                "%s 里**不许**有闸门步骤 ⇒ 闸门只在 %s 里跑"
                "(别的 workflow 没有研究侧依赖 ⇒ 闸门跑不起来 ⇒ "
                "闸门没跑而 CI 看上去是绿的)" % (workflow.name, primary.name))
    if not seen_primary:
        problems.append("闸门所在的 workflow %s 没被检查 ⇒ 覆盖判定无从谈起(fail-closed)"
                        % primary.name)
    problems.extend(_coverage_problems(roster_suites, covered))
    return problems


def _self_guard_failure_semantics(self_guard: str, raw_run: str) -> list[str]:
    """分档器调用**失败**时,这一步会不会**照样绿**?

    **为什么**(第 55 轮之九,实测):第 55 轮之八只堵了「**行尾**的 `|| true`」
    ⇒ 实测把仓库整个复制到临时目录、逐个施加绕过写法、跑**副本里的真守卫**,
    发现下面四种写法**照样**能让分档器的失败被吃掉 ⇒ 一族能力,四条通道:

    | 写法 | 为什么失败被吃掉 |
    |---|---|
    | `cmd || true;` | 分号让「或真」不再是行尾 ⇒ 但语义一样(失败仍被吃掉) |
    | `cmd || true && echo …` | 同上 |
    | `cmd; true` | 最后一条命令的退出码是 `true` 的 ⇒ 失败被盖掉 |
    | `if ! cmd; then … fi` | 调用被**包在条件里** ⇒ 它的失败不会让这一步变红 |

    ⇒ 处置:**按能力**(「失败会不会被吃掉」)列全通道。

    **不误伤**:仓库里那一步本来就有的 `|| rc=$?` 是**捕获**退出码
    (后面还要拿它判 2)⇒ 不是吃掉 ⇒ 不匹配。
    """
    problems: list[str] = []
    depth = 0          # 条件块的嵌套深度(`if`/`while`/`until` … `fi`/`done`)
    lines = raw_run.splitlines()
    for index, raw_line in enumerate(lines):
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            continue                       # 纯注释行不是命令
        if (_CONDITIONAL_OPEN_RE.match(stripped)
                and not _CONDITIONAL_CLOSE_ANY_RE.search(stripped)):
            # **单行的 `if …; then …; fi`**(第 55 轮之十一,子代理复核的 M-2):
            # 开与关在**同一行**⇒ 嵌套深度**不**增加
            # ⇒ 此前这一行让深度 +1 却**永不归零**
            # ⇒ 它**后面**的每一步都被当成「被包在条件里」⇒ **误伤**。
            depth += 1
        if "check_domain" in stripped:
            if _OR_TRUE_RE.search(stripped):
                continue                   # **行尾**形态由 R17 报(不重复报)
            if _SWALLOW_RE.search(stripped):
                problems.append(
                    "分档器冒烟(%s)那一步里,分档器调用后面跟着"
                    "**把失败吃掉**的后缀 ⇒ 分档器说「环境不可用」时这一步照样绿"
                    % self_guard)
            elif _continuation_swallows(lines, index):
                problems.append(
                    "分档器冒烟(%s)那一步里,分档器调用**换行**接了一个"
                    "**把失败吃掉**的后缀 ⇒ 分档器说「环境不可用」时这一步照样绿"
                    % self_guard)
            elif depth > 0 or _CONDITIONAL_RE.match(stripped):
                problems.append(
                    "分档器冒烟(%s)那一步里,分档器调用被**包在条件里**"
                    "(`if` / `while` / `until`)⇒ 它的失败**不会**让这一步变红"
                    % self_guard)
        if _CONDITIONAL_CLOSE_RE.match(stripped):
            depth = max(0, depth - 1)
    return problems


def _continuation_swallows(lines: list[str], index: int) -> bool:
    """这一行以 `||` / `&&` **续行**,而**下一行**把失败吃掉了吗?

    **为什么**(第 55 轮之九,实测):`cmd ||`(换行)`true` ⇒ 失败照样被吃掉
    ⇒ 但 `cmd ||`(换行)`rc=$?` 是**捕获**退出码 ⇒ 合法、必须放行
    ⇒ 所以**不能**只看行尾的 `||`,得看**下一行**。
    """
    stripped = lines[index].strip()
    if not (stripped.endswith("||") or stripped.endswith("&&")):
        return False
    for following in lines[index + 1:]:
        nxt = following.strip()
        if nxt == "" or nxt.startswith("#"):
            continue
        return bool(re.match(r"true(?![A-Za-z0-9_])", nxt)
                    or re.match(r":\s*$", nxt)
                    or re.match(r"exit\s+0(?![0-9])", nxt))
    return False


def _workflow_has_gate_step(workflow: Path) -> bool:
    """这个 workflow 里有没有**闸门步骤**(跑 `tools/ci_gate.py` 的那一步)。

    **为什么**(第 55 轮之九):裁决者冒烟调用必须与**闸门**同在
    ——闸门在哪个 workflow 跑,裁决者的关卡就得在**同一个** workflow 里。
    """
    try:
        data = _load_yaml(workflow)
    except Exception:                        # noqa: BLE001 - 读不了 ⇒ 上层已判红
        return False
    if not isinstance(data, dict):
        return False
    for entry in _steps_of(data):
        raw = entry["run"]
        if not isinstance(raw, str):
            continue
        for command in logical_commands(raw):
            if _gate_step_mentions(command) is not None:
                return True
    return False


def _self_guard_calls(workflow: Path) -> list[str]:
    """这个 workflow 里**跑裁决者冒烟**的地方(返回命令文本)。

    **为什么**(第 55 轮之八,实测):第 55 轮之七给裁决者的冒烟关卡施加了
    「旁路键纪律」⇒ 隐含假设是「那一步**在**」⇒ **实测**发现:把**整个步骤**
    删掉 ⇒ 工具**一声不吭** ⇒ 裁决者的关卡压根不在 CI 里,却没人发现。
    ⇒ 「那一步在不在」本身也要成为一条判据。
    """
    try:
        data = _load_yaml(workflow)
    except Exception:                            # noqa: BLE001 - 读不了 ⇒ 判红(上层)
        return []
    if not isinstance(data, dict):
        return []
    calls: list[str] = []
    for entry in _steps_of(data):
        raw = entry["run"]
        if not isinstance(raw, str):
            continue
        # **真的会被执行** ＋ **真的在裁决**(第 55 轮之十一,子代理复核的
        # C-4 / C-5):`--help`(argparse 退出 0、**不做任何分档**)、没有参数、
        # `exit 0` **之后**的死代码、**从未被调用**的函数体里的调用
        # ⇒ 都**不算**「裁决者的关卡在 CI 里」。
        for command in _live_self_guard_calls(raw):
            if not _self_guard_call_is_adjudication(command):
                continue
            calls.append(command)
    return calls


def _weak_self_guard_calls(workflow: Path) -> list[str]:
    """这个 workflow 里**提到**裁决者冒烟、但**不算数**的调用。

    **为什么要和「没有调用」分开**(第 55 轮之十一,子代理复核的 C-4 / C-5):
    两者都意味着「分档器的关卡等于没有」⇒ 但**修法不同**:
      - 没有调用 ⇒ 整步被删了 ⇒ 要把调用加回来;
      - 调用不算数 ⇒ 调用还在,但它**不做任何分档**(`--help` / 没参数),
        或者它在 `exit 0` **之后**(死代码)、或者它在**从未被调用**的
        函数体里 ⇒ 要把那处调用改成**真的会裁决**的写法。
    ⇒ 报成同一条「没有」会误导修法 ⇒ 分开报。
    """
    try:
        data = _load_yaml(workflow)
    except Exception:                            # noqa: BLE001 - 读不了 ⇒ 判红(上层)
        return []
    if not isinstance(data, dict):
        return []
    strong = set(_self_guard_calls(workflow))
    weak: list[str] = []
    for entry in _steps_of(data):
        raw = entry["run"]
        if not isinstance(raw, str):
            continue
        for command in _self_guard_commands_in_run(raw):
            if command in strong:
                continue
            weak.append(command)
    return weak


def check_self_guard_present(workflows: list[Path]) -> list[str]:
    """**被守的 workflow 里必须至少有一处裁决者冒烟调用。**

    **为什么**(第 55 轮之八,实测):第 55 轮之七只管「那一步带了旁路键」,
    管不到「那一步**根本不在**」⇒ 实测把整个「后果域分档器冒烟」步骤删掉
    ⇒ 工具**零问题** ⇒ 分档器的关卡静默消失。

    **为什么这要紧**:`tools/check_domain.py` 决定「哪些文件算研究域、哪些算
    执行域」——它就是「谁拥有判定点」的**裁决者**(项目 CLAUDE.md 把它列进
    **不可降级**清单)⇒ 它的关卡消失 ⇒ 后果域分档这条线在 CI 里**没人管**。
    """
    problems: list[str] = []
    for workflow in workflows:
        # **范围**(第 55 轮之九,实测):此前是「**任何一个** workflow 里有
        # 调用就算过」⇒ 可以在**别的** workflow 里放一个**假调用**把真调用
        # 顶掉 ⇒ 收紧为「**有闸门步骤的** workflow 必须**自己**带一处调用」。
        if not _workflow_has_gate_step(workflow):
            continue
        if _self_guard_calls(workflow):
            continue
        if _weak_self_guard_calls(workflow):
            problems.append(
                "被守的 workflow 里那处裁决者冒烟调用**不算数**"
                "(`tools/check_domain.py` 只跑 `--help`、没有参数、"
                "或者写在 `exit 0` 之后 / 从没被调用的函数体里)"
                "⇒ 分档器的关卡等于没有 ⇒ 它被改坏、或者被整个删掉,"
                "都不会有人发现")
            continue
        problems.append(
            "被守的 workflow 里**没有**任何裁决者冒烟调用"
            "(`tools/check_domain.py`)⇒ 分档器的关卡压根不在 CI 里 "
            "⇒ 它被改坏、或者被整个删掉,都不会有人发现")
    return problems


def _gate_step_mentions(command: str) -> str | None:
    """这条命令是不是**闸门步骤**?是 ⇒ 返回它的标识;不是 ⇒ None。

    **按标识判**(第 55 轮之七,codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3):
    `python tools/ci_gate.py`、`python ci_gate.py`、`python -m tools.ci_gate`
    是**同一个脚本** ⇒ 只按**字面**比对 ⇒ 模块形态完全隐形。

    **认不出来**(`bash -c "…"`)⇒ 只要**提到了**闸门就算(fail-closed 方向)。
    """
    tokens = _tokens(command)
    script = script_of(tokens) if tokens else None
    if script is None:
        # **只读命令里提到闸门不算闸门步骤**(第 55 轮之十二,codex M-1):
        # `echo "闸门是 ci_gate.py"` / `grep -rn ci_gate .` ⇒ 只是**提到**
        # ⇒ 不是**在跑** ⇒ 此前一律当成闸门步骤 ⇒ 那个 job 被当成「闸门 job」
        # ⇒ 它带的 job 级 `env:` 被**误伤**。
        # (与第 55 轮之七同源:**禁引用,不是禁出现**。)
        if _is_inert_command(tokens, command):
            return None
        if any(word in command for word in ("check_ci_ran", "ci_gate")):
            return "认不出来的命令提到了闸门"
        return None
    base = _script_base(script)
    if base in ("ci_gate", "check_ci_ran"):
        return base
    return None


def _action_run_blocks(data: dict) -> list[str]:
    """**复合动作**(`action.yml`)里那些 `run:` 块。

    **为什么也要扫**(第 55 轮之七):复合动作也是「跑命令的地方」⇒
    里面藏一条闸门命令 ⇒ 闸门在**别处**跑 ⇒ 那个地方没有研究侧依赖
    ⇒ 闸门跑不起来 ⇒ 「闸门跑了但没跑成」和「闸门压根没跑」一模一样。
    """
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return []
    blocks: list[str] = []
    for step in runs.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("run"), str):
            blocks.append(step["run"])
    return blocks


def _composite_action_problems(path: Path, data: dict) -> list[str]:
    """复合动作里的两类问题:**换掉环境**与**认不出来的命令**。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-3):此前复合动作**只**按
    「闸门步骤」这一条判据扫 ⇒ ①`echo "PATH=/tmp/evil" >> $GITHUB_ENV`
    (给后续所有步骤换掉解释器)与 ②`awk 'BEGIN{system("python -m pytest …")}'`
    (藏一条非规范的 pytest)**都漏过**(实测)⇒ 而复合动作是 GitHub 官方
    推荐的复用方式 ⇒ 这条通道**原样存在**。
    """
    problems: list[str] = []
    blocks = _action_run_blocks(data)
    for index, raw_run in enumerate(blocks):
        for marker in ("GITHUB_ENV", "GITHUB_PATH"):
            if marker in raw_run:
                problems.append(
                    "复合动作 %s 的第 %d 个 `run:` 块提到了 `$%s`"
                    "⇒ 它能**给后续所有步骤**换掉解释器/模块搜索路径"
                    "(与 `env:` 等价)⇒ 闸门压根没跑而 CI 看上去是绿的"
                    % (path.name, index + 1, marker))
                break
        # **复合动作里跑裁决者**(第 55 轮之十一,子代理复核的 H-6):
        # 裁决者的冒烟关卡受**旁路键纪律**与**失败语义纪律**约束
        # (R15 / R17)⇒ 但那些纪律只管**被守的 workflow** 里的步骤
        # ⇒ 把这一步搬进**复合动作** ⇒ 那些纪律**一条都套不上**
        # ⇒ 分档器的关卡等于没有。
        if "check_domain" in raw_run:
            problems.append(
                "复合动作 %s 的第 %d 个 `run:` 块里出现了裁决者冒烟"
                "(`tools/check_domain.py`)⇒ 裁决者的关卡跑在**别人**复用的"
                "动作里 ⇒ 它的旁路键 / 失败语义**逃出了**本工具的纪律"
                "⇒ 分档器的关卡等于没有" % (path.name, index + 1))
        for command in logical_commands(raw_run):
            tokens = _tokens(command)
            # **分词失败不许跳过**(第 55 轮之十四,codex H-2 的配套):
            # 此前 `tokens is None ⇒ continue` ⇒ 整条命令**隐形**
            # ⇒ 与 workflow 步骤那条路径(没有这个跳过)口径**分叉**
            # ⇒ 「认不出来 ⇒ 提到 pytest/闸门就判红」这条**兜底**在这里失效。
            kind = command_kind(tokens, command)
            if kind == "pytest":
                # **复合动作里跑 pytest**(第 55 轮之十一,子代理复核的 C-2):
                # 复合动作是 GitHub 官方推荐的**复用**方式 ⇒ 别的 workflow
                # 引用它 ⇒ 研究侧依赖(pytest / 研究侧模块)在那儿**装不上**、
                # **跑不起来** ⇒ 闸门在**别处**跑 ⇒ 「闸门跑了但没跑成」
                # 与「闸门压根没跑」在 CI 看上去**一模一样**。
                problems.append(
                    "复合动作 %s 的第 %d 个 `run:` 块里**跑 pytest**(%s)"
                    "⇒ 复合动作是**别人**复用的 ⇒ 研究侧依赖在那儿跑不起来"
                    "⇒ 闸门在别处跑 ⇒ 闸门压根没跑而 CI 看上去是绿的"
                    % (path.name, index + 1, command[:120]))
                continue
            if kind != "unknown":
                continue
            if _is_inert_command(tokens, command):
                continue
            if any(word in command for word in _DANGEROUS_WORDS):
                problems.append(
                    "复合动作 %s 的第 %d 个 `run:` 块里有认不出来的命令"
                    "提到了 pytest/闸门(判不了 ⇒ 判红):%s"
                    % (path.name, index + 1, command[:120]))
    return problems


def _uses_of(data: dict) -> list[tuple[str, str]]:
    """(位置, `uses:` 的值) 列表:步骤级与 job 级(可复用 workflow)都要。"""
    out: list[tuple[str, str]] = []
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return out
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if isinstance(job.get("uses"), str):
            out.append(("job「%s」" % job_name, job["uses"]))
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("uses"), str):
                out.append(("步骤「%s」" % (step.get("name") or "?"), step["uses"]))
    return out


def _third_party_action_problems(where: str, uses: str) -> list[str]:
    """这个 `uses:` 允许吗?

    - `./…` ⇒ 本仓库自己的动作 ⇒ **另有纪律**(`_local_action_problems`)
      ⇒ 不在这里判;
    - 在**登记名单**里 ⇒ 允许;
    - 其余(`docker://…`、没登记的第三方)⇒ 判红。
    """
    if uses.startswith("./"):
        return []
    if uses in _ALLOWED_THIRD_PARTY_ACTIONS:
        return []
    return ["%s 用了**未登记的**第三方动作 `%s` ⇒ 它可以执行**任意代码**"
            "(YAML 里连 `pytest` / `ci_gate` 字样都不必有)⇒ "
            "闸门/守卫可能压根没跑而 CI 看上去是绿的"
            "⇒ 要用它,先把 `owner/repo@ref` 登记进 "
            "`tools/check_ci_wiring.py` 的 `_ALLOWED_THIRD_PARTY_ACTIONS`"
            % (where, uses)]


def check_third_party_actions(workflows_dir: Path) -> list[str]:
    """仓库里用到的**第三方动作**必须**逐个登记**。

    **为什么**(第 55 轮之十二,codex H-4):此前守卫**只扫 `run:`**
    ⇒ `uses:` 这一整类入口**完全不在扫描面内**
    ⇒ 外部动作可以在 YAML 里不带任何 `pytest` / `ci_gate` 字样
    而执行任意代码 ⇒ 「闸门跑了但没跑成」与「闸门压根没跑」一模一样。
    ⇒ 现在**按目录扫**:`.github/workflows/` 下所有 workflow
    + `.github/actions/**/action.yml` 里的每一个 `uses:` 都要在**名单**里。
    """
    problems: list[str] = []
    if not workflows_dir.is_dir():
        return ["workflow 目录 %s 不存在 ⇒ 扫不了第三方动作(fail-closed)"
                % workflows_dir]
    candidates: list[Path] = []
    try:
        candidates.extend(sorted(path for path in workflows_dir.iterdir()
                                 if path.is_file()
                                 and path.suffix in (".yml", ".yaml")))
    except OSError as exc:                        # noqa: BLE001 - 读不了 ⇒ 判红
        return ["workflow 目录 %s 读不了:%s(fail-closed)" % (workflows_dir, exc)]
    actions_dir = workflows_dir.parent / "actions"
    if actions_dir.is_dir():
        for suffix in ("yml", "yaml"):
            candidates.extend(sorted(actions_dir.rglob("action.%s" % suffix)))
    # **仓库根目录** ⇒ 本地动作(`uses: ./…`)的路径按它解析
    # (GitHub:本地动作的路径相对**仓库根**)。
    root = workflows_dir.parent.parent
    for path in candidates:
        try:
            data = _load_yaml(path)
        except Exception as exc:                  # noqa: BLE001 - 读不了 ⇒ 判红
            problems.append("%s 读不了/不是合法 YAML:%s(fail-closed)"
                            % (path.name, exc))
            continue
        if not isinstance(data, dict):
            continue
        for where, uses in _uses_of(data):
            if uses.startswith("./"):
                problems.extend(_local_action_problems(
                    root, "%s 的%s" % (path.name, where), uses))
                continue
            problems.extend(_third_party_action_problems(
                "%s 的%s" % (path.name, where), uses))
    return problems


def _local_action_problems(root: Path, where: str, uses: str) -> list[str]:
    """**本地动作**(`uses: ./…`)的纪律。

    **为什么**(第 55 轮之十四,codex C-1):此前 `./…` **无条件放行**
    ⇒ 而扫描面只有 `.github/actions/**/action.yml`
    ⇒ 仓库里**别处**的本地动作(`./evil/action.yml`)**完全逃出扫描面**
    ⇒ 那个动作 `run:` 块里的命令**没人看**
    ⇒ 闸门压根没跑而 CI 看上去是绿的。
    ⇒ 现在:本地动作必须**存在**、必须是**复合动作**、且它的 `run:` 块
    **受同一套纪律**(`_composite_action_problems`)。
    """
    rel = uses[2:].strip()
    if not rel:
        return ["%s 用了**空的**本地动作引用 `%s` ⇒ 认不出来 ⇒ 判红"
                % (where, uses)]
    # **本地可复用 workflow**(`uses: ./.github/workflows/x.yml`)
    # ⇒ 它**本身**就是 workflow 文件 ⇒ 已被目录扫描覆盖
    # ⇒ 这里只判「文件在不在」与「位置对不对」。
    if rel.endswith((".yml", ".yaml")):
        target = root / rel
        if not target.is_file():
            return ["%s 用了**本地可复用 workflow** `%s` ⇒ 仓库里没有这个文件"
                    " ⇒ 认不出来 ⇒ 判红" % (where, uses)]
        workflows_dir = (root / ".github" / "workflows").resolve()
        resolved = target.resolve()
        inside = str(resolved).startswith(str(workflows_dir))
        if not inside:
            return ["%s 用的本地可复用 workflow `%s` 不在 `.github/workflows/` 下"
                    " ⇒ GitHub 认不到 ⇒ 判红" % (where, uses)]
        return []
    candidates = [root / rel / "action.yml", root / rel / "action.yaml"]
    found = [candidate for candidate in candidates if candidate.is_file()]
    if not found:
        return ["%s 用了**本地动作** `%s` ⇒ 仓库里没有 `%s/action.yml`"
                " ⇒ 认不出来 ⇒ 判红" % (where, uses, rel)]
    action = found[0]
    try:
        data = _load_yaml(action)
    except Exception as exc:                      # noqa: BLE001 - 读不了 ⇒ 判红
        return ["本地动作 %s 读不了/不是合法 YAML:%s(fail-closed)"
                % (action.name, exc)]
    if not isinstance(data, dict):
        return ["本地动作 %s 不是合法的 YAML 映射 ⇒ 认不出来 ⇒ 判红"
                % action.name]
    runs = data.get("runs")
    using = runs.get("using") if isinstance(runs, dict) else None
    if using != "composite":
        return ["本地动作 %s 的 `runs.using` 是 `%s` ⇒ 它的代码**不在**本工具的"
                "扫描面内(node/docker 动作能执行任意代码)⇒ 判红"
                % (action.name, using)]
    return _composite_action_problems(action, data)


def check_no_gate_steps_elsewhere(workflows_dir: Path, primary: Path) -> list[str]:
    """仓库里**别的** workflow 文件(以及复合动作)里不许有闸门步骤。

    **为什么要有这一条(第 55 轮之六,claude 第 3 轮的 M-2)**:
    `check_workflows` 只看**传进来**的那几个文件(默认两个)
    ⇒ 新增第三个 workflow 文件、或把闸门挪进去
    ⇒ 那个文件**压根不会被检查** ⇒ 闸门在**没有研究侧依赖**的地方跑
    ⇒ 「闸门跑了但没跑成」和「闸门压根没跑」在 CI 看上去一模一样。

    ⇒ 现在**按目录扫**:`.github/workflows/` 下所有 `.yml` / `.yaml`
      (`.disabled` 之类不算)里,除 `primary` 之外的文件一旦出现闸门命令
      ⇒ 判红。

    **还有复合动作**(第 55 轮之七):`.github/actions/**/action.yml` 同样是
    「能跑命令的地方」⇒ 一并扫。

    **认不出来的命令也判红**(fail-closed 方向):`bash -c "… ci_gate …"`
    这种藏起来的写法,按「提到了闸门」也算 ⇒ 藏起来也躲不掉。
    """
    problems: list[str] = []
    if not workflows_dir.is_dir():
        return ["workflow 目录 %s 不存在 ⇒ 扫不了「别的 workflow」(fail-closed)"
                % workflows_dir]
    try:
        files = sorted(path for path in workflows_dir.iterdir()
                       if path.is_file() and path.suffix in (".yml", ".yaml"))
    except OSError as exc:                        # noqa: BLE001 - 读不了 ⇒ 判红
        return ["workflow 目录 %s 读不了:%s(fail-closed)" % (workflows_dir, exc)]

    def _scan(path: Path, blocks: list[str], what: str) -> None:
        for raw_run in blocks:
            for command in logical_commands(raw_run):
                base = _gate_step_mentions(command)
                if base is None:
                    # **别的 workflow 里跑 pytest**(第 55 轮之十一,子代理复核的 C-3):
                    # 此前这里**只**查闸门步骤 ⇒ 裸 pytest 在别的 workflow 里
                    # **不被扫** ⇒ 那些地方**没装**研究侧依赖 ⇒ pytest 跑不起来
                    # ⇒ 「pytest 压根没跑」与「pytest 跑了但没跑成」在 CI 看上去
                    # **一模一样** ⇒ 闸门同样等于没有。
                    tokens = _tokens(command)
                    if tokens is not None and command_kind(tokens, command) == "pytest":
                        problems.append(
                            "%s 里**不许**跑 pytest(%s)⇒ 闸门只在 %s 里跑"
                            "(别的地方没有研究侧依赖 ⇒ pytest 跑不起来 ⇒ "
                            "闸门压根没跑而 CI 看上去是绿的)"
                            % (what, command[:120], primary.name))
                        return
                    continue
                problems.append(
                    "%s 里**不许**有闸门步骤(%s)⇒ 闸门只在 %s 里跑"
                    "(别的地方没有研究侧依赖 ⇒ 闸门跑不起来 ⇒ "
                    "闸门没跑而 CI 看上去是绿的)"
                    % (what, base, primary.name))
                return

    for path in files:
        if path.resolve() == primary.resolve():
            continue
        try:
            data = _load_yaml(path)
        except Exception as exc:                  # noqa: BLE001 - 读不了 ⇒ 判红
            problems.append("%s 读不了/不是合法 YAML:%s(fail-closed)" % (path.name, exc))
            continue
        if not isinstance(data, dict):
            continue
        _scan(path, [entry["run"] for entry in _steps_of(data)
                     if isinstance(entry["run"], str)], path.name)

    # **复合动作**(第 55 轮之七):`.github/actions/**/action.yml`。
    actions_dir = workflows_dir.parent / "actions"
    if actions_dir.is_dir():
        action_files: list[Path] = []
        for suffix in ("yml", "yaml"):
            action_files.extend(sorted(actions_dir.rglob("action.%s" % suffix)))
        for path in action_files:
            try:
                data = _load_yaml(path)
            except Exception as exc:              # noqa: BLE001 - 读不了 ⇒ 判红
                problems.append("复合动作 %s 读不了/不是合法 YAML:%s(fail-closed)"
                                % (path.name, exc))
                continue
            if not isinstance(data, dict):
                continue
            _scan(path, _action_run_blocks(data),
                  "复合动作 %s" % path.relative_to(workflows_dir.parent))
            problems.extend(_composite_action_problems(path, data))
    return problems


def check_wiring_presence(workflow: Path, expected_script: str = _CANONICAL_WIRING_SCRIPT) -> list[str]:
    """这个 workflow 里**必须**有一处「跑 `expected_script`」的调用。

    **为什么要有这一条(以及为什么两个文件都要有)**:
    守卫放在它所守的步骤里 ⇒ 那一步被删,守卫一起消失
    (这正是 codex C1 说的「守卫自己悄悄没了」)。所以它必须**独立**存在,
    而且两个 workflow **各自**都要有一处 ⇒ 删掉其中一处,另一处会发现。
    (两处都被删 ⇒ 仓库内无人发现——这是**已知残余风险**,只能靠评审与分支保护兜。)

    **两个落点各是什么(第 55 轮之四)**:
    - `research-guard.yml` → `python tools/check_ci_wiring.py`
      (它读 workflow 的 YAML ⇒ 需要 PyYAML ⇒ 只能待在研究层);
    - `constitution-guard.yml` → `python tools/check_constitution_history.py`
      (宪法层是**纯标准库 + git**,**不装**任何东西 ⇒ 那个工具自己
      用**文本级**核对「研究 workflow 里的接线自检还在」)。

    **而且那处调用必须是「不带参数」的规范形**(第 55 轮**实测**出来的洞):
    带参数(尤其 `--workflow`)能把守卫的检查范围缩小到**别的文件**上,
    于是守卫「查的不是它该查的」却照样报「[OK]」。
    """
    problems: list[str] = []
    try:
        data = _load_yaml(workflow)
    except Exception as exc:                      # noqa: BLE001
        return ["%s 读不了:%s(fail-closed)" % (workflow.name, exc)]

    # **workflow 级 `env:`(第 55 轮之六,codex 第 3 轮的 C1):
    # 它被**所有** job/step 继承 ⇒ 连守卫自己都能被换掉解释器。
    #
    # **措辞与上面那条(形状检查里的)必须逐字相同**(第 55 轮之七,claude 第 4 轮的 L-2):
    # 两条规则判的是**同一条问题** ⇒ 文案不同 ⇒ `unique_problems` 的
    # **整串去重**合不掉 ⇒ 同一条问题在结论里出现两遍(措辞还不一样)
    # ⇒ 读的人以为是两件事 ⇒ 反而掩盖了「其实只有一处」。
    # ⇒ 现在两边**共用同一句** ⇒ 去重生效 ⇒ 只报一遍。
    if "env" in data:
        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "
                        "解释器/模块搜索路径可以被换掉(实测:提交一个 `exit 0` 的 python "
                        "放在 PATH 前面 ⇒ 闸门没跑而工具照样报「[OK]」)" % workflow.name)

    found = False
    saw_wiring = False
    for entry in _steps_of(data):
        raw_run = entry["run"]
        if not isinstance(raw_run, str):
            problems.append("%s 里步骤「%s」(%s)的 `run:` 不是字符串(是 %s)⇒ "
                            "判不了(fail-closed)"
                            % (workflow.name, entry["name"], entry["job"],
                               type(raw_run).__name__))
            continue
        commands = logical_commands(raw_run)
        for command in commands:
            tokens = _tokens(command)
            script = script_of(tokens) if tokens else None
            if script is None:
                # 认不出来(藏进 `bash -c` 之类)⇒ 不算「跑过」⇒
                # 下面会报「压根没有」——藏起来也躲不掉。
                continue
            if _normalize_script(script) != _normalize_script(expected_script):
                continue
            saw_wiring = True
            if not canonical_wiring(tokens, expected_script):
                problems.append(
                    "%s 里跑守卫的那一步(%s)的调用**不是规范形**(%s):%s ⇒ "
                    "守卫的检查范围可以被缩小(实测:加 `--workflow <别处文件>` ⇒ "
                    "真实的 workflow 压根没被查,工具却照样报「[OK]」)"
                    % (workflow.name, entry["name"], expected_script, command[:120]))
                continue
            found = True
            step = entry["step"]
            job = entry["job_body"]
            if len(commands) != 1:
                problems.append("%s 里跑守卫的那一步(%s)必须**只有一条命令**"
                                % (workflow.name, entry["name"]))
            if has_background_operator(raw_run):
                problems.append("%s 里跑守卫的那一步(%s)的命令尾部有 `&` ⇒ "
                                "守卫被丢到后台、**退出码被丢弃**"
                                % (workflow.name, entry["name"]))
            for key in ("if", "continue-on-error", "shell", "working-directory"):
                if key in step:
                    problems.append("%s 里跑守卫的那一步带了 `%s:` ⇒ 它可以不算数"
                                    % (workflow.name, key))
            if "env" in step:
                problems.append("%s 里跑守卫的那一步带了 `env:` ⇒ 解释器/模块搜索路径"
                                "可以被换掉 ⇒ 守卫压根没跑而工具照样报「[OK]」"
                                % workflow.name)
            for key in ("if", "continue-on-error"):
                if key in job:
                    problems.append("%s 里跑守卫的那个 job 带了 `%s:`" % (workflow.name, key))
            if "env" in job:
                problems.append("%s 里跑守卫的那个 job 带了 `env:` ⇒ 解释器可以被换掉"
                                % workflow.name)
            # **容器**(第 55 轮之七,codex 第 4 轮的 H1):
            # `container:` / `services:` ⇒ 这一步整个跑在**别人给的镜像**里
            # ⇒ 守卫自己压根没跑,而工具照样报「[OK]」。
            for key in ("container", "services"):
                if key in job:
                    problems.append(
                        "%s 里跑守卫的那个 job 带了 `%s:` ⇒ 守卫整个跑在"
                        "**别人给的镜像/容器**里 ⇒ 守卫压根没跑而工具照样报「[OK]」"
                        % (workflow.name, key))
            # **job 级 `defaults.run.*`**(第 55 轮之七):
            # 此前只禁**步骤级**的 `working-directory`/`shell` 与 workflow 级的
            # ⇒ job 级**照样**能把守卫的执行语义/工作目录换掉。
            job_defaults = job.get("defaults")
            if isinstance(job_defaults, dict):
                run_defaults = job_defaults.get("run")
                if isinstance(run_defaults, dict):
                    if "shell" in run_defaults:
                        problems.append(
                            "%s 里跑守卫的那个 job 在 **job 级** defaults.run.shell 上"
                            "改了 shell(%r)⇒ 守卫的执行语义不再由我们自己说了算"
                            % (workflow.name, run_defaults["shell"]))
                    if "working-directory" in run_defaults:
                        problems.append(
                            "%s 里跑守卫的那个 job 在 **job 级** "
                            "defaults.run.working-directory 上改了工作目录(%r)⇒ "
                            "守卫那一步不在仓库根下跑"
                            % (workflow.name, run_defaults["working-directory"]))
            if "needs" in job:
                # **C1(codex 第 2 轮)**:守卫那个 job 挂了 `needs:` 指向一个
                # 恒被跳过的 job ⇒ 守卫自己也不跑 ⇒ 闸门与自检双双消失。
                problems.append("%s 里跑守卫的那个 job 带了 `needs:` ⇒ 上游 job 一旦被跳过"
                                "(如 `if: false`),守卫自己也不跑 ⇒ 闸门与自检双双消失,"
                                "而 CI 记成 skipped(分支保护视为通过)" % workflow.name)
    if not found and not saw_wiring:
        # 上面报过「调用不是规范形」时**不再**说「压根没有」——
        # 把「有但写法不对」说成「没有」会让人以为守卫漏看了那一行。
        problems.append("%s 里**没有任何**跑 `%s` 的地方 ⇒ "
                        "守卫自己可以被悄悄删掉" % (workflow.name, expected_script))
    return problems


def check_constitution_duty(root: Path) -> list[str]:
    """宪法层那个「独立落点」的职责**必须真的在**——否则它只是纸上的。

    **为什么要有这一条**:宪法 workflow 里跑的是 `tools/check_constitution_history.py`,
    它**自己**负责用纯文本核对「研究 workflow 里的接线自检还在」。
    如果有人把那条职责从那个工具里**删掉**(或者把函数改名),
    宪法 workflow 照样绿 ⇒ 独立落点变成**空壳** ⇒ 而研究层
    只会看到「宪法 workflow 里有一处规范形的调用」,**看不出来**它已经不做事了。

    ⇒ 这里**动态导入**那个工具,把它的检查**真的跑一遍**:
      - 导入失败 / 函数不在 / 调用抛异常 ⇒ **fail-closed** 报问题;
      - 它自己报出问题 ⇒ 原样带上来(研究层与宪法层**同一个结论**)。
    """
    problems: list[str] = []
    module_path = root / "tools" / "check_constitution_history.py"
    if not module_path.exists():
        return ["%s 不存在 ⇒ 宪法层的独立落点拿不到检查(fail-closed)" % module_path.name]
    try:
        spec = importlib.util.spec_from_file_location(
            "check_constitution_history", module_path)
        if spec is None or spec.loader is None:
            raise ImportError("拿不到 %s 的加载器" % module_path.name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception as exc:                      # noqa: BLE001 - 崩了 ⇒ 判红
        return ["宪法层的工具导不进来:%r(fail-closed)" % (exc,)]
    checker = getattr(module, "check_ci_wiring_presence", None)
    if checker is None:
        return ["宪法层的工具里**没有** `check_ci_wiring_presence` ⇒ "
                "独立落点的职责被删掉了(fail-closed)"]
    try:
        found = checker(root)
    except Exception as exc:                      # noqa: BLE001 - 崩了 ⇒ 判红
        return ["宪法层的检查自己崩了:%r(fail-closed)" % (exc,)]
    if not isinstance(found, list):
        return ["宪法层的检查返回的不是清单:%r(fail-closed)" % (found,)]
    problems.extend(found)
    return problems


def unique_problems(problems: list[str]) -> list[str]:
    """去掉**重复**的问题(保序:第一次出现的先后顺序不变)。

    **为什么**:同一条问题报两遍 ⇒ 读的人会以为是两件事 ⇒ 反而掩盖了
    「其实只有一处」。

    **为什么现在还需要它**:第 55 轮之五之后,**结构上**已经没有重复源了
    (名单只解析一次)⇒ 这层是**兜底**:将来新增检查点时,同一件事被两个
    检查点各报一遍,只报一遍。兜底也要**有人盯**⇒ 它有自己的用例钉着。
    """
    seen: set[str] = set()
    unique: list[str] = []
    for problem in problems:
        if problem in seen:
            continue
        seen.add(problem)
        unique.append(problem)
    return unique


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="CI 接线的结构自检(规范形 + fail-closed)")
    parser.add_argument("--workflow", action="append", default=[], dest="workflows",
                        type=Path,
                        help="**额外**再查一个 workflow(可重复)。**不是**替换:"
                             "默认要查的那两个永远会被查——"
                             "从命令行缩小检查范围会让守卫可以被自己关掉")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER,
                        help="机器可读的名单(默认 tests/guard_roster.json)")
    args = parser.parse_args(argv)

    # **默认那两个永远都要查**:`--workflow` 只是**追加**。
    # (第 55 轮实测的洞:曾经 `--workflow` 是**替换** ⇒ 加一个参数就能让守卫空转。)
    workflows: list[Path] = [RESEARCH_WORKFLOW, CONSTITUTION_WORKFLOW]
    for extra in args.workflows:
        if extra.resolve() not in {w.resolve() for w in workflows}:
            workflows.append(extra)

    problems: list[str] = []
    existing: list[Path] = []
    # 名单本身的问题(读不了 / 不是合法 JSON / 结构不对 / 不在仓库里)
    # ⇒ 「判不了」⇒ 退出码 2(见下面)。
    roster_problems = _read_roster(args.roster).problems
    for workflow in workflows:
        if not workflow.exists():
            problems.append("workflow 不存在:%s" % workflow)
            continue
        existing.append(workflow)
        # 两个落点各归其位:研究 workflow 跑接线自检(它要读 YAML ⇒ 需要 PyYAML),
        # 宪法 workflow 跑宪法守卫(纯标准库 ⇒ 那个层不装任何东西,永远会跑)。
        expected = (_CONSTITUTION_GUARD_SCRIPT
                    if workflow.resolve() == CONSTITUTION_WORKFLOW.resolve()
                    else _CANONICAL_WIRING_SCRIPT)
        problems.extend(check_wiring_presence(workflow, expected))

    # 宪法层那个落点的职责**必须真的在**(否则独立落点只是纸上的)——
    # 动态导入那个工具并把它的检查**真的跑一遍**(导入失败 ⇒ fail-closed)。
    problems.extend(check_constitution_duty(ROOT))

    # 形状 + 名单覆盖:覆盖按**单文件**算(闸门所在的 workflow 自己就要覆盖全)。
    problems.extend(check_workflows(existing, args.roster, RESEARCH_WORKFLOW))

    # 仓库里**别的** workflow 文件里不许有闸门步骤(M-2):按**目录**扫,
    # 不只查传进来的那几个 ⇒ 新增第三个 workflow 也躲不掉。
    problems.extend(check_no_gate_steps_elsewhere(
        RESEARCH_WORKFLOW.parent, RESEARCH_WORKFLOW))

    # **第三方动作必须逐个登记**(第 55 轮之十二,codex H-4):
    # `uses:` 一步能执行**任意代码** ⇒ 它比 `run:` 更危险
    # ⇒ 此前**完全不在扫描面内** ⇒ 外部动作可以在 YAML 里不带任何
    # 闸门字样而执行任意代码 ⇒ 「闸门跑了但没跑成」与「闸门压根没跑」一样。
    problems.extend(check_third_party_actions(RESEARCH_WORKFLOW.parent))

    # **裁决者的关卡必须在**(第 55 轮之八,实测):把那一步整个删掉 ⇒
    # 上面那些纪律一条都不会触发 ⇒ 静默消失 ⇒ 后果域分档那条线没人管。
    problems.extend(check_self_guard_present(existing))

    # **同样的问题只报一遍**(第 55 轮之五,codex 第 2 轮的 L2):
    # 名单被每个 workflow 各解析一次 ⇒ 名单坏掉时同一条问题报两遍。
    # (结构上的重复源已经去掉 ⇒ 这层现在是**兜底**。)
    problems = unique_problems(problems)

    # **名单本身判不了 ⇒ 退出码 2**(第 55 轮之十,codex 第 5 轮的 M-5):
    # 「名单结构不对」在三处消费者里必须**同一个退出码**:
    # `tools/check_ci_ran.py`(闸门核对)与 `tools/ci_gate.py`(闸门)都返回 **2**
    # (环境不可用/fail-closed)⇒ 接线自检此前返回 **1**
    # ⇒ 与「形状不合规」混在一起 ⇒ 同一个坏名单在三个工具里**看起来**是三件事。
    if roster_problems:
        print("[FAIL] CI 接线判不了(退出码 2):名单本身有问题(%d 条)"
              "⇒ 判不了 ⇒ fail-closed(与闸门、闸门工具同一个退出码)"
              % len(roster_problems))
        # 名单问题**也要列出来**(否则结论行说「名单有问题」却看不到问题)。
        for problem in unique_problems(list(roster_problems) + problems):
            print("  - %s" % problem)
        return 2

    if problems:
        print("[FAIL] CI 接线有问题(%d 条):" % len(problems))
        for problem in problems:
            print("  - %s" % problem)
        return 1
    print("[OK ] CI 接线合规:闸门步骤都是规范形、名单里的套件都被跑到、"
          "自检本身也有人跑。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
