"""前置地址白名单只能收窄不能放宽;交易通道判活必须看 login_status。

这两个都是 codex 第二轮审查指出的高危项,单独成文件,方便回看。
"""
from types import SimpleNamespace

from ctp_tools.broker_targets import (
    GUOXIN_TEST_BROKER_ID,
    GUOXIN_TEST_MD,
    GUOXIN_TEST_TD,
    KNOWN_MD,
    KNOWN_TD,
    SIMNOW_BROKER_ID,
    SIMNOW_MD,
    SIMNOW_TD,
    allowed_for_broker,
    resolve_allowed,
    resolve_allowed_for_broker,
)
from ctp_tools.collector import trade_live


def test_whitelist_can_only_narrow_not_widen():
    """白名单与 CTP_TD_ADDRESS 同源,采信它等于自己给自己发通行证。

    所以白名单只能取「可信地址全集」的子集:不在全集里的地址,
    哪怕同时写进地址和白名单,也不放行。
    """
    allowed, dropped = resolve_allowed("10.0.0.1:41285", KNOWN_TD)
    assert allowed == frozenset()
    assert dropped == ["10.0.0.1:41285"]


def test_whitelist_may_select_a_subset_of_the_trusted_addresses():
    allowed, dropped = resolve_allowed("121.13.218.19:41285,120.86.114.57:41285", KNOWN_TD)
    assert allowed == GUOXIN_TEST_TD and dropped == []


def test_no_whitelist_means_nothing_is_allowed():
    """「操作员明确说出连哪台机器」这一步不能省:代码不能替他决定。"""
    allowed, dropped = resolve_allowed("", KNOWN_TD)
    assert allowed == frozenset() and dropped == []


def test_address_normalisation_happens_before_matching():
    allowed, _ = resolve_allowed("tcp://121.13.218.19:41285", KNOWN_TD)
    assert allowed == frozenset({"121.13.218.19:41285"})


def test_trusted_tables_contain_no_production_fronts():
    """可信地址全集里只有期货公司测试前置与 SimNow,不能混进别的东西。"""
    assert KNOWN_TD == GUOXIN_TEST_TD | set(SIMNOW_TD)
    assert KNOWN_MD == GUOXIN_TEST_MD | set(SIMNOW_MD)
    assert all(":41285" in a or ":3000" in a or ":4000" in a for a in KNOWN_TD)


def test_simnow_tables_come_from_one_place():
    """SimNow 的交易/行情地址表只在 ctp_tools/simnow.py 里写一份。"""
    from ctp_tools.simnow import SIMNOW_MD_ADDRESSES, SIMNOW_TD_ADDRESSES

    assert SIMNOW_TD == SIMNOW_TD_ADDRESSES and SIMNOW_MD == SIMNOW_MD_ADDRESSES


class _Gateway:
    def __init__(self, connect_status=True, login_status=True):
        self.td_api = SimpleNamespace(connect_status=connect_status, login_status=login_status)


class _Engine:
    def __init__(self, connect_status=True, login_status=True):
        self.gateway = _Gateway(connect_status, login_status)

    def get_gateway(self, name):
        return self.gateway


def test_liveness_follows_login_status_not_connect_status():
    """vnpy_ctp 断线只清 login_status,connect_status 一直是 True。

    用 connect_status 判活的话,断线之后查询照样「成功」,返回空持仓——
    空仓闸门就成了摆设:明明连不上柜台,却被判成「账户是空的」。
    """
    assert trade_live(_Engine(connect_status=True, login_status=False)) is False
    assert trade_live(_Engine(connect_status=True, login_status=True)) is True


def test_liveness_is_false_when_login_status_is_absent():
    """拿不到 login_status = 不知道通道状态 → 判不在线(失败关闭)。

    退回 connect_status 会 fail-open:断线后它恒为 True。
    """
    engine = _Engine()
    del engine.gateway.td_api.login_status
    assert trade_live(engine) is False


# ---------- 白名单按 BrokerID 绑定(M1) ----------

def test_the_address_table_is_bound_to_the_broker_id():
    """国信档案只认国信前置,SimNow 档案只认 SimNow 前置。

    两个环境是**不同的账户、不同的柜台**:白名单能混搭,
    清场就可能平到不是取证那个账户的仓位上。
    """
    from ctp_tools.broker_targets import allowed_for_broker

    td, md = allowed_for_broker(GUOXIN_TEST_BROKER_ID)
    assert td == GUOXIN_TEST_TD and md == GUOXIN_TEST_MD
    td, md = allowed_for_broker(SIMNOW_BROKER_ID)
    assert td == SIMNOW_TD and md == SIMNOW_MD
    # 未知 BrokerID → 两张表都是空(没有「默认放行」)
    assert allowed_for_broker("12345") == (frozenset(), frozenset())


def test_a_guoxin_profile_cannot_whitelist_a_simnow_front():
    from ctp_tools.broker_targets import resolve_allowed_for_broker

    td, md, dropped_td, _ = resolve_allowed_for_broker(
        GUOXIN_TEST_BROKER_ID, "182.254.243.31:30001,121.13.218.19:41285", "121.13.218.19:41293")
    # 白名单只能**收窄**:配置里写的 SimNow 前置被丢弃,只留下国信前置
    assert td == frozenset({"121.13.218.19:41285"})
    assert dropped_td == ["182.254.243.31:30001"]
    assert md == frozenset({"121.13.218.19:41293"})


