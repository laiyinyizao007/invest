"""evidence/logstats.py 的增量解析:任何切分方式得到的结果都必须与一次性全量解析逐字段相同。"""
import random
from collections import Counter

from evidence import logstats as ls

LINES = [
    "2026-11-12 09:00:01.100 | INFO | runtime | 已启动",
    "2026-11-12 09:00:01.200 | INFO | runtime | 启动耗时 1.2 秒",
    "2026-11-12 09:00:05.000 | INFO | runtime | [INFO] 成交: c2701.DCE 多开 1@2400",
    "2026-11-12 09:30:00.000 | WARNING | x | 连接断开 8193",
    "2026-11-12 09:31:00.000 | WARNING | x | 行情停更 61 秒",
    "",
    "2026-11-12 10:00:00.000 | ERROR | g | [ERROR] 当日熔断: 权益下降 650",
    "没有时间戳的一行 对账失败",
    "2026-11-12 10:05:00.000 | INFO | runtime | 已启动",
    "2026-11-12 10:06:00.000 | INFO | runtime | [FATAL] 紧急平仓: 触发",
    "2026-11-12 10:07:00.000 | INFO | runtime | 中文 日志 行 ✓ 成交 多字节",
]


def key(s):
    return (s.day, s.started, s.trades, sorted(s.events.items()), s.first_ts, s.last_ts)


def full(tmp_path, data: bytes):
    p = tmp_path / "vt_20261112.log"
    p.write_bytes(data)
    return key(ls.parse_day_log(p))


def incremental(tmp_path, data: bytes, cuts):
    parser = ls.DayLogParser("2026-11-12")
    prev = 0
    for c in list(cuts) + [len(data)]:
        parser.feed(data[prev:c])
        prev = c
    return key(parser.snapshot("x"))


def test_one_shot_incremental_equals_the_original_full_parse(tmp_path):
    data = ("\n".join(LINES) + "\n").encode("utf-8")
    assert incremental(tmp_path, data, []) == full(tmp_path, data)


def test_every_single_cut_point_gives_the_same_result_even_inside_a_line_or_a_multibyte_character(tmp_path):
    data = ("\n".join(LINES) + "\n").encode("utf-8")
    expected = full(tmp_path, data)
    for cut in range(len(data) + 1):
        assert incremental(tmp_path, data, [cut]) == expected, cut


def test_many_random_chunkings_agree(tmp_path):
    data = ("\n".join(LINES * 5) + "\n").encode("utf-8")
    expected = full(tmp_path, data)
    rnd = random.Random(42)
    for _ in range(200):
        cuts = sorted(rnd.sample(range(1, len(data)), rnd.randint(1, 12)))
        assert incremental(tmp_path, data, cuts) == expected, cuts


def test_a_half_written_line_that_matches_nothing_yet_does_not_count_and_is_not_double_counted_when_completed(tmp_path):
    """写了一半的行(标记还没写全)不匹配任何事件;补全后只数一次,不会『半行数一次、补全后又数一次』。"""
    parser = ls.DayLogParser("2026-11-12")
    parser.feed("2026-11-12 09:00:01.000 | INFO | r | 已启动\n2026-11-12 09:00:02.000 | WARNING | x | 连接断".encode("utf-8"))
    assert parser.snapshot("x").events == Counter()
    parser.feed("开 8193\n".encode("utf-8"))
    assert parser.snapshot("x").events == Counter({("警告", "连接断开"): 1})


def test_a_complete_last_line_without_a_trailing_newline_is_visible_in_the_snapshot_immediately(tmp_path):
    """G8 复核 #10:进程在写入严重事件后崩溃,最后一行没有换行——事件曾一直留在缓冲里,增量路径永远不显示它。
    snapshot() 应在缓冲的副本上处理尾行:展示当前所有已知内容,又不破坏后续继续追加的状态。"""
    data = "2026-11-12 09:00:01.000 | INFO | r | 已启动\n2026-11-12 09:00:02.000 | ERROR | g | [ERROR] 当日熔断: 权益下降 650".encode("utf-8")
    parser = ls.DayLogParser("2026-11-12")
    parser.feed(data)
    assert parser.snapshot("x").events == Counter({("严重", "当日熔断"): 1})
    assert key(parser.snapshot("x")) == full(tmp_path, data)


def test_a_tail_line_shown_early_is_not_counted_twice_when_the_newline_finally_arrives(tmp_path):
    parser = ls.DayLogParser("2026-11-12")
    parser.feed("2026-11-12 09:00:02.000 | ERROR | g | [ERROR] 当日熔断: 权益下降 650".encode("utf-8"))
    assert parser.snapshot("x").events == Counter({("严重", "当日熔断"): 1})
    parser.feed(b"\n")
    assert parser.snapshot("x").events == Counter({("严重", "当日熔断"): 1})            # 仍是 1,不是 2
    parser.feed("2026-11-12 09:00:03.000 | WARNING | x | 行情停更\n".encode("utf-8"))
    assert parser.snapshot("x").events == Counter({("严重", "当日熔断"): 1, ("警告", "行情停更"): 1})


