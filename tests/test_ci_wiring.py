"""接线自检工具**自己**的用例(第 55 轮)。

**为什么新写的守卫工具要有人守**(仓库教训:「新写的守卫工具自己也要有人守」):
`tools/check_ci_wiring.py` 是**接线自检**的本体。它要是坏了(判据放松了、
自己崩了、或者干脆变成"一律报红"),CI 会照样绿——而它守着的正是
「CI 会不会红」这件事本身。

所以这里配**双向**探针:
  - **放行侧**:合法形态**不许**报红(否则工具变成"一律报红",等于没判据);
  - **拦截侧**:第 54 轮**实测**过的那几种藏法,每一种都必须被抓;
  - **金丝雀**:`tools/ci_gate.py` 真的会让 CI 变红吗?——用一个临时套件
    证明「名单点名的用例没跑 ⇒ 退出码非 0」。

**这一份套件自己也在名单里**(`tests/guard_roster.json`):它整份被跳过时,
CI 侧的闸门会点名报红。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI_GATE = ROOT / "tools" / "ci_gate.py"
RESEARCH_WORKFLOW = ROOT / ".github" / "workflows" / "research-guard.yml"
CONSTITUTION_WORKFLOW = ROOT / ".github" / "workflows" / "constitution-guard.yml"
ROSTER = ROOT / "tests" / "guard_roster.json"


def _load(name: str, rel: str):
    """按**文件路径**导入工具(tools/ 不是包)。"""
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wiring = _load("check_ci_wiring", "tools/check_ci_wiring.py")
constitution = _load("check_constitution_history", "tools/check_constitution_history.py")
ci_ran = _load("check_ci_ran", "tools/check_ci_ran.py")

# 两个 workflow **各自**的落点(第 55 轮之四):
#   研究层 → 接线自检(要读 YAML ⇒ 需要 PyYAML);
#   宪法层 → 宪法守卫(纯标准库 ⇒ 那个层一个依赖都不装,永远会跑)。
WIRING_SCRIPT = "tools/check_ci_wiring.py"
CONSTITUTION_SCRIPT = "tools/check_constitution_history.py"


def _problems(workflow: Path) -> list[str]:
    """跑一遍接线自检;工具**崩了**也算一条问题(不能把崩溃当成"没问题")。"""
    try:
        return wiring.check_gate_wiring(workflow, ROSTER)
    except Exception as exc:                      # noqa: BLE001 - 崩溃 ⇒ 判红
        return ["接线自检自己崩了:%r" % (exc,)]


def _write_workflow(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "mutated.yml"
    path.write_text(text, encoding="utf-8")
    return path


REAL_TEXT = RESEARCH_WORKFLOW.read_text(encoding="utf-8")

GATE_LINE = ('        run: python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
             ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py')
STEP_NAME = ("      - name: 研究侧边界不变量(批准唯一写入者 / Preset 边界 / 工具目录 /"
             " 不 import vnpy)")
JOB_HEAD = "  research:\n    runs-on: ubuntu-latest"
TRIGGERS = "on:\n  push:\n    branches: [main]\n  pull_request:"

# 藏起来的几种形态(第 54 轮**实测**过其中一种会让七条纪律守卫全部真空绿)。
HIDDEN = {
    "藏进 bash -c":
        '        run: bash -c "python tools/ci_gate.py --junitxml \\"$RUNNER_TEMP/boundary.xml\\"'
        ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py"',
    "藏进命令替换":
        '        run: out=$(python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
        ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py)',
    "藏进 shell 函数":
        '        run: |\n'
        '          run_gate() { python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
        ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py; }\n'
        '          run_gate',
    "用 eval 藏起来":
        '        run: eval "python tools/ci_gate.py --junitxml \\"$RUNNER_TEMP/boundary.xml\\"'
        ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py"',
    "直接跑 pytest":
        "        run: python -m pytest -p no:cacheprovider -q tests/test_research_boundary.py",
    "用版本化解释器跑 pytest":
        "        run: python3.12 -m pytest -p no:cacheprovider -q tests/test_research_boundary.py",
    "用运行器跑 pytest":
        "        run: uv run python -m pytest -p no:cacheprovider -q tests/test_research_boundary.py",
}


# --------------------------------------------------------------------------
# 一、真实仓库的接线必须合规(这一条是**变异见证用例**)
# --------------------------------------------------------------------------
def test_the_repository_workflow_satisfies_the_wiring_rules():
    """仓库里的 workflow 必须**原样**通过接线自检。

    **这一条是变异验证的见证用例**:workflow 被改坏(藏起来、加旁路、
    删掉某一步)时,它必须**红**。
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert problems == [], "CI 接线不合规:\n  - " + "\n  - ".join(problems)


def test_both_workflows_run_the_wiring_check_itself():
    """两个 workflow 里**各自**都要有一处跑**自己那个守卫**的地方。

    **为什么两个都要有**:守卫放在它所守的步骤里 ⇒ 那一步被删,守卫一起消失。
    两处互为兜底(删掉一处,另一处会发现)。

    **两个落点各是什么(第 55 轮之四)**:
      - `research-guard.yml` → `python tools/check_ci_wiring.py`
        (它读 workflow 的 YAML ⇒ 需要 PyYAML ⇒ 只能待在研究层);
      - `constitution-guard.yml` → `python tools/check_constitution_history.py`
        (宪法层是**纯标准库 + git**,**不装**任何东西 ⇒ 那个层永远会跑;
         它自己用**纯文本**核对「研究 workflow 里的接线自检还在」)。
    """
    for workflow, script in ((RESEARCH_WORKFLOW, WIRING_SCRIPT),
                             (CONSTITUTION_WORKFLOW, CONSTITUTION_SCRIPT)):
        problems = wiring.check_wiring_presence(workflow, script)
        assert problems == [], "%s 缺少 %s:\n  - %s" % (
            workflow.name, script, "\n  - ".join(problems))


def test_the_roster_suites_are_all_run_by_some_gate_step():
    """名单点名的**每一份**套件都必须真的被 CI 跑(codex 的 C1)。"""
    roster = json.loads(ROSTER.read_text(encoding="utf-8"))
    must_run = set(roster.get("must_run", {}))
    covered: set[str] = set()
    for entry in wiring._steps_of(wiring._load_yaml(RESEARCH_WORKFLOW)):
        for command in wiring.logical_commands(entry["run"]):
            candidate = wiring.canonical_gate(wiring._tokens(command))
            if candidate:
                covered.update(candidate["suites"])
    assert must_run, "名单是空的 ⇒ 这条断言会空转"
    assert not (must_run - covered), (
        "这些套件没有任何规范闸门步骤在跑它们:%s(把那一步删掉也不会有人发现)"
        % sorted(must_run - covered))


