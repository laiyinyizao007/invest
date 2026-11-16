"""清场工具的平仓计划测试:方向、手数拆分、价格取法。不联网。"""
import json
import threading
from types import SimpleNamespace

from vnpy.trader.constant import Direction, Exchange, Offset, Status
from vnpy.trader.object import PositionData

from flatten_positions import login_failed, manual_positions, plan_closes, split_is_reliable


def _pos(symbol: str, exchange: Exchange, direction: Direction, volume: int, yd: int = 0) -> PositionData:
    pos = PositionData(symbol=symbol, exchange=exchange, direction=direction, gateway_name="CTP")
    pos.volume = volume
    pos.yd_volume = yd
    pos.contract = SimpleNamespace(pricetick=1.0)
    return pos



def _profile(env, hard=(), notes=()):
    """构造 ctp_tools.profile.Profile(load_profile 的返回值)。"""
    from ctp_tools.profile import Profile

    return Profile(env=dict(env), hard_conflicts=list(hard), notes=list(notes))

def test_long_position_is_closed_by_selling_at_bid():
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    plan = plans[0]
    assert plan.direction == Direction.SHORT      # 平多 = 卖出
    assert plan.offset == Offset.CLOSE            # 大商所不分今昨
    assert plan.volume == 3
    assert plan.price == 2185.0                    # 挂买一,主动打买盘


def test_short_position_is_closed_by_buying_at_ask():
    pos = _pos("c2701", Exchange.DCE, Direction.SHORT, 2)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].direction == Direction.LONG    # 平空 = 买入
    assert plans[0].price == 2186.0                # 挂卖一
    assert plans[0].volume == 2


def test_shfe_splits_a_pure_yesterday_position():
    """纯昨仓可以安全拆:vnpy_ctp 只在「YdPosition and not TodayPosition」时才写 yd_volume,
    这时 yd_volume == 总手数,拆成平昨是对的。"""
    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 5, yd=5)
    plans = plan_closes([pos], {"rb2701.SHFE": (3000.0, 3001.0, 3000.0)})
    offsets = {p.offset: p.volume for p in plans}
    assert offsets == {Offset.CLOSEYESTERDAY: 5}
    assert all(p.direction == Direction.SHORT for p in plans)


def test_shfe_mixed_position_is_refused_instead_of_guessed():
    """今昨混合仓的 yd_volume 恒为 0,拆出来的「平今手数」会超过实际今仓。

    柜台会以「平今量超过今持仓量」拒单 → 平仓平不掉,还容易被误判成柜台故障。
    这种持仓**不生成平仓单**,交人工处理。
    """
    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 5, yd=2)     # 混合仓:yd_volume 拿不到
    assert split_is_reliable(pos) is False
    assert plan_closes([pos], {"rb2701.SHFE": (3000.0, 3001.0, 3000.0)}) == []
    manual = manual_positions([pos])
    assert manual and "请人工平仓" in manual[0]


def test_shfe_pure_today_position_is_also_refused():
    """纯今仓同样判为不可靠:yd_volume=0 时分不清「纯今」还是「今昨混合」。"""
    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 3, yd=0)
    assert split_is_reliable(pos) is False
    assert plan_closes([pos], {"rb2701.SHFE": (3000.0, 3001.0, 3000.0)}) == []


def test_other_exchanges_never_need_a_split():
    """大商所/郑商所不分今昨,一张平仓单就行,不受上期所那条规则影响。"""
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3, yd=0)
    assert split_is_reliable(pos) is True
    assert [p.offset for p in plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})] == [Offset.CLOSE]


def test_net_direction_is_never_guessed():
    """净持仓回报里没有多空之分,猜错方向等于把平仓做成开仓。"""
    pos = _pos("c2701", Exchange.DCE, Direction.NET, 3)
    assert plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)}) == []


