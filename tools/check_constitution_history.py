#!/usr/bin/env python
"""不可变宪法的"只能追加"守卫:清单文件与代码内置保护项,相对历史提交都只增不减。

**为什么要有这个独立脚本**:这条守卫若寄生在 pytest 里,CI 就得把整个交易环境装上
(`tests/conftest.py` 会 import segment_ledger → bootstrap → vnpy),守卫在 CI 上根本跑不起来——
而"跑不起来"等于"没人执行",这份宪法的保护承诺就是空的(第 6 轮 codex 实测:
那个工作流在自己声明的环境里必然失败,退出码 4)。
所以守卫只用标准库 + git,CI 直接 `python tools/check_constitution_history.py`。

退出码:
- 0 通过;
- 1 发现问题(清单被删了条目、内置保护被删了、注册表与源码不一致);
- 2 环境不可用(浅克隆 / git 不可用)——**同样是失败**:查不了等于没查。
"""
from __future__ import annotations

import ast
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION_REL = "config/immutable_constitution.json"
CONSTITUTION_SOURCE_REL = "risk/constitution.py"

# 清单文件里"只能增不减"的字段。
CONSTITUTION_FIELDS = (
    "allowed_overrides", "protected_names", "protected_parameters",
    "forbidden_order_calls", "protected_module_attrs",
    "protected_entry_points", "protected_state_objects", "protected_paths",
)

# **白名单**类字段:语义相反,必须"只减不增"。
# allowed_overrides 是"候选**可以**重写什么"——多一项就是多放开一点底线,
# 与保护清单"只能追加"的规则正好相反,不能同规则比(第 7 轮 codex 实测:
# `BUILTIN_ALLOWED_OVERRIDES` 加一项 "get_size",守卫退出码 0,而候选重写
# `get_size()` 就能让 300 元单笔预算失效)。
CONSTITUTION_SHRINK_ONLY_FIELDS = ("allowed_overrides",)

# 代码里的内置保护项(字符串集合)。这些是清单文件之外的保护,删掉一个等于开一个洞,
# 而清单文件的历史比对看不见它们——它们在代码常量里,只能靠比对历史版本来看住。
BUILTIN_SETS = (
    "BUILTIN_ALLOWED_OVERRIDES",
    "BUILTIN_PROTECTED_NAMES",
    "BUILTIN_PROTECTED_PARAMETERS",
    "BUILTIN_FORBIDDEN_ORDER_CALLS",
    "BUILTIN_PROTECTED_MODULE_ATTRS",
    "BUILTIN_PROTECTED_READ_ATTRS",
    "BUILTIN_FORBIDDEN_DUNDERS",
    "BUILTIN_FORBIDDEN_BY_NAME_SETTERS",
    "BUILTIN_ALLOWED_TEMPLATES",
    "BUILTIN_PROTECTED_ENTRY_POINTS",
    "BUILTIN_PROTECTED_STATE_OBJECTS",
    "BUILTIN_PROTECTED_PATHS",
    "BUILTIN_PROTECTED_CONTAINERS",
    # 闸门函数名:改写它们 = 把「拒绝」这件事的执行者换掉(第 3 轮 codex 实测:
    # 候选把 `check_protected_state` 换成「调用时把原函数装回去」的假函数,
    # 指纹结论恒为空、事后身份核对也看不出来)。
    "BUILTIN_PROTECTED_GATE_ATTRS",
    # 容器上的变更型方法:第 11 轮 codex 的 High 1(`self.am.update_bar(fake_bar)`
    # 注入假 K 线,四层防线全绿)——这份名单是"容器只挡写、不挡调用"的补丁。
    "BUILTIN_PROTECTED_CONTAINER_SCOPE",
    "BUILTIN_PROTECTED_CONTAINER_MUTATORS",
    # 容器上的**视图型方法**:返回的是原数组的视图(共享内存),拿到就能原地改写
    # (第 12 轮 codex 的 High 1 变体①:`self.am.close[:]` / `reshape` 把 ATR 压塌)。
    "BUILTIN_PROTECTED_CONTAINER_VIEWS",
    # 运行期第四层(身份指纹)的覆盖范围:这三个包里的模块与类成员不许被候选改写。
    # 少一个包 = 那一个包里的对象不再受指纹保护,所以它同样"只能增不能减"。
    "PROTECTED_RUNTIME_PACKAGES",
    "_PROTECTED_PRELOAD_MODULES",
)

# 同为"白名单"语义的内置常量:只减不增。
# `ALLOWED_REPO_IMPORT_DIRS` 也在列:它是仓库内导入白名单,多一项就多放开一点
# 候选能 import 的东西(第 21 轮新增)。
BUILTIN_SHRINK_ONLY_SETS = ("BUILTIN_ALLOWED_OVERRIDES", "ALLOWED_REPO_IMPORT_DIRS")

# 沙盒层的内置 deny set 同样是"代码内置的保护",一并纳入比对。
#
# 第 25 轮(codex 的 Medium 8)补登记了三份:
# - DANGEROUS_MODULE_NAMES:危险模块名单(属性层与导入门**共用**的那一份);
# - FORBIDDEN_MODULE_PATHS:按**全名**禁用的路径(numpy.ctypeslib 之类);
# - CAPABILITY_MODULE_NAMES:会把执行能力递出来的模块(ctypes / subprocess)。
# 这三份都是"少一项 = 多放开一点能力",所以只能增不能减。
#
# 之前它们**既没登记、也没被解析过**:提取器不认 `frozenset({...})` 写法,
# `check_registry_completeness` 也就没把它们算成"漏登记"——
# 从危险名单里删掉 "ssl",守卫照样全绿(第 25 轮 codex 实测)。
# FORBIDDEN_KEY_STRINGS 也在其中:危险模块的**短名**只在"按键取"的语境里拒,
# 同样是一份 deny 名单(它原先解析不了,所以"漏登记"的检查一直没报)。
SANDBOX_SETS = ("FORBIDDEN_MODULES", "FORBIDDEN_CALLS", "FORBIDDEN_ATTRS", "FORBIDDEN_STRINGS",
                "DANGEROUS_MODULE_NAMES", "FORBIDDEN_MODULE_PATHS", "CAPABILITY_MODULE_NAMES",
                "FORBIDDEN_KEY_STRINGS",
                # Issue #31:候选执行窗口的审计钩子拒绝名单与"函数级能力库"名单。少一项 = 多放开一种能力,只增不减。
                "AUDIT_DENIED_EVENTS", "AUDIT_DENIED_EVENT_PREFIXES", "CAPABILITY_LIB_ROOTS", "UNREACHABLE_MEMBERS",
                "UNREACHABLE_ORIGINS", "BUILTIN_PRIMITIVE_NAMES", "AUDIT_DENIED_EVENTS_WINREG", "UNREACHABLE_QUALIFIED",
                # Issue #31 第 40 轮:"按名求值的执行器"名单(对象层按"模块名.成员名"拒)。
                # 少一项 = 少堵一个"把真内建递给候选"的执行器,只增不减。
                "EVALUATOR_MEMBERS",
                # Issue #31 第 40 轮:"禁止的求值帧"名单(事件层按"谁触发的这次 exec"拒)。
                # 少一项 = 少堵一个"替候选求值候选给的字符串"的求值点,只增不减。
                "FORBIDDEN_EVAL_FRAMES")

# 沙盒层的**允许**集合(第 21 轮新增的仓库内导入白名单):语义与 deny set 相反——
# 多一项 = 多放开一点仓库内的模块(候选能 import 的东西变多),所以它必须**只减不增**。
# 登记在 `BUILTIN_SHRINK_ONLY_SETS` 里;同时登记进注册表是为了让它进入
# 引用次数的守望(执行点不许消失)——按现有先例(`BUILTIN_ALLOWED_OVERRIDES`),
# 两个方向都被冻住:要改这个白名单必须显式改守卫,不能顺手加一项。
SANDBOX_ALLOW_SETS = ("ALLOWED_REPO_IMPORT_DIRS",)

# **受保护的编译正则**(第 40 轮,Medium 3):`DATA_PRIMITIVE_PATTERN` 是编译后的正则,
# 原来的提取器只认"字符串集合",于是它**从来没有被比对过**——把模式改宽(少拒几个名字)
# 守卫照样全绿(与第 25 轮 `frozenset` 那次一样的"没登记 = 没守着")。
# 正则没有"只增不减"的自然序(模式字符串与集合不同,无法逐项比较),所以按**冻结**处理:
# 模式字符串与历史版本不同就报错——要改必须显式改守卫(而不是顺手把模式放宽)。
SANDBOX_FROZEN_PATTERNS = ("DATA_PRIMITIVE_PATTERN",)

# **受保护的映射**(第 40 轮,Medium 3):`AUDIT_PATH_MUTATION_EVENTS` 是 {事件: 参数下标元组}——
# 审计钩子靠它决定"哪些事件要查哪个参数是不是落在允许目录里"。判据:
# - **键只能增**:少一个事件 = 少查一种会改文件的动作(比如把 `os.remove` 删掉);
# - **每个键的下标集合只能增**:少一个下标 = 少查一条路径(比如把 `os.rename` 的第二个下标删掉)。
SANDBOX_PROTECTED_MAPS = ("AUDIT_PATH_MUTATION_EVENTS",)

# 注册表:(文件相对路径, 该文件里受保护的常量名)
BUILTIN_REGISTRY = (
    (CONSTITUTION_SOURCE_REL, BUILTIN_SETS),
    ("scripts/research_sandbox.py", SANDBOX_SETS + SANDBOX_ALLOW_SETS),
)