# --------------------------------------------------------------------------
# 二、规范形的识别:放行侧
# --------------------------------------------------------------------------
CANONICAL = ('python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
             ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py')


@pytest.mark.parametrize("command", [
    CANONICAL,
    # 顺序不同也认(选项的**集合**一样就行)。
    ('python tools/ci_gate.py --suite tests/test_research_boundary.py'
     ' --roster tests/guard_roster.json --junitxml "$RUNNER_TEMP/boundary.xml"'),
    # 解释器换个写法(第 54 轮的反向对照:守卫不能认死字符串)。
    CANONICAL.replace("python ", "python3 ", 1),
    CANONICAL.replace("python ", "python3.12 ", 1),
    # 多个套件。
    ('python tools/ci_gate.py --junitxml "$RUNNER_TEMP/g.xml" --roster tests/guard_roster.json'
     ' --suite tests/test_a.py --suite tests/test_b.py'),
])
def test_the_canonical_form_is_recognised(command):
    assert wiring.canonical_gate(shlex.split(command)) is not None


# --------------------------------------------------------------------------
# 三、规范形的识别:拦截侧
# --------------------------------------------------------------------------
@pytest.mark.parametrize("command", [
    # 直接跑 pytest(绕开闸门)。
    "python -m pytest -q tests/test_research_boundary.py",
    "python3.12 -m pytest -q tests/test_research_boundary.py",
    "uv run python -m pytest -q tests/test_research_boundary.py",
    # 旧的闸门工具(它只核对报告,不生成报告——中间那道缝还在)。
    ('python tools/check_ci_ran.py --junitxml "$RUNNER_TEMP/boundary.xml"'
     " --roster tests/guard_roster.json --suite tests/test_research_boundary.py"),
    # 藏起来。
    'bash -c "python tools/ci_gate.py --junitxml \\"$RUNNER_TEMP/boundary.xml\\""',
    "eval python tools/ci_gate.py",
    # 少了 --suite / 多了别的选项 / 换了报告路径 ⇒ 都不是规范形。
    ('python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
     " --roster tests/guard_roster.json"),
    ('python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
     " --roster tests/guard_roster.json --suite tests/test_a.py --allow-unlisted-suite"),
])
def test_a_non_canonical_command_is_not_recognised(command):
    assert wiring.canonical_gate(shlex.split(command)) is None


@pytest.mark.parametrize("label,command", sorted(HIDDEN.items()))
def test_a_hidden_gate_call_is_not_canonical(label, command):
    """藏起来的闸门调用**不得**被当成规范形。"""
    assert wiring.canonical_gate(wiring._tokens(command)) is None, label


@pytest.mark.parametrize("command", [
    "python -W error -m pytest -q tests/test_a.py",
    "python -m pytest -q tests/test_a.py",
    "uv run python -m pytest -q tests/test_a.py",
    "python3.12 -m pytest -q tests/test_a.py",
])
def test_the_script_of_a_command_is_found_even_with_flags(command):
    """**会吃值的选项**不能让识别失灵(codex 第 2 轮的 M2)。

    `python -W error -m pytest` 里,`error` 是 `-W` 的**值**,不是脚本。
    此前按「跳过所有以 - 开头的」处理 ⇒ `error` 被当成脚本 ⇒ 认不出 pytest。
    """
    assert wiring.command_kind(wiring._tokens(command), command) == "pytest"


def test_an_unrecognisable_command_is_reported_as_unknown():
    """认不出来的命令 ⇒ `unknown`(上层据此 fail-closed)。"""
    assert wiring.command_kind(wiring._tokens('bash -c "ls"'), 'bash -c "ls"') == "unknown"


# --------------------------------------------------------------------------
# 四、workflow 层面的探针:每一种改坏都必须被抓
# --------------------------------------------------------------------------
@pytest.mark.parametrize("label,old,new,expected", [
    ("闸门藏进 bash -c", GATE_LINE, HIDDEN["藏进 bash -c"], "认不出来"),
    ("闸门藏进命令替换", GATE_LINE, HIDDEN["藏进命令替换"], "认不出来"),
    ("闸门藏进 shell 函数", GATE_LINE, HIDDEN["藏进 shell 函数"], "认不出来"),
    ("闸门用 eval 藏起来", GATE_LINE, HIDDEN["用 eval 藏起来"], "认不出来"),
    ("直接跑 pytest", GATE_LINE, HIDDEN["直接跑 pytest"], "非规范形态"),
    ("用版本化解释器跑 pytest", GATE_LINE, HIDDEN["用版本化解释器跑 pytest"], "非规范形态"),
    ("用运行器跑 pytest", GATE_LINE, HIDDEN["用运行器跑 pytest"], "非规范形态"),
    ("闸门后接恒真后缀", GATE_LINE, GATE_LINE + " || true", "只有一条命令"),
    ("闸门后接 exit 0", GATE_LINE,
     "        run: |\n          python tools/ci_gate.py --junitxml \"$RUNNER_TEMP/boundary.xml\""
     " --roster tests/guard_roster.json --suite tests/test_research_boundary.py\n"
     "          exit 0", "只有一条命令"),
    ("闸门前接 set +e", GATE_LINE,
     "        run: |\n          set +e\n"
     "          python tools/ci_gate.py --junitxml \"$RUNNER_TEMP/boundary.xml\""
     " --roster tests/guard_roster.json --suite tests/test_research_boundary.py",
     "只有一条命令"),
    ("报告写进仓库里", GATE_LINE,
     '        run: python tools/ci_gate.py --junitxml "boundary.xml"'
     " --roster tests/guard_roster.json --suite tests/test_research_boundary.py",
     "$RUNNER_TEMP"),
    ("闸门步骤带 if", STEP_NAME + "\n" + GATE_LINE,
     STEP_NAME + '\n        if: ""\n' + GATE_LINE, "`if:`"),
    ("闸门步骤带 continue-on-error", STEP_NAME + "\n" + GATE_LINE,
     STEP_NAME + '\n        continue-on-error: "false"\n' + GATE_LINE, "continue-on-error"),
    ("闸门步骤带 shell", STEP_NAME + "\n" + GATE_LINE,
     STEP_NAME + "\n        shell: bash\n" + GATE_LINE, "`shell:`"),
    ("闸门所在的 job 带 if", JOB_HEAD, JOB_HEAD + "\n    if: false", "job 带了"),
    ("闸门所在的 job 带 continue-on-error", JOB_HEAD,
     JOB_HEAD + "\n    continue-on-error: true", "job 带了"),
    ("没有 pull_request 触发器", TRIGGERS, "on:\n  push:\n    branches: [main]", "pull_request"),
    ("删掉整个闸门步骤", STEP_NAME + "\n" + GATE_LINE + "\n", "", "没有任何规范闸门步骤在跑它"),
])
def test_a_broken_wiring_is_reported(label, old, new, expected):
    """每一种把接线改坏的方式,都必须**报出问题**。"""
    assert old in REAL_TEXT, "锚点没命中:%s(workflow 改了写法?先改这里)" % label
    mutated = REAL_TEXT.replace(old, new)
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert problems, "这一种改坏方式**没有**被抓:%s" % label
    assert any(expected in problem for problem in problems), (
        "抓到了,但不是预期的理由(%s):%s" % (expected, problems))


# --------------------------------------------------------------------------
# 四之二、接线自检**自己**的判定点:每一条都有**专属**的见证用例
#
# **为什么要单独写这四条**(而不是复用上面那个参数化用例):
# 纪律守卫的变异脚手架靠 **nodeid** 钉见证用例,而 pytest 的失败摘要行会把
# **非 ASCII** 的参数 id 转义成 `\uXXXX` ⇒ 参数化用例的 nodeid 在摘要里对不上
# (实测:参数 id 里的中文在摘要行里被转义成 `\uXXXX`)。
# ⇒ 需要被**单独钉**的判定点,必须有一条 **ASCII 名字、不参数化**的用例。
# --------------------------------------------------------------------------
def test_the_checker_reports_a_hidden_gate_call():
    """把闸门藏进 `bash -c` ⇒ 自检必须报「认不出来」(fail-closed)。"""
    mutated = REAL_TEXT.replace(GATE_LINE, HIDDEN["藏进 bash -c"])
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("认不出来" in problem for problem in problems), (
        "藏起来的闸门没有被报出来:%s" % problems)


def test_the_checker_reports_a_missing_roster_suite():
    """删掉整个闸门步骤 ⇒ 自检必须报「名单里的套件没人跑」(codex 的 C1)。"""
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE + "\n", "")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("没有任何规范闸门步骤在跑它" in problem for problem in problems), (
        "名单覆盖没有被报出来:%s" % problems)


def test_the_checker_reports_a_second_command_in_a_gate_step():
    """闸门步骤里多一条命令 ⇒ 必须报「只有一条命令」。"""
    mutated = REAL_TEXT.replace(GATE_LINE, GATE_LINE + " || true")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("只有一条命令" in problem for problem in problems), (
        "多出来的命令没有被报出来:%s" % problems)


def test_the_checker_reports_a_bypass_key_on_a_gate_step():
    """闸门步骤带 `if:` ⇒ 必须报出来(它可以让这一步不出力)。"""
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                STEP_NAME + '\n        if: ""\n' + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("`if:`" in problem for problem in problems), (
        "旁路键没有被报出来:%s" % problems)


def test_an_harmless_edit_keeps_the_workflow_compliant():
    """**无害改动必须照样合规**——证明自检不是"一律报红"(空转的见证不算见证)。"""
    mutated = REAL_TEXT.replace(
        "python tools/ci_gate.py --junitxml \"$RUNNER_TEMP/boundary.xml\"",
        "python3 tools/ci_gate.py --junitxml \"$RUNNER_TEMP/boundary.xml\"")
    assert mutated != REAL_TEXT
    assert _problems(_write_workflow(Path(_tmp_dir()), mutated)) == []


