# -*- coding: utf-8 -*-
"""`tools/check_ci_ran.py` 自己的守卫(CI 侧那道「真的跑了吗」闸门)。

为什么要有这个文件(仓库的老教训:**新写的守卫工具自己也要有人守**):
`tools/check_ci_ran.py` 是 CI 上唯一的「某条防线到底跑没跑」判定点。
它自己如果判错了(放行了没跑的、或把跑了的判成没跑),CI 那一步的绿/red 就**全无意义**。
而且它是**判别式**工具(退出码 0/1),测它不能用"跑一下看输出"糊弄——
必须**逐条钉住**:哪一种形态 ⇒ 哪一个退出码。

每个方向都要有反例:
  - 放行侧:报告完整 ⇒ 0;
  - 拦截侧:缺用例 / 被跳过 / 全跳过 / 一条没跑 / 有失败 / 报告不存在 / 报告不合法 ⇒ 全是 1。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "check_ci_ran.py"


def _load_tool():
    """按路径加载工具模块(tools/ 不是包,不能直接 import)。"""
    spec = importlib.util.spec_from_file_location("check_ci_ran_under_test", TOOL)
    assert spec and spec.loader, "加载 %s 失败" % TOOL
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(tmp_path: Path, cases: list[dict], *, root_tag: str = "testsuites",
                  module: str = "tests.test_x") -> Path:
    """写一份 junit 报告。

    `cases` 里每项: {"name": ..., "skipped": bool, "failed": bool, "classname": ...}

    **`classname` 默认给上模块名**:pytest 写的就是模块路径(`tests.test_x`),
    而闸门按**(模块, 函数名)**配对(第 49 轮:只按函数名配对时,
    别的套件里的同名函数能顶替名单点名的用例)。
    """
    body = []
    for case in cases:
        attrs = 'name="%s"' % case["name"]
        attrs += ' classname="%s"' % case.get("classname", module)
        inner = ""
        if case.get("skipped"):
            inner = "<skipped/>"
        elif case.get("failed"):
            inner = "<failure message=\"boom\"/>"
        body.append("<testcase %s>%s</testcase>" % (attrs, inner))
    if root_tag == "testsuite":
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<testsuite name="t" tests="%d" skipped="%d" failures="%d" errors="0">%s</testsuite>'
               % (len(cases),
                  sum(1 for c in cases if c.get("skipped")),
                  sum(1 for c in cases if c.get("failed")),
                  "".join(body)))
    else:
        xml = ('<?xml version="1.0" encoding="utf-8"?>'
               '<testsuites><testsuite name="t" tests="%d" skipped="%d" failures="%d" errors="0">%s'
               '</testsuite></testsuites>'
               % (len(cases),
                  sum(1 for c in cases if c.get("skipped")),
                  sum(1 for c in cases if c.get("failed")),
                  "".join(body)))
    path = tmp_path / "report.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def _write_roster(tmp_path: Path, must_run: dict, allow_skipped: dict | None = None) -> Path:
    path = tmp_path / "roster.json"
    payload = {"must_run": must_run}
    if allow_skipped is not None:
        payload["allow_skipped"] = allow_skipped
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run(tmp_path: Path, cases, must_run, suite: str = "tests/test_x.py", **kwargs) -> int:
    module = _load_tool()
    report = _write_report(tmp_path, cases, **kwargs)
    roster = _write_roster(tmp_path, must_run)
    return module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", suite,
    ])


# --------------------------------------------------------------------------
# 放行侧
# --------------------------------------------------------------------------

def test_a_complete_report_passes(tmp_path):
    assert _run(
        tmp_path,
        [{"name": "test_a"}, {"name": "test_b"}],
        {"tests/test_x.py": ["test_a", "test_b"]},
    ) == 0


def test_a_parametrized_case_is_matched_by_its_function_name(tmp_path):
    """名单里写的是**函数名**,报告里是**参数化后的用例名**(`test_x[gate]`)。

    不去掉参数后缀就永远匹配不上 ⇒ 兜底静默失效(查的是一条不存在的用例)。
    """
    assert _run(
        tmp_path,
        [{"name": "test_x[gate]"}, {"name": "test_x[evaluate]"}],
        {"tests/test_x.py": ["test_x"]},
    ) == 0


# --------------------------------------------------------------------------
# 拦截侧:每一种"没真跑"的形态都要红
# --------------------------------------------------------------------------

def test_a_named_test_missing_from_the_report_fails(tmp_path):
    assert _run(
        tmp_path,
        [{"name": "test_a"}],
        {"tests/test_x.py": ["test_a", "test_b"]},
    ) == 1


def test_a_named_test_that_was_skipped_fails(tmp_path):
    assert _run(
        tmp_path,
        [{"name": "test_a"}, {"name": "test_b", "skipped": True}],
        {"tests/test_x.py": ["test_a", "test_b"]},
    ) == 1


def test_a_partially_skipped_parametrized_test_fails(tmp_path):
    """参数化的两条里只要有一条被跳过 ⇒ 红(不是"至少有一条跑过"就算过)。"""
    assert _run(
        tmp_path,
        [{"name": "test_x[gate]"}, {"name": "test_x[evaluate]", "skipped": True}],
        {"tests/test_x.py": ["test_x"]},
    ) == 1


def test_an_entirely_skipped_report_fails(tmp_path):
    assert _run(
        tmp_path,
        [{"name": "test_a", "skipped": True}],
        {"tests/test_x.py": ["test_a"]},
    ) == 1


def test_an_empty_report_fails(tmp_path):
    assert _run(
        tmp_path,
        [],
        {"tests/test_x.py": ["test_a"]},
    ) == 1


def test_a_failing_test_fails(tmp_path):
    assert _run(
        tmp_path,
        [{"name": "test_a"}, {"name": "test_b", "failed": True}],
        {"tests/test_x.py": ["test_a", "test_b"]},
    ) == 1


def test_a_missing_report_fails(tmp_path):
    """报告文件不存在(比如 pytest 崩在写报告之前)⇒ 一样必须红。"""
    module = _load_tool()
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(tmp_path / "nope.xml"),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_broken_report_fails(tmp_path):
    """报告不是合法 XML ⇒ 红(不能当成"没有失败"放行)。"""
    module = _load_tool()
    report = tmp_path / "report.xml"
    report.write_text("<testsuites><testsuite>", encoding="utf-8")
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_single_testsuite_root_is_also_accepted(tmp_path):
    """有的工具写的是单个 `<testsuite>` 根——两种根都要认。"""
    assert _run(
        tmp_path,
        [{"name": "test_a"}],
        {"tests/test_x.py": ["test_a"]},
        root_tag="testsuite",
    ) == 0


def test_a_same_named_test_from_another_module_does_not_satisfy_the_roster(tmp_path):
    """**别的模块里的同名函数不能顶替**名单点名的用例(第 49 轮 codex L2)。

    闸门按「这份套件的用例真的跑了」判,不是按「有个同名的东西跑过了」判。
    旧实现只按函数名归拢 ⇒ 造一份 `classname` 指向别的模块的报告就能骗过它。
    """
    assert _run(
        tmp_path,
        [{"name": "test_a", "classname": "tests.some_other_module"}],
        {"tests/test_x.py": ["test_a"]},
    ) == 1


def test_a_class_based_case_still_satisfies_the_roster(tmp_path):
    """用例被放进 `class` 里时,报告里的 `classname` **带着类名** ⇒ 仍要匹配得上。

    第 55 轮(codex 第 2 轮的 **M3**):此前**逐条点名**那一步按**精确**模块名取,
    而同一文件里的 `_cases_of_suite` 按**前缀**取 —— 两处口径不一致 ⇒
    这类用例**永远**匹配不上 ⇒ CI **永久变红**,而且报的是**误导性的**理由
    (「点名点名的用例没有出现在报告里」——它明明跑了)。
    """
    assert _run(
        tmp_path,
        [{"name": "test_a", "classname": "tests.test_x.TestFoo"}],
        {"tests/test_x.py": ["test_a"]},
    ) == 0


def test_a_roster_whose_must_run_is_not_an_object_fails_instead_of_crashing(tmp_path):
    """名单结构不对(JSON 合法但不是对象)⇒ **红并给结论**,不是抛栈(第 49 轮 codex L4)。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": []}, ensure_ascii=False), encoding="utf-8")
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_roster_entry_that_is_not_a_list_fails_instead_of_crashing(tmp_path):
    """名单里某一份的条目不是列表 ⇒ 一样要红并给结论。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"must_run": {"tests/test_x.py": "test_a"}}, ensure_ascii=False),
                      encoding="utf-8")
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_report_with_a_non_numeric_count_fails_instead_of_crashing(tmp_path):
    """报告里的 `tests="many"` ⇒ 红并给结论(不是 ValueError 抛栈)。"""
    module = _load_tool()
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" tests="many" skipped="0" failures="0" errors="0">'
        '<testcase name="test_a" classname="tests.test_x"/></testsuite></testsuites>',
        encoding="utf-8")
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


# --------------------------------------------------------------------------
# 第 53 轮(codex 第 2 轮 M4):计数必须按报告的**内容**数,不读属性
# --------------------------------------------------------------------------

def _write_report_with_attributes(tmp_path: Path, cases_xml: str, attrs: str) -> Path:
    """写一份**属性与内容对不上**的报告(属性是"自述",内容才是"事实")。"""
    path = tmp_path / "report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites><testsuite name="t" %s>%s</testsuite></testsuites>' % (attrs, cases_xml),
        encoding="utf-8")
    return path


def test_the_counts_come_from_the_report_content_not_the_attributes(tmp_path):
    """计数必须**按内容**数出来(第 53 轮 codex M4)。

    此前计数只读 `<testsuite>` 上的属性 ⇒ 属性写 `failures="0"` 而实际带着
    `<failure>` 子元素时,闸门**看不出有失败**——改报告就能骗过它。
    这里直接钉住 `_load_report` 的返回值:总数/跳过/失败三项**都来自内容**。
    """
    module = _load_tool()
    report = _write_report_with_attributes(
        tmp_path,
        '<testcase name="test_a" classname="tests.test_x"/>'
        '<testcase name="test_b" classname="tests.test_x"><failure message="boom"/></testcase>'
        '<testcase name="test_c" classname="tests.test_x"><skipped/></testcase>',
        'tests="99" skipped="0" failures="0" errors="0"')
    cases, total, skipped, failed = module._load_report(report)
    assert total == 3, "总数必须按 <testcase> 元素数出来(不是属性里的 99)"
    assert skipped == 1, "跳过数必须按 <skipped> 子元素数出来(不是属性里的 0)"
    assert failed == 1, "失败数必须按 <failure>/<error> 子元素数出来(不是属性里的 0)"
    assert len(cases) == 3


def test_a_report_whose_attributes_hide_a_failure_fails(tmp_path, capsys):
    """拦截侧:属性说「没有失败」而内容里有 `<failure>` ⇒ **红**。

    这是 M4 那条洞的直接反例:旧实现读属性 ⇒ 直接放行。
    """
    module = _load_tool()
    report = _write_report_with_attributes(
        tmp_path,
        '<testcase name="test_a" classname="tests.test_x"/>'
        '<testcase name="test_b" classname="tests.test_x"><failure message="boom"/></testcase>',
        'tests="2" skipped="0" failures="0" errors="0"')
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a", "test_b"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1
    out = capsys.readouterr().out
    assert "有 1 条失败/错误" in out, "失败数必须按内容数出来(输出:%s)" % out


def test_a_report_whose_attributes_hide_a_skip_fails(tmp_path, capsys):
    """拦截侧:属性说「没有跳过」而内容里整份都被 `<skipped>` ⇒ **红**。

    旧实现读属性 ⇒ `skipped=0` ⇒ 「全部被跳过」这条检查**不触发** ⇒ 放行。
    """
    module = _load_tool()
    report = _write_report_with_attributes(
        tmp_path,
        '<testcase name="test_a" classname="tests.test_x"><skipped/></testcase>',
        'tests="1" skipped="0" failures="0" errors="0"')
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1
    out = capsys.readouterr().out
    assert "都被跳过" in out, "跳过数必须按内容数出来(输出:%s)" % out


def test_a_report_with_no_testcases_fails_even_when_the_attributes_claim_tests(tmp_path, capsys):
    """拦截侧:属性写 `tests="5"` 而报告里**一条 `<testcase>` 都没有** ⇒ 红。

    「一条都没跑」这条判据必须**按内容**判:属性说 5 条不算数。
    """
    module = _load_tool()
    report = _write_report_with_attributes(tmp_path, "", 'tests="5" skipped="0" failures="0" errors="0"')
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1
    out = capsys.readouterr().out
    assert "一条测试都没跑" in out, "「一条都没跑」必须按内容判(输出:%s)" % out


def test_an_unlisted_suite_that_did_not_run_fails(tmp_path):
    """拦截侧:显式允许「不做点名核对」**不等于**允许「这份套件没跑」。

    全局核对看的是**整份报告**——别的套件跑了就够 ⇒ 挡不住这一份压根没跑。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a", "classname": "tests.some_other_module"}])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
        "--allow-unlisted-suite",
    ]) == 1


