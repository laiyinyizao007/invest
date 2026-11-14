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


def inject_handoff(monkeypatch, handoff: dict) -> None:
    """把交接单按**真实路径**喂给子进程入口:走 stdin,不是改模块属性。

    为什么不用 `monkeypatch.setattr(api, "_child_handoff", ...)`:入口的引用是闭包里的
    (候选改写模块属性对写结果这条路不起作用),而且指纹里会比对入口本身 ——
    改模块属性会让框架**响亮地拒绝**,不是"注入成功"。
    喂 stdin 才是子进程拿到交接单的真实途径,测的就是这条路。"""
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(handoff) + "\n"))


def _utf8_env() -> dict:
    """子进程的 stderr 要能读出中文(Windows 默认 cp1252 会变成 ?)。"""
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


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
    inject_handoff(monkeypatch, handoff)
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
    handoff = {"path": str(result_path), "key": "k" * 64, "dir": str(tmp_path)}
    script = tmp_path / "child.py"
    script.write_text(
        "import atexit, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        "def sabotage():\n"
        "    Path(r'''%s''').write_text('FORGED', encoding='utf-8')\n"
        "atexit.register(sabotage)\n"
        "api.emit_result({'stats': {'sharpe': 1.0}})\n" % str(result_path),
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          input=json.dumps(handoff) + "\n")
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


def test_a_candidate_that_replaces_the_result_entry_point_cannot_hide_the_result(tmp_path):
    """候选把 `emit_result` 换成空函数 → 通道不许把这当成"写成功了"。

    第 30 轮 codex 实测:指纹只核对叶子对象(`json.dumps` 等),入口被整体替换时
    那段检查**根本不执行** —— 探针实测退出码 0、父进程照常采信。
    现在:调用方走导入时按住的引用(改写模块属性对这条路不起作用),
    指纹里也比对入口本身 → 被改写就**响亮地失败**(退出码 2),结果不写出去。"""
    scripts_dir = str(Path(api.__file__).resolve().parent)
    result_path = tmp_path / "child_result.json"
    handoff = {"path": str(result_path), "key": "k" * 64, "dir": str(tmp_path)}
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        "api.emit_result = lambda body: None        # 候选把入口换成空函数\n"
        "print('EXIT', api._emit_or_fail({'stats': {'sharpe': 42.0}}))\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          input=json.dumps(handoff) + "\n", env=_utf8_env())
    assert proc.returncode == 0, proc.stderr
    assert "EXIT 2" in proc.stdout                      # 拒绝(EXIT_REFUSED),不是"写成功了"
    assert "运行时对象被改写" in proc.stderr
    assert not result_path.exists()                    # 真结果没写出去


def test_a_candidate_that_replaces_the_handoff_reader_is_detected(tmp_path):
    """候选把"读交接单"这一步换掉 → 指纹比对入口本身 → **响亮地失败**。

    交接单里有结果文件的路径与 MAC 密钥。若候选能改写这一步,
    它就能让框架把结果写到它选的文件里(越出父进程指定的位置)。
    现在:指纹里比对了入口本身 → 被改写过就拒绝回传(退出码 2),结果不写出去。"""
    scripts_dir = str(Path(api.__file__).resolve().parent)
    result_path = tmp_path / "child_result.json"
    other_path = tmp_path / "elsewhere.json"
    handoff = {"path": str(result_path), "key": "k" * 64, "dir": str(tmp_path)}
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        f"api._child_handoff = lambda: {{'path': {str(other_path)!r}, 'key': 'k' * 64, 'dir': {str(tmp_path)!r}}}\n"
        "print('EXIT', api._emit_or_fail({'stats': {'sharpe': 1.0}}))\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          input=json.dumps(handoff) + "\n", env=_utf8_env())
    assert proc.returncode == 0, proc.stderr
    assert "EXIT 2" in proc.stdout
    assert "运行时对象被改写" in proc.stderr
    assert not result_path.exists() and not other_path.exists()