# 清单**字段**的执行点也要守:删掉 `if node.attr in c.forbidden_order_calls:` 这个分支,
# 常量集合没变、清单文件也没变,但下单黑名单不再被执行——只有"字段名被引用的次数"会变。
# 字段名与常量名分开登记,免得两套语义混在一张表里。
FIELD_REGISTRY = ((CONSTITUTION_SOURCE_REL, CONSTITUTION_FIELDS),)

# 运行期第四层的**执行点**(第 10 轮 codex 的 Medium 4):
# 把 `check_protected_state(...)` 那一行删掉,常量集合没变、清单文件也没变,
# 但"指纹比对"就此不再发生——受保护对象被改写也照样放行。
# 这两个名字是**函数名**而不是常量,所以按"引用次数只许增不许减"来守。
CALL_SITE_REGISTRY = (
    ("scripts/backtest_lib.py", ("snapshot_protected_state", "check_protected_state")),
    ("scripts/research_api.py", ("snapshot_protected_state", "check_protected_state")),
    # TODO(#34):`risk/approved_guard.py` 也是执行候选代码的入口,应当登记进来。
    # 暂不登记的原因:登记表是**历史守卫**用的——它逐个历史版本检查「有没有把第四层摘掉过」。
    # 本分支早期的那几个提交里,第四层用的是**同名局部引用**的写法(已被「同名遮蔽」规则判违规),
    # 登记进来会让历史守卫把**那些已提交的版本**判成违规。
    # 等这个文件进了主干、有了「合规形态」的历史版本之后再登记(见 ADR 的遗留问题)。
)

# 回测入口的**时序**(第 14 轮 codex 的 G6):`backtest_lib` 没有裸 exec,执行候选的是
# `engine.run_backtesting()`。基准快照必须在它**之前**拍——把
# `_protected = _protected_state_snapshot()` 挪到回测之后,第四层就成了"自己跟自己比",
# 而守卫只验第二个实参,从不问第一个实参是什么时候拍的。
RUN_CALL_ORDER = (
    ("scripts/backtest_lib.py", "run_backtesting", ("_protected_state_snapshot", "snapshot_protected_state")),
)

# 单值常量:不是集合,没有"只增不减"可言。它只能通过"必须落在白名单里"来约束。
SINGLE_CONSTANTS = (("risk/constitution.py", "BUILTIN_TEMPLATE", "BUILTIN_ALLOWED_TEMPLATES"),)

# 会被完整性检查扫到的常量名前缀。
GUARDED_PREFIXES = ("BUILTIN_", "FORBIDDEN_")


class HistoryUnavailable(Exception):
    """环境不可用(浅克隆 / git 不可用):查不了等于没查,调用方必须当失败处理。"""


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """跑 git。显式按 utf-8 解码:系统默认编码在中文 Windows 上是 cp1252,
    解不出仓库里的中文注释,守卫会以 UnicodeDecodeError 崩掉(而不是干净地失败)。"""
    return subprocess.run(["git", *args], capture_output=True, cwd=str(root),
                          encoding="utf-8", errors="replace")


def require_full_history(root: Path) -> None:
    """浅克隆下 `git log` 只能看到一个提交,比对无从谈起——那是"查不了",必须当失败。"""
    shallow = git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        raise HistoryUnavailable(f"git 不可用: {shallow.stderr.strip()}")
    if shallow.stdout.strip() == "true":
        raise HistoryUnavailable("仓库是浅克隆,无法比对历史(需要完整历史,CI 请用 fetch-depth: 0)")


def const_string_set(source: str, names) -> dict[str, set[str]]:
    """从源码里取出若干常量的字符串集合。

    求值范围(受控,不是完整的 Python 求值器):
    - tuple / list / set 字面量,元素是字符串常量;
    - 常量之间的 `+` 或 `|`(如 `BUILTIN_PROTECTED_NAMES = (...) + BUILTIN_PROTECTED_PARAMETERS`);
    - 引用同文件里已解析的常量名。

    **不支持的形式一律不返回值**——调用方据此断言"每个应检查的常量都被解析到了",
    查不了就报错,不能让它冒充通过。早先的提取器只认 tuple/list,
    `BUILTIN_PROTECTED_NAMES` 是 BinOp,于是这组受保护成员从来没被比对过:
    删掉 `on_disconnect` 之后守卫照样全绿(第 5 轮 codex 实测)。"""
    def evaluate(node, seen):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = set()
            for elt in node.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    return None
                out.add(elt.value)
            return out
        if isinstance(node, ast.Call):
            # `frozenset({...})` / `set({...})` / `frozenset(("a", "b"))`。
            # 提取器原来不认这种写法,于是 CAPABILITY_MODULE_NAMES 与
            # FORBIDDEN_MODULE_PATHS 这两份名单**从来没被解析过**——
            # 守卫对它们的历史比对一直是空的,而"漏登记"的检查也因此不报。
            func = node.func
            if not (isinstance(func, ast.Name) and func.id in ("frozenset", "set")):
                return None
            if not node.args:
                return set()
            if len(node.args) != 1 or node.keywords:
                return None
            return evaluate(node.args[0], seen)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
            left = evaluate(node.left, seen)
            right = evaluate(node.right, seen)
            if left is None or right is None:
                return None
            return left | right
        if isinstance(node, ast.Name):
            return seen.get(node.id)
        return None

    found: dict[str, set[str]] = {}
    assignments = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments[node.target.id] = node.value      # 带注解的常量定义(`X: frozenset[str] = {...}`)

    # 按依赖顺序反复求解:常量之间可能互相引用(如 PROTECTED_NAMES 引用 PARAMETERS)
    pending = {name: assignments[name] for name in names if name in assignments}
    for _ in range(len(pending) + 1):
        progressed = False
        for name in list(pending):
            values = evaluate(pending[name], found)
            if values is not None:
                found[name] = values
                del pending[name]
                progressed = True
        if not progressed:
            break
    return found


def const_frozen_pattern(source: str, names) -> dict[str, str]:
    """从源码里取出若干 `re.compile("字面量")` 形式的常量(第 40 轮)。

    只认**单个位置参数是字符串字面量**的写法;`re.compile(常量)`、拼接出来的模式等一律取不到——
    取不到就报"无法解析"(查不了等于没查),不能让它冒充通过。"""
    found: dict[str, str] = {}
    for node in ast.parse(source).body:
        name = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        if name is None or name not in names or value is None:
            continue
        if not isinstance(value, ast.Call) or not (1 <= len(value.args) <= 2):
            continue
        func = value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "compile"
                and isinstance(func.value, ast.Name) and func.value.id == "re"):
            continue
        arg = value.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            found[name] = arg.value
    return found


def const_int_map(source: str, names) -> dict[str, dict[str, frozenset[int]]]:
    """从源码里取出 {名称: {键: 下标集合}} 形式的常量(第 40 轮:`AUDIT_PATH_MUTATION_EVENTS`)。

    值的元素必须是**整数常量**(布尔不算);其它形态取不到就报"无法解析"。"""
    def eval_indexes(node):
        if not isinstance(node, ast.Tuple) or not node.elts:
            return None
        out = set()
        for elt in node.elts:
            if not isinstance(elt, ast.Constant) or isinstance(elt.value, bool) \
                    or not isinstance(elt.value, int):
                return None
            out.add(elt.value)
        return frozenset(out)

    found: dict[str, dict[str, frozenset[int]]] = {}
    for node in ast.parse(source).body:
        name = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        if name is None or name not in names or value is None:
            continue
        if not isinstance(value, ast.Dict):
            continue
        out: dict[str, frozenset[int]] = {}
        ok = True
        for key, item in zip(value.keys, value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                ok = False
                break
            indexes = eval_indexes(item)
            if indexes is None:
                ok = False
                break
            out[key.value] = indexes
        if ok:
            found[name] = out
    return found


def removed_protections(current_source: str, older_source: str, names) -> dict[str, set[str]]:
    """从 older 到 current 被删掉的保护项:{常量名: 被删掉的条目}。

    这条比较是"内置保护只增不减"的全部逻辑,单独抽出来是为了能对它自己写测试——
    历史上这条检查对 `BUILTIN_PROTECTED_NAMES` 完全失效,而失效时没有任何测试会报警。"""
    current = const_string_set(current_source, names)
    older = const_string_set(older_source, names)
    removed = {}
    for name, values in older.items():
        diff = values - current.get(name, set())
        if diff:
            removed[name] = diff
    return removed


def _statically_false(node: ast.AST) -> bool:
    """条件在语法上就恒为假(`if False:`、`if 0:`、`if False and x:`、`if []:`)。

    死分支里的引用**不算数**:第 11 轮 codex 实测
    `if False and name in BUILTIN_FORBIDDEN_DUNDERS:` 能让守卫全绿——
    常量集合没变、引用次数也没变,但那圈检查**永远不执行**。

    只在**能证明**的时候才返回 True:`not X` 不是"恒假"(除非 X 恒真),
    早先的写法把 `not X` 当成恒假,于是每个 `if not ...:` 的分支体都不计数——
    计数被凭空压低,守卫会误报"引用次数减少"。"""
    if isinstance(node, ast.Constant):
        value = node.value
        return value is False or value is None or value == 0 or value == ""
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return any(_statically_false(value) for value in node.values)
        return all(_statically_false(value) for value in node.values)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _statically_true(node.operand)
    return False


def _statically_true(node: ast.AST) -> bool:
    """条件在语法上就恒为真(`if True:`、`if 1:`、`if [] or x:`…)。"""
    if isinstance(node, ast.Constant):
        value = node.value
        if value is True:
            return True
        if value is False or value is None:
            return False
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return bool(value)
        return False
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_statically_true(value) for value in node.values)
        return any(_statically_true(value) for value in node.values)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _statically_false(node.operand)
    return False


