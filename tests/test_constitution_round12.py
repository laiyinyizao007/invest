"""第 12 轮 codex 复核(38.5/50,未达 >=45)的逐条回归用例。

第 12 轮 codex 逐条复现了第 9/10/11 轮的修复,并找出 2 条 High / 2 条 Medium / 2 条 Low。
本文件把这些问题**一条一条**钉成用例,防止下一轮改动把它们带回来:
- High 1:容器规则仍有三条绕过(视图 / dunder 写入口 / bg 别名),¥300 单笔风险预算被架空;
- High 2:历史守卫的执行点检查仍有五条绕过(死函数 / 语义同参 / 注释冒充 / eval 入口 / import 闭包);
- Medium 1:第 11 轮的 Low 1 只做了一半——裸局部名 `am.append(...)` 被误拒;
- Medium 2:`__module__` 伪造可以骗过"仓库自己的类"豁免;
- Low 2:文档里有三处与实测不符的断言。
"""
import textwrap
from pathlib import Path

import pytest

from risk import constitution as const

ROOT = Path(const.CONSTITUTION_FILE.parent.parent)


def screen(src, c=None):
    return const.screen_source(src, c or const.default())


# ---------- High 1:容器规则的三条绕过 ----------

@pytest.mark.parametrize("src", [
    # 变体①:numpy 视图(`self.am.close[:]` 切出来的是视图,不是副本)
    "def entry_signal(self):\n"
    "    close = self.am.close[:]\n"
    "    high = self.am.high[:]\n"
    "    high[:] = close\n"
    "    return 1\n",
    # 变体②:dunder 写入口
    "def entry_signal(self):\n"
    "    self.am.close.__setitem__(slice(None), self.am.close)\n"
    "    return 1\n",
    # 变体③:bg 别名(`bg` 与 `am` 是同一类容器)
    "def entry_signal(self):\n"
    "    x = self.bg\n"
    "    x.update_bar(fake_bar)\n"
    "    return 1\n",
    # 视图型方法:返回的还是同一块内存
    "def entry_signal(self):\n"
    "    arr = self.am.close.reshape(-1)\n"
    "    arr[0] = 999.0\n"
    "    return 1\n",
])
def test_the_static_layer_refuses_every_container_bypass(src):
    """High 1:第 11 轮只堵住了"按名调用变更型方法",没堵住"拿到视图/dunder/别名再原地改"。

    第 12 轮 codex 端到端实测(120 根合成 15 分钟 K 线):
    宪法层与沙盒层都返回 [],ATR 被压 40 -> 2.86,`skipped_by_budget` 由 1 变 0,
    诚实模板 0 成交而候选成交 2 笔——**¥300 单笔风险预算被架空**,四层防线全绿。"""
    findings = screen(src)
    assert findings, "容器绕过没被拦下(High 1 复发)"


@pytest.mark.parametrize("src", [
    # 取别名
    "def entry_signal(self):\n    arr = self.am.close\n    return 1\n",
    # 带注解的赋值
    "def entry_signal(self):\n    arr: list = self.am.close\n    return 1\n",
    # 海象表达式
    "def entry_signal(self):\n    (arr := self.am.close)\n    return 1\n",
    # 元组解包
    "def entry_signal(self):\n    arr, _ = self.am.close, 0\n    return 1\n",
    # 传给别的函数
    "def entry_signal(self):\n    helper(self.am)\n    return 1\n",
    # 装进容器
    "def entry_signal(self):\n    stuff = [self.am.close]\n    return 1\n",
    # 返回出去
    "def entry_signal(self):\n    return self.am.close\n",
])
def test_the_static_layer_refuses_every_way_of_taking_the_container_out(src):
    """引用一旦**流出去**(存进名字/传参/装容器/返回),按名字的检查就看不见改写点了。

    所以静态层封的是"交出引用"这个动作:`g = self.bg` 之后 `g.update_bar(fake)` 已经看不出 g 是谁。"""
    findings = screen(src)
    assert findings, "受保护容器被取用却没被拦下"


