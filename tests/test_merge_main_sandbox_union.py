"""合并 main #22(DSH 研究入口与带外批准)与 #26(辅助模块逃逸)后的**并集回归**。

两边都在加固同一个沙盒,而且都有一道"导入门":
- main:**导入白名单** `ALLOWED_MODULE_ROOTS` + 模块代理 `_GuardedModule` + 配置追加模块的准入审计;
- #26:**按路径**的导入裁决 `RepoImportGuard` + 按全名禁用的子模块 + 模块视图(对象层)判据 + 启动期受信目录核对。

这一份只测**两边规则合在一起之后**的行为:每条规则一个**放行 + 拦截**双向探针,
"被拒"的用例**断言是哪条规则拒的**(只断言"抛了异常"不够,第 22 轮踩过的坑)。
发现两边打架的地方也写成回归用例(见文件里标"冲突"的几条)。
"""
import sys
import textwrap
import types as pytypes
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bootstrap  # noqa: E402,F401  必须先于 vnpy.trader.database 导入
import research_api as api  # noqa: E402
import research_sandbox as rs  # noqa: E402


# ---------------------------------------------------------------------------
# 一、两边的名单不能打架
# ---------------------------------------------------------------------------

def test_the_import_allowlist_and_the_blocklists_do_not_overlap():
    """白名单里的根模块不能同时出现在任何一份黑名单里(否则"放行"与"拦截"互相打架)。"""
    blocked = (set(rs.FORBIDDEN_MODULES) | set(rs.EXTRA_FORBIDDEN_MODULES)
               | set(rs.DANGEROUS_MODULE_NAMES))

    assert not (rs.ALLOWED_MODULE_ROOTS & blocked), rs.ALLOWED_MODULE_ROOTS & blocked


def test_the_allowlist_lets_the_repo_import_guard_own_the_strategies_package():
    """`strategies` 在白名单里,但**能不能导入**仍由路径裁决说了算(两道闸是"与"的关系)。"""
    assert "strategies" in rs.ALLOWED_MODULE_ROOTS
    assert rs.ALLOWED_REPO_IMPORT_DIRS == ("strategies",)


def test_the_proxy_and_the_view_share_the_same_object_layer_rule():
    """代理(main)与视图(#26)的对象层判据是**同一个函数**,不会各自漂移。"""
    import inspect

    assert "_check_object_rules" in inspect.getsource(rs._GuardedModule.__getattribute__)
    assert "_check_object_rules" in inspect.getsource(rs._view_value)


# ---------------------------------------------------------------------------
# 二、导入门:白名单 + 黑名单 + 全名禁用 + 路径裁决,各自的放行与拦截
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["numpy", "pandas", "math", "enum", "statistics"])
def test_runtime_gate_allows_the_allowlisted_modules(name):
    """放行:白名单内的模块照常能导。"""
    safe = rs.safe_builtins(())

    assert safe["__import__"](name) is not None


@pytest.mark.parametrize("name, rule", [
    ("textwrap", "未列入白名单"),            # 只被白名单拒(不在任何黑名单里)
    ("os", "禁止导入"),                       # 黑名单先拒,不是笼统的"没列入白名单"
    ("zipfile", "禁止导入"),                  # 第 26 轮补进危险名单的名字
    ("numpy.ctypeslib", "按全名禁用"),        # 全名禁用先于白名单(根 numpy 在白名单里)
    ("numpy.f2py", "按全名禁用"),
])
def test_runtime_gate_refuses_and_reports_which_rule(name, rule):
    """拦截:同一个名字可能被几条规则同时拒,报的必须是**最具体的那条**。"""
    safe = rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert rule in str(exc.value), str(exc.value)


@pytest.mark.parametrize("source, needle", [
    ("import numpy as np\n", None),                       # 放行
    ("import textwrap\n", "未列入白名单"),                 # 白名单拒
    ("import os\n", "禁止导入 os"),                        # 黑名单拒
    ("from numpy import ctypeslib\n", "numpy.ctypeslib"),  # 全名禁用(fromlist 形态)
])
def test_static_gate_allow_and_refuse(source, needle):
    """静态层与运行期同口径:放行 + 拦截,拦截要点名规则。"""
    findings = rs.screen_source(source)

    if needle is None:
        assert findings == [], findings
    else:
        assert any(needle in f for f in findings), findings


