"""risk/constitution.py:不可变宪法——候选只能重写白名单里的成员,风控底线与下单路径不可碰。

依据 docs/adr/20261111-risk-floor-immutability.md(风控底线永不优化)与
docs/adr/20261113-evolution-engine.md 阶段 2。清单只能追加,不能删减内置项。
"""
import dataclasses
import json
import textwrap

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
        "forbidden_order_calls": [], "protected_module_attrs": []}


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
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    root = const.family_root(TrendAtr15mStrategy)
    assert root.__name__ == "TrendAtrStrategy", f"家族根算成了 {root.__name__}"


def test_a_plain_real_candidate_is_not_rejected_for_metaclass_generated_members():
    """空候选类不该因为 ABCMeta 自动生成的 _abc_impl 被拒。"""
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    c = const.default()
    findings = const.check_class(TrendAtr15mStrategy, TrendAtr15mStrategy, c)
    assert findings == [], findings


def test_a_real_daily_family_candidate_is_accepted():
    """日线族候选(只继承 TrendAtrStrategy)不该被当成重写模板成员。"""
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


def test_constitution_history_is_append_only():
    """清单的历史追加项不得被删除:load() 只与代码里的内置集合合并,察觉不到文件里的删除。

    "只能追加"必须靠版本控制来保证——每次提交后的清单,相对上一次提交只能增不能减。
    这个检查在 CI 里跑,防止有人改配置时顺手删掉一条保护。

    浅克隆(git clone --depth=1 / actions/checkout 的 fetch-depth: 1)下 `git log` 只能看到
    一个提交,检查会"看起来通过"实则根本没比对——那种情况必须报错而不是跳过:
    查不了等于没查,不能让它冒充通过。CI 需要完整历史(fetch-depth: 0)。"""
    import json
    import subprocess

    root = str(const.CONSTITUTION_FILE.parent.parent)

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root)

    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode == 0 and shallow.stdout.strip() == "true":
        pytest.fail("仓库是浅克隆,无法比对清单历史(清单只能追加的检查需要完整历史;"
                    "CI 请用 fetch-depth: 0)")

    out = git("log", "--format=%H", "--", "config/immutable_constitution.json")
    if out.returncode != 0:
        pytest.fail(f"git 不可用,无法检查清单历史(fail-closed): {out.stderr.strip()}")
    commits = [h.strip() for h in out.stdout.split() if h.strip()]
    if len(commits) < 2:
        # 仓库历史是完整的(上面已排除浅克隆),清单确实只有一个版本:还没有"上一次"可比,
        # 不存在被删的可能。这**不是**"查不了"(查不了的是浅克隆,那种直接失败),
        # 所以这里平凡通过而不是 skip——早先这里是 skip,而 CI 输出里的 "1 skipped"
        # 与 pass 长得一样,让人以为检查跑过了(第 5 轮 codex 指出)。
        return

    def read(rev):
        raw = git("show", f"{rev}:config/immutable_constitution.json")
        if raw.returncode != 0:
            pytest.fail(f"无法读取历史版本 {rev}: {raw.stderr.strip()}")
        return json.loads(raw.stdout)

    current = read(commits[0])
    for rev in commits[1:]:
        older = read(rev)
        for field in ("allowed_overrides", "protected_names", "protected_parameters",
                      "forbidden_order_calls", "protected_module_attrs"):
            removed = set(older.get(field, [])) - set(current.get(field, []))
            assert not removed, f"{field} 里的 {sorted(removed)} 在后续提交中被删除了(清单只能追加)"


BUILTIN_SETS = (
    "BUILTIN_ALLOWED_OVERRIDES",
    "BUILTIN_PROTECTED_NAMES",
    "BUILTIN_PROTECTED_PARAMETERS",
    "BUILTIN_FORBIDDEN_ORDER_CALLS",
    "BUILTIN_PROTECTED_MODULE_ATTRS",
    "BUILTIN_FORBIDDEN_DUNDERS",
    "BUILTIN_FORBIDDEN_BY_NAME_SETTERS",
)

# 沙盒层的三个内置 deny set 也是代码内置的"保护",一并纳入"只增不减"的历史比对:
# 它们挡的是模块导入与反射入口,删掉一个等于开一个洞,而清单文件的比对看不见它们。
SANDBOX_SETS = ("FORBIDDEN_MODULES", "FORBIDDEN_CALLS", "FORBIDDEN_ATTRS")

# 注册表:(文件相对路径, 该文件里受保护的常量名)
BUILTIN_REGISTRY = (
    ("risk/constitution.py", BUILTIN_SETS),
    ("scripts/research_sandbox.py", SANDBOX_SETS),
)


def _const_string_set(source, names):
    """从源码里取出若干常量的字符串集合。

    求值范围(受控,不是完整的 Python 求值器):
    - tuple / list / set 字面量,元素是字符串常量;
    - 常量之间的 `+`(如 `BUILTIN_PROTECTED_NAMES = (...) + BUILTIN_PROTECTED_PARAMETERS`);
    - 引用同文件里已解析的常量名。

    **不支持的形式一律不返回值**——调用方据此断言"每个应检查的常量都被解析到了",
    查不了就报错,不能让它冒充通过。早先的提取器只认 tuple/list,
    `BUILTIN_PROTECTED_NAMES` 是 BinOp,于是这个最大的一组受保护成员从来没被比对过:
    删掉 `on_disconnect` 之后 Constitution Guard 照样全绿(第 5 轮 codex 实测)。"""
    import ast

    def evaluate(node, seen):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = set()
            for elt in node.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    return None
                out.add(elt.value)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = evaluate(node.left, seen)
            right = evaluate(node.right, seen)
            if left is None or right is None:
                return None
            return left | right
        if isinstance(node, ast.Name):
            if node.id in seen:
                return seen[node.id]
            return None
        return None

    found = {}
    assignments = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value

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