# ---------- 持仓取「本次查询回报」,不取 OMS 快照 ----------

class _QueryCollector:
    """只提供查询回报的缓冲区。"""

    def __init__(self):
        self.rows = []

    def mark_positions(self):
        return len(self.rows)

    def positions_since(self, mark):
        return list(self.rows[mark:])


class _QueryEngine:
    """假引擎:query_position 把**当前**的持仓推进采集器;OMS 快照可以是过期的。"""

    def __init__(self, current=(), stale=(), live=True):
        self.current = list(current)
        self.stale = list(stale)
        self.live = live
        self.td_api = SimpleNamespace(connect_status=True, login_status=live)
        self.collector = None

    def get_gateway(self, name):
        collector = self.collector
        current = list(self.current)

        class _Gw:
            td_api = SimpleNamespace(connect_status=True, login_status=self.live)

            def query_position(self):
                if collector is not None:
                    collector.rows.extend(current)

        return _Gw()

    def get_all_positions(self):
        return list(self.stale)


def _pos(symbol="c2701.DCE", volume=1):
    return SimpleNamespace(vt_symbol=symbol, direction=SimpleNamespace(value="多"), volume=volume)


def test_the_position_query_reports_the_current_state_not_the_stale_oms_cache():
    """vn.py 的 OMS **从不删除**条目:平掉的仓位会永远留在缓存里。

    按 OMS 快照判断空仓,平仓成功后仍然会说「账户有持仓」——
    取证运行器后面所有发单步骤都会被闸门挡住。
    """
    from ctp_tools.collector import query_positions

    engine = _QueryEngine(current=[], stale=[_pos()])      # OMS 里还留着旧仓位
    collector = _QueryCollector()
    engine.collector = collector
    rows, status = query_positions(engine, collector, wait=0.2, quiet=0.05)
    assert status == "empty"          # 查询回报里没有行 = 账户确实空着
    assert rows == []


def test_the_position_query_returns_the_rows_the_broker_reported():
    from ctp_tools.collector import query_positions

    engine = _QueryEngine(current=[_pos(volume=3)])
    collector = _QueryCollector()
    engine.collector = collector
    rows, status = query_positions(engine, collector, wait=0.5, quiet=0.05)
    assert status == "ok" and len(rows) == 1 and rows[0].volume == 3


def test_an_offline_channel_makes_the_position_query_untrusted():
    """通道不在线时「没有行」不能当成「没有持仓」。"""
    from ctp_tools.collector import query_positions

    engine = _QueryEngine(current=[], live=False)
    collector = _QueryCollector()
    engine.collector = collector
    rows, status = query_positions(engine, collector, wait=0.2, quiet=0.05)
    assert status == "untrusted" and rows == []


# ---------- 「没有行」要有柜台应答作证 ----------

class _SilentCounterEngine:
    """在线,但柜台不应答任何查询(流控拒绝 = 零行零日志)。"""

    def __init__(self, replies: bool):
        self.replies = replies
        self.collector = None
        self.td_api = SimpleNamespace(connect_status=True, login_status=True)
        self.gateway = SimpleNamespace(td_api=self.td_api, query_position=lambda: None,
                                       query_account=self._query_account)

    def _query_account(self):
        if self.replies and self.collector is not None:
            self.collector.on_account(None)

    def get_gateway(self, name):
        return self.gateway


class _ProbeCollector(_QueryCollector):
    def __init__(self):
        super().__init__()
        import threading

        self.lock = threading.Lock()
        self.account_count = 0

    def on_account(self, event):
        with self.lock:
            self.account_count += 1


def test_no_rows_with_a_silent_counter_is_untrusted_not_empty(real_probe, monkeypatch):
    """柜台流控拒掉持仓查询时零行零日志,看起来和空仓一模一样 —— 不能放行发单。"""
    from ctp_tools import collector as c

    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    engine = _SilentCounterEngine(replies=False)
    collector = _ProbeCollector()
    engine.collector = collector
    rows, status = c.query_positions(engine, collector, wait=0.05, quiet=0.01)
    assert (rows, status) == ([], "untrusted")


def test_no_rows_with_a_replying_counter_is_empty(real_probe, monkeypatch):
    from ctp_tools import collector as c

    monkeypatch.setattr(c.time, "sleep", lambda s: None)
    engine = _SilentCounterEngine(replies=True)
    collector = _ProbeCollector()
    engine.collector = collector
    rows, status = c.query_positions(engine, collector, wait=0.05, quiet=0.01)
    assert (rows, status) == ([], "empty")


def test_a_collector_that_cannot_prove_the_counter_replies_is_untrusted(real_probe):
    from ctp_tools import collector as c

    engine = _SilentCounterEngine(replies=True)
    assert c._probe_counter_replies(engine, _QueryCollector()) is False     # 没有 account_count
