"""collect_trading:心跳 + 风控状态 + 暂停标志 + 实盘准入。核心断言:缺失/过期/损坏绝不是 ok。"""
import json
import os

import pytest

from dashboard import config, collectors as col

NOW = 1_800_000_000.0
ACCOUNT = "66170099"


def settings(tmp_path):
    return config.load(path=tmp_path / "none.json", root=tmp_path)


def put(tmp_path, name, text):
    p = tmp_path / "data" / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text if isinstance(text, str) else json.dumps(text), encoding="utf-8")
    return p


def guard(**over):
    g = {"account_id": ACCOUNT, "last_equity": 9870.5, "peak_equity": 10120.0, "day": "2026-11-13",
         "day_start_equity": 10000.0, "daily_locked_day": None, "hard_locked": False, "hard_reason": ""}
    g.update(over)
    return g


def healthy(tmp_path, age=4.0, **over):
    put(tmp_path, "heartbeat", str(NOW - age))
    put(tmp_path, "account_guard.json", guard(**over))


def run(tmp_path):
    return col.collect_trading(settings(tmp_path), NOW)


def test_healthy_runtime_is_ok_and_derives_numbers_on_the_server(tmp_path):
    healthy(tmp_path)
    r = run(tmp_path)
    s, d = r.section, r.section["data"]
    assert s["state"] == "ok" and s["age_seconds"] == 4.0 and s["stale_after_seconds"] == 60.0
    assert d["heartbeat"]["process"] == "running"
    g = d["guard"]
    assert g["daily_pnl"] == -129.5 and g["drawdown_from_peak"] == 249.5
    assert g["limits"] == {"daily_loss_yuan": 600.0, "hard_drawdown_yuan": 2000.0}
    assert g["account_masked"] == "66****99" and ACCOUNT not in json.dumps(r.section)
    assert d["pause"] == {"paused": False, "since": None} and d["live_admission"] == {"approved": False}
    assert r.level == "ok" and "运行中" in r.headline


def test_missing_everything_is_missing_not_ok(tmp_path):
    r = run(tmp_path)
    assert r.section["state"] == "missing" and r.section["data"] is None
    assert r.level == "unknown" and r.reasons and "无法判断" in r.reasons[0]["text"]


def test_stale_heartbeat_is_stale_and_never_reports_running(tmp_path):
    healthy(tmp_path, age=36000.0)
    r = run(tmp_path)
    assert r.section["state"] == "stale"
    assert r.section["data"]["heartbeat"]["process"] == "stopped"
    assert r.level == "unknown"                              # 过期 -> 无法判断,不是 critical 也不是 ok
    assert "运行中" not in r.headline


def test_guard_without_heartbeat_cannot_prove_the_process_is_running(tmp_path):
    put(tmp_path, "account_guard.json", guard())
    r = run(tmp_path)
    assert r.section["state"] == "unknown" and r.section["data"]["heartbeat"]["process"] == "unknown"
    assert r.level == "unknown"


@pytest.mark.parametrize("text", ["", "{bad json", "[1, 2]", '"just a string"'])
def test_corrupt_guard_file_is_corrupt_with_no_data(tmp_path, text):
    put(tmp_path, "heartbeat", str(NOW - 1))
    put(tmp_path, "account_guard.json", text)
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.section["data"] is None
    assert r.level == "unknown"


def test_guard_with_wrong_field_types_is_corrupt_not_a_crash(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 1))
    put(tmp_path, "account_guard.json", guard(last_equity="oops"))
    assert run(tmp_path).section["state"] == "corrupt"


def test_guard_missing_required_keys_is_corrupt(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 1))
    put(tmp_path, "account_guard.json", {"account_id": ACCOUNT})
    assert run(tmp_path).section["state"] == "corrupt"


def test_hard_lock_is_critical_even_when_the_heartbeat_is_stale(tmp_path):
    healthy(tmp_path, age=36000.0, hard_locked=True, hard_reason="累计回撤超过 2000 元", last_equity=8000.0)
    r = run(tmp_path)
    assert r.level == "critical" and "硬锁定" in r.headline
    assert r.section["data"]["guard"]["hard_locked"] is True
    assert any(x["level"] == "critical" for x in r.reasons)


def test_daily_lock_for_the_current_guard_day_is_critical(tmp_path):
    healthy(tmp_path, daily_locked_day="2026-11-13")
    r = run(tmp_path)
    assert r.level == "critical" and "熔断" in r.headline


def test_old_daily_lock_is_not_critical(tmp_path):
    healthy(tmp_path, daily_locked_day="2026-11-12")
    assert run(tmp_path).level == "ok"


def test_pause_flag_makes_it_warn_and_reports_since(tmp_path):
    healthy(tmp_path)
    flag = put(tmp_path, config.PAUSE_FLAG_NAME, "")
    os.utime(flag, (NOW - 600, NOW - 600))
    r = run(tmp_path)
    assert r.section["data"]["pause"]["paused"] is True and r.section["data"]["pause"]["since"]
    assert r.level == "warn" and "暂停" in r.headline


def test_promotion_approval_file_is_reported_without_claiming_live_trading(tmp_path):
    healthy(tmp_path)
    put(tmp_path, "promotion_approval.json", {"x": 1})
    assert run(tmp_path).section["data"]["live_admission"] == {"approved": True}


def test_non_finite_numbers_in_the_guard_file_are_corrupt_not_passed_through(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 1))
    put(tmp_path, "account_guard.json",
        '{"account_id":"x","last_equity":NaN,"peak_equity":1,"day":"d","day_start_equity":1,"hard_locked":false}')
    r = run(tmp_path)
    assert r.section["state"] == "corrupt"
    json.dumps(r.section, allow_nan=False)


def test_future_heartbeat_after_clock_rollback_is_age_zero_not_negative(tmp_path):
    put(tmp_path, "heartbeat", str(NOW + 500))
    put(tmp_path, "account_guard.json", guard())
    s = run(tmp_path).section
    assert s["age_seconds"] == 0.0 and s["state"] == "ok"


def test_heartbeat_file_with_garbage_falls_back_to_mtime(tmp_path):
    p = put(tmp_path, "heartbeat", "not-a-number")
    os.utime(p, (NOW - 5, NOW - 5))
    put(tmp_path, "account_guard.json", guard())
    assert run(tmp_path).section["data"]["heartbeat"]["age_seconds"] == 5.0