# --------------------------------------------------------------------------
# 四之三、第 55 轮**复核时**实测出来的洞:守卫的**自检调用**不许带参数
#
# **这个洞的形状**:守卫(`tools/check_ci_wiring.py`)自己也是被写在 workflow 里的。
# 此前它自己的调用**可以带参数** ⇒ 而 `--workflow` 是**替换**语义
# ⇒ 把调用改成 `python tools/check_ci_wiring.py --workflow <别处文件>`,
# 被检查的就只剩那个文件 ⇒ **真实的两个 workflow 压根没被查**,
# 而工具**照样报「[OK] CI 接线合规」**。
#
# 也就是说:**守卫可以被自己一键关掉,而没有任何用例会红**——
# 这正是仓库反复吃过的亏:「写下了但没人跑」/「守卫自己悄悄没了」。
#
# 修法(两道,互相独立):
#   ① `check_wiring_presence` 要求那处调用**一个选项都不许带**;
#   ② `main()` 的 `--workflow` 改成**追加**语义 ⇒ 命令行缩小不了检查范围。
# --------------------------------------------------------------------------
def test_the_wiring_check_is_called_without_arguments():
    """两个 workflow 里跑**各自那个守卫**的调用**必须不带任何选项**。

    **这一条是变异见证用例**:调用被加上参数 ⇒ 它必红。
    """
    seen = 0
    for workflow, script in ((RESEARCH_WORKFLOW, WIRING_SCRIPT),
                             (CONSTITUTION_WORKFLOW, CONSTITUTION_SCRIPT)):
        for entry in wiring._steps_of(wiring._load_yaml(workflow)):
            for command in wiring.logical_commands(entry["run"]):
                tokens = wiring._tokens(command)
                running = tokens and wiring.script_of(tokens) == script
                if not running:
                    continue
                seen += 1
                assert wiring.canonical_wiring(tokens, script), (
                    "%s 里守卫的调用带了参数:%s"
                    "⇒ 守卫的检查范围可以被缩小(实测:加 `--workflow <别处文件>` ⇒ "
                    "真实的 workflow 压根没被查,工具却照样报「[OK]」)"
                    % (workflow.name, command))
    assert seen >= 2, "两个 workflow 各自的落点调用都没找到 ⇒ 这条断言会空转"


def test_the_checker_reports_a_wiring_call_with_arguments():
    """自检调用被加上 `--workflow` ⇒ 必须报出来(不许悄悄缩小守卫的检查范围)。"""
    mutated = REAL_TEXT.replace(
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
        "        run: |\n"
        "          python tools/check_ci_wiring.py",
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
        "        run: |\n"
        "          python tools/check_ci_wiring.py --workflow .github/workflows/decoy.yml")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("不是规范形" in problem for problem in problems), (
        "自检调用带了参数却没有被报出来:%s" % problems)


def test_the_checker_reports_a_path_filtered_pull_request_trigger():
    """`pull_request` 带 `paths:` 过滤 ⇒ 必须报出来(否则闸门压根不会跑)。"""
    mutated = REAL_TEXT.replace(
        "on:\n  push:\n    branches: [main]\n  pull_request:",
        "on:\n  push:\n    branches: [main]\n  pull_request:\n    paths: ['docs/**']")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(Path(_tmp_dir()), mutated))
    assert any("paths" in problem for problem in problems), (
        "被过滤的触发器没有被报出来:%s" % problems)


def test_the_workflow_option_cannot_narrow_the_checked_files(tmp_path, monkeypatch, capsys):
    """`--workflow` 是**追加**语义 ⇒ 命令行缩小不了守卫的检查范围。

    **为什么**:曾经 `--workflow` 是**替换**语义 ⇒ 加一个参数就能让守卫空转
    (实测:真实的 workflow 被改坏,而工具报「[OK]」)。

    **这条是行为测试,不是断言源码**(第 55 轮之五,codex 第 2 轮的 M2):
    断言源码字符串太脆——改个写法、换个变量名就误红,而它**测的是**「守卫能不能
    被自己关掉」这件事,那就该**跑一遍**看结论。

    **做法**:把工具认的那两个 workflow 换成临时文件(一个合规、一个**坏**),
    再额外给一个 `--workflow` ⇒ 那个额外的**不能**把「坏的那个」挤出检查范围
    ⇒ 坏的那个**仍然**要被报出来。
    """
    good = tmp_path / "good.yml"
    good.write_text(REAL_TEXT, encoding="utf-8")
    # 坏的那个:去掉 pull_request 触发器 ⇒ PR 上的改动根本不经这道关卡。
    bad_text = REAL_TEXT.replace(
        "on:\n  push:\n    branches: [main]\n  pull_request:",
        "on:\n  push:\n    branches: [main]\n")
    assert bad_text != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    bad = tmp_path / "bad.yml"
    bad.write_text(bad_text, encoding="utf-8")
    extra = tmp_path / "extra.yml"
    extra.write_text(REAL_TEXT, encoding="utf-8")

    monkeypatch.setattr(wiring, "RESEARCH_WORKFLOW", good)
    monkeypatch.setattr(wiring, "CONSTITUTION_WORKFLOW", bad)
    rc = wiring.main(["--workflow", str(extra)])
    out = capsys.readouterr().out
    assert rc == 1, "给了 --workflow 之后工具居然报绿了:\n%s" % out
    assert "没有 pull_request 触发器" in out, (
        "给了 --workflow 之后,**坏的那个默认 workflow 竟然没被查**"
        "⇒ `--workflow` 又变回『替换』语义了 ⇒ 从命令行就能把守卫关掉:\n%s" % out)


def test_the_checker_reports_a_roster_whose_must_run_is_not_an_object(tmp_path):
    """名单结构不对(`must_run` 不是对象)⇒ 必须报出来。

    **为什么**:覆盖判定是 `名单点名的套件 − 覆盖到的套件`;
    `must_run` 不是对象 ⇒ 名单点名的套件变成**空集** ⇒ 覆盖判定**静默失效**
    (什么都被「覆盖」了)。这正是仓库吃过的亏:「判不了 ⇒ 不放行」。
    """
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": []}), encoding="utf-8")
    problems = wiring.check_gate_wiring(RESEARCH_WORKFLOW, roster)
    assert any("must_run" in problem for problem in problems), (
        "名单结构不对没有被报出来:%s" % problems)


def test_the_checker_reports_a_roster_outside_the_repository(tmp_path):
    """名单在仓库之外 ⇒ 必须报出来(而不是崩在 `relative_to` 上)。

    **为什么**:未捕获的异常退出码**也是 1**,而仓库的规矩是
    「崩掉也要给结论行」——崩了与「判为不合规」在调用者眼里分不开。
    """
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": {}}), encoding="utf-8")
    problems = wiring.check_gate_wiring(RESEARCH_WORKFLOW, roster)
    assert any("不在仓库里" in problem for problem in problems), (
        "仓库之外的名单没有被报出来:%s" % problems)


_TMP_DIRS: list = []


def _tmp_dir() -> str:
    """探针用的临时目录(模块级,避免每个用例各建一个)。"""
    import tempfile

    if not _TMP_DIRS:
        _TMP_DIRS.append(tempfile.mkdtemp(prefix="ci_wiring_probe_"))
    return _TMP_DIRS[0]


# --------------------------------------------------------------------------
# 五、金丝雀:闸门判红 ⇒ 退出码非 0
# --------------------------------------------------------------------------
def _run_gate(cwd: Path, report: Path, roster: Path, suite: str) -> int:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(CI_GATE), "--junitxml", report.name,
         "--roster", roster.name, "--suite", suite],
        cwd=str(cwd), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode


def _write_probe_suite(directory: Path) -> Path:
    suite = directory / "test_probe_suite.py"
    suite.write_text(
        "def test_a_passes():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    return suite


def test_the_gate_passes_when_the_named_case_actually_ran(tmp_path):
    """正常情形:名单点名的用例真的跑了 ⇒ 退出码 0。"""
    _write_probe_suite(tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({
        "must_run": {"test_probe_suite.py": ["test_a_passes"]},
        "allow_skipped": {},
    }), encoding="utf-8")
    assert _run_gate(tmp_path, tmp_path / "r.xml", roster, "test_probe_suite.py") == 0


def test_the_gate_turns_red_when_the_named_case_did_not_run(tmp_path):
    """**金丝雀**:名单点名的用例压根没跑 ⇒ 闸门必须让这一步红。

    这一条是 codex 建议的「canary」的落地:如果哪天闸门坏了(判据放松),
    这一条会先红——因为它断言的是「闸门**会**判红」这件事本身。
    """
    _write_probe_suite(tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({
        "must_run": {"test_probe_suite.py": ["test_that_never_ran"]},
        "allow_skipped": {},
    }), encoding="utf-8")
    assert _run_gate(tmp_path, tmp_path / "r.xml", roster, "test_probe_suite.py") != 0


def test_the_gate_turns_red_when_the_whole_suite_was_skipped(tmp_path):
    """整份被跳过(退出码仍是 0)⇒ 闸门必须判红。"""
    suite = tmp_path / "test_probe_suite.py"
    suite.write_text(
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.skipif(True, reason=\"probe\")\n"
        "\n"
        "def test_a_passes():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({
        "must_run": {"test_probe_suite.py": ["test_a_passes"]},
        "allow_skipped": {},
    }), encoding="utf-8")
    assert _run_gate(tmp_path, tmp_path / "r.xml", roster, "test_probe_suite.py") != 0


