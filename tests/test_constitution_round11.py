"""第 11 轮 codex 复核(43.0/50,未达 >=45)的逐条回归用例。

第 11 轮 codex 逐条复现了第 10 轮的 10 条修复,并找出 2 条 High / 3 条 Medium / 2 条 Low。
本文件把这些问题**一条一条**钉成用例,防止下一轮改动把它们带回来:
- High 1:受保护容器只挡"写/取别名",不挡"变更型方法调用"(注入假 K 线四层全绿);
- High 2:第四层执行点守卫只数名字,不看语义(影子函数 / 自己比自己 / 死分支 / 新执行入口);
- Medium 1:`self.trading_day` 被误判成改写风控函数(第 10 轮新引入的误报);
- Medium 2:指纹只覆盖快照时已加载的受保护模块;
- Medium 3:vnpy 核心对象只靠"模板按名 import"的巧合被覆盖;
- Medium 4:numpy 数组退化成身份标记,原地改写看不见;
- Low 1:裸局部变量名 `am` / `bg` 被拒;
- Low 3:第三层只查实例字典,看不见运行期给类打的补丁。
"""
from pathlib import Path

import pytest

from risk import constitution as const

# 交易栈侧:**必须先于 vnpy.trader.database 导入**(固定时区)。
# 本文件的指纹用例会经 `const.snapshot_protected_state()` 预加载
# `vnpy_ctastrategy.backtesting`,那会连带加载 `vnpy.trader.database`;
# 若此时 bootstrap 还没导入,后面 `import backtest_lib` 里的
# `import bootstrap` 会因导入顺序检查而抛 RuntimeError。
# (以前这段顺序是靠 `conftest → segment_ledger → bootstrap` 兜住的,
#  登记文件与交易栈解耦后那层兜底没了,这里显式补上。)
# 放在 `from risk import constitution` 之后是安全的:宪法模块在**导入期**
# 不碰 vnpy(它只在自己的函数里按需 import)。
#
# **只判断「交易栈在不在」,不吞异常**(第 12 轮 codex 的 M1):
# 用 `try/except ImportError: pass` 的话,「装了但装坏了」的 vnpy
# (缺 vnpy.trader.setting / bootstrap 自身有语法错误)会被**静默吞掉**,
# bootstrap 于是从没被导入,后面报的是「导入顺序错了」,
# 真实原因(交易栈没装好)反而看不见——排查方向被带偏。
import importlib.util

if importlib.util.find_spec("vnpy") is not None:
    import bootstrap  # noqa: F401

ROOT = Path(const.CONSTITUTION_FILE.parent.parent)


def screen(src, c=None):
    return const.screen_source(src, c or const.default())


# ---------- High 1:受保护容器上的变更型方法调用 ----------

@pytest.mark.parametrize("src", [
    "def entry_signal(self):\n    self.am.update_bar(fake_bar)\n    return 1\n",
    "def entry_signal(self):\n    self.bg.update_tick(fake_tick)\n    return 1\n",
    "def entry_signal(self):\n    self.am.close.fill(0.0)\n    return 1\n",
    "def entry_signal(self):\n    self.am.close_list.append(1.0)\n    return 1\n",
])
def test_the_static_layer_refuses_to_mutate_the_market_data_container(src):
    """High 1:受保护容器只挡"写/取别名",不挡"变更型方法调用"。

    第 11 轮 codex 端到端实测:`self.am.update_bar(fake_bar)` 静态层与沙盒层都返回 [],
    第 3/4 层也不覆盖活实例——四层全绿,而回测结果被完全改写
    (skipped_by_budget 从 0 变成 3928)。这与容器规则的立意
    ("防止伪造价格 -> 伪造收益")直接矛盾:写数组被拦、插数组不拦。"""
    findings = screen(src)
    assert findings, "改写行情数据容器没被拦下(High 1 复发)"


def test_the_static_layer_allows_reading_the_market_data_container():
    """拦的是"改",不是"读":正常取指标不许被误伤。"""
    findings = screen(
        "def entry_signal(self):\n"
        "    fast = self.am.sma(self.fast_window, array=True)\n"
        "    return 1\n")
    assert not findings, findings


# ---------- Medium 1:`self.trading_day` 的新误报 ----------

def test_the_static_layer_allows_the_candidate_to_keep_its_own_trading_day():
    """Medium 1:第 10 轮把 `trading_day` 放进模块级成员名单之后,
    `self.trading_day = bar.datetime.date()` 被误判成"改写了受保护的风控函数"——
    "每天只开一次仓"这类常规候选会被整片拒掉。模块级成员名只在**根不是 self** 时命中。"""
    findings = screen(
        "def entry_signal(self):\n"
        "    self.trading_day = self.bar.datetime.date()\n"
        "    return 1\n")
    assert not findings, findings


