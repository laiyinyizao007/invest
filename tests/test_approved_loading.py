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
import os
import subprocess
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

# 交易运行时装载用的是**实盘可用**的骨架(日线);
# 15m 骨架带 research_only 标记,装载时会被拒绝(见 test_research_only_skeleton_is_refused)。
ROOT = Path(__file__).resolve().parents[1]

LIVE_TEMPLATE = "strategies.trend_atr_strategy:TrendAtrStrategy"

CANDIDATE = '''from strategies.trend_atr_strategy import TrendAtrStrategy


class Candidate(TrendAtrStrategy):
    fast_window = 8
    slow_window = 20
    atr_mult = 2.0
'''


@pytest.fixture()
def approved(tmp_path):
    """一个临时的批准目录 + 账本。"""
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    # 账本放在与运行时一致的路径约定下:<root>/data/research-control/approvals.json
    ledger = tmp_path / "data" / "research-control" / "approvals.json"
    ledger.parent.mkdir(parents=True)
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


def _load(approved_dir, ledger, name="cand.py", setting=None, template_name=LIVE_TEMPLATE):
    cls, _sha = approved_guard.load_approved_strategy(
        approved_dir=approved_dir, approvals_file=ledger, file_name=name,
        class_name="Candidate", setting=setting, template_name=template_name)
    return cls


def test_an_approved_candidate_loads(approved):
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    cls = _load(approved_dir, ledger)

    assert cls.__name__ == "Candidate"
    from strategies.trend_atr_strategy import TrendAtrStrategy
    assert issubclass(cls, TrendAtrStrategy)


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
    rt = make_runtime({"approved_file": "cand.py", "approved_root": approved_dir.parent,
                       "approved_template": LIVE_TEMPLATE, "strategy_class": "ApprovedCandidate"})

    assert rt._load_approved_strategy() is None
    assert rt.cta.classes["ApprovedCandidate"].__name__ == "Candidate"


def test_runtime_refuses_to_start_when_the_approved_strategy_is_not_approved(make_runtime, approved):
    approved_dir, ledger = approved
    (approved_dir / "cand.py").write_text(CANDIDATE, encoding="utf-8")
    rt = make_runtime({"approved_file": "cand.py", "approved_root": approved_dir.parent,
                       "approved_template": LIVE_TEMPLATE})

    code = rt.start()

    assert code == EXIT_STARTUP_FAILED
    assert rt.strategy is None                      # 没有偷偷换成默认策略
    assert any(t.startswith("策略装载被拒") for t in rt.notifier.titles())


def test_runtime_without_an_approved_file_behaves_exactly_as_before(make_runtime):
    """没配 approved_file 时,行为与从前完全一致(不装载、不拒绝)。"""
    rt = make_runtime({})

    assert rt._load_approved_strategy() is None
    assert "ApprovedCandidate" not in rt.cta.classes


# ---------- 第 1 轮 codex 复核实测出的绕过(必须逐条钉住)----------

def test_the_fourth_layer_cannot_be_switched_off_by_the_candidate(approved):
    """候选不能把第四层(受保护对象的指纹比对)自己关掉。

    绕过手法:候选在自己的模块体里 `import risk.constitution as _c;
    _c.check_protected_state = lambda *a, **k: []`——如果装载器在 exec **之后**
    才经 `const.` 去取比较器,取到的就是候选换上去的那个空函数,第四层静默失效。
    修法:比较器与快照器在 exec 之前就绑定成局部引用,并在 exec 之后核对它们没被换掉。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "import risk.constitution as _c\n"
        "_c.check_protected_state = lambda *a, **k: []\n"
        "_c.snapshot_protected_state = lambda *a, **k: {}\n"
        "\n"
        "class Candidate:\n"
        "    pass\n"))

    # ⚠️ 这个载荷会把**本进程**里的 risk.constitution.check_protected_state
    # 换成空函数(拒绝发生在比对之后,替换发生在 exec 期间)。
    # 留着会污染后面所有依赖真第四层的用例(全套跑时表现为几条指纹测试莫名失败)——
    # 所以这条在**子进程**里跑。
    probe = """
import sys
sys.path.insert(0, r'%s')
from risk import approved_guard
from pathlib import Path
try:
    approved_guard.load_approved_strategy(
        approved_dir=Path(r'%s'), approvals_file=Path(r'%s'), file_name='cand.py',
        class_name='Candidate',
        template_name='strategies.trend_atr_strategy:TrendAtrStrategy')
