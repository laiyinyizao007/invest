"""研究沙盒 CLI:拒绝路径与输出契约(真实回测由人工在有数据的机器上验证)。"""
import hashlib
import hmac
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import research_api as api

# 这些用例要**真的载入候选类**(候选继承 vnpy_ctastrategy 的 CtaTemplate)→ 需要交易栈。
# CI 的研究侧闸门刻意不装交易环境(研究侧与交易栈必须解耦),所以那里**跳过**而不是失败:
# 缺依赖不等于行为不对,红着反而会把闸门变成噪声。
def _trading_stack_missing() -> bool:
    try:
        return importlib.util.find_spec("vnpy_ctastrategy") is None
    except (ImportError, ValueError):
        return True


requires_trading_stack = pytest.mark.skipif(
    _trading_stack_missing(),
    reason="需要交易栈(vnpy_ctastrategy):CI 的研究侧闸门不装交易环境")




def fake_subprocess(monkeypatch, popen):
    # 只替换 research_api 看到的 subprocess,不碰全局模块(vnpy/asyncio 导入期间也会用到 Popen)
    monkeypatch.setattr(api, "subprocess", SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired, run=subprocess.run))


def write_child_result(handoff_line: str, body: dict) -> None:
    """模拟子进程:把结果写进父进程给的随机文件,并附上**正确**的 MAC。"""
    handoff = json.loads(handoff_line)
    payload = json.dumps(body, ensure_ascii=False, sort_keys=True)
    mac = hmac.new(handoff["key"].encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    path = Path(handoff["path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mac": mac, "body": payload}, ensure_ascii=False), encoding="utf-8")


class FakeChild:
    """假子进程:按父进程交给它的交接单把结果写回去(带正确的 MAC)。

    `stdout` 里的内容**只是日志**——父进程不再从 stdout 里取结果,这一点由用例断言。"""

    def __init__(self, body=None, returncode=0, stdout="", stderr="", tamper=None):
        self.body = body
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.tamper = tamper          # 可选:收到交接单后回调(handoff),用来伪造/篡改结果文件

    def communicate(self, timeout=None, input=None):
        if self.body is not None and input:
            write_child_result(input, self.body)
            if self.tamper is not None:
                self.tamper(json.loads(input))
        return self.stdout, self.stderr


def child_capture(monkeypatch, tmp_path, func, args):
    """在进程内跑一个子入口:给它一份交接单,再把写回的结果读出来。

    返回 (退出码, 结果字典)。子入口现在把结果写进**父进程建的随机文件**,不再打 stdout。
    交接单按**真实路径**喂给入口(走 stdin):入口的引用在闭包里,改模块属性改不动它,
    而且指纹会比对入口本身 —— 改模块属性只会让框架响亮地拒绝,不是"注入成功"。"""
    import io
    handoff = {"path": str(tmp_path / "child_result.json"), "key": "k" * 64, "dir": str(tmp_path)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(handoff) + "\n"))
    code = func(args)
    payload = {}
    path = Path(handoff["path"])
    if path.exists():
        payload = json.loads(json.loads(path.read_text(encoding="utf-8"))["body"])
    return code, payload


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    # 数据根(候选目录/批准记录)指到临时树:研究侧不再用 bootstrap.ROOT(那会拖进 vnpy 与 .env)。
    monkeypatch.setattr(api.rc, "ROOT", tmp_path)
    # 默认假装 stdin 是交互式终端:批准类动作要求「人在场」,pytest 里 stdin 是管道。
    monkeypatch.setattr(api.sys, "stdin", _FakeTTY())
    # `--confirm-reviewed` 现在也要**粘贴完整哈希**(TTY 只是「有个控制台」,不是「有人在」,
    # 第 7 轮 codex 的 F7):测试里默认「人」把正确的哈希粘回来。
    monkeypatch.setattr(api, "_prompt_hash", lambda sha: sha)
    return tmp_path


def run(*argv):
    return api.main(list(argv))


BT = ("backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ", "--segment", "train")


def test_backtest_requires_review_confirmation(tree, capsys):
    (tree / "pending_strategies" / "s.py").write_text("x = 1\n", encoding="utf-8")

    assert run(*BT) == api.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "confirm-reviewed" in err and "approve" in err


def test_backtest_refuses_test_segment_at_parser_level():
    with pytest.raises(SystemExit):
        run("backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ", "--segment", "test", "--confirm-reviewed")


def test_backtest_refuses_path_outside_pending(tree, capsys):
    (tree / "evil.py").write_text("x = 1\n", encoding="utf-8")

    code = run("backtest", "--strategy", "../evil.py", "--class", "S", "--key", "CADJ", "--segment", "train", "--confirm-reviewed")

    assert code == api.EXIT_REFUSED and "pending_strategies" in capsys.readouterr().err


def test_backtest_refuses_code_that_fails_static_screen_without_spawning(tree, monkeypatch, capsys):
    (tree / "pending_strategies" / "s.py").write_text("import os\nclass S: pass\n", encoding="utf-8")
    fake_subprocess(monkeypatch, lambda *a, **k: pytest.fail("筛查未过不得启动子进程"))

    code = run(*BT, "--confirm-reviewed")

    assert code == api.EXIT_REFUSED and "禁止导入 os" in capsys.readouterr().err


def test_backtest_refuses_bad_params(tree):
    (tree / "pending_strategies" / "s.py").write_text("x = 1\n", encoding="utf-8")

    assert run(*BT, "--params", "[1]", "--confirm-reviewed") == api.EXIT_REFUSED
    assert run(*BT, "--params", "{bad", "--confirm-reviewed") == api.EXIT_REFUSED


def test_backtest_child_gets_scrubbed_environment(tree, monkeypatch):
    (tree / "pending_strategies" / "s.py").write_text("class S: pass\n", encoding="utf-8")
    monkeypatch.setenv("CTP_PASSWORD", "secret")
    seen = {}

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None, input=None):
            return "", "boom"

    def fake_popen(cmd, **kw):
        seen["env"] = kw["env"]
        return FakeProc()

    fake_subprocess(monkeypatch, fake_popen)

    assert run(*BT, "--confirm-reviewed") == api.EXIT_FAILED
    assert "CTP_PASSWORD" not in seen["env"] and "PATH" in {k.upper() for k in seen["env"]}


def test_backtest_success_writes_result_with_diagnosis_and_hash(tree, monkeypatch, capsys):
    (tree / "pending_strategies" / "s.py").write_text("class S: pass\n", encoding="utf-8")
    stats = {"return_pct": 1.0, "maxdd_pct": -2.0, "maxdd_yuan": -200.0, "sharpe": 2.0, "round_trips": 200,
             "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 10.0}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None, input=None):
            write_child_result(input, {"stats": stats, "pnls": [1, -1, 1]})
            return "noise\n", ""

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())

    assert run("backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ", "--segment", "valid", "--confirm-reviewed") == api.EXIT_OK
    raw = capsys.readouterr().out
    out = json.loads(raw)
    # CLI 的 stdout 是模型可见通道的候选:**哈希不进 stdout**(第 7 轮 codex 的 F2),
    # 磁盘上的结果文件里仍然记着(那是内部账本)。
    assert "strategy_sha256" not in out and out["segment"] == "valid"
    assert not re.search(r"[0-9a-fA-F]{64}", raw)
    assert out["diagnosis"]["dimensions"]["sharpe"]["level"] == "ok"
    assert (tree / "data" / "research" / f"{out['run_id']}.json").exists()
    assert run("results", out["run_id"]) == api.EXIT_OK
    assert json.loads(capsys.readouterr().out)["run_id"] == out["run_id"]


def test_results_lists_and_rejects_unsafe_id(tree, capsys):
    assert run("results") == api.EXIT_OK and json.loads(capsys.readouterr().out) == []
    assert run("results", "../x") == api.EXIT_REFUSED


def test_data_reports_segments_and_never_offers_test_segment(capsys):
    assert run("data") == api.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert {x["key"] for x in out} == {"CADJ", "MAADJ"}
    assert all(x["test_segment_accessible_via_sandbox"] is False and set(x["segments"]) == {"train", "valid"} for x in out)


# ---------- gate ----------

GT = ("gate", "--strategy", "s.py", "--class", "S", "--params", "{}")


def test_gate_requires_review_confirmation(tree, capsys):
    (tree / "pending_strategies" / "s.py").write_text("x = 1\n", encoding="utf-8")

    assert run(*GT) == api.EXIT_REFUSED and "confirm-reviewed" in capsys.readouterr().err


def test_gate_refuses_unsafe_code_without_spawning(tree, monkeypatch, capsys):
    (tree / "pending_strategies" / "s.py").write_text("import subprocess\n", encoding="utf-8")
    fake_subprocess(monkeypatch, lambda *a, **k: pytest.fail("筛查未过不得启动子进程"))

    assert run(*GT, "--confirm-reviewed") == api.EXIT_REFUSED and "禁止导入" in capsys.readouterr().err