def test_the_static_layer_still_refuses_to_rewrite_the_module_level_trading_day():
    """放宽只对 `self.` 生效:模块级的 `trading_day` 照样拦。"""
    findings = screen(
        "import risk.account_guard as ag\n"
        "ag.trading_day = lambda: None\n")
    assert findings, "改写模块级 trading_day 没被拦下"


# ---------- Low 1:裸局部变量名 am / bg ----------

def test_the_static_layer_allows_a_local_variable_named_am():
    """Low 1:`am = self.am.sma(5)` 是 ArrayManager 的常见缩写,按名字一刀切会把正常候选拒掉。
    容器名降级为**路径保护**:只保护 `self.am.xxx` 这条路径,不保护裸局部名。"""
    findings = screen(
        "def entry_signal(self):\n"
        "    am = self.am.sma(self.fast_window, array=True)\n"
        "    bg = list(range(3))\n"
        "    return 1\n")
    assert not findings, findings


def test_the_static_layer_refuses_taking_the_bar_generator_itself():
    """第 12 轮 codex 的 High 1 变体③:`bg` 与 `am` 是同一类容器。

    `x = self.bg` 之后 `x.update_bar(fake_bar)` 往回测里插一根假 K 线,
    而调用点已经看不出 x 是谁——所以容器**本身**也不许取出来另存。
    (裸局部名 `bg = list(range(3))` 仍然放行:那是候选自己的列表。)"""
    findings = screen(
        "def entry_signal(self):\n"
        "    x = self.bg\n"
        "    x.update_bar(fake_bar)\n"
        "    return 1\n")
    assert findings, "取走 BarGenerator 没被拦下"


def test_the_static_layer_still_refuses_a_class_attribute_named_am():
    """裸局部名放行,但**类体**里定义 `am` 仍然拒:那是在遮蔽容器的调用路径。"""
    findings = screen(
        "class C:\n"
        "    am = 3\n"
        "    def entry_signal(self):\n"
        "        return 1\n")
    assert findings, "类体里定义 am 没被拦下"


# ---------- Medium 2:运行期新加载的受保护模块 ----------

def test_the_fingerprint_reports_a_protected_module_loaded_at_runtime():
    """Medium 2:指纹只覆盖**快照时已加载**的受保护模块,候选在运行期 import 一个
    此前没加载过的受保护子模块再改写它,指纹完全看不见。这里显式报出来。"""
    import types

    before = const.snapshot_protected_state()
    after = dict(before)
    after["risk.brand_new_module"] = ("id", types.ModuleType("risk.brand_new_module"))
    findings = const.check_protected_state(before, after)
    assert any("risk.brand_new_module" in f for f in findings), findings


# ---------- Medium 3:vnpy 核心对象不再只靠"巧合"被覆盖 ----------

def test_the_fingerprint_covers_the_framework_classes_by_module_scope():
    """Medium 3:`vnpy.trader.utility.ArrayManager` 此前只因为模板**按名 import**
    才恰好出现在指纹里;模板改成 `import vnpy.trader.utility as util` 之后
    这层保护会静默消失。现在按**模块路径**显式纳入范围,改写法也不掉保护。"""
    snapshot = const.snapshot_protected_state()
    assert "vnpy.trader.utility.ArrayManager" in snapshot
    assert "vnpy.trader.object.BarData" in snapshot
    assert "vnpy.trader.constant.Exchange" in snapshot


def test_the_fingerprint_notices_a_patched_framework_class():
    """Medium 3 的另一半:框架类被改写必须被抓到(不再只靠模板按名 import 的巧合)。"""
    import vnpy.trader.utility as util

    before = const.snapshot_protected_state()
    original = util.ArrayManager.sma
    try:
        util.ArrayManager.sma = lambda self, n, array=False: 0.0
        after = const.snapshot_protected_state()
        findings = const.check_protected_state(before, after)
    finally:
        util.ArrayManager.sma = original
    assert findings, "框架类 ArrayManager 被改写没被抓到"


def test_the_database_module_is_out_of_the_fingerprint_scope():
    """`vnpy.trader.database` **故意不在范围内**:它的模块级 `database` 全局
    在第一次 `get_database()` 时会被合法替换(离线取数脚本会这么做),
    纳入范围会造成运行期虚假 diff;而研究链路的数据来自 CSV,数据库不在数据路径上。"""
    snapshot = const.snapshot_protected_state()
    assert not any(key.startswith("vnpy.trader.database") for key in snapshot)


# ---------- Medium 4:numpy 数组的原地改写要看得见 ----------

def test_the_fingerprint_sees_in_place_changes_of_numpy_arrays():
    """Medium 4:`_safe_token` 曾把 numpy 数组退化成 `("id", id(x))`,
    于是 `arr[0] = 5` 这种原地改写在指纹里看不见。"""
    import numpy as np

    arr = np.array([1.0, 2.0, 3.0])
    before = const._protected_fingerprint_value(arr)
    arr[0] = 99.0
    after = const._protected_fingerprint_value(arr)
    assert not const._protected_values_same(before, after)


