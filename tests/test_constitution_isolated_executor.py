"""「候选在独立进程里执行」的执行入口对第四层的**有条件豁免**(Issue #29 S1;ADR 拍板点 3 的一部分,2026-11-26 用户确认)。

宪法守卫要求:谁执行候选代码,谁就必须在同一进程里对受保护对象(`risk` / `strategies` / `vnpy_*`)做快照与指纹比对。
候选宿主(`scripts/research_candidate_host.py`)运行在**刻意没有这些包**的进程里(A1 验收:候选里 `import risk` 必须失败),
那里没有受保护对象可比;强行调用第四层只会把框架模块带进候选进程,违背 S1 的目的。

所以豁免是**有条件的**,条件可被 AST 验证:这个文件**不 import 任何仓库本地模块、不做动态导入**
(只允许标准库、numpy 与候选侧运行时包 `candidate`)。只要它一 import 仓库模块,豁免立刻失效、守卫重新要求第四层。
豁免**只认登记在册的那个文件名**,不认「长得像宿主」的函数体——别的新执行入口照旧不得裸奔。
"""
from __future__ import annotations

import pytest

HOST_REL = "scripts/research_candidate_host.py"

# 宿主的典型形态:只用标准库 + numpy + candidate,在本进程里 exec 候选
ISOLATED_BODY = '''
import json
import os
import sys


def _exec_candidate(source, params, bars):
    from candidate.protocol import pack_signals
    import numpy as np
    np.random.seed(0)
    module = {}
    exec(compile(source, "<candidate-source>", "exec"), module)
    return pack_signals(module["Candidate"](params).run(bars))
'''


def _root(tmp_path):
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _problems(tmp_path, rel, body):
    from tools.check_constitution_history import check_executor_completeness
    root = _root(tmp_path)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return check_executor_completeness(root)


# ---------------------------------------------------------------- 豁免成立


def test_the_registered_isolated_host_with_only_safe_imports_is_exempt(tmp_path):
    assert _problems(tmp_path, HOST_REL, ISOLATED_BODY) == []


# ---------------------------------------------------------------- 豁免只认登记的文件名


def test_the_exemption_is_by_registered_file_name_not_by_body_shape(tmp_path):
    problems = _problems(tmp_path, "scripts/brand_new_runner.py", ISOLATED_BODY)
    assert any("brand_new_runner" in p and "不得裸奔" in p for p in problems), problems


# ---------------------------------------------------------------- 条件不成立 ⇒ 豁免失效,守卫重新要求第四层


@pytest.mark.parametrize("extra, why", [
    ("import risk.constitution as const\n", "import 受保护的框架包"),
    ("from risk import constitution\n", "from … import 框架包"),
    ("import strategies.trend_atr_strategy\n", "import 策略包"),
    ("import scripts.backtest_lib\n", "import 仓库脚本"),
    ("from tools import check_domain\n", "import 仓库工具"),
    ("import research_sandbox\n", "import 研究沙盒模块"),
    ("import vnpy\n", "import 交易栈"),
    ("from . import sibling\n", "相对导入"),
    # 相对导入的模块名恰好是允许名单里的名字:`from .json import x` 指的是**同目录的 json.py**,不是标准库。
    # 没有「相对导入一律算违规」这条规则时,它会被当成「只用标准库」而误放行(变异验证抓到的缺口:
    # 上面那条 `from . import sibling` 的 module 是 None,会被「模块名不在名单里」顺带拦住,删规则测试也不红)。
    ("from .json import loads\n", "相对导入同名标准库"),
    ("from ..candidate import protocol\n", "相对导入同名候选运行时包"),
    ("import importlib\nimportlib.import_module('risk')\n", "动态导入(importlib)"),
    ("__import__('risk')\n", "动态导入(__import__)"),
])
def test_the_exemption_is_withdrawn_when_the_host_imports_anything_repo_local(tmp_path, extra, why):
    problems = _problems(tmp_path, HOST_REL, extra + ISOLATED_BODY)
    assert any(HOST_REL in p and "豁免" in p for p in problems), (why, problems)


