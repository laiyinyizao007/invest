"""CI 侧的「真的跑了吗」闸门(第 48 轮,codex 第 2 轮复核的 H1/M5/M6)。

**为什么要有这个工具**:pytest 的**退出码分不出**「全跑了」与「整份被跳过」——
`pytestmark = pytest.mark.skipif(True)` 会让整份文件 skipped 而退出码仍是 **0**,
CI 那一步照样绿。而**纪律守卫自己**(tests/test_research_boundary.py)此前
**没有任何 CI 级兜底**:把它整份跳过、或把其中几条守卫用例删掉,CI 都不会红。
第 46~47 轮消灭的失败模式(「闸门配好了,实际没跑」)因此仍然能打在守卫身上。

**这个工具做三件事**(全部 fail-closed):
1. 报告里**一条测试都没跑** ⇒ 红;
2. **全部**被跳过 ⇒ 红;
3. **名单里点名的用例**没有出现,或出现了却是 skipped ⇒ 红
   ——这条是关键:它把「某条防线没跑」从「看不出来」变成「CI 直接红」。
   名单在 `tests/guard_roster.json`(机器可读,守卫与本工具共读同一份)。

**计数一律按报告的「内容」数出来**(第 53 轮 codex M4),不读 `<testsuite>` 的属性:
属性是**自述**,内容是**事实**——属性写 `failures="0"` 而实际带着 `<failure>`
的子元素时,读属性就看不出有失败(改报告即可骗过闸门)。

**名单有两段**(第 54 轮 codex H4):
- `must_run`:这些用例必须出现在报告里**且不能被跳过**;
- `allow_skipped`:这些用例必须出现在报告里,但**允许被跳过**
  (如「本地没有账本就跳过」那种**数据依赖**的跳过)。
  两段不许重叠,也不许悬空(在 allow_skipped 却不在 must_run 的套件 ⇒ 判红)。

用法:
    python tools/check_ci_ran.py --junitxml REPORT --roster tests/guard_roster.json \
        --suite tests/test_fs_capability_closure.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROSTER = ROOT / "tests" / "guard_roster.json"


def _load_report(path: Path) -> tuple[list, int, int, int]:
    """读 junitxml ⇒ (用例列表, 总数, 跳过数, 失败数)。

    **计数一律按报告的「内容」数出来**(第 53 轮 codex M4):
      - 总数 = `<testcase>` 元素有几个;
      - 跳过 = **用例的** `<skipped>` 子元素有几个;
      - 失败 = **用例的** `<failure>` / `<error>` 子元素有几个。

    此前**只读 `<testsuite>` 上的属性**(`tests`/`skipped`/`failures`/`errors`)
    ⇒ 报告写 `failures="0"` 而实际带着 `<failure>` 子元素时,闸门
    **看不出有失败**(直接放行)——计数与内容脱钩 ⇒ 改报告就能骗过闸门。
    属性**仍然校验是不是整数**(格式不对 ⇒ 判不了 ⇒ 不放行),
    但**不再作为计数依据**。

    **第 54 轮(codex L1)**:跳过数与失败数改成**逐用例**数
    (`case.find("skipped")` / `case.find("failure")`),与 `_is_skipped`
    认的是同一件事。此前用 `root.iter("skipped")`——**任意深度**都算,
    连 `<system-out>` 里嵌的 `<skipped/>` 都会把跳过数抬高 ⇒
    「全部被跳过」这条判据可能**假红**,汇总行也会报出并非用例级跳过的数。

    pytest 写的根是 `<testsuites>`,里面每个 `<testsuite>` 带那四个属性;
    有的工具写的是单个 `<testsuite>` 根——两种根都认。
    (**注意**:根是 `<testsuites>` 而里面**没有** `<testsuite>` 子元素时,
    底下没有属性可校验——那种报告本来就是空的。)
    """
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases = list(root.iter("testcase"))

    # --- 计数:一律按内容,且**逐用例** ---
    total = len(cases)
    skipped = sum(1 for case in cases if case.find("skipped") is not None)
    failed = sum(1 for case in cases
                 if case.find("failure") is not None or case.find("error") is not None)

    # --- 属性:只做**格式校验**,不做计数依据 ---
    for suite in suites:
        for attr in ("tests", "skipped", "failures", "errors"):
            raw = suite.get(attr)
            if raw is None:
                continue
            # 第 54 轮(codex L2):`int()` 接受 "-5" / " 5 " / "1_0" ——
            # 这些不是「非负整数」。属性虽然不参与计数,但「格式不对 ⇒ 判不了
            # ⇒ 不放行」这条承诺要**真的**成立,否则写一个 `tests="-5"` 的报告
            # 就能声称"负五条测试"。
            if not re.fullmatch(r"\d+", raw.strip()):
                raise ValueError("报告里 %s 不是非负整数:%r" % (attr, raw))
    return cases, total, skipped, failed


def _case_name(case) -> str:
    """用例名:`classname` 与 `name` 合起来(pytest 的 classname 常是模块路径)。"""
    name = case.get("name", "")
    cls = case.get("classname", "") or ""
    if cls and name.startswith(cls + "::"):
        name = name[len(cls) + 2:]
    return name


def _base_name(name: str) -> str:
    """去掉参数化后缀:`test_x[gate]` → `test_x`。

    名单里写的是**函数名**(人看的是函数名),报告里写的是**参数化后的用例名**。
    不去掉后缀就永远匹配不上 ⇒ 兜底静默失效(查的是一条不存在的用例)。
    """
    index = name.find("[")
    return name[:index] if index >= 0 else name


def _is_skipped(case) -> bool:
    return case.find("skipped") is not None


def _module_of_suite(suite_path: str) -> str:
    """套件路径 → pytest 报告里的模块名。

    `tests/test_x.py` ⇒ `tests.test_x`(pytest 的 `classname` 就是模块路径)。
    """
    rel = suite_path.replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[: -len(".py")]
    return rel.replace("/", ".")


def _module_name(case) -> str:
    """报告里这条用例属于哪个模块(junit 的 `classname`)。

    pytest 写的是**点号**模块名(`tests.test_x`);有的工具写的是文件路径
    (`tests/test_x.py`)⇒ 两种都归一成点号形态,免得匹配不上。
    """
    raw = (case.get("classname") or "").replace("\\", "/")
    if raw.endswith(".py"):
        raw = raw[: -len(".py")]
    return raw.replace("/", ".")


def _cases_of_suite(cases: list, suite_path: str, modules: list | None = None) -> list:
    """报告里属于**这一份套件**的用例。

    按模块名配对(`tests/test_x.py` ⇒ `tests.test_x`)。用**前缀**匹配:
    pytest 的 `classname` 在用例被放进 `class` 里时会带上类名
    (`tests.test_x.TestFoo`)⇒ 精确相等会漏掉它们。

    第 54 轮(codex L4):`modules` 可以把**已经算好的**模块名传进来 ——
    此前每条用例都要算两遍 `_module_name`(四次字符串归一化)。
    """
    module = _module_of_suite(suite_path)
    if modules is None:
        modules = [_module_name(case) for case in cases]
    return [case for case, name in zip(cases, modules)
            if name == module or name.startswith(module + ".")]


def validate_roster(roster) -> list[str]:
    """名单**结构**校验(第 55 轮之五,codex 第 2 轮的 H2)。

    **一份实现,三处共用**:接线自检(`tools/check_ci_wiring.py`)、
    闸门(`tools/ci_gate.py`)、闸门工具自己(`tools/check_ci_ran.py`)。
    此前三处**各写各的** ⇒ **坏名单的判法不一致** ⇒ 最严重的一条:

        `must_run: {}` ⇒ 闸门把每份套件都当成「不在名单里」
        ⇒ 自动加 `--allow-unlisted-suite`
        ⇒ **逐条点名核对整个消失** ⇒ 闸门照样打印「[OK] 闸门通过」。

    ⇒ 现在:**结构不对一律 fail-closed**,且三处判法一致。

    返回**问题清单**(空列表 = 结构没问题)。不抛异常——调用方自己决定怎么表态。
    """
    problems: list[str] = []
    if not isinstance(roster, dict):
        # 顶层是数组/字符串 ⇒ 此前 `roster.get(...)` 直接 AttributeError 抛栈,
        # **没有结论行**(与本工具自己的注释宣称「已按第 49 轮 L4 修好」相悖)。
        return ["名单的顶层不是对象(是 %s)" % type(roster).__name__]

    must_run = roster.get("must_run", {})
    allow_skipped = roster.get("allow_skipped", {})
    # **未知的顶层键**(第 55 轮之六,codex 第 3 轮的 M1):
    # 名单里多写一个键 ⇒ 没人知道它是什么意思 ⇒ 可能是**以为**写了就生效
    # (例如把 `must_skip` 写上去,以为能跳过某些用例)⇒ 实际什么都没发生。
    # ⇒ 未知的键一律判红。下划线开头的键当作文档说明(`_说明`),放行。
    known_top = {"must_run", "allow_skipped"}
    for key in roster:
        if not isinstance(key, str) or (key not in known_top and not key.startswith("_")):
            problems.append("名单里有**未知的顶层键**:%r(只认 must_run / allow_skipped;"
                            "下划线开头的当作文档说明放行)" % (key,))
    if not isinstance(must_run, dict):
        problems.append("名单里的 must_run 不是对象(是 %s)" % type(must_run).__name__)
    if not isinstance(allow_skipped, dict):
        problems.append("名单里的 allow_skipped 不是对象(是 %s)"
                        % type(allow_skipped).__name__)

    if isinstance(must_run, dict):
        if not must_run:
            # **空的 must_run ⇒ 点名核对一条都做不了** ⇒ 静默降级 ⇒ 假绿。
            problems.append("名单里的 must_run 是空的 ⇒ 逐条点名核对一条都做不了 ⇒ "
                            "静默降级 ⇒ 假绿")
        for suite_path, names in must_run.items():
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                problems.append("名单里 must_run 的 %s 条目不是字符串列表" % suite_path)
            elif not names:
                # **空列表**(第 55 轮之六,claude 第 3 轮的 L-1):
                # 这份套件被点名了,但**一条用例都没点名** ⇒ 逐条核对等于没做
                # ⇒ 闸门会「跑完 0 条」而照样报「[OK]」。
                problems.append("名单里 must_run 的 %s 条目是空的 ⇒ 这份套件一条用例"
                                "都没被点名 ⇒ 逐条核对等于没做" % suite_path)
    if isinstance(allow_skipped, dict):
        for suite_path, names in allow_skipped.items():
            if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
                problems.append("名单里 allow_skipped 的 %s 条目不是字符串列表" % suite_path)
            elif not names:
                # 同上:「允许跳过」却一条都不许跳过 ⇒ 这条允许是空转。
                problems.append("名单里 allow_skipped 的 %s 条目是空的 ⇒ 这条允许是空转"
                                % suite_path)

    if isinstance(must_run, dict) and isinstance(allow_skipped, dict):
        for suite_path, names in allow_skipped.items():
            if suite_path not in must_run:
                # 「允许跳过」却不在 must_run ⇒ 这条允许是**空转**(压根没被点名核对)。
                problems.append("名单里 %s 列在 allow_skipped,却不在 must_run:"
                                "「允许跳过」是空转" % suite_path)
                continue
            if (not isinstance(names, list)
                    or not isinstance(must_run.get(suite_path), list)):
                continue
            overlap = sorted(set(names) & set(must_run[suite_path]))
            if overlap:
                # 既要求真跑、又允许跳过 ⇒ 判据自相矛盾。
                problems.append("名单里 %s 的 %s 同时在 must_run 与 allow_skipped:"
                                "既要求真跑又允许跳过,判据自相矛盾"
                                % (suite_path, ", ".join(overlap)))
            # **注意(第 55 轮之六)**:这里**不做**「allow_skipped 的名字必须
            # 出现在 must_run 里」的名字级检查——那会**误伤合法名单**。
            # 本工具的语义是:`allow_skipped` 是**独立的一段**——
            # 那些用例**必须出现在报告里**,但**允许被跳过**;
            # 它们本来就不在 must_run 里(正因为预期会被跳过)。
            # 真名单就是这样:`tests/test_approval_ledger.py` 的
            # `test_reading_is_consistent_with_the_research_side` 就在
            # allow_skipped 段、且不在 must_run 段 ⇒ 名字级「悬空」是**正常**的。
    return problems


def main(argv: list[str] | None = None) -> int:
    # **Windows 兼容**(第 48 轮):默认控制台是 cp1252,打印中文会抛 UnicodeEncodeError
    # ⇒ 工具"崩"在打印阶段 ⇒ 退出码非 0 ⇒ 看起来像"红了",实际是**没给出结论**。
    # 这正是仓库反复吃过的亏("Linux 全绿 ≠ Windows 可用")。
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):        # pragma: no cover - 极端环境
        pass
    parser = argparse.ArgumentParser(description="检查 junit 报告里该跑的用例真的跑了")
    parser.add_argument("--junitxml", required=True, type=Path, help="pytest --junitxml 生成的报告")
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER, help="机器可读的名单(默认 tests/guard_roster.json)")
    parser.add_argument("--suite", default="", help="只核对名单里属于这份套件的条目")
    parser.add_argument(
        "--allow-unlisted-suite", action="store_true",
        help="允许「名单里没有这一份套件」(只做全局核对 + 这一份自己的核对)。"
             "**默认不允许**:名单里缺这一份 ⇒ 点名核对一条都做不了 ⇒ 静默降级 ⇒ 假绿。"
             "只有像并集回归那种**故意不在名单里**的套件才显式打开这个口。"
             "**打开这个口也不等于允许这份没跑**:它仍然必须在报告里、且不能整份被跳过。",
    )
    args = parser.parse_args(argv)

    if not args.junitxml.exists():
        print("[FAIL] 报告文件不存在:%s(pytest 崩在写报告之前?一样必须红)" % args.junitxml)
        return 1

    try:
        cases, total, skipped, failed = _load_report(args.junitxml)
    except ET.ParseError as exc:
        print("[FAIL] 报告不是合法 XML:%s(%s)" % (args.junitxml, exc))
        return 1
    except ValueError as exc:
        print("[FAIL] 报告里的属性不合法:%s(%s)" % (args.junitxml, exc))
        return 1

    print("报告:%s tests=%d skipped=%d failed=%d" % (args.junitxml, total, skipped, failed))

    problems: list[str] = []
    if total == 0:
        problems.append("一条测试都没跑(报告里没有 <testcase> 元素)")
    if failed > 0:
        problems.append("有 %d 条失败/错误" % failed)
    if total > 0 and skipped >= total:
        problems.append("全部 %d 条都被跳过(整份一刀切跳过)" % skipped)

    # 名单核对:点名的用例必须**在报告里、且不是 skipped**。
    try:
        roster = json.loads(args.roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[FAIL] 名单读不了:%s(%s)" % (args.roster, exc))
        return 1
    # **结构校验**(第 55 轮之五,codex 第 2 轮的 H2):**一份共享实现**
    # (`validate_roster`),接线自检与闸门也调它 ⇒ 三处判法一致。
    # 名单是 JSON,但 JSON 合法 ≠ 结构对;结构不对 ⇒ **一律 fail-closed**。
    roster_problems = validate_roster(roster)
    if roster_problems:
        for problem in roster_problems:
            print("[FAIL] 名单结构不对:%s(%s)" % (problem, args.roster))
        return 1
    must_run = roster.get("must_run", {})
    allow_skipped = roster.get("allow_skipped", {})

    def _names_for(suite_path: str) -> list[str]:
        """这一份套件要**逐条点名**的用例(必须真跑 + 允许跳过)。"""
        return list(must_run.get(suite_path, [])) + list(allow_skipped.get(suite_path, []))

    target_suites = [s for s in must_run if (not args.suite or s == args.suite)]
    wanted: list[str] = []
    for suite_path in target_suites:
        wanted.extend(_names_for(suite_path))
    must_count = sum(len(must_run.get(s, [])) for s in target_suites)
    allow_count = sum(len(allow_skipped.get(s, [])) for s in target_suites)

    # 报告里按**(模块, 函数名)**归拢(参数化的多条归到同一个函数名下)。
    #
    # **为什么必须带上模块**(第 49 轮 codex L2):只按函数名归拢时,
    # 另一个套件里的**同名**函数就能顶替名单点名的用例 ⇒
    # 闸门证明不了「跑的是那份套件」。实测(造一份 classname 指向别的模块的报告)
    # 旧实现照样返回 0。
    modules = [_module_name(case) for case in cases]          # 第 54 轮:只算一遍
    # **第 55 轮(codex 第 2 轮的 M3)**:此前这里按**(模块, 函数名)精确**取,
    # 而 `_cases_of_suite` 按**前缀**取 —— 两处口径不一致:
    #   - pytest 把用例放进 `class` 里时,报告里的 `classname` 是
    #     `tests.test_x.TestFoo` ⇒ 精确相等匹配不上 ⇒ 名单点名的用例
    #     「没有出现在报告里」⇒ **CI 永久变红**,而且报的是**误导性的**理由
    #     (真跑过了,只是模块名带了类名)。
    # 现在两处**同一条口径**:模块名相等**或**以「模块.」开头。
    by_base: dict[str, list] = {}
    for case, module in zip(cases, modules):
        by_base.setdefault(_base_name(_case_name(case)), []).append((module, case))
    for suite_path in target_suites:
        module = _module_of_suite(suite_path)
        skip_ok = set(allow_skipped.get(suite_path, []))
        for name in _names_for(suite_path):
            matched = [case for case_module, case in by_base.get(name, [])
                       if case_module == module or case_module.startswith(module + ".")]
            if not matched:
                problems.append("名单点名的用例**没有出现在报告里**:%s" % name)
                continue
            for case in matched:
                if _is_skipped(case) and name not in skip_ok:
                    problems.append("名单点名的用例**被跳过**:%s(%s)"
                                    % (name, _case_name(case)))
    # 结论行必须**自证**做的是哪种核对(第 53 轮):
    # 「点名核对 0 条」会被读成「什么都没查」——而这条路径上做的是
    # 「这一份在报告里跑了几条」,两者不是一回事。
    checked = ("点名核对 %d 条(必须真跑 %d 条,允许跳过 %d 条)"
               % (len(wanted), must_count, allow_count))
    if args.suite and args.suite not in must_run:
        if not args.allow_unlisted_suite:
            print("[FAIL] 名单里没有 %s 的条目:点名核对一条都做不了 ⇒ 静默降级。"
                  "要么把这份套件登记进名单,要么显式加 --allow-unlisted-suite。"
                  % args.suite)
            return 1
        print("[提示] 名单里没有 %s 的条目(已显式允许):这次做全局核对"
              "(总数/失败/全跳过)+ **这一份自己的**核对"
              "(必须出现在报告里、且不能整份被跳过)" % args.suite)
        # **不在名单里也得真的跑过**(第 53 轮 codex M2):
        # 显式允许"不做点名核对"≠ 允许"这份套件根本没跑"。
        # 全局核对只看得见**整份报告**——别的套件跑了就算数 ⇒
        # 这一份被整份跳过/压根没跑,全局核对照样绿。
        # 所以这里**单独**盯这一份:它必须在报告里出现,且不能整份被跳过。
        own = _cases_of_suite(cases, args.suite, modules)
        own_skipped = sum(1 for case in own if _is_skipped(case))
        if not own:
            # 第 54 轮(codex L6):不再**提前 return**——那样会把
            # 「有 N 条失败/错误」这类结论**吞掉**(只打印第一条就退出)。
            # 改成进 problems,统一在末尾打印 ⇒ 结论完整。
            problems.append("报告里没有 %s 的用例 ⇒ 这一份**没跑**"
                            "(全局核对看的是整份报告,别的套件跑了就够 ⇒ 挡不住这一份没跑)"
                            % args.suite)
        elif own_skipped == len(own):
            problems.append("%s 的 %d 条**全部被跳过**(整份一刀切跳过)" % (args.suite, own_skipped))
        # 第 54 轮(codex M3):「跑了 N 条」必须**自证**跑的是哪几条——
        # `own` 含**被跳过**的用例,直接报总数会**高报**。
        checked = ("%s 在报告里 %d 条(其中 %d 条被跳过;不做点名核对:名单里没有这一份)"
                   % (args.suite, len(own), own_skipped))
    elif args.suite and not wanted:
        print("[FAIL] 名单里 %s 的条目是**空的**:点名核对一条都做不了 ⇒ 静默降级。" % args.suite)
        return 1

    if problems:
        for problem in problems:
            print("[FAIL] %s" % problem)
        return 1
    print("[OK ] 该跑的都跑了(%s)" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