class _LiveReferenceCounter(ast.NodeVisitor):
    """统计**活代码**里对受保护常量的引用次数(AST 层面,只数 Load)。

    历史守卫原来只比对常量**集合**:把执行点删掉——例如把
    `if name in BUILTIN_FORBIDDEN_DUNDERS:` 整块删掉、或把它窄化成硬编码元组、
    或让 `_is_protected` 恒返回 False——常量集合本身没变,守卫照样全绿:
    常量还在,底线的**执行**没了(第 7 轮 codex 实测:五种改法守卫退出码全是 0)。

    所以这里把"引用次数"也纳入比对:只许增,不许减。少一次引用,就可能少一道闸。

    第 11 轮再补一条:**死分支里的引用不算引用**。`if False and name in ...:`
    让引用次数保持不变,却把那圈检查永远停掉——所以死分支不计数。"""

    def __init__(self, names):
        self.counts = {name: 0 for name in names}

    def visit_If(self, node: ast.If) -> None:
        if _statically_false(node.test):
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        if _statically_false(node.test):
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and node.id in self.counts:
            self.counts[node.id] += 1

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and node.attr in self.counts:
            self.counts[node.attr] += 1


def constant_usage_counts(source: str, names) -> dict[str, int]:
    """每个受保护常量在**活代码**里被引用的次数。"""
    counts = {name: 0 for name in names}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return counts
    counter = _LiveReferenceCounter(names)
    counter.visit(tree)
    return counter.counts


def usage_regressions_from_counts(current_counts: dict[str, int], older_source: str, names) -> dict[str, tuple[int, int]]:
    """拿**已经数好的**当前引用次数去比:只解析历史源码那一份。

    第 9 轮 codex 指出这里把同一份当前源码重复解析了很多遍(每个历史版本解析一次)。
    引用次数的语义没变,只是把重复的解析去掉。"""
    tracked = list(names)
    older_counts = constant_usage_counts(older_source, tracked)
    regressions = {}
    for name in tracked:
        if older_counts.get(name, 0) > current_counts.get(name, 0):
            regressions[name] = (older_counts[name], current_counts.get(name, 0))
    return regressions


def usage_regressions(current_source: str, older_source: str, names) -> dict[str, tuple[int, int]]:
    """引用次数减少的名字:{名字: (历史次数, 当前次数)}。

    单独抽出来是为了能对它自己写测试——历史上这条检查对"常量在、但执行点被删"
    完全失效,而失效时没有任何测试会报警。"""
    tracked = list(names)
    current_counts = constant_usage_counts(current_source, tracked)
    return usage_regressions_from_counts(current_counts, older_source, tracked)


def shrink_only_additions(current: set[str], older: set[str]) -> set[str]:
    """白名单里"多出来"的项(当前有、历史没有)——白名单只减不增,多一项就是多放开一点底线。"""
    return set(current) - set(older)


def check_protected_usage(root: Path) -> list[str]:
    """受保护常量与清单字段的**引用次数**相对历史提交不许减少(守的是执行点,不是常量本身)。"""
    problems: list[str] = []
    # (文件, 名字列表, 这些名字是不是"源码里的常量")
    registry = [(rel, names, True) for rel, names in BUILTIN_REGISTRY]
    registry += [(rel, names, False) for rel, names in FIELD_REGISTRY]
    registry += [(rel, names, False) for rel, names in CALL_SITE_REGISTRY]
    for rel, names, is_constant in registry:
        current_source = (root / rel).read_text(encoding="utf-8")
        if is_constant:
            current = const_string_set(current_source, names)
            # 已经被删掉的常量由集合比对报告(报一次就够);字段名没有集合可比,一律跟踪
            tracked = [name for name in names if name in current]
        else:
            tracked = list(names)
        if not tracked:
            continue
        # 当前源码的引用次数只数一遍(第 9 轮 codex:此前每个历史版本都重新解析一次)
        current_counts = constant_usage_counts(current_source, tracked)
        commits = _commits_touching(root, rel)
        for rev in commits:
            older_source = _read_at(root, rev, rel)
            if older_source is None:
                continue
            for name, (older_count, current_count) in usage_regressions_from_counts(
                    current_counts, older_source, tracked).items():
                problems.append(
                    f"{rel} 里的 {name} 的引用次数从 {older_count} 降到 {current_count}"
                    f"(提交 {rev[:8]}):常量还在但底线的执行点被删了(只能增不能减)")
    return problems


def check_shrink_only(root: Path) -> list[str]:
    """白名单类清单(`allowed_overrides`)相对历史提交**只减不增**。

    保护清单是"只能追加"(多一项=多一道保护),白名单正好相反:
    多一项=多放开一点底线。两种语义用同一条规则比,等于给白名单开了后门。"""
    problems: list[str] = []

    # 清单文件里的白名单字段
    commits = _commits_touching(root, CONSTITUTION_REL)
    if len(commits) >= 2:
        current = json.loads((root / CONSTITUTION_REL).read_text(encoding="utf-8"))
        for rev in commits[1:]:
            raw = _read_at(root, rev, CONSTITUTION_REL)
            if raw is None:
                continue
            older = json.loads(raw)
            for field in CONSTITUTION_SHRINK_ONLY_FIELDS:
                added = set(current.get(field, [])) - set(older.get(field, []))
                if added:
                    problems.append(
                        f"{CONSTITUTION_REL} 的 {field} 新增了 {sorted(added)}(提交 {rev[:8]} 之后)"
                        "(白名单只减不增:多一项就多放开一点底线)")

    # 代码里的白名单常量
    for rel, names in BUILTIN_REGISTRY:
        current_source = (root / rel).read_text(encoding="utf-8")
        current = const_string_set(current_source, names)
        for name in names:
            if name not in BUILTIN_SHRINK_ONLY_SETS or name not in current:
                continue
            for rev in _commits_touching(root, rel):
                older_source = _read_at(root, rev, rel)
                if older_source is None:
                    continue
                older = const_string_set(older_source, (name,)).get(name)
                if older is None:
                    continue
                added = shrink_only_additions(current[name], older)
                if added:
                    problems.append(
                        f"{rel} 里的 {name} 新增了 {sorted(added)}(提交 {rev[:8]} 之后)"
                        "(白名单只减不增:多一项就多放开一点底线)")
    return problems


def _read_at(root: Path, rev: str, rel: str) -> str | None:
    out = git(root, "show", f"{rev}:{rel}")
    if out.returncode != 0:
        return None            # 该版本里文件还不存在:没有内容可比
    return out.stdout


def _commits_touching(root: Path, rel: str) -> list[str]:
    # --full-history:默认的历史简化会把"与某个父提交内容相同"的合并提交剪掉。
    # 那种提交的内容与被剪掉的父提交完全一样,不比也不会漏掉内容;
    # 但带上 --full-history 后列表更全,读起来也更直白。
    out = git(root, "log", "--full-history", "--format=%H", "--", rel)
    if out.returncode != 0:
        raise HistoryUnavailable(f"git 不可用,无法检查 {rel}: {out.stderr.strip()}")
    return [h.strip() for h in out.stdout.split() if h.strip()]


def check_constitution_file(root: Path) -> list[str]:
    """清单文件相对历史提交只增不减(含 template 的配套约束)。"""
    commits = _commits_touching(root, CONSTITUTION_REL)
    if len(commits) < 2:
        # 仓库历史是完整的(浅克隆已在 require_full_history 里排除),清单确实只有一个版本:
        # 没有"上一次"可比,不存在被删的可能,平凡通过。
        return []

    def read(rev: str) -> dict:
        raw = _read_at(root, rev, CONSTITUTION_REL)
        if raw is None:
            raise HistoryUnavailable(f"无法读取历史版本 {rev}")
        return json.loads(raw)

    # "当前"取**工作区**的状态(而不是最近一次改动清单的那个提交):
    # 白名单可能是在另一个提交里改的(那个提交没碰清单),拿工作区的白名单比才说得通。
    current = json.loads((root / CONSTITUTION_REL).read_text(encoding="utf-8"))

    problems: list[str] = []
    for rev in commits[1:]:
        older = read(rev)
        for field in CONSTITUTION_FIELDS:
            removed = set(older.get(field, [])) - set(current.get(field, []))
            if removed:
                problems.append(f"{CONSTITUTION_REL} 的 {field} 里的 {sorted(removed)} 在后续提交中被删除了(清单只能追加)")
    # template 不在"只能增不减"之列(换模板是合法需求),但**每个版本的清单**都必须指向
    # "那个版本的内置白名单"允许的模板:只改清单里的 template 就等于绕开了白名单。
    # 早先的写法是"当前白名单 vs 旧白名单不相等才报",那只在白名单从未增长过时才成立——
    # 白名单一旦在历史上涨过,这条检查就永远不报(第 7 轮独立复核实测:隔离克隆里
    # 先给白名单加一项、再改清单的 template,守卫退出码 0)。
    # 现在按"逐个版本"检查:该版本清单里的 template 必须在**该版本**的白名单里。
    for rev in commits:
        manifest = read(rev)
        template = manifest.get("template")
        if not template:
            continue
        source = _read_at(root, rev, CONSTITUTION_SOURCE_REL)
        if source is None:
            continue                      # 那个版本里还没有宪法模块:无从判断,跳过
        templates = const_string_set(source, ("BUILTIN_ALLOWED_TEMPLATES",)).get(
            "BUILTIN_ALLOWED_TEMPLATES", set())
        if templates and template not in templates:
            problems.append(
                f"{CONSTITUTION_REL} 在提交 {rev[:8]} 里的 template={template!r} "
                f"不在那一版内置白名单 BUILTIN_ALLOWED_TEMPLATES 里(清单不得指向白名单之外的模板)")
    return problems