@pytest.mark.parametrize("src", [
    # 裸局部名:候选自己的列表/字典
    "def entry_signal(self):\n    am = []\n    am.append(1)\n    return 1\n",
    "def entry_signal(self):\n    am = {}\n    am.update({'a': 1})\n    return 1\n",
    "def entry_signal(self):\n    bg = [3, 1, 2]\n    bg.sort()\n    return 1\n",
    # 只读调用
    "def entry_signal(self):\n    fast = self.am.sma(self.fast_window, array=True)\n    return 1\n",
    # 就地取值
    "def entry_signal(self):\n    close = self.am.close[-1]\n    return 1\n",
    # 就地比较
    "def entry_signal(self):\n    if self.am.close[-1] > 0:\n        return 1\n    return 0\n",
])
def test_the_static_layer_does_not_punish_ordinary_candidate_code(src):
    """Medium 1:第 11 轮的 Low 1 承诺"裸局部名 am/bg 放行",但只放宽了赋值路径,
    变更型方法调用路径照旧按裸名拒——`am = []` 再 `am.append(x)` 会被整片拒掉。"""
    findings = screen(src)
    assert not findings, findings


# ---------- 运行时第五层:行情数据完整性 ----------

def _synth_bars(n=120):
    """合成 15 分钟 K 线(不碰任何真实/封存数据)。"""
    from datetime import datetime, timedelta

    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData

    start = datetime(2020, 1, 2, 9, 0)
    bars = []
    for i in range(n):
        price = 2000.0 - 0.05 * i + (100.0 if i >= 60 else 0.0)
        bars.append(BarData(
            symbol="CADJ15", exchange=Exchange.DCE, datetime=start + timedelta(minutes=15 * i),
            interval=Interval.MINUTE,
            open_price=price, high_price=price + 20.0, low_price=price - 20.0,
            close_price=price + (1.0 if i >= 60 else -1.0),
            volume=100, turnover=0.0, open_interest=0.0, gateway_name="TEST"))
    return bars


def test_the_fifth_layer_sees_nothing_wrong_with_an_honest_run():
    """第五层必须在诚实回测上零发现,否则它会把整个研究链路拒掉。"""
    import backtest_lib
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    bars = _synth_bars()
    engine = backtest_lib.run_engine_bars(
        TrendAtr15mStrategy,
        {"risk_budget": 300.0, "max_daily_losses": 3, "atr_window": 14,
         "history_vt_symbol": "C15"},
        "C15", bars)
    findings = const.check_market_data_integrity(engine.strategy, bars)
    assert not findings, findings


def test_the_fifth_layer_notices_a_rewritten_price():
    """回测途中把行情数组改掉(哪怕只改一根),第五层必须报出来。

    第 12 轮 codex 的 High 1 就是这样把 ATR 从 40 压到 2.86 的。"""
    import backtest_lib
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    bars = _synth_bars()
    engine = backtest_lib.run_engine_bars(
        TrendAtr15mStrategy,
        {"risk_budget": 300.0, "max_daily_losses": 3, "atr_window": 14,
         "history_vt_symbol": "C15"},
        "C15", bars)
    am = engine.strategy.am
    am.high_array[-1] = 999999.0
    findings = const.check_market_data_integrity(engine.strategy, bars)
    assert findings, "改写行情数组没被第五层抓到"


def test_the_fifth_layer_notices_an_invented_bar():
    """凭空多出来的 K 线(候选往容器里插一根假 K 线)必须报出来。"""
    import backtest_lib
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    bars = _synth_bars()
    engine = backtest_lib.run_engine_bars(
        TrendAtr15mStrategy,
        {"risk_budget": 300.0, "max_daily_losses": 3, "atr_window": 14,
         "history_vt_symbol": "C15"},
        "C15", bars)
    am = engine.strategy.am
    am.count += 1
    findings = const.check_market_data_integrity(engine.strategy, bars)
    assert findings, "凭空多出的 K 线没被第五层抓到"


