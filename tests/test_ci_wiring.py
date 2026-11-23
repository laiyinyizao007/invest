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
    assert wiring.main(["--roster", str(roster)]) == 2
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
    这一步的退出码**就是**「142 种变异全部被抓住」的证据。
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


# --------------------------------------------------------------------------
# 第 55 轮之七:两轮外部审查(codex 第 4 轮 39/50、claude 第 4 轮 35/50)
# 落地的那些规则。每条规则都有**放行侧**与**拦截侧**两侧的探针。
# --------------------------------------------------------------------------

def test_the_checker_reports_a_job_level_container(tmp_path):
    """job 级 `container:` ⇒ 必须报出来。

    **为什么**(codex 第 4 轮的 H1):`container:` 让这一步整个跑在
    **别人给的镜像**里 ⇒ 装什么、跑哪个 python 全不由我们说了算
    ⇒ 闸门/守卫压根没跑,而 CI 看上去是绿的。
    """
    mutated = REAL_TEXT.replace(
        JOB_HEAD,
        JOB_HEAD + "\n    container:\n      image: python:3.12-slim")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("container:" in problem for problem in problems), (
        "job 级 container: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_job_level_services(tmp_path):
    """job 级 `services:` ⇒ 与 `container:` 同一条洞,必须报出来。"""
    mutated = REAL_TEXT.replace(
        JOB_HEAD,
        JOB_HEAD + "\n    services:\n      db:\n        image: postgres:16")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("services:" in problem for problem in problems), (
        "job 级 services: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_step_mentioning_github_env(tmp_path):
    """步骤里提到 `$GITHUB_ENV` / `$GITHUB_PATH` ⇒ 必须报出来。

    **为什么**(claude 第 4 轮的 H-1):这两个是 GitHub 提供的**官方**通道
    ⇒ 往里写一行 PATH 就等于**给后续所有步骤**换掉解释器
    ⇒ 与本轮禁掉的 `env:` **完全等价** ⇒ 闸门压根没跑而 CI 看上去是绿的。
    """
    evil = ("      - name: 换掉后续所有步骤的解释器\n"
            "        run: echo \"PYTHONPATH=/tmp/evil\" >> $GITHUB_ENV\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                evil + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("GITHUB_ENV" in problem for problem in problems), (
        "$GITHUB_ENV 没有被报出来:%s" % problems)


def test_the_checker_reports_a_push_trigger_with_a_paths_filter(tmp_path):
    """`push:` 触发器上带 `paths:` / `paths-ignore:` ⇒ 必须报出来。

    **为什么**(codex 第 4 轮的 L-2):文件过滤意味着**不匹配的提交压根不触发**
    这个 workflow ⇒ 闸门压根没跑,而 CI 看上去是绿的
    (分支保护只看到「这个 workflow 没跑」,不当作失败)。
    """
    mutated = REAL_TEXT.replace(
        TRIGGERS,
        "on:\n  push:\n    branches: [main]\n    paths:\n      - 'src/**'\n"
        "  pull_request:")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("push 触发器" in problem and "paths" in problem
               for problem in problems), (
        "push 触发器上的 paths: 没有被报出来:%s" % problems)


def test_the_checker_reports_a_gate_step_in_a_composite_action(tmp_path):
    """**复合动作**(`action.yml`)里藏闸门步骤 ⇒ 必须报出来。

    **为什么**(第 55 轮之七):复合动作也是「能跑命令的地方」⇒
    里面藏一条闸门命令 ⇒ 闸门在**别处**跑 ⇒ 那个地方没有研究侧依赖
    ⇒ 闸门跑不起来 ⇒ 「闸门跑了但没跑成」和「闸门压根没跑」一模一样。
    """
    actions = tmp_path / ".github" / "actions" / "evil"
    actions.mkdir(parents=True)
    (actions / "action.yml").write_text(
        "name: evil\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: python tools/ci_gate.py --junitxml \"$RUNNER_TEMP/x.xml\""
        " --roster tests/guard_roster.json --suite tests/test_a.py\n",
        encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    primary = workflows / "research-guard.yml"
    primary.write_text(REAL_TEXT, encoding="utf-8")
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert any("复合动作" in problem for problem in problems), (
        "复合动作里的闸门步骤没有被报出来:%s" % problems)


def test_the_checker_reports_a_module_form_gate_call_in_a_composite_action(tmp_path):
    """**模块形态**的闸门调用(`python -m tools.ci_gate`)⇒ 同样必须报出来。

    **为什么**(codex 第 4 轮的 M-2 / claude 第 4 轮的 M-3):
    `python -m tools.ci_gate` 与 `python tools/ci_gate.py` 是**同一个脚本**
    ⇒ 只按**字面**比对 ⇒ 模块形态**完全隐形**
    ⇒ 「别处不许有闸门步骤」这条不变量可以被一步绕过。
    """
    actions = tmp_path / ".github" / "actions" / "evil"
    actions.mkdir(parents=True)
    (actions / "action.yml").write_text(
        "name: evil\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: python -m tools.ci_gate --junitxml \"$RUNNER_TEMP/x.xml\""
        " --roster tests/guard_roster.json --suite tests/test_a.py\n",
        encoding="utf-8")
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    primary = workflows / "research-guard.yml"
    primary.write_text(REAL_TEXT, encoding="utf-8")
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert any("ci_gate" in problem for problem in problems), (
        "模块形态的闸门调用没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_redirection_on_a_gate_step(tmp_path):
    """**放行侧**:闸门步骤带 `2>&1` ⇒ 不该被判红。

    **为什么**(codex 第 4 轮的 M-1 / claude 第 4 轮的 L-1):
    重定向**不影响退出码** ⇒ 它不该让「闸门步骤不是规范形」。
    此前 `2>&1` 被切成**两条**命令 ⇒ 「这一步有两条命令」⇒ 误伤。
    """
    mutated = REAL_TEXT.replace(GATE_LINE, GATE_LINE + " 2>&1")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("只有一条命令" in problem or "非规范形" in problem
                   for problem in problems), (
        "带重定向的闸门步骤被误判了:%s" % problems)


def test_the_checker_does_not_report_a_command_that_only_mentions_pytest(tmp_path):
    """**放行侧**:`grep -rn "pytest" tests/` 这种**列举**写法 ⇒ 不该判红。

    **为什么**(codex 第 4 轮的 M-3):fail-closed 的判据要问
    「这条命令**会不会执行** pytest/闸门」,而不是「文本里**有没有**这个词」。
    ⇒ 仓库教训:**禁引用,不是禁出现**。
    """
    listing = ("      - name: 看看哪些文件提到 pytest\n"
               "        run: grep -rn \"pytest\" tests/ | head -5\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                listing + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("pytest" in problem for problem in problems), (
        "只是列举 pytest 的命令被误判了:%s" % problems)


def test_the_checker_reports_a_hidden_pytest_call_it_cannot_judge(tmp_path):
    """**拦截侧**:藏起来的 pytest 调用(`bash -c "… pytest …"`)⇒ 必须报出来。

    (与上面那条成对:**列举**不算,**藏起来**要算。)
    """
    hidden = ("      - name: 藏起来的 pytest\n"
              "        run: bash -c \"pytest -q tests/test_a.py\"\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                hidden + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来" in problem for problem in problems), (
        "藏起来的 pytest 调用没有被报出来:%s" % problems)


def test_the_checker_reports_a_protected_step_without_the_verify_flag(tmp_path):
    """**受保护脚本**那一步少了 `--verify` ⇒ 必须报出来。

    **为什么**(第 55 轮之七):`--verify` 才是「**ADR 的变异表与脚本双向核对**」
    的那一步。少了它,变异照样被抓,但那条一致性核对**不再被 CI 执行**
    ⇒ 文档里「两边各 N 行,逐条对上」这句话**没人保证**。
    ⇒ 接线自检不能只管**形状**(一条命令、不许带旁路键),**内容**也得管。
    """
    mutated = REAL_TEXT.replace(
        "          python tools/check_guard_mutations.py --verify",
        "          python tools/check_guard_mutations.py")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("受保护脚本" in problem and "--verify" in problem
               for problem in problems), (
        "受保护脚本那一步缺 --verify 没有被报出来:%s" % problems)


def test_the_checker_reports_a_protected_step_with_an_extra_option(tmp_path):
    """**受保护脚本**那一步多带一个选项 ⇒ 必须报出来(只允许一种形态)。"""
    mutated = REAL_TEXT.replace(
        "          python tools/check_guard_mutations.py --verify",
        "          python tools/check_guard_mutations.py --verify --quiet")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("受保护脚本" in problem and "--verify" in problem
               for problem in problems), (
        "受保护脚本那一步多带选项没有被报出来:%s" % problems)


def test_the_repository_protected_steps_carry_the_verify_flag():
    """**放行侧**:仓库里那几步受保护脚本**必须**带 `--verify`。

    (只测「少带要报」不够:还要测「本来就带」⇒ 否则把仓库里那一步的
    `--verify` 删掉、把测试改成期望报错,也可能自洽地通过。)
    """
    for script in ("tools/check_mutations.py", "tools/check_guard_mutations.py"):
        assert ("python %s --verify" % script) in REAL_TEXT, (
            "仓库里的受保护步骤没有带 --verify:%s" % script)


def test_the_two_layers_agree_on_what_a_canonical_wiring_call_is():
    """**两层口径必须一致**(研究层 ↔ 宪法层)。

    **为什么**(claude 第 4 轮的 M-1):宪法层是**纯标准库 + git**、**不装**
    PyYAML ⇒ 它用**文本级**的办法核对「研究 workflow 里的接线自检还在」。
    ⇒ 两层各自实现了一遍「什么算规范形」⇒ 一旦口径漂移
    (`./tools/…`、`tools/../tools/…`、`-Xutf8` 这些**等价**写法),
    同一处调用**一层说合规、一层说不是规范形** ⇒ CI 变红,
    或者更糟:两层都判「压根没有」而真实调用还在。

    ⇒ 这里**逐个写法**比一遍两层的结论(两个方向都钉住)。
    """
    canonical = [
        "python tools/check_ci_wiring.py",
        "python3 tools/check_ci_wiring.py",
        "py -3 tools/check_ci_wiring.py",
        "python -X utf8 tools/check_ci_wiring.py",
        "python -Xutf8 tools/check_ci_wiring.py",
        "python -W error tools/check_ci_wiring.py",
        "python -Werror tools/check_ci_wiring.py",
        "python ./tools/check_ci_wiring.py",
        "python tools/../tools/check_ci_wiring.py",
    ]
    non_canonical = [
        "python tools/check_ci_wiring.py --workflow other.yml",
        "python -m tools.check_ci_wiring",
        "python -c \"import tools.check_ci_wiring\"",
        "bash -c \"python tools/check_ci_wiring.py\"",
        "ruff check tools/check_ci_wiring.py",
    ]
    for shape in canonical:
        research = wiring.canonical_wiring(shlex.split(shape))
        constitution_view = constitution._is_canonical_wiring_call(shape)
        assert research == constitution_view, (
            "两层结论不一致(%r):研究层=%s 宪法层=%s" % (shape, research,
                                                      constitution_view))
        assert research, "这一种**等价**写法不该被判非规范形:%r" % shape
    for shape in non_canonical:
        research = wiring.canonical_wiring(shlex.split(shape))
        constitution_view = constitution._is_canonical_wiring_call(shape)
        assert research == constitution_view, (
            "两层结论不一致(%r):研究层=%s 宪法层=%s" % (shape, research,
                                                      constitution_view))
        assert not research, "这一种写法不该被判规范形:%r" % shape


def test_the_checker_reports_a_wiring_job_with_a_job_level_default_shell(tmp_path):
    """**守卫所在那个 job** 的 job 级 `defaults.run.shell` ⇒ 必须报出来。

    **为什么**(第 55 轮之七):守卫自己的纪律此前只管**步骤级**的
    `shell:` / `working-directory:` 与 **workflow 级**的 `defaults.run.*`
    ⇒ **job 级**能把守卫的执行语义换掉。
    """
    mutated = REAL_TEXT.replace(
        "  wiring:\n    runs-on: ubuntu-latest",
        "  wiring:\n    runs-on: ubuntu-latest\n"
        "    defaults:\n      run:\n        shell: bash {0}")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    path = _write_workflow(tmp_path, mutated)
    problems = wiring.check_wiring_presence(path, WIRING_SCRIPT)
    assert any("defaults.run.shell" in problem for problem in problems), (
        "守卫 job 上的 job 级 defaults.run.shell 没有被报出来:%s" % problems)


def test_a_job_level_problem_is_reported_once_per_job(tmp_path):
    """job 级的问题**一个 job 只报一遍**(第 55 轮之七)。

    **为什么**:此前 job 级是**按步骤**报的 ⇒ 一个有 5 个闸门步骤的 job
    会把同一条问题报 5 遍 ⇒ 读的人以为是 5 件事 ⇒ 反而掩盖了「其实只有一处」。
    """
    mutated = REAL_TEXT.replace(
        JOB_HEAD, JOB_HEAD + "\n    env:\n      PATH: /tmp/evil:$PATH")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    hits = [problem for problem in problems if "job 带了 `env:`" in problem]
    assert len(hits) == 1, (
        "job 级 `env:` 被报了 %d 遍(应当恰好一遍):%s" % (len(hits), problems))


def test_the_background_operator_check_ignores_quoted_ampersands():
    """**放行侧**:引号里的 `&` 不是后台执行(codex 第 4 轮的 L-3)。

    `--suite "a&b"` 此前被误判成「丢到后台」⇒ 误伤合法写法。
    """
    assert wiring.has_background_operator('python x.py --suite "a&b"') is False
    assert wiring.has_background_operator("python x.py --suite 'a&b'") is False
    assert wiring.has_background_operator("python x.py &") is True
    assert wiring.has_background_operator("python x.py 2>&1") is False


def test_the_constitution_layer_normalises_the_script_path():
    """宪法层必须按**归一化后的路径**认规范形(与研究层同一套口径)。

    **为什么**(claude 第 4 轮的 M-1):`./tools/check_ci_wiring.py` 与
    `tools/../tools/check_ci_wiring.py` 与 `tools/check_ci_wiring.py`
    是**同一个文件** ⇒ 研究层按 `os.path.normpath` 判 ⇒ 宪法层也必须
    ⇒ 否则**两层结论相反**(一层说合规、一层说不是规范形)⇒ CI 变红。
    """
    for shape in ("python ./tools/check_ci_wiring.py",
                  "python tools/../tools/check_ci_wiring.py"):
        assert constitution._is_canonical_wiring_call(shape), shape


def test_the_constitution_layer_accepts_the_attached_value_options():
    """宪法层必须认**连写**的取值选项(`-Xutf8` / `-Werror`)。

    **为什么**(claude 第 4 轮的 M-1):CPython **也接受**这种写法
    ⇒ 研究层已经认 ⇒ 宪法层也要认 ⇒ 否则同一处调用**两层结论相反**。
    """
    for shape in ("python -Xutf8 tools/check_ci_wiring.py",
                  "python -Werror tools/check_ci_wiring.py"):
        assert constitution._is_canonical_wiring_call(shape), shape


def test_the_two_workflow_level_env_messages_are_identical(tmp_path):
    """两条规则判的**是同一条问题** ⇒ 文案必须**逐字相同**。

    **为什么**(claude 第 4 轮的 L-2):workflow 级 `env:` 由**两处**判:
    形状检查(`_check_workflow_shape`)与接线存在性检查(`check_wiring_presence`)。
    此前两处**措辞不同** ⇒ `unique_problems` 的**整串去重**合不掉
    ⇒ 同一条问题在结论里出现**两遍**(措辞还不一样)
    ⇒ 读的人以为是两件事 ⇒ 反而掩盖了「其实只有一处」。

    ⇒ 这里钉住:**两边给出的是同一句话** ⇒ 去重能生效。
    """
    workflow = ("on:\n  push:\n    branches: [main]\n  pull_request:\n"
                "env:\n  FOO: bar\n"
                "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - name: 闸门\n"
                "        run: python tools/ci_gate.py --junitxml \"$RUNNER_TEMP/x.xml\""
                " --roster tests/guard_roster.json --suite tests/test_a.py\n")
    path = Path("\u0001")
    path = _write_workflow(tmp_path, workflow)
    shape = [problem for problem in wiring.check_gate_wiring(path, ROSTER)
             if "workflow 级" in problem and "env:" in problem]
    presence = [problem for problem in wiring.check_wiring_presence(path)
                if "workflow 级" in problem and "env:" in problem]
    assert shape, "形状检查没有报 workflow 级 env:"
    assert presence, "接线存在性检查没有报 workflow 级 env:"
    assert set(shape) == set(presence), (
        "两条规则给的是同一条问题,文案却不同 ⇒ 去重合不掉 ⇒ 会报两遍:\n"
        "形状检查=%s\n接线检查=%s" % (shape, presence))


def test_the_repository_domain_classifier_step_is_not_flagged():
    """**放行侧**:仓库里分档器冒烟那一步**不该**被判红。

    (同时**钉住「它被认出来了」**——否则把整条规则删掉,这个用例照样绿。)
    """
    roles = wiring._job_roles(wiring._load_yaml(RESEARCH_WORKFLOW))
    assert any(role.get("self_guard") for role in roles.values()), (
        "分档器那一步没被认成裁决者关卡 ⇒ 规则在真实 workflow 上形同虚设")
    problems = wiring.check_gate_wiring(RESEARCH_WORKFLOW, ROSTER)
    assert not any("分档器冒烟" in problem for problem in problems), (
        "仓库自己那一步被误判了:%s" % [p for p in problems if "分档器" in p])


def test_the_checker_reports_a_domain_classifier_step_with_continue_on_error(tmp_path):
    """**拦截侧**:分档器那一步带 `continue-on-error:` ⇒ 必须报出来。

    **为什么**(claude 第 4 轮的 L-5):分档器是「谁拥有判定点」的**裁决者**
    (项目 CLAUDE.md 明说它属于**不可降级**清单)⇒ 它自己的冒烟关卡被
    `continue-on-error: true` 轻轻放过 ⇒ **分档器的关卡永远不会红**
    ⇒ 而 CI 看上去是绿的。
    """
    mutated = REAL_TEXT.replace(
        "      - name: 后果域分档器冒烟(非 UTF-8 控制台 + 退出码 + 输出自证)\n",
        "      - name: 后果域分档器冒烟(非 UTF-8 控制台 + 退出码 + 输出自证)\n"
        "        continue-on-error: true\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "continue-on-error" in problem
               for problem in problems), (
        "分档器那一步的 continue-on-error 没有被报出来:%s" % problems)


def test_a_domain_classifier_job_without_a_gate_step_is_not_flagged(tmp_path):
    """**放行侧**:一个**只有**分档器、没有闸门步骤的 job ⇒ 不该被判红。

    (这条是**必须**的:那一步在仓库里本来就是一个**多命令块**
    ⇒ 若套用「只有一条命令」就会误伤仓库自己。)
    """
    workflow = ("on:\n  push:\n    branches: [main]\n  pull_request:\n"
                "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - name: 分档器冒烟\n"
                "        run: |\n"
                "          set -e\n"
                "          python tools/check_domain.py --diff origin/main\n")
    path = _write_workflow(tmp_path, workflow)
    problems = wiring.check_gate_wiring(path, ROSTER)
    assert not any("分档器冒烟" in problem for problem in problems), (
        "只有分档器那一步的 job 被误判了:%s"
        % [p for p in problems if "分档器" in p])


def test_the_checker_reports_a_domain_classifier_job_with_a_bypass_key(tmp_path):
    """**拦截侧**:只有分档器的那个 job 带了 job 级 `env:` ⇒ 必须报出来。

    (job 级 ⇒ 整个 job 可以不出力 ⇒ 分档器的关卡压根没跑。)
    """
    workflow = ("on:\n  push:\n    branches: [main]\n  pull_request:\n"
                "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
                "    env:\n      PATH: /tmp/evil:$PATH\n"
                "    steps:\n"
                "      - name: 分档器冒烟\n"
                "        run: |\n"
                "          set -e\n"
                "          python tools/check_domain.py --diff origin/main\n")
    path = _write_workflow(tmp_path, workflow)
    problems = wiring.check_gate_wiring(path, ROSTER)
    assert any("分档器冒烟" in problem and "job 带了 `env:`" in problem
               for problem in problems), (
        "分档器所在 job 的 env: 没有被报出来:%s" % problems)


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


# --- 第 55 轮之八:旁路**键**纪律拦不住的三条通道(实测) ---
#
# 第 55 轮之七给裁决者(`tools/check_domain.py`)的冒烟关卡施加了「旁路键纪律」
# (`if:` / `continue-on-error:` / `shell:` / `env:` / 尾部 `&`)。
# **实测发现**:那一步在仓库里本来就是一个**多命令块**
# (`set -e` + 显式 rc 判定 + 结论行自证)⇒ 「只有一条命令」的规范形套不上它
# ⇒ 于是有**三条通道**绕过了全部纪律:
#   ① `set -e` ⇒ `set +e`(errexit 被关掉 ⇒ 分档器崩了/返回 2 了,这步照样绿);
#   ② 分档器调用后面接 `|| true`(失败被吃掉);
#   ③ 把整个步骤**删掉**(关卡压根不在 CI 里)。
# ⇒ 本轮把三条都补上,并各配**放行侧 + 拦截侧**两枚探针。


def test_the_checker_reports_a_domain_classifier_step_that_disables_errexit(tmp_path):
    """**拦截侧**:分档器那一步里出现 `set +e` ⇒ 必须报出来。

    **为什么**:errexit 关掉之后,分档器**崩了**(退出码 1 = 「判为执行域」,
    在这一步的语义里是**正常结果**)或返回 2(fail-closed)时,
    这一步**照样绿** ⇒ 裁决者的关卡等于没有。
    """
    mutated = REAL_TEXT.replace(
        "        run: |\n          set -e\n",
        "        run: |\n          set +e\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "set +e" in problem
               for problem in problems), (
        "关掉 errexit 的分档器那一步没有被报出来:%s" % problems)


def test_the_checker_does_not_report_the_repository_errexit_setting(tmp_path):
    """**放行侧**:仓库里那一步本来就写着 `set -e` ⇒ 不该被判红。

    (否则新规则会**误伤仓库自己** ⇒ 守卫生效的前提是它不误伤。)
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert not any("errexit" in problem for problem in problems), (
        "仓库自己的 `set -e` 被误判了:%s"
        % [p for p in problems if "errexit" in p])


def test_the_checker_reports_a_domain_classifier_call_followed_by_or_true(tmp_path):
    """**拦截侧**:分档器调用后面跟着 `|| true` ⇒ 必须报出来。

    (`|| true` 把失败**吃掉** ⇒ 分档器返回 2(fail-closed)时这一步照样绿。)
    """
    mutated = REAL_TEXT.replace(
        "PYTHONIOENCODING=cp1252 python tools/check_domain.py scripts/optimize.py",
        "PYTHONIOENCODING=cp1252 python tools/check_domain.py scripts/optimize.py"
        " || true")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "|| true" in problem
               for problem in problems), (
        "被 `|| true` 吃掉的分档器调用没有被报出来:%s" % problems)


def test_the_checker_does_not_report_the_domain_classifier_capturing_its_exit_code(
        tmp_path):
    """**放行侧**:分档器那一步里的 `|| rc=$?` ⇒ **不该**被判红。

    **为什么**:`|| rc=$?` 是**捕获**退出码(后面还要拿它判 2),
    不是**吃掉**失败 ⇒ 与 `|| true` 完全是两回事 ⇒ 不能连坐。
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert not any("|| true" in problem for problem in problems), (
        "捕获退出码的 `|| rc=$?` 被误判了:%s"
        % [p for p in problems if "|| true" in p])


def test_the_checker_reports_a_missing_domain_classifier_gate(tmp_path):
    """**拦截侧**:把**整个**分档器冒烟步骤删掉 ⇒ 必须报出来。

    **为什么**(第 55 轮之八,实测):第 55 轮之七只管「那一步带了旁路键」,
    管不到「那一步**根本不在**」⇒ 实测删掉整步 ⇒ 工具**零问题**
    ⇒ 后果域分档这条线在 CI 里**静默消失**。
    """
    start = REAL_TEXT.index("      - name: 后果域分档器冒烟")
    end = REAL_TEXT.index("      - name:", start + 10)
    mutated = REAL_TEXT[:start] + REAL_TEXT[end:]
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("裁决者冒烟调用" in problem for problem in problems), (
        "分档器那一步被整个删掉却没有被报出来:%s" % problems)


def test_the_repository_has_a_domain_classifier_gate():
    """**放行侧**:仓库里**必须**有裁决者冒烟调用 ⇒ 现在有。

    (同时钉住「它被认出来了」——否则把整条规则删掉,这个用例照样绿。)
    """
    assert wiring._self_guard_calls(RESEARCH_WORKFLOW), (
        "仓库里找不到分档器冒烟调用 ⇒ 规则在真实 workflow 上形同虚设")
    problems = wiring.check_self_guard_present(
        [RESEARCH_WORKFLOW, CONSTITUTION_WORKFLOW])
    assert not problems, (
        "仓库自己有分档器冒烟调用,却被报成没有:%s" % problems)


# ---------------------------------------------------------------------------
# 第 55 轮之九:R19(失败被吃掉的其它形态)与 R20(裁决者冒烟调用必须与闸门同在)
#
# **来历**:实测(把仓库整个复制到临时目录、在副本的 workflow 上逐个施加绕过写法、
# 跑**副本里的真守卫**)发现第 55 轮之八只堵了「**行尾**的 `|| true`」
# ⇒ 还有四种写法照样能吃掉失败 ⇒ 一族能力,四条通道。
# ---------------------------------------------------------------------------


def test_the_checker_reports_a_domain_classifier_call_whose_failure_is_swallowed(
        tmp_path):
    """**拦截侧**:分档器调用后面跟着 `|| true;` ⇒ 必须报出来。

    **为什么**:`|| true` 的**行尾**形态第 55 轮之八已经堵了;但多一个分号
    (`|| true;`)语义一样(失败照样被吃掉)却**不再落在行尾**
    ⇒ 实测**漏过** ⇒ 本轮补上。
    """
    mutated = REAL_TEXT.replace(
        "PYTHONIOENCODING=cp1252 python tools/check_domain.py scripts/optimize.py",
        "PYTHONIOENCODING=cp1252 python tools/check_domain.py scripts/optimize.py"
        " || true;")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "把失败吃掉" in problem
               for problem in problems), (
        "失败被 `|| true;` 吃掉的分档器调用没有被报出来:%s" % problems)


def test_the_checker_reports_a_domain_classifier_call_swallowed_across_a_continuation(
        tmp_path):
    """**拦截侧**:`cmd ||`(换行)`true` ⇒ 必须报出来。

    **为什么**:shell 里 `cmd ||` 换行接 `true` 与写在一行**完全等价**
    ⇒ 失败照样被吃掉 ⇒ 实测**漏过** ⇒ 本轮补上。
    """
    mutated = REAL_TEXT.replace(
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n",
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py ||\n"
        "            true\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "换行" in problem
               for problem in problems), (
        "失败被续行里的 `true` 吃掉的分档器调用没有被报出来:%s" % problems)


def test_the_checker_reports_a_domain_classifier_call_wrapped_in_a_condition(
        tmp_path):
    """**拦截侧**:分档器调用被**包在 `if` 块里** ⇒ 必须报出来。

    **为什么**:条件里的调用,它的失败**不会**让这一步变红(条件分支决定走向)
    ⇒ 实测**漏过** ⇒ 本轮补上。
    """
    mutated = REAL_TEXT.replace(
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n",
        "          if true; then\n"
        "            PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n"
        "          fi\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "包在条件里" in problem
               for problem in problems), (
        "被条件包住的分档器调用没有被报出来:%s" % problems)


def test_the_checker_reports_a_decoy_domain_classifier_call_in_another_workflow(
        tmp_path):
    """**拦截侧**:真调用删掉、在**别的** workflow 里放一个**假调用** ⇒ 必须报出来。

    **为什么**(第 55 轮之九,实测):此前「存在性」的口径是
    「**任何一个** workflow 里有调用就算过」⇒ 可以在别的 workflow 里放个假调用
    把真调用顶掉 ⇒ 收紧为「**有闸门步骤的** workflow 必须**自己**带一处调用」。
    """
    start = REAL_TEXT.index("      - name: 后果域分档器冒烟")
    end = REAL_TEXT.index("      - name:", start + 10)
    research_text = REAL_TEXT[:start] + REAL_TEXT[end:]
    constitution_text = CONSTITUTION_WORKFLOW.read_text(encoding="utf-8")
    decoy_text = constitution_text.rstrip("\n") + (
        "\n      - name: 假调用\n"
        "        run: python tools/check_domain.py scripts/optimize.py\n")
    research_path = tmp_path / "research-guard.yml"
    constitution_path = tmp_path / "constitution-guard.yml"
    research_path.write_text(research_text, encoding="utf-8")
    constitution_path.write_text(decoy_text, encoding="utf-8")
    problems = wiring.check_self_guard_present([research_path, constitution_path])
    assert any("裁决者冒烟调用" in problem for problem in problems), (
        "真调用被删掉、假调用顶上来,却没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_domain_classifier_capture_across_a_continuation(
        tmp_path):
    """**放行侧**:`cmd ||`(换行)`rc=$?` ⇒ **不该**被判红。

    **为什么**:`rc=$?` 是**捕获**退出码(后面还要拿它判 2),不是吃掉失败
    ⇒ 与「吃掉」完全是两回事 ⇒ 不能连坐(否则守卫会误伤仓库自己)。
    """
    mutated = REAL_TEXT.replace(
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n",
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py ||\n"
        "            rc=$?\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("把失败吃掉" in problem for problem in problems), (
        "捕获退出码的续行写法被误判了:%s"
        % [p for p in problems if "把失败吃掉" in p])


def test_the_checker_does_not_report_the_repository_conditional_blocks():
    """**放行侧**:仓库自己那一步**本来就有** `if` 块(判 rc、判结论行)
    ⇒ 不该被判红。

    (否则新规则会**误伤仓库自己** ⇒ 守卫生效的前提是它不误伤。)
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert not any("包在条件里" in problem for problem in problems), (
        "仓库自己的条件块被误判了:%s"
        % [p for p in problems if "包在条件里" in p])


# ==========================================================================
# 第 55 轮之十:codex 第 5 轮的 H-1 / M-2 / M-3 / M-4 / M-5 / L-6 / L-7
#
# 这一轮的七条判据(F1–F7)与宪法层的口径修复,**每一条都配双向探针**:
#   拦截侧 = 那条绕过写法必须被抓;放行侧 = 合法写法必须**不被**误伤。
# 断言写的是「**哪条规则**拦的」——否则一条无关规则就能把断言喂饱
# (仓库教训:一条静默通过的用例比没有用例更坏)。
#
# **这一轮的由来**:codex 第 5 轮在**仓库副本**上实测出七条漏过的通道。
# 静态推理必然漏 ⇒ 每条判据都要**实测**它在副本里抓不抓得住。
# ==========================================================================

# 受保护脚本那一步(模块形态的绕法锚在它上面)
PROTECTED_LINE = "          python tools/check_guard_mutations.py --verify\n"
# 裁决者冒烟那一步的块首(关掉 errexit 的绕法锚在它后面)
ADJUDICATOR_LINE = "          set -e\n"


def _insert_step(text: str, step: str) -> str:
    """把一步**插到 research job 的 steps 列表最前面**(YAML 才合法)。

    **为什么插在 `steps:` 之后**:插在 job 头与 `steps:` 之间 ⇒ YAML 非法
    ⇒ 探针自己先崩 ⇒ 「抓到了」是**假阳性**(第 55 轮之十实测踩过:
    每一条合法变体都被误判成「误伤」)。
    """
    anchor = "    steps:\n"
    assert text.count(anchor) >= 1, "workflow 里没有 steps: 锚点"
    return text.replace(anchor, anchor + step, 1)


def _composite_action_repo(tmp_path: Path, run_block: str) -> Path:
    """搭一个「仓库目录」:一个复合动作 + 一个 workflow 目录。"""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    action = tmp_path / ".github" / "actions" / "evil" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(
        "name: evil\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: bash\n"
        "      run: %s\n" % run_block,
        encoding="utf-8")
    primary = workflows / "research-guard.yml"
    primary.write_text("on:\n  push:\n    branches: [main]\n", encoding="utf-8")
    return workflows


# --- F1:受保护脚本的**模块形态**必须照样要求 `--verify` ---
def test_the_checker_reports_a_protected_script_called_in_module_form(tmp_path):
    """**拦截侧**:受保护脚本写成**模块形态** ⇒ 必须报出来。

    **为什么**(第 55 轮之十,codex 第 5 轮的 H-1):
    `python -m tools.check_guard_mutations` 跑的是**同一个脚本**
    ⇒ 此前按**带 `.py` 的基名**比对 ⇒ **查不到它** ⇒ `--verify` 那条要求
    **整条被跳过** ⇒ 实测 8 个变体**全部漏过**。
    ⇒ 而 `--verify` 才是「ADR 的变异表与脚本**双向核对**」的那一步
    ⇒ 文档里「两边各 N 行,逐条对上」这句话就没人保证了。
    """
    mutated = REAL_TEXT.replace(
        PROTECTED_LINE,
        "          python -m tools.check_guard_mutations --verify\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("不是规范形" in problem and "check_guard_mutations.py --verify" in problem
               for problem in problems), (
        "模块形态的受保护脚本没有被报出来:%s" % problems)


def test_the_checker_reports_a_protected_script_in_module_form_without_verify(tmp_path):
    """**拦截侧**:模块形态**且不带** `--verify` ⇒ 必须报出来。

    **这是 H-1 的见证用例**:模块形态的**基名**查不到那份「必须带 `--verify`」
    的要求 ⇒ 这条要求**整条被跳过** ⇒ 模块形态什么选项都不带也照样放行。
    """
    mutated = REAL_TEXT.replace(
        PROTECTED_LINE,
        "          python -m tools.check_guard_mutations\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("不是规范形" in problem and "check_guard_mutations.py --verify" in problem
               for problem in problems), (
        "模块形态且没有 --verify 的受保护脚本没有被报出来:%s" % problems)


# --- F2:`awk` / `sed` / `find` / `git` **真的会执行** ⇒ 不许再当「不执行类」 ---
def test_the_checker_reports_an_awk_command_that_hides_pytest(tmp_path):
    """**拦截侧**:`awk 'BEGIN{system("python -m pytest …")}'` ⇒ 必须报出来。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-2):
    `awk` / `sed` / `find` / `git` 此前在「不执行类命令」白名单里
    ⇒ 但 `awk 'BEGIN{system(…)}'`、`sed '…e'`、`find -exec`、
    `git -c alias` **都能执行** ⇒ 白名单把它们放过去
    ⇒ 藏一条 pytest 进去 ⇒ 闸门那一步被换掉 ⇒ CI 照样绿。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: awk 'BEGIN{system(\"python -m pytest tests/test_ci_wiring.py\")}'\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "awk 里藏的 pytest 没有被报出来:%s" % problems)


def test_the_checker_reports_a_find_command_that_hides_pytest(tmp_path):
    """**拦截侧**:`find … -exec python -m pytest {} +` ⇒ 必须报出来。"""
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: find . -name '*.py' -exec python -m pytest {} +\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "find -exec 里藏的 pytest 没有被报出来:%s" % problems)