def check_builtin_protections(root: Path) -> list[str]:
    """代码里的内置保护项相对历史提交只增不减。"""
    problems: list[str] = []
    for rel, names in BUILTIN_REGISTRY:
        current_source = (root / rel).read_text(encoding="utf-8")
        current = const_string_set(current_source, names)
        missing = [name for name in names if name not in current]
        if missing:
            # 提取失败就是这条保护没被守着:查不了等于没查,不能让它冒充通过。
            problems.append(f"{rel} 里的内置常量 {missing} 无法解析成字符串集合,历史比对对它们无效")
            continue

        commits = _commits_touching(root, rel)
        if len(commits) < 2:
            continue          # 该文件确实只有一个版本:没有"上一次"可比
        for rev in commits:
            older_source = _read_at(root, rev, rel)
            if older_source is None:
                continue      # 该版本里文件还不存在
            for name, values in removed_protections(current_source, older_source, names).items():
                problems.append(
                    f"{rel} 的 {name} 里的 {sorted(values)} 在后续提交中被删除了(内置保护只能增不能减)")
    return problems


def check_frozen_patterns(root: Path) -> list[str]:
    """受保护的编译正则:模式字符串与历史版本不同就报错(冻结)。

    第 40 轮(Medium 3):`DATA_PRIMITIVE_PATTERN` 此前**从未被比对过**——提取器不认 `re.compile`,
    于是把模式改宽(少拒几个名字)守卫照样全绿。正则没有"只增不减"的自然序,
    所以按冻结处理:要改必须显式改守卫。"""
    problems: list[str] = []
    rel = "scripts/research_sandbox.py"
    current_source = (root / rel).read_text(encoding="utf-8")
    current = const_frozen_pattern(current_source, SANDBOX_FROZEN_PATTERNS)
    missing = [name for name in SANDBOX_FROZEN_PATTERNS if name not in current]
    if missing:
        problems.append(f"{rel} 里的受保护正则 {missing} 无法解析成字面量模式,历史比对对它们无效")
        return problems
    for rev in _commits_touching(root, rel):
        older_source = _read_at(root, rev, rel)
        if older_source is None:
            continue
        older = const_frozen_pattern(older_source, SANDBOX_FROZEN_PATTERNS)
        for name, pattern in older.items():
            if current[name] != pattern:
                problems.append(
                    f"{rel} 的 {name} 在提交 {rev[:8]} 之后被改动"
                    f"(受保护的正则是冻结项:要改必须显式改守卫)")
    return problems


def check_protected_maps(root: Path) -> list[str]:
    """受保护的映射({事件: 参数下标}):键只能增,每个键的下标集合只能增。

    第 40 轮(Medium 3):`AUDIT_PATH_MUTATION_EVENTS` 此前**从未被比对过**——
    删掉 `os.remove` 或删掉 `os.rename` 的第二个下标,守卫照样全绿。"""
    problems: list[str] = []
    rel = "scripts/research_sandbox.py"
    current_source = (root / rel).read_text(encoding="utf-8")
    current = const_int_map(current_source, SANDBOX_PROTECTED_MAPS)
    missing = [name for name in SANDBOX_PROTECTED_MAPS if name not in current]
    if missing:
        problems.append(f"{rel} 里的受保护映射 {missing} 无法解析成 {{键: 下标}} 形式,历史比对对它们无效")
        return problems
    for rev in _commits_touching(root, rel):
        older_source = _read_at(root, rev, rel)
        if older_source is None:
            continue
        older = const_int_map(older_source, SANDBOX_PROTECTED_MAPS)
        for name, values in older.items():
            now = current[name]
            for key, indexes in values.items():
                if key not in now:
                    problems.append(
                        f"{rel} 的 {name} 里的事件 {key!r} 在提交 {rev[:8]} 之后被删除了"
                        f"(受保护的映射只能增:少一个事件 = 少查一种改文件的动作)")
                    continue
                gone = indexes - now[key]
                if gone:
                    problems.append(
                        f"{rel} 的 {name} 里事件 {key!r} 的参数下标 {sorted(gone)} 在提交 {rev[:8]} 之后被删除了"
                        f"(受保护的映射只能增:少一个下标 = 少查一条路径)")
    return problems


def check_registry_completeness(root: Path) -> list[str]:
    """注册表必须覆盖源码里**全部**字符串集合型的内置保护常量。

    漏登记一个常量 = 那个常量的历史守卫是空的,而它自己不会报警:
    当年 `BUILTIN_PROTECTED_NAMES` 因为提取器不认 BinOp 从未被比对过,
    正是不该漏的那一组(第 5 轮 codex 实测:删掉 on_disconnect 守卫照样全绿)。"""
    problems: list[str] = []
    for rel, names in BUILTIN_REGISTRY:
        source = (root / rel).read_text(encoding="utf-8")
        registered = set(names)
        for node in ast.parse(source).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                # 带注解的写法(`FORBIDDEN_X: frozenset[str] = {...}`)同样是常量定义
                name = node.target.id
            else:
                continue
            if not name.startswith(GUARDED_PREFIXES) or name in registered:
                continue
            if const_string_set(source, (name,)).get(name) is not None:
                problems.append(f"{rel} 的 {name} 是字符串集合型的内置保护常量,但没登记进历史守卫的注册表")

    # 第 40 轮(Medium 3):字符串集合之外的两种保护形状也必须登记——
    # **公开的编译正则**(拒谁靠它)与**公开的 {键: 下标} 映射**(查哪条路径靠它)。
    # 这两种形状此前完全没有守护:改宽正则、删掉映射里的一个事件,守卫都全绿。
    # 只管**公开名**(下划线开头的是解析/脱敏用的启发式,不属于"拒谁"的保护清单)。
    rel = "scripts/research_sandbox.py"
    source = (root / rel).read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
        else:
            continue
        if name.startswith("_"):
            continue
        if const_frozen_pattern(source, (name,)).get(name) is not None \
                and name not in SANDBOX_FROZEN_PATTERNS:
            problems.append(f"{rel} 的 {name} 是编译出来的保护正则,但没登记进历史守卫的冻结正则表")
        if const_int_map(source, (name,)).get(name) is not None \
                and name not in SANDBOX_PROTECTED_MAPS:
            problems.append(f"{rel} 的 {name} 是 {{键: 下标}} 形态的保护映射,但没登记进历史守卫的映射表")
    return problems


def check_single_constants(root: Path) -> list[str]:
    """单值内置常量必须落在对应的白名单里(它没有"只增不减"可言,只能靠白名单约束)。"""
    problems: list[str] = []
    for rel, name, whitelist in SINGLE_CONSTANTS:
        source = (root / rel).read_text(encoding="utf-8")
        value = _single_string(source, name)
        if value is None:
            problems.append(f"{rel} 的 {name} 无法解析出字符串值")
            continue
        allowed = const_string_set(source, (whitelist,)).get(whitelist, set())
        if value not in allowed:
            problems.append(f"{rel} 的 {name}={value!r} 不在 {whitelist} 里")
    return problems


def _single_string(source: str, name: str) -> str | None:
    """取一个顶层单值字符串常量(如 `BUILTIN_TEMPLATE = "模块:类"`)。"""
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    return None


def _live_calls(tree: ast.AST, name: str) -> list[ast.Call]:
    """源码里对 `name` 的**活调用**(死分支里的不算)。

    第 11 轮 codex 的 High 2:守卫原来只数"名字出现次数",于是
    `def check_protected_state(b, a): return []` + 改为裸名调用,
    或 `if False and name in BUILTIN_FORBIDDEN_DUNDERS:` 都能让守卫全绿
    而第四层已经失效。这里要的是**调用**,不是引用。"""

    class Finder(ast.NodeVisitor):
        def __init__(self):
            self.calls: list[ast.Call] = []
            self.scopes: list[ast.AST] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.scopes.append(node)
            self.generic_visit(node)
            self.scopes.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_If(self, node: ast.If) -> None:
            if _statically_false(node.test):
                for stmt in node.orelse:
                    self.visit(stmt)
                return
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            if _statically_false(node.test):
                return
            self.generic_visit(node)

        def visit_IfExp(self, node: ast.IfExp) -> None:
            # `(check(...) if False else [])`(第 14 轮 codex 的 G3):条件表达式把结论吞掉
            if _statically_false(node.test):
                self.visit(node.orelse)
                return
            self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if ((isinstance(func, ast.Name) and func.id == name)
                    or (isinstance(func, ast.Attribute) and func.attr == name)):
                self.calls.append(node)
            self.generic_visit(node)

    finder = Finder()
    finder.visit(tree)
    return finder.calls


def _call_scope(tree: ast.AST, node: ast.Call) -> ast.AST | None:
    """调用点**所在的最内层函数体**(不在任何函数里则返回 None)。

    第 12 轮 codex 的 High 2(A3):把第四层的比对挪进一个**永不被调用**的函数,
    守卫 exit 0 而第四层就此消失——所以光看"有没有活调用"不够,
    还要看这个调用**在哪**、那个函数**有没有人叫**。"""
    found: list[ast.AST] = []

    class ScopeFinder(ast.NodeVisitor):
        def __init__(self):
            self.stack: list[ast.AST] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.stack.append(node)
            self.generic_visit(node)
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef
        # lambda 也是作用域(第 13 轮 codex 的 High 2):把比对挪进 `lambda: ...` 它就永远不会执行,
        # 而原先的 _call_scope 认不出 lambda,把它当成外层函数的一部分。
        visit_Lambda = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if node is target:
                found.append(self.stack[-1] if self.stack else None)
                return
            self.generic_visit(node)

    target = node
    ScopeFinder().visit(tree)
    return found[0] if found else None


