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


def test_an_empty_log_file_created_just_now_is_not_evidence_of_activity(tmp_path):
    """回归(真实数据发现):交易进程停了几十小时,别的进程却新建了一个 0 字节的当天日志,
    曾让『日志』和『告警』区块显示『刚更新、ok』。空文件不是活动证据。"""
    old = log(tmp_path, "20261111", CLEAN)
    os.utime(old, (NOW - 100 * 3600, NOW - 100 * 3600))
    empty = tmp_path / "runtime" / "vntrader" / "log" / "vt_20261113.log"
    empty.write_bytes(b"")
    os.utime(empty, (NOW, NOW))
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.section["state"] == "stale", collect.__name__
        assert r.section["age_seconds"] >= 99 * 3600


def test_the_age_comes_from_the_last_logged_line_when_the_file_has_content(tmp_path):
    p = log(tmp_path, "20261111", CLEAN)
    os.utime(p, (NOW - 2 * 3600, NOW - 2 * 3600))
    assert abs(col.collect_days(settings(tmp_path), NOW).section["age_seconds"] - 2 * 3600) < 2


def test_only_empty_logs_means_missing_not_a_clean_week(tmp_path):
    d = tmp_path / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True)
    (d / "vt_20261111.log").write_bytes(b"")
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "missing"


# ---------------------------------------------------------------- 解析缓存(G8 审查发现:大日志下总览超过轮询间隔)

def test_unchanged_logs_are_read_only_once_across_collectors_and_requests(tmp_path, monkeypatch):
    log(tmp_path, "20261111", CLEAN)
    log(tmp_path, "20261112", SEVERE)
    col._PARSE_CACHE.clear()
    read = _spy(monkeypatch)
    s = settings(tmp_path)
    col.collect_days(s, NOW)
    col.collect_alerts(s, NOW)
    col.collect_overview(s, NOW)
    assert sorted(name for name, _ in read) == ["vt_20261111.log", "vt_20261112.log"]      # 每个文件只读一次,且都是从头读(冷)
    assert all(start == 0 for _, start in read)


def test_a_changed_log_is_read_from_its_old_end_and_unchanged_ones_are_not_read_at_all(tmp_path, monkeypatch):
    log(tmp_path, "20261111", CLEAN)
    p2 = log(tmp_path, "20261112", SEVERE)
    col._PARSE_CACHE.clear()
    s = settings(tmp_path)
    col.collect_days(s, NOW)
    old_end = p2.stat().st_size
    read = _spy(monkeypatch)
    with open(p2, "a", encoding="utf-8") as fh:                                  # 当天日志在增长
        fh.write("2026-11-12 22:30:00.000 | ERROR | x | [ERROR] 对账失败: 持仓漂移\n")
    d = col.collect_days(s, NOW).section["data"]
    assert read == [("vt_20261112.log", old_end)]                                # 只读变化文件的新增部分;历史日志一个字节都不读
    assert "对账失败" in {e["name"] for day in d["days"] for e in day["events"]}


def test_cache_does_not_serve_stale_results_when_size_changes_but_mtime_is_equal(tmp_path):
    p = log(tmp_path, "20261112", CLEAN)
    col._PARSE_CACHE.clear()
    s = settings(tmp_path)
    col.collect_days(s, NOW)
    mt = p.stat().st_mtime
    p.write_text(p.read_text(encoding="utf-8") + "2026-11-12 09:05:00.000 | ERROR | x | [ERROR] 对账失败\n", encoding="utf-8")
    os.utime(p, (mt, mt))                                                        # 精度不足的文件系统上 mtime 可能不变
    names = {e["name"] for day in col.collect_days(s, NOW).section["data"]["days"] for e in day["events"]}
    assert "对账失败" in names


def test_parse_cache_is_bounded(tmp_path):
    col._PARSE_CACHE.clear()
    for i in range(col.PARSE_CACHE_MAX + 20):
        p = tmp_path / f"vt_2026{i // 28 + 1:02d}{i % 28 + 1:02d}.log"
        p.write_text("2026-11-12 09:00:01.000 | INFO | r | 已启动\n", encoding="utf-8")
        col._parse_cached(p)
    assert len(col._PARSE_CACHE) <= col.PARSE_CACHE_MAX


# ---------------------------------------------------------------- 增量解析接入面板