def test_the_path_rule_wins_over_the_allowlist_when_both_refuse(tmp_path, monkeypatch):
    """冲突点之一(合并时处理的顺序问题):`scripts.backtest_lib` 既不在白名单、也在仓库的允许目录之外。

    两条规则都会拒,报的必须是**路径规则**(它说得出"为什么"),不是笼统的白名单。
    放行对照:`strategies.base`(在允许目录里、运行前就存在)两道闸都过。
    """
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (root / "strategies" / "base.py").write_text("VALUE = 5" + chr(10), encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "backtest_lib.py").write_text("X = 1" + chr(10), encoding="utf-8")
    for key in [n for n in list(sys.modules) if n.split(".")[0] in ("strategies", "scripts")]:
        monkeypatch.delitem(sys.modules, key, raising=False)
    monkeypatch.syspath_prepend(str(root))
    guard = rs.RepoImportGuard(root, allowed_dirs=rs.ALLOWED_REPO_IMPORT_DIRS,
                               snapshot=rs.snapshot_allowed_files(root),
                               verify_trusted_against_git=False)
    safe = rs.safe_builtins((), repo_guard=guard)

    with pytest.raises(ImportError) as exc:
        safe["__import__"]("scripts.backtest_lib")
    assert "禁止导入" in str(exc.value) and "未列入白名单" not in str(exc.value), str(exc.value)

    assert safe["__import__"]("strategies.base", fromlist=["VALUE"]).VALUE == 5


# ---------------------------------------------------------------------------
# 三、配置追加的白名单模块:准入审计(main)与模块过滤(#26)
# ---------------------------------------------------------------------------

def test_a_config_added_clean_module_is_usable_and_wrapped_in_the_proxy():
    """放行:配置追加的**干净**模块能导,而且候选拿到的是代理。"""
    safe = rs.safe_builtins((), ["textwrap"])

    module = safe["__import__"]("textwrap")

    assert type(module) is rs._GuardedModule
    assert module.fill is not None


def test_a_config_added_module_that_leaks_is_refused_by_the_audit():
    """拦截:配置追加的模块自己把 sys 挂在属性上,**整个拒收**(规则:准入审计)。"""
    safe = rs.safe_builtins((), ["zipfile"])           # zipfile 在危险名单里,黑名单先拒
    with pytest.raises(ImportError) as blocked:
        safe["__import__"]("zipfile")
    assert "禁止导入" in str(blocked.value), "配置追加不能把黑名单里的东西放出来"

    safe = rs.safe_builtins((), ["shlex"])             # shlex 不在黑名单,但自己挂着 os/sys 之类?
    try:
        module = safe["__import__"]("shlex")
    except ImportError as err:
        assert "暴露了" in str(err), str(err)
    else:
        assert type(module) is rs._GuardedModule


@pytest.mark.parametrize("name", ["matplotlib", "psutil"])
def test_conflict_matplotlib_and_psutil_cannot_be_enabled_even_by_config(name):
    """**冲突(两边口径不一致,取更严的一侧,如实记录)**。

    #26 的第 25 轮 High 3 要求 matplotlib/psutil 照常可导(它们的命名空间里有 subprocess,
    但只要不把 subprocess 递给候选就无害);main 的导入白名单默认不含它们,配置追加后
    又过不了**准入审计**(它们自己把 subprocess 挂在属性上)。
    合并后:默认被白名单拒;配置追加被审计拒。没有放宽任何一边——要放行得改审计规则
    (属于 main 的白名单策略,需人工决定),不是这次合并该替用户做的取舍。
    """
    safe = rs.safe_builtins(())
    with pytest.raises(ImportError) as default:
        safe["__import__"](name)
    assert "未列入白名单" in str(default.value), str(default.value)

    safe = rs.safe_builtins((), [name])
    with pytest.raises(ImportError) as configured:
        safe["__import__"](name)
    assert "不能加入白名单" in str(configured.value), str(configured.value)


# ---------------------------------------------------------------------------
# 四、模块代理:名字层(main)+ 对象层(#26),各自的放行与拦截
# ---------------------------------------------------------------------------

def test_proxy_name_layer_refuses_dunder_and_leaky_names_with_import_error():
    """拦截(名字层,main):下划线属性与会泄漏的属性名 → ImportError。"""
    proxy = rs.safe_builtins(())["__import__"]("enum")

    for name in ("__dict__", "__spec__", "__loader__", "_module", "sys"):
        with pytest.raises(ImportError) as exc:
            getattr(proxy, name)
        assert "不可用" in str(exc.value), (name, str(exc.value))


def test_proxy_object_layer_refuses_a_dangerous_module_under_an_unlisted_name(tmp_path, monkeypatch):
    """拦截(对象层,#26):危险模块换个名单里没有的名字挂着 → AttributeError(规则:模块视图)。

    名字层(按名字)看不见这条,所以这是对象层独立的价值;也是合并时最容易丢的一条。
    """
    pkg = tmp_path / "leaky_union_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "import os as innocent_name" + chr(10) + "VALUE = 41" + chr(10), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for key in [n for n in list(sys.modules) if n.startswith("leaky_union_pkg")]:
        monkeypatch.delitem(sys.modules, key, raising=False)

    proxy = rs.safe_builtins((), ["leaky_union_pkg"])["__import__"]("leaky_union_pkg")

    assert proxy.VALUE == 41                            # 放行:普通属性照常给
    with pytest.raises(AttributeError) as exc:
        proxy.innocent_name
    assert "规则:模块视图" in str(exc.value), str(exc.value)