def _is_snapshot_call(node: ast.AST) -> bool:
    """这个表达式是不是**一次** `snapshot_protected_state()` 调用。

    第 12 轮 codex 的 High 2(A2'):`check_protected_state(before, dict(snapshot_protected_state()))`
    的两个实参语法不同、语义相同,比对必然通过——所以第二个实参必须是**调用本身**,
    不能是它的拷贝、也不能是提前存好的变量。"""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "snapshot_protected_state"
    if isinstance(func, ast.Attribute):
        return func.attr == "snapshot_protected_state"
    return False


def _called_names_in_repo(root: Path) -> set[str]:
    """仓库里**被调用过**的函数名(按名字调用)。

    用来判"第四层所在的那个函数有没有人叫"——
    第 12 轮 codex 的 High 2(A3)实测:调用挪进一个从不被调用的函数,
    守卫的引用次数与名字都没变,于是 exit 0,而第四层已经死了。
    测试目录不算:只在测试里被调用的函数,在生产路径上同样不会执行。"""
    called: set[str] = set()
    for path in _repo_python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    return called


def _defined_names(tree: ast.AST) -> set[str]:
    """模块里自己定义的名字(函数/类/赋值)——用来抓"同名影子"。"""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


# 只"累积"、不"决策"的方法名:`findings.extend(...)` 本身不算用掉了结论。
_ACCUMULATE_ATTRS = ("extend", "append", "add", "update", "insert")


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


_LOGGING_CALLS = frozenset({"print", "debug", "info", "warning", "error", "log", "write_log", "pprint"})


def _is_logging_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in _LOGGING_CALLS
    if isinstance(func, ast.Attribute):
        return func.attr in _LOGGING_CALLS
    return False


def _flows_to_decision(node: ast.AST, parents: dict) -> bool:
    """这个表达式的值有没有流向**决策**:条件、返回值、`raise`、`assert`。

    第 14 轮 codex 的 G1:结论存进一个**只被 `print` 的名字**——名字确实被读了,
    但从不导致拒绝。所以"被读取过"不够,读取必须发生在决策位置:
    `if findings:` / `return findings` / `raise ...(findings)` / `assert not findings`。
    途中经过日志/打印调用则不算(那是旁路)。"""
    current = node
    while current in parents:
        parent = parents[current]
        if _is_logging_call(parent):
            return False
        if isinstance(parent, (ast.Return, ast.Raise, ast.Assert)):
            return True
        if isinstance(parent, (ast.If, ast.While, ast.IfExp)) and parent.test is current:
            return True
        if isinstance(parent, ast.IfExp):
            # 值落在条件表达式的某个分支里:要看**这个条件表达式**自己的值有没有流向决策
            current = parent
            continue
        if isinstance(parent, ast.stmt):
            return False                                  # 走到语句边界还没碰到决策
        current = parent
    return False


def _name_is_consumed(scope: ast.AST, name: str, parents: dict) -> bool:
    """`name` 在 `scope` 里是否在**决策位置**被读取过(只给它 extend/append 不算读取)。"""
    for node in ast.walk(scope):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            parent = parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.value is node \
                    and parent.attr in _ACCUMULATE_ATTRS:
                continue
            if _flows_to_decision(node, parents):
                return True
    return False


def _result_is_used(tree: ast.AST, call: ast.Call, scope: ast.AST | None) -> bool:
    """`check_protected_state(...)` 的**结论**有没有流向决策。

    第 13 轮 codex 的 High 1:`_ = const.check_protected_state(...)` 调用形态完全对,
    守卫 exit 0,而比对的结论被扔掉——第四层静默死亡。
    第 14 轮 codex 的 G1/G3:存进"只被 print 的名字"、被条件表达式吞掉,同样让结论不导致拒绝。
    所以结论必须**流向决策**(见 `_flows_to_decision`)。未知的父节点形态一律按"未使用"处理。
    (这仍是形态层面的近似;"结论必须导致拒绝"的**效果**由端到端回归用例守——
    把任一层关掉,对应的端到端用例必须变红。)"""
    parents = _parent_map(tree)
    target_scope = scope if scope is not None else tree
    node: ast.AST = call
    parent = parents.get(node)
    # 条件表达式的分支:取这个条件表达式自己的值继续判
    while isinstance(parent, ast.IfExp) and parent.test is not node:
        node = parent
        parent = parents.get(node)
    if isinstance(parent, ast.Expr):
        return False                                     # 整条语句只为调用,结论被丢弃
    if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        targets = parent.targets if isinstance(parent, ast.Assign) else [parent.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        if not names:
            return False
        if all(not name.strip("_") for name in names):
            return False                                 # `_ = ...`:明确丢弃
        return any(_name_is_consumed(target_scope, name, parents) for name in names)
    if isinstance(parent, ast.Call):
        if _is_logging_call(parent):
            return False
        func = parent.func
        if isinstance(func, ast.Attribute) and func.attr in _ACCUMULATE_ATTRS \
                and isinstance(func.value, ast.Name):
            return _name_is_consumed(target_scope, func.value.id, parents)
        return _flows_to_decision(parent, parents)
    return _flows_to_decision(node, parents)


def _check_call_problems(rel: str, tree: ast.AST, call: ast.Call, snapshot_calls: list,
                         called_names: set | None) -> list[str]:
    """对**一个** `check_protected_state(...)` 调用点做形态核对(注册表与执行入口共用)。

    `called_names` 为 None 表示不要求"所在函数在仓库里被调用过"
    (执行入口自己就是入口,没有调用方)。"""
    problems: list[str] = []
    if len(call.args) < 2:
        problems.append(
            f"{rel} 里 check_protected_state 只给了 {len(call.args)} 个实参:"
            "指纹比对需要前后两张快照")
        return problems
    if ast.dump(call.args[0]) == ast.dump(call.args[1]):
        problems.append(
            f"{rel} 里 check_protected_state 的两个实参是同一个对象"
            "(自己比自己,比对必然通过)")
    # 第二个实参必须是一次 snapshot_protected_state() 调用本身:
    # `dict(snapshot_protected_state())` 语法不同、语义相同,比对必然通过
    # (第 12 轮 codex 的 High 2 / A2')。
    if not _is_snapshot_call(call.args[1]):
        problems.append(
            f"{rel} 里 check_protected_state 的第二个实参"
            "不是一次 snapshot_protected_state() 调用"
            "(指纹必须与**新拍**的快照比对,拿拷贝或旧变量比对必然通过)")
    scope = _call_scope(tree, call)
    if isinstance(scope, ast.Lambda):
        problems.append(
            f"{rel} 里第四层的比对在 lambda 里(第 {call.lineno} 行):"
            "lambda 不会被调用,比对永远不会执行")
        return problems
    # 结论必须被用掉(第 13 轮 codex 的 High 1)
    if not _result_is_used(tree, call, scope):
        problems.append(
            f"{rel} 第 {call.lineno} 行 check_protected_state 的结论被丢弃了"
            "(`_ = ...` 或单独一条语句):比对做了也等于没做,第四层静默失效")
    # 调用点必须在**取快照的同一个函数体**里,且那个函数得有人叫
    # (第 12 轮 codex 的 High 2 / A3:挪进一个从不被调用的函数)。
    if scope is not None:
        # 嵌套函数(第 14 轮 codex 的 G2):`_called_names_in_repo` 是全仓**按名字**匹配,
        # 一个嵌套的死函数只要取了个仓库里别处被调用过的名字(`run`/`main`/`load`)就能冒充"被调用过"。
        # 嵌套的函数/方法必须在**本文件内**被调用。
        parent = _parent_map(tree).get(scope)
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            local_calls = {
                (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))}
            if scope.name not in local_calls:
                problems.append(
                    f"{rel} 里第四层的比对在嵌套的 {scope.name} 里,而它在本文件里从没被调用过")
        same_scope = [c for c in snapshot_calls if _call_scope(tree, c) is scope]
        if not same_scope:
            problems.append(
                f"{rel} 里第四层的比对在 {scope.name} 里,"
                "而取快照不在同一个函数体里(快照与比对必须成对出现)")
        if called_names is not None and scope.name not in called_names:
            problems.append(
                f"{rel} 里第四层的比对在 {scope.name} 里,"
                "而这个函数在整个仓库里从没被调用过(第四层实际上不会执行)")
    return problems


def check_run_call_order(root: Path) -> list[str]:
    """每个跑回测的函数里,基准快照必须在 `run_backtesting()` 之前(按行号)。"""
    problems: list[str] = []
    for rel, run_name, snapshot_names in RUN_CALL_ORDER:
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            problems.append(f"{rel} 读取/解析失败,无法核对回测的时序: {exc}")
            continue
        run_calls = _live_calls(tree, run_name)
        if not run_calls:
            problems.append(f"{rel} 里没有对 {run_name} 的活调用:回测入口已经不在了,时序检查失去对象")
            continue
        for call in run_calls:
            scope = _call_scope(tree, call)
            if scope is None or isinstance(scope, ast.Lambda):
                problems.append(f"{rel} 第 {call.lineno} 行在函数体之外(或 lambda 里)跑回测")
                continue
            snapshots = [c for name in snapshot_names for c in _live_calls(tree, name)
                         if _call_scope(tree, c) is scope]
            if not any(c.lineno < call.lineno for c in snapshots):
                problems.append(
                    f"{rel} 第 {call.lineno} 行 {run_name}() 之前,{scope.name} 里没有取基准快照"
                    "(基准拍在回测之后,第四层的指纹比对必然通过)")
    return problems


