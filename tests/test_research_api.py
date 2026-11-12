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
    monkeypatch.setattr(api, "RESULTS_DIR", tmp_path / "results")
    return tmp_path


def run(*argv):
    return api.main(list(argv))


BT = ("backtest", "--strategy", "s.py", "--class", "S", "--key", "CADJ", "--segment", "train")


def test_backtest_requires_review_confirmation(tree, capsys):
    (tree / "pending_strategies" / "s.py").write_text("x = 1\n", encoding="utf-8")

    assert run(*BT) == api.EXIT_REFUSED
    assert "confirm-reviewed" in capsys.readouterr().err


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
    assert (tree / "results" / f"{out['run_id']}.json").exists()
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
