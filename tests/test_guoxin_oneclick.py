"""一键流程的纯逻辑检查:环境闸门、时段判断、证据目录模式一致性。"""
import json
import os
from datetime import datetime

import pytest

import guoxin_oneclick as g


@pytest.fixture(autouse=True)
def clean_process_env(monkeypatch):
    """测试进程里可能已经有 .env 加载进来的 CTP_*(进程环境变量会盖住档案值),先清干净。"""
    for key in list(os.environ):
        if key.startswith("CTP_") or key.startswith("BROKER_TEST_") or key == "LIVE_VT_SYMBOL":
            monkeypatch.delenv(key, raising=False)


def good_env():
    return {"CTP_ENVIRONMENT": "测试", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "121.13.218.19:41285",
            "CTP_MD_ADDRESS": "tcp://121.13.218.19:41293", "CTP_USERNAME": "u", "CTP_PASSWORD": "p",
            "CTP_APP_ID": "a", "CTP_AUTH_CODE": "c", "LIVE_VT_SYMBOL": "c2701.DCE"}


def simnow_env():
    return good_env() | {"CTP_ENVIRONMENT": "实盘", "CTP_BROKER_ID": "9999",
                          "CTP_TD_ADDRESS": "182.254.243.31:30001", "CTP_MD_ADDRESS": "182.254.243.31:30011"}


def test_good_guoxin_profile_passes():
    assert g.check_env(good_env()) == []


def test_simnow_or_production_profiles_are_refused():
    env = good_env() | {"CTP_ENVIRONMENT": "实盘", "CTP_BROKER_ID": "9999", "CTP_TD_ADDRESS": "182.254.243.31:30001"}
    joined = "\n".join(g.check_env(env))
    assert "CTP_ENVIRONMENT" in joined and "BROKER_ID" in joined and "交易前置" in joined


def test_missing_credentials_are_reported_without_printing_values():
    env = good_env()
    del env["CTP_AUTH_CODE"]
    assert g.check_env(env) == ["缺少 CTP_AUTH_CODE"]


def test_guoxin_gate_checks_market_data_front_too():
    """只校验交易前置会漏掉「行情连到别处」——两个前置都要在地址表内。"""
    env = good_env() | {"CTP_MD_ADDRESS": "8.8.8.8:1"}
    assert any("行情前置" in p for p in g.check_env(env))


def test_address_normalization_accepts_tcp_scheme():
    env = good_env() | {"CTP_TD_ADDRESS": "tcp://121.13.218.19:41285/ctp"}
    assert g.check_env(env) == []


def test_read_env_ignores_comments_and_blank_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\n\nA=1\nB = two\n", encoding="utf-8")
    assert g.read_env(p) == {"A": "1", "B": "two"}


def test_read_env_matches_runtime_parsing_for_bom_and_quotes(tmp_path):
    """闸门看到的值必须和 dotenv 送给柜台的值一致:带 BOM、带引号都不能解析歪。"""
    p = tmp_path / ".env"
    p.write_bytes("﻿CTP_TD_ADDRESS=\"121.13.218.19:41285\"\n".encode("utf-8"))
    assert g.read_env(p)["CTP_TD_ADDRESS"] == "121.13.218.19:41285"


def test_process_env_override_is_detected_as_conflict(monkeypatch):
    """进程环境变量会盖住 .env(dotenv override=False)——档案校验通过不等于运行时连的是它。"""
    monkeypatch.setenv("CTP_TD_ADDRESS", "221.231.141.60:4130")
    eff, conflicts = g.effective_env(good_env())
    assert eff["CTP_TD_ADDRESS"] == "221.231.141.60:4130"
    assert any("CTP_TD_ADDRESS" in c for c in conflicts)


def test_process_env_only_key_is_also_a_conflict(monkeypatch):
    monkeypatch.setenv("CTP_BROKER_ID", "8030")
    env = good_env()
    del env["CTP_BROKER_ID"]
    eff, conflicts = g.effective_env(env)
    assert eff["CTP_BROKER_ID"] == "8030"
    assert any("CTP_BROKER_ID" in c for c in conflicts)


def test_no_conflict_when_process_env_matches(monkeypatch):
    monkeypatch.setenv("CTP_TD_ADDRESS", "121.13.218.19:41285")
    _eff, conflicts = g.effective_env(good_env())
    assert conflicts == []


def test_session_windows_use_given_beijing_time():
    assert g.in_session(datetime(2026, 11, 13, 21, 30)) is True        # 周五夜盘
    assert g.in_session(datetime(2026, 11, 13, 15, 42)) is False       # 周五收盘后
    assert g.in_session(datetime(2026, 11, 13, 10, 20)) is False       # 日盘小休
    assert g.in_session(datetime(2026, 11, 14, 10, 0)) is False        # 周六


def test_rehearsal_profile_accepts_simnow_and_refuses_guoxin_or_unknown_hosts():
    assert g.check_simnow_env(simnow_env()) == []
    assert any("9999" in p for p in g.check_simnow_env(good_env()))                       # 国信档案不能当预演档案
    assert any("SimNow" in p for p in g.check_simnow_env(simnow_env() | {"CTP_TD_ADDRESS": "8.8.8.8:1"}))


def test_rehearsal_gate_checks_market_data_front_too():
    env = simnow_env() | {"CTP_MD_ADDRESS": "121.13.218.19:41293"}
    assert any("行情前置" in p for p in g.check_simnow_env(env))


def test_rehearsal_refuses_test_environment_profile():
    """预演档案写成「测试」会让运行期走「测试环境免检地址」的分支,闸门形同虚设。"""
    assert any("CTP_ENVIRONMENT" in p for p in g.check_simnow_env(simnow_env() | {"CTP_ENVIRONMENT": "测试"}))


def test_mode_marker_roundtrip_and_resume_guard(tmp_path):
    d = tmp_path / "rehearsal-20261113-210500"
    g.write_mode_marker(d, "rehearsal", simnow_env())
    marker = json.loads((d / "mode.json").read_text(encoding="utf-8"))
    assert marker["mode"] == "rehearsal" and marker["broker_id"] == "9999"
    # 预演不能在正式证据目录上补测:否则 SimNow 的结果会覆盖国信的 PASS/FAIL
    problems = g.check_resume_mode(d, "guoxin")
    assert any("不能拿" in p for p in problems)
    (d / "evidence.json").write_text("[]", encoding="utf-8")
    assert not any("不能拿" in p for p in g.check_resume_mode(d, "rehearsal"))


def test_resume_without_evidence_is_refused(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    problems = g.check_resume_mode(d, "guoxin")
    assert any("evidence.json" in p for p in problems)


@pytest.mark.parametrize("raw,expected", [
    ("121.13.218.19:41285,120.86.114.57:41285", {"121.13.218.19:41285", "120.86.114.57:41285"}),
    ("tcp://121.13.218.19:41285", {"121.13.218.19:41285"}),
    ("", set()),
])
def test_allowed_addresses_parsing(raw, expected):
    from broker_test import _allowed_addresses

    assert _allowed_addresses(raw) == expected
