#!/usr/bin/env python
"""真钱链路判定点的**直接归属**分析(纯标准库,无副作用)。

**为什么要有它**:后果域分档器(`tools/check_domain.py`)的白名单是手写的,会失准。
要证伪它,得有一把不靠人眼的尺子。本模块用 AST 算出「某个文件是不是**自己**
在真钱链路的判定点上做决定」。

**判据:直接归属(direct ownership),不是传递可达。**
一个文件**自己**调用了判定点(而不是经由别的模块间接调用),它就**拥有**那个决定:
改它 = 改「什么被允许发生」。
反过来,只经由助手间接到达的,那个决定属于助手:
例如 `scripts/calc_ladder_eta.py` 调用 `backtest_lib.run_engine_bars`,
而 `check_instance_state`(风控底线核对)住在 `backtest_lib` 里——
它是那个助手自己的决定,不是调用者的。把它算到调用者头上会误伤一大批文件,
而**误伤的探针等于没有探针**(天天喊狼来了,人就开始忽略它;
本仓库吃过一次「假安心」的亏——状态页把损坏的心跳当成运行中)。

**本模块看不见的那一类(重要)**:数据流。
`scripts/research_grid.py` 自己不调用任何判定点,但它算出的 `on_plateau`
是晋级门槛第 8 条的**输入**——改它能让一个不合格候选过关。
这类耦合必须靠 `tools/check_domain.py` 里的人工清单兜住。
**两层都要有:图找得到的归图,图找不到的归清单。**

只用标准库,不 import bootstrap(不装交易环境也能跑)。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 真钱链路上的判定点:这些函数是闸门本体。
VERDICT_SINKS = (
    "check_gate",             # 晋级门槛
    "check_live_admission",   # 实盘准入
    "write_approval",         # 实盘准入批准文件的写出(内部调 check_gate)
    "record_approval",        # 批准账本写入
    "screen_source",          # 候选静态筛查
    "check_instance_state",   # 风控底线运行期核对
    "check_class",            # 候选类结构核对
)

_CALL_NAME = "name"
_CALL_ATTR = "attr"
_CALL_UNKNOWN = "unknown"


def _local_module_path(mod: str) -> Path | None:
    """按模块名找本地文件。

    `scripts/` 下的模块是**按目录**导入的(`import research_sandbox` 实际解析到
    `scripts/research_sandbox.py`,靠的是 pytest/CI 里的 `sys.path` 注入),
    所以只按「仓库根 + 模块名」找会漏掉它们——`research_sandbox.py` 定义着
    `record_approval` 与 `screen_source`,漏掉它等于漏掉整条批准链。
    """
    p = mod.replace(".", "/")
    candidates = [
        ROOT / f"{p}.py",
        ROOT / p / "__init__.py",
        ROOT / "scripts" / f"{p}.py",
        ROOT / "scripts" / p / "__init__.py",
    ]
    for cand in candidates:
        if cand.exists():
            return cand
    return None


def _parse(fp: Path) -> ast.Module | None:
    try:
        return ast.parse(fp.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _call_descriptor(node: ast.Call) -> tuple[str, ...] | None:
    func = node.func
    if isinstance(func, ast.Name):
        return (_CALL_NAME, func.id)
    if isinstance(func, ast.Attribute):
        base = func.value
        if isinstance(base, ast.Name):
            return (_CALL_ATTR, base.id, func.attr)
        return (_CALL_UNKNOWN, func.attr)
    return None


class ModuleInfo:
    """一个本地模块的函数体调用与 import 别名表。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.functions: dict[str, list[tuple[str, ...]]] = {}
        self.aliases: dict[str, tuple[Path, str | None]] = {}  # 别名 → (目标模块, 具体函数名或 None)
        self.module_level_calls: list[tuple[str, ...]] = []
        tree = _parse(path)
        if tree is None:
            return

        # 顶层函数
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = self._calls_of(node)
        # **类里的方法也要算**:本仓库不少判定调用藏在方法体内
        # (如 `research_gateway.py` 的 HTTP handler 方法里调 `rs.screen_source`)。
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            for meth in cls.body:
                if isinstance(meth, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.functions[f"{cls.name}.{meth.name}"] = self._calls_of(meth)
        # 模块级语句(不在任何函数里)
        self.module_level_calls = self._calls_of_module_level(tree)

        # import 可能在**函数体内**(本仓库常见:`from risk import constitution as const`
        # 写在函数里,为了避免模块级就拉进交易栈)。所以要扫整棵树,不只是 tree.body。
        # 代价:函数内的 import 别名被当成模块级可见 —— 这是**保守**方向
        # (多算依赖),宁可误伤不可漏。
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _local_module_path(alias.name)
                    if target:
                        # import a.b.c → 绑定名指向模块,具体函数未知(None = 该模块任意函数)
                        self.aliases[alias.asname or alias.name] = (target, None)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name
                    # `from pkg import mod`:被绑定的可能是一个**子模块**(pkg/mod.py),
                    # 而不是 pkg 里的某个函数。两种情况必须分清,否则
                    # `from risk import candidate_eval as ce` 会被当成
                    # 「调用 risk 包里的 candidate_eval 函数」,而它其实是模块。
                    sub = _local_module_path(f"{node.module}.{alias.name}")
                    if sub is not None:
                        self.aliases[bound] = (sub, None)   # 模块别名
                        continue
                    target = _local_module_path(node.module)
                    if target is not None:
                        self.aliases[bound] = (target, alias.name)  # 函数别名

    @staticmethod
    def _calls_of(node: ast.AST) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                desc = _call_descriptor(sub)
                if desc:
                    calls.append(desc)
        return calls

    @staticmethod
    def _calls_of_module_level(tree: ast.Module) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    desc = _call_descriptor(sub)
                    if desc:
                        calls.append(desc)
        return calls


_INFO_CACHE: dict[Path, ModuleInfo] = {}


def _info(path: Path) -> ModuleInfo:
    if path not in _INFO_CACHE:
        _INFO_CACHE[path] = ModuleInfo(path)
    return _INFO_CACHE[path]


def _resolved_name(info: ModuleInfo, desc: tuple[str, ...]) -> str | None:
    """把一次调用解析成「实际被调用的函数名」(能确定时),否则 None。

    `rs.screen_source(...)` → "screen_source"(rs 是模块别名,属性名就是函数名)
    `from risk.promotion_gate import check_gate` → "check_gate"
    `from risk.promotion_gate import check_gate as cg` → "check_gate"
    `const.check_instance_state(...)` → "check_instance_state"
    `self.foo()` → None(接收者不是裸名字,无法确定)

    **别名那一支不能省**:`from X import f as g` 之后调的是 `g(...)`,
    裸名字 `g` 与判定点的名字对不上,只看裸名字会**漏报**——
    实测过:`from risk.promotion_gate import check_gate as cg` + `cg(c)`
    在漏报版里 `owned_sinks` 返回空,等于给了一条绕过所有探针的路。
    """
    if desc[0] == _CALL_NAME:
        name = desc[1]
        # 先看它是不是某个「函数别名」(from X import f as g)
        alias = info.aliases.get(name)
        if alias is not None:
            _target, func = alias
            if func is not None:
                return func
        return name
    if desc[0] == _CALL_ATTR:
        alias_name, attr = desc[1], desc[2]
        alias = info.aliases.get(alias_name)
        if alias is None:
            return None
        target, func = alias
        if func is None:
            # 模块别名 → 属性名就是被调用的函数名
            return attr
        # 函数别名再取属性:那是「函数对象上的属性」,不是模块函数
        return None
    # _CALL_UNKNOWN:接收者不是裸名字。若属性名本身是判定点,保守命中。
    return desc[1] if desc[1] in VERDICT_SINKS else None


def owned_sinks(path: Path) -> set[str]:
    """这个文件**自己**直接决定/调用了哪些判定点。

    两种情况算「自己拥有」:
    1. 本文件**定义**了某个判定点(闸门本体就住在这里);
    2. 本文件的函数体(顶层函数、方法、模块级语句)**直接调用**了某个判定点。
    """
    info = _info(path)
    hits: set[str] = set()

    # 1) 定义了判定点
    for fname in info.functions:
        base = fname.split(".")[-1]
        if base in VERDICT_SINKS:
            hits.add(base)

    # 2) 直接调用了判定点
    for calls in list(info.functions.values()) + [info.module_level_calls]:
        for desc in calls:
            name = _resolved_name(info, desc)
            if name in VERDICT_SINKS:
                hits.add(name)
    return hits


# --------------------------------------------------------------------------
# 数据流:门槛**输入**的提供者
# --------------------------------------------------------------------------
#
# 只认调用关系会漏掉一整类耦合:某个模块**不调用**判定点,但它的**值**被喂进判定点。
# 实测例子(codex 第 1 轮复核的 H1):`scripts/research_api.py` 从
# `walk_forward_optimizer` 取 `wf.DATA_FILE`(用哪份数据)与 `wf.build_windows()`
# (在哪个窗口上评估),再交给 `run_gate(...)` → `check_gate`。
# 改那个模块 = 改 check_gate 的输入,而调用图上看它「谁都不拥有」。
#
# 判据:**一个拥有判定点的文件,从某个 import 进来的模块上读取了属性**(如 `wf.DATA_FILE`),
# 那么那个模块就是判定输入的提供者,必须与判定点同级受保护。


def _attribute_reads(info: ModuleInfo) -> set[tuple[Path, str]]:
    """这个文件从 import 进来的模块上读了哪些属性(只算「读取」)。"""
    reads: set[tuple[Path, str]] = set()
    tree = _parse(info.path)
    if tree is None:
        return reads
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
            continue
        base = node.value
        if not isinstance(base, ast.Name):
            continue
        alias = info.aliases.get(base.id)
        if alias is None:
            continue
        target, func = alias
        if func is not None:
            continue  # 函数别名上取属性,不是模块属性
        reads.add((target, node.attr))
    return reads


def gate_input_providers(path: Path) -> set[str]:
    """判定输入的提供者(仓库相对路径):从它们手里取的值会流进某个判定点。"""
    info = _info(path)
    if not owned_sinks(path):
        return set()
    out: set[str] = set()
    for target, _attr in _attribute_reads(info):
        try:
            out.add(target.relative_to(ROOT).as_posix())
        except ValueError:
            continue
    return out


def all_gate_input_providers(paths: list[Path]) -> set[str]:
    """在给定的文件集合里找出所有判定输入的提供者(迭代到不动点)。

    **只看生产文件**:测试用例读某个模块的属性(如 `tests/test_plateau.py` 读
    `optimize.smoothed`)是为了断言,不是把它的值喂进判定点。测试文件本来就已经
    整片在不可降级清单里(改断言 = 削弱检出能力),不需要这条规则重复覆盖。
    """
    providers: set[str] = set()
    frontier = []
    for fp in paths:
        try:
            rel = fp.relative_to(ROOT).as_posix()
        except ValueError:
            continue  # 不在仓库内(如 .github 下的脚本)
        if not rel.startswith("tests/"):
            frontier.append(fp)
    for _ in range(5):  # 迭代上限,防环
        grew = False
        for fp in frontier:
            for rel in gate_input_providers(fp):
                if rel not in providers:
                    providers.add(rel)
                    grew = True
        if not grew:
            break
        frontier = [ROOT / rel for rel in providers if (ROOT / rel).exists()]
    return providers
