"""废单文本采集脚本的纯逻辑测试(不联网、不连 CTP)。"""
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import collect_reject_texts as c  # noqa: E402  (内部会先 import bootstrap)
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType  # noqa: E402


def make_cases(**kw):
    return dict((name, req) for name, req in c.build_cases("c2701.DCE", Exchange.DCE, 2195.0, 2000, **kw))


def test_all_cases_are_opens_or_closes_that_cannot_fill():
    """采集用的委托必须都不会成交 —— 脚本在模拟盘上跑,不能留下持仓。"""
    for name, req in make_cases().items():
        if name == "无持仓时平仓":
            assert req.offset == Offset.CLOSE
            continue
        # 其余都是开仓:价格要么越界(价格超出涨跌停),要么合约不存在/手数超限
        assert req.offset == Offset.OPEN
        if name == "价格超出涨跌停":
            assert req.price > 2195.0
        if name == "超大手数":
            assert req.volume > 2000
        if name == "不存在的合约":
            assert req.symbol == "c9999"


def test_huge_volume_uses_contract_limit_plus_one():
    cases = dict((name, req) for name, req in c.build_cases("c2701.DCE", Exchange.DCE, 100.0, 500))
    assert cases["超大手数"].volume == 501


def test_prices_are_aligned_to_the_price_tick():
    """价格不是最小变动价位的整数倍时 CTP 会按"价格非法"拒单,会把想测的场景变成另一个场景。"""
    cases = make_cases(pricetick=0.5)
    for name, req in cases.items():
        if req.offset == Offset.CLOSE:
            continue
        assert abs(req.price / 0.5 - round(req.price / 0.5)) < 1e-9, name


@pytest.mark.parametrize(
    "dt, expected",
    [
        (datetime(2026, 11, 10, 9, 30), True),      # 周二日盘
        (datetime(2026, 11, 10, 10, 20), False),     # 中间休息
        (datetime(2026, 11, 10, 14, 0), True),
        (datetime(2026, 11, 10, 21, 30), True),      # 夜盘
        (datetime(2026, 11, 10, 23, 30), True),      # 保守取并集:21:00 之后一律算在时段内
        (datetime(2026, 11, 10, 1, 0), True),        # 夜盘跨午夜延续段
        (datetime(2026, 11, 7, 10, 0), False),       # 周六
        (datetime(2026, 11, 8, 10, 0), False),       # 周日
        (datetime(2026, 11, 10, 16, 0), False),      # 收盘后
    ],
)
def test_in_trading_session(dt, expected):
    assert c.in_trading_session(dt) is expected


def test_during_trading_session_no_order_can_fill():
    """交易时段内跑采集:所有"价格合法"的场景都改成不可能成交的越界价,并跳过时段场景。"""
    cases = make_cases(in_session=True)
    assert "非交易时段开仓" not in cases, "交易时段内测不出时段拒单,应该跳过"
    assert cases["超大手数"].price < 2195.0, "越界低价才可能保证不成交"
    assert cases["不存在的合约"].price == 2195.0, "合约不存在,价格不影响安全性"


def test_collector_picks_the_closest_error_log():
    """拒单原因按时间关联:取离废单时刻最近的那条,超出窗口则不取。"""
    col = c.Collector()
    col.errors = [(10.0, "较早的错误"), (20.0, "最近的错误")]
    assert col.error_near(19.5) == "最近的错误"
    assert col.error_near(2.0) == ""            # 距离 8 秒,超出 6 秒窗口


def test_wait_error_waits_for_the_log_to_arrive():
    """CTP 先推订单回报、后写错误日志;拿到拒单回报时日志还没入队,必须等。"""

    def later():
        time.sleep(0.2)
        col.errors.append((100.5, "交易委托失败，代码：21，信息：CTP:该交易席位未连接到交易所"))

    col = c.Collector()
    threading.Thread(target=later, daemon=True).start()
    assert col.wait_error(100.0, timeout=2.0) != ""


def test_wait_error_gives_up_after_the_timeout():
    col = c.Collector()
    assert col.wait_error(100.0, timeout=0.2) == ""


