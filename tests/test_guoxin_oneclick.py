"""一键流程的纯逻辑检查:环境闸门、时段判断、证据目录模式一致性。"""
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

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


def test_credential_conflict_is_detected_and_never_printed(monkeypatch):
    """进程环境变量里的密码/授权码也会盖住档案——只校验地址不校验凭据,
    就会出现「连的是国信地址、登的是另一个账号」。冲突信息里不能出现明文。"""
    monkeypatch.setenv("CTP_PASSWORD", "另一个密码")
    monkeypatch.setenv("CTP_AUTH_CODE", "另一个授权码")
    _eff, conflicts = g.effective_env(good_env())
    joined = "\n".join(conflicts)
    assert "CTP_PASSWORD" in joined and "CTP_AUTH_CODE" in joined
    assert "另一个密码" not in joined
    assert "另一个授权码" not in joined


def test_child_env_drops_inherited_ctp_keys_and_uses_validated_ones(monkeypatch):
    """子进程必须拿到闸门校验过的那一份环境,不能继承来路不明的 CTP_*。"""
    monkeypatch.setenv("CTP_TD_ADDRESS", "8.8.8.8:1")
    monkeypatch.setenv("CTP_PASSWORD", "遗留密码")
    eff = dict(good_env())
    eff["CTP_TD_ADDRESS"] = "121.13.218.19:41285"
    child = g.child_env(eff, {"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285"})
    assert child["CTP_TD_ADDRESS"] == "121.13.218.19:41285"
    assert child["CTP_PASSWORD"] == "p"
    assert child["BROKER_TEST_ALLOWED_TD"] == "121.13.218.19:41285"
    assert "遗留密码" not in " ".join(child.values())


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
    from ctp_tools.broker_targets import KNOWN_TD, resolve_allowed

    allowed, dropped = resolve_allowed(raw, KNOWN_TD)
    assert set(allowed) == (expected & set(KNOWN_TD))
    assert dropped == sorted(expected - set(KNOWN_TD))


def test_app_id_is_not_printed_like_a_plain_value():
    """AppID 是看穿式监管发的唯一标识,和密码一样不能原样打印。"""
    assert g.describe_env_value("CTP_APP_ID", "simnow_2026") == "(已隐藏)"
    assert g.describe_env_value("CTP_PASSWORD", "pw") == "(已隐藏)"
    assert g.describe_env_value("CTP_AUTH_CODE", "code") == "(已隐藏)"
    assert "simnow_2026" not in g.describe_env_value("CTP_APP_ID", "simnow_2026")


def test_mode_marker_is_not_overwritten_on_resume(tmp_path):
    """补测不能改写这个目录的「身份」:mode.json 记的是**第一次**运行的前置与时间。"""
    d = tmp_path / "guoxin-20261114-090000"
    g.write_mode_marker(d, "guoxin", good_env())
    first = json.loads((d / "mode.json").read_text(encoding="utf-8"))
    g.write_mode_marker(d, "guoxin", good_env() | {"CTP_TD_ADDRESS": "120.86.114.57:41285"})
    again = json.loads((d / "mode.json").read_text(encoding="utf-8"))
    assert again == first


# ---------- 子进程超时:先礼后兵 ----------

class _FakeProc:
    """按脚本指定的顺序回答 wait()。"""

    def __init__(self, script):
        self._script = list(script)
        self.signals = []
        self.killed = False

    def wait(self, timeout=None):
        action = self._script.pop(0)
        if action == "timeout":
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return action

    def send_signal(self, sig):
        self.signals.append(sig)

    def kill(self):
        self.killed = True


def test_child_exit_code_is_passed_through(monkeypatch):
    monkeypatch.setattr(g.subprocess, "Popen", lambda *a, **k: _FakeProc([0]))
    assert g.run_child(["cmd"], Path("."), {}, timeout=5) == 0


