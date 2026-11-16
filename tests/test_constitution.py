"""risk/constitution.py:不可变宪法——候选只能重写白名单里的成员,风控底线与下单路径不可碰。

依据 docs/adr/20261111-risk-floor-immutability.md(风控底线永不优化)与
docs/adr/20261113-evolution-engine.md 阶段 2。清单只能追加,不能删减内置项。
"""
import dataclasses
import json
import textwrap
from pathlib import Path

import pytest

from risk import constitution as const

GOOD = textwrap.dedent('''
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    class Cand(TrendAtr15mStrategy):
        fast_window = 8
        atr_mult = 2.0
        parameters = TrendAtr15mStrategy.parameters + ["adx_window"]
        adx_window = 14
        param_grid = {"fast_window": [6, 8, 10], "slow_window": [30, 40, 50], "atr_mult": [1.5, 2.0, 2.5]}

        def _helper(self):
            return 1

        def entry_signal(self) -> int:
            return 1 if self._helper() else 0
''')


def screen(src, c=None):
    return const.screen_source(src, c or const.default())


# ---------- 静态筛查 ----------

def test_a_well_behaved_candidate_passes():
    assert screen(GOOD) == []


def test_new_members_are_allowed_by_the_static_layer():
    """静态层不判断'是否重写了模板成员'(模板成员名单要到运行时才知道);新指标由运行时那层管。"""
    src = "class C:\n    adx_window = 14\n    def helper(self):\n        return 1\n"
    assert screen(src) == []


@pytest.mark.parametrize("line", [
    "risk_budget = 99999.0",
    "max_daily_losses = 99",
    "atr_window = 2",
    "history_vt_symbol = 'X'",
    "guard = None",
    "trail = None",
    "risk_budget: float = 1.0",
])
def test_overriding_protected_class_attributes_is_rejected(line):
    findings = screen(f"class C:\n    {line}\n")
    assert findings and any("受保护" in f for f in findings), line


@pytest.mark.parametrize("name", [
    "__init__", "on_bar", "on_tick", "on_init", "on_trade", "on_order", "on_stop_order",
    "_try_enter", "_manage_position", "_send_entry", "_place_protective_stop",
    "_place_pending_entry", "_stop_order_price",
])
def test_overriding_protected_methods_is_rejected(name):
    findings = screen(f"class C:\n    def {name}(self, *a):\n        pass\n")
    assert any(name in f for f in findings), name


def test_async_def_is_also_caught():
    assert screen("class C:\n    async def on_bar(self, bar):\n        pass\n")


@pytest.mark.parametrize("stmt", [
    "self.risk_budget = 1e9",
    "self.max_daily_losses += 5",
    "self.guard = object()",
    "self.trail = None",
    "self.risk_budget: float = 1.0",
    "del self.guard",
])
def test_assigning_protected_attributes_on_instances_is_rejected(stmt):
    findings = screen(f"class C:\n    def entry_signal(self):\n        {stmt}\n        return 0\n")
    assert any("受保护" in f for f in findings), stmt


def test_monkeypatching_a_risk_helper_through_a_module_is_rejected():
    src = "import strategies.trend_atr_strategy as m\nm.within_budget = lambda *a: True\n"
    assert any("within_budget" in f for f in screen(src))


@pytest.mark.parametrize("call", ["buy", "sell", "short", "cover", "send_order", "send_stop_order", "cancel_order", "cancel_all"])
def test_order_calls_are_rejected_in_candidate_code(call):
    findings = screen(f"class C:\n    def entry_signal(self):\n        self.{call}(1, 1)\n        return 0\n")
    assert any(call in f and "下单" in f for f in findings), call


def test_param_grid_literal_must_not_name_protected_parameters():
    """param_grid 里出现受保护参数必须被拦。

    这条检查的实际范围是**任何字典字面量**(不只是 param_grid)——受保护参数出现在哪个字典里
    都是把底线写进候选自己的配置。提示语因此写"字典字面量"而不是"param_grid":
    对着一个与 param_grid 毫无关系的字典说"param_grid 含受保护参数",会把改候选的人带偏。"""
    src = 'class C:\n    param_grid = {"risk_budget": [100, 200, 300], "fast_window": [1, 2, 3], "atr_mult": [1, 2, 3]}\n'
    assert any("risk_budget" in f and "字典字面量" in f for f in screen(src))


def test_protected_parameter_in_any_dict_literal_is_rejected():
    """受保护参数出现在**任何**字典字面量里都要被拦,不限于 param_grid。"""
    src = 'class C:\n    def entry_signal(self):\n        d = {"risk_budget": 1}\n        return 0\n'
    assert any("risk_budget" in f for f in screen(src))


# ---------- 框架的"按名 setter":update_setting ----------

def test_by_name_setter_call_is_rejected():
    """`self.update_setting(dict(risk_budget=1e12))` 必须被静态层拦下。

    vnpy 的 `CtaTemplate.update_setting` 会把字典里的键**按键名** setattr 到实例上,
    而模板的 `parameters` 列表里正好有四个受保护参数(risk_budget / max_daily_losses /
    atr_window / history_vt_symbol)。第 5 轮外部复核实测:改之前三层防线全绿,
    `self.risk_budget` 由 300.0 变成 1e12,`within_budget` 因此返回 True——
    单笔风险预算被彻底改掉。
    名字在源码里既不是属性赋值也不是字典键(是 ast.keyword.arg),按语法位置筛查看不见它,
    所以必须显式拦"按名 setter"这一类入口。"""
    src = ("class C:\n"
           "    def entry_signal(self):\n"
           "        self.update_setting(dict(risk_budget=1e12))\n"
           "        return 0\n")
    findings = screen(src)
    assert any("update_setting" in f and "按名 setter" in f for f in findings), findings


def test_by_name_setter_alias_is_rejected():
    """取别名再调用同样要拦:`f = self.update_setting` 之后调用点看不出是谁。"""
    src = ("class C:\n"
           "    def entry_signal(self):\n"
           "        f = self.update_setting\n"
           "        f({})\n"
           "        return 0\n")
    findings = screen(src)
    assert any("update_setting" in f for f in findings), findings


def test_candidate_may_not_define_a_by_name_setter():
    """候选自己定义 update_setting 也要拦:引擎初始化时会调用它。"""
    src = ("class C:\n"
           "    def update_setting(self, setting):\n"
           "        self.risk_budget = 1e12\n")
    findings = screen(src)
    assert any("update_setting" in f and "按名 setter" in f for f in findings), findings


def test_protected_parameter_as_keyword_argument_is_rejected():
    """受保护参数作为**关键字参数**出现也要拦:`dict(risk_budget=…)` 里它是 ast.keyword.arg。"""
    src = ("class C:\n"
           "    def entry_signal(self):\n"
           "        d = dict(risk_budget=1e12)\n"
           "        return 0\n")
    findings = screen(src)
    assert any("risk_budget" in f and "关键字参数" in f for f in findings), findings


def test_protected_parameter_keyword_is_case_insensitive():
    """大小写不敏感,与 check_params 同一把尺子(Risk_Budget 与 risk_budget 是两个属性)。"""
    src = ("class C:\n"
           "    def entry_signal(self):\n"
           "        d = dict(Risk_Budget=1e12)\n"
           "        return 0\n")
    findings = screen(src)
    assert any("Risk_Budget" in f for f in findings), findings


def test_syntax_errors_fail_closed():
    """解析不过就等于没筛查,返回空列表会让"没查"看起来像"查过没问题"。"""
    findings = screen("class C(:\n")
    assert findings and any("语法错误" in f for f in findings), findings


