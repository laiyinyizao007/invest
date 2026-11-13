"""只读探针的档案解析:优先级必须明确,否则会连到别的前置。不联网。"""
import os
from pathlib import Path

import pytest

import probe_position as p


@pytest.fixture(autouse=True)
def clean_process_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("CTP_") or key == "LIVE_VT_SYMBOL":
            monkeypatch.delenv(key, raising=False)


def test_profile_overrides_dotenv_and_process_env(tmp_path, monkeypatch):
    """指定了档案就必须以档案为准:否则 --profile .env.guoxin 会被 .env 里的旧值盖掉。"""
    dotenv = tmp_path / ".env"
    dotenv.write_text("CTP_TD_ADDRESS=1.1.1.1:1\nCTP_USERNAME=dotenv_user\n", encoding="utf-8")
    profile = tmp_path / ".env.guoxin"
    profile.write_text("CTP_TD_ADDRESS=121.13.218.19:41285\n", encoding="utf-8")
    monkeypatch.setattr(p.bootstrap, "ROOT", tmp_path)
    monkeypatch.setenv("CTP_TD_ADDRESS", "9.9.9.9:9")
    env = p.load_profile(str(profile))
    assert env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"   # 档案优先
    assert env["CTP_USERNAME"] == "dotenv_user"              # .env 次之
    assert p.load_profile("")["CTP_TD_ADDRESS"] == "1.1.1.1:1"  # 没指定档案就用 .env


def test_missing_profile_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(p.bootstrap, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        p.load_profile(".env.不存在")


def test_cli_arguments_match_the_documented_usage():
    args = p.parse_args(["--profile", ".env.guoxin", "--seconds", "45"])
    assert args.profile == ".env.guoxin"
    assert args.seconds == 45
    assert p.parse_args([]).seconds == 60
