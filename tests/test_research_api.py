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
                                          "--random-runs", "7", "--on-plateau", "0"])

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
    assert not (tree / "data" / "research" / "approvals.json").exists()


def test_confirm_reviewed_flag_records_the_approval(tree, monkeypatch):
    path = make_strategy(tree)

    class FakeProc:
        returncode = 1

        def communicate(self, timeout=None):
            return "", "x"

    fake_subprocess(monkeypatch, lambda *a, **k: FakeProc())
    run(*BT, "--confirm-reviewed")

    assert api.rs.is_approved(path, tree / "data" / "research" / "approvals.json")


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