# ---------- 第五层的"预热基准"(第 12 轮补记的实测教训) ----------

def _daily_bars(n=210):
    """合成日线(不碰任何真实/封存数据)。"""
    from datetime import datetime, time, timedelta

    from vnpy.trader.constant import Exchange, Interval
    from vnpy.trader.object import BarData

    start = datetime(2020, 1, 1)
    bars = []
    for i in range(n):
        close = 100.0 + i * 0.1
        bars.append(BarData(
            symbol="C0", exchange=Exchange.DCE, interval=Interval.DAILY,
            datetime=datetime.combine((start + timedelta(days=i)).date(), time()),
            open_price=close - 0.2, high_price=close + 0.5, low_price=close - 0.5,
            close_price=close, volume=1000, open_interest=1000, gateway_name="TEST"))
    return bars


def test_the_fifth_layer_does_not_mistake_the_warm_up_for_invented_bars(monkeypatch):
    """日线族的 `on_init` 会先加载一段历史日线做预热,那些根数也记在 `am.count` 里。

    第五层若把"累计根数"直接当成"回放根数",就会把预热误判成"凭空多出来的 K 线"
    ——这会让**整个日线研究链路**在回测结束时 fail-closed。基准必须拍在预热之后。"""
    import backtest_lib
    import vnpy_ctastrategy.backtesting as vt_backtesting
    from strategies.trend_atr_strategy import TrendAtrStrategy

    bars = _daily_bars()

    def fake_load(symbol, exchange, interval, start, end):
        return [b for b in bars if start <= b.datetime <= end]

    monkeypatch.setattr(vt_backtesting, "load_bar_data", fake_load)

    engine = backtest_lib.run_engine(
        TrendAtrStrategy, {"fast_window": 5, "slow_window": 20, "atr_mult": 1.5},
        "C", bars[70].datetime, bars[-1].datetime)
    # 预热确实发生过:累计根数多于回放喂进来的根数
    assert engine.strategy.am.count > len(engine.history_data)


def test_the_fifth_layer_still_notices_an_invented_bar_after_the_warm_up(monkeypatch):
    """预热基准不能把"凭空多出来的 K 线"也一起放过(端到端走回测脚本)。"""
    import backtest_lib
    import vnpy_ctastrategy.backtesting as vt_backtesting
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Injector(TrendAtrStrategy):
        """每根 K 线都往容器里多插一根——回测结果就此失真。"""

        def on_bar(self, bar):                          # noqa: ANN001 - 与模板同签名
            super().on_bar(bar)
            self.am.update_bar(bar)

    bars = _daily_bars()

    def fake_load(symbol, exchange, interval, start, end):
        return [b for b in bars if start <= b.datetime <= end]

    monkeypatch.setattr(vt_backtesting, "load_bar_data", fake_load)

    with pytest.raises(RuntimeError, match="凭空"):
        backtest_lib.run_engine(
            Injector, {"fast_window": 5, "slow_window": 20, "atr_mult": 1.5},
            "C", bars[70].datetime, bars[-1].datetime)


# ---------- 仓库自己的类:豁免按**源码文件**判定 ----------

def test_a_strategy_defined_inside_a_test_function_keeps_the_repository_exemption():
    """测试替身(`tests/test_x.py` 里就地定义的类)照抄了内部方法,不该被自己的宪法拒掉。

    豁免的判据是"这个类**真的**定义在它声称的模块文件里"——
    就地定义的类,类体里自己写的函数与模块文件同源 → 豁免。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Chatty(TrendAtrStrategy):
        def on_bar(self, bar):                            # noqa: ANN001 - 与模板同签名
            super().on_bar(bar)

    assert const._is_in_repo_strategy_class(Chatty)


def test_a_class_that_lies_about_its_module_does_not_get_the_exemption():
    """候选伪造 `__module__` 时,类体里自己的代码落在**别的文件**里 → 不豁免。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Cand(TrendAtrStrategy):
        def entry_signal(self) -> int:
            return 0

    Cand.__module__ = "strategies.trend_atr_15m_strategy"
    assert not const._is_in_repo_strategy_class(Cand)


