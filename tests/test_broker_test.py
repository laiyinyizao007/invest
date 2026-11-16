"""国信取证运行器的步骤测试:假网关 + 同步事件引擎 + **真实**风控规则(不联网、不连 CTP)。"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

import broker_evidence as be  # noqa: E402
import broker_test as bt  # noqa: E402
from collect_reject_texts import Collector  # noqa: E402
from fakes import FakeGateway, SyncEventEngine  # noqa: E402
from trading.events import EVENT_GUARD_ALERT  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402
from vnpy.event import Event  # noqa: E402
from vnpy.trader.constant import Direction, Offset  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import (  # noqa: E402
    EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_TIMER, EVENT_TRADE,
)


class Env:
    """假网关命名为 CTP(取证运行器里网关名写死为 CTP),挂真实的 SafeRiskManagerApp 与 Collector。"""

    def __init__(self, tmp_path: Path, in_session: bool = True) -> None:
        self.event_engine = SyncEventEngine()
        self.main = MainEngine(self.event_engine)
        self.gw: FakeGateway = self.main.add_gateway(FakeGateway, "CTP")
        self.gw.td_api = SimpleNamespace(connect_status=True, login_status=True)
        self.risk = self.main.add_app(SafeRiskManagerApp)
        self.rule = self.risk.rules["账户风控"]
        self.state_file = tmp_path / "state" / "guard.json"
        self.rule.update_setting({"state_file": str(self.state_file)})
        self.clock_t = 1000.0
        self.rule.mono = lambda: self.clock_t
        self.collector = Collector()
        self.alerts: list[dict] = []
        self.trades: list = []
        for ev, fn in ((EVENT_LOG, self.collector.on_log), (EVENT_ORDER, self.collector.on_order),
                       (EVENT_TICK, self.collector.on_tick), (EVENT_CONTRACT, self.collector.on_contract),
                       (EVENT_POSITION, self.collector.on_position),
                       (EVENT_GUARD_ALERT, lambda e: self.alerts.append(e.data)), (EVENT_TRADE, lambda e: self.trades.append(e.data))):
            self.event_engine.register(ev, fn)
        self.gw.push_contract()
        self.gw.push_tick()
        self.gw.push_account(10_000)
        self.ctx = bt.Ctx(main_engine=self.main, risk_engine=self.risk, collector=self.collector, symbol="c2601.DCE",
                          gateway_name="CTP", alerts=self.alerts, trades=self.trades,
                          pump=self.pump, sleep=lambda s: None, in_session=in_session, position_wait=0.2,
                          # 注入假时钟:每次 pump 前进 1 秒。超时与「计数稳定 1 秒」都按它算,
                          # 测试不必真等(2026-11-16 codex 复核 M3:改之前整个套件慢 63%)。
                          clock=lambda: self.clock_t)
        self.ctx.contract = self.collector.contract_of("c2601.DCE")
        self.ctx.tick = self.collector.tick_of("c2601.DCE")
        self.book = bt.Book()
        self.pump()

    def pump(self) -> None:
        self.clock_t += 1.0
        self.event_engine.put(Event(EVENT_TIMER))


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.main.close()



def _profile_from_os():
    """让 main() 按**当前进程环境变量**解析配置(绕开真实的 .env 文件)。

    配置解析现在只走 ctp_tools.profile.load_profile,不再走 bootstrap.load_env。
    """
    from ctp_tools.profile import Profile

    return Profile(env=dict(os.environ), hard_conflicts=[], notes=[])

def item(env, name) -> be.Evidence:
    return env.book.items[name]


# ---------- P4.1 指令检查 ----------

def test_validity_checks_block_bad_contract_price_and_volume(env):
    bt.step_validity(env.ctx, env.book)
    ev = item(env, "P4.1")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["合约代码", "合约最小变动价位", "单笔委托最大手数"]
    assert all("本地风控已拒绝" in s.lines[1] for s in ev.shots)
    assert env.gw.sent == []                                   # 三个错误指令一个都没发往柜台


# ---------- P2.3 重复报单 / P3.1 阈值 / T6 ----------

def test_duplicate_detection_blocks_and_alerts_and_restores_limit(env):
    original = env.risk.rules["重复报单检查"].duplicate_order_limit
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["ok"] and item(env, "P2.3").status == be.PASS
    assert env.risk.rules["重复报单检查"].duplicate_order_limit == original
    assert any(a["title"] == "风控阈值触发" and "重复报单" in a["message"] for a in env.alerts)
    assert all(env.collector.order_of(oid) is None or not env.collector.order_of(oid).is_active() for oid in list(env.gw.orders))


def test_threshold_covers_all_three_metrics_in_session(env):
    daily = env.risk.rules["每日上限检查"]
    limits = (daily.total_order_limit, daily.total_cancel_limit)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.PASS, p31.summary
    assert [s.label for s in p31.shots] == ["报单总笔数", "撤单总笔数", "重复报单总笔数"]
    assert item(env, "T6").status == be.PASS
    assert (daily.total_order_limit, daily.total_cancel_limit) == limits         # 阈值已还原
    titles = [a["message"] for a in env.alerts if a["title"] == "风控阈值触发"]
    assert any("汇总委托笔数" in t for t in titles) and any("汇总撤单笔数" in t for t in titles)


def test_a_stalled_counter_makes_the_threshold_step_inconclusive_not_failed(env, monkeypatch):
    """计数没推进 = 演示**不可靠**,不能写成「阈值失灵」(FAIL)。

    计数是**异步**更新的(委托回报到达才 +1),没推进时我们根本不知道第二笔到底
    会不会被拦 —— 那是"没法判定",不是"判定为失败"。证据写错方向会误导整改。
    """
    monkeypatch.setattr(bt, "wait_counter_at_least", lambda *a, **k: False)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.status
    assert "计数未推进" in p31.summary
    assert item(env, "T6").status == be.SKIP
    assert any("计数在 5 秒内没有推进" in line for s in p31.shots for line in s.lines)


def test_the_cancel_metric_only_judges_its_counter_after_the_cancel(env, monkeypatch):
    """撤单指标的计数判断必须放在**撤单之后**。

    「汇总撤单笔数」只在**撤单回报**到达后才 +1(`DailyLimitRule.on_order`),
    在撤单之前等它必然等不到 → 这一项会永远判「计数未推进」(SKIP)。

    这个缺陷在假件里是**看不出来**的:假件的时钟每 pump 一次跳 1 秒,
    挂单超时会被风控自动撤掉,撤单计数提前推了上去,于是"等计数"总能等到。
    所以这里把挂单超时调到极大(不让风控自动撤单),再盯「等待发生的时机」。
    """
    env.rule.stale_order_seconds = 10 ** 6          # 挡住「挂单超时自动撤单」这个干扰项
    seen: list[tuple[str, int]] = []
    real = bt.wait_counter_at_least

    def spy(ctx, rule, attr, target, timeout):
        seen.append((attr, len(env.gw.cancel_requests)))
        return real(ctx, rule, attr, target, timeout)

    monkeypatch.setattr(bt, "wait_counter_at_least", spy)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    cancel_waits = [n for attr, n in seen if attr == "total_cancel_count"]
    assert cancel_waits, "撤单指标应当等待撤单计数推进"
    assert min(cancel_waits) >= 1, "撤单计数只能在撤单回报之后判断推进"
    assert item(env, "P3.1").status == be.PASS, item(env, "P3.1").summary


def test_a_stale_counter_base_makes_the_threshold_step_inconclusive(env, monkeypatch):
    """计数**基数取旧** → 对照做不成 → 判不出来(SKIP),不能报成「阈值失灵」。

    场景:设置阈值时计数已经被别的委托回报推过了阈值(基数是旧的)。这时
    「阈值内的那一笔」被拦下是**必然**的,不是阈值没生效 —— 拿旧基数比出来的
    结论不算数,只能记「判不出来」。
    """
    real = bt.settle_counter

    def stale(ctx, rule, attr, timeout=5.0):
        return max(real(ctx, rule, attr, timeout) - 1, 0)     # 故意报旧一笔

    monkeypatch.setattr(bt, "settle_counter", stale)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert any("基数取旧" in line for s in p31.shots for line in s.lines)


def test_an_unconfirmed_cancel_makes_the_cancel_metric_inconclusive(env, monkeypatch):
    """我们自己的撤单**没确认** → 判不出来(SKIP),不能报成「阈值生效」。

    场景:撤单请求没生效(柜台没撤),但**别的**撤单回报把计数推过了阈值 ——
    这时"超过阈值的委托被拦住"确实发生了,可它不是我们要演示的那一次触发
    (我们那张单还挂在柜台上,迟早成交)→ 只能记「判不出来」。
    """
    env.ctx.cancel_wait = 0.5
    env.gw.cancel_works = False                     # 我们的撤单请求不生效
    real_wait = bt.wait_counter_at_least

    def fake_wait(ctx, rule, attr, target, timeout):
        if attr == "total_cancel_count":
            rule.total_cancel_count = max(rule.total_cancel_count, target)   # 别的撤单回报把计数推上去
            return True
        return real_wait(ctx, rule, attr, target, timeout)

    monkeypatch.setattr(bt, "wait_counter_at_least", fake_wait)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert any("撤单未确认" in line for s in p31.shots for line in s.lines)


def test_an_order_over_the_limit_that_was_still_sent_is_a_failure_not_a_skipped_test(env, monkeypatch):
    """阈值确实失效 = **硬失败**,不能被「判不出来」洗成 SKIP。

    2026-11-16 codex 复核 H1 复现:计数一次跳了不止 +1(基数取旧),
    而超阈值的那一笔**真的发出去了**(证据行字面写着「未被拦截(意外)」)——
    这时 P3.1/T6 判成 SKIP,报告里就写成「未测试」,一次真实的阈值失效被洗掉了。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:                     # 第二笔 = 超阈值那笔:假装风控没拦下
            return "VT999999"
        return real_send(ctx, req)

    real_settle = bt.settle_counter

    def stale(ctx, rule, attr, timeout=5.0):
        return max(real_settle(ctx, rule, attr, timeout) - 2, 0)   # 故意报旧两笔 ⇒ 基数取旧

    monkeypatch.setattr(bt, "settle_counter", stale)
    dup = bt.step_duplicate(env.ctx, env.book)
    calls["n"] = 0
    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "阈值未生效" in p31.summary
    assert item(env, "T6").status == be.FAIL
    assert any("未被拦截(意外)" in line for s in p31.shots for line in s.lines)