def test_a_candidate_that_rewrites_both_sides_of_the_fingerprint_is_still_caught(tmp_path):
    """双向(更狠的一版):候选把"被比的"与"期望的"**一起**改写 → 指纹照样逮到。

    第 32 轮 codex 实测:此前期望值(`_ORIGINAL_HANDOFF_READER` 等)是模块全局,
    候选把 `_child_handoff` 和它一起换成自己的,比对就成了"自己比自己",检查静音、
    结果照写(再配合劫持 `json.dumps`,框架会在伪造的 payload 上算出**真 MAC**)。
    现在期望值在闭包单元里,候选改不到 → 退出码 2,哪条路径上都没有结果。"""
    scripts_dir = str(Path(api.__file__).resolve().parent)
    result_path = tmp_path / "child_result.json"
    other_path = tmp_path / "elsewhere.json"
    handoff = {"path": str(result_path), "key": "k" * 64, "dir": str(tmp_path)}
    script = tmp_path / "child.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        f"fake = lambda: {{'path': {str(other_path)!r}, 'key': 'k' * 64, 'dir': {str(tmp_path)!r}}}\n"
        "api._child_handoff = fake\n"
        "api._ORIGINAL_HANDOFF_READER = fake      # 老版本里的期望侧(现已不存在,改写它没有任何作用)\n"
        "print('EXIT', api._emit_or_fail({'stats': {'sharpe': 1.0}}))\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          input=json.dumps(handoff) + "\n", env=_utf8_env())
    assert proc.returncode == 0, proc.stderr
    assert "EXIT 2" in proc.stdout
    assert "运行时对象被改写" in proc.stderr
    assert not result_path.exists() and not other_path.exists()


def test_a_candidate_that_hijacks_the_serializer_and_its_expected_value_cannot_forge_a_valid_mac(tmp_path):
    """候选劫持 `json.dumps` 并把(老版本里的)期望值指向它 → 不能得到"带真 MAC 的伪造结果"。"""
    scripts_dir = str(Path(api.__file__).resolve().parent)
    result_path = tmp_path / "child_result.json"
    handoff = {"path": str(result_path), "key": "k" * 64, "dir": str(tmp_path)}
    script = tmp_path / "child.py"
    script.write_text(
        "import json, sys\n"
        f"sys.path.insert(0, {scripts_dir!r})\n"
        "import research_api as api\n"
        "real = json.dumps\n"
        "def evil(obj, *a, **k):\n"
        "    if isinstance(obj, dict) and 'stats' in obj:\n"
        "        obj = {'stats': {'sharpe': 42.0}}\n"
        "    return real(obj, *a, **k)\n"
        "json.dumps = evil\n"
        "api._JSON_DUMPS = evil\n"
        "print('EXIT', api._emit_or_fail({'stats': {'sharpe': 1.0}}))\n",
        encoding="utf-8")

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          input=json.dumps(handoff) + "\n", env=_utf8_env())
    assert proc.returncode == 0, proc.stderr
    assert "EXIT 2" in proc.stdout
    assert "运行时对象被改写" in proc.stderr
    assert not result_path.exists()


# ---------------- 第 31 轮:子进程中途死掉,格子数按保守值记账 ----------------

def test_a_child_that_dies_mid_way_still_charges_the_declared_grid(tree, monkeypatch):
    """子进程中途死掉(超时/崩溃/被杀)→ 没人回报扫过多少格子 → 按**保守值**补记。

    此前这条路上补记完全不发生:账本只留预扣的 27 格,而那一轮最多已扫 64 格,
    少记 37 次试验 = DSR 的 N 偏小 = **悄悄放松**已锁定的 DSR ≥ 0.95(ADR 第 9 条)。"""
    make_strategy(tree, "class S:\n"
                        "    param_grid = {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4], 'c': [1, 2, 3, 4]}\n")

    def die(cmd, timeout):
        raise api.JobError(api.EXIT_TIMEOUT, "子进程超时")

    monkeypatch.setattr(api, "spawn_child", die)

    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == 64      # 27 预扣 + 37 补记


def test_a_dead_child_with_a_grid_the_parent_cannot_read_charges_the_configured_maximum(tree, monkeypatch):
    """动态网格(静态读不出格子数)→ 按配置上限记,取保守方向(宁可多记,不可少记)。"""
    make_strategy(tree, "class S:\n    param_grid = dict(a=[1, 2, 3])\n")

    def die(cmd, timeout):
        raise api.JobError(api.EXIT_TIMEOUT, "子进程超时")

    monkeypatch.setattr(api, "spawn_child", die)

    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == api.settings()[0]["grid"]["max_cells"]