def test_wait_error_does_not_return_a_previous_scenario_error():
    """场景之间会串味:上一个场景的旧错误离本次发单更近,会被误当成这次的拒单原因。

    所以发单前必须记下错误条数(error_index),之后只在新增的那些里找。
    """
    col = c.Collector()
    col.errors = [(10.0, "上一个场景的错误")]
    since = col.error_index()
    assert since == 1
    # 本次场景没有产生新错误 → 不能把上一个场景的错误当答案
    assert col.wait_error(10.1, timeout=0.2, since=since) == ""
    # 本次场景产生了新错误 → 只匹配新错误
    col.errors.append((10.2, "本次场景的错误"))
    assert col.error_near(10.1, since=since) == "本次场景的错误"


def test_error_index_is_a_consistent_snapshot():
    col = c.Collector()
    assert col.error_index() == 0
    col.errors.append((1.0, "一条"))
    assert col.error_index() == 1


def test_exchange_level_reject_logs_are_captured():
    """交易所拒单走 onRtnOrder,原因在 StatusMsg 里,措辞是"委托 X 状态更新，<原因>"。

    这条路径不经过 onRspOrderInsert,"交易委托失败"收不到它;漏掉的话交易所拒单的原因
    就永远是"未知",一律按硬拒绝,平仓单会被提前升级为人工处理。
    """
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import INFO
    from vnpy.trader.object import LogData

    col = c.Collector()
    col.on_log(Event(EVENT_LOG, LogData(msg="委托 1_1_5 状态更新，价格超出涨跌停",
                                         level=INFO, gateway_name="CTP")))
    assert col.errors and "超出涨跌停" in col.errors[0][1]


def test_collector_recognizes_info_level_reject_logs():
    """真实 vnpy_ctp 把拒单写成 INFO 级别,不能只认 ERROR。"""
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import INFO
    from vnpy.trader.object import LogData

    col = c.Collector()
    col.on_log(Event(EVENT_LOG, LogData(msg="交易委托失败,代码：21，信息：CTP:该交易席位未连接到交易所",
                                         level=INFO, gateway_name="CTP")))
    assert col.errors and "该交易席位未连接到交易所" in col.errors[0][1]

    col2 = c.Collector()
    col2.on_log(Event(EVENT_LOG, LogData(msg="订阅行情成功", level=INFO, gateway_name="CTP")))
    assert not col2.errors, "普通 INFO 日志不该被当成错误"


def test_gateway_traceback_is_captured_as_an_error():
    """vnpy_ctp 对不存在的合约在回调里抛 KeyError,事件引擎把 traceback 写成一条日志。"""
    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.object import LogData

    col = c.Collector()
    col.on_log(Event(EVENT_LOG, LogData(msg="KeyError: 'c9999'\nAt: ctp_gateway.py(505): onRspOrderInsert",
                                         gateway_name="")))
    assert col.errors and "KeyError" in col.errors[0][1]


class _FakeApi:
    connect_status = True


class _FakeGateway:
    def __init__(self, connect_status=True):
        self.td_api = _FakeApi()
        self.td_api.connect_status = connect_status
        self.queried = False

    def query_position(self):
        self.queried = True


class _FakeEngine:
    def __init__(self, positions=(), connect_status=True):
        self._positions = list(positions)
        self.gateway = _FakeGateway(connect_status)

    def get_gateway(self, name):
        return self.gateway

    def get_all_positions(self):
        return self._positions


def test_check_positions_reports_the_connection_state():
    """连接断了就查不到持仓,这时"没查到"不等于"没有持仓",必须能区分。"""
    engine = _FakeEngine(connect_status=False)
    positions, connected = c.check_positions(engine, "c2701.DCE", wait=0.2)
    assert positions == [] and connected is False
    assert engine.gateway.queried


def test_check_positions_returns_non_zero_positions():
    class _Pos:
        def __init__(self, symbol, direction, volume):
            self.vt_symbol = symbol
            self.direction = direction
            self.volume = volume

    engine = _FakeEngine([_Pos("c2701.DCE", Direction.LONG, 1), _Pos("c2701.DCE", Direction.SHORT, 0)])
    positions, connected = c.check_positions(engine, "c2701.DCE", wait=0.2)
    assert positions and "c2701.DCE" in positions[0] and connected is True


def test_symbol_has_position_detects_an_existing_position():
    """发单前必须确认目标合约没有持仓:"无持仓时平仓"场景发的是平仓单,
    账户上本来有持仓的话,那张单会真的把仓位平掉。"""
    class _Pos:
        def __init__(self, symbol, direction, volume):
            self.vt_symbol = symbol
            self.direction = direction
            self.volume = volume

    engine = _FakeEngine([_Pos("c2701.DCE", Direction.LONG, 1)])
    has_pos, connected = c.symbol_has_position(engine, "c2701.DCE", wait=0.2)
    assert has_pos is True and connected is True


