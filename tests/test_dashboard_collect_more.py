"""collect_recording / collect_research / get_run / get_doc / overview。"""
import hashlib
import json
import os

import pytest

from dashboard import collectors as col, config
from risk.report import backtest_stats_row

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
    # started_at 取很早的时间:用例会把 heartbeat_at 覆盖成各种较早的值,而『开始时间不得晚于心跳』是面板的一致性规则
    data = {"pid": 4242, "started_at": "2020-01-01T00:00:00+00:00", "status": "recording", "profile": "simnow",
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
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.level != "ok"
    # 契约规定前端『先看 state』:认不出来的状态 = 无法判断,区块状态不能是 ok(否则渲染成绿卡片)
    assert r.section["state"] != "ok"


# ---------------------------------------------------------------- 研究

def res(tmp_path, runs=(), ledger=(), pending=None, approvals=None):
    base = tmp_path / "data" / "research"
    base.mkdir(parents=True, exist_ok=True)
    for rid, body in runs:
        if isinstance(body, dict) and "run_id" not in body and not body.get("_no_run_id"):
            body = {**body, "run_id": rid}                       # 真实产出里 save_result 一定写入 run_id
        body = {k: v for k, v in body.items() if k != "_no_run_id"} if isinstance(body, dict) else body
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


RUN = {"kind": "evaluate", "passed": False, "strategy_file": "demo.py", "strategy_sha256": "a" * 64, "ran_at": "2026-11-12T21:00:00",
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

def write_guard(tmp_path, **over):
    """写风控状态文件,mtime 对齐到测试时钟(NOW 是虚构的 2027 年;真实 mtime 会被正确判为落后几个月)。"""
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta, timezone
    sampled_at = datetime.fromtimestamp(NOW - 2, timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    data = {"sampled_at": sampled_at, "account_id": "1", "last_equity": 1.0, "peak_equity": 1.0, "day": "d", "day_start_equity": 1.0, "hard_locked": False}
    data.update(over)
    p = st / "account_guard.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    os.utime(p, (NOW - 2, NOW - 2))
    return p


def write_healthy_trading(tmp_path):
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True, exist_ok=True)
    (st / "heartbeat").write_text(str(NOW - 3), encoding="utf-8")
    write_guard(tmp_path)
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
    # 进化是可选子系统:没部署时不计入总体级别(codex 第十次复核要求它进 overview,否则 level 算了没出口)
    assert o["sections"]["evolution"]["state"] == "missing" and o["level"] == "ok"
    assert set(o["sections"]) == {"trading", "alerts", "days", "recording", "research", "evolution"}


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
    write_guard(tmp_path, peak_equity=2.0, hard_locked=True, hard_reason="累计回撤")
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


def test_candidate_hashes_are_never_cached_because_approved_is_a_security_relevant_status(tmp_path):
    """G8 第 2 轮审查:曾按 (路径, mtime, size) 缓存哈希,同长度改动并恢复 mtime 后『已批准』会继续显示。
    这条测试把『不缓存』的决定固化下来:同一个文件,内容变了(即使 mtime 与大小都没变),下一次读取必须反映新内容。"""
    f = tmp_path / "a.py"
    f.write_bytes(b"x=1\n")
    first = col._sha256(f)
    st = f.stat()
    f.write_bytes(b"x=2\n")
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert col._sha256(f) != first
    assert not hasattr(col, "_SHA_CACHE")


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


# ---------------------------------------------------------------- G8 第 2 轮(codex):录制 stopped / 研究完整性

def test_stopped_without_any_timestamp_proves_nothing_and_is_not_ok(tmp_path):
    """曾经:{"status": "stopped"} 单独一行就返回 state=ok、level=ok、as_of=null。"""
    d = tmp_path / ".ops" / "recording"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"status": "stopped"}), encoding="utf-8")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] != "ok" and r.level == "unknown"


def test_stopped_with_a_valid_final_timestamp_is_a_normal_inactive_state_with_as_of(tmp_path):
    rec(tmp_path, status="stopped", heartbeat_at="2026-11-12T00:00:00+00:00")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] == "ok" and r.level == "ok" and r.section["as_of"] is not None
    assert r.section["data"]["wrapper_running"] is False


def test_contradictory_timestamps_are_corrupt(tmp_path):
    """started_at 晚于 heartbeat_at:文件内部矛盾。"""
    rec(tmp_path, started_at="2026-11-13T05:00:00+00:00", heartbeat_at="2026-11-13T01:00:00+00:00")
    assert col.collect_recording(S(tmp_path), NOW).section["state"] == "corrupt"


def test_a_recording_heartbeat_far_in_the_future_is_not_fresh(tmp_path):
    rec(tmp_path, heartbeat_at="2099-01-01T00:00:00+00:00")
    r = col.collect_recording(S(tmp_path), NOW)
    assert r.section["state"] in ("corrupt", "unknown") and r.level == "unknown"


