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
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ADR 变异表的核对口径**两个脚本共用**(第 49 轮:此前两处各写一份,
# 同名函数口径不同——一处有反向断言、一处没有)。
sys.path.insert(0, str(ROOT / "tools"))
from check_mutations import (  # noqa: E402
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

    # ---- 第 54 轮:CI **接线**那一族(codex 第 2 轮 H1/H2/H3/H5) ----
    #
    # 这一族改的是 `.github/workflows/research-guard.yml` 本身——
    # 此前接线类缺陷(H1~H5)**没有任何变异验证**:第 53 轮写下的守卫
    # 若哪天被放松(比如把 `|| true` 加回去),CI 不会红,ADR 里那条结论
    # 也就变成了一句没人复核的话(codex 第 2 轮 M4:证据只活在 %TEMP%)。
    # 现在每一条旁路都有**一条变异**把它变红,且**钉死见证用例**。
    _mutation(
        "在闸门命令后面接一个恒真后缀(判红也不算失败)",
        WORKFLOW,
        ("            --roster tests/guard_roster.json --suite tests/test_research_boundary.py",
         "            --roster tests/guard_roster.json --suite tests/test_research_boundary.py || true"),
    ),
    _mutation(
        "在闸门调用之前加 `set +e`",
        WORKFLOW,
        ('          rm -f "$RUNNER_TEMP/boundary.xml"',
         _lines("          set +e",
                '          rm -f "$RUNNER_TEMP/boundary.xml"')),
    ),
    _mutation(
        "在闸门调用之后加 `exit 0`(强制判成功)",
        WORKFLOW,
        ("          python tools/check_ci_ran.py --junitxml=\"$RUNNER_TEMP/boundary.xml\" \\\n"
         "            --roster tests/guard_roster.json --suite tests/test_research_boundary.py",
         "          python tools/check_ci_ran.py --junitxml=\"$RUNNER_TEMP/boundary.xml\" \\\n"
         "            --roster tests/guard_roster.json --suite tests/test_research_boundary.py\n"
         "          exit 0"),
    ),
    _mutation(
        "给那一步加 `continue-on-error: true`(红了也不算数)",
        WORKFLOW,
        ("      - name: 研究侧边界不变量(批准唯一写入者 / Preset 边界 / 工具目录 / 不 import vnpy)\n"
         "        run: |",
         "      - name: 研究侧边界不变量(批准唯一写入者 / Preset 边界 / 工具目录 / 不 import vnpy)\n"
         "        continue-on-error: true\n"
         "        run: |"),
    ),
    _mutation(
        "给整个 job 加 `if: false`(这一步压根不跑)",
        WORKFLOW,
        ("  research:\n    runs-on: ubuntu-latest",
         "  research:\n    if: false\n    runs-on: ubuntu-latest"),
    ),
    _mutation(
        "把闸门点名的套件换成**同一份报告里另一份**套件",
        WORKFLOW,
        ('              --roster tests/guard_roster.json --suite "$suite" \\',
         "              --roster tests/guard_roster.json --suite tests/test_research_config.py \\"),
    ),
    _mutation(
        "把 CI 循环里的一份套件从 pytest 调用里删掉",
        WORKFLOW,
        ('            --junitxml="$RUNNER_TEMP/research_rest.xml" \\\n'
         "            tests/test_research_config.py \\",
         '            --junitxml="$RUNNER_TEMP/research_rest.xml" \\'),
    ),
    _mutation(
        "把删报告的 `rm -f` 挪进注释里(命令根本不执行)",
        WORKFLOW,
        ('          rm -f "$RUNNER_TEMP/boundary.xml"',
         '          # rm -f "$RUNNER_TEMP/boundary.xml"'),
    ),
    _mutation(
        "把删报告的 `rm -f` 挪进 `if false` 里(命令根本不执行)",
        WORKFLOW,
        ('          rm -f "$RUNNER_TEMP/boundary.xml"',
         '          if false; then rm -f "$RUNNER_TEMP/boundary.xml"; fi'),
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

    # ---- 反向:无害改动必须**保持绿**(证明脚本不是"一律报红") ----
    _mutation(
        "只改守卫里的一个注释(无害改动)",
        BOUNDARY,
        ('"""「被包住」的判定**自己**也要有探针(第 47 轮之三,实测发现的洞)。',
         '"""「被包住」的判定**自己**也要有探针(第 47 轮之三,实测发现的洞;无害改动)。'),
        expect="green",
    ),
    _mutation(
        # 第 54 轮(codex H3)的**反向对照**:把 `python -m pytest` 写成
        # `python3 -m pytest` —— 守卫必须**照样认得**它(否则就是"只认字面量")。
        # 这条**保持绿**才是对的;若它变红,说明守卫还是认死字符串。
        "把 `python -m pytest` 写成 `python3 -m pytest`(守卫照样认得)",
        WORKFLOW,
        ("          python -m pytest -p no:cacheprovider -q --junitxml=\"$RUNNER_TEMP/boundary.xml\" \\",
         "          python3 -m pytest -p no:cacheprovider -q --junitxml=\"$RUNNER_TEMP/boundary.xml\" \\"),
        expect="green",
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
    "在闸门命令后面接一个恒真后缀(判红也不算失败)":
        "tests/test_research_boundary.py::test_every_gate_call_can_actually_fail_the_step",
    "在闸门调用之前加 `set +e`":
        "tests/test_research_boundary.py::test_every_gate_call_can_actually_fail_the_step",
    "在闸门调用之后加 `exit 0`(强制判成功)":
        "tests/test_research_boundary.py::test_every_gate_call_can_actually_fail_the_step",
    "给那一步加 `continue-on-error: true`(红了也不算数)":
        "tests/test_research_boundary.py::test_every_gate_call_can_actually_fail_the_step",
    "给整个 job 加 `if: false`(这一步压根不跑)":
        "tests/test_research_boundary.py::test_every_gate_call_can_actually_fail_the_step",
    "把闸门点名的套件换成**同一份报告里另一份**套件":
        "tests/test_research_boundary.py::test_every_gate_call_checks_the_suites_its_own_report_produced",
    "把 CI 循环里的一份套件从 pytest 调用里删掉":
        "tests/test_research_boundary.py::test_every_gate_call_checks_the_suites_its_own_report_produced",
    "把删报告的 `rm -f` 挪进注释里(命令根本不执行)":
        "tests/test_research_boundary.py::test_every_junit_report_is_deleted_before_pytest_writes_it",
    "把删报告的 `rm -f` 挪进 `if false` 里(命令根本不执行)":
        "tests/test_research_boundary.py::test_every_junit_report_is_deleted_before_pytest_writes_it",
    "让结论行不说明「点名核对里的几条是允许跳过的」":
        "tests/test_check_ci_ran.py::test_the_conclusion_line_discloses_how_many_may_be_skipped",
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
        for targets in ((BOUNDARY,), (TOOL_TEST,)):
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