def test_the_fingerprint_does_not_report_unchanged_numpy_arrays():
    """内容没变就不能报——否则每次回测都会刷出一堆虚假 diff。"""
    import numpy as np

    before = const._protected_fingerprint_value(np.array([1.0, 2.0, 3.0]))
    after = const._protected_fingerprint_value(np.array([1.0, 2.0, 3.0]))
    assert const._protected_values_same(before, after)


# ---------- Low 3:运行期给类打补丁 ----------

def test_the_runtime_check_notices_a_class_patched_at_runtime():
    """Low 3:第三层只查**实例字典**,候选在运行期给自己的类打补丁
    (`Cand.get_size = ...`)看不见。这里在回测结束后再比一次类字典。

    候选类在真实流水线里由 `research_api.import_candidate` 现场建壳
    (模块名 `pending_strategy`,**不进 sys.modules**),所以这里照着做:
    把类的 `__module__` 指到一个不在仓库里的模块名,让它走"候选"这条路。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Cand(TrendAtrStrategy):
        def entry_signal(self) -> int:
            return 0

    Cand.__module__ = "pending_candidate_module"       # 候选的模块名,不在 sys.modules 里
    template = const.load_template()
    strategy = object.__new__(Cand)
    assert const._runtime_class_dict_violations(strategy, template, const.default()) == []

    Cand.get_size = lambda self: 0.001
    findings = const._runtime_class_dict_violations(strategy, template, const.default())
    assert findings, "运行期给类打补丁没被抓到"


def test_the_runtime_check_does_not_flag_the_repository_strategies():
    """仓库自己的策略**合法地**重写内部方法(演示策略重写了 `_try_enter` /
    `_process_daily_bar`),不受"候选不得重写受保护成员"这条规矩约束——
    否则整个仓库的策略都会被自己的宪法拒掉。"""
    from strategies.intraday_demo_strategy import IntradayDemoStrategy

    template = const.load_template()
    strategy = object.__new__(IntradayDemoStrategy)
    assert const._runtime_class_dict_violations(strategy, template, const.default()) == []


# ---------- High 2:第四层执行点的**调用形态**与**完备性** ----------

def _guard_root(tmp_path):
    """造一个最小的仓库骨架:历史守卫只认注册表里那两个文件的相对路径。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_the_guard_notices_a_shadowed_execution_point(tmp_path):
    """High 2:同名影子函数让第四层的调用落到假函数上,守卫必须报出来。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _guard_root(tmp_path)
    body = (
        "import risk.constitution as const\n\n"
        "def check_protected_state(before, after):\n"
        "    return []\n\n"
        "def run():\n"
        "    before = const.snapshot_protected_state()\n"
        "    after = const.snapshot_protected_state()\n"
        "    return check_protected_state(before, after)\n")
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    problems = check_call_site_shape(root)
    assert any("同名" in p for p in problems), problems


def test_the_guard_notices_a_self_comparing_execution_point(tmp_path):
    """High 2:`check_protected_state(before, before)`——自己比自己,比对必然通过。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _guard_root(tmp_path)
    body = (
        "import risk.constitution as const\n\n"
        "def run():\n"
        "    before = const.snapshot_protected_state()\n"
        "    return const.check_protected_state(before, before)\n")
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    problems = check_call_site_shape(root)
    assert any("同一个对象" in p for p in problems), problems


def test_the_guard_notices_a_dead_branch_execution_point(tmp_path):
    """High 2:把第四层的调用挪进死分支(`if False:`),名字引用数不变但底线不再执行。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _guard_root(tmp_path)
    body = (
        "import risk.constitution as const\n\n"
        "def run():\n"
        "    before = const.snapshot_protected_state()\n"
        "    after = const.snapshot_protected_state()\n"
        "    if False:\n"
        "        const.check_protected_state(before, after)\n"
        "    return None\n")
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    problems = check_call_site_shape(root)
    assert any("活调用" in p for p in problems), problems


def test_the_guard_notices_an_executor_without_the_fourth_layer(tmp_path):
    """High 2 的完备性:新增一个跑候选的执行入口却完全不调第四层,守卫必须报出来。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    covered = (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    after = const.snapshot_protected_state()\n"
        "    return const.check_protected_state(before, after)\n")
    (root / "scripts" / "backtest_lib.py").write_text(covered, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(
        "def run(code):\n    exec(code, {})\n    return None\n", encoding="utf-8")
    problems = check_executor_completeness(root)
    assert any("brand_new_runner" in p for p in problems), problems


def test_the_guard_accepts_the_shipped_execution_points():
    """仓库里现有的两个执行点必须**通过**形态与完备性检查(守卫不能只会报错)。"""
    from tools.check_constitution_history import check_call_site_shape, check_executor_completeness

    assert check_call_site_shape(ROOT) == []
    assert check_executor_completeness(ROOT) == []