def test_taking_a_snapshot_never_changes_what_later_snapshots_see(tmp_path):
    """快照是只读的:反复取快照、中间穿插追加,结果与从不取快照时相同。"""
    data = "".join(f"2026-11-12 09:00:{i:02d}.000 | WARNING | x | 连接断开 {i}\n" for i in range(20)).encode("utf-8") + "2026-11-12 09:01:00.000 | WARNING | x | 行情停更".encode("utf-8")
    quiet = ls.DayLogParser("2026-11-12")
    noisy = ls.DayLogParser("2026-11-12")
    for i in range(0, len(data), 37):
        quiet.feed(data[i:i + 37])
        noisy.feed(data[i:i + 37])
        noisy.snapshot("x")
        noisy.snapshot("x")
    assert key(quiet.snapshot("x")) == key(noisy.snapshot("x")) == full(tmp_path, data)


def test_started_uses_max_of_the_two_counters_not_their_sum(tmp_path):
    parser = ls.DayLogParser("2026-11-12")
    parser.feed("2026-11-12 09:00:01.000 | INFO | r | 已启动\n".encode())
    parser.feed("2026-11-12 09:00:01.100 | INFO | r | 启动耗时 1 秒\n".encode())
    parser.feed("2026-11-12 09:05:00.000 | INFO | r | 已启动\n".encode())
    assert parser.snapshot("x").started == 2                                     # 已启动×2 与 启动耗时×1 取较大者


def test_snapshot_does_not_expose_internal_state(tmp_path):
    parser = ls.DayLogParser("2026-11-12")
    parser.feed("2026-11-12 09:00:01.000 | WARNING | x | 连接断开\n".encode())
    snap = parser.snapshot("x")
    snap.events.clear()
    assert parser.snapshot("x").events == Counter({("警告", "连接断开"): 1})


def test_invalid_utf8_bytes_do_not_crash_and_match_errors_replace_semantics(tmp_path):
    data = b"\xff\xfe\x00bad\n" + "2026-11-12 09:00:01.000 | INFO | r | 已启动\n".encode()
    assert incremental(tmp_path, data, [3]) == full(tmp_path, data)


def test_exotic_line_separators_split_exactly_like_the_one_shot_parse(tmp_path):
    """str.splitlines() 把 CR、VT、FF、FS/GS/RS、NEL、U+2028/2029 也当行分隔符。增量版与全量版必须在这些字符上行为一致,
    不论它们出现在哪、切分点落在哪里。(『靠构造保证等价』的声明需要这条证据。)"""
    seps = ["\r", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", " ", " ", "\r\n"]
    for sep in seps:
        text = ("2026-11-12 09:00:01.000 | INFO | r | 已启动" + sep +
                "2026-11-12 09:00:02.000 | WARNING | x | 连接断开" + sep +
                "2026-11-12 09:00:03.000 | WARNING | x | 行情停更" + "\n")
        data = text.encode("utf-8")
        expected = full(tmp_path, data)
        for cut in range(len(data) + 1):
            assert incremental(tmp_path, data, [cut]) == expected, (repr(sep), cut)


def test_the_last_line_without_a_trailing_newline_is_counted_by_finish_like_the_full_parse(tmp_path):
    data = "2026-11-12 09:00:01.000 | INFO | r | 已启动\n2026-11-12 09:00:02.000 | WARNING | x | 连接断开".encode()
    parser = ls.DayLogParser("2026-11-12")
    parser.feed(data)
    assert key(parser.snapshot("x")) == full(tmp_path, data)     # 不必 finish():快照已包含尾行
    parser.finish()                                              # finish() 之后仍然一致(不重复计数)
    assert key(parser.snapshot("x")) == full(tmp_path, data)


def test_equivalence_also_holds_for_data_that_does_not_end_with_a_newline(tmp_path):
    """之前所有等价性测试的数据都以换行结尾,从未覆盖过『没有末尾换行』的尾行——正是 G8 复核发现的盲区。"""
    data = ("\n".join(LINES)).encode("utf-8")                    # 注意:没有结尾换行
    assert not data.endswith(b"\n")
    expected = full(tmp_path, data)
    for cut in range(len(data) + 1):
        assert incremental(tmp_path, data, [cut]) == expected, cut


def test_random_chunkings_agree_for_data_without_a_trailing_newline(tmp_path):
    data = ("\n".join(LINES * 5)).encode("utf-8")
    expected = full(tmp_path, data)
    rnd = random.Random(99)
    for _ in range(200):
        cuts = sorted(rnd.sample(range(1, len(data)), rnd.randint(1, 12)))
        assert incremental(tmp_path, data, cuts) == expected, cuts


def test_a_severe_event_on_the_unterminated_last_line_is_reported(tmp_path):
    data = ("2026-11-12 09:00:01.000 | INFO | r | 已启动\n" * 3 + "2026-11-12 09:05:00.000 | ERROR | g | [FATAL] 紧急平仓: 触发").encode("utf-8")
    snap = incremental(tmp_path, data, [10, 77])
    assert ("严重", "触发紧急平仓") in dict(snap[3])
