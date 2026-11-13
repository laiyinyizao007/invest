"""一键流程的纯逻辑检查:只放行国信测试环境,时段判断按北京时间。"""
from datetime import datetime

import guoxin_oneclick as g


def good_env():
    return {"CTP_ENVIRONMENT": "测试", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "121.13.218.19:41285",
            "CTP_MD_ADDRESS": "tcp://121.13.218.19:41293", "CTP_USERNAME": "u", "CTP_PASSWORD": "p",
            "CTP_APP_ID": "a", "CTP_AUTH_CODE": "c", "LIVE_VT_SYMBOL": "c2701.DCE"}


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


def test_read_env_ignores_comments_and_blank_lines(tmp_path):
    p = tmp_path / ".env"
    p.write_text("# c\n\nA=1\nB = two\n", encoding="utf-8")
    assert g.read_env(p) == {"A": "1", "B": "two"}


def test_session_windows_use_given_beijing_time():
    assert g.in_session(datetime(2026, 11, 13, 21, 30)) is True        # 周五夜盘
    assert g.in_session(datetime(2026, 11, 13, 15, 42)) is False       # 周五收盘后
    assert g.in_session(datetime(2026, 11, 13, 10, 20)) is False       # 日盘小休
    assert g.in_session(datetime(2026, 11, 14, 10, 0)) is False        # 周六