def test_gate_rejects_nonpositive_random_runs(tree):
    (tree / "pending_strategies" / "s.py").write_text("x = 1\n", encoding="utf-8")

    assert run(*GT, "--random-runs", "0", "--confirm-reviewed") == api.EXIT_REFUSED


def test_gate_plateau_file_is_validated_and_missing_means_not_on_plateau(tree, tmp_path):
    assert api.load_plateau(None) == (False, "未提供 --plateau-file:参数高原未评估,按未通过处理")
    bad = tmp_path / "p.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(api.rs.SandboxError):
        api.load_plateau(str(bad))


def test_gate_plateau_file_uses_plateau_logic(tmp_path):
    import itertools
    scores = [[a, b, c, 1.0] for a, b, c in itertools.product(range(5), repeat=3)]
    good = tmp_path / "p.json"
    good.write_text(json.dumps({"cell": [2, 2, 2], "scores": scores}), encoding="utf-8")
    spike = tmp_path / "q.json"
    spike.write_text(json.dumps({"cell": [2, 2, 2], "scores": [[*s[:3], 9.0 if s[:3] == [2, 2, 2] else -1.0] for s in scores]}), encoding="utf-8")

    assert api.load_plateau(str(good))[0] is True
    assert api.load_plateau(str(spike))[0] is False


def test_gate_success_saves_result_without_exposing_test_segment(tree, monkeypatch, capsys):
    (tree / "pending_strategies" / "s.py").write_text("class S: pass\n", encoding="utf-8")
    body = {"passed": False, "failures": ["x"], "metrics": {"random_runs": 3}, "windows": [], "diagnosis": {}}

    class FakeProc:
        returncode = 0

        def communicate(self, timeout=None, input=None):
            write_child_result(input, body)
            return "", ""

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())

    assert run(*GT, "--random-runs", "20", "--confirm-reviewed") == api.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "gate" and out["passed"] is False and "strategy_sha256" not in out
    assert out["plateau_note"].startswith("未提供")
    assert "没看过滚动验证窗口" in out["protocol_notice"]
    assert (tree / "data" / "research" / f"{out['run_id']}.json").exists()


def test_gate_child_parser_converts_random_runs_to_int():
    args = api.build_parser().parse_args(["_gate_child", "--file", "f", "--cls", "S", "--params", "{}",
                                          "--random-runs", "7", "--on-plateau", "0", "--sha256", "abc"])

    assert args.random_runs == 7


# ---------- approve 与配置驱动 ----------

def make_strategy(tree, body="class S: pass\n"):
    path = tree / "pending_strategies" / "s.py"
    path.write_text(body, encoding="utf-8")
    return path


def approve(path):
    """记录一次人工批准。

    子入口自己查审批记录(哈希谁都能自己算,"人看过代码"这条前提不能靠父入口担保),
    所以直接调用子入口的用例必须先走一次批准,否则被拒的是"未批准"而不是被测的那一条。
    """
    api.rs.record_approval(path, api.settings()[1]["approvals_file"])


class _FakeTTY:
    """假装 stdin 是交互式终端。"""

    def isatty(self):
        return True

    def readline(self):
        return ""


def approve_interactively(monkeypatch, path, digest=None):
    """在交互式终端里「粘贴完整 sha256」完成一次人工批准。"""
    sha = api.rs.file_sha256(path)
    monkeypatch.setattr(api.sys, "stdin", _FakeTTY())
    monkeypatch.setattr("builtins.input", lambda prompt="": digest if digest is not None else sha)
    return sha


def test_approve_requires_an_interactive_terminal(tree, monkeypatch, capsys):
    """非交互(stdin 是管道)时 `approve` 必须拒绝:批准权在人手里,不在能喂字符串的程序手里。"""
    make_strategy(tree)
    monkeypatch.setattr(api.sys, "stdin", io.StringIO(""))      # 管道,不是终端

    code = run("approve", "--strategy", "s.py")

    assert code == api.EXIT_REFUSED
    assert "交互式终端" in capsys.readouterr().err
    assert not (tree / "data" / "research-control" / "approvals.json").exists()


def test_approve_requires_the_full_sha256(tree, monkeypatch, capsys):
    """哈希填错(哪怕只错一位)不批准。"""
    path = make_strategy(tree)
    approve_interactively(monkeypatch, path, digest="0" * 64)

    code = run("approve", "--strategy", "s.py")

    assert code == api.EXIT_OK
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["approved"] is False
    # **stdout 里不能有哈希**(第 5 轮 codex 的 M6):CLI 的 stdout 是「模型可见通道」的候选,
    # 哈希只留在 stderr(给人的通道)。
    assert "sha256" not in out
    assert api.rs.file_sha256(path) not in captured.out
    assert api.rs.file_sha256(path) in captured.err
    assert not (tree / "data" / "research-control" / "approvals.json").exists()


def test_approve_records_hash_and_unlocks_backtest_without_confirm_flag(tree, monkeypatch, capsys):
    path = make_strategy(tree)
    approve_interactively(monkeypatch, path)
    assert run("approve", "--strategy", "s.py") == api.EXIT_OK
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    # stdout 不带哈希,哈希只在 stderr(见上一条测试的 M6 说明)。
    assert "sha256" not in out
    assert api.rs.file_sha256(path) not in captured.out
    assert api.rs.file_sha256(path) in captured.err


def test_approve_stdout_is_pure_json(tree, monkeypatch, capsys):
    """stdout 必须能被 json.loads 整段解析。

    `input(prompt)` 会把提示语写进 stdout,那样 stdout 就不是纯 JSON 了——
    下游按行取 JSON 的调用方会被提示语卡住,而且提示语本身也是「给人看的东西」
    混进了可能外发的通道。提示语一律走 stderr。
    """
    path = make_strategy(tree)
    approve_interactively(monkeypatch, path)

    assert run("approve", "--strategy", "s.py") == api.EXIT_OK
    captured = capsys.readouterr()

    # 整段 stdout 必须是一个 JSON 值(不是「JSON + 别的」)
    json.loads(captured.out)
    assert "粘贴" not in captured.out
    assert "粘贴" in captured.err

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None, input=None):
            return "", "boom"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    assert run(*BT) == api.EXIT_FAILED                    # 通过了批准关口,走到子进程(假进程失败)


def test_edit_after_approval_requires_reapproval(tree, monkeypatch, capsys):
    path = make_strategy(tree)
    approve_interactively(monkeypatch, path)
    run("approve", "--strategy", "s.py")
    capsys.readouterr()
    path.write_text("class S:\n    x = 1\n", encoding="utf-8")
    fake_subprocess(monkeypatch, lambda *a, **k: pytest.fail("未重新批准不得启动子进程"))

    assert run(*BT) == api.EXIT_REFUSED and "approve" in capsys.readouterr().err


def test_approve_refuses_code_that_fails_screening(tree, capsys):
    make_strategy(tree, "import os\n")

    assert run("approve", "--strategy", "s.py") == api.EXIT_REFUSED
    assert not (tree / "data" / "research-control" / "approvals.json").exists()


def test_confirm_reviewed_flag_records_the_approval(tree, monkeypatch):
    path = make_strategy(tree)

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None, input=None):
            return "", "x"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    run(*BT, "--confirm-reviewed")

    assert api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_confirm_reviewed_is_refused_when_stdin_is_not_a_terminal(tree, monkeypatch, capsys):
    """`--confirm-reviewed` 也要求人在场:非 TTY 时任何程序都能自己带上这个开关批准代码。"""
    path = make_strategy(tree)
    monkeypatch.setattr(api.sys, "stdin", io.StringIO(""))      # 管道,不是终端

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None):
            return "", "x"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    code = run(*BT, "--confirm-reviewed")

    assert code == api.EXIT_REFUSED
    assert "交互式终端" in capsys.readouterr().err
    assert not api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_defaults_come_from_config_and_cli_flags_override(tree, monkeypatch):
    make_strategy(tree)
    cfg = api.rc.load()
    cfg["gate"]["random_runs"] = 37
    cfg["backtest"]["timeout_seconds"] = 11
    monkeypatch.setattr(api.rc, "load", lambda path=None: cfg)
    seen = {}

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None, input=None):
            seen["timeout"] = timeout
            return "", "x"

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    fake_subprocess(monkeypatch, popen)
    run(*GT, "--confirm-reviewed")
    assert seen["cmd"][seen["cmd"].index("--random-runs") + 1] == "37"

    run(*BT, "--confirm-reviewed")
    assert seen["timeout"] == 11
    run(*BT, "--timeout", "5", "--confirm-reviewed")
    assert seen["timeout"] == 5


def test_random_runs_outside_configured_bounds_is_refused(tree):
    make_strategy(tree)

    assert run(*GT, "--random-runs", "5", "--confirm-reviewed") == api.EXIT_REFUSED


def test_invalid_config_refuses_every_command(tree, monkeypatch, capsys):
    def boom(path=None):
        raise api.rc.ConfigError("坏了")

    monkeypatch.setattr(api.rc, "load", boom)

    assert run("results") == api.EXIT_REFUSED and "配置非法" in capsys.readouterr().err