def test_a_host_that_does_call_the_fourth_layer_needs_no_exemption(tmp_path):
    """放行侧:宿主若真的自己走第四层(import 了框架包并成对快照/比对),按普通执行入口通过——豁免不是唯一出路。"""
    body = (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    return const.check_protected_state(before, const.snapshot_protected_state())\n")
    assert _problems(tmp_path, HOST_REL, body) == []


# ---------------------------------------------------------------- 真实仓库


def test_the_shipped_repository_passes_the_completeness_check():
    from pathlib import Path

    from tools.check_constitution_history import check_executor_completeness
    assert check_executor_completeness(Path(__file__).resolve().parents[1]) == []


# ---------------------------------------------------------------- codex 对豁免的专项复核(2 个 High)后的加固
#
# 1. 条件判据原先只认「调用的字面形态」且允许名单是整个标准库:别名 / 取属性 / 字符串 / `importlib`·`runpy`·`subprocess`
#    全部绕过。现在:允许名单收紧为宿主**实际用到的** 11 个模块;动态导入原语**引用即拒**;拒绝写 `sys.modules`;
#    拒绝对常量字符串的 exec/eval。静态判据是尽力而为——真正的保证是运行期 A1 探针(候选进程里 import risk 必失败)。
# 2. 豁免表与条件检查器本身没人看管:钉死登记内容、钉死检查器确实在被调用、钉死豁免所依赖的**性质**(宿主只作为子进程源文本)。

import ast  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ADR = REPO / "docs" / "adr" / "20261115-candidate-process-isolation.md"


@pytest.mark.parametrize("extra, why", [
    ("from importlib import import_module as im\nim('risk')\n", "importlib 别名调用"),
    ("import importlib.util\n", "importlib.util"),
    ("imp = __import__\nimp('risk')\n", "__import__ 的别名(引用即拒,不只拒调用)"),
    ("getattr(__builtins__, '__import__')('risk')\n", "__builtins__ 取属性"),
    ("exec('import risk')\n", "exec 常量字符串"),
    ("eval('__import__(\"risk\")')\n", "eval 常量字符串"),
    ("import runpy\nrunpy.run_path('scripts/x.py')\n", "runpy"),
    ("import subprocess\n", "subprocess"),
    ("import socket\n", "socket"),
    ("import shutil\n", "shutil"),
    ("import builtins\n", "builtins"),
    ("import sys\nsys.modules['risk'] = object()\n", "sys.modules 下标写入"),
    ("import sys\nsys.modules.update({'risk': 1})\n", "sys.modules.update"),
    ("import sys\nsys.modules.setdefault('risk', 1)\n", "sys.modules.setdefault"),
    ("import sys\ndel sys.modules['json']\n", "sys.modules 删除"),
    # **不经过 import** 就拿到加载器——这才是「Attribute 形态引用即拒」要防的:原先它的每个用例都先因为
    # `import importlib` / `import runpy` 不在允许名单里被拦住,删掉这条规则测试也不红(变异验证抓到的缺口)。
    # 仓库自己在第 21 轮踩过这条:`__loader__.exec_module(m)` 用**真实内建**把代码再跑一遍。
    ("__loader__.exec_module(object())\n", "模块自己的 __loader__.exec_module"),
    ("__spec__.loader.exec_module(object())\n", "__spec__.loader.exec_module"),
    ("__spec__.loader.load_module('risk')\n__spec__.loader.import_module('risk')\n", "loader 上的 import_module"),
])
def test_the_exemption_is_withdrawn_for_aliased_or_dynamic_ways_to_reach_framework_code(tmp_path, extra, why):
    problems = _problems(tmp_path, HOST_REL, extra + ISOLATED_BODY)
    assert any(HOST_REL in p and "豁免" in p for p in problems), (why, problems)


def test_what_the_real_host_legitimately_does_is_still_allowed(tmp_path):
    """放行侧:宿主真实在做的事(读 sys.modules、改 sys.path、ctypes 调 prctl、exec 变量里的候选源码)不得被误伤。"""
    body = ISOLATED_BODY + '''

def _legit():
    import ctypes
    main_module = sys.modules.get("__main__")
    sys.path.insert(0, "/runtime")
    ctypes.CDLL(None).prctl(1, 9, 0, 0, 0)
    code = compile("x = 1", "<candidate-source>", "exec")
    exec(code, {})
    return main_module
'''
    assert _problems(tmp_path, HOST_REL, body) == []


# ---------------------------------------------------------------- 豁免表与检查器本身被钉死


def test_the_exemption_registry_is_pinned_and_documented_in_the_adr():
    import tools.check_constitution_history as guard
    assert guard._ISOLATED_EXECUTOR_FILES == ("scripts/research_candidate_host.py",), \
        "豁免清单变了:要加文件须同时改这条测试与 ADR 的「宪法守卫的有条件豁免」一节(人看得见的评审)"
    assert guard._ISOLATED_EXECUTOR_ALLOWED_IMPORTS == frozenset({
        "__future__", "candidate", "ctypes", "json", "numpy", "os", "pathlib", "random", "sys", "traceback", "types"})
    section = ADR.read_text(encoding="utf-8").split("## 宪法守卫的有条件豁免", 1)[1]
    for rel in guard._ISOLATED_EXECUTOR_FILES:
        assert f"`{rel}`" in section or rel in section, f"ADR 没有登记豁免文件 {rel}"


def test_the_completeness_check_really_consults_the_condition_checker():
    """把 `_isolated_executor_violations` 的调用换成常量,守卫的命令行照样 exit 0——所以在测试里钉住这个调用点。"""
    tree = ast.parse((REPO / "tools" / "check_constitution_history.py").read_text(encoding="utf-8"))
    func = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "check_executor_completeness")
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "_isolated_executor_violations"]
    assert calls, "check_executor_completeness 不再调用 _isolated_executor_violations:豁免成了无条件的"


