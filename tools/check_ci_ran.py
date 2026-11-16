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

用法:
    python tools/check_ci_ran.py --junitxml REPORT --roster tests/guard_roster.json \
        --suite tests/test_fs_capability_closure.py
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROSTER = ROOT / "tests" / "guard_roster.json"


def _load_report(path: Path) -> tuple[list, int, int, int]:
    """读 junitxml ⇒ (用例列表, 总数, 跳过数, 失败数)。

    pytest 写的根是 `<testsuites>`,里面每个 `<testsuite>` 带
    `tests`/`skipped`/`failures`/`errors` 四个属性;有的工具写的是单个
    `<testsuite>` 根——两种都认。
    """
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases = list(root.iter("testcase"))

    def _count(attr: str) -> int:
        total = 0
        for suite in suites:
            raw = suite.get(attr, "0")
            try:
                total += int(raw)
            except (TypeError, ValueError):
                raise ValueError("报告里 %s 不是整数:%r" % (attr, raw))
        return total

    total = _count("tests")
    skipped = _count("skipped")
    failed = _count("failures") + _count("errors")
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
        help="允许「名单里没有这一份套件」(只做全局核对)。"
             "**默认不允许**:名单里缺这一份 ⇒ 点名核对一条都做不了 ⇒ 静默降级 ⇒ 假绿。"
             "只有像并集回归那种**故意不在名单里**的套件才显式打开这个口。",
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
        print("[FAIL] 报告里的计数不合法:%s(%s)" % (args.junitxml, exc))
        return 1

    print("报告:%s tests=%d skipped=%d failed=%d" % (args.junitxml, total, skipped, failed))

    problems: list[str] = []
    if total == 0:
        problems.append("一条测试都没跑(报告里 tests=0)")
    if failed > 0:
        problems.append("有 %d 条失败/错误" % failed)
    if total > 0 and skipped == total:
        problems.append("全部 %d 条都被跳过(整份一刀切跳过)" % skipped)

    # 名单核对:点名的用例必须**在报告里、且不是 skipped**。
    try:
        roster = json.loads(args.roster.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print("[FAIL] 名单读不了:%s(%s)" % (args.roster, exc))
        return 1
    must_run = roster.get("must_run", {})
    # **结构校验**:名单是 JSON,但 JSON 合法 ≠ 结构对。
    # 此前 `roster.get("must_run", {})` 之后直接 `.items()`/`.extend()`:
    # 顶层是数组 ⇒ AttributeError ⇒ 抛栈(退出码虽非 0,但**没有结论**),
    # 与仓库「崩掉也要给结论行」的规矩不符(第 49 轮 codex L4)。
    if not isinstance(must_run, dict):
        print("[FAIL] 名单里的 must_run 不是对象(是 %s):%s" % (type(must_run).__name__, args.roster))
        return 1
    for suite_path, names in must_run.items():
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            print("[FAIL] 名单里 %s 的条目不是字符串列表:%s" % (suite_path, args.roster))
            return 1
    wanted: list[str] = []
    for suite_path, names in must_run.items():
        if args.suite and suite_path != args.suite:
            continue
        wanted.extend(names)

    # 报告里按**(模块, 函数名)**归拢(参数化的多条归到同一个函数名下)。
    #
    # **为什么必须带上模块**(第 49 轮 codex L2):只按函数名归拢时,
    # 另一个套件里的**同名**函数就能顶替名单点名的用例 ⇒
    # 闸门证明不了「跑的是那份套件」。实测(造一份 classname 指向别的模块的报告)
    # 旧实现照样返回 0。
    by_base: dict[tuple[str, str], list] = {}
    for case in cases:
        module = _module_name(case)
        by_base.setdefault((module, _base_name(_case_name(case))), []).append(case)
    for name in wanted:
        matched = []
        for suite_path in must_run:
            if args.suite and suite_path != args.suite:
                continue
            if name not in must_run[suite_path]:
                continue
            matched.extend(by_base.get((_module_of_suite(suite_path), name), []))
        if not matched:
            problems.append("名单点名的用例**没有出现在报告里**:%s" % name)
            continue
        for case in matched:
            if _is_skipped(case):
                problems.append("名单点名的用例**被跳过**:%s(%s)"
                                % (name, _case_name(case)))
    if args.suite and args.suite not in must_run:
        if not args.allow_unlisted_suite:
            print("[FAIL] 名单里没有 %s 的条目:点名核对一条都做不了 ⇒ 静默降级。"
                  "要么把这份套件登记进名单,要么显式加 --allow-unlisted-suite。"
                  % args.suite)
            return 1
        print("[提示] 名单里没有 %s 的条目(已显式允许):这次只做全局核对(总数/失败/全跳过)"
              % args.suite)
    elif args.suite and not wanted:
        print("[FAIL] 名单里 %s 的条目是**空的**:点名核对一条都做不了 ⇒ 静默降级。" % args.suite)
        return 1

    if problems:
        for problem in problems:
            print("[FAIL] %s" % problem)
        return 1
    print("[OK ] 该跑的都跑了(点名核对 %d 条)" % len(wanted))
    return 0


if __name__ == "__main__":
    sys.exit(main())
