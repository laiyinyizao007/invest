"""子进程结果通道与阶段归属(第 27 轮 codex 的 Critical / Medium-2)。

纪律:每条规则配**双向**动态探针——
- 一条证明拦得住,并且断言**是哪一条规则拦的**(错误文案);
- 一条证明正常路径放行。
再加**变异验证**:把修复改回旧行为,对应的用例必须每次都变红(不是靠时钟运气)。
"""
import hashlib
import hmac
import json
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

import optimize  # noqa: F401  预先导入,避免在补丁生效期间触发 vnpy 的惰性导入
import research_api as api
from risk import outward


# ---------------- 假子进程 ----------------

def fake_subprocess(monkeypatch, popen):
    monkeypatch.setattr(api, "subprocess", SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired, run=subprocess.run))


def write_child_result(handoff: dict, body: dict, *, mac: str | None = None) -> None:
    """模拟子进程:把结果写进父进程给的随机文件,并附上 MAC(mac=None 时按 body 现算)。"""
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    if mac is None:
        mac = hmac.new(handoff["key"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    path = Path(handoff["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mac": mac, "body": payload}, ensure_ascii=False), encoding="utf-8")


class FakeChild:
    """假子进程:把父进程交给它的交接单(经 stdin)原样交回测试用例处理。"""

    def __init__(self, body=None, returncode=0, stdout="", stderr="", on_handoff=None):
        self.body = body
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.on_handoff = on_handoff      # 回调(handoff):在这里写结果文件/篡改
        self.handoff = None

    def communicate(self, timeout=None, input=None):
        if input:
            self.handoff = json.loads(input)
            if self.on_handoff is not None:
                self.on_handoff(self.handoff)
            elif self.body is not None:
                write_child_result(self.handoff, self.body)
        return self.stdout, self.stderr


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    monkeypatch.setattr(api.bootstrap, "ROOT", tmp_path)
    return tmp_path


def make_strategy(tree, body="class S: pass\n"):
    path = tree / "pending_strategies" / "s.py"
    path.write_text(body, encoding="utf-8")
    return path


def approve(path):
    api.rs.record_approval(path, api.settings()[1]["approvals_file"])


# ---------------- Critical:结果不能由候选在 stdout 上伪造 ----------------

def test_a_forged_result_line_on_stdout_is_never_taken_as_the_result(tree, monkeypatch, capsys):
    """候选在 stdout 上打一行伪造的结果(老协议)→ 父进程**不许**采信它。

    第 27 轮 codex 的 Critical:老实现取 stdout 里**最后**一行 `RESEARCH_RESULT_JSON:`,
    候选注册一个 atexit 钩子(atexit 不在沙盒的禁用模块名单里)在框架打印之后再打一行,那一行就会赢。
    现在 stdout 只是日志,结果走**父进程建的随机文件 + MAC**。"""
    make_strategy(tree)
    forged = {"stats": {"sharpe": 999.0}, "pnls": [1, 1, 1]}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(
        body=None, returncode=0, stdout="RESEARCH_RESULT_JSON:" + json.dumps(forged) + "\n"))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    err = capsys.readouterr().err
    assert code == api.EXIT_FAILED
    # 断言**是哪一条规则**拒的:结果文件缺失,而不是"碰巧失败"
    assert "没有回传结果" in err
    assert "999" not in err


def test_the_authenticated_file_wins_over_a_forged_stdout_line(tree, monkeypatch, capsys):
    """双向:stdout 上有伪造的一行、文件里有真结果 → 采信的是**文件里**那个。"""
    make_strategy(tree)
    real = {"stats": {"sharpe": 1.5, "round_trips": 3}, "pnls": [1, -1, 1]}

    def on_handoff(handoff):
        write_child_result(handoff, real)

    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(
        body=None, returncode=0, stdout="RESEARCH_RESULT_JSON:" + json.dumps({"stats": {"sharpe": 999.0}}) + "\n",
        on_handoff=on_handoff))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    out = json.loads(capsys.readouterr().out)
    assert code == api.EXIT_OK
    assert out["stats"]["sharpe"] == 1.5


def test_a_result_file_whose_signature_does_not_match_is_refused(tree, monkeypatch, capsys):
    """结果文件被换过(正文改了、签名没改)→ 拒绝采信,并说清是哪一条规则。"""
    make_strategy(tree)

    def on_handoff(handoff):
        write_child_result(handoff, {"stats": {"sharpe": 1.0}}, mac="0" * 64)

    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=None, returncode=0, on_handoff=on_handoff))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    err = capsys.readouterr().err
    assert code == api.EXIT_FAILED
    assert "校验失败" in err and "伪造" in err