# --------------------------------------------------------------------------
# 四、第 55 轮之四:宪法层这个**独立落点**(纯标准库,不装任何东西)
# --------------------------------------------------------------------------
# **为什么要有一个独立落点**:接线自检放在它所守的步骤里 ⇒ 那一步被删,守卫一起消失。
# 研究层那一处删掉时,必须**还有别的地方**会发现 ⇒ 宪法 workflow 那个落点。
#
# **为什么宪法层不做成「再跑一次接线自检」**:接线自检要读 workflow 的 YAML ⇒
# 需要 PyYAML ⇒ 宪法层就得 `pip install` ⇒ 违反「宪法层是纯标准库 + git」
# (仓库里 `tests/test_constitution.py` 就钉着这一条)⇒
# 于是宪法层用**纯文本**做同一件事:认出「研究 workflow 里还有一处规范形的
# `python tools/check_ci_wiring.py`」。
def test_the_constitution_layer_accepts_the_real_repository():
    """放行侧:仓库原样**不许**被宪法层报红(否则它变成「一律报红」= 没判据)。"""
    problems = constitution.check_ci_wiring_presence(ROOT)
    assert problems == [], "宪法层把合规的仓库报成了有问题:\n  - %s" % "\n  - ".join(problems)


def test_the_constitution_layer_reports_a_missing_wiring_call(tmp_path):
    """拦截侧:研究 workflow 里的接线自检被删 ⇒ 宪法层必须报出来。

    **为什么**:研究层那一处被删时,只有这个独立落点能发现。
    """
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 干活\n        run: python -m pytest -q tests/\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert any("check_ci_wiring.py" in problem for problem in problems), (
        "接线自检被删掉却没有被报出来:%s" % problems)


def test_the_constitution_layer_reports_a_narrowed_wiring_call(tmp_path):
    """拦截侧:接线自检被加了 `--workflow` ⇒ 宪法层必须报「不是规范形」。

    **为什么**:第 55 轮**实测**——`--workflow` 曾是**替换**语义 ⇒
    真实的 workflow 压根没被查,而工具**照样报「[OK]」**。
    """
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 接线自检\n        run: |\n"
        "          python tools/check_ci_wiring.py --workflow .github/workflows/decoy.yml\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert any("不是规范形" in problem for problem in problems), (
        "被收窄的守卫调用没有被报出来:%s" % problems)


def test_the_constitution_layer_reports_a_hidden_wiring_call(tmp_path):
    """拦截侧:接线自检被藏进 `bash -c` ⇒ 宪法层必须报「不是规范形」。"""
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 接线自检\n        run: bash -c \"python tools/check_ci_wiring.py\"\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert any("不是规范形" in problem for problem in problems), (
        "藏起来的守卫调用没有被报成「不是规范形」:%s" % problems)


def test_the_wiring_layer_requires_the_constitution_guard_to_run(tmp_path):
    """拦截侧:宪法 workflow 里的守卫调用被删 ⇒ 研究层必须报出来。

    **为什么**:宪法层那个独立落点,自己不会发现「自己被删了」——
    被删的那一刻它就不存在了 ⇒ 只有研究层(在**另一个** workflow 里)能发现。
    """
    original = CONSTITUTION_WORKFLOW.read_text(encoding="utf-8")
    mutated = original.replace(
        "        run: python tools/check_constitution_history.py\n", "")
    assert mutated != original, "锚点没命中:宪法 workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(
        _write_workflow(tmp_path, mutated), CONSTITUTION_SCRIPT)
    assert any(CONSTITUTION_SCRIPT in problem for problem in problems), (
        "宪法层的守卫被删掉却没有被报出来:%s" % problems)


def test_the_wiring_layer_fails_closed_when_the_constitution_duty_is_gone(tmp_path):
    """拦截侧:宪法层那个工具里的**职责**被删 ⇒ 研究层必须 fail-closed。

    **为什么要有这一条**:那个职责被删掉时,宪法 workflow 照样绿
    (它只是少做了一件事)⇒ 而研究层若只看「宪法 workflow 里有一处规范形的调用」,
    **看不出来**它已经不做事了 ⇒ 独立落点变成空壳。
    """
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir(parents=True)
    (tools_dir / "check_constitution_history.py").write_text(
        "def some_other_check(root):\n    return []\n", encoding="utf-8")
    problems = wiring.check_constitution_duty(tmp_path)
    assert problems, "宪法层的职责被删掉时必须 fail-closed 报问题"
    assert any("check_ci_wiring_presence" in problem for problem in problems), (
        "报出来的问题没说清是哪个职责没了:%s" % problems)


def test_the_constitution_layer_works_without_any_third_party_package(tmp_path):
    """放行侧:宪法层必须**只靠标准库**就能干活。

    **为什么**:它那个独立落点的**全部价值**就在于「没有理由不跑」——
    纯标准库 + git ⇒ 不装任何东西 ⇒ 不会因为装不上依赖而压根没跑
    (仓库的老教训:守卫因为 conftest 导入交易栈而在 CI 上一次都没跑过,
     却看起来「配好了」)。
    """
    script = tmp_path / "probe.py"
    script.write_text(
        "import importlib.util, pathlib, sys\n"
        "spec = importlib.util.spec_from_file_location('c', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(len(module.check_ci_wiring_presence(pathlib.Path(sys.argv[2]))))\n",
        encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(script),
         str(ROOT / "tools" / "check_constitution_history.py"), str(ROOT)],
        capture_output=True, text=True)
    assert result.returncode == 0, (
        "只靠标准库跑不起来(说明它偷偷依赖了第三方包):%s" % result.stderr[-500:])
    assert result.stdout.strip() == "0", (
        "只靠标准库跑出来的结论不是「没问题」:%s" % result.stdout)


# --------------------------------------------------------------------------
# 六、第 55 轮之五:codex 第 2 轮的 C1 / C2 / H1 / H2 / M1 / L2 / L3 / L4
# --------------------------------------------------------------------------
# 这一节的每一条规则都有**放行侧**与**拦截侧**两枚探针。
#
# **为什么必须两枚**:仓库的教训是「静态推理必漏,每条规则都配双向探针」
# (记忆:static-ban-rules-need-dynamic-probes)。只测拦截侧 ⇒ 规则可能一直
# 误伤合法写法;只测放行侧 ⇒ 规则根本没生效也看不出来。


# --- 放行侧:解释器选项不该被误伤(codex 第 2 轮的 M1) ---
@pytest.mark.parametrize("command", [
    pytest.param(
        'python -X utf8 tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json --suite tests/test_a.py",
        id="utf8"),
    pytest.param(
        'python -W error tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json --suite tests/test_a.py",
        id="werror"),
    pytest.param(
        'py -3 tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json --suite tests/test_a.py",
        id="py3"),
    pytest.param(
        'python -u tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json --suite tests/test_a.py",
        id="unbuffered"),
    pytest.param(
        'python3.12 -X utf8 tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json --suite tests/test_a.py",
        id="py312_utf8"),
])
def test_the_canonical_form_allows_interpreter_flags(command):
    """解释器选项(`-X utf8` / `-W error` / `py -3` / `-u`)**算规范形**。

    **为什么**(codex 第 2 轮的 M1):此前一律判红 ⇒ **误伤合法写法**;
    而 ADR 的明文写着「闸门命令允许 `-W/-X` 等取值选项」⇒ 代码与文档矛盾。
    """
    assert wiring.canonical_gate(shlex.split(command)) is not None, command


def test_the_canonical_form_rejects_the_dash_c_and_dash_m_forms():
    """`-c` / `-m` **必须**判红:它们把脚本整个换掉 ⇒ 闸门压根没跑。

    **为什么**:`python -c "import os; os._exit(0)"` ⇒ 进程立刻退出 0
    ⇒ CI 看着绿,而**一条用例都没跑**。这不是「解释器选项」,是**替换脚本**。
    """
    for command in (
        'python -c "import os; os._exit(0)" tools/ci_gate.py'
        ' --junitxml "$RUNNER_TEMP/x.xml" --roster tests/guard_roster.json'
        " --suite tests/test_a.py",
        "python -m pytest -q tests/test_a.py",
    ):
        assert wiring.canonical_gate(shlex.split(command)) is None, command