# ---------- 清单文件:只能追加 ----------

def _write(tmp_path, payload):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


BASE = {"version": 1, "template": "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy",
        "allowed_overrides": [], "protected_names": [], "protected_parameters": [],
        "forbidden_order_calls": [], "protected_module_attrs": [],
        "protected_entry_points": [], "protected_state_objects": [], "protected_paths": []}


def test_json_can_extend_but_never_shrink_the_builtin_lists(tmp_path):
    c = const.load(_write(tmp_path, BASE | {"protected_names": ["my_extra"], "forbidden_order_calls": ["my_call"],
                                            "protected_module_attrs": ["my_fn"]}))

    assert {"risk_budget", "max_daily_losses", "_try_enter", "my_extra"} <= c.protected_names
    assert {"buy", "short", "my_call"} <= c.forbidden_order_calls
    assert {"within_budget", "my_fn"} <= c.protected_module_attrs
    assert "entry_signal" not in c.protected_names


def test_allowed_overrides_cannot_whitelist_a_builtin_protected_name(tmp_path):
    """试图把风控底线加进白名单一律拒绝(fail-closed):静默过滤会掩盖配置错误。"""
    with pytest.raises(const.ConstitutionError):
        const.load(_write(tmp_path, BASE | {"allowed_overrides": ["risk_budget", "entry_signal"]}))


@pytest.mark.parametrize("payload", [
    "not json", "[]",
    dict(BASE, version=2),
    {"version": 1, "template": "x:Y"},
    dict(BASE, template="nocolon"),
    dict(BASE, allowed_overrides="entry_signal"),
    dict(BASE, allowed_overrides=[1]),
    dict(BASE, protected_names=[1]),
    dict(BASE, protected_module_attrs=[1]),
    dict(BASE, template="strategies.trend_atr_15m_strategy:TrendAtr15mStrategy:extra"),
])
def test_invalid_constitution_file_fails_closed(tmp_path, payload):
    path = tmp_path / "c.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    with pytest.raises(const.ConstitutionError):
        const.load(path)


def test_missing_constitution_file_fails_closed(tmp_path):
    with pytest.raises(const.ConstitutionError):
        const.load(tmp_path / "nope.json")


def test_shipped_constitution_file_loads():
    c = const.load()
    assert c.template == "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy"
    assert {"entry_signal", "param_grid", "parameters"} <= c.allowed_overrides
    assert {"risk_budget", "max_daily_losses"} <= c.protected_parameters


# ---------- 请求参数 ----------

def test_params_must_not_set_protected_parameters():
    """设置字典能直接改策略参数:risk_budget 一旦被请求方指定,风控底线就被绕开了。"""
    c = const.default()
    assert const.check_params({"fast_window": 10, "atr_mult": 1.5}, c) == []
    bad = const.check_params({"fast_window": 10, "risk_budget": 1e9, "max_daily_losses": 99}, c)
    assert len(bad) == 2 and all("受保护" in f for f in bad)


def test_params_reject_non_string_keys():
    assert const.check_params({1: 2}, const.default())


# ---------- 运行时:相对模板的实际成员判定 ----------

class Template:
    risk_budget = 300.0
    fast_window = 10
    parameters = ["fast_window", "risk_budget"]

    def entry_signal(self):
        return 0

    def _try_enter(self):
        return "template"


def runtime(cls, template=Template):
    return const.check_class(cls, template, const.default())


def test_runtime_allows_whitelisted_overrides_and_new_names():
    class Ok(Template):
        fast_window = 8
        parameters = ["fast_window", "risk_budget", "adx"]
        adx = 14
        param_grid = {}

        def entry_signal(self):
            return 1

        def helper(self):
            return 2

    assert runtime(Ok) == []


def test_runtime_rejects_overriding_a_protected_member():
    class Bad(Template):
        risk_budget = 1e9

    assert any("risk_budget" in f for f in runtime(Bad))


def test_runtime_rejects_defining_a_by_name_setter():
    """候选不得自己定义按名 setter。

    引擎初始化时会调用 `update_setting`,候选在这里改写底线,静态层之外的路径也会中招。
    这条不依赖"模板恰好有同名成员"——即使模板没有 update_setting,候选也不许定义它。"""
    class Bad(Template):
        def update_setting(self, setting):
            self.risk_budget = 1e12

    assert any("update_setting" in f for f in runtime(Bad))


def test_runtime_rejects_overriding_a_method_even_through_an_intermediate_class():
    class Mid(Template):
        def _try_enter(self):
            return "mid"

    class Bad(Mid):
        pass

    assert any("_try_enter" in f for f in runtime(Bad))


def test_runtime_rejects_classes_that_do_not_derive_from_the_template():
    class Alien:
        def entry_signal(self):
            return 1

    assert any("模板" in f for f in runtime(Alien))


def test_runtime_rejects_a_member_injected_after_class_creation():
    class Sneaky(Template):
        pass

    Sneaky._try_enter = lambda self: "patched"        # 类创建后的补丁:类体 AST 看不到,运行时必须发现
    assert any("_try_enter" in f for f in runtime(Sneaky))


def test_runtime_rejects_overriding_a_template_member_that_is_not_whitelisted():
    class Tmpl2(Template):
        def brand_new_template_member(self):
            return 1

    class Bad(Tmpl2):
        def brand_new_template_member(self):
            return 2

    assert const.check_class(Bad, Tmpl2, const.default())


def test_runtime_rejects_overriding_an_inherited_engine_callback():
    """vnpy 回调(on_order/on_trade 等)被重写会绕过连亏计数与订单记账。"""
    class EngineBase:
        def on_trade(self, trade):
            return None

    class Tmpl(EngineBase, Template):
        pass

    class Bad(Tmpl):
        def on_trade(self, trade):
            return None

    assert any("on_trade" in f for f in const.check_class(Bad, Tmpl, const.default()))


# ---------- 模板载入 ----------

