"""scripts/unattended_report.py 的纯逻辑测试(不联网、不连 CTP)。"""
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import unattended_report as u  # noqa: E402


def write_log(directory: Path, name: str, lines: list[str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_normal_day_has_no_events(tmp_path):
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:01:02.000 启动耗时(自发起连接): 行情登录=0.5s",
        "2026-11-10 09:01:03.000 [INFO] 已启动: trend_atr 运行于 c2701.DCE,权益 20000000",
        "2026-11-10 09:05:00.000 [INFO] 成交: c2701.DCE 多开 1@2400",
        "2026-11-10 15:00:00.000 [INFO] 停止: 正常停止",
    ])
    stats = u.parse_day_log(path)
    assert stats.day == "2026-11-10"
    assert stats.started == 1, "同一次启动的'启动耗时'与'已启动'不能数成两次"
    assert stats.trades == 1
    assert stats.events == {}
    assert stats.clean is True


def test_a_restart_is_counted_as_two_starts(tmp_path):
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:00:00.000 [INFO] 已启动: a",
        "2026-11-10 10:00:00.000 [INFO] 已启动: b",
    ])
    assert u.parse_day_log(path).started == 2


def test_abnormal_events_are_counted_and_classified(tmp_path):
    path = write_log(tmp_path, "vt_20261111.log", [
        "2026-11-11 09:00:00.000 [FATAL] 对账失败,拒绝启动: 持仓不一致",
        "2026-11-11 10:00:00.000 [WARN] 行情停更: 交易时段内 90s 没有收到 Tick",
        "2026-11-11 10:00:01.000 [WARN] 行情停更: 又一次",
        "2026-11-11 11:00:00.000 [ERROR] 网关连接断开",
    ])
    stats = u.parse_day_log(path)
    assert stats.events[("严重", "对账失败")] == 1
    assert stats.events[("警告", "行情停更")] == 2
    assert stats.events[("警告", "连接断开")] == 1
    assert stats.clean is False


def test_a_log_line_with_the_word_trade_but_not_a_trade_is_not_counted(tmp_path):
    """心跳日志里有"策略持仓 0"这类字样,不能被当成成交。"""
    path = write_log(tmp_path, "vt_20261112.log", [
        "2026-11-12 09:00:00.000 [INFO] 心跳: 运行中。权益 20000000,策略持仓 0",
    ])
    assert u.parse_day_log(path).trades == 0


def test_collect_days_merges_multiple_directories(tmp_path):
    write_log(tmp_path / "a", "vt_20261113.log", ["2026-11-13 09:00:00.000 已启动"])
    write_log(tmp_path / "b", "vt_20261113.log", ["2026-11-13 10:00:00.000 [INFO] 成交: x"])
    days = u.collect_days([tmp_path / "a", tmp_path / "b"])
    assert len(days) == 1
    assert days[0].trades == 1


def test_collect_days_can_filter_one_day(tmp_path):
    write_log(tmp_path, "vt_20261114.log", ["2026-11-14 09:00:00.000 已启动"])
    write_log(tmp_path, "vt_20261115.log", ["2026-11-15 09:00:00.000 已启动"])
    days = u.collect_days([tmp_path], day_filter="2026-11-15")
    assert [d.day for d in days] == ["2026-11-15"]