def test_the_fifth_layer_notices_bars_forged_during_the_warm_up():
    """第 13 轮自查:基准若拍在预热之后,候选在 `on_init` 里塞的假 K 线就成了"基线"。

    实测(基准拍在预热后的第一版):预热期塞 40 根假 K 线,`am.count=160`、
    引擎只喂了 120 根,第五层照样放行。基准必须拍在 `on_init` 之前,
    预热的合法根数只认引擎 `load_bar` 实际交出的那些。"""
    import backtest_lib
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy
    from vnpy.trader.object import BarData

    bars = _synth_bars()

    class PreheatForger(TrendAtr15mStrategy):
        def on_init(self):
            b0 = bars[0]
            for _ in range(40):
                self.am.update_bar(BarData(
                    symbol=b0.symbol, exchange=b0.exchange, datetime=b0.datetime,
                    interval=b0.interval, open_price=2000.0, high_price=2000.1,
                    low_price=1999.9, close_price=2000.0, volume=1, turnover=0.0,
                    open_interest=0.0, gateway_name="TEST"))

    with pytest.raises(RuntimeError, match="凭空"):
        backtest_lib.run_engine_bars(
            PreheatForger,
            {"risk_budget": 300.0, "max_daily_losses": 3, "atr_window": 14,
             "history_vt_symbol": "C15"},
            "C15", bars)


def _transient_tamper_class():
    from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy

    class Transient(TrendAtr15mStrategy):
        """第 13 轮 codex 的 Critical 1:改 -> 用 -> 改回。

        第 30 根 K 线结束时把最后一根的最高价改成 5000,第 31 根 K 线(它的 ATR 计算会用到
        被改过的值)结束时再还原——回测结束时容器与诚实回测逐值相同。"""
        _n = 0
        _saved = 0.0

        def on_bar(self, bar):
            super().on_bar(bar)
            self._n += 1
            if self._n == 30:
                self._saved = float(self.am.high_array[-1])
                self.am.high_array[-1] = 5000.0
            elif self._n == 31:
                self.am.high_array[-2] = self._saved        # 数组已左移一格

    return Transient


_TRANSIENT_SETTING = {"risk_budget": 300.0, "max_daily_losses": 3, "atr_window": 14,
                      "history_vt_symbol": "C15"}


def test_the_fifth_layer_notices_a_transient_tamper_that_is_restored_later():
    """逐根核对:瞬时改写必须撑过一根 K 线的处理,下一次核对必然露馅。"""
    import backtest_lib

    with pytest.raises(RuntimeError, match="第 30 根 K 线之后"):
        backtest_lib.run_engine_bars(_transient_tamper_class(), _TRANSIENT_SETTING, "C15", _synth_bars())


def test_the_transient_tamper_test_really_needs_the_per_bar_check(monkeypatch):
    """证明上一条用例有判别力:关掉逐根核对,只剩结束时的末态比对,这次改写就放过去了。"""
    import backtest_lib

    monkeypatch.setattr(const.MarketDataWatch, "step", lambda self: [])
    engine = backtest_lib.run_engine_bars(
        _transient_tamper_class(), _TRANSIENT_SETTING, "C15", _synth_bars())
    assert engine.strategy is not None


@pytest.mark.parametrize("src", [
    "def entry_signal(self):\n    x = self.am.close[:, None]\n    return 1\n",
    "def entry_signal(self):\n    x = self.am.close[...]\n    return 1\n",
    "def entry_signal(self):\n    x = self.am.close[None]\n    return 1\n",
    "def entry_signal(self):\n    x = self.am.close.swapaxes(0, 0)\n    return 1\n",
    "def entry_signal(self):\n    x = self.am.close[slice(None)]\n    return 1\n",
])
def test_the_static_layer_refuses_every_basic_index_that_makes_a_view(src):
    """第 13 轮 Critical 1(静态半边):第 12 轮只认 `ast.Slice`,`[:, None]`/`[...]`/`[None]`/
    `.swapaxes()`/`slice(...)` 都被放过,拿到的是 numpy 视图。"""
    assert screen(src), "产生视图的下标没被拦下"