def test_zero_volume_is_skipped_and_missing_quote_falls_back_to_last():
    empty = _pos("c2701", Exchange.DCE, Direction.LONG, 0)
    assert plan_closes([empty], {}) == []
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    plans = plan_closes([pos], {"c2701.DCE": (0.0, 0.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].price == 2185.0
    assert "最新价" in plans[0].reason


def test_pricetick_comes_from_contract_not_from_position_object():
    """最小变动价位必须来自合约信息。

    PositionData 上没有 .contract(vnpy 不填这个属性),按 1 计价的话,
    国债(0.001)、股指(0.2)这类品种的价格不是整数倍,柜台会以「价格非法」拒单,
    平仓就平不掉——还会误以为是柜台问题。
    """
    pos = _pos("T2601", Exchange.CFFEX, Direction.LONG, 1)
    plans = plan_closes([pos], {"T2601.CFFEX": (3900.123, 3900.222, 3900.1)}, {"T2601.CFFEX": 0.2})
    assert len(plans) == 1
    assert plans[0].price == 3900.2          # 3900.123 → 3900.2(0.2 的整数倍)
    assert "没有合约信息" not in plans[0].reason


def test_missing_pricetick_falls_back_to_one_and_says_so():
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert plans[0].price == 2185.0
    assert "没有合约信息" in plans[0].reason


def test_position_without_vt_symbol_suffix_is_skipped():
    """vt_symbol 形如 c2701.DCE;没有交易所后缀时拼不出合约代码,宁可不发单。"""
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701"
    assert plan_closes([pos], {"c2701": (2185.0, 2186.0, 2185.0)}) == []


def test_login_failure_markers_are_recognised():
    """登录/授权失败要能被认出来(否则会一直干等到超时)。"""
    assert login_failed(["… 交易服务器登录失败,原因:密码错误"]) is True
    assert login_failed(["… 授权验证失败,原因:AppID不合法"]) is True
    assert login_failed(["交易服务器连接成功", "交易服务器登录成功"]) is False
    assert login_failed([]) is False


# ---------- 「查不到」不等于「没有」 ----------

class _FakeEngine:
    """最小可用的引擎替身:只提供 _wait_positions 用到的那几个方法。"""

    def __init__(self, live=True, positions=()):
        self.td_api = SimpleNamespace(connect_status=True, login_status=live)
        self._positions = list(positions)
        self.gateway = SimpleNamespace(td_api=self.td_api, query_position=lambda: None)

    def get_gateway(self, name):
        return self.gateway

    def get_all_positions(self):
        return self._positions


class _FakeCollector:
    """只提供「本次查询回报的行」这一样东西。"""

    def __init__(self, rows=()):
        self._rows = list(rows)
        self.lock = threading.Lock()

    def mark_positions(self):
        return 0

    def positions_since(self, mark):
        return list(self._rows)


def test_no_response_is_not_reported_as_flat():
    """柜台没连上、持仓查询没回报时,一样是空列表——但那不是「账户是空的」。

    把它当成「空的」就会在断线的柜台上发平仓单(或者告诉操作员「账户已空」)。
    """
    from flatten_positions import _wait_positions

    positions, trusted = _wait_positions(_FakeEngine(live=False), _FakeCollector(), wait=0.2)
    assert positions == [] and trusted is False
    positions, trusted = _wait_positions(_FakeEngine(live=True), _FakeCollector(), wait=0.2)
    assert positions == [] and trusted is True


def test_a_returned_position_is_trusted():
    from flatten_positions import _wait_positions

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    positions, trusted = _wait_positions(_FakeEngine(live=True), _FakeCollector([pos]), wait=0.2)
    assert len(positions) == 1 and trusted is True


# ---------- 收尾按**成交量**对账 ----------

def test_the_reconciliation_compares_traded_volume_not_just_the_position_query():
    """查询显示空仓还不够:平掉的量必须等于初始持仓量。

    「单子没成交、持仓却消失了」在现实中不可能发生(除非仓位被别人动过),
    这种对不上必须升级人工,不能记成「已平空」。
    """
    from flatten_positions import reconcile_filled

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    results = [{"vt_symbol": "c2701.DCE", "direction": "Short", "成交手数": 3}]   # 卖出平多
    ok, problems = reconcile_filled([pos], [], results)
    assert ok and problems == []

    ok, problems = reconcile_filled([pos], [], [{"vt_symbol": "c2701.DCE", "direction": "Short", "成交手数": 0}])
    assert not ok and "只成交了 0 手" in problems[0]

    left = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    ok, problems = reconcile_filled([pos], [left], results)
    assert not ok and "仍有 1 手" in problems[0]


def test_a_position_outside_the_initial_snapshot_is_never_reported_as_flat():
    """快照被截断/期间有人开了新仓:剩余持仓不在初始清单里,也不能报「已平空」。"""
    from flatten_positions import reconcile_filled

    initial = [_pos("c2601", Exchange.DCE, Direction.LONG, 5)]
    remaining = [_pos("c2701", Exchange.DCE, Direction.SHORT, 1)]
    results = [{"vt_symbol": "c2601.DCE", "direction": "Short", "成交手数": 5}]
    ok, problems = reconcile_filled(initial, remaining, results)
    assert not ok and any("初始快照之外" in p and "c2701.DCE" in p for p in problems)


def test_two_sided_positions_are_reconciled_per_direction():
    """多空同时持有:成交量按(合约, 方向)分别对账,不能互相覆盖(曾报出负的差额)。"""
    from flatten_positions import reconcile_filled

    initial = [_pos("c2601", Exchange.DCE, Direction.LONG, 5), _pos("c2601", Exchange.DCE, Direction.SHORT, 3)]
    results = [{"vt_symbol": "c2601.DCE", "direction": "Short", "成交手数": 5},     # 卖出平多
              {"vt_symbol": "c2601.DCE", "direction": "Long", "成交手数": 3}]     # 买入平空
    ok, problems = reconcile_filled(initial, [], results)
    assert ok and problems == []
    # 空头那 3 手没平掉:必须点名是哪个方向
    ok, problems = reconcile_filled(initial, [], results[:1])
    assert not ok and "c2601.DCE 空" in problems[0]


# ---------- 撤单要等到终态 ----------

class _Order:
    def __init__(self, vt_orderid, active=True):
        self.vt_orderid = vt_orderid
        self._active = active

    def is_active(self):
        return self._active

    def create_cancel_request(self):
        return SimpleNamespace(orderid=self.vt_orderid)


class _Collector:
    """只提供 _cancel_all 用到的两个东西:订单表和等终态。"""

    def __init__(self, orders, final):
        self.lock = threading.Lock()      # 真实锁:with 语句认的是类型上的 __enter__/__exit__
        self.orders = {o.vt_orderid: o for o in orders}
        self._final = final
        self.cancelled = []

    def wait_order_final(self, vt_orderid, timeout=15.0):
        return self._final.get(vt_orderid)


class _Engine:
    def __init__(self, collector):
        self.collector = collector

    def cancel_order(self, req, gateway_name):
        self.collector.cancelled.append(req.orderid)


def test_cancel_waits_for_the_final_status():
    """撤单请求发出 ≠ 撤成功:没等到终态的单要单独报出来(ADR §5)。"""
    from flatten_positions import _cancel_all

    order = _Order("1")
    collector = _Collector([order], {"1": _Order("1", active=False)})   # 撤单后收到终态
    engine = _Engine(collector)
    still_active, failures = _cancel_all(engine, collector)
    assert collector.cancelled == ["1"]
    assert still_active == [] and failures == []


def test_cancel_reports_orders_that_never_reached_a_final_state():
    from flatten_positions import _cancel_all

    order = _Order("1")
    collector = _Collector([order], {})                                # 一直等不到终态
    still_active, failures = _cancel_all(_Engine(collector), collector)
    assert still_active == []                       # 没终态 = 撤单结果不明
    assert failures and "没有收到终态" in failures[0]


def test_cancel_reports_a_failure_without_raising():
    from flatten_positions import _cancel_all

    class _Boom(_Engine):
        def cancel_order(self, req, gateway_name):
            raise RuntimeError("柜台断开")

    order = _Order("1")
    collector = _Collector([order], {})
    still_active, failures = _cancel_all(_Boom(collector), collector)
    assert still_active == [] and failures and "柜台断开" in failures[0]


# ---------- 跑之前的那几道闸门 ----------

def _args(**kw):
    import argparse

    base = dict(profile=".env.guoxin", symbol="c2701.DCE", confirm_flatten=False, confirm_direction=False,
                connect_timeout=1.0, position_wait=0.1, tick_wait=0.1, fill_timeout=0.1)
    base.update(kw)
    return argparse.Namespace(**base)


def _setting(td="121.13.218.19:41285", md="121.13.218.19:41293", environment="测试"):
    return {"用户名": "012345678", "密码": "pw", "经纪商代码": "8030",
            "交易服务器": td, "行情服务器": md, "产品名称": "app", "授权编码": "code",
            "柜台环境": environment}


def test_a_missing_profile_file_stops_the_run(monkeypatch, capsys):
    import flatten_positions as f

    def missing(profile="", root=None):
        raise FileNotFoundError("找不到档案 .env.guoxin")

    monkeypatch.setattr(f, "load_profile", missing)
    assert f.run(_args()) == f.EXIT_CONFIG
    assert "找不到档案" in capsys.readouterr().err


def test_a_conflict_with_the_process_environment_stops_the_run(monkeypatch, capsys):
    """一键流程会把 .env 换成国信档案;清场跑在别的档案上,平的就是另一个账户的仓位。"""
    import flatten_positions as f

    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile({}, ["CTP_TD_ADDRESS: 进程环境里是 x,档案里是 y"]))
    assert f.run(_args()) == f.EXIT_CONFIG
    assert "无法判断该连哪台机器" in capsys.readouterr().err


def test_an_explicit_profile_only_warns_instead_of_stopping(monkeypatch, capsys):
    """档案与进程环境不一致 = 操作员的正常意图(「用这个档案」),只提示不拦。

    拦下来就没法用 `--profile .env.guoxin` 了 —— 那是这个工具的主要用法。
    """
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile(
        {"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285",
         "BROKER_TEST_ALLOWED_MD": "121.13.218.19:41293"},
        notes=["CTP_TD_ADDRESS:生效配置里是 'x',档案里是 'y'(取档案的值)"]))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    monkeypatch.setattr(f, "check_target", lambda environment, address, confirmed, allowed: (False, "闸门拦下"))
    assert f.run(_args()) == f.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "已按档案取值" in err and "无法判断该连哪台机器" not in err