def test_the_checker_reports_a_git_alias_command_that_hides_pytest(tmp_path):
    """**拦截侧**:`git -c alias.evil='!python -m pytest' evil` ⇒ 必须报出来。"""
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: git -c alias.evil='!python -m pytest' evil\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "git alias 里藏的 pytest 没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_find_command_without_pytest(tmp_path):
    """**放行侧**:`find . -name '*.py'`(不提 pytest)⇒ **不该**被判红。

    **为什么**:fail-closed 的判据问的是「这条命令**会不会执行** pytest/闸门」
    ⇒ `find` 只是找文件 ⇒ 不执行 ⇒ 不该连坐(否则守卫会误伤仓库自己)。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 无害\n"
        "        run: find . -name '*.py'\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来的命令" in problem for problem in problems), (
        "不提 pytest 的 find 被误判了:%s"
        % [p for p in problems if "认不出来的命令" in p])


# --- F3:复合动作(`.github/actions/**/action.yml`)也要受同一条纪律 ---
def test_the_checker_reports_a_composite_action_that_rewrites_the_environment(tmp_path):
    """**拦截侧**:复合动作里 `>> $GITHUB_ENV` ⇒ 必须报出来。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-3):
    复合动作是 GitHub 官方推荐的复用方式 ⇒ 此前它**只**按「闸门步骤」
    这一条判据扫 ⇒ ①`echo "PATH=/tmp/evil" >> $GITHUB_ENV`
    (给后续所有步骤换掉解释器)②藏一条非规范的 pytest ⇒ **都漏过**(实测)。
    """
    workflows = _composite_action_repo(
        tmp_path, 'echo "PATH=/tmp/evil" >> $GITHUB_ENV')
    primary = workflows / "research-guard.yml"
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert any("GITHUB_ENV" in problem and "复合动作" in problem
               for problem in problems), (
        "复合动作里改写环境的写法没有被报出来:%s" % problems)


def test_the_checker_reports_a_composite_action_that_hides_pytest(tmp_path):
    """**拦截侧**:复合动作里藏一条非规范的 pytest ⇒ 必须报出来。"""
    workflows = _composite_action_repo(
        tmp_path,
        'awk \'BEGIN{system("python -m pytest tests/test_ci_wiring.py")}\'')
    primary = workflows / "research-guard.yml"
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert any("认不出来的命令" in problem and "复合动作" in problem
               for problem in problems), (
        "复合动作里藏的 pytest 没有被报出来:%s" % problems)


# --- F4:`set` 按**参数**解析(`set +eo pipefail` 一样关掉 errexit) ---
def test_the_checker_reports_a_combined_errexit_switch(tmp_path):
    r"""**拦截侧**:`set +eo pipefail` ⇒ 必须报出来(它**一样**关掉 errexit)。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-4):此前是一条正则
    `set\s+\+e(?![A-Za-z0-9_])` ⇒ 后视把 `+eo` **排除**掉了 ⇒ 实测**漏过**
    ⇒ 而 `bash -c 'set -e; set +eo pipefail; false; echo survived'`
    真的打印 survived ⇒ 分档器崩了、返回 2 了,那一步**照样绿**。
    """
    mutated = REAL_TEXT.replace(
        ADJUDICATOR_LINE,
        ADJUDICATOR_LINE + "          set +eo pipefail\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("errexit 被关掉" in problem for problem in problems), (
        "`set +eo pipefail` 没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_pipefail_switch(tmp_path):
    """**放行侧**:`set +o pipefail` ⇒ **不该**被判红(它只关 pipefail)。"""
    mutated = REAL_TEXT.replace(
        ADJUDICATOR_LINE,
        ADJUDICATOR_LINE + "          set +o pipefail\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("errexit 被关掉" in problem for problem in problems), (
        "`set +o pipefail` 被误判成关掉 errexit:%s"
        % [p for p in problems if "errexit 被关掉" in p])


def test_the_checker_does_not_report_the_errexit_inheritance_flag(tmp_path):
    """**放行侧**:`set +E`(errexit 的**继承**标志)⇒ **不该**被判红。

    **为什么**(实测):`bash -c 'set -e; set +E; false'` ⇒ 脚本**照样中止**
    ⇒ `+E` 不影响 errexit 本身 ⇒ 连坐它就是误伤。
    """
    mutated = REAL_TEXT.replace(
        ADJUDICATOR_LINE,
        ADJUDICATOR_LINE + "          set +E\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("errexit 被关掉" in problem for problem in problems), (
        "`set +E` 被误判成关掉 errexit:%s"
        % [p for p in problems if "errexit 被关掉" in p])


# --- F5:名单**判不了** ⇒ 退出码 2(三个消费者同一个退出码) ---
def test_the_wiring_layer_exits_with_two_when_the_roster_is_malformed(tmp_path, capsys):
    """**拦截侧**:名单**结构**不对 ⇒ 退出码必须是 **2**(「判不了」)。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-5):同一个坏名单在三个消费者里
    (`tools/check_ci_ran.py` / `tools/ci_gate.py` / 本工具)必须**同一个退出码**
    ⇒ 此前本工具返回 **1** ⇒ 与「形状不合规」混在一起
    ⇒ 同一个坏名单在三个工具里**看起来**是三件事。
    """
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": []}), encoding="utf-8")
    assert wiring.main(["--roster", str(roster)]) == 2
    out = capsys.readouterr().out
    assert "判不了" in out and "退出码 2" in out, (
        "名单判不了时没有给出结论行:\n%s" % out)


# --- F6:job 级键名**小写归一**后比对(`Container:` 也要报) ---
def test_the_checker_reports_a_job_level_container_with_a_capital_key(tmp_path):
    """**拦截侧**:job 级 `Container:`(大写)⇒ 必须报出来。

    **为什么**(第 55 轮之十,codex 第 5 轮的 L-6):GitHub 的 job 键是**小写**的
    ⇒ `Container:` GitHub 会**忽略**它 ⇒ 但本工具的规则写的是「键存在即判红」
    ⇒ 两边口径要对齐 ⇒ 现在按小写归一后比对(宁可多报,也不放过
    「写成大写就不算容器」这种写法)。
    """
    mutated = REAL_TEXT.replace(
        JOB_HEAD,
        JOB_HEAD + "\n    Container:\n      image: ubuntu:24.04\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("container:" in problem for problem in problems), (
        "大写的 job 级 Container: 没有被报出来:%s" % problems)


# --- F7:转义的 `&` 不是后台操作符 ---
def test_the_background_operator_check_ignores_an_escaped_ampersand():
    r"""**放行侧**:`\&` 是**转义**的 `&`(字面量),不是后台执行。

    **为什么**(第 55 轮之十,codex 第 5 轮的 L-7):此前正则把 `\&`
    也当成后台操作符 ⇒ 合法写法被误判 ⇒ 守卫误伤仓库自己。
    """
    assert not wiring.has_background_operator("echo a \\& b"), (
        "转义的 & 被当成了后台操作符")
    assert wiring.has_background_operator("echo a & b"), (
        "真正的后台操作符没被认出来")


# --- 宪法层:行尾注释与重定向**不该**被当成「非规范形」 ---
def test_the_constitution_layer_accepts_a_wiring_call_with_a_trailing_comment(tmp_path):
    """**放行侧**:宪法层必须接受带**行尾注释**的规范形调用。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-5):宪法层此前把整个 `run:` 块
    当**一条**命令、用 `shlex.split(…, comments=False)` 切词 ⇒ 行尾注释
    **留在词里** ⇒ 「脚本后面不许再有任何词」那条 ⇒ 判成**非规范形**
    ⇒ **误伤合法写法** ⇒ 而研究层会**动态跑**宪法层这条检查
    ⇒ **两层一起红** ⇒ CI 变红。
    """
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 接线自检\n        run: |\n"
        "          python tools/check_ci_wiring.py  # 接线自检\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert problems == [], (
        "宪法层把带行尾注释的合法写法判红了:\n  - " + "\n  - ".join(problems))


def test_the_constitution_layer_accepts_a_wiring_call_with_a_redirection(tmp_path):
    """**放行侧**:宪法层必须接受带**重定向**(`2>&1`)的规范形调用。

    **为什么**:重定向**不是参数** ⇒ 研究层早就把它剥掉了
    ⇒ 两层口径必须一致 ⇒ 否则一次合法编辑就让 CI 变红。
    """
    workflow = tmp_path / ".github" / "workflows" / "research-guard.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "on:\n  push:\n    branches: [main]\n  pull_request:\n"
        "jobs:\n  research:\n    steps:\n"
        "      - name: 接线自检\n        run: |\n"
        "          python tools/check_ci_wiring.py 2>&1\n",
        encoding="utf-8")
    problems = constitution.check_ci_wiring_presence(tmp_path)
    assert problems == [], (
        "宪法层把带重定向的合法写法判红了:\n  - " + "\n  - ".join(problems))

# ==========================================================================
# 第 55 轮之十一:第三轮复核(子代理在**仓库副本**上实测)抓出的通道
#
# **来历**:子代理「Review CI wiring guard」把仓库整个复制到临时目录、
# 逐条施加绕过写法、跑**副本里的真守卫** ⇒ C-1…C-7 + H-1…H-6 **实测复现**。
#
# **纪律**:每条新判据都配**双向**探针 ——
#   - **拦截侧**:那条绕过写法必须被抓;
#   - **放行侧**:合法的同类写法必须**放行**(否则工具变成"一律报红",
#     等于没有判据——仓库教训:**误伤**也是失效)。
# ==========================================================================

SELF_GUARD_CALL_A = ("          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
                     " scripts/optimize.py\n")
SELF_GUARD_CALL_B = ('          out=$(python tools/check_domain.py --diff'
                     ' "origin/${{ github.base_ref || \'main\' }}" 2>&1) || rc=$?\n')
PROTECTED_CALL = "          python tools/check_mutations.py --verify"


# --- C-1:`python -m pytest.__main__` 也**真的跑 pytest** ---
def test_the_checker_reports_a_pytest_main_module_invocation():
    """**拦截侧**:`python -m pytest.__main__` ⇒ 必须认得出它是 pytest。

    **为什么**(第三轮复核的 C-1,实测):`python -m pytest.__main__`
    **真的跑 pytest**(实测 `--version` ⇒ pytest 8.4.2)
    ⇒ 而按**最后一段**取脚本名会得到 `__main__`
    ⇒ 既不等于 `pytest` 也不含 `pytest` ⇒ 落到 `other` ⇒ **放行**
    ⇒ 同一族写法:`python -m pytest` 判红、`python -m pytest.__main__` 判绿。
    """
    command = "python -m pytest.__main__ -q tests/"
    assert wiring.script_of(shlex.split(command)) == "pytest", (
        "`python -m pytest.__main__` 没被认成 pytest ⇒ 这条通道还开着")
    assert wiring.command_kind(shlex.split(command), command) == "pytest"


def test_the_checker_still_reports_a_plain_pytest_module_invocation():
    """**放行侧对照**:同族的**朴素**写法仍要判红(别把通道堵成"全放行")。"""
    command = "python -m pytest -q tests/"
    assert wiring.command_kind(shlex.split(command), command) == "pytest"


# --- C-2:复合动作里跑 pytest ---
def test_the_checker_reports_a_pytest_invocation_inside_a_composite_action(tmp_path):
    """**拦截侧**:复合动作(`action.yml`)里**跑 pytest** ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-2,实测):此前复合动作**只**查「认不出来的命令」
    ⇒ 一条**规范**的 `python -m pytest` 在复合动作里**不被报**
    ⇒ 而复合动作是 GitHub 官方推荐的**复用**方式 ⇒ 别的 workflow 引用它
    ⇒ 研究侧依赖(pytest / 研究侧模块)在那儿**装不上**、**跑不起来**
    ⇒ 「pytest 压根没跑」与「pytest 跑了但没跑成」在 CI 看上去**一模一样**。
    """
    action = tmp_path / "action.yml"
    action.write_text(
        "name: 跑测试\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: python -m pytest -q tests/\n",
        encoding="utf-8")
    problems = wiring._composite_action_problems(action, wiring._load_yaml(action))
    assert any("跑 pytest" in problem for problem in problems), (
        "复合动作里的 pytest 调用没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_composite_action_that_only_mentions_pytest(
        tmp_path):
    """**放行侧**:复合动作里**只是提到** pytest(`echo`)⇒ 不该判红。"""
    action = tmp_path / "action.yml"
    action.write_text(
        "name: 说明\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: echo \"pytest 单独钉版本\"\n",
        encoding="utf-8")
    problems = wiring._composite_action_problems(action, wiring._load_yaml(action))
    assert problems == [], "只是提到 pytest 的复合动作被误判了:%s" % problems


# --- C-3:别的 workflow 里跑 pytest ---
def test_the_checker_reports_a_pytest_invocation_in_another_workflow(tmp_path):
    """**拦截侧**:**别的** workflow 里跑 pytest ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-3,实测):此前「别的 workflow」那条判据
    **只**找闸门步骤 ⇒ 裸 pytest 在别的 workflow 里**不被扫**
    ⇒ 那些地方**没装**研究侧依赖 ⇒ pytest 跑不起来
    ⇒ 「pytest 压根没跑」与「pytest 跑了但没跑成」在 CI 看上去**一模一样**。
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    primary = workflows / "research-guard.yml"
    primary.write_text(
        "on:\n  push:\n    branches: [main]\n"
        "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo hi\n",
        encoding="utf-8")
    other = workflows / "other.yml"
    other.write_text(
        "on:\n  push:\n    branches: [main]\n"
        "jobs:\n  other:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: python -m pytest -q tests/\n",
        encoding="utf-8")
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert any("pytest" in problem for problem in problems), (
        "别的 workflow 里的 pytest 调用没有被报出来:%s" % problems)