def test_a_skipped_threshold_says_which_part_could_not_be_judged(env, monkeypatch):
    """SKIP 必须说清**哪里**判不出来,不能一律写「计数未推进」。

    2026-11-16 codex 复核 M2:summary 写死「计数未推进」,对「基数取旧」「撤单未确认」
    两档完全说反;这段文字会落进报告的缺失项清单,写错会把人引向错误的动作。
    """
    real = bt.settle_counter

    def stale(ctx, rule, attr, timeout=5.0):
        return max(real(ctx, rule, attr, timeout) - 1, 0)     # 故意报旧一笔 ⇒ 基数取旧

    monkeypatch.setattr(bt, "settle_counter", stale)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "计数基数取旧" in p31.summary
    assert "计数未推进" not in p31.summary


def test_an_unconfirmed_cancel_in_the_summary_points_at_the_counter(env, monkeypatch):
    """「撤单未确认」要在摘要里点名:演示单可能还挂在柜台上,得人去核对。"""
    env.ctx.cancel_wait = 0.5
    env.gw.cancel_works = False                     # 我们的撤单请求不生效
    real_wait = bt.wait_counter_at_least

    def fake_wait(ctx, rule, attr, target, timeout):
        if attr == "total_cancel_count":
            rule.total_cancel_count = max(rule.total_cancel_count, target)
            return True
        return real_wait(ctx, rule, attr, target, timeout)

    monkeypatch.setattr(bt, "wait_counter_at_least", fake_wait)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "撤单未确认" in p31.summary
    assert "柜台" in p31.summary


def test_orders_that_could_not_be_cancelled_are_written_into_the_evidence(env, monkeypatch):
    """撤不掉的委托必须进**证据**,不能只在日志里说一句。

    2026-11-16 codex 复核 L3:`cancel_active` 原先什么都不返回,步骤证据里一个字都没有,
    只能靠整轮结束时的 final_safety 兜底。
    """
    env.rule.stale_order_seconds = 10 ** 6         # 挡住「挂单超时自动撤单」这个干扰项
    env.gw.cancel_works = False                    # 撤单请求不生效
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert any("仍在活动或本地查不到的委托" in line for s in p31.shots for line in s.lines), p31.shots


def test_an_order_blocked_by_another_rule_is_not_reported_as_a_tight_threshold(env):
    """委托被**别的规则**拦下 ⇒ 不能写成「阈值设得太紧」(2026-11-16 codex 复核 H1 复现)。

    复现:把「委托规模检查」的委托价值上限调到 1 元,任何委托都会被它拦下。
    风控日志写的是「委托被拦截，委托价值X超过上限1.0」,而旧代码的断言却是
    「阈值内的委托被拦下了(阈值设得太紧,演示不成立)」—— 那是对风控系统的错误指控,
    原样进国信取证报告会把人引向「调阈值/改配置」,而真正该做的是改演示委托的规模。
    """
    env.risk.rules["委托规模检查"].order_value_limit = 1.0
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "阈值设得太紧" not in p31.summary
    assert "不是本项阈值拦的" in p31.summary
    assert "委托价值" in p31.summary               # 把风控日志里的真实原因带进摘要
    assert "报单阈值未能限制下单" not in item(env, "T6").summary


def test_a_late_report_between_reading_the_count_and_sending_is_inconclusive(env, monkeypatch):
    """迟到回报在「读计数」与「发单」之间推进计数 ⇒ 判不出来(SKIP),不是「阈值失灵」。

    复现(codex 复核 H1 路径 B):计数被**别的**回报推进了一格,于是我们那笔「阈值内」的
    委托被阈值拦下 —— 阈值其实**生效**了,这轮对照只是做不成(基数取旧)。
    旧代码把它写成硬失败「阈值内的委托被拦下了(阈值设得太紧,演示不成立)」。
    """
    daily = env.risk.rules["每日上限检查"]
    real_send = bt.send

    def fake_send(ctx, req):
        daily.total_order_count += 1               # 模拟迟到回报:发单**之前**计数就动了
        return real_send(ctx, req)

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "阈值设得太紧" not in p31.summary
    assert "基数取旧" in p31.summary
    # 归因靠的是**风控日志点名了我们这一项**(「汇总委托笔数…达到上限…」),
    # 不是靠事后猜计数 —— 断言里能看到日志原文才算数。
    assert "汇总委托笔数" in p31.summary


def test_a_disconnected_gateway_does_not_accuse_the_threshold(env):
    """通道断开 ⇒ 委托没发出去,但**不是**阈值的锅,也不能说「阈值太紧」。

    断连时框架本地就失败(没有委托、没有风控拦截日志),计数一动不动。
    """
    env.gw.mode = "disconnected"
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "阈值设得太紧" not in p31.summary
    assert "没有风控拦截日志" in p31.summary


def test_a_demo_order_that_filled_points_at_the_position_not_at_the_counter(env):
    """撤单没赶上、演示单**成交了** ⇒ 摘要要说「账户可能多出持仓」,不是「去柜台撤挂单」。

    2026-11-16 codex 复核 M-1:同一份证据里写着「终态 全部成交」,摘要却让人去柜台撤单 ——
    方向完全相反:真正的问题是账户多出一笔持仓,要平仓核对。
    """
    env.gw.mode = "fill"
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "已成交" in p31.summary
    assert "柜台上" not in p31.summary


def test_the_runtime_guard_and_the_precheck_share_one_trading_day_clock(env):
    """预检与运行时必须用**同一个**时钟函数,否则两边算出的期货交易日会分叉。

    2026-11-16 codex 复核 M2:预检按北京时间算交易日、运行时的账户风控按宿主机本地时间算。
    宿主机是 UTC-8 时,北京时间 20:30 与本地 04:30 分属不同的交易日 ⇒
    预检判「没锁」放行、运行时判「当日锁定」⇒ 白跑一次国信登录额度。
    """
    from risk import account_guard as ag
    from trading import guard_rule as gr

    assert gr.beijing_now is ag.beijing_now          # 运行时与纯逻辑模块同源
    assert env.rule.clock is ag.beijing_now          # 运行时的账户风控按北京时间算交易日
    assert bt.beijing_now is ag.beijing_now          # 取证预检用同一个函数


def test_the_settle_round_budget_follows_the_timeout():
    """等待窗口变长时轮数上限必须跟着长 —— 写死的上限会**静默**提前返回。

    2026-11-16 codex 复核 L1:原先上限写死 200 轮(=20 秒),timeout 调大后
    `settle_counter` 在 20 秒处悄悄返回,返回值与「到点放弃」无法区分。
    """
    assert bt.settle_rounds(5.0) == bt.SETTLE_MIN_ROUNDS
    assert bt.settle_rounds(60.0) > bt.SETTLE_MIN_ROUNDS


def test_an_order_the_collector_lost_is_reported_as_not_confirmed_gone(env):
    """本地查不到委托 ≠ 它已经不在柜台上:状态未知就当还挂着,进证据。

    2026-11-16 第 1 轮复核建议:查不到订单号时只当「已消失」会漏报,
    而这张单可能还挂在柜台上 → 必须和「仍在活动」一起报出来。
    """
    sent: list[str] = []
    real_send = bt.send

    def fake_send(ctx, req):
        oid = real_send(ctx, req)
        sent.append(oid)
        return oid

    env.ctx.collector.orders.clear()        # 本地缓存里没有这张单(模拟回报丢失)
    bt.send = fake_send
    try:
        left = bt.cancel_active(env.ctx, ["VT-NOT-FOUND"])
    finally:
        bt.send = real_send
    assert left == ["VT-NOT-FOUND"]         # 查不到 = 撤不掉,不许当成「干净了」


def test_the_production_clock_is_time_monotonic():
    """生产里 `Ctx.clock` 必须就是 `time.monotonic`(超时语义不能被改)。

    测试注入假时钟是为了跑得快;一旦默认时钟被换成别的东西,
    生产里的「等 5 秒/等稳定 1 秒」的口径就变了,而测试还全绿 ⇒ 兜一条断言。
    """
    import time as _time
    assert bt.Ctx.__dataclass_fields__["clock"].default is _time.monotonic


def test_cancel_active_waits_until_the_order_reaches_a_terminal_state(env, monkeypatch):
    """撤单请求发出 ≠ 撤成功:撤完必须等到终态。

    不等终态的后果有两个:①单子还挂在柜台上,会变成"晚到的成交";
    ②撤单回报还没到,风控的撤单计数还没 +1 → 下一项指标拿到的**计数基数是旧的**。
    """
    pending: list = []
    real_cancel = env.gw.cancel_order

    def delayed_cancel(req):
        pending.append(req)                      # 柜台回报是异步的:先收下请求

    monkeypatch.setattr(env.gw, "cancel_order", delayed_cancel)

    def pump_and_apply():
        env.pump()
        while pending:
            real_cancel(pending.pop(0))          # 下一次事件循环才真正撤掉

    env.ctx.pump = pump_and_apply
    oid = env.main.send_order(bt.make_req(env.ctx, price=2200.0), "CTP")
    assert oid
    bt.cancel_active(env.ctx, [oid])
    order = env.collector.order_of(oid)
    assert order is not None and not order.is_active(), "撤单后必须等到终态"


def test_a_counter_that_jumps_by_more_than_one_is_inconclusive(env, monkeypatch):
    """计数一次跳不止 +1 = 有**别的**委托回报混进来 → 基数不可信 → 判不出来(SKIP)。"""
    real = bt.wait_counter_at_least

    def jumpy(ctx, rule, attr, target, timeout):
        if attr == "total_order_count":
            rule.total_order_count = max(rule.total_order_count, target + 1)   # 别的回报也到了
            return True
        return real(ctx, rule, attr, target, timeout)

    monkeypatch.setattr(bt, "wait_counter_at_least", jumpy)
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert any("不止 +1" in line for s in p31.shots for line in s.lines)


def test_a_missing_state_file_means_not_locked(tmp_path):
    """没有状态文件 = 从来没锁过(头一次跑),放行。"""
    assert bt.locked_state_note(tmp_path / "state" / "guard.json") == ""


