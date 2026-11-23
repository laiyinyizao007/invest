"""研究沙盒自动批准(Issue #67):独立账本、开关、尝试记录、与运行侧的隔离。"""
import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tools" / "research_mcp"))

import research_attempts as ra          # noqa: E402
import research_config as rc            # noqa: E402
import research_sandbox as rs           # noqa: E402
import server as mcp_server             # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "STATE_FILE", tmp_path / "sessions.json")
    monkeypatch.setattr(mcp_server, "KEY_FILE", tmp_path / "missing.key")
    monkeypatch.setattr(mcp_server, "ROOT", tmp_path)
    monkeypatch.setenv("BISHENG_OPENAPI_KEY", "bs-sak-test")
    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    cand = pending / "cand_1.py"
    cand.write_text("class C: pass\n", encoding="utf-8")
    sha = rs.file_sha256(cand)
    mcp_server.save_state({"current": {"session_id": "s1", "idea": "想法", "candidate": "cand_1.py",
                                       "candidate_sha": sha, "waiting_node": "input_wait",
                                       "waiting_message_id": 12}})
    human = tmp_path / "data" / "research-control" / "approvals.json"
    auto = tmp_path / "data" / "research-control" / "auto_approvals.json"
    attempts = tmp_path / "data" / "research" / "attempts.jsonl"
    monkeypatch.setattr(mcp_server, "approvals_file", lambda: human)
    monkeypatch.setattr(mcp_server, "auto_approvals_file", lambda: auto)
    monkeypatch.setattr(mcp_server, "attempts_file", lambda: attempts)
    return cand, sha, human, auto, attempts


# ---- 配置 ----

def test_switch_defaults_off_and_must_be_boolean():
    cfg = rc.load()
    assert cfg["auto_approve"]["enabled"] is False
    broken = json.loads(json.dumps(cfg))
    broken["auto_approve"]["enabled"] = "yes"
    with pytest.raises(rc.ConfigError):
        rc._validate(broken)


def test_auto_ledger_and_attempts_paths_are_fixed():
    cfg = rc.load()
    for key, bad in (("auto_approvals_file", "data/research-control/other.json"),
                     ("attempts_file", "data/other/attempts.jsonl")):
        broken = json.loads(json.dumps(cfg))
        broken["paths"][key] = bad
        with pytest.raises(rc.ConfigError):
            rc._validate(broken)


# ---- 独立账本 ----

def test_auto_approval_goes_to_its_own_ledger_only(tmp_path):
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    human, auto = tmp_path / "approvals.json", tmp_path / "auto_approvals.json"
    sha = rs.record_auto_approval(cand, auto)
    assert rs._load_approvals(auto)[sha]["via"] == "auto-research"
    assert not human.exists()                                       # 人工账没被碰
    assert rs.is_approved_sha_any(sha, human, auto) is True
    assert rs.is_approved_sha(sha, human) is False                  # 只读人工账的读法看不到它


def test_auto_writer_refuses_the_human_ledger_name(tmp_path):
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(rs.SandboxError):
        rs.record_auto_approval(cand, tmp_path / "approvals.json")


def test_changing_the_file_invalidates_auto_approval(tmp_path):
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    auto = tmp_path / "auto_approvals.json"
    rs.record_auto_approval(cand, auto)
    cand.write_text("x = 2\n", encoding="utf-8")
    assert rs.is_approved_sha_any(rs.file_sha256(cand), tmp_path / "approvals.json", auto) is False


def test_a_corrupt_auto_ledger_fails_closed_not_open(tmp_path):
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    auto = tmp_path / "auto_approvals.json"
    sha = rs.record_auto_approval(cand, auto)
    auto.write_text(auto.read_text(encoding="utf-8").replace(sha, "0" * 64), encoding="utf-8")
    with pytest.raises(rs.SandboxError):
        rs.is_approved_sha_any(sha, tmp_path / "approvals.json", auto)


# ---- 隔离:运行侧/装载链路绝不读自动账 ----

RUNTIME_SIDE = ["risk/approval_ledger.py", "risk/approved_guard.py", "scripts/research_collect.py",
                "scripts/record_promotion.py", "risk/live_admission.py", "risk/promotion_gate.py"]
AUTO_NAMES = {"auto_approvals", "auto_approvals_file", "record_auto_approval", "is_approved_sha_any",
              "AUTO_APPROVALS_FILE_NAME", "auto_approvals.json"}


def _mentions(path: Path) -> set[str]:
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        for name in (getattr(node, "id", None), getattr(node, "attr", None), getattr(node, "name", None)):
            if name in AUTO_NAMES:
                found.add(name)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(token in node.value for token in AUTO_NAMES):
                found.add(node.value[:40])
    return found


def test_runtime_side_never_reads_the_auto_ledger():
    files = [ROOT / rel for rel in RUNTIME_SIDE if (ROOT / rel).exists()]
    files += sorted((ROOT / "trading").rglob("*.py"))
    assert files
    hits = {str(f.relative_to(ROOT)): _mentions(f) for f in files if _mentions(f)}
    assert not hits, f"运行侧/装载链路出现了自动账的引用: {hits}"


