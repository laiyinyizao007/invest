"""持仓回报的处理:两次快照落到同一个窗口时只能按**最后一次**算。

背景(机制,不是"柜台推两遍"):CTP 网关有一个自动查询,大约每 4 秒发一次持仓查询
(`vnpy_ctp/ctp_gateway.py` 的 `process_timer_event` 在 `query_account` 与
`query_position` 之间轮转)。采集器自己也会主动查一次持仓,于是同一个采集窗口里
常常落进**两次快照**。合并口径必须是"取最后一次快照",而不是求和、也不是取最大。

求和 = 平 6 手 = 超平 = 被柜台整单拒(错误码 30)。取最大 = 平仓量偏大 = 同样超平。

后一批快照里**消失**的合约 = 已经被平掉了(柜台对已平合约不再推持仓事件),
所以还要挑出"最新一批",免得去平一个不存在的持仓。
"""
from types import SimpleNamespace

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.logger import ERROR
from vnpy.trader.object import LogData, PositionData

from ctp_tools.collector import (
    Collector, merge_position_rows, position_key, query_positions, select_latest_snapshot,
)


def _pos(symbol: str, direction: Direction, volume: int) -> PositionData:
    pos = PositionData(symbol=symbol, exchange=Exchange.DCE, direction=direction, gateway_name="CTP")
    pos.volume = volume
    return pos


class _FakeGateway:
    def __init__(self) -> None:
        self.queried = 0

    def query_position(self) -> None:
        self.queried += 1


class _FakeEngine:
    def __init__(self, gateway) -> None:
        self._gateway = gateway

    def get_gateway(self, name: str):
        return self._gateway


def test_identical_rows_are_merged_into_one():
    """两次快照量相同:只留一行。"""
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2701", Direction.LONG, 3)]
    merged = merge_position_rows(rows)
    assert len(merged) == 1
    assert merged[0].volume == 3


def test_merging_does_not_add_up_the_volumes():
    """3 手持仓被采到两遍,平仓量必须是 3 而不是 6。

    求和 = 平 6 手 = 超平 = 柜台整单拒(错误码 30),平仓根本发不出去。
    """
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2701", Direction.LONG, 3)]
    assert sum(p.volume for p in merge_position_rows(rows)) == 3


def test_the_latest_snapshot_wins():
    """两次快照量不同:取**最后一次**的量。

    两次快照之间如果平过仓,后一次才是当前真相;取最大会把已经平掉的部分再平一遍
    (超平,柜台整单拒)。
    """
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2701", Direction.LONG, 5)]
    merged = merge_position_rows(rows)
    assert len(merged) == 1
    assert merged[0].volume == 5


def test_a_shrinking_snapshot_is_not_inflated():
    """持仓从 3 手缩到 1 手:只能按 1 手算,不能按 3 手更不能按 4 手。"""
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2701", Direction.LONG, 1)]
    merged = merge_position_rows(rows)
    assert len(merged) == 1
    assert merged[0].volume == 1
    assert sum(p.volume for p in merged) == 1


def test_different_directions_are_not_merged():
    """多空双向持仓不能互相覆盖(净持仓账户两边都有)。"""
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2701", Direction.SHORT, 2)]
    merged = merge_position_rows(rows)
    assert len(merged) == 2


def test_different_contracts_are_not_merged():
    rows = [_pos("c2701", Direction.LONG, 3), _pos("c2601", Direction.LONG, 1)]
    assert len(merge_position_rows(rows)) == 2


def test_empty_input_stays_empty():
    assert merge_position_rows([]) == []


def test_rows_without_a_usable_key_are_dropped():
    """认不出键的行不能合并(也不能安全地平),直接丢掉。

    宁可漏掉一行,也不要凭空造一个键去平仓。
    """
    good = _pos("c2701", Direction.LONG, 3)
    broken = SimpleNamespace(vt_symbol="", direction=None, volume=9)
    merged = merge_position_rows([good, broken])
    assert len(merged) == 1
    assert merged[0].volume == 3


