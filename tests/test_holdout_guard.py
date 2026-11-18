"""封存前向留出集防护(risk/holdout_guard.py)。

依据 docs/adr/20261113-evolution-engine.md「封存前向留出集」:最后一根可见 K 线是 2026-06-15 14:45;
当晚 21:00 起的夜盘按 20:00 日界线归属 2026-06-16 交易日,属于封存段。
- 读取入口:越界一律拒绝(不悄悄截断);需要截断时显式调用 split,并拿到被截掉的数量。
- 一次性使用:留出段只能在最终确认时由人触发使用一次;台账按区间记账,新区间不得与已用区间重叠
  (否则「每个候选各看一次」就成了多重检验)。
"""
import json
from datetime import date, datetime

import pytest

from risk import holdout_guard as h


@pytest.mark.parametrize("value,sealed", [
    (datetime(2026, 6, 15, 14, 45), False),     # 最后一根可见
    (datetime(2026, 6, 15, 19, 59), False),
    (datetime(2026, 6, 15, 21, 0), True),       # 当晚夜盘归 6/16
    (datetime(2026, 6, 16, 9, 0), True),
    (date(2026, 6, 15), False),                 # 日线按交易日
    (date(2026, 6, 16), True),
    (datetime(2024, 1, 2, 9, 0), False),
])
def test_is_sealed(value, sealed):
    assert h.is_sealed(value) is sealed


def test_constant_is_defined_in_one_place():
    assert h.LAST_VISIBLE_TRADING_DAY == date(2026, 6, 15)
    assert h.FIRST_SEALED_TRADING_DAY == date(2026, 6, 16)


def test_check_range_allows_visible_and_rejects_sealed_end():
    h.check_range(date(2020, 7, 23), date(2026, 6, 15), purpose="选参")
    with pytest.raises(h.HoldoutViolation, match="选参"):
        h.check_range(date(2020, 7, 23), date(2026, 11, 9), purpose="选参")


def test_check_range_rejects_inverted_ranges():
    with pytest.raises(ValueError):
        h.check_range(date(2026, 1, 2), date(2025, 1, 2), purpose="x")


ROWS = [{"datetime": datetime(2026, 6, 15, 14, 45)}, {"datetime": datetime(2026, 6, 15, 21, 0)},
        {"datetime": datetime(2026, 6, 16, 9, 0)}]


def test_assert_visible_rejects_any_sealed_row():
    h.assert_visible(ROWS[:1], key=lambda r: r["datetime"], purpose="回测")
    with pytest.raises(h.HoldoutViolation, match="2 根"):
        h.assert_visible(ROWS, key=lambda r: r["datetime"], purpose="回测")


def test_split_is_explicit_about_what_it_drops():
    visible, dropped = h.split(ROWS, key=lambda r: r["datetime"])
    assert visible == ROWS[:1] and dropped == 2


# ---------------------------------------------------------------- 一次性使用台账

SHA = "a" * 64


def test_first_use_is_recorded_and_second_overlapping_use_is_refused(tmp_path):
    ledger = tmp_path / "holdout_usage.json"
    h.record_use(date(2026, 6, 16), date(2026, 11, 13), sha256=SHA, note="最终确认", path=ledger)
    entries = json.loads(ledger.read_text(encoding="utf-8"))
    assert entries[0]["start"] == "2026-06-16" and entries[0]["sha256"] == SHA
    with pytest.raises(h.HoldoutAlreadyUsed):
        h.record_use(date(2026, 6, 16), date(2026, 11, 13), sha256="b" * 64, path=ledger)     # 另一个候选也不行
    with pytest.raises(h.HoldoutAlreadyUsed):
        h.record_use(date(2026, 9, 1), date(2027, 3, 1), sha256=SHA, path=ledger)              # 部分重叠也不行


def test_a_later_non_overlapping_range_can_be_used(tmp_path):
    ledger = tmp_path / "holdout_usage.json"
    h.record_use(date(2026, 6, 16), date(2026, 11, 13), sha256=SHA, path=ledger)
    h.record_use(date(2026, 11, 16), date(2027, 6, 1), sha256=SHA, path=ledger)
    assert len(json.loads(ledger.read_text(encoding="utf-8"))) == 2


@pytest.mark.parametrize("start,end,sha", [
    (date(2026, 6, 1), date(2026, 11, 13), SHA),         # 起点落在可见段:这不是留出段
    (date(2026, 6, 16), date(2026, 11, 13), "abc"),      # 不是完整 sha256
    (date(2026, 11, 13), date(2026, 6, 16), SHA),        # 倒置
])
def test_record_use_rejects_bad_requests(tmp_path, start, end, sha):
    with pytest.raises(ValueError):
        h.record_use(start, end, sha256=sha, path=tmp_path / "l.json")


@pytest.mark.parametrize("body", [
    "{", '{"a": 1}', '[{"start": "bad", "end": "2026-07-01"}]',
    # G8 第 2 轮:语义无效的记录原先也被接受
    '[{"start": "2026-12-01", "end": "2026-07-01", "sha256": "%s"}]' % ("a" * 64),     # 倒置
    '[{"start": "2026-06-01", "end": "2026-07-01", "sha256": "%s"}]' % ("a" * 64),     # 起点不在封存段
    '[{"start": "2026-06-16", "end": "2026-07-01", "sha256": "abc"}]',                # sha 不完整
    # G8 复核:台账内部的区间互相重叠(人工改坏)原先也被接受
    '[{"start": "2026-06-16", "end": "2026-09-01", "sha256": "%s"}, {"start": "2026-08-01", "end": "2026-10-01", "sha256": "%s"}]'
    % ("a" * 64, "a" * 64),
])
def test_corrupt_ledger_fails_closed(tmp_path, body):
    ledger = tmp_path / "l.json"
    ledger.write_text(body, encoding="utf-8")
    with pytest.raises(h.LedgerError):
        h.check_unused(date(2026, 6, 16), date(2026, 11, 13), path=ledger)


def test_check_unused_on_missing_ledger_passes(tmp_path):
    h.check_unused(date(2026, 6, 16), date(2026, 11, 13), path=tmp_path / "none.json")


def test_research_stack_safe_imports_only_stdlib():
    import ast
    import sys
    from pathlib import Path
    tree = ast.parse(Path(h.__file__).read_text(encoding="utf-8"))
    mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert mods <= set(sys.stdlib_module_names) | {"__future__"}, mods