def test_only_the_mcp_server_calls_the_auto_writer():
    callers = []
    files = list((ROOT / "scripts").glob("*.py")) + list((ROOT / "tools").rglob("*.py")) \
        + list((ROOT / "dashboard").rglob("*.py"))
    for f in files:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "record_auto_approval":
                callers.append(f.name)
            if isinstance(node, ast.Name) and node.id == "record_auto_approval":
                callers.append(f.name)
    assert sorted(set(callers)) == ["server.py"], callers
    server_tree = ast.parse((ROOT / "tools/research_mcp/server.py").read_text(encoding="utf-8"))
    uses = [n for n in ast.walk(server_tree) if isinstance(n, ast.Attribute) and n.attr == "record_auto_approval"]
    assert len(uses) == 1


def test_auto_writer_is_gated_by_the_switch_in_the_same_function():
    tree = ast.parse((ROOT / "tools/research_mcp/server.py").read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            call = next((n for n in ast.walk(fn) if isinstance(n, ast.Attribute)
                         and n.attr == "record_auto_approval"), None)
            if call is None:
                continue
            guards = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                      and getattr(n.func, "id", "") == "auto_approve_enabled"]
            assert guards and min(guards) < call.lineno, "自动账写入前必须先判开关"
            return
    pytest.fail("没找到自动账的写入点")


# ---- 服务端行为 ----

def test_switch_off_keeps_the_manual_two_stage_flow(env, monkeypatch):
    _cand, sha, human, auto, _attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: False)
    out = mcp_server._request_approval_inline("cand_1.py", None)
    assert sha in out and "亲自" in out
    assert not auto.exists() and not human.exists()


def test_switch_on_auto_approves_into_the_auto_ledger_and_records_the_attempt(env, monkeypatch):
    _cand, sha, human, auto, attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)
    out = mcp_server._request_approval_inline("cand_1.py", None)
    assert "自动批准" in out
    assert sha not in out                                           # 自动路径不外发哈希
    assert rs._load_approvals(auto)[sha]["via"] == "auto-research"
    assert not human.exists()
    rows = ra.read(attempts)
    assert [r["event"] for r in rows] == ["approval_requested", "auto_approved"]
    assert all(r["sha"] == sha and r["via"] == "auto-research" for r in rows)
    assert "已经批准过" in mcp_server._request_approval_inline("cand_1.py", None)


def test_unrecordable_attempt_blocks_the_auto_approval(env, monkeypatch):
    _cand, _sha, _human, auto, _attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)
    monkeypatch.setattr(mcp_server, "attempts_file", lambda: Path(__file__).parent)   # 目录 ⇒ 写不进去
    out = mcp_server._request_approval_inline("cand_1.py", None)
    assert "未执行" in out
    assert not auto.exists()


def test_screening_rejection_is_recorded_and_not_approved(env, monkeypatch):
    cand, _sha, _human, auto, attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)
    cand.write_text("import os\nos.system('x')\n", encoding="utf-8")
    state = mcp_server.load_state()
    state["current"]["candidate_sha"] = rs.file_sha256(cand)
    mcp_server.save_state(state)
    out = mcp_server._request_approval_inline("cand_1.py", None)
    assert "静态筛查未通过" in out
    assert not auto.exists()
    assert [r["event"] for r in ra.read(attempts)] == ["screening_rejected"]


def test_auto_approval_makes_approval_state_true(env, monkeypatch):
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)
    mcp_server._request_approval_inline("cand_1.py", None)
    ok, why = mcp_server.approval_state(mcp_server.current_session())
    assert ok and not why


# ---- 尝试记录 ----

def test_attempts_are_append_only_and_tamper_evident(tmp_path):
    path = tmp_path / "attempts.jsonl"
    ra.record_attempt(path, "start", session="s", idea="原始想法")
    ra.record_attempt(path, "failure", note="节点失败")
    assert [r["event"] for r in ra.read(path)] == ["start", "failure"]
    path.write_text(path.read_text(encoding="utf-8").replace("原始想法", "改过的想法"), encoding="utf-8")
    with pytest.raises(ra.AttemptsError):
        ra.read(path)


def test_attempts_reject_unknown_events_and_bad_hashes(tmp_path):
    with pytest.raises(ra.AttemptsError):
        ra.record_attempt(tmp_path / "a.jsonl", "bogus")
    with pytest.raises(ra.AttemptsError):
        ra.record_attempt(tmp_path / "a.jsonl", "start", sha256="xyz")


# ---- 候选(模型写的代码)伪造/改写自动账 ----

def test_a_forged_plain_entry_in_the_auto_ledger_is_not_trusted(tmp_path):
    auto = tmp_path / "data" / "research-control" / "auto_approvals.json"
    auto.parent.mkdir(parents=True)
    auto.write_text(json.dumps({"a" * 64: {"name": "evil.py", "at": "now"}}), encoding="utf-8")
    with pytest.raises(rs.SandboxError):
        rs.is_approved_sha_any("a" * 64, tmp_path / "approvals.json", auto)


