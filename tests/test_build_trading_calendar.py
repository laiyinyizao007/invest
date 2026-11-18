"""交易日历生成脚本(tools/build_trading_calendar.py)的测试:数据源一律注入假函数,不联网。"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import build_trading_calendar as b  # noqa: E402
from risk import sessions as s  # noqa: E402


def weekdays(start, end, skip=()):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d not in skip:
            out.append(d)
        d += timedelta(days=1)
    return out


HOLIDAYS = {date(2025, 10, 1), date(2025, 10, 2), date(2025, 10, 3)}


def test_build_records_only_weekday_closures():
    days = weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    out = b.build(days, date(2025, 9, 29), date(2025, 10, 12), source="fake", fetched_at="2026-11-17")
    assert out["closed_weekdays"] == ["2025-10-01", "2025-10-02", "2025-10-03"]
    assert out["coverage"] == {"start": "2025-09-29", "end": "2025-10-12"}


def test_build_rejects_a_weekend_trading_day():
    days = weekdays(date(2025, 9, 29), date(2025, 10, 12)) + [date(2025, 10, 4)]   # 周六
    with pytest.raises(b.CalendarBuildError, match="周末"):
        b.build(days, date(2025, 9, 29), date(2025, 10, 12), source="fake", fetched_at="x")


def test_build_rejects_an_implausible_full_year():
    days = weekdays(date(2025, 1, 1), date(2025, 12, 31))[:100]     # 一整年只有 100 个交易日:数据源出错
    with pytest.raises(b.CalendarBuildError, match="2025"):
        b.build(days, date(2025, 1, 1), date(2025, 12, 31), source="fake", fetched_at="x")


def test_build_rejects_empty_source():
    with pytest.raises(b.CalendarBuildError):
        b.build([], date(2025, 1, 1), date(2025, 1, 31), source="fake", fetched_at="x")


def test_build_output_loads_with_sessions(tmp_path):
    days = weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(b.build(days, date(2025, 9, 29), date(2025, 10, 12), source="f", fetched_at="x")), encoding="utf-8")
    assert s.load_calendar(p).status(date(2025, 10, 2)) == s.CLOSED


# ---------------------------------------------------------------- 与上一版比较:只许延长,不许改旧日期

def _cal(closed, start="2025-09-29", end="2025-10-12"):
    return {"source": "f", "fetched_at": "x", "coverage": {"start": start, "end": end}, "closed_weekdays": closed}


def test_extending_coverage_is_not_a_conflict():
    old = _cal(["2025-10-01"])
    new = _cal(["2025-10-01", "2025-12-25"], end="2025-12-31")
    assert b.conflicts(old, new) == []


@pytest.mark.parametrize("new,needle", [
    (_cal([]), "2025-10-01"),                                        # 旧的休市日消失了
    (_cal(["2025-10-01", "2025-10-08"]), "2025-10-08"),               # 区间内多出休市日
    (_cal(["2025-10-01"], end="2025-10-10"), "缩短"),                 # 覆盖区间缩短
    (_cal(["2025-10-01"], start="2025-10-01"), "缩短"),
])
def test_changes_to_existing_dates_are_conflicts(new, needle):
    found = b.conflicts(_cal(["2025-10-01"]), new)
    assert found and any(needle in c for c in found)


def test_main_refuses_conflicting_update_without_flag(tmp_path, capsys):
    out = tmp_path / "cal.json"
    out.write_text(json.dumps(_cal(["2025-10-01"])), encoding="utf-8")
    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12))              # 数据源说 10/1 是交易日
    rc = b.main(["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)], fetch=fake, today="2026-11-17")
    assert rc == 1
    assert json.loads(out.read_text(encoding="utf-8"))["closed_weekdays"] == ["2025-10-01"]     # 原文件未被覆盖
    rc = b.main(["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out), "--allow-changes"], fetch=fake, today="2026-11-17")
    assert rc == 0 and json.loads(out.read_text(encoding="utf-8"))["closed_weekdays"] == []


def test_main_writes_new_file_and_check_mode(tmp_path):
    out = tmp_path / "cal.json"
    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    assert b.main(["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)], fetch=fake, today="2026-11-17") == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["fetched_at"] == "2026-11-17" and data["closed_weekdays"] == ["2025-10-01", "2025-10-02", "2025-10-03"]
    assert b.main(["--check", "--out", str(out)], fetch=None, today="2026-11-17") == 0
    out.write_text("{", encoding="utf-8")
    assert b.main(["--check", "--out", str(out)], fetch=None, today="2026-11-17") == 2


def test_cli_check_survives_a_non_utf8_console():
    """真实 CLI 路径:Windows cp1252 控制台上打印中文不得崩溃(分档器踩过同一个坑)。"""
    script = Path(__file__).resolve().parents[1] / "tools" / "build_trading_calendar.py"
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    assert "OK" in r.stdout.decode("utf-8", "replace")


def test_unchanged_data_does_not_rewrite_the_file(tmp_path):
    """内容没变时不写文件(否则抓取日期每天都变,定时任务会每天开一个空 PR)。"""
    out = tmp_path / "cal.json"

    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    args = ["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)]
    assert b.main(args, fetch=fake, today="2026-11-17") == 0
    before = out.read_bytes()
    assert b.main(args, fetch=fake, today="2026-11-18") == 0
    assert out.read_bytes() == before                                  # fetched_at 仍是第一次的


def test_check_can_fail_on_coverage_warning(tmp_path):
    out = tmp_path / "cal.json"

    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    b.main(["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)], fetch=fake, today="2025-09-01")
    assert b.main(["--check", "--out", str(out)], fetch=None, today="2025-09-01") == 0
    assert b.main(["--check", "--fail-on-warning", "--out", str(out)], fetch=None, today="2025-09-01") == 0     # 还早
    assert b.main(["--check", "--fail-on-warning", "--out", str(out)], fetch=None, today="2025-10-01") == 3     # 进入 30 天窗口
    assert b.main(["--check", "--out", str(out)], fetch=None, today="2025-10-01") == 0                         # 不加开关只告警


# ---------------------------------------------------------------- G8 第 2 轮(codex 39/50)复现后补的回归

def test_fetch_failure_is_exit_code_2_not_a_conflict(tmp_path):
    """Medium:数据源网络异常原先未捕获,Python 以退出码 1 结束,被定时任务误当成「与旧日期冲突」。"""
    def boom():
        raise ConnectionError("sina down")
    assert b.main(["--out", str(tmp_path / "c.json")], fetch=boom, today="2026-11-17") == 2


def test_deleting_the_committed_file_does_not_bypass_the_extend_only_rule(tmp_path, monkeypatch):
    """Medium:原先只在文件存在时比较,删掉文件再生成就绕过了「只许延长」。现在缺文件时以 git 里的入库版本为基线。"""
    out = tmp_path / "cal.json"
    monkeypatch.setattr(b, "_git_baseline", lambda path: _cal(["2025-10-01"]))

    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12))          # 数据源说 10/1 是交易日
    args = ["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)]
    assert b.main(args, fetch=fake, today="2026-11-17") == 1 and not out.exists()
    assert b.main(args + ["--allow-changes"], fetch=fake, today="2026-11-17") == 0


def test_manual_closures_are_merged_and_survive_regeneration(tmp_path):
    """Medium:A 股日历表达不了交易所临时休市。允许人工追加休市日(只能加不能减,必须写原因),重新生成时保留。"""
    out = tmp_path / "cal.json"

    def fake():
        return weekdays(date(2025, 9, 29), date(2025, 10, 12), HOLIDAYS)
    args = ["--start", "2025-09-29", "--end", "2025-10-12", "--out", str(out)]
    assert b.main(args, fetch=fake, today="2026-11-17") == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    data["manual_closed"] = [{"date": "2025-10-09", "reason": "交易所临时休市公告(示例)"}]
    out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert s.load_calendar(out).status(date(2025, 10, 9)) == s.CLOSED
    assert b.main(args, fetch=fake, today="2026-11-18") == 0
    assert json.loads(out.read_text(encoding="utf-8"))["manual_closed"][0]["date"] == "2025-10-09"


@pytest.mark.parametrize("entry", [
    {"date": "2025-10-09"},                                  # 没写原因
    {"date": "2025-10-09", "reason": ""},
    {"date": "2025-10-11", "reason": "周六"},                 # 周末
    {"date": "2026-01-05", "reason": "区间外"},
])
def test_bad_manual_closures_are_rejected(entry):
    payload = _cal(["2025-10-01"])
    payload["manual_closed"] = [entry]
    with pytest.raises(s.CalendarError):
        s.calendar_from_dict(payload)


@pytest.mark.parametrize("field", ["source", "fetched_at"])
def test_metadata_must_be_present(field):
    payload = _cal(["2025-10-01"])
    payload[field] = ""
    with pytest.raises(s.CalendarError):
        s.calendar_from_dict(payload)