def test_load_template_imports_the_dotted_path(tmp_path, monkeypatch):
    (tmp_path / "tmpl_mod.py").write_text("class T:\n    pass\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    c = dataclasses.replace(const.default(), template="tmpl_mod:T")
    assert const.load_template(c).__name__ == "T"


def test_load_template_fails_closed_when_the_template_cannot_be_imported():
    c = dataclasses.replace(const.default(), template="no_such_module_zzz:T")
    with pytest.raises(const.ConstitutionError):
        const.load_template(c)


def test_load_template_fails_closed_when_the_attribute_is_missing(tmp_path, monkeypatch):
    (tmp_path / "tmpl_mod2.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    c = dataclasses.replace(const.default(), template="tmpl_mod2:Missing")
    with pytest.raises(const.ConstitutionError):
        const.load_template(c)


def test_a_daily_family_candidate_is_accepted_alongside_the_15m_one():
    """研究与沙盒也跑日线族:只认 15 分钟模板会把日线候选误拒。"""
    class Daily:
        risk_budget = 300.0
        parameters = ["fast_window", "risk_budget"]

        def entry_signal(self):
            return 0

    class Intraday(Daily):
        def on_bar(self, bar):
            return None

    class Candidate(Daily):
        fast_window = 8

        def entry_signal(self):
            return 1

    assert const.check_class(Candidate, Intraday, const.default()) == []


def test_family_root_is_the_first_non_vnpy_ancestor():
    class Base:
        pass

    class Mid(Base):
        pass

    class Leaf(Mid):
        pass

    assert const.family_root(Leaf) is Base


# ---------- 覆盖补齐 ----------

def test_protected_parameters_may_not_also_be_allowed_overrides(tmp_path):
    with pytest.raises(const.ConstitutionError):
        const.load(_write(tmp_path, BASE | {"protected_parameters": ["entry_signal"]}))


def test_module_level_assignment_to_a_protected_name_is_rejected():
    assert any("受保护成员" in f for f in screen("risk_budget = 1.0\n"))


def test_deleting_a_protected_member_is_rejected():
    assert any("受保护" in f for f in screen("class C:\n    def entry_signal(self):\n        del self.guard\n"))


def test_forbidden_order_call_by_bare_name_is_rejected():
    assert any("下单" in f for f in screen("class C:\n    def entry_signal(self):\n        buy(1, 1)\n        return 0\n"))


def test_check_params_rejects_non_dict():
    assert const.check_params([1], const.default()) == ["params 必须是 JSON 对象"]


def test_grid_forbidden_axes_come_from_the_constitution_not_a_second_copy():
    """两处各写一份会漂移:内置清单加了风控参数而网格这处没同步,就静默少拦了。"""
    import research_grid
    from risk.constitution import default

    assert research_grid.FORBIDDEN_AXES == set(default().protected_parameters)


# ============================================================================
# 回归:codex 第 2 轮发现的绕过路径
# ============================================================================

# ---- 缺陷 1:真实模板的 MRO 含 abc.ABC,family_root 判断错误 ----
# 真实 MRO:TrendAtr15mStrategy -> TrendAtrStrategy -> CtaTemplate -> ABC -> object
# 旧实现按"祖先名字不是 CtaTemplate/Template 且模块不以 vnpy 开头"过滤,
# 于是 abc.ABC 被当成了家族根,模板自身的成员被误判为"候选重写"。

def test_family_root_of_the_real_template_is_the_application_base_not_abc():
    """家族根应当是 TrendAtrStrategy(应用层基类),不是 abc.ABC。"""
    pytest.importorskip("vnpy.trader.setting")     # 这三个用例要真实模板,没有 vnpy 的环境跳过
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    root = const.family_root(TrendAtr15mStrategy)
    assert root.__name__ == "TrendAtrStrategy", f"家族根算成了 {root.__name__}"


def test_a_plain_real_candidate_is_not_rejected_for_metaclass_generated_members():
    """空候选类不该因为 ABCMeta 自动生成的 _abc_impl 被拒。"""
    pytest.importorskip("vnpy.trader.setting")
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    c = const.default()
    findings = const.check_class(TrendAtr15mStrategy, TrendAtr15mStrategy, c)
    assert findings == [], findings


def test_a_real_daily_family_candidate_is_accepted():
    """日线族候选(只继承 TrendAtrStrategy)不该被当成重写模板成员。"""
    pytest.importorskip("vnpy.trader.setting")
    from strategies.trend_atr_strategy import TrendAtrStrategy
    c = const.default()

    class Cand(TrendAtrStrategy):
        def entry_signal(self) -> int:
            return 1

    findings = const.check_class(Cand, TrendAtrStrategy, c)
    assert findings == [], findings


# ---- 缺陷 2:__setattr__ / type.__setattr__ 绕过静态筛查与运行时校验 ----

@pytest.mark.parametrize("snippet", [
    'object.__setattr__(self, "risk_budget", 10**12)',
    'type.__setattr__(self, "risk_budget", 10**12)',
    'super(type(self), self).__setattr__("risk_budget", 10**12)',
])
def test_dunder_attribute_mutation_is_rejected(snippet):
    """object/type.__setattr__ 能直接改掉实例上的风控底线,静态层必须拦。"""
    src = f"class C:\n    def entry_signal(self):\n        {snippet}\n        return 0\n"
    findings = screen(src)
    assert findings, f"未拦下: {snippet}"


@pytest.mark.parametrize("snippet", [
    "def __setattr__(self, name, value):\n        return object.__setattr__(self, name, value)",
    "def __delattr__(self, name):\n        return object.__delattr__(self, name)",
    "def __getattribute__(self, name):\n        return object.__getattribute__(self, name)",
])
def test_defining_mutation_dunders_is_rejected(snippet):
    """候选自定义 __setattr__ 等,可把任意请求参数重定向到受保护属性。"""
    src = f"class C:\n    {snippet}\n"
    findings = screen(src)
    assert findings, snippet


@pytest.mark.parametrize("snippet", [
    "f = object.__setattr__\nf(self, 'risk_budget', 10**12)",
    "f = type.__setattr__\nf(self, 'risk_budget', 10**12)",
    "f = object.__delattr__\nf(self, 'risk_budget')",
])
def test_aliasing_a_mutation_dunder_is_rejected(snippet):
    """先取别名再调用同样能改写属性,而调用点已经看不出是谁。

    只拦"直接调用 object.__setattr__(...)"是不够的:`f = object.__setattr__` 之后
    `f(self, "risk_budget", 1e12)` 的调用点是裸名,静态层看不出被调用的是什么。
    因此受禁 dunder 的**任何引用**都要拦。"""
    src = f"class C:\n    def entry_signal(self):\n        {snippet}\n        return 0\n"
    findings = screen(src)
    assert findings, f"未拦下: {snippet}"


def test_runtime_rejects_a_candidate_defining_setattr():
    """静态层可能漏(动态构造),运行时层还要拦一道。"""

    class Tmpl:
        risk_budget = 300.0

        def entry_signal(self):
            return 0

    class Bad(Tmpl):
        def __setattr__(self, name, value):
            return object.__setattr__(self, name, value)

    findings = const.check_class(Bad, Tmpl, const.default())
    assert any("__setattr__" in f for f in findings), findings


# ---- 缺陷 3:参数名大小写绕过 ----

def test_params_reject_case_variants_of_protected_parameters():
    """Risk_Budget / RISK_BUDGET 能绕过精确匹配,再配合自定义 __setattr__ 改写底线。"""
    c = const.default()
    for key in ("Risk_Budget", "RISK_BUDGET", "risk_budget"):
        assert any("受保护" in f for f in const.check_params({key: 1.0}, c)), key


# ---- 缺陷 4:清单的 template 可被改成任意可导入类 ----

def test_constitution_file_cannot_replace_the_template(tmp_path):
    """template 换成别的类会显著放松继承约束;模板只能在内置白名单里选。"""
    payload = BASE | {"template": "vnpy_ctastrategy.template:CtaTemplate"}
    with pytest.raises(const.ConstitutionError):
        const.load(_write(tmp_path, payload))


def test_constitution_file_cannot_swap_allowed_and_protected(tmp_path):
    """同一名字同时进白名单与受保护清单时必须报错,而不是 allow 静默胜出。"""
    with pytest.raises(const.ConstitutionError):
        const.load(_write(tmp_path, BASE | {"allowed_overrides": ["risk_budget"]}))
    with pytest.raises(const.ConstitutionError):
        const.load(_write(tmp_path, BASE | {"protected_names": ["entry_signal"]}))


# ---------- "只能追加"的历史守卫 ----------

# 守卫本体在 tools/check_constitution_history.py:只用标准库 + git。
# CI 跑的是那个**脚本**而不是 pytest——pytest 的 conftest 会 import segment_ledger →
# bootstrap → vnpy,在只装 pytest 的 CI 上根本收集不起来(第 6 轮 codex 实测:退出码 4)。
# 守卫跑不起来 = 没人执行 = 这份宪法的保护承诺是空的。
from tools.check_constitution_history import (  # noqa: E402
    BUILTIN_REGISTRY,
    BUILTIN_SETS,
    HistoryUnavailable,
    check_all,
    check_constitution_file,
    check_registry_completeness,
    const_string_set,
    removed_protections,
)

ROOT = Path(const.CONSTITUTION_FILE.parent.parent)


def test_constitution_history_is_append_only():
    """清单的历史追加项不得被删除:load() 只与代码里的内置集合合并,察觉不到文件里的删除。

    "只能追加"必须靠版本控制来保证——每次提交后的清单,相对上一次提交只能增不能减。

    浅克隆(git clone --depth=1 / actions/checkout 的 fetch-depth: 1)下 `git log` 只能看到
    一个提交,检查会"看起来通过"实则根本没比对——那种情况必须报错而不是跳过:
    查不了等于没查,不能让它冒充通过。CI 需要完整历史(fetch-depth: 0)。"""
    try:
        problems = check_all(ROOT)
    except HistoryUnavailable as exc:
        pytest.fail(f"无法检查宪法历史(fail-closed): {exc}")
    assert not problems, "宪法历史守卫发现问题:" + chr(10) + chr(10).join("  " + x for x in problems)


def test_history_guard_would_notice_a_shrunk_protection():
    """这条检查自己必须能发现"内置保护被删"——否则它自己失效时没有任何测试会报警。

    早先的提取器只认 tuple/list,而 `BUILTIN_PROTECTED_NAMES` 的定义是 `tuple + tuple`,
    于是这个最大的一组受保护成员从来没进过比对:删掉 `on_disconnect` 之后
    Constitution Guard 依然全绿(第 5 轮 codex 实测)。这个用例是那道 mutation 的守卫。"""
    source = (ROOT / "risk" / "constitution.py").read_text(encoding="utf-8")
    assert '"on_disconnect"' in source
    shrunk = source.replace('"on_disconnect",', "", 1)
    assert shrunk != source, "源码里找不到 on_disconnect,用例本身失效了"
    removed = removed_protections(shrunk, source, BUILTIN_SETS)
    assert "on_disconnect" in removed.get("BUILTIN_PROTECTED_NAMES", set()), removed


def test_registry_covers_every_registered_constant():
    """注册表里的每个常量都必须能被提取器解析——有一个解析不了,那条保护就是没被守着。"""
    for rel, names in BUILTIN_REGISTRY:
        source = (ROOT / rel).read_text(encoding="utf-8")
        parsed = const_string_set(source, names)
        for name in names:
            assert name in parsed, f"{rel} 的 {name} 解析不出字符串集合(注册表与源码不一致)"


def test_registry_covers_every_string_set_constant():
    """源码里新增一个字符串集合型的内置保护常量时,必须同时登记进历史守卫。

    漏登记 = 那个常量的历史守卫是空的,而它自己不会报警:
    当年 `BUILTIN_PROTECTED_NAMES` 正是不该漏的那一组,却因为提取器不认 BinOp 从未被比对过。"""
    problems = check_registry_completeness(ROOT)
    assert not problems, chr(10).join(problems)


def test_adding_an_unregistered_protection_constant_is_noticed():
    """守卫的完整性检查自己必须能发现"新增了没登记的常量"。"""
    source = (ROOT / "risk" / "constitution.py").read_text(encoding="utf-8")
    added = source + chr(10) + 'BUILTIN_MADE_UP_PROTECTION = ("x", "y")' + chr(10)
    parsed = const_string_set(added, ("BUILTIN_MADE_UP_PROTECTION",))
    assert "BUILTIN_MADE_UP_PROTECTION" in parsed, "提取器认不出新常量,用例本身失效了"


def test_a_manifest_may_not_point_at_a_template_outside_the_whitelist(tmp_path):
    """清单的 template 必须落在**那一版**的内置白名单里。

    只改清单里的 template 就等于绕开了白名单——白名单才是真正限制"能换成哪个模板"的东西。
    第 7 轮独立复核指出早先的写法("当前白名单 vs 旧白名单不相等才报")只在白名单从未增长过时成立:
    白名单一旦在历史上涨过,那条检查就永远不报(隔离克隆里先给白名单加一项、再改清单的 template,
    守卫退出码 0)。现在按逐个版本检查,不再依赖历史的形状。"""
    import json
    import subprocess

    repo = tmp_path / "repo"
    (repo / "config").mkdir(parents=True)
    (repo / "risk").mkdir()
    manifest = repo / "config" / "immutable_constitution.json"
    source = repo / "risk" / "constitution.py"

    def git(*args):
        return subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                               *args], cwd=str(repo), capture_output=True, encoding="utf-8", errors="replace")

    def commit(template: str, allowed: tuple[str, ...], message: str = "manifest"):
        manifest.write_text(json.dumps({
            "version": 1, "template": template, "allowed_overrides": [], "protected_names": [],
            "protected_parameters": [], "forbidden_order_calls": [], "protected_module_attrs": [],
        }), encoding="utf-8")
        source.write_text("BUILTIN_ALLOWED_TEMPLATES = " + repr(allowed) + chr(10), encoding="utf-8")
        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", message)

    commit("pkg:A", ("pkg:A", "pkg:B"))
    commit("pkg:B", ("pkg:A", "pkg:B"))            # 换成白名单里的另一个模板 → 合法
    assert not check_constitution_file(repo), "白名单内的模板切换被误报了"

    # 白名单先涨过一次(历史上真实存在过的形状),再把清单指向白名单之外的模板
    commit("pkg:B", ("pkg:A", "pkg:B", "pkg:C"), "widen the whitelist")
    commit("pkg:X", ("pkg:A", "pkg:B", "pkg:C"), "point outside the whitelist")
    problems = check_constitution_file(repo)
    assert problems, "清单指向白名单之外的模板,守卫却没有报出来"
    assert any("template" in p for p in problems), problems


