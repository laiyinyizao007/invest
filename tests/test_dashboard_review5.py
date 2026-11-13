"""无人值守验收表与状态页都不能把『不可信的日志』算作『无异常』。

`clean` 的语义:『没有事件』**且**『这份日志可信』。日志不可信时,没看到事件不等于没有事件。
"""
from evidence import logstats as ls


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_clean_requires_trust_not_merely_the_absence_of_events(tmp_path):
    p = _write(tmp_path, "vt_20261112.log",
               "2026-11-12 08:00:00 | INFO | Runtime | 已启动\n".encode()
               + "2026-11-12 09:00:00 | ERROR | Runtime | 硬锁定".encode()[:-3])   # 砍掉最后一个字
    stats = ls.parse_day_log(p)
    assert stats.trusted is False
    assert stats.clean is False, "不可信的日志不能算作『无异常』"
    assert "无异常" not in stats.as_row()
    assert "无法确认" in stats.as_row()


def test_a_trusted_log_with_no_events_is_clean(tmp_path):
    p = _write(tmp_path, "vt_20261112.log", "2026-11-12 08:00:00 | INFO | Runtime | 已启动\n".encode())
    stats = ls.parse_day_log(p)
    assert stats.trusted is True and stats.clean is True
    assert "无异常" in stats.as_row()


def test_merging_two_files_of_the_same_day_keeps_the_untrusted_flag(tmp_path):
    """同一天有两个日志文件(比如夜盘跨零点),其中一个不可信 → 那天整体不可信。"""
    good = _write(tmp_path, "vt_20261112.log", "2026-11-12 08:00:00 | INFO | Runtime | 已启动\n".encode())
    bad_dir = tmp_path / "second"
    bad_dir.mkdir()
    bad = _write(bad_dir, "vt_20261112.log",
                 "2026-11-12 08:00:00 | INFO | Runtime | 已启动\n".encode()
                 + "2026-11-12 09:00:00 | ERROR | Runtime | 硬锁定".encode()[:-3])
    days = ls.collect_days([tmp_path, bad_dir])
    assert len(days) == 1
    assert days[0].trusted is False and days[0].clean is False


def test_the_clean_streak_does_not_count_an_untrusted_day(tmp_path):
    """不可信的一天不能算进『连续无异常交易日』。"""
    from datetime import date

    days = []
    for offset in range(3):
        day = date(2026, 11, 9 + offset).isoformat()          # 周一、周二、周三
        days.append(ls.DayStats(day=day, trusted=True))
    broken = ls.DayStats(day="2026-11-12", trusted=False)
    days.append(broken)
    assert ls.longest_clean_streak(days) == 3                # 周四不可信 → 连续段在周三结束
    assert ls.longest_clean_streak(days + [ls.DayStats(day="2026-11-13", trusted=True)]) == 3
