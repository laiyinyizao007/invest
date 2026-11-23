"""批准账本的共享只读模块 `risk/approval_ledger.py`。

两条不变式:
  1. **只读**:这个模块里没有写入口。写账本的入口唯一在 `research_sandbox.record_approval`。
     一旦这里也能写,「批准只能经 record_approval 落账」这条边界就漏了。
  2. **fail-closed**:账本读不了就抛错,绝不回退成「没有批准」或「默认批准」。
     只有「账本文件不存在」才是「空」——那是"还没批准过任何东西"。
"""
import ast
import json
from pathlib import Path

import pytest

from risk import approval_ledger

REPO_ROOT = Path(__file__).resolve().parents[1]

# 会写文件的调用形态:出现任何一个都说明这个模块想写账本
WRITE_CALLS = {"write_text", "write_bytes", "unlink", "replace", "mkdir", "rename", "touch"}


def _module_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "risk" / "approval_ledger.py").read_text(encoding="utf-8"))


def test_the_shared_module_has_no_write_entry():
    """只读:模块里不得出现任何写文件的调用。"""
    offenders: list[str] = []
    for node in ast.walk(_module_tree()):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name in WRITE_CALLS:
                offenders.append(f"{name} @ line {node.lineno}")
        if isinstance(node, ast.Name) and node.id in {"os", "shutil"}:
            offenders.append(f"import {node.id} @ line {node.lineno}")
    assert offenders == [], f"共享账本模块必须只读,却发现:{offenders}"


def test_missing_file_is_empty_not_an_error(tmp_path):
    """账本不存在 = 还没批准过任何东西(空),不是错。"""
    assert approval_ledger.load_approvals(tmp_path / "nope.json") == {}
    assert approval_ledger.is_approved_sha("0" * 64, tmp_path / "nope.json") is False


def test_broken_ledger_is_rejected_fail_closed(tmp_path):
    """账本坏了必须报错,不能悄悄当成空账本(那等于「谁都没批准过」)。"""
    path = tmp_path / "approvals.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(approval_ledger.ApprovalLedgerError):
        approval_ledger.load_approvals(path)

    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(approval_ledger.ApprovalLedgerError):
        approval_ledger.load_approvals(path)


def test_sha_lookup_matches_the_recorded_one(tmp_path):
    target = tmp_path / "cand.py"
    target.write_text("x = 1\n", encoding="utf-8")
    sha = approval_ledger.file_sha256(target)
    ledger = tmp_path / "approvals.json"
    ledger.write_text(json.dumps({sha: {"name": "cand.py", "at": "2026-11-14T00:00:00"}}),
                      encoding="utf-8")

    assert approval_ledger.is_approved_sha(sha, ledger) is True
    assert approval_ledger.is_approved_path(target, ledger) is True
    # 改了内容就不再是同一份字节
    target.write_text("x = 2\n", encoding="utf-8")
    assert approval_ledger.is_approved_path(target, ledger) is False


def test_reading_is_consistent_with_the_research_side():
    """研究侧与运行侧必须读到**同一份**账本(口径漂移 = 边界漏一条)。"""
    import research_sandbox as rs

    tmp = REPO_ROOT / "data" / "research-control"
    ledger = tmp / "approvals.json"
    if not ledger.exists():
        pytest.skip("本机没有批准账本,跳过一致性对照")
    assert rs._load_approvals(ledger) == approval_ledger.load_approvals(ledger)


def test_research_side_still_raises_sandbox_error_on_broken_ledger(tmp_path):
    """委托之后,研究侧的异常类型与文案不得变(既有调用点靠它识别拒绝)。"""
    import research_sandbox as rs

    path = tmp_path / "approvals.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(rs.SandboxError) as excinfo:
        rs._load_approvals(path)
    assert "fail-closed" in str(excinfo.value)