def test_the_ci_workflow_runs_the_guard_without_installing_the_trading_stack():
    """CI 必须跑那个独立脚本,而不是 pytest。

    pytest 的 conftest 会 import vnpy,只装 pytest 的 CI 上会直接收集失败(退出码 4)——
    守卫于是在 CI 上一次都没跑过,却看起来"配好了"(第 6 轮 codex 实测)。"""
    workflow = (ROOT / ".github" / "workflows" / "constitution-guard.yml").read_text(encoding="utf-8")
    assert "check_constitution_history.py" in workflow, "CI 没有调用独立守卫脚本"
    assert "fetch-depth: 0" in workflow, "CI 必须拉完整历史,否则比对无从谈起"
    installs = [ln.strip() for ln in workflow.splitlines()
                if ln.strip().startswith("run:") and "pip install" in ln]
    assert not installs, f"宪法层不该安装交易环境依赖: {installs}"


# ---------- 别名绕过:下单接口与 dunder 同样拦引用 ----------

@pytest.mark.parametrize("snippet", [
    "f = self.buy\nf(1, 1)",
    "f = self.cancel_all\nf()",
    "g = buy\ng(1, 1)",
])
def test_aliasing_an_order_call_is_rejected(snippet):
    """下单接口的引用也要拦:`f = self.buy` 之后 `f(1, 1)` 的调用点看不出是下单。"""
    src = f"class C:\n    def entry_signal(self):\n        {snippet}\n        return 0\n"
    findings = screen(src)
    assert findings, f"未拦下: {snippet}"


