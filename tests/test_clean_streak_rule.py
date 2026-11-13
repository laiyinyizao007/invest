"""连续段的判定口径(用户 2026-11-13 拍板):**只有严重事件才打断**,警告不打断但必须显示出来。

验收标准要证明的是『无人值守也能运行』,不是『网络永不抖动』。夜盘一次重连就从头再数 5 天的话,
这个标准会逼着人去调标准,而不是去修问题。

但放宽不等于藏起来:
- 严重事件(对账失败、硬锁定、紧急平仓失败、非策略委托…)仍然打断;
- 不可信的日子(损坏、尾部不完整)仍然打断 —— 『没看到事件』不等于『没有事件』;
- 缺日志的工作日仍然打断 —— 进程静默死亡那天根本没有日志文件;
- 警告要被**数出来**,和连续段一起给出,不能让验收结论悄悄变宽。
"""
from collections import Counter

from evidence import logstats as ls


def _day(day, *events, trusted=True):
    return ls.DayStats(day=day, events=Counter({e: 1 for e in events}), trusted=trusted)


WARN = ("警告", "连接断开")
SEVERE = ("严重", "硬锁定")


def test_a_weekday_with_only_warnings_does_not_break_the_streak():
    days = [_day("2026-11-02"), _day("2026-11-03", WARN), _day("2026-11-04", WARN), _day("2026-11-05"), _day("2026-11-06")]
    assert ls.longest_clean_streak(days) == 5


def test_a_weekday_with_a_severe_event_still_breaks_the_streak():
    days = [_day("2026-11-02"), _day("2026-11-03"), _day("2026-11-04", SEVERE), _day("2026-11-05"), _day("2026-11-06")]
    assert ls.longest_clean_streak(days) == 2


def test_a_severe_event_hidden_among_warnings_still_breaks_the_streak():
    days = [_day("2026-11-02"), _day("2026-11-03", WARN, SEVERE), _day("2026-11-04")]
    assert ls.longest_clean_streak(days) == 1


def test_an_untrusted_weekday_still_breaks_the_streak_even_without_events():
    """日志损坏或尾部不完整:没看到事件不等于没有事件。"""
    days = [_day("2026-11-02"), _day("2026-11-03", trusted=False), _day("2026-11-04")]
    assert ls.longest_clean_streak(days) == 1


def test_a_missing_weekday_still_breaks_the_streak():
    days = [_day("2026-11-02"), _day("2026-11-04"), _day("2026-11-05")]      # 11-03(周二)没有日志
    assert ls.longest_clean_streak(days) == 2


def test_the_warnings_inside_the_streak_are_counted_not_hidden():
    days = [_day("2026-11-02"), _day("2026-11-03", WARN), _day("2026-11-04", WARN, ("警告", "行情停更")),
            _day("2026-11-05"), _day("2026-11-06")]
    assert ls.longest_clean_streak(days) == 5
    assert ls.streak_warning_count(days) == 3


def test_warnings_outside_the_best_streak_are_not_counted_in_it():
    days = [_day("2026-11-02", WARN), _day("2026-11-03", SEVERE), _day("2026-11-04"), _day("2026-11-05"), _day("2026-11-06")]
    assert ls.longest_clean_streak(days) == 3
    assert ls.streak_warning_count(days) == 0


def test_a_day_with_warnings_is_no_longer_clean_for_the_table_but_does_not_break_the_streak():
    """表里的『无异常』列仍然是『一个事件都没有』,与连续段是两回事。"""
    day = _day("2026-11-03", WARN)
    assert day.clean is False
    assert day.has_severe is False
