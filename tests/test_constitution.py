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
    src = 'class C:\n    param_grid = {"risk_budget": [100, 200, 300], "fast_window": [1, 2, 3], "atr_mult": [1, 2, 3]}\n'
    assert any("risk_budget" in f and "param_grid" in f for f in screen(src))


def test_syntax_errors_are_left_to_the_sandbox_screener():
    assert screen("class C(:\n") == []


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