def test_a_correctly_signed_result_is_accepted(tree, monkeypatch, capsys):
    """双向:签名正确 → 照常采信(这一条不许被"更严"误伤)。"""
    make_strategy(tree)
    body = {"stats": {"sharpe": 1.0, "round_trips": 3}, "pnls": [1, -1, 1]}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    out = json.loads(capsys.readouterr().out)
    assert code == api.EXIT_OK and out["stats"]["sharpe"] == 1.0


def test_a_missing_result_file_is_refused(tree, monkeypatch, capsys):
    """子进程退出码 0 却没写结果 → 拒绝(老实现是"没有那一行就失败",现在同理但说的是文件)。"""
    make_strategy(tree)
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=None, returncode=0, stdout="日志而已\n"))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    assert code == api.EXIT_FAILED and "没有回传结果" in capsys.readouterr().err


def test_the_handoff_is_not_in_the_child_environment(tree, monkeypatch):
    """交接单(随机路径 + 密钥)走 **stdin**,不进环境变量。

    环境变量会原样进子进程,而候选能通过反射摸到运行时;stdin 在候选执行期间还躺在管道里。"""
    make_strategy(tree)
    seen = {}

    def fake_popen(cmd, **kw):
        seen["env"] = kw["env"]
        return FakeChild(body={"stats": {"sharpe": 1.0, "round_trips": 1}, "pnls": []})

    fake_subprocess(monkeypatch, fake_popen)
    api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
              "--segment", "train", "--confirm-reviewed"])

    assert "handoff" not in seen["env"]
    assert not any("research-result-" in str(v) for v in seen["env"].values())


def test_an_out_of_range_sharpe_in_the_result_is_refused(tree, monkeypatch, capsys):
    """结果里的夏普站不住(比如被编排成 1e9)→ 拒绝采信,报出是哪一条。"""
    make_strategy(tree)
    body = {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 27},
            "metrics": {"oos_sharpe": 1e9}}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["gate", "--strategy", "s.py", "--class", "S", "--params", "{}",
                     "--random-runs", "20", "--confirm-reviewed"])

    err = capsys.readouterr().err
    assert code == api.EXIT_FAILED
    assert "超出合理范围" in err


def test_a_normal_sharpe_still_passes_through(tree, monkeypatch, capsys):
    """双向:正常区间的夏普照常采信(别把门槛外的正常值也拒了)。"""
    make_strategy(tree)
    body = {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 27},
            "metrics": {"oos_sharpe": 1.5, "stress_sharpe": 0.9}}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["gate", "--strategy", "s.py", "--class", "S", "--params", "{}",
                     "--random-runs", "20", "--confirm-reviewed"])

    out = json.loads(capsys.readouterr().out)
    assert code == api.EXIT_OK and out["metrics"]["oos_sharpe"] == 1.5


# ---------------- Medium-2:阶段只由框架决定 ----------------

class GridError(Exception):
    """候选自己起的异常类名(与框架的 research_grid.GridError **同名**)。

    这正是攻击面:类名是候选自己起的,老实现按名字兜底 `GridError ⇒ declaration`。"""


