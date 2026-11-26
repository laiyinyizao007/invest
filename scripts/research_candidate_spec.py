"""候选源码的**静态解析**:框架自己读出网格与参数声明(ADR `20261115-candidate-process-isolation.md` 第 5.3、6.1 节)。

为什么要有这一层:候选进程**不可信**,它报上来的任何数字都不采信。
网格必须由框架从**自己读过、算过哈希的那一份源码**里解析出来,框架自己展开格子清单、自己记账。
读不出(动态构造)⇒ **fail-closed 拒绝评估**,并给出"请把 param_grid 写成字面量"的明确原因
(拍板点 2 的裁决)。

解析出来的东西只有四处(全部是**字面量**,不接受表达式):
- `class_name`:模块级唯一的类;
- `base_name`:它继承的基类(必须是候选版模板 `SignalTemplate`);
- `parameters`:可优化参数名单(字面量列表);
- `param_grid`:参数网格(字面量 dict);
- `defaults`:类体里写成字面量的类属性(框架据此还原"这一格的**有效参数**",
  与旧路径"候选类属性覆盖模板默认值"的口径一致)。

`parameters` 必须是字面量,这是 ADR 9.1 R3 登记的**第二处候选源码 breaking change**
(旧候选写的是 `parameters = TrendAtr15mStrategy.parameters + ["breakout_n"]`)。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field

# 候选版模板的基类名(候选进程里唯一的可用基类)
CANDIDATE_BASE = "SignalTemplate"
# 候选版模板的导入位置(候选源码必须从这里导入基类)
CANDIDATE_BASE_MODULE = "candidate.signal_template"


# 给模型看的说明只能是**固定文字**:完整 message 里有候选可控的东西(类名、参数名、被赋值的名字……),
# 那是不可信文本,回给模型就是提示词注入通道(仓库硬规矩:模型可见出口只回固定说明,不回传抽取出来的标识符)。
# 做法:按 message 里**框架自己写的固定短语**选一条固定说明;候选可控的子串最多只影响「选哪一条」,进不了输出。
_GENERIC_PUBLIC = ("候选声明不符合新格式要求:须是 SignalTemplate 子类,parameters 与 param_grid 写成字面量,"
                   "类体与模块级只含简单赋值与方法(详情见本机日志)")
_PUBLIC_BY_PHRASE = (
    ("不是字面量", "parameters 与 param_grid 必须写成字面量"),
    ("没有类定义", "候选源码里没有类定义"),
    ("多于一个顶层类", "候选源码里只允许有一个顶层类"),
    ("恰好继承一个基类", "候选类必须直接继承 SignalTemplate"),
    ("基类必须是直接的名字", "候选类必须直接继承 SignalTemplate"),
    ("必须继承候选版模板", "候选类必须直接继承 SignalTemplate"),
    ("缺少 parameters", "候选类缺少 parameters 声明"),
    ("parameters 必须是非空", "parameters 必须是非空的字面量列表"),
    ("parameters 的每一项", "parameters 的每一项都必须是合法的参数名"),
    ("parameters 里有重复项", "parameters 里有重复项"),
    ("缺少 param_grid", "候选类缺少 param_grid 声明"),
    ("param_grid 必须是非空", "param_grid 必须是非空的字面量 dict"),
    ("param_grid 的键", "param_grid 的键必须是字符串"),
    ("的取值必须是非空列表", "param_grid 的每个轴必须是非空列表"),
    ("的取值必须全是数值", "param_grid 的取值必须全是数值"),
    ("模块级只允许", "模块级只允许 import、函数定义、文档字符串与简单常量赋值(不得改写候选类)"),
    ("特殊方法", "候选类不得定义特殊方法(双下划线方法)"),
    ("只允许对简单名字赋值", "类体里只允许对简单名字赋值"),
    ("海象运算符", "类体里不得使用海象运算符"),
    ("被赋值了多次", "类体里有名字被赋值了多次"),
    ("类体里只允许赋值", "类体里只允许赋值、方法定义与文档字符串"),
    ("不是合法的 UTF-8", "候选源码不是合法的 UTF-8"),
    ("语法错误", "候选源码有语法错误"),
    ("必须 `from", "候选源码必须 from candidate.signal_template import SignalTemplate"),
)


class SpecError(ValueError):
    """候选源码的声明读不出来(动态构造、缺声明、语法错误)⇒ fail-closed 拒绝评估。

    `str(exc)` 是完整信息(含标识符与行号,**只给本机日志/人看**);`exc.public` 是固定说明(可以给模型看)。
    少数调用点显式传 `public=`:那里的文字只含框架自己的名字,不含候选可控的字符串。"""

    def __init__(self, message: str, public: str | None = None):
        super().__init__(message)
        self.public = public or next((text for phrase, text in _PUBLIC_BY_PHRASE if phrase in message), _GENERIC_PUBLIC)


@dataclass(frozen=True)
class CandidateSpec:
    """候选源码的静态声明(框架侧读出来的,可信)。"""

    class_name: str
    base_name: str
    parameters: tuple[str, ...] = ()
    param_grid: dict[str, tuple[int | float, ...]] = field(default_factory=dict)
    defaults: dict[str, object] = field(default_factory=dict)
    # 类体里被赋值、但值读不成数值/字符串/布尔字面量的名字(表达式、别的名字、列表……)。
    # 框架参数名落在这里面就拒绝评估:框架读不到它的值,会悄悄用模板默认值,与旧路径不等价。
    nonliteral: tuple[str, ...] = ()

    def effective_params(self, params: dict | None = None) -> dict[str, object]:
        """这一格的**有效参数** = 类体默认值 ← 网格/调用方给的参数(后者覆盖前者)。"""
        merged = dict(self.defaults)
        merged.update(params or {})
        return merged


def _literal(node: ast.AST):
    """把 AST 节点求成 Python 字面量;不是字面量就抛 SpecError(不猜、不执行)。"""
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise SpecError(f"不是字面量(请写成字面量): {type(node).__name__}") from exc


def _module_classes(tree: ast.Module) -> list[ast.ClassDef]:
    return [node for node in tree.body if isinstance(node, ast.ClassDef)]


def _find_class(tree: ast.Module) -> ast.ClassDef:
    classes = _module_classes(tree)
    if not classes:
        raise SpecError("候选源码里没有类定义")
    if len(classes) > 1:
        raise SpecError(f"候选源码里有多于一个顶层类({', '.join(c.name for c in classes)}):只允许一个")
    return classes[0]


def _base_name(node: ast.ClassDef) -> str:
    if len(node.bases) != 1:
        raise SpecError(f"候选类必须恰好继承一个基类({CANDIDATE_BASE})")
    base = node.bases[0]
    if not isinstance(base, ast.Name):
        raise SpecError(f"候选类的基类必须是直接的名字(现在是 {type(base).__name__})")
    return base.id


def _imports_base(tree: ast.Module) -> bool:
    """源码里是否 `from candidate.signal_template import SignalTemplate`。"""
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            if node.module == CANDIDATE_BASE_MODULE:
                if any(alias.name == CANDIDATE_BASE for alias in node.names):
                    return True
    return False


def _class_attribute(node: ast.ClassDef, name: str) -> ast.AST | None:
    """类体里的 `name = <表达式>`(只看类体第一层的直接赋值)。"""
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name) and stmt.target.id == name and stmt.value is not None:
                return stmt.value
    return None


def _parse_parameters(node: ast.ClassDef) -> tuple[str, ...]:
    value_node = _class_attribute(node, "parameters")
    if value_node is None:
        raise SpecError("候选类缺少 parameters 声明(可优化参数名单)")
    value = _literal(value_node)
    if not isinstance(value, (list, tuple)) or not value:
        raise SpecError("parameters 必须是非空的字面量列表")
    if not all(isinstance(item, str) and item.isidentifier() for item in value):
        raise SpecError("parameters 的每一项都必须是合法的参数名(字符串标识符)")
    if len(set(value)) != len(value):
        raise SpecError("parameters 里有重复项")
    return tuple(value)


def _parse_grid(node: ast.ClassDef) -> dict[str, tuple[int | float, ...]]:
    value_node = _class_attribute(node, "param_grid")
    if value_node is None:
        raise SpecError("候选类缺少 param_grid 声明(参数网格)")
    value = _literal(value_node)
    if not isinstance(value, dict) or not value:
        raise SpecError("param_grid 必须是非空的字面量 dict")
    axes: dict[str, tuple[int | float, ...]] = {}
    for name, values in value.items():
        if not isinstance(name, str):
            raise SpecError("param_grid 的键必须是字符串")
        if not isinstance(values, (list, tuple)) or not values:
            raise SpecError(f"param_grid[{name!r}] 的取值必须是非空列表")
        numbers: list[int | float] = []
        for item in values:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise SpecError(f"param_grid[{name!r}] 的取值必须全是数值")
            # **保持原样,不转 float**:`20` 变成 `20.0` 后,候选拿去当窗口长度(talib / range)会报错,
            # 也会让新旧两条路径对同一份源码给出不同的参数(等价性探针 A4 会抓到)。
            numbers.append(item)
        axes[name] = tuple(numbers)
    return axes


def _parse_defaults(node: ast.ClassDef) -> tuple[dict[str, object], tuple[str, ...]]:
    """类体里的标量类属性:(能读成字面量的 {名: 值}, 读不成字面量的名字)。

    认 `x = 2.5`、`x: float = 2.5`、`x = -2.5`(一元运算)。表达式、别的名字、容器一律记入 `nonliteral`
    而**不是悄悄丢掉**——丢掉会让框架侧用模板默认值,与旧路径(候选类属性直接生效)不等价,且没有任何报错。
    `parameters` / `param_grid` 另有专门的解析,不在这里。
    """
    defaults: dict[str, object] = {}
    nonliteral: list[str] = []
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            targets = [t for t in stmt.targets if isinstance(t, ast.Name)]
            value_node = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value is not None:
            targets, value_node = [stmt.target], stmt.value
        else:
            continue
        for target in targets:
            if target.id in ("parameters", "param_grid"):
                continue
            try:
                value = ast.literal_eval(value_node)
            except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
                nonliteral.append(target.id)
                continue
            if isinstance(value, (int, float, str, bool)):
                defaults[target.id] = value
            else:
                nonliteral.append(target.id)
    return defaults, tuple(dict.fromkeys(nonliteral))


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _simple_targets(stmt: ast.stmt) -> list[ast.expr]:
    return list(stmt.targets) if isinstance(stmt, ast.Assign) else [stmt.target]


def _check_module_structure(tree: ast.Module, class_node: ast.ClassDef) -> None:
    """模块级只允许 import、函数定义、文档字符串与**对简单名字**的赋值,且不得重绑定候选类。

    框架只读 AST、从不 exec 候选,所以任何能在类定义**之后**改写类的写法(`Candidate.param_grid = ...`、
    `Candidate = decorate(Candidate)`、`if` 里改写、`+=`)都会让「框架读到的」与「候选进程里生效的」不同——一律拒绝评估。
    `locals()` / `setattr` / `metaclass=` 这类由静态筛查拦,这里不重复。"""
    for stmt in tree.body:
        if stmt is class_node or isinstance(stmt, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Pass)) or _is_docstring(stmt):
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = _simple_targets(stmt)
            if all(isinstance(t, ast.Name) for t in targets) and class_node.name not in {t.id for t in targets}:
                continue
        raise SpecError(f"第 {stmt.lineno} 行:模块级只允许 import、函数定义、文档字符串与简单常量赋值"
                        "(不得改写或重绑定候选类;框架只读源码结构,读到的必须与运行时生效的一致)")


def _check_class_body(node: ast.ClassDef) -> None:
    """类体只允许:文档字符串、`pass`、对**简单名字**的赋值(每个名字至多一次)、非特殊方法的定义。

    被拒绝的写法都会让框架(读第一次/看不见块内赋值)与候选进程(运行时生效最后一次/块内赋值)读出不同的声明:
    - 重复赋值:`parameters` 重复 ⇒ 运行时网格轴对候选无作用,相邻格子信号相同,参数高原被人为做平;
    - 块里(`if`/`try`/`for`/`with`)赋值、`+=`、元组解包、`del`、嵌套类、海象运算符:框架看不见,静默用模板默认值;
    - `__init__` / `__init_subclass__` / `__set_name__` 等特殊方法:`self.atr_mult = 5` 让候选实际用 5,框架止损按类体字面量算。"""
    assigned: set[str] = set()
    for stmt in node.body:
        if _is_docstring(stmt) or isinstance(stmt, ast.Pass):
            continue
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name.startswith("__") and stmt.name.endswith("__"):
                raise SpecError(f"第 {stmt.lineno} 行:候选类不得定义特殊方法 {stmt.name}"
                                "(会让候选实际生效的参数与框架读到的不同)")
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            targets = _simple_targets(stmt)
            if not all(isinstance(t, ast.Name) for t in targets):
                raise SpecError(f"第 {stmt.lineno} 行:类体里只允许对简单名字赋值(不得解包、不得写属性/下标)")
            if any(isinstance(sub, ast.NamedExpr) for sub in ast.walk(stmt)):
                raise SpecError(f"第 {stmt.lineno} 行:类体里不得使用海象运算符")
            if isinstance(stmt, ast.AnnAssign) and stmt.value is None:
                continue
            for target in targets:
                if target.id in assigned:
                    raise SpecError(f"第 {stmt.lineno} 行:类体里 {target.id} 被赋值了多次"
                                    "(框架读第一次、运行时生效最后一次,二者会不一致)")
                assigned.add(target.id)
            continue
        raise SpecError(f"第 {stmt.lineno} 行:类体里只允许赋值、方法定义与文档字符串(出现了 {type(stmt).__name__})")


def parse_spec(source: str | bytes) -> CandidateSpec:
    """解析候选源码,返回静态声明。读不出来一律抛 SpecError(fail-closed)。"""
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SpecError("候选源码不是合法的 UTF-8") from exc
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise SpecError(f"候选源码语法错误: 第 {exc.lineno} 行: {exc.msg}") from exc

    node = _find_class(tree)
    _check_module_structure(tree, node)
    base_name = _base_name(node)
    if base_name != CANDIDATE_BASE:
        raise SpecError(f"候选类必须继承候选版模板 {CANDIDATE_BASE}(现在是 {base_name})")
    if not _imports_base(tree):
        raise SpecError(f"候选源码必须 `from {CANDIDATE_BASE_MODULE} import {CANDIDATE_BASE}`")
    _check_class_body(node)

    defaults, nonliteral = _parse_defaults(node)
    return CandidateSpec(
        class_name=node.name,
        base_name=base_name,
        parameters=_parse_parameters(node),
        param_grid=_parse_grid(node),
        defaults=defaults,
        nonliteral=nonliteral,
    )


def check_framework_defaults(spec: CandidateSpec, framework_parameters) -> None:
    """框架参数(止损距离、指标窗口……)的类体默认值必须是字面量,否则拒绝评估(fail-closed)。

    候选自己的额外参数(如 `breakout_n`)框架不认识,写成表达式没关系;但 `atr_mult = 1.5 * 2` 这种
    会改变框架模板止损距离的写法,框架必须读得到值才能采用同样的有效参数。"""
    clash = sorted(set(spec.nonliteral) & set(framework_parameters))
    if clash:
        # 这里的名字是 `spec.nonliteral ∩ framework_parameters`——框架自己的参数名,不是候选可控的字符串,可以进 public
        names = ", ".join(clash)
        raise SpecError(f"类体里的 {names} 必须写成数值字面量(框架要采用同样的有效参数,读不出就不评估)",
                        public=f"类体里的 {names} 必须写成数值字面量(框架要采用同样的有效参数)")


def proxy_class(spec: CandidateSpec) -> type:
    """框架进程里**代表候选**的代理类:只带 `parameters` 与 `param_grid` 两个声明。

    候选源码永远不在框架进程里 `exec`——`research_grid.select_params` / `run_evaluation` 只需要
    这两个属性(读网格、校验轴名),所以给它们一个由 AST 字面量合成的壳就够了。
    取值列表按 `research_grid.validate_grid` 的要求是 `list`。
    """
    return type(spec.class_name, (), {
        "parameters": list(spec.parameters),
        "param_grid": {name: list(values) for name, values in spec.param_grid.items()},
        "__module__": "candidate_proxy",
    })


def framework_setting(spec: CandidateSpec, params: dict | None, framework_parameters) -> dict:
    """这一格交给**框架引擎**的设置 = (类体字面量默认值 ← 网格给的参数)∩ 框架模板认识的参数名。

    为什么要带类体默认值:旧路径里候选类写 `atr_mult = 2.0` 会直接改变框架模板的止损距离与指标窗口;
    新路径里候选只产出信号,框架侧必须自己采用同样的有效参数,否则两条路径对同一份源码的止损不同。
    候选自己的额外参数(如 `breakout_n`)框架不认识,只留在候选进程里。
    调用方须对返回值再过一遍受保护参数检查(`risk_budget` 之类写在类体里也不得生效)。
    """
    allowed = set(framework_parameters)
    merged = spec.effective_params(params)
    return {name: value for name, value in merged.items() if name in allowed}
