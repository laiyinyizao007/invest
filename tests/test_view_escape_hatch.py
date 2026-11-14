"""第 2 轮 G8 复核(29/50)的 Critical 1 / High 1 / High 2 / Medium 1–3:真容器发放口与换入换出的健壮性。

Critical 1:候选 `from strategies.trend_atr_strategy import swap_out` 静态层零发现,运行期拿到真 ArrayManager,
把 ATR 40.0 压到 2.9、单笔预算闸门被架空——今天拦住它的只有第五层。现在这些名字在静态层就拦。
"""
import pytest

import bootstrap  # noqa: F401
import research_sandbox as rs
from risk import constitution as const
from risk.market_view import ReadOnlyViewError, read_only_view, swap_in, swap_out
from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
from test_market_view import filled_am


def flagged(source: str) -> bool:
    return bool(rs.screen_source(source)) or bool(const.screen_source(source))


HATCH = ["swap_in", "swap_out", "read_only_view", "_REAL", "_SOURCE"]


@pytest.mark.parametrize("name", HATCH)
@pytest.mark.parametrize("form", [
    "from strategies.trend_atr_strategy import {n}\n",
    "import strategies.trend_atr_strategy as t\nx = t.{n}\n",
    "def entry_signal(self):\n    {n}(self)\n    return 0\n",
    "def entry_signal(self):\n    f = something.{n}\n    return 0\n",
])
def test_every_way_to_reach_the_real_container_dispenser_is_refused_statically(name, form):
    assert flagged(form.format(n=name)), f"{name} 没被拦: {form!r}"


def test_an_honest_candidate_is_still_accepted():
    source = ("def entry_signal(self):\n    if self.am.sma(5) > self.am.sma(20):\n        return 1\n    return 0\n")
    assert not flagged(source)


def test_the_wildcard_import_cannot_bring_the_dispenser_in():
    """通配符导入(`from X import *`)把被导入模块的**全部公开名字**一次性绑进候选命名空间,
    逐名裁决看不见它拿到了什么——第 4 轮 G8 复核的 Critical。整条规则的用例见
    `tests/test_g8_round4.py`(那里有放行方向的对照用例)。"""
    findings = const.screen_source("from strategies.trend_atr_strategy import *\n")
    assert findings, "通配符导入没有被拦下"
    assert any("通配符" in f for f in findings), findings
    assert any("swap_out" in f for f in findings), findings


@pytest.mark.parametrize("source", [
    "x = (lambda: self.am.close)()\n",
    "x = (lambda: self.bg)()\n",
    "y = 1 if True else self.am.high\n",
    "y = None or self.am.close\n",
])
def test_reading_the_container_in_a_lambda_or_expression_is_allowed(source):
    """第 4 轮 G8 复核的 F2:只读视图落地后,读取返回的是**拷贝**,别名无害。

    早先这里是一条"引用流出去就拒"的规则,把仓库自己的 LLM 提示词写法
    (`self.am.close[-2:]`、`np.mean(self.am.close)`、`self.am.count`)整片拒掉——
    进化循环因此被闲置。现在读取一律放行,**写入**仍然拒(见下面那条)。"""
    assert not flagged("def entry_signal(self):\n    " + source + "    return 0\n")


@pytest.mark.parametrize("source", [
    "x = (lambda: self.am.close.fill(0.0))()\n",
    "x = 1 if True else self.am.close.__setitem__(0, 1.0)\n",
    "x = None or self.am.update_bar(fake_bar)\n",
])
def test_writing_through_a_lambda_or_an_expression_is_still_refused(source):
    """收窄的是"读取",不是"写入":变更型方法(写入口)照旧拒。"""
    assert flagged("def entry_signal(self):\n    " + source + "    return 0\n")


def test_reading_a_number_inside_a_lambda_is_still_allowed():
    assert not flagged("def entry_signal(self):\n    f = lambda: self.am.close[-1]\n    return 0\n")


class Host:
    """最小的"策略":有 am/bg/_am_view。"""

    def __init__(self):
        self.am = filled_am()
        self.bg = object()
        self._am_view = read_only_view(self.am)


def test_swap_out_without_a_swap_in_is_a_noop_not_a_keyerror():
    host = Host()
    swap_out(host)                                      # 不抛
    assert host.am is not host._am_view


def test_swap_in_twice_is_refused_instead_of_writing_the_view_into_the_registry():
    host = Host()
    real = host.am
    swap_in(host)
    with pytest.raises(ReadOnlyViewError):
        swap_in(host)
    swap_out(host)
    assert host.am is real                              # 真容器没有被覆盖成视图


def test_a_stale_view_is_refused_at_swap_in():
    host = Host()
    host.am = filled_am()                               # 换了容器,视图还包着旧的
    with pytest.raises(ReadOnlyViewError):
        swap_in(host)


def test_swap_out_in_a_finally_does_not_mask_the_original_exception():
    host = Host()
    swap_in(host)
    swap_out(host)                                      # 候选先自己换回去了
    with pytest.raises(ValueError, match="原始异常"):
        try:
            raise ValueError("原始异常")
        finally:
            swap_out(host)                              # 不得抛 KeyError 顶掉它


def test_a_callable_that_is_not_a_known_indicator_is_not_forwarded():
    """vnpy 升级新增一个写入口:不在白名单里就不转发,候选访问不到。"""
    class FakeAm:
        count, size, inited = 1, 1, True
        open = high = low = close = volume = turnover = open_interest = []
        open_array = high_array = low_array = close_array = volume_array = turnover_array = open_interest_array = []

        def sma(self, n):
            return 1.0

        def reset_everything(self):                     # 假想的新写入口
            raise AssertionError("不应被调用")

    view = read_only_view(FakeAm())
    assert view.sma(3) == 1.0
    with pytest.raises(AttributeError):
        view.reset_everything