def test_a_dead_child_with_a_small_grid_still_costs_the_minimum(tree, monkeypatch):
    """双向:声明的网格比最小网格还小 → 仍按最小网格 27 记(预扣的那份不退,不变成 8 格)。"""
    make_strategy(tree, "class S:\n    param_grid = {'a': [1, 2], 'b': [1, 2], 'c': [1, 2]}\n")

    def die(cmd, timeout):
        raise api.JobError(api.EXIT_TIMEOUT, "子进程超时")

    monkeypatch.setattr(api, "spawn_child", die)

    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == 27


def test_the_declared_grid_is_read_statically_from_the_source():
    """静态读格子数:只认字面量;读不出就说读不出(不猜)。"""
    assert api.declared_grid_cells("class S:\n    param_grid = {'a': [1, 2, 3], 'b': [1, 2], 'c': [1]}\n") == 6
    assert api.declared_grid_cells("class S:\n    param_grid = dict(a=[1, 2, 3])\n") is None
    assert api.declared_grid_cells("class S:\n    pass\n") == 0            # 没有网格 → 只有单一配置
    assert api.declared_grid_cells("class S:\n    param_grid = {'a': [1, 2, 3]}\n") == 3
    assert api.declared_grid_cells("param_grid = {'a': [1, 2, 3]}\n") is None   # 类外的网格读不准 → 保守


def test_an_annotated_grid_is_read_the_same_way_as_a_plain_one():
    """带注解的写法 `param_grid: dict = {...}` 也要读得出格子数。

    读不出 → 当成"没有网格" → 只记预扣的 27 格 → 少记试验次数 →
    DSR 的 N 偏小 = 悄悄放松已锁定的 DSR ≥ 0.95。"""
    assert api.declared_grid_cells("class S:\n    param_grid: dict = {'a': [1, 2, 3], 'b': [1, 2]}\n") == 6
    # 类外的带注解网格同样读不准 → 保守
    assert api.declared_grid_cells("param_grid: dict = {'a': [1, 2, 3]}\n") is None
    # 保守记账:带注解的 60 格 → 60(不是预扣的 27,也不是配置上限 343)
    assert api.conservative_grid_cells(
        "class S:\n    param_grid: dict = {'a': [1, 2, 3, 4], 'b': [1, 2, 3], 'c': [1, 2, 3, 4, 5]}\n", 343) == 60


def test_the_conservative_charge_never_goes_below_the_precharged_minimum():
    """双向:保守值不得低于预扣的最小网格(否则等于退还试验次数)。"""
    assert api.conservative_grid_cells("class S:\n    pass\n", 343) == 27
    assert api.conservative_grid_cells("class S:\n    param_grid = {'a': [1, 2]}\n", 343) == 27
    assert api.conservative_grid_cells("class S:\n    param_grid = {'a': [1, 2, 3, 4]}\n", 343) == 27
    assert api.conservative_grid_cells("class S:\n    param_grid = {'a': list(range(99))}\n", 343) == 343


# ---------------- 第 31 轮:拒绝原因只回传"框架自己写的"那一行 ----------------

def test_only_the_frameworks_own_lines_are_relayed_back_as_the_reason():
    """候选与框架同进程:候选 print 的内容不得被当成"框架的拒绝原因"传出去。

    以前父层取 stderr 的最后一行非空输出 —— 候选只要在被拒绝之后打印一行,
    就能顶替框架的结论;也可以把验证窗口的数字塞进 stderr,让那串数字
    被当成框架的话传回调用方/LLM。"""
    # 框架自己写的那行会被回传
    assert api._child_reason("[框架] 审批缺失") == "审批缺失"
    # 候选自己写的一行(哪怕只有这一行)不被回传
    assert api._child_reason("close=2728.0 是验证窗口的收盘价") == "子进程拒绝执行(原因见本机日志)"
    # 候选写的那行排在框架的行**后面**也顶替不了
    assert api._child_reason("[框架] 审批缺失\nclose=2728.0 是验证窗口的收盘价") == "审批缺失"
    # 候选那行排在前面也一样:回传的是框架的行,验证窗口的数字不出现在回传内容里
    assert "2728.0" not in api._child_reason("close=2728.0 是验证窗口的收盘价\n[框架] 审批缺失")