def test_query_positions_returns_merged_rows(monkeypatch):
    """走完整条查询路径:两次快照落到一个窗口,调用方只拿到一行。"""
    collector = Collector()
    gateway = _FakeGateway()
    engine = _FakeEngine(gateway)

    def fake_query() -> None:
        gateway.queried += 1
        # 模拟同一窗口里落进两次快照(我们主动查一次 + 网关自动查一次)
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))

    monkeypatch.setattr(gateway, "query_position", fake_query)
    monkeypatch.setattr("ctp_tools.collector.trade_live", lambda *a, **k: True)

    rows, status = query_positions(engine, collector, wait=2.0, quiet=0.2)
    assert status == "ok"
    assert len(rows) == 1
    assert rows[0].volume == 3
    assert gateway.queried == 1


def test_query_positions_records_the_raw_row_count(monkeypatch):
    """原始行数要留痕:合并后只剩 1 行,没有原始计数就看不出发生过几次快照。"""
    collector = Collector()
    gateway = _FakeGateway()
    engine = _FakeEngine(gateway)

    def fake_query() -> None:
        gateway.queried += 1
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))

    monkeypatch.setattr(gateway, "query_position", fake_query)
    monkeypatch.setattr("ctp_tools.collector.trade_live", lambda *a, **k: True)

    query_positions(engine, collector, wait=2.0, quiet=0.2)
    assert collector.position_rows_raw == 2


def test_a_single_batch_is_the_latest_snapshot():
    """只有一批回报:那就是最新快照,全要。"""
    rows = [_pos("c2701", Direction.LONG, 3)]
    assert select_latest_snapshot([rows]) == rows


def test_no_batches_means_no_rows():
    """一批都没有(窗口里没收到任何回报)→ 没有行,不能凭空造。"""
    assert select_latest_snapshot([]) == []


def test_a_contract_missing_from_the_newer_snapshot_counts_as_closed():
    """后一批快照里没有的合约 = 已经平掉了,不能再留着去平。

    柜台对**已平掉**的合约不再推持仓事件,所以后一批快照的键集合是前一批的子集。
    这时必须只认后一批,否则会去平一个不存在的持仓(柜台整单拒,错误码 30)。
    """
    old = [_pos("c2701", Direction.LONG, 3), _pos("c2601", Direction.LONG, 1)]
    new = [_pos("c2701", Direction.LONG, 3)]
    assert select_latest_snapshot([old, new]) == new


def test_a_new_key_in_the_last_batch_keeps_every_row():
    """最后一批冒出了**新键** → 判不出来(可能是同一次回报被切成两批)→ 保守保留全部。

    宁可多留一行(多平一手会被柜台拒),也不能少平一手。
    """
    old = [_pos("c2701", Direction.LONG, 3)]
    new = [_pos("c2601", Direction.LONG, 1)]
    merged = select_latest_snapshot([old, new])
    assert len(merged) == 2


def test_a_response_split_into_three_batches_keeps_every_row():
    """同一次回报被切成三批时,不能把还在的持仓当成「已平掉」丢掉。

    一次回报内部的键互不相同,所以被切开的几批键**不相交** → 判不出「子集」→ 全留。
    (若拿前面所有批的并集去比,这里会误判成「最后一批 ⊆ 并集」→ 丢掉还在的合约。)
    """
    first = [_pos("c2701", Direction.LONG, 3), _pos("c2601", Direction.LONG, 1)]
    middle = [_pos("c2701", Direction.LONG, 3)]
    last = [_pos("c2601", Direction.LONG, 1)]
    merged = merge_position_rows(select_latest_snapshot([first, middle, last]))
    assert sorted(p.vt_symbol for p in merged) == ["c2601.DCE", "c2701.DCE"]


def test_an_empty_last_batch_keeps_the_earlier_rows():
    """空快照(账户已空)推不出任何行 → 看不出来的盲区 → 保守保留。

    这条盲区由调用方的「轮询到空为止」兜住(每次查询重新记 mark)。
    """
    old = [_pos("c2701", Direction.LONG, 3)]
    assert select_latest_snapshot([old, []]) == old


def test_position_key_treats_a_bare_direction_string_the_same_as_the_enum():
    """方向统一取 `direction.value`,裸字符串与枚举归到同一个键。"""
    assert position_key(_pos("c2701", Direction.LONG, 3)) == ("c2701.DCE", "Long")
    row = SimpleNamespace(vt_symbol="c2701.DCE", direction="Long", volume=3)
    assert position_key(row) == ("c2701.DCE", "Long")
    assert position_key(SimpleNamespace(vt_symbol="", direction=None)) is None