def test_symbol_has_position_is_true_even_for_other_symbols():
    """判定放宽到"账户有持仓就中止":采集脚本会发各种委托,动到别人的仓位同样不可接受。"""
    class _Pos:
        def __init__(self, symbol, direction, volume):
            self.vt_symbol = symbol
            self.direction = direction
            self.volume = volume

    engine = _FakeEngine([_Pos("m2701.DCE", Direction.LONG, 3)])
    has_pos, connected = c.symbol_has_position(engine, "c2701.DCE", wait=0.2)
    assert has_pos is True and connected is True


def test_position_check_waits_the_whole_window_before_concluding_flat():
    """CTP 的持仓查询是异步的:看到别的合约有持仓就早退,可能漏掉本合约还没到达的持仓回报,
    于是误以为空仓、发出一张平仓单把仓位真的平掉。发单前的检查必须等满整个窗口。"""
    class _Pos:
        def __init__(self, symbol, direction, volume):
            self.vt_symbol = symbol
            self.direction = direction
            self.volume = volume

    engine = _FakeEngine([_Pos("m2701.DCE", Direction.LONG, 3)])
    started = time.monotonic()
    c.symbol_has_position(engine, "c2701.DCE", wait=0.5)
    assert time.monotonic() - started >= 0.4, "必须等满窗口,不能看到持仓就早退"


def test_symbol_has_position_reports_a_broken_connection():
    """连接断了就查不到持仓,这时"没查到"不等于"没有持仓",必须能区分。"""
    engine = _FakeEngine(connect_status=False)
    has_pos, connected = c.symbol_has_position(engine, "c2701.DCE", wait=0.2)
    assert has_pos is False and connected is False


def test_positions_of_symbol_filters_by_symbol():
    positions = ["c2701.DCE 多 1", "m2701.DCE 空 2"]
    assert c.positions_of_symbol(positions, "c2701.DCE") == ["c2701.DCE 多 1"]
    assert c.positions_of_symbol(positions, "m2701.DCE") == ["m2701.DCE 空 2"]
    assert c.positions_of_symbol(positions, "x2701.DCE") == []


def test_close_engine_gives_up_after_the_timeout():
    """CTP 原生线程在断连风暴下可能不返回,关闭必须限时,否则脚本"跑完了但不退出"。"""
    class _StuckEngine:
        def close(self):
            time.sleep(30)

    started = time.monotonic()
    c.close_engine(_StuckEngine(), timeout=0.2)
    assert time.monotonic() - started < 5.0


class _CollectEngine(_FakeEngine):
    """给 _collect 用的假引擎:能订阅、能收行情、能发单。"""

    def __init__(self, positions=(), connect_status=True):
        super().__init__(positions=positions, connect_status=connect_status)
        self.subscribed = []
        self.sent = []

    def subscribe(self, req):
        self.subscribed.append(req)

    def send_order(self, req, gateway_name):
        self.sent.append(req)
        return "VT.1"


def _fake_contract(symbol="c2701.DCE"):
    from vnpy.trader.constant import Product
    from vnpy.trader.object import ContractData

    code, exch = symbol.split(".")
    return ContractData(symbol=code, exchange=Exchange.DCE, name=code, product=Product.FUTURES,
                        size=10, pricetick=1.0, min_volume=1, gateway_name="CTP")


def test_collect_returns_3_when_the_contract_never_arrives():
    """合约表没到位就发单,所有委托都会被"合约不存在"拒掉,测不到想测的东西 → 直接失败退出。"""
    import argparse

    args = argparse.Namespace(connect_timeout=0.2, out="", position_check_timeout=0.1)
    collector = c.Collector()                       # 故意不放合约
    engine = _CollectEngine()
    code = c._collect(args, "c2701.DCE", {}, None, engine, collector)
    assert code == 3
    assert engine.sent == [], "没拿到合约信息就不该发任何委托"