def test_no_trusted_trade_front_whitelist_stops_the_run(monkeypatch, capsys):
    """白名单为空 = 操作员没说清连哪台机器,不能替他决定。"""
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile({"BROKER_TEST_ALLOWED_TD": ""}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    assert f.run(_args()) == f.EXIT_CONFIG
    assert "白名单" in capsys.readouterr().out


def test_an_empty_market_data_whitelist_stops_the_run(monkeypatch, capsys):
    """平仓价来自行情前置的盘口:行情前置没白名单就不能连。"""
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile",
                        lambda profile="", root=None: _profile({"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285"}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    assert f.run(_args()) == f.EXIT_CONFIG
    assert "行情前置白名单为空" in capsys.readouterr().err


def test_a_market_front_outside_the_trusted_table_stops_the_run(monkeypatch, capsys):
    """交易前置在白名单里 ≠ 行情前置也在:两者要分别查。"""
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile(
        {"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285",
         "BROKER_TEST_ALLOWED_MD": "121.13.218.19:41293"}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting(md="8.8.8.8:41293"))
    assert f.run(_args()) == f.EXIT_CONFIG
    assert "行情前置" in capsys.readouterr().err


def test_whitelist_entries_outside_the_trusted_table_are_dropped_with_a_warning(monkeypatch, capsys):
    """白名单与地址同源:不在可信地址表内的条目直接丢弃并说明。"""
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile(
        {"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285,10.0.0.1:41285",
         "BROKER_TEST_ALLOWED_MD": "121.13.218.19:41293"}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    # 停在交易前置闸门(不联网):这里只看白名单被丢弃时的提示
    monkeypatch.setattr(f, "check_target", lambda environment, address, confirmed, allowed: (False, "闸门拦下"))
    assert f.run(_args()) == f.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "10.0.0.1:41285" in err and "已丢弃" in err


# ---------- 跑起来之后的几条路 ----------

class _FakeEventEngine:
    def __init__(self):
        self.handlers = {}

    def register(self, event, handler):
        self.handlers[event] = handler


class _FakeContract:
    def __init__(self, symbol="c2701", exchange=Exchange.DCE, pricetick=1.0):
        self.symbol = symbol
        self.exchange = exchange
        self.pricetick = pricetick


class _FakeMainEngine:
    def __init__(self, event_engine=None, positions=()):
        self.positions = list(positions)
        self.connected = None
        self.subscribed = []

    def add_gateway(self, gateway):
        pass

    def connect(self, setting, gateway_name):
        self.connected = (setting, gateway_name)

    def get_gateway(self, name):
        return SimpleNamespace(td_api=SimpleNamespace(login_status=True, connect_status=True),
                               query_position=lambda: None)

    def get_all_positions(self):
        return list(self.positions)

    def get_contract(self, vt_symbol):
        return _FakeContract()

    def subscribe(self, request, gateway_name):
        self.subscribed.append(request)

    def send_order(self, req, gateway_name):
        return SimpleNamespace(vt_orderid="1")

    def cancel_order(self, req, gateway_name):
        pass


def _install_fakes(monkeypatch, tmp_path, positions=(), positions_after=(), trusted=True, fill=None):
    """把 vnpy 的引擎/网关换成假的:这一组用例不联网、不发单。

    positions:第一次查持仓的结果;positions_after:平仓之后复查的结果。
    fill:回报给 wait_order_final 的终态(None=一直没有回报,"filled"=全部成交,
         "nottraded"=挂着没成交)。
    """
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile(
        {"BROKER_TEST_ALLOWED_TD": "121.13.218.19:41285",
         "BROKER_TEST_ALLOWED_MD": "121.13.218.19:41293"}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    monkeypatch.setattr(f, "check_target", lambda environment, address, confirmed, allowed: (True, "ok"))
    monkeypatch.setattr(f.bootstrap, "ROOT", tmp_path)

    class _FakeCollector:
        def __init__(self):
            self.lock = threading.Lock()
            self.log_lines = ["合约信息查询成功"]
            self.orders = {}
            self.position_rows = []

        def on_log(self, event):
            pass

        def on_order(self, event):
            pass

        def on_tick(self, event):
            pass

        def on_contract(self, event):
            pass

        def on_position(self, event):
            pass

        def on_account(self, event):
            pass

        def mark_positions(self):
            return len(self.position_rows)

        def positions_since(self, mark):
            return list(self.position_rows[mark:])

        def contract_of(self, vt_symbol):
            return _FakeContract()

        def wait_order_final(self, vt_orderid, timeout=15.0):
            if fill == "filled":
                return SimpleNamespace(status=Status.ALLTRADED, traded=3, status_msg="成交",
                                       is_active=lambda: False)
            if fill == "nottraded":
                return SimpleNamespace(status=Status.NOTTRADED, traded=0, status_msg="未成交",
                                       is_active=lambda: True)
            return None

    monkeypatch.setattr(f, "Collector", _FakeCollector)
    monkeypatch.setattr(f, "close_engine", lambda engine: None)
    monkeypatch.setattr(f, "_quotes_from", lambda collector: {"c2701.DCE": (2185.0, 2186.0, 2185.0)})

    calls = {"n": 0}

    def fake_wait_positions(engine, collector, wait):
        calls["n"] += 1
        return (list(positions) if calls["n"] == 1 else list(positions_after), trusted)

    monkeypatch.setattr(f, "_wait_positions", fake_wait_positions)
    monkeypatch.setattr("vnpy.event.EventEngine", _FakeEventEngine)
    monkeypatch.setattr("vnpy.trader.engine.MainEngine", lambda event_engine=None: _FakeMainEngine())
    monkeypatch.setattr("vnpy_ctp.CtpGateway", object)


def test_an_empty_account_needs_no_order(monkeypatch, tmp_path, capsys):
    """账户本来就是空的:什么都不发,退出码 3(=没有持仓,不是失败)。"""
    import flatten_positions as f

    _install_fakes(monkeypatch, tmp_path, positions=[])
    assert f.run(_args(tick_wait=0.0)) == f.EXIT_NO_POSITION
    assert "账户已空" in capsys.readouterr().out


def test_an_unreachable_front_is_not_an_empty_account(monkeypatch, tmp_path, capsys):
    """查不到持仓 → 退出码 4:绝不能当成「账户是空的」。"""
    import flatten_positions as f

    _install_fakes(monkeypatch, tmp_path, positions=[], trusted=False)
    assert f.run(_args(tick_wait=0.0)) == f.EXIT_UNCONFIRMED
    assert "无法确认账户是否空仓" in capsys.readouterr().err


def test_a_dry_run_only_prints_the_plan(monkeypatch, tmp_path, capsys):
    """默认只看不动:没有 --confirm-flatten 就不发任何委托。"""
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos])
    assert f.run(_args(tick_wait=0.0)) == f.EXIT_OK
    out = capsys.readouterr().out
    assert "计划:卖出平仓 c2701.DCE 3 手" in out
    assert "未加 --confirm-flatten" in out


def test_a_short_position_is_flagged_for_a_direction_check(monkeypatch, tmp_path, capsys):
    """柜台报空头 → 计划是「买入平仓」→ 必须点名提示人工核对方向。

    为什么:CTP 网关把**净持仓账户的净多**也报成空头(PosiDirection=净 → Direction.SHORT,
    见 vnpy_ctp 的 DIRECTION_CTP2VT:键 '1' 与 D_Sell 撞车)。那种情况下这单是**加仓**。
    双边持仓账户不受影响,但两者在 vnpy 侧长得一样,代码里分不出来 → 交给人核对。
    """
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.SHORT, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos])
    assert f.run(_args(tick_wait=0.0)) == f.EXIT_OK
    err = capsys.readouterr().err
    assert "买入平仓" in err and "加仓" in err


