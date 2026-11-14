"""第 4 轮 G8 复核(codex,37.5/50)的两条主要缺陷的回归。

F1(通配符导入的缺口):
    候选写 `from strategies.trend_atr_strategy import *`,静态筛查与沙盒筛查两层都是 **0 findings**,
    却把 `swap_in`/`swap_out`/`read_only_view`(连同 `within_budget` 等风控函数)一次性绑进
    候选的命名空间——只读视图的整个前提("候选碰不到换入换出")被绕开。
    修法:筛查 `import *` 时**解析被导入模块的名字集合**,含受保护符号(或模块级私有符号)就拒,
    并给出是哪一条规则拒的。

F2(读取外流规则误伤诚实候选):
    "受保护容器读取不得流出去"这条规则把仓库自己的 LLM 提示词写法
    (`self.am.close[-2:]`、`np.mean(self.am.close)`、`self.am.count`、`self.am.close_array`)
    整片拒掉,进化循环因此被闲置。只读视图落地后读取返回的是**拷贝**,别名无害,
    所以规则收窄到**写入**;写入仍然拒。

纪律:每条规则都配**双向**用例——一条证明拦得住(并断言是哪一条规则拦的),
一条证明正常写法放行(不误伤)。只断言"被拒"不算数。
"""
import numpy as np
import pytest

import bootstrap  # noqa: F401  (必须先于 vnpy)
import research_sandbox as rs
from risk import constitution as const


def const_findings(source: str) -> list[str]:
    return const.screen_source(source)


def sandbox_findings(source: str) -> list[str]:
    return rs.screen_source(source)


# ---------- F1:通配符导入 ----------

STAR_RULE = "规则:通配符导入不得暴露受保护符号"
NAMESPACE_RULE = "规则:候选命名空间不得持有受保护对象"


def test_the_wildcard_import_really_puts_the_dispenser_into_the_candidate_namespace():
    """**事实依据**:通配符导入确实把 `swap_out`/`read_only_view` 带进候选命名空间。

    没有这一条,后面的"必须拦"就没有根据——静态层能不能拦住是规则问题,
    "拦的是什么"是事实问题,两者都要有证据。"""
    namespace: dict = {}
    exec("from strategies.trend_atr_strategy import *", namespace)          # noqa: S102  (复现攻击)
    assert "swap_out" in namespace
    assert "swap_in" in namespace
    assert "read_only_view" in namespace
    assert "within_budget" in namespace


@pytest.mark.parametrize("screen", [const_findings, sandbox_findings])
def test_the_wildcard_import_is_refused_by_both_screening_layers(screen):
    """两个筛查层(静态筛查 + 沙盒筛查)都必须拒,而且要说清是哪一条规则拒的。"""
    findings = screen("from strategies.trend_atr_strategy import *\n")
    assert findings, "通配符导入没有被拦下"
    assert any("通配符" in f for f in findings), findings
    assert any("swap_out" in f for f in findings), findings
    assert any(STAR_RULE in f for f in findings), findings


def test_the_wildcard_import_of_the_market_view_module_is_refused_too():
    """`risk.market_view` 里住着 `swap_in`/`swap_out`/`read_only_view`——同一个洞的另一个入口。

    (`_REAL`/`_SOURCE` 是私有名,Python 的 `import *` 本来就不绑它们;真正会被绑走、
    因而真正危险的是上面三个公开名。)"""
    findings = const_findings("from risk.market_view import *\n")
    assert findings
    assert any("swap_out" in f and STAR_RULE in f for f in findings), findings


# ---------- 名字集合解析的边界(第 4 轮 codex 复核的 Medium-1/2、Low-3/4) ----------

@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    """把"仓库根"指到一个临时目录:被导入模块的源码在那里,静态层照样能核验。"""
    monkeypatch.setattr(const, "REPO_ROOT", tmp_path)
    return tmp_path


