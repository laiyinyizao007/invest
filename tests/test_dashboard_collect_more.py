"""collect_recording / collect_research / get_run / get_doc / overview。"""
import hashlib
import json
import os

import pytest

from dashboard import collectors as col, config

NOW = 1_800_000_000.0


def settings(tmp_path, **over):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(over), encoding="utf-8")
    return config.load(path=p, root=tmp_path)


def S(tmp_path):
    return settings(tmp_path)


# ---------------------------------------------------------------- 录制

def rec(tmp_path, **over):
    d = tmp_path / ".ops" / "recording"
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    hb = datetime.fromtimestamp(NOW - 5, timezone.utc).isoformat(timespec="seconds")
    data = {"pid": 4242, "started_at": "2026-11-13T01:00:00+00:00", "status": "recording", "profile": "simnow",
            "symbols": ["C2701.DCE", "MA701.CZCE"], "database_dir": r"C:\Dev\x\.vntrader_record", "last_tick": "2026-11-13T09:00:01",
            "last_bar": "2026-11-13T09:00:00", "last_error": None, "heartbeat_at": hb, "data_warning": None}
    data.update(over)
    p = d / "status.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_recording_running(tmp_path):
    rec(tmp_path)
    r = col.collect_recording(S(tmp_path), NOW)
    d = r.section["data"]
    assert r.section["state"] == "ok" and d["wrapper_running"] is True and d["status"] == "recording"
    assert d["symbols"] == ["C2701.DCE", "MA701.CZCE"] and r.level == "ok" and "录制中" in r.headline
    assert "vntrader_record" not in json.dumps(r.section) and "pid" not in d       # 目录与进程号不外泄


def test_recording_stalled_is_a_warning_even_with_a_fresh_heartbeat(tmp_path):
    rec(tmp_path, status="stalled", last_tick=None, last_bar=None, last_error="连接后 146s 仍未订阅到任何合约")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.level == "warn" and r.section["data"]["status"] == "stalled" and r.reasons
    assert r.section["data"]["last_tick"] is None


def test_recording_with_a_dead_heartbeat_is_stale_and_not_running(tmp_path):
    rec(tmp_path, heartbeat_at="2026-11-12T00:00:00+00:00")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] == "stale" and r.section["data"]["wrapper_running"] is False and r.level == "warn"


def test_recording_stopped_normally_is_not_an_alarm(tmp_path):
    rec(tmp_path, status="stopped", heartbeat_at="2026-11-12T00:00:00+00:00")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] == "ok" and r.level == "ok" and r.section["data"]["wrapper_running"] is False


def test_recording_missing_and_corrupt(tmp_path):
    assert col.collect_recording(S(tmp_path), NOW).section["state"] == "missing"
    p = rec(tmp_path)
    p.write_text("{bad", encoding="utf-8")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] == "corrupt" and r.section["data"] is None and r.level == "unknown"


def test_recording_unknown_status_value_is_not_reported_as_healthy(tmp_path):
    rec(tmp_path, status="future_status")
    assert col.collect_recording(S(tmp_path), NOW).level != "ok"


# ---------------------------------------------------------------- 研究

def res(tmp_path, runs=(), ledger=(), pending=None, approvals=None):
    base = tmp_path / "data" / "research"
    base.mkdir(parents=True, exist_ok=True)
    for rid, body in runs:
        (base / f"{rid}.json").write_text(json.dumps(body), encoding="utf-8")
    if ledger:
        (base / "submissions.jsonl").write_text("\n".join(json.dumps(x) if isinstance(x, dict) else x for x in ledger) + "\n", encoding="utf-8")
    pend = tmp_path / "pending_strategies"
    pend.mkdir(exist_ok=True)
    for name, src in (pending or {}).items():
        (pend / name).write_bytes(src.encode("utf-8"))       # 按字节写:沙盒按字节算哈希,Windows 的 write_text 会改写换行符

    if approvals is not None:
        ctl = tmp_path / "data" / "research-control"
        ctl.mkdir(parents=True, exist_ok=True)
        (ctl / "approvals.json").write_text(json.dumps(approvals), encoding="utf-8")