def test_a_candidate_cannot_smuggle_a_fake_reason_out_through_stderr(monkeypatch):
    """端到端:子进程以"拒绝"退出,但 stderr 里写的是候选自己的话 → 回传的消息不含它。"""
    stderr = "close=2728.0 是验证窗口的收盘价\n"
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=None, returncode=api.EXIT_REFUSED,
                                                            stderr=stderr))
    with pytest.raises(api.JobError) as excinfo:
        api.spawn_child(["python", "-c", "pass"], 10)
    assert "2728.0" not in excinfo.value.message
    assert "拒绝执行" in excinfo.value.message


def test_the_frameworks_own_refusal_reason_is_still_relayed(monkeypatch):
    """双向:框架写的拒绝原因照旧原样传到父层(标记只是过滤手段,不是把诊断掐了)。"""
    stderr = "[框架] 策略代码尚未批准\n"
    fake_subprocess(monkeypatch, lambda *a, **k: FakeChild(body=None, returncode=api.EXIT_REFUSED,
                                                            stderr=stderr))
    with pytest.raises(api.JobError) as excinfo:
        api.spawn_child(["python", "-c", "pass"], 10)
    assert "策略代码尚未批准" in excinfo.value.message


# ---------------- 第 31 轮:结果里该有上界的数字,都要有上界 ----------------

def test_an_out_of_range_sharpe_in_the_selection_summary_is_refused():
    """选参窗口汇总里的 best_sharpe / best_smoothed 也是夏普 → 一样要卡上界。

    漏了这个形状,就等于给"结果形状是 selection 的那类作业"留了一个后门。"""
    with pytest.raises(api.JobError) as excinfo:
        api.check_result_bounds({"selection": {"best_sharpe": 1e9}})
    assert "best_sharpe" in excinfo.value.message

    with pytest.raises(api.JobError):
        api.check_result_bounds({"selection": {"best_smoothed": 1e9}})


def test_a_normal_selection_summary_is_still_accepted():
    """双向:正常的选参汇总照常采信(别把正常结果也拒了)。"""
    api.check_result_bounds({"selection": {"best_sharpe": 1.8, "best_smoothed": 1.5,
                                           "best_cell": [0, 1], "on_plateau": False}})


def test_a_non_finite_number_in_the_stats_is_refused():
    """stats 里的非夏普数字(回撤额、成交笔数)只查"是不是有限数"。

    NaN/Inf 会顺着下游计算传下去(回撤、夏普、诊断);但回撤额本来就可能远大于
    夏普上界,拿夏普上界去卡它会误杀正常结果。"""
    with pytest.raises(api.JobError) as excinfo:
        api.check_result_bounds({"stats": {"sharpe": 1.2, "max_drawdown_yuan": float("nan")}})
    assert "max_drawdown_yuan" in excinfo.value.message

    api.check_result_bounds({"stats": {"sharpe": 1.2, "max_drawdown_yuan": 987654.0,
                                       "round_trips": 1234}})


def test_a_non_finite_or_over_long_pnl_list_is_refused():
    """逐笔盈亏:每一笔都得是有限数字;条数有上限。"""
    with pytest.raises(api.JobError):
        api.check_result_bounds({"stats": {"sharpe": 1.0}, "pnls": [1.0, float("inf")]})
    with pytest.raises(api.JobError):
        api.check_result_bounds({"stats": {"sharpe": 1.0}, "pnls": ["1.0"]})
    with pytest.raises(api.JobError):
        api.check_result_bounds({"stats": {"sharpe": 1.0}, "pnls": {"a": 1}})
    api.check_result_bounds({"stats": {"sharpe": 1.0}, "pnls": [1.0, -2.5, 0.0]})


def test_a_child_that_refused_before_scanning_is_not_charged_for_cells_it_never_scanned(tree, monkeypatch):
    """双向:子进程**拒绝**(退出码 2)时它一个格子都没扫 → 不按网格补记。

    拒绝发生在子进程自己的检查点(路径/审批/参数/导入)上,那时网格还没开始扫。
    按网格补记就是凭空多记试验次数 —— DSR 被算得更严,严到把正常候选也挡掉。
    但预扣的那 27 格**不退**:失败不变成免费探索(ADR 第 9 条)。"""
    make_strategy(tree, "class S:\n"
                        "    param_grid = {'a': [1, 2, 3, 4], 'b': [1, 2, 3, 4], 'c': [1, 2, 3, 4]}\n")

    def refuse(cmd, timeout):
        raise api.JobError(api.EXIT_REFUSED, "策略代码尚未批准")

    monkeypatch.setattr(api, "spawn_child", refuse)

    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    from risk.trial_ledger import total_trials
    assert total_trials(api.trials_path(api.settings()[1])) == 27      # 只剩预扣的那份