def test_the_false_positive_on_ordinary_identifiers_is_intentional():
    """局部变量/参数恰好叫 `cover`/`short`/`buy` 会被拒——这是有意的,不是 AST 检查的缺陷。

    静态层按名字拦,不做作用域解析,所以 `cover = True` 这种普通写法也会命中下单接口名单。
    这是 fail-closed 的取舍:宁可误伤让人来看,不可放过一个真绕过(第 5 轮两轮复核都实测确认)。
    这个用例把"有意为之"钉住——以后有人看到误报就放松规则时,会先撞到这里。
    正确的放宽方式是做作用域解析,不是删掉这条规则。"""
    src = "class C:\n    def entry_signal(self):\n        cover = True\n        return 1 if cover else 0\n"
    findings = screen(src)
    assert any("cover" in f for f in findings), findings


# ---------- 第 6 轮 codex 的 L3/L4/L5:静态层的三处口径不一致 ----------

def test_a_dict_literal_with_a_differently_cased_protected_key_is_rejected():
    """字典键与参数层同一把尺子(大小写不敏感)。

    精确比对会让 `{"Risk_Budget": 1}` 放行,而参数层的 `check_params` 早就按
    不敏感比对了——两处口径分裂迟早变成漏洞。"""
    findings = const.screen_source("d = {'Risk_Budget': 1}" + chr(10))
    assert findings, "大小写不同的受保护参数名必须被拦下"


def test_a_dict_literal_with_a_protected_member_name_is_rejected():
    """字典键也要查受保护**成员**,不只受保护参数——与 check_params 用的同一把尺子。"""
    findings = const.screen_source("d = {'on_disconnect': 1}" + chr(10))
    assert findings, "受保护成员名出现在字典字面量里必须被拦下"


def test_assigning_a_forbidden_dunder_in_a_class_body_is_rejected():
    """`class C: __setattr__ = print` 这种**赋值**形式的定义同样要拦。

    此前静态层只看 `def` 形式,赋值形式能溜过去(运行时层能抓到,但那时已经在跑候选了)。"""
    findings = const.screen_source("class C:" + chr(10) + "    __setattr__ = print" + chr(10))
    assert findings and any("__setattr__" in f for f in findings), findings


def test_assigning_a_by_name_setter_in_a_class_body_is_rejected():
    """`class C: update_setting = print` 同理:按名 setter 的赋值形式也要拦。"""
    findings = const.screen_source("class C:" + chr(10) + "    update_setting = print" + chr(10))
    assert findings and any("update_setting" in f for f in findings), findings


# ---------- 第 7 轮 codex 复核:C1/C2 与历史守卫的执行点 ----------

# 这一节的每条用例都对应一条**实测复现**过的绕过(真实 15 分钟玉米数据回测,
# 不是推测):
#   C1  候选直接调 self._send_entry(...) → 绕开 _try_enter 里的 within_budget
#       与 guard.can_open。atr_mult=50 时诚实模板成交 0 笔(157 次被预算拦下),
#       候选成交 2 笔、单笔止损距离 508.7(≈17 倍 300 元预算)。
#   C2  候选写 self.guard.max_losses / self.guard._losses / self.cta_engine.size
#       → 熔断器失效;后者把回撤从 -4272 元/-39.97% 缩成 -0 元/-0.00%,
#       promotion_gate 的 2000 元与 20% 两道门槛同时失效。
# 两条在当时都是**三层校验全绿**。

EVIL_CALLS_ENTRY = textwrap.dedent('''
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            signal = super().entry_signal()
            if signal:
                self._send_entry(signal, float(self.atr_value) * 50.0, float(self.am.close[-1]))
            return 0
''')

EVIL_TAMPERS_BREAKER = textwrap.dedent('''
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            self.guard.max_losses = 999
            self.guard._losses = 0
            return super().entry_signal()
''')

EVIL_TAMPERS_ENGINE = textwrap.dedent('''
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    class Evil(TrendAtr15mStrategy):
        def entry_signal(self) -> int:
            self.cta_engine.size = 0.001
            return super().entry_signal()
''')


@pytest.mark.parametrize("source", [EVIL_CALLS_ENTRY, EVIL_TAMPERS_BREAKER, EVIL_TAMPERS_ENGINE])
def test_round7_bypass_candidates_are_rejected_by_the_static_layer(source):
    """第 7 轮实测的三条绕过:静态层必须全部拦下(原来全是空的)。"""
    findings = screen(source)
    assert findings, "第 7 轮的绕过候选没有被静态层拦下:" + source


@pytest.mark.parametrize("method", [
    "_send_entry", "_try_enter", "_manage_position", "_place_protective_stop",
    "_place_pending_entry", "_process_daily_bar",
])
def test_referencing_a_protected_entry_point_is_rejected(method):
    """风控执行点连"引用"都不许——`f = self._send_entry` 之后 `f(...)` 的调用点看不出是谁。"""
    src = ("class C:\n    def entry_signal(self):\n"
           f"        f = self.{method}\n        return 0\n")
    findings = screen(src)
    assert findings and any("风控执行点" in f for f in findings), findings


@pytest.mark.parametrize("statement", [
    "self.guard.max_losses = 999",
    "self.guard._losses = 0",
    "self.cta_engine.size = 0.001",
    "self.trail.stop = 0.0",
    "self.am.close = [1.0]",
    "del self.guard.max_losses",
])
def test_writing_into_a_protected_object_is_rejected(statement):
    """写受保护对象的**内部状态**要拦——早先只看末段属性名,整条属性路径没人看。"""
    src = "class C:\n    def entry_signal(self):\n        " + statement + "\n        return 0\n"
    findings = screen(src)
    assert findings and any("受保护" in f for f in findings), f"{statement} 未被拦下:{findings}"


@pytest.mark.parametrize("statement", [
    "g = self.guard",
    "g = self.trail",
    "g = self.cta_engine",
    "arr = self.am.array",
])
def test_aliasing_a_protected_object_is_rejected(statement):
    """取别名也要拦:只禁"写"会被 `g = self.guard` 之后的 `g.max_losses = 999` 绕过去。"""
    src = "class C:\n    def entry_signal(self):\n        " + statement + "\n        return 0\n"
    findings = screen(src)
    assert findings, f"{statement} 未被拦下:{findings}"


def test_reading_a_protected_state_object_is_rejected():
    """熔断器与引擎连读都不许:候选读得到它们,就离改写它们只差一步。"""
    src = "class C:\n    def entry_signal(self):\n        n = self.guard._losses\n        return 0\n"
    findings = screen(src)
    assert findings and any("风控状态对象" in f for f in findings), findings


def test_a_candidate_may_still_keep_its_own_nested_state():
    """别把候选自己的状态也一起拦了:风控底线之外的东西必须还能写。

    这条是上面几条的**反面**用例:静态层拦的是"受保护的对象",
    不是"所有带点号的赋值"。"""
    src = ("class C:\n    def entry_signal(self):\n"
           "        self.my_state.counter = self.my_state.counter + 1\n        return 0\n")
    assert screen(src) == []


def test_reading_the_market_data_is_still_allowed():
    """候选的正当用途不能被误伤:读 `self.am.close[-1]` 是它存在的意义。"""
    src = ("class C:\n    def entry_signal(self):\n"
           "        close = self.am.close[-1]\n        return 1 if close > 0 else 0\n")
    assert screen(src) == []


# ---------- 第 8 轮自查发现的两个缺口(别名改写 + 行情数组被原地改写) ----------

@pytest.mark.parametrize("statement", [
    "s = self\n        s.risk_budget = 1e12",          # 给 self 取别名之后改底线
    "o = object()\n        o.risk_budget = 1e12",      # 根不是 self 也要拦
    "s = self\n        s.am.close = [1.0]",
])
def test_writing_a_protected_member_is_rejected_whatever_the_root_is(statement):
    """写受保护成员不看根:只认 `self.` 开头的话,`s = self` 之后就绕过去了。

    早先的实现只按 `self.` 开头的路径判定(第 8 轮自查实测:三种写法静态层全返回空列表),
    现在改成"属性路径里任何一段命中受保护成员就算越权"。"""
    src = "class C:\n    def entry_signal(self):\n        " + statement + "\n        return 0\n"
    findings = screen(src)
    assert findings and any("受保护" in f for f in findings), f"{statement!r} 未被拦下:{findings}"


