"""scripts/drill.py 的纯逻辑测试(不联网、不连 CTP)。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

import drill as d  # noqa: E402
from simnow_guard import SIMNOW_TD_ADDRESSES, check_simnow_address, is_simnow_address  # noqa: E402


class _NullNotifier:
    def __init__(self):
        self.closed = False

    def send(self, level, title, message, extra=None):
        return True

    def close(self, drain_timeout=10.0):
        self.closed = True


def test_recording_notifier_records_every_alert():
    rec = d.RecordingNotifier(_NullNotifier())
    rec.send("ERROR", "当日熔断", "亏了 700")
    rec.send("WARN", "行情停更", "90s 没有 Tick")
    assert len(rec.alerts) == 2
    assert rec.find("熔断")[0]["title"] == "当日熔断"
    assert rec.find("不存在的关键字") == []


def test_recording_notifier_waits_for_an_alert():
    import threading
    rec = d.RecordingNotifier(_NullNotifier())

    def later():
        import time
        time.sleep(0.2)
        rec.send("WARN", "行情停更", "x")

    threading.Thread(target=later, daemon=True).start()
    alert = rec.wait_for("行情停更", timeout=3.0)
    assert alert is not None and alert["title"] == "行情停更"


def test_recording_notifier_gives_up_after_the_timeout():
    rec = d.RecordingNotifier(_NullNotifier())
    assert rec.wait_for("永远不会出现的标题", timeout=0.3) is None


def test_recording_notifier_passes_through_to_the_real_notifier():
    class _Inner:
        def __init__(self):
            self.sent = []

        def send(self, level, title, message, extra=None):
            self.sent.append(title)
            return True

        def close(self, drain_timeout=10.0):
            pass

    inner = _Inner()
    rec = d.RecordingNotifier(inner)
    rec.send("FATAL", "硬锁定", "x")
    assert inner.sent == ["硬锁定"]


def test_the_recording_notifier_hands_the_drain_budget_to_the_real_notifier():
    """排空预算要原样传下去 —— 演练的收尾全靠它,短了 FATAL 告警会被丢掉。"""
    class _Inner:
        def __init__(self):
            self.budget = None

        def send(self, level, title, message, extra=None):
            return True

        def close(self, drain_timeout=10.0):
            self.budget = drain_timeout

    inner = _Inner()
    rec = d.RecordingNotifier(inner)
    rec.close(drain_timeout=42.0)
    assert inner.budget == 42.0


def test_results_are_rendered_as_a_table():
    results = [d.DrillResult("latency", True, "延迟 0.12 秒", {"成交→持仓回报延迟(秒)": 0.12}),
               d.DrillResult("disconnect", False, "没有告警", {})]
    text = d.render_results(results)
    assert "| 场景 | 结论 | 明细 |" in text
    assert "✅" in text and "❌" in text
    assert "0.12" in text


def test_a_skipped_scenario_is_marked_as_skipped_not_as_a_failure():
    """前提不满足时跳过,不能记成失败,否则会让人误以为系统有问题。"""
    results = [d.DrillResult("overnight", True, "跳过:不在夜盘时段", {}, skipped=True)]
    text = d.render_results(results)
    assert "⏭" in text and "✅" not in text


def test_the_overnight_scenario_needs_the_night_session():
    ok, why = d.scenario_precondition("overnight", datetime(2026, 11, 10, 15, 30))
    assert ok is False
    assert "夜盘" in why
    ok, _ = d.scenario_precondition("overnight", datetime(2026, 11, 10, 21, 5))
    assert ok is True


def test_the_tick_stale_scenario_needs_a_trading_session():
    """看门狗只在交易时段检查行情停更,收盘后跑必然等不到告警。"""
    ok, why = d.scenario_precondition("tick-stale", datetime(2026, 11, 10, 16, 0))
    assert ok is False
    assert "交易时段" in why
    ok, _ = d.scenario_precondition("tick-stale", datetime(2026, 11, 10, 21, 5))
    assert ok is True


def test_the_risk_scenarios_have_no_time_precondition():
    """熔断/硬锁定是本地链路,任何时候都能测。"""
    for name in ("daily-loss", "hard-lock"):
        ok, _ = d.scenario_precondition(name, datetime(2026, 11, 10, 16, 0))
        assert ok is True, name


def test_write_results_keeps_the_manual_parts(tmp_path):
    out = tmp_path / "drills.md"
    out.write_text("# 标题\n\n人工结论\n\n" + d.BEGIN_MARK + "\n旧结果\n" + d.END_MARK + "\n\n结尾\n",
                   encoding="utf-8")
    d.write_results(out, [d.DrillResult("latency", True, "ok", {})], "2026-11-10 21:00:00")
    text = out.read_text(encoding="utf-8")
    assert "人工结论" in text and "结尾" in text
    assert "latency" in text and "旧结果" not in text


def test_write_results_creates_the_file_when_missing(tmp_path):
    out = tmp_path / "drills.md"
    d.write_results(out, [d.DrillResult("latency", True, "ok", {})], "2026-11-10 21:00:00")
    text = out.read_text(encoding="utf-8")
    assert d.BEGIN_MARK in text and "latency" in text


def test_write_results_can_be_run_twice_and_still_updates_the_main_file(tmp_path):
    """标记必须每次都写回去。

    第一次重写时如果只写正文、不写回 END_MARK,第二次运行 old.find(END_MARK) 会返回 -1
    → 主演练记录不再更新,只写 -raw.md —— 演练审计记录会停在旧结果上,还不报错。
    """
    out = tmp_path / "drills.md"
    d.write_results(out, [d.DrillResult("latency", True, "第一轮", {})], "2026-11-10 21:00:00")
    assert d.END_MARK in out.read_text(encoding="utf-8")
    d.write_results(out, [d.DrillResult("latency", True, "第二轮", {})], "2026-11-10 22:00:00")
    text = out.read_text(encoding="utf-8")
    assert "第二轮" in text and "第一轮" not in text
    assert d.BEGIN_MARK in text and d.END_MARK in text
    assert not (tmp_path / "drills-raw.md").exists()


def test_the_escape_hatch_warning_is_kept_in_the_result_file(tmp_path):
    """用了逃生舱(--allow-any-address)必须留在结果文件里,不能只打在终端上。"""
    out = tmp_path / "drills.md"
    d.write_results(out, [d.DrillResult("latency", True, "ok", {})], "2026-11-10 21:00:00",
                    warnings=["目标交易服务器 123.45.67.89:10130 **不在**已知的 SimNow 地址表内。"])
    text = out.read_text(encoding="utf-8")
    assert "⚠" in text and "123.45.67.89:10130" in text


def test_the_result_file_has_no_warning_line_when_nothing_went_wrong(tmp_path):
    out = tmp_path / "drills.md"
    d.write_results(out, [d.DrillResult("latency", True, "ok", {})], "2026-11-10 21:00:00")
    assert "⚠" not in out.read_text(encoding="utf-8")


def test_the_flush_timeout_covers_the_retries_and_the_backoff():
    """close() 至少要等这么久,后台线程才可能把队列里的消息发完。

    默认的 10 秒短于最坏情况(超时 × 重试次数 + 退避),hard-lock 的 FATAL 会随进程退出丢掉。
    """
    from notify.webhook import WebhookNotifier

    inner = WebhookNotifier("http://127.0.0.1:1/hook", timeout=8.0, retry_delays=(2.0, 6.0))
    notifier = d.RecordingNotifier(inner)
    assert d._drain_budget(notifier) >= 8.0 * 3 + 2.0 + 6.0


def test_the_flush_timeout_falls_back_when_the_notifier_has_no_estimate():
    """通知器不给估算值时也要有一个够用的兜底,不能是 0(那等于不等待)。"""

    class _Bare:
        def send(self, level, title, message, extra=None):
            return True

        def close(self, drain_timeout=10.0):
            pass

    assert d._drain_budget(_Bare()) >= 30.0


def test_the_drill_state_is_isolated_from_the_live_state():
    """演练的风控状态必须写在演练目录里,不能污染实盘的 account_guard.json。"""
    assert d.DRILL_ROOT != (ROOT / "data" / "state")
    assert "drill" in str(d.DRILL_ROOT)


def test_the_drill_logs_and_strategy_data_do_not_land_in_the_live_directory():
    """vn.py 4.5 没有 SETTINGS["data_path"];真正决定落盘位置的是 TEMP_DIR。

    演练的日志写进实盘的 .vntrader/log 会直接污染"无人值守每日证据表"
    (演练的"已启动"会被当成实盘的启动次数),所以这条必须守住。
    """
    import vnpy.trader.utility as utility

    assert utility.TEMP_DIR == d.DRILL_ROOT, "TEMP_DIR 没被改到演练目录"
    # 日志目录:vnpy.trader.logger 在导入时就算死了路径
    assert utility.get_folder_path("log") == d.DRILL_ROOT / "log"
    # CTA 策略数据文件:load_json/save_json 走 get_file_path
    assert utility.get_file_path("cta_strategy_data.json") == d.DRILL_ROOT / "cta_strategy_data.json"
    # 数据库:vnpy_sqlite 在导入时就把路径算死了
    import vnpy_sqlite.sqlite_database as sqlite_db

    assert str(sqlite_db.path).startswith(str(d.DRILL_ROOT)), sqlite_db.path


def test_the_live_directory_is_not_the_drill_directory():
    """演练目录绝不能就是实盘目录。"""
    assert d.DRILL_ROOT != ROOT / ".vntrader"
    assert "drill" in d.DRILL_ROOT.name


def test_noop_strategy_never_trades():
    """演练用空策略:不能自己下单,否则会干扰演练动作。"""
    from datetime import datetime

    from vnpy.trader.constant import Exchange
    from vnpy.trader.object import TickData
    strategy = d.NoopStrategy.__new__(d.NoopStrategy)
    tick = TickData(symbol="c2701", exchange=Exchange.DCE, gateway_name="CTP", datetime=datetime.now())
    assert strategy.on_tick(tick) is None
    assert strategy.state_consistent() is True


def test_simnow_address_allowlist_accepts_the_known_addresses():
    for address in SIMNOW_TD_ADDRESSES:
        allowed, _ = check_simnow_address(address)
        assert allowed is True
        assert is_simnow_address(address) is True


def test_simnow_address_allowlist_refuses_a_production_broker():
    """期货公司生产 API 的地址不在表里 → 必须拒绝连接。"""
    allowed, note = check_simnow_address("123.45.67.89:10130")
    assert allowed is False
    assert "真实资金账户" in note or "拒绝连接" in note


def test_simnow_address_allowlist_has_an_escape_hatch_that_warns():
    allowed, note = check_simnow_address("123.45.67.89:10130", allow_any=True)
    assert allowed is True
    assert "危险" in note and "真实资金账户" in note


def test_the_address_is_normalized_before_the_allowlist_check():
    """配置里写成 tcp://host:port 或带空格时,不该因为"长得不一样"就被判成非 SimNow。"""
    from simnow_guard import normalize_address

    assert normalize_address("tcp://182.254.243.31:30001") == "182.254.243.31:30001"
    assert normalize_address("  182.254.243.31:30001  ") == "182.254.243.31:30001"
    assert normalize_address("tcp://182.254.243.31:30001/ctp") == "182.254.243.31:30001"
    assert normalize_address("") == ""
    allowed, _ = check_simnow_address("tcp://182.254.243.31:30001")
    assert allowed is True