def test_a_corrupt_state_file_is_not_treated_as_unlocked(tmp_path):
    """状态文件损坏 = 判不出来 → 拒绝跑,不能当成「没锁」。

    当成「没锁」照跑的后果:风控按锁定状态把委托全拦下,白费一次券商登录额度,
    证据只剩一堆「被拦下」。
    """
    state = tmp_path / "guard.json"
    state.write_text("{这不是 JSON", encoding="utf-8")
    note = bt.locked_state_note(state)
    assert note and "损坏" in note


def test_an_unreadable_state_file_is_not_treated_as_unlocked(tmp_path, monkeypatch):
    """状态文件读不出来(权限/占用)同样判不出来 → 拒绝跑。

    判据与运行时同一份(`AccountGuard._load` 把读失败当损坏 → 硬锁定),
    所以提示里出现的是「硬锁定」并附上真实原因,不是本地的「读不出来」。
    """
    state = tmp_path / "guard.json"
    state.write_text("{}", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("文件被另一个进程占用")

    monkeypatch.setattr(Path, "read_text", boom)
    note = bt.locked_state_note(state)
    assert note and "占用" in note          # 拒绝跑,并说明真实原因


def test_threshold_cancel_metric_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        dup = bt.step_duplicate(e.ctx, e.book)
        bt.step_threshold(e.ctx, e.book, dup)
        p31 = e.book.items["P3.1"]
        assert p31.status == be.SKIP and "交易时段" in p31.summary
        assert e.book.items["T6"].status == be.PASS                              # 报单阈值照样可演示
    finally:
        e.main.close()


# ---------- P1.3 稳定运行(模板 2.1)----------

def test_stability_sends_all_orders_records_metrics_and_cleans_up(env):
    daily = env.risk.rules["每日上限检查"]
    dup, active = env.risk.rules["重复报单检查"], env.risk.rules["活动委托检查"]
    before = (daily.total_order_limit, daily.total_cancel_limit, dup.duplicate_order_limit, active.active_order_limit,
              env.rule.cancel_lock_limit, env.rule.cancel_count)
    env.ctx.stability_interval = 0
    bt.step_stability(env.ctx, env.book, total=30)
    ev = item(env, "P1.3")
    assert ev.status == be.PASS, ev.summary
    assert ev.metrics["orders"] == 30 and ev.metrics["accepted"] == 30 and ev.metrics["rejected"] == 0
    assert [s.label for s in ev.shots] == ["30笔"]
    assert len(env.gw.sent) == 30                                   # 30 笔全部经风控放行到了柜台(超过活动委托上限 10 也没被拦)
    assert all(not o.is_active() for o in map(env.collector.order_of, env.gw.orders) if o is not None)    # 清场:不留挂单
    after = (daily.total_order_limit, daily.total_cancel_limit, dup.duplicate_order_limit, active.active_order_limit,
             env.rule.cancel_lock_limit, env.rule.cancel_count)
    assert after == before                                          # 临时抬高的上限与撤单锁定计数都已还原


def test_stability_fails_when_the_counter_rejects_orders_and_still_restores_limits(env):
    env.gw.mode = "reject"
    env.ctx.stability_interval = 0
    limit = env.risk.rules["活动委托检查"].active_order_limit
    bt.step_stability(env.ctx, env.book, total=5)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and ev.metrics["rejected"] == 5
    assert env.risk.rules["活动委托检查"].active_order_limit == limit


def test_stability_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        bt.step_stability(e.ctx, e.book)
        ev = e.book.items["P1.3"]
        assert ev.status == be.SKIP and "交易时段" in ev.summary and e.gw.sent == []
    finally:
        e.main.close()


def test_stability_send_exception_becomes_failed_evidence_and_restores_limits(env, monkeypatch):
    limit = env.risk.rules["每日上限检查"].total_order_limit
    monkeypatch.setattr(bt, "send", lambda ctx, req: (_ for _ in ()).throw(RuntimeError("boom")))
    bt.step_stability(env.ctx, env.book, total=3)                    # 不再把异常抛出去:否则整次运行的证据都丢了
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "boom" in ev.summary
    assert env.risk.rules["每日上限检查"].total_order_limit == limit


def test_stability_cleanup_exception_still_restores_limits_and_cancel_count(env, monkeypatch):
    daily, guard = env.risk.rules["每日上限检查"], env.rule
    before = (daily.total_order_limit, guard.cancel_lock_limit, guard.cancel_count)
    env.ctx.stability_interval = 0
    monkeypatch.setattr(bt, "cancel_active", lambda ctx, ids: (_ for _ in ()).throw(RuntimeError("cancel boom")))
    bt.step_stability(env.ctx, env.book, total=3)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "cancel boom" in ev.summary
    assert (daily.total_order_limit, guard.cancel_lock_limit, guard.cancel_count) == before


def test_stability_fails_when_orders_are_still_active_after_cleanup(env, monkeypatch):
    env.ctx.stability_interval = 0
    env.ctx.stability_wait = 0.0
    monkeypatch.setattr(bt, "cancel_active", lambda ctx, ids: None)           # 撤单没发出去:订单仍然挂着
    bt.step_stability(env.ctx, env.book, total=3)
    ev = item(env, "P1.3")
    assert ev.status == be.FAIL and "未清场 3" in ev.summary
    assert any("未确认撤销" in line for line in ev.shots[0].lines)


def test_final_safety_reports_orders_that_stay_active(env, monkeypatch):
    env.gw.mode = "accept"
    oid = bt.send(env.ctx, bt.make_req(env.ctx, price=bt.probe_price(env.ctx)))
    assert oid
    monkeypatch.setattr(bt, "FINAL_CANCEL_WAIT", 0.0)
    monkeypatch.setattr(env.main, "cancel_order", lambda *a, **k: None)        # 撤单请求被吞掉
    notes = bt.final_safety(env.ctx)
    assert any("⚠⚠" in n and "未确认撤销" in n for n in notes)


def test_stability_is_gated_by_flat_check_like_other_order_steps():
    assert "P1.3" in bt.ORDER_ITEMS


# ---------- P5.1 暂停交易 ----------

def test_pause_step_blocks_opens_allows_closes_and_restores(env):
    bt.step_pause(env.ctx, env.book)
    ev = item(env, "P5.1")
    assert ev.status == be.PASS, ev.summary
    text = "\n".join(ev.shots[0].lines)
    assert "暂停期间开仓委托:被拒绝" in text and "暂停期间平仓委托:放行" in text and "恢复后开仓委托:通过风控" in text
    assert not (env.state_file.parent / "pause_trading.flag").exists()


# ---------- P2.2 笔数统计 ----------

def test_counts_step_reads_rule_counters(env):
    env.gw.mode = "accept"
    bt.send(env.ctx, bt.make_req(env.ctx, price=2100.0))
    bt.step_counts(env.ctx, env.book)
    ev = item(env, "P2.2")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["报单笔数统计", "撤单笔数统计"]
    assert "汇总报单笔数 1" in ev.shots[0].lines[1]


def test_counts_step_is_skipped_when_nothing_was_sent(env):
    bt.step_counts(env.ctx, env.book)
    assert item(env, "P2.2").status == be.SKIP


# ---------- T3 / T5 / P1.2 / T4 ----------

def test_basic_trade_opens_closes_and_confirms_flat(env):
    env.gw.mode = "fill"
    env.gw.position_lag = False
    bt.step_basic_trade(env.ctx, env.book)
    assert item(env, "T3").status == be.PASS, item(env, "T3").summary
    assert item(env, "T5").status == be.PASS and "成交回报" in item(env, "T5").shots[0].lines[0]
    assert [s.label for s in item(env, "P1.2").shots] == ["开仓", "平仓"]
    assert [r.direction for r in env.gw.sent] == [Direction.LONG, Direction.SHORT]
    assert env.gw.sent[1].offset == Offset.CLOSE


def test_basic_trade_aborts_when_account_already_holds_a_position(env):
    env.gw.set_position(Direction.LONG, 1)
    bt.step_basic_trade(env.ctx, env.book)
    assert item(env, "T3").status == be.FAIL and "已中止开平仓演示" in item(env, "T3").summary
    assert env.gw.sent == []


def test_basic_trade_is_skipped_off_session(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        bt.step_basic_trade(e.ctx, e.book)
        assert {k: v.status for k, v in e.book.items.items()} == {"T3": be.SKIP, "T5": be.SKIP, "P1.2": be.SKIP}
        assert e.gw.sent == []
    finally:
        e.main.close()


def test_cancel_step_feeds_t4_and_the_p12_cancel_shot(env):
    env.gw.mode = "accept"
    bt.step_cancel(env.ctx, env.book)
    assert item(env, "T4").status == be.PASS and item(env, "P1.2").shots[0].label == "撤单"
    assert len(env.gw.cancel_requests) == 1


# ---------- P4.2 柜台错误 ----------

def test_counter_errors_bypass_local_risk_and_capture_broker_text(tmp_path):
    e = Env(tmp_path, in_session=False)
    try:
        e.gw.mode = "reject"
        e.gw.reject_text = "CTP:资金不足"
        bt.step_counter_errors(e.ctx, e.book)
        ev = e.book.items["P4.2"]
        assert [s.label for s in ev.shots] == ["无足够资金开仓", "无仓位可平", "市场状态不允许"]
        assert ev.status == be.PASS, ev.summary
        assert "资金不足" in ev.shots[1].lines[2]        # 柜台错误文本确实被采到了
        # 「无足够资金开仓」**故意不发**:要么柜台直接接受(真开大仓),要么跌停价以下触发硬拒绝
        # 把账户锁成当日禁止开仓(2026-11-16 夜盘实测:200 笔只放行 9 笔)。
        assert len(e.gw.sent) == 2                                              # 直发,没有经过本地风控拦截
        assert "专用小资金账户补测" in ev.summary
    finally:
        e.main.close()


def test_counter_errors_in_session_cannot_demo_market_state(env):
    env.gw.mode = "reject"
    env.gw.reject_text = "CTP:资金不足"
    bt.step_counter_errors(env.ctx, env.book)
    ev = item(env, "P4.2")
    # 「无仓位可平」这一项演示到了(真收到了柜台错误文本)→ 本项算通过;
    # 没能演示的两个场景按「需补测」记在说明里,不能当成失败,也不能当没这回事。
    assert ev.status == be.PASS, ev.summary
    assert "非交易时段补测" in ev.summary and "专用小资金账户补测" in ev.summary
    assert "非交易时段" in ev.shots[2].lines[0]


def test_counter_errors_fail_when_no_broker_text_arrives(env):
    """柜台没给任何错误文本 → 这一项就是没演示到,必须判失败(不能含糊过去)。"""
    env.gw.mode = "accept"
    bt.step_counter_errors(env.ctx, env.book)
    ev = item(env, "P4.2")
    assert ev.status == be.FAIL, ev.summary
    assert "未收到柜台错误文本" in ev.shots[1].lines[2]


# ---------- 其他步骤 ----------

def test_connection_step_is_manual_without_interactive(env):
    bt.step_connection(env.ctx, env.book)
    assert item(env, "P2.1").status == be.MANUAL


def test_batch_cancel_is_recorded_as_not_implemented(env):
    bt.step_batch_cancel(env.ctx, env.book)
    assert item(env, "P5.2").status == be.SKIP and "未实现" in item(env, "P5.2").shots[0].lines[0]


def test_logs_step_needs_all_four_categories(env, monkeypatch):
    monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["Send new order -> CTP", "交易服务器登录成功", "[WARN] 风控阈值触发:x", "交易委托失败,代码:21"])
    bt.step_logs(env.ctx, env.book)
    ev = item(env, "P6.1")
    assert ev.status == be.PASS and [s.label for s in ev.shots] == ["交易日志", "系统运行记录", "监测记录", "错误提示记录"]
    monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["交易服务器登录成功"])
    env.book.items.clear()
    bt.step_logs(env.ctx, env.book)
    assert item(env, "P6.1").status == be.FAIL and "监测记录" in item(env, "P6.1").summary