# ---------------- 第 32 轮:入口自身可被改写、拒绝原因的来源与完整性 ----------------

def test_a_candidate_that_replaces_the_call_site_name_cannot_displace_the_real_entry(tree, monkeypatch, capsys):
    """候选把**调用点真正用的那个名字**换掉 → 指纹检查必须照样执行(响亮地失败)。

    第 32 轮 codex 实测:此前子进程入口的调用点查的是**模块全局** `_EMIT_OR_FAIL`,
    候选 `api._EMIT_OR_FAIL = 假函数` 之后:
      - 整段指纹检查**根本不执行**(指纹在 writer 体内,假函数压根不调它);
      - 父进程照常采信假结果,退出码 0。
    现在调用点走**闭包单元**里导入时捕获的那一份 → 指纹照跑 → 退出码 2,不写结果。"""
    body = ("import research_api\n"
            "research_api._EMIT_OR_FAIL = lambda body: 0\n"
            "from vnpy_ctastrategy import CtaTemplate\n"
            "class S(CtaTemplate):\n"
            "    pass\n")
    path = make_strategy(tree, body)
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    approve(path)

    handoff = {"path": str(tree / "child_result.json"), "key": "k" * 64, "dir": str(tree)}
    import io
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(handoff) + "\n"))
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    # 候选在**本进程**里改写了框架模块的属性(子进程里改的是它自己那份)——
    # 测完必须还原,否则后面的用例全被这一次改写污染。
    original = api._EMIT_OR_FAIL
    try:
        code = api.run_evaluate_child(args)
    finally:
        api._EMIT_OR_FAIL = original

    # 入口走闭包单元里的那一份 → 候选改模块全局**不起任何作用**:框架照常把自己的结果写出去
    # (旧写法下,假函数顶替了入口、什么都没写,退出码 0、父进程拿不到结果也不报警)。
    assert code == api.EXIT_OK, capsys.readouterr().err
    result_file = tree / "child_result.json"
    assert result_file.exists(), "框架自己的结果必须写出去(没写 = 入口被候选顶替了)"
    envelope = json.loads(result_file.read_text(encoding="utf-8"))
    assert set(envelope) == {"mac", "body"}, "写出去的必须是带 MAC 的信封"


def test_the_frameworks_own_reason_wins_over_a_candidate_line_printed_at_exit():
    """候选在**进程退出时**(框架写完原因之后)打一行带同样标记的文本 → 框架那行赢。

    此前父层取的是**最后**一行带标记的行,候选的 atexit 钩子排在后面 → 候选那行被回传,
    顶掉了框架自己的结论。atexit 不在沙盒的禁用名单里,这条路径是通的。"""
    stderr = ("[框架] NoSuchClass 不存在或不是 CtaTemplate 子类\n"
              "[框架] close=2728.0 是验证窗口的收盘价\n")
    reason = api._child_reason(stderr)
    assert "NoSuchClass" in reason
    assert "2728.0" not in reason, "候选在退出时打的那一行不得顶替框架的结论"


def test_a_multiline_refusal_reason_is_relayed_in_full(capsys):
    """多行的拒绝原因要**完整传到父层**(折成一行写),否则细节(是哪一条)全丢。

    多行消息的后续行没有标记,父层只剩首行:框架写"静态筛查未通过:\n  第 1 行: 禁止导入 os",
    父层只回传'静态筛查未通过:'——被丢掉的正是**是哪一条**。"""
    api.say_refusal("静态筛查未通过:\n  第 1 行: 禁止导入 os\n  第 2 行: 禁止导入 sys")
    err = capsys.readouterr().err
    reason = api._child_reason(err)
    assert "静态筛查未通过" in reason
    assert "禁止导入 os" in reason
    assert "禁止导入 sys" in reason