def test_batches_are_split_by_the_gap_between_reports():
    """一次回报的行连续到达,两次回报之间有间隔 → 按间隔分批。"""
    collector = Collector()
    clock = [0.0]
    collector.now = lambda: clock[0]
    for row, t in ((_pos("c2701", Direction.LONG, 3), 0.0),
                   (_pos("c2601", Direction.LONG, 1), 0.02),
                   (_pos("c2701", Direction.LONG, 3), 1.5)):
        clock[0] = t
        collector.on_position(SimpleNamespace(data=row))
    batches = collector.position_batches_since(0)
    assert [len(b) for b in batches] == [2, 1]
    assert batches[0][0].vt_symbol == "c2701.DCE"


def test_query_positions_drops_a_contract_closed_between_the_two_snapshots(monkeypatch):
    """走完整条查询路径:窗口里落进两批,平掉的合约不该出现在结果里。"""
    collector = Collector()
    gateway = _FakeGateway()
    engine = _FakeEngine(gateway)
    clock = [0.0]
    collector.now = lambda: clock[0]

    def fake_query() -> None:
        gateway.queried += 1
        clock[0] = 0.0
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))
        clock[0] = 0.02
        collector.on_position(SimpleNamespace(data=_pos("c2601", Direction.LONG, 1)))
        clock[0] = 1.5                       # 新的一次回报:c2601 已经平掉,不再出现
        collector.on_position(SimpleNamespace(data=_pos("c2701", Direction.LONG, 3)))

    monkeypatch.setattr(gateway, "query_position", fake_query)
    monkeypatch.setattr("ctp_tools.collector.trade_live", lambda *a, **k: True)

    rows, status = query_positions(engine, collector, wait=2.0, quiet=0.2)
    assert status == "ok"
    assert [r.vt_symbol for r in rows] == ["c2701.DCE"]
    assert collector.position_rows_raw == 3


def test_rows_appended_without_a_timestamp_are_never_dropped(monkeypatch):
    """绕过 `on_position` 直接塞进 `position_rows` 的行**不能丢**。

    丢行 = 以为账户是空仓 = 会去发单(fail-open)。所以拿不到到达时刻就整段当一批,
    退回旧行为(行全在)。
    """
    collector = Collector()
    gateway = _FakeGateway()
    engine = _FakeEngine(gateway)

    def fake_query() -> None:
        gateway.queried += 1
        collector.position_rows.append(
            SimpleNamespace(vt_symbol="c2701.DCE", direction=Direction.LONG, volume=2))

    monkeypatch.setattr(gateway, "query_position", fake_query)
    monkeypatch.setattr("ctp_tools.collector.trade_live", lambda *a, **k: True)

    rows, status = query_positions(engine, collector, wait=2.0, quiet=0.2)
    assert status == "ok"
    assert [r.vt_symbol for r in rows] == ["c2701.DCE"]
    assert rows[0].volume == 2


def test_only_gateway_logs_count_as_counter_errors():
    """本地风控的 ERROR 日志不是柜台错误。

    2026-11-16 夜盘实测:P4.2 取到的「柜台错误提示」其实是本地风控的告警文案
    (trading/guard_rule.py 的 `_alert` 就写成 ERROR 级别)。
    判据与 `trading/guard_rule.py:_on_log` 一致:只收交易网关(CTP)的日志。
    """
    collector = Collector()
    collector.on_log(Event(EVENT_LOG, LogData(msg="风控阈值触发:汇总委托笔数达到上限",
                                              level=ERROR, gateway_name="")))
    assert collector.errors == []
    assert collector.log_lines == ["风控阈值触发:汇总委托笔数达到上限"]      # 仍然留痕,只是不算柜台错误

    collector.on_log(Event(EVENT_LOG, LogData(msg="交易委托失败，代码：30，信息：CTP:平仓量超过持仓量",
                                              level=ERROR, gateway_name="CTP")))
    assert len(collector.errors) == 1
    assert "平仓量超过持仓量" in collector.errors[0][1]

    # 网关回调线程里的异常由事件引擎写成日志,**没有网关名**——那也是柜台侧的证据,
    # 不能被「只收交易网关」一起滤掉(否则 vnpy_ctp 的 KeyError 就查不到了)。
    collector.on_log(Event(EVENT_LOG, LogData(msg="KeyError: 'c9999'\nAt: ctp_gateway.py(505): onRspOrderInsert",
                                              level=ERROR, gateway_name="")))
    assert len(collector.errors) == 2
    assert "KeyError" in collector.errors[1][1]