def test_a_buy_to_close_plan_needs_an_explicit_direction_confirmation(monkeypatch, tmp_path, capsys):
    """「买入平仓」计划 ⇒ 必须再有 `--confirm-direction` 才发单。

    为什么:净持仓账户的**净多**会被 vnpy_ctp 报成空头,那一单是**加仓**而不是平仓。
    这个工具会动真实仓位,一句 stderr 提示不算闸门 —— 读完提示时单子已经在路上
    (2026-11-16 codex 复核 M4)。所以:方向没核对过 ⇒ 拒绝发单,退出码 2。
    """
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.SHORT, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos], positions_after=(), fill="filled")
    assert f.run(_args(confirm_flatten=True, tick_wait=0.0)) == f.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "--confirm-direction" in err
    records = list((tmp_path / "data" / "flatten").glob("flatten-*.json"))
    assert records and json.loads(records[0].read_text(encoding="utf-8"))["结论"].startswith("方向待人工核对")


def test_the_direction_confirmation_lets_the_buy_to_close_plan_through(monkeypatch, tmp_path, capsys):
    """人工核对过方向(`--confirm-direction`)⇒ 照常发单。"""
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.SHORT, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos], positions_after=(), fill="filled")
    assert f.run(_args(confirm_flatten=True, confirm_direction=True, tick_wait=0.0)) == f.EXIT_OK
    out = capsys.readouterr().out
    assert "买入平仓 c2701.DCE 3 手" in out