RUN = {"kind": "evaluate", "status": "ok", "passed": False, "strategy_file": "demo.py", "strategy_sha256": "a" * 64,
       "report": "候选评估\n结论: 未通过(3 项未过)\n样本外夏普 -1.84", "failures": ["f1", "f2"], "params": {"n": 72},
       "metrics": {"oos_sharpe": -1.84}, "diagnosis": {"weakest": "drawdown"}, "selection": None, "windows": None}
RID = "20261112-210000-000000"


def test_research_quota_pending_and_recent_runs(tmp_path):
    src = "class C: pass\n"
    sha = hashlib.sha256(src.encode()).hexdigest()
    res(tmp_path, runs=[(RID, RUN)], ledger=[{"at": NOW - 3600, "name": "a.py", "sha256": "x"}, {"at": NOW - 90000, "name": "old.py", "sha256": "y"}],
        pending={"demo.py": src, "other.py": "x = 1\n"}, approvals={sha: {"name": "demo.py"}})
    r = col.collect_research(S(tmp_path), NOW)
    d = r.section["data"]
    assert d["quota"]["used"] == 1 and d["quota"]["limit"] == 5 and d["quota"]["window_hours"] == 24
    assert d["quota"]["next_release_in_seconds"] == int(24 * 3600 - 3600)          # 只有窗口内的一条
    assert d["approvals_count"] == 1
    pend = {p["name"]: p for p in d["pending"]}
    assert pend["demo.py"]["approved"] is True and pend["other.py"]["approved"] is False and len(pend["demo.py"]["sha256"]) == 64
    run = d["recent_runs"][0]
    assert run["run_id"] == RID and run["kind"] == "evaluate" and run["passed"] is False and "未通过" in run["headline"]
    assert r.section["state"] == "ok" and r.level == "ok" and "1/5" in r.headline


def test_research_full_quota_is_a_warning(tmp_path):
    res(tmp_path, ledger=[{"at": NOW - 100 * i, "name": f"{i}.py", "sha256": "x"} for i in range(5)])
    r = col.collect_research(S(tmp_path), NOW)
    assert r.level == "warn" and "用尽" in r.headline


def test_research_corrupt_ledger_is_corrupt_like_the_gateway_fail_closed(tmp_path):
    res(tmp_path, ledger=["{broken"])
    r = col.collect_research(S(tmp_path), NOW)
    assert r.section["state"] == "corrupt" and r.section["data"] is None


def test_research_not_deployed_is_missing(tmp_path):
    assert col.collect_research(S(tmp_path), NOW).section["state"] == "missing"


def test_research_run_files_with_odd_names_or_bad_json_are_ignored(tmp_path):
    res(tmp_path, runs=[(RID, RUN)])
    base = tmp_path / "data" / "research"
    (base / "notes.json").write_text("{}", encoding="utf-8")
    (base / "20261112-220000-000000.json").write_text("{bad", encoding="utf-8")
    ids = [x["run_id"] for x in col.collect_research(S(tmp_path), NOW).section["data"]["recent_runs"]]
    assert ids == [RID]


def test_get_run_returns_detail_and_never_touches_paths_outside_the_results_dir(tmp_path):
    res(tmp_path, runs=[(RID, RUN)])
    secret = tmp_path / "secret.json"
    secret.write_text('{"kind":"backtest"}', encoding="utf-8")
    d = col.get_run(S(tmp_path), RID)
    assert d["run_id"] == RID and d["report"].startswith("候选评估") and d["failures"] == ["f1", "f2"]
    for bad in ("../../secret", "..\\secret", "20261112-210000-000000/../x", "x", "", "20261112-210000-00000a", RID + ".json", "%2e%2e"):
        assert col.get_run(S(tmp_path), bad) is None, bad
    assert col.get_run(S(tmp_path), "20261112-999999-000000") is None


# ---------------------------------------------------------------- 文档

