"""tools/repro_logout_crash.py 的纯函数与 dry-run(不碰 docker、不读真实凭据)。"""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("repro_logout_crash", Path(__file__).resolve().parents[1] / "tools" / "repro_logout_crash.py")
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


def test_load_simnow_env_keeps_only_ctp_and_symbol_keys(tmp_path):
    f = tmp_path / "e"
    f.write_text('# c\nCTP_USER_ID="u1"\nCTP_TD_ADDRESS=tcp://1.2.3.4:30001\nOTHER=x\nLIVE_VT_SYMBOL=c2701.DCE\nbad line\n', encoding="utf-8")
    env = r.load_simnow_env(f)
    assert env == {"CTP_USER_ID": "u1", "CTP_TD_ADDRESS": "tcp://1.2.3.4:30001", "LIVE_VT_SYMBOL": "c2701.DCE"}


def test_apply_front_rewrites_only_the_ports():
    env = {"CTP_TD_ADDRESS": "182.254.243.31:30001", "CTP_MD_ADDRESS": "182.254.243.31:30011", "CTP_USER_ID": "u"}
    out = r.apply_front(env, "second")
    assert out["CTP_TD_ADDRESS"] == "182.254.243.31:40001" and out["CTP_MD_ADDRESS"] == "182.254.243.31:40011"
    assert out["CTP_USER_ID"] == "u" and env["CTP_TD_ADDRESS"].endswith("30001")      # 不改入参


def test_classify_exit_codes():
    assert [r.classify(c) for c in (0, 139, -11, 3, None)] == ["ok", "CRASH", "CRASH", "nonzero", "timeout"]


def test_command_passes_credentials_by_name_only_never_by_value():
    cmd = r.build_command("c1", ["CTP_PASSWORD", "CTP_USER_ID"], "disconnect", "c2701.DCE", 2, "t")
    assert cmd.count("-e") == 2 and "CTP_PASSWORD" in cmd and "=" not in "".join(cmd[cmd.index("-e"):cmd.index("--entrypoint")])
    assert "--confirm-simnow" in cmd and "--external-cut" in cmd and "disconnect" in cmd


def test_dry_run_does_not_need_credentials_or_docker(capsys):
    assert r.main(["--dry-run", "--rounds", "2"]) == 0
    assert "计划" in capsys.readouterr().out


def test_unreasonable_parameters_are_rejected():
    assert r.main(["--dry-run", "--cut-seconds", "30"]) == 2
    assert r.main(["--dry-run", "--gap-seconds", "10"]) == 2