def test_the_unescaped_addresses_are_not_in_the_allowlist():
    """第二套只公布了 40001;按第一套的编号规律外推出的 40002/40003 没有官方背书。"""
    from simnow_guard import SIMNOW_TD_ADDRESSES

    assert "182.254.243.31:40002" not in SIMNOW_TD_ADDRESSES
    assert "182.254.243.31:40003" not in SIMNOW_TD_ADDRESSES


def test_the_scenarios_that_place_real_orders_are_listed():
    """latency 开平仓;daily-loss / hard-lock 触发运行时撤单与强平 —— 都会发出委托。"""
    assert d.REAL_ORDER_SCENARIOS == frozenset({"latency", "daily-loss", "hard-lock"})


def test_a_real_order_scenario_on_an_unknown_address_needs_an_extra_confirmation():
    """地址不在白名单 + 会真的下单 → 必须再加一道开关。"""
    assert d.risky_scenarios(["latency"], "123.45.67.89:10130") == ["latency"]
    assert d.risky_scenarios(["daily-loss", "hard-lock"], "123.45.67.89:10130") == ["daily-loss", "hard-lock"]


def test_a_simnow_address_needs_no_extra_confirmation():
    for scenario in d.REAL_ORDER_SCENARIOS:
        assert d.risky_scenarios([scenario], "182.254.243.31:30001") == []