def test_corrupt_approvals_make_the_research_section_corrupt_not_zero_approvals(tmp_path):
    """曾经:approvals.json 损坏被解释成『0 个批准』,section 仍是 ok。"""
    res(tmp_path, pending={"demo.py": "x = 1\n"})
    ctl = tmp_path / "data" / "research-control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "approvals.json").write_text("{broken", encoding="utf-8")
    r = col.collect_research(S(tmp_path), NOW)
    assert r.section["state"] == "corrupt" and r.section["data"] is None and r.level == "unknown"


def test_missing_approvals_file_simply_means_nothing_is_approved(tmp_path):
    res(tmp_path, pending={"demo.py": "x = 1\n"})
    r = col.collect_research(S(tmp_path), NOW)
    assert r.section["state"] == "ok" and r.section["data"]["pending"][0]["approved"] is False


def test_unreadable_run_files_are_counted_and_surfaced_not_silently_dropped(tmp_path):
    res(tmp_path, runs=[(RID, RUN)])
    (tmp_path / "data" / "research" / "20261112-220000-000000.json").write_text("{bad", encoding="utf-8")
    r = col.collect_research(S(tmp_path), NOW)
    assert [x["run_id"] for x in r.section["data"]["recent_runs"]] == [RID]
    assert r.level == "warn" and "1" in r.headline or any("损坏" in x["text"] for x in r.reasons)
    assert "损坏" in (r.section["message"] or "")


def test_unreadable_pending_candidates_are_surfaced(tmp_path):
    res(tmp_path, pending={"ok.py": "x = 1\n"})
    big = tmp_path / "pending_strategies" / "huge.py"
    big.write_bytes(b"#" * (col.MAX_SOURCE_BYTES + 10))
    r = col.collect_research(S(tmp_path), NOW)
    assert [p["name"] for p in r.section["data"]["pending"]] == ["ok.py"]
    assert r.level == "warn" and "huge.py" not in json.dumps(r.section, ensure_ascii=False)


def test_an_edited_candidate_with_restored_mtime_and_same_size_is_no_longer_shown_as_approved(tmp_path):
    """曾经:SHA 缓存按 (路径, mtime, size) 命中,同长度改动并恢复 mtime 后仍显示 approved=true。现在每次请求重新哈希。"""
    import hashlib
    res(tmp_path, pending={"a.py": "x=1\n"})
    p = tmp_path / "pending_strategies" / "a.py"
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    ctl = tmp_path / "data" / "research-control"
    ctl.mkdir(parents=True, exist_ok=True)
    (ctl / "approvals.json").write_text(json.dumps({sha: {"name": "a.py"}}), encoding="utf-8")
    assert col.collect_research(S(tmp_path), NOW).section["data"]["pending"][0]["approved"] is True
    st = p.stat()
    p.write_bytes(b"x=2\n")                                          # 同长度
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))                 # 恢复 mtime
    assert col.collect_research(S(tmp_path), NOW).section["data"]["pending"][0]["approved"] is False


# ---- G8 第 3 轮 #8:运行结果必须符合结构,不能把 {} 显示成正常的 backtest ----
def _runs_of(tmp_path):
    r = col.collect_research(S(tmp_path), NOW)
    return r, r.section["data"]["recent_runs"]


@pytest.mark.parametrize("body", [
    {}, {"kind": "mystery", "strategy_file": "d.py"}, {"kind": "gate"}, {"kind": "gate", "passed": "yes", "strategy_file": "d.py"},
    {"kind": "evaluate", "strategy_file": "d.py"}, {"kind": "backtest", "strategy_file": "d.py"},
    {"kind": "gate", "passed": True, "strategy_file": 7}, [], "text", {"kind": "gate", "passed": True, "strategy_file": "d.py", "run_id": "20200101-000000-000000"},
])
def test_a_run_result_that_does_not_match_the_schema_is_unreadable_not_a_normal_backtest(tmp_path, body):
    res(tmp_path, runs=[(RID, body)])
    r, runs = _runs_of(tmp_path)
    assert runs == []
    assert r.level == "warn" and "无法读取" in r.section["message"]
    assert col.get_run(S(tmp_path), RID) is None


def test_valid_runs_of_each_kind_are_accepted(tmp_path):
    base = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "ran_at": "2026-11-12T21:00:00", "report": "结论: x"}
    ok = [("20261112-210000-000001", {**base, "kind": "backtest", "stats": backtest_stats_row({"total_trade_count": 156, "end_balance": 20000.0, "sharpe_ratio": 1.2, "total_return": 0.1, "max_ddpercent": -12.5, "max_drawdown": -2500.0, "total_net_pnl": 200.0, "total_commission": 100.0, "total_slippage": 50.0})}),
          ("20261112-210000-000002", {**base, "kind": "backtest", "status": "strategy_error", "error": {"type": "X", "message": "m"}}),
          ("20261112-210000-000003", {**base, "kind": "gate", "passed": False, "failures": ["f"]}),
          ("20261112-210000-000005", {**base, "kind": "evaluate", "status": "strategy_error", "passed": False, "error": {"type": "X", "message": "m"}}),
          ("20261112-210000-000004", RUN)]
    res(tmp_path, runs=ok)
    r, runs = _runs_of(tmp_path)
    assert len(runs) == 5 and r.level == "ok"