def test_a_refusal_written_by_the_refuse_helper_is_relayed_too(capsys):
    """`refuse()` 此前写的是**无标记**的"拒绝: ..." → 父层只认带标记的行 → 诊断全丢。

    子进程侧会走到它(配置非法 → main() 的 ConfigError 分支),实测回传退化成
    "子进程拒绝执行(原因见本机日志)"。"""
    api.refuse("配置非法,拒绝运行: grid.max_cells=0 超出允许范围")
    err = capsys.readouterr().err
    assert api.FRAMEWORK_TAG in err, "拒绝原因必须带框架标记,否则父层不认"
    reason = api._child_reason(err)
    assert "配置非法" in reason


def test_a_grid_declared_with_a_tuple_target_is_not_treated_as_no_grid():
    """`param_grid, x = {...}, 1` 里的 param_grid 也是网格声明。

    目标名要递归取:只看最外层会以为"没有 param_grid" → 当成没网格 → 只记 27 格
    → 少记试验次数 = 悄悄放松已锁定的 DSR ≥ 0.95(第 32 轮 codex 的 Low-2)。"""
    assert api.declared_grid_cells("class S:\n    param_grid, x = {'a': [1, 2]}, 1\n") is None
    assert api.conservative_grid_cells("class S:\n    param_grid, x = {'a': [1, 2]}, 1\n", 343) == 343


# ---------------- 第 32 轮(复审):静态读网格 fail-closed ----------------

@pytest.mark.parametrize("source", [
    # 类外重绑:目标是 Attribute,旧实现连 unknown 都不置位 → 只认到类体里那个 3 格的小网格
    "class S:\n    param_grid = {'a': [1, 2, 3]}\nS.param_grid = {'a': [1]*7, 'b': [1]*7, 'c': [1]*7}\n",
    # 增强赋值
    "class S:\n    param_grid = {'a': [1, 2, 3]}\n    param_grid |= {'b': [1]*10, 'c': [1]*10}\n",
    # 原地更新
    "class S:\n    param_grid = {'a': [1, 2, 3]}\n    param_grid.update({'b': [1]*10})\n",
    # 字符串键
    "GRID = {'param_grid': {'a': [1]}}\nclass S:\n    pass\n",
    # 只是读:保守(更严),不是少记
    "class S:\n    param_grid = {'a': [1, 2, 3]}\n    def f(self):\n        return self.param_grid\n",
])
def test_any_other_mention_of_the_grid_makes_the_declared_size_unreadable(source):
    """`param_grid` 的出现次数必须正好等于被认出的字面量赋值次数,多一次就当"读不出"。

    第 32 轮 codex 复审实测:声明小网格、再用类外重绑/`|=`/`.update()` 塞进大网格,
    子进程中途死掉时账本只记 27 格(真实 343),少记 316 次试验 = 悄悄放松 DSR ≥ 0.95。"""
    assert api.declared_grid_cells(source) is None
    assert api.conservative_grid_cells(source, 343) == 343


def test_a_single_literal_grid_is_still_read_exactly():
    """双向:兜底不能把正常写法误伤成"读不出"。"""
    assert api.declared_grid_cells("class S:\n    param_grid = {'a': [1, 2, 3]}\n") == 3
    assert api.declared_grid_cells("class S:\n    param_grid: dict = {'a': [1, 2, 3]}\n") == 3
    assert api.declared_grid_cells("class S:\n    pass\n") == 0


def test_the_child_entries_really_close_over_the_emit_function():
    """闭包性质要被钉住:入口是壳,`emit` 必须是闭包单元,且就是导入时生成的那一份。

    第 32 轮 codex 复审:变异"入口改回查模块全局"存活 —— 旧的静态用例只断言
    `handler in entry.__closure__`,变异后闭包里只剩 handler,照样通过。"""
    for name in ("run_child", "run_evaluate_child", "run_gate_child"):
        entry = getattr(api, name)
        assert "emit" in entry.__code__.co_freevars, f"{name} 没有把 emit 放进闭包单元"
        cells = dict(zip(entry.__code__.co_freevars, (c.cell_contents for c in entry.__closure__)))
        assert cells["emit"] is api._EMIT_OR_FAIL, f"{name} 闭包里的 emit 不是导入时生成的那一份"
