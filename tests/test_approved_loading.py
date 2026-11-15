"""从 `approved/` 装载策略(`risk/approved_guard.py` + `trading/runtime.py`)。

这条路径回答的是整个系统里最要紧的问题:**这份代码凭什么有权被运行时执行?**
答案只有一个:人批准过这段字节。于是每一条「不是人批准过的字节」的路都必须被拒绝:
  - 账本里没有 → 拒绝
  - 批准后被改过 → 拒绝
  - 不在批准目录里(路径穿越/符号链接)→ 拒绝
  - 违反宪法 → 拒绝
  - 不是骨架的子类 → 拒绝
  - 设置里夹带受保护参数 → 拒绝
而且失败必须是**拒绝启动**,不能悄悄退回默认策略。
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

import bootstrap  # noqa: F401,E402  (把仓库根放进 sys.path)
import vnpy_ctastrategy.engine as cta_engine_module  # noqa: E402
from fakes import FakeDatabase, FakeGateway, SyncEventEngine, history_bars  # noqa: E402
from risk import approved_guard  # noqa: E402
from trading.runtime import (  # noqa: E402
    EXIT_STARTUP_FAILED,
    RuntimeConfig,
    TradingRuntime,
)

CANDIDATE = '''from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy


class Candidate(TrendAtr15mStrategy):
    fast_window = 8
    slow_window = 20
    atr_mult = 2.0
'''


@pytest.fixture()
def approved(tmp_path):
    """一个临时的批准目录 + 账本。"""
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    ledger = tmp_path / "approvals.json"
    ledger.write_text("{}", encoding="utf-8")
    return approved_dir, ledger


def _install(approved_dir: Path, ledger: Path, name: str = "cand.py", source: str = CANDIDATE) -> str:
    path = approved_dir / name
    path.write_text(source, encoding="utf-8")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data[sha] = {"name": name, "at": "2026-11-14T00:00:00"}
    ledger.write_text(json.dumps(data), encoding="utf-8")
    return sha


def _load(approved_dir, ledger, name="cand.py", setting=None):
    return approved_guard.load_approved_strategy(
        approved_dir=approved_dir, approvals_file=ledger, file_name=name,
        class_name="Candidate", setting=setting)


def test_an_approved_candidate_loads(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    cls = _load(approved_dir, ledger)

    assert cls.__name__ == "Candidate"
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    assert issubclass(cls, TrendAtr15mStrategy)


def test_a_file_that_was_never_approved_is_refused(approved):
    approved_dir, ledger = approved
    (approved_dir / "cand.py").write_text(CANDIDATE, encoding="utf-8")

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "没有人工批准记录" in str(excinfo.value)


def test_a_file_changed_after_approval_is_refused(approved):
    approved_dir, ledger = approved
    sha = _install(approved_dir, ledger)
    (approved_dir / "cand.py").write_text(CANDIDATE + "\n# 事后被改\n", encoding="utf-8")

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "没有人工批准记录" in str(excinfo.value)
    assert sha != hashlib.sha256((approved_dir / "cand.py").read_bytes()).hexdigest()


def test_a_broken_ledger_is_refused_fail_closed(approved):
    """账本坏了不能当成「谁都没批准过」——那会让所有文件都被拒,但错误信息会误导人。"""
    approved_dir, ledger = approved
    _install(approved_dir, ledger)
    ledger.write_text("{not json", encoding="utf-8")

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "fail-closed" in str(excinfo.value)


def test_path_traversal_and_odd_names_are_refused(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    for bad in ("../cand.py", "sub/cand.py", "..\\cand.py", "CAND.py", "cand.txt", ""):
        with pytest.raises(approved_guard.ApprovedLoadError):
            _load(approved_dir, ledger, name=bad)


def test_a_file_outside_the_approved_dir_is_refused(approved, tmp_path):
    approved_dir, ledger = approved
    outside = tmp_path / "elsewhere.py"
    outside.write_text(CANDIDATE, encoding="utf-8")
    sha = hashlib.sha256(outside.read_bytes()).hexdigest()
    data = json.loads(ledger.read_text(encoding="utf-8"))
    data[sha] = {"name": "elsewhere.py", "at": "2026-11-14T00:00:00"}
    ledger.write_text(json.dumps(data), encoding="utf-8")

    # 文件名合法、也在账本里,但它**不在** approved/ 里 → 拒绝
    with pytest.raises(approved_guard.ApprovedLoadError):
        _load(approved_dir, ledger, name="elsewhere.py")


def test_a_candidate_that_violates_the_constitution_is_refused(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=CANDIDATE + (
        "\n    def entry_signal(self) -> int:\n"
        "        self.guard.max_losses = 99\n        return 0\n"))

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "静态筛查未通过" in str(excinfo.value)


def test_a_class_that_is_not_a_skeleton_subclass_is_refused(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "class Candidate:\n"
        "    pass\n"))

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "不是策略骨架" in str(excinfo.value)


def test_a_missing_class_is_refused(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source="X = 1\n")

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "没有类 Candidate" in str(excinfo.value)


def test_a_setting_that_carries_protected_parameters_is_refused(approved):
    """风控底线不得由调用方指定:设置里夹带 risk_budget 之类 → 拒绝装载。"""
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger, setting={"risk_budget": 999999})
    assert "策略设置未通过校验" in str(excinfo.value)


def test_consistency_check_reports_a_tampered_approved_file(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger)
    assert approved_guard.check_approved_consistency(
        approved_dir=approved_dir, approvals_file=ledger) == []

    (approved_dir / "cand.py").write_text("# 事后被改\n", encoding="utf-8")
    problems = approved_guard.check_approved_consistency(
        approved_dir=approved_dir, approvals_file=ledger)
    assert problems and "不在批准账本里" in problems[0]


# ---------- 运行时装载 ----------

class RecordingNotifier:
    def __init__(self):
        self.messages = []

    def send(self, level, title, message, extra=None):
        self.messages.append((level, title, message))
        return True

    def close(self, drain_timeout=0.0):
        return None

    def titles(self):
        return [t for _, t, _ in self.messages]


@pytest.fixture()
def make_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(cta_engine_module, "load_json", lambda filename: {})
    monkeypatch.setattr(cta_engine_module, "save_json", lambda *a, **k: None)
    created = []

    def factory(config_overrides):
        cfg = RuntimeConfig(
            vt_symbol="c2601.DCE",
            # 注意:设置里**不带** risk_budget——它是受保护参数,调用方不得指定
            # (宪法 check_params 会拒绝)。风控底线只能来自代码里的默认值。
            strategy_setting={"fast_window": 2, "slow_window": 3, "atr_mult": 1.5},
            gateway_setting={"用户名": "u"},
            position_settle_seconds=1.0, connect_timeout=5.0,
            **config_overrides,
        )
        rt = TradingRuntime(cfg, FakeGateway, notifier=RecordingNotifier(),
                            event_engine=SyncEventEngine())
        rt.rule.update_setting({"state_file": str(tmp_path / "guard.json")})
        rt.cta.database = FakeDatabase(history_bars())
        rt.gw = rt.gateway
        created.append(rt)
        return rt

    yield factory
    for rt in created:
        rt.shutdown()


def test_runtime_loads_the_approved_strategy(make_runtime, approved, tmp_path):
    approved_dir, ledger = approved
    _install(approved_dir, ledger)
    rt = make_runtime({"approved_file": "cand.py", "approved_dir": approved_dir,
                       "approvals_file": ledger, "strategy_class": "ApprovedCandidate"})

    assert rt._load_approved_strategy() is None
    assert rt.cta.classes["ApprovedCandidate"].__name__ == "Candidate"


def test_runtime_refuses_to_start_when_the_approved_strategy_is_not_approved(make_runtime, approved):
    approved_dir, ledger = approved
    (approved_dir / "cand.py").write_text(CANDIDATE, encoding="utf-8")
    rt = make_runtime({"approved_file": "cand.py", "approved_dir": approved_dir,
                       "approvals_file": ledger})

    code = rt.start()

    assert code == EXIT_STARTUP_FAILED
    assert rt.strategy is None                      # 没有偷偷换成默认策略
    assert any(t.startswith("策略装载被拒") for t in rt.notifier.titles())


def test_runtime_without_an_approved_file_behaves_exactly_as_before(make_runtime):
    """没配 approved_file 时,行为与从前完全一致(不装载、不拒绝)。"""
    rt = make_runtime({})

    assert rt._load_approved_strategy() is None
    assert "ApprovedCandidate" not in rt.cta.classes