def test_a_trailing_shell_comment_does_not_break_the_canonical_form(tmp_path):
    """块标量里行尾的 shell 注释**不改运行结果** ⇒ 不许判红。

    **为什么**:块标量(`run: |`)里写注释是常规写法。此前按空白切分
    ⇒ 注释那截变成一个「认不出来的命令」⇒ 误伤。
    """
    mutated = REAL_TEXT.replace(
        GATE_LINE,
        "        run: |\n"
        '          python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
        " --roster tests/guard_roster.json"
        " --suite tests/test_research_boundary.py  # 这条闸门不许带旁路")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert problems == [], "带注释的规范形被误伤了:\n  - %s" % "\n  - ".join(problems)


# --- 放行侧:非字典的 `on:` 是合法写法(codex 第 2 轮的 H1) ---
@pytest.mark.parametrize("on_block", [
    "on: [push, pull_request]",
    "on: pull_request",
])
def test_a_list_or_scalar_trigger_is_accepted(tmp_path, on_block):
    """`on: [push, pull_request]` / `on: pull_request` 是**合法**的 GitHub 写法。

    **为什么**(codex 第 2 轮的 H1):此前 `_triggers()` 只认字典 ⇒ 这两种
    写法一律返回 {} ⇒ 被判红「没有 pull_request 触发器」⇒ **误伤合法配置**。
    """
    mutated = REAL_TEXT.replace(TRIGGERS, on_block)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert problems == [], "合法的 `on:` 写法被误伤了:\n  - %s" % "\n  - ".join(problems)