def test_the_checker_does_not_report_another_workflow_that_only_lists_suites(tmp_path):
    """**放行侧**:别的 workflow 里**只是列出**套件名 ⇒ 不该判红。"""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    primary = workflows / "research-guard.yml"
    primary.write_text(
        "on:\n  push:\n    branches: [main]\n"
        "jobs:\n  research:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo hi\n",
        encoding="utf-8")
    other = workflows / "other.yml"
    other.write_text(
        "on:\n  push:\n    branches: [main]\n"
        "jobs:\n  other:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: echo \"tests/test_a.py\"\n",
        encoding="utf-8")
    problems = wiring.check_no_gate_steps_elsewhere(workflows, primary)
    assert problems == [], "只是列出套件名的 workflow 被误判了:%s" % problems


# --- C-4:只跑 `--help` 的分档器调用 ---
def test_the_checker_reports_a_self_guard_call_that_only_prints_help(tmp_path):
    """**拦截侧**:分档器冒烟只跑 `--help` ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-4,实测):`python tools/check_domain.py --help`
    ⇒ argparse 直接退出 0、**不做任何分档** ⇒ 而它满足「这个 workflow 里有
    裁决者冒烟调用」⇒ 分档器的关卡**等于没有**,而工具照样报「自检有人跑」。
    """
    mutated = REAL_TEXT.replace(SELF_GUARD_CALL_A,
                                "          python tools/check_domain.py --help\n")
    mutated = mutated.replace(SELF_GUARD_CALL_B,
                              "          python tools/check_domain.py --help\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "只跑 `--help` 的分档器冒烟没有被报出来:%s" % problems)


# --- C-5:死代码 / 从没被调用的函数体 ---
def test_the_checker_reports_a_self_guard_call_after_an_exit(tmp_path):
    """**拦截侧**:分档器冒烟写在 `exit 0` **之后** ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-5,实测):`exit 0` 之后的代码是**死代码**
    ⇒ 分档器**一步都没跑** ⇒ 而工具照样报「自检本身有人跑」。
    """
    mutated = REAL_TEXT.replace(SELF_GUARD_CALL_A,
                                "          exit 0\n" + SELF_GUARD_CALL_A)
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "写在 `exit 0` 之后的分档器冒烟没有被报出来:%s" % problems)