@pytest.mark.parametrize("statement", [
    "self.am.close[0] = 999.0",     # 原地改写行情数组(下标赋值)
    "self.am.close_array[-1] = 999.0",
])
def test_writing_into_the_price_array_in_place_is_rejected(statement):
    """下标赋值改的是被取到的那个对象本身——行情数组被改 = 回测数据被伪造。"""
    src = "class C:\n    def entry_signal(self):\n        " + statement + "\n        return 0\n"
    findings = screen(src)
    assert findings and any("受保护" in f for f in findings), f"{statement!r} 未被拦下:{findings}"


def test_binding_the_price_array_to_another_name_is_allowed_now():
    """**第 4 轮 G8 复核的 F2(更正本用例原先的断言)**。

    原先的断言是"`arr = self.am.close` 之后 `arr[0] = 999` 一律拒",理由是
    vn.py 的 `ArrayManager.close` 是 property、返回的就是 `close_array` 本身(不是副本)。
    只读行情视图落地之后这个前提不成立了:候选在 `entry_signal` 里读到的 `self.am.close`
    是**拷贝**,别名改不到回测数据(端到端用例 `tests/test_market_view.py::
    test_writing_every_array_the_view_gives_changes_nothing` 逐笔验证过)。

    而这条规则原先把仓库**自己的 LLM 提示词**写法
    (`self.am.close[-2:]`、`np.mean(self.am.close)`、`self.am.count`、`self.am.close_array`)
    整片拒掉,进化循环因此被闲置。静态层随之**收窄到"写入"**:
    写入的形态仍拒(见 `test_writing_into_the_price_array_in_place_is_rejected`
    与 `tests/test_g8_round4.py` 的写入清单)。"""
    src = ("class C:\n    def entry_signal(self):\n"
           "        arr = self.am.close\n        arr[0] = 999.0\n        return 0\n")
    findings = screen(src)
    assert findings == [], f"读取被误伤: {findings}"


def test_writing_through_a_view_of_the_price_array_is_rejected():
    """收窄到"写入"之后,这条**不能跟着漏掉**:
    `self.am.close.reshape(-1)[0] = 9.0` 是把容器上的**视图型结果**当写入目标——
    读是允许的(视图下是拷贝),写不是。"""
    src = ("class C:\n    def entry_signal(self):\n"
           "        self.am.close.reshape(-1)[0] = 9.0\n        return 0\n")
    findings = screen(src)
    assert findings and any("视图型结果" in f for f in findings), findings


def test_reading_a_single_price_is_still_allowed():
    """取别名这条不许太宽:带下标的读(取到的是一个数)与带调用的读都要放行。"""
    src = ("class C:\n    def entry_signal(self):\n"
           "        close = self.am.close[-1]\n"
           "        atr = self.am.atr(14)\n"
           "        return 1 if close > 0 else 0\n")
    assert screen(src) == []


# ---------- 运行时第三层:回测跑完之后核对受保护对象的实例状态 ----------

class _FakeGuard:
    def __init__(self, max_losses: int) -> None:
        self.max_losses = max_losses
        self._losses = 0


class _FakeEngine:
    def __init__(self, size: float) -> None:
        self.size = size


