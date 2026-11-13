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
    os.utime(p, (NOW - 1, NOW - 1))        # 夹具文件的 mtime 对齐到测试时钟(NOW 是虚构的 2027 年,真实 mtime 会被正确判为落后几个月)
    return p


def sampled(seconds_ago=1.0):
    """带时区的权益采样时间(风控进程写的就是这个形态)。"""
    from datetime import datetime, timedelta, timezone
    return datetime.fromtimestamp(NOW - seconds_ago, timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def age_guard(path, seconds):
    """把风控文件整体『变旧』:采样时间与 mtime 一起(两者本来就是一起前进的)。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sampled_at"] = sampled(seconds)
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, (NOW - seconds, NOW - seconds))


def guard(**over):
    g = {"sampled_at": sampled(), "account_id": ACCOUNT, "last_equity": 9870.5, "peak_equity": 10120.0, "day": "2026-11-13",
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


# ---------------------------------------------------------------- G8 第 2 轮审查(codex)发现

@pytest.mark.parametrize("content", ["garbage", "not-a-number", "", "nan", "inf", "-5", "0", "12abc"])
def test_a_corrupt_heartbeat_is_corrupt_and_never_falls_back_to_file_mtime(tmp_path, content):
    """曾经:内容不可解析时退回文件 mtime,刚写入的垃圾文件会被显示成『运行中』。"""
    p = put(tmp_path, "heartbeat", content)
    os.utime(p, (NOW - 1, NOW - 1))                                          # mtime 很新,但内容是垃圾
    put(tmp_path, "account_guard.json", guard())
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.section["data"] is None and r.level == "unknown"
    assert "running" not in json.dumps(r.section)


def test_a_heartbeat_far_in_the_future_is_not_fresh_evidence(tmp_path):
    """曾经:max(now - ts, 0) 把任何未来时间戳当成 0 秒新鲜(时钟回拨、文件异常后可长期显示正常)。"""
    put(tmp_path, "heartbeat", str(NOW + 500))
    put(tmp_path, "account_guard.json", guard())
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.level == "unknown" and "未来" in r.section["message"]


def test_small_clock_skew_into_the_future_is_tolerated(tmp_path):
    put(tmp_path, "heartbeat", str(NOW + 20))
    put(tmp_path, "account_guard.json", guard())
    s = run(tmp_path).section
    assert s["state"] == "ok" and s["age_seconds"] == 0.0


def test_a_stale_guard_file_is_stale_even_when_the_heartbeat_is_fresh(tmp_path):
    """风控状态运行时至少每 60 秒落盘一次(risk/account_guard.py);落后很多说明权益/盈亏/锁定状态不是当前值。"""
    put(tmp_path, "heartbeat", str(NOW - 2))
    g = put(tmp_path, "account_guard.json", guard())
    age_guard(g, 86400)
    r = run(tmp_path)
    assert r.section["state"] == "stale" and r.level == "unknown"
    assert "权益" in r.section["message"] and r.section["data"]["guard"]["age_seconds"] >= 86000
    assert r.section["data"]["heartbeat"]["process"] == "running"             # 进程在跑这个事实本身没错,错的是风控数字可能过期


def test_a_fresh_guard_reports_its_own_age(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2))
    g = put(tmp_path, "account_guard.json", guard())
    age_guard(g, 30)
    guard_data = run(tmp_path).section["data"]["guard"]
    assert guard_data["age_seconds"] == 30.0 and guard_data["as_of"].endswith("+08:00")


def test_a_hard_lock_is_still_critical_when_the_guard_file_is_stale(tmp_path):
    """陈旧的『已硬锁定』也必须报警:锁定状态不会因为文件旧了就失效。"""
    put(tmp_path, "heartbeat", str(NOW - 2))
    g = put(tmp_path, "account_guard.json", guard(hard_locked=True, hard_reason="累计回撤"))
    age_guard(g, 86400)
    r = run(tmp_path)
    assert r.level == "critical" and r.section["state"] == "stale"


def test_a_guard_file_dated_in_the_future_is_not_fresh(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2))
    g = put(tmp_path, "account_guard.json", guard())
    os.utime(g, (NOW + 9999, NOW + 9999))
    assert run(tmp_path).section["state"] in ("corrupt", "stale", "unknown")


def test_an_oversized_heartbeat_file_is_corrupt_not_read_into_memory(tmp_path):
    """内容是一个完全合法的时间戳加十万个空格——strip() 之后 float() 能解析,所以只有大小上限能拦住它。
    (曾用 '1'*100000 做输入:它会溢出成 inf 被 isfinite 兜住,测不到大小上限。)"""
    put(tmp_path, "heartbeat", str(NOW - 2) + " " * 100_000)
    put(tmp_path, "account_guard.json", guard())
    assert run(tmp_path).section["state"] == "corrupt"


def test_a_heartbeat_just_under_the_size_limit_is_still_accepted(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2) + " " * 900)
    put(tmp_path, "account_guard.json", guard())
    assert run(tmp_path).section["state"] == "ok"


# ---- G8 第 3 轮:严格解码与 JSON 非法常量 ----
def test_invalid_utf8_inside_a_json_string_is_corrupt_not_silently_repaired(tmp_path):
    """errors='replace' 会把非法字节变成 U+FFFD,文件被『修复』后仍是合法 JSON、被判 ok;非法编码本身就说明文件不可信。"""
    healthy(tmp_path)
    p = tmp_path / "data" / "state" / "account_guard.json"
    p.write_bytes(json.dumps(guard(hard_reason="XX")).encode("utf-8").replace(b"XX", bytes([0xFF, 0xFE])))
    os.utime(p, (NOW - 1, NOW - 1))
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.level != "ok"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_json_constants_nan_and_infinity_are_corrupt_even_in_unused_fields(tmp_path, bad):
    healthy(tmp_path)
    text = json.dumps(guard(extra="@@")).replace('"@@"', bad)
    put(tmp_path, "account_guard.json", text)
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.level != "ok"


def test_heartbeat_with_invalid_utf8_is_corrupt(tmp_path):
    healthy(tmp_path)
    p = tmp_path / "data" / "state" / "heartbeat"
    p.write_bytes(str(NOW - 4).encode() + bytes([0x0A, 0xFF]))
    os.utime(p, (NOW - 1, NOW - 1))
    assert run(tmp_path).section["state"] == "corrupt"


def test_outbox_with_invalid_utf8_is_not_reported_as_empty(tmp_path):
    healthy(tmp_path)
    p = put(tmp_path, "webhook_outbox.jsonl", "")
    p.write_bytes(b'{"time": "2027-01-15T00:00:00"}\n\xff\xfe\n')
    os.utime(p, (NOW - 1, NOW - 1))
    alerts = col.collect_alerts(settings(tmp_path), NOW)
    assert alerts.section["state"] == "corrupt" or alerts.section["data"]["outbox"]["state"] == "corrupt"


# ---- G8 第 3 轮 #4:年龄按权益采样时间算,不按文件写入时间 ----
def test_a_freshly_written_file_with_an_old_sample_is_stale(tmp_path):
    """网关断开后风控进程仍每分钟落盘:mtime 是新的,权益却是一小时前的。"""
    put(tmp_path, "heartbeat", str(NOW - 2))
    put(tmp_path, "account_guard.json", guard(sampled_at=sampled(3600)))          # put 把 mtime 设为 NOW-1
    r = run(tmp_path)
    assert r.section["state"] == "stale" and r.level == "unknown"
    g = r.section["data"]["guard"]
    assert g["age_seconds"] >= 3600 and "_sampled_ts" not in g


def test_a_guard_file_without_sampled_at_is_not_reported_fresh(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2))
    g = guard()
    del g["sampled_at"]
    put(tmp_path, "account_guard.json", g)
    r = run(tmp_path)
    assert r.section["state"] == "stale" and "采样时间" in r.section["message"]
    assert r.section["data"]["guard"]["as_of"] is None


def test_sampled_at_null_is_also_unverifiable(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2))
    put(tmp_path, "account_guard.json", guard(sampled_at=None))
    assert run(tmp_path).section["state"] == "stale"


@pytest.mark.parametrize("bad", ["2027-01-15T16:00:00", "not-a-time", 123, "", ["x"]])
def test_a_malformed_or_timezone_less_sampled_at_is_corrupt(tmp_path, bad):
    """无时区的字符串会被当成 UTC,而风控进程写本地时间,差 8 小时——不猜,直接 corrupt。"""
    put(tmp_path, "heartbeat", str(NOW - 2))
    put(tmp_path, "account_guard.json", guard(sampled_at=bad))
    r = run(tmp_path)
    assert r.section["state"] == "corrupt" and r.level != "ok"


def test_a_sample_time_in_the_future_is_not_fresh(tmp_path):
    put(tmp_path, "heartbeat", str(NOW - 2))
    put(tmp_path, "account_guard.json", guard(sampled_at=sampled(-9999)))
    assert run(tmp_path).section["state"] == "stale"
