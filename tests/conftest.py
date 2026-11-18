"""全局测试夹具。"""
import pytest

import segment_ledger


@pytest.fixture(autouse=True)
def isolated_segment_ledger(tmp_path, monkeypatch):
    """测试跑 `--test` 路径时不得污染入库的真实登记文件。"""
    monkeypatch.setattr(segment_ledger, "LEDGER", tmp_path / "segment_usage.json")



@pytest.fixture()
def numpy_on_allowlist(monkeypatch):
    """模拟"运维显式把 numpy/pandas 追加进候选白名单"(Issue #31:它们已不在默认白名单里)。

    这批用例拿 numpy 当**攻击载体**(`np.savetxt` 写辅助模块等),要测的是更下层的规则
    (按路径裁决/受限内建/模块视图/审计钩子),所以先让 numpy 能 import;
    默认白名单本身不含 numpy/pandas 由 tests/test_fs_capability_closure.py 断言。"""
    for rs in _sandbox_modules():
        monkeypatch.setattr(rs, "ALLOWED_MODULE_ROOTS", rs.ALLOWED_MODULE_ROOTS | rs.CAPABILITY_LIB_ROOTS)


@pytest.fixture()
def without_audit_window(monkeypatch):
    """关掉审计钩子窗口,只测更下层的规则(Issue #31)。

    这批用例用 `np.savetxt` 写辅助模块来测"按路径裁决/受限内建"等下层规则;
    窗口开着时写文件在更早的一步就被审计钩子拒了,用例测到的就不是它们要测的那条规则
    (第 22 轮的教训:规则失灵/被抢先时用例照样绿)。钩子本身由 tests/test_fs_capability_closure.py 测。"""
    import contextlib
    for rs in _sandbox_modules():
        monkeypatch.setattr(rs, "candidate_window", lambda *a, **k: contextlib.nullcontext())


def _sandbox_modules():
    """`research_sandbox` 与 `scripts.research_sandbox` 是两个模块对象(不同测试按不同名字导入),都要打补丁。"""
    import sys
    import research_sandbox  # noqa: F401
    return [m for name, m in list(sys.modules.items())
            if name in ("research_sandbox", "scripts.research_sandbox") and m is not None]