@pytest.mark.parametrize("src", [
    "def entry_signal(self):\n    x = self.am.close[-1]\n    return 1\n",
    "def entry_signal(self):\n    i = 3\n    x = self.am.close[i]\n    return 1\n",
    "def entry_signal(self):\n    x = self.am.close[-2] - self.am.close[-1]\n    return 1\n",
])
def test_the_static_layer_still_allows_scalar_reads(src):
    """反向:整数下标取到的是一个数,必须放行,否则正常候选会被整片拒掉。"""
    assert not screen(src), screen(src)


# ---------- High 2:历史守卫的执行点检查 ----------

def _guard_root(tmp_path):
    """造一个最小的仓库骨架:历史守卫只认注册表里那两个文件的相对路径。"""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    return tmp_path


_COVERED_BODY = (
    "import risk.constitution as const\n\n"
    "def run(code):\n"
    "    before = const.snapshot_protected_state()\n"
    "    exec(code, {})\n"
    "    return const.check_protected_state(before, const.snapshot_protected_state())\n")


def test_the_guard_notices_a_check_moved_into_a_function_nobody_calls(tmp_path):
    """High 2(A3):把第四层的比对挪进一个**永不被调用**的函数。

    第 12 轮 codex 在独立克隆上端到端证明:这么一改,守卫 exit 0,
    而第四层就此消失——候选改写风控函数不再有人报。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _guard_root(tmp_path)
    body = (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    return None\n\n"
        "def _never_called(before):\n"
        "    return const.check_protected_state(before, const.snapshot_protected_state())\n")
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    problems = check_call_site_shape(root)
    assert problems, "第四层挪进死函数,守卫却没报出来"


def test_the_guard_notices_a_semantically_equal_second_argument(tmp_path):
    """High 2(A2'):`check_protected_state(before, dict(snapshot_protected_state()))`。

    两个实参语法不同、语义相同,比对必然通过——第二个实参必须是一次
    `snapshot_protected_state()` 调用**本身**。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _guard_root(tmp_path)
    body = (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    return const.check_protected_state(before, dict(const.snapshot_protected_state()))\n")
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    problems = check_call_site_shape(root)
    assert any("snapshot_protected_state" in p for p in problems), problems


def test_the_guard_notices_an_executor_that_only_mentions_the_names_in_a_comment(tmp_path):
    """High 2(A5):新执行入口在**注释**里写上那两个名字,就想冒充"已走第四层"。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(
        "# 这里本该调用 snapshot_protected_state 与 check_protected_state\n"
        "def run(code):\n"
        "    exec(code, {})\n"
        "    return None\n", encoding="utf-8")
    problems = check_executor_completeness(root)
    assert any("brand_new_runner" in p for p in problems), problems


def test_the_guard_notices_an_executor_that_evaluates_the_candidate(tmp_path):
    """High 2(A4):新执行入口用 `eval(compile(src, ...))` 跑候选——它也必须走第四层。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(
        "def run(code):\n"
        "    exec(compile(code, '<candidate>', 'exec'), {})\n"
        "    return None\n", encoding="utf-8")
    problems = check_executor_completeness(root)
    assert any("brand_new_runner" in p for p in problems), problems


def test_the_guard_notices_an_executor_that_only_imports_a_covered_module(tmp_path):
    """High 2(A8):`import scripts.backtest_lib` 只是把模块拉进来,
    不代表这个入口自己比对了指纹——传递闭包不能算数。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(
        "import scripts.backtest_lib\n\n"
        "def run(code):\n"
        "    exec(code, {})\n"
        "    return None\n", encoding="utf-8")
    problems = check_executor_completeness(root)
    assert any("brand_new_runner" in p for p in problems), problems


def test_the_guard_accepts_a_new_executor_that_does_the_fourth_layer(tmp_path):
    """守卫不能只会报错:自己走了第四层的新执行入口必须放行。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(_COVERED_BODY, encoding="utf-8")
    assert check_executor_completeness(root) == []


def test_the_guard_accepts_the_shipped_execution_points_after_the_hardening():
    """仓库里现有的两个执行点必须**通过**新加的形态检查(守卫不能只会报错)。"""
    from tools.check_constitution_history import (check_call_site_shape,
                                                 check_constitution_module_is_clean,
                                                 check_executor_completeness)

    assert check_call_site_shape(ROOT) == []
    assert check_executor_completeness(ROOT) == []
    assert check_constitution_module_is_clean(ROOT) == []


# ---------- 第 13 轮 codex:守卫要验"效果"而不只验"形态" ----------

def _two_file_guard_root(tmp_path, body):
    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(body, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(body, encoding="utf-8")
    return root


def test_the_guard_notices_a_discarded_comparison_result(tmp_path):
    """第 13 轮 High 1:`_ = const.check_protected_state(...)`——调用形态完全对,结论被扔掉。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _two_file_guard_root(tmp_path, (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    _ = const.check_protected_state(before, const.snapshot_protected_state())\n"
        "    return None\n\n"
        "run('')\n"))
    problems = check_call_site_shape(root)
    assert any("丢弃" in p for p in problems), problems


def test_the_guard_notices_a_comparison_whose_result_is_never_read(tmp_path):
    """结论存进了列表,但这个列表之后从没被读取——和扔掉一样。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _two_file_guard_root(tmp_path, (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    findings = []\n"
        "    findings.extend(const.check_protected_state(before, const.snapshot_protected_state()))\n"
        "    return None\n\n"
        "run('')\n"))
    problems = check_call_site_shape(root)
    assert any("丢弃" in p for p in problems), problems


def test_the_guard_notices_a_comparison_hidden_in_a_lambda(tmp_path):
    """第 13 轮 High 2:把比对挪进 `lambda: ...`,它永远不会执行。"""
    from tools.check_constitution_history import check_call_site_shape

    root = _two_file_guard_root(tmp_path, (
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    exec(code, {})\n"
        "    check = lambda: const.check_protected_state(before, const.snapshot_protected_state())\n"
        "    return None\n\n"
        "run('')\n"))
    problems = check_call_site_shape(root)
    assert any("lambda" in p for p in problems), problems


def test_the_guard_notices_an_executor_whose_fourth_layer_hangs_on_another_function(tmp_path):
    """第 13 轮 High 3:`exec` 在 `run()` 里裸奔,那两个名字只放在一个从不被调用的函数里。

    文件级的"出现过那两个调用"被满足了,执行点本身却没有走第四层。"""
    from tools.check_constitution_history import check_executor_completeness

    root = _guard_root(tmp_path)
    (root / "scripts" / "backtest_lib.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "research_api.py").write_text(_COVERED_BODY, encoding="utf-8")
    (root / "scripts" / "brand_new_runner.py").write_text(
        "import risk.constitution as const\n\n"
        "def run(code):\n"
        "    exec(code, {})\n"
        "    return None\n\n"
        "def _decoy(code):\n"
        "    before = const.snapshot_protected_state()\n"
        "    return const.check_protected_state(before, const.snapshot_protected_state())\n",
        encoding="utf-8")
    problems = check_executor_completeness(root)
    assert any("brand_new_runner" in p for p in problems), problems


# ---------- Medium 2:__module__ 伪造骗不过"仓库自己的类"豁免 ----------

def test_a_spoofed_module_name_does_not_buy_the_repository_exemption():
    """候选在类体里写 `__module__ = "strategies.trend_atr_15m_strategy"` 想骗过豁免。

    豁免的判定必须是"这个类**确实由那个模块定义**":
    `vars(sys.modules[module]).get(name) is klass`——伪造 `__module__` 只是改了字符串,
    并没有把类塞进那个模块的字典里。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Cand(TrendAtrStrategy):
        def entry_signal(self) -> int:
            return 0

        def get_size(self) -> float:            # 越权:重写了受保护的方法
            return 0.001

    Cand.__module__ = "strategies.trend_atr_15m_strategy"
    template = const.load_template()
    strategy = object.__new__(Cand)
    findings = const._runtime_class_dict_violations(strategy, template, const.default())
    assert findings, "伪造 __module__ 骗过了豁免(Medium 2 复发)"


def test_a_candidate_cannot_buy_the_exemption_by_planting_itself_in_a_repository_module(monkeypatch):
    """第 13 轮 Medium 1:`Evil.__module__ = "<仓库模块>"; <仓库模块>.Evil = Evil`。

    第 12 轮的判据之一是"模块字典里有同名对象"——候选自己就能满足它。
    现在只看类体里自己的代码落在哪个文件:候选的函数不在那个模块文件里,豁免不成立。"""
    import strategies.trend_atr_15m_strategy as repo_module
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class Evil(TrendAtrStrategy):
        def get_size(self) -> float:                      # 越权:重写了受保护的方法
            return 0.001

    Evil.__module__ = repo_module.__name__
    monkeypatch.setattr(repo_module, "Evil", Evil, raising=False)
    assert not const._is_in_repo_strategy_class(Evil)
    findings = const._runtime_class_dict_violations(
        object.__new__(Evil), const.load_template(), const.default())
    assert findings, "伪造模块成员表骗过了豁免(第 13 轮 Medium 1 复发)"


def test_a_class_with_only_data_attributes_does_not_get_the_exemption():
    """没有任何自己的函数的类不豁免:"全部来自仓库"在空集上恒成立,
    候选写个只有数据属性的类(`risk_budget = 1e9`)就能蒙混过去。"""
    from strategies.trend_atr_strategy import TrendAtrStrategy

    class OnlyData(TrendAtrStrategy):
        risk_budget = 1e9

    assert not const._is_in_repo_strategy_class(OnlyData)


def test_the_pending_dir_reserved_names_match_the_sandbox_config():
    """宪法模块不能 import `research_config`(它依赖 bootstrap/vnpy),所以复刻了保留目录集合;
    两份必须一致,否则宪法与沙盒对"候选目录"的口径就会错位(第 13 轮 Medium 2)。"""
    import research_config

    assert const._PENDING_DIR_RESERVED == frozenset(research_config.RESERVED_DIRS)


@pytest.mark.parametrize("pending, expected_default", [
    ("strategies", True),            # 沙盒会拒绝加载这份配置;宪法不得比它更宽松
    ("risk", True),
    ("../outside", True),            # 越出仓库
    ("/abs/elsewhere", True),        # 绝对路径
    ("my_pending", False),           # 合法的自定义候选目录
])
def test_the_pending_dir_follows_the_same_validation_as_the_sandbox(tmp_path, monkeypatch, pending, expected_default):
    import json

    config = tmp_path / "sandbox.json"
    config.write_text(json.dumps({"paths": {"pending_dir": pending}}), encoding="utf-8")
    monkeypatch.setenv("RESEARCH_SANDBOX_CONFIG", str(config))
    resolved = const._repo_pending_dir()
    default = const._REPO_ROOT / const._DEFAULT_PENDING_DIR
    assert (resolved == default) is expected_default, resolved


def test_the_repository_strategies_keep_their_exemption():
    """仓库自己的策略仍然合法地重写内部方法(豁免不能一刀切掉)。"""
    from strategies.intraday_demo_strategy import IntradayDemoStrategy

    template = const.load_template()
    strategy = object.__new__(IntradayDemoStrategy)
    assert const._runtime_class_dict_violations(strategy, template, const.default()) == []
