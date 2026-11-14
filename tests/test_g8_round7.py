"""第 7 轮 G8 复核(codex,38/50)发现的 HIGH-1 的回归。

**这轮的发现是"证伪",不是"新漏洞"**:
上一轮(codex 40/50)的 ADR/CHANGELOG 里写着"`self` 别名形态今天不可利用",
第 7 轮 codex 实测把它证伪了——`import strategies.trend_atr_strategy as self` +
`self.swap_out(strategy)` 在静态层与运行期**同时放行**,真 ArrayManager 回到候选手里。
所以这一轮的用例既要**证明它现在被拦住**,也要**证明它当初真的能溜过去**
(否则"拦住了"可能只是因为别的规则顺手拦的,判别力不明)。

纪律(项目铁律):每条规则配**双向**用例——一条证明拦得住**并断言是哪一条规则拦的**
(不能只断言"被拒"),一条证明正常写法放行(不误伤)。
"""
import pytest

import bootstrap  # noqa: F401  (必须先于 vnpy)
import research_sandbox as rs
from risk import constitution as const

IMPORT_RULE = "规则:候选不得导入含受保护符号的模块"
NAMESPACE_RULE = "规则:候选命名空间不得持有受保护对象"
ATTR_RULE = "禁止引用受保护的风控对象"

# 候选把**整个模块**握在手里:模块里的发放口一步可达。
SELF_ALIAS = "import strategies.trend_atr_strategy as self\n"


def const_findings(source: str) -> list[str]:
    return const.screen_source(source)


def sandbox_findings(source: str) -> list[str]:
    return rs.screen_source(source)


# ---------- 事实依据:这个形态当初真的能溜过去 ----------

def test_the_self_alias_really_puts_the_real_dispenser_one_step_away():
    """**事实依据**:`import strategies.trend_atr_strategy as self` 把**模块**绑成 `self`,
    于是 `self.swap_out` 就是框架那个真的发放口。

    没有这一条,后面的"必须拦"就没有根据:能不能拦住是规则问题,
    "当初能不能溜过去"是事实问题,两者都要有证据。"""
    import strategies.trend_atr_strategy as tas

    namespace: dict = {}
    exec(SELF_ALIAS, namespace)                                  # noqa: S102  (复现攻击)
    assert namespace["self"].swap_out is tas.swap_out
    assert namespace["self"].read_only_view is tas.read_only_view


# ---------- 拦截方向 ----------

@pytest.mark.parametrize("screen", [const_findings, sandbox_findings])
def test_the_self_alias_import_is_refused_by_both_screening_layers(screen):
    """两个筛查层(静态筛查 + 沙盒筛查)都必须拒,并说清是哪一条规则拒的。"""
    findings = screen(SELF_ALIAS)
    assert findings, "把整个受保护模块握在手里的导入竟然放行了"
    assert any(IMPORT_RULE in f for f in findings), findings
    assert any("swap_out" in f for f in findings), findings


def test_the_attribute_use_of_an_import_bound_self_is_refused_too():
    """**纵深**:即使导入那一路被绕过,`self.swap_out` 这一路也必须拦。

    第 7 轮 codex 的 HIGH-1 根因之一:`self` 被 import 绑成了模块,
    `self.<名字>` 于是长得像"实例自己的属性"(候选自己的 `self.trading_day` 那种),
    属性那一路的豁免把它放行了。这里断言**属性**规则自己会拦。"""
    source = SELF_ALIAS + "\n\nclass Cand:\n    def entry_signal(self):\n        return self.swap_out(self)\n"
    findings = const_findings(source)
    assert findings
    assert any(ATTR_RULE in f and "swap_out" in f for f in findings), findings


def test_importing_the_module_that_holds_the_dispensers_is_refused():
    """不带别名也拒:候选拿到整个模块就等于拿到发放口(别名只是其中一种写法)。"""
    findings = const_findings("import strategies.trend_atr_strategy\n")
    assert findings
    assert any(IMPORT_RULE in f and "swap_out" in f for f in findings), findings


def test_importing_the_market_view_module_is_refused_too():
    """`risk.market_view` 里住着 `read_only_view`/`swap_in`/`swap_out`——同一个洞的另一个入口。"""
    findings = const_findings("import risk.market_view\n")
    assert findings
    assert any(IMPORT_RULE in f and "read_only_view" in f for f in findings), findings


def test_the_runtime_guard_refuses_a_candidate_holding_the_protected_module():
    """**运行期**那一层也要拦:候选命名空间里握着整个模块,一步就能走到发放口。

    第 7 轮 codex 的 HIGH-1 的第三根因:运行期只按**符号身份**核对,
    没把**模块对象**算进去——`import strategies.trend_atr_strategy as t` 整个模块就在手里。"""
    import strategies.trend_atr_strategy as tas

    problems = const.check_candidate_namespace({"t": tas})
    assert problems, "候选握着整个受保护模块竟然放行了"
    assert any(NAMESPACE_RULE in p for p in problems), problems
    assert any("strategies.trend_atr_strategy" in p for p in problems), problems


def test_the_real_screening_entry_refuses_the_self_alias(tmp_path):
    """走**真实筛查入口**(`scripts/research_api.py::screen`,网关与 CLI 都走它),
    不是只测内部函数。"""
    import research_api
    import research_config as rc

    cfg = rc.load()
    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    candidate = pending / "self_alias_candidate.py"
    candidate.write_text(SELF_ALIAS + "\n\nclass Cand:\n    def entry_signal(self):\n"
                                        "        return self.swap_out(self)\n",
                         encoding="utf-8")
    findings = research_api.screen(candidate, cfg)
    assert findings, "真实筛查入口没有拦下自别名导入"
    assert any(IMPORT_RULE in f for f in findings), findings