def test_collect_gives_up_immediately_when_the_login_failed():
    """登录失败时合约表永远不会来,再等满超时只是浪费时间。

    实测:SimNow 第二套(7x24)对新注册账号回"交易服务器登录失败,代码：3,信息：CTP:不合法的登录",
    账号要到第三个交易日才能用 —— 白等 180 秒什么也采不到。
    """
    import argparse

    from vnpy.event import Event
    from vnpy.trader.event import EVENT_LOG
    from vnpy.trader.logger import ERROR
    from vnpy.trader.object import LogData

    args = argparse.Namespace(connect_timeout=180.0, out="")
    collector = c.Collector()
    collector.on_log(Event(EVENT_LOG, LogData(
        msg="交易服务器登录失败,代码：3,信息：CTP:不合法的登录", level=ERROR, gateway_name="CTP")))
    engine = _CollectEngine()
    started = time.monotonic()
    code = c._collect(args, "c2701.DCE", {}, None, engine, collector)
    assert code == 6
    assert time.monotonic() - started < 5.0, "登录失败应当立刻退出,不能等满超时"
    assert engine.sent == []


def test_collect_aborts_when_the_symbol_already_has_a_position():
    """账户上本来就有持仓时,"无持仓时平仓"场景的平仓单会真的平掉仓位 → 必须中止。"""
    import argparse

    class _Pos:
        def __init__(self, symbol, direction, volume):
            self.vt_symbol = symbol
            self.direction = direction
            self.volume = volume

    args = argparse.Namespace(connect_timeout=0.2, out="", position_check_timeout=0.1)
    collector = c.Collector()
    collector.contracts["c2701.DCE"] = _fake_contract()
    engine = _CollectEngine(positions=[_Pos("c2701.DCE", Direction.LONG, 2)])
    code = c._collect(args, "c2701.DCE", {}, None, engine, collector)
    assert code == 5
    assert engine.sent == [], "有持仓时不能发任何委托"


def test_write_collected_keeps_the_manual_analysis(tmp_path):
    """重新采集不能把人工写的分析段落冲掉。"""
    out = tmp_path / "doc.md"
    out.write_text("# 标题\n\n人工分析段落\n\n" + c.BEGIN_MARK + " 旧表格 " + c.END_MARK + "\n\n结尾\n",
                   encoding="utf-8")
    c.write_collected(out, c.BEGIN_MARK + " 新表格 " + c.END_MARK)
    text = out.read_text(encoding="utf-8")
    assert "人工分析段落" in text and "结尾" in text
    assert "新表格" in text and "旧表格" not in text


def test_write_collected_does_not_overwrite_a_doc_without_markers(tmp_path):
    out = tmp_path / "doc.md"
    out.write_text("# 人工维护的文档\n", encoding="utf-8")
    c.write_collected(out, "新表格")
    assert out.read_text(encoding="utf-8") == "# 人工维护的文档\n"
    assert (tmp_path / "doc-raw.md").exists()


def test_write_collected_keeps_the_begin_marker_so_the_second_run_still_updates(tmp_path):
    """BEGIN 标记必须原样写回去。

    丢了下一次运行 old.find(BEGIN_MARK) 返回 -1 → 采集结果只落到 -raw.md,
    主文档里的表格从此不再更新,而且不报错。
    """
    out = tmp_path / "doc.md"
    out.write_text("# 标题\n\n" + c.BEGIN_MARK + ": 自动采集区 -->\n旧表格\n" + c.END_MARK + "\n\n结尾\n",
                   encoding="utf-8")
    c.write_collected(out, "新表格\n" + c.END_MARK)
    text = out.read_text(encoding="utf-8")
    assert c.BEGIN_MARK in text and "新表格" in text and "旧表格" not in text
    c.write_collected(out, "又一轮\n" + c.END_MARK)
    text2 = out.read_text(encoding="utf-8")
    assert "又一轮" in text2 and "新表格" not in text2
    assert c.BEGIN_MARK in text2 and c.END_MARK in text2
    assert not (tmp_path / "doc-raw.md").exists()


def test_without_confirm_flag_it_refuses_to_run(monkeypatch, capsys):
    """没有 --confirm-simnow 就直接拒绝执行:这个脚本只允许在模拟盘上跑。"""
    monkeypatch.setenv("CTP_ENVIRONMENT", "实盘")
    assert c.main(["--symbol", "c2701.DCE"]) == 2
    assert "模拟盘" in capsys.readouterr().err


def test_missing_symbol_is_reported(monkeypatch, capsys):
    # 不加载真实的 .env(里面有 LIVE_VT_SYMBOL),也不去连 CTP
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: None)
    monkeypatch.delenv("LIVE_VT_SYMBOL", raising=False)
    assert c.main(["--confirm-simnow"]) == 2
    assert "未指定合约" in capsys.readouterr().err