@requires_trading_stack
def test_child_refuses_when_file_changed_after_approval(tree):
    # 候选必须放在 pending_strategies/ 内:子入口自己会查这条,否则拒绝原因会变成"路径不对"
    f = tree / "pending_strategies" / "s.py"
    f.write_text("class S: pass\n", encoding="utf-8")
    sha = api.rs.file_sha256(f)
    f.write_text("class S:\n    x = 1\n", encoding="utf-8")                 # 批准之后被改动(TOCTOU)

    args = api.build_parser().parse_args(["_child", "--file", str(f), "--cls", "S", "--key", "CADJ", "--segment", "train",
                                          "--params", "{}", "--sha256", sha])

    assert api.run_child(args) == api.EXIT_REFUSED


def test_parent_passes_the_approved_hash_to_children(tree, monkeypatch):
    make_strategy(tree)
    seen = {}

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None, input=None):
            return "", "x"

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    fake_subprocess(monkeypatch, popen)
    run(*BT, "--confirm-reviewed")

    assert seen["cmd"][seen["cmd"].index("--sha256") + 1] == api.rs.file_sha256(tree / "pending_strategies" / "s.py")


# ---------- 策略自身报错是研究中的正常结果,不是网关故障 ----------

def test_describe_error_keeps_type_and_message_but_drops_paths_and_truncates():
    text = ("回测中策略抛出异常:" + chr(10) + "Traceback (most recent call last):" + chr(10) + "  File \"/app/pending_strategies/x.py\", line 9" + chr(10)
            + "ValueError: too many values to unpack (expected 3) in C:/Users/me/secret/x.py")

    out = api.describe_error(RuntimeError(text), api.PHASE_VALIDATION)

    assert out["type"] == "ValueError" and "too many values to unpack" in out["message"]
    assert "C:" not in out["message"] and "/app/" not in out["message"] and len(out["message"]) <= 200


def test_describe_error_falls_back_to_exception_type_for_plain_errors():
    assert api.describe_error(KeyError("boom"), api.PHASE_VALIDATION)["type"] == "KeyError"
    long = api.describe_error(RuntimeError("x" * 1000), api.PHASE_VALIDATION)
    assert len(long["message"]) <= 200


def test_backtest_job_records_strategy_errors_as_results_instead_of_raising(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"error": {"type": "ValueError", "message": "too many values"}})

    result = api.backtest_job(strategy="s.py", cls="S", key="CADJ", segment="train", params={}, confirm=True)

    assert result["status"] == "strategy_error" and result["error"]["type"] == "ValueError"
    assert "stats" not in result and result["run_id"]
    assert (tree / "data" / "research" / f"{result['run_id']}.json").exists()


def test_gate_job_records_strategy_errors_too(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"error": {"type": "KeyError", "message": "x"}})

    result = api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)

    assert result["status"] == "strategy_error" and result["kind"] == "gate" and result["passed"] is False


# ---------- evaluate:候选声明 param_grid,服务端选参 + 门槛评估 ----------

def test_evaluate_job_requires_prior_approval_and_never_spawns(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda *a, **k: pytest.fail("未批准不得启动子进程"))

    with pytest.raises(api.JobError) as err:
        api.evaluate_job(strategy="s.py", cls="S")

    assert err.value.message == api.NEEDS_APPROVAL


def test_evaluate_job_spawns_the_evaluate_child_with_hash_and_configured_timeouts(tree, monkeypatch):
    path = make_strategy(tree)
    seen = {}

    def fake_spawn(cmd, timeout):
        seen.update(cmd=cmd, timeout=timeout)
        return {"passed": False, "failures": ["x"], "params": {"a": 1}, "selection": {"n_cells": 27}, "metrics": {}}

    monkeypatch.setattr(api, "spawn_child", fake_spawn)

    result = api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    cfg = api.rc.load()
    assert "_evaluate_child" in seen["cmd"] and seen["cmd"][seen["cmd"].index("--sha256") + 1] == api.rs.file_sha256(path)
    assert seen["timeout"] == cfg["gate"]["timeout_seconds"] + cfg["grid"]["timeout_seconds"]
    assert result["kind"] == "evaluate" and result["passed"] is False and result["run_id"]
    assert "选参窗口" in result["protocol_notice"]
    assert (tree / "data" / "research" / f"{result['run_id']}.json").exists()


def test_evaluate_job_records_strategy_errors_as_results(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"error": {"type": "GridError", "message": "param_grid 必须恰好 3 个参数"}})

    result = api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert result["status"] == "strategy_error" and result["kind"] == "evaluate" and result["passed"] is False
    assert result["error"]["type"] == "GridError"


def test_evaluate_job_validates_random_runs_bounds(tree):
    make_strategy(tree)

    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", random_runs=5, confirm=True)


def test_evaluate_cli_and_child_parsers():
    parser = api.build_parser()

    args = parser.parse_args(["evaluate", "--strategy", "s.py", "--class", "S", "--random-runs", "50"])
    child = parser.parse_args(["_evaluate_child", "--file", "f", "--cls", "S", "--random-runs", "50", "--sha256", "abc"])

    assert args.random_runs == 50 and child.random_runs == 50 and child.sha256 == "abc"


@requires_trading_stack
def test_evaluate_child_refuses_a_file_changed_after_approval(tree):
    f = tree / "pending_strategies" / "s.py"
    f.write_text("class S: pass\n", encoding="utf-8")
    sha = api.rs.file_sha256(f)
    f.write_text("class S:\n    x = 1\n", encoding="utf-8")

    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(f), "--cls", "S", "--random-runs", "20", "--sha256", sha])

    assert api.run_evaluate_child(args) == api.EXIT_REFUSED


# ---------- 结果里带确定性中文报告 ----------

def test_every_job_result_carries_a_report_and_saves_it(tree, monkeypatch):
    make_strategy(tree)
    stats = {"return_pct": 1.0, "maxdd_pct": -2.0, "maxdd_yuan": -200.0, "sharpe": 2.0, "round_trips": 200,
             "end_balance": 10100.0, "net_pnl": 100.0, "total_cost": 10.0}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"stats": stats, "pnls": [1, -1, 1]})

    bt = api.backtest_job(strategy="s.py", cls="S", key="CADJ", segment="train", params={}, confirm=True)

    assert "沙盒回测" in bt["report"]
    saved = json.loads((tree / "data" / "research" / f"{bt['run_id']}.json").read_text(encoding="utf-8"))
    assert saved["report"] == bt["report"]


def test_gate_and_evaluate_results_carry_reports_including_strategy_errors(tree, monkeypatch):
    make_strategy(tree)
    body = {"passed": False, "failures": ["样本外夏普 -1 < 1.5"], "metrics": {"oos_sharpe": -1.0}, "selection": {"n_cells": 27, "valid_cells": 5, "on_plateau": False},
            "params": {"a": 1}, "diagnosis": {"summary": "s"}}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: body)

    ev = api.evaluate_job(strategy="s.py", cls="S", confirm=True)
    gt = api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)
    assert "选参 + 晋级门槛评估" in ev["report"] and "晋级门槛评估" in gt["report"] and "未通过" in ev["report"]

    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"error": {"type": "ValueError", "message": "boom"}})
    err = api.evaluate_job(strategy="s.py", cls="S", confirm=True)
    assert "策略运行报错" in err["report"] and "boom" in err["report"]


# ---------- review:先看代码再批准 ----------

def test_review_shows_numbered_source_and_approves_only_on_the_full_hash(tree, capsys):
    """review 的确认方式是**粘贴完整 64 位 sha256**,不是敲个 y。"""
    path = make_strategy(tree, "class S:\n    x = 1\n")
    shown = []
    sha = api.rs.file_sha256(path)

    out = api.review_job("s.py", input_fn=lambda prompt: sha, write=shown.append)

    text = "\n".join(shown)
    assert "sha256" in text and sha in text and "静态筛查: 通过" in text
    assert "   1 | class S:" in text and "   2 |     x = 1" in text
    assert out["approved"] is True and api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


@pytest.mark.parametrize("answer", ["", "n", "N", "y", "yes please", " ", "0" * 64])
def test_review_does_not_approve_on_anything_but_the_full_hash(tree, answer):
    """除了完整哈希,别的输入一律不批准(敲 y 也不行)。"""
    path = make_strategy(tree)

    out = api.review_job("s.py", input_fn=lambda prompt: answer, write=lambda *_: None)

    assert out["approved"] is False
    assert not api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_review_requires_an_interactive_terminal(tree, monkeypatch):
    """非交互(stdin 是管道)时 `review` 必须拒绝:批准权在人手里,不在能喂字符串的程序手里。"""
    path = make_strategy(tree)
    monkeypatch.setattr(api.sys, "stdin", io.StringIO(""))

    with pytest.raises(api.JobError) as err:
        api.review_job("s.py", input_fn=lambda prompt: api.rs.file_sha256(path), write=lambda *_: None)

    assert "交互式终端" in err.value.message
    assert not api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_review_treats_closed_stdin_as_no(tree):
    path = make_strategy(tree)

    def eof(prompt):
        raise EOFError

    assert api.review_job("s.py", input_fn=eof, write=lambda *_: None)["approved"] is False
    assert not api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_review_refuses_code_that_fails_static_screening_without_asking(tree):
    make_strategy(tree, "import os\n")

    with pytest.raises(api.JobError) as err:
        api.review_job("s.py", input_fn=lambda prompt: pytest.fail("筛查不过不该提问"), write=lambda *_: None)

    assert "静态筛查未通过" in err.value.message