def test_the_plan_dispatches_on_the_normalized_direction(monkeypatch, tmp_path):
    """方向是字符串时也要能分派(与合并键同源)。

    2026-11-16 codex 复核 L1:合并键用 `normalize_direction`(枚举名),分派却用枚举相等
    ⇒ 一行 `direction` 是 "Long" 的持仓会被合进 LONG 键却在分派时被跳过,留在账户上。
    """
    from flatten_positions import plan_closes
    from vnpy.trader.constant import Exchange as _Ex

    pos = SimpleNamespace(vt_symbol="c2701.DCE", exchange=_Ex.DCE, direction="Long",
                          volume=3, yd_volume=0, frozen=0)
    plans = plan_closes([pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)}, {"c2701.DCE": 1.0})
    assert len(plans) == 1
    assert plans[0].direction == Direction.SHORT          # 平多 = 卖出
    assert plans[0].volume == 3


def test_a_position_without_a_reliable_split_goes_to_a_human(monkeypatch, tmp_path, capsys):
    """上期所的今昨仓拆不出来:不自动发单,交人工(退出码 1 = 需人工处理)。"""
    import flatten_positions as f

    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 3, yd=0)
    pos.vt_symbol = "rb2701.SHFE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos])
    assert f.run(_args(symbol="rb2701.SHFE", tick_wait=0.0)) == f.EXIT_FAILED
    assert "请人工平仓" in capsys.readouterr().err