def check_call_site_shape(root: Path) -> list[str]:
    """第四层的执行点必须是**真的调用**,而且调用形态对得上。

    第 11 轮 codex 的 High 2:注册表只按"名字出现次数"守,于是三种改法都能让守卫 exit 0
    而第四层已失效:
    - 同名影子函数(`def check_protected_state(b, a): return []`)后改为裸名调用;
    - `check_protected_state(before, before)`——自己比自己,比对必然通过;
    - 把调用挪进死分支(`if False: ...`)。

    这里逐条按**调用形态**核对:必须有活调用、没有同名影子、比对的两个实参必须不同。"""
    problems: list[str] = []
    for rel, names in CALL_SITE_REGISTRY:
        source = (root / rel).read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            problems.append(f"{rel} 解析失败,无法核对第四层的执行点: {exc}")
            continue
        defined = _defined_names(tree)
        for name in names:
            if name in defined:
                problems.append(
                    f"{rel} 里定义了与第四层执行点同名的 {name}"
                    "(调用会落到影子函数上,第四层形同虚设)")
            calls = _live_calls(tree, name)
            if not calls:
                problems.append(
                    f"{rel} 里没有对 {name} 的**活调用**(只有引用、或者调用在死分支里):"
                    "第四层的执行点已经不在了")
                continue
            if name == "check_protected_state":
                snapshot_calls = _live_calls(tree, "snapshot_protected_state")
                called_names = _called_names_in_repo(root)
                for call in calls:
                    problems.extend(_check_call_problems(rel, tree, call, snapshot_calls, called_names))
    return problems


# 会执行候选代码的特征:**裸名调用** `exec(...)` / `eval(...)` / `compile(...)`,
# 沙盒的执行入口(`safe_builtins`)、候选导入入口。
#
# 裸名与属性名要分开:`re.compile(...)` 与 `qapp.exec()` 都不是执行候选代码
# (前者是编译正则,后者是 Qt 事件循环),按属性名一刀切会误判成执行入口。
_EXECUTOR_CALLS = ("exec", "eval", "compile", "safe_builtins", "import_candidate")

# 属性形式的执行入口:动态求值、动态导入与运行脚本。
# (`importlib.import_module` 能把任意模块拉进来执行,`runpy.run_path` 能跑任意脚本。)
#
# 例外:`re.compile(...)` 是编译正则,不是执行代码——按属性名一刀切会把它误判成执行入口
# (宪法模块自己就用它校验模板名)。
_EXECUTOR_ATTR_CALLS = ("eval", "compile", "import_module", "run_path", "run_module")
_EXECUTOR_ATTR_SAFE_RECEIVERS = ("re",)

# 守卫自身不参与"执行候选"的判定。
_EXECUTOR_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv", "venv",
                                 ".pytest_tmp"})

# 宪法模块自己**不执行候选**:它只用 importlib 载入模板与框架模块(为身份指纹),
# 那是导入不是执行。它是"谁执行候选"这件事的裁判,自己却能执行代码就名存实亡——
# 所以它被排除在"执行入口"判定之外的同时,另有一条专门检查盯着它
# (见 check_constitution_module_is_clean)。
_EXECUTOR_SKIP_FILES = ("risk/constitution.py",)


def _git_python_files(root: Path) -> list[Path] | None:
    """**git 视角**的 Python 文件清单;不是 git 仓库(或 git 不可用)时返回 None。

    `git ls-files --cached` = 已入库的文件;
    `git ls-files --others --exclude-standard` = 尚未入库但**没有被 gitignore**的文件。
    合起来既不会漏掉「刚写好还没提交」的新执行入口,
    又不会把评审脚手架、临时目录里的半成品 `.py` 当成仓库代码
    (第 11 轮 codex 的 F10:原先按 `rglob` 扫整个文件系统,开发者随手落在仓库里的
    临时 `.py` 会把守卫弄红,而报错完全指不到「你的临时文件」)。

    **根目录下以 `.` 开头的未入库目录不算仓库代码**(第 14 轮 codex 的 L2):
    评审脚手架目录(`.codex_tmp_r14/`、`.reviewtmp2/` 之类)**不保证被 .gitignore 覆盖**,
    一旦某个评审代理在里面写了 `exec(`/`eval(`,守卫就会红,报错指向一个临时路径——
    正是第 11 轮 F10 想根治的现象,只修了一半。
    判据是「**未入库**且**顶层目录名以 `.` 开头**且该顶层目录里没有任何入库文件」:
    这样 `.github/scripts/dedup.py`(已入库)照常被扫,而 `.codex_tmp_r14/x.py` 不被扫。
    """
    listed = git(root, "ls-files", "--cached", "--", "*.py")
    others = git(root, "ls-files", "--others", "--exclude-standard", "--", "*.py")
    if listed.returncode != 0 or others.returncode != 0:
        return None
    tracked: list[str] = []
    for line in listed.stdout.splitlines():
        rel = line.strip()
        if rel:
            tracked.append(rel)
    tracked_top = {rel.split("/", 1)[0] for rel in tracked}
    files: list[Path] = []
    seen: set[str] = set()
    for rel in tracked:
        path = root / rel
        if path.is_file():
            files.append(path)
            seen.add(rel)
    for line in others.stdout.splitlines():
        rel = line.strip()
        if not rel or rel in seen:
            continue
        top = rel.split("/", 1)[0]
        if top.startswith(".") and top not in tracked_top:
            continue                      # 未入库的隐藏目录:评审脚手架,不是仓库代码
        path = root / rel
        if path.is_file():
            files.append(path)
    return sorted(files)


