#!/usr/bin/env python
"""纪律守卫的变异验证脚手架:对**守卫自己**做定点变异,断言每个变异都被抓住。

**为什么要有这个脚本**(codex 第 2 轮 L6):第 46~47 轮那些"变异必须变红"的结论
是**手工跑一遍**记在 ADR 里的——"写下了但没人跑"等于没有防线(仓库自己的教训)。
本脚本把那批变异**变成可复算的**:每次改守卫后跑一遍,当场知道哪条规则松了。

**与 `tools/check_mutations.py` 的分工**:
- `check_mutations.py` 盯的是**后果域分档清单**(`tools/check_domain.py`);
- 本脚本盯的是**纪律守卫**(`tests/test_research_boundary.py` 与它守着的两份套件)
  ——"跳过守卫""改写守卫判据""把守卫用例从名单里摘掉"这一族。

**四条防「假绿」的要求**:
1. **变异必须真的改变判定**:每种"应该变红"的变异都必须让见证用例组真的红;
   一条都没红即**空操作**(等于没测)⇒ 脚本失败。
2. **反向对照**:另有一条**无害改动**(只改注释)的变异必须**保持绿**——
   证明脚本不是"一律报红"(那等于没判据)。
3. **基线必须全绿**:不变异的副本先跑一遍;有失败 ⇒ 环境不可用(退出码 2),
   不是"变异没被抓住"。
4. **收集错误与"没被抓住"要分开**:变异把文件改成语法错误 ⇒ pytest 收集阶段就炸,
   失败数是 0——那不是"守卫没抓住",是**变异规格写错了** ⇒ 退出码 2。

**为什么在副本里做**:变异要改的是工作树里的文件,来回改去风险太大
(中途出错就留下半个改坏的守卫)。先复制一份仓库到临时目录,真工作树全程不动。

用法:
    python tools/check_guard_mutations.py            # 跑全部变异,打印条数
    python tools/check_guard_mutations.py --verify   # 额外核对 ADR 表格里的变异集合

退出码:
    0 全部变异都符合预期(且 --verify 时 ADR 集合一致);
    1 有变异没被抓住 / 反向对照变红 / ADR 集合对不上;
    2 环境不可用或变异规格写错了(fail-closed)。
"""
from __future__ import annotations

