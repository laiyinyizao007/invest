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
# 「危险工具」的文本指纹:出现在**认不出来的**命令里 ⇒ 判红。
_DANGEROUS_WORDS = ("pytest", "check_ci_ran", "ci_gate")
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
# 这些脚本的**退出码就是证据链**——「75 种变异全部被抓住」这句话,
# 靠的就是 `tools/check_mutations.py --verify` 与
# `tools/check_guard_mutations.py --verify` 这两步**红**。
# 若这两步可以被 `continue-on-error:` / `if: false` / `|| true` /
# 尾部 `&` 轻轻放过 ⇒ 那句话变成一句空话,而 CI 照样绿。
# ⇒ 对它们施加与闸门步骤**同一套**纪律。
_PROTECTED_SCRIPTS = {
    "tools/check_mutations.py",
    "tools/check_guard_mutations.py",
}
# 「后台执行」的操作符:裸 `&`(不是 `&&`,也不是 `2>&1` / `&>` 这种重定向)。
# `cmd &` ⇒ shell 立刻返回 0 ⇒ 退出码被丢弃(实测:`bash -c 'false &'` → 0)。
_BACKGROUND_RE = re.compile(r"(?<![&>])&(?!&|>)")
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


def logical_commands(script: str) -> list[str]:
    """把一段 shell 脚本切成**逻辑命令行**。

    切点:续行(`\\` 结尾)、换行、`;`、`&&`、`||`、`|`、`&`。

    **不做引号/转义解析**——这是**故意的**:切得比真实 shell **更碎**,
    只会让「危险命令」更容易被认出来(认不出 ⇒ 判红,方向是安全的)。
    (第 54 轮那套按引号配对切分,结果 `bash -c "a; b"` 被当成一条命令 ⇒ 漏。)
    """
    lines: list[str] = []
    buf = ""
    for raw in script.splitlines():
        line = _strip_shell_comment(raw.strip()).strip()
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

    commands: list[str] = []
    for line in lines:
        for part in re.split(r"&&|\|\||[;|&]", line):
            part = part.strip()
            if part:
                commands.append(part)
    return commands


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
        if _BACKGROUND_RE.search(line):
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
    base = normalized.rsplit("/", 1)[-1]
    if base.endswith(".py"):
        base = base[:-3]
    if base in {"check_mutations", "check_guard_mutations"}:
        return base
    return None


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
    if _PYTHON_RE.match(head):
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
    base = script.rsplit("/", 1)[-1].lower()
    if base == "pytest":
        return "pytest"
    if base in ("check_ci_ran.py", "ci_gate.py"):
        return "gate"
    if base == "check_ci_wiring.py":
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
    if not _PYTHON_RE.match(tokens[0].rsplit("/", 1)[-1].lower()):
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

    # **哪些 job 里有闸门步骤**(第 55 轮之六):job 级的旁路键(env/needs/…)
    # 由**闸门那条规则**负责报;受保护脚本那条规则**只在没有闸门步骤的 job 里**
    # 才报 job 级键 ⇒ 同一条问题**不报两遍**(L2),而两条规则**各有各的**见证。
    gate_jobs: set[str] = set()
    for job_name, job in (data.get("jobs") or {}).items() if isinstance(
            data.get("jobs"), dict) else []:
        if not isinstance(job, dict):
            continue
        for entry in _steps_of_job(job_name, job):
            raw = entry["run"]
            if not isinstance(raw, str):
                continue
            for command in logical_commands(raw):
                tokens = _tokens(command)
                if tokens is not None and canonical_gate(tokens) is not None:
                    gate_jobs.add(job_name)
                    break

    for entry in _steps_of(data):
        step = entry["step"]
        job = entry["job_body"]
        raw_run = entry["run"]
        if "working-directory" in step:
            # **H-2(claude 第 3 轮)**:步骤级 `working-directory:` 把这一步的工作目录
            # 换掉 ⇒ 相对路径(`tools/…`、`tests/…`)指向别处 ⇒ 跑的东西不是我们的。
            problems.append("步骤「%s」(%s)带了 `working-directory:` ⇒ 这一步不在仓库根下跑"
                            "(相对路径会指向别处)" % (entry["name"], entry["job"]))
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

        for command in commands:
            tokens = _tokens(command)
            kind = command_kind(tokens, command)
            candidate = canonical_gate(tokens)
            if protected is None:
                protected = _protected_call_label(tokens)
            if candidate is not None:
                if gate is not None:
                    stray.append("这一步里**有两条**规范闸门命令")
                gate = candidate
                continue
            if kind in ("pytest", "gate"):
                stray.append("非规范形态的 %s 调用:%s" % (kind, command[:120]))
            elif kind == "unknown":
                if any(word in command for word in _DANGEROUS_WORDS):
                    stray.append("认不出来的命令里提到了 pytest/闸门(判不了 ⇒ 判红):%s"
                                 % command[:120])

        # --- **受保护脚本**(证据链):与闸门步骤同一套纪律 ---
        # (第 55 轮之六,codex 第 3 轮的 H2:此前只有闸门步骤与两处接线调用
        #  受约束 ⇒ `check_mutations.py --verify` / `check_guard_mutations.py
        #  --verify` 这两步可以被 `continue-on-error:` 一行废掉 ⇒
        #  「75 种变异全部被抓住」变成空话,而 CI 照样绿。)
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
            if entry["job"] not in gate_jobs:
                # 这个 job 里**没有**闸门步骤 ⇒ job 级的那几键由**这条**规则报
                # (有闸门步骤的 job ⇒ 由闸门那条规则报 ⇒ 同一条问题只报一遍)。
                for key in ("if", "continue-on-error", "env", "needs"):
                    if key in job:
                        problems.append("受保护脚本(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"
                                        % (protected, key))

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
        for key in ("if", "continue-on-error"):
            if key in job:
                problems.append("闸门步骤「%s」所在的 job 带了 `%s:` ⇒ 整个 job 可以不出力"
                                % (entry["name"], key))
        if "env" in job:
            problems.append("闸门步骤「%s」所在的 job 带了 `env:` ⇒ 同上(解释器可被换掉)"
                            % entry["name"])
        if "needs" in job:
            # **C1(codex 第 2 轮)**:job 挂了 `needs:` 指向一个**恒被跳过**的 job
            # ⇒ GitHub 把这个 job 一起跳过 ⇒ 记为 skipped(分支保护**不算失败**)
            # ⇒ 闸门压根没跑,而 CI 看上去是绿的。
            problems.append("闸门步骤「%s」所在的 job 带了 `needs:` ⇒ 上游 job 一旦被跳过"
                            "(如 `if: false`),这个 job 也被跳过 ⇒ 闸门压根没跑,"
                            "而 CI 记成 skipped(分支保护视为通过)" % entry["name"])
        job_defaults = job.get("defaults")
        if isinstance(job_defaults, dict):
            run_defaults = job_defaults.get("run")
            if isinstance(run_defaults, dict) and "shell" in run_defaults:
                problems.append("闸门步骤「%s」所在的 job 在 **job 级** defaults.run.shell "
                                "上改了 shell(%r)⇒ 这一步的执行语义不再由我们自己说了算"
                                % (entry["name"], run_defaults["shell"]))
            if isinstance(run_defaults, dict) and "working-directory" in run_defaults:
                problems.append("闸门步骤「%s」所在的 job 在 **job 级** "
                                "defaults.run.working-directory 上改了工作目录(%r)⇒ "
                                "闸门那一步不在仓库根下跑"
                                % (entry["name"], run_defaults["working-directory"]))

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


