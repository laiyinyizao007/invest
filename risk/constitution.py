"""不可变宪法:候选策略能改什么、不能改什么的机器可校验清单。

**这里的每一层都是纵深防御,不是安全边界**:AST 黑名单封不住 Python 的动态能力
(运行时构造的名字、字节码、导入期副作用都绕得开)。真正的边界是容器隔离
(`docker-compose.research.yml`:无网络、只读挂载、`cap_drop ALL`)与"运行前由人读过代码"。
本模块单独阅读时请把这句放在心里——它挡的是"明面上的越权写法"。

为什么需要:`risk-floor-immutability` ADR 说"风控底线永不优化",但此前只靠两条软约束保证——
候选继承基类、以及 `research_grid.FORBIDDEN_AXES` 不让风控参数进网格。缺口有三处:
1. 候选可以直接在类体里写 `risk_budget = 99999`,把底线改掉;
2. 调用方可以在请求参数里带 `risk_budget`,而 `backtest`/`gate` 原本不校验参数键名;
3. 候选可以重写 `on_trade`/`_try_enter` 等路径,绕过连亏熔断与预算检查,或直接下单。

设计(见 docs/adr/20261113-evolution-engine.md 阶段 2):
- 内置清单在代码里(BUILTIN_*),清单文件 `config/immutable_constitution.json` **只能追加**,不能删减内置项;
- 静态筛查 `screen_source`(看 AST):拒绝覆盖受保护成员、实例上改写受保护属性、改写受保护模块函数、
  **引用**下单接口或受禁 dunder(不只是调用)、引用框架的"按名 setter"、
  受保护参数作为关键字参数或字典键出现。**新增**成员不在静态层管(模板成员名单在运行时才知道);
- 运行时 `check_class`(对照模板类的真实成员):候选必须继承模板;不得重写白名单以外的**模板成员**
  (含中间基类与类创建后打的补丁)。候选自己新增的成员(新指标、新辅助方法)允许;
- 运行时 `check_params`:拒绝调用方在设置字典里指定受保护参数;
- 一律 fail-closed:清单文件缺失或非法即抛 ConstitutionError,不做"默认放行"。

清单文件的字段:
  allowed_overrides    候选可重写的模板成员白名单(只能追加)
  protected_names      不可触碰的成员(只能追加)
  protected_parameters 不可出现在设置字典与网格里的参数(只能追加)
  forbidden_order_calls 候选不得调用的下单/撤单接口(只能追加)
  protected_module_attrs 候选不得改写的模块级风控函数/类(只能追加)
"""
from __future__ import annotations

import ast
import dataclasses
import importlib
import json
import re
from pathlib import Path

CONSTITUTION_FILE = Path(__file__).resolve().parents[1] / "config" / "immutable_constitution.json"

# ---- 内置清单(清单文件只能追加,不能删减) ----
BUILTIN_TEMPLATE = "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy"

# 清单文件里 template 只能取这些内置模板之一:模板是"哪些成员受保护"的基准,
# 换成别的类(如 vnpy 的 CtaTemplate)会让继承约束形同虚设。要换模板必须改代码、走代码评审。
BUILTIN_ALLOWED_TEMPLATES = (
    "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy",
    "strategies.trend_atr_strategy:TrendAtrStrategy",
)

_TEMPLATE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$")

# 风控底线与固定参数:不得被候选覆盖,不得进设置字典,不得进 param_grid
BUILTIN_PROTECTED_PARAMETERS = (
    "risk_budget",          # 单笔最大亏损预算(元)
    "max_daily_losses",     # 当日连亏熔断笔数
    "atr_window",           # ATR 窗口:固定,不参与优化
    "history_vt_symbol",    # 历史日线代码
)

# 受保护的类成员:候选不得在类体里重新赋值,也不得重写这些方法
BUILTIN_PROTECTED_NAMES = (
    # 风控状态与工具
    "guard", "trail", "am", "bg", "pending_dist", "pending_entry", "_stop_armed",
    "atr_value", "stop_price", "entry_price", "entry_dist", "prev_pos", "skipped_by_budget",
    "trail_side", "trail_extreme", "trail_stop", "is_live", "state_consistent",
    # 下单与持仓管理路径:重写即绕过预算检查与连亏熔断
    "__init__", "on_bar", "on_tick", "on_init", "on_start", "on_stop", "on_trade", "on_order",
    "on_stop_order", "on_account_status", "on_position", "on_cancel_order", "on_disconnect",
    "_process_daily_bar", "_manage_position", "_try_enter", "_send_entry", "_place_protective_stop",
    "_place_pending_entry", "_stop_order_price", "_ensure_protective_stop", "_complete_stale_daily_bar",
) + BUILTIN_PROTECTED_PARAMETERS