def test_review_on_already_approved_content_does_not_ask_again(tree):
    path = make_strategy(tree)
    api.rs.record_approval(path, tree / "data" / "research-control" / "approvals.json")

    out = api.review_job("s.py", input_fn=lambda prompt: pytest.fail("已批准不该再问"), write=lambda *_: None)

    assert out["approved"] is True and out["already"] is True


def test_review_cli_wires_through_builtin_input(tree, monkeypatch, capsys):
    path = make_strategy(tree)
    sha = api.rs.file_sha256(path)
    monkeypatch.setattr("builtins.input", lambda prompt="": sha)

    assert run("review", "--strategy", "s.py") == api.EXIT_OK
    assert api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")
    # 人读的内容与结果走 stderr(留给人的通道),stdout 仍只出 JSON。
    assert "已批准" in capsys.readouterr().err


def test_review_refuses_paths_outside_pending(tree, capsys):
    (tree / "evil.py").write_text("x = 1\n", encoding="utf-8")

    assert run("review", "--strategy", "../evil.py") == api.EXIT_REFUSED


def test_review_refuses_when_file_changes_while_user_is_reading(tree):
    path = make_strategy(tree, "class S:\n    x = 1\n")
    seen = api.rs.file_sha256(path)          # 人读的是这一份

    def swap_then_say_yes(prompt):
        path.write_text("class S:\n    x = 2\n", encoding="utf-8")      # 阅读期间被换成没看过的内容
        return seen

    with pytest.raises(api.JobError):
        api.review_job("s.py", input_fn=swap_then_say_yes, write=lambda _: None)
    assert not api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


# ---------- 试验记账:账本在可信的父进程读写,子进程只拿到一个数字 ----------

def _trials(tree):
    from risk.trial_ledger import total_trials
    return total_trials(tree / "data" / "research" / "trials.jsonl")


def _seed_trials(tree, n):
    from risk.trial_ledger import record_trials
    record_trials(tree / "data" / "research" / "trials.jsonl", "submission", n, at=1.0)


def _arg(cmd, name):
    return cmd[cmd.index(name) + 1]


def test_evaluate_job_hands_the_child_the_prior_trial_count(tree, monkeypatch):
    make_strategy(tree)
    _seed_trials(tree, 5)
    seen = {}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: seen.update(cmd=cmd) or
                        {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 27}, "metrics": {},
                         "trials": {"prior": 5, "grid_cells": 27, "total": 32}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _arg(seen["cmd"], "--prior-trials") == "5"


def test_evaluate_job_records_the_grid_cells_the_child_consumed(tree, monkeypatch):
    make_strategy(tree)
    _seed_trials(tree, 5)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 27}, "metrics": {},
                         "trials": {"prior": 5, "grid_cells": 27, "total": 32}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 32


def test_failed_evaluations_still_consume_trials(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"passed": False, "failures": ["无法选参: x"], "params": None, "selection": {"n_cells": 27},
                         "kind_detail": "selection-only", "trials": {"prior": 0, "grid_cells": 27, "total": 27}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)
    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 54


def test_strategy_errors_still_consume_the_minimum_grid(tree, monkeypatch):
    """子进程报错也可能已扫过格子(后半段异常):无法区分时按最小网格 27 保守预扣,不让失败变成免费探索。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"error": {"type": "GridError", "message": "x"}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 27


def test_timeouts_and_crashes_consume_the_minimum_grid_too(tree, monkeypatch):
    make_strategy(tree)

    def boom(cmd, timeout):
        raise api.JobError(api.EXIT_TIMEOUT, "超时")

    monkeypatch.setattr(api, "spawn_child", boom)
    with pytest.raises(api.JobError):
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 27


def test_precharge_is_not_double_counted_with_the_child_report(tree, monkeypatch):
    make_strategy(tree)
    _seed_trials(tree, 5)
    seen = {}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: seen.update(cmd=cmd) or
                        {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 64}, "metrics": {},
                         "trials": {"prior": 5, "grid_cells": 64, "total": 69}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _arg(seen["cmd"], "--prior-trials") == "5"        # 子进程拿到的是预扣之前的数,否则 27 会被算两次
    assert _trials(tree) == 5 + 64


def test_corrupt_trial_ledger_refuses_before_spawning(tree, monkeypatch):
    make_strategy(tree)
    (tree / "data" / "research").mkdir(parents=True)
    (tree / "data" / "research" / "trials.jsonl").write_text("garbage\n", encoding="utf-8")
    monkeypatch.setattr(api, "spawn_child", lambda *a, **k: pytest.fail("账本损坏不得启动子进程"))

    with pytest.raises(api.JobError) as exc:
        api.evaluate_job(strategy="s.py", cls="S", confirm=True)
    assert exc.value.code == api.EXIT_REFUSED and "试验账本" in exc.value.message

    with pytest.raises(api.JobError):
        api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)


def test_gate_job_counts_itself_as_at_least_one_trial(tree, monkeypatch):
    make_strategy(tree)
    seen = {}
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: seen.update(cmd=cmd) or {"passed": False, "failures": [], "metrics": {}})

    api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)
    assert _arg(seen["cmd"], "--prior-trials") == "1" and _trials(tree) == 1        # 账本为空也不能给 0,且这次要落账

    api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)
    assert _arg(seen["cmd"], "--prior-trials") == "2" and _trials(tree) == 2        # 反复换参数,N 必须递增

    _seed_trials(tree, 40)
    api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)
    assert _arg(seen["cmd"], "--prior-trials") == "43" and _trials(tree) == 43


def test_gate_attempt_is_recorded_even_when_the_child_times_out(tree, monkeypatch):
    make_strategy(tree)

    def boom(cmd, timeout):
        raise api.JobError(api.EXIT_TIMEOUT, "超时")

    monkeypatch.setattr(api, "spawn_child", boom)
    with pytest.raises(api.JobError):
        api.gate_job(strategy="s.py", cls="S", params={}, plateau=(False, "n"), confirm=True)

    assert _trials(tree) == 1


def test_child_parsers_accept_prior_trials():
    parser = api.build_parser()
    e = parser.parse_args(["_evaluate_child", "--file", "f", "--cls", "S", "--random-runs", "5", "--sha256", "a", "--prior-trials", "9"])
    g = parser.parse_args(["_gate_child", "--file", "f", "--cls", "S", "--params", "{}", "--on-plateau", "0", "--sha256", "a",
                           "--random-runs", "5", "--prior-trials", "9"])
    assert e.prior_trials == 9 and g.prior_trials == 9


def test_reported_grid_cells_are_floored_at_the_minimum_possible_grid(tree, monkeypatch):
    """子进程回报的格子数低于 3×3×3 在 validate_grid 下不可能出现:按下限 27 记账,不信任更小的数字。"""
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 1}, "metrics": {},
                         "trials": {"prior": 0, "grid_cells": 1, "total": 1}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 27


def test_reported_grid_cells_above_the_floor_are_recorded_as_is(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout:
                        {"passed": False, "failures": [], "params": None, "selection": {"n_cells": 64}, "metrics": {},
                         "trials": {"prior": 0, "grid_cells": 64, "total": 64}})

    api.evaluate_job(strategy="s.py", cls="S", confirm=True)

    assert _trials(tree) == 64


# ---------- 不可变宪法:参数与运行时类校验 ----------

def test_backtest_refuses_params_that_touch_protected_parameters(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda *a, **k: pytest.fail("受保护参数不得启动子进程"))

    with pytest.raises(api.JobError) as exc:
        api.backtest_job(strategy="s.py", cls="S", key="CADJ", segment="train",
                          params={"risk_budget": 99999.0}, confirm=True)
    assert exc.value.code == api.EXIT_REFUSED and "risk_budget" in exc.value.message


def test_gate_refuses_params_that_touch_protected_parameters(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda *a, **k: pytest.fail("受保护参数不得启动子进程"))

    with pytest.raises(api.JobError):
        api.gate_job(strategy="s.py", cls="S", params={"max_daily_losses": 99}, plateau=(False, "n"), confirm=True)


def test_ordinary_params_are_accepted(tree, monkeypatch):
    make_strategy(tree)
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"stats": {}, "pnls": []})

    result = api.backtest_job(strategy="s.py", cls="S", key="CADJ", segment="train",
                              params={"fast_window": 10}, confirm=True)
    assert result["kind"] == "backtest"


@requires_trading_stack
def test_child_refuses_a_candidate_that_overrides_a_protected_member(tree, monkeypatch):
    """候选在类体里改写风控底线:静态筛查在提交时就拒绝,这里守住第二道(文件被替换的 TOCTOU 情形)。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_text("class S:\n    risk_budget = 99999.0\n", encoding="utf-8")
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                           "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])

    assert api.run_evaluate_child(args) == api.EXIT_REFUSED