def test_a_read_only_scenario_on_an_unknown_address_needs_no_extra_confirmation():
    """tick-stale / disconnect / overnight 不会发委托,不需要第二道开关。"""
    assert d.risky_scenarios(["tick-stale", "disconnect", "overnight"], "123.45.67.89:10130") == []


# ---------------- 场景(用假运行时,不联网) ----------------

import argparse  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402

from vnpy.trader.constant import Direction, Exchange, Offset  # noqa: E402


def _args(**kw):
    base = dict(tick_timeout=0.5, fill_timeout=0.5, close_timeout=1.0, tick_stale_seconds=60.0,
                stale_timeout=0.5, reconnect_timeout=0.5, overnight_timeout=0.5,
                connect_timeout=1.0, disconnect_timeout=0.5, lock_timeout=0.5)
    base.update(kw)
    return argparse.Namespace(**base)


def _Tick(symbol="c2701.DCE", ask=2400.0, bid=2399.0, last=2400.0):
    """用真实的 vn.py TickData 构造:假对象曾使用不存在的 ask_price 字段,让 drill.py 的同款错误通过了全部单测。"""
    from vnpy.trader.object import TickData
    code, exchange = symbol.split(".")
    return TickData(gateway_name="CTP", symbol=code, exchange=Exchange(exchange), datetime=datetime(2026, 11, 10, 21, 0, 1),
                    last_price=last, ask_price_1=ask, bid_price_1=bid)


def test_real_tick_data_has_level_one_prices_but_no_bare_ask_price():
    tick = _Tick()
    assert tick.ask_price_1 == 2400.0 and tick.bid_price_1 == 2399.0
    assert not hasattr(tick, "ask_price") and not hasattr(tick, "bid_price")     # 防止有人再把假字段加回来


class _Trade:
    datetime = datetime(2026, 11, 10, 21, 0, 2)


class _Pos:
    def __init__(self, symbol="c2701.DCE", volume=1):
        self.vt_symbol = symbol
        self.volume = volume
        self.direction = Direction.LONG


class _Contract:
    pricetick = 1.0
    exchange = Exchange.DCE


class _FakeMain:
    """模拟主引擎:开仓单 → 成交 + 持仓回报;平仓单 → 持仓清零。"""

    def __init__(self, drill):
        self.drill = drill
        self.orders = []

    def get_contract(self, symbol):
        return _Contract()

    def get_all_positions(self):
        return self.drill.position_state

    def send_order(self, req, gateway_name):
        self.orders.append(req)
        if req.offset == Offset.OPEN:
            self.drill.trades.append((time.monotonic(), _Trade()))
            self.drill.positions.append((time.monotonic() + 0.01, _Pos()))
            self.drill.position_state = [_Pos()]
        else:
            self.drill.position_state = []
        return "VT.1"


class _FakeApi:
    """假的 CTP API:close() 时回调 on_close(模拟断开瞬间系统发出的告警)。"""

    def __init__(self):
        self.closed = False
        self.on_close = None

    def close(self):
        self.closed = True
        if self.on_close is not None:
            self.on_close()


class _FakeGateway:
    def __init__(self):
        self.md_api = _FakeApi()
        self.td_api = _FakeApi()


class _FakeRuntime:
    def __init__(self, drill):
        self.main = _FakeMain(drill)
        self.gateway = _FakeGateway()
        self.rule = None
        self.exit_code = None
        self.connection_state: dict = {}