# 候选不得调用的下单/撤单接口
BUILTIN_FORBIDDEN_ORDER_CALLS = (
    "buy", "sell", "short", "cover", "send_order", "send_stop_order", "send_local_stop_order",
    "cancel_order", "cancel_all", "cancel_all_orders",
)

# 候选不得改写的模块级风控函数/类(防止绕过预算与熔断)
BUILTIN_PROTECTED_MODULE_ATTRS = (
    "within_budget", "stop_distance", "DailyLossGuard", "TrailingStop", "ceil_to", "floor_to",
)

# 候选可以重写的模板成员(白名单);不是白名单的**模板成员**一律不许重写
BUILTIN_ALLOWED_OVERRIDES = (
    "entry_signal", "param_grid", "parameters", "fast_window", "slow_window", "atr_mult", "take_profit_r",
)

# 候选不得定义、也不得调用的 dunder:它们能在运行时改写属性,绕开静态筛查与成员比对。
# __init__ 例外(它本来就在受保护名单里,单独处理)。
BUILTIN_FORBIDDEN_DUNDERS = (
    "__setattr__", "__delattr__", "__getattribute__", "__getattr__",
    "__init_subclass__", "__set_name__",
)

# 框架自带的"按名 setter":这类方法把调用方给的字典**按键名** setattr 到实例上。
# 候选能**直接调用**的只有 CtaTemplate.update_setting 一处:
#   for name in self.parameters: if name in setting: setattr(self, name, setting[name])
# (另有两处按名恢复的路径——CTA 引擎按 strategy.variables 恢复变量、spreadtrading 的对应实现——
#  它们由框架自己调用,当前无害是因为 variables 是模板成员、候选改不了,不是这里的名单保证的。)
# 而模板的 parameters 列表里正好有四个受保护参数(risk_budget / max_daily_losses /
# atr_window / history_vt_symbol),于是候选只要写
#     self.update_setting(dict(risk_budget=1e12))
# 就能把风控底线改掉——而这个名字在源码里既不是属性赋值也不是字典键,
# 按语法位置筛查看不见它(第 5 轮外部复核实测:静态层与运行时层全绿,底线从 300 被改成 1e12)。
# 所以这一类入口**整体禁用**:不是逐个补 API,而是不给"按名改写"留任何入口。
#
# 但要说清楚:静态层封的是**已知写法**,不是能力本身——同一条后果还能从别的路径拿到
# (第 7 轮实测:经异常回溯拿到 frame.f_builtins 里的 setattr;以及 unittest.mock.patch.object)。
# 真正把能力收走的是**执行时不给这些内建**(scripts/research_sandbox.py::safe_builtins):
# 候选的内建字典里没有 setattr,那些路径就都不成立。
BUILTIN_FORBIDDEN_BY_NAME_SETTERS = ("update_setting",)

# 由元类自动注入类字典的成员(ABCMeta 会给每个类塞一个 _abc_impl)。
# 它们不是候选"重写"的产物,比对成员时必须忽略,否则每个真实候选都会被误判。
METACLASS_GENERATED = ("_abc_impl",)



class ConstitutionError(Exception):
    """清单缺失、非法或与模板不符(fail-closed)。"""


@dataclasses.dataclass(frozen=True)
class Constitution:
    version: int
    template: str
    allowed_overrides: frozenset[str]
    protected_names: frozenset[str]
    protected_parameters: frozenset[str]
    forbidden_order_calls: frozenset[str]
    protected_module_attrs: frozenset[str]