def check_no_gate_steps_elsewhere(workflows_dir: Path, primary: Path) -> list[str]:
    """仓库里**别的** workflow 文件里不许有闸门步骤。

    **为什么要有这一条(第 55 轮之六,claude 第 3 轮的 M-2)**:
    `check_workflows` 只看**传进来**的那几个文件(默认两个)
    ⇒ 新增第三个 workflow 文件、或把闸门挪进去
    ⇒ 那个文件**压根不会被检查** ⇒ 闸门在**没有研究侧依赖**的地方跑
    ⇒ 「闸门跑了但没跑成」和「闸门压根没跑」在 CI 看上去一模一样。

    ⇒ 现在**按目录扫**:`.github/workflows/` 下所有 `.yml` / `.yaml`
      (`.disabled` 之类不算)里,除 `primary` 之外的文件一旦出现闸门命令
      ⇒ 判红。

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
        for entry in _steps_of(data):
            raw_run = entry["run"]
            if not isinstance(raw_run, str):
                continue
            for command in logical_commands(raw_run):
                tokens = _tokens(command)
                script = script_of(tokens) if tokens else None
                if script is None:
                    # 认不出来 ⇒ 只在**提到闸门**时判红(fail-closed 方向)。
                    if any(word in command for word in ("check_ci_ran", "ci_gate")):
                        problems.append(
                            "%s 里有认不出来的命令提到了闸门:%s ⇒ 判不了(fail-closed)"
                            % (path.name, command[:120]))
                        break
                    continue
                base = script.rsplit("/", 1)[-1].lower()
                if base in ("ci_gate.py", "check_ci_ran.py"):
                    problems.append(
                        "%s 里**不许**有闸门步骤(%s)⇒ 闸门只在 %s 里跑"
                        "(别的 workflow 没有研究侧依赖 ⇒ 闸门跑不起来 ⇒ "
                        "闸门没跑而 CI 看上去是绿的)"
                        % (path.name, base, primary.name))
                    break
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
    if "env" in data:
        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "
                        "守卫自己的解释器也可以被换掉 ⇒ 守卫压根没跑而工具照样报「[OK]」"
                        % workflow.name)

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

    # **同样的问题只报一遍**(第 55 轮之五,codex 第 2 轮的 L2):
    # 名单被每个 workflow 各解析一次 ⇒ 名单坏掉时同一条问题报两遍。
    # (结构上的重复源已经去掉 ⇒ 这层现在是**兜底**。)
    problems = unique_problems(problems)

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
