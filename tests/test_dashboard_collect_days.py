"""collect_alerts / collect_days:基于每日日志与待补发队列。日志是外部输入,必须容忍脏数据且不泄露原文。"""
import json
import os

from dashboard import collectors as col, config

NOW = 1_800_000_000.0           # 2027-01-15 08:00:00 UTC 附近;日志文件名里的日期与它无关,只看相对关系


def settings(tmp_path):
    return config.load(path=tmp_path / "none.json", root=tmp_path)


def log(tmp_path, day, lines, dirname="runtime/vntrader/log"):
    """写一份每日日志。把行首的日期改成文件名的日期,保证夹具自洽(面板要求『记录日期 = 文件名日期』,
    否则被移植的日志会被判不可信——那条规则有专门的测试,不要靠夹具自相矛盾来触发)。"""
    d = tmp_path / dirname
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"vt_{day}.log"
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    fixed = [(iso + ln[10:]) if len(ln) > 10 and ln[4] == "-" and ln[7] == "-" else ln for ln in lines]
    p.write_text("\n".join(fixed) + "\n", encoding="utf-8")
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
    assert r.section["state"] != "ok" and r.level != "ok"      # 含非法字节的日志不可信:不能被『修复』成正常的一天
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
    cold, incremental = [], _spy(monkeypatch)
    real_full = col._full_parse
    monkeypatch.setattr(col, "_full_parse", lambda path, key, st: (cold.append(path.name), real_full(path, key, st))[1])
    s = settings(tmp_path)
    col.collect_days(s, NOW)
    col.collect_alerts(s, NOW)
    col.collect_overview(s, NOW)
    assert sorted(cold) == ["vt_20261111.log", "vt_20261112.log"]          # 每个文件只做一次冷解析,跨 collector、跨请求共用
    assert incremental == []                                                # 之后没有任何增量数据读取:日志没变


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


# ---------------------------------------------------------------- G8 第 2 轮(codex):垃圾日志不是证据

def _garbage_log(tmp_path, day="20261112", body=b"garbage only\nnothing recognisable here\n"):
    d = tmp_path / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"vt_{day}.log"
    p.write_bytes(body)
    os.utime(p, (NOW - 5, NOW - 5))
    return p


def test_a_fresh_garbage_log_is_not_a_clean_day_and_not_evidence_of_activity(tmp_path):
    """曾经:只排除了空文件;任何新鲜的非空垃圾文件都被解释成『干净的一天』,并给 alerts 提供『数据仍新鲜』的证据。"""
    _garbage_log(tmp_path)
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.section["state"] in ("corrupt", "missing"), collect.__name__
        assert r.level == "unknown" and r.section["data"] is None, collect.__name__


def test_garbage_does_not_hide_behind_a_valid_log_but_also_does_not_poison_it(tmp_path):
    log(tmp_path, "20261111", CLEAN)
    _garbage_log(tmp_path)                                                   # 另一天是垃圾
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    assert [x["day"] for x in d["days"]] == ["2026-11-11"]                    # 垃圾那天不出现,更不会显示成『无异常』


def test_a_log_with_one_recognisable_line_among_noise_counts(tmp_path):
    p = _garbage_log(tmp_path, body=b"junk\n2026-11-12 09:00:01.000 | INFO | r | \xe5\xb7\xb2\xe5\x90\xaf\xe5\x8a\xa8\njunk\n")
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    assert d["days"][0]["started"] == 1


def test_touching_an_old_garbage_file_cannot_make_alerts_look_fresh(tmp_path):
    old = log(tmp_path, "20261110", CLEAN)
    os.utime(old, (NOW - 100 * 3600, NOW - 100 * 3600))
    _garbage_log(tmp_path)                                                   # mtime 很新
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.section["state"] == "stale" and r.section["age_seconds"] >= 99 * 3600


def test_logs_that_exist_but_are_all_unreadable_are_corrupt_with_an_honest_message_not_missing(tmp_path):
    """『没找到日志』(进程可能没跑过)与『日志在但读不懂』(格式变了/被写坏)对排查方向完全不同。"""
    _garbage_log(tmp_path)
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.section["state"] == "corrupt", collect.__name__
        assert "无法识别" in r.section["message"], collect.__name__
    assert col.collect_days(settings(tmp_path), NOW).level == "unknown"


def test_no_log_files_at_all_is_still_missing(tmp_path):
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "missing"
    (tmp_path / "runtime" / "vntrader" / "log").mkdir(parents=True)
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "missing"


# ---------------------------------------------------------------- 读取上限(codex #10):异常增长的文件不能在单次请求里耗尽内存