import argparse
import marshal
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ADR 变异表的核对口径**两个脚本共用**(第 49 轮:此前两处各写一份,
# 同名函数口径不同——一处有反向断言、一处没有)。
sys.path.insert(0, str(ROOT / "tools"))
from check_mutations import (  # noqa: E402
    _purge_bytecode_cache,
    cleanup_temp_dir,
    copy_repo,
    run_outcome,
    verify_mutation_table,
)

BOUNDARY = "tests/test_research_boundary.py"
CLOSURE = "tests/test_fs_capability_closure.py"
TOOL_TEST = "tests/test_check_ci_ran.py"
# 第 54 轮:接线类变异改的是 **workflow 本身**(不是守卫源码)
WORKFLOW = ".github/workflows/research-guard.yml"
# 第 55 轮:接线自检工具**自己**的用例(接线这件事的见证用例在它里面)
WIRING = "tests/test_ci_wiring.py"

# 规范闸门命令在 workflow 里的**那一行**(第 55 轮的接线变异都锚在它上面)
GATE_LINE = ('        run: python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
             ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py')

# 闸门**整个步骤**(name + run):删掉它 = 名单里的套件没人跑
GATE_STEP = (
    "      - name: 研究侧边界不变量(批准唯一写入者 / Preset 边界 / 工具目录 / 不 import vnpy)\n"
    + GATE_LINE
    + "\n"
)

# 闸门所在的 job 的头部(job 级旁路键锚在它上面)
JOB_HEAD = "  research:\n    runs-on: ubuntu-latest"

# 第 55 轮之二(G8 复核时**实测**出来的洞):接线自检**自己**的调用
# (带 name + run + 命令,保证**唯一命中**——workflow 里有两处调用)。
WIRING_CALL = (
    "      - name: CI 接线自检(规范形 + 名单覆盖 + 自检自身在场)\n"
    "        run: |\n"
    "          python tools/check_ci_wiring.py"
)
# workflow 的触发器段(加过滤 ⇒ 不匹配的改动**不会触发**这个 workflow)
TRIGGER_BLOCK = "on:\n  push:\n    branches: [main]\n  pull_request:"

# 多行替换用拼接,不写转义序列——避免"源码里到底是几个反斜杠"这种歧义
# (本文件第一版就栽在这儿:转义写错 ⇒ 整个文件被替换成一行 ⇒ 变异"全红"但毫无意义)。
NL = chr(10)


def _lines(*parts: str) -> str:
    """把几行拼成一段(行间用真换行)。"""
    return NL.join(parts)


def _mutation(label, rel, edits, targets=None, expect="red"):
    """一条变异 = 一个标签 + 一组编辑。

    `edits` 是 [(锚点, 替换成的那几行), …]:一条变异有时要改**多处**
    (例:「用别名写法加 skipif」既要加 import,又要改装饰器)。
    """
    if isinstance(edits, tuple):
        edits = [edits]
    return {
        "label": label,
        "rel": rel,
        "edits": list(edits),
        "targets": targets or [BOUNDARY],
        "expect": expect,
    }


MUTATIONS = (
    # ---- 第 46 轮那一族:「闸门配好了,实际没跑」 ----
    _mutation(
        "在核心防线套件顶部加一行模块级 pytestmark 一刀切跳过",
        CLOSURE,
        ("requires_trading_stack = pytest.mark.skipif(",
         _lines('pytestmark = pytest.mark.skipif(True, reason="mutation")',
                "requires_trading_stack = pytest.mark.skipif(")),
    ),
    _mutation(
        "把模块级 pytestmark 包在 if 里(顶层扫描的老洞)",
        CLOSURE,
        ("requires_trading_stack = pytest.mark.skipif(",
         _lines("if True:",
                '    pytestmark = pytest.mark.skipif(True, reason="mutation")',
                "requires_trading_stack = pytest.mark.skipif(")),
    ),
    _mutation(
        "给一条没登记的用例加条件 skipif",
        CLOSURE,
        ("def test_a_harmless_typing_member_is_still_reachable():",
         _lines('@pytest.mark.skipif(os.name == "nt", reason="mutation")',
                "def test_a_harmless_typing_member_is_still_reachable():")),
    ),
    _mutation(
        "用别名写法给一条没登记的用例加 skipif",
        CLOSURE,
        [("requires_trading_stack = pytest.mark.skipif(",
          _lines("from pytest import skipif",
                 "requires_trading_stack = pytest.mark.skipif(")),
         ("def test_a_harmless_typing_member_is_still_reachable():",
          _lines('@skipif(os.name == "nt", reason="mutation")',
                 "def test_a_harmless_typing_member_is_still_reachable():"))],
    ),
    _mutation(
        "把登记过的平台跳过改成无条件跳过(skipif(True))",
        CLOSURE,
        ('@pytest.mark.skipif(os.name != "nt", reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")',
         '@pytest.mark.skipif(True, reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")'),
    ),
    _mutation(
        "把登记过的平台跳过改成 skipif(1)",
        CLOSURE,
        ('@pytest.mark.skipif(os.name != "nt", reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")',
         '@pytest.mark.skipif(1, reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")'),
    ),
    _mutation(
        "把登记过的平台跳过改成 skipif(condition=True)",
        CLOSURE,
        ('@pytest.mark.skipif(os.name != "nt", reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")',
         '@pytest.mark.skipif(condition=True, reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")'),
    ),
    _mutation(
        "给一条用例加裸 pytest.mark.skip",
        CLOSURE,
        ("def test_a_harmless_typing_member_is_still_reachable():",
         _lines('@pytest.mark.skip(reason="mutation")',
                "def test_a_harmless_typing_member_is_still_reachable():")),
    ),

    # ---- 标记纪律那一族 ----
    _mutation(
        "给一条不依赖交易栈的用例加上 @requires_trading_stack",
        CLOSURE,
        ("def test_a_harmless_typing_member_is_still_reachable():",
         _lines("@requires_trading_stack",
                "def test_a_harmless_typing_member_is_still_reachable():")),
    ),
    _mutation(
        "给一条『期待入口拒绝候选』的用例加上 @requires_trading_stack",
        CLOSURE,
        ("def test_forward_ref_evaluate_is_refused_by_the_static_screen(repo, tmp_path, monkeypatch):",
         _lines("@requires_trading_stack",
                "def test_forward_ref_evaluate_is_refused_by_the_static_screen(repo, tmp_path, monkeypatch):")),
    ),
    _mutation(
        "把 TRADING_STACK_MARKED 名单里的一条删掉",
        BOUNDARY,
        ('    "test_backtest_reads_market_data_before_the_candidate_is_constructed",',
         "    # (removed by mutation)"),
    ),
    _mutation(
        "把『没被包住』的判定反过来",
        BOUNDARY,
        ("    return any(not _is_failure_swallowing(call, parents)",
         "    return any(_is_failure_swallowing(call, parents)"),
    ),

    # ---- 「被包住」判定自己那一族(探针必须抓到) ----
    _mutation(
        # 标签里**可以**带 `*` 了(第 49 轮 codex M1):ADR 核对的归一化改成**逐格**
        # 且**只去反引号与成对的 `**`**,不去掉单个 `*`。
        # 此前是全局去星号 ⇒ `except*` 被削成 `except` ⇒ 这个标签只能绕开写
        # ("try-star 语法"),**掩盖了归一化的缺陷**,还把它固化成了「标签不许有星号」。
        "让『被包住』的判定不认 except* 语法",
        BOUNDARY,
        ('_TRY_NODES = (ast.Try, getattr(ast, "TryStar", ast.Try))',
         "_TRY_NODES = (ast.Try,)"),
    ),
    _mutation(
        "让『被包住』的判定不认裸名 raises",
        BOUNDARY,
        ('        return func.id in ("raises", "suppress", "ExitStack")',
         '        return func.id in ("suppress", "ExitStack")'),
    ),
    _mutation(
        "让『被包住』的判定不认 contextlib.suppress",
        BOUNDARY,
        ('        return func.attr in ("raises", "suppress", "ExitStack")',
         '        return func.attr in ("raises", "ExitStack")'),
    ),

    # ---- 收集钩子与名单那一族 ----
    _mutation(
        "在 conftest 里加一个改收集的钩子",
        "tests/conftest.py",
        ("import pytest",
         _lines("import pytest",
                "",
                "",
                "def pytest_collection_modifyitems(session, config, items):",
                "    # 静默摘掉核心防线套件里的用例——守卫自己看不见(CI 报告里压根没出现)",
                "    items[:] = [item for item in items",
                "                if 'fs_capability_closure' not in item.nodeid]")),
    ),
    _mutation(
        "把 CI 名单里的一条用例换成不存在的名字",
        "tests/guard_roster.json",
        ('      "test_the_evaluator_rule_catches_a_reexported_get_type_hints",',
         '      "__removed_by_mutation__",'),
    ),

    # ---- CI 侧闸门自己那一族 ----
    _mutation(
        "让 CI 闸门放行『名单点名的用例被跳过』",
        "tools/check_ci_ran.py",
        # 第 54 轮:锚点跟着工具的现状更新(此前是 `if _is_skipped(case):`,
        # 现在多了一个「允许跳过」的条件:`and name not in skip_ok`)。
        # 锚点写错 ⇒ 命中 0 次 ⇒ 脚手架判「变异规格写错了」(退出码 2),
        # **不是**「守卫有洞」——这个区分是脚手架 fail-closed 的关键。
        ("            for case in matched:" + NL +
         "                if _is_skipped(case) and name not in skip_ok:",
         "            for case in matched:" + NL +
         "                if False and _is_skipped(case) and name not in skip_ok:"),
        targets=[TOOL_TEST],
    ),
    _mutation(
        "让 CI 闸门不再把参数化后缀去掉(名单永远匹配不上)",
        "tools/check_ci_ran.py",
        ('    index = name.find("[")' + NL + "    return name[:index] if index >= 0 else name",
         '    index = name.find("[")' + NL + "    return name"),
        targets=[TOOL_TEST],
    ),
    _mutation(
        # 这一条要**改两处**:闸门里有两条独立的分支都能把「点名的那份不在名单里」判红
        # (①`if not args.allow_unlisted_suite` ②`elif args.suite and not wanted`)。
        # 只关掉其中一处,另一处仍然会红 ⇒ 变异没有真正把防线拆掉 ⇒ 那不算有效的变异。
        "让 CI 闸门放行『点名的那份不在名单里』",
        "tools/check_ci_ran.py",
        [("        if not args.allow_unlisted_suite:",
          "        if False and not args.allow_unlisted_suite:"),
         ("    elif args.suite and not wanted:",
          "    elif False and args.suite and not wanted:")],
        targets=[TOOL_TEST],
    ),

    # ---- 第 49 轮:「守卫自己的守卫」那一族 ----
    _mutation(
        # 让守卫的视野退回只剩那两份核心套件 ⇒ 三份「守卫工具自己」的用例
        # 不再被守卫看见(整份被跳过/用例被删掉都没人发现)。
        "让守卫的视野退回只剩两份核心套件",
        BOUNDARY,
        ("    return [ROOT / rel for rel in GUARD_SUITE_ROSTER]",
         "    return [ROOT / rel for rel in GUARD_SUITE_ROSTER[:2]]"),
    ),
    _mutation(
        # 把一份守卫套件在**名单**里的键改掉 ⇒ 名单与套件不再互相吻合。
        "把一份守卫套件从 CI 名单里改名",
        "tests/guard_roster.json",
        ('    "tests/test_check_guard_mutations.py": [',
         '    "__removed_by_mutation__": ['),
    ),

    # ---- 名单采集那一族(第 49 轮 codex M2) ----
    _mutation(
        "让名单的采集看不见条件块里的用例",
        BOUNDARY,
        ("                collect(node.body)" + NL +
         "                for extra in (getattr(node, \"orelse\", None), getattr(node, \"finalbody\", None)):",
         "                pass  # (removed by mutation)" + NL +
         "                for extra in (getattr(node, \"orelse\", None), getattr(node, \"finalbody\", None)):"),
    ),

    # 第 55 轮:接线族的**绿对照**——把规范闸门命令的解释器从 `python` 换成 `python3`
    # (规范形允许换解释器)⇒ 接线必须**照样合规**。
    # 证明的不是「workflow 改不改都会红」,而是「自检认的是**形态**不是字面」。
    _mutation(
        "把闸门命令的解释器从 python 换成 python3(无害改动)",
        WORKFLOW,
        ('        run: python tools/ci_gate.py --junitxml "$RUNNER_TEMP/union.xml"',
         '        run: python3 tools/ci_gate.py --junitxml "$RUNNER_TEMP/union.xml"'),
        targets=[WIRING],
        expect="green",
    ),

    # ---- 第 55 轮:CI 接线的**规范形**那一族 ----
    #
    # 这一族改的是 `.github/workflows/research-guard.yml` 本身。
    # 第 54 轮那 9 条接线变异锚在**旧写法**(两段 shell + `rm -f` + for 循环)上;
    # 第 55 轮改成**规范形**(一条命令)⇒ 锚点换了,变异也得跟着换。
    #
    # **为什么必须重写而不是删掉**:codex 第 2 轮的 C1 指出——
    # 「名单里那几份套件真的被 CI 跑着」此前**没有任何守卫**断言;
    # 而第 55 轮**本轮实测**又发现:把 pytest 与闸门**一起**藏进 `bash -c "…"`,
    # 纪律守卫**全部真空绿**(守卫只认它认得的形态)。
    # ⇒ 新的每一种「把接线改坏」都必须有一条**红变异**钉死见证用例。
    #
    # 这一族的见证用例统一是
    # `tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules`:
    # 它拿**真实的 workflow 文本**跑一遍接线自检,接线被改坏 ⇒ 它必红。
    _mutation(
        "把闸门命令藏进 bash -c(第 55 轮实测:旧守卫全部真空绿)",
        WORKFLOW,
        (GATE_LINE,
         '        run: bash -c "python tools/ci_gate.py --junitxml \\"$RUNNER_TEMP/boundary.xml\\"'
         ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py"'),
    ),
    _mutation(
        "把闸门命令藏进命令替换 $()",
        WORKFLOW,
        (GATE_LINE,
         '        run: out=$(python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
         ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py)'),
    ),
    _mutation(
        "把闸门命令藏进 shell 函数",
        WORKFLOW,
        (GATE_LINE,
         "        run: |\n"
         '          run_gate() { python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"'
         " --roster tests/guard_roster.json --suite tests/test_research_boundary.py; }\n"
         "          run_gate"),
    ),
    _mutation(
        "把闸门命令藏进 eval",
        WORKFLOW,
        (GATE_LINE,
         '        run: eval "python tools/ci_gate.py --junitxml \\"$RUNNER_TEMP/boundary.xml\\"'
         ' --roster tests/guard_roster.json --suite tests/test_research_boundary.py"'),
    ),
    _mutation(
        "把闸门命令换成直接跑 pytest(绕开闸门)",
        WORKFLOW,
        (GATE_LINE,
         "        run: python -m pytest -p no:cacheprovider -q tests/test_research_boundary.py"),
    ),
    _mutation(
        "用版本化解释器直接跑 pytest(绕开闸门)",
        WORKFLOW,
        (GATE_LINE,
         "        run: python3.12 -m pytest -p no:cacheprovider -q tests/test_research_boundary.py"),
    ),
    _mutation(
        "用运行器直接跑 pytest(绕开闸门)",
        WORKFLOW,
        (GATE_LINE,
         "        run: uv run python -m pytest -p no:cacheprovider -q tests/test_research_boundary.py"),
    ),
    _mutation(
        "在闸门命令后面接一个恒真后缀(判红也不算失败)",
        WORKFLOW,
        (GATE_LINE, GATE_LINE + " || true"),
    ),
    _mutation(
        "在闸门命令之后加 exit 0(强制判成功)",
        WORKFLOW,
        (GATE_LINE,
         "        run: |\n"
         '          python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"\n'
         "            --roster tests/guard_roster.json --suite tests/test_research_boundary.py\n"
         "          exit 0"),
    ),
    _mutation(
        "在闸门命令之前加 set +e",
        WORKFLOW,
        (GATE_LINE,
         "        run: |\n"
         "          set +e\n"
         '          python tools/ci_gate.py --junitxml "$RUNNER_TEMP/boundary.xml"\n'
         "            --roster tests/guard_roster.json --suite tests/test_research_boundary.py"),
    ),
    _mutation(
        "给闸门步骤加 continue-on-error: true(红了也不算数)",
        WORKFLOW,
        (GATE_LINE,
         "        continue-on-error: true\n" + GATE_LINE),
    ),
    _mutation(
        "给闸门步骤加 if: 空串(整步可以被跳过)",
        WORKFLOW,
        (GATE_LINE,
         '        if: ""\n' + GATE_LINE),
    ),
    _mutation(
        "给闸门步骤加 shell: bash(没有 errexit,闸门判红也不算数)",
        WORKFLOW,
        (GATE_LINE,
         "        shell: bash\n" + GATE_LINE),
    ),
    _mutation(
        "给整个 job 加 if: false(这一步压根不跑)",
        WORKFLOW,
        (JOB_HEAD,
         "  research:\n    if: false\n    runs-on: ubuntu-latest"),
    ),
    _mutation(
        "删掉整个闸门步骤(名单里的套件从此没人跑,而守卫自己也在那一步里)",
        WORKFLOW,
        (GATE_STEP, ""),
    ),
    _mutation(
        "把报告写进仓库里(而不是 $RUNNER_TEMP)",
        WORKFLOW,
        (GATE_LINE,
         '        run: python tools/ci_gate.py --junitxml "boundary.xml"'
         " --roster tests/guard_roster.json --suite tests/test_research_boundary.py"),
    ),

    # ---- 第 55 轮:接线自检工具**自己**的那一族 ----
    #
    # 接线自检本身是守卫,而守卫**自己也要有人守**(仓库教训)。
    # 这一族改的是 `tools/check_ci_wiring.py` 与 `tools/ci_gate.py` 的**判据**:
    # 把「认不出来 ⇒ 判红」「名单覆盖」「只有一条命令」「不许旁路键」
    # 「闸门自己核对名单」这几条**放松**掉,各自必须有一条**红变异**。
    #
    # 见证用例刻意**不参数化、名字全 ASCII**:pytest 的失败摘要行会把非 ASCII 的
    # 参数 id 转义成 `\uXXXX`,而脚手架靠 nodeid 钉见证 ⇒ 参数化用例的 nodeid
    # 在摘要里对不上(实测踩过)。
    _mutation(
        "让接线自检不再把『认不出来的命令』判红(fail-closed 被放松)",
        "tools/check_ci_wiring.py",
        ("                if any(word in command for word in _DANGEROUS_WORDS):\n"
         "                    stray.append(\"认不出来的命令里提到了 pytest/闸门(判不了 ⇒ 判红):%s\"",
         "                if False:\n"
         "                    stray.append(\"认不出来的命令里提到了 pytest/闸门(判不了 ⇒ 判红):%s\""),
    ),
    _mutation(
        "让接线自检不再核对名单覆盖(删掉闸门步骤也没人发现)",
        "tools/check_ci_wiring.py",
        ("    missing = sorted(roster_suites - covered)",
         "    missing = []"),
    ),
    _mutation(
        "让接线自检不再要求闸门步骤只有一条命令(多出来的命令不再报)",
        "tools/check_ci_wiring.py",
        ("        # --- 这一步是闸门步骤:规范形 + 不许带旁路键 ---\n"
         "        has_gate = True\n"
         "        if len(commands) != 1:",
         "        # --- 这一步是闸门步骤:规范形 + 不许带旁路键 ---\n"
         "        has_gate = True\n"
         "        if False:"),
    ),
    _mutation(
        "让接线自检不再禁止闸门步骤带旁路键(if/continue-on-error/shell)",
        "tools/check_ci_wiring.py",
        ('        for key in ("if", "continue-on-error", "shell"):\n'
         "            if key in step:\n"
         '                problems.append("闸门步骤「%s」带了 `%s:` ⇒ 这一步可以不出力/不算失败"\n',
         "        for key in ():\n"
         "            if key in step:\n"
         '                problems.append("闸门步骤「%s」带了 `%s:` ⇒ 这一步可以不出力/不算失败"\n'),
    ),
    _mutation(
        "让闸门工具不再核对名单(名单点名的用例没跑也不红)",
        "tools/ci_gate.py",
        ("        rc = check_ci_ran.main(gate_argv)\n"
         "        if rc != 0:\n"
         "            problems += 1",
         "        rc = 0\n"
         "        if rc != 0:\n"
         "            problems += 1"),
    ),
    _mutation(
        # 第 54 轮(codex M3 的延伸):结论行必须说清「点名核对的几条里,
        # 几条是**允许跳过**的」。第 53 轮那条 M5a(结论行不按路径自证)
        # 只活在 %TEMP% 里,仓库里**没有**——本条把它补进来(并且更强:
        # 不只分路径,还要分「必须真跑 / 允许跳过」)。
        "让结论行不说明「点名核对里的几条是允许跳过的」",
        "tools/check_ci_ran.py",
        ('    checked = ("点名核对 %d 条(必须真跑 %d 条,允许跳过 %d 条)"\n'
         "               % (len(wanted), must_count, allow_count))",
         '    checked = "点名核对 %d 条" % len(wanted)'),
        targets=[TOOL_TEST],
    ),

    # ---- 第 55 轮之二:G8 复核时**实测**出来的洞 ----
    #
    # 这一族是**复核阶段才发现**的:接线自检**自己**的调用**可以带参数**,
    # 而 `--workflow` 当时是**替换**语义 ⇒ 把调用改成
    # `python tools/check_ci_wiring.py --workflow <别处文件>`,
    # 被检查的就只剩那个文件 ⇒ **真实的两个 workflow 压根没被查**,
    # 而工具**照样报「[OK] CI 接线合规」**(探针实测:退出码 0)。
    #
    # **这就是「守卫可以被自己一键关掉,而没有任何用例会红」**——
    # 仓库反复吃过的亏:守卫自己悄悄没了 / 写下了但没人跑。
    #
    # 另有一条同族:`pull_request` 触发器带 `paths:` 过滤 ⇒
    # 不匹配的改动**根本不会触发**这个 workflow ⇒ 闸门压根没跑,
    # 而 CI 看上去是绿的。
    _mutation(
        "给接线自检的调用加上 --workflow 参数(守卫的检查范围可以被缩小)",
        WORKFLOW,
        (WIRING_CALL, WIRING_CALL + " --workflow .github/workflows/decoy.yml"),
        targets=[WIRING],
    ),
    _mutation(
        "给 pull_request 触发器加 paths: 过滤(闸门压根不会跑)",
        WORKFLOW,
        (TRIGGER_BLOCK, TRIGGER_BLOCK + "\n    paths: ['docs/**']"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再要求自检调用不带参数(守卫可以被自己收窄)",
        "tools/check_ci_wiring.py",
        ("            if not canonical_wiring(tokens, expected_script):",
         "            if False:"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再禁止 pull_request 触发器带过滤(闸门不跑也算合规)",
        "tools/check_ci_wiring.py",
        ("                if key in pull_request:",
         "                if False:"),
        targets=[WIRING],
    ),
    _mutation(
        # **第 55 轮之五**:名单结构校验已经搬到**共享的那一份**
        # (`tools/check_ci_ran.py::validate_roster`)⇒ 接线自检不再自己写一份。
        # ⇒ 这条变异改的必须是**共享实现**本身:它一松,接线自检也跟着不报
        # (症状在接线自检,根在共享实现)⇒ 见证用例钉在接线自检自己的用例上。
        "让共享的名单结构校验不再报「must_run 不是对象」(接线自检也跟着不报)",
        "tools/check_ci_ran.py",
        ("    if not isinstance(must_run, dict):\n"
         "        problems.append(\"名单里的 must_run 不是对象(是 %s)\" % type(must_run).__name__)",
         "    if False:\n"
         "        problems.append(\"名单里的 must_run 不是对象(是 %s)\" % type(must_run).__name__)"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再判「名单在仓库之外」(崩在 relative_to 上也算过)",
        "tools/check_ci_wiring.py",
        ("    try:\n        return roster_path.resolve().relative_to(ROOT).as_posix()\n"
         "    except ValueError:\n        return None",
         "    try:\n        return roster_path.resolve().relative_to(ROOT).as_posix()\n"
         "    except ValueError:\n        return \"\""),
        targets=[WIRING],
    ),

    # ---- 第 55 轮之四:宪法层那个**独立落点** ----
    #
    # 接线自检放在它所守的步骤里 ⇒ 那一步被删,守卫一起消失(第 55 轮 C1)。
    # 独立的落点放在**宪法 workflow** 里——而宪法层是「纯标准库 + git」,
    # 一个依赖都不装 ⇒ 它永远会跑 ⇒ 研究层那一处被删时,只有它能发现。
    #
    # 这一族守的是「那个独立落点**真的在做它该做的事**」:
    #   - 它会不会核对「研究 workflow 里的接线自检还在」;
    #   - 它认不认「非规范形的调用」(带选项 / 藏进 bash -c);
    #   - 它是不是真的只靠标准库(偷偷引入第三方包 ⇒ 那个层会因为装不上依赖而压根没跑);
    # 以及研究层那一侧:宪法层的守卫调用被删 / 宪法层的职责被删,研究层会不会发现。
    _mutation(
        "让宪法层不再核对「研究 workflow 里的接线自检还在」(独立落点变空壳)",
        "tools/check_constitution_history.py",
        ("    if canonical:\n        return []",
         "    if True:\n        return []"),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层不再判「接线自检的调用不是规范形」(检查范围可以被缩小)",
        "tools/check_constitution_history.py",
        # **第 55 轮之五**:规范形的判法改成了「脚本后面不许再有任何词」
        # (`_is_canonical_wiring_call` 的最后一行)⇒ 变异点跟着搬家。
        ("    return index == len(tokens) - 1",
         "    return True"),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层不再判「藏起来的调用」(bash -c 里也能躲过去)",
        "tools/check_constitution_history.py",
        ("        elif WIRING_SCRIPT_REL in cleaned and _normalize_script_path(cleaned) != WIRING_SCRIPT_REL:",
         "        elif False:"),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层偷偷依赖第三方包(独立落点会因为装不上依赖而压根没跑)",
        "tools/check_constitution_history.py",
        ("import ast\nimport json\n", "import ast\nimport json\nimport yaml\n"),
        targets=[WIRING],
    ),
    _mutation(
        "让研究层不再报「某个 workflow 里压根没有守卫调用」(独立落点可以被删)",
        "tools/check_ci_wiring.py",
        ("    if not found and not saw_wiring:",
         "    if False:"),
        targets=[WIRING],
    ),
    _mutation(
        "让研究层在宪法层的职责被删时不再 fail-closed(独立落点变空壳也没人发现)",
        "tools/check_ci_wiring.py",
        ("    if checker is None:\n"
         "        return [\"宪法层的工具里**没有** `check_ci_wiring_presence` ⇒ \"\n"
         "                \"独立落点的职责被删掉了(fail-closed)\"]",
         "    if checker is None:\n        return []"),
        targets=[WIRING],
    ),

    # ---- 第 55 轮之五:codex 第 2 轮的 C1 / C2 / H1 / H2 / M1 / L2 / L3 / L4 ----
    #
    # 这一族把 codex 第 2 轮指出的**每一个洞**都钉一条红变异:
    # 见证用例逐条对应(见 WITNESSES),不许共用一条(共用 ⇒ 走错原因的红也算数)。
    #
    # **注意**:见证用例里带参数的那几条**必须显式给 ASCII 的 id**
    # (`pytest.param(..., id="utf8")`)——pytest 的失败摘要行会把非 ASCII 的
    # 参数 id 转义成 `\uXXXX`,而脚手架靠 nodeid 钉见证 ⇒ 对不上。
    _mutation(
        # C2:闸门步骤上的 `env:` ⇒ 解释器可被换掉。
        "让接线自检不再报闸门步骤上的 env:",
        "tools/check_ci_wiring.py",
        ("        if \"env\" in step:\n            # **C2(codex 第 2 轮)**",
         "        if False and \"env\" in step:\n            # **C2(codex 第 2 轮)**"),
        targets=[WIRING],
    ),
    _mutation(
        # C2:闸门 **job** 上的 `env:`。
        "让接线自检不再报闸门 job 上的 env:",
        "tools/check_ci_wiring.py",
        ("            if \"env\" in job:\n"
         "                problems.append(\n"
         "                    \"闸门步骤「%s」所在的 job 带了 `env:` ⇒ 解释器/模块搜索路径\"",
         "            if False and \"env\" in job:\n"
         "                problems.append(\n"
         "                    \"闸门步骤「%s」所在的 job 带了 `env:` ⇒ 解释器/模块搜索路径\""),
        targets=[WIRING],
    ),
    _mutation(
        # C1:闸门 job 上的 `needs:` ⇒ 上游一被跳过,闸门也不跑。
        "让接线自检不再报闸门 job 上的 needs:",
        "tools/check_ci_wiring.py",
        ("            if \"needs\" in job:\n"
         "                problems.append(\n"
         "                    \"闸门步骤「%s」所在的 job 带了 `needs:` ⇒ 上游 job 一旦被跳过\"",
         "            if False and \"needs\" in job:\n"
         "                problems.append(\n"
         "                    \"闸门步骤「%s」所在的 job 带了 `needs:` ⇒ 上游 job 一旦被跳过\""),
        targets=[WIRING],
    ),
    _mutation(
        # C2:守卫**步骤**上的 `env:`。
        "让接线自检不再报守卫步骤上的 env:",
        "tools/check_ci_wiring.py",
        ("            if \"env\" in step:\n"
         "                problems.append(\"%s 里跑守卫的那一步带了 `env:` ⇒ 解释器/模块搜索路径\"",
         "            if False and \"env\" in step:\n"
         "                problems.append(\"%s 里跑守卫的那一步带了 `env:` ⇒ 解释器/模块搜索路径\""),
        targets=[WIRING],
    ),
    _mutation(
        # C2:守卫 **job** 上的 `env:`。
        "让接线自检不再报守卫 job 上的 env:",
        "tools/check_ci_wiring.py",
        ("            if \"env\" in job:\n"
         "                problems.append(\"%s 里跑守卫的那个 job 带了 `env:` ⇒ 解释器可以被换掉\"",
         "            if False and \"env\" in job:\n"
         "                problems.append(\"%s 里跑守卫的那个 job 带了 `env:` ⇒ 解释器可以被换掉\""),
        targets=[WIRING],
    ),
    _mutation(
        # C1:守卫 **job** 上的 `needs:`。
        "让接线自检不再报守卫 job 上的 needs:",
        "tools/check_ci_wiring.py",
        ("            if \"needs\" in job:\n                # **C1(codex 第 2 轮)**:守卫那个 job 挂了 `needs:` 指向一个",
         "            if False and \"needs\" in job:\n                # **C1(codex 第 2 轮)**:守卫那个 job 挂了 `needs:` 指向一个"),
        targets=[WIRING],
    ),
    _mutation(
        # job 级 `defaults.run.shell`(此前只查 workflow 级)。
        "让接线自检不再报 job 级 defaults.run.shell",
        "tools/check_ci_wiring.py",
        ("        job_defaults = job.get(\"defaults\")\n        if isinstance(job_defaults, dict):",
         "        job_defaults = job.get(\"defaults\")\n        if False and isinstance(job_defaults, dict):"),
        targets=[WIRING],
    ),
    _mutation(
        # L3:别的 workflow 里不许有闸门步骤(并集口径会让「把闸门挪走」糊弄过去)。
        "让接线自检不再禁止别的 workflow 里有闸门步骤",
        "tools/check_ci_wiring.py",
        ("        elif has_gate:\n            problems.append(\n"
         "                \"%s 里**不许**有闸门步骤 ⇒ 闸门只在 %s 里跑\"",
         "        elif False and has_gate:\n            problems.append(\n"
         "                \"%s 里**不许**有闸门步骤 ⇒ 闸门只在 %s 里跑\""),
        targets=[WIRING],
    ),
    _mutation(
        # L4:`run:` 不是字符串 ⇒ 报出来(此前抛栈 ⇒ 没有结论行)。
        "让接线自检不再报非字符串的 run:",
        "tools/check_ci_wiring.py",
        ("        if not isinstance(raw_run, str):\n            # **`run:` 不是字符串**",
         "        if False:\n            # **`run:` 不是字符串**"),
        targets=[WIRING],
    ),
    _mutation(
        # L2:同一条问题不许报两遍。
        # **第 55 轮之五之后**:结构上的重复源已经去掉(名单只解析一次)⇒
        # 这层是**兜底**;端到端场景里已经造不出「一模一样的两行」了
        # ⇒ 变异点必须落在**去重函数本身**,见证用例也必须是**直接测它**的那条。
        "让接线自检把同一条问题报两遍",
        "tools/check_ci_wiring.py",
        ("        if problem in seen:\n            continue",
         "        if False:\n            continue"),
        targets=[WIRING],
    ),
    _mutation(
        # H2:接线自检与闸门**共用**同一份名单结构校验
        # (口径分叉 ⇒ 接线自检报绿而闸门报红 ⇒ 前者是误导性的绿)。
        "让接线自检不再用共享的名单结构校验",
        "tools/check_ci_wiring.py",
        ("    problems.extend(_roster_structure_problems(data, roster_path))",
         "    pass  # (removed by mutation)"),
        targets=[WIRING],
    ),
    _mutation(
        # H2:闸门对坏名单必须 fail-closed(退出码 2),不许放行。
        "让闸门在名单结构不对时仍然放行",
        "tools/ci_gate.py",
        ("        print(\"[FAIL] 名单结构不对 ⇒ 点名核对做不了 ⇒ 退出码 2(fail-closed)\")\n"
         "        return 2",
         "        print(\"[FAIL] 名单结构不对 ⇒ 点名核对做不了 ⇒ 退出码 2(fail-closed)\")\n"
         "        return 0"),
        targets=[WIRING],
    ),
    _mutation(
        # M1(反向的一半):规范形**不再**接受取值型解释器选项(`-X utf8` / `-W error`)。
        "让规范形不再接受取值型的解释器选项",
        "tools/check_ci_wiring.py",
        ("        if token in _VALUE_INTERPRETER_FLAGS:\n"
         "            index += 2                       # 跳过选项**和它的值**\n"
         "            continue",
         "        if token in _VALUE_INTERPRETER_FLAGS:\n"
         "            break"),
        targets=[WIRING],
    ),
    _mutation(
        # M1(反向的另一半):规范形**不再**接受不取值的解释器选项(`-u` / `py -3`)。
        "让规范形不再接受不取值的解释器选项",
        "tools/check_ci_wiring.py",
        ("        if token in _NO_VALUE_INTERPRETER_FLAGS or re.match(r\"^-\\d+(\\.\\d+)*$\", token):\n"
         "            index += 1\n"
         "            continue",
         "        if token in _NO_VALUE_INTERPRETER_FLAGS or re.match(r\"^-\\d+(\\.\\d+)*$\", token):\n"
         "            break"),
        targets=[WIRING],
    ),

    # ---- 反向:无害改动必须**保持绿**(证明脚本不是"一律报红") ----
    _mutation(
        "只改守卫里的一个注释(无害改动)",
        BOUNDARY,
        ('"""「被包住」的判定**自己**也要有探针(第 47 轮之三,实测发现的洞)。',
         '"""「被包住」的判定**自己**也要有探针(第 47 轮之三,实测发现的洞;无害改动)。'),
        expect="green",
    ),
    _mutation(
        # 第 55 轮:第 54 轮那条反向对照的锚点(workflow 里的裸 pytest 行)已经没了
        # (接线改成了**规范形**,那行不存在了)⇒ 把**同一个意图**挪到新位置:
        # 「守卫不能认死字符串」现在落在 `tools/check_ci_wiring.py` 的解释器正则上——
        # 把它改写成**另一种写法**(仍认得出 python / python3 / python3.12),
        # 守卫套件必须**照样全绿**;若它变红,说明守卫变成了认死字符串。
        "把解释器的正则改写成另一种写法(守卫照样认得)",
        "tools/check_ci_wiring.py",
        ('_PYTHON_RE = re.compile(r"^(?:python(?:\\d+(?:\\.\\d+)*)?|py|pythonw)$")',
         '_PYTHON_RE = re.compile(r"^(?:python[0-9.]*|py|pythonw)$")'),
        targets=[WIRING],
        expect="green",
    ),

    # ---- 第 55 轮之六:claude 第 3 轮(C-1 / H-2 / M-2 / L-1 / L-2)
    #  ＋ codex 第 3 轮(H-1 尾部 `&` / H-2 受保护脚本 / M-1 未知顶层键
    #                     / M-2 `types:` 判效果 / M-3 相对路径归一化 / L1 覆盖计数)
    #
    # 这一族全部是**收紧型**规则(把此前漏掉的洞补上)⇒ 每一条都必须有
    # 一条**红变异**钉死**专属**见证用例。
    _mutation(
        "让接线自检不再报 workflow 级 env:",
        "tools/check_ci_wiring.py",
        ('    # (此前只禁了 step 级与 job 级 ⇒ workflow 级是个洞。)\n'
         '    if "env" in data:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "解释器/模块搜索路径可以被换掉',
         '    # (此前只禁了 step 级与 job 级 ⇒ workflow 级是个洞。)\n'
         '    if False:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "解释器/模块搜索路径可以被换掉'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检的存在性检查不再报 workflow 级 env:",
        "tools/check_ci_wiring.py",
        ('    # ⇒ 现在两边**共用同一句** ⇒ 去重生效 ⇒ 只报一遍。\n'
         '    if "env" in data:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "解释器/模块搜索路径可以被换掉',
         '    # ⇒ 现在两边**共用同一句** ⇒ 去重生效 ⇒ 只报一遍。\n'
         '    if False:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "解释器/模块搜索路径可以被换掉'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报步骤级 working-directory:",
        "tools/check_ci_wiring.py",
        ('        if "working-directory" in step:',
         '        if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 job 级 defaults.run.working-directory",
        "tools/check_ci_wiring.py",
        ('                if "working-directory" in run_defaults:\n'
         '                    problems.append(\n'
         '                        "job「%s」在 **job 级** defaults.run.working-directory 上"',
         '                if False and "working-directory" in run_defaults:\n'
         '                    problems.append(\n'
         '                        "job「%s」在 **job 级** defaults.run.working-directory 上"'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 job 级 container:(这一步整个跑在别人给的镜像里)",
        "tools/check_ci_wiring.py",
        ('        if _job_key_present(job, "container"):',
         '        if False and _job_key_present(job, "container"):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 job 级 services:(这一步整个跑在别人给的容器里)",
        "tools/check_ci_wiring.py",
        ('        if _job_key_present(job, "services"):',
         '        if False and _job_key_present(job, "services"):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 $GITHUB_ENV/$GITHUB_PATH(给后续所有步骤换解释器)",
        "tools/check_ci_wiring.py",
        ('        for marker in ("GITHUB_ENV", "GITHUB_PATH"):\n'
         '            if marker in raw_run:\n'
         '                problems.append(\n'
         '                    "%s 的步骤「%s」(%s)提到了 `$%s` ⇒ 它能**给后续所有步骤**"',
         '        for marker in ("GITHUB_ENV", "GITHUB_PATH"):\n'
         '            if False and marker in raw_run:\n'
         '                problems.append(\n'
         '                    "%s 的步骤「%s」(%s)提到了 `$%s` ⇒ 它能**给后续所有步骤**"'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 push 触发器上的 paths 过滤(不匹配的提交压根不触发)",
        "tools/check_ci_wiring.py",
        ('            if key in push:',
         '            if False and key in push:'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再扫复合动作(.github/actions/**/action.yml 里的闸门步骤)",
        "tools/check_ci_wiring.py",
        (_lines('    # **复合动作**(第 55 轮之七):`.github/actions/**/action.yml`。',
                '    actions_dir = workflows_dir.parent / "actions"',
                '    if actions_dir.is_dir():'),
         _lines('    # **复合动作**(第 55 轮之七):`.github/actions/**/action.yml`。',
                '    actions_dir = workflows_dir.parent / "actions"',
                '    if False and actions_dir.is_dir():')),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再认模块形态的闸门调用(python -m tools.ci_gate)",
        "tools/check_ci_wiring.py",
        ('    return _script_identity(script).replace("/", ".").rsplit(".", 1)[-1].lower()',
         '    return _script_identity(script).rsplit("/", 1)[-1].lower()'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再要求受保护脚本那一步带 --verify(ADR 的变异表双向核对不再被 CI 执行)",
        "tools/check_ci_wiring.py",
        ('            if _protected_requirement(protected) is not None:',
         '            if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层不再按归一化路径认接线自检的调用(两层口径漂移)",
        "tools/check_constitution_history.py",
        ('    if _normalize_script_path(tokens[index]) != WIRING_SCRIPT_REL:',
         '    if tokens[index] != WIRING_SCRIPT_REL:'),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层不再认连写的取值选项(-Xutf8)(两层口径漂移)",
        "tools/check_constitution_history.py",
        ('        if any(token.startswith(flag) and len(token) > len(flag)\n'
         '               for flag in _INTERPRETER_VALUE_FLAGS):\n'
         '            index += 1                      # 连写形式:`-Werror` / `-Xutf8`\n'
         '            continue\n',
         '        if False:\n'
         '            index += 1\n'
         '            continue\n'),
        targets=[WIRING],
    ),
    _mutation(
        "让名单结构不对时的退出码回到 1(判不了与失败混为一谈)",
        "tools/check_ci_ran.py",
        ('        print("[FAIL] 名单结构不对 ⇒ 点名核对做不了 ⇒ 退出码 2(fail-closed)")\n'
         '        return 2',
         '        print("[FAIL] 名单结构不对 ⇒ 点名核对做不了 ⇒ 退出码 2(fail-closed)")\n'
         '        return 1'),
        targets=[TOOL_TEST],
    ),
    _mutation(
        "让接线自检不再管分档器冒烟那一步的旁路键(裁决者的关卡可以被轻轻放过)",
        "tools/check_ci_wiring.py",
        ('        if self_guard is not None:\n'
         '            for key in ("if", "continue-on-error", "shell", "env"):\n'
         '                if key in step:\n'
         '                    problems.append(\n'
         '                        "分档器冒烟(%s)那一步带了 `%s:` ⇒ 它可以不算数"',
         '        if self_guard is not None:\n'
         '            for key in ("if", "continue-on-error", "shell", "env"):\n'
         '                if False and key in step:\n'
         '                    problems.append(\n'
         '                        "分档器冒烟(%s)那一步带了 `%s:` ⇒ 它可以不算数"'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报分档器冒烟所在 job 的 job 级旁路键(整个 job 可以不出力)",
        "tools/check_ci_wiring.py",
        ('            for key in ("if", "continue-on-error", "env", "needs"):\n'
         '                if key in job:\n'
         '                    problems.append(\n'
         '                        "分档器冒烟(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"',
         '            for key in ("if", "continue-on-error", "env", "needs"):\n'
         '                if False and key in job:\n'
         '                    problems.append(\n'
         '                        "分档器冒烟(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线层的 workflow 级 env 文案与形状检查的不一样(同一条问题报两遍)",
        "tools/check_ci_wiring.py",
        ('    if "env" in data:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "解释器/模块搜索路径可以被换掉(实测:提交一个 `exit 0` 的 python "\n'
         '                        "放在 PATH 前面 ⇒ 闸门没跑而工具照样报「[OK]」)" % workflow.name)\n'
         '\n'
         '    found = False',
         '    if "env" in data:\n'
         '        problems.append("%s 在 **workflow 级**带了 `env:` ⇒ 它被所有 job/step 继承 ⇒ "\n'
         '                        "守卫自己的解释器也可以被换掉 ⇒ 守卫压根没跑而工具照样报「[OK]」"\n'
         '                        % workflow.name)\n'
         '\n'
         '    found = False'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报 workflow 级 defaults.run.working-directory",
        "tools/check_ci_wiring.py",
        ('        if isinstance(run_defaults, dict) and "working-directory" in run_defaults:\n'
         '            # **H-2(claude 第 3 轮)**:换掉工作目录 ⇒ 脚本路径/相对路径全变。',
         '        if False:\n'
         '            # **H-2(claude 第 3 轮)**:换掉工作目录 ⇒ 脚本路径/相对路径全变。'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再扫别的 workflow 文件里的闸门步骤",
        "tools/check_ci_wiring.py",
        ('    if base in ("ci_gate", "check_ci_ran"):',
         '    if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让名单校验不再报空条目(一份套件被点名却一条用例都没点名)",
        "tools/check_ci_ran.py",
        ('            elif not names:\n'
         '                # **空列表**(第 55 轮之六,claude 第 3 轮的 L-1):',
         '            elif False:\n'
         '                # **空列表**(第 55 轮之六,claude 第 3 轮的 L-1):'),
        targets=[WIRING],
    ),
    _mutation(
        "让名单校验不再报未知的顶层键",
        "tools/check_ci_ran.py",
        ('        if not isinstance(key, str) or (key not in known_top and not key.startswith("_")):',
         '        if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让规范形不再接受连写的取值选项(-Werror / -Xutf8)",
        "tools/check_ci_wiring.py",
        ('        if any(token.startswith(flag) and len(token) > len(flag)\n'
         '               for flag in _VALUE_INTERPRETER_FLAGS):',
         '        if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报闸门命令尾部的 &(退出码被丢弃)",
        "tools/check_ci_wiring.py",
        ('        if has_background_operator(raw_run):\n'
         '            # **H1(codex 第 3 轮)**:尾部 `&` ⇒ 闸门被丢到后台、退出码被丢弃',
         '        if False:\n'
         '            # **H1(codex 第 3 轮)**:尾部 `&` ⇒ 闸门被丢到后台、退出码被丢弃'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报受保护脚本所在 job 的 job 级旁路键",
        "tools/check_ci_wiring.py",
        ('            for key in ("if", "continue-on-error", "env", "needs"):\n'
         '                if key in job:\n'
         '                    problems.append(\n'
         '                        "受保护脚本(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"',
         '            for key in ("if", "continue-on-error", "env", "needs"):\n'
         '                if False and key in job:\n'
         '                    problems.append(\n'
         '                        "受保护脚本(%s)所在的 job 带了 `%s:` ⇒ 这一步可以不算数"'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再管受保护脚本(变异验证那两步可以被 continue-on-error 废掉)",
        "tools/check_ci_wiring.py",
        ('        if protected is not None:',
         '        if False:'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再按效果判 types:(少了 synchronize 也不报)",
        "tools/check_ci_wiring.py",
        ('                missing = sorted({"opened", "synchronize"} - type_set)',
         '                missing = []'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再归一化相对脚本路径(./tools/ci_gate.py 被误判成非规范形)",
        "tools/check_ci_wiring.py",
        ('    if _normalize_script(script) != "tools/ci_gate.py":',
         '    if script != "tools/ci_gate.py":'),
        targets=[WIRING],
    ),
    _mutation(
        "让不存在的套件也被算成「已覆盖」(覆盖判定被一个根本不存在的文件顶掉)",
        "tools/check_ci_wiring.py",
        ('            else:\n'
         '                # 只有**合法且确实存在**的套件才计入「已覆盖」\n'
         '                # (第 55 轮之六,codex 第 3 轮的 L1:此前 `covered.add` 在\n'
         '                #  `if/elif` **之外** ⇒ 不存在的套件也被算成「已覆盖」)。\n'
         '                covered.add(suite)',
         '            else:\n'
         '                # 只有**合法且确实存在**的套件才计入「已覆盖」\n'
         '                # (第 55 轮之六,codex 第 3 轮的 L1:此前 `covered.add` 在\n'
         '                #  `if/elif` **之外** ⇒ 不存在的套件也被算成「已覆盖」)。\n'
         '                covered.add(suite)\n'
         '            covered.add(suite)'),
        targets=[WIRING],
    ),
    # ---- 第 55 轮之十:codex 第 5 轮实测出的七条通道 ----
    #
    # 这一族改的是 `tools/check_ci_wiring.py` 与 `tools/check_constitution_history.py`
    # **自己**的判据。codex 第 5 轮在**仓库副本**上实测出七条漏过的通道
    # (模块形态的受保护脚本 / awk|sed|find|git 能执行 / 复合动作不受管 /
    # `set +eo pipefail` / 退出码不一致 / `Container:` 大写 / 转义的 `&`),
    # 外加宪法层把**行尾注释与重定向**误判成「非规范形」⇒ 两层一起红。
    _mutation(
        "让接线自检查不到模块形态的受保护脚本(它就不再要求 --verify 了)",
        "tools/check_ci_wiring.py",
        ("        if name == base:\n"
         "            return required",
         "        if False and name == base:\n"
         "            return required"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检把 awk 当成不执行类命令(它能 system())",
        "tools/check_ci_wiring.py",
        ('    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "cat", "head",',
         '    "echo", "printf", "grep", "egrep", "fgrep", "rg", "ag", "cat", "awk", "head",'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报复合动作里的 $GITHUB_ENV(它能给后续所有步骤换解释器)",
        "tools/check_ci_wiring.py",
        ("    for index, raw_run in enumerate(blocks):\n"
         "        for marker in (\"GITHUB_ENV\", \"GITHUB_PATH\"):\n"
         "            if marker in raw_run:",
         "    for index, raw_run in enumerate(blocks):\n"
         "        for marker in (\"GITHUB_ENV\", \"GITHUB_PATH\"):\n"
         "            if False and marker in raw_run:"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再报复合动作里认不出来的命令(藏一条 pytest 进去)",
        "tools/check_ci_wiring.py",
        ("            if any(word in command for word in _DANGEROUS_WORDS):\n"
         "                problems.append(\n"
         "                    \"复合动作 %s 的第 %d 个 `run:` 块里有认不出来的命令\"",
         "            if False and any(word in command for word in _DANGEROUS_WORDS):\n"
         "                problems.append(\n"
         "                    \"复合动作 %s 的第 %d 个 `run:` 块里有认不出来的命令\""),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再认出 set +eo 关掉 errexit",
        "tools/check_ci_wiring.py",
        ('        if arg.startswith("+") and "e" in arg[1:]:\n'
         "            return True",
         '        if False and arg.startswith("+") and "e" in arg[1:]:\n'
         "            return True"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检在名单判不了时返回 1 而不是 2(三个消费者退出码不一致)",
        "tools/check_ci_wiring.py",
        ("        for problem in unique_problems(list(roster_problems) + problems):\n"
         "            print(\"  - %s\" % problem)\n"
         "        return 2",
         "        for problem in unique_problems(list(roster_problems) + problems):\n"
         "            print(\"  - %s\" % problem)\n"
         "        return 1"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检只认小写的 job 级 container 键(大写的 Container: 就不报了)",
        "tools/check_ci_wiring.py",
        ("    return any(str(name).lower() == key for name in job)",
         "    return any(str(name) == key for name in job)"),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检把转义的 & 也当成后台操作符(合法写法被误伤)",
        "tools/check_ci_wiring.py",
        ("        if _BACKGROUND_RE.search(_remove_quoted(_remove_escaped(line))):",
         "        if _BACKGROUND_RE.search(_remove_quoted(line)):"),
        targets=[WIRING],
    ),
    _mutation(
        "让宪法层不再剥掉行尾注释(它把合法写法判成非规范形)",
        "tools/check_constitution_history.py",
        ("        tokens = shlex.split(text, comments=True, posix=True)",
         "        tokens = shlex.split(text, comments=False, posix=True)"),
        targets=[WIRING],
    ),
    # ---- 第 55 轮之八:旁路**键**纪律拦不住的三条通道(实测) ----
    _mutation(
        "让接线自检不再管分档器那一步里关掉的 errexit(分档器崩了也不红)",
        "tools/check_ci_wiring.py",
        ('            if _errexit_disabled(raw_run):',
         '            if False and _errexit_disabled(raw_run):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再管分档器调用后面的「或真」后缀(失败被吃掉)",
        "tools/check_ci_wiring.py",
        ('                if _OR_TRUE_RE.search(stripped):',
         '                if False and _OR_TRUE_RE.search(stripped):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再要求裁决者冒烟关卡存在(整个步骤删掉也没人发现)",
        "tools/check_ci_wiring.py",
        ('        problems.append(\n            "被守的 workflow 里**没有**任何裁决者冒烟调用"\n            "(`tools/check_domain.py`)⇒ 分档器的关卡压根不在 CI 里 "\n            "⇒ 它被改坏、或者被整个删掉,都不会有人发现")',
         '        pass  # 变异:不再要求裁决者冒烟关卡存在'),
        targets=[WIRING],
    ),
    # ---- 第 55 轮之九:「失败被吃掉」的**其它形态** + 存在性口径收紧 ----
    _mutation(
        "让接线自检不再管分档器调用失败被吃掉的其它形态(失败照样被吃掉)",
        "tools/check_ci_wiring.py",
        ('            if _SWALLOW_RE.search(stripped):',
         '            if False and _SWALLOW_RE.search(stripped):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再管分档器调用换行接后缀的续行(失败照样被吃掉)",
        "tools/check_ci_wiring.py",
        ('            elif _continuation_swallows(lines, index):',
         '            elif False and _continuation_swallows(lines, index):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再管分档器调用被包在条件里(失败不会让这一步变红)",
        "tools/check_ci_wiring.py",
        ('            elif depth > 0 or _CONDITIONAL_RE.match(stripped):',
         '            elif False and (depth > 0 or _CONDITIONAL_RE.match(stripped)):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再要求本 workflow 自己带裁决者冒烟调用(拿别的 workflow 的假调用顶掉)",
        "tools/check_ci_wiring.py",
        ('        if _self_guard_calls(workflow):\n'
         '            continue',
         '        if any(_self_guard_calls(w) for w in workflows):'
         '  # 变异:不再要求本 workflow 自己带调用\n'
         '            continue'),
        targets=[WIRING],
    ),

    # ---- 第 55 轮之十一:子代理复核实测出的 C-1~C-7 / H-1~H-6 / M-1 / M-2 ----

    _mutation(
        "让接线自检不再把 pytest.__main__ 归一成 pytest(它真的跑 pytest)",
        "tools/check_ci_wiring.py",
        (
            '    if module.endswith(".__main__"):',
            '    if False and module.endswith(".__main__"):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再报复合动作里跑的 pytest(闸门在别处跑而 CI 是绿的)",
        "tools/check_ci_wiring.py",
        (
            '            if kind == "pytest":',
            '            if False and kind == "pytest":'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再报别的 workflow 里跑的 pytest",
        "tools/check_ci_wiring.py",
        (
            '                    if tokens is not None and command_kind(tokens, command) == "pytest":',
            '                    if False and tokens is not None and command_kind(tokens, command) == "pytest":'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检把只跑 --help 的裁决者调用也当成关卡(它不做任何分档)",
        "tools/check_ci_wiring.py",
        (
            '    if all(arg in ("-h", "--help") for arg in args):',
            '    if False and all(arg in ("-h", "--help") for arg in args):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再管写在 exit 之后的裁决者调用(那是死代码)",
        "tools/check_ci_wiring.py",
        (_lines('        if depth == 0:',
                '            for _operator, text in _split_segments(line):',
                '                if _segment_terminates_shell(text):',
                '                    dead = True'),
         _lines('        if depth == 0:',
                '            for _operator, text in _split_segments(line):',
                '                if False and _segment_terminates_shell(text):',
                '                    dead = True')),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再管从未被调用的函数体里的裁决者调用",
        "tools/check_ci_wiring.py",
        (
            '        elif _FUNCTION_DEF_END_RE.match(stripped):',
            '        elif False and _FUNCTION_DEF_END_RE.match(stripped):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再把续行接起来判(失败被吃掉的形态漏过)",
        "tools/check_ci_wiring.py",
        (
            '        sources.append(line)\n        if buf.endswith("\\\\"):\n            buf = buf[:-1].strip()\n            continue',
            '        sources.append(line)\n        if buf.endswith("\\\\"):\n            buf = buf[:-1].strip()'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再管 eval 里的 set +e(它关掉了 errexit)",
        "tools/check_ci_wiring.py",
        (
            '            if _head_of(tokens) in _SCRIPT_STRING_HEADS and _SET_OFF_RE.search(text):',
            '            if False and _head_of(tokens) in _SCRIPT_STRING_HEADS and _SET_OFF_RE.search(text):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再把绝对路径的 true 后缀判成吃掉失败",
        "tools/check_ci_wiring.py",
        (
            '                if next_operator in ("||", ";", "|", "&"):',
            '                if next_operator in (";", "|", "&"):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检把 echo skipped 后缀也当成捕获(失败被吃掉却看不见)",
        "tools/check_ci_wiring.py",
        (
            '                    if _is_capture(next_text):',
            '                    if True:'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再报步骤级的 container 键",
        "tools/check_ci_wiring.py",
        (
            '            if any(str(name).lower() == key for name in step):',
            '            if False and any(str(name).lower() == key for name in step):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检只认规范形的闸门步骤(非规范形的 job 级攻击面被盖住)",
        "tools/check_ci_wiring.py",
        (
            '                    elif _gate_step_mentions(command) is not None:',
            '                    elif False and _gate_step_mentions(command) is not None:'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检按基名认受保护脚本(仓库根放个同名空壳就能顶掉证据链)",
        "tools/check_ci_wiring.py",
        (
            '    if _normalize_script(script) != _normalize_script(expected):',
            '    if False and _normalize_script(script) != _normalize_script(expected):'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检不再管复合动作里的裁决者冒烟",
        "tools/check_ci_wiring.py",
        (
            '        if "check_domain" in raw_run:',
            '        if False and "check_domain" in raw_run:'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检把能执行的 sed 形态也当成不执行类命令",
        "tools/check_ci_wiring.py",
        (
            '    if head == "sed":\n        return not _sed_executes(tokens, command)',
            '    if head == "sed":\n        return True'),
        targets=[WIRING],
    ),

    _mutation(
        "让接线自检被单行的 if 分支骗到(后面的调用全被当成在条件里)",
        "tools/check_ci_wiring.py",
        (
            '        if (_CONDITIONAL_OPEN_RE.match(stripped)\n                and not _CONDITIONAL_CLOSE_ANY_RE.search(stripped)):\n            # **单行的 `if …; then …; fi`**(第 55 轮之十一,子代理复核的 M-2):',
            '        if _CONDITIONAL_OPEN_RE.match(stripped):\n            # **单行的 `if …; then …; fi`**(第 55 轮之十一,子代理复核的 M-2):'),
        targets=[WIRING],
    ),
    _mutation(
        "让接线自检不再按 token 找 set(条件里的 set +e 认不出来)",
        "tools/check_ci_wiring.py",
        (
            '                if token == "set" and _flags_disable_errexit(tokens[index + 1:]):',
            '                if False and token == "set" and _flags_disable_errexit(tokens[index + 1:]):'),
        targets=[WIRING],
    ),

    # ---- 第 55 轮之十二:codex 第 6 轮的 H-1 / H-2 / H-3 / H-4 与 M-1 / M-2 ----
    #
    # 这一族把 codex 第 6 轮指出的**每一个新洞**都钉一条红变异:
    # 见证用例逐条对应(见 WITNESSES),**一条变异一条见证**(共用 ⇒ 走错原因的红也算数)。
    _mutation(
        # H-1:同一逻辑行里 `exit 0` **之后**的调用是**死代码**
        # ⇒ 裁决者那一步压根走不到 ⇒ 而守卫此前恒把它当成活调用。
        "让接线自检不再把同一逻辑行里 exit 之后的调用当死代码",
        "tools/check_ci_wiring.py",
        (
            '                if _segment_terminates_shell(text):\n'
            '                    break',
            '                if False and _segment_terminates_shell(text):\n'
            '                    break'),
        targets=[WIRING],
    ),
    _mutation(
        # H-2:`sed` 的 `e` 标志在**不引号**的形态下就认不出来了
        # ⇒ `sed -e s/a/pytest/e x.txt` 能把 pytest 藏进去。
        "让接线自检认不出不引号的 sed e 标志",
        "tools/check_ci_wiring.py",
        (
            "      [a-z0-9]* e [a-z0-9]*",
            "      [a-z0-9]* x [a-z0-9]*"),
        targets=[WIRING],
    ),
    _mutation(
        # H-3A:`rg --pre` 会**执行**预处理器 ⇒ 它不是「只读文本」。
        "让接线自检把 rg --pre 当成只读命令",
        "tools/check_ci_wiring.py",
        (
            '    if head == "rg" and any(token == "--pre" or token.startswith("--pre=")\n'
            '                              for token in tokens):',
            '    if False and head == "rg" and any(token == "--pre" or token.startswith("--pre=")\n'
            '                              for token in tokens):'),
        targets=[WIRING],
    ),
    _mutation(
        # H-3B / 第 55 轮之十三:**管道给 shell** ⇒ 能执行
        # (原先这条判据只写在 `awk` 分支里 ⇒ 只对 awk 生效
        #  ⇒ 且作用在**切完 `|` 之后**的片段上 ⇒ **永远匹配不到**
        #  ⇒ 整条判据是**空转**的 ⇒ 变异脚手架打红才发现。)
        "让接线自检不再认管道给 shell",
        "tools/check_ci_wiring.py",
        (
            "    if _PIPE_TO_SHELL_RE.search(command):\n"
            "        return False",
            "    if False and _PIPE_TO_SHELL_RE.search(command):\n"
            "        return False"),
        targets=[WIRING],
    ),
    _mutation(
        # H-4:`uses:` 一步能执行**任意代码** ⇒ 第三方动作必须**逐个登记**。
        "让接线自检不再逐个登记第三方动作",
        "tools/check_ci_wiring.py",
        (
            "    if uses in _ALLOWED_THIRD_PARTY_ACTIONS:\n"
            "        return []",
            "    if True:  # 变异:第三方动作不再逐个登记\n"
            "        return []"),
        targets=[WIRING],
    ),
    _mutation(
        # M-1:只读命令里**提到**闸门 ≠ **在跑**闸门。
        "让接线自检把只读命令里提到的闸门也算成闸门步骤",
        "tools/check_ci_wiring.py",
        (
            "        if _is_inert_command(tokens, command):\n"
            "            return None\n"
            "        if any(word in command for word in (\"check_ci_ran\", \"ci_gate\")):",
            "        if any(word in command for word in (\"check_ci_ran\", \"ci_gate\")):"),
        targets=[WIRING],
    ),
    _mutation(
        # 第 55 轮之十三之二:`sed` 的标志位判据**必须认分隔符**。
        # 放宽回「引号里有 s … e … 就算」的老写法 ⇒
        # `sed 's/pytest/e/'`(那个 `e` 是**替换文本**)被误判成「可执行」
        # ⇒ **误伤方向** ⇒ 放行侧探针必须变红。
        #
        # **这一版的写法是被脚手架实测换掉的**(第 55 轮之十三之三):
        # 上一版把分隔符**写死成 `/`** ⇒ 实测**不命中** `sed 's/pytest/e/'`
        # ⇒ 见证不红 ⇒ **那条变异是空转的** ⇒ 改成**真正不分隔符**的形态
        # (连分隔符结构都没有)⇒ 两条放行侧探针**都**会红。
        "让接线自检把 sed 的标志位判据放宽回不分隔符的形态",
        "tools/check_ci_wiring.py",
        (
            "      [^'\"]* (?P=q) [^'\"]* (?P=q) [a-z0-9]* e [a-z0-9]* ['\"]",
            "      [^'\"]* e [^'\"]* ['\"]"),
        targets=[WIRING],
    ),
    _mutation(
        # 第 55 轮之十三之三:另一种「放宽」——把分隔符**写死成斜杠**
        # (而不是**回引用**同一个分隔符)⇒ `sed 's/.*/python -m pytest …/'`
        # 这种**只是写文本**的写法被误判成「可执行」⇒ **误伤方向**
        # ⇒ 那条放行侧探针必须变红。
        "让接线自检把 sed 的分隔符写死成斜杠",
        "tools/check_ci_wiring.py",
        (
            "      [^'\"]* (?P=q) [^'\"]* (?P=q) [a-z0-9]* e [a-z0-9]* ['\"]",
            "      [^'\"]* / [^'\"]* / [^'\"]* e [^'\"]* ['\"]"),
        targets=[WIRING],
    ),
    _mutation(
        # 第 55 轮之十三:「管道给 shell」这条判据**依赖**逻辑行**不在 `|` 处切**。
        # 切了 ⇒ 管道右侧那个 shell 看不见 ⇒ `awk '…' | sh`(管道在引号**外**)
        # 那种形态**又漏过** ⇒ 拦截侧探针必须变红。
        "让接线自检又在管道处把逻辑行切开",
        "tools/check_ci_wiring.py",
        (
            '            if not pipe_to_shell and not in_single and not in_double:',
            '            if not in_single and not in_double:'),
        targets=[WIRING],
    ),
    _mutation(
        # M-2:`rc="$?"` / `rc='$?'` / `export rc=$?` 与 `rc=$?` **同义**
        # ⇒ 只认最后一种 ⇒ 等价的合法写法被误判成「失败被吃掉」。
        "让接线自检认不出带引号的退出码捕获",
        "tools/check_ci_wiring.py",
        (
            "_CAPTURE_RE = re.compile(\n"
            "    r\"^(?:(?:export|local|readonly|typeset)\\s+|declare\\s+(?:-\\w+\\s+)*)?\"\n"
            "    r\"[A-Za-z_][A-Za-z0-9_]*=(?:\\\"\\$\\?\\\"|'\\$\\?'|\\$\\?)$\")",
            "_CAPTURE_RE = re.compile(r\"^[A-Za-z_][A-Za-z0-9_]*=\\$\\?$\")"),
        targets=[WIRING],
    ),
    # ---- 第 55 轮之十四:codex 第 2 轮的 6 条发现 ⇒ 10 条规则 ----
    #
    # **每条发现都先自己复现过**(见 %TEMP%/r64a_verify_codex.py)才动手改的。
    # **一条变异一条见证**(共用 ⇒ 走错原因的红也算数)。
    _mutation(
        # R14-1(C-1a):仓库里**不存在**的本地动作 ⇒ 判红。
        # 此前 `uses: ./…` **无条件放行** ⇒ 而扫描面只有
        # `.github/actions/**/action.yml` ⇒ 仓库里**别处**的本地动作
        # **完全逃出扫描面** ⇒ 那个动作里的命令**没人看**。
        "让接线自检放行仓库里不存在的本地动作",
        "tools/check_ci_wiring.py",
        (
            '    if not found:\n'
            '        return ["%s 用了**本地动作** `%s` ⇒ 仓库里没有 `%s/action.yml`"\n'
            '                " ⇒ 认不出来 ⇒ 判红" % (where, uses, rel)]',
            '    if not found:\n'
            '        return []'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-2(C-1b):本地动作**不是复合动作** ⇒ 判红。
        # node/docker 动作的代码**不在**本工具的扫描面内
        # ⇒ 它的 `run:` 块没有 ⇒ 那些命令**没人看**。
        "让接线自检放行非复合的本地动作",
        "tools/check_ci_wiring.py",
        (
            '    if using != "composite":\n'
            '        return ["本地动作 %s 的 `runs.using` 是 `%s` ⇒ 它的代码**不在**本工具的"\n'
            '                "扫描面内(node/docker 动作能执行任意代码)⇒ 判红"\n'
            '                % (action.name, using)]',
            '    if using != "composite":\n'
            '        return []'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-3(C-1c):本地**复合**动作的 `run:` 块受同一套纪律。
        # 复合动作是 GitHub 官方推荐的复用方式 ⇒ 别的 workflow 引用它
        # ⇒ 研究侧依赖在那儿**跑不起来** ⇒ 闸门在别处跑。
        "让接线自检不扫本地复合动作的 run 块",
        "tools/check_ci_wiring.py",
        (
            '    return _composite_action_problems(action, data)',
            '    return []'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-4(H-1):`exit` 的**等价写法**同样终止这个 shell。
        # `eval "exit 0"` / `builtin exit 0` / `command exit 0`
        # ⇒ 裁决者那一步压根走不到 ⇒ 而守卫照样报「自检本身有人跑」。
        "让接线自检认不出 eval 里的 exit",
        "tools/check_ci_wiring.py",
        (
            '    if head in ("eval", "builtin", "command"):\n'
            '        return bool(_EXIT_WORD_RE.search(text))',
            '    if False and head in ("eval", "builtin", "command"):\n'
            '        return bool(_EXIT_WORD_RE.search(text))'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-5(H-2):awk 的 **co-process**(`| "命令"`)能把**任意命令**执行掉
        # ⇒ 它**不是**「只读文本」⇒ 不该给它免检通道。
        "让接线自检把 awk 的 co-process 当成只读命令",
        "tools/check_ci_wiring.py",
        (
            '        if _awk_has_coprocess(tokens):\n'
            '            return False',
            '        if False and _awk_has_coprocess(tokens):\n'
            '            return False'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-6(H-2b):**引号里**的 `|` 不是管道(那是 awk 的 co-process 语法)
        # ⇒ 在那儿切碎 ⇒ 两半都**引号不配对** ⇒ 分词失败
        # ⇒ 整条命令碎掉 ⇒ co-process **完全隐形**。
        "让接线自检在引号里也把逻辑行切开",
        "tools/check_ci_wiring.py",
        (
            '            if not pipe_to_shell and not in_single and not in_double:',
            '            if not pipe_to_shell:'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-7(H-3):`rg --pre=COMMAND` 是 `--pre COMMAND` 的**等价写法**
        # ⇒ 只认独立的 `--pre` token ⇒ 等价写法**漏过**。
        "让接线自检认不出 rg --pre= 这种写法",
        "tools/check_ci_wiring.py",
        (
            '    if head == "rg" and any(token == "--pre" or token.startswith("--pre=")\n'
            '                              for token in tokens):',
            '    if head == "rg" and any(token == "--pre"\n'
            '                              for token in tokens):'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-8(M-1):`sed` 的 `e` **命令**(不是 `e` **标志**)也能执行
        # ⇒ GNU sed 的 `e [COMMAND]` ⇒ 只认标志位 ⇒ **命令**形态漏过。
        "让接线自检认不出 sed 的 e 命令",
        "tools/check_ci_wiring.py",
        (
            '    if head == "sed":\n'
            '        return not _sed_executes(tokens, command)',
            '    if head == "sed":\n'
            '        return _SED_EXEC_RE.search(command) is None'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-9(M-2):here-doc 的**正文是数据,不是命令**
        # ⇒ 正文里的裁决者调用被当成「真跑了」⇒ 而其实什么命令都没跑。
        "让接线自检把 here-doc 的正文当成命令",
        "tools/check_ci_wiring.py",
        (
            '    for raw in _strip_heredoc_bodies(script).splitlines():',
            '    for raw in script.splitlines():'),
        targets=[WIRING],
    ),
    _mutation(
        # R14-10:复合动作里**分词失败** ⇒ 提到 pytest/闸门就判红(兜底)
        # ⇒ 此前复合动作那条路径有 `tokens is None ⇒ continue`
        # ⇒ 整条命令**隐形** ⇒ 与 workflow 步骤那条路径**口径分叉**
        # ⇒ 而那条兜底本来是**最后一道**防线。
        "让接线自检在复合动作里跳过认不出来的命令",
        "tools/check_ci_wiring.py",
        (
            '        for command in logical_commands(raw_run):\n'
            '            tokens = _tokens(command)',
            '        for command in logical_commands(raw_run):\n'
            '            tokens = _tokens(command)\n'
            '            if tokens is None:\n'
            '                continue'),
        targets=[WIRING],
    ),
    # ---- 第 55 轮之十四之四:变异被**陈旧的 .pyc** 吃掉(变异空转) ----
    #
    # 这三条把「跑见证之前必须清掉字节码缓存」这条规则钉死:
    # 少了它,「同秒 + 同大小」的变异会被上一次编译出的 `.pyc` 顶掉
    # ⇒ 变异根本没生效 ⇒ 见证保持绿 ⇒ 看起来像「守卫有洞」(实际是空转)。
    #
    # **一条变异一条见证**:纪律那套与分档那套各有**专属**的拦截侧探针
    # ⇒ 不会「走错原因的红」(仓库教训:变异见证必须指向唯一那一条规则)。
    # (`targets` 只在**绿对照**时用;这三条是「应该变红」的变异 ⇒ 不写 targets。)
    _mutation(
        # 共享实现被掏空 ⇒ 两个脚手架都失去保护
        # 【第 55 轮之十四之五】锚点**搬过**:共享实现加了「找到几个/删掉几个」
        # 的计数与提醒(原锚点 `for cache in root.rglob(...)` 已不在)
        # ⇒ **规则还在就搬锚点,不许删变异**(仓库教训:变异规格漂移)。
        "不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)",
        "tools/check_mutations.py",
        (
            '    removed = 0\n'
            '    found = 0\n',
            '    return 0  # (mutation: 不再清字节码缓存)\n'
            '    removed = 0\n'
            '    found = 0\n'),
    ),
    _mutation(
        # 纪律守卫那套不再清 ⇒ 只有它自己的变异会空转
        "纪律守卫不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)",
        "tools/check_guard_mutations.py",
        (
            "    _purge_bytecode_cache(cwd)\n",
            "    pass  # (mutation: 不再清字节码缓存)\n"),
    ),
    _mutation(
        # 分档那套不再清 ⇒ 它的变异(全部作用在 tools/check_domain.py)会空转
        "分档守卫不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)",
        "tools/check_mutations.py",
        (
            "    _purge_bytecode_cache(cwd)\n",
            "    pass  # (mutation: 不再清字节码缓存)\n"),
    ),
    # ---- 第 55 轮之十四之五:清理动作**本身**的健壮性(codex 第 2 轮 Low-1~Low-3) ----
    #
    # 清理字节码缓存是**本轮修复的支点** ⇒ 它自己若「静悄悄地失败」,
    # 本轮修的坑就会**以更隐蔽的方式回来**(删不掉的没说 ⇒ 看起来像「守卫有洞」;
    # 环境变量让解释器压根不写缓存 ⇒ 拦截侧探针假红 ⇒ 误报成「变异被吃」)。
    # ⇒ 这几条把「清理要么做对、要么出声」钉死。**一条变异一条见证**。
    _mutation(
        # 删不掉的缓存**不再出声** ⇒ 防线无声失效
        "字节码缓存删不掉时不再出声",
        "tools/check_mutations.py",
        (
            '            except OSError as exc:    # 删不掉 ⇒ 防线可能**无声失效** ⇒ 必须出声\n'
            '                print("[提醒] 删不掉字节码缓存:%s(%s)⇒ 本轮可能仍被陈旧字节码影响"\n'
            '                      % (pyc, exc), file=sys.stderr)\n',
            '            except OSError:    # (mutation: 删不掉时不再出声)\n'
            '                pass\n'),
    ),
    _mutation(
        # 目录读不了 ⇒ 整体崩(而不是"不出声地跳过")
        "字节码缓存目录读不了时直接崩掉",
        "tools/check_mutations.py",
        (
            '    try:\n'
            '        caches = list(root.rglob("__pycache__"))\n'
            '    except OSError as exc:            # 目录不可读 ⇒ 不能当成「没有缓存」\n'
            '        print("[提醒] 遍历字节码缓存目录时出错:%s ⇒ 本次清理**不完整**" % exc,\n'
            '              file=sys.stderr)\n'
            '        caches = []\n',
            '    caches = list(root.rglob("__pycache__"))  # (mutation: 目录读不了就崩)\n'),
    ),
    _mutation(
        # 分档那套不再摘掉 PYTHONDONTWRITEBYTECODE ⇒ 拦截侧探针假红
        "分档守卫不再摘掉 PYTHONDONTWRITEBYTECODE",
        "tools/check_mutations.py",
        (
            '    env.pop("PYTHONDONTWRITEBYTECODE", None)\n',
            '    pass  # (mutation: 不再摘掉 PYTHONDONTWRITEBYTECODE)\n'),
    ),
    _mutation(
        # 纪律那套不再摘掉 PYTHONDONTWRITEBYTECODE ⇒ 拦截侧探针假红
        "纪律守卫不再摘掉 PYTHONDONTWRITEBYTECODE",
        "tools/check_guard_mutations.py",
        (
            '    env.pop("PYTHONDONTWRITEBYTECODE", None)\n',
            '    pass  # (mutation: 不再摘掉 PYTHONDONTWRITEBYTECODE)\n'),
    ),
)


# --------------------------------------------------------------------------
# 每条"应该变红"的变异**钉死的见证用例**(第 49 轮 codex M3)
# --------------------------------------------------------------------------
#
# **为什么必须有它**:此前只数"红了几条"⇒ **走错原因的红**也算数。
# 比如某条变异碰巧让**另一条**用例红了(或者名单过期让守卫用例本来就红),
# 脚本照样报"抓住了"——那是**假绿**,比没有变异验证更坏。
#
# 见证用例是**实测**出来的:对每条变异跑一遍,记下真正变红的 nodeid,
# 挑那条**为这条规则而红**的用例钉在这里(见 ADR 第 49 轮)。
#
# 钉死的好处:每条变异**只跑这一条**(而不是整份套件)⇒ CI 从 ~15 分钟降到 ~1 分钟;
# 而且"红了"必须**指名道姓**,不能只是"红了几条"。
WITNESSES: dict[str, str] = {
    # ---- 第 55 轮之十 ----
    "让接线自检查不到模块形态的受保护脚本(它就不再要求 --verify 了)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_protected_script_in_module_form_without_verify",
    "让接线自检把 awk 当成不执行类命令(它能 system())":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_awk_command_that_hides_pytest",
    "让接线自检不再报复合动作里的 $GITHUB_ENV(它能给后续所有步骤换解释器)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_composite_action_that_rewrites_the_environment",
    "让接线自检不再报复合动作里认不出来的命令(藏一条 pytest 进去)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_composite_action_that_hides_pytest",
    "让接线自检不再认出 set +eo 关掉 errexit":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_combined_errexit_switch",
    "让接线自检在名单判不了时返回 1 而不是 2(三个消费者退出码不一致)":
        "tests/test_ci_wiring.py::test_the_wiring_layer_exits_with_two_when_the_roster_is_malformed",
    "让接线自检只认小写的 job 级 container 键(大写的 Container: 就不报了)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_job_level_container_with_a_capital_key",
    "让接线自检把转义的 & 也当成后台操作符(合法写法被误伤)":
        "tests/test_ci_wiring.py::test_the_background_operator_check_ignores_an_escaped_ampersand",
    "让宪法层不再剥掉行尾注释(它把合法写法判成非规范形)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_accepts_a_wiring_call_with_a_trailing_comment",
    "在核心防线套件顶部加一行模块级 pytestmark 一刀切跳过":
        "tests/test_research_boundary.py::test_the_core_closure_suite_cannot_be_skipped_wholesale",
    "把模块级 pytestmark 包在 if 里(顶层扫描的老洞)":
        "tests/test_research_boundary.py::test_the_core_closure_suite_cannot_be_skipped_wholesale",
    "给一条没登记的用例加条件 skipif":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "用别名写法给一条没登记的用例加 skipif":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "把登记过的平台跳过改成无条件跳过(skipif(True))":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "把登记过的平台跳过改成 skipif(1)":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "把登记过的平台跳过改成 skipif(condition=True)":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "给一条用例加裸 pytest.mark.skip":
        "tests/test_research_boundary.py::test_the_core_closure_suite_only_skips_for_registered_reasons",
    "给一条不依赖交易栈的用例加上 @requires_trading_stack":
        "tests/test_research_boundary.py::test_the_trading_stack_marks_are_justified_and_never_blanket",
    "给一条『期待入口拒绝候选』的用例加上 @requires_trading_stack":
        "tests/test_research_boundary.py::test_the_trading_stack_marks_are_justified_and_never_blanket",
    "把 TRADING_STACK_MARKED 名单里的一条删掉":
        "tests/test_research_boundary.py::test_the_trading_stack_marks_match_the_registered_roster",
    "把『没被包住』的判定反过来":
        "tests/test_research_boundary.py::test_the_trading_stack_marks_are_justified_and_never_blanket",
    "让『被包住』的判定不认 except* 语法":
        "tests/test_research_boundary.py::test_the_failure_swallowing_detector_covers_every_wrapper_form",
    "让『被包住』的判定不认裸名 raises":
        "tests/test_research_boundary.py::test_the_failure_swallowing_detector_covers_every_wrapper_form",
    "让『被包住』的判定不认 contextlib.suppress":
        "tests/test_research_boundary.py::test_the_failure_swallowing_detector_covers_every_wrapper_form",
    "在 conftest 里加一个改收集的钩子":
        "tests/test_research_boundary.py::test_no_conftest_may_silently_drop_or_deselect_tests",
    "把 CI 名单里的一条用例换成不存在的名字":
        "tests/test_research_boundary.py::test_the_guard_roster_matches_the_suites_it_names",
    "让 CI 闸门放行『名单点名的用例被跳过』":
        "tests/test_check_ci_ran.py::test_a_named_test_that_was_skipped_fails",
    "让 CI 闸门不再把参数化后缀去掉(名单永远匹配不上)":
        "tests/test_check_ci_ran.py::test_a_parametrized_case_is_matched_by_its_function_name",
    "让 CI 闸门放行『点名的那份不在名单里』":
        "tests/test_check_ci_ran.py::test_a_suite_missing_from_the_roster_fails",
    "让守卫的视野退回只剩两份核心套件":
        "tests/test_research_boundary.py::test_the_guard_looks_at_every_suite_the_roster_names",
    "把一份守卫套件从 CI 名单里改名":
        "tests/test_research_boundary.py::test_the_guard_looks_at_every_suite_the_roster_names",
    "让名单的采集看不见条件块里的用例":
        "tests/test_research_boundary.py::test_the_name_collector_sees_a_conditionally_defined_test",
    # ---- 第 54 轮:CI 接线那一族(codex 第 2 轮 H1/H2/H3/H5) ----
    "把闸门命令藏进 bash -c(第 55 轮实测:旧守卫全部真空绿)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "把闸门命令藏进命令替换 $()":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "把闸门命令藏进 shell 函数":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "把闸门命令藏进 eval":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "把闸门命令换成直接跑 pytest(绕开闸门)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "用版本化解释器直接跑 pytest(绕开闸门)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "用运行器直接跑 pytest(绕开闸门)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "在闸门命令后面接一个恒真后缀(判红也不算失败)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "在闸门命令之后加 exit 0(强制判成功)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "在闸门命令之前加 set +e":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "给闸门步骤加 continue-on-error: true(红了也不算数)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "给闸门步骤加 if: 空串(整步可以被跳过)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "给闸门步骤加 shell: bash(没有 errexit,闸门判红也不算数)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "给整个 job 加 if: false(这一步压根不跑)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "删掉整个闸门步骤(名单里的套件从此没人跑,而守卫自己也在那一步里)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "把报告写进仓库里(而不是 $RUNNER_TEMP)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "让接线自检不再把『认不出来的命令』判红(fail-closed 被放松)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_hidden_gate_call",
    "让接线自检不再核对名单覆盖(删掉闸门步骤也没人发现)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_missing_roster_suite",
    "让接线自检不再要求闸门步骤只有一条命令(多出来的命令不再报)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_second_command_in_a_gate_step",
    "让接线自检不再禁止闸门步骤带旁路键(if/continue-on-error/shell)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_bypass_key_on_a_gate_step",
    "让闸门工具不再核对名单(名单点名的用例没跑也不红)":
        "tests/test_ci_wiring.py::test_the_gate_turns_red_when_the_named_case_did_not_run",
    "让结论行不说明「点名核对里的几条是允许跳过的」":
        "tests/test_check_ci_ran.py::test_the_conclusion_line_discloses_how_many_may_be_skipped",
    "给接线自检的调用加上 --workflow 参数(守卫的检查范围可以被缩小)":
        "tests/test_ci_wiring.py::test_the_wiring_check_is_called_without_arguments",
    "给 pull_request 触发器加 paths: 过滤(闸门压根不会跑)":
        "tests/test_ci_wiring.py::test_the_repository_workflow_satisfies_the_wiring_rules",
    "让接线自检不再要求自检调用不带参数(守卫可以被自己收窄)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_wiring_call_with_arguments",
    "让接线自检不再禁止 pull_request 触发器带过滤(闸门不跑也算合规)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_path_filtered_pull_request_trigger",
    "让共享的名单结构校验不再报「must_run 不是对象」(接线自检也跟着不报)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_roster_whose_must_run_is_not_an_object",
    "让接线自检不再判「名单在仓库之外」(崩在 relative_to 上也算过)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_roster_outside_the_repository",
    # ---- 第 55 轮之四:宪法层那个独立落点 ----
    "让宪法层不再核对「研究 workflow 里的接线自检还在」(独立落点变空壳)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_reports_a_missing_wiring_call",
    "让宪法层不再判「接线自检的调用不是规范形」(检查范围可以被缩小)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_reports_a_narrowed_wiring_call",
    "让宪法层不再判「藏起来的调用」(bash -c 里也能躲过去)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_reports_a_hidden_wiring_call",
    "让宪法层偷偷依赖第三方包(独立落点会因为装不上依赖而压根没跑)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_works_without_any_third_party_package",
    "让研究层不再报「某个 workflow 里压根没有守卫调用」(独立落点可以被删)":
        "tests/test_ci_wiring.py::test_the_wiring_layer_requires_the_constitution_guard_to_run",
    "让研究层在宪法层的职责被删时不再 fail-closed(独立落点变空壳也没人发现)":
        "tests/test_ci_wiring.py::test_the_wiring_layer_fails_closed_when_the_constitution_duty_is_gone",
    # ---- 第 55 轮之五:codex 第 2 轮的 C1 / C2 / H1 / H2 / M1 / L2 / L3 / L4 ----
    "让接线自检不再报闸门步骤上的 env:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_step_with_an_env_block",
    "让接线自检不再报闸门 job 上的 env:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_job_with_an_env_block",
    "让接线自检不再报闸门 job 上的 needs:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_job_with_needs",
    "让接线自检不再报守卫步骤上的 env:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_wiring_step_with_an_env_block",
    "让接线自检不再报守卫 job 上的 env:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_wiring_job_with_an_env_block",
    "让接线自检不再报守卫 job 上的 needs:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_wiring_job_with_needs",
    "让接线自检不再报 job 级 defaults.run.shell":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_job_level_default_shell",
    "让接线自检不再禁止别的 workflow 里有闸门步骤":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_step_in_another_workflow",
    "让接线自检不再报非字符串的 run:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_non_string_run_without_crashing",
    "让接线自检把同一条问题报两遍":
        "tests/test_ci_wiring.py::test_the_dedupe_keeps_one_copy_of_each_problem",
    "让接线自检不再用共享的名单结构校验":
        "tests/test_ci_wiring.py::test_the_wiring_layer_rejects_every_bad_roster[must_run_not_object]",
    "让闸门在名单结构不对时仍然放行":
        "tests/test_ci_wiring.py::test_the_gate_exits_with_two_when_the_roster_is_invalid[empty_must_run]",
    "让规范形不再接受取值型的解释器选项":
        "tests/test_ci_wiring.py::test_the_canonical_form_allows_interpreter_flags[utf8]",
    "让规范形不再接受不取值的解释器选项":
        "tests/test_ci_wiring.py::test_the_canonical_form_allows_interpreter_flags[unbuffered]",
    # ---- 第 55 轮之六:claude 第 3 轮 + codex 第 3 轮 ----
    "让接线自检不再报 workflow 级 env:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_workflow_level_env_block",
    "让接线自检的存在性检查不再报 workflow 级 env:":
        "tests/test_ci_wiring.py::test_the_wiring_presence_check_reports_a_workflow_level_env_block",
    "让接线自检不再报步骤级 working-directory:":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_step_level_working_directory",
    "让接线自检不再报 job 级 defaults.run.working-directory":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_job_level_working_directory",
    "让接线自检不再报 job 级 container:(这一步整个跑在别人给的镜像里)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_job_level_container",
    "让接线自检不再报 job 级 services:(这一步整个跑在别人给的容器里)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_job_level_services",
    "让接线自检不再报 $GITHUB_ENV/$GITHUB_PATH(给后续所有步骤换解释器)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_step_mentioning_github_env",
    "让接线自检不再报 push 触发器上的 paths 过滤(不匹配的提交压根不触发)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_push_trigger_with_a_paths_filter",
    "让接线自检不再扫复合动作(.github/actions/**/action.yml 里的闸门步骤)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_step_in_a_composite_action",
    "让接线自检不再认模块形态的闸门调用(python -m tools.ci_gate)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_module_form_gate_call_in_a_composite_action",
    "让接线自检不再要求受保护脚本那一步带 --verify(ADR 的变异表双向核对不再被 CI 执行)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_protected_step_without_the_verify_flag",
    "让宪法层不再按归一化路径认接线自检的调用(两层口径漂移)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_normalises_the_script_path",
    "让宪法层不再认连写的取值选项(-Xutf8)(两层口径漂移)":
        "tests/test_ci_wiring.py::test_the_constitution_layer_accepts_the_attached_value_options",
    "让名单结构不对时的退出码回到 1(判不了与失败混为一谈)":
        "tests/test_check_ci_ran.py::test_an_empty_roster_entry_fails",
    "让接线自检不再管分档器冒烟那一步的旁路键(裁决者的关卡可以被轻轻放过)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_step_with_continue_on_error",
    "让接线自检不再管分档器那一步里关掉的 errexit(分档器崩了也不红)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_step_that_disables_errexit",
    "让接线自检不再管分档器调用后面的「或真」后缀(失败被吃掉)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_call_followed_by_or_true",
    "让接线自检不再要求裁决者冒烟关卡存在(整个步骤删掉也没人发现)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_missing_domain_classifier_gate",
    "让接线自检不再管分档器调用失败被吃掉的其它形态(失败照样被吃掉)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_call_whose_failure_is_swallowed",
    "让接线自检不再管分档器调用换行接后缀的续行(失败照样被吃掉)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_call_swallowed_across_a_continuation",
    "让接线自检不再管分档器调用被包在条件里(失败不会让这一步变红)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_call_wrapped_in_a_condition",
    "让接线自检不再要求本 workflow 自己带裁决者冒烟调用(拿别的 workflow 的假调用顶掉)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_decoy_domain_classifier_call_in_another_workflow",
    "让接线自检不再报分档器冒烟所在 job 的 job 级旁路键(整个 job 可以不出力)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_domain_classifier_job_with_a_bypass_key",
    "让接线层的 workflow 级 env 文案与形状检查的不一样(同一条问题报两遍)":
        "tests/test_ci_wiring.py::test_the_two_workflow_level_env_messages_are_identical",
    "让接线自检不再报 workflow 级 defaults.run.working-directory":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_workflow_level_working_directory",
    "让接线自检不再扫别的 workflow 文件里的闸门步骤":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_step_in_a_workflow_that_was_not_passed_in",
    "让名单校验不再报空条目(一份套件被点名却一条用例都没点名)":
        "tests/test_ci_wiring.py::test_the_shared_roster_validator_rejects_an_empty_suite_entry",
    "让名单校验不再报未知的顶层键":
        "tests/test_ci_wiring.py::test_the_shared_roster_validator_rejects_an_unknown_top_level_key",
    "让规范形不再接受连写的取值选项(-Werror / -Xutf8)":
        "tests/test_ci_wiring.py::test_the_canonical_form_allows_the_attached_value_options[Werror]",
    "让接线自检不再报闸门命令尾部的 &(退出码被丢弃)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_gate_step_with_a_background_ampersand",
    "让接线自检不再报受保护脚本所在 job 的 job 级旁路键":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_protected_job_with_an_env_block",
    "让接线自检不再管受保护脚本(变异验证那两步可以被 continue-on-error 废掉)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_protected_step_with_continue_on_error",
    "让接线自检不再按效果判 types:(少了 synchronize 也不报)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_types_filter_without_synchronize",
    "让接线自检不再归一化相对脚本路径(./tools/ci_gate.py 被误判成非规范形)":
        "tests/test_ci_wiring.py::test_the_canonical_form_accepts_equivalent_relative_paths[dot_slash]",
    "让不存在的套件也被算成「已覆盖」(覆盖判定被一个根本不存在的文件顶掉)":
        "tests/test_ci_wiring.py::test_a_nonexistent_suite_is_not_counted_as_covered",
    "让接线自检不再把 pytest.__main__ 归一成 pytest(它真的跑 pytest)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_pytest_main_module_invocation",

    "让接线自检不再报复合动作里跑的 pytest(闸门在别处跑而 CI 是绿的)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_pytest_invocation_inside_a_composite_action",

    "让接线自检不再报别的 workflow 里跑的 pytest":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_pytest_invocation_in_another_workflow",

    "让接线自检把只跑 --help 的裁决者调用也当成关卡(它不做任何分档)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_that_only_prints_help",

    "让接线自检不再管写在 exit 之后的裁决者调用(那是死代码)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_after_an_exit",

    "让接线自检不再管从未被调用的函数体里的裁决者调用":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_inside_a_function_that_is_never_called",

    "让接线自检不再把续行接起来判(失败被吃掉的形态漏过)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_backslash_continuation_that_swallows_the_failure",

    "让接线自检不再管 eval 里的 set +e(它关掉了 errexit)":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_eval_that_disables_errexit",

    "让接线自检不再把绝对路径的 true 后缀判成吃掉失败":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_absolute_true_that_swallows_the_failure",

    "让接线自检把 echo skipped 后缀也当成捕获(失败被吃掉却看不见)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_generic_success_suffix_that_swallows_the_failure",

    "让接线自检不再报步骤级的 container 键":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_step_level_container",

    "让接线自检只认规范形的闸门步骤(非规范形的 job 级攻击面被盖住)":
        "tests/test_ci_wiring.py::test_the_checker_reports_job_level_keys_of_a_job_whose_gate_call_is_not_canonical",

    "让接线自检按基名认受保护脚本(仓库根放个同名空壳就能顶掉证据链)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_protected_script_called_through_a_stub_path",

    "让接线自检不再管复合动作里的裁决者冒烟":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_inside_a_composite_action",

    "让接线自检把能执行的 sed 形态也当成不执行类命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_sed_command_that_executes_pytest",

    "让接线自检被单行的 if 分支骗到(后面的调用全被当成在条件里)":
        "tests/test_ci_wiring.py::test_the_checker_does_not_report_a_call_after_a_single_line_conditional",

    "让接线自检不再按 token 找 set(条件里的 set +e 认不出来)":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_set_plus_e_inside_a_single_line_conditional",

    # ---- 第 55 轮之十二 ----
    "让接线自检不再把同一逻辑行里 exit 之后的调用当死代码":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_after_an_exit_on_the_same_line",

    "让接线自检认不出不引号的 sed e 标志":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_unquoted_sed_exec_flag",

    "让接线自检把 rg --pre 当成只读命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_rg_pre_processor_command",

    "让接线自检不再认管道给 shell":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_sed_program_that_is_piped_into_a_shell",

    "让接线自检不再逐个登记第三方动作":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_unregistered_third_party_action",

    "让接线自检把只读命令里提到的闸门也算成闸门步骤":
        "tests/test_ci_wiring.py::test_the_checker_does_not_treat_an_inert_mention_of_the_gate_as_a_gate_step",

    "让接线自检把 sed 的标志位判据放宽回不分隔符的形态":
        "tests/test_ci_wiring.py::test_the_checker_does_not_report_a_sed_replacement_that_merely_contains_an_e",
    "让接线自检把 sed 的分隔符写死成斜杠":
        "tests/test_ci_wiring.py::test_the_checker_does_not_report_a_sed_program_that_writes_a_command",
    "让接线自检又在管道处把逻辑行切开":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_awk_program_that_is_piped_into_a_shell",
    "让接线自检认不出带引号的退出码捕获":
        "tests/test_ci_wiring.py::test_the_checker_does_not_report_a_quoted_capture_as_a_swallowed_failure",

    # ---- 第 55 轮之十四 ----
    "让接线自检放行仓库里不存在的本地动作":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_local_action_whose_definition_is_missing",
    "让接线自检放行非复合的本地动作":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_local_action_that_is_not_composite",
    "让接线自检不扫本地复合动作的 run 块":
        "tests/test_ci_wiring.py::test_the_checker_scans_the_run_blocks_of_a_local_composite_action",
    "让接线自检认不出 eval 里的 exit":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_after_an_eval_that_exits",
    "让接线自检把 awk 的 co-process 当成只读命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_awk_coprocess_command",
    "让接线自检在引号里也把逻辑行切开":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_quoted_pipe_as_part_of_one_command",
    "让接线自检认不出 rg --pre= 这种写法":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_rg_pre_with_an_equals_sign",
    "让接线自检认不出 sed 的 e 命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_sed_e_command",
    "让接线自检把 here-doc 的正文当成命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_a_self_guard_call_inside_a_heredoc_body",
    "让接线自检在复合动作里跳过认不出来的命令":
        "tests/test_ci_wiring.py::test_the_checker_reports_an_unparsable_block_inside_a_local_composite_action",

    # ---- 第 55 轮之十四之四(变异被陈旧的 .pyc 吃掉) ----
    "不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)":
        "tests/test_check_guard_mutations.py::test_running_the_suite_clears_the_bytecode_cache_before_it_runs",
    "纪律守卫不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)":
        "tests/test_check_guard_mutations.py::test_the_discipline_harness_sees_a_same_second_mutation",
    "分档守卫不再清掉副本里的字节码缓存(陈旧 .pyc 会让变异空转)":
        "tests/test_check_guard_mutations.py::test_the_domain_harness_sees_a_same_second_mutation",

    # ---- 第 55 轮之十四之五(清理动作本身的健壮性) ----
    "字节码缓存删不掉时不再出声":
        "tests/test_check_guard_mutations.py::test_the_purge_reports_a_cache_file_it_cannot_delete",
    "字节码缓存目录读不了时直接崩掉":
        "tests/test_check_guard_mutations.py::test_the_purge_survives_an_unreadable_directory",
    "分档守卫不再摘掉 PYTHONDONTWRITEBYTECODE":
        "tests/test_check_guard_mutations.py::test_the_domain_harness_writes_bytecode_even_when_the_environment_forbids_it",
    "纪律守卫不再摘掉 PYTHONDONTWRITEBYTECODE":
        "tests/test_check_guard_mutations.py::test_the_discipline_harness_writes_bytecode_even_when_the_environment_forbids_it",

}


def _reconfigure_stdio() -> None:
    """把 stdout/stderr 设成 utf-8 + errors="replace"。

    **为什么必须做**:Windows 控制台默认 cp1252,打印中文会抛 `UnicodeEncodeError`
    ——而本仓库的主力开发环境就是 Windows。与 `tools/check_mutations.py` 同口径。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _run_tests(cwd: Path, targets: list[str]) -> dict:
    """跑指定用例,返回一份**结果字典**(第 49 轮 codex M3/M4 加了 nodeid 与退出码)。

    **为什么把"收集错误"单独拎出来**:变异把文件改成语法错误时,pytest 在**收集阶段**
    就炸,一条 FAILED 都不会有——那与"守卫没抓住"在退出码上无法区分。
    前者是**变异规格写错了**(脚本自己该红),后者是**守卫有洞**(也该红,但要分开说)。

    **第 52 轮(codex 第 2 轮 M1/L2)**:这个分类此前是**本文件自己写的一套**
    (`line.startswith("ERROR") or " errors" in line and "error" in line.lower()`),
    与 `tools/check_mutations.py` 那套**同名不同义**——同一份 pytest 输出,
    两个脚手架可能给出**相反**的结论。现在**共用同一份实现**
    (`check_mutations.run_outcome`):退出码 0 且无失败 ⇒ ok;退出码 1 且**有** FAILED ⇒ ok;
    退出码 1 却一条 FAILED 没有(收集阶段炸了)或退出码 ≥ 2 ⇒ spec_broken。
    """
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    # 【第 55 轮之十四之五,codex L3】**不能带着 `PYTHONDONTWRITEBYTECODE` 跑**:
    # 一旦这个变量(或 `-B`)在环境里,解释器**根本不写 `.pyc`**
    # ⇒ 拦截侧探针的「第一次跑会写出 `.pyc`」前提不成立 ⇒ 探针假红
    # ⇒ 把「环境配置」误报成「变异被吃」。这里**显式摘掉**它。
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    # 【第 55 轮之十四之四】**先清字节码缓存**再跑:
    # 否则「同秒 + 同大小」的变异会被**陈旧的 .pyc** 吃掉 ⇒ 变异空转
    # ⇒ 见证保持绿(假绿)。共享实现见 `check_mutations._purge_bytecode_cache`
    # (两个脚手架共用一份 ⇒ 不会只改一边)。
    _purge_bytecode_cache(cwd)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(cwd), env=env,
        encoding="utf-8", errors="replace",
    )
    output = proc.stdout + proc.stderr
    failed = [line for line in proc.stdout.splitlines() if line.startswith("FAILED")]
    # **见证用例的 nodeid**:pytest 的短摘要行是 "FAILED tests/x.py::test_y - 原因",
    # 取 " - " 之前的部分就是 nodeid(第 49 轮 codex M3:必须能指名道姓)。
    failed_nodes: list[str] = []
    for line in failed:
        node = line[len("FAILED "):].split(" - ")[0].strip()
        if node:
            failed_nodes.append(node)
    return {
        "failed": len(failed),
        "output": output,
        "failed_nodes": failed_nodes,
        "returncode": proc.returncode,
        "passed": _passed_count(output),
        # **共用**分档那套脚手架的判定(第 52 轮):不再自己写一套关键词启发式。
        "outcome": run_outcome(len(failed), proc.returncode),
    }


def _passed_count(output: str) -> int | None:
    """从 pytest 的摘要行里读出**通过数**(绿对照要比基线,codex M4)。

    从**末尾**往前找:摘要行在最后,而 FAILED 行里也可能出现 "passed" 字样。

    **第 52 轮(codex 第 2 轮 L1)**:读不出来时返回 `None`(此前返回 `-1`)。
    `-1` 与 `-1` **相等** ⇒ 绿对照的 `res["passed"] == base` 在「两边都没读出来」时
    **恒真** ⇒ 绿对照变成空转的。现在 `None` 与任何数都不相等,
    且判定处额外要求 `base > 0`(基线必须真的跑过用例)。
    """
    for line in reversed(output.splitlines()):
        match = re.search(r"(\d+) passed", line)
        if match:
            return int(match.group(1))
    return None




def _diagnose_no_red(repo: Path, path: Path, witness: str, res: dict) -> list[str]:
    """见证没红时,把「变异到底生效了没有」查清楚(第 55 轮之十四之三)。

    **为什么要有这一步**:脚手架此前只说「见证没红 ⇒ 守卫有洞」,
    但**变异空转**(变异根本没改到行为)与**守卫有洞**在退出码上**完全一样**
    ⇒ 照字面处置就会去"修"一个没坏的守卫,或者删掉一条还在生效的变异
    (仓库教训:变异见证必须指向唯一那一条规则)。

    **查三件事**:
      1. 变异后的文件里到底有没有那处改动(没落到这份文件 ⇒ 根本没执行到);
      2. **字节码缓存**是不是把这次变异吃掉了(第 55 轮之十四之四的成因):
         **决定性的问法**是「**显式**清掉缓存再跑同一条见证」——
         清完就红 ⇒ 之前那次绿是**陈旧的 `.pyc` 把变异吃掉了**(空转);
         清完仍然不红 ⇒ 不是缓存的事 ⇒ 变异对**所有**用例都没影响
         (空转,或改到的不是被执行的那份代码);
      3. **同一变异下整份套件**的失败集合(空 ⇒ 变异对所有用例都没影响;
         非空但**不含**见证 ⇒ 见证被**第二条规则**顶替)。

    **顺序很重要(第 55 轮之十四之四的复核发现)**:`_run_tests` **会清字节码缓存**
    ⇒ 查缓存必须在**任何一次跑用例之前**做,否则查的就是「刚被清空后重编出来」
    的缓存 ⇒ 那条诊断**永远看不到**陈旧的字节码(写下了但没人跑)。
    """
    out: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:                       # noqa: BLE001 - 诊断不改变结论
        return ["读不出变异后的文件:%r" % (exc,)]
    # **`False and` 计数只对那一族变异有意义**(第 55 轮之十四之五,codex Info):
    # 其余一百多条变异改的不是这个形状 ⇒ 这里恒为 0 ⇒ 排障时容易误读
    # ⇒ **把适用范围写清楚**,而不是让人以为「0 次 ⇒ 变异没生效」。
    out.append("变异后的 %s 里 `False and` 出现 %d 次"
               "(**此计数只对 `False and` 族有意义**,其余族的变异恒为 0)"
               % (path.name, text.count("False and")))
    out.append("见证用例的 pytest 输出尾部:%s"
               % res["output"][-500:].replace("\n", " | "))
    # **先查缓存**(必须在跑用例之前:跑用例会清缓存)。
    out.extend(_probe_stale_pyc(repo, path))
    suite = witness.split("::")[0]
    whole = _run_tests(repo, [suite])
    out.append("同一变异下**整份套件**(%s)的失败集合:%s"
               % (suite, ", ".join(whole["failed_nodes"][:20])
                  or "无 ⇒ 变异对所有用例都没影响(空转,或改到的不是被执行的那份代码)"))
    out.append("整份套件的输出尾部:%s" % whole["output"][-500:].replace("\n", " | "))
    # **决定性的一问**:显式清掉缓存,再跑**同一条见证**。
    # (`_run_tests` 自己会清;这里**再显式清一次**是为了让这条诊断在
    #  「清缓存那一步被删掉」时**仍然可用**——那正是要诊断的故障模式。)
    _purge_bytecode_cache(repo)
    again = _run_tests(repo, [witness])
    if again["failed"]:
        out.append("**显式清掉字节码缓存后,同一条见证红了 %d 条** ⇒ 之前那次绿是"
                   "**陈旧的 `.pyc` 把变异吃掉了**(变异空转,不是守卫有洞)"
                   % again["failed"])
    else:
        out.append("**显式清掉字节码缓存后,同一条见证仍然没红** ⇒ 不是缓存的问题 ⇒ "
                   "变异对**所有**用例都没影响(空转,或改到的不是被执行的那份代码)")
    return out


def _const_key(const) -> str:
    """把一个常量压成**稳定**的字符串。

    **为什么要专门写这个**:`frozenset` 的 `repr` 取决于**迭代顺序**
    ⇒ 同一份源码,「进程内编译出来的」与「从 `.pyc` 反序列化出来的」
    两次 `repr` **顺序可能不同** ⇒ 直接比 `repr` 会**假阳性**
    (实测踩过:未变异的文件被报成「旧字节码被复用」)
    ⇒ 集合类常量必须**排序后再比**。
    """
    if isinstance(const, (frozenset, set)):
        return "{%s}" % ",".join(sorted(_const_key(item) for item in const))
    if isinstance(const, tuple):
        return "(%s)" % ",".join(_const_key(item) for item in const)
    return repr(const)


def _fingerprint(code) -> tuple:
    """把代码对象压成一个**可比较**的摘要(递归)。

    用来判断「`.pyc` 里的代码是不是当前源文件编译出来的那一份」。
    **刻意不包含 `co_filename`**(同一份代码在不同路径下编译出来的
    `co_filename` 不同 ⇒ 会造成假的不一致)。
    """
    consts = []
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            consts.append(_fingerprint(const))
        else:
            consts.append(_const_key(const))
    return (code.co_code, tuple(consts))


# 「把当前源文件编译成代码对象」这一步交给**一次性子解释器**做
# (第 55 轮之十四之六)。
#
# **为什么绕到子进程**:宪法层(`tools/check_constitution_history.py`)按
# 「模块里有没有编译/执行原语」判定「谁执行候选代码」⇒ 谁执行候选谁必须走
# 第四层(快照 + 指纹比对)。本模块**从不执行候选代码**,它只是比较
# 「缓存里的代码对象」与「当前源文件编译出来的代码对象」——
# 若在本模块里直接写 `compile(...)`,宪法层会判本模块「裸奔」⇒
# 而那会逼着人给一个**根本不执行候选代码**的工具硬凑第四层,或者去动
# 宪法层的清单 ⇒ **两条都是坏路**(前者是无意义的仪式,后者是让守卫变松)。
# ⇒ 编译交给子解释器,本模块只拿到**代码对象的字节**,自己不持有任何原语。
#
# **这不是把规则藏起来**:本模块里**一个**编译/执行原语都没有
# (有一条反向探针钉住这一点,见 `tests/test_check_guard_mutations.py`);
# 子解释器只做「编译 + 序列化」,**不执行**被编译的代码。
_CHILD_COMPILE_PROGRAM = (
    "import marshal,sys;"
    "src=sys.argv[1];"
    "code=compile(open(src,encoding='utf-8').read(),src,'exec');"
    "sys.stdout.buffer.write(marshal.dumps(code))"
)


def _compile_current_source(src: Path) -> tuple[object | None, str]:
    """在**一次性子解释器**里编译当前源文件,取回代码对象。

    返回 `(代码对象, 错误说明)`;编译不了时第一个是 `None`。
    **调用方必须把 `None` 写成「查不了」**,**不许**当成「缓存是新的」
    (查不了 ≠ 干净的结论——仓库教训:守卫不许无声失效)。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _CHILD_COMPILE_PROGRAM, str(src)],
            capture_output=True, cwd=str(src.parent), timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001 - 诊断不改变结论
        return None, "子解释器没跑起来:%r" % (exc,)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return None, "子解释器编译失败:%s" % (tail[-1] if tail else "退出码 %d" % proc.returncode,)
    try:
        return marshal.loads(proc.stdout), ""
    except Exception as exc:                       # noqa: BLE001 - 诊断不改变结论
        return None, "读不出子解释器给的代码对象:%r" % (exc,)


def _probe_stale_pyc(repo: Path, src: Path) -> list[str]:
    """查「字节码缓存陈旧」这条路径(Python 的 .pyc 按 (mtime 秒, size) 校验)。

    **为什么查这条**:Python 导入模块时,若 `.pyc` 头里记的 (mtime 秒, size)
    与源文件**当前**的 (mtime 秒, size) 一致,就直接用**旧的字节码**,
    源文件改了也看不见 ⇒ 变异「施加了但没生效」。

    **时间戳一致**本身**不足以**断定「字节码是旧的」——碰撞的定义就是
    两者一致 ⇒ 这里**再比一次内容**:把 `.pyc` 里的代码对象与
    **当前源文件重新编译**出来的代码对象比摘要
    ⇒ 不一致 ⇒ **确实是旧字节码被复用**(决定性的判据)。
    (编译这一步在**子解释器**里做,理由见 `_CHILD_COMPILE_PROGRAM` 上面的说明。)

    **必须在跑用例之前调用**:`_run_tests` 会清缓存 ⇒ 跑过再查就永远查不到。
    """
    out: list[str] = []
    cache_dir = src.parent / "__pycache__"
    if not cache_dir.is_dir():
        out.append("副本里没有 %s 的 __pycache__(第一次跑就会重新编译)" % src.name)
        return out
    try:
        st = src.stat()
    except OSError as exc:                        # noqa: BLE001 - 诊断不改变结论
        return ["读不出变异后的文件:%r" % (exc,)]
    current, err = _compile_current_source(src)
    if current is None:
        return ["变异后的文件编译不了(%s)⇒ **查不了**字节码(查不了 ≠ 结论干净)" % err]
    for pyc in sorted(cache_dir.glob(src.stem + ".*.pyc")):
        try:
            raw = pyc.read_bytes()
        except OSError:                            # noqa: BLE001 - 诊断不改变结论
            continue
        if len(raw) < 16:
            continue
        flags = int.from_bytes(raw[4:8], "little")
        if flags != 0:
            out.append("%s 是 hash-based pyc(flags=%d)⇒ 不做时间戳校验" % (pyc.name, flags))
            continue
        mtime = int.from_bytes(raw[8:12], "little")
        size = int.from_bytes(raw[12:16], "little")
        if not (mtime == int(st.st_mtime) and size == st.st_size):
            out.append("%s 记的 (mtime=%d, size=%d) vs 变异后源文件 (mtime=%d, size=%d) ⇒ "
                       "时间戳不一致 ⇒ Python 会重新编译(这一项不构成陷阱)"
                       % (pyc.name, mtime, size, int(st.st_mtime), st.st_size))
            continue
        try:
            cached = marshal.loads(raw[16:])
            stale = _fingerprint(cached) != _fingerprint(current)
        except Exception as exc:                   # noqa: BLE001 - 诊断不改变结论
            out.append("%s 读不出缓存里的代码:%r" % (pyc.name, exc))
            continue
        out.append("%s 记的 (mtime=%d, size=%d) vs 变异后源文件 (mtime=%d, size=%d) ⇒ 时间戳一致;"
                   "缓存里的代码与当前源文件%s"
                   % (pyc.name, mtime, size, int(st.st_mtime), st.st_size,
                      "**不一致 ⇒ 旧字节码被复用 ⇒ 变异根本没被执行**" if stale
                      else "一致 ⇒ 缓存是新的(不是这一条成因)"))
    return out


def _read_text(path: Path) -> tuple[str, str]:
    """读文件 ⇒ (内容, 行尾符)。行尾符必须**原样保留**:
    仓库里既有 CRLF 文件也有 LF 文件,统一改写会让 diff 全是行尾噪声,
    评审时真正的那一行改动就被淹没了。
    """
    raw = path.read_bytes().decode("utf-8")
    if NL in raw and raw.count(chr(13) + NL) == raw.count(NL):
        return raw.replace(chr(13) + NL, NL), chr(13) + NL
    return raw, NL


def _write_text(path: Path, text: str, eol: str) -> None:
    path.write_bytes(text.replace(NL, eol).encode("utf-8"))


def _apply(repo: Path, rel: str, edits: list[tuple[str, str]]) -> str:
    """把含锚点的那几行替换成新的几行;返回 'ok' 或错误说明。

    定位靠**子串**且要求**唯一命中**:锚点不唯一 ⇒ 规格与仓库现状对不上 ⇒ 退出码 2
    (不许"猜一个改"——那会让变异变成空操作,而空操作比没有变异更坏)。

    **匹配的是整段文本,不是单行**——锚点可以跨行(第 18 条变异要同时改两行)。
    早先按行匹配时,跨行锚点**永远命中 0 次**(一个字符串里含换行,不可能是某一行
    的子串)⇒ 那条变异直接被判成"规格写错了"。这是脚本自身的洞,不是仓库的洞。
    """
    path = repo / rel
    if not path.exists():
        return "文件不存在:%s" % rel
    text, eol = _read_text(path)
    for token, new_text in edits:
        hits = text.count(token)
        if hits != 1:
            return "锚点命中 %d 次(应为 1):%r" % (hits, token[:70])
        text = text.replace(token, new_text)
    _write_text(path, text, eol)
    return "ok"


def _verify_adr(results: list[tuple[str, str]], adr_path: Path | None = None) -> int:
    """核对 ADR 里的**变异集合**与实测是否一致(口径同 `check_mutations.py`)。

    第 49 轮(codex 第 2 轮 H2/M1):口径搬到 `check_mutations.verify_mutation_table`
    ——**双向**(ADR 多写/陈旧 => 红)、**只看那一节**的表格、**逐格**归一化
    (不去掉单个 `*` => 标签可以写回真实的 `except*`)。
    """
    adr = adr_path or (ROOT / "docs" / "adr" / "20261113-evolution-engine.md")
    labels = [label for label, _expect in results]
    return verify_mutation_table(labels, adr, "纪律守卫的变异表")


def _cleanup(path: Path) -> None:
    """删掉临时副本(第 49 轮 codex L5)。

    此前是 `shutil.rmtree(tmp, ignore_errors=True)`——**忽略错误**等于"删不掉就算了":
    Windows 上文件被占用(索引器/杀软/git)时会**留下 7 份仓库副本**在临时目录里,
    越攒越多。现在:重试几次 ⇒ 仍不行就**把路径报出来**(让人能自己删)。

    **第 50 轮**:这份实现搬到了 `check_mutations.cleanup_temp_dir`,
    两个脚手架**共用同一份**——此前两边各写一份(分档那套还是老的 ignore_errors),
    迟早会只改一边(仓库教训:「同一个问题两个答案」)。
    """
    cleanup_temp_dir(path)


def main(argv: list[str] | None = None) -> int:
    _reconfigure_stdio()
    parser = argparse.ArgumentParser(description="纪律守卫的变异验证:每条规则松了都必须被抓")
    parser.add_argument("--verify", action="store_true",
                        help="额外核对 ADR 表格里的变异集合与实测是否一致")
    args = parser.parse_args(argv)

    # **每条"应该变红"的变异都必须钉了见证用例**(第 49 轮 codex M3)。
    # 没钉 ⇒ "红了几条"照样算数 ⇒ 走错原因的红会被算成"抓住了"(假绿)。
    missing = [spec["label"] for spec in MUTATIONS
               if spec["expect"] == "red" and spec["label"] not in WITNESSES]
    if missing:
        print("这些变异没有钉见证用例:", file=sys.stderr)
        for label in missing:
            print("  - %s" % label, file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="guard_mutations_"))
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    try:
        copy_repo(repo)

        # **基线**:两份套件各跑一遍,必须全绿。
        # 见证用例在基线里就该绿——否则"红了"分不清是变异造成的还是本来就红
        # (第 49 轮 codex M4:绿对照要比**基线的通过数**,不只是"没红")。
        baselines: dict[tuple[str, ...], int] = {}
        for targets in ((BOUNDARY,), (TOOL_TEST,), (WIRING,)):
            res = _run_tests(repo, list(targets))
            # **基线必须全绿**:一条失败都不许有。
            #
            # **这里不能拿 `outcome` 当判据**(第 52 轮实测踩到的坑):
            # `run_outcome` 的 "ok" 覆盖两种情况——「退出码 0 且无失败」**与**
            # 「退出码 1 且**有** FAILED」(对变异脚手架来说那是"探针在工作")。
            # 但对**基线**来说,「有 FAILED」恰恰是**环境坏了**(名单过期、
            # 守卫本身红了),必须停下报退出码 2。
            # 本轮第一次写这里时用了 `outcome != "ok"` ⇒ **红基线被当成了绿基线**,
            # 脚手架据此说「全绿」——**假绿**。它是被那条「无害改动必须保持绿」的
            # 反向对照逮住的(改完之后绿对照自己红了,才暴露出基线本来就是红的)。
            if res["failed"] or res["returncode"] != 0:
                print("基线(未变异的副本)就有测试失败/收集错误,变异验证无从谈起:", file=sys.stderr)
                print(res["output"][-2000:], file=sys.stderr)
                return 2
            # **基线必须真的跑过用例**(第 52 轮 codex L1):
            # 通过数读不出来(None)或读到 0 ⇒ 「全绿」是空话(可能整份被跳过),
            # 绿对照就没有基准可比 ⇒ 退出码 2(环境不可用)。
            if not res["passed"] or res["passed"] <= 0:
                print("基线(未变异的副本)读不出通过数(读到了 %r):"
                      "「全绿」无从谈起,变异验证无从谈起:" % res["passed"], file=sys.stderr)
                print(res["output"][-2000:], file=sys.stderr)
                return 2
            baselines[targets] = res["passed"]
            print("基线(未变异):%s 全绿(%d 条通过)" % (targets[0], res["passed"]))
        print()

        results: list[tuple[str, str]] = []
        problems: list[str] = []
        for spec in MUTATIONS:
            label = spec["label"]
            rel = spec["rel"]
            path = repo / rel
            if not path.exists():
                print("  [跳过] %s:文件不存在(%s)" % (label, rel), file=sys.stderr)
                return 2
            orig = path.read_bytes()
            msg = _apply(repo, rel, spec["edits"])
            if msg != "ok":
                path.write_bytes(orig)
                print("  [跳过] %s:%s" % (label, msg), file=sys.stderr)
                return 2

            expect = spec["expect"]
            if expect == "red":
                # **只跑见证那一条**(第 49 轮 codex M3/M4):
                # 跑整份套件慢(~15 分钟)而且抓不住"走错原因的红"。
                run_targets = [WITNESSES[label]]
            else:
                run_targets = list(spec["targets"])
            res = _run_tests(repo, run_targets)
            # 【第 55 轮之十四之三】见证没红时,先把「变异到底生效了没有」查清楚
            # (诊断必须在**还原之前**做:还原了就看不到变异的效果了)。
            diag: list[str] = []
            if expect == "red" and not res["failed_nodes"] and res["returncode"] == 0:
                diag = _diagnose_no_red(repo, path, WITNESSES[label], res)
            path.write_bytes(orig)

            # **共用**分档那套的判定(第 52 轮 codex M1/L2):
            # spec_broken = 变异把文件改到收集阶段就炸了 ⇒ 变异规格写错了(不是守卫有洞)。
            if res["outcome"] == "spec_broken":
                print("  [跳过] %s:变异把文件改到收集阶段就报错了(变异规格写错了)" % label,
                      file=sys.stderr)
                print(res["output"][-1500:], file=sys.stderr)
                return 2

            if expect == "red":
                witness = WITNESSES[label]
                ok = witness in res["failed_nodes"]
                mark = "OK " if ok else "!! "
                print("  [%s见证红了] %s" % (mark, label))
                if not ok:
                    print("      ↑ 见证用例 %s 没有红(实际退了 %d 条:%s)"
                          % (witness, res["failed"], ", ".join(res["failed_nodes"][:5]) or "无"),
                          file=sys.stderr)
                    print("      ↑ **这句提示语本身有两种含义,先分清是哪一种**"
                          "(仓库教训:变异见证必须指向唯一那一条规则):"
                          "①变异空转(变异根本没改到行为)②守卫有洞(变异改坏了守卫但没人抓住)。",
                          file=sys.stderr)
                    for line in diag:
                        print("      ↑ [诊断] %s" % line, file=sys.stderr)
                    problems.append(label)
            else:
                base = baselines.get(tuple(spec["targets"]))
                # **绿对照不许空转**(第 52 轮 codex L1):
                # 此前 `same = base is not None and res["passed"] == base`,
                # 而 `_passed_count` 读不出来时返回 -1 ⇒ **-1 == -1 恒真**
                # ⇒ 「没跑用例」会被算成「与基线一致」⇒ 绿对照什么也没证明。
                # 现在:通过数必须**读得出来**、**大于 0**、且与基线**相等**。
                same = (base is not None and base > 0
                        and res["passed"] is not None and res["passed"] > 0
                        and res["passed"] == base)
                ok = res["failed"] == 0 and res["returncode"] == 0 and same
                mark = "OK " if ok else "!! "
                print("  [%s保持绿] %s(通过数 %s,基线 %s)" % (mark, label, res["passed"], base))
                if not ok:
                    print("      ↑ 无害改动竟然让套件**变红**或**少跑了用例**:"
                          "脚本可能变成了「一律报红」", file=sys.stderr)
                    problems.append(label)
            results.append((label, expect))

        print()
        if problems:
            print("以下变异**没有**产生预期效果(守卫有洞):", file=sys.stderr)
            for label in problems:
                print("  - %s" % label, file=sys.stderr)
            return 1
        print("%d 种变异全部符合预期。" % len(results))

        if args.verify:
            return _verify_adr(results)
        return 0
    finally:
        _cleanup(tmp)


if __name__ == "__main__":
    sys.exit(main())