def _spy(monkeypatch):
    """记录每次从磁盘读了多少字节(而不是调用次数):增量的意义在于读得少。"""
    read = []
    real = col._read_range
    monkeypatch.setattr(col, "_read_range", lambda path, start, end=None: (read.append((path.name, start)), real(path, start, end))[1])
    return read


def test_a_growing_log_is_read_from_the_previous_offset_not_from_the_start(tmp_path, monkeypatch):
    p = log(tmp_path, "20261112", CLEAN)
    col._PARSE_CACHE.clear()
    read = _spy(monkeypatch)
    s = settings(tmp_path)
    col.collect_days(s, NOW)
    first_size = p.stat().st_size
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("2026-11-12 22:30:00.000 | ERROR | x | [ERROR] 对账失败\n")
    read.clear()
    d = col.collect_days(s, NOW).section["data"]
    assert read == [("vt_20261112.log", first_size)]                              # 只从旧末尾开始读
    assert "对账失败" in {e["name"] for day in d["days"] for e in day["events"]}


def test_incremental_result_equals_a_cold_parse_after_many_appends(tmp_path):
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    for i in range(30):
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"2026-11-12 10:{i:02d}:00.000 | WARNING | x | 连接断开 {i}\n")
            if i % 7 == 0:
                fh.write("2026-11-12 10:59:59.000 | INFO | r | 已启动\n")
        warm = col.collect_days(s, NOW).section["data"]
    col._PARSE_CACHE.clear()
    cold = col.collect_days(s, NOW).section["data"]
    assert warm == cold


def test_a_truncated_or_rotated_log_is_fully_reparsed(tmp_path):
    p = log(tmp_path, "20261112", SEVERE)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    p.write_text("2026-11-12 09:00:01.000 | INFO | r | 已启动\n", encoding="utf-8")        # 变小了
    d = col.collect_days(s, NOW).section["data"]
    assert d["days"][0]["events"] == [] and d["days"][0]["started"] == 1


def test_a_replaced_log_that_happens_to_be_larger_is_not_misread_as_an_append(tmp_path):
    """整个文件被换成另一个更大的文件:size 变大但前缀内容不同,不能从旧偏移继续读。"""
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    other = ["2026-11-12 08:00:01.000 | ERROR | x | [ERROR] 硬锁定: 回撤"] * 20
    p.write_text("\n".join(other) + "\n", encoding="utf-8")
    assert p.stat().st_size > 0
    d = col.collect_days(s, NOW).section["data"]
    assert {e["name"] for e in d["days"][0]["events"]} == {"硬锁定"} and d["days"][0]["events"][0]["count"] == 20


def test_a_half_written_last_line_is_not_counted_until_it_is_complete(tmp_path):
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    with open(p, "ab") as fh:
        fh.write("2026-11-12 22:30:00.000 | ERROR | x | [ERROR] 对账".encode("utf-8"))      # 写了一半
    before = col.collect_days(s, NOW).section["data"]
    assert not any(e["name"] == "对账失败" for day in before["days"] for e in day["events"])
    with open(p, "ab") as fh:
        fh.write("失败\n".encode("utf-8"))
    after = col.collect_days(s, NOW).section["data"]
    assert sum(e["count"] for day in after["days"] for e in day["events"] if e["name"] == "对账失败") == 1


def test_concurrent_requests_do_not_corrupt_the_cache(tmp_path):
    import threading
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    errors = []

    def worker():
        try:
            for _ in range(30):
                with open(p, "a", encoding="utf-8") as fh:
                    fh.write("2026-11-12 10:00:00.000 | WARNING | x | 连接断开\n")
                col.collect_days(s, NOW)
        except Exception as exc:                                 # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    warm = col.collect_days(s, NOW).section["data"]
    col._PARSE_CACHE.clear()
    assert warm == col.collect_days(s, NOW).section["data"]      # 并发结束后缓存结果与冷解析一致


def test_warm_up_primes_the_cache_so_the_first_request_does_not_pay_the_cold_parse(tmp_path, monkeypatch):
    log(tmp_path, "20261111", CLEAN)
    log(tmp_path, "20261112", SEVERE)
    col._PARSE_CACHE.clear()
    s = settings(tmp_path)
    col.warm_up(s)
    read = _spy(monkeypatch)
    col.collect_overview(s, NOW)
    assert read == []                                           # 预热之后第一个请求不再读任何日志数据


def test_warm_up_never_raises_even_when_everything_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(col, "_days", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    col.warm_up(settings(tmp_path))                             # 预热只是优化,失败不能阻止服务启动
