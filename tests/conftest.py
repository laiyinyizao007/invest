"""全局测试夹具。"""
import pytest

import segment_ledger


@pytest.fixture(autouse=True)
def isolated_segment_ledger(tmp_path, monkeypatch):
    """测试跑 `--test` 路径时不得污染入库的真实登记文件。"""
    monkeypatch.setattr(segment_ledger, "LEDGER", tmp_path / "segment_usage.json")


from ctp_tools import collector as _collector_module

_REAL_PROBE = _collector_module._probe_counter_replies


@pytest.fixture(autouse=True)
def stub_counter_probe(monkeypatch):
    """持仓查询「没有行」时的账户探针会 sleep 1.1s(躲柜台查询流控)。

    绝大多数假引擎用例只关心别的逻辑,统一桩成「柜台在应答」;
    要测探针本身的用例请求 `real_probe` 夹具把真函数换回来。
    """
    monkeypatch.setattr(_collector_module, "_probe_counter_replies", lambda *a, **k: True)


@pytest.fixture
def real_probe(monkeypatch):
    monkeypatch.setattr(_collector_module, "_probe_counter_replies", _REAL_PROBE)
    return _REAL_PROBE