def test_heartbeat_state_reports_a_dead_process(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text(str(time.time()), encoding="utf-8")
    assert "前" in u.heartbeat_state(path)

    path.write_text(str(time.time() - 7200), encoding="utf-8")
    assert u.heartbeat_state(path).startswith("⚠")


def test_heartbeat_state_handles_a_missing_file(tmp_path):
    assert u.heartbeat_state(tmp_path / "nope") == "无心跳"


def test_replace_section_keeps_the_manual_parts():
    text = "# 标题\n\n人工段落\n\n" + u.BEGIN_MARK + "\n旧内容\n" + u.END_MARK + "\n\n结尾\n"
    new = u.replace_section(text, u.BEGIN_MARK, u.END_MARK, "新内容")
    assert "人工段落" in new and "结尾" in new
    assert "新内容" in new and "旧内容" not in new


def test_replace_section_refuses_a_document_without_markers():
    text = "# 人工维护的文档\n"
    assert u.replace_section(text, u.BEGIN_MARK, u.END_MARK, "x") == text


def test_the_local_probe_detects_both_success_and_failure():
    """离线自检:本地接收端确认消息送达,且飞书式的业务失败能被识别。"""
    ok, msg = u.probe_webhook_local()
    assert ok is True, msg


def test_the_local_probe_says_the_phone_is_not_verified():
    ok, msg = u.probe_webhook_local()
    assert "未涉及真实 webhook" in msg


def test_the_flush_timeout_covers_all_retries():
    """等待时间必须够"重试次数 × 单次超时 + 退避",否则慢端会被误判成没送达。"""
    limit = u._flush_timeout(10.0, (1.0, 3.0))
    assert limit > 10.0 * 3 + 1.0 + 3.0, limit


def test_a_clean_day_row_says_no_exception():
    stats = u.DayStats(day="2026-11-10")
    assert "无异常" in stats.as_row()


def test_acceptance_checklist_mentions_the_five_day_requirement():
    checklist = u.acceptance_checklist([u.DayStats(day="2026-11-10")])
    text = "\n".join(checklist)
    assert "5 个交易日" in text
    assert "心跳" in text


def test_the_clean_streak_counts_consecutive_days_not_the_total():
    """验收要求"连续 ≥ 5 个交易日",中间夹一个有异常的日子连续性就断了。"""
    days = [u.DayStats(day="2026-11-02"),
            u.DayStats(day="2026-11-03"),
            u.DayStats(day="2026-11-04", events=Counter({("WARN", "网关连接失败"): 1})),
            u.DayStats(day="2026-11-05"),
            u.DayStats(day="2026-11-06")]
    assert u.longest_clean_streak(days) == 2


def test_the_clean_streak_counts_a_fully_clean_run():
    """11-02(周一)~11-06(周五)是 5 个交易日;11-07 是周六,不是交易日。"""
    days = [u.DayStats(day=f"2026-11-{d:02d}") for d in range(2, 8)]
    assert u.longest_clean_streak(days) == 5


def test_a_weekend_with_logs_is_not_counted_as_a_trading_day():
    """容器常驻,周末也会写日志(周五夜盘跨过零点还会写进周六的文件);
    周末不是交易日,不能计入"连续 5 个交易日"。"""
    days = [u.DayStats(day="2026-11-06"),      # 周五
            u.DayStats(day="2026-11-07"),      # 周六(有日志)
            u.DayStats(day="2026-11-08")]      # 周日(有日志)
    assert u.longest_clean_streak(days) == 1


def test_a_clean_weekday_run_is_not_inflated_by_the_weekend_logs():
    """周一到下周一 = 6 个交易日(中间两个周末日不计)。"""
    days = [u.DayStats(day=f"2026-11-{d:02d}") for d in range(2, 10)]
    assert u.longest_clean_streak(days) == 6


def test_a_serious_event_on_a_weekend_breaks_the_clean_streak():
    """夜盘跨零点的事故(落在周六的日志)不能因为是周末就被放过。"""
    days = [u.DayStats(day="2026-11-06"),      # 周五
            u.DayStats(day="2026-11-07", events=Counter({("严重", "触发紧急平仓"): 1})),
            u.DayStats(day="2026-11-09")]      # 周一
    assert u.longest_clean_streak(days) == 1


def test_a_warning_on_a_weekend_does_not_break_the_clean_streak():
    """周末空闲期的连接抖动只是警告 —— 让它打断连续段,
    "连续 5 个交易日"就永远达不成了。"""
    days = [u.DayStats(day="2026-11-06"),      # 周五
            u.DayStats(day="2026-11-07", events=Counter({("警告", "连接断开"): 1})),
            u.DayStats(day="2026-11-09")]      # 周一
    assert u.longest_clean_streak(days) == 2


def test_the_clean_streak_of_an_empty_run_is_zero():
    assert u.longest_clean_streak([]) == 0


def test_the_checklist_reports_the_longest_clean_streak():
    days = [u.DayStats(day=f"2026-11-{d:02d}") for d in range(2, 7)]
    text = "\n".join(u.acceptance_checklist(days))
    assert "最长连续无异常 5 天" in text


def test_the_heartbeat_belongs_to_one_day_only(tmp_path):
    """心跳是"当前进程"的状态,挂到历史日期上会误导人。"""
    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text(str(time.time()), encoding="utf-8")
    day = u.heartbeat_day(heartbeat)
    days = [u.DayStats(day="2026-11-09"), u.DayStats(day=day)]
    table = u.render_table(days, heartbeat="12s 前", heartbeat_on=day)
    rows = [line for line in table.splitlines() if line.startswith("| 2026-")]
    assert "12s 前" in rows[-1]
    assert "12s 前" not in rows[0]


def test_the_heartbeat_day_is_computed_in_the_exchange_timezone(tmp_path):
    """mtime 是 epoch 秒,按本机时区换算会算错日期。

    宿主机是 UTC-8、容器是 Asia/Shanghai,差 16 小时:容器里 11-10 08:00 的心跳
    按宿主机时区算是 11-09 16:00 → 挂到错误的那一行。
    """
    import os
    from datetime import datetime

    heartbeat = tmp_path / "heartbeat"
    heartbeat.write_text("0", encoding="utf-8")
    # 交易所时区的 2026-11-10 08:00
    epoch = datetime(2026, 11, 10, 8, 0, 0, tzinfo=u.EXCHANGE_TZ).timestamp()
    os.utime(heartbeat, (epoch, epoch))
    assert u.heartbeat_day(heartbeat) == "2026-11-10"


def test_a_missing_heartbeat_is_still_shown_in_the_table():
    """心跳文件不存在 = 进程可能已经死了,这条证据不能从表里消失。"""
    days = [u.DayStats(day="2026-11-09"), u.DayStats(day="2026-11-10")]
    table = u.render_table(days, heartbeat="无心跳", heartbeat_on="")
    rows = [line for line in table.splitlines() if line.startswith("| 2026-")]
    assert "无心跳" in rows[-1]


def test_a_missing_heartbeat_file_has_no_day():
    assert u.heartbeat_day(Path("no-such-heartbeat")) == ""


def test_a_gateway_failure_is_reported_as_a_serious_event():
    """网关连不上是"严重"级事件,不能混进普通告警里。"""
    assert "网关连接失败" in u.EVENT_MARKERS
    assert u.EVENT_MARKERS["网关连接失败"][0] == "严重"


def test_the_timestamp_parser_accepts_milliseconds():
    """真实日志行带毫秒,按秒解析会失败 → 整天的起止时间全丢。"""
    assert u._parse_ts("2026-11-10 15:36:11.123 | INFO | RiskEngine | x") == "15:36:11"
    assert u._parse_ts("不是日志行") == ""


def test_the_day_is_derived_from_the_log_file_name():
    assert u._day_from_filename(Path("vt_20261110.log")) == "2026-11-10"
    assert u._day_from_filename(Path("vt_weird.log")) == "weird"


def test_a_day_with_events_lists_them():
    from collections import Counter
    stats = u.DayStats(day="2026-11-10", events=Counter({("严重", "网关连接失败"): 2}))
    assert "网关连接失败×2" in stats.as_row()


def test_the_report_needs_at_least_one_flag():
    assert u.main([]) == 2


def test_an_empty_evidence_table_is_not_evidence(tmp_path):
    """没有日志却退出 0,会让人误以为"当天正常"。"""
    code = u.main(["--report", "--runtime-dir", str(tmp_path),
                   "--out", str(tmp_path / "log.md")])
    assert code == 1


def test_the_report_is_written_from_the_container_logs(tmp_path):
    log_dir = tmp_path / "vntrader" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "vt_20261110.log").write_text(
        "2026-11-10 09:00:01.000 | INFO | MainEngine | 已启动\n"
        "2026-11-10 09:30:00.000 | INFO | CtaStrategy | 成交 buy c2701\n",
        encoding="utf-8")
    out = tmp_path / "log.md"
    code = u.main(["--report", "--runtime-dir", str(tmp_path), "--out", str(out)])
    assert code == 0
    text = out.read_text(encoding="utf-8")
    assert "2026-11-10" in text and u.BEGIN_MARK in text