def test_a_candidate_named_grid_error_in_the_validation_window_keeps_the_message_closed(tree, monkeypatch, capsys):
    """候选自己起名叫 `GridError` 的异常,在验证窗口回放里报错 → 原文**不外传**。

    第 27 轮 codex 的 Medium-2:老实现按**异常类名**兜底 `GridError ⇒ declaration`,
    而类名是候选自己起的——于是验证窗口的数字被当成"我们自己写的声明期消息"放出去
    (实测 view.error.phase == "declaration",消息里带着验证窗口的收盘价)。"""
    path = make_strategy(tree, "from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    VALUE = 'GOOD'\n")
    approve(path)

    wf = types.ModuleType("walk_forward_optimizer")
    wf.DATA_FILE = "x.csv"
    wf.SYMBOL = "c2701.DCE"
    wf.KEY = "CADJ"

    class Win:
        name = "w"
        train_start = 1
        train_end = 2
        valid_start = 3
        valid_end = 4

    wf.build_windows = lambda: [Win()]
    bl = types.ModuleType("backtest_lib")
    bl.DEFAULT_CAPITAL = 100000.0
    bl.load_bars_csv = lambda *a, **k: []

    def raising_run_evaluation(**kw):
        raise GridError("close=2728.0 是验证窗口的收盘价")

    rg = types.ModuleType("research_gate")
    rg.engine_runner = lambda key: (lambda *a, **k: None)
    rg.run_evaluation = raising_run_evaluation
    rnd = types.ModuleType("strategies.random_entry_15m_strategy")
    rnd.RandomEntry15mStrategy = object
    for name, mod in (("walk_forward_optimizer", wf), ("backtest_lib", bl), ("research_gate", rg),
                      ("strategies.random_entry_15m_strategy", rnd)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)

    handoff = {"path": str(tree / "child_result.json"), "key": "k" * 64, "dir": str(tree)}
    monkeypatch.setattr(api, "_child_handoff", lambda: handoff)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code = api.run_evaluate_child(args)

    payload = json.loads(json.loads(Path(handoff["path"]).read_text(encoding="utf-8"))["body"])
    assert code == api.EXIT_OK
    assert payload["error"]["phase"] == "validation"

    # 对外揭示层:验证窗口的原文不放行,阶段也不是 declaration
    result = {"kind": "evaluate", "run_id": "r1", "class": "S", "status": "strategy_error",
              "error": payload["error"]}
    view = outward.build_outward(result)
    assert view["error"]["message"] is None
    assert view["error"]["phase"] != "declaration"


def test_the_phase_constants_are_the_same_in_both_modules():
    """阶段常量只有一处定义(另一处 import):两处不一致会让"阶段只由框架决定"变成空话。"""
    import research_gate
    assert api.PHASES == (research_gate.PHASE_DECLARATION, research_gate.PHASE_SELECTION, research_gate.PHASE_VALIDATION)


# ---------------- ADR 第 9 条:失败的一轮也要按扫过的格子记账 ----------------

def test_a_failed_evaluation_still_charges_the_cells_it_scanned(tree, monkeypatch):
    """扫了 100 个格子却在验证期失败 → 账本按 100 记,不是按最小网格 27 记。

    少记格子数 = DSR 的试验次数少算 = 悄悄放松已锁定的 DSR ≥ 0.95(ADR 第 9 条)。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"error": {"type": "ValueError", "message": "x", "phase": "validation"},
                         "trials": {"prior": 0, "grid_cells": 100, "total": 100, "cell_sharpes": []}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == 100


def test_a_failed_evaluation_without_cell_information_still_costs_the_minimum(tree, monkeypatch):
    """双向:子进程没报格子数(老协议的错误结果)→ 仍按最小网格 27 记,不让失败变成免费探索。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"error": {"type": "ValueError", "message": "x", "phase": "validation"}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == 27