def _repo_python_files(root: Path) -> list[Path]:
    """仓库里的 Python 文件(排除测试与临时目录)。

    优先用 git 的跟踪集(见 `_git_python_files`);不在 git 仓库里(测试用的临时目录)
    时退回按目录遍历,否则测试就没法造场景了。

    第 13 轮 codex 的 Medium:**git 列表为空时也要退回目录遍历**。
    原先的判据是「git 用不了才退回」(`tracked is not None`),可是当被扫目录**位于
    git 工作树内、而其中的 .py 既没被跟踪又被 gitignore 忽略**时,
    `git ls-files` **返回码是 0、输出却是空的** → 守卫一个文件都不扫 →
    报「没有问题」——**fail-open**,与本文件「查不了等于没查、必须 fail-closed」
    的原则正好相反。实测:守卫自己的 6 条回归用例在「basetemp 落在仓库内」时
    全部失败(失败的正是「新执行入口不得裸奔」那几条)。
    现在空列表也退回 `rglob`:仓库根目录的 git 列表不可能为空(有的是被跟踪的 .py),
    所以这一改动只影响「扫一个 git 看不见的子目录」这一种场景。
    """
    tracked = _git_python_files(root)
    source = tracked if tracked else sorted(root.rglob("*.py"))
    files: list[Path] = []
    for path in source:
        rel = path.relative_to(root)
        if any(part in _EXECUTOR_SKIP_DIRS or part.startswith("_probe") for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == "tests":
            continue
        files.append(path)
    return files


def _imported_modules(tree: ast.AST) -> set[str]:
    """这个模块 import 到的**仓库内**模块名。

    `import scripts.research_api` → "scripts.research_api";
    `from scripts import research_api` → "scripts.research_api";
    `from scripts.research_api import import_candidate` → "scripts.research_api"。"""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            for alias in node.names:
                modules.add(f"{node.module}.{alias.name}")
    return modules


def _is_candidate_executor(tree: ast.AST) -> bool:
    """这个模块会不会**执行候选代码**。

    `exec` 只认**裸名调用**(`exec(code, ...)`):`qapp.exec()` 是 Qt 的事件循环,
    不是执行代码——按属性名一刀切会把 GUI 脚本误判成执行入口(实测误报过一次)。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _EXECUTOR_CALLS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _EXECUTOR_ATTR_CALLS:
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in _EXECUTOR_ATTR_SAFE_RECEIVERS:
                continue
            return True
    return False


def _primitive_executor_calls(tree: ast.AST) -> list[ast.Call]:
    """真正**执行代码**的原语调用:裸 `exec/eval/compile` 与属性形式的动态求值/导入/运行脚本。

    `safe_builtins` 与 `import_candidate` 不在此列——它们是"调用已受检的入口",
    由入口自己(`research_api.import_candidate`)负责第四层。"""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # `compile` 只生成字节码、不执行,不在此列(`exec(compile(...))` 的 exec 照样被抓)
        if isinstance(func, ast.Name) and func.id in ("exec", "eval"):
            calls.append(node)
        elif isinstance(func, ast.Attribute) and func.attr in _EXECUTOR_ATTR_CALLS and func.attr != "compile":
            receiver = func.value
            if isinstance(receiver, ast.Name) and receiver.id in _EXECUTOR_ATTR_SAFE_RECEIVERS:
                continue
            calls.append(node)
    return calls


def check_executor_completeness(root: Path) -> list[str]:
    """**完备性检查**:谁执行候选,谁就必须走第四层(快照 + 指纹比对)。

    第 11 轮 codex 的 High 2:注册表是手工维护的,新增一个执行入口
    (`scripts/brand_new_runner.py` 跑候选却完全不调第三/四层)守卫照样 exit 0——
    "执行点有人守"这句话当时是**言过其实**的。

    这里按仓库实际内容自动判定:
    1. 找出所有执行候选的模块(裸 `exec(...)` / 沙盒执行入口 / 候选导入入口);
    2. 算出哪些模块**走**第四层:自己调用了 `snapshot_protected_state` 与
       `check_protected_state`,或者 import 了走第四层的模块(传递闭包);
    3. 执行候选却不在闭包里的模块 → 报出来。"""
    problems: list[str] = []
    sources: dict[str, str] = {}
    trees: dict[str, ast.AST] = {}
    for path in _repo_python_files(root):
        rel = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            trees[rel] = ast.parse(source)
        except (UnicodeDecodeError, SyntaxError):
            continue
        sources[rel] = source

    # 走第四层的模块:自己**调用**了那两个名字(按 AST 判定,不是全文子串——
    # 第 12 轮 codex 的 High 2 / A5:在注释里写上这两个名字就能冒充"已覆盖")。
    covered = set()
    for rel, tree in trees.items():
        snapshot_calls = _live_calls(tree, "snapshot_protected_state")
        check_calls = _live_calls(tree, "check_protected_state")
        if snapshot_calls and check_calls:
            covered.add(rel)

    # 第 13 轮 codex 的 High 3:光是"文件里出现过那两个调用"不够——
    # 新执行入口可以只在一个**从不被调用的函数**里放上它们(名义覆盖),而 `exec` 在另一处裸奔。
    # 所以对每个**原语执行调用**,要求它所在的**同一个函数体**里自己成对取快照并比对,
    # 且每个比对调用点都过形态核对(结论被用掉、第二个实参是新拍快照、不在 lambda 里)。
    # 不要求"所在函数被仓库调用过":执行入口自己就是入口,没有调用方。
    for rel, tree in trees.items():
        if rel in _EXECUTOR_SKIP_FILES or rel not in covered:
            continue
        primitives = _primitive_executor_calls(tree)
        if not primitives:
            continue
        snapshot_calls = _live_calls(tree, "snapshot_protected_state")
        check_calls = _live_calls(tree, "check_protected_state")
        for call in check_calls:
            for problem in _check_call_problems(rel, tree, call, snapshot_calls, None):
                problems.append(problem)
        for call in primitives:
            scope = _call_scope(tree, call)
            if scope is None or isinstance(scope, ast.Lambda):
                problems.append(
                    f"{rel} 第 {call.lineno} 行在函数体之外(或 lambda 里)执行代码:"
                    "执行点必须在一个自己走第四层的函数里")
                continue
            in_scope_snapshot = [c for c in snapshot_calls if _call_scope(tree, c) is scope]
            in_scope_check = [c for c in check_calls if _call_scope(tree, c) is scope]
            if not in_scope_snapshot or not in_scope_check:
                problems.append(
                    f"{rel} 第 {call.lineno} 行执行代码的函数 {scope.name} 里"
                    "没有自己成对取快照并比对(第四层挂在别的函数上,执行点本身裸奔)")
                continue
            # 时序(第 14 轮 codex 的 G6b):基准必须在执行**之前**拍,比对在执行**之后**。
            # 把 `before = snapshot_protected_state()` 挪到 exec 之后,第四层就成了"自己跟自己比"。
            if not any(c.lineno < call.lineno for c in in_scope_snapshot):
                problems.append(
                    f"{rel} 第 {call.lineno} 行执行代码之前,{scope.name} 里没有取基准快照"
                    "(快照拍在执行之后,指纹比对必然通过)")
            if not any(c.lineno > call.lineno for c in in_scope_check):
                problems.append(
                    f"{rel} 第 {call.lineno} 行执行代码之后,{scope.name} 里没有指纹比对")

    # **不**做跨模块的传递闭包:执行入口必须自己走第四层。
    # `import scripts.backtest_lib` 只是把模块拉进来,不代表这个入口自己比对了指纹
    # (第 12 轮 codex 的 High 2 / A8:靠 import 传递闭包就能让守卫全绿)。

    for rel, tree in trees.items():
        if rel in _EXECUTOR_SKIP_FILES:
            continue
        if not _is_candidate_executor(tree):
            continue
        if rel not in covered:
            problems.append(
                f"{rel} 会执行候选代码,却没有走第四层(受保护对象的快照与指纹比对):"
                "新执行入口不得裸奔")
    return problems


def check_constitution_module_is_clean(root: Path) -> list[str]:
    """宪法模块自己不得有**执行入口**(裸 `exec` / `eval` / `compile` 调用)。

    它是"谁执行候选、谁必须走第四层"这件事的裁判,自己却能执行代码,
    那这道防线就名存实亡。它需要 importlib 来载入模板与框架模块(那是导入,不是执行),
    所以只禁这三个执行入口。"""
    problems: list[str] = []
    rel = CONSTITUTION_SOURCE_REL
    tree = ast.parse((root / rel).read_text(encoding="utf-8"))
    for name in ("exec", "eval", "compile"):
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == name):
                problems.append(
                    f"{rel} 第 {node.lineno} 行调用了 {name}:"
                    "宪法模块是裁判,自己不得有执行入口")
    return problems


# --------------------------------------------------------------------------
# 第 55 轮:接线自检的**独立落点**(这一层是纯标准库 + git,永远会跑)
# --------------------------------------------------------------------------
# **为什么放在宪法层**:宪法 workflow 是「纯标准库 + git」——不装 PyYAML、
# 不装 pytest、不装整个交易环境 ⇒ 它**没有理由不跑** ⇒ 它是接线自检的
# **独立落点**(研究层那一处被删掉时,这里还在;反之亦然)。
#
# **为什么是纯文本**:宪法层不许引入依赖(没有 PyYAML)。文本级足够——
# 只需要认出「有没有一处规范形的调用」,不需要解析整个 YAML。
#
# **只认规范形**:第 55 轮**实测**的洞——接线自检的调用可以被加参数
# (`--workflow` 曾是**替换**语义 ⇒ 真实的 workflow 压根没被查,
#  而工具**照样报「[OK] CI 接线合规」**)⇒ 这里要求
# `python tools/check_ci_wiring.py`,**一个选项都不许带**。
RESEARCH_WORKFLOW_REL = ".github/workflows/research-guard.yml"
WIRING_SCRIPT_REL = "tools/check_ci_wiring.py"
_PYTHON_TOKEN_RE = re.compile(r"^(?:python(?:\d+(?:\.\d+)*)?|py|pythonw)$")

# **规范形里允许的解释器选项**(第 55 轮之五,codex 第 2 轮的 M1):
# `python -X utf8 …`、`py -3 …` 这些**合法**写法此前一律判红(误伤)。
# **两层必须一致**:研究层(`tools/check_ci_wiring.py`)放宽了,这里也要放宽,
# 否则同一处调用研究层说「合规」、宪法层说「不是规范形」⇒ CI 变红。
# **禁止**的选项:`-c`(把脚本整个换掉)、`-m`(把脚本换成模块)。
_INTERPRETER_VALUE_FLAGS = {"-W", "-X"}
_INTERPRETER_NO_VALUE_FLAGS = {
    "-3", "-u", "-B", "-E", "-s", "-I", "-O", "-OO", "-P", "-v",
    "--check-hash-based-pycs",
}
_FORBIDDEN_INTERPRETER_FLAGS = {"-c", "-m"}


def _run_blocks(text: str) -> list[str]:
    """把 workflow 文本切成「每个 run 步骤的命令串」。

    **为什么文本级**:宪法层没有 PyYAML。这里只需要认出
    「有没有一处规范形的 `python tools/check_ci_wiring.py`」,
    不需要解析整个 YAML ⇒ 文本级够用,而且**不引入依赖**。

    块标量(`run: |`)按**缩进**收集后续行,拼成**一个**命令串
    (这样 `ruff check ... tools/check_ci_wiring.py` 这种
    「把脚本列进文件清单」的写法不会被误认成「在跑它」)。
    """
    lines = text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped.startswith("run:"):
            index += 1
            continue
        rest = stripped[len("run:"):].strip()
        indent = len(line) - len(line.lstrip())
        if rest and rest not in ("|", ">", "|-", ">-", "|+", ">+"):
            blocks.append(rest)                 # `run: 一条命令`(行内写法)
            index += 1
            continue
        cursor = index + 1
        body: list[str] = []
        while cursor < len(lines):
            candidate = lines[cursor]
            if not candidate.strip():
                cursor += 1
                continue
            if len(candidate) - len(candidate.lstrip()) <= indent:
                break                           # 缩进回退了 ⇒ 这一步结束
            if not candidate.strip().startswith("#"):
                body.append(candidate.strip())
            cursor += 1
        blocks.append("\n".join(body))
        index = cursor
    return blocks


# **重定向词**:`2>&1` / `>out` / `&>log` / `2>` …… 它们**不是参数**
# (研究层 `logical_commands` 早就把它们剥掉了 ⇒ 两层口径必须一致)。
_REDIRECTION_TOKEN_RE = re.compile(r"^[0-9<>&()]*[<>][0-9<>&()]*$")


def _command_tokens(text: str) -> list[str]:
    """把一段文本切成「命令词」:去掉**行尾注释**与**重定向**。

    **为什么**(第 55 轮之十,codex 第 5 轮的 M-5):
    宪法层此前用 `shlex.split(block, comments=False)` ⇒ ①行尾注释
    (`python tools/check_ci_wiring.py  # 接线自检`)②重定向(`… 2>&1`)
    都**留在词里** ⇒ 「脚本后面不许再有任何词」那条 ⇒ 判成**非规范形**
    ⇒ **误伤合法写法** ⇒ 而研究层会**动态跑**宪法层这条检查
    ⇒ **两层一起红** ⇒ CI 变红(实测)。
    ⇒ 现在:注释交给 shlex 的 `comments=True`,重定向词**整个丢掉**
    ——与研究层 `logical_commands` 的口径**一致**。
    """
    try:
        tokens = shlex.split(text, comments=True, posix=True)
    except ValueError:
        return []                    # 引号不配对 ⇒ 认不出来 ⇒ 交给上层判红
    out: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if _REDIRECTION_TOKEN_RE.match(token):
            continue                # 重定向 ⇒ 不是参数
        out.append(token)
    return out


def _normalize_script_path(path: str) -> str:
    """脚本路径**归一化**:统一分隔符、去掉 `./` 与 `..`。

    **为什么**(第 55 轮之七,claude 第 4 轮的 M-1):
    研究层(`tools/check_ci_wiring.py`)用的是 `os.path.normpath` ⇒
    `python ./tools/check_ci_wiring.py` 与 `python tools/../tools/check_ci_wiring.py`
    在**研究层**都算规范形;而宪法层此前只按**字面**比 ⇒ 同一处调用
    **两层结论相反**(一层说合规、一层说不是规范形)⇒ CI 变红,
    或者更糟:两层都判「压根没有」而真实调用还在。
    ⇒ 两层**必须同一套口径**(仓库规矩:两层都要有,不能只靠一边)。
    """
    return os.path.normpath(path.replace("\\", "/")).replace("\\", "/")


def _script_index(tokens: list[str]) -> int | None:
    """命令位置上的那个脚本词(解释器**和它的选项**之后);认不出 ⇒ None。

    **为什么要跳过解释器选项**(第 55 轮之五,codex 第 2 轮的 M1):
    `python -X utf8 tools/check_ci_wiring.py` 里,脚本那个词**不紧跟**解释器
    ⇒ 此前认不出「在跑它」⇒ 报「压根没有」⇒ **误伤合法写法**。

    **连写的取值选项**(第 55 轮之七,claude 第 4 轮的 M-1):
    CPython **也接受** `-Werror` / `-Xutf8` 这种「选项和值连在一起」的写法
    ⇒ 研究层已经认 ⇒ 宪法层也要认 ⇒ 否则**两层结论相反**。
    """
    if not tokens:
        return None
    if not _PYTHON_TOKEN_RE.match(tokens[0].rsplit("/", 1)[-1].lower()):
        return None
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in _FORBIDDEN_INTERPRETER_FLAGS:
            return None
        if token in _INTERPRETER_VALUE_FLAGS:
            index += 2                      # 跳过选项**和它的值**
            continue
        if any(token.startswith(flag) and len(token) > len(flag)
               for flag in _INTERPRETER_VALUE_FLAGS):
            index += 1                      # 连写形式:`-Werror` / `-Xutf8`
            continue
        if token in _INTERPRETER_NO_VALUE_FLAGS or re.match(r"^-\d+(\.\d+)*$", token):
            index += 1
            continue
        return index
    return None


def _wiring_call_shapes(block: str) -> list[str]:
    """这个步骤里「在跑接线自检」的写法(认不出规范形就返回**非规范形**的写法)。

    - 规范形:`python tools/check_ci_wiring.py`(解释器是 python 系,
      可以带解释器自己的选项,但脚本后面**不许**再有任何词);
    - 其它一切**真的在跑那个脚本**的写法(带选项、直接执行脚本、
      藏进 `bash -c "…"` 里)⇒ 都算「非规范形」。

    **怎么区分「在跑它」和「只是把它列进文件清单」**:
    只有当脚本那个词**在命令位置**(解释器及其选项之后)才算「在跑它」
    ⇒ `ruff check … tools/check_ci_wiring.py` 不算。
    """
    tokens = _command_tokens(block)
    if not tokens:
        return []
    # **为什么要 strip**:shlex 的 POSIX 模式会把行尾的续行反斜杠
    # 当成转义 ⇒ 换行变成**词内的一个字面换行** ⇒
    # `tools/check_ci_wiring.py` 会变成 "\ntools/check_ci_wiring.py"。
    # 不 strip 就会把「只是把脚本列进文件清单」误认成「在跑它」。
    tokens = [token.strip() for token in tokens]
    script_index = _script_index(tokens)
    shapes: list[str] = []
    for index, cleaned in enumerate(tokens):
        if _normalize_script_path(cleaned) == WIRING_SCRIPT_REL:
            if script_index is None or index != script_index:
                continue          # 不在命令位置 ⇒ 只是被列进文件清单 ⇒ 不算「在跑它」
            if index == 0:
                shapes.append(block)            # 直接执行脚本 ⇒ 非规范形
            else:
                rest = tokens[index + 1:]
                if not rest:
                    shapes.append(block)        # 规范形
                else:
                    shapes.append(" ".join([tokens[0], cleaned] + rest))  # 带了选项 ⇒ 非规范形
        elif WIRING_SCRIPT_REL in cleaned and _normalize_script_path(cleaned) != WIRING_SCRIPT_REL:
            # 藏在引号里(例如 `bash -c "python tools/check_ci_wiring.py"`)
            shapes.append(block)
    return shapes


def check_ci_wiring_presence(root: Path) -> list[str]:
    """研究 workflow 里必须**还在**跑接线自检(而且是规范形)。

    **失败模式(都是第 55 轮实测过的)**:
      - 调用被删 ⇒ 接线自检自己被悄悄拆掉;
      - 调用被加上 `--workflow <别处文件>` ⇒ 检查范围被缩小,
        真实的 workflow 压根没被查,而工具**照样报「[OK]」**。

    **失败即 fail-closed**:文件读不到 ⇒ 报问题;不当作「没问题」。
    """
    workflow = root / RESEARCH_WORKFLOW_REL
    if not workflow.exists():
        return ["%s 不存在 ⇒ 宪法层无法核对接线自检是否还在(fail-closed)"
                % RESEARCH_WORKFLOW_REL]
    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError as exc:
        return ["%s 读不了:%s(fail-closed)" % (RESEARCH_WORKFLOW_REL, exc)]

    canonical = False
    non_canonical: list[str] = []
    for block in _run_blocks(text):
        shapes = _wiring_call_shapes(block)
        if not shapes:
            continue
        for shape in shapes:
            if _is_canonical_wiring_call(shape):
                canonical = True
            else:
                non_canonical.append(shape)
    if canonical:
        return []
    if non_canonical:
        return ["%s 里跑接线自检的那处调用**不是规范形**:%s ⇒ "
                "守卫的检查范围可以被缩小(实测:加 `--workflow <别处文件>` ⇒ "
                "真实的 workflow 压根没被查,工具却照样报「[OK]」)"
                % (RESEARCH_WORKFLOW_REL, " | ".join(non_canonical)[:200])]
    return ["%s 里**没有任何**跑 `tools/check_ci_wiring.py` 的地方 ⇒ "
            "接线自检自己可以被悄悄删掉" % RESEARCH_WORKFLOW_REL]


def _is_canonical_wiring_call(shape: str) -> bool:
    """这一段文本是不是规范形的 `python tools/check_ci_wiring.py`。

    **允许解释器自己的选项**(第 55 轮之五):`python -X utf8 …`、`py -3 …` 算规范形;
    **禁止** `-c`(把脚本整个换掉)与 `-m`(把脚本换成模块);
    脚本后面**不许**再有任何词(那才是「缩小检查范围」的口子)。
    """
    tokens = _command_tokens(shape)
    if not tokens:
        return False
    index = _script_index(tokens)
    if index is None:
        return False
    if _normalize_script_path(tokens[index]) != WIRING_SCRIPT_REL:
        return False
    return index == len(tokens) - 1


def check_all(root: Path) -> list[str]:
    """跑完全部检查,返回问题清单(空列表表示通过)。"""
    require_full_history(root)
    problems: list[str] = []
    problems += check_constitution_file(root)
    problems += check_builtin_protections(root)
    problems += check_registry_completeness(root)
    problems += check_frozen_patterns(root)
    problems += check_protected_maps(root)
    problems += check_single_constants(root)
    problems += check_protected_usage(root)
    problems += check_call_site_shape(root)
    problems += check_run_call_order(root)
    problems += check_executor_completeness(root)
    problems += check_constitution_module_is_clean(root)
    problems += check_shrink_only(root)
    problems += check_ci_wiring_presence(root)   # 第 55 轮:接线自检的独立落点
    seen: set[str] = set()
    unique = []
    for problem in problems:            # 同一个问题可能对多个历史版本重复出现,报一次就够
        if problem not in seen:
            seen.add(problem)
            unique.append(problem)
    return unique


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    root = Path(argv[0]).resolve() if argv else ROOT
    # Windows 控制台默认是 cp1252,直接打印中文/符号会抛 UnicodeEncodeError——
    # 那会让**成功路径**也以异常退出(退出码 1),看起来像"检查不通过"。先改成 utf-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # 老解释器或已被替换的流:不影响结论
            pass
    try:
        problems = check_all(root)
    except HistoryUnavailable as exc:
        print(f"✗ 无法检查宪法历史(fail-closed): {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print("✗ " + problem, file=sys.stderr)
        print(f"发现 {len(problems)} 处问题", file=sys.stderr)
        return 1
    print("✓ 宪法清单与代码内置保护项相对历史提交均只增不减")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