def default() -> Constitution:
    """内置清单(不读文件);用于单元测试与"文件尚未落地"的场景。"""
    return Constitution(
        version=1,
        template=BUILTIN_TEMPLATE,
        allowed_overrides=frozenset(BUILTIN_ALLOWED_OVERRIDES),
        protected_names=frozenset(BUILTIN_PROTECTED_NAMES),
        protected_parameters=frozenset(BUILTIN_PROTECTED_PARAMETERS),
        forbidden_order_calls=frozenset(BUILTIN_FORBIDDEN_ORDER_CALLS),
        protected_module_attrs=frozenset(BUILTIN_PROTECTED_MODULE_ATTRS),
    )


def _require_name_list(value: object, field: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v.isidentifier() for v in value):
        raise ConstitutionError(f"清单字段 {field} 必须是标识符字符串列表")
    return frozenset(value)


def load(path: Path | None = None) -> Constitution:
    """载入清单文件;文件缺失或非法一律抛错(fail-closed)。文件只能在内置清单之上追加。"""
    path = Path(path) if path is not None else CONSTITUTION_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConstitutionError(f"不可变宪法清单缺失或不可读(fail-closed): {path.name}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConstitutionError(f"不可变宪法清单不是合法 JSON(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise ConstitutionError("不可变宪法清单必须是 JSON 对象")
    if data.get("version") != 1:
        raise ConstitutionError(f"不可变宪法清单版本不支持: {data.get('version')!r}")
    template = data.get("template")
    if not isinstance(template, str) or not _TEMPLATE_RE.match(template):
        raise ConstitutionError("不可变宪法清单的 template 必须是 '模块路径:类名' 形式")
    # 模板只能取内置白名单里的类:换成任意可导入的类(如 vnpy 的 CtaTemplate)会让继承约束形同虚设。
    # 要换模板必须改代码、走代码评审。
    if template not in BUILTIN_ALLOWED_TEMPLATES:
        raise ConstitutionError(
            f"不可变宪法清单的 template 不在内置白名单内: {template!r};"
            f"可选: {', '.join(BUILTIN_ALLOWED_TEMPLATES)}")
    for field in ("allowed_overrides", "protected_names", "protected_parameters",
                  "forbidden_order_calls", "protected_module_attrs"):
        if field not in data:
            raise ConstitutionError(f"不可变宪法清单缺少字段 {field}")

    builtin = default()
    allowed = _require_name_list(data["allowed_overrides"], "allowed_overrides")
    protected_names = _require_name_list(data["protected_names"], "protected_names")
    protected_params = _require_name_list(data["protected_parameters"], "protected_parameters")
    order_calls = _require_name_list(data["forbidden_order_calls"], "forbidden_order_calls")
    module_attrs = _require_name_list(data["protected_module_attrs"], "protected_module_attrs")

    if allowed & builtin.protected_names:
        raise ConstitutionError("allowed_overrides 不得包含内置受保护成员(清单只能追加,不能放宽底线)")
    if allowed & builtin.protected_parameters:
        raise ConstitutionError("allowed_overrides 不得包含内置受保护参数(清单只能追加,不能放宽底线)")
    if protected_params & builtin.allowed_overrides:
        raise ConstitutionError("protected_parameters 不得与内置允许重写的成员冲突")
    # 文件自身新增的项之间也不能互相抵消:同一个名字既在白名单又在受保护名单里时,
    # 白名单在运行时优先生效,新增的保护会静默失效——必须报错而不是让它生效。
    # 冲突要在**合并后**的集合上查:文件新增的 protected_names 也可能撞上内置白名单。
    effective_allowed = allowed | builtin.allowed_overrides
    effective_protected = (protected_names | builtin.protected_names) | (protected_params | builtin.protected_parameters)
    if effective_allowed & effective_protected:
        clash = ", ".join(sorted(effective_allowed & effective_protected))
        raise ConstitutionError(f"白名单与受保护清单冲突({clash}):白名单会胜出,保护将失效")
    if protected_names & protected_params:
        raise ConstitutionError("protected_names 与 protected_parameters 有重叠:同一项被登记两次")

    return Constitution(
        version=1,
        template=template,
        allowed_overrides=effective_allowed,
        protected_names=protected_names | builtin.protected_names,
        protected_parameters=protected_params | builtin.protected_parameters,
        forbidden_order_calls=order_calls | builtin.forbidden_order_calls,
        protected_module_attrs=module_attrs | builtin.protected_module_attrs,
    )


def load_template(c: Constitution | None = None) -> type:
    """按 '模块:类' 载入模板类;导入或取属性失败一律抛错(fail-closed)。"""
    c = c or default()
    module_name, _, class_name = c.template.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:                                  # noqa: BLE001 - 任何导入失败都必须拒绝
        raise ConstitutionError(f"模板类无法导入(fail-closed): {c.template}: {exc}") from exc
    cls = getattr(module, class_name, None)
    if not isinstance(cls, type):
        raise ConstitutionError(f"模板类不存在(fail-closed): {c.template}")
    return cls


# ---------- 静态筛查 ----------

def _class_body_names(node: ast.ClassDef) -> set[str]:
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
    return names


def _is_protected(name: str, c: Constitution) -> bool:
    """名字是否受保护。

    两个集合是**各自独立**的:清单文件里两者有交集时 `load()` 会拒绝加载,内置常量里
    `BUILTIN_PROTECTED_NAMES` 已包含 `BUILTIN_PROTECTED_PARAMETERS`。这里仍然查两遍,
    免得"参数只登记在 protected_parameters"时被漏掉。
    """
    return name in c.protected_names or name in c.protected_parameters


def _protected_lower(c: Constitution) -> set[str]:
    """受保护项的小写形式:与 `check_params` 同一口径(大小写不敏感)。

    `Risk_Budget` 与 `risk_budget` 是两个不同的属性,精确匹配会让这条路径绕过参数校验;
    静态层的关键字参数检查用同一把尺子,免得两处口径不一致。"""
    return {name.lower() for name in c.protected_names} | {name.lower() for name in c.protected_parameters}


def screen_source(source: str, c: Constitution | None = None) -> list[str]:
    """静态筛查:返回可疑构造的中文说明;空列表表示未发现问题。**不是安全保证**,只是纵深防御的一层。

    检查项:
    - 类体内覆盖受保护成员或方法
    - 类体内定义受禁 dunder(如 `__setattr__`)或"按名 setter"
    - 自定义 metaclass、使用类装饰器
    - 实例上改写或删除受保护属性
    - 改写受保护的风控模块函数
    - 引用受禁 dunder(裸名或别名)、引用下单/撤单接口(裸名或别名)
    - 引用框架的"按名 setter"(`update_setting`):它能把调用方的字典按键名 setattr 到实例上
    - 受保护参数作为关键字参数(`dict(risk_budget=…)`)或字典键出现

    语法错误在这里报(fail-closed):解析不过就等于没筛查,返回空列表会让"没查"看起来像"查过没问题"。
    静态层不判断"是否重写了模板成员"——模板成员名单要到运行时才知道;那一层由 `check_class` 负责。"""
    c = c or default()
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"语法错误: {exc.msg} (第 {exc.lineno} 行)"]

    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for name in sorted(_class_body_names(node)):
                if _is_protected(name, c):
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 覆盖了受保护成员 {name}")
                elif name in BUILTIN_FORBIDDEN_DUNDERS:
                    # `class C: __setattr__ = print` 这种**赋值**形式的定义,
                    # 与 `def __setattr__` 一样是候选自定义受禁 dunder,静态层也要拦
                    # (运行时层能抓到,但那时已经在跑候选了)。
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 定义了受禁的 {name}(可绕过属性改写检查)")
                elif name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 定义了受禁的按名 setter {name}(可按键名改写实例属性)")
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if _is_protected(stmt.name, c):
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 重写了受保护方法 {stmt.name}")
                    elif stmt.name in BUILTIN_FORBIDDEN_DUNDERS:
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 定义了受禁的 {stmt.name}(可绕过属性改写检查)")
                    elif stmt.name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                        findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 定义了受禁的按名 setter {stmt.name}(可按键名改写实例属性)")
            # 自定义元类:元类的 __setattr__ 能在类创建时改写属性,绕开成员比对
            for kw in node.keywords:
                if kw.arg == "metaclass":
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 不得自定义 metaclass")
            # 类装饰器:装饰器可以在类创建后改写它
            for dec in node.decorator_list:
                findings.append(f"第 {node.lineno} 行: 类 {node.name} 不得使用类装饰器")
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            if isinstance(target, ast.Name) and _is_protected(target.id, c):
                findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
            elif isinstance(target, ast.Attribute) and _is_protected(target.attr, c):
                findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {target.attr}")
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_protected(target.id, c):
                    findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
                elif isinstance(target, ast.Attribute):
                    if _is_protected(target.attr, c):
                        findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {target.attr}")
                    elif target.attr in c.protected_module_attrs:
                        findings.append(f"第 {node.lineno} 行: 改写了受保护的风控函数 {target.attr}")
        elif isinstance(node, ast.AugAssign):
            target = node.target
            if isinstance(target, ast.Attribute) and _is_protected(target.attr, c):
                findings.append(f"第 {node.lineno} 行: 改写了受保护属性 {target.attr}")
            elif isinstance(target, ast.Name) and _is_protected(target.id, c):
                findings.append(f"第 {node.lineno} 行: 覆盖了受保护成员 {target.id}")
        elif isinstance(node, ast.Attribute):
            # 受禁 dunder 的**任何引用**都要拦,不只调用:`f = object.__setattr__` 之后
            # `f(self, "risk_budget", 1e9)` 同样能改写实例属性,而调用点已经看不出是谁了。
            if node.attr in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.attr}(可绕过属性改写检查)")
            elif node.attr in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                # 框架的按名 setter:名字出现在这里就说明候选想按键名改写实例属性。
                findings.append(f"第 {node.lineno} 行: 禁止引用按名 setter {node.attr}(可按键名改写实例属性)")
            elif node.attr in c.forbidden_order_calls:
                # 下单接口同理拦**引用**:`f = self.buy` 之后 `f(1, 1)` 的调用点看不出是下单。
                findings.append(f"第 {node.lineno} 行: 禁止引用下单/撤单接口 {node.attr}")
            elif isinstance(node.ctx, ast.Del) and _is_protected(node.attr, c):
                findings.append(f"第 {node.lineno} 行: 删除了受保护属性 {node.attr}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # 裸名字的引用同样要拦:`__setattr__(self, ...)`、`buy(...)`、以及先取别名再调用。
            if node.id in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.id}(可绕过属性改写检查)")
            elif node.id in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                findings.append(f"第 {node.lineno} 行: 禁止引用按名 setter {node.id}(可按键名改写实例属性)")
            elif node.id in c.forbidden_order_calls:
                findings.append(f"第 {node.lineno} 行: 禁止引用下单/撤单接口 {node.id}")
        elif isinstance(node, ast.keyword) and node.arg:
            # 关键字参数是**按名**传参的另一个入口:`dict(risk_budget=1e12)` 里这个名字
            # 是 ast.keyword.arg,既不是属性赋值也不是字典键,按语法位置筛查看不见。
            # 与 check_params 一样按大小写不敏感比对。
            if _is_protected(node.arg, c) or node.arg.lower() in _protected_lower(c):
                findings.append(f"第 {node.lineno} 行: 关键字参数 {node.arg} 属于受保护项,不得由候选指定")
        elif isinstance(node, ast.Dict):
            # 字典字面量的键:与参数层同一把尺子(大小写不敏感,且受保护**成员**同样算数)。
            # 早先这里只精确比对 protected_parameters,与 check_params 的口径不一致。
            protected_lower = _protected_lower(c)
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if _is_protected(key.value, c):
                        findings.append(f"第 {node.lineno} 行: 字典字面量含受保护项 {key.value}")
                    elif key.value.lower() in protected_lower:
                        findings.append(f"第 {node.lineno} 行: 字典字面量含受保护项 {key.value}(仅大小写不同)")
    return findings