def test_the_selection_crash_path_reports_the_declared_cell_count():
    """选参窗口里崩掉的一轮:按**声明的格子数**记(最坏情况),并带上 trials 字段。"""
    import research_gate

    class Candidate:
        param_grid = {"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3, 4]}

    def runner(*a, **k):
        raise RuntimeError("回测炸了")

    result = research_gate.run_evaluation(
        candidate_cls=Candidate, bars=[], runner=runner, random_cls=object, random_runs=1, capital=100000.0,
        max_cells=1000, min_round_trips=1, select_window=(None, None), validation_windows=[],
        prior_trials=5)

    assert result["trials"]["grid_cells"] == 36          # 3×3×4
    assert result["trials"]["total"] == 41
    assert result["error"]["phase"] == "selection"


def test_the_validation_failure_path_still_reports_the_cells():
    """选参成功、验证期失败 → 扫过的格子数照样记账(否则 DSR 少算试验)。"""
    import research_gate

    class Candidate:
        param_grid = {"a": [1, 2, 3], "b": [1, 2, 3], "c": [1, 2, 3]}

    def runner(strategy_class, params, chunk, cost):
        return {"daily_pnls": [1.0, -1.0], "round_trips": 5, "maxdd_pct": -1.0, "maxdd_yuan": -1.0}

    calls = {"n": 0}

    def flaky_runner(strategy_class, params, chunk, cost):
        calls["n"] += 1
        if calls["n"] > 27:            # 网格扫完(27 格)之后,验证期才炸
            raise RuntimeError("验证期炸了")
        return runner(strategy_class, params, chunk, cost)

    result = research_gate.run_evaluation(
        candidate_cls=Candidate, bars=[], runner=flaky_runner, random_cls=object, random_runs=1, capital=100000.0,
        max_cells=1000, min_round_trips=1, select_window=(None, None), validation_windows=[("w", None, None)],
        prior_trials=0)

    assert "error" in result
    assert result["trials"]["grid_cells"] == 27


def test_an_out_of_range_sharpe_in_a_backtest_result_is_refused(tree, monkeypatch, capsys):
    """回测作业的结果把夏普放在 `stats.sharpe`(第 27 轮 Critical 伪造的正是这个形状)——
    合理性上界不能只查 `metrics.oos_sharpe` 那一支,否则回测这条路留着一个口子。"""
    make_strategy(tree)
    body = {"stats": {"sharpe": 1e9, "round_trips": 3}, "pnls": [1, -1, 1]}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    err = capsys.readouterr().err
    assert code == api.EXIT_FAILED
    assert "超出合理范围" in err


def test_a_normal_backtest_sharpe_still_passes(tree, monkeypatch, capsys):
    """双向:回测结果的正常夏普照常采信(别把门槛外的正常值也拒了)。"""
    make_strategy(tree)
    body = {"stats": {"sharpe": 1.2, "round_trips": 3}, "pnls": [1, -1, 1]}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    out = json.loads(capsys.readouterr().out)
    assert code == api.EXIT_OK and out["stats"]["sharpe"] == 1.2


# ---------------- 第 30 轮:钩子清空要真的接上,结果文件要有大小上限 ----------------

def test_the_candidate_atexit_hook_cannot_rewrite_the_result_after_it_was_written(tmp_path):
    """候选注册的 atexit 钩子不许在**结果写完之后**再改那个文件。

    第 27 轮订的规矩是"写结果前清掉候选注册的 atexit 钩子",但那条规矩**只在文档里**:
    `_clear_atexit()` 定义了却从来没被调用过(死代码)。候选的钩子是在进程退出时跑的,
    于是它能在框架写完结果之后再改一次那个文件——改坏了会被父进程验签拒掉(fail-closed),
    但那是一次"结果明明跑完了却被判失败"的响亮失败,不是设计里说的那样。

    这条用例在**真子进程**里跑:在 pytest 进程里调 emit_result 会把 pytest 自己的
    atexit 钩子一起清掉(覆盖率统计等),那是副作用不是被测行为。"""
    scripts_dir = str(Path(api.__file__).resolve().parent)
    result_path = tmp_path / "child_result.json"
    script = tmp_path / "child.py"
    script.write_text(
        "import atexit, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        f"handoff = {{'path': {str(result_path)!r}, 'key': 'k' * 64, 'dir': {str(tmp_path)!r}}}\n"
        "api._child_handoff = lambda: handoff\n"
        "def sabotage():\n"
        f"    Path(handoff['path']).write_text('FORGED', encoding='utf-8')\n"
        "atexit.register(sabotage)\n"
        "api.emit_result({'stats': {'sharpe': 1.0}})\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    assert proc.returncode == 0, proc.stderr

    envelope = json.loads(result_path.read_text(encoding="utf-8"))
    assert json.loads(envelope["body"]) == {"stats": {"sharpe": 1.0}}


def test_an_oversized_result_file_is_refused(tree, monkeypatch, capsys):
    """候选回传一个超大的结果文件(比如百万条 pnl)→ 父进程**不把它整个读进内存**。

    沙盒不给子进程设资源上限,所以父进程侧必须有一道大小兜底:先看文件多大,超了直接拒。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "MAX_RESULT_BYTES", 1024)

    def on_handoff(handoff):
        write_child_result(handoff, {"stats": {"sharpe": 1.0}, "pnls": ["x" * 64] * 100})

    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=None, returncode=0, on_handoff=on_handoff))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    err = capsys.readouterr().err
    assert code == api.EXIT_FAILED
    assert "过大" in err


def test_the_result_size_cap_is_a_sane_number():
    """双向:上限本身得是个像样的数(别写成 0 或几十字节,把正常结果也拒了)。"""
    assert 1024 * 1024 <= api.MAX_RESULT_BYTES <= 64 * 1024 * 1024


def test_a_result_file_just_under_the_cap_is_still_read(tree, monkeypatch, capsys):
    """双向:没超上限的结果照常采信(别把正常结果也拒了)。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "MAX_RESULT_BYTES", 1024)
    body = {"stats": {"sharpe": 1.2, "round_trips": 3}, "pnls": [1, -1, 1]}
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=body))

    code = api.main(["backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ",
                     "--segment", "train", "--confirm-reviewed"])

    out = json.loads(capsys.readouterr().out)
    assert code == api.EXIT_OK and out["stats"]["sharpe"] == 1.2