def test_the_report_refuses_to_overwrite_a_document_without_markers(tmp_path):
    log_dir = tmp_path / "vntrader" / "log"
    log_dir.mkdir(parents=True)
    (log_dir / "vt_20261110.log").write_text("2026-11-10 09:00:01.000 | INFO | x | 已启动\n",
                                              encoding="utf-8")
    out = tmp_path / "log.md"
    out.write_text("# 人工维护的文档\n", encoding="utf-8")
    code = u.main(["--report", "--runtime-dir", str(tmp_path), "--out", str(out)])
    assert code == 2
    assert out.read_text(encoding="utf-8") == "# 人工维护的文档\n"


def test_the_probe_falls_back_to_the_local_endpoint_when_no_url_is_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(u, "load_env", lambda: {})
    assert u.main(["--probe-webhook"]) == 0


# ---------------- 静默死亡:缺天必须打断连续段 ----------------

def test_a_day_without_any_log_breaks_the_clean_streak():
    """进程静默死亡的那天根本没有日志文件 —— 只按"有日志的天"算,连续段会被拉长。"""
    days = [u.DayStats(day="2026-11-02"),      # 周一
            u.DayStats(day="2026-11-03"),      # 周二
            # 2026-11-04(周三)整个缺失:进程静默死亡
            u.DayStats(day="2026-11-05"),
            u.DayStats(day="2026-11-06")]
    assert u.longest_clean_streak(days) == 2


