"""G8 第四轮自查:日志尾部被截断时,不能把这一天算成『无异常』。

codex 的第三轮指出过这个方向,但当时的修复只覆盖了『截断落在多字节字符中间』。
**截断落在字符边界上**(更常见)同样会隐藏严重事件:解码完全成功,没有任何异常信号。
"""
from evidence import logstats as ls

PREFIX = "2026-11-13 08:00:00 | INFO | Runtime | 已启动\n".encode("utf-8")


def _parse(data: bytes) -> ls.DayStats:
    parser = ls.DayLogParser("2026-11-13")
    parser.feed(data)
    return parser.snapshot("x")


def _severe_line(marker: str) -> bytes:
    return f"2026-11-13 09:00:00 | ERROR | Runtime | {marker}\n".encode("utf-8")


def test_a_complete_severe_event_is_detected():
    stats = _parse(PREFIX + _severe_line("硬锁定"))
    assert stats.trusted is True and stats.has_severe is True


def test_a_severe_event_truncated_at_a_character_boundary_is_still_not_trusted():
    """『硬锁定』被截成『硬』:解码完全成功,没有坏字节信号,但关键字不匹配 → 曾被判为干净的一天。"""
    full = "2026-11-13 09:00:00 | ERROR | Runtime | 硬锁定".encode("utf-8")
    stats = _parse(PREFIX + full[:-3])                    # 砍掉最后一个字(整字,不是半个字节)
    assert stats.trusted is False, "尾部被截断的日志不能被当成可信"
    assert stats.has_severe is False


def test_a_severe_event_truncated_in_the_middle_of_a_character_is_not_trusted():
    full = "2026-11-13 09:00:00 | ERROR | Runtime | 硬锁定".encode("utf-8")
    stats = _parse(PREFIX + full[:-1])                    # 砍掉半个字
    assert stats.trusted is False


def test_a_truncated_line_that_cannot_be_a_severe_marker_is_still_trusted():
    """普通日志写到一半被截断(比如正在写 INFO 行):不该因此整天不可信——那会让正在写入的日志一直显示未知。"""
    full = "2026-11-13 09:00:00 | INFO | Runtime | 普通信息".encode("utf-8")
    stats = _parse(PREFIX + full[:-3])                    # 砍掉一整个字(字符边界上)
    assert stats.trusted is True


def test_a_truncated_marker_prefix_is_never_trusted():
    """任何严重/警告关键字的『前缀』都不能被当成没有事件。"""
    for marker in ls.EVENT_MARKERS:
        for cut in range(1, len(marker)):
            line = f"2026-11-13 09:00:00 | ERROR | Runtime | {marker[:cut]}".encode("utf-8")
            stats = _parse(PREFIX + line)
            assert stats.trusted is False, (marker, cut)


def test_the_truncation_only_applies_to_the_last_line():
    """中间的行有换行结尾,就是完整的;只有没有换行的最后一行才可能是写了一半。"""
    line = "2026-11-13 09:00:00 | ERROR | Runtime | 硬锁定".encode("utf-8")
    stats = _parse(PREFIX + line + b"\n" + PREFIX)        # 最后一行完整(有换行)
    assert stats.trusted is True and stats.has_severe is True
