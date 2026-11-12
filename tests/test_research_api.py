"""研究沙盒 CLI:拒绝路径与输出契约(真实回测由人工在有数据的机器上验证)。"""
import json
import subprocess
from types import SimpleNamespace

import pytest

import optimize  # noqa: F401  预先导入,避免在补丁生效期间触发 vnpy 的惰性导入
import research_api as api


def fake_subprocess(monkeypatch, popen):
    # 只替换 research_api 看到的 subprocess,不碰全局模块(vnpy/asyncio 导入期间也会用到 Popen)
    monkeypatch.setattr(api, "subprocess", SimpleNamespace(
        Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired, run=subprocess.run))


@pytest.fixture
def tree(tmp_path, monkeypatch):
    (tmp_path / "pending_strategies").mkdir()
    monkeypatch.setattr(api.bootstrap, "ROOT", tmp_path)
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

        def communicate(self, timeout=None):
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

        def communicate(self, timeout=None):
            return "noise\n" + api.CHILD_MARK + json.dumps({"stats": stats, "pnls": [1, -1, 1]}) + "\n", ""

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())

    assert run("backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ", "--segment", "valid", "--confirm-reviewed") == api.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert len(out["strategy_sha256"]) == 64 and out["segment"] == "valid"
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

        def communicate(self, timeout=None):
            return api.CHILD_MARK + json.dumps(body) + "\n", ""

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())

    assert run(*GT, "--random-runs", "20", "--confirm-reviewed") == api.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "gate" and out["passed"] is False and len(out["strategy_sha256"]) == 64
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


def test_approve_records_hash_and_unlocks_backtest_without_confirm_flag(tree, monkeypatch, capsys):
    path = make_strategy(tree)
    assert run("approve", "--strategy", "s.py") == api.EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["sha256"] == api.rs.file_sha256(path)

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None):
            return "", "boom"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    assert run(*BT) == api.EXIT_FAILED                    # 通过了批准关口,走到子进程(假进程失败)


def test_edit_after_approval_requires_reapproval(tree, monkeypatch, capsys):
    path = make_strategy(tree)
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

        def communicate(self, timeout=None):
            return "", "x"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    run(*BT, "--confirm-reviewed")

    assert api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


def test_defaults_come_from_config_and_cli_flags_override(tree, monkeypatch):
    make_strategy(tree)
    cfg = api.rc.load()
    cfg["gate"]["random_runs"] = 37
    cfg["backtest"]["timeout_seconds"] = 11
    monkeypatch.setattr(api.rc, "load", lambda path=None: cfg)
    seen = {}

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None):
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


def test_child_refuses_when_file_changed_after_approval(tmp_path):
    f = tmp_path / "s.py"
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

        def communicate(self, timeout=None):
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

    out = api.describe_error(RuntimeError(text))

    assert out["type"] == "ValueError" and "too many values to unpack" in out["message"]
    assert "C:" not in out["message"] and "/app/" not in out["message"] and len(out["message"]) <= 200


def test_describe_error_falls_back_to_exception_type_for_plain_errors():
    assert api.describe_error(KeyError("boom"))["type"] == "KeyError"
    long = api.describe_error(RuntimeError("x" * 1000))
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


def test_evaluate_child_refuses_a_file_changed_after_approval(tmp_path):
    f = tmp_path / "s.py"
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

def test_review_shows_numbered_source_and_approves_only_on_y(tree, capsys):
    path = make_strategy(tree, "class S:\n    x = 1\n")
    shown = []

    out = api.review_job("s.py", input_fn=lambda prompt: "y", write=shown.append)

    text = "\n".join(shown)
    assert "sha256" in text and api.rs.file_sha256(path) in text and "静态筛查: 通过" in text
    assert "   1 | class S:" in text and "   2 |     x = 1" in text
    assert out["approved"] is True and api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")


@pytest.mark.parametrize("answer", ["", "n", "N", "yes please", " "])
def test_review_does_not_approve_on_anything_but_y(tree, answer):
    path = make_strategy(tree)

    out = api.review_job("s.py", input_fn=lambda prompt: answer, write=lambda *_: None)

    assert out["approved"] is False
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
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")

    assert run("review", "--strategy", "s.py") == api.EXIT_OK
    assert api.rs.is_approved(path, tree / "data" / "research-control" / "approvals.json")
    assert "已批准" in capsys.readouterr().out


def test_review_refuses_paths_outside_pending(tree, capsys):
    (tree / "evil.py").write_text("x = 1\n", encoding="utf-8")

    assert run("review", "--strategy", "../evil.py") == api.EXIT_REFUSED