def test_child_that_cannot_be_started_is_a_crash(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("找不到文件")

    monkeypatch.setattr(g.subprocess, "Popen", boom)
    assert g.run_child(["cmd"], Path("."), {}, timeout=5) == g.EXIT_CRASH
    assert "启动子进程失败" in capsys.readouterr().err


def test_a_hung_child_is_asked_to_finish_first(monkeypatch, capsys):
    """超时不能直接强杀:子进程里的撤单/登出还要跑,否则柜台上留着脏委托。"""
    proc = _FakeProc(["timeout", 0])
    monkeypatch.setattr(g.subprocess, "Popen", lambda *a, **k: proc)
    assert g.run_child(["cmd"], Path("."), {}, timeout=5) == 0
    assert proc.signals              # 先发了中断信号
    assert not proc.killed           # 它自己收完尾就退了,没被强杀
    assert "先请它自己收尾" in capsys.readouterr().err


def test_a_child_that_refuses_to_finish_is_killed_and_reported(monkeypatch, capsys):
    proc = _FakeProc(["timeout", "timeout", "timeout", "timeout"])
    monkeypatch.setattr(g.subprocess, "Popen", lambda *a, **k: proc)
    assert g.run_child(["cmd"], Path("."), {}, timeout=5) == g.EXIT_CRASH
    assert proc.killed
    assert "强制结束" in capsys.readouterr().err


# ---------- 一键流程的主流程 ----------

def _profile_file(tmp_path, name=".env.guoxin", env=None):
    p = tmp_path / name
    body = env or good_env()
    p.write_text("".join(f"{k}={v}\n" for k, v in body.items()), encoding="utf-8")
    return p


def _install_flow(monkeypatch, tmp_path, profile_name=".env.guoxin", env=None, run_codes=None):
    """把「找文件 / 跑子进程 / 打开目录」换掉,只测编排逻辑本身。"""
    profile = _profile_file(tmp_path, profile_name, env)
    template = tmp_path / "template.docx"
    template.write_bytes(b"x")

    def fake_find_file(*relative):
        name = relative[-1] if relative else ""
        if name.startswith("docs/"):
            return template
        if name == profile_name:
            return profile
        return None

    codes = list(run_codes or [0, 0])
    calls = []

    def fake_run_child(cmd, cwd, env, timeout):
        calls.append(cmd)
        return codes.pop(0) if codes else 0

    monkeypatch.setattr(g, "find_file", fake_find_file)
    monkeypatch.setattr(g, "ROOT", tmp_path)
    monkeypatch.setattr(g, "run_child", fake_run_child)
    monkeypatch.setattr(g.os, "startfile", lambda path: None)
    monkeypatch.setattr(g, "in_session", lambda now: True)
    return profile, template, calls


def test_check_only_stops_after_the_environment_check(monkeypatch, tmp_path, capsys):
    """--check-only:只查环境,不登录不发单。"""
    _install_flow(monkeypatch, tmp_path)
    assert g.main(["--check-only", "--yes"]) == g.EXIT_OK
    assert "--check-only" in capsys.readouterr().out


def test_a_missing_profile_stops_the_flow(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(g, "in_session", lambda now: True)      # 不依赖真实时钟
    monkeypatch.setattr(g, "find_file", lambda *a: None)
    assert g.main(["--yes"]) == g.EXIT_CONFIG
    assert "找不到" in capsys.readouterr().err


def test_a_failing_environment_check_stops_the_flow(monkeypatch, tmp_path, capsys):
    """档案里的前置不在国信地址表内:不连接任何环境。"""
    bad = good_env() | {"CTP_TD_ADDRESS": "8.8.8.8:1"}
    _install_flow(monkeypatch, tmp_path, env=bad)
    assert g.main(["--yes"]) == g.EXIT_CONFIG
    assert "检查未通过" in capsys.readouterr().err


def test_a_missing_template_stops_the_flow(monkeypatch, tmp_path, capsys):
    profile = _profile_file(tmp_path)
    monkeypatch.setattr(g, "in_session", lambda now: True)      # 不依赖真实时钟
    monkeypatch.setattr(g, "find_file", lambda *a: profile if a[-1] == ".env.guoxin" else None)
    assert g.main(["--yes"]) == g.EXIT_CONFIG
    assert "找不到报告模板" in capsys.readouterr().err


def test_outside_the_trading_session_it_refuses_without_force(monkeypatch, tmp_path, capsys):
    """非交易时段硬跑会白耗一次登录额度:必须显式 --force。"""
    _install_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "in_session", lambda now: False)
    assert g.main(["--yes"]) == g.EXIT_CONFIG
    assert "非交易时段" in capsys.readouterr().out


def test_the_full_flow_runs_the_evidence_and_the_report(monkeypatch, tmp_path, capsys):
    """正式流程:切档案 → 取证 → 出报告 → 还原 .env。"""
    profile, _template, calls = _install_flow(monkeypatch, tmp_path)
    target = tmp_path / ".env"
    target.write_text("CTP_ENVIRONMENT=实盘\n", encoding="utf-8")
    assert g.main(["--yes"]) == g.EXIT_OK
    assert len(calls) == 2 and "broker_test.py" in calls[0][1] and "broker_report.py" in calls[1][1]
    assert target.read_text(encoding="utf-8") == "CTP_ENVIRONMENT=实盘\n"      # .env 已还原


def test_a_failed_evidence_run_does_not_produce_a_report(monkeypatch, tmp_path, capsys):
    """登录失败/配置错:不生成报告(半份证据不能当报告用)。"""
    _install_flow(monkeypatch, tmp_path, run_codes=[g.EXIT_LOGIN])
    assert g.main(["--yes"]) == g.EXIT_LOGIN
    assert "不生成报告" in capsys.readouterr().err


def test_a_crashed_evidence_run_does_not_produce_a_report(monkeypatch, tmp_path, capsys):
    _install_flow(monkeypatch, tmp_path, run_codes=[g.EXIT_CRASH])
    assert g.main(["--yes"]) == g.EXIT_CRASH
    assert "异常退出" in capsys.readouterr().err


def test_the_rehearsal_report_is_marked_as_not_submittable(monkeypatch, tmp_path, capsys):
    """预演:连 SimNow,报告用独立文件名 + 水印,不能交国信。"""
    _profile, _template, calls = _install_flow(monkeypatch, tmp_path, profile_name=".env.simnow",
                                                env=simnow_env(), run_codes=[0, 0])
    assert g.main(["--yes", "--rehearsal"]) == g.EXIT_OK
    out = capsys.readouterr().out
    assert "**不能**提交国信" in out
    # 报告命令里带水印与独立文件名:预演产物不能冒充国信报告
    report_cmd = calls[1]
    assert "--watermark" in report_cmd and "不可提交国信" in " ".join(report_cmd)
    assert any("预演-SimNow" in part for part in report_cmd)


def test_a_process_environment_conflict_stops_the_flow(monkeypatch, tmp_path, capsys):
    """进程环境里残留的 CTP_* 会盖住档案:两边不一致就停下,别连错账户。"""
    _install_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("CTP_TD_ADDRESS", "8.8.8.8:1")
    assert g.main(["--yes"]) == g.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "冲突" in err and "CTP_TD_ADDRESS" in err


def test_a_resume_directory_from_another_mode_is_refused(monkeypatch, tmp_path, capsys):
    """预演目录不能拿来补国信的测:那会把 SimNow 的结果写进国信的证据。"""
    _install_flow(monkeypatch, tmp_path)
    other = tmp_path / "rehearsal-dir"
    other.mkdir()
    (other / "mode.json").write_text(json.dumps({"mode": "rehearsal", "broker_id": "9999"}), encoding="utf-8")
    (other / "evidence.json").write_text("[]", encoding="utf-8")
    assert g.main(["--yes", "--resume", str(other)]) == g.EXIT_CONFIG
    assert "补测目录检查未通过" in capsys.readouterr().err


def test_terminate_child_asks_first_and_then_kills(monkeypatch, capsys):
    """父进程被 Ctrl+C/终止时,独立进程组里的子进程不会收到控制台信号,必须由父进程收掉。"""
    class _Live(_FakeProc):
        def poll(self):
            return None

    graceful = _Live([0])
    monkeypatch.setattr(g, "_CURRENT_CHILD", graceful)
    g.terminate_child(grace=1)
    assert graceful.signals and not graceful.killed

    stubborn = _Live(["timeout", 0])
    monkeypatch.setattr(g, "_CURRENT_CHILD", stubborn)
    g.terminate_child(grace=1)
    assert stubborn.signals and stubborn.killed
    assert "强制结束" in capsys.readouterr().err

    monkeypatch.setattr(g, "_CURRENT_CHILD", None)
    g.terminate_child()                              # 没有子进程 = 什么都不做


def test_the_child_handle_is_cleared_after_a_normal_exit(monkeypatch):
    monkeypatch.setattr(g.subprocess, "Popen", lambda *a, **k: _FakeProc([0]))
    g.run_child(["cmd"], Path("."), {}, timeout=5)
    assert g._CURRENT_CHILD is None


def test_the_pushed_whitelist_never_contradicts_the_profile_the_child_reads(tmp_path, monkeypatch):
    """父进程下推的白名单与子进程 .env 里的不一致 = 子进程判硬冲突、整条流程退出码 2。

    预演档案只写了 `:30001`,而 SimNow 全集有 4 个地址:档案里有白名单就用档案的。
    """
    from ctp_tools.profile import load_profile

    for key in list(__import__("os").environ):
        if key.startswith(("CTP_", "BROKER_TEST_ALLOWED")):
            monkeypatch.delenv(key, raising=False)
    profile_env = {"CTP_BROKER_ID": "9999", "CTP_TD_ADDRESS": "182.254.243.31:30001",
                   "BROKER_TEST_ALLOWED_TD": "182.254.243.31:30001"}
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in profile_env.items()), encoding="utf-8")
    # 与 main() 里同样的构造方式
    extra = {}
    if not profile_env.get("BROKER_TEST_ALLOWED_TD"):
        extra["BROKER_TEST_ALLOWED_TD"] = "x"
    child = g.child_env(profile_env, extra)
    for key, value in child.items():
        monkeypatch.setenv(key, value) if key in profile_env else None
    assert load_profile("", root=tmp_path).hard_conflicts == []


def test_a_dotenv_that_did_not_exist_before_the_run_is_removed_afterwards(monkeypatch, tmp_path):
    """运行前没有 .env:跑完要把我们写进去的国信档案删掉,否则别的脚本会静默停在国信账号上。"""
    _install_flow(monkeypatch, tmp_path)
    target = tmp_path / ".env"
    assert not target.exists()
    assert g.main(["--yes"]) == g.EXIT_OK
    assert not target.exists()