except approved_guard.ApprovedLoadError as exc:
    print('REFUSED:' + str(exc))
    sys.exit(0)
print('NOT_REFUSED')
sys.exit(1)
""" % (str(ROOT), str(approved_dir), str(ledger))
    # 子进程要显式给 UTF-8:控制台代码页是 cp1252,子进程打印中文会 UnicodeEncodeError
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert "REFUSED" in proc.stdout, (proc.stdout, proc.stderr[-800:])
    # 拒绝理由必须指出「闸门被改写」——只断言"被拒"没有区分力:
    # 修复前的代码会以「不是策略骨架的子类」拒绝,断言照样成立。
    assert "闸门" in proc.stdout, proc.stdout


def test_a_candidate_that_tampers_with_a_risk_function_is_refused(approved):
    """端到端载荷:把 300 元单笔预算换成一个恒 0 的函数,再把第四层关掉。

    静态层对 `sys.modules[...]` + `setattr` 这条路**看不见**(实测 screen_source 返回 []),
    第四层是最后的兜底——所以这一条必须过。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "import sys\n"
        "_m = sys.modules['risk.risk_guard']\n"
        "setattr(_m, 'within_budget', lambda *a, **k: 0.0)\n"
        "import risk.constitution as _c\n"
        "_c.check_protected_state = lambda *a, **k: []\n"
        "\n"
        "from strategies.trend_atr_strategy import TrendAtrStrategy\n"
        "\n"
        "class Candidate(TrendAtrStrategy):\n"
        "    pass\n"))

    # 这个载荷在**被拒之前**就已经把进程里的 risk.risk_guard.within_budget 换掉了
    # (拒绝发生在比对之后,而改写发生在 exec 期间)。所以:
    #   ① 拒绝必须发生(这里是断言的重点);
    #   ② 污染不能留在本进程里——否则后面的测试都跑在一个被拆了风控的进程上。
    # 因此这一条在**子进程**里跑(与 codex 复核时的做法一致)。
    probe = """
import sys, types
sys.path.insert(0, r'%s')
from risk import approved_guard
from pathlib import Path
approved_dir = Path(r'%s')
ledger = Path(r'%s')
try:
    approved_guard.load_approved_strategy(
        approved_dir=approved_dir, approvals_file=ledger, file_name='cand.py',
        class_name='Candidate',
        template_name='strategies.trend_atr_strategy:TrendAtrStrategy')
except approved_guard.ApprovedLoadError as exc:
    print('REFUSED:' + str(exc))
    sys.exit(0)
print('NOT_REFUSED')
sys.exit(1)
""" % (str(ROOT), str(approved_dir), str(ledger))
    # 子进程要显式给 UTF-8:控制台代码页是 cp1252,子进程打印中文会 UnicodeEncodeError
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert "REFUSED" in proc.stdout, (proc.stdout, proc.stderr[-800:])
    # 拒绝理由必须指向「闸门被改写」或「受保护对象被改写」——
    # 只断言"被拒"没有区分力:载荷必须**真的是骨架子类**,
    # 否则「不是策略骨架的子类」会先一步拒绝,断言照样成立(第 2 轮 codex 复核指出)。
    assert ("闸门" in proc.stdout or "受保护对象" in proc.stdout), proc.stdout


def test_a_candidate_that_overrides_a_template_member_is_refused(approved):
    """类级校验(`check_class`):候选不得重写白名单以外的模板成员。

    静态层只按**名字**查黑名单,`on_daily_bar` / `variables` / `daily_end` 这些
    不在受保护名单里的模板成员,静态层看不见——那一层由 `check_class` 负责。
    研究侧有这层,运行侧原先漏了(第 1 轮 codex 复核发现)。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "from strategies.trend_atr_strategy import TrendAtrStrategy\n"
        "\n"
        "class Candidate(TrendAtrStrategy):\n"
        "    def on_daily_bar(self, bar):\n"
        "        return None\n"))

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger)
    assert "模板成员" in str(excinfo.value)


def test_the_research_only_skeleton_is_refused_in_the_trading_runtime(approved):
    """15m 骨架仅支持研究回测(on_tick 直接抛异常)→ 交易运行时装载时必须拒绝。

    否则策略会在实盘第一根 tick 上抛异常并被引擎静默停掉:
    失败方向安全(不下单),但错误信息极具误导性。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n"
        "\n"
        "class Candidate(TrendAtr15mStrategy):\n"
        "    pass\n"))

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger, template_name=None)
    assert "仅支持研究回测" in str(excinfo.value)