# ---------- 运行时校验 ----------

def check_params(params: object, c: Constitution | None = None) -> list[str]:
    """校验调用方给的设置字典:不得包含受保护参数(否则风控底线可被请求方绕过)。

    受保护参数按**大小写不敏感**比对:vn.py 的 `update_setting` 直接 `setattr(self, name, ...)`,
    而 Python 属性名区分大小写,`Risk_Budget` 与 `risk_budget` 是两个不同的属性——
    候选只要自定义 `__setattr__` 就能把 `Risk_Budget` 重定向到真正的 `risk_budget`。
    精确匹配会让这条路径完全绕过参数校验。"""
    c = c or default()
    if not isinstance(params, dict):
        return ["params 必须是 JSON 对象"]
    findings = []
    protected_lower = _protected_lower(c)
    for key in params:
        if not isinstance(key, str):
            findings.append(f"参数名非法: {key!r}")
        elif _is_protected(key, c):
            findings.append(f"参数 {key} 属于受保护项,不得由调用方指定")
        elif key.lower() in protected_lower:
            findings.append(f"参数 {key} 与受保护参数仅大小写不同,不得由调用方指定")
    return findings


_DUNDER_OK = {"__init__"}          # 其余 dunder(__doc__、__module__、__qualname__ 等)由解释器自动生成,不算重写


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__") and name not in _DUNDER_OK