def test_the_checker_reports_a_self_guard_call_inside_a_function_that_is_never_called(
        tmp_path):
    """**拦截侧**:分档器冒烟写在**从没被调用**的函数体里 ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-5,实测):调用在**函数体**里而那个函数
    **从未被调用** ⇒ 它压根不会跑 ⇒ 分档器一步都没跑。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          never_called() {\n"
        "            python tools/check_domain.py scripts/optimize.py\n"
        "          }\n")
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "写在没被调用的函数体里的分档器冒烟没有被报出来:%s" % problems)


# --- C-6:续行把 `|| true` 推到下一行 ---
def test_the_checker_reports_a_backslash_continuation_that_swallows_the_failure(
        tmp_path):
    """**拦截侧**:`cmd … \\`(换行)`|| true` ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-6,实测):续行把 `|| true` 推到**下一行**
    ⇒ 判据建在**物理行**上 ⇒ 那条 `|| true` **看不见**
    ⇒ 失败照样被吃掉而守卫报绿。
    (注意:这与「`cmd ||` 换行 `true`」**不是**同一条通道——那条第 55 轮之九
    已经堵了;这一条的后缀在**下一行**,前面还隔着一个续行符。)
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py \\\n"
        "          || true\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "被吃掉" in problem
               for problem in problems), (
        "被续行里的 `|| true` 吃掉的分档器调用没有被报出来:%s" % problems)