def _fake_strategy(**overrides):
    """造一个"模板家族的实例"但不跑 __init__:运行时第三层只对模板家族生效。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    strategy = TrendAtr15mStrategy.__new__(TrendAtr15mStrategy)
    strategy.risk_budget = 300.0
    strategy.atr_window = 14
    strategy.max_daily_losses = 2
    strategy.guard = _FakeGuard(2)
    strategy.cta_engine = _FakeEngine(10.0)
    for key, value in overrides.items():
        setattr(strategy, key, value)
    return strategy


def test_instance_state_check_passes_for_an_untouched_strategy():
    assert const.check_instance_state(_fake_strategy(), expected_size=10.0) == []


def test_instance_state_check_ignores_strategies_outside_the_template_family():
    """别的策略(随机入场对照等)有各自的参数,不该被拿模板的底线比。"""

    class Other:
        risk_budget = 999.0
        atr_window = 7
        max_daily_losses = 9
        guard = _FakeGuard(1)
        cta_engine = _FakeEngine(1.0)

    assert const.check_instance_state(Other(), expected_size=10.0) == []


def test_instance_state_check_catches_a_tampered_breaker():
    """熔断器阈值与策略用的阈值不一致 = 候选在回测途中改写了其中一个。"""
    strategy = _fake_strategy()
    strategy.guard.max_losses = 999
    findings = const.check_instance_state(strategy, expected_size=10.0)
    assert findings and any("熔断" in f for f in findings), findings


def test_instance_state_check_catches_a_tampered_contract_size():
    """合约乘数被改小 → 止损距离算出来的风险金额接近 0 → 两道回撤门槛同时失效。"""
    strategy = _fake_strategy()
    strategy.cta_engine.size = 0.001
    findings = const.check_instance_state(strategy, expected_size=10.0)
    assert findings and any("合约乘数" in f for f in findings), findings


def test_instance_state_check_catches_a_tampered_risk_budget():
    strategy = _fake_strategy()
    strategy.risk_budget = 1e12
    findings = const.check_instance_state(strategy, expected_size=10.0)
    assert findings and any("风险预算" in f for f in findings), findings


# ---------- 参数层:嵌套也要查(Medium 5) ----------

def test_check_params_rejects_a_protected_key_in_a_nested_dict():
    """嵌套里的受保护键同样要查:静态层对任何位置的字典字面量都查,口径必须一致。"""
    findings = const.check_params({"outer": {"risk_budget": 1}})
    assert findings, "嵌套的受保护参数未被拒绝"
    assert any("risk_budget" in f for f in findings), findings


def test_check_params_rejects_a_protected_key_in_a_nested_list():
    findings = const.check_params({"outer": [{"Risk_Budget": 1}]})
    assert findings, "嵌套列表里的受保护参数(大小写不同)未被拒绝"


def test_check_params_still_accepts_ordinary_nested_settings():
    assert const.check_params({"fast_window": [10, 20], "nested": {"slow_window": 40}}) == []


# ---------- 受禁 dunder 的补齐(Low 6)与 __slots__(Low 7) ----------

@pytest.mark.parametrize("dunder", [
    "__del__", "__reduce__", "__reduce_ex__", "__mro_entries__",
    "__dict__", "__class__", "__mro__", "__globals__", "__code__",
])
def test_reflecting_dunders_may_not_be_referenced(dunder):
    """反射型 dunder 能拿到"装着状态的那个容器",从那里绕回来改写状态。"""
    src = f"class C:\n    def entry_signal(self):\n        x = self.guard.{dunder}\n        return 0\n"
    findings = screen(src)
    assert findings and any(dunder in f for f in findings), findings


def test_defining_a_forbidden_dunder_is_rejected():
    for dunder in ("__del__", "__reduce__", "__reduce_ex__", "__mro_entries__"):
        findings = screen(f"class C:\n    def {dunder}(self):\n        pass\n")
        assert findings and any(dunder in f for f in findings), findings


def test_slots_hiding_a_protected_name_is_rejected():
    """`__slots__ = ("risk_budget",)` 早先只有运行时层拦得住,静态层也扫一遍。"""
    findings = screen("class C:\n    __slots__ = ('risk_budget',)\n")
    assert findings and any("__slots__" in f for f in findings), findings


# ---------- 历史守卫:守"执行点",白名单只减不增(High 3) ----------

def test_history_guard_notices_a_deleted_enforcement_site():
    """把 `if name in BUILTIN_FORBIDDEN_DUNDERS:` 整块删掉,常量集合没变,底线不再被执行。

    第 7 轮 codex 实测:这类改法下守卫退出码是 0(全绿)。守卫必须看住**执行点**。"""
    from tools.check_constitution_history import usage_regressions

    source = (ROOT / "risk" / "constitution.py").read_text(encoding="utf-8")
    assert "BUILTIN_FORBIDDEN_DUNDERS" in source
    # 改的是**执行点**(引用),常量定义本身留着——这正是"常量还在、底线不执行"的那种改法
    mutilated = source.replace("in BUILTIN_FORBIDDEN_DUNDERS", "in SOMETHING_ELSE")
    assert mutilated != source, "用例本身失效:源码里找不到 BUILTIN_FORBIDDEN_DUNDERS 的引用"
    regressions = usage_regressions(mutilated, source, ("BUILTIN_FORBIDDEN_DUNDERS",))
    assert "BUILTIN_FORBIDDEN_DUNDERS" in regressions, regressions


def test_history_guard_notices_a_removed_field_branch():
    """删掉 `c.forbidden_order_calls` 的执行分支:清单与常量都没变,下单黑名单却不再被执行。"""
    from tools.check_constitution_history import usage_regressions

    source = (ROOT / "risk" / "constitution.py").read_text(encoding="utf-8")
    mutilated = source.replace("c.forbidden_order_calls", "c.something_else", 1)
    assert mutilated != source, "用例本身失效:源码里找不到 c.forbidden_order_calls"
    regressions = usage_regressions(mutilated, source, ("forbidden_order_calls",))
    assert "forbidden_order_calls" in regressions, regressions


def test_history_guard_treats_the_override_whitelist_as_shrink_only():
    """白名单多一项 = 多放开一点底线,与保护清单"只能追加"的规则正好相反。"""
    from tools.check_constitution_history import shrink_only_additions

    assert shrink_only_additions({"entry_signal", "get_size"}, {"entry_signal"}) == {"get_size"}
    assert shrink_only_additions({"entry_signal"}, {"entry_signal", "get_size"}) == set()


def test_the_shipped_manifest_has_the_new_fields():
    """清单文件必须带上新字段:缺字段一律 fail-closed,不能让新规则静默失效。"""
    constitution = const.load()
    assert constitution.protected_entry_points
    assert constitution.protected_state_objects
    assert "_send_entry" in constitution.protected_entry_points
    assert "guard" in constitution.protected_state_objects


# ---------- 第 9 轮 codex:模块级 monkey-patch(Critical 1)、__new__ 早于快照(High 2) ----------

def test_patching_a_risk_guard_helper_is_rejected():
    """`rg.risk_per_trade = lambda ...: 0.0` 直接架空单笔风险预算。

    第 9 轮 codex 实测:这一行让 skipped_by_budget 从 326 变成 0、单笔止损算出 2901 元
    (预算的 9.7 倍),而静态层/沙盒层/check_class/check_instance_state **四层全部放行**。
    `risk_per_trade` 此前根本不在受保护的模块属性名单里——`within_budget` 只是把算术委托给它。"""
    findings = screen('''
import risk.risk_guard as rg

rg.risk_per_trade = lambda stop_dist, size, volume=1: 0.0
''')
    assert findings, "模块级 monkey-patch 没被静态层拦下"
    assert any("risk_per_trade" in f for f in findings)


def test_patching_the_loss_breaker_class_is_rejected():
    """`rg.DailyLossGuard.can_open = lambda self, day: True` 架空当日连亏熔断。

    末段属性名是 `can_open`,不在受保护名单里;**受保护的是路径中间的 `DailyLossGuard`**,
    所以必须按整条路径判,不能只看末段。"""
    findings = screen('''
import risk.risk_guard as rg

rg.DailyLossGuard.can_open = lambda self, day: True
''')
    assert findings, "改写熔断器类没被静态层拦下"
    assert any("DailyLossGuard" in f for f in findings)


def test_patching_a_risk_guard_module_is_rejected_however_it_is_spelled():
    """别名、深层路径都要拦:枚举写法封不住"能力",但能给出清楚的拒绝理由。"""
    for source in (
        '''
import risk.risk_guard as rg
rg.within_budget = lambda *a, **k: True
''',
        '''
import risk.risk_guard
risk.risk_guard.risk_per_trade = lambda *a, **k: 0.0
''',
        '''
from risk import risk_guard
risk_guard.TrailingStop.update = lambda *a, **k: 0.0
''',
    ):
        findings = screen(source)
        assert findings, source


def test_a_write_whose_root_is_a_call_is_rejected():
    """根不是可解析的名字时(`type(cls).risk_budget = 1e12`)也要按末段属性名判。

    属性路径解析遇到调用表达式就断了,于是整条路径判不到——第 9 轮 codex 与本次自查
    都实测到这一类放行。"""
    findings = screen('''
class Evil(TrendAtr15mStrategy):
    def entry_signal(self) -> int:
        type(self).risk_budget = 1e12
        return 0
''')
    assert findings, "根是调用表达式的写入没被拦下"
    assert any("risk_budget" in f for f in findings)


def test_defining_new_is_rejected():
    """`__new__` 在实例构造时执行,**早于**回测前的快照:它里面的改写会被当成"基线"。

    第 9 轮 codex 实测三层全放行。候选没有正当理由自定义 `__new__`,直接禁掉。"""
    findings = screen('''
class Evil(TrendAtr15mStrategy):
    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)
''')
    assert findings, "自定义 __new__ 没被拦下"
    assert any("__new__" in f for f in findings)


def test_referencing_new_is_rejected():
    """引用 `__new__` 同样要拦:定义被拦了,拿别名再调用还在。"""
    findings = screen('''
class Evil(TrendAtr15mStrategy):
    def entry_signal(self) -> int:
        obj = super().__new__(type(self))
        return 0
''')
    assert findings and any("__new__" in f for f in findings)


# ---------- 运行期第四层:受保护模块与模板类的"身份指纹" ----------

def test_the_protected_state_covers_the_risk_guard_and_the_template():
    """指纹必须覆盖风控模块**和**模板类:两者都是候选够得着的改写目标。"""
    snapshot = const.snapshot_protected_state()
    keys = " ".join(snapshot)
    assert "risk_per_trade" in keys
    assert "DailyLossGuard" in keys
    assert "get_size" in keys, "模板类的方法不在指纹里"


def test_an_untouched_protected_state_produces_no_findings():
    before = const.snapshot_protected_state()
    after = const.snapshot_protected_state()
    assert const.check_protected_state(before, after) == []


def test_the_protected_state_notices_a_patched_risk_helper():
    import risk.risk_guard as rg

    before = const.snapshot_protected_state()
    original = rg.risk_per_trade
    try:
        rg.risk_per_trade = lambda stop_dist, size, volume=1: 0.0
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "改写风控模块函数没被抓到"
        assert any("risk_per_trade" in f for f in findings)
    finally:
        rg.risk_per_trade = original


def test_the_protected_state_notices_a_patched_breaker_method():
    import risk.risk_guard as rg

    before = const.snapshot_protected_state()
    original = rg.DailyLossGuard.can_open
    try:
        rg.DailyLossGuard.can_open = lambda self, day: True
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "改写熔断方法没被抓到"
    finally:
        rg.DailyLossGuard.can_open = original


def test_the_protected_state_notices_a_patched_template_method():
    """模板类被改写同样致命:`get_size` 改一下,风险金额算出来就不一样了。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    before = const.snapshot_protected_state()
    original = TrendAtr15mStrategy.get_size
    try:
        TrendAtr15mStrategy.get_size = lambda self: 100
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "改写模板类方法没被抓到"
        assert any("get_size" in f for f in findings)
    finally:
        TrendAtr15mStrategy.get_size = original