def _make_drill(**kw):
    args = _args(**kw)
    rec = d.RecordingNotifier(_NullNotifier())
    drill = d.Drill(args, {}, "c2701.DCE", rec)
    drill.position_state = []
    drill.runtime = _FakeRuntime(drill)
    return drill


def test_latency_measures_the_delay_between_the_fill_and_the_position_report():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    result = d.scenario_latency(drill)
    assert result.ok is True, result.summary
    assert result.details["成交→持仓回报延迟(秒)"] >= 0
    assert result.details["平仓成功"] is True
    # 开仓一笔、平仓一笔,不留仓位
    assert len(drill.runtime.main.orders) == 2
    assert drill.runtime.main.orders[0].offset == Offset.OPEN
    assert drill.runtime.main.orders[1].offset == Offset.CLOSE


def test_latency_fails_when_no_tick_arrives():
    drill = _make_drill()
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "行情" in result.summary


def test_latency_fails_when_the_order_does_not_fill():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))

    class _NoFill(_FakeMain):
        def send_order(self, req, gateway_name):
            self.orders.append(req)
            return "VT.1"

    drill.runtime.main = _NoFill(drill)
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "没有成交" in result.summary


def test_an_unfilled_order_is_cancelled_so_it_does_not_fill_later():
    """没成交就立刻撤单:否则这张单会挂着,几分钟后成交,仓位留到下一次运行里。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    cancelled = []

    class _NoFill(_FakeMain):
        def send_order(self, req, gateway_name):
            self.orders.append(req)
            return "VT.9"

        def cancel_order(self, req, gateway_name):
            cancelled.append(req)

    drill.runtime.main = _NoFill(drill)
    d.scenario_latency(drill)
    assert len(cancelled) == 1
    assert cancelled[0].orderid == "VT.9"


def test_latency_fails_when_the_contract_is_unknown():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))

    class _NoContract(_FakeMain):
        def get_contract(self, symbol):
            return None

    drill.runtime.main = _NoContract(drill)
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "合约信息" in result.summary


def test_latency_fails_when_the_tick_has_no_usable_price():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick(ask=0.0, last=0.0)))
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "买价" in result.summary


def test_a_position_that_cannot_be_closed_is_a_failure_and_raises_a_fatal_alert():
    """演练开的仓位没平掉 = 账户上留了仓位 → 必须判失败并发 FATAL,不能"演练通过"。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))

    class _StuckPosition(_FakeMain):
        def send_order(self, req, gateway_name):
            self.orders.append(req)
            if req.offset == Offset.OPEN:
                self.drill.trades.append((time.monotonic(), _Trade()))
                self.drill.positions.append((time.monotonic() + 0.01, _Pos()))
                self.drill.position_state = [_Pos()]
            # 平仓单发出去了,但持仓回报不来 → 仓位一直在
            return "VT.1"

    drill.runtime.main = _StuckPosition(drill)
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "没能平掉" in result.summary
    alerts = drill.notifier.find("演练未能平仓")
    assert alerts and alerts[0]["level"] == "FATAL"


def test_closing_a_shfe_position_uses_close_today():
    """上期所/能源中心用普通平仓会被交易所拒单。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos()]

    class _ShfeMain(_FakeMain):
        def get_contract(self, symbol):
            c = _Contract()
            c.exchange = Exchange.SHFE
            return c

    drill.runtime.main = _ShfeMain(drill)
    d._close_position(drill, 1.0)
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders and close_orders[0].offset == Offset.CLOSETODAY


def test_closing_a_dce_position_uses_the_plain_close_offset():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos()]
    d._close_position(drill, 1.0)
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders and close_orders[0].offset == Offset.CLOSE


def test_closing_a_flat_position_does_not_send_an_order():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    assert d._close_position(drill, 1.0) is True
    assert drill.runtime.main.orders == []


def test_cancelling_without_an_order_id_does_nothing():
    class _Boom:
        def cancel_order(self, req, gateway_name):
            raise AssertionError("不应该走到这里")

    d._cancel_order(_Boom(), "", "c2701", Exchange.DCE)


def test_a_failed_cancel_does_not_break_the_drill():
    """撤单只是善后:失败不该让演练崩掉。"""

    class _Boom:
        def cancel_order(self, req, gateway_name):
            raise RuntimeError("CTP 已断开")

    d._cancel_order(_Boom(), "VT.1", "c2701", Exchange.DCE)     # 不抛异常即通过


def test_the_drill_lists_its_scenarios():
    assert d.main(["--list"]) == 0
    assert "tick-stale" in d.SCENARIOS
    assert "hard-lock" in d.SCENARIOS


def test_the_drill_also_accepts_list_as_a_positional_argument(capsys):
    """运行手册里写的是 `drill.py list`(不是 `--list`),两种写法都得能用。"""
    assert d.main(["list"]) == 0
    assert "tick-stale" in capsys.readouterr().out


def test_the_drill_refuses_to_run_without_the_simnow_confirmation(capsys):
    assert d.main(["latency"]) == 2
    assert "模拟盘" in capsys.readouterr().err


def test_tick_stale_reports_the_alert_after_the_feed_is_cut():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    # 看门狗是在断掉行情之后才告警的:模拟"断开行情 → 触发告警"
    drill.runtime.gateway.md_api.on_close = lambda: drill.notifier.send(
        "WARN", "行情停更", "交易时段内 90s 没有收到 Tick")
    result = d.scenario_tick_stale(drill)
    assert result.ok is True
    assert drill.runtime.gateway.md_api.closed is True
    assert "行情停更" in result.details["告警"]


def test_tick_stale_ignores_an_alert_from_before_the_scenario():
    """场景开始前就发过的"行情停更"不能算作本次场景的结果(假阳性)。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.notifier.send("WARN", "行情停更", "上一次演练留下的旧告警")
    result = d.scenario_tick_stale(drill)
    assert result.ok is False
    assert "行情停更" in result.summary


