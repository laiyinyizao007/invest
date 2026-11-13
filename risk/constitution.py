"""不可变宪法:候选策略能改什么、不能改什么的机器可校验清单。

为什么需要:`risk-floor-immutability` ADR 说"风控底线永不优化",但此前只靠两条软约束保证——
候选继承基类、以及 `research_grid.FORBIDDEN_AXES` 不让风控参数进网格。缺口有三处:
1. 候选可以直接在类体里写 `risk_budget = 99999`,把底线改掉;
2. 调用方可以在请求参数里带 `risk_budget`,而 `backtest`/`gate` 原本不校验参数键名;
3. 候选可以重写 `on_trade`/`_try_enter` 等路径,绕过连亏熔断与预算检查,或直接下单。

设计(见 docs/adr/20261113-evolution-engine.md 阶段 2):
- 内置清单在代码里(BUILTIN_*),清单文件 `config/immutable_constitution.json` **只能追加**,不能删减内置项;
- 静态筛查 `screen_source`(看 AST):拒绝覆盖受保护成员、实例上改写受保护属性、改写受保护模块函数、
  调用下单接口、param_grid 里出现受保护参数。**新增**成员不在静态层管(模板成员名单在运行时才知道);
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

    return Constitution(
        version=1,
        template=template,
        allowed_overrides=allowed | builtin.allowed_overrides,
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
    return name in c.protected_names or name in c.protected_parameters


def screen_source(source: str, c: Constitution | None = None) -> list[str]:
    """静态筛查:返回可疑构造的中文说明;空列表表示未发现问题。**不是安全保证**,只是纵深防御的一层。

    语法错误不在这里报(由 `research_sandbox.screen_source` 统一报告),本函数对语法错误返回空列表。
    静态层不判断"是否重写了模板成员"——模板成员名单要到运行时才知道;那一层由 `check_class` 负责。"""
    c = c or default()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for name in sorted(_class_body_names(node)):
                if _is_protected(name, c):
                    findings.append(f"第 {node.lineno} 行: 类 {node.name} 覆盖了受保护成员 {name}")
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_protected(stmt.name, c):
                    findings.append(f"第 {stmt.lineno} 行: 类 {node.name} 重写了受保护方法 {stmt.name}")
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
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Del):
            if _is_protected(node.attr, c):
                findings.append(f"第 {node.lineno} 行: 删除了受保护属性 {node.attr}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in c.forbidden_order_calls:
                findings.append(f"第 {node.lineno} 行: 禁止直接下单/撤单({node.func.attr})")
            elif isinstance(node.func, ast.Name) and node.func.id in c.forbidden_order_calls:
                findings.append(f"第 {node.lineno} 行: 禁止直接下单/撤单({node.func.id})")
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value in c.protected_parameters:
                    findings.append(f"第 {node.lineno} 行: param_grid 含受保护参数 {key.value}")
    return findings


# ---------- 运行时校验 ----------

def check_params(params: object, c: Constitution | None = None) -> list[str]:
    """校验调用方给的设置字典:不得包含受保护参数(否则风控底线可被请求方绕过)。"""
    c = c or default()
    if not isinstance(params, dict):
        return ["params 必须是 JSON 对象"]
    findings = []
    for key in params:
        if not isinstance(key, str):
            findings.append(f"参数名非法: {key!r}")
        elif _is_protected(key, c):
            findings.append(f"参数 {key} 属于受保护项,不得由调用方指定")
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


def family_root(template: type) -> type:
    """模板所属策略家族的根:模板自己往上,第一个不是 vnpy CtaTemplate 的祖先。

    要求继承家族根而不是模板本身:研究与沙盒也会跑日线族(TrendAtrStrategy 的子类),
    只认 15 分钟模板会把它们误拒。"""
    ancestors = [k for k in template.__mro__[1:]
                 if k.__name__ not in ("object", "CtaTemplate", "Template")
                 and not k.__module__.startswith("vnpy")]
    return ancestors[-1] if ancestors else template


def check_class(candidate: type, template: type, c: Constitution | None = None) -> list[str]:
    """运行时校验候选类:必须继承模板所属家族;不得重写白名单以外的**模板成员**。

    覆盖三处:候选自身的类体、候选与模板之间的中间基类、以及类创建后动态打的补丁。"""
    c = c or default()
    findings: list[str] = []
    root = family_root(template)
    if not (isinstance(candidate, type) and issubclass(candidate, root)):
        return [f"候选类必须继承模板家族 {root.__name__}"]

    members = template_members(template) | template_members(root)
    # 候选直接继承模板时,模板自身的成员不算"候选重写";候选只继承家族根时,家族根也不算。
    stop_at = template if (isinstance(candidate, type) and issubclass(candidate, template)) else root
    chain: list[type] = []
    for klass in candidate.__mro__:
        if klass is stop_at:
            break
        chain.append(klass)

    for klass in chain:
        for name in sorted(n for n in vars(klass) if not _is_dunder(n)):
            if name in c.allowed_overrides:
                continue
            if _is_protected(name, c):
                findings.append(f"候选重写了受保护成员 {name}({klass.__name__})")
            elif name in members:
                findings.append(f"候选重写了模板成员 {name},但它不在允许重写的白名单里({klass.__name__})")
    return findings
