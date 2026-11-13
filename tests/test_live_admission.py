"""实盘准入检查与 record_promotion 的测试。见 docs/adr/20261111-discipline-enforcement.md。"""
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import record_promotion  # noqa: E402
import run_headless  # noqa: E402
from fakes import FakeGateway  # noqa: E402
from risk.live_admission import check_live_admission, write_approval  # noqa: E402
from risk.promotion_gate import CandidateMetrics, check_gate  # noqa: E402
from test_scripts_cli import ENV, StubRuntime  # noqa: E402,F401

NOW = datetime(2026, 11, 11, 10, 0)

PASSING = CandidateMetrics(oos_sharpe=2.0, oos_years=6.0, max_drawdown_pct=10.0, max_drawdown_yuan=1200.0,
                           round_trips=200, random_entry_percentile=99.0, stress_sharpe=1.5,
                           data_adjusted=True, parameter_on_plateau=True, dsr=0.99, n_trials=50)
FAILING = CandidateMetrics(oos_sharpe=-1.08, oos_years=2.5, max_drawdown_pct=22.0, max_drawdown_yuan=2200.0,
                           round_trips=70, random_entry_percentile=40.0, stress_sharpe=-2.0,
                           data_adjusted=True, parameter_on_plateau=False)


def test_fixture_metrics_behave_as_labelled():
    assert check_gate(PASSING) == []
    assert len(check_gate(FAILING)) >= 5


# ---------- 纯逻辑 ----------

def test_non_production_environments_are_exempt(tmp_path):
    missing = tmp_path / "none.json"
    assert check_live_admission("测试", False, missing)[0]
    assert check_live_admission("实盘", True, missing)[0]                 # SimNow


def test_production_without_approval_is_refused(tmp_path):
    ok, note = check_live_admission("实盘", False, tmp_path / "none.json")
    assert not ok and "record_promotion.py" in note


def test_write_approval_refuses_failing_candidate_and_writes_nothing(tmp_path):
    path = tmp_path / "state" / "a.json"
    failures = write_approval(path, "S", FAILING, "me", NOW)
    assert failures and not path.exists()


def test_write_approval_then_admission_passes(tmp_path):
    path = tmp_path / "state" / "a.json"
    assert write_approval(path, "TrendAtr15m", PASSING, "me", NOW) == []
    ok, note = check_live_admission("实盘", False, path)
    assert ok and "TrendAtr15m" in note and "me" in note
    assert json.loads(path.read_text(encoding="utf-8"))["metrics"]["oos_sharpe"] == 2.0


@pytest.mark.parametrize("record", [
    {"strategy": "S", "approver": "me", "approved_at": "t", "gate_failures": ["夏普不足"]},
    {"strategy": "S", "approver": "", "approved_at": "t", "gate_failures": []},
    {"strategy": "", "approver": "me", "approved_at": "t", "gate_failures": []},
    {"strategy": "S", "approver": "me", "gate_failures": []},
    [],
])
def test_incomplete_or_failing_approval_is_refused(tmp_path, record):
    path = tmp_path / "a.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    assert not check_live_admission("实盘", False, path)[0]


def test_corrupt_approval_is_refused(tmp_path):
    path = tmp_path / "a.json"
    path.write_text("{not json", encoding="utf-8")
    assert not check_live_admission("实盘", False, path)[0]


# ---------- record_promotion 命令行 ----------

def run_record(tmp_path, metrics, extra=()):
    mfile = tmp_path / "m.json"
    mfile.write_text(json.dumps(asdict(metrics)), encoding="utf-8")
    out = tmp_path / "approval.json"
    code = record_promotion.main(["--metrics", str(mfile), "--strategy", "S", "--approver", "me",
                                  "--out", str(out), *extra], now=NOW)
    return code, out


def test_record_promotion_rejects_negative_baseline(tmp_path, capsys):
    code, out = run_record(tmp_path, FAILING)
    assert code == record_promotion.EXIT_GATE_FAILED and not out.exists()
    assert "未通过晋级门槛" in capsys.readouterr().err


def test_record_promotion_writes_for_passing(tmp_path):
    code, out = run_record(tmp_path, PASSING)
    assert code == record_promotion.EXIT_OK and out.exists()


def test_record_promotion_bad_inputs(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"oos_sharpe": 1}), encoding="utf-8")
    assert record_promotion.main(["--metrics", str(bad), "--strategy", "S", "--approver", "me"]) == record_promotion.EXIT_BAD_INPUT
    assert record_promotion.main(["--metrics", str(tmp_path / "nope.json"), "--strategy", "S",
                                  "--approver", "me"]) == record_promotion.EXIT_BAD_INPUT
    good = tmp_path / "g.json"
    good.write_text(json.dumps(asdict(PASSING)), encoding="utf-8")
    assert record_promotion.main(["--metrics", str(good), "--strategy", " ", "--approver", "me"]) == record_promotion.EXIT_BAD_INPUT


# ---------- run_headless 的四种组合 ----------

PROD_ENV = {**ENV, "CTP_ENVIRONMENT": "实盘"}
SIMNOW_ENV = {**PROD_ENV, "CTP_TD_ADDRESS": "tcp://182.254.243.31:30001"}


def headless(env, approval):
    return run_headless.main(["--check"], env=env, gateway_loader=lambda: FakeGateway,
                             runtime_factory=StubRuntime, approval_path=approval)


def test_headless_broker_test_environment_needs_no_approval(tmp_path):
    assert headless(ENV, tmp_path / "none.json") == 0


def test_headless_simnow_needs_no_approval(tmp_path):
    assert headless(SIMNOW_ENV, tmp_path / "none.json") == 0


def test_headless_production_without_approval_exits_2(tmp_path, capsys):
    assert headless(PROD_ENV, tmp_path / "none.json") == 2
    assert "拒绝启动" in capsys.readouterr().err


def test_headless_production_with_approval_passes(tmp_path):
    path = tmp_path / "a.json"
    write_approval(path, "S", PASSING, "me", NOW)
    assert headless(PROD_ENV, path) == 0


# ---------- CTP_ENVIRONMENT 严格校验 ----------

def test_environment_is_stripped_and_strictly_validated():
    from trading.runtime import build_ctp_setting
    assert build_ctp_setting({**ENV, "CTP_ENVIRONMENT": " 实盘 "})["柜台环境"] == "实盘"
    assert build_ctp_setting({k: v for k, v in ENV.items() if k != "CTP_ENVIRONMENT"})["柜台环境"] == "实盘"
    for bad in ("live", "PROD", "实 盘", "test"):
        with pytest.raises(ValueError, match="CTP_ENVIRONMENT"):
            build_ctp_setting({**ENV, "CTP_ENVIRONMENT": bad})