def template_members(template: type) -> set[str]:
    """模板(含其基类,不含 object)上的全部成员名,供清单漂移检查使用(不含自动生成的 dunder)。"""
    names: set[str] = set()
    for klass in template.__mro__:
        if klass is object:
            break
        names |= {n for n in vars(klass) if not _is_dunder(n)}
    return names


def _is_framework_class(klass: type) -> bool:
    """框架/标准库基类:不属于策略家族(要排除掉,否则家族根会算成 abc.ABC)。"""
    return (klass.__module__ in ("abc", "builtins")
            or klass.__name__ == "object"
            or klass.__module__.startswith("vnpy")
            or klass.__name__ in ("CtaTemplate", "Template"))


def family_root(template: type) -> type:
    """模板所属策略家族的根:MRO 里最靠近 object 的**应用层**基类。

    要求继承家族根而不是模板本身:研究与沙盒也会跑日线族(TrendAtrStrategy 的子类),
    只认 15 分钟模板会把它们误拒。

    真实继承链:
        TrendAtr15mStrategy -> TrendAtrStrategy -> CtaTemplate -> ABC -> object
    家族根是 TrendAtrStrategy。早先的实现靠"祖先名字不是 CtaTemplate/Template 且模块不以 vnpy 开头"
    来过滤,于是 abc.ABC(模块名 "abc",不在 vnpy 下)被当成了应用层基类,
    导致:①家族根算成 ABC;②模板自身的成员被误判为"候选重写",每一个真实候选都被拒。"""
    application = [k for k in template.__mro__ if not _is_framework_class(k)]
    return application[-1] if application else template