def test_candidate_window_cannot_write_the_auto_ledger(tmp_path):
    auto = tmp_path / "data" / "research-control" / "auto_approvals.json"
    auto.parent.mkdir(parents=True)
    with rs.candidate_window():
        with pytest.raises(OSError):
            auto.write_text("{}", encoding="utf-8")
    assert not auto.exists()


# ---- 外部复核(codex)指出的缺口 ----

def test_record_row_enforces_the_channel_to_file_pairing_itself(tmp_path):
    """H2:配对校验在共用底层,不能靠包装层——直接调 _record_row 也写不进错的账。"""
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(rs.SandboxError):
        rs._record_row(cand, tmp_path / "approvals.json", via="auto-research")
    with pytest.raises(rs.SandboxError):
        rs._record_row(cand, tmp_path / "auto_approvals.json", via="cli")
    with pytest.raises(rs.SandboxError):
        rs.record_approval(cand, tmp_path / "auto_approvals.json", via="cli")      # L1:人工写入者写不进自动账
    with pytest.raises(rs.SandboxError):
        rs.record_approval(cand, tmp_path / "approvals.json", via="auto-research")
    assert not (tmp_path / "approvals.json").exists() and not (tmp_path / "auto_approvals.json").exists()


def test_the_screened_bytes_are_the_hashed_bytes(env, monkeypatch):
    """H1:哈希取自被筛查的同一份字节,不再二次读文件。"""
    cand, sha, _human, _auto, _attempts = env

    def boom(_path):
        raise AssertionError("不应再读一次文件算哈希")
    monkeypatch.setattr(rs, "file_sha256", boom)
    path, got, findings, err = mcp_server._inline_approval_target("cand_1.py", mcp_server.current_session())
    assert err == "" and got == sha and findings == []


def test_attempt_log_never_runs_ahead_of_the_ledger(env, monkeypatch):
    """M1:批准写入失败时,日志里不得出现 auto_approved。"""
    _cand, _sha, _human, _auto, attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)

    def fail(*_a, **_k):
        raise rs.SandboxError("磁盘满")
    monkeypatch.setattr(mcp_server.rs, "record_auto_approval", fail)
    out = mcp_server._request_approval_inline("cand_1.py", None)
    assert "写入失败" in out
    events = [r["event"] for r in ra.read(attempts)]
    assert events == ["approval_requested", "failure"]
    assert "auto_approved" not in events


def test_a_successful_auto_approval_logs_request_then_approved(env, monkeypatch):
    _cand, _sha, _human, _auto, attempts = env
    monkeypatch.setattr(mcp_server, "auto_approve_enabled", lambda: True)
    mcp_server._request_approval_inline("cand_1.py", None)
    assert [r["event"] for r in ra.read(attempts)] == ["approval_requested", "auto_approved"]


def test_a_corrupt_auto_ledger_fails_closed_even_when_the_human_ledger_hits(tmp_path):
    """M4:人工账命中也不能跳过自动账的核链。"""
    cand = tmp_path / "c.py"
    cand.write_text("x = 1\n", encoding="utf-8")
    human, auto = tmp_path / "approvals.json", tmp_path / "auto_approvals.json"
    sha = rs.record_approval(cand, human, via="cli")
    other = tmp_path / "d.py"
    other.write_text("y = 1\n", encoding="utf-8")
    rs.record_auto_approval(other, auto)
    auto.write_text("{ not json", encoding="utf-8")
    with pytest.raises(rs.SandboxError):
        rs.is_approved_sha_any(sha, human, auto)


def test_constants_are_not_drifting_between_config_and_sandbox():
    assert rs.AUTO_APPROVALS_FILE_NAME == rc.AUTO_APPROVALS_FILE_NAME


def test_the_repo_config_names_the_switch_explicitly_and_it_is_off():
    cfg = json.loads((ROOT / "config" / "research_sandbox.json").read_text(encoding="utf-8"))
    assert cfg["auto_approve"] == {"enabled": False}


def test_newest_pending_is_deterministic_on_equal_mtimes(env):
    import os
    pending = mcp_server.pending_dir()
    for name in ("a.py", "b.py"):
        (pending / name).write_text("x\n", encoding="utf-8")
        os.utime(pending / name, (1_700_000_000, 1_700_000_000))
    for _ in range(3):
        assert mcp_server.newest_pending({"a.py", "b.py"}) == "b.py"


def test_the_shared_writer_references_both_pairing_constants():
    """守卫由「白名单」补成「不变式」:_record_row 必须自己做通道/文件名配对。"""
    tree = ast.parse((ROOT / "scripts/research_sandbox.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_record_row")
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert {"AUTO_APPROVAL_VIA", "AUTO_APPROVALS_FILE_NAME"} <= names
