"""全局测试夹具。"""
import pytest

import segment_ledger


@pytest.fixture(autouse=True)
def isolated_segment_ledger(tmp_path, monkeypatch):
    """测试跑 `--test` 路径时不得污染入库的真实登记文件。"""
    monkeypatch.setattr(segment_ledger, "LEDGER", tmp_path / "segment_usage.json")


@pytest.fixture(autouse=True)
def restore_strategy_template_classes():
    """每个测试结束后把模板类的类字典还原成测试开始时的样子。

    研究沙盒的测试会让"候选"在导入期真的改写仓库里的模板类
    (`t.TrendAtr15mStrategy.get_pricetick = lambda self: 10**9`),用来证明运行期第四层能抓到。
    改写发生在**共享的类对象**上,用例结束后没有人还原——同一进程里之后的测试看到的是被篡改的模板:
    `test_real_spawn_*` 里串行结果(进程内、被污染)与新进程结果(干净)的净盈亏对不上
    (-572 对 -632),这条失败在基线上就存在;第 13 轮把仓库类豁免收紧到"类体里的函数必须来自
    仓库自己的文件"之后,被污染的类还会被运行期类字典检查正确地拒掉,`test_backtest_one_*` 也跟着红。
    根因是测试隔离,不是被测代码——所以在这里统一还原,而不是放宽豁免。"""
    import sys

    names = ("strategies.trend_atr_strategy", "strategies.trend_atr_15m_strategy")
    classes = []
    for module_name in names:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == module_name:
                classes.append((value, dict(vars(value))))
    yield
    for klass, before in classes:
        for key in set(vars(klass)) - set(before):
            try:
                delattr(klass, key)
            except (AttributeError, TypeError):
                pass
        for key, value in before.items():
            if key in ("__dict__", "__weakref__") or vars(klass).get(key) is value:
                continue
            try:
                setattr(klass, key, value)
            except (AttributeError, TypeError):
                pass