def test_exit_step_runs_the_runtime_exit_tests(env, monkeypatch):
    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout="12 passed in 3s\n".encode()))
    bt.step_exit(env.ctx, env.book)
    ev = item(env, "T7")
    assert ev.status == be.PASS and any("12 passed" in ln for ln in ev.shots[0].lines) and "代码级验证" in ev.shots[0].lines[0]
    monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=b"1 failed"))
    env.book.items.clear()
    bt.step_exit(env.ctx, env.book)
    assert item(env, "T7").status == be.FAIL


def test_login_step_reports_milestones_and_failures(tmp_path):
    e = Env(tmp_path)
    try:
        e.ctx.connect_timeout = 0.5
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        assert bt.step_login(e.ctx, e.book) is True
        assert e.book.items["T1"].status == be.PASS and e.book.items["P1.1"].status == be.PASS
        e.book.items.clear()
        e.collector.log_lines.clear()
        e.collector.log_lines.extend(["交易服务器连接成功", "交易服务器授权验证失败，代码：63，信息：x"])
        assert bt.step_login(e.ctx, e.book) is False
        assert e.book.items["T1"].status == be.FAIL and "代码：63" in e.book.items["T1"].summary
    finally:
        e.main.close()


def test_run_steps_stops_after_failed_login_and_runs_everything_otherwise(tmp_path, monkeypatch):
    e = Env(tmp_path)
    try:
        e.ctx.connect_timeout = 0.3
        assert bt.run_steps(e.ctx, e.book) is False                              # 没有里程碑日志 → 登录失败 → 后续不执行
        assert set(e.book.items) == {"T1", "P1.1"}
        e.book.items.clear()
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        monkeypatch.setattr(bt.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"ok"))
        monkeypatch.setattr(bt, "collect_log_lines", lambda ctx: ["Send new order", "登录成功", "[WARN] 风控阈值触发", "委托被拦截"])
        e.gw.mode = "fill"
        e.gw.position_lag = False
        assert bt.run_steps(e.ctx, e.book) is True
        assert set(e.book.items) == set(be.ITEMS)                               # 全部测试项都有结果
        assert e.book.items["P2.1"].status == be.MANUAL
    finally:
        e.main.close()


def test_run_steps_only_filter(tmp_path):
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        assert bt.run_steps(e.ctx, e.book, only={"P4.1"}) is True
        assert set(e.book.items) == {"T1", "P1.1", "P4.1"}
    finally:
        e.main.close()


# ---------- Book / 入口 ----------

def test_book_combines_statuses_and_shots():
    book = bt.Book()
    book.add("P1.2", be.PASS, "", [be.Shot("开仓", ["a"])], "t")
    book.add("P1.2", be.SKIP, "撤单需交易时段", [be.Shot("撤单", ["b"])], "t")
    ev = book.items["P1.2"]
    assert ev.status == be.SKIP and [s.label for s in ev.shots] == ["开仓", "撤单"] and "撤单需交易时段" in ev.summary
    book.add("P1.2", be.FAIL, "x", [], "t")
    assert book.items["P1.2"].status == be.FAIL


def test_main_refuses_without_confirmation_and_for_production(monkeypatch, capsys):
    env = {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "10.0.0.1:1",
           "CTP_MD_ADDRESS": "10.0.0.1:2", "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2", "LIVE_VT_SYMBOL": "c2701.DCE",
           "CTP_ENVIRONMENT": "测试"}
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert bt.main([]) == bt.EXIT_CONFIG and "--confirm-broker-test" in capsys.readouterr().err
    # 白名单为空 → 拒绝(「柜台环境=测试」不说明连的是哪台机器)
    monkeypatch.setenv("CTP_ENVIRONMENT", "实盘")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "白名单" in capsys.readouterr().err
    # 地址在可信表内、但柜台环境是「实盘」→ 生产 API,拒绝
    monkeypatch.setenv("CTP_TD_ADDRESS", "121.13.218.19:41285")
    monkeypatch.setenv("CTP_MD_ADDRESS", "121.13.218.19:41293")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_TD", "121.13.218.19:41285")
    monkeypatch.setenv("BROKER_TEST_ALLOWED_MD", "121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "生产 API" in capsys.readouterr().err
    monkeypatch.setenv("CTP_ENVIRONMENT", "测试")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    monkeypatch.delenv("LIVE_VT_SYMBOL")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG


def test_pause_flag_helper_is_shared_with_the_cli(tmp_path):
    import pause_trading
    flag = pause_trading.pause(tmp_path, "x", now=datetime(2026, 11, 11))
    assert flag.exists() and pause_trading.resume(tmp_path)


def _guoxin_env(monkeypatch, **over):
    env = {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030",
           "CTP_TD_ADDRESS": "121.13.218.19:41285", "CTP_MD_ADDRESS": "121.13.218.19:41293",
           "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2", "LIVE_VT_SYMBOL": "c2701.DCE",
           "CTP_ENVIRONMENT": "测试"}
    env.update(over)
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def test_market_data_front_must_be_whitelisted_too(monkeypatch, capsys):
    """行情前置喂回来的盘口价会直接变成委托价,只校验交易前置等于留了半个口子。"""
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="121.13.218.19:41285")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "行情前置" in err and "BROKER_TEST_ALLOWED_MD" in err