def test_proxy_object_layer_refuses_a_capability_class(tmp_path, monkeypatch):
    """拦截(对象层):只递出**类**(`ctypes.CDLL`)也拒(判据是对象从哪儿定义)。"""
    import ctypes

    fake = pytypes.ModuleType("fake_union_cap")
    fake.Handle = ctypes.CDLL
    fake.Number = 3
    proxy = rs._proxy_for(fake)

    assert proxy.Number == 3
    with pytest.raises(AttributeError) as exc:
        proxy.Handle
    assert "规则:能力泄漏" in str(exc.value), str(exc.value)


def test_the_ordinary_numeric_stack_works_through_the_proxy_end_to_end():
    """放行:合并后的候选路径上,正常的数值代码照常跑(防止"双层规则叠起来把正常用法也堵了")。"""
    source = textwrap.dedent("""
        import numpy as np
        import pandas as pd
        import statistics
        a = np.array([1.0, 2.0, 3.0])
        out = {
            "mean": float(a.mean()),
            "df": float(pd.DataFrame({"x": [1.0, 2.0]})["x"].mean()),
            "stat": statistics.mean([1.0, 3.0]),
            "name": np.__name__,
        }
    """)
    assert rs.screen_source(source) == []
    namespace: dict = {"__builtins__": rs.safe_builtins(())}
    with rs.guarded_scope():
        exec(compile(source, "cand.py", "exec"), namespace)

    assert namespace["out"] == {"mean": 2.0, "df": 1.5, "stat": 2.0, "name": "numpy"}


# ---------------------------------------------------------------------------
# 五、静态层:两边的属性/字符串规则合并后各自仍然生效,且互不误伤
# ---------------------------------------------------------------------------

MAIN_ONLY_ATTRS = ("__spec__", "__loader__", "__file__", "__path__", "__package__",
                   "__reduce__", "__reduce_ex__", "__getattr__")
BRANCH_ONLY_ATTRS = ("__suppress_context__", "tb_lineno", "gi_frame", "gi_code", "cr_frame",
                     "cr_code", "ag_frame", "ag_code", "__getattribute__", "__closure__", "cell_contents")