def test_an_oversized_outbox_is_reported_not_read_into_memory(tmp_path):
    ob = tmp_path / "data" / "state" / "webhook_outbox.jsonl"
    ob.parent.mkdir(parents=True)
    ob.write_bytes(b'{"time": "2026-11-12T09:00:00"}\n' * 400_000)           # ~12 MB,远超上限
    log(tmp_path, "20261112", CLEAN)
    r = col.collect_alerts(settings(tmp_path), NOW)
    assert r.section["state"] == "corrupt" and r.level == "unknown" and "异常增长" in (r.section["message"] or "")


def test_an_outbox_just_under_the_limit_is_still_read(tmp_path):
    ob = tmp_path / "data" / "state" / "webhook_outbox.jsonl"
    ob.parent.mkdir(parents=True)
    ob.write_bytes(entry(10)["time"].encode() and (json.dumps(entry(10)) + "\n").encode() * 50)
    log(tmp_path, "20261112", CLEAN)
    assert col.collect_alerts(settings(tmp_path), NOW).section["data"]["outbox"]["pending"] == 50


def test_read_json_refuses_huge_state_files(tmp_path):
    from dashboard import models
    p = tmp_path / "big.json"
    p.write_text('{"a": "' + "x" * 3_000_000 + '"}', encoding="utf-8")
    data, err = models.read_json(p, retries=0)
    assert data is None and err == "corrupt"



def test_a_replaced_log_whose_tail_bytes_happen_to_match_is_still_fully_reparsed(tmp_path):
    """(d) 复核:增量解析的前缀指纹若只看已读部分的末尾 64 字节,会被『旧长度处的末尾相同、更早内容不同、文件更长』
    的替换文件骗过,导致新文件的计数混进旧解析器的累计值。指纹还要包含文件开头的字节。

    构造要点(上一版测试没真正构造出碰撞,所以在弱实现下也会通过):新文件的前 len(old) 个字节里,最后 64 字节必须与
    旧文件的最后 64 字节逐字节相同,而更早的内容不同;然后再追加内容使文件更长,旧偏移落在新文件内部。"""
    tail = "2026-11-12 09:59:59.000 | INFO | runtime | 公共尾部 用来让末尾六十四个字节完全相同 ................\n"
    old = "2026-11-12 09:00:01.000 | ERROR | x | [ERROR] 对账失败\n" * 3 + tail
    old_b = old.encode("utf-8")
    tail_b = tail.encode("utf-8")
    pad_len = len(old_b) - len(tail_b)
    filler_line = "2026-11-12 08:00:00.000 | INFO | y | 完全不同的开头\n".encode("utf-8")
    filler = (filler_line * (pad_len // len(filler_line) + 1))[:pad_len]
    new_b = filler + tail_b + "2026-11-12 10:00:00.000 | WARNING | z | 连接断开\n".encode("utf-8")
    assert new_b[: len(old_b)][-64:] == old_b[-64:], "构造失败:旧长度处的末尾 64 字节应当相同"
    assert new_b[:64] != old_b[:64] and len(new_b) > len(old_b)
    p = tmp_path / "runtime" / "vntrader" / "log" / "vt_20261112.log"
    p.parent.mkdir(parents=True)
    p.write_bytes(old_b)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    first = col.collect_days(s, NOW).section["data"]["days"][0]
    assert any(e["name"] == "对账失败" and e["count"] == 3 for e in first["events"])
    p.write_bytes(new_b)
    got = col.collect_days(s, NOW).section["data"]["days"][0]
    col._PARSE_CACHE.clear()
    cold = col.collect_days(s, NOW).section["data"]["days"][0]
    assert got == cold
    assert not any(e["name"] == "对账失败" for e in got["events"]), "旧文件的计数不应混进新文件"


# ---------------------------------------------------------------- codex 复核 #3:日志的未来 mtime

def test_a_log_dated_in_the_future_is_not_fresh_evidence(tmp_path):
    """曾经:freshness() 用 max(now - as_of, 0),日志 mtime 在未来会被当成『刚更新』(心跳与风控文件有未来检查,日志漏了)。"""
    p = log(tmp_path, "20261112", CLEAN)
    os.utime(p, (NOW + 86400, NOW + 86400))
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.section["state"] == "corrupt" and r.level == "unknown", collect.__name__
        assert "未来" in r.section["message"], collect.__name__


def test_small_clock_skew_in_a_log_mtime_is_tolerated(tmp_path):
    p = log(tmp_path, "20261112", CLEAN)
    os.utime(p, (NOW + 20, NOW + 20))
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "ok"


def test_freshness_is_the_shared_place_for_the_future_check():
    from dashboard import models
    assert models.freshness(NOW + 10_000, NOW, 60) == "corrupt"
    assert models.freshness(NOW + 20, NOW, 60) == "ok"
    assert models.freshness(NOW - 10, NOW, 60) == "ok" and models.freshness(NOW - 100, NOW, 60) == "stale"
    assert models.freshness(None, NOW, 60) == "unknown"


def test_a_future_log_mtime_never_hides_a_severe_event_in_the_log_content(tmp_path):
    """时间戳怪不能掩盖日志内容里的严重事件:区块 state 是 corrupt,但 level 仍是 critical。"""
    p = log(tmp_path, "20261112", SEVERE)
    os.utime(p, (NOW + 86400, NOW + 86400))
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.level == "critical", collect.__name__


# ---------------------------------------------------------------- codex 复核 #1:同长度的中间改写

def test_a_same_length_rewrite_in_the_middle_with_head_and_tail_untouched_is_detected(tmp_path):
    """codex 的复现:首次日志无异常;保持开头与末尾字节不变,把中间 12 个 ASCII 字节原地换成同为 12 字节的『启动失败』。
    头 64 + 尾 64 字节的指纹原样不变,缓存曾继续使用旧结果,严重事件永久不显示。现在校验整个已读前缀的哈希。"""
    head = "2026-11-12 09:00:01.000 | INFO | runtime | " + "x" * 40 + "\n"
    middle_old = "2026-11-12 09:00:02.000 | INFO | runtime | abcdefghijkl 正常运行\n"
    middle_new = "2026-11-12 09:00:02.000 | INFO | runtime | 启动失败 正常运行\n"
    tail = "2026-11-12 09:00:03.000 | INFO | runtime | " + "y" * 40 + "\n"
    assert len("abcdefghijkl".encode()) == len("启动失败".encode()) == 12
    old_b = (head + middle_old + tail).encode("utf-8")
    new_b = (head + middle_new + tail).encode("utf-8")
    assert len(old_b) == len(new_b) and old_b[:64] == new_b[:64] and old_b[-64:] == new_b[-64:] and old_b != new_b
    p = tmp_path / "runtime" / "vntrader" / "log" / "vt_20261112.log"
    p.parent.mkdir(parents=True)
    p.write_bytes(old_b)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    assert col.collect_days(s, NOW).section["data"]["days"][0]["events"] == []
    st = p.stat()
    p.write_bytes(new_b)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))        # mtime 变了(哪怕只是更新),大小没变
    got = col.collect_days(s, NOW).section["data"]["days"][0]
    col._PARSE_CACHE.clear()
    cold = col.collect_days(s, NOW).section["data"]["days"][0]
    assert got == cold
    assert any(e["name"] == "启动失败" for e in got["events"]), "同长度的中间改写必须被发现"