@pytest.mark.parametrize("patch", [
    {"_no_run_id": True}, {"run_id": "20200101-000000-000000"}, {"strategy_file": ""}, {"strategy_file": "a/b.py"}, {"strategy_file": "x.txt"},
    {"strategy_sha256": "xyz"}, {"strategy_sha256": None}, {"report": None}, {"report": ""}, {"failures": None}, {"failures": "f"},
])
def test_a_minimal_gate_result_missing_required_fields_is_not_accepted_as_a_pass(tmp_path, patch):
    """codex 第 3 轮复核 #8:只有 kind/strategy_file/passed 的残缺文件曾显示成『通过』。"""
    body = {"kind": "gate", "passed": True, "strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r", "failures": [], "ran_at": "x"}
    body.update(patch)
    res(tmp_path, runs=[(RID, body)])
    r, runs = _runs_of(tmp_path)
    assert runs == [] and r.level == "warn"


@pytest.mark.parametrize("body", [
    {"kind": "backtest", "stats": {}}, {"kind": "backtest", "stats": []}, {"kind": "backtest", "status": "strategy_error"},
    {"kind": "evaluate", "status": "strategy_error", "passed": False},
])
def test_empty_stats_or_a_strategy_error_without_error_detail_is_not_accepted(tmp_path, body):
    full = {"strategy_file": "d.py", "strategy_sha256": "b" * 64, "report": "r", "ran_at": "x", **body}
    res(tmp_path, runs=[(RID, full)])
    assert _runs_of(tmp_path)[1] == []


def test_invalid_runs_older_than_the_displayed_window_are_still_counted(tmp_path):
    """曾经只检查最近 N 个文件:更早的损坏文件永远不会被发现。"""
    good = [(f"20261112-2100{i:02d}-000000", RUN) for i in range(12)]
    bad = [("20261101-000000-000000", {})]
    res(tmp_path, runs=good + bad)
    r, runs = _runs_of(tmp_path)
    assert len(runs) == 10 and r.level == "warn" and "1 个运行结果" in r.section["message"]


def test_an_oversized_document_is_marked_as_truncated_not_silently_cut(tmp_path):
    """运行手册被砍掉一半还显示成完整的一篇,人会照着缺步骤的操作——必须能看出来。"""
    from dashboard import collectors as col
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "unattended-log.md").write_text("x" * (col.MAX_DOC_BYTES + 100), encoding="utf-8")
    s = config.load(path=tmp_path / "none.json", root=tmp_path)
    s.raw["docs"]["unattended-log"] = docs / "unattended-log.md"
    d = col.get_doc(s, "unattended-log")
    assert d is not None and d["truncated"] is True
    assert len(d["markdown"]) == col.MAX_DOC_BYTES


def test_a_document_within_the_limit_is_not_marked_truncated(tmp_path):
    from dashboard import collectors as col
    docs = tmp_path / "docs"
    docs.mkdir(parents=True)
    (docs / "unattended-log.md").write_text("# 标题\n正文\n", encoding="utf-8")
    s = config.load(path=tmp_path / "none.json", root=tmp_path)
    s.raw["docs"]["unattended-log"] = docs / "unattended-log.md"
    d = col.get_doc(s, "unattended-log")
    assert d is not None and d["truncated"] is False and d["markdown"].startswith("# 标题")


def test_the_run_detail_exposes_the_backtest_statistics(tmp_path):
    """回测的数字在 stats 里(收益/夏普/往返笔数)。少了它,前端只能从报告文本里读,画不了图。"""
    row = backtest_stats_row({"total_trade_count": 156, "end_balance": 20000.0, "sharpe_ratio": 1.2,
                               "total_return": 0.1, "max_ddpercent": -12.5, "max_drawdown": -2500.0,
                               "total_net_pnl": 200.0, "total_commission": 100.0, "total_slippage": 50.0})
    res(tmp_path, runs=[(RID, {"kind": "backtest", "strategy_file": "d.py",
                               "strategy_sha256": "b" * 64, "ran_at": "2026-11-12T21:00:00",
                               "report": "沙盒回测", "stats": row})])
    d = col.get_run(S(tmp_path), RID)
    assert d is not None and d["stats"] is not None
    assert d["stats"]["round_trips"] == 78 and d["stats"]["sharpe"] == 1.2
    assert d["metrics"] is None
