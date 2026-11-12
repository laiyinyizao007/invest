"""研究沙盒底座:路径限制、静态筛查、环境白名单、窗口限制、往返盈亏、结果存取。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import research_sandbox as rs


# ---------- 路径:只认 pending_strategies/ ----------

def test_resolve_pending_accepts_file_inside_pending_dir(tmp_path):
    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    (pending / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert rs.resolve_pending("pending_strategies/a.py", tmp_path) == (pending / "a.py").resolve()
    assert rs.resolve_pending("a.py", tmp_path) == (pending / "a.py").resolve()      # 裸文件名也按 pending 解析


@pytest.mark.parametrize("arg", ["../outside.py", "pending_strategies/../x.py", "/etc/passwd.py", "pending_strategies/a.txt", "approved/a.py"])
def test_resolve_pending_rejects_escapes_and_wrong_suffix(tmp_path, arg):
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "outside.py").write_text("x", encoding="utf-8")
    (tmp_path / "x.py").write_text("x", encoding="utf-8")

    with pytest.raises(rs.SandboxError):
        rs.resolve_pending(arg, tmp_path)


def test_resolve_pending_rejects_missing_file(tmp_path):
    (tmp_path / "pending_strategies").mkdir()

    with pytest.raises(rs.SandboxError, match="不存在"):
        rs.resolve_pending("nope.py", tmp_path)


# ---------- 静态筛查(纵深防御,不是沙盒) ----------

CLEAN = "from vnpy_ctastrategy import CtaTemplate\nimport math\nclass S(CtaTemplate):\n    pass\n"


def test_screen_accepts_plain_strategy():
    assert rs.screen_source(CLEAN) == []


@pytest.mark.parametrize("src, needle", [
    ("import os\n", "os"),
    ("import subprocess\n", "subprocess"),
    ("from pathlib import Path\n", "pathlib"),
    ("import socket, math\n", "socket"),
    ("from urllib.request import urlopen\n", "urllib"),
    ("import importlib\n", "importlib"),
    ("exec('1')\n", "exec"),
    ("eval('1')\n", "eval"),
    ("open('.env').read()\n", "open"),
    ("__import__('os')\n", "__import__"),
    ("getattr(x, 'y')\n", "getattr"),
    ("x.__class__.__bases__\n", "__bases__"),
])
def test_screen_flags_dangerous_constructs(src, needle):
    findings = rs.screen_source(src)

    assert findings and any(needle in f for f in findings)


def test_screen_reports_syntax_error_instead_of_crashing():
    assert any("语法" in f for f in rs.screen_source("def (:\n"))


# ---------- 子进程环境白名单 ----------

def test_sandbox_env_drops_trading_credentials_and_unknown_vars():
    base = {"PATH": "p", "SYSTEMROOT": "r", "CTP_PASSWORD": "x", "CTP_AUTH_CODE": "y", "WEBHOOK_URL": "u",
            "LIVE_VT_SYMBOL": "c", "ANTHROPIC_API_KEY": "k", "TEMP": "t"}

    env = rs.sandbox_env(base)

    assert env["PATH"] == "p" and env["TEMP"] == "t"
    assert not any(k.startswith(("CTP_", "WEBHOOK_", "LIVE_", "ANTHROPIC_")) for k in env)


# ---------- 窗口:测试段不可达 ----------

def test_segment_window_allows_train_and_valid_only():
    start, end = rs.segment_window("CADJ", "train")
    assert start < end
    assert rs.segment_window("CADJ", "valid")[0] > end - (end - start)       # 取得到即可

    with pytest.raises(rs.SandboxError, match="测试段"):
        rs.segment_window("CADJ", "test")
    with pytest.raises(rs.SandboxError):
        rs.segment_window("NOPE", "train")


def test_segment_window_never_reaches_test_start():
    import optimize
    for key in ("CADJ", "MAADJ"):
        test_start = optimize.windows_for(key)["test"][0]
        for seg in ("train", "valid"):
            assert rs.segment_window(key, seg)[1] < test_start


# ---------- 往返盈亏 ----------

def T(direction, offset, price, vol=1):
    from vnpy.trader.constant import Direction, Offset      # 真实枚举:value 的文案是英文,不能按中文匹配
    return SimpleNamespace(direction=Direction[direction], offset=Offset[offset], price=price, volume=vol)


def test_round_trip_pnls_long_and_short():
    trades = [T("LONG", "OPEN", 100), T("SHORT", "CLOSE", 110),        # +10 点
              T("SHORT", "OPEN", 120), T("LONG", "CLOSE", 125)]        # -5 点
    assert rs.round_trip_pnls(trades, size=10) == [100.0, -50.0]


def test_round_trip_pnls_ignores_unclosed_position_and_handles_empty():
    assert rs.round_trip_pnls([T("LONG", "OPEN", 100)], size=10) == []
    assert rs.round_trip_pnls([], size=10) == []


# ---------- 结果存取 ----------

def test_save_and_load_result_roundtrip_and_list(tmp_path):
    rid = rs.save_result({"a": 1}, tmp_path, now=datetime(2026, 11, 12, 21, 0, 0))

    assert rs.load_result(rid, tmp_path) == {"a": 1, "run_id": rid}
    assert rs.list_results(tmp_path) == [rid]


@pytest.mark.parametrize("rid", ["../x", "a/b", "", "x.json"])
def test_load_result_rejects_unsafe_ids(tmp_path, rid):
    with pytest.raises(rs.SandboxError):
        rs.load_result(rid, tmp_path)