def test_a_rewrite_that_also_grows_the_file_and_keeps_head_and_tail_of_the_old_part_is_detected(tmp_path):
    head = "2026-11-12 09:00:01.000 | INFO | runtime | " + "x" * 40 + "\n"
    middle_old = "2026-11-12 09:00:02.000 | INFO | runtime | abcdefghijkl 正常运行\n"
    middle_new = "2026-11-12 09:00:02.000 | INFO | runtime | 启动失败 正常运行\n"
    tail = "2026-11-12 09:00:03.000 | INFO | runtime | " + "y" * 40 + "\n"
    old_b = (head + middle_old + tail).encode("utf-8")
    new_b = (head + middle_new + tail).encode("utf-8") + "2026-11-12 09:00:04.000 | INFO | runtime | 追加\n".encode("utf-8")
    p = tmp_path / "runtime" / "vntrader" / "log" / "vt_20261112.log"
    p.parent.mkdir(parents=True)
    p.write_bytes(old_b)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    p.write_bytes(new_b)
    got = col.collect_days(s, NOW).section["data"]["days"][0]
    col._PARSE_CACHE.clear()
    assert got == col.collect_days(s, NOW).section["data"]["days"][0]
    assert any(e["name"] == "启动失败" for e in got["events"])


def test_pure_appends_are_still_read_incrementally_not_reparsed(tmp_path, monkeypatch):
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    old_end = p.stat().st_size
    read = _spy(monkeypatch)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write("2026-11-12 22:30:00.000 | ERROR | x | [ERROR] 对账失败\n")
    col.collect_days(s, NOW)
    assert read == [("vt_20261112.log", old_end)]                           # 数据读取仍然只从旧末尾开始


def test_touching_a_log_without_changing_it_keeps_the_cache(tmp_path, monkeypatch):
    """只是 mtime 变了、内容没变(比如被备份工具碰了一下):前缀哈希相同,不需要重新解析。"""
    p = log(tmp_path, "20261112", CLEAN)
    s = settings(tmp_path)
    col._PARSE_CACHE.clear()
    col.collect_days(s, NOW)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    read = _spy(monkeypatch)
    col.collect_days(s, NOW)
    assert read == []