# ---- 回归:三个子进程入口必须走同一套三层校验 ----
# 早先三个入口各复制一遍流程,gate 入口漏掉了运行时类校验,整条 gate 路径只剩静态筛查一层。

@requires_trading_stack
def test_gate_child_rejects_a_candidate_that_fails_the_runtime_class_check(tree, monkeypatch, capsys):
    """gate 子进程必须做运行时类校验:静态筛查放行的候选也要在这里被拒。"""
    body = (
        "from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n"
        "class S(TrendAtr15mStrategy):\n"
        "    def entry_signal(self):\n"
        "        return 1\n"
    )
    path = make_strategy(tree, body)
    approve(path)
    args = api.build_parser().parse_args(
        ["_gate_child", "--file", str(path), "--cls", "S", "--params", "{}",
         "--on-plateau", "0", "--sha256", api.rs.file_sha256(path), "--random-runs", "20"])

    monkeypatch.setattr(api.const, "check_class",
                        lambda *a, **k: ["测试注入:候选重写了受保护成员 risk_budget"])

    code = api.run_gate_child(args)
    err = capsys.readouterr().err
    assert code == api.EXIT_REFUSED
    assert "受保护" in err


def test_all_three_child_entrypoints_go_through_the_same_validation_function(tree, monkeypatch):
    """三个子进程入口共用 import_candidate:复制粘贴的防线会漏,收敛成一个入口才不会漏。

    第 32 轮:入口改成"闭包包一层"(回传走闭包单元里的 emit,候选改模块属性骗不过它),
    于是入口本身只是一层壳 —— 壳里**真的**执行的处理函数要能被静态核对到,
    而且要能证明"壳委派的就是被核对的那个函数"(否则核对的是一个没人执行的副本)。"""
    import inspect
    for name in ("run_child", "run_evaluate_child", "run_gate_child"):
        entry = getattr(api, name)
        handler = getattr(entry, "handler", None)
        assert handler is not None, f"{name} 没有暴露处理函数,静态核对失效"
        # 壳必须真的委派给被核对的那个处理函数
        closed = [cell.cell_contents for cell in (entry.__closure__ or ())]
        assert handler in closed, f"{name} 委派的不是被核对的那个处理函数"
        source = inspect.getsource(handler)
        assert "import_candidate(" in source, f"{name} 没走统一入口"
        assert "check_candidate_class(" not in source, f"{name} 仍在自己做校验,会漏"


# ---------- codex 第 4 轮:pyc 绕过、参数绕过、evaluate 回归 ----------

@requires_trading_stack
def test_a_stale_pyc_cannot_replace_the_hashed_source(tmp_path, monkeypatch):
    """预置一个"时间戳有效"的旧 .pyc:执行的必须是被哈希、被筛查的那份源码。

    Python 默认的 .pyc 只比对源文件的 mtime 与大小,不确认字节码对应当前内容。
    如果载入走 `spec.loader.exec_module()`,攻击者可以让"人工批准的干净源码"与
    "实际执行的字节码"不是一回事——所以哈希、筛查、编译、执行必须基于同一份字节。"""
    import importlib.util
    import os
    import py_compile

    good = "from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    VALUE = 'GOOD'\n"
    bad = good.replace("'GOOD'", "'BAD!'")          # 等长:pyc 的大小校验同样过得去
    assert len(good) == len(bad)
    f = tmp_path / "s.py"
    f.write_text(bad, encoding="utf-8")
    py_compile.compile(str(f), doraise=True)        # 生成与 bad 对应的 .pyc
    stat = os.stat(f)
    f.write_text(good, encoding="utf-8")
    os.utime(f, (stat.st_atime, stat.st_mtime))      # 把 mtime 调回去:pyc 的时间戳校验因此通过

    # 先证明这套"攻击"确实成立:走 importlib 的老路子会执行旧字节码
    spec = importlib.util.spec_from_file_location("stale_demo", f)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.S.VALUE == "BAD!", "这个攻击没复现,测试无效"

    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    cls = api.import_candidate(f, "S", api.rs.file_sha256(f))
    assert cls.VALUE == "GOOD", "执行的不是被哈希的那份源码"


@requires_trading_stack
def test_child_entrypoints_refuse_protected_parameters_when_called_directly(tree):
    """子入口是独立的信任边界:绕过父入口直接传受保护参数,必须一样被拒。"""
    f = tree / "pending_strategies" / "s.py"
    f.write_text("class S: pass\n", encoding="utf-8")
    sha = api.rs.file_sha256(f)
    bad = '{"risk_budget": 1000000000}'

    args = api.build_parser().parse_args(["_child", "--file", str(f), "--cls", "S", "--key", "CADJ",
                                          "--segment", "train", "--params", bad, "--sha256", sha])
    assert api.run_child(args) == api.EXIT_REFUSED

    args = api.build_parser().parse_args(["_gate_child", "--file", str(f), "--cls", "S", "--params", bad,
                                          "--on-plateau", "0", "--sha256", sha, "--random-runs", "20"])
    assert api.run_gate_child(args) == api.EXIT_REFUSED


@requires_trading_stack
def test_evaluate_child_really_runs_the_evaluation(tree, monkeypatch, capsys):
    """evaluate 子进程必须真的走到评估那一步。

    早先统一入口的重构漏掉了 `cfg = rc.load()`,而 `cfg` 的使用点包在宽泛的
    `except Exception` 里——结果每个通过导入的 evaluate 作业都静默变成一条
    "NameError" 的错误结果。这个用例跑真实的子进程路径,断言拿到的是评估结果。"""
    import sys
    import types as pytypes

    body = "from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    VALUE = 'GOOD'\n"
    path = tree / "pending_strategies" / "s.py"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)

    class Win:
        name = "W1"
        train_start = 1
        train_end = 2
        valid_start = 3
        valid_end = 4

    seen = {}

    def fake_run_evaluation(**kw):
        seen.update(kw)
        return {"passed": True, "sharpe": 1.0}

    wf = pytypes.ModuleType("walk_forward_optimizer")
    wf.DATA_FILE = "x.csv"
    wf.SYMBOL = "c2701.DCE"
    wf.KEY = "CADJ"
    wf.build_windows = lambda: [Win()]
    bl = pytypes.ModuleType("backtest_lib")
    bl.DEFAULT_CAPITAL = 100000.0
    bl.load_bars_csv = lambda *a, **k: []
    rg = pytypes.ModuleType("research_gate")
    rg.engine_runner = lambda key: (lambda *a, **k: None)
    rg.run_evaluation = fake_run_evaluation
    rnd = pytypes.ModuleType("strategies.random_entry_15m_strategy")
    rnd.RandomEntry15mStrategy = object
    for name, mod in (("walk_forward_optimizer", wf), ("backtest_lib", bl), ("research_gate", rg),
                      ("strategies.random_entry_15m_strategy", rnd)):
        monkeypatch.setitem(sys.modules, name, mod)

    approve(path)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code, payload = child_capture(monkeypatch, tree, api.run_evaluate_child, args)

    assert code == api.EXIT_OK
    assert "error" not in payload, payload
    assert payload["passed"] is True
    assert seen["max_cells"] == api.rc.load()["grid"]["max_cells"]


def test_the_saved_backtest_stats_are_exactly_what_the_dashboard_validates():
    """落盘的 stats 与面板校验的键集必须同源。

    曾经面板另写一份键集(写成 vnpy 原始的 `total_trade_count`),而落盘的是投影后的 `round_trips`,
    结果每一个真实回测都被判成损坏、从列表消失、详情 404——而测试夹具照着那份错的写,套件全绿也证明不了。
    """
    from risk.report import BACKTEST_STATS_KEYS, backtest_stats_row

    stats = {"total_return": 0.1, "max_ddpercent": -5.0, "max_drawdown": -500.0, "sharpe_ratio": 1.2,
             "total_trade_count": 156, "end_balance": 20000.0, "total_net_pnl": 200.0,
             "total_commission": 100.0, "total_slippage": 50.0}
    row = backtest_stats_row(stats)
    assert row["round_trips"] == 78                     # total_trade_count 被折半
    assert "total_trade_count" not in row               # 落盘的形状里没有它
    for key in BACKTEST_STATS_KEYS:
        assert key in row and isinstance(row[key], (int, float))
# ---------- 第 6 轮:隐藏入口的信任边界要闭合(数值参数、路径、导入期错误) ----------