def test_a_position_with_an_unknown_direction_is_named_for_a_human(monkeypatch, tmp_path, capsys):
    """方向认不出来 / 净持仓:不自动发单,但必须**点名**(2026-11-16 codex 复核 L3)。

    方向为空时这行连合并键都取不到(`position_key` 返回 None)→ 被 `merge_position_rows`
    丢掉 → 既不进平仓计划也不进对账。不点名的话它就「隐形」了:
    账户上明明有仓位,报告却只说「生成不出平仓单」,看的人不知道是哪一笔。
    净持仓(NET)方向是认得出来的,但没有多空可平 —— 自动发单会变成**开仓**,同样必须人工。
    """
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.NET, 3)
    pos.vt_symbol = "c2701.DCE"
    listed = f.manual_positions([pos])
    assert listed and "净持仓" in listed[0]
    assert "c2701.DCE" in listed[0]

    weird = SimpleNamespace(vt_symbol="pp2701.DCE", direction="???", volume=2,
                            yd_volume=0, frozen=0)
    listed2 = f.manual_positions([weird])
    assert listed2 and "方向认不出来" in listed2[0]
    assert "pp2701.DCE" in listed2[0]

    _install_fakes(monkeypatch, tmp_path, positions=[pos])
    assert f.run(_args(symbol="c2701.DCE", tick_wait=0.0)) == f.EXIT_FAILED
    err = capsys.readouterr().err
    assert "净持仓" in err
    assert "c2701.DCE" in err


