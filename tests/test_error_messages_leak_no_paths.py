"""写入失败的错误消息会经网关的 500 响应原样交给外层:不得带本机绝对路径(G7)。

自查发现:`OSError` 的原文里有绝对路径;另有 `record_sharpes` 的 `mkdir` 在 try 之外,目录被占用时抛出的是
裸的 `FileExistsError`,而不是账本自己的错误类型,上层接不住。
"""
import pytest

from risk.heatmap import HeatmapError, save_heatmap
from risk.ladder import Ladder, LadderError
from risk.lineage import LineageError, append_record
from risk.outward import OutwardError, RevealStore
from risk.trial_ledger import TrialLedgerError, record_sharpes, record_trials
from test_heatmap import good


def sha(n):
    return f"{n:064x}"


@pytest.fixture
def blocked(tmp_path):
    """一个**文件**挡在本该是目录的位置上:任何往它下面创建东西的操作都会抛 OSError。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    return tmp_path, blocker


def assert_no_path(error, tmp_path):
    message = str(error)
    assert str(tmp_path) not in message and "blocker" not in message and "\\\\" not in message and "Users" not in message, message


def test_a_heatmap_write_failure_names_no_local_path(blocked):
    tmp_path, blocker = blocked
    with pytest.raises(HeatmapError) as exc:
        save_heatmap(blocker / "sub", "r1", good())
    assert_no_path(exc.value, tmp_path)


def test_a_lineage_write_failure_names_no_local_path(blocked):
    tmp_path, blocker = blocked
    with pytest.raises(LineageError) as exc:
        append_record(blocker / "sub", "c", {"generation": 0, "candidate_sha256": sha(1), "status": "gate_failed"})
    assert_no_path(exc.value, tmp_path)


def test_a_sharpe_ledger_write_failure_is_the_ledgers_own_error_and_names_no_path(blocked):
    tmp_path, blocker = blocked
    with pytest.raises(TrialLedgerError) as exc:
        record_sharpes(blocker / "t.jsonl", "grid", [0.1], at=1.0)
    assert_no_path(exc.value, tmp_path)


def test_the_trial_count_write_failure_is_the_ledgers_own_error_and_names_no_path(blocked):
    """同一个缺陷在已有的 `record_trials` 里也有(mkdir 在 try 之外)。"""
    tmp_path, blocker = blocked
    with pytest.raises(TrialLedgerError) as exc:
        record_trials(blocker / "t.jsonl", "grid_cell", 27, at=1.0)
    assert_no_path(exc.value, tmp_path)


def test_a_ladder_write_failure_names_no_local_path(blocked):
    tmp_path, blocker = blocked
    with pytest.raises(LadderError) as exc:
        Ladder(blocker / "sub" / "ladder.json").submit(1.0)
    assert_no_path(exc.value, tmp_path)


def test_a_reveal_archive_write_failure_names_no_local_path(blocked):
    from risk.outward import build_outward
    tmp_path, blocker = blocked
    view = build_outward({"run_id": "r1", "kind": "evaluate", "passed": False})
    with pytest.raises(OutwardError) as exc:
        RevealStore(blocker / "sub").put("r1", view)
    assert_no_path(exc.value, tmp_path)


def test_an_unreadable_ledger_names_no_local_path(tmp_path):
    """账本路径是个目录:读取抛 OSError(IsADirectoryError / PermissionError),消息里不得有路径。"""
    directory = tmp_path / "t.jsonl"
    directory.mkdir()
    from risk.trial_ledger import total_trials
    with pytest.raises(TrialLedgerError) as exc:
        total_trials(directory)
    assert_no_path(exc.value, tmp_path)