# ---------- 放行方向(不误伤) ----------

def test_importing_a_clean_repository_module_is_still_allowed():
    """**放行**:仓库里干净的模块照常可导——`strategies.trend_atr_15m_strategy`
    是候选的模板基类所在,它里面没有受保护符号。"""
    assert const_findings("import strategies.trend_atr_15m_strategy\n") == []


def test_importing_a_third_party_module_is_still_allowed():
    """**放行**:第三方库(numpy/pandas)照常可导。

    把它们也拒掉,不是纵深防御,那是自伤(进化循环因此被闲置)。"""
    assert const_findings("import numpy as np\nimport pandas as pd\n") == []


def test_the_template_base_class_import_is_still_allowed():
    """**放行**:候选本来就要继承模板基类,这一行必须照旧放行。"""
    assert const_findings("from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n") == []


def test_a_candidate_may_still_write_its_own_instance_attribute():
    """**放行**:`self.trading_day = ...` 是候选自己的实例属性,不是模块级函数——
    第 11 轮 codex 的 Medium-1 修过一次,这一轮不能把它改回去。"""
    assert const_findings("class Cand:\n    def entry_signal(self):\n        self.trading_day = 1\n        return 0\n") == []


# ---------- 第 7 轮 codex 的 Medium-1:绑定形式的漏网之鱼 ----------

BINDING_FORMS = {
    "参数": "def f(trading_day):\n    return trading_day\n",
    "for 目标": "def f(xs):\n    for swap_out in xs:\n        print(swap_out)\n    return 1\n",
    "with ... as": "def f(ctx):\n    with ctx as stop_distance:\n        return stop_distance\n",
    "except ... as": "def f():\n    try:\n        pass\n    except ValueError as within_budget:\n        return within_budget\n",
    "lambda 参数": "f = lambda read_only_view: read_only_view\n",
    "海象表达式": "def f():\n    if (swap_in := 1):\n        return swap_in\n    return 0\n",
    "match 捕获": "def f(x):\n    match x:\n        case trading_day:\n            return trading_day\n    return 0\n",
}


@pytest.mark.parametrize("source", BINDING_FORMS.values(), ids=list(BINDING_FORMS))
def test_a_candidate_may_bind_a_protected_name_in_any_binding_form(source):
    """**放行**:候选把受保护符号的名字绑成**自己的**局部变量,不是框架那个对象。

    第 7 轮 codex 的 Medium-1:上一版只认 `def`/`class`/赋值目标,
    参数/for/with/except/lambda/海象/match 这些绑定形式全漏了 → 诚实候选被整片误拒。"""
    assert const_findings(source) == []


# ---------- 第 7 轮 codex 的 Medium-2:豁免的作用域 ----------

def test_a_function_local_definition_does_not_exempt_a_module_level_reference():
    """**拦截**:某个函数里定义了同名符号,**不能**把模块级的引用一起豁免掉。

    第 7 轮 codex 的 Medium-2:上一版把整个模块里出现过的名字都当成"候选自己定义的",
    于是函数里的 `def swap_out` 会把模块级的 `swap_out()` 调用也放行——
    而模块级那个 `swap_out` 很可能来自 `import *`(它已被另一条规则拦,这里只是兜底)。"""
    source = ("def helper():\n"
              "    def swap_out(value):\n"
              "        return value\n"
              "    return swap_out\n"
              "\n"
              "\n"
              "swap_out(1)\n")
    findings = const_findings(source)
    assert findings, "模块级的受保护符号引用被函数内的同名定义豁免了"
    assert any(ATTR_RULE in f and "swap_out" in f for f in findings), findings


def test_a_module_level_definition_does_exempt_the_module_level_reference():
    """**放行**:候选在**模块级**定义的名字,模块级引用它当然放行。"""
    source = "def swap_out(value):\n    return value\n\n\nswap_out(1)\n"
    assert const_findings(source) == []


def test_a_class_attribute_is_visible_in_the_class_body_but_not_in_a_method():
    """**作用域**要按 Python 的规则来:类作用域在**类体**里可见,在方法里不可见。"""
    in_body = "class Cand:\n    trading_day = 1\n    value = trading_day\n"
    assert const_findings(in_body) == []
    in_method = "class Cand:\n    trading_day = 1\n\n    def entry_signal(self):\n        return trading_day\n"
    findings = const_findings(in_method)
    assert findings, "方法里引用类属性(运行期是 NameError)却放行了"
    assert any(ATTR_RULE in f and "trading_day" in f for f in findings), findings


# ---------- 第 7 轮 codex 的 Low-1:运行期的兜底口径 ----------

def test_the_runtime_fallback_does_not_refuse_a_candidate_own_symbol(monkeypatch):
    """**放行**:拿不到身份表时(受保护模块没被加载),兜底那一条按**名字**核对,
    但要排除候选**自己**的同名符号——否则与静态层"候选自己定义的放行"口径相反,
    诚实的 `def trading_day()` 会被整片误拒(第 7 轮 codex 的 Low-1)。"""
    monkeypatch.setattr(const, "_protected_runtime_objects", dict)

    def trading_day():                       # noqa: ANN001, ANN202  (候选自己的函数)
        return 2026

    assert const.check_candidate_namespace({"trading_day": trading_day}) == []


def test_the_runtime_fallback_still_refuses_the_framework_symbol(monkeypatch):
    """**拦截**:同一个兜底路径,遇到**框架那个**符号仍然要拒。"""
    from risk.market_view import swap_out

    monkeypatch.setattr(const, "_protected_runtime_objects", dict)
    problems = const.check_candidate_namespace({"swap_out": swap_out})
    assert problems, "兜底路径把框架的受保护符号放行了"
    assert any(NAMESPACE_RULE in p for p in problems), problems