def test_the_confirmed_run_closes_the_position_and_leaves_a_record(monkeypatch, tmp_path, capsys):
    """--confirm-flatten:真的发平仓单,成交后复查为空,并留下可追溯的记录。"""
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos], positions_after=(), fill="filled")
    assert f.run(_args(confirm_flatten=True, tick_wait=0.0)) == f.EXIT_OK
    out = capsys.readouterr().out
    assert "卖出平仓 c2701.DCE 3 手" in out
    assert "全部成交" in out
    records = list((tmp_path / "data" / "flatten").glob("flatten-*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["结论"] == "已平空"
    assert record["收尾持仓"] == []
    assert record["确认发单"] is True
    assert record["账号"] == "01*****78"          # 账号是掩码,不是明文


def test_a_close_that_does_not_fill_is_still_reported_and_escalated(monkeypatch, tmp_path, capsys):
    """没成交的单 → 仓位还在 → 不能记成「平干净了」,必须升级人工(退出码 1)。"""
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos], positions_after=[pos], fill="nottraded")
    assert f.run(_args(confirm_flatten=True, tick_wait=0.0)) == f.EXIT_FAILED
    record = json.loads(next((tmp_path / "data" / "flatten").glob("flatten-*.json")).read_text(encoding="utf-8"))
    assert "未平干净" in record["结论"]
    assert record["结果"] and record["结果"][0]["成交"] is False


def test_a_position_that_vanished_without_a_fill_is_escalated(monkeypatch, tmp_path, capsys):
    """「单没成交、持仓却没了」在现实中不可能:来历不明,按成交量对账不通过 → 人工。"""
    import flatten_positions as f

    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    pos.vt_symbol = "c2701.DCE"
    _install_fakes(monkeypatch, tmp_path, positions=[pos], positions_after=(), fill="nottraded")
    assert f.run(_args(confirm_flatten=True, tick_wait=0.0)) == f.EXIT_FAILED
    record = json.loads(next((tmp_path / "data" / "flatten").glob("flatten-*.json")).read_text(encoding="utf-8"))
    assert "成交量对不上" in record["结论"]
    assert record["对账问题"] and "只成交了 0 手" in record["对账问题"][0]


# ---------- 集成用例:真的 .env + 真的档案(不 mock 解析器) ----------

def test_a_real_profile_file_is_not_blocked_by_a_stale_process_environment(monkeypatch, tmp_path, capsys):
    """codex 指出的 Critical-1:指定档案时,进程环境里的残留值**不能**把工具挡住。

    挡住的话 `--profile .env.guoxin` 这个主要用法根本没法用。
    这一条走**真的** `ctp_tools.profile.load_profile`(不 mock 解析器),
    只把最后「连不连」那道闸门换掉,免得用例去连国信的机器。
    """
    import flatten_positions as f

    (tmp_path / ".env").write_text(
        "CTP_TD_ADDRESS=10.0.0.1:41285\nCTP_BROKER_ID=8030\n", encoding="utf-8")
    (tmp_path / ".env.guoxin").write_text(
        "CTP_TD_ADDRESS=121.13.218.19:41285\nCTP_MD_ADDRESS=121.13.218.19:41293\n"
        "CTP_BROKER_ID=8030\nCTP_USERNAME=012345678\nCTP_PASSWORD=pw\n"
        "CTP_APP_ID=app\nCTP_AUTH_CODE=code\nCTP_ENVIRONMENT=测试\n"
        "BROKER_TEST_ALLOWED_TD=121.13.218.19:41285\n"
        "BROKER_TEST_ALLOWED_MD=121.13.218.19:41293\n", encoding="utf-8")
    monkeypatch.setenv("CTP_TD_ADDRESS", "9.9.9.9:9")        # 上一次运行残留的进程环境变量
    monkeypatch.setattr(f.bootstrap, "ROOT", tmp_path)
    monkeypatch.setattr(f, "ISOLATED", True)

    seen = {}

    def capture(env):
        seen.update(env)
        return _setting()

    monkeypatch.setattr(f, "build_ctp_setting", capture)
    monkeypatch.setattr(f, "check_target", lambda environment, address, confirmed, allowed: (False, "闸门拦下"))
    assert f.run(_args(profile=".env.guoxin")) == f.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "无法判断该连哪台机器" not in err          # 没有硬冲突
    assert "已丢弃" not in err                        # 白名单里的地址都在可信表内
    assert seen["CTP_TD_ADDRESS"] == "121.13.218.19:41285"   # 档案的值生效(不是 .env,也不是进程环境)


