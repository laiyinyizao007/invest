"""档案解析:优先级、冲突分类、缺文件。三个脚本(探针/清场/一键)都靠它。"""
import os

import pytest

from ctp_tools.profile import load_profile


def _write(path, text):
    path.write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_process_env(monkeypatch):
    """清掉进程里残留的 CTP_* 变量。

    别的用例跑过 `bootstrap.load_env()`(dotenv 会把项目 .env 写进 os.environ),
    那些值会飘到本文件里来,让断言取决于「跑测试的顺序」——
    这是最坏的测试写法(曾经因此把一个用例会跑成另一个结果)。
    """
    for key in list(os.environ):
        if key.startswith("CTP_"):
            monkeypatch.delenv(key, raising=False)


def test_profile_wins_over_dotenv_and_process_env(tmp_path, monkeypatch):
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=1.1.1.1:1\nCTP_USERNAME=dotenv_user\n")
    _write(tmp_path / ".env.guoxin", "CTP_TD_ADDRESS=121.13.218.19:41285\n")
    monkeypatch.setenv("CTP_TD_ADDRESS", "9.9.9.9:9")      # 测试里故意污染进程环境
    loaded = load_profile(".env.guoxin", root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"    # 档案优先
    assert loaded.env["CTP_USERNAME"] == "dotenv_user"               # .env 次之
    # 档案显式指定了值 → 进程环境不一致只是**提示**,不是硬冲突
    assert any("CTP_TD_ADDRESS" in n for n in loaded.notes)
    assert not any("CTP_TD_ADDRESS" in c for c in loaded.hard_conflicts)


def test_without_a_profile_the_dotenv_is_used(tmp_path):
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=1.1.1.1:1\n")
    loaded = load_profile("", root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "1.1.1.1:1" and loaded.hard_conflicts == []


def test_a_missing_profile_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_profile(".env.不存在", root=tmp_path)


def test_unrelated_keys_do_not_count_as_conflicts(tmp_path):
    """只有决定「连到哪、用哪个账号」的键才算冲突,别的键吵起来没有意义。"""
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=1.1.1.1:1\n")
    _write(tmp_path / ".env.p", "SOME_OTHER_VAR=abc\n")
    os.environ["SOME_OTHER_VAR"] = "abc"
    try:
        loaded = load_profile(".env.p", root=tmp_path)
    finally:
        del os.environ["SOME_OTHER_VAR"]
    assert loaded.hard_conflicts == [] and loaded.notes == []


def test_bom_and_quotes_are_parsed_like_the_runtime(tmp_path):
    """闸门看到的值必须和 dotenv 送给柜台的值一致。"""
    (tmp_path / ".env").write_bytes("﻿CTP_TD_ADDRESS=\"121.13.218.19:41285\"\n".encode("utf-8"))
    loaded = load_profile("", root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"


def test_a_stale_process_environment_is_a_hard_conflict_without_a_profile(tmp_path, monkeypatch):
    """取证运行器用 dotenv(进程环境变量赢),本模块用 .env 赢。

    两边不一致时必须报出来:否则清场工具按 .env 连,取证运行器按进程环境连,
    平掉的仓位根本不是同一个账户的。没有指定档案 → 只能猜 → 硬冲突,必须拒绝。
    """
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=121.13.218.19:41285\n")
    monkeypatch.setenv("CTP_TD_ADDRESS", "10.0.0.1:41285")
    loaded = load_profile("", root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"     # 本模块取 .env 的值
    assert any("CTP_TD_ADDRESS" in c and "取 .env 的值" in c for c in loaded.hard_conflicts)


def test_a_stale_process_environment_is_only_a_note_when_the_profile_says_otherwise(tmp_path, monkeypatch):
    """指定了档案 = 操作员明说「用这个档案」:进程环境的残留值只提示,不拦。

    不这样处理的话,`--profile .env.guoxin` 这个用法根本没法用
    (上一次运行残留的 CTP_* 变量会一直把工具挡住)。
    """
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=10.0.0.1:41285\n")
    _write(tmp_path / ".env.guoxin", "CTP_TD_ADDRESS=121.13.218.19:41285\n")
    monkeypatch.setenv("CTP_TD_ADDRESS", "9.9.9.9:9")
    loaded = load_profile(".env.guoxin", root=tmp_path)
    assert loaded.env["CTP_TD_ADDRESS"] == "121.13.218.19:41285"
    assert loaded.hard_conflicts == []
    assert any("CTP_TD_ADDRESS" in n for n in loaded.notes)


def test_a_key_the_profile_does_not_define_stays_a_hard_conflict(tmp_path, monkeypatch):
    """档案没覆盖到的键,进程环境与 .env 打架时依然只能猜 → 硬冲突。"""
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=10.0.0.1:41285\nCTP_BROKER_ID=8030\n")
    _write(tmp_path / ".env.guoxin", "CTP_TD_ADDRESS=121.13.218.19:41285\n")
    monkeypatch.setenv("CTP_BROKER_ID", "9999")
    loaded = load_profile(".env.guoxin", root=tmp_path)
    assert any("CTP_BROKER_ID" in c for c in loaded.hard_conflicts)


def test_matching_process_environment_is_not_a_conflict(tmp_path, monkeypatch):
    _write(tmp_path / ".env", "CTP_TD_ADDRESS=121.13.218.19:41285\n")
    monkeypatch.setenv("CTP_TD_ADDRESS", "121.13.218.19:41285")
    loaded = load_profile("", root=tmp_path)
    assert loaded.hard_conflicts == []


def test_the_conflict_text_never_contains_the_credential(tmp_path, monkeypatch):
    """冲突信息会被打印到 stderr、也可能写进证据文件。

    原文带着密码/授权码/账号打出去 = 凭据泄露(G7)。
    """
    _write(tmp_path / ".env", "CTP_PASSWORD=dotenv_secret\nCTP_USERNAME=dotenv_user_1234\n")
    monkeypatch.setenv("CTP_PASSWORD", "process_secret")
    monkeypatch.setenv("CTP_USERNAME", "process_user_1234")
    loaded = load_profile("", root=tmp_path)
    text = "\n".join(loaded.hard_conflicts + loaded.notes)
    assert "dotenv_secret" not in text and "process_secret" not in text
    assert "dotenv_user_1234" not in text and "process_user_1234" not in text
    assert "CTP_PASSWORD" in text          # 键名要留着,人才知道哪个键对不上
    assert "已隐藏" in text


def test_the_note_text_never_contains_the_credential(tmp_path, monkeypatch):
    _write(tmp_path / ".env", "CTP_PASSWORD=dotenv_secret\n")
    _write(tmp_path / ".env.guoxin", "CTP_PASSWORD=profile_secret\n")
    monkeypatch.setenv("CTP_PASSWORD", "process_secret")
    loaded = load_profile(".env.guoxin", root=tmp_path)
    text = "\n".join(loaded.hard_conflicts + loaded.notes)
    assert "profile_secret" not in text and "process_secret" not in text


def test_a_reordered_whitelist_is_not_a_conflict(tmp_path, monkeypatch):
    """一键流程把排序后的白名单推给子进程,而 .env 里是手写顺序:同一集合不算冲突。

    按字符串比会把它误判成硬冲突 → 一键取证流程退出码 2,根本跑不起来。
    """
    _write(tmp_path / ".env", "BROKER_TEST_ALLOWED_TD=121.13.218.19:41285,120.86.114.57:41285\n")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "120.86.114.57:41285, 121.13.218.19:41285")
    loaded = load_profile("", root=tmp_path)
    assert loaded.hard_conflicts == []
    # 真的不同的集合仍然是冲突
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "10.0.0.1:41285")
    assert load_profile("", root=tmp_path).hard_conflicts


def test_a_gated_key_only_in_the_process_environment_is_a_hard_conflict(tmp_path, monkeypatch):
    """.env 和档案都没有、只在进程里的 CTP_* 会静默生效,必须报出来。"""
    _write(tmp_path / ".env", "CTP_BROKER_ID=8030\n")
    monkeypatch.setenv("CTP_USERNAME", "stale_user_0001")
    loaded = load_profile("", root=tmp_path)
    assert any("CTP_USERNAME" in c and "只在进程环境里" in c for c in loaded.hard_conflicts)
    assert "stale_user_0001" not in "\n".join(loaded.hard_conflicts)


def test_the_same_address_in_two_spellings_is_not_a_conflict(tmp_path, monkeypatch):
    """tcp:// 前缀不同的同一地址:地址闸门会放行,这里也不能判硬冲突(归一化与 resolve_allowed 一致)。"""
    _write(tmp_path / ".env", "BROKER_TEST_ALLOWED_TD=121.13.218.19:41285\n")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "tcp://121.13.218.19:41285")
    assert load_profile("", root=tmp_path).hard_conflicts == []


def test_hostname_case_is_not_a_conflict(tmp_path, monkeypatch):
    _write(tmp_path / ".env", "BROKER_TEST_ALLOWED_TD=Front.Example.com:41285\n")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "front.example.COM:41285")
    assert load_profile("", root=tmp_path).hard_conflicts == []
