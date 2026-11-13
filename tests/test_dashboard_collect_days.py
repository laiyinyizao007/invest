"""collect_alerts / collect_days:基于每日日志与待补发队列。日志是外部输入,必须容忍脏数据且不泄露原文。"""
import json
import os

from dashboard import collectors as col, config

NOW = 1_800_000_000.0           # 2027-01-15 08:00:00 UTC 附近;日志文件名里的日期与它无关,只看相对关系


def settings(tmp_path):
    return config.load(path=tmp_path / "none.json", root=tmp_path)


def log(tmp_path, day, lines, dirname="runtime/vntrader/log"):
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"vt_{day}.log"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


CLEAN = ["2026-11-11 09:00:01.123 | INFO | runtime | 已启动", "2026-11-11 09:00:02.000 | INFO | runtime | [INFO] 成交: c2701.DCE 多开 1@2400"]
SEVERE = ["2026-11-12 21:00:03.000 | INFO | runtime | 已启动",
          "2026-11-12 22:00:00.000 | ERROR | guard | [ERROR] 当日熔断: 权益下降 650",
          "2026-11-12 22:01:00.000 | WARNING | x | 行情停更 61 秒", "2026-11-12 22:02:00.000 | WARNING | x | 行情停更 62 秒"]


def test_days_table_uses_the_shared_log_stats_and_maps_levels(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    log(tmp_path, "20261112", SEVERE)
    r = col.collect_days(settings(tmp_path), NOW, limit=14)
    d = r.section["data"]
    by_day = {x["day"]: x for x in d["days"]}
    assert [x["day"] for x in d["days"]] == ["2026-11-12", "2026-11-11"]           # 倒序
    assert by_day["2026-11-11"]["clean"] is True and by_day["2026-11-11"]["events"] == [] and by_day["2026-11-11"]["trades"] == 1
    sev = by_day["2026-11-12"]
    assert sev["has_severe"] is True and sev["clean"] is False
    names = {(e["level"], e["name"]): e["count"] for e in sev["events"]}
    assert names[("severe", "当日熔断")] == 1 and names[("warning", "行情停更")] == 2
    assert r.level == "critical"                                             # 最新一天有严重事件


def test_checklist_marks_only_what_can_be_computed(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    items = d["checklist"]
    assert items[0]["checked"] is False and "连续" in items[0]["text"]
    assert all(i["checked"] is None for i in items[1:])                      # 其余只能人工核对,不能显示为未勾选以外的判断
    assert d["required_streak"] == 5 and d["longest_clean_streak"] in (0, 1)
    assert not any(i["text"].startswith("- [") for i in items)               # Markdown 记号已去掉


def test_five_clean_weekdays_check_the_streak_item(tmp_path):
    for day in ("20261109", "20261110", "20261111", "20261112", "20261113"):     # 周一到周五
        log(tmp_path, day, [f"{day[:4]}-{day[4:6]}-{day[6:]} 09:00:01.000 | INFO | r | 已启动"])
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    assert d["longest_clean_streak"] == 5 and d["checklist"][0]["checked"] is True


def test_missing_weekdays_are_listed(tmp_path):
    log(tmp_path, "20261109", CLEAN)
    log(tmp_path, "20261112", CLEAN)
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    assert d["missing_weekdays"] == ["2026-11-10", "2026-11-11"]


def test_no_log_directory_means_missing_not_a_clean_week(tmp_path):
    r = col.collect_days(settings(tmp_path), NOW)
    assert r.section["state"] == "missing" and r.section["data"] is None and r.level == "unknown"


def test_empty_log_directory_is_missing_too(tmp_path):
    (tmp_path / "runtime" / "vntrader" / "log").mkdir(parents=True)
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "missing"


def test_stale_when_the_newest_log_is_old(tmp_path):
    p = log(tmp_path, "20261111", CLEAN)
    os.utime(p, (NOW - 5000, NOW - 5000))
    r = col.collect_days(settings(tmp_path), NOW)
    assert r.section["state"] == "stale" and r.level in ("unknown", "warn")


def test_limit_caps_the_number_of_days(tmp_path):
    for i in range(1, 9):
        log(tmp_path, f"2026110{i}", CLEAN)
    assert len(col.collect_days(settings(tmp_path), NOW, limit=3).section["data"]["days"]) == 3


def test_binary_garbage_in_a_log_does_not_crash_or_leak(tmp_path):
    d = tmp_path / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True)
    (d / "vt_20261111.log").write_bytes(b"\xff\xfe\x00garbage\n" + "2026-11-11 09:00:01.000 | INFO | r | 已启动\n".encode())
    r = col.collect_days(settings(tmp_path), NOW)
    assert r.section["state"] in ("ok", "stale")
    json.dumps(r.section, allow_nan=False)


# ---------------------------------------------------------------- alerts

def entry(age, **kw):
    """真实队列条目的形状(notify/webhook.py):level/title/message/extra/time,time 是本机本地时间的 ISO 字符串(无时区)。"""
    from datetime import datetime
    base = {"level": "ERROR", "title": "t", "message": "m", "extra": {}, "time": datetime.fromtimestamp(NOW - age).isoformat(timespec="seconds")}
    base.update(kw)
    return base


def outbox(tmp_path, rows):
    p = tmp_path / "data" / "state" / "webhook_outbox.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n", encoding="utf-8")
    return p


def test_alerts_report_pending_count_and_oldest_age_without_message_text(tmp_path):
    log(tmp_path, "20261112", SEVERE)
    outbox(tmp_path, [entry(400, title="当日熔断", message="账号 66170099 权益 8000"), entry(100, level="WARN")])
    r = col.collect_alerts(settings(tmp_path), NOW)
    d = r.section["data"]
    assert d["outbox"]["pending"] == 2 and abs(d["outbox"]["oldest_age_seconds"] - 400.0) <= 1.0
    assert "66170099" not in json.dumps(r.section, ensure_ascii=False)       # 队列正文永不返回
    assert r.level == "critical"                                             # 有严重事件优先于队列警告


def test_pending_alerts_alone_are_a_warning(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    outbox(tmp_path, [entry(50)])
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.level == "warn" and r.section["data"]["outbox"]["pending"] == 1


def test_empty_outbox_and_clean_logs_is_ok_and_explicitly_empty(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.section["state"] in ("ok", "stale")
    assert r.section["data"]["outbox"] == {"pending": 0, "oldest_age_seconds": None} and r.section["data"]["recent"] == []


def test_corrupt_outbox_lines_are_skipped_but_counted_conservatively(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    outbox(tmp_path, [entry(10), "{broken", "", "[1]"])
    d = col.collect_alerts(settings(tmp_path), NOW).section["data"]
    assert d["outbox"]["pending"] >= 1


def test_alerts_with_no_logs_and_no_outbox_is_missing(tmp_path):
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.section["state"] == "missing" and r.level == "unknown"