def test_tick_stale_fails_when_the_watchdog_stays_silent():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    result = d.scenario_tick_stale(drill)
    assert result.ok is False
    assert "行情停更" in result.summary


def test_disconnect_records_whether_the_client_reconnects():
    drill = _make_drill()
    drill.runtime.gateway.td_api.on_close = lambda: drill.notifier.send(
        "ERROR", "网关连接断开", "交易服务器连接断开")
    result = d.scenario_disconnect(drill)
    assert result.ok is True
    assert drill.runtime.gateway.td_api.closed is True
    assert result.details["自动重连"] == "否"


def test_disconnect_notices_a_successful_reconnect():
    drill = _make_drill()

    def on_close():
        drill.notifier.send("ERROR", "网关连接断开", "x")
        drill.notifier.send("INFO", "网关已连接", "交易服务器登录成功")

    drill.runtime.gateway.td_api.on_close = on_close
    result = d.scenario_disconnect(drill)
    assert result.ok is True
    assert result.details["自动重连"] == "是"


def test_disconnect_fails_without_any_alert():
    drill = _make_drill()
    result = d.scenario_disconnect(drill)
    assert result.ok is False
    assert "没有收到告警" in result.summary


def test_disconnect_records_connection_state_and_no_backlog_evidence():
    """重连后记录连接状态,并给出「无积压补发」的证据项(确定性语义在离线测试里锁定)。"""
    drill = _make_drill()

    def on_close():
        drill.notifier.send("ERROR", "网关连接断开", "x")
        drill.runtime.connection_state["交易"] = False
        drill.runtime.connection_state["交易"] = True
        drill.notifier.send("INFO", "网关已连接", "交易服务器登录成功")

    drill.runtime.gateway.td_api.on_close = on_close
    result = d.scenario_disconnect(drill)
    assert result.ok is True
    assert result.details["重连后连接状态"] == "交易:已连接"
    assert result.details["信号积压补发"].startswith("无补发")
    assert result.details["窗口内发单失败告警(条)"] == 0


def test_disconnect_counts_send_failure_alerts_in_the_window():
    """断线期间的本地发单失败要计入窗口旁证(框架不排队,这些失败不会在重连后补发)。"""
    drill = _make_drill()

    def on_close():
        drill.notifier.send("ERROR", "网关连接断开", "x")
        drill.notifier.send("WARN", "网关错误", "委托请求发送失败，错误代码：-3")

    drill.runtime.gateway.td_api.on_close = on_close
    result = d.scenario_disconnect(drill)
    assert result.details["窗口内发单失败告警(条)"] == 1


def test_overnight_reports_the_trading_day_of_the_first_tick():
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.ticks[-1] = (time.monotonic(), _Tick())
    drill.ticks[-1][1].datetime = datetime(2026, 11, 10, 21, 0, 1)
    result = d.scenario_overnight(drill)
    assert result.ok is True
    assert result.details["归属交易日"] == "2026-11-11", "夜盘的第一根 Tick 属于下一个交易日"


def test_overnight_fails_when_no_tick_arrives():
    drill = _make_drill()
    result = d.scenario_overnight(drill)
    assert result.ok is False


class _FakeAccountView:
    def __init__(self, accountid, balance):
        self.accountid = accountid
        self.balance = balance


class _FakeRule:
    """假的账户风控规则:后台线程模拟运行时的定时器,
    于是"注入权益 → 真实 AccountGuard 判定 → 产生锁定事件 → 告警"整条链路都能被验证。"""

    def __init__(self, drill, limits, balance, state_path=None):
        import threading

        from risk.account_guard import AccountGuard, AccountLimits
        self.drill = drill
        self.daily_loss_limit = limits.daily_loss_limit
        self.max_drawdown = limits.max_drawdown
        self.lock_status = "OK"
        self.guard = AccountGuard(AccountLimits(self.daily_loss_limit, self.max_drawdown), state_path)
        self._balance = balance
        self.guard.peak_equity = balance
        self.guard.day_start_equity = balance
        self.guard.last_equity = balance
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._timer, daemon=True)
        self._thread.start()

    def _account(self):
        return _FakeAccountView("sim-account", self._balance)

    def _timer(self):
        """模拟运行时的 on_timer:取权益 → 更新守卫 → 有锁定事件就告警。"""
        while not self._stop.is_set():
            try:
                account = self._account()
                for event in self.guard.update(account.balance, datetime.now()):
                    if event.level == "HARD":
                        self.drill.notifier.send("FATAL", "累计死线触发", event.reason)
                        self.lock_status = "HARD"
                        if self.drill.runtime.exit_code is None:
                            self.drill.runtime.exit_code = 0
                    else:
                        self.drill.notifier.send("ERROR", "当日熔断", event.reason)
                        self.lock_status = "DAILY_LOCKED"
            except Exception:
                pass
            self._stop.wait(0.05)

    def stop(self):
        self._stop.set()