SHARED_ATTRS = ("__traceback__", "__context__", "__cause__", "tb_frame", "tb_next", "tb_lasti",
                "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "f_lineno")


@pytest.mark.parametrize("attr", MAIN_ONLY_ATTRS + BRANCH_ONLY_ATTRS + SHARED_ATTRS)
def test_the_forbidden_attr_set_is_the_union_of_both_sides(attr):
    """拦截:两边各自新增的属性名,合并后**属性写法**与**字符串写法**都拒。"""
    assert attr in rs.FORBIDDEN_ATTRS
    assert attr in rs.FORBIDDEN_STRINGS

    attr_findings = rs.screen_source(f"x = object()\ny = x.{attr}\n")
    str_findings = rs.screen_source(f"k = {attr!r}\n")

    assert any(f"禁止访问 {attr}" in f for f in attr_findings), attr_findings
    assert any(attr in f for f in str_findings), str_findings


@pytest.mark.parametrize("source", [
    "label = 'os'\nprint('sys', 'nt', 'posix')\n",          # 短名当普通词(#26 的按键取语境)
    "d = {'status': 'open'}\n",                              # open/type 当字段名
    "class A:\n    def f(self):\n        self._state = 1\n        return self._file\n",   # 自己的私有字段(main 的按模块别名语境)
    "x = __name__\n",
    "import numpy as np\nv = np.__version__\n",
])
def test_ordinary_code_is_not_hit_by_either_sides_rules(source):
    """放行:两边的"按语境"收窄都还在——合并没有把全局字符串禁令/一刀切的私有名禁令带回来。"""
    findings = rs.screen_source(source)

    assert findings == [], findings


@pytest.mark.parametrize("source, needle", [
    ("d = {}\nd['os']\n", "按键取危险模块"),                          # #26:按键取
    ("d = {}\nd.get('sys')\n", "按键取危险模块"),
    ("s = '{0.__globals__}'.format(1)\n", "__globals__"),             # main:子串规则(拼接/模板形态)
    ("s = '{0.__gl' + 'obals__}'\n", "__globals__"),
    ("import numpy as np\nx = np._module\n", "禁止访问 _module"),     # main:模块别名上的私有能力名
])
def test_each_sides_context_sensitive_rule_still_fires(source, needle):
    """拦截:每条"按语境"的规则合并后仍然生效,并且报的是各自的规则文本。"""
    findings = rs.screen_source(source)

    assert any(needle in f for f in findings), findings


def test_substring_rule_covers_the_branch_only_dunder_names_too():
    """FORBIDDEN_SUBSTRINGS(main)由合并后的 FORBIDDEN_ATTRS 派生,#26 新增的 dunder 同样被子串规则覆盖。"""
    assert "__closure__" in rs.FORBIDDEN_SUBSTRINGS
    findings = rs.screen_source("s = '{0.__closure__}'.format(1)\n")

    assert any("__closure__" in f for f in findings), findings


# ---------------------------------------------------------------------------
# 六、相对导入:#26 的按包还原 + main 的白名单
# ---------------------------------------------------------------------------

def test_relative_import_resolves_against_the_package_and_then_meets_the_allowlist():
    """放行 + 拦截:`from . import x` 按所在包还原成绝对名,再过白名单。"""
    assert rs.screen_source("from . import base\n", package="strategies") == []

    findings = rs.screen_source("from . import base\n", package="helperpkg")
    assert any("未列入白名单" in f and "helperpkg" in f for f in findings), findings

    findings = rs.screen_source("from . import base\n")
    assert any("禁止相对导入" in f for f in findings), findings


def test_screen_source_keeps_both_sides_positional_signature():
    """`research_api`/网关等调用方按位置传 `(源码, 禁用模块, 禁用调用, 追加白名单)`,`package` 在最后。"""
    import inspect

    params = list(inspect.signature(rs.screen_source).parameters)

    assert params == ["source", "extra_modules", "extra_calls", "extra_allowed", "package"]


def test_safe_builtins_keeps_both_sides_signature():
    """main 位置传 `(禁用模块, 追加白名单)`;#26 用关键字传 `repo_guard`。"""
    import inspect

    params = list(inspect.signature(rs.safe_builtins).parameters)

    assert params == ["forbidden_modules", "allowed_modules", "repo_guard"]


# ---------------------------------------------------------------------------
# 七、生产入口:静态层先拒的(白名单/走栈属性),与运行期拒的,分开断言
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("source, needle", [
    ("import helperpkg\n", "未列入白名单"),
    ("import os\n", "禁止导入 os"),
    ("x = __loader__\n", "禁止引用 __loader__"),
    ("import numpy as np\nx = np.__spec__\n", "禁止访问 __spec__"),
])
def test_the_production_screen_refuses_with_the_expected_rule(tmp_path, source, needle):
    """生产入口的静态筛查:这几类源码**在静态层就被拒**,并且点名规则。"""
    path = tmp_path / "cand.py"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(api.JobError) as exc:
        api.import_candidate(path, "S", api.rs.file_sha256(path))

    assert needle in str(exc.value), str(exc.value)


def test_the_production_path_wires_both_defences_into_one_call():
    """接线检查(声明了的防线必须真接上):`import_candidate` 同时传了 `allowed_modules` 与 `repo_guard`,
    并且在预加载之前调用了 `verify_trusted_tree`。"""
    import inspect

    src = inspect.getsource(api.import_candidate)
    call = src[src.index("rs.safe_builtins("):]
    call = call[:call.index(")") + 1]

    assert 'cfg["screening"]["allowed_modules"]' in call, call
    assert "repo_guard=repo_guard" in call, call
    assert src.index("verify_trusted_tree") < src.index("protected_before = const.snapshot_protected_state()")


# 这些用例会走生产入口、把**临时仓库里的**包真的导入进 sys.modules。monkeypatch.delitem 只还原
# "删除时存在的"几项,测试期间新导入的不会被清掉,于是后面的测试(例如 test_research_api 里的
# `import strategies.instruments`)拿到指向临时仓库的残缺包 —— 表现为"按文件顺序才出现"的
# ModuleNotFoundError(合并 main #22 之后暴露)。每个用例前后自己保存/还原这几个前缀。
_SANDBOX_TEST_PREFIXES = ("strategies", "pkgx", "pkgrel", "pkgok", "leaky_union_pkg")


@pytest.fixture(autouse=True)
def _restore_modules_touched_by_sandbox_tests():
    import sys as _sys

    def touched(name):
        return name.split(".")[0] in _SANDBOX_TEST_PREFIXES

    saved = {n: m for n, m in _sys.modules.items() if touched(n)}
    yield
    for name in [n for n in list(_sys.modules) if touched(n)]:
        del _sys.modules[name]
    _sys.modules.update(saved)