# --- C-7:`eval 'set +e'` ---
def test_the_checker_reports_an_eval_that_disables_errexit(tmp_path):
    """**拦截侧**:`eval 'set +e'` ⇒ 必须报出来。

    **为什么**(第三轮复核的 C-7,实测):`eval` 的参数**不逐 token 解析**
    ⇒ 实测 `eval 'set +e'` **漏过** ⇒ 而 bash 侧 errexit **确实**被关掉
    ⇒ 分档器崩了(退出码 1 =「判为执行域」,正常结果)或返回 2(fail-closed)时,
    这一步**照样绿**。
    """
    mutated = REAL_TEXT.replace(SELF_GUARD_CALL_A,
                                "          eval 'set +e'\n" + SELF_GUARD_CALL_A)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "errexit" in problem
               for problem in problems), (
        "`eval 'set +e'` 没有被报出来:%s" % problems)


# --- H-1:`|| /bin/true` ---
def test_the_checker_reports_an_absolute_true_that_swallows_the_failure(tmp_path):
    """**拦截侧**:`cmd || /bin/true` ⇒ 必须报出来。

    **为什么**(第三轮复核的 H-1,实测):旧判据只认**字面** `true`
    ⇒ `/bin/true` 是**同一个意思**却**漏过** ⇒ 失败照样被吃掉。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py || /bin/true\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "被吃掉" in problem
               for problem in problems), (
        "被 `|| /bin/true` 吃掉的分档器调用没有被报出来:%s" % problems)


# --- H-2:`|| echo skipped`(任何「退出码必为 0」的右操作数) ---
def test_the_checker_reports_a_generic_success_suffix_that_swallows_the_failure(
        tmp_path):
    """**拦截侧**:`cmd || echo skipped` ⇒ 必须报出来。

    **为什么**(第三轮复核的 H-2,实测):`echo` 的退出码**必为 0**
    ⇒ 与 `|| true` **完全等价** ⇒ 失败照样被吃掉 ⇒ 而旧判据只认字面 `true`。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py || echo skipped\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "被吃掉" in problem
               for problem in problems), (
        "被 `|| echo skipped` 吃掉的分档器调用没有被报出来:%s" % problems)


# --- 放行侧:仓库自己那处 `|| rc=$?` 是**捕获**,不是**吃掉** ---
def test_the_checker_does_not_report_the_repository_captured_exit_code():
    """**放行侧**:仓库里那处 `|| rc=$?` ⇒ **不该**被判成「失败被吃掉」。

    **为什么**:`rc=$?` 是**捕获**退出码(后面还要拿它判 2),
    不是**吃掉**失败 ⇒ 与「吃掉」完全是两回事 ⇒ 不能连坐
    (否则守卫会**误伤仓库自己** ⇒ 守卫生效的前提是它不误伤)。
    """
    problems = _problems(RESEARCH_WORKFLOW)
    assert not any("被吃掉" in problem for problem in problems), (
        "仓库自己的 `|| rc=$?` 被误判了:%s"
        % [p for p in problems if "被吃掉" in p])