def check_class(candidate: type, template: type, c: Constitution | None = None) -> list[str]:
    """运行时校验候选类:必须继承模板所属家族;不得重写白名单以外的**模板成员**。

    覆盖三处:候选自身的类体、候选与模板之间的中间基类、以及类创建后动态打的补丁。"""
    c = c or default()
    findings: list[str] = []
    root = family_root(template)
    if not (isinstance(candidate, type) and issubclass(candidate, root)):
        return [f"候选类必须继承模板家族 {root.__name__}"]

    # 自定义元类:元类的 __setattr__ 能在类创建时改写属性,绕开成员比对
    if type(candidate) is not type(template):
        findings.append(f"候选类不得自定义 metaclass(实际 {type(candidate).__name__})")

    members = template_members(template) | template_members(root)
    # 候选直接继承模板时,模板自身的成员不算"候选重写";候选只继承家族根时,家族根也不算。
    stop_at = template if (isinstance(candidate, type) and issubclass(candidate, template)) else root
    chain: list[type] = []
    for klass in candidate.__mro__:
        if klass is stop_at:
            break
        chain.append(klass)

    for klass in chain:
        for name in sorted(vars(klass)):
            if name in BUILTIN_FORBIDDEN_DUNDERS:
                findings.append(f"候选定义了受禁的 {name}({klass.__name__}):可绕过属性改写检查")
                continue
            if name in BUILTIN_FORBIDDEN_BY_NAME_SETTERS:
                findings.append(f"候选定义了受禁的按名 setter {name}({klass.__name__}):可按键名改写实例属性")
                continue
            # 元类自动注入的成员(ABCMeta 给每个类塞 _abc_impl)不是候选"重写"的产物
            if name in METACLASS_GENERATED:
                continue
            if _is_dunder(name):
                continue
            if name in c.allowed_overrides:
                continue
            if _is_protected(name, c):
                findings.append(f"候选重写了受保护成员 {name}({klass.__name__})")
            elif name in members:
                findings.append(f"候选重写了模板成员 {name},但它不在允许重写的白名单里({klass.__name__})")
    return findings