def test_the_whitelist_is_bound_to_the_broker_id(monkeypatch, tmp_path, capsys):
    """国信档案(BrokerID 8030)不能把 SimNow 的地址写进白名单。

    两个环境是**不同的账户、不同的柜台**:白名单能混搭,清场就可能平错账户的仓位。
    """
    import flatten_positions as f

    monkeypatch.setattr(f, "ISOLATED", True)
    monkeypatch.setattr(f, "load_profile", lambda profile="", root=None: _profile(
        {"BROKER_TEST_ALLOWED_TD": "182.254.243.31:30001,121.13.218.19:41285",
         "BROKER_TEST_ALLOWED_MD": "121.13.218.19:41293"}))
    monkeypatch.setattr(f, "build_ctp_setting", lambda env: _setting())
    monkeypatch.setattr(f, "check_target", lambda environment, address, confirmed, allowed: (False, "闸门拦下"))
    assert f.run(_args()) == f.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "182.254.243.31:30001" in err and "已丢弃" in err


def test_duplicate_position_rows_produce_only_one_close_order():
    """同一窗口落进两次快照时,只能出一张平仓单。

    2026-11-16 实测:SimNow 采集窗口里落进两行 `c2701.DCE Long 3`
    (我们主动查的那次 + 网关约每 4 秒自动查的那次),
    平仓计划照抄就是**两张 3 手平多单** —— 3 手持仓被平 6 手,超平会被柜台整单拒
    (错误码 30「CTP:平仓量超过持仓量」)。
    """
    pos = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    plans = plan_closes([pos, pos], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].volume == 3
    assert plans[0].direction == Direction.SHORT


def test_a_shrinking_snapshot_does_not_plan_the_stale_volume():
    """两次快照之间持仓变小:按最后一次快照算,不能按更旧、更大的那个数平。"""
    stale = _pos("c2701", Exchange.DCE, Direction.LONG, 3)
    fresh = _pos("c2701", Exchange.DCE, Direction.LONG, 1)
    plans = plan_closes([stale, fresh], {"c2701.DCE": (2185.0, 2186.0, 2185.0)})
    assert len(plans) == 1
    assert plans[0].volume == 1


def test_the_manual_list_uses_the_merged_rows_not_the_raw_snapshot():
    """同一 (合约, 方向) 出现两行(网关自动查 + 我们主动查)时,人工清单只列**合并后**那一行。

    2026-11-16 codex 复核 L3:人工清单原先直接遍历原始行 ⇒ 重复列两行,
    而且手数取到较旧的那次快照(与 `merge_position_rows` 的口径相反)。
    """
    import flatten_positions as f

    older = _pos("c2701", Exchange.DCE, Direction.NET, 5)
    older.vt_symbol = "c2701.DCE"
    newer = _pos("c2701", Exchange.DCE, Direction.NET, 2)
    newer.vt_symbol = "c2701.DCE"

    listed = f.manual_positions([older, newer])
    assert len(listed) == 1, listed
    assert "2 手" in listed[0], listed[0]


def test_a_position_without_a_direction_is_still_named_for_a_human():
    """方向为**空**的行连合并键都取不到(`position_key` 返回 None)⇒ 合并层会把它丢掉。

    2026-11-16 codex 复核 L3:人工清单改成「用合并后的行」之后,这类行如果不做特殊处理
    就会既不进计划、也不进人工清单 —— 账户上明明有仓位,提示里却一个字都没有。
    """
    import flatten_positions as f

    blank = SimpleNamespace(vt_symbol="pp2701.DCE", direction=None, volume=2, yd_volume=0, frozen=0)
    listed = f.manual_positions([blank])
    assert listed and "方向认不出来" in listed[0], listed
    assert "pp2701.DCE" in listed[0]


def test_an_unknown_direction_never_leaks_an_internal_enum_name():
    """方向认不出来时显示「无方向」,不把内部枚举名漏给看报告的人。

    2026-11-16 codex 复核 L7:`probe_position` 早就改对了,`manual_positions` 的
    「今昨仓拆不出来」那一支还在用 `direction_text(pos.direction)` —— 兜底会把
    认不出来的方向**原样**返回(大写原文),报告里就会冒出 `FOO` 这种内部名字。
    """
    from vnpy.trader.constant import Direction

    pos = _pos("rb2701", Exchange.SHFE, Direction.LONG, 5, yd=2)     # 今昨混合 ⇒ 走「拆不出来」那一支
    pos.direction = "SOME_INTERNAL_NAME"                              # 认不出来的方向
    manual = manual_positions([pos])
    assert manual, manual
    assert "无方向" in manual[0], manual[0]
    assert "SOME_INTERNAL_NAME" not in manual[0], manual[0]