def test_daily_loss_triggers_when_the_injected_equity_is_below_the_limit():
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=1.0)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0)
    drill.runtime.rule = rule
    try:
        result = d.scenario_daily_loss(drill)
    finally:
        rule.stop()
    assert result.ok is True, result.summary
    assert result.details["注入权益"] < result.details["真实权益"] - 600
    assert "当日熔断" in result.details["告警"]


def test_daily_loss_fails_when_the_loss_is_not_big_enough():
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=0.3)
    # 阈值放到极大 → 注入的跌幅达不到熔断线
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=1e8, max_drawdown=1e9), 20000000.0)
    drill.runtime.rule = rule
    try:
        result = d.scenario_daily_loss(drill)
    finally:
        rule.stop()
    assert result.ok is False
    assert "没有触发当日熔断" in result.summary


def test_hard_lock_triggers_on_a_drawdown_and_stops_with_exit_code_zero(tmp_path):
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=1.0)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0,
                     state_path=tmp_path / "account_guard.json")
    drill.runtime.rule = rule
    try:
        result = d.scenario_hard_lock(drill)
    finally:
        rule.stop()
    assert result.ok is True, result.summary
    assert result.details["退出码"] == 0
    assert "累计死线" in result.details["告警"]
    # 硬锁定必须落盘:不落盘的话"重启后拒绝交易"只是文档里的说法
    assert result.details["重启后拒绝交易"] is True
    assert d._hard_lock_persisted(tmp_path / "account_guard.json") is True


def test_hard_lock_fails_when_the_state_is_not_persisted(tmp_path):
    """守卫没有状态文件 → 硬锁定丢了 → 场景必须判失败。"""
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=1.0)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0,
                     state_path=None)
    drill.runtime.rule = rule
    try:
        result = d.scenario_hard_lock(drill)
    finally:
        rule.stop()
    assert result.ok is False
    assert "状态文件" in result.summary


def test_injecting_equity_does_not_change_the_real_account():
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=0.3)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0)
    drill.runtime.rule = rule
    real_balance = rule._account().balance
    try:
        d.inject_equity(drill, 123.0)
        assert rule._account().balance == 123.0
        d.restore_equity(drill)
        assert rule._account().balance == real_balance
    finally:
        rule.stop()


def test_the_scenario_marker_is_taken_before_the_equity_is_injected():
    """守卫的定时器线程随时可能在注入权益的瞬间就发出告警。

    先注入再打标记的话,那条告警会落在标记**之前**,被 since 过滤掉 → 假阴性:
    场景明明触发了熔断,却报"没有触发"。
    """
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=1.0)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0)
    drill.runtime.rule = rule
    order = []
    original_inject = d.inject_equity

    def spy(drill_, balance):
        order.append(("inject", drill_.notifier.mark()))
        return original_inject(drill_, balance)

    d.inject_equity = spy
    try:
        result = d.scenario_daily_loss(drill)
    finally:
        d.inject_equity = original_inject
        rule.stop()
    assert result.ok is True, result.summary
    # 标记是在注入之前取的:注入时的告警条数 >= 标记值
    assert order and order[0][1] == 0


def test_the_hard_lock_scenario_marks_before_injecting_too(tmp_path):
    from risk.account_guard import AccountLimits
    drill = _make_drill(lock_timeout=1.0)
    rule = _FakeRule(drill, AccountLimits(daily_loss_limit=600.0, max_drawdown=2000.0), 20000000.0,
                     state_path=tmp_path / "account_guard.json")
    drill.runtime.rule = rule
    marks = []
    original_inject = d.inject_equity

    def spy(drill_, balance):
        marks.append(drill_.notifier.mark())
        return original_inject(drill_, balance)

    d.inject_equity = spy
    try:
        result = d.scenario_hard_lock(drill)
    finally:
        d.inject_equity = original_inject
        rule.stop()
    assert result.ok is True, result.summary
    assert marks and marks[0] == 0


# ---------------- 撤单与成交的竞态 ----------------

def test_a_fill_that_arrives_after_the_cancel_is_not_silently_ignored():
    """CTP 撤单是异步的:撤单请求发出前后订单仍可能成交。

    竞态成交会让账户上多出仓位,演练必须发现它、平掉它、发 FATAL 告警并判失败 ——
    不能"没等到成交回报"就当作什么都没发生,把仓位留给下一次运行。
    """
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))

    class _RaceMain(_FakeMain):
        def send_order(self, req, gateway_name):
            self.orders.append(req)
            return "VT.7"

        def cancel_order(self, req, gateway_name):
            # 撤单请求发出之后,订单仍然成交了
            self.drill.trades.append((time.monotonic(), _Trade()))
            self.drill.positions.append((time.monotonic() + 0.01, _Pos()))
            self.drill.position_state = [_Pos()]

    drill.runtime.main = _RaceMain(drill)
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "撞车" in result.summary or "撤单" in result.summary
    alerts = drill.notifier.find("演练未能平仓")
    assert alerts and alerts[0]["level"] == "FATAL"


def test_the_cancel_path_leaves_no_position_behind_when_nothing_fills():
    """正常路径:没成交 → 撤单 → 确认账户确实没有仓位。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))

    class _NoFill(_FakeMain):
        def send_order(self, req, gateway_name):
            self.orders.append(req)
            return "VT.8"

    drill.runtime.main = _NoFill(drill)
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert drill.net_position() == 0
    assert "没有成交" in result.summary
    assert not drill.notifier.find("演练未能平仓")


# ---------------- 不许动演练之前就存在的仓位 ----------------

def test_the_latency_scenario_refuses_to_run_when_a_position_already_exists():
    """账户上本来就有仓位 → 分不清哪一手是演练开的 → 拒绝演练,而不是误平别人的仓。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos(volume=2)]
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "拒绝演练" in result.summary
    assert drill.runtime.main.orders == []      # 一张单都没发出去