@requires_trading_stack
def test_child_entrypoints_refuse_a_negative_prior_trials(tree, capsys):
    """`--prior-trials` 为负必须被拒:evaluate 路径会先做 `prior + 格子数` 再算 DSR,
    负数让 N 变小,而 DSR 随 N 增大而变严——于是**晋级门槛被放松**。
    (gate 路径不加分号,负数会被 candidate_eval 的 `n_trials >= 1` 拦下,evaluate 路径漏了。)"""
    path = make_strategy(tree, "class S: pass\n")
    approve(path)
    sha = api.rs.file_sha256(path)
    for cmd in (["_gate_child", "--params", "{}", "--on-plateau", "0", "--random-runs", "20"],
                ["_evaluate_child", "--random-runs", "20"]):
        argv = cmd + ["--file", str(path), "--cls", "S", "--sha256", sha, "--prior-trials", "-5"]
        args = api.build_parser().parse_args(argv)
        code = api.run_gate_child(args) if cmd[0] == "_gate_child" else api.run_evaluate_child(args)
        assert code == api.EXIT_REFUSED, cmd
        assert "prior-trials" in capsys.readouterr().err


@requires_trading_stack
def test_child_entrypoints_refuse_random_runs_out_of_bounds(tree, capsys):
    """`--random-runs` 越界必须被拒:子进程自身没有超时(超时只由父进程兜),
    直接调用即可拖资源。父入口本来就用同一把配置尺子量,子入口不能比父入口松。"""
    path = make_strategy(tree, "class S: pass\n")
    approve(path)
    sha = api.rs.file_sha256(path)
    args = api.build_parser().parse_args(["_gate_child", "--file", str(path), "--cls", "S", "--params", "{}",
                                          "--on-plateau", "0", "--sha256", sha, "--random-runs", "999999999"])
    assert api.run_gate_child(args) == api.EXIT_REFUSED
    assert "random-runs" in capsys.readouterr().err


@requires_trading_stack
def test_child_entrypoints_refuse_a_file_outside_the_pending_directory(tree, capsys):
    """候选文件必须在 pending_strategies/ 内:子入口能被直接调用,
    不该假设 `--file` 是父进程给的——否则可以直接指向仓库里任何 .py。"""
    outside = tree / "not_pending.py"
    outside.write_text("class S: pass\n", encoding="utf-8")
    args = api.build_parser().parse_args(["_child", "--file", str(outside), "--cls", "S", "--key", "CADJ",
                                          "--segment", "train", "--params", "{}",
                                          "--sha256", api.rs.file_sha256(outside)])
    assert api.run_child(args) == api.EXIT_REFUSED
    assert "pending_strategies" in capsys.readouterr().err


@requires_trading_stack
def test_child_refuses_an_unknown_segment_with_the_refusal_code(tree, capsys):
    """非法 --segment 的退出码必须是"拒绝"(2),不是未捕获异常的 1。

    `rs.segment_window` 原来在受控异常处理之外,直接调用子入口会带 traceback 退出 1。"""
    path = make_strategy(tree, "class S: pass\n")
    approve(path)
    args = api.build_parser().parse_args(["_child", "--file", str(path), "--cls", "S", "--key", "CADJ",
                                          "--segment", "train", "--params", "{}",
                                          "--sha256", api.rs.file_sha256(path)])
    args.segment = "test"                                  # 绕过 parser 的 choices,模拟直接调用
    assert api.run_child(args) == api.EXIT_REFUSED
    assert "测试段" in capsys.readouterr().err


@requires_trading_stack
def test_an_import_time_error_comes_back_as_a_structured_error(tree, monkeypatch, capsys):
    """候选在**导入期**出错,要按结构化错误回传,而不是带 traceback 退出 1。

    契约:所有候选侧的失败都走 `{"error": ...}` 这条通道,LLM 才拿得到诊断信息;
    早先这类异常直接冒泡,父进程只报"回测失败(详情见本机日志)"。"""
    path = make_strategy(tree, "import math\nraise ValueError('boom')\nclass S: pass\n")
    approve(path)
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code, payload = child_capture(monkeypatch, tree, api.run_evaluate_child, args)
    assert code == api.EXIT_OK
    assert "error" in payload, payload


@requires_trading_stack
def test_a_candidate_file_with_a_bom_still_loads(tree, monkeypatch):
    """带 BOM 的候选文件要能正常载入。

    早先走标准文件载入器时 BOM 会被自动剥掉;改成自己读字节编译之后,
    BOM 会留在源码里让 compile 报"invalid non-printable character U+FEFF"——
    这是"同一份字节"改造带来的行为差异,不该悄悄存在。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_bytes("﻿from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    VALUE = 'GOOD'\n".encode("utf-8"))
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    cls = api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert cls.VALUE == "GOOD"


# ---------- 第 6 轮 codex 的 H1/H2/M2/M3/L6 ----------

def test_the_parent_side_screen_also_accepts_a_bom_candidate(tree):
    """父入口的筛查与子入口的载入必须对 BOM 一致。

    父入口此前按纯 utf-8 读文件,BOM 留在字符串里让 `ast.parse` 报"语法错误",
    于是同一份带 BOM 的候选在父入口被拒、在子入口却能过——而 ADR 写的是
    "BOM 候选与无 BOM 候选行为一致"。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_bytes(("from vnpy_ctastrategy import CtaTemplate" + chr(10)
                      + "class S(CtaTemplate):" + chr(10) + "    pass" + chr(10)).encode("utf-8-sig"))
    cfg = api.rc.load()
    findings = api.screen(path, cfg)
    assert not findings, findings