# --- 拦截侧:C1(`needs:`)与 C2(`env:`) ---
def test_the_checker_reports_a_gate_step_with_an_env_block(tmp_path):
    """闸门步骤带 `env:` ⇒ 必须报出来。

    **为什么**(codex 第 2 轮的 C2):`env: PATH: …/evil:$PATH` + 仓库里一个
    提交的 `evil/python` ⇒ **解释器被换掉** ⇒ 闸门压根没跑,CI 照样绿。
    """
    mutated = REAL_TEXT.replace(
        STEP_NAME + "\n" + GATE_LINE,
        STEP_NAME + "\n        env:\n          PATH: /tmp/evil:$PATH\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("env:" in problem for problem in problems), (
        "闸门步骤上的 env: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_gate_job_with_an_env_block(tmp_path):
    """闸门**所在 job** 带 `env:` ⇒ 必须报出来(与步骤级同一条洞)。

    **断言钉的是「哪条规则」**:这个 job 里**同时**还有受保护脚本那一步
    ⇒ 「受保护脚本」那条规则也会报 job 级的 `env:`
    ⇒ 若只断言「有没有报 env:」,把**闸门**那条规则删掉也不会红
    ⇒ 见证会**空转**(仓库规矩:断言必须说清是**哪条**规则让它红)。
    """
    mutated = REAL_TEXT.replace(
        JOB_HEAD, JOB_HEAD + "\n    env:\n      PATH: /tmp/evil:$PATH")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("闸门步骤" in problem and "job 带了 `env:`" in problem
               for problem in problems), (
        "闸门 job 上的 env: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_gate_job_with_needs(tmp_path):
    """闸门**所在 job** 带 `needs:` ⇒ 必须报出来。

    **为什么**(codex 第 2 轮的 C1):`needs:` 指向一个**恒被跳过**的 job
    ⇒ GitHub **跳过**这个 job ⇒ 闸门压根没跑 ⇒ 而工具照样报绿。

    **断言钉的是「哪条规则」**(同 `env:` 那条):这个 job 里还有受保护脚本那一步
    ⇒ 「受保护脚本」那条规则也会报 job 级的 `needs:`
    ⇒ 只断言「有没有报 needs:」⇒ 删掉**闸门**那条规则也不会红 ⇒ 见证空转。
    """
    mutated = REAL_TEXT.replace(JOB_HEAD, JOB_HEAD + "\n    needs: dummy")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("闸门步骤" in problem and "job 带了 `needs:`" in problem
               for problem in problems), (
        "闸门 job 上的 needs: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_wiring_job_with_needs(tmp_path):
    """接线自检那个 job 带 `needs:` ⇒ 必须报出来(守卫自己被跳过)。"""
    mutated = REAL_TEXT.replace(
        "  wiring:\n    runs-on: ubuntu-latest",
        "  wiring:\n    runs-on: ubuntu-latest\n    needs: dummy")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("needs:" in problem for problem in problems), (
        "守卫 job 上的 needs: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_wiring_job_with_an_env_block(tmp_path):
    """接线自检那个 **job** 带 `env:` ⇒ 必须报出来(解释器可被换掉)。"""
    mutated = REAL_TEXT.replace(
        "  wiring:\n    runs-on: ubuntu-latest",
        "  wiring:\n    runs-on: ubuntu-latest\n    env:\n      PATH: /tmp/evil:$PATH")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("env:" in problem for problem in problems), (
        "守卫 job 上的 env: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_wiring_step_with_an_env_block(tmp_path):
    """接线自检那一步带 `env:` ⇒ 必须报出来(解释器可被换掉)。"""
    mutated = REAL_TEXT.replace(
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
        "        run: |\n",
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
        "        env:\n          PATH: /tmp/evil:$PATH\n"
        "        run: |\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("env:" in problem for problem in problems), (
        "守卫步骤上的 env: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_job_level_default_shell(tmp_path):
    """job 级 `defaults.run.shell` ⇒ 必须报出来。

    **为什么**:此前只查 workflow 级 ⇒ job 级照样能把执行语义换掉。
    """
    mutated = REAL_TEXT.replace(
        JOB_HEAD,
        JOB_HEAD + "\n    defaults:\n      run:\n        shell: bash {0}")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("defaults.run.shell" in problem for problem in problems), (
        "job 级 defaults.run.shell 没有被报出来:%s" % problems)


# --- 拦截侧:`types:` 过滤(codex 第 2 轮的 H1) ---
def test_the_checker_reports_a_types_filtered_pull_request(tmp_path):
    """`pull_request` 带 `types:` 过滤 ⇒ 必须报出来。

    **为什么**:`types: [opened, closed]` ⇒ PR 的后续 push **不触发**
    ⇒ PR 上的改动**根本不经**这道关卡。
    """
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "    types: [opened, closed]")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("types" in problem for problem in problems), (
        "被 types: 过滤的触发器没有被报出来:%s" % problems)


# --- 放行侧 + 拦截侧:共享的名单结构校验(codex 第 2 轮的 H2) ---
def test_the_shared_roster_validator_accepts_the_real_roster():
    """放行侧:仓库里那份真名单必须**没有**结构问题。

    **为什么**:共享校验是新加的 ⇒ 判太严会把真名单也判红 ⇒ 闸门永久变红。
    """
    data = json.loads(ROSTER.read_text(encoding="utf-8"))
    assert ci_ran.validate_roster(data) == [], (
        "真名单被判成结构不对:%s" % ci_ran.validate_roster(data))


@pytest.mark.parametrize("bad_roster,expected", [
    ({"must_run": {}}, "must_run"),
    ({"must_run": []}, "must_run"),
    ({"must_run": {}, "allow_skipped": []}, "allow_skipped"),
    ({"must_run": {"tests/test_a.py": "not_a_list"}}, "字符串列表"),
    ({"must_run": {}, "allow_skipped": {"tests/test_a.py": ["x"]}}, "空转"),
    ({"must_run": {"tests/test_a.py": ["x"]},
      "allow_skipped": {"tests/test_a.py": ["x"]}}, "自相矛盾"),
    ([], "顶层"),
])
def test_the_shared_roster_validator_rejects_bad_rosters(bad_roster, expected):
    """拦截侧:七种坏名单形态必须**全部**被共享校验报出来。

    **为什么**(codex 第 2 轮的 H2):此前接线自检只校验「`must_run` 是对象」
    ⇒ 七种坏形态里只判红两种 ⇒ 接线自检报「[OK] CI 接线合规」而闸门报红
    ⇒ 前者是**误导性的绿**。
    """
    problems = ci_ran.validate_roster(bad_roster)
    assert problems, "坏名单没有被报出来:%s" % (bad_roster,)
    assert any(expected in problem for problem in problems), (
        "报出来的问题没说清是哪儿不对(期望提到 %r):%s" % (expected, problems))


@pytest.mark.parametrize("bad_roster", [
    pytest.param({"must_run": {}}, id="empty_must_run"),
    pytest.param({"must_run": []}, id="must_run_not_object"),
    pytest.param({"must_run": {}, "allow_skipped": []}, id="allow_skipped_not_object"),
    pytest.param({"must_run": {"tests/test_a.py": "not_a_list"}}, id="entry_not_string_list"),
    pytest.param({"must_run": {}, "allow_skipped": {"tests/test_a.py": ["x"]}},
                 id="dangling_allow_skipped"),
    pytest.param({"must_run": {"tests/test_a.py": ["x"]},
                  "allow_skipped": {"tests/test_a.py": ["x"]}}, id="overlap"),
    pytest.param([], id="top_level_list"),
])
def test_the_wiring_layer_rejects_every_bad_roster(tmp_path, bad_roster):
    """接线自检层与闸门层**判法一致**(共享同一份 `validate_roster`)。

    **断言的是「哪条规则拦的」**,不是「有没有报错」——否则「名单不在仓库里」
    这条无关规则会把断言喂饱(空转的见证不算见证)。
    """
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(bad_roster), encoding="utf-8")
    problems = wiring.check_gate_wiring(RESEARCH_WORKFLOW, roster)
    assert problems, "坏名单没有被接线自检报出来:%s" % (bad_roster,)
    assert any(("must_run" in problem or "allow_skipped" in problem
                 or "顶层" in problem) for problem in problems), (
        "接线自检报的问题不是名单结构那条(口径与闸门不一致):%s" % problems)


@pytest.mark.parametrize("bad_roster", [
    pytest.param({"must_run": {}}, id="empty_must_run"),
    pytest.param({"must_run": []}, id="must_run_not_object"),
    pytest.param([], id="top_level_list"),
])
def test_the_gate_exits_with_two_when_the_roster_is_invalid(tmp_path, bad_roster):
    """名单结构不对 ⇒ 闸门**退出码 2**(压根不跑 pytest),不许降级成放行。

    **为什么**(codex 第 2 轮的 H2):`must_run: {}` 时,闸门此前把每份套件都
    当成「不在名单里」⇒ 自动加 `--allow-unlisted-suite`
    ⇒ **逐条点名核对整个消失** ⇒ 闸门照样打印「[OK] 闸门通过」。
    """
    _write_probe_suite(tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps(bad_roster), encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(CI_GATE), "--junitxml", "r.xml",
         "--roster", "roster.json", "--suite", "test_probe_suite.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert proc.returncode == 2, (
        "名单坏掉时闸门的退出码不是 2(而是 %d):%s"
        % (proc.returncode, (proc.stdout or "")[-800:]))
    assert "名单结构不对" in (proc.stdout or ""), (
        "名单坏掉时没有给出结论行:%s" % (proc.stdout or "")[-800:])


# --- 拦截侧:L3(闸门只许在 research-guard.yml 里) ---
def test_the_checker_reports_a_gate_step_in_another_workflow(tmp_path):
    """闸门步骤只许出现在 `research-guard.yml`。

    **为什么**(codex 第 2 轮的 L3):并集口径下,把闸门挪到
    `constitution-guard.yml` ⇒ 并集仍覆盖 ⇒ 工具报绿;但那个 workflow
    **没有研究侧依赖** ⇒ 闸门压根跑不起来 ⇒ **CI 看着绿、什么也没跑**。
    """
    original = CONSTITUTION_WORKFLOW.read_text(encoding="utf-8")
    mutated = original.replace(
        "        run: python tools/check_constitution_history.py",
        '        run: python tools/ci_gate.py --junitxml "$RUNNER_TEMP/x.xml"'
        " --roster tests/guard_roster.json"
        " --suite tests/test_research_boundary.py")
    assert mutated != original, "锚点没命中:宪法 workflow 改了写法?先改这里"
    problems = wiring.check_workflows(
        [RESEARCH_WORKFLOW, _write_workflow(tmp_path, mutated)], ROSTER)
    assert any("不许" in problem and "闸门步骤" in problem for problem in problems), (
        "别的 workflow 里的闸门步骤没有被报出来:%s" % problems)


# --- 拦截侧:L4(`run:` 不是字符串 ⇒ 报出来,不许崩) ---
def test_the_checker_reports_a_non_string_run_without_crashing(tmp_path):
    """`run:` 写成列表 ⇒ 必须报出来,**不许崩**(崩了没有结论行)。

    **为什么**(codex 第 2 轮的 L4):此前异常直接逃出 `main()` ⇒
    未捕获的异常退出码**也是 1**,而仓库的规矩是「崩掉也要给结论行」——
    崩了与「判为不合规」在调用者眼里分不开。
    """
    mutated = REAL_TEXT.replace(
        GATE_LINE,
        "        run:\n"
        "          - python tools/ci_gate.py\n"
        "          - --junitxml x\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("run:" in problem for problem in problems), (
        "非字符串的 run: 没有被报出来:%s" % problems)


# --- 拦截侧:L2(同一条问题不许报两遍) ---
def test_the_same_problem_is_not_reported_twice(tmp_path, capsys):
    """同一条问题不许报两遍(名单被每个 workflow 各解析一次 ⇒ 报两遍)。"""
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": []}), encoding="utf-8")
    assert wiring.main(["--roster", str(roster)]) == 1
    out = capsys.readouterr().out
    lines = [line.strip() for line in out.splitlines()
             if line.strip().startswith("- ")]
    assert lines, "工具没有给出结论行:\n%s" % out
    assert len(lines) == len(set(lines)), (
        "同一条问题报了两遍:\n%s" % out)


def test_the_dedupe_keeps_one_copy_of_each_problem():
    """去重本身也要有人盯(它是**兜底**层,端到端场景里已经不会有重复源了)。"""
    problems = ["b", "a", "b", "c", "a"]
    assert wiring.unique_problems(problems) == ["b", "a", "c"], (
        "去重坏了:同一条问题保留了两份,或顺序被改乱")


# --- 放行侧:宪法层与研究层的规范形口径必须一致 ---
def test_the_constitution_layer_accepts_the_widened_canonical_form(tmp_path):
    """宪法层也必须接受 `-X utf8` 这样的写法(两层口径不许分叉)。

    **为什么**:宪法层是**纯文本**核对(不读 YAML)⇒ 它对「规范形」的判断
    是自己写了一遍 ⇒ 两层一旦分叉,就会出现「研究层放行、宪法层判红」
    ⇒ CI 永久变红,或者反过来(宪法层放行、研究层判红)。
    """
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 接线自检\n        run: |\n"
        "          python -X utf8 tools/check_ci_wiring.py\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert problems == [], "宪法层把合法写法判红了:\n  - %s" % "\n  - ".join(problems)


# ==========================================================================
# 第 55 轮之六:claude 第 3 轮(C-1 / H-2 / M-2 / L-1 / L-2)
#  ＋ codex 第 3 轮(H-1 尾部 `&` / H-2 受保护脚本 / M-1 未知顶层键
#                     / M-2 `types:` 判效果 / M-3 相对路径归一化)
#
# **每一节都是双向的**:放行侧不许误伤,拦截侧必须抓住。
# 断言写的是「**哪条规则**拦的」,不是「有没有报错」——
# 否则一条无关规则就能把断言喂饱(空转的见证不算见证)。
# ==========================================================================

# --- C-1:workflow 级 `env:`(claude 第 3 轮 / codex 第 3 轮的 C1) ---
def test_the_checker_reports_a_workflow_level_env_block(tmp_path):
    """**workflow 级** `env:` ⇒ 必须报出来。

    **为什么**(claude 第 3 轮的 C-1 / codex 第 3 轮的 C1):
    此前只禁了**步骤级**与**job 级** `env:` ⇒ **workflow 级**是个洞。
    而 workflow 级 `env:` 恰恰是最狠的那一级:它被**所有** job、所有 step 继承
    ⇒ 提交一个 `exit 0` 的 `python` 放在 PATH 前面
    ⇒ 闸门**和**接线自检**双双**被换掉 ⇒ CI 全绿,而什么也没跑。
    """
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "env:\n  PATH: /tmp/evil:$PATH\n" + TRIGGERS)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("workflow 级" in problem and "env:" in problem
               for problem in problems), (
        "workflow 级 env: 没有被报出来:%s" % problems)


def test_the_wiring_presence_check_reports_a_workflow_level_env_block(tmp_path):
    """接线自检的**存在性**检查也要看 workflow 级 `env:`。

    **为什么**:守卫自己跑在同一个 workflow 里 ⇒ workflow 级 `env:`
    同样能把**守卫的解释器**换掉 ⇒ 只在形状检查里报不够。
    """
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "env:\n  PATH: /tmp/evil:$PATH\n" + TRIGGERS)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("workflow 级" in problem and "env:" in problem
               for problem in problems), (
        "workflow 级 env: 没有被报出来:%s" % problems)


# --- H-2:`working-directory`(claude 第 3 轮) ---
def test_the_checker_reports_a_step_level_working_directory(tmp_path):
    """步骤级 `working-directory:` ⇒ 必须报出来。

    **为什么**:规范形钉死的是**相对**脚本词 `tools/ci_gate.py`
    ⇒ 它按**这一步的 cwd** 解析 ⇒ cwd 可控 ⇒ 规范形形同虚设:
    提交 `sub/tools/ci_gate.py`(打印「[OK]」后 `exit 0`)就能完全绕过。
    """
    mutated = REAL_TEXT.replace(STEP_NAME, STEP_NAME + "\n        working-directory: sub")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("working-directory" in problem for problem in problems), (
        "步骤级 working-directory: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_job_level_working_directory(tmp_path):
    """job 级 `defaults.run.working-directory` ⇒ 必须报出来(与步骤级同一条洞)。"""
    mutated = REAL_TEXT.replace(
        JOB_HEAD,
        JOB_HEAD + "\n    defaults:\n      run:\n        working-directory: decoy")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("working-directory" in problem for problem in problems), (
        "job 级 defaults.run.working-directory 没有被报出来:%s" % problems)


def test_the_checker_reports_a_workflow_level_working_directory(tmp_path):
    """workflow 级 `defaults.run.working-directory` ⇒ 必须报出来。"""
    mutated = REAL_TEXT.replace(
        "jobs:\n  research:",
        "defaults:\n  run:\n    working-directory: decoy\njobs:\n  research:")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("working-directory" in problem for problem in problems), (
        "workflow 级 defaults.run.working-directory 没有被报出来:%s" % problems)


def test_the_checker_reports_a_wiring_step_with_a_working_directory(tmp_path):
    """接线自检那一步带 `working-directory:` ⇒ 必须报出来。"""
    mutated = REAL_TEXT.replace(
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n",
        "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
        "        working-directory: sub\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("working-directory" in problem for problem in problems), (
        "守卫步骤上的 working-directory: 没有被报出来:%s" % problems)


# --- M-2:别的 workflow 文件里的闸门步骤(claude 第 3 轮) ---
def test_the_checker_reports_a_gate_step_in_a_workflow_that_was_not_passed_in(tmp_path):
    """**没被传进来**的 workflow 文件里有闸门步骤 ⇒ 必须报出来。

    **为什么**(claude 第 3 轮的 M-2):`check_workflows` 只看**传进来**的那几个
    (默认两个)⇒ 新增第三个 workflow、或把闸门挪进去
    ⇒ 那个文件**压根不会被检查** ⇒ 闸门在**没有研究侧依赖**的地方跑
    ⇒ 「闸门跑了但没跑成」与「闸门压根没跑」在 CI 看上去一模一样。
    """
    workflows_dir = tmp_path / ".github" / "workflows"
    workflows_dir.mkdir(parents=True)
    (workflows_dir / "research-guard.yml").write_text(REAL_TEXT, encoding="utf-8")
    (workflows_dir / "extra.yml").write_text(
        "name: extra\non:\n  pull_request:\njobs:\n  x:\n"
        "    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: python tools/ci_gate.py --junitxml /tmp/a.xml"
        " --roster tests/guard_roster.json --suite tests/test_ci_wiring.py\n",
        encoding="utf-8")
    problems = wiring.check_no_gate_steps_elsewhere(
        workflows_dir, workflows_dir / "research-guard.yml")
    assert any("不许" in problem for problem in problems), (
        "别的 workflow 文件里的闸门步骤没有被报出来:%s" % problems)


def test_the_other_workflow_files_in_the_repository_have_no_gate_steps():
    """放行侧:仓库里**现存的**别的 workflow 文件里没有闸门步骤。

    **为什么**:新规则要**先**在真仓库上成立,否则就是一条空规则。
    """
    problems = wiring.check_no_gate_steps_elsewhere(
        ROOT / ".github" / "workflows", RESEARCH_WORKFLOW)
    assert problems == [], "仓库里别的 workflow 里有闸门步骤:%s" % problems


# --- L-1:名单里的空条目(claude 第 3 轮) ---
def test_the_shared_roster_validator_rejects_an_empty_suite_entry():
    """名单里某份套件被点名、但**一条用例都没点名** ⇒ 必须报出来。

    **为什么**(claude 第 3 轮的 L-1):`must_run: {"tests/test_a.py": []}`
    ⇒ 这份套件被点名了,却**一条都没点名** ⇒ 逐条核对等于没做
    ⇒ 闸门「跑完 0 条」而照样报「[OK]」。
    """
    problems = ci_ran.validate_roster({"must_run": {"tests/test_a.py": []}})
    assert problems, "空的名单条目没有被报出来"
    assert any("空的" in problem for problem in problems), (
        "报出来的问题没说清是空条目:%s" % problems)


def test_the_gate_exits_with_two_when_a_roster_entry_is_empty(tmp_path):
    """端到端:名单里空条目 ⇒ 闸门**退出码 2**(不是放行)。"""
    _write_probe_suite(tmp_path)
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": {"test_probe_suite.py": []}}),
                       encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    proc = subprocess.run(
        [sys.executable, str(CI_GATE), "--junitxml", "r.xml",
         "--roster", "roster.json", "--suite", "test_probe_suite.py"],
        cwd=str(tmp_path), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    assert proc.returncode == 2, (
        "名单里有空条目时闸门的退出码不是 2(而是 %d):%s"
        % (proc.returncode, (proc.stdout or "")[-800:]))
    assert "名单结构不对" in (proc.stdout or ""), (
        "名单里有空条目时没有给出结论行:%s" % (proc.stdout or "")[-800:])


# --- L-2:连写的取值选项 `-Werror` / `-Xutf8`(claude 第 3 轮) ---
@pytest.mark.parametrize("option", ["-Werror", "-Xutf8"], ids=["Werror", "Xutf8"])
def test_the_canonical_form_allows_the_attached_value_options(option):
    """放行侧:`-Werror` / `-Xutf8` 这种**连写**写法必须被接受。

    **为什么**(claude 第 3 轮的 L-2):CPython **也接受**「选项和值连在一起」
    的写法 ⇒ 此前一律判红 ⇒ **误伤合法写法**(而且与 ADR 里
    「闸门命令允许 -W/-X 等取值选项」的明文直接矛盾)。
    """
    command = ("python %s tools/ci_gate.py --junitxml $RUNNER_TEMP/x.xml"
               " --roster tests/guard_roster.json --suite tests/test_a.py"
               % option)
    gate = wiring.canonical_gate(shlex.split(command))
    assert gate is not None, "连写的取值选项 %s 被误判成非规范形" % option
    assert gate["suites"] == ["tests/test_a.py"], "解析结果不对:%s" % gate


def test_the_canonical_form_still_rejects_the_separate_value_options():
    """放行侧(对照):`-W error` 这种**分开**的写法也必须被接受(不许只修一半)。"""
    command = ("python -W error tools/ci_gate.py --junitxml $RUNNER_TEMP/x.xml"
               " --roster tests/guard_roster.json --suite tests/test_a.py")
    gate = wiring.canonical_gate(shlex.split(command))
    assert gate is not None, "分开的取值选项被误判成非规范形"


# --- H-1:命令尾部的 `&`(codex 第 3 轮) ---
def test_the_checker_reports_a_gate_step_with_a_background_ampersand(tmp_path):
    """闸门命令尾部有 `&` ⇒ 必须报出来。

    **为什么**(codex 第 3 轮的 H1):`cmd &` ⇒ shell 把命令丢到后台、
    **立刻返回 0** ⇒ 闸门判红被丢弃(实测:`bash -c 'false &'` 的退出码是 **0**)
    ⇒ 闸门看着绿,而它压根没跑完。
    (此前 `logical_commands` 把 `cmd &` 切成**一条**命令 ⇒ 「只有一条命令」
    的检查压根没触发。)
    """
    mutated = REAL_TEXT.replace(GATE_LINE, GATE_LINE + " &")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("退出码被丢弃" in problem for problem in problems), (
        "尾部 `&` 没有被报出来:%s" % problems)


def test_the_checker_reports_a_wiring_step_with_a_background_ampersand(tmp_path):
    """接线自检那一步尾部有 `&` ⇒ 必须报出来(守卫自己被丢到后台)。"""
    mutated = REAL_TEXT.replace(
        "        run: |\n          python tools/check_ci_wiring.py",
        "        run: |\n          python tools/check_ci_wiring.py &")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_wiring_presence(_write_workflow(tmp_path, mutated),
                                            WIRING_SCRIPT)
    assert any("退出码被丢弃" in problem for problem in problems), (
        "守卫步骤尾部的 `&` 没有被报出来:%s" % problems)


# --- H-2(codex 第 3 轮):受保护脚本(证据链)也要受同一套纪律 ---
PROTECTED_STEP = ("      - name: 纪律守卫的变异验证(每条守卫松了都必须被抓)\n"
                  "        run: |\n"
                  "          python tools/check_guard_mutations.py --verify")


def test_the_checker_reports_a_protected_step_with_continue_on_error(tmp_path):
    """**受保护脚本**那一步带 `continue-on-error:` ⇒ 必须报出来。

    **为什么**(codex 第 3 轮的 H2):`tools/check_guard_mutations.py --verify`
    这一步的退出码**就是**「75 种变异全部被抓住」的证据。
    若它可以被 `continue-on-error: true` 轻轻放过 ⇒ 那句话变成空话,
    而 CI 照样绿。⇒ 受保护脚本与闸门步骤**同一套**纪律。
    """
    mutated = REAL_TEXT.replace(
        PROTECTED_STEP,
        PROTECTED_STEP.replace("        run: |",
                               "        continue-on-error: true\n        run: |"))
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("受保护脚本" in problem and "continue-on-error" in problem
               for problem in problems), (
        "受保护脚本那一步的 continue-on-error: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_protected_step_with_a_background_ampersand(tmp_path):
    """**受保护脚本**那一步尾部有 `&` ⇒ 必须报出来(退出码被丢弃)。"""
    mutated = REAL_TEXT.replace(
        "          python tools/check_guard_mutations.py --verify",
        "          python tools/check_guard_mutations.py --verify &")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("受保护脚本" in problem and "退出码被丢弃" in problem
               for problem in problems), (
        "受保护脚本那一步尾部的 `&` 没有被报出来:%s" % problems)


def test_the_checker_reports_a_protected_step_with_an_extra_command(tmp_path):
    """**受保护脚本**那一步多出一条命令 ⇒ 必须报出来(插进来的缝)。"""
    mutated = REAL_TEXT.replace(
        "          python tools/check_guard_mutations.py --verify",
        "          python tools/check_guard_mutations.py --verify\n"
        "          echo done")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("受保护脚本" in problem and "只有一条命令" in problem
               for problem in problems), (
        "受保护脚本那一步的多余命令没有被报出来:%s" % problems)


def test_the_repository_protected_steps_are_compliant():
    """放行侧:仓库里那几步受保护脚本必须**本来就合规**。

    **为什么**:新规则要**先**在真仓库上成立,否则就是一条空规则
    (「规则存在但从不变红」= 没有约束力)。
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert not any("受保护脚本" in problem for problem in problems), (
        "仓库自己的受保护脚本被判红了:%s" % problems)


# --- M-1(codex 第 3 轮):名单里有未知的顶层键 ---
def test_the_shared_roster_validator_rejects_an_unknown_top_level_key():
    """名单里有**未知的顶层键** ⇒ 必须报出来。

    **为什么**(codex 第 3 轮的 M1):多写一个键 ⇒ 没人知道它是什么意思
    ⇒ 很可能是**以为**写了就生效(例如把 `must_skip` 写上去,
    以为能跳过某些用例)⇒ 实际什么都没发生 ⇒ 假绿。
    """
    problems = ci_ran.validate_roster({"must_run": {}, "must_skip": ["x"]})
    assert problems, "未知的顶层键没有被报出来"
    assert any("未知的顶层键" in problem for problem in problems), (
        "报出来的问题没说清是未知的顶层键:%s" % problems)


def test_the_shared_roster_validator_accepts_a_documentation_key():
    """放行侧:下划线开头的键当作文档说明(`_说明`)⇒ 必须放行。

    **为什么**:真名单里就有 `_说明` ⇒ 判太严会把真名单判红。
    """
    problems = ci_ran.validate_roster({"_说明": "随便写", "must_run": {}})
    assert not any("未知的顶层键" in problem for problem in problems), (
        "文档说明键被误判了:%s" % problems)


# --- M-2(codex 第 3 轮):`types:` 判「效果」而不是判「键」 ---
def test_a_types_filter_that_keeps_synchronize_is_accepted(tmp_path):
    """放行侧:`types: [opened, reopened, synchronize]` ⇒ 必须放行。

    **为什么**(codex 第 3 轮的 M2):这是 GitHub 的**默认三件套**,
    写成显式列表**完全合法且不削弱**任何东西
    ⇒ 此前「键存在就判红」是**误伤**。
    """
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "    types: [opened, reopened, synchronize]")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("types" in problem for problem in problems), (
        "合法的 types: 被误伤了:%s" % problems)


def test_the_checker_reports_a_types_filter_without_synchronize(tmp_path):
    """拦截侧:`types:` 少了 `synchronize` ⇒ 必须报出来(判**效果**)。"""
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "    types: [opened, closed]")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("types" in problem and "synchronize" in problem
               for problem in problems), (
        "少了 synchronize 的 types: 没有被报出来:%s" % problems)


# --- M-3(codex 第 3 轮):相对脚本路径的归一化 ---
@pytest.mark.parametrize("script", [
    "tools/ci_gate.py",
    "./tools/ci_gate.py",
    "tools/../tools/ci_gate.py",
], ids=["plain", "dot_slash", "dot_dot"])
def test_the_canonical_form_accepts_equivalent_relative_paths(script):
    """放行侧:`./tools/ci_gate.py` 这类**等价**写法必须被接受。

    **为什么**(codex 第 3 轮的 M3):此前按**字面**比较脚本词
    ⇒ `./tools/ci_gate.py` 被判成「非规范形」⇒ 覆盖塌方 ⇒ 整个工具判红
    ⇒ **误伤合法写法**。
    """
    command = ("python %s --junitxml $RUNNER_TEMP/x.xml"
               " --roster tests/guard_roster.json --suite tests/test_a.py" % script)
    gate = wiring.canonical_gate(shlex.split(command))
    assert gate is not None, "等价的相对路径 %s 被误判成非规范形" % script
    assert gate["roster"] == "tests/guard_roster.json", "解析结果不对:%s" % gate


# --- L1(codex 第 3 轮):不存在的套件**不算**「已覆盖」 ---
def test_a_nonexistent_suite_is_not_counted_as_covered(tmp_path):
    """名单点名的套件**文件不存在** ⇒ 它**不算**「已覆盖」。

    **为什么**(codex 第 3 轮的 L1):此前 `covered.add` 在 `if/elif` **之外**
    ⇒ 一个**根本不存在**的文件也被算成「已覆盖」
    ⇒ 覆盖判定被它顶掉 ⇒ 名单里真正没跑的套件反而没人报。
    """
    roster = tmp_path / "roster.json"
    roster.write_text(
        json.dumps({"must_run": {"tests/test_does_not_exist.py": ["x"]}}),
        encoding="utf-8")
    mutated = REAL_TEXT.replace(
        GATE_LINE,
        '        run: python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
        " --roster tests/guard_roster.json"
        " --suite tests/test_does_not_exist.py")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_gate_wiring(_write_workflow(tmp_path, mutated), roster)
    # **断言必须说清是哪条规则报的**(仓库规矩)⇒ 这里钉住的是
    # 「覆盖判定」那条规则:名单点名的套件没有任何**规范闸门步骤**在跑它。
    # 只钉路径会落空(「套件文件不存在」那条报文里也带路径)⇒ 空断言比没断言更糟。
    assert any("test_does_not_exist.py" in problem
               and "没有任何规范闸门步骤在跑它" in problem
               for problem in problems), (
        "不存在的套件被算成了「已覆盖」(覆盖判定那条规则没报):%s" % problems)


def test_the_checker_reports_a_protected_job_with_an_env_block(tmp_path):
    """**没有闸门步骤**的 job 里,受保护脚本那一步的 job 级 `env:` 也要报。

    **为什么**:job 级的旁路键由**两条**规则负责——
    「闸门」那条(有闸门步骤的 job)与「受保护脚本」那条(没有闸门步骤的 job)。
    两条**各有各的**见证(仓库规矩:见证不许共用 ⇒ 走错原因的红也算数)。
    把闸门步骤全删掉(那份套件没人跑,正是我们要防的改法)之后,
    job 级 `env:` 仍必须被抓。
    """
    without_gate = (
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
        "    env:\n      PATH: /tmp/evil:$PATH\n"
        "    steps:\n"
        "      - name: 纪律守卫的变异验证\n"
        "        run: |\n"
        "          python tools/check_guard_mutations.py --verify\n")
    problems = _problems(_write_workflow(tmp_path, without_gate))
    assert any("受保护脚本" in problem and "job 带了 `env:`" in problem
               for problem in problems), (
        "受保护脚本所在 job 的 env: 没有被报出来:%s" % problems)