def test_the_latency_scenario_refuses_to_run_when_the_positions_are_locked():
    """多空锁仓(各 1 手)的净持仓是 0,只看净额会被骗过去:
    收尾平仓时会动到演练之前就存在的仓位 → 同样要拒绝。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    long_pos = _Pos(volume=1)
    short_pos = _Pos(volume=1)
    short_pos.direction = Direction.SHORT
    drill.position_state = [long_pos, short_pos]
    assert drill.net_position() == 0            # 净额骗人
    result = d.scenario_latency(drill)
    assert result.ok is False
    assert "拒绝演练" in result.summary
    assert drill.runtime.main.orders == []


def test_a_net_position_is_neither_long_nor_short_but_still_a_position():
    """净持仓账户(Direction.NET)方向不明:净额里不算多也不算空,
    但总手数里要算 —— 否则会以为账户上没有仓位。"""
    drill = _make_drill()
    net_pos = _Pos(volume=1)
    net_pos.direction = Direction.NET
    drill.position_state = [net_pos]
    assert drill.net_position() == 0
    assert drill.gross_position() == 1
    assert drill.has_position() is True


def test_a_position_of_another_symbol_is_not_counted():
    """别的合约的仓位与这次演练无关 —— 既不算净额,也不算总手数。"""
    drill = _make_drill()
    drill.position_state = [_Pos(symbol="m2701.DCE", volume=3)]
    assert drill.net_position() == 0
    assert drill.gross_position() == 0
    assert drill.has_position() is False


def test_a_position_the_net_position_does_not_show_is_not_reported_as_closed():
    """方向不明的仓位(NET)不会体现在净额里:净额为 0 **不等于**账户上没有仓位。
    收尾判断必须看总手数,否则会把"还留着仓"当成"已经平干净"。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    net_pos = _Pos(volume=1)
    net_pos.direction = Direction.NET
    drill.position_state = [net_pos]
    drill.gross_baseline = 0
    assert d._close_position(drill, 1.0) is False
    assert drill.runtime.main.orders == []


def test_closing_only_touches_the_volume_the_drill_itself_opened():
    """平仓量 = 当前净持仓 − 进场前的净持仓,不能按账户净持仓一刀切。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos(volume=3)]        # 进场前就有 2 手,演练开了 1 手
    d._close_position(drill, 1.0, baseline=2)
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert len(close_orders) == 1
    assert close_orders[0].volume == 1             # 只平自己那 1 手,不是 3 手


def test_closing_a_position_that_is_entirely_the_baseline_sends_no_order():
    """净持仓没有超出进场前的水位 → 没有演练开的仓,不该发平仓单。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos(volume=2)]
    assert d._close_position(drill, 1.0, baseline=2) is True
    assert drill.runtime.main.orders == []


def test_closing_a_short_position_buys_to_close_it():
    """净持仓为负(空头)时,平仓要用 BUY,否则会开出更大的空头。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    short = _Pos(volume=1)
    short.direction = Direction.SHORT
    drill.position_state = [short]
    d._close_position(drill, 1.0)
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders and close_orders[0].direction == Direction.LONG


def test_the_close_order_is_priced_on_the_side_it_trades_on():
    """平多(卖出)挂在**买一**上,平空(买入)挂在**卖一**上:
    用错一边就会挂在对手价上等不到成交,演练变成"平仓超时"。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick(ask=2400.0, bid=2399.0)))
    drill.position_state = [_Pos(volume=1)]
    d._close_position(drill, 1.0)
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders and close_orders[0].price == 2399.0        # 卖出 → 买一(bid)

    short_drill = _make_drill()
    short_drill.ticks.append((time.monotonic(), _Tick(ask=2400.0, bid=2399.0)))
    short = _Pos(volume=1)
    short.direction = Direction.SHORT
    short_drill.position_state = [short]
    d._close_position(short_drill, 1.0)
    close_orders2 = [r for r in short_drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders2 and close_orders2[0].price == 2400.0     # 买入 → 卖一(ask)


def test_the_position_the_drill_opened_is_closed_even_on_a_netting_account():
    """净持仓账户:持仓回报是 `Direction.NET`,净额算出来是 0。

    只看净额会以为"没有仓位要平" → 既不发平仓单,又把留仓手数误报成 0,
    仓位留在账户上下次运行还得接着还。演练知道自己开了 1 手多仓,
    收尾必须按这个手数、方向(卖出平多)去平。
    """
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick(ask=2400.0, bid=2399.0)))
    net_pos = _Pos(volume=1)
    net_pos.direction = Direction.NET
    drill.position_state = [net_pos]
    drill.gross_baseline = 0
    drill.owned_volume = 1
    drill.owned_direction = Direction.LONG
    assert d._close_position(drill, 1.0) is True
    close_orders = [r for r in drill.runtime.main.orders if r.offset != Offset.OPEN]
    assert close_orders, "净持仓账户下也必须发出平仓单"
    assert close_orders[0].volume == 1
    assert close_orders[0].direction == Direction.SHORT       # 开的是多仓 → 用卖出平
    assert close_orders[0].price == 2399.0                    # 卖出 → 挂买一