# --- H-3:步骤级的 `container:` ---
def test_the_checker_reports_a_step_level_container(tmp_path):
    """**拦截侧**:**步骤级**的 `container:` ⇒ 必须报出来。

    **为什么**(第三轮复核的 H-3):GitHub 的**步骤**也支持 `container:`
    ⇒ 这一步整个跑在**别人给的镜像**里 ⇒ 装什么、跑哪个版本全不由我们说了算
    ⇒ 闸门/守卫压根没跑,而 CI 看上去是绿的
    (此前只查 **job 级** ⇒ 同一个键写在步骤上就绕过去了)。
    """
    mutated = REAL_TEXT.replace(
        STEP_NAME + "\n" + GATE_LINE,
        STEP_NAME + "\n        container:\n          image: ubuntu:24.04\n"
        + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("别人给的镜像" in problem for problem in problems), (
        "步骤级的 container: 没有被报出来:%s" % problems)


# --- H-4:闸门写法**非规范形**的那个 job,job 级键也要报 ---
def test_the_checker_reports_job_level_keys_of_a_job_whose_gate_call_is_not_canonical(
        tmp_path):
    """**拦截侧**:闸门写法**非规范形**的那个 job,它的 job 级 `env:` 也必须报。

    **为什么**(第三轮复核的 H-4,实测):此前 job 级键只对**规范形**的闸门调用
    生效 ⇒ 把闸门**写法改坏**(让它非规范形)⇒ 那个 job 的 job 级
    `container:` / `env:` / `needs:` **一条都不报**
    ⇒ 真正的攻击面被「非规范形」这条消息**盖住**了。

    **断言钉的是「哪条规则」**:这里用一份**最小 workflow**——
    整个 job 里**只有**一个闸门步骤、而且它的调用是**非规范形**
    ⇒ 这个 job 的闸门角色**只能**由「提到了闸门」那条分支填上
    ⇒ 断言里那条消息(「闸门步骤「非规范形的闸门调用」所在的 job 带了 `env:`」)
    **只可能**由**闸门**这条规则产生(这个 job 里没有受保护脚本、没有裁决者冒烟)
    ⇒ 把**闸门**那条分支关掉 ⇒ 角色填不上 ⇒ 这条消息**不再出现** ⇒ 见证会红。

    **上一版为什么空转**(第 55 轮之十一自查时实测):上一版改的是**真实 workflow**,
    那个 job 里还有别的**规范形**闸门步骤 ⇒ 闸门角色照样填得上
    ⇒ 变异之后那条消息**还在** ⇒ 见证**恒绿** ⇒ 等于没有见证。
    ⇒ 所以这里换成**最小 workflow**:它**没有**别的闸门步骤可顶替。
    """
    minimal = (
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "jobs:\n"
        "  rogue:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PATH: /tmp/evil:$PATH\n"
        "    steps:\n"
        "      - name: 非规范形的闸门调用\n"
        "        run: python tools/ci_gate.py\n"
    )
    problems = _problems(_write_workflow(tmp_path, minimal))
    hits = [problem for problem in problems
            if "闸门步骤" in problem and "job 带了 `env:`" in problem]
    assert hits, (
        "非规范形闸门所在 job 的 job 级 env: 没有被报出来:%s" % problems)
    assert "非规范形的闸门调用" in hits[0], (
        "命中的不是这个 job 的那一条消息(说明断言被别的 job 顶替了):%s" % hits)


# --- H-5:受保护脚本被换成仓库根的同名空壳 ---
def test_the_checker_reports_a_protected_script_called_through_a_stub_path(tmp_path):
    """**拦截侧**:受保护脚本被写成**仓库根的同名空壳** ⇒ 必须报出来。

    **为什么**(第三轮复核的 H-5,实测):此前只按**基名**认 ⇒ 在仓库根放一个
    4 行的 `check_mutations.py`(内容 `print(...); sys.exit(0)`)
    ⇒ `python check_mutations.py --verify` **照样合规**
    ⇒ 「142 种变异全部被抓住」这句话**换成了一个空壳**。
    """
    mutated = REAL_TEXT.replace(PROTECTED_CALL,
                                "          python check_mutations.py --verify")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("规范路径" in problem for problem in problems), (
        "从仓库根调用的受保护脚本没有被报出来:%s" % problems)


# --- H-6:复合动作里跑裁决者 ---
def test_the_checker_reports_a_self_guard_call_inside_a_composite_action(tmp_path):
    """**拦截侧**:复合动作里跑**裁决者**(check_domain)⇒ 必须报出来。

    **为什么**(第三轮复核的 H-6):裁决者的冒烟受**旁路键纪律**与
    **失败语义纪律**约束 ⇒ 但那些纪律只管**被守的 workflow** 里的步骤
    ⇒ 把这一步搬进**复合动作** ⇒ 那些纪律**一条都套不上**
    ⇒ 分档器的关卡等于没有。
    """
    action = tmp_path / "action.yml"
    action.write_text(
        "name: 分档\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - run: python tools/check_domain.py scripts/optimize.py\n",
        encoding="utf-8")
    problems = wiring._composite_action_problems(action, wiring._load_yaml(action))
    assert any("逃出了" in problem for problem in problems), (
        "复合动作里的裁决者冒烟没有被报出来:%s" % problems)


# --- M-1:按**形态**判断「不执行类」,别按「怎么写」 ---
def test_the_checker_does_not_report_a_git_grep_command_that_mentions_pytest(tmp_path):
    """**放行侧**:`git grep -n pytest tests/` ⇒ **不该**判红。

    **为什么**(第三轮复核的 M-1):第 55 轮之十把 `git`/`sed`/`find`/`awk`
    从「不执行类」名单里**移出**(它们**能**执行)⇒ 但「**能**」不等于
    「这一条**真的**执行了」⇒ `git grep` 这种**检索**形态被**误伤**
    ⇒ 判据取决于「怎么写」而不是「会不会执行」⇒ 与 R5 的立意相反。
    """
    step = ("      - name: 检索\n"
            "        run: git grep -n pytest tests/\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                step + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "只是检索 pytest 的 git 命令被误判了:%s" % problems)


def test_the_checker_does_not_report_a_pip_install_command_that_mentions_pytest(
        tmp_path):
    """**放行侧**:`pip install pytest==8.3.4` ⇒ **不该**判红。

    **为什么**(同上):装包**不执行** pytest ⇒ 与 `python -m pytest` 完全不同。
    """
    step = ("      - name: 装依赖\n"
            "        run: pip install pytest==8.3.4\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                step + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "只是装 pytest 包的命令被误判了:%s" % problems)


def test_the_checker_does_not_report_a_sed_command_that_mentions_pytest(tmp_path):
    """**放行侧**:`sed -i 's/pytest/PYTEST/' x.py` ⇒ **不该**判红。

    **为什么**(同上):`sed` 的**改写**形态不会执行任何东西。
    """
    step = ("      - name: 改写\n"
            "        run: sed -i 's/pytest/PYTEST/' x.py\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                step + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "只是改写文本的 sed 命令被误判了:%s" % problems)


def test_the_checker_reports_a_sed_command_that_executes_pytest(tmp_path):
    """**拦截侧**:`sed 's/a/pytest/e'` ⇒ 必须报出来。

    **为什么**:`sed` 的 `e` 标志把模式空间当**命令**执行 ⇒ 它**真的能执行**
    ⇒ 不能按「命令头」一律放行 ⇒ 判据必须**按形态**。
    """
    step = ("      - name: 藏起来的 pytest\n"
            "        run: sed 's/a/pytest/e'\n")
    mutated = REAL_TEXT.replace(STEP_NAME + "\n" + GATE_LINE,
                                step + STEP_NAME + "\n" + GATE_LINE)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来" in problem for problem in problems), (
        "能执行 pytest 的 sed 命令没有被报出来:%s" % problems)


# --- M-2:单行的 `if …; then …; fi` 不该把后面的调用**误伤** ---
def test_the_checker_does_not_report_a_call_after_a_single_line_conditional(tmp_path):
    """**放行侧**:单行的 `if …; then …; fi` **之后**的调用 ⇒ 不该被误伤。

    **为什么**(第三轮复核的 M-2,实测):单行的 `if` 让嵌套深度 +1
    却**永不归零** ⇒ 它**后面**的每一步都被当成「被包在条件里」
    ⇒ **误伤**(仓库教训:误伤也是失效)。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          if [ -f README.md ]; then echo hi; fi\n" + SELF_GUARD_CALL_A)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("包在条件里" in problem for problem in problems), (
        "单行条件块之后的调用被误判了:%s"
        % [p for p in problems if "包在条件里" in p])
# --- M-3:`if set +e; then …; fi` 里的 `set +e` 也算「关掉 errexit」 ---
def test_the_checker_reports_a_set_plus_e_inside_a_single_line_conditional(tmp_path):
    """**拦截侧**:`if set +e; then :; fi` ⇒ 必须报出来。

    **为什么**(第三轮复核的 M-3,实测):`logical_commands` 先按 `;` 切
    ⇒ 这一步的首 token 变成 `if` ⇒ 此前只认「**首 token** 是 `set`」的
    判据认不出 `set +e` ⇒ 「errexit 被关掉」这件事**零报告**
    ⇒ 真实缺陷被一条**错误理由**的误报(单行 `if` 的深度泄漏)掩盖。
    """
    mutated = REAL_TEXT.replace(
        "        run: |\n          set -e\n",
        "        run: |\n          if set +e; then :; fi\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("分档器冒烟" in problem and "errexit" in problem
               for problem in problems), (
        "单行条件里的 `set +e` 没有被报出来:%s" % problems)


# ======================================================================
# 第 55 轮之十二:codex 第 6 轮的复核发现
# (H-1 同一逻辑行的死代码 / H-2 sed 的 e 标志 / H-3 rg·awk 的可执行形态 /
#  H-4 第三方动作 / M-1 只读命令里提到闸门 / M-2 引号形态的捕获)
# ======================================================================

# --- H-1:同一逻辑行里 `exit` 之后的调用是**死代码** ---
def test_the_checker_reports_a_self_guard_call_after_an_exit_on_the_same_line(
        tmp_path):
    """**拦截侧**:`exit 0; python tools/check_domain.py …` ⇒ 必须报出来。

    **为什么**(第 55 轮之十二,codex H-1):`_live_self_guard_calls` 此前是
    「**先收调用、后看 exit**」⇒ 同一逻辑行里 `exit 0` **之后**的调用
    仍被当成**活调用** ⇒ 工具照样报「自检本身有人跑」
    ⇒ 而分档器那一步**压根走不到**(分档器的关卡等于没有)。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          exit 0; PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n")
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "写在同一逻辑行 `exit 0` 之后的分档器冒烟没有被报出来:%s" % problems)


def test_the_checker_still_counts_a_self_guard_call_that_precedes_the_exit(
        tmp_path):
    """**放行侧**:调用在 `exit 0` **之前** ⇒ 它**算数** ⇒ 不该报「不算数」。

    **为什么**:H-1 那条判据是「`exit` **之后**的才算死代码」
    ⇒ 若它变成「**整行**都不算数」⇒ 正常的裁决者冒烟会被**误伤**
    ⇒ 误伤也是失效(仓库教训)。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          PYTHONIOENCODING=cp1252 python tools/check_domain.py"
        " scripts/optimize.py\n"
        "          exit 0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert not any("不算数" in problem for problem in problems), (
        "活着的分档器冒烟被误判成死代码:%s" % problems)


# --- H-2:`sed` 的 `e` 标志(引号在不在、分隔符是 `/` 还是 `,` 都一样) ---
def test_the_checker_reports_an_unquoted_sed_exec_flag(tmp_path):
    """**拦截侧**:`sed -e s/a/pytest/e x.txt`(**不引号**)⇒ 必须报出来。

    **为什么**(第 55 轮之十二,codex H-2):此前 `_SED_EXEC_RE` 只认
    **被引号包住**的形态 ⇒ 不引号的**同一种**写法**漏过**
    ⇒ 藏一条 pytest 进去 ⇒ 闸门那一步被换掉 ⇒ CI 照样绿。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: sed -e s/a/pytest/e x.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "不引号的 sed `e` 标志没有被报出来:%s" % problems)


def test_the_checker_reports_a_comma_separated_sed_exec_flag(tmp_path):
    """**拦截侧**:`sed -e s,a,pytest,e x.txt`(逗号分隔)⇒ 必须报出来。"""
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: sed -e s,a,pytest,e x.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "逗号分隔的 sed `e` 标志没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_plain_sed_replacement(tmp_path):
    """**放行侧**:`sed 's/pytest/PYTEST/' x.txt`(普通替换)⇒ **不该**判红。

    **为什么**:第 55 轮之十二把 `sed` 的判据按「标志位里有 `e`」重写
    ⇒ 若写得太宽(例如大小写不敏感 ⇒ `PYTEST` 里的 E 也算 `e`)
    ⇒ 合法写法被**误伤**(实测踩过:第一版就写成了 `(?ix)`)。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 改写文本\n"
        "        run: sed 's/pytest/PYTEST/' x.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "普通的 sed 替换被误判了:%s" % problems)


# --- H-3:`rg --pre` 与 `awk` 的可执行形态 ---
def test_the_checker_reports_an_rg_pre_processor_command(tmp_path):
    """**拦截侧**:`rg --pre "python -m pytest …" …` ⇒ 必须报出来。

    **为什么**(第 55 轮之十二,codex H-3A):`rg` 在「只读文本」名单里
    ⇒ 但 `--pre` 让它**执行**一个预处理器命令 ⇒ 那条命令里藏 pytest
    ⇒ 闸门那一步被换掉而 CI 照样绿。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: rg --pre \"python -m pytest tests/test_ci_wiring.py\""
        " -n pattern tests/\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "rg --pre 里的预处理器命令没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_plain_rg_search(tmp_path):
    """**放行侧**:`rg -n pytest tests/`(普通检索)⇒ **不该**判红。"""
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 检索\n"
        "        run: rg -n pytest tests/\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "普通的 rg 检索被误判了:%s" % problems)


def test_the_checker_reports_an_awk_command_that_pipes_into_a_shell(tmp_path):
    """**拦截侧**:`awk 'BEGIN{print "…" | "/bin/sh"}'` ⇒ 必须报出来。

    **为什么**(第 55 轮之十二,codex H-3B):`awk` 此前只拦 `system(`
    ⇒ 管道给 shell 的形态**漏过**。
    (此前它是被**分词失败**兜住的,不是靠规则 ⇒ 规则本身仍缺 ⇒ 补上。)
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: awk 'BEGIN{print \"python -m pytest tests/test_ci_wiring.py\""
        " | \"/bin/sh\"}'\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "管道给 shell 的 awk 没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_plain_awk_command(tmp_path):
    """**放行侧**:`awk '{print $1}' f.txt`(普通 awk)⇒ **不该**判红。"""
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 取第一列\n"
        "        run: awk '{print $1}' f.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "普通的 awk 被误判了:%s" % problems)


# --- 第 55 轮之十三之二:`sed` 的 `e` **标志位**才��执行,别的一律不是 ---
def test_the_checker_does_not_report_a_sed_replacement_that_merely_contains_an_e(
        tmp_path):
    """**放行侧**:`sed 's/pytest/e/' x.txt` ⇒ **不该**判红。

    **为什么**(第 55 轮之十三之二):那个 `e` 是**替换文本**,**不是标志位**
    ⇒ 这条 sed **什么也不执行** ⇒ 不该判红。
    (上一轮的 `(?x)` 正则把「分隔符结构」漏掉了 ⇒ 它被误判成「可执行」;
     上一轮把它登记成残余 ⇒ **这一轮修掉**。)
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 改写文本\n"
        "        run: sed 's/pytest/e/' x.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "只是改写文本的 sed 被误判了:%s" % problems)


def test_the_checker_does_not_report_a_sed_program_that_writes_a_command(tmp_path):
    """**放行侧**:`sed 's/.*/python -m pytest …/' f.txt` ⇒ **不该**判红。

    **为什么**(第 55 轮之十三之二):它只是把**文本写进文件**
    ⇒ **不执行** ⇒ 不该判红。
    (它之所以被判红,是因为 `(?x)` 模式把分隔符结构漏掉了。)
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 改写文本\n"
        "        run: sed 's/.*/python -m pytest tests\\/test_ci_wiring.py/' f.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "只是写文本的 sed 被误判了:%s" % problems)


# --- 第 55 轮之十三:「管道给 shell」**真的**能被看见 ---
#
# **为什么有这一组**(变异脚手架打红才发现):
# 上一轮的判据「`awk` 把模式管道给 shell」**是空转的** ⇒
# `logical_commands` 在 `|` 处**切开** ⇒ 管道右侧那个 shell **看不见**
# ⇒ 判据作用在切完之后的片段上 ⇒ **永远匹配不到**
# ⇒ `awk 'BEGIN{print "…" | "sh"}'` 之所以被报出来,是**分词失败**
# ⇒ 走的是「认不出来 ⇒ 判红」那条**兜底**,不是这条判据
# ⇒ 见证断言的是**错误的理由** ⇒ 变异打不红。
#
# **真正的洞**(实测):管道在**引号外**时,切开后左半段引号配对
# ⇒ 分词成功 ⇒ 头是 `awk` ⇒ 「只读文本」⇒ **免检**
# ⇒ **闸门压根没跑而 CI 看上去是绿的**。
def test_the_checker_reports_an_awk_program_that_is_piped_into_a_shell(tmp_path):
    """**拦截侧**:`awk 'BEGIN{print "…" }' | sh`(管道在**引号外**)⇒ 必须报出来。

    **为什么**(第 55 轮之十三):这一条**没有任何兜底**能救
    ⇒ 切开后左半段引号是配对的 ⇒ 分词**成功** ⇒ 头是 `awk`
    ⇒ 「只读文本」⇒ 免检 ⇒ 载荷压根没被看见。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: awk 'BEGIN{print \"python -m pytest tests/test_ci_wiring.py\"}'"
        " | sh\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "管道给 shell 的 awk 载荷没有被报出来:%s" % problems)


def test_the_checker_reports_a_sed_program_that_is_piped_into_a_shell(tmp_path):
    """**拦截侧**:`sed 's/.*/python -m pytest …/' | sh` ⇒ 必须报出来。

    **为什么**(第 55 轮之十三):`sed` 与 `awk` **同族** ⇒ 上一轮的判据
    只写在 `awk` 分支里 ⇒ `sed` 这一条**漏过**。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: sed 's/.*/python -m pytest tests\\/test_ci_wiring.py/'"
        " | sh\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "管道给 shell 的 sed 载荷没有被报出来:%s" % problems)


def test_the_checker_reports_an_echo_payload_that_is_piped_into_a_shell(tmp_path):
    """**拦截侧**:`echo "python -m pytest …" | bash` ⇒ 必须报出来。

    **为什么**(第 55 轮之十三):`echo` 在「只读名单」里
    ⇒ 判据若不**提到名单之前** ⇒ `echo … | bash` 就是一条免检通道。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: echo \"python -m pytest tests/test_ci_wiring.py\" | bash\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "管道给 shell 的 echo 载荷没有被报出来:%s" % problems)


def test_the_checker_does_not_report_a_pipeline_that_does_not_reach_a_shell(tmp_path):
    """**放行侧**:`awk '{print $1}' f.txt | sort` ⇒ **不该**判红。

    **为什么**:管道右侧**不是 shell** ⇒ 那条管道**不执行**任何东西
    ⇒ 「只读文本」这个保证成立 ⇒ 不能误伤。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 排序\n"
        "        run: awk '{print $1}' f.txt | sort\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "不通向 shell 的管道被误判了:%s" % problems)


def test_the_checker_does_not_report_a_shell_pipeline_without_a_dangerous_word(
        tmp_path):
    """**放行侧**:`echo hello | bash`(载荷里没有 pytest/闸门)⇒ **不该**判红。

    **为什么**:这条判据只管「**载荷里提到 pytest/闸门**」这件事
    ⇒ 不许把它变成「见到管道就判红」。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 打招呼\n"
        "        run: echo hello | bash\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("认不出来" in problem for problem in problems), (
        "没有危险词的管道被误判了:%s" % problems)