# ---------------------------------------------------------------- 豁免赖以成立的**性质**

def _tracked(pattern: str) -> list[Path]:
    done = subprocess.run(["git", "ls-files", pattern], cwd=REPO, capture_output=True, text=True, check=True)
    return [REPO / line for line in done.stdout.splitlines() if line.strip()]


def test_the_host_is_never_imported_only_launched_as_subprocess_source():
    """豁免的理由是「宿主在没有受保护对象的**子进程**里执行候选」。若有别的模块 import 了宿主,
    候选就会在**那个模块所在的进程**里被执行——豁免的前提就没了。"""
    offenders = []
    for path in _tracked("*.py"):
        if path.name == "research_candidate_host.py" or path.parts[-2:-1] == ("tests",):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            if any("research_candidate_host" in n for n in names):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, f"这些文件 import 了候选宿主(候选会在它们的进程里执行): {offenders}"


def test_the_launcher_feeds_the_host_source_to_a_subprocess_with_dash_c():
    tree = ast.parse((REPO / "scripts" / "research_candidate_process.py").read_text(encoding="utf-8"))
    popens = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Popen"]
    assert popens, "探针前提:启动器里应当有 Popen 调用"
    assert any(isinstance(a, ast.List) and any(isinstance(e, ast.Constant) and e.value == "-c" for e in a.elts)
               for n in popens for a in n.args), "启动器不再用 `-c <宿主源码>` 起子进程"


RUNTIME_PACKAGE_ALLOWED = frozenset({"__future__", "dataclasses", "datetime", "json", "struct", "numpy", "talib"})


def test_the_candidate_side_runtime_package_imports_no_framework_code():
    """宿主允许 `import candidate`——它就是仓库里的 `research/candidate/`。若这个包 import 了框架模块,
    「宿主只用标准库」就成了表面功夫(包内加一行 `import risk` 守卫仍全绿)。"""
    package = REPO / "research" / "candidate"
    siblings = {p.stem for p in package.glob("*.py")}
    problems = []
    for path in sorted(package.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                problems += [f"{path.name}: import {a.name}" for a in node.names if a.name.split(".")[0] not in RUNTIME_PACKAGE_ALLOWED]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    if node.level > 1 or (node.module or "").split(".")[0] not in siblings | {""}:
                        problems.append(f"{path.name}: 相对导入越出包 .{node.module}")
                elif (node.module or "").split(".")[0] not in RUNTIME_PACKAGE_ALLOWED:
                    problems.append(f"{path.name}: from {node.module} import …")
    assert not problems, problems


def test_no_repository_file_shadows_a_module_name_the_exemption_allows():
    """允许名单是按名字判的:仓库里若出现与这些名字同名的本地文件/目录(如 scripts/random.py),
    `import random` 就可能加载到仓库代码而守卫仍认为它是标准库。"""
    import tools.check_constitution_history as guard
    allowed = set(guard._ISOLATED_EXECUTOR_ALLOWED_IMPORTS) - {"candidate", "numpy", "__future__"}
    shadows = []
    for path in _tracked("*"):
        parts = path.relative_to(REPO).parts
        stem = path.stem
        if stem in allowed or any(part in allowed for part in parts[:-1]):
            if path.suffix in ("", ".py") or any(part in allowed for part in parts[:-1]):
                shadows.append(str(path.relative_to(REPO)))
    assert not shadows, f"仓库里有与允许名单同名的文件/目录(可能遮蔽标准库): {shadows[:10]}"