def test_a_setting_with_a_history_symbol_still_loads(approved):
    """`history_vt_symbol` 是操作参数(预热用哪个历史合约),不是风控底线。

    它在宪法的受保护参数表里,但运行时自己要从 `--history` 传它;
    若一并禁掉,实盘的正常配置(带历史预热)就永远装不上。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    cls = _load(approved_dir, ledger, setting={"history_vt_symbol": "CADJ.DCE",
                                              "fast_window": 10})
    assert cls.__name__ == "Candidate"


def test_a_setting_carrying_the_risk_floor_is_refused(approved):
    """风控底线参数仍由调用方指定 → 拒绝。"""
    approved_dir, ledger = approved
    _install(approved_dir, ledger)

    with pytest.raises(approved_guard.ApprovedLoadError) as excinfo:
        _load(approved_dir, ledger, setting={"risk_budget": 999999})
    assert "策略设置未通过校验" in str(excinfo.value)


def test_a_poisoned_process_refuses_every_further_load(approved):
    """被投毒之后,本进程不再允许装载任何候选(第 2 轮 codex 实测的 F1)。

    载荷 #1 关掉闸门并被拒;载荷 #2 才是真正的攻击。
    如果第二次装载绑到**已被换掉**的闸门上,第四层就死了。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "import risk.constitution as _c\n"
        "_c.check_protected_state = lambda *a, **k: []\n"
        "\n"
        "from strategies.trend_atr_strategy import TrendAtrStrategy\n"
        "\n"
        "class Candidate(TrendAtrStrategy):\n"
        "    pass\n"))
    probe = """
import sys
sys.path.insert(0, r'%s')
from risk import approved_guard
from pathlib import Path
approved_dir = Path(r'%s')
ledger = Path(r'%s')

def load(name):
    try:
        approved_guard.load_approved_strategy(
            approved_dir=approved_dir, approvals_file=ledger, file_name=name,
            class_name='Candidate',
            template_name='strategies.trend_atr_strategy:TrendAtrStrategy')
        return 'LOADED'
    except approved_guard.ApprovedLoadError as exc:
        return 'REFUSED:' + str(exc)

print('FIRST:' + load('cand.py'))
print('SECOND:' + load('cand.py'))
""" % (str(ROOT), str(approved_dir), str(ledger))
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    out = proc.stdout
    assert "FIRST:REFUSED" in out, (out, proc.stderr[-800:])
    assert "SECOND:REFUSED" in out, (out, proc.stderr[-800:])
    assert "NOT_LOADED" not in out


def test_a_candidate_cannot_exit_the_process_silently(approved):
    """候选在导入期 `raise SystemExit(0)` 不得让交易进程以**退出码 0** 结束。

    那会变成「正常停止」:`restart: on-failure` 不重启、监控看着像没事、且零告警。
    """
    approved_dir, ledger = approved
    _install(approved_dir, ledger, source=(
        "raise SystemExit(0)\n"
        "\n"
        "from strategies.trend_atr_strategy import TrendAtrStrategy\n"
        "\n"
        "class Candidate(TrendAtrStrategy):\n"
        "    pass\n"))
    probe = """
import sys
sys.path.insert(0, r'%s')
from risk import approved_guard
from pathlib import Path
try:
    approved_guard.load_approved_strategy(
        approved_dir=Path(r'%s'), approvals_file=Path(r'%s'), file_name='cand.py',
        class_name='Candidate',
        template_name='strategies.trend_atr_strategy:TrendAtrStrategy')
except approved_guard.ApprovedLoadError as exc:
    print('REFUSED:' + str(exc))
    sys.exit(0)
print('NOT_REFUSED')
sys.exit(1)
""" % (str(ROOT), str(approved_dir), str(ledger))
    proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert "REFUSED" in proc.stdout, (proc.stdout, proc.stderr[-800:])
    assert proc.returncode == 0, proc.returncode