def test_get_doc_by_key_only(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "unattended-log.md").write_text("# 记录\n日志来源: C:\\Dev\\x\n", encoding="utf-8")
    d = col.get_doc(S(tmp_path), "unattended-log")
    assert d["key"] == "unattended-log" and d["title"] == "无人值守运行记录" and d["markdown"].startswith("# 记录")
    assert col.get_doc(S(tmp_path), "simnow-drills") is None                      # 文件不存在
    for bad in ("../.env", "..%2f.env", "C:\\x", "docs/unattended-log.md", "unattended-log.md", "", "UNATTENDED-LOG"):
        assert col.get_doc(S(tmp_path), bad) is None, bad


def test_get_doc_truncates_huge_files(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "unattended-log.md").write_text("x" * (2_000_000), encoding="utf-8")
    assert len(col.get_doc(S(tmp_path), "unattended-log")["markdown"]) <= 600_000


# ---------------------------------------------------------------- 总览

def write_healthy_trading(tmp_path):
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True, exist_ok=True)
    (st / "heartbeat").write_text(str(NOW - 3), encoding="utf-8")
    (st / "account_guard.json").write_text(json.dumps({"account_id": "1", "last_equity": 1.0, "peak_equity": 1.0, "day": "d",
                                                        "day_start_equity": 1.0, "hard_locked": False}), encoding="utf-8")
    log = tmp_path / "runtime" / "vntrader" / "log"
    log.mkdir(parents=True, exist_ok=True)
    p = log / "vt_20261112.log"
    p.write_text("2026-11-12 09:00:01.000 | INFO | r | 已启动\n", encoding="utf-8")
    os.utime(p, (NOW - 10, NOW - 10))


def test_overview_is_ok_only_when_core_sections_are_ok_and_optional_ones_may_be_absent(tmp_path):
    write_healthy_trading(tmp_path)
    o = col.collect_overview(S(tmp_path), NOW)
    assert o["level"] == "ok" and o["reasons"] == []
    assert o["sections"]["recording"]["state"] == "missing" and o["sections"]["research"]["state"] == "missing"
    assert set(o["sections"]) == {"trading", "alerts", "days", "recording", "research"}


def test_overview_with_nothing_is_unknown_never_ok(tmp_path):
    o = col.collect_overview(S(tmp_path), NOW)
    assert o["level"] == "unknown" and o["reasons"]


def test_overview_takes_the_worst_level_and_sorts_reasons_by_severity(tmp_path):
    write_healthy_trading(tmp_path)
    (tmp_path / "data" / "state" / "pause_trading.flag").write_text("", encoding="utf-8")
    rec(tmp_path, status="stalled")
    o = col.collect_overview(S(tmp_path), NOW)
    assert o["level"] == "warn"
    assert [r["level"] for r in o["reasons"]] == sorted((r["level"] for r in o["reasons"]), key={"critical": 0, "unknown": 1, "warn": 2, "ok": 3}.get)
    (tmp_path / "data" / "state" / "account_guard.json").write_text(json.dumps({"account_id": "1", "last_equity": 1.0, "peak_equity": 2.0, "day": "d",
                                                                                 "day_start_equity": 1.0, "hard_locked": True, "hard_reason": "累计回撤"}), encoding="utf-8")
    o = col.collect_overview(S(tmp_path), NOW)
    assert o["level"] == "critical" and o["reasons"][0]["level"] == "critical"