# ---------------------------------------------------------------- codex 复核 #2:日志可信度

def _raw_log(tmp_path, day, text):
    d = tmp_path / "runtime" / "vntrader" / "log"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"vt_{day}.log"
    p.write_text(text, encoding="utf-8")
    os.utime(p, (NOW - 5, NOW - 5))
    return p


def test_codex_exact_repro_one_plausible_line_plus_garbage_is_not_a_clean_day(tmp_path):
    """codex 的复现:第一行勉强像时间戳,第二行是任意垃圾 -> 曾判为 state=ok, clean=True。"""
    _raw_log(tmp_path, "20261112", "2026-11-12 09:00:00 | INFO | boot\nTHIS IS ARBITRARY GARBAGE WITHOUT LOG FORMAT\n")
    r = col.collect_days(settings(tmp_path), NOW)
    assert r.section["state"] == "corrupt" and r.level == "unknown"


def test_a_line_must_look_like_a_full_record_not_just_start_with_a_date(tmp_path):
    """只取前 19 字符能当时间戳的行,不一定是一条日志记录。要求『时间 | 级别 | 来源』的完整形状。"""
    for bad in ("2026-11-12 09:00:00 plus arbitrary text that is not a log record at all\n",
                "2026-11-12 09:00:00\n", "2026-11-12 09:00:00 | | |\n", "2026-11-12 09:00:00 | not a level | x | y\n"):
        _raw_log(tmp_path, "20261112", bad * 3)
        assert col.collect_days(settings(tmp_path), NOW).section["state"] == "corrupt", repr(bad)


def test_records_must_belong_to_the_day_in_the_file_name(tmp_path):
    """被移植/重命名的日志:内容是另一天的,不能当作这一天『无异常』的证据。"""
    _raw_log(tmp_path, "20261112", "2026-10-01 09:00:00.000 | INFO | r | 已启动\n" * 5)
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "corrupt"


def test_tracebacks_with_continuation_lines_do_not_make_a_real_log_untrusted(tmp_path):
    """真实日志里的多行 traceback:续行不是记录,但少量续行不能让整份日志被判不可信。"""
    lines = ["2026-11-12 09:00:00.000 | INFO | r | 已启动"] + ["2026-11-12 09:00:%02d.000 | INFO | r | 心跳" % i for i in range(1, 11)]
    lines += ["2026-11-12 09:01:00.000 | ERROR | x | 策略异常", "Traceback (most recent call last):", '  File "x.py", line 1, in f', "ValueError: boom"]
    _raw_log(tmp_path, "20261112", "\n".join(lines) + "\n")
    r = col.collect_days(settings(tmp_path), NOW)
    assert r.section["state"] == "ok" and r.section["data"]["days"][0]["started"] == 1


def test_mostly_garbage_with_a_few_valid_records_is_untrusted(tmp_path):
    lines = ["2026-11-12 09:00:00.000 | INFO | r | 已启动"] + ["garbage line %d" % i for i in range(50)]
    _raw_log(tmp_path, "20261112", "\n".join(lines) + "\n")
    assert col.collect_days(settings(tmp_path), NOW).section["state"] == "corrupt"


def test_an_unreadable_log_is_surfaced_even_when_another_day_is_fine(tmp_path):
    """曾经:只要有一个正常日志,其他损坏日志就被静默排除;今天的日志坏了却显示『昨天是干净的』。"""
    log(tmp_path, "20261111", CLEAN)
    _raw_log(tmp_path, "20261112", "garbage only\nmore garbage\n")
    for collect in (col.collect_days, col.collect_alerts):
        r = collect(settings(tmp_path), NOW)
        assert r.level == "unknown", collect.__name__
        assert "无法识别" in json.dumps(r.reasons, ensure_ascii=False) or "无法识别" in (r.section["message"] or ""), collect.__name__
    d = col.collect_days(settings(tmp_path), NOW).section["data"]
    assert [x["day"] for x in d["days"]] == ["2026-11-11"]                          # 数据仍显示好的那天,但级别不是 ok


def test_real_log_format_is_trusted(tmp_path):
    """用真实日志的样子(取自 runtime/vntrader/log)确认判据不会误伤。"""
    real = ("2026-11-10 13:58:29.727 | INFO | RiskEngine | 风控规则[ActiveOrderRule]加载成功\n"
            "2026-11-10 13:58:29.728 | WARNING | CTP | 连接断开 8193\n"
            "2026-11-10 13:58:30.001 | ERROR | MainEngine | 启动失败: x\n")
    _raw_log(tmp_path, "20261110", real)
    assert col.collect_days(settings(tmp_path), NOW).section["data"]["days"][0]["started"] == 0