def _removed_protections(current_source, older_source, names):
    """从 older 到 current 被删掉的保护项:{常量名: 被删掉的条目}。

    这条比较是"内置保护只增不减"的全部逻辑,单独抽出来是为了能对它自己写测试——
    历史上这条检查对 `BUILTIN_PROTECTED_NAMES` 完全失效,而失效时没有任何测试会报警。"""
    current = _const_string_set(current_source, names)
    older = _const_string_set(older_source, names)
    removed = {}
    for name, values in older.items():
        diff = values - current.get(name, set())
        if diff:
            removed[name] = diff
    return removed


def test_builtin_protections_are_never_shrunk():
    """代码里的内置保护项也不许被删掉。

    清单文件只能追加,靠的是上面那个历史比对;但**内置**的保护项写在
    `risk/constitution.py` 的常量里,那个比对看不见它们——把
    `BUILTIN_PROTECTED_PARAMETERS` 里的 `risk_budget` 删掉,其它检查一样全绿,
    而候选已经可以改写风控底线了。这条检查把历史版本里的常量也读出来比对。

    两个曾经让它形同虚设的地方,这次一并堵上:
    1. 提取器只认 tuple/list,`BUILTIN_PROTECTED_NAMES` 是 `tuple + tuple` 于是从未被比对
       (第 5 轮 codex 与另一轮独立复核都指出这一点);
    2. 提取失败时静默跳过——现在改为 fail-closed:该比的常量没解析出来就报错。"""
    import subprocess

    root = str(const.CONSTITUTION_FILE.parent.parent)

    def git(*args):
        # 显式按 utf-8 解码:系统默认编码在中文 Windows 上是 cp1252,会解不出仓库里的中文注释。
        return subprocess.run(["git", *args], capture_output=True, cwd=root, encoding="utf-8",
                              errors="replace")

    shallow = git("rev-parse", "--is-shallow-repository")
    if shallow.returncode == 0 and shallow.stdout.strip() == "true":
        pytest.fail("仓库是浅克隆,无法比对内置保护项的历史(fail-closed)")

    for rel, names in BUILTIN_REGISTRY:
        current_source = open(str(const.CONSTITUTION_FILE.parent.parent / rel), encoding="utf-8").read()
        current = _const_string_set(current_source, names)
        missing = [name for name in names if name not in current]
        assert not missing, (f"{rel} 里的内置常量 {missing} 无法解析成字符串集合,"
                             "历史比对对它们无效——查不了等于没查,不能让它冒充通过")

        # --full-history:默认的历史简化会把"与某个父提交内容相同"的合并提交剪掉。
        # 那种提交的内容与被剪掉的父提交完全一样,不比也不会漏掉内容;
        # 但带上 --full-history 后列表更全,读起来也更直白。
        out = git("log", "--full-history", "--format=%H", "--", rel)
        if out.returncode != 0:
            pytest.fail(f"git 不可用,无法检查内置保护项(fail-closed): {out.stderr.strip()}")
        commits = [h.strip() for h in out.stdout.split() if h.strip()]
        if len(commits) < 2:
            # 仓库历史是完整的(已排除浅克隆),该文件确实只有一个版本:没有"上一次"可比,
            # 不存在被删的可能,这不是检查没生效。
            continue
        for rev in commits:
            raw = git("show", f"{rev}:{rel}")
            if raw.returncode != 0:
                continue        # 该版本里文件还不存在:没有内容可比,跳过
            removed = _removed_protections(current_source, raw.stdout, names)
            for name, values in removed.items():
                assert not values, f"{rel} 的 {name} 里的 {sorted(values)} 在后续提交中被删除了(内置保护只能增不能减)"


def test_history_check_would_notice_a_shrunk_protection():
    """这条检查自己必须能发现"内置保护被删"——否则它自己失效时没有任何测试会报警。

    早先的提取器只认 tuple/list,而 `BUILTIN_PROTECTED_NAMES` 的定义是 `tuple + tuple`,
    于是这个最大的一组受保护成员从来没进过比对:删掉 `on_disconnect` 之后
    Constitution Guard 依然全绿(第 5 轮 codex 实测)。这个用例是那道 mutation 的守卫。"""
    source = open(str(const.CONSTITUTION_FILE.parent.parent / "risk" / "constitution.py"),
                   encoding="utf-8").read()
    assert '"on_disconnect"' in source
    shrunk = source.replace('"on_disconnect",', "", 1)
    assert shrunk != source, "源码里找不到 on_disconnect,用例本身失效了"
    removed = _removed_protections(shrunk, source, BUILTIN_SETS)
    assert "on_disconnect" in removed.get("BUILTIN_PROTECTED_NAMES", set()), removed


def test_history_check_covers_every_registered_constant():
    """注册表里的每个常量都必须能被提取器解析——有一个解析不了,那条保护就是没被守着。"""
    for rel, names in BUILTIN_REGISTRY:
        source = open(str(const.CONSTITUTION_FILE.parent.parent / rel), encoding="utf-8").read()
        parsed = _const_string_set(source, names)
        for name in names:
            assert name in parsed, f"{rel} 的 {name} 解析不出字符串集合(注册表与源码不一致)"


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