def test_the_recorded_hash_matches_the_approved_hash_for_a_crlf_file(tree, monkeypatch):
    """结果里记的 sha256 必须与批准记录里的同一个值。

    此前结果里的哈希取的是 `read_text` 之后的文本,而 read_text 会做通用换行转换:
    CRLF 文件于是算出一个与批准哈希**必然不同**的值(与并发无关,是确定性的),
    审计人拿结果里的哈希去比对 approvals 文件会查无此项。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_bytes(b"class S: pass" + b"\r" + b"\n")
    api.rs.record_approval(path, tree / "data" / "research-control" / "approvals.json")
    monkeypatch.setattr(api, "spawn_child", lambda cmd, timeout: {"stats": {}, "pnls": []})
    result = api.backtest_job(strategy="s.py", cls="S", key="CADJ", segment="train", params={})
    assert result["strategy_sha256"] == api.rs.file_sha256(path)


def test_a_child_refusal_reaches_the_caller_with_its_reason():
    """子进程的"拒绝"必须原样传到父层。

    早先一律压成"回测失败(退出码 3)",于是子入口里那些拒绝的原因只进了本机 stderr,
    永远进不了返回给调用方的结果——而那条通道正是为"把诊断送回去"设计的。

    第 31 轮加了一层:只有**框架自己写的**那行(带 `[框架]` 标记)才会被回传 ——
    候选与框架同进程,它 print 的内容不能顶替框架的结论(见
    tests/test_child_result_channel.py::test_only_the_frameworks_own_lines_are_relayed_back_as_the_reason)。"""
    import sys

    script = ("import sys; sys.stderr.write(%r + '参数 risk_budget 属于受保护项\\n'); sys.exit(2)"
              % api.FRAMEWORK_TAG)
    with pytest.raises(api.JobError) as excinfo:
        api.spawn_child([sys.executable, "-c", script], 30)
    assert excinfo.value.code == api.EXIT_REFUSED
    assert "risk_budget" in excinfo.value.message


def test_a_candidate_written_refusal_reason_is_not_relayed_as_the_frameworks_own():
    """反向:候选自己 print 的"拒绝原因"不被当成框架的结论回传。

    候选与框架同进程,它的 print 走同一条 stderr;以前父层取"最后一行非空输出",
    候选只要在被拒之后打印一行就能顶替框架的结论(也能把验证窗口的数字塞进去)。"""
    import sys

    script = "import sys; sys.stderr.write('close=2728.0 是验证窗口的收盘价\\n'); sys.exit(2)"
    with pytest.raises(api.JobError) as excinfo:
        api.spawn_child([sys.executable, "-c", script], 30)
    assert excinfo.value.code == api.EXIT_REFUSED
    assert "2728.0" not in excinfo.value.message
    assert "拒绝执行" in excinfo.value.message


@requires_trading_stack
def test_a_child_entrypoint_refuses_a_candidate_that_was_never_approved(tree, capsys):
    """子入口必须自己查审批记录。

    `--sha256` 只证明"文件没变",不证明"人看过":哈希谁都能自己算。子入口能被直接调用,
    不查审批就能跑一份从未被人审阅过的候选——那正是这套设计最核心的前提。"""
    path = make_strategy(tree, "class S: pass" + chr(10))
    args = api.build_parser().parse_args(["_child", "--file", str(path), "--cls", "S", "--key", "CADJ",
                                          "--segment", "train", "--params", "{}",
                                          "--sha256", api.rs.file_sha256(path)])
    assert api.run_child(args) == api.EXIT_REFUSED
    assert "批准" in capsys.readouterr().err


@requires_trading_stack
def test_a_missing_class_is_a_refusal_not_a_failure(tree, monkeypatch):
    """`--cls` 指向不存在的类是**参数错误**,退出码应为"拒绝"(2)而不是"失败"(3)。"""
    path = make_strategy(tree, "class S: pass" + chr(10))
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    with pytest.raises(api.JobError) as excinfo:
        api.import_candidate(path, "NoSuchClass", api.rs.file_sha256(path))
    assert excinfo.value.code == api.EXIT_REFUSED


@requires_trading_stack
def test_child_refuses_a_hash_that_was_never_approved(tree, capsys):
    """子入口查审批记录要按**传进来的那个哈希**查,不是按文件当前内容。

    场景:文件被改过(内容 B),调用方把 B 的哈希传进来——哈希是自洽的,但 **B 从未被批准过**
    (批准记录里是旧内容 A 的哈希)。子入口若按文件当前内容去查就等于"文件没变所以批准过",
    恰恰把"人看过的是 A"这件事丢了。所以查的必须是传进来的哈希。"""
    path = make_strategy(tree, "class S: pass\n")
    approve(path)                                   # 批准的是内容 A
    path.write_text("class S:\n    x = 1\n", encoding="utf-8")     # 现在文件是内容 B
    new_sha = api.rs.file_sha256(path)              # B 的哈希(自洽,但没被批准过)

    args = api.build_parser().parse_args(["_child", "--file", str(path), "--cls", "S", "--key", "CADJ",
                                          "--segment", "train", "--params", "{}", "--sha256", new_sha])
    assert api.run_child(args) == api.EXIT_REFUSED
    assert "批准" in capsys.readouterr().err


# ---------- 第 9 轮 codex:模块级 monkey-patch(Critical 1)—— 运行期第四层 ----------

@requires_trading_stack
def test_a_candidate_that_rewrites_a_framework_object_at_import_time_is_refused(tree, monkeypatch, request):
    """候选在导入期改写框架对象必须被拒。

    第 9 轮 codex 实测:`import risk.risk_guard as rg; rg.risk_per_trade = lambda ...: 0.0`
    三行就能架空 300 元单笔预算,而静态层/沙盒层/check_class/check_instance_state
    **四层全部放行**。静态层现在按整条属性路径拦住了直白的写法,
    但"换个写法"改不掉的是**身份**——运行期第四层比对导入前后的身份指纹。"""
    path = tree / "pending_strategies" / "s.py"
    # 候选会在**共享的类对象**上永久挂一个 `get_pricetick`——用例结束必须还原,
    # 否则同一进程里之后的测试看到的是被篡改的模板(`test_real_spawn_*` 里串行结果与新进程结果对不上,
    # 这条失败在基线上就存在)。按用例回滚:用 monkeypatch 先记下"类字典里原来有没有这个名字"。
    import strategies.trend_atr_15m_strategy as template_module

    template_class = template_module.TrendAtr15mStrategy
    had_in_dict = "get_pricetick" in vars(template_class)
    saved = vars(template_class).get("get_pricetick")

    def restore_template_class():
        if had_in_dict:
            template_class.get_pricetick = saved
        elif "get_pricetick" in vars(template_class):
            del template_class.get_pricetick

    request.addfinalizer(restore_template_class)
    # 这条写法静态层看不出来:`strategies` 是候选**必须**导入的包(继承基类),
    # 静态层没有理由禁它;能抓住它的只有身份指纹(第四层)。
    # 候选改写模板类的方法,与改写风控函数等价——模板方法正是风控判定的入口。
    # (第 10 轮:`get_size` 已经进了受保护名单,静态层就能拦;这里用一个静态层
    #  按名字看不见的成员,专门盯住"第四层还在工作"这件事。)
    path.write_text(
        "import strategies.trend_atr_15m_strategy as t\n"
        "t.TrendAtr15mStrategy.get_pricetick = lambda self: 10**9\n"
        "from vnpy_ctastrategy import CtaTemplate\n"
        "class S(CtaTemplate):\n"
        "    pass\n",
        encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    with pytest.raises(api.JobError) as exc:
        api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert "受保护的框架对象" in str(exc.value)


def test_a_candidate_that_shadows_get_size_on_the_instance_is_refused(tree, monkeypatch):
    """候选在**实例**上遮蔽 `get_size` 也要被拒(第 10 轮自查发现的 Critical)。

    `self.get_size = lambda: 0.001` 把合约乘数 10 换成 0.001:
    300 元单笔预算算成 0.003 元,任何止损距离都"在预算内"。
    类字典里没有这个赋值,四层防线原本全都放行——现在静态层按名字拦住了。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_text(
        "from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy\n"
        "class S(TrendAtr15mStrategy):\n"
        "    def entry_signal(self):\n"
        "        self.get_size = lambda: 0.001\n"
        "        return 1\n",
        encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    with pytest.raises(api.JobError) as exc:
        api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert "get_size" in str(exc.value)


def test_the_plain_module_monkey_patch_is_refused_by_the_static_layer(tree, monkeypatch):
    """直白的写法静态层就该拦(整条属性路径判定,不再只看末段)。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_text(
        "import risk.risk_guard as rg\n"
        "rg.DailyLossGuard.can_open = lambda self, day: True\n"
        "from vnpy_ctastrategy import CtaTemplate\n"
        "class S(CtaTemplate):\n"
        "    pass\n",
        encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    with pytest.raises(api.JobError) as exc:
        api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert "静态筛查未通过" in str(exc.value)


# ---------- 第 32 轮(复审):评估输入在候选导入之前取定 ----------

def _install_fake_evaluation_modules(monkeypatch, windows_factory):
    """装一套假的 wf / backtest_lib / research_gate,返回 (wf 模块, 记录 run_evaluation 实参的字典)。"""
    import sys
    import types as pytypes
    seen = {}

    def fake_run_evaluation(**kw):
        seen.update(kw)
        return {"passed": True, "sharpe": 1.0}

    wf = pytypes.ModuleType("walk_forward_optimizer")
    wf.DATA_FILE = "x.csv"
    wf.SYMBOL = "c2701.DCE"
    wf.KEY = "CADJ"
    wf.build_windows = windows_factory
    bl = pytypes.ModuleType("backtest_lib")
    bl.DEFAULT_CAPITAL = 100000.0
    bl.load_bars_csv = lambda *a, **k: []
    rg = pytypes.ModuleType("research_gate")
    rg.engine_runner = lambda key: (lambda *a, **k: None)
    rg.run_evaluation = fake_run_evaluation
    rnd = pytypes.ModuleType("strategies.random_entry_15m_strategy")
    rnd.RandomEntry15mStrategy = object
    for name, mod in (("walk_forward_optimizer", wf), ("backtest_lib", bl), ("research_gate", rg),
                      ("strategies.random_entry_15m_strategy", rnd)):
        monkeypatch.setitem(sys.modules, name, mod)
    return wf, seen


class _Win:
    def __init__(self, name, train_start, train_end, valid_start, valid_end):
        self.name, self.train_start, self.train_end = name, train_start, train_end
        self.valid_start, self.valid_end = valid_start, valid_end


def _allow_test_only_imports(monkeypatch, *modules):
    """合并 main #22 的导入白名单后,候选不能再直接 `import walk_forward_optimizer` / `research_api`。
    这两条用例要模拟的是"候选在**导入期**改写框架模块的属性"(同进程反射到的结果),
    所以仅在测试里把这几个模块加进白名单,让候选能跑到改写那一步;生产配置不动。
    白名单本身的放行/拦截另有用例(tests/test_research_sandbox.py、test_sandbox_round25_rules.py)。"""
    real_load = api.rc.load

    def load(*a, **k):
        cfg = real_load(*a, **k)
        cfg["screening"]["allowed_modules"] = list(cfg["screening"]["allowed_modules"]) + list(modules)
        return cfg
    monkeypatch.setattr(api.rc, "load", load)


@requires_trading_stack
def test_a_candidate_cannot_change_the_validation_windows_by_rebinding_the_builder(tree, monkeypatch):
    """候选在导入期改写 `wf.build_windows`,想让"样本外"变成选参窗口(样本内)→ 框架用的仍是导入前取定的窗口。

    第 32 轮 codex 复审实测:此前窗口在候选导入**之后**才读,验证窗口 == 选参窗口。"""
    # 合并 main #26 后候选已不能 `import walk_forward_optimizer`(仓库路径守卫直接拒;见
    # test_candidate_cannot_import_walk_forward_directly)。"在导入期改写 wf"现在只剩"经帧反射拿到
    # 模块"这一条(已登记残余),所以下面用导入期钩子**模拟**那次改写:在真正的 import_candidate
    # 之前把 wf 改掉,断言框架用的仍是导入**前**取定的输入。
    body = ("from vnpy_ctastrategy import CtaTemplate\n"
            "class S(CtaTemplate):\n"
            "    pass\n")
    path = tree / "pending_strategies" / "s.py"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    _, seen = _install_fake_evaluation_modules(
        monkeypatch, lambda: [_Win("W1", 1, 2, 3, 4)])
    loaded = {}
    real_import = api.import_candidate

    def hostile_import(path_, cls_name, sha):
        import walk_forward_optimizer as wf_mod            # 测试代码(不经沙盒)里模拟候选的改写
        evil = _Win("EVIL", 1, 2, 1, 2)
        wf_mod.build_windows = lambda: [evil]
        wf_mod.DATA_FILE = "attacker.csv"
        return real_import(path_, cls_name, sha)
    monkeypatch.setattr(api, "import_candidate", hostile_import)
    import backtest_lib
    monkeypatch.setattr(backtest_lib, "load_bars_csv", lambda path, *a, **k: loaded.setdefault("path", path) and [])

    approve(path)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code, payload = child_capture(monkeypatch, tree, api.run_evaluate_child, args)

    assert code == api.EXIT_OK, payload
    assert "error" not in payload, payload
    assert seen["select_window"] == (1, 2)
    assert seen["validation_windows"] == [("W1", 3, 4)], "验证窗口必须是导入前取定的那一份"
    assert loaded["path"] == "x.csv", "数据文件必须是导入前取定的那一份"


def test_candidate_cannot_import_walk_forward_directly(tree, monkeypatch):
    """放行/拦截的另一半:候选直接 `import walk_forward_optimizer` 被 main #26 的仓库路径守卫拒绝。"""
    body = ("import walk_forward_optimizer as wf\n"
            "from vnpy_ctastrategy import CtaTemplate\n"
            "class S(CtaTemplate):\n"
            "    pass\n")
    path = tree / "pending_strategies" / "s.py"
    path.write_text(body, encoding="utf-8")
    _allow_test_only_imports(monkeypatch, "walk_forward_optimizer")      # 先过静态白名单,才轮到运行期的路径守卫
    _install_fake_evaluation_modules(monkeypatch, lambda: [_Win("W1", 1, 2, 3, 4)])
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    approve(path)
    with pytest.raises(Exception) as exc:
        api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert "walk_forward_optimizer" in str(exc.value)


@requires_trading_stack
def test_windows_where_validation_is_not_after_selection_are_refused(tree, monkeypatch, capsys):
    """窗口不变量自己复查(不信任 `wf.Window.__post_init__`):验证窗口必须晚于选参窗口的结束。"""
    path = tree / "pending_strategies" / "s.py"
    path.write_text("from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    _install_fake_evaluation_modules(monkeypatch, lambda: [_Win("W1", 1, 5, 3, 4)])    # 验证开始 3 <= 选参结束 5

    approve(path)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code, payload = child_capture(monkeypatch, tree, api.run_evaluate_child, args)

    assert code == api.EXIT_REFUSED
    assert "验证窗口晚于选参窗口" in capsys.readouterr().err


@requires_trading_stack
def test_a_candidate_cannot_raise_the_grid_cap_by_mutating_the_config_after_the_import(tree, monkeypatch):
    """上限在候选导入前取成局部整数:导入期改写配置字典(哪怕经帧反射拿到它)也改不动评估用的上限。

    第 33 轮 codex 实测:`cfg` 虽在导入前取了,但 `cfg["grid"]["max_cells"]` 在导入之后的调用点才求值,
    候选把 343 改成 10^9 立刻生效 → 子进程实扫远多于账本记的格子 = 悄悄放松 DSR ≥ 0.95。"""
    import copy
    path = tree / "pending_strategies" / "s.py"
    path.write_text("from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    pass\n", encoding="utf-8")
    _, seen = _install_fake_evaluation_modules(monkeypatch, lambda: [_Win("W1", 1, 2, 3, 4)])
    cfg = copy.deepcopy(api.rc.load())
    original_cap = cfg["grid"]["max_cells"]
    monkeypatch.setattr(api.rc, "load", lambda *a, **k: cfg)

    def evil_import(path_, cls_name, sha):
        cfg["grid"]["max_cells"] = 10 ** 9            # 候选在导入期改写配置字典
        cfg["grid"]["min_round_trips"] = 0

        class S:
            pass
        return S

    monkeypatch.setattr(api, "import_candidate", evil_import)
    approve(path)
    args = api.build_parser().parse_args(["_evaluate_child", "--file", str(path), "--cls", "S",
                                          "--random-runs", "20", "--sha256", api.rs.file_sha256(path)])
    code, payload = child_capture(monkeypatch, tree, api.run_evaluate_child, args)

    assert code == api.EXIT_OK, payload
    assert seen["max_cells"] == original_cap, "上限必须是导入前取定的那个整数"
    assert seen["min_round_trips"] != 0


def test_a_hanging_child_is_killed_on_timeout():
    """子进程不返回时必须超时终止,不能永远挂着(第 9 轮 codex 的 M4)。

    超时这一路原来一条测试都没有:回测子进程卡死 → 网关那边一直等,
    研究任务被无限期占住,而且没有任何日志说明它卡在哪。
    """
    import sys

    with pytest.raises(api.JobError) as exc:
        api.spawn_child([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
    assert exc.value.code == api.EXIT_TIMEOUT
    assert "已终止" in str(exc.value)


# ---------- 第 12 轮 codex 的回归用例 ----------

def test_the_cli_stdout_covers_secret_values_by_value(tmp_path, monkeypatch, capsys):
    """CLI 的 stdout 是**模型可见通道的候选**,但原先只有「按名字」认凭据这一层
    (第 12 轮 codex 的 M5):候选把令牌连名字一起塞进异常文本时,网关与 MCP 会被
    按值盖掉,CLI 不会——三条通道口径不一致,漏的那一条就是漏的那一条。"""
    token = "s3cr3t-value-1234567890"
    token_file = tmp_path / "gateway.token"
    token_file.write_text(token + "\n", encoding="utf-8")
    monkeypatch.setattr(api.rc, "load", lambda: {})
    monkeypatch.setattr(api.rc, "resolve_paths", lambda cfg, root: {"token_file": token_file})
    monkeypatch.setattr(api.rc, "bisheng_openapi_key", lambda *a, **k: "")

    assert api._model_visible_secrets() == (token,)

    api.emit({"message": "候选把令牌 " + token + " 写进了异常文本"})
    out = capsys.readouterr().out
    assert token not in out, out
    assert "<凭据已隐去>" in out, out


def test_the_cli_stdout_still_works_without_a_token_file(tmp_path, monkeypatch, capsys):
    """读不到令牌文件时**不能让整条脱敏失效**(按值那一层跳过,其余照旧)。"""
    monkeypatch.setattr(api.rc, "load", lambda: {})
    monkeypatch.setattr(api.rc, "resolve_paths",
                        lambda cfg, root: {"token_file": tmp_path / "missing.token"})
    monkeypatch.setattr(api.rc, "bisheng_openapi_key", lambda *a, **k: "")

    assert api._model_visible_secrets() == ()

    api.emit({"message": "普通消息"})
    assert json.loads(capsys.readouterr().out) == {"message": "普通消息"}


# ---------- 第 14 轮 codex 的回归用例:三条通道共用同一份「按值凭据清单」 ----------

def test_the_cli_also_covers_the_bisheng_key_by_value(tmp_path, monkeypatch, capsys):
    """CLI 的「按值」清单原先**只有网关令牌**,没有毕昇开放 API 密钥
    (第 14 轮 codex 的 M3):候选把密钥写进异常文本,网关与 MCP 盖得住、CLI 盖不住。

    现在三条通道共用 `research_config.known_secret_values`,这一条断言 CLI 侧也覆盖到了。"""
    key = "bs-sak-cli-0123456789"
    monkeypatch.setattr(api.rc, "load", lambda: {})
    monkeypatch.setattr(api.rc, "resolve_paths",
                        lambda cfg, root: {"token_file": tmp_path / "missing.token"})
    monkeypatch.setattr(api.rc, "bisheng_openapi_key", lambda *a, **k: key)

    assert key in api._model_visible_secrets()

    api.emit({"message": "候选把密钥 " + key + " 写进了异常文本"})
    out = capsys.readouterr().out
    assert key not in out, out
    assert "<凭据已隐去>" in out, out


def test_the_three_model_visible_channels_share_one_secret_list(tmp_path, monkeypatch):
    """**清单不共享 = 口径漂移 = 漏的那一条就是漏的那一条**(第 14 轮 codex 的 M3/L10)。

    网关 / MCP / CLI 三条通道必须调同一个函数拿清单,而不是各自读一遍凭据:
    各自读一遍,读到的内容就会不一样(有的读到令牌、有的读到密钥)。
    """
    import research_gateway as gw

    key = "bs-sak-shared-0123456789"
    token = "shared-gateway-token-0123456789"
    monkeypatch.setattr(api.rc, "bisheng_openapi_key", lambda *a, **k: key)
    monkeypatch.setattr(api.rc, "load", lambda: {})
    monkeypatch.setattr(api.rc, "resolve_paths",
                        lambda cfg, root: {"token_file": tmp_path / "missing.token"})

    cli_list = api._model_visible_secrets()
    gateway_list = api.rc.known_secret_values(label="research-gateway", token=token)

    assert key in cli_list and key in gateway_list
    assert token in gateway_list
    # 同一份清单本体:函数是同一个,不是各写一份。
    assert gw.rc.known_secret_values is api.rc.known_secret_values