# --- H-4:第三方动作必须**逐个登记** ---
def _third_party_workflow_dir(tmp_path: Path, uses_line: str) -> Path:
    """造一个只含一条 `uses:` 的 workflow 目录,返回那个目录。"""
    workflows = tmp_path / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "probe.yml").write_text(
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "jobs:\n"
        "  probe:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: probe\n"
        "        uses: %s\n" % uses_line,
        encoding="utf-8")
    return workflows


def test_the_checker_reports_an_unregistered_third_party_action(tmp_path):
    """**拦截侧**:`uses:` 一个**没登记**的第三方动作 ⇒ 必须报出来。

    **为什么**(第 55 轮之十二,codex H-4):守卫此前**只扫 `run:`**
    ⇒ `uses:` 这一整类入口**完全不在扫描面内**
    ⇒ 外部动作可以在 YAML 里**连 `pytest` 字样都不带**而执行任意代码
    ⇒ 「闸门跑了但没跑成」与「闸门压根没跑」在 CI 看上去一模一样。
    """
    workflows = _third_party_workflow_dir(tmp_path, "evil/pytest-action@v1")
    problems = wiring.check_third_party_actions(workflows)
    assert any("第三方动作" in problem and "未登记" in problem
               for problem in problems), (
        "没登记的第三方动作没有被报出来:%s" % problems)


def test_the_checker_allows_a_registered_third_party_action(tmp_path):
    """**放行侧**:`uses: actions/checkout@v4`(在登记名单里)⇒ **不该**判红。"""
    workflows = _third_party_workflow_dir(tmp_path, "actions/checkout@v4")
    problems = wiring.check_third_party_actions(workflows)
    assert problems == [], (
        "已登记的第三方动作被误判了:%s" % problems)


def test_the_repository_workflows_only_use_registered_third_party_actions():
    """**放行侧**:仓库自己的每一个 `uses:` 都必须**已登记**。

    **为什么**:名单本身也会过期 ⇒ 这一条让「名单漏了仓库已有的动作」
    在 CI 上当场变红(否则新规则只能挡住**将来**的第三方动作)。
    """
    problems = wiring.check_third_party_actions(ROOT / ".github" / "workflows")
    assert problems == [], (
        "仓库自己的 workflow 里有未登记的第三方动作:%s" % problems)


# --- M-1:只读命令里**提到**闸门 ≠ 在跑闸门 ---
def test_the_checker_does_not_treat_an_inert_mention_of_the_gate_as_a_gate_step(
        tmp_path):
    """**放行侧**:`echo "闸门是 ci_gate.py"` ⇒ **不该**把那个 job 当闸门 job。

    **为什么**(第 55 轮之十二,codex M-1):只读命令里**提到**闸门 ≠ **在跑**它
    ⇒ 此前一律当成闸门步骤 ⇒ 那个 job 被当成「闸门 job」
    ⇒ 它带的 job 级 `env:` 被**误伤**(误伤也是失效)。
    """
    minimal = (
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "jobs:\n"
        "  docs:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      FOO: bar\n"
        "    steps:\n"
        "      - name: docs\n"
        "        run: echo \"闸门是 ci_gate.py\"\n"
    )
    problems = _problems(_write_workflow(tmp_path, minimal))
    assert not any("job 带了 `env:`" in problem for problem in problems), (
        "只读命令里提到闸门 ⇒ 那个 job 的 job 级 env: 被误伤了:%s" % problems)


def test_the_checker_still_reports_a_hidden_gate_call_in_a_job_with_env(
        tmp_path):
    """**拦截侧**:`bash -c "python tools/ci_gate.py"` ⇒ 该 job 的 job 级 `env:`
    **必须**报。

    **为什么**:第 55 轮之十二只把**只读命令**豁免掉
    ⇒ 「认不出来 ⇒ 提到就算」这条 fail-closed 纪律**原样保留**
    ⇒ 否则「豁免」就变成了新的免检通道。
    """
    minimal = (
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "jobs:\n"
        "  rogue:\n"
        "    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      PATH: /tmp/evil:$PATH\n"
        "    steps:\n"
        "      - name: 藏起来的闸门\n"
        "        run: bash -c \"python tools/ci_gate.py\"\n"
    )
    problems = _problems(_write_workflow(tmp_path, minimal))
    assert any("job 带了 `env:`" in problem for problem in problems), (
        "藏起来的闸门调用所在 job 的 job 级 env: 没有被报出来:%s" % problems)


# --- M-2:引号/前缀形态的捕获**也是**捕获 ---
def test_the_checker_does_not_report_a_quoted_capture_as_a_swallowed_failure(
        tmp_path):
    """**放行侧**:`|| rc="$?"` ⇒ **不该**报「失败被吃掉」。

    **为什么**(第 55 轮之十二,codex M-2):`rc="$?"` 与 `rc=$?` **同义**
    ⇒ 此前只认后者 ⇒ 等价的合法写法被**误伤**(误伤也是失效)。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_B,
        '          out=$(python tools/check_domain.py --diff'
        ' "origin/${{ github.base_ref || \'main\' }}" 2>&1) || rc="$?"\n')
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert not any("被吃掉" in problem for problem in problems), (
        "带引号的捕获被误判成「失败被吃掉」:%s" % problems)

# ==========================================================================
# 第 55 轮之十四:codex 第 2 轮的 6 条发现 ⇒ 10 条规则 + 14 条探针
#
# **每条发现都先自己复现过**才动手改的 ⇒ 下面每条探针都对着**一条具体的**
# 规则,不是对着「工具报了红」这件事。
#
# **纪律**(沿用前面那几条):
#   - 每条新判据都配**双向**探针:拦截侧(必须被抓)+ 放行侧(必须放行);
#   - 断言「**是哪条规则**判红的」,不是「判红了」——
#     兜底消息(「认不出来 ⇒ 提到 pytest/闸门就判红」)与具体规则的结论
#     **可能长一样** ⇒ 只断言「报了红」⇒ 见证用例就**空转**了
#     (仓库教训:**空转的测试比没有测试更糟**)。
# ==========================================================================


def _reported_commands(problems: list[str]) -> list[str]:
    """从「认不出来 ⇒ 判红」这类结论里,取出**被报出来的命令原文**。

    **为什么要取原文**:兜底消息与具体规则的结论**后缀一样**
    ⇒ 只断言「报了红」分不清是哪条规则干的
    ⇒ 断言**原文的开头**就能区分:
      - 兜底(分词失败)报的是**碎片**(例如 `python -m pytest tests/"}'`);
      - 规则报的是**整条命令**(例如 `awk '…'` / `bash -c "…"`)。
    """
    out: list[str] = []
    for problem in problems:
        if "判红):" in problem:
            out.append(problem.split("判红):", 1)[1].strip())
    return out


def _local_action_repo(tmp_path: Path, uses: str, action_rel: str | None = None,
                       action_body: str | None = None) -> Path:
    """搭一个「仓库」:`.github/workflows/probe.yml` 用 `uses:` 引一个本地动作。

    **为什么要 `.github/workflows/`**:本地动作的路径按**仓库根**解析
    (GitHub 的规矩)⇒ 守卫那边 `root = workflows_dir.parent.parent`
    ⇒ 这里必须是 `<root>/.github/workflows/` 才算同一个仓库。
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "probe.yml").write_text(
        "on:\n"
        "  push:\n"
        "    branches: [main]\n"
        "  pull_request:\n"
        "jobs:\n"
        "  probe:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: probe\n"
        "        uses: %s\n" % uses,
        encoding="utf-8")
    if action_rel is not None and action_body is not None:
        target = tmp_path / action_rel
        target.mkdir(parents=True, exist_ok=True)
        (target / "action.yml").write_text(action_body, encoding="utf-8")
    return workflows


# --- R14-1(C-1a):仓库里**不存在**的本地动作 ⇒ 判红 ---
def test_the_checker_reports_a_local_action_whose_definition_is_missing(tmp_path):
    """**拦截侧**:`uses: ./evil` 而仓库里**没有** `evil/action.yml` ⇒ 判红。

    **为什么**(第 55 轮之十四,codex C-1a):此前 `uses: ./…` **无条件放行**
    ⇒ 而扫描面只有 `.github/actions/**/action.yml`
    ⇒ 仓库里**别处**的本地动作**完全逃出扫描面**
    ⇒ 那个动作里的命令**没人看** ⇒ 闸门压根没跑而 CI 看上去是绿的。
    """
    workflows = _local_action_repo(tmp_path, "./evil")
    problems = wiring.check_third_party_actions(workflows)
    assert any("本地动作" in problem and "action.yml" in problem
               for problem in problems), (
        "仓库里不存在的本地动作没有被报出来:%s" % problems)