def _write_module(root, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_a_module_with_only_a_private_symbol_is_refused(fake_repo):
    """模块级私有符号(`_secret`)是"这个模块在藏东西"的信号——通配符导入一律拒。"""
    _write_module(fake_repo, "priv_mod.py", "_secret = 1\n\n\ndef helper():\n    return 2\n")
    findings = const_findings("from priv_mod import *\n")
    assert findings
    assert any("私有符号" in f for f in findings), findings


def test_a_clean_module_with_no_private_symbol_is_allowed(fake_repo):
    """**放行方向**:干净模块(只有公开名)不许被误伤。"""
    _write_module(fake_repo, "clean_mod.py", "def helper():\n    return 2\n\n\nVALUE = 3\n")
    assert const_findings("from clean_mod import *\n") == []


def test_a_literal_all_list_decides_exactly_what_is_exported(fake_repo):
    """`__all__` 是字面量时,以它为准:只导出列表里的名字
    (Python 就是这么定的——原来一律按"模块里有什么"判,会多拒)。"""
    _write_module(fake_repo, "all_ok.py",
                  "__all__ = ['helper']\n\n\ndef helper():\n    return 1\n\n\ndef other():\n    return 2\n")
    assert const_findings("from all_ok import *\n") == []

    _write_module(fake_repo, "all_bad.py",
                  "__all__ = ['swap_out']\n\n\ndef swap_out(x):\n    return x\n")
    findings = const_findings("from all_bad import *\n")
    assert findings, "`__all__` 里点名了受保护符号竟然放行"
    assert any("swap_out" in f for f in findings), findings


def test_a_literal_all_list_can_hide_a_protected_symbol(fake_repo):
    """`__all__` 也能**缩小**导出面:模块里有受保护符号,但 `__all__` 没点它,
    `import *` 就不会绑它(Python 就是这么定的)——这时必须**放行**。

    这条是"`__all__` 说了算"的双向另一半:上面那条测"点名了要拒",
    这条测"没点名要放行"。少了它,按"模块里有什么"判的实现也能全绿——
    变异验证里这条正是靠它才变红。"""
    _write_module(fake_repo, "all_hide.py",
                  "__all__ = ['helper']\n\n\ndef helper():\n    return 1\n\n\ndef swap_out(x):\n    return x\n")
    assert const_findings("from all_hide import *\n") == []


def test_a_computed_all_list_is_refused(fake_repo):
    """`__all__` 算不出来 → 名字集合不可证 → fail-closed 拒。"""
    _write_module(fake_repo, "all_dyn.py",
                  "import os\n\n\n__all__ = [n for n in dir()]\n")
    findings = const_findings("from all_dyn import *\n")
    assert findings
    assert any("不可证" in f for f in findings), findings


def test_a_module_with_a_dynamic_getattr_is_refused(fake_repo):
    """模块级 `__getattr__`(PEP 562)能按需造名字 → 名字集合不可证 → 拒。"""
    _write_module(fake_repo, "dyn_mod.py",
                  "def __getattr__(name):\n    return lambda *a: name\n")
    findings = const_findings("from dyn_mod import *\n")
    assert findings
    assert any("不可证" in f for f in findings), findings


def test_a_module_that_itself_uses_a_wildcard_import_is_refused(fake_repo):
    """被导入模块内部自己用了 `from ... import *` → 它绑了什么我们看不到 → 拒。"""
    _write_module(fake_repo, "inner.py", "def helper():\n    return 1\n")
    _write_module(fake_repo, "star_inner.py", "from .inner import *\n")
    findings = const_findings("from star_inner import *\n")
    assert findings
    assert any("不可证" in f for f in findings), findings


def test_a_package_is_resolved_before_a_same_named_module(fake_repo):
    """**包优先**:Python 的 `FileFinder` 先找包(`X/__init__.py`)再找模块(`X.py`)。

    顺序反了会核验错的文件——`pkg/__init__.py` 脏、`pkg.py` 干净时,
    静态层按干净的那个放行,而运行时绑进来的是脏包里的名字
    (第 4 轮 codex 复核的 Medium-1)。"""
    _write_module(fake_repo, "pkg.py", "def helper():\n    return 1\n")
    _write_module(fake_repo, "pkg/__init__.py", "def swap_out(x):\n    return x\n")
    findings = const_findings("from pkg import *\n")
    assert findings, "包里明明有受保护符号,静态层却按同名模块放行了"
    assert any("swap_out" in f for f in findings), findings


def test_the_named_import_of_the_template_base_class_is_still_allowed():
    """**不能误伤仓库自己的正常写法**:候选本来就要继承模板基类。"""
    source = "from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n"
    assert const_findings(source) == []
    assert sandbox_findings(source) == []


def test_the_wildcard_import_of_a_clean_module_is_still_allowed():
    """规则不是"一律不许通配符":被导入模块没有受保护符号、也没有模块级私有符号就放行
    (模板的 15 分钟子模块正是这种——它只导出 `TrendAtrStrategy`/`TrendAtr15mStrategy`/`BarData`)。"""
    assert const_findings("from strategies.trend_atr_15m_strategy import *\n") == []


def test_a_wildcard_import_of_a_module_we_cannot_inspect_is_refused():
    """查不了等于没查(fail-closed):仓库外的模块解析不出名字集合,一律拒。"""
    findings = const_findings("from numpy import *\n")
    assert findings
    assert any("无法核验" in f and STAR_RULE in f for f in findings), findings


def test_a_relative_wildcard_import_is_refused():
    """相对导入(`from . import *`)的基准模块静态层看不到,一律拒。"""
    findings = const_findings("from . import *\n")
    assert findings
    assert any("无法确定" in f for f in findings), findings


def test_the_runtime_guard_refuses_a_namespace_that_carries_the_protected_symbols():
    """运行期守卫:候选导入之后核对命名空间,**持有**受保护对象就 fail-closed。

    按**身份**核对(第 4 轮 codex 复核的 High-1):候选 `_hoard = swap_out` 再
    `del swap_out`,名字删干净了,但**手里还握着真的 `swap_out`**——只查名字会放行。"""
    from risk.market_view import read_only_view, swap_out

    problems = const.check_candidate_namespace({"swap_out": swap_out, "read_only_view": read_only_view})
    assert problems
    assert any("swap_out" in p for p in problems), problems
    assert any(NAMESPACE_RULE in p for p in problems), problems


def test_the_runtime_guard_refuses_a_renamed_and_deleted_protected_symbol():
    """**High-1 的正面用例**:改名 + 删除之后仍然被抓到——因为核的是值不是名字。"""
    from risk.market_view import swap_out

    namespace: dict = {"_hoard": swap_out}
    problems = const.check_candidate_namespace(namespace)
    assert problems, "改名 + 删除之后竟然放行了(High-1 复发)"
    assert any("swap_out" in p for p in problems), problems


def test_the_runtime_guard_refuses_a_protected_symbol_hidden_in_a_container():
    """藏一层(列表/字典)也抓得到。"""
    from risk.market_view import swap_out

    problems = const.check_candidate_namespace({"_hoard": [swap_out]})
    assert problems
    assert any("swap_out" in p for p in problems), problems


def test_the_runtime_guard_lets_a_candidate_own_symbol_of_the_same_name_through():
    """**误伤方向**(第 4 轮 codex 复核的 Medium-4):候选**自己**定义了一个叫
    `trading_day` 的函数——这不是框架的风控对象,按身份核对就不该拒。

    按**名字**核对会把它拒掉,而那正是 F2 要消灭的那类误伤(诚实候选被整片拒掉)。"""
    def trading_day():          # noqa: ANN001, ANN202  (候选自己的函数)
        return 2026

    assert const.check_candidate_namespace({"trading_day": trading_day}) == []


def test_the_runtime_guard_lets_an_ordinary_namespace_through():
    assert const.check_candidate_namespace({"TrendAtr15mStrategy": object, "np": np}) == []


def test_the_real_screening_entry_refuses_the_wildcard_import(tmp_path):
    """走**真实筛查入口**(`scripts/research_api.py::screen`,网关与 CLI 都走它),
    不是只测内部函数:候选文件放在真实的 pending_strategies 目录形状下被筛一遍。"""
    import research_api
    import research_config as rc

    cfg = rc.load()
    pending = tmp_path / "pending_strategies"
    pending.mkdir()

    wildcard = pending / "wildcard_candidate.py"
    wildcard.write_text("from strategies.trend_atr_strategy import *\n\n\n"
                        "class Cand:\n    def entry_signal(self):\n        return 0\n",
                        encoding="utf-8")
    findings = research_api.screen(wildcard, cfg)
    assert findings, "真实筛查入口没有拦下通配符导入"
    assert any(STAR_RULE in f for f in findings), findings

    honest = pending / "honest_candidate.py"
    honest.write_text("from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n\n\n"
                       "class Cand(TrendAtr15mStrategy):\n    def entry_signal(self):\n"
                       "        return self.am.sma(self.fast_window, array=True)[-1] > 0 and 1 or 0\n",
                       encoding="utf-8")
    assert research_api.screen(honest, cfg) == []


def test_the_candidate_import_entry_refuses_a_namespace_that_carries_the_protected_symbols(tmp_path, monkeypatch):
    """**运行期**那一层:候选真的被导入一次,导入之后核对命名空间,发现受保护符号就拒绝执行。

    关键在于把**静态筛查中性掉**再跑一次:静态层本来就拒这一路,
    不中性掉的话,这条用例会因为"静态层拒了"而通过,根本证明不了运行期那一层有判别力
    (变异验证 M2 就是这么发现的:把运行期核对去掉,用例照样全绿)。"""
    import hashlib

    import research_api

    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    path = pending / "wildcard_candidate.py"
    raw = ("from strategies.trend_atr_strategy import *\n\n\n"
           "class Cand:\n    def entry_signal(self):\n        return 0\n").encode("utf-8")
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()

    # ① 静态筛查照常:照样拒(两层都在)
    with pytest.raises(research_api.JobError) as excinfo:
        research_api.import_candidate(path, "Cand", sha)
    assert "受保护" in str(excinfo.value) or "静态筛查未通过" in str(excinfo.value), str(excinfo.value)

    # ② 静态筛查被中性掉(模拟"静态层被绕开"):运行期这一层仍然拒
    monkeypatch.setattr(research_api.rs, "screen_source", lambda *a, **k: [])
    # 合并 main #26 之后,生产入口在**更早**的一层(模块代理:通配符导入需要读模块的 `__dict__`,
    # 代理一律不给)就把它拒了——候选依然进不来,但"运行期命名空间核对"这一层不再被走到。
    # 所以:生产入口只断言**被拒**(并且是代理那条规则拒的,不是别的原因),
    # 而命名空间核对的判别力改为**直接**对真实的通配符命名空间验证(见下)。
    with pytest.raises((research_api.JobError, research_api.CandidateError)) as excinfo:
        research_api.import_candidate(path, "Cand", sha)
    assert "受保护对象" in str(excinfo.value) or "不可用" in str(excinfo.value), str(excinfo.value)

    # ③ 命名空间核对本身(不经代理):真实的通配符命名空间里持有受保护符号 → 必须报
    from risk import constitution as const
    namespace: dict = {}
    exec("from strategies.trend_atr_strategy import *", namespace)          # noqa: S102 - 只在测试里复现命名空间
    findings = const.check_candidate_namespace(namespace)
    assert findings and any("受保护" in f for f in findings), findings


def test_the_candidate_import_entry_refuses_a_renamed_and_deleted_protected_symbol(tmp_path, monkeypatch):
    """**High-1 的端到端用例**:候选把 `swap_out` 改名藏起来再删掉名字,
    静态筛查被中性掉(模拟静态层被绕开)之后,运行期这一层仍按**身份**抓到它。"""
    import hashlib

    import research_api

    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    path = pending / "hoarding_candidate.py"
    raw = ("from strategies.trend_atr_strategy import *\n"
           "_hoard = swap_out\n"
           "del swap_out, swap_in, read_only_view\n\n\n"
           "class Cand:\n    def entry_signal(self):\n        return 0\n").encode("utf-8")
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()

    monkeypatch.setattr(research_api.rs, "screen_source", lambda *a, **k: [])
    # 合并 main #26 之后生产入口由模块代理更早拒绝(见上一条用例的说明);这里同样断言被拒,
    # 并把"按**身份**抓到改名藏起来的符号"这一层直接对真实命名空间验证。
    with pytest.raises((research_api.JobError, research_api.CandidateError)) as excinfo:
        research_api.import_candidate(path, "Cand", sha)
    assert "受保护对象" in str(excinfo.value) or "不可用" in str(excinfo.value), str(excinfo.value)

    from risk import constitution as const
    namespace: dict = {}
    exec("from strategies.trend_atr_strategy import *\n_hoard = swap_out\n"            # noqa: S102
         "del swap_out, swap_in, read_only_view\n", namespace)
    findings = const.check_candidate_namespace(namespace)
    assert any("受保护对象" in f for f in findings), findings


# ---------- F2:读取放行、写入拒 ----------

READS = [
    "x = self.am.close[-2:]\n",                    # 仓库提示词里的写法之一
    "x = np.mean(self.am.close)\n",                # 仓库提示词里的写法之二
    "x = self.am.count\n",                         # 仓库提示词里的写法之三
    "x = self.am.close_array\n",                   # 仓库提示词里的写法之四
    "x = (lambda: self.am.close)()\n",             # lambda 位置
    "x = 1 if True else self.am.high\n",           # 条件表达式位置
    "x = None or self.am.close\n",                 # or/and 位置
    "x = [self.am.close]\n",                       # 装进容器
    "x = helper(self.am.close)\n",                 # 传给出调用
]


@pytest.mark.parametrize("body", READS)
def test_reading_the_container_is_allowed(body):
    """读取一律放行:视图下读到的是拷贝,别名无害。"""
    source = "def entry_signal(self):\n    " + body + "    return 0\n"
    findings = const_findings(source)
    assert findings == [], f"读取被误伤: {findings}"
    assert sandbox_findings(source) == [], f"读取被误伤: {sandbox_findings(source)}"


WRITES = [
    ("self.am.close.fill(0.0)\n", "变更型方法"),
    ("self.am.update_bar(fake_bar)\n", "变更型方法"),
    ("self.am.close.__setitem__(slice(None), 0.0)\n", "变更型方法"),
    ("self.am.close[0] = 1.0\n", "改写了受保护属性"),
    ("self.am.close[:] = 1.0\n", "改写了受保护属性"),
    ("self.am.count = 5\n", "改写了受保护属性"),
    ("self.am.count += 1\n", "改写了受保护属性"),
    ("del self.am.close\n", "删除"),
    ("self.bg.update_bar(fake_bar)\n", "变更型方法"),
]


@pytest.mark.parametrize("body,why", WRITES)
def test_writing_the_container_is_refused_and_the_reason_says_it_is_a_write(body, why):
    """规则拒的确实是**写入**,不是读取——断言里带上"为什么被拒"的那一段。"""
    source = "def entry_signal(self):\n    " + body + "    return 0\n"
    findings = const_findings(source)
    assert findings, f"写入没有被拦下: {body!r}"
    assert any(why in f for f in findings), findings


def test_the_two_layers_agree_on_writes_too():
    source = "def entry_signal(self):\n    self.am.close.fill(0.0)\n    return 0\n"
    assert const_findings(source)
    assert sandbox_findings(source)


# ---------- F2 的补充:容器上的**视图型结果**当写入目标 ----------

VIEW_READS = [
    "x = self.am.close.reshape(-1)\n",       # 读视图:允许
    "x = self.am.close.view()\n",            # 读视图:允许
    "x = self.am.close_array.T\n",           # 读视图:允许
    "x = np.mean(self.am.close.reshape(-1))\n",   # 读视图:允许
]


@pytest.mark.parametrize("body", VIEW_READS)
def test_reading_a_view_of_the_container_is_allowed(body):
    """收窄到"写入"之后,**读**视图型结果不能跟着被拒。

    (这条是 F2 的直接推论:仓库提示词里的 `np.mean(self.am.close)` 就是这一类。)"""
    source = "def entry_signal(self):\n    " + body + "    return 0\n"
    findings = const_findings(source)
    assert findings == [], f"读取被误伤: {findings}"
    assert sandbox_findings(source) == [], f"读取被误伤: {sandbox_findings(source)}"


VIEW_WRITES = [
    "self.am.close.reshape(-1)[0] = 9.0\n",
    "self.am.close.view()[1] = 8.0\n",
    "self.am.close_array.T[0] = 9.0\n",
    "self.am.close.swapaxes(0, 1)[0] = 1.0\n",
    "self.am.close.reshape(-1)[0] += 1.0\n",
    "del self.am.close.reshape(-1)[0]\n",
]


@pytest.mark.parametrize("body", VIEW_WRITES)
def test_writing_through_a_view_of_the_container_is_refused(body):
    """把容器上的**视图型结果**当写入目标,仍然拒——这条不能跟着"读取放行"一起漏掉。

    断言里写明是哪一段判据拒的("视图型结果"),不是笼统的"被拒"。"""
    source = "def entry_signal(self):\n    " + body + "    return 0\n"
    findings = const_findings(source)
    assert findings, f"写入没有被拦下: {body!r}"
    assert any("视图型结果" in f for f in findings), findings


# ---------- 容器别名 / 自由函数形态 / 下标删除(第 4 轮 codex 复核的 Medium-3、Low-1/2) ----------

ALIAS_WRITES = [
    ("a = self.am\n    a.update_bar(fake_bar)\n", "容器别名调用变更型方法"),
    ("b = self.bg\n    b.update_bar(fake_bar)\n", "容器别名调用变更型方法"),
    ("a = self.am\n    a.close[0] = 1.0\n", "容器别名写入受保护容器"),
    ("a = self.am\n    a.count = 5\n", "容器别名写入受保护容器"),
]


@pytest.mark.parametrize("body,why", ALIAS_WRITES)
def test_writing_through_a_container_alias_is_refused(body, why):
    """**容器别名**(`a = self.am`)上的写入仍然拒。

    只查"属性路径里有没有 `am`/`bg`"的规则看不见别名——别名让路径变成 `a.close`,
    名字里没有 `am`(第 4 轮 codex 复核的 Medium-3:旧规则能拦,收窄后 0 findings)。"""
    source = "def entry_signal(self):\n    " + body + "    return 0\n"
    findings = const_findings(source)
    assert findings, f"容器别名上的写入没有被拦下: {body!r}"
    assert any(why in f for f in findings), findings


def test_binding_an_array_from_the_container_is_still_allowed():
    """**放行方向**:绑定**数组**(`x = self.am.close`)不是容器别名,写它无害,不许被拒。

    (这条与"容器别名"只差一个字:别把两者混在一起,否则又会误伤诚实候选。)"""
    source = "def entry_signal(self):\n    x = self.am.close\n    x[0] = 1.0\n    return 0\n"
    assert const_findings(source) == []


def test_binding_a_candidates_own_list_is_still_allowed():
    """**放行方向**:候选自己的列表叫 `am` 也不许被拒(第 12 轮的 Low 1)。"""
    source = "def entry_signal(self):\n    am = []\n    am.append(1)\n    return 0\n"
    assert const_findings(source) == []


def test_handing_the_container_to_a_mutating_function_is_refused():
    """**自由函数形态**的变更型函数:`np.copyto(self.am.close, x)`。

    名单里本来就有 `copyto`,但规则只匹配 `接收者.方法()` 形态
    (第 4 轮 codex 复核的 Low-2)。"""
    source = "def entry_signal(self):\n    np.copyto(self.am.close, other)\n    return 0\n"
    findings = const_findings(source)
    assert findings
    assert any("变更型函数" in f for f in findings), findings


def test_deleting_an_element_of_the_container_is_refused():
    """下标删除(`del self.am.close[0]`)也是删除——删除规则原来只看属性形态。"""
    source = "def entry_signal(self):\n    del self.am.close[0]\n    return 0\n"
    findings = const_findings(source)
    assert findings
    assert any("删除" in f for f in findings), findings


# ---------- 第 6 轮 codex 的发现:候选自己的同名符号(H-1) ----------

def test_a_candidate_may_define_its_own_trading_day():
    """**放行方向**:候选自己写一个 `trading_day()`(LLM 候选极常见的辅助名;
    本仓 `risk/account_guard.py` 就有同名函数)——它引用的是**自己**那个函数,
    不是框架的风控函数。

    第 6 轮 codex 的 High-1:静态层此前按**名字**一律拒,而运行期那一层已经改成
    按**身份**核对(候选自己的同名符号放行)——两层口径相反,
    静态筛查先执行,运行期那条"放行"在真实流水线里**永远走不到**。
    这正是 F2 要消灭的那类误伤:把诚实候选整片拒掉,进化循环因此被闲置。"""
    source = (
        "def trading_day():\n    return 20261113\n\n\n"
        "def entry_signal(self):\n    if trading_day() == 20261113:\n        return 1\n    return 0\n")
    assert const_findings(source) == []


def test_a_candidate_may_define_its_own_stop_distance_and_within_budget():
    """**放行方向**同上:`stop_distance`/`within_budget` 也是候选可能自写的辅助名。"""
    source = (
        "def stop_distance(x):\n    return x\n\n\n"
        "def within_budget(a, b):\n    return a < b\n\n\n"
        "def entry_signal(self):\n    return 1 if stop_distance(1) and within_budget(1, 2) else 0\n")
    assert const_findings(source) == []


def test_importing_a_protected_object_is_still_refused():
    """**拦截方向**:豁免只对**候选自己定义**的名字有效——
    `from risk.market_view import swap_out` 绑的是框架那个,照旧拒。"""
    findings = const_findings(
        "from risk.market_view import swap_out\n\n\ndef entry_signal(self):\n    return 0\n")
    assert findings, "从受保护模块导入受保护对象竟然放行"
    assert any("不得导入受保护的风控对象" in f for f in findings), findings


def test_taking_a_protected_object_by_attribute_is_still_refused():
    """**拦截方向**:候选自己定义了同名函数,也不能顺带把框架那个取到手:
    `x = risk.market_view.swap_out` 的属性那一路照旧拒。"""
    findings = const_findings(
        "def swap_out(x):\n    return x\n\n\n"
        "x = risk.market_view.swap_out\n\n\ndef entry_signal(self):\n    return 0\n")
    assert findings, "按属性取受保护对象竟然放行"
    assert any("禁止引用受保护的风控对象 swap_out" in f for f in findings), findings


def test_referencing_an_undefined_protected_name_is_still_refused():
    """**拦截方向**:候选**没有**定义过这个名字 → 引用的是框架那个,照旧拒。"""
    findings = const_findings(
        "def entry_signal(self):\n    return 1 if trading_day() else 0\n")
    assert findings, "引用未定义的受保护名字竟然放行"
    assert any("禁止引用受保护的风控对象 trading_day" in f for f in findings), findings


def test_the_candidates_own_trading_day_passes_the_real_screening_entry(tmp_path):
    """**真实入口**:走 `research_api.screen`(网关与 CLI 走的那个)也必须放行——
    静态层放行不等于流水线放行。"""
    import research_api
    import research_config as rc

    cfg = rc.load()
    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    candidate = pending / "own_trading_day_candidate.py"
    candidate.write_text(
        "def trading_day():\n    return 20261113\n\n\n"
        "class Cand:\n    def entry_signal(self):\n        return 1 if trading_day() else 0\n",
        encoding="utf-8")
    assert research_api.screen(candidate, cfg) == []


# ---------- 第 6 轮 codex 的发现:关键字形态的自由函数(Medium-2) ----------

def test_handing_the_container_to_a_mutating_function_by_keyword_is_refused():
    """**关键字形态**的自由函数:`np.copyto(dst=self.am.close, src=x)`。

    numpy 的 `copyto` 是 ufunc、`put` 是普通函数,两种都接受关键字;
    规则原来只看位置实参,这一形态 0 findings(第 6 轮 codex 的 Medium-2)。"""
    source = "def entry_signal(self):\n    np.copyto(dst=self.am.close, src=other)\n    return 0\n"
    findings = const_findings(source)
    assert findings, "关键字形态的变更型函数没有被拦下"
    assert any("变更型函数" in f for f in findings), findings


def test_handing_the_container_to_put_by_keyword_is_refused():
    """`np.put(a=self.am.close, ind=[0], v=1.0)` 同理。"""
    source = "def entry_signal(self):\n    np.put(a=self.am.close, ind=[0], v=1.0)\n    return 0\n"
    findings = const_findings(source)
    assert findings, "关键字形态的 np.put 没有被拦下"
    assert any("变更型函数" in f for f in findings), findings


def test_a_keyword_that_is_not_the_target_is_still_allowed():
    """**放行方向**:关键字实参不是"被写的目标"时,不许误伤。"""
    source = "def entry_signal(self):\n    np.copyto(dst=other, src=self.am.close)\n    return 0\n"
    assert const_findings(source) == []


# ---------- 第 6 轮 codex 的发现:文案必须说真话(Medium-3) ----------

def test_the_private_symbol_finding_does_not_claim_a_false_binding(fake_repo):
    """裁决文本必须**说真话**:没有 `__all__` 时,Python 的 `import *` 本来就**不绑**
    下划线开头的名字——发现文本却说"会把它放进候选命名空间"(第 6 轮 codex 的 Medium-3)。

    拒它的**理由**是"这个模块在藏东西"这个信号,所以行为不变,但文本要改准。

    (这一条要**单独造**一个"有私有名、没有受保护名"的模块:直接拿 `risk.market_view` 试不行,
    它命中的是"受保护符号"那一条,根本走不到私有符号这一条——那样这条用例就白写了。)"""
    _write_module(fake_repo, "priv_text.py", "_secret = 1\n\n\ndef helper():\n    return 2\n")
    findings = const_findings("from priv_text import *\n")
    assert findings, "带模块级私有名字的模块竟然放行"
    assert all("会把模块级私有符号" not in f for f in findings), findings
    assert any("私有符号" in f for f in findings), findings


def test_the_private_symbol_finding_still_names_the_symbol(fake_repo):
    """改准之后仍要点名具体符号(复核者要能自证是哪一条规则、哪些符号)。"""
    _write_module(fake_repo, "priv_mod.py", "_secret = 1\n\n\ndef helper():\n    return 2\n")
    findings = const_findings("from priv_mod import *\n")
    assert findings
    assert any("私有符号" in f and "_secret" in f for f in findings), findings


# ---------- 第 6 轮 codex 的发现:命名空间核对的边界(Low-3/Low-4) ----------

def test_a_plain_integer_in_the_namespace_is_not_treated_as_a_protected_object():
    """**放行方向**:身份核对按 `id` 记对象,标量(小整数)会被复用——
    拿它当身份会让候选命名空间里任何一个同值整数都被误判成持有受保护对象,
    **整个进化循环被闲置**(第 6 轮 codex 的 Low-3)。"""
    assert const.check_candidate_namespace({"x": 1, "y": 2, "z": 0}) == []


def test_a_protected_object_hidden_in_a_dict_key_is_still_caught():
    """**拦截方向**:字典的**键**同样能把对象持在手里(`{swap_out: 1}`)。"""
    from risk.market_view import swap_out

    problems = const.check_candidate_namespace({"hoard": {swap_out: 1}})
    assert problems, "藏在字典键里的受保护对象没有被抓到"
    assert any("规则:候选命名空间不得持有受保护对象" in p for p in problems), problems


def test_a_scalar_bound_to_a_protected_name_is_not_treated_as_a_protected_object(monkeypatch):
    """**放行方向**(第 6 轮 codex 的 Low-3):身份核对用 `id` 当键,
    而**标量**的 id 会被复用/撞上别的对象——拿它当"身份"会让候选命名空间里
    任何一个同值的小整数都被误判成持有受保护对象,**整个进化循环被闲置**。

    这里把受保护模块里的一个受保护名临时绑成整数(用完自动还原),
    再看候选命名空间里同值的整数会不会被误判。"""
    import risk.market_view as mv

    monkeypatch.setattr(mv, "swap_out", 5)
    assert const.check_candidate_namespace({"x": 5}) == []
