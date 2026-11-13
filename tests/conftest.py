"""全局测试夹具。"""
import pytest

import segment_ledger


@pytest.fixture(autouse=True)
def isolated_segment_ledger(tmp_path, monkeypatch):
    """测试跑 `--test` 路径时不得污染入库的真实登记文件。"""
    monkeypatch.setattr(segment_ledger, "LEDGER", tmp_path / "segment_usage.json")