def test_the_protected_state_notices_a_deleted_member():
    import risk.risk_guard as rg

    before = const.snapshot_protected_state()
    original = rg.within_budget
    try:
        del rg.within_budget
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "删掉风控函数没被抓到"
    finally:
        rg.within_budget = original


def test_the_protected_state_is_taken_before_the_candidate_imports_anything():
    """子进程里候选执行前就要把模板模块**预先导入**并拍快照。

    否则模板模块是候选自己 import 的,快照里根本没有它,候选在导入期把它改写了也抓不到。"""
    import subprocess
    import sys

    code = (
        "import sys;"
        "from risk import constitution as const;"
        "snap = const.snapshot_protected_state();"
        "print(any('get_size' in k for k in snap))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip().endswith("True"), out.stdout + out.stderr


# ---------- 第 10 轮 codex:函数对象的**状态**与实例上的**遮蔽**(两条 Critical) ----------

def test_the_static_layer_refuses_an_instance_shadow_of_get_size():
    """候选在**实例**上遮蔽 `get_size`,静态层必须拦。

    `self.get_size = lambda: 0.001` 把真实合约乘数 10 换成 0.001:
    `within_budget(dist, self.get_size(), self.risk_budget)` 算出来的风险金额是
    dist × 0.001,300 元预算等于 0.003 元,任何止损距离都"在预算内"。
    类字典里没有这个赋值(`check_class` 比不出来),实例字段快照也不盯它——
    四层防线全都放行(第 10 轮自查实测)。"""
    src = textwrap.dedent(
        """
        from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
        class S(TrendAtr15mStrategy):
            def entry_signal(self):
                self.get_size = lambda: 0.001
                return 1
        """)
    findings = const.screen_source(src)
    assert findings, "实例上遮蔽 get_size 没被拦"
    assert any("get_size" in f for f in findings), findings


def test_the_static_layer_refuses_tampering_with_a_functions_state():
    """改函数**对象的状态**(默认参数)也要拦。

    `within_budget.__defaults__ = (0.0,)` 把 volume 的默认值变成 0,
    单笔风险金额恒为 0——改的不是名字,是对象的状态,按名字的黑名单全都看不见。"""
    findings = const.screen_source("f.__defaults__ = (0.0,)\n")
    assert findings, "改写函数的默认参数没被拦"
    assert any("__defaults__" in f for f in findings), findings


def test_the_static_layer_refuses_reading_the_risk_functions():
    """候选没有正当理由拿到风控函数的**定义**:风控由基类固定,候选只写信号逻辑。

    拿到定义就多一条改写的路(改对象状态,连名字都不用碰)。"""
    findings = const.screen_source("from strategies.trend_atr_strategy import within_budget\n")
    assert findings, "导入风控函数没被拦"
    assert any("within_budget" in f for f in findings), findings


def test_the_static_layer_refuses_the_type_check_entry_points():
    """改掉 `__instancecheck__` 就能把 isinstance 判定废掉(运行时第三层的家族判定)。"""
    findings = const.screen_source("x.__instancecheck__\n")
    assert findings, "__instancecheck__ 没被拦"
    assert any("__instancecheck__" in f for f in findings), findings


def test_the_protected_state_notices_an_in_place_mutation_of_a_function_object():
    """指纹比的不只是**身份**,还有函数**对象的状态**。

    第 10 轮 codex 实测:只比身份时,`within_budget.__defaults__ = (0.0,)` 完全看不见,
    300 元单笔预算就此恒真,而四层防线全部放行。"""
    import risk.risk_guard as rg

    before = const.snapshot_protected_state()
    original = rg.within_budget.__defaults__
    try:
        rg.within_budget.__defaults__ = (0.0,)
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "就地改写函数对象的状态没被抓到"
        assert any("within_budget" in f for f in findings), findings
    finally:
        rg.within_budget.__defaults__ = original


def test_the_protected_state_notices_a_patched_class_dunder():
    """类的 dunder 也在指纹里:改写 `__getattribute__` 能让 `self.get_size()` 返回假的。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    before = const.snapshot_protected_state()
    original = TrendAtr15mStrategy.__getattribute__
    try:
        TrendAtr15mStrategy.__getattribute__ = lambda self, name: 0.001
        findings = const.check_protected_state(before, const.snapshot_protected_state())
        assert findings, "改写类的 __getattribute__ 没被抓到"
    finally:
        TrendAtr15mStrategy.__getattribute__ = original


def _bare_family_instance():
    """造一个**不跑 __init__** 的家族实例:只用来核对第三层的判定逻辑。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    return object.__new__(TrendAtrStrategy)


def test_the_instance_state_check_notices_a_shadowed_template_method():
    """实例上遮蔽模板方法(第三层):类字典比不出来的那一类改写。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    strategy = _bare_family_instance()
    findings = const.check_instance_state(strategy, template=TrendAtr15mStrategy)
    assert not findings, findings

    strategy.get_size = lambda: 0.001
    findings = const.check_instance_state(strategy, template=TrendAtr15mStrategy)
    assert findings, "实例上遮蔽模板方法没被抓到"
    assert any("get_size" in f for f in findings), findings


def test_the_family_check_does_not_depend_on_isinstance():
    """家族判定走 MRO(结构),不走 isinstance(判定逻辑可被改写)。

    第 10 轮 codex 实测:把 `abc.ABCMeta.__instancecheck__` 换掉之后,
    isinstance 一律返回 False,第三层直接判成"不适用"而放行。"""
    import abc

    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    strategy = _bare_family_instance()
    snapshot = const.snapshot_instance_state(strategy)          # 底线原值
    strategy.risk_budget = 10 ** 12                            # 途中被改写

    original = abc.ABCMeta.__instancecheck__
    try:
        abc.ABCMeta.__instancecheck__ = lambda cls, instance: False
        findings = const.check_instance_state(strategy, template=TrendAtr15mStrategy, snapshot=snapshot)
    finally:
        abc.ABCMeta.__instancecheck__ = original
    assert findings, "家族判定被改写之后第三层就不工作了"
    assert any("risk_budget" in f for f in findings), findings


def test_the_static_layer_does_not_flag_an_ordinary_reset():
    """`reset` 这类泛用名字不该按名字一刀切(第 10 轮 codex 的 Low 1)。

    `reset` 是 TrailingStop 的方法名,但候选代码里 `self.indicator.reset = 1` 太常见——
    按名字一刀切会把正常候选整片拒掉。带主的名字只在**主人也在路径里**时才算命中。"""
    findings = const.screen_source("class C:\n    def f(self):\n        self.indicator.reset = 1\n")
    assert not findings, findings
    assert not const.screen_source("make().reset = 1\n")


def test_the_static_layer_still_flags_the_risk_objects_own_reset():
    """带主的名字在主人出现时照样拦:`TrailingStop.reset` 被换掉等于移动止损失效。"""
    findings = const.screen_source("rg.TrailingStop.reset = lambda self: None\n")
    assert findings, "改写移动止损的 reset 没被拦"