def test_overview_survives_a_collector_exception_and_reports_it_as_unknown(tmp_path, monkeypatch):
    write_healthy_trading(tmp_path)
    monkeypatch.setattr(col, "collect_recording", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    o = col.collect_overview(S(tmp_path), NOW)
    assert o["sections"]["recording"]["state"] == "unknown" and o["level"] in ("unknown", "warn")
    assert "boom" not in json.dumps(o, ensure_ascii=False)                        # 异常细节不外泄


def test_overview_server_time_is_beijing_iso(tmp_path):
    assert col.collect_overview(S(tmp_path), NOW)["server_time"].endswith("+08:00")


def test_run_id_format_is_the_first_line_of_defence_against_traversal(tmp_path, monkeypatch):
    """格式校验必须在碰文件系统之前生效:即使后面的目录检查失效,畸形 ID 也不能到达读文件那一步。"""
    res(tmp_path, runs=[(RID, RUN)])
    touched = []
    real = col.m.read_json
    monkeypatch.setattr(col.m, "read_json", lambda p, **k: (touched.append(str(p)), real(p, **k))[1])
    for bad in ("../../secret", "..\secret", RID + "/../x", "x", "", RID + ".json"):
        assert col.get_run(S(tmp_path), bad) is None
    assert not any("secret" in t or t.endswith("x.json") for t in touched)


def test_sections_never_report_a_negative_age(tmp_path):
    from dashboard import models
    assert models.section("ok", {}, as_of=NOW + 500, now=NOW)["age_seconds"] == 0.0


# ---------------------------------------------------------------- G8 第 1 轮审查发现

def test_a_run_file_that_is_a_symlink_pointing_outside_the_results_dir_is_not_read(tmp_path):
    """符号链接/junction:文件名合法,但解析后的真实位置在结果目录之外——必须拒绝。"""
    res(tmp_path, runs=[(RID, RUN)])
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"kind": "backtest", "report": "OUTSIDE-SECRET"}), encoding="utf-8")
    link = tmp_path / "data" / "research" / "20261112-220000-000000.json"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    assert col.get_run(S(tmp_path), "20261112-220000-000000") is None
    assert "OUTSIDE-SECRET" not in json.dumps(col.collect_research(S(tmp_path), NOW).section, ensure_ascii=False)


def test_sha_cache_is_bounded(tmp_path):
    col._SHA_CACHE.clear()
    pend = tmp_path / "pending_strategies"
    pend.mkdir()
    for i in range(col.SHA_CACHE_MAX + 50):
        f = pend / f"s{i}.py"
        f.write_bytes(f"x = {i}\n".encode())
        col._sha256(f)
    assert len(col._SHA_CACHE) <= col.SHA_CACHE_MAX


def test_alerts_with_an_outbox_but_no_logs_cannot_claim_there_are_no_new_alerts(tmp_path):
    """没有任何日志,就没有『进程在运行』的证据;空队列不能被解读成『一切正常』。"""
    ob = tmp_path / "data" / "state" / "webhook_outbox.jsonl"
    ob.parent.mkdir(parents=True)
    ob.write_text("", encoding="utf-8")
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.section["state"] != "ok" and r.level == "unknown"


def test_a_pending_candidate_that_is_a_symlink_outside_the_pending_dir_is_not_hashed_or_listed(tmp_path):
    res(tmp_path, pending={"ok.py": "x = 1\n"})
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"secret = 1\n")
    try:
        os.symlink(outside, tmp_path / "pending_strategies" / "linked.py")
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    names = [p["name"] for p in col.collect_research(S(tmp_path), NOW).section["data"]["pending"]]
    assert names == ["ok.py"]


def test_run_id_with_a_trailing_newline_is_not_a_valid_run_id(tmp_path):
    res(tmp_path, runs=[(RID, RUN)])
    assert col.get_run(S(tmp_path), RID + "\n") is None


def test_legitimate_files_are_still_found_when_the_project_root_itself_is_reached_through_a_link(tmp_path):
    """_really_inside 要求目录已是解析过的真实路径;项目根本身经符号链接访问时,正常文件不能被误判为越界。"""
    real_root = tmp_path / "real"
    real_root.mkdir()
    res(real_root, runs=[(RID, RUN)], pending={"demo.py": "x = 1\n"})
    link_root = tmp_path / "linked_root"
    try:
        os.symlink(real_root, link_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    d = col.collect_research(settings(link_root), NOW).section["data"]
    assert [r["run_id"] for r in d["recent_runs"]] == [RID] and [p["name"] for p in d["pending"]] == ["demo.py"]
    assert col.get_run(settings(link_root), RID)["run_id"] == RID