def test_whitelist_cannot_widen_beyond_the_trusted_addresses(monkeypatch, capsys):
    """白名单与地址同在 .env 里,采信它等于自己给自己发通行证。

    所以白名单只能**收窄**:不在可信地址表内的地址,哪怕同时写进
    CTP_TD_ADDRESS 和 BROKER_TEST_ALLOWED_TD,也不放行。
    """
    _guoxin_env(monkeypatch, CTP_TD_ADDRESS="10.0.0.1:41285",
                BROKER_TEST_ALLOWED_TD="10.0.0.1:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "可信地址表" in err and "已丢弃" in err and "拒绝连接" in err


def test_a_whitelist_outside_the_trusted_table_is_dropped_and_reported(monkeypatch, capsys):
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="10.0.0.1:41285,121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "10.0.0.1:41285" in err and "已丢弃" in err


def test_market_data_front_outside_the_whitelist_is_refused(monkeypatch, capsys):
    _guoxin_env(monkeypatch, CTP_MD_ADDRESS="8.8.8.8:1",
                BROKER_TEST_ALLOWED_TD="121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    assert "行情前置" in capsys.readouterr().err


def test_both_fronts_whitelisted_pass_the_gate(monkeypatch, capsys):
    """两个白名单都配好时,闸门放行(这里靠「未知测试项」提前收住,不会真的去连)。"""
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    assert bt.main(["--confirm-broker-test", "--only", "NOPE"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "未知测试项" in err and "行情前置" not in err


# ---------- 收尾兜底 ----------

def test_final_safety_cancels_leftover_orders_and_flags_positions(env):
    env.gw.mode = "accept"
    oid = bt.send(env.ctx, bt.make_req(env.ctx, price=2100.0))
    assert env.collector.order_of(oid).is_active()
    env.gw.set_position(Direction.LONG, 1)
    notes = bt.final_safety(env.ctx)
    assert not env.collector.order_of(oid).is_active()
    assert any("撤销了 1 笔遗留委托" in n for n in notes) and any("⚠⚠ 收尾发现账户有持仓" in n for n in notes)


def test_final_safety_is_quiet_when_clean_and_never_raises(env):
    assert bt.final_safety(env.ctx) == []
    env.ctx.main_engine = SimpleNamespace(get_all_active_orders=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "收尾检查出错" in bt.final_safety(env.ctx)[0]


def test_main_refuses_to_connect_when_state_isolation_did_not_take_effect(monkeypatch, capsys):
    monkeypatch.setattr(bt, "load_profile", lambda profile="", root=None: _profile_from_os())
    for k, v in {"CTP_USERNAME": "u", "CTP_PASSWORD": "pw_secret_1", "CTP_BROKER_ID": "8030", "CTP_TD_ADDRESS": "10.0.0.1:1",
                 "CTP_MD_ADDRESS": "10.0.0.1:2", "CTP_APP_ID": "client_x_1.0", "CTP_AUTH_CODE": "auth_secret_2",
                 "LIVE_VT_SYMBOL": "c2701.DCE", "CTP_ENVIRONMENT": "测试"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bt, "ISOLATED", False)
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG and "隔离未生效" in capsys.readouterr().err


def test_isolation_holds_when_run_as_a_fresh_script():
    import subprocess
    code = ("import sys; sys.path.insert(0, 'scripts'); import broker_test; print('ISOLATED=' + str(broker_test.ISOLATED))")
    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code], cwd=ROOT, capture_output=True, timeout=120)
    assert "ISOLATED=True" in proc.stdout.decode("utf-8", errors="replace")


# ---------- codex 第二轮审查补的用例 ----------

def test_probe_price_gives_each_probe_order_a_unique_price(env):
    prices = [bt.probe_price(env.ctx) for _ in range(5)]
    assert len(set(prices)) == 5                                       # 不同价格,重复报单计数不跨步骤污染
    tick = abs(prices[1] - prices[0])
    assert tick == env.ctx.contract.pricetick


def test_cancel_then_duplicate_orders_share_no_price_key(env):
    """真实运行顺序 T4 → P2.3:撤单演示的委托不能计入重复报单的同一价格键。"""
    env.gw.mode = "accept"
    bt.step_cancel(env.ctx, env.book)
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["ok"] and item(env, "P2.3").status == be.PASS, item(env, "P2.3").summary


def test_run_steps_only_order_items_with_a_position_gates_every_order_step(tmp_path):
    """--only P4.2(或 P5.1)也必须先过空仓闸门:有持仓时所有会发单的演示都记 FAIL,一笔都不发。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        e.gw.set_position(Direction.LONG, 1)
        assert bt.run_steps(e.ctx, e.book, only={"P4.2"}) is True
        assert e.gw.sent == []
        item = e.book.items.get("P4.2")
        assert item is not None and item.status == be.FAIL and "已有持仓" in item.summary
    finally:
        e.main.close()


def test_flat_gate_says_which_failure_it_was(tmp_path):
    """闸门失败必须说清是「有持仓」还是「查不到持仓」——两种原因的处理方式完全不同。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        e.gw.set_position(Direction.LONG, 1)
        assert bt.run_steps(e.ctx, e.book, only={"P4.2"}) is True
        summary = e.book.items["P4.2"].summary
        assert "已有持仓" in summary and "查不到持仓" not in summary      # 有持仓:不说查不到
        assert e.ctx.flat_reason.startswith("账户已有持仓")
    finally:
        e.main.close()

    e2 = Env(tmp_path / "b")
    try:
        e2.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        # 断线:vnpy_ctp 只清 login_status,connect_status 一直是 True
        # (ctp_gateway.py 的 onFrontDisconnected)。以前这里只改 connect_status,
        # 于是「断线」这个场景根本没被测到——闸门照样放行。
        e2.gw.td_api.login_status = False
        assert e2.gw.td_api.connect_status is True                      # 陷阱:连接标志不会变
        assert bt.run_steps(e2.ctx, e2.book, only={"P4.2"}) is True
        summary = e2.book.items["P4.2"].summary
        assert "查不到持仓" in summary and "已有持仓" not in summary
        assert e2.ctx.flat_reason.startswith("交易连接断开")
    finally:
        e2.main.close()


def test_pause_step_cleans_flag_and_orders_when_sending_raises(env, monkeypatch):
    def boom(ctx, req=None):
        raise RuntimeError("模拟发送中途崩溃")
    monkeypatch.setattr(bt, "send", boom)
    with pytest.raises(RuntimeError):
        bt.step_pause(env.ctx, env.book)
    assert not (env.state_file.parent / "pause_trading.flag").exists()   # finally 解除暂停标志


def test_book_redacts_summary_and_shot_lines_at_add_time():
    book = bt.Book(clean=lambda t: t.replace("pw_secret_1", "***"))
    book.add("P1.1", be.PASS, "密码 pw_secret_1 泄露了", [be.Shot("", ["auth pw_secret_1 ok"])], "t")
    ev = book.items["P1.1"]
    assert ev.summary == "密码 *** 泄露了" and ev.shots[0].lines == ["auth *** ok"]


# ---------- 连接环境留痕 ----------

SETTING = {"用户名": "user_secret_9", "密码": "pw_secret_1", "经纪商代码": "8030", "交易服务器": "10.0.0.1:41205",
           "行情服务器": "10.0.0.1:41213", "产品名称": "client_product_1.0", "授权编码": "auth_secret_2", "柜台环境": "测试"}


def test_connection_info_records_fronts_and_broker_but_no_secrets():
    info = bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0))
    assert info["trade_front"] == "10.0.0.1:41205" and info["market_front"] == "10.0.0.1:41213"
    assert info["broker_id"] == "8030" and info["environment"] == "测试" and info["at"] == "2026-11-12T21:00:00"
    dumped = str(info)
    for secret in ("user_secret_9", "pw_secret_1", "auth_secret_2", "client_product_1.0"):
        assert secret not in dumped
    assert info["app_id"] and info["app_id"] != SETTING["产品名称"]       # 只留掩码,足以区分不同产品名


def test_save_connection_info_appends_one_entry_per_run(tmp_path):
    be.save_connection_info(tmp_path, {"at": "a"})
    be.save_connection_info(tmp_path, {"at": "b"})                       # --resume 的多次运行都要留痕

    import json
    assert [r["at"] for r in json.loads((tmp_path / "connection.json").read_text(encoding="utf-8"))] == ["a", "b"]


def test_session_and_evidence_timestamps_use_beijing_time_not_host_local_time(monkeypatch):
    """宿主机可能是 UTC-8:北京周五 10:00 在本机是周四 18:00,按本地时间会把交易时段误判为休市。"""
    from datetime import datetime as real_datetime, timezone
    from risk import account_guard as ag
    real_now = real_datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)          # = 北京 2026-11-13 10:00 周五

    class Fake(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_now.astimezone(tz) if tz else real_now.astimezone(timezone.utc).replace(tzinfo=None) - __import__("datetime").timedelta(hours=8)

    monkeypatch.setattr(bt, "datetime", Fake)
    monkeypatch.setattr(ag, "datetime", Fake)      # 北京时间函数现在住在 risk.account_guard,补丁要打在它身上
    now = bt.beijing_now()
    assert (now.hour, now.weekday()) == (10, 4)
    assert bt.in_trading_session(now) is True


def test_the_evidence_runner_refuses_a_non_guoxin_target_without_the_rehearsal_flag(monkeypatch, capsys):
    """这个取证运行器出的是**国信**的报告:连着别的环境生成的证据,
    目录名和内容都长得像正式证据,事后很容易被当成国信环境的结果交上去。
    """
    _guoxin_env(monkeypatch,
                CTP_BROKER_ID="9999",
                CTP_ENVIRONMENT="实盘",
                CTP_TD_ADDRESS="182.254.243.31:30001",
                CTP_MD_ADDRESS="182.254.243.31:30011",
                BROKER_TEST_ALLOWED_TD="182.254.243.31:30001",
                BROKER_TEST_ALLOWED_MD="182.254.243.31:30011")
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "--rehearsal" in err, "非国信目标必须要求显式声明预演"


def test_the_rehearsal_flag_passes_the_gate_but_still_stops_before_connecting(monkeypatch, capsys):
    """显式声明预演后不再被那道闸门挡住;后面仍然要过别的闸门。

    这里用「状态目录隔离未生效」把它挡在连接之前 —— 测试绝不能真的去连柜台。
    """
    _guoxin_env(monkeypatch,
                CTP_BROKER_ID="9999",
                CTP_ENVIRONMENT="实盘",
                CTP_TD_ADDRESS="182.254.243.31:30001",
                CTP_MD_ADDRESS="182.254.243.31:30011",
                BROKER_TEST_ALLOWED_TD="182.254.243.31:30001",
                BROKER_TEST_ALLOWED_MD="182.254.243.31:30011")
    monkeypatch.setattr(bt, "ISOLATED", False)
    assert bt.main(["--confirm-broker-test", "--rehearsal"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "--rehearsal" not in err and "隔离" in err


def test_the_connection_record_states_which_environment_it_was(monkeypatch):
    """证据目录要能自证是在哪台机器上跑出来的,不能靠人记得。"""
    _guoxin_env(monkeypatch)
    guoxin = bt.connection_info({"经纪商代码": "8030", "柜台环境": "测试",
                                  "交易服务器": "121.13.218.19:41285", "行情服务器": "121.13.218.19:41293",
                                  "产品名称": "client_x_1.0"})
    assert guoxin["mode"] == "guoxin"
    rehearsal = bt.connection_info({"经纪商代码": "9999", "柜台环境": "实盘",
                                    "交易服务器": "182.254.243.31:30001", "行情服务器": "182.254.243.31:30011",
                                    "产品名称": "client_x_1.0"})
    assert rehearsal["mode"] == "rehearsal"


def test_connection_mode_follows_the_rehearsal_flag_even_for_the_guoxin_broker():
    """对 8030 跑预演也必须标成预演(mode 以 --rehearsal 为准,并与经纪商代码交叉校验)。"""
    assert bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0))["mode"] == "guoxin"
    assert bt.connection_info(SETTING, now=datetime(2026, 11, 12, 21, 0), rehearsal=True)["mode"] == "rehearsal"
    other = {**SETTING, "经纪商代码": "9999"}
    assert bt.connection_info(other, now=datetime(2026, 11, 12, 21, 0))["mode"] == "rehearsal"


def test_a_step_that_raises_does_not_discard_the_evidence_already_collected(tmp_path, monkeypatch):
    """任一步抛异常也要保住已采到的证据:这一步记 FAIL,后面的发单演示因状态不明而中止。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        e.gw.mode = "fill"
        e.gw.position_lag = False

        def boom(ctx, book):
            raise RuntimeError("柜台炸了 pw_secret_1")

        monkeypatch.setattr(bt, "step_basic_trade", boom)
        assert bt.run_steps(e.ctx, e.book, only={"T3", "P4.1"}) is True        # 不抛
        assert e.book.items["T3"].status == be.FAIL
        assert "RuntimeError" in e.book.items["T3"].summary
        assert "P4.1" in e.book.items                                         # 不发单的步骤照常出证据
    finally:
        e.main.close()


def test_a_crashed_step_makes_the_run_exit_with_the_crash_code(tmp_path, monkeypatch):
    """步骤抛异常 = 证据不完整:run_steps 记下失败步骤,main 据此返回崩溃码(一键流程不出报告)。"""
    e = Env(tmp_path)
    try:
        e.collector.log_lines.extend([text for _k, text in bt.MILESTONES])
        monkeypatch.setattr(bt, "in_trading_session", lambda dt: True)
        monkeypatch.setattr(bt, "step_validity", lambda ctx, book: (_ for _ in ()).throw(RuntimeError("x")))
        assert bt.run_steps(e.ctx, e.book, only={"P4.1"}) is True
        assert e.ctx.failed_steps == ["<lambda>"]
    finally:
        e.main.close()


def test_exit_code_separates_incomplete_evidence_from_failed_items():
    """一键流程只对 0/1 出报告:登录失败 3、步骤崩溃 4(证据不完整)、跑完有失败 1、全过 0。"""
    ok = be.Evidence("T1", be.PASS, "", "t")
    bad = be.Evidence("T2", be.FAIL, "x", "t")
    assert bt.exit_code_for(False, [], [], [ok]) == bt.EXIT_LOGIN
    assert bt.exit_code_for(True, ["step_x"], [], [ok]) == bt.EXIT_CRASH      # 即使其余项全过
    assert bt.exit_code_for(True, [], ["⚠⚠ 留有持仓"], [ok]) == bt.EXIT_FAILED
    assert bt.exit_code_for(True, [], [], [ok, bad]) == bt.EXIT_FAILED
    assert bt.exit_code_for(True, [], [], [ok]) == bt.EXIT_OK


# ---------- 账户锁定:不要白跑一次登录 ----------

def _guard_state(**over) -> dict:
    """一份**完整**的 AccountGuard 状态文件内容。

    必填字段缺一个,运行时的 `AccountGuard._load` 就判「文件损坏」→ 硬锁定
    (2026-11-16 codex 复核 H2:本地另写一套解析会把这判成「没锁」)。
    """
    state = {
        "account_id": "TESTACCT",   # 虚构占位:真实账号号不进版本库
        "last_equity": 10000.0,
        "peak_equity": 10000.0,
        "day": "2026-01-01",
        "day_start_equity": 10000.0,
        "daily_locked_day": None,
        "hard_locked": False,
        "hard_reason": "",
        "hard_locked_at": "",
        "unlock_history": [],
        "manual_orders": {},
    }
    state.update(over)
    return state


def test_locked_state_note_reads_the_isolated_guard_state(tmp_path):
    """锁定状态下开仓委托会被本地风控全部拦下,取证会白跑一次券商登录额度。"""
    from risk.account_guard import trading_day
    state = tmp_path / "guard.json"
    day = trading_day(bt.beijing_now()).isoformat()

    state.write_text(json.dumps(_guard_state(hard_locked=True, hard_reason="权益回撤")), encoding="utf-8")
    note = bt.locked_state_note(state)
    assert "硬锁定" in note and "权益回撤" in note

    state.write_text(json.dumps(_guard_state(daily_locked_day=day)), encoding="utf-8")
    note = bt.locked_state_note(state)
    assert "当日锁定" in note and day in note

    state.write_text(json.dumps(_guard_state(daily_locked_day="2000-01-01")), encoding="utf-8")
    assert bt.locked_state_note(state) == ""            # 旧交易日的锁定已经过期

    assert bt.locked_state_note(tmp_path / "missing.json") == ""   # 没有状态文件 = 从来没锁过


def test_locked_state_note_agrees_with_the_runtime_guard(tmp_path):
    """本地判「没锁」而运行时判「硬锁定」⇒ 白跑一次国信登录额度。

    2026-11-16 codex 复核 H2 复现:状态文件只写 `{"hard_locked": false}` 时,
    本地解析判「没锁」放行,而 `AccountGuard._load` 因缺必填字段直接硬锁定
    ⇒ 登录一次、委托全被拒、证据只剩一堆「被拦下」。
    两份判据必须同源:直接用 AccountGuard 读。
    """
    from risk.account_guard import AccountGuard, AccountLimits
    state = tmp_path / "guard.json"
    state.write_text(json.dumps({"hard_locked": False}), encoding="utf-8")

    guard = AccountGuard(AccountLimits(), state)
    assert guard.hard_locked is True                    # 运行时:文件损坏 → 硬锁定

    note = bt.locked_state_note(state)
    assert note != ""                                   # 本地也必须拒绝跑
    assert "硬锁定" in note or "损坏" in note


def test_locked_state_note_refuses_a_broken_state_file_instead_of_crashing(tmp_path):
    """坏文件(不是 JSON / 不是 JSON 对象 / 不是 UTF-8)⇒ 拒绝跑,并且**不抛异常**。

    抛异常的话,操作者拿到的是 traceback 而不是可执行的说明,退出码也会变成「崩溃」。
    """
    state = tmp_path / "guard.json"
    for raw in (b"not json at all", b"[]", b"42", b'"x"', b"\xff\xfe\x00broken"):
        state.write_bytes(raw)
        note = bt.locked_state_note(state)
        assert note != "", raw                           # 绝不能当成「没锁」放行



def test_main_refuses_to_connect_when_the_account_is_locked(monkeypatch, capsys, tmp_path):
    """账户被锁 → 直接拒绝连接,不消耗一次登录(国信会查登录次数)。"""
    _guoxin_env(monkeypatch, BROKER_TEST_ALLOWED_TD="121.13.218.19:41285",
                BROKER_TEST_ALLOWED_MD="121.13.218.19:41293")
    from risk.account_guard import trading_day
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "guard.json").write_text(
        json.dumps(_guard_state(daily_locked_day=trading_day(bt.beijing_now()).isoformat())),
        encoding="utf-8")
    monkeypatch.setattr(bt, "BT_ROOT", tmp_path)
    assert bt.main(["--confirm-broker-test"]) == bt.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "拒绝连接" in err and "当日锁定" in err


def test_stability_is_skipped_when_the_account_is_locked(env):
    """账户锁定时 200 笔一笔都发不出去,证据只会显示「放行 0 笔」——直接判成需补测。"""
    from risk.account_guard import trading_day
    env.rule.guard.daily_locked_day = trading_day(bt.beijing_now())
    env.ctx.stability_interval = 0
    bt.step_stability(env.ctx, env.book, total=5)
    ev = item(env, "P1.3")
    assert ev.status == be.SKIP and "DAILY_LOCKED" in ev.summary
    assert env.gw.sent == []


# ---------- 阈值对照:计数是异步更新的 ----------

def test_the_over_limit_order_is_only_sent_after_the_counter_advanced(env, monkeypatch):
    """报单计数在**委托回报事件**里异步更新。

    不等计数涨上去就发第二笔,第二笔会因为计数还停在旧值而被放行 ——
    2026-11-16 夜盘实测就是这样把「超过阈值的委托」放过去的。
    """
    trace: list[tuple] = []
    real_wait = bt.wait_counter_at_least
    real_send = bt.send

    def fake_wait(ctx, rule, attr, target, timeout):
        trace.append(("wait", attr, target))
        return real_wait(ctx, rule, attr, target, timeout)

    def fake_send(ctx, req):
        trace.append(("send",))
        return real_send(ctx, req)

    monkeypatch.setattr(bt, "wait_counter_at_least", fake_wait)
    monkeypatch.setattr(bt, "send", fake_send)
    dup = bt.step_duplicate(env.ctx, env.book)
    start = len(trace)                                  # 只看阈值对照这一步的次序
    bt.step_threshold(env.ctx, env.book, dup)

    kinds = [t[0] for t in trace[start:]]
    assert kinds.count("send") >= 2                    # 阈值内一笔 + 超阈值一笔
    assert kinds.count("wait") >= 1
    # 次序必须是「阈值内那笔 → **等计数推进** → 超阈值那笔」:
    # 计数没涨上去就发第二笔,第二笔会被放行(2026-11-16 夜盘实测)。
    first_send = kinds.index("send")
    second_send = kinds.index("send", first_send + 1)
    assert first_send < kinds.index("wait") < second_send


def test_wait_counter_at_least_reports_a_stalled_counter():
    """计数没推进时要如实返回 False —— 上面那句「对照不可靠」的告警才有依据。"""

    class _Rule:
        total_order_count = 0

    class _Ctx:
        def wait_until(self, predicate, timeout, step=0.2):
            for _ in range(3):
                if predicate():
                    return True
            return False

    rule = _Rule()
    assert bt.wait_counter_at_least(_Ctx(), rule, "total_order_count", 1, 5.0) is False
    assert bt.counter_value(rule, "total_order_count") == 0
    assert bt.counter_value(rule, "不存在的计数器") == 0


def test_settle_counter_gives_up_instead_of_hanging():
    """计数一直不稳定时,`settle_counter` 到点就返回 —— 不能把取证流程挂死。"""

    class _Jumpy:
        """每次读都变一个值:永远等不到「连续 1 秒不变」。"""

        def __init__(self) -> None:
            self._n = 0

        @property
        def total_order_count(self) -> int:
            self._n += 1
            return self._n

    class _Ctx:
        """假上下文:时钟每次 pump 前进 1 秒(计数一直跳 ⇒ 永远等不到「稳定 1 秒」)。"""

        def __init__(self) -> None:
            self.t = 0.0

        def clock(self) -> float:
            return self.t

        def pump(self) -> None:
            self.t += 1.0

        def sleep(self, _s: float) -> None:
            return None

    started = time.monotonic()
    value = bt.settle_counter(_Ctx(), _Jumpy(), "total_order_count", timeout=0.3)
    assert time.monotonic() - started < 5.0
    assert value >= 1


def test_the_position_warning_survives_another_metric_being_inconclusive(env, monkeypatch):
    """一个指标判不出来,不能把**另一个**指标的关键告警从摘要里挤掉。

    2026-11-16 codex 复核 M1:摘要原来只取**第一个**非空原因
    (`order_stalled or cancel_stalled`),于是报单指标判不出来时,撤单指标那条
    「演示单已成交 ⇒ 账户可能多出持仓」整条消失 —— 而摘要会进 `evidence.json`、终端输出,
    以及报告工具的「缺失项清单」(`broker_report.py` 渲染 `f"{ev.status}({ev.summary})"` 写进
    `outcome.missing`;真正进 docx 正文的是**证据行**,不是摘要)。
    持仓风险跟「为什么判不出来」无关,不能被它盖掉。
    """
    daily = env.risk.rules["每日上限检查"]
    real_send = bt.send

    def fake_send(ctx, req):
        daily.total_order_count += 1               # 迟到回报:报单指标的基数取旧 ⇒ 判不出来
        return real_send(ctx, req)

    env.gw.mode = "fill"                           # 撤单指标:演示单直接成交
    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "已成交" in p31.summary                  # 成交告警没被另一个指标挤掉
    assert "持仓" in p31.summary
    assert "撤单未确认" in p31.summary               # 两个指标的原因都在


def test_the_counter_comparison_is_never_a_false_inequality(env, monkeypatch):
    """摘要里的不等式必须**真的成立**:不能打出「计数 1 < 阈值 1」这种话。

    2026-11-16 codex 复核 M2:原先这句话无条件拼在「不是本项阈值拦的」后面,
    而那个计数是读到时的值、不是发单时的值,而且没校验不等式成立 ——
    这种句子会原样进国信的取证报告。
    """
    daily = env.risk.rules["每日上限检查"]
    real_send = bt.send

    def fake_send(ctx, req):
        oid = real_send(ctx, req)                  # 委托被「委托规模检查」按价值拦下
        daily.total_order_count += 1               # 计数被**别的**回报推到阈值上
        return oid

    # 用**别的规则**拦住委托(委托价值超限):风控日志里点名的是「委托价值」,
    # 不是我们这一项 ⇒ 归因走「不是本项阈值拦的」那一支,那里会带上计数对比。
    env.risk.rules["委托规模检查"].order_value_limit = 1.0
    env.ctx.in_session = False                     # 只留报单这一个指标,免得撤单指标搅局
    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert "不是本项阈值拦的" in p31.summary
    assert "读取计数 1(本项阈值 1)" in p31.summary     # 计数已经到阈值,不能写成 1 < 1
    assert "读取计数 1 < 本项阈值" not in p31.summary


def test_a_partially_filled_demo_order_points_at_both_the_position_and_the_counter(env):
    """部分成交的后果**有两个**:账户多出一部分持仓 + 余量可能还挂在柜台上。

    2026-11-16 codex 复核 M6:原先只有终态恰好是「全部成交」才提示持仓,
    部分成交只提示「可能仍挂在柜台上」—— 操作员会被引去柜台撤单,忘了核持仓。
    """
    env.gw.mode = "partial"
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "部分成交" in p31.summary
    assert "持仓" in p31.summary
    assert "柜台" in p31.summary


def test_a_hard_failure_also_carries_the_position_warning(env, monkeypatch):
    """**硬失败**(阈值确实没生效)的摘要里也必须带上「账户可能多出持仓」。

    2026-11-16 codex 复核 M1:持仓告警原先只在「判不出来」分支里补,硬失败分支反而漏掉 ——
    而恰恰是演示单可能已经成交的那一轮(撤单没赶上)会同时命中两个分支。
    摘要会进 `evidence.json`、终端输出与报告工具的「缺失项清单」,漏掉就是把真风险写没了。
    """
    env.gw.mode = "fill"                          # 演示单一报出去就成交 ⇒ 撤单来不及
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:                      # 超阈值那笔:假装风控没拦下 ⇒ 硬失败
            return "VT999999"
        return real_send(ctx, req)

    real_settle = bt.settle_counter

    def stale(ctx, rule, attr, timeout=5.0):
        return max(real_settle(ctx, rule, attr, timeout) - 2, 0)   # 故意报旧两笔 ⇒ 基数取旧

    monkeypatch.setattr(bt, "settle_counter", stale)
    dup = bt.step_duplicate(env.ctx, env.book)
    calls["n"] = 0
    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "阈值未生效" in p31.summary, p31.summary
    assert "已成交" in p31.summary, p31.summary
    assert "持仓" in p31.summary, p31.summary


def test_a_blocked_order_with_no_risk_log_falls_back_to_the_counter(env, monkeypatch):
    """压根没有风控拦截日志 ⇒ 最后才用计数兜底,措辞必须是「读取计数已到 N ≥ 本项阈值 M」。

    2026-11-16 codex 复核 M2:这个兜底分支以前写的是「发单时计数」—— 那个数是**读到**那一刻的,
    不是发单那一刻的,措辞对不上;而且不等式无条件写出,会打出「计数 1 < 阈值 1」这种假话。
    """
    daily = env.risk.rules["每日上限检查"]

    def fake_send(ctx, req):
        daily.total_order_count += 1           # 计数推到阈值,但没有任何拦截日志
        return None                             # 委托没发出去

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    lines = [line for s in p31.shots if s.label == "报单总笔数" for line in s.lines]
    assert any("读取计数已到" in line and "≥ 本项阈值" in line for line in lines), lines
    assert not any("< 本项阈值" in line for line in lines), lines


def test_the_t6_summary_does_not_blame_our_threshold_when_another_rule_blocked_it(env, monkeypatch):
    """超过阈值的那一笔若是**别的规则**拦的,T6 不能断言「阈值拦住了委托」。

    2026-11-16 codex 复核 M2:风控规则按顺序判定(活动委托检查在每日上限检查之前),
    委托规模检查也会拦 ⇒ 那笔单可能压根没走到本项阈值。
    旧文案无条件写「阈值拦住了委托但没有收到预警告警」—— 那是**没有证据的因果断言**,
    而且落在必测项 T6 上。归因只按证据:风控日志点名了我们这一项才算数。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_send(ctx, req)
        # 第二笔(超阈值那笔)被**别的规则**拦下:日志里没有点名「汇总委托笔数」
        ctx.collector.log_lines.append("委托被拦截，活动委托数量10达到上限10")
        return None

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    t6 = item(env, "T6")
    # 归因措辞对不对是一回事,判定**仍是 FAIL**(必测项没过就是没过)——两条都要断言,
    # 否则将来有人把 t6_status 改成 SKIP,这两条照样绿(2026-11-16 codex 复核 L7)。
    assert t6.status == be.FAIL, t6
    assert "阈值拦住了委托" not in t6.summary, t6.summary
    assert "被其它规则拦下" in t6.summary, t6.summary


def test_the_duplicate_order_metric_failure_gets_a_reason_in_the_summary(env):
    """重复报单这一项失败时,P3.1 的摘要里必须有**原因**。

    2026-11-16 codex 复核 M3:三个阈值指标里,重复报单原来只回一个布尔,
    失败时摘要退化成「有阈值未能触发拦截与预警」—— 一个原因都没有。
    """
    env.risk.rules.pop("重复报单检查", None)            # 规则缺失 ⇒ 这一项失败
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["ok"] is False
    assert dup["reason"], dup
    bt.step_threshold(env.ctx, env.book, dup)
    p31 = item(env, "P3.1")
    assert "重复报单" in p31.summary, p31.summary


def test_a_rejected_demo_order_is_not_reported_as_still_hanging(env, monkeypatch):
    """废单(被柜台拒单)没有挂单,不能说「可能仍挂在柜台上」。

    2026-11-16 codex 复核 L1:未知终态原先一律落进「可能仍挂着」——
    对一张**被拒单**的演示单,那句话是事实错误(它压根没上柜台),
    而真正的事实(废单)一个字都没留。
    """
    from vnpy.trader.constant import Status

    env.gw.cancel_works = False                    # 撤单请求发出去但撤不掉 ⇒ 终态留在废单上
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        oid = real_send(ctx, req)
        # 撤单指标的第一笔(第 3 次发单)被柜台拒单:模拟一个**终态是废单**的演示单
        if calls["n"] == 3 and oid:
            order = ctx.collector.order_of(oid)
            if order is not None:
                order.status = Status.REJECTED
        return oid

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    p31 = item(env, "P3.1")
    assert "拒单" in p31.summary or "废单" in p31.summary, p31.summary
    assert "仍挂在柜台上" not in p31.summary, p31.summary


def test_the_connection_info_timestamp_is_beijing_time():
    """证据目录里的时间戳一律北京时间:条目时间与连接记录时间差 16 小时,事后对时间线会打架。

    2026-11-16 codex 复核 L3:`connection_info` 的 `at` 原先写的是本机本地时间。
    """
    from risk import account_guard as ag

    info = bt.connection_info(SETTING)
    stamp = datetime.fromisoformat(info["at"])
    assert stamp.tzinfo is None
    assert abs((stamp - ag.beijing_now()).total_seconds()) < 120, info["at"]


def test_the_t6_summary_says_our_threshold_blocked_it_when_the_log_names_it(env, monkeypatch):
    """风控日志**点名了**本项指标 ⇒ 摘要可以说「本项阈值拦住了委托」。

    与上一条对照:归因要有证据才下结论 —— 点名了才算数,没点名只说「被其它规则拦下」。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:
            # 超阈值那笔:风控日志点名了「汇总委托笔数」,但没有触发预警
            ctx.collector.log_lines.append("委托被拦截，汇总委托笔数1达到上限1")
            return None
        return real_send(ctx, req)

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    t6 = item(env, "T6")
    # 判定必须仍是 FAIL:归因措辞与判定是两件事,都要钉住(2026-11-16 codex 复核 L7)。
    assert t6.status == be.FAIL, t6
    assert "本项阈值拦住了委托" in t6.summary, t6.summary
    p31 = item(env, "P3.1")
    assert "报单:" in p31.summary, p31.summary


def test_a_hard_failure_also_reports_the_duplicate_order_reason(env, monkeypatch):
    """硬失败分支里**也要**带上重复报单的失败原因。

    三个指标各有各的结论:硬失败(报单阈值失效)不能把「重复报单这项为什么没过」
    整条吞掉 —— 摘要会被读成「只有报单这一项有问题」,运维会去错的地方查。
    """
    env.risk.rules.pop("重复报单检查", None)          # 重复报单这项失败(找不到规则)
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:                          # 超阈值那笔:假装风控没拦下 ⇒ 硬失败
            return "VT999999"
        return real_send(ctx, req)

    real_settle = bt.settle_counter

    def stale(ctx, rule, attr, timeout=5.0):
        return max(real_settle(ctx, rule, attr, timeout) - 2, 0)   # 故意报旧两笔 ⇒ 基数取旧

    monkeypatch.setattr(bt, "settle_counter", stale)
    dup = bt.step_duplicate(env.ctx, env.book)
    calls["n"] = 0
    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "阈值未生效" in p31.summary, p31.summary
    assert "重复报单:" in p31.summary, p31.summary


def test_an_unreliable_threshold_also_reports_the_duplicate_order_reason(env, monkeypatch):
    """「判不出来(SKIP)」分支里**也要**带上重复报单的失败原因。"""
    env.risk.rules.pop("重复报单检查", None)          # 重复报单这项失败
    env.gw.mode = "fill"                             # 演示单一报出去就成交 ⇒ 撤单来不及
    dup = bt.step_duplicate(env.ctx, env.book)
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert p31.status == be.SKIP, p31.summary
    assert "重复报单:" in p31.summary, p31.summary


def test_the_cancel_threshold_failure_says_which_metric_failed(env, monkeypatch):
    """撤单这项没过时,摘要必须点名是**撤单**那一项(而不是笼统一句)。"""
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 4:                          # 撤单指标的超阈值那笔:被**别的规则**拦下
            ctx.collector.log_lines.append("委托被拦截，委托价值20900.0超过上限")
            return None
        return real_send(ctx, req)

    monkeypatch.setattr(bt, "send", fake_send)
    dup = bt.step_duplicate(env.ctx, env.book)
    calls["n"] = 0
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert p31.status == be.FAIL, p31.summary
    assert "撤单:" in p31.summary, p31.summary


def test_the_duplicate_order_metric_says_when_it_was_not_blocked_at_all(env, monkeypatch):
    """重复报单这项**压根没被拦**时,摘要必须这么写(不是笼统一句「没触发拦截或预警」)。"""
    rule = env.risk.rules["重复报单检查"]
    monkeypatch.setattr(rule, "check_allowed", lambda *a, **k: True)   # 规则不再拦任何委托
    dup = bt.step_duplicate(env.ctx, env.book)
    assert not dup["ok"]
    assert dup["reason"] == "没有被拦截", dup["reason"]   # 不带前缀:前缀由 P3.1 统一补
    assert item(env, "P2.3").status == be.FAIL


def test_the_duplicate_order_metric_says_when_it_was_blocked_without_an_alert(env, monkeypatch):
    """被拦了但**没收到预警**是另一种结论,必须与「压根没被拦」分开写。"""
    monkeypatch.setattr(env.rule, "_alert", lambda *a, **k: None)     # 预警通道静默,拦截照旧
    dup = bt.step_duplicate(env.ctx, env.book)
    assert not dup["ok"]
    assert dup["reason"] == "被拦截但没有收到预警", dup["reason"]   # 同上
    assert item(env, "P2.3").status == be.FAIL


def test_a_late_risk_log_does_not_make_the_summary_and_the_evidence_row_disagree(env, monkeypatch):
    """日志在**等待窗口之外**才到时,摘要与证据行必须**说同一件事**。

    2026-11-16 codex 第八轮复核 L2/L3:归因读一次日志、证据行再读一次,
    日志正好在两次之间到达 ⇒ 摘要写「没有风控拦截日志」、证据行里却摆着那条日志
    ⇒ 报告自相矛盾。现在归因与证据行用**同一份快照**(等完之后只读一次)。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:
            return None                                   # 超阈值那笔被拦下
        return real_send(ctx, req)

    real_wait = env.ctx.wait_until

    def wait_with_late_log(predicate, timeout, step=0.2):
        # 预警等待窗口是 4 秒(与归因等待的 attrib_wait 不同)⇒ 用它当「迟到的时刻」锚点
        if timeout == 4.0:
            env.collector.log_lines.append("委托被拦截，汇总委托笔数1达到上限1")
        return real_wait(predicate, timeout, step)

    monkeypatch.setattr(bt, "send", fake_send)
    monkeypatch.setattr(env.ctx, "wait_until", wait_with_late_log)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    t6 = item(env, "T6")
    quoted_log = [ln for shot in t6.shots for ln in shot.lines if "委托被拦截" in ln]
    says_no_log = "没有风控拦截日志" in t6.summary
    # 摘要说有日志,证据行就得有;摘要说没有,证据行就不能有 —— 两边必须一致
    assert bool(quoted_log) != says_no_log, (t6.summary, quoted_log)


def test_a_log_that_arrives_between_the_attribution_and_the_snapshot_is_still_used(env, monkeypatch):
    """归因读完**之后**、取快照**之前**才到的风控日志,也要被算进归因。

    2026-11-16 codex 第八轮复核 L2/L3:风控日志是**异步**投递的,可能正好落在这
    两次读之间 —— 生产里这一小段窗口真实存在。落在这里的日志原先被忽略 ⇒
    摘要写「没有风控拦截日志」、而证据行里其实有那条日志(自相矛盾)。
    现在补记它,并且**按同一份证据给出同一个判定**(日志晚到只影响什么时候看到,
    不影响「是谁拦的」)。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 1:
            return None                      # 「对照用的那一笔」没发出去,而且**当时没有**拦截日志
        return real_send(ctx, req)

    real_wait = env.ctx.wait_until
    real_logs_since = env.ctx.logs_since
    state = {"waited": False, "appended": False}

    def wait_marking(predicate, timeout, step=0.2):
        result = real_wait(predicate, timeout, step)
        state["waited"] = True               # 归因的等待窗口已经结束
        return result

    def logs_since_with_late(mark, *needles):
        result = real_logs_since(mark, *needles)
        if state["waited"] and not state["appended"] and needles:
            # 日志正好落在这两次读之间:归因那次读**看不到**它,快照那次看得到
            state["appended"] = True
            env.collector.log_lines.append("委托被拦截，汇总委托笔数1达到上限1")
        return result

    monkeypatch.setattr(bt, "send", fake_send)
    monkeypatch.setattr(env.ctx, "wait_until", wait_marking)
    monkeypatch.setattr(env.ctx, "logs_since", logs_since_with_late)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})

    p31 = item(env, "P3.1")
    late = [ln for shot in p31.shots for ln in shot.lines if "日志在等待窗口之后才到" in ln]
    assert late, p31.shots
    assert "没有风控拦截日志" not in p31.summary, p31.summary
    # 日志点名了**我们这一项** ⇒ 与「日志准时到达」时同一个判定(判不出来,不是失败)
    assert p31.status == be.SKIP, (p31.status, p31.summary)


def test_a_late_log_naming_another_rule_still_counts_as_a_failure(env, monkeypatch):
    """迟到的日志点名的是**别的规则**时,判定必须与「日志准时到达」时一样是失败。

    2026-11-16 codex 第八轮复核 L2/L3:同一份证据(别的规则拦的)必须给出同一个判定,
    否则同样的输入跑两次会得到两个结论,报告没法复核。
    """
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_send(ctx, req)

    real_wait = env.ctx.wait_until
    real_logs_since = env.ctx.logs_since
    state = {"waited": False, "appended": False}

    def wait_marking(predicate, timeout, step=0.2):
        result = real_wait(predicate, timeout, step)
        state["waited"] = True
        return result

    def logs_since_with_late(mark, *needles):
        result = real_logs_since(mark, *needles)
        if state["waited"] and not state["appended"] and needles:
            state["appended"] = True
            # 迟到的是**别的规则**的拦截日志(不是本项阈值)
            env.collector.log_lines.append("委托被拦截，委托价值20900.0超过上限")
        return result

    monkeypatch.setattr(bt, "send", fake_send)
    monkeypatch.setattr(env.ctx, "wait_until", wait_marking)
    monkeypatch.setattr(env.ctx, "logs_since", logs_since_with_late)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})

    p31 = item(env, "P3.1")
    late = [ln for shot in p31.shots for ln in shot.lines if "日志在等待窗口之后才到" in ln]
    assert late, p31.shots
    assert "不是本项阈值拦的" in p31.summary, p31.summary
    assert p31.status == be.FAIL, (p31.status, p31.summary)


def test_the_hanging_warning_survives_a_stale_base_verdict(env, monkeypatch):
    """「撤单没确认」这条**结构化标记**不能因为先命中别的分支而丢掉。

    2026-11-16 codex 第八轮复核 L6:标记原先只在 elif 链的那一支置位 ⇒
    先命中「计数基数取旧」时,「演示单可能仍挂在柜台上」这条告警会整条消失 ——
    而演示单**确实**发出去了、撤单也**确实**没确认,它就真的可能还挂在柜台上,
    操作员必须去柜台撤(这个动作不能因为「另一项判不出来」而消失)。
    """
    dup = bt.step_duplicate(env.ctx, env.book)

    daily = env.risk.rules["每日上限检查"]
    real_check = daily.check_allowed
    calls = {"n": 0}

    def counting_check(req, gateway_name):
        calls["n"] += 1
        if calls["n"] == 3:        # 撤单指标的「阈值内」那一笔:它**发出去了**,只是基数取旧
            return True
        return real_check(req, gateway_name)

    real_settle = bt.settle_counter

    def settle_with_late_cancel(ctx, rule, attr, timeout=5.0):
        base = real_settle(ctx, rule, attr, timeout)
        if attr == "total_cancel_count":
            # 迟到的撤单回报把计数推上去 ⇒ 设完阈值时计数已经到阈值 ⇒ 基数取旧
            rule.total_cancel_count = max(rule.total_cancel_count, base + 1)
        return base

    monkeypatch.setattr(daily, "check_allowed", counting_check)
    monkeypatch.setattr(bt, "settle_counter", settle_with_late_cancel)
    env.gw.cancel_works = False                    # 撤单撤不掉 ⇒ 撤单没确认
    bt.step_threshold(env.ctx, env.book, dup)

    p31 = item(env, "P3.1")
    assert "计数基数取旧" in p31.summary, p31.summary
    assert "演示单可能仍挂在柜台上" in p31.summary, p31.summary


def test_the_duplicate_order_step_keeps_its_own_wording_and_returns_a_short_reason(env, monkeypatch):
    """P2.3 的措辞是完整短语;回给 P3.1 的原因**不带前缀**(前缀由 P3.1 统一补)。

    2026-11-16 codex 第八轮复核 L4:上一轮把原因改成「找不到规则」后,
    P2.3 的摘要变成了不自然的「重复报单找不到规则」,文档也跟着漂移。
    """
    env.risk.rules.pop("重复报单检查", None)              # 重复报单这项失败(找不到规则)
    dup = bt.step_duplicate(env.ctx, env.book)
    assert dup["reason"] == "找不到规则", dup["reason"]
    assert item(env, "P2.3").summary == "找不到重复报单规则", item(env, "P2.3").summary


def test_the_t6_summary_does_not_blame_our_threshold_when_there_is_no_log_at_all(env, monkeypatch):
    """超阈值那笔没发出去、**又没有风控拦截日志** ⇒ 判不出是谁拦的,不能赖到本项阈值头上。"""
    real_send = bt.send
    calls = {"n": 0}

    def fake_send(ctx, req):
        calls["n"] += 1
        if calls["n"] == 2:
            return None                                   # 发出去了但没有任何风控日志
        return real_send(ctx, req)

    monkeypatch.setattr(bt, "send", fake_send)
    bt.step_threshold(env.ctx, env.book, {"ok": True, "lines": []})
    t6 = item(env, "T6")
    assert t6.status == be.FAIL, t6.summary
    assert "判不出是不是本项阈值拦的" in t6.summary, t6.summary