def test_an_unlisted_suite_that_was_entirely_skipped_fails(tmp_path):
    """拦截侧:不在名单里的那一份**整份被跳过** ⇒ 红。

    「整份一刀切跳过时 pytest 退出码仍是 0」——这正是这道闸门存在的理由,
    名单外的套件同样适用。

    报告里**故意再放一条别的套件的通过用例**:否则全局的「全部被跳过」
    就会先一步挡住,这条用例就分不清是**哪一条**判据在起作用。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [
        {"name": "test_a", "skipped": True},
        {"name": "test_b", "classname": "tests.some_other_module"},
    ])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_b"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
        "--allow-unlisted-suite",
    ]) == 1


def test_an_unlisted_suite_that_ran_among_others_passes(tmp_path):
    """放行侧:这一份**确实跑了**(只是不在名单里)⇒ 0。

    没有这一条,上一条就可能是「凡不在名单里的一律红」的假红。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [
        {"name": "test_a"},
        {"name": "test_b", "classname": "tests.some_other_module"},
    ])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_b"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
        "--allow-unlisted-suite",
    ]) == 0


def test_the_conclusion_line_says_which_check_ran_for_an_unlisted_suite(tmp_path, capsys):
    """结论行必须**自证**做的是哪种核对(放行侧)。

    名单外那条路径做的是「这一份在报告里跑了几条」,**不是**「点名核对」——
    结论行若写成「点名核对 0 条」,读的人会以为**什么都没查**
    (仓库的老教训:结论行必须自证,不能只给退出码)。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [
        {"name": "test_a"},
        {"name": "test_b", "classname": "tests.some_other_module"},
    ])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_b"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
        "--allow-unlisted-suite",
    ]) == 0
    out = capsys.readouterr().out
    assert "在报告里 1 条" in out, "结论行必须说清这条路径上做的是哪种核对(输出:%s)" % out
    assert "点名核对 0 条" not in out, "结论行不能自称「点名核对 0 条」(读起来像什么都没查)"


def test_the_conclusion_line_says_the_named_checks_for_a_rostered_suite(tmp_path, capsys):
    """放行侧:在名单里的那一份,结论行必须报出**点名核对了几条**。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a", "classname": "tests.test_x"}])
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 0
    out = capsys.readouterr().out
    assert "点名核对 1 条" in out, "结论行必须报出点名核对的条数(输出:%s)" % out


