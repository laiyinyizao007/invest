"""策略骨架契约(`strategies/trend_atr_15m_strategy.py`)的**独立**单测。

roadmap §2.2 的设计:「模板限制(§6.2-1):LLM 只写信号函数,风控写死 | 内层只读资产:
策略骨架模板由人维护、**有独立单测**;LLM 产出只能继承该骨架」。

「独立」的意思是:这份契约**不依赖**候选是否合规来间接保证。候选侧的检查在宪法里
(见 `tests/test_constitution*.py`);这里是骨架自己的那一面——骨架提供了什么、
候选子类化它之后哪些东西**仍然动不了**。

骨架改了契约面(参数集合、变量集合、可重写成员)就必须改这份测试,这是有意的:
契约面变更要显式,不能悄悄发生。
"""
import hashlib
import importlib
import json
from pathlib import Path

import pytest

import bootstrap  # noqa: F401,E402
from risk import approved_guard  # noqa: E402
from risk import constitution as const  # noqa: E402

# 骨架类**在测试里现取**,不在模块顶层 import:
# 别的测试会把 `strategies.*` 从 sys.modules 里清掉(沙盒的模块隔离用例),
# 顶层 import 拿到的是**清理前**那个类对象,后面再 import 同一个模块会得到一个新的类对象——
# 两者 `is` 不等但行为一样。断言要跟「当下解析到的那个」比,别跟历史遗留的那个比。
def skeleton_class() -> type:
    return importlib.import_module("strategies.trend_atr_15m_strategy").TrendAtr15mStrategy

# 骨架的契约面:改了这里,就要想清楚对候选与对恢复的影响
SKELETON_PARAMETERS = [
    "fast_window", "slow_window", "atr_mult", "atr_window",
    "risk_budget", "take_profit_r", "max_daily_losses", "history_vt_symbol",
]
SKELETON_VARIABLES = [
    "atr_value", "stop_price", "entry_price", "entry_dist", "prev_pos",
    "skipped_by_budget", "trail_side", "trail_extreme", "trail_stop",
]

CANDIDATE = '''from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    fast_window = 8
    slow_window = 20
    atr_mult = 2.0
'''


def test_the_constitution_points_at_this_skeleton():
    """宪法指定的模板必须就是这份骨架——候选继承的必须是「人维护的那一个」。"""
    assert const.default().template == "strategies.trend_atr_15m_strategy:TrendAtr15mStrategy"
    assert const.load_template() is skeleton_class()


def test_skeleton_parameters_contract():
    assert list(skeleton_class().parameters) == SKELETON_PARAMETERS


def test_skeleton_variables_contract():
    """变量集合是重启恢复的契约面:少一个,重启后就丢掉一份状态。"""
    assert list(skeleton_class().variables) == SKELETON_VARIABLES


def test_skeleton_carries_the_risk_floor_as_code_not_as_a_parameter():
    """风控底线固化在骨架里:默认值就是底线,候选改不了、调用方也传不进来。"""
    skeleton = skeleton_class()
    assert skeleton.risk_budget == 300.0
    assert skeleton.max_daily_losses == 2
    assert skeleton.atr_window == 14
    # 调用方传进来也不行(受保护参数)
    assert const.check_params({"risk_budget": 999999})
    assert const.check_params({"max_daily_losses": 99})
    assert const.check_params({"Risk_Budget": 999999})       # 大小写不同也算
    assert const.check_params({"fast_window": 10}) == []      # 可调参数当然可以


def test_a_subclass_cannot_weaken_the_risk_floor():
    """候选子类化骨架之后,风控对象仍然碰不到。"""
    source = CANDIDATE + (
        "\n    def entry_signal(self) -> int:\n"
        "        self.guard.max_losses = 99\n"
        "        return 0\n"
    )
    findings = const.screen_source(source)
    assert findings
    assert any("guard" in f for f in findings)


def test_a_subclass_cannot_redefine_a_non_overridable_member():
    """白名单之外的模板成员不得被重写(重写即绕过风控)。"""
    source = CANDIDATE + (
        "\n    def _manage_position(self, bar) -> None:\n"
        "        return None\n"
    )
    findings = const.screen_source(source)
    assert findings
    assert any("_manage_position" in f for f in findings)


def test_two_candidates_can_coexist_in_the_approved_dir(tmp_path):
    """多个候选可以并存于 approved/:互不污染,都能装载。"""
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    ledger = tmp_path / "approvals.json"
    ledger.write_text("{}", encoding="utf-8")

    data = {}
    for name, fast in (("slow_candidate.py", 8), ("fast_candidate.py", 12)):
        source = CANDIDATE.replace("fast_window = 8", f"fast_window = {fast}")
        # 写**字节**:Windows 上 write_text 会把 \n 翻成 \r\n,哈希得按真正落到盘上的字节算
        (approved_dir / name).write_bytes(source.encode("utf-8"))
        sha = hashlib.sha256((approved_dir / name).read_bytes()).hexdigest()
        data[sha] = {"name": name, "at": "2026-11-14T00:00:00"}
    ledger.write_text(json.dumps(data), encoding="utf-8")

    first = approved_guard.load_approved_strategy(
        approved_dir=approved_dir, approvals_file=ledger, file_name="slow_candidate.py")
    second = approved_guard.load_approved_strategy(
        approved_dir=approved_dir, approvals_file=ledger, file_name="fast_candidate.py")

    assert first is not second
    assert first.fast_window == 8 and second.fast_window == 12
    skeleton = skeleton_class()
    assert issubclass(first, skeleton)
    assert issubclass(second, skeleton)


def test_the_skeleton_has_its_own_unit_tests():
    """骨架自己的行为要有测试兜着(不是靠候选侧顺带覆盖)。"""
    root = Path(__file__).resolve().parents[1]
    candidates = [
        p for p in (root / "tests").glob("test_*.py")
        if "skeleton" in p.name or "trend_atr" in p.name or "strategy_backtest" in p.name
    ]
    assert candidates, "骨架缺少自己的单测文件"