def test_the_missing_weekdays_are_listed_for_manual_review(tmp_path):
    days = [u.DayStats(day="2026-11-02"),
            u.DayStats(day="2026-11-03"),
            u.DayStats(day="2026-11-06")]
    missing = u.missing_weekdays(days)
    assert "2026-11-04" in missing and "2026-11-05" in missing
    assert "2026-11-03" not in missing


def test_a_weekend_without_a_log_is_not_a_missing_day():
    """周末本来就不是交易日,没有日志很正常,不该当成缺天。"""
    days = [u.DayStats(day="2026-11-06"),      # 周五
            u.DayStats(day="2026-11-09")]      # 周一
    assert u.missing_weekdays(days) == []
    assert u.longest_clean_streak(days) == 2


def test_the_checklist_mentions_the_missing_days(tmp_path):
    days = [u.DayStats(day="2026-11-02"),
            u.DayStats(day="2026-11-06")]
    text = "\n".join(u.acceptance_checklist(days))
    assert "缺" in text and "2026-11-03" in text


# ---------------- 熔断类告警必须被算成异常 ----------------

def test_the_circuit_breaker_alerts_are_counted_as_events(tmp_path):
    """守卫与运行时发出的熔断类告警都在日志里,漏了就把熔断日当成干净的一天。"""
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:10:00.000 | ERROR | RiskManager | [ERROR] 当日熔断:当日权益 9300",
        "2026-11-10 09:20:00.000 | ERROR | RiskManager | [FATAL] 累计死线触发:回撤超过死线",
        "2026-11-10 09:30:00.000 | ERROR | RiskManager | [ERROR] 委托被拒:c2701.DCE 多开 1",
    ])
    stats = u.parse_day_log(path)
    assert stats.events[("严重", "当日熔断")] == 1
    assert stats.events[("严重", "累计死线触发")] == 1
    assert stats.events[("警告", "委托被拒")] == 1
    assert stats.clean is False


def test_the_normal_end_of_an_emergency_flatten_is_not_an_event(tmp_path):
    """"紧急平仓完成"是正常收尾,不能算异常;"[FATAL] 紧急平仓:"才是。"""
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:10:00.000 | ERROR | RiskManager | [INFO] 紧急平仓完成:账户已空仓。",
    ])
    stats = u.parse_day_log(path)
    assert stats.events == {}
    assert stats.clean is True


def test_triggering_an_emergency_flatten_is_an_event(tmp_path):
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:10:00.000 | ERROR | RiskManager | [FATAL] 紧急平仓:累计死线触发",
    ])
    stats = u.parse_day_log(path)
    assert stats.events[("严重", "触发紧急平仓")] == 1
    assert stats.clean is False


def test_a_failed_emergency_flatten_is_counted_once(tmp_path):
    """"紧急平仓失败"既命中自己的标记,也带 [FATAL] 前缀 —— 不能重复计数。"""
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:10:00.000 | ERROR | RiskManager | [FATAL] 紧急平仓失败:账户仍有持仓",
    ])
    stats = u.parse_day_log(path)
    assert stats.events[("严重", "紧急平仓失败")] == 1
    assert ("严重", "触发紧急平仓") not in stats.events


# ---------------- stdout 编码必须在解析参数之前修好 ----------------

def test_the_help_text_prints_without_a_unicode_error(capsys):
    """argparse 的用法说明里有中文:控制台是 cp1252 时必须先修好 stdout。"""
    code = u.main([])
    assert code == 2
    assert "无人值守" in capsys.readouterr().out


def test_manual_intervention_makes_the_day_dirty_and_severe(tmp_path):
    """验收要求"人工干预次数 = 0":出现非策略委托告警的那天不能算干净,连续段要断。"""
    path = write_log(tmp_path, "vt_20261110.log", [
        "2026-11-10 09:01:03.000 [INFO] 已启动: trend_atr 运行于 c2701.DCE",
        "2026-11-10 10:00:00.000 [ERROR] 检测到非策略委托: c2701.DCE 多开 1@2400",
    ])
    stats = u.parse_day_log(path)
    assert stats.events == Counter({("严重", "非策略委托"): 1})
    assert not stats.clean and stats.has_severe
    assert any("人工干预次数 = 0" in line for line in u.acceptance_checklist([stats]))