def test_the_conclusion_line_discloses_how_many_may_be_skipped(tmp_path, capsys):
    """结论行必须说清「点名核对的几条里,几条是**允许跳过**的」。

    第 54 轮(codex M3 的延伸):名单分两段(`must_run` / `allow_skipped`)之后,
    「点名核对 N 条」里的 N 是**两段合起来**的总数——只报总数,读的人会以为
    **那 N 条全都必须真跑**。这正是 M3 那条「结论行必须自证」要防的事:
    允许跳过的那几条在 CI 上**可能整条不生效**,而结论行没说。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [
        {"name": "test_a", "classname": "tests.test_x"},
        {"name": "test_b", "classname": "tests.test_x", "skipped": True},
    ])
    roster = _write_roster(tmp_path, {"tests/test_x.py": ["test_a"]},
                           allow_skipped={"tests/test_x.py": ["test_b"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 0
    out = capsys.readouterr().out
    assert "点名核对 2 条" in out, "结论行必须报出点名核对的总数(输出:%s)" % out
    assert "必须真跑 1 条" in out, "结论行必须说清其中几条是必须真跑的(输出:%s)" % out
    assert "允许跳过 1 条" in out, "结论行必须说清其中几条是允许跳过的(输出:%s)" % out


def test_the_real_roster_is_readable_and_not_empty():
    """仓库里那份名单必须**存在**且非空(否则 CI 侧那道闸门查了个空)。"""
    roster = ROOT / "tests" / "guard_roster.json"
    assert roster.exists(), "%s 不存在" % roster
    data = json.loads(roster.read_text(encoding="utf-8"))
    assert data["must_run"], "must_run 是空的"
    for suite, names in data["must_run"].items():
        assert names, "%s 的名单是空的" % suite


@pytest.mark.parametrize("suite", ["tests/test_fs_capability_closure.py",
                                   "tests/test_research_boundary.py"])
def test_every_roster_entry_exists_in_its_suite(suite):
    """名单里的名字必须**真的存在**于对应套件(否则兜底查了个空 ⇒ 静默失效)。"""
    import ast

    path = ROOT / Path(*suite.split("/"))
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }
    data = json.loads((ROOT / "tests" / "guard_roster.json").read_text(encoding="utf-8"))
    listed = set(data["must_run"][suite])
    assert listed, "%s 的名单是空的" % suite
    assert not (listed - names), "名单里有不存在的用例:%s" % sorted(listed - names)


# --------------------------------------------------------------------------
# 静默降级:点名核对一条都做不了时,**不许**当成通过
# --------------------------------------------------------------------------

def test_a_suite_missing_from_the_roster_fails(tmp_path):
    """拦截侧:点名的那份套件**不在名单里** ⇒ 红。

    这是本轮复核发现的洞:此前只打印一句「提示」就继续,于是
    「名单里被删掉了这一份」⇒ 点名核对一条都不做 ⇒ 闸门**静默降级**成
    只看总数 ⇒ 全绿。名单少一行也会被守卫侧那条「双向相等」抓到,
    但那是**另一条防线**;闸门自己必须 fail-closed。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_an_unlisted_suite_passes_only_when_explicitly_allowed(tmp_path):
    """放行侧:显式 `--allow-unlisted-suite` ⇒ 只做全局核对 ⇒ 0。

    这个口子是**故意开给**并集回归那份套件的(它有按依赖跳过的用例,
    不在名单里)。默认**不开**:开了它,「名单被删一行」就再也抓不到了。
    """
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = _write_roster(tmp_path, {"tests/test_other.py": ["test_a"]})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
        "--allow-unlisted-suite",
    ]) == 0


def test_an_empty_roster_entry_fails(tmp_path):
    """拦截侧:名单里有这一份套件、但**条目是空的** ⇒ 红(同样是静默降级)。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = _write_roster(tmp_path, {"tests/test_x.py": []})
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_missing_roster_fails_instead_of_crashing(tmp_path):
    """拦截侧:名单文件读不到 ⇒ 红(而不是抛异常崩掉——崩掉也是红,但没有结论)。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(tmp_path / "nope.json"),
        "--suite", "tests/test_x.py",
    ]) == 1


def test_a_broken_roster_fails_instead_of_crashing(tmp_path):
    """拦截侧:名单不是合法 JSON ⇒ 红。"""
    module = _load_tool()
    report = _write_report(tmp_path, [{"name": "test_a"}])
    roster = tmp_path / "roster.json"
    roster.write_text("{不是 json", encoding="utf-8")
    assert module.main([
        "--junitxml", str(report),
        "--roster", str(roster),
        "--suite", "tests/test_x.py",
    ]) == 1
