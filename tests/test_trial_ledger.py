"""risk/trial_ledger.py:试验次数账本(只增不减,损坏即 fail-closed)。"""
import json

import pytest

from risk.trial_ledger import TrialLedgerError, record_trials, total_trials


def test_missing_ledger_counts_zero(tmp_path):
    assert total_trials(tmp_path / "trials.jsonl") == 0


def test_record_accumulates_across_kinds(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_trials(path, "submission", 1, at=1.0)
    record_trials(path, "grid_cell", 27, at=2.0)
    record_trials(path, "grid_cell", 27, at=3.0)
    assert total_trials(path) == 55


def test_record_creates_parent_dir(tmp_path):
    path = tmp_path / "nested" / "trials.jsonl"
    record_trials(path, "submission", 1, at=1.0)
    assert total_trials(path) == 1


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "3", None])
def test_record_rejects_non_positive_or_non_int_counts(tmp_path, count):
    with pytest.raises(TrialLedgerError):
        record_trials(tmp_path / "trials.jsonl", "grid_cell", count, at=1.0)
    assert total_trials(tmp_path / "trials.jsonl") == 0


def test_record_rejects_unknown_kind(tmp_path):
    with pytest.raises(TrialLedgerError):
        record_trials(tmp_path / "trials.jsonl", "mystery", 1, at=1.0)


@pytest.mark.parametrize("line", [
    "not json",
    json.dumps([1, 2]),
    json.dumps({"kind": "submission", "count": 0, "at": 1}),
    json.dumps({"kind": "submission", "count": -3, "at": 1}),
    json.dumps({"kind": "submission", "count": True, "at": 1}),
    json.dumps({"kind": "other", "count": 1, "at": 1}),
    json.dumps({"kind": "submission", "at": 1}),
])
def test_corrupt_line_fails_closed(tmp_path, line):
    path = tmp_path / "trials.jsonl"
    record_trials(path, "submission", 1, at=1.0)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(line + "\n")
    with pytest.raises(TrialLedgerError):
        total_trials(path)
    with pytest.raises(TrialLedgerError):       # 损坏时也不允许继续追加,避免在坏账本上累计
        record_trials(path, "submission", 1, at=2.0)


def test_blank_lines_are_ignored(tmp_path):
    path = tmp_path / "trials.jsonl"
    record_trials(path, "submission", 2, at=1.0)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write("\n\n")
    assert total_trials(path) == 2


def test_unreadable_ledger_fails_closed(tmp_path):
    path = tmp_path / "trials.jsonl"
    path.mkdir()                                  # 是目录而不是文件:读取失败
    with pytest.raises(TrialLedgerError):
        total_trials(path)