def test_the_close_order_is_cancelled_when_it_does_not_fill():
    """平仓单没成交时它还挂在交易所上,过一会儿成交就会留下一笔"晚到的"持仓 ——
    账户状态与演练结论就不一致了。撤掉它。"""
    drill = _make_drill()
    drill.ticks.append((time.monotonic(), _Tick()))
    drill.position_state = [_Pos(volume=1)]
    cancelled = []

    class _StuckMain:
        """平仓单发出去就是不成交(挂在对手价上等不到)。"""

        def __init__(self):
            self.orders = []

        def get_contract(self, symbol):
            return _Contract()

        def get_all_positions(self):
            return drill.position_state

        def send_order(self, req, gateway_name):
            self.orders.append(req)
            return "VT.CLOSE"

        def cancel_order(self, req, gateway_name):
            cancelled.append(req)

    drill.runtime.main = _StuckMain()
    assert d._close_position(drill, 1.0) is False
    assert cancelled, "没成交的平仓单必须撤掉"


# ---------------- 告警"发出"与"送达"要分开记 ----------------

def test_an_alert_that_the_notifier_rejected_is_marked_as_not_delivered():
    """底层拒绝(去重/过滤/队列满)时手机端并没有响,不能算"收到告警"。"""

    class _Rejecting:
        enabled = True

        def send(self, level, title, message, extra=None):
            return False

        def close(self, drain_timeout=10.0):
            pass

    rec = d.RecordingNotifier(_Rejecting())
    rec.send("ERROR", "当日熔断", "亏了 700")
    summary = rec.delivery_summary()
    assert summary["发出"] == 1
    assert summary["底层接受"] == 0
    assert summary["未送达"] == 1
    assert "当日熔断" in summary["未送达明细"][0]


def test_an_alert_that_was_accepted_is_marked_as_delivered():
    rec = d.RecordingNotifier(_NullNotifier())
    rec.send("ERROR", "当日熔断", "亏了 700")
    summary = rec.delivery_summary()
    assert summary["发出"] == 1 and summary["底层接受"] == 1
    assert summary["未送达"] == 0


def test_an_unconfigured_notification_channel_is_not_reported_as_a_failed_delivery():
    """WEBHOOK_URL 为空时底层对每条告警都返回 False —— 那是"没地方可送",不是"丢了"。"""

    class _Disabled:
        enabled = False

        def send(self, level, title, message, extra=None):
            return False

        def close(self, drain_timeout=10.0):
            pass

    rec = d.RecordingNotifier(_Disabled())
    rec.send("ERROR", "当日熔断", "亏了 700")
    summary = rec.delivery_summary()
    assert summary["通知渠道已配置"] is False
    assert summary["发出"] == 1 and summary["底层接受"] == 0


# ---------- 诊断埋点(--trace):默认关闭;开启后 disconnect 的各检查点都落盘 ----------

def _traced_drill(tmp_path, **kw):
    import drill_trace
    drill = _make_drill(**kw)
    drill.trace = drill_trace.TraceLog(tmp_path / "t.log")
    return drill


def test_disconnect_with_trace_records_every_checkpoint_in_order(tmp_path):
    drill = _traced_drill(tmp_path)
    drill.runtime.gateway.td_api.on_close = lambda: drill.notifier.send("ERROR", "网关连接断开", "交易服务器连接断开")

    result = d.scenario_disconnect(drill)

    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    order = ["disconnect_before_close", "close_begin", "api_close_called", "close_returned", "api_marked_released",
             "wait_disconnect_alert_done", "wait_reconnect_done"]
    positions = [text.index(name) for name in order]
    assert positions == sorted(positions) and result.ok is True
    assert drill.runtime.gateway.td_api.closed is True            # 探针只记录,原函数照常执行


def test_disconnect_trace_flags_calls_into_the_released_api(tmp_path):
    drill = _traced_drill(tmp_path)
    api = drill.runtime.gateway.td_api
    api.query_account = lambda: "acct"                            # 假 API 上补一个会被探针包装的方法

    def on_close():
        drill.notifier.send("ERROR", "网关连接断开", "交易服务器连接断开")
        api.query_account()                                       # 释放之后窗口内有人又调用了 API

    api.on_close = on_close
    d.scenario_disconnect(drill)
    drill.runtime.gateway.td_api.query_account()                  # 窗口结束后再调一次

    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert "call_after_release" in text and "method=query_account" in text


def test_trace_skip_close_is_a_control_run_that_never_releases_the_api(tmp_path):
    drill = _traced_drill(tmp_path)
    drill.args.trace_skip_close = True

    d.scenario_disconnect(drill)

    text = (tmp_path / "t.log").read_text(encoding="utf-8")
    assert drill.runtime.gateway.td_api.closed is False
    assert "close_begin skipped=True" in text and "api_marked_released" not in text


def test_disconnect_without_trace_leaves_the_api_unwrapped():
    drill = _make_drill()
    drill.runtime.gateway.td_api.on_close = lambda: drill.notifier.send("ERROR", "网关连接断开", "交易服务器连接断开")

    d.scenario_disconnect(drill)

    assert "close" not in vars(drill.runtime.gateway.td_api)       # 没有任何包装
    assert drill.trace.enabled is False


def test_trace_flag_parses_and_defaults_to_off():
    import argparse
    import re
    src = (Path(d.__file__)).read_text(encoding="utf-8")
    assert re.search(r'add_argument\("--trace",\s*default=""', src)
    assert "--trace-skip-close" in src