# --- R14-2(C-1b):本地动作**不是复合动作** ⇒ 判红 ---
def test_the_checker_reports_a_local_action_that_is_not_composite(tmp_path):
    """**拦截侧**:本地动作的 `runs.using` 是 `node20` ⇒ 判红。

    **为什么**(第 55 轮之十四,codex C-1b):node/docker 动作的代码
    **不在**本工具的扫描面内(它是一个 `main:` 脚本)⇒ 它的 `run:` 块
    没有 ⇒ 那些命令**没人看**⇒ 认不出来 ⇒ 按 fail-closed 判红。
    """
    workflows = _local_action_repo(
        tmp_path, "./evil", "evil",
        "name: evil\n"
        "runs:\n"
        "  using: node20\n"
        "  main: index.js\n")
    problems = wiring.check_third_party_actions(workflows)
    assert any("runs.using" in problem and "node20" in problem
               for problem in problems), (
        "非复合的本地动作没有被报出来:%s" % problems)


# --- R14-3(C-1c):本地**复合**动作的 `run:` 块受同一套纪律 ---
def test_the_checker_scans_the_run_blocks_of_a_local_composite_action(tmp_path):
    """**拦截侧**:本地复合动作里**跑 pytest** ⇒ 判红。

    **为什么**(第 55 轮之十四,codex C-1c):复合动作是 GitHub 官方推荐的
    复用方式 ⇒ 别的 workflow 引用它 ⇒ 研究侧依赖在那儿**跑不起来**
    ⇒ 闸门在别处跑 ⇒「闸门压根没跑」在 CI 看上去**一模一样**。
    """
    workflows = _local_action_repo(
        tmp_path, "./evil", "evil",
        "name: evil\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: bash\n"
        "      run: python -m pytest -p no:cacheprovider -q"
        " tests/test_research_boundary.py\n")
    problems = wiring.check_third_party_actions(workflows)
    assert any("跑 pytest" in problem and "复合动作" in problem
               for problem in problems), (
        "本地复合动作里的 pytest 没有被报出来:%s" % problems)


# --- R14-3 的**放行侧**:干净的本地复合动作不许判红 ---
def test_the_checker_allows_a_clean_local_composite_action(tmp_path):
    """**放行侧**:本地复合动作只做 `echo` ⇒ **不该**判红。

    **为什么**:第 55 轮之十四把本地动作**收进**扫描面
    ⇒ 若收得过宽(例如把任何本地动作都判红)
    ⇒ 那就是「一律报红」,等于没有判据(仓库教训:**误伤**也是失效)。
    """
    workflows = _local_action_repo(
        tmp_path, "./hello", "hello",
        "name: hello\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: bash\n"
        "      run: echo hello\n")
    problems = wiring.check_third_party_actions(workflows)
    assert not problems, (
        "干净的本地复合动作被误判了:%s" % problems)


# --- R14-10:复合动作里**分词失败** ⇒ 提到 pytest/闸门就判红(兜底不许失效) ---
def test_the_checker_reports_an_unparsable_block_inside_a_local_composite_action(
        tmp_path):
    """**拦截侧**:本地复合动作里有一条**分词失败**的命令 ⇒ 判红。

    **为什么**(第 55 轮之十四,codex H-2 的配套):此前复合动作那条路径
    有 `tokens is None ⇒ continue` ⇒ 整条命令**隐形**
    ⇒ 与 workflow 步骤那条路径(没有这个跳过)**口径分叉**
    ⇒ 「认不出来 ⇒ 提到 pytest/闸门就判红」这条**兜底**在这里失效
    ⇒ 那条兜底本来是**最后一道**防线。
    """
    workflows = _local_action_repo(
        tmp_path, "./evil", "evil",
        "name: evil\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - shell: bash\n"
        "      run: echo \"没闭合的引号 python tools/ci_gate.py\n")
    problems = wiring.check_third_party_actions(workflows)
    assert any("复合动作" in problem and "认不出来" in problem
               for problem in problems), (
        "复合动作里分词失败的命令没有被报出来:%s" % problems)


# --- R14-4(H-1):`exit` 的**等价写法**同样终止这个 shell ---
def test_the_checker_reports_a_self_guard_call_after_an_eval_that_exits(tmp_path):
    """**拦截侧**:分档器冒烟写在 `eval "exit 0"` **之后** ⇒ 必须报出来。

    **为什么**(第 55 轮之十四,codex H-1):此前只认**字面** `exit`
    ⇒ `eval "exit 0"` / `builtin exit 0` / `command exit 0`
    这些**等价**写法**漏过** ⇒ 裁决者那一步压根走不到
    ⇒ 而守卫照样报「自检本身有人跑」⇒ 分档器的关卡等于没有。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        '          eval "exit 0"\n' + SELF_GUARD_CALL_A)
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "写在 `eval \"exit 0\"` 之后的分档器冒烟没有被报出来:%s" % problems)


def test_the_checker_reports_a_self_guard_call_after_a_builtin_exit(tmp_path):
    """**拦截侧**:分档器冒烟写在 `builtin exit 0` **之后** ⇒ 必须报出来。

    **为什么**(同上):`builtin exit 0` 与 `exit 0` **同义**
    (同一个内置命令,只是换了写法)⇒ 只认字面写法 ⇒ 漏过。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          builtin exit 0\n" + SELF_GUARD_CALL_A)
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("不算数" in problem for problem in problems), (
        "写在 `builtin exit 0` 之后的分档器冒烟没有被报出来:%s" % problems)


def test_the_checker_still_counts_a_self_guard_call_after_an_eval_that_does_not_exit(
        tmp_path):
    """**放行侧**:`eval "echo hi"` **不终止** shell ⇒ 之后的调用**算数**。

    **为什么**(同上):把「`eval` 后面带 `exit`」当成「`eval` 就终止」
    ⇒ 那是**过宽**的判据 ⇒ 合法写法被**误伤**(误伤也是失效)。
    ⇒ 所以必须有这条放行侧探针盯着。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        '          eval "echo hi"\n' + SELF_GUARD_CALL_A)
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert not any("不算数" in problem or "没有" in problem
                   for problem in problems), (
        "跟在 `eval \"echo hi\"` 后面的分档器冒烟被误判成死代码:%s" % problems)


# --- R14-5(H-2):awk 的 co-process(`| "命令"`)能把**任意命令**执行掉 ---
def test_the_checker_reports_an_awk_coprocess_command(tmp_path):
    """**拦截侧**:`awk 'BEGIN{print "x" | "python -m pytest …"}'` ⇒ 判红。

    **为什么**(第 55 轮之十四,codex H-2):awk 把 `|` 右侧那个**字符串**
    当**命令**执行 ⇒ 与 `| sh` **等价** ⇒ 它**不是**「只读文本」
    ⇒ 此前它被当成只读检索而**免检** ⇒ 藏一条 pytest 进去
    ⇒ 闸门那一步被换掉而 CI 照样绿。

    **断言「报出来的命令原文以 `awk` 开头」**:这样才分得清
    「**整条** awk 命令被认出」与「分词失败后报了个**碎片**」——
    后者是兜底在干活,不是这条规则在干活(仓库教训:**见证不许空转**)。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: awk 'BEGIN{print \"x\" | \"python -m pytest"
        " tests/test_ci_wiring.py\"}'\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any(command.startswith("awk") for command in _reported_commands(problems)), (
        "awk 的 co-process 没有被当成一条命令报出来:%s" % problems)


# --- R14-6(H-2b):**引号里**的 `|` 不是管道 ⇒ 不许在那里切分 ---
def test_the_checker_reports_a_quoted_pipe_as_part_of_one_command(tmp_path):
    """**拦截侧**:`bash -c "echo a | python -m pytest …"` ⇒ 判红且是**整条**。

    **为什么**(第 55 轮之十四,codex H-2 的另一半):此前 `|` **一律**切分
    ⇒ 引号里的 `|` 也被切 ⇒ 两半都**引号不配对** ⇒ 分词失败
    ⇒ 整条命令**碎成两段**⇒ 那条 co-process **完全隐形**
    ⇒ 而「切碎」这个方向本来是**安全**的(第 54 轮的教训)
    ⇒ **例外**就在这里:引号里的 `|` 是 **awk 的 co-process 语法**
    ⇒ 切碎它就等于**看不见**它。
    ⇒ 断言「报出来的原文以 `bash` 开头」⇒ 分词失败报的是碎片(`python …`)
    ⇒ 这条断言在旧行为下**会红**。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: bash -c \"echo a | python -m pytest tests/test_ci_wiring.py\"\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any(command.startswith("bash") for command in _reported_commands(problems)), (
        "引号里的 `|` 把命令切碎了 ⇒ co-process 隐形:%s" % problems)


# --- R14-7(H-3):`rg --pre=COMMAND` 是 `--pre COMMAND` 的**等价写法** ---
def test_the_checker_reports_an_rg_pre_with_an_equals_sign(tmp_path):
    """**拦截侧**:`rg --pre='python -m pytest …' …` ⇒ 判红。

    **为什么**(第 55 轮之十四,codex H-3):此前只认**独立的** `--pre` token
    ⇒ `rg --pre=…` 这种**等价写法**被当成「只读检索」⇒ **漏过**。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: rg --pre='python -m pytest tests/test_ci_wiring.py'"
        " -n pattern tests/\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "rg --pre= 里的预处理器命令没有被报出来:%s" % problems)


# --- R14-8(M-1):`sed` 的 `e` **命令**(不是 `e` **标志**)也能执行 ---
def test_the_checker_reports_a_sed_e_command(tmp_path):
    """**拦截侧**:`sed -e 'e python -m pytest …'` ⇒ 判红。

    **为什么**(第 55 轮之十四,codex M-1):GNU sed 支持 `e [COMMAND]`
    ⇒ 那个**内联命令**会被执行掉 ⇒ 此前只认 `s/x/y/e` 那个**标志位**
    ⇒ **命令**形态漏过 ⇒ 而 ADR 把这条残余写成「载荷必须来自输入文件」
    ⇒ **描述低估了实际可执行形态**(仓库教训㉝:「洞」与「描述」分开验收)。
    """
    mutated = _insert_step(
        REAL_TEXT,
        "      - name: 藏 pytest\n"
        "        run: sed -e 'e python -m pytest tests/test_ci_wiring.py' f.txt\n")
    problems = _problems(_write_workflow(tmp_path, mutated))
    assert any("认不出来的命令里提到了 pytest/闸门" in problem
               for problem in problems), (
        "sed 的 `e` 命令没有被报出来:%s" % problems)


# --- R14-9(M-2):here-doc 的**正文是数据,不是命令** ---
def test_the_checker_reports_a_self_guard_call_inside_a_heredoc_body(tmp_path):
    """**拦截侧**:分档器冒烟写在 **here-doc 的正文**里 ⇒ 必须报出来。

    **为什么**(第 55 轮之十四,codex M-2):shell 解析器此前**不认识**
    here-doc ⇒ 正文被逐行当成**命令**解析 ⇒ 正文里写一条
    `python tools/check_domain.py …` 就被当成「裁决者真的跑了」
    ⇒ 而实际上**什么命令都没跑** ⇒ 分档器的关卡等于没有。
    ⇒ 正确结论:被守的 workflow 里**没有**任何裁决者冒烟调用。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          cat <<'CLAUDE_EOF'\n"
        "          python tools/check_domain.py scripts/optimize.py\n"
        "          CLAUDE_EOF\n")
    mutated = mutated.replace(SELF_GUARD_CALL_B, "          rc=0\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert any("没有" in problem or "不算数" in problem for problem in problems), (
        "写在 here-doc 正文里的分档器冒烟被当成了真调用:%s" % problems)


def test_the_checker_still_counts_a_self_guard_call_beside_a_heredoc(tmp_path):
    """**放行侧**:here-doc **旁边**还有一条真调用 ⇒ 那条**算数**。

    **为什么**(同上):把 here-doc 的**结束符**认错(比如连着两行都当正文)
    ⇒ 真调用被一起吞掉 ⇒ 误伤 ⇒ 所以必须有放行侧探针盯着。
    """
    mutated = REAL_TEXT.replace(
        SELF_GUARD_CALL_A,
        "          cat <<'CLAUDE_EOF'\n"
        "          这里只是一段文本\n"
        "          CLAUDE_EOF\n")
    assert mutated != REAL_TEXT, "锚点没命中:workflow 改了写法?先改这里"
    problems = wiring.check_self_guard_present([_write_workflow(tmp_path, mutated)])
    assert not any("没有" in problem or "不算数" in problem
                   for problem in problems), (
        "here-doc 旁边的真调用被误吞了:%s" % problems)
