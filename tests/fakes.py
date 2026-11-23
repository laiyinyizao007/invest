"""离线集成测试用的假件:同步事件引擎 + 可编程行为的假网关,不依赖 CTP、不联网。"""
import copy
from datetime import datetime, timedelta

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Interval, Offset, Product, Status
from vnpy.trader.event import EVENT_LOG
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    AccountData,
    BarData,
    CancelRequest,
    ContractData,
    LogData,
    OrderRequest,
    PositionData,
    SubscribeRequest,
    TickData,
    TradeData,
)
from vnpy.trader.logger import ERROR, INFO


class SyncEventEngine(EventEngine):
    """无线程、无定时器的确定性事件引擎:测试结果确定,不会因调度而偶发失败。

    保持真实引擎的顺序语义:处理函数运行期间 put 进来的事件先排队,等当前处理函数返回后再依次处理
    (否则废单回报会"嵌套"跑到发单函数返回之前,与真实行为不一致)。
    """

    def __init__(self) -> None:
        super().__init__(interval=3600)
        self._backlog: list[Event] = []
        self._busy = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def put(self, event: Event) -> None:
        self._backlog.append(event)
        if self._busy:
            return
        self._busy = True
        try:
            while self._backlog:
                self._process(self._backlog.pop(0))
        finally:
            self._busy = False


class FakeGateway(BaseGateway):
    """mode: accept(挂单不成交) / fill(立即全部成交) / partial(部分成交,余量还挂着) /
    reject(废单并输出错误日志) / silent(不产生任何回报) / disconnected(模拟断连:发单本地快速失败)。

    复现真实 CTP 的三个行为(codex 审查指出过,曾被乐观的假件掩盖):
    - 废单时先推送委托回报(REJECTED),之后才写错误日志(error_after_order);
    - 成交后持仓回报滞后:OMS 里的持仓要等 query_position() 才会刷新(position_lag);
    - 断连期间 reqOrderInsert 本地失败:返回空串、不产生任何 OrderData、只写一条 INFO 日志,
      框架不缓存该订单——重连后不会"一窝蜂"补发(见 tests/test_disconnect_signal.py)。
    """

    default_name = "FAKE"
    default_setting: dict = {}
    exchanges = [Exchange.DCE]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        super().__init__(event_engine, gateway_name)
        self.mode = "accept"
        self.reject_text = "CTP:资金不足"
        self.sent: list[OrderRequest] = []
        self.cancel_requests: list[CancelRequest] = []
        self.orders: dict[str, object] = {}
        self.positions: dict[Direction, float] = {Direction.LONG: 0.0, Direction.SHORT: 0.0}
        self.error_after_order = True
        self.position_lag = True
        self.cancel_works = True
        self.position_queries = 0
        self._seq = 0

    # ---- BaseGateway 接口 ----
    def connect(self, setting: dict) -> None:
        """记录连接参数;connect_pushes=True 时模拟登录成功后推送合约与账户(像 CTP 登录流程那样)。"""
        self.connected_with = setting
        self.connect_count = getattr(self, "connect_count", 0) + 1
        if getattr(self, "connect_pushes", True):
            self.push_contract()
            self.push_account(getattr(self, "initial_balance", 10_000.0))

    def close(self) -> None:
        self.closed = True

    def subscribe(self, req: SubscribeRequest) -> None:
        pass

    def query_account(self) -> None:
        pass

    def query_position(self) -> None:
        self.position_queries += 1
        self.push_positions()

    def send_order(self, req: OrderRequest) -> str:
        if self.mode == "disconnected":
            # 复现 CtpTdApi 断连时的 send_order:reqOrderInsert 本地失败 → 返回空串、无 OrderData、
            # 只写 INFO 日志(措辞对齐 vnpy_ctp)。订单不进任何队列,重连后无从补发。
            self.on_event(EVENT_LOG, LogData(
                msg="委托请求发送失败，错误代码：-3，信息：CTP:未处理请求队列已满(断连模拟)",
                level=INFO, gateway_name=self.gateway_name))
            return ""
        self._seq += 1
        order = req.create_order_data(str(self._seq), self.gateway_name)
        self.sent.append(req)
        self.orders[order.orderid] = order
        if self.mode == "silent":
            return order.vt_orderid
        if self.mode == "reject":
            order.status = Status.REJECTED
            if not self.error_after_order:
                self._log_reject()
            self.on_order(copy.copy(order))
            if self.error_after_order:
                self._log_reject()
            return order.vt_orderid
        elif self.mode == "fill":
            order.status, order.traded = Status.ALLTRADED, order.volume
        elif self.mode == "partial":
            # 部分成交:成交了一部分,余量还挂着 —— 撤单也撤不掉(整单撤单对部分成交单不成立)。
            # 这个形态的后果**有两个**:账户已经多出一部分持仓 + 余量可能还挂在柜台上,
            # 报告里两个都要说(2026-11-16 codex 复核 M6)。
            order.status, order.traded = Status.PARTTRADED, max(1, order.volume // 2)
        else:
            order.status = Status.NOTTRADED
        self.on_order(copy.copy(order))
        if self.mode == "fill":
            self._apply_fill(order)
        elif self.mode == "partial":
            self._apply_fill(order, volume=order.traded)
        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        self.cancel_requests.append(req)
        order = self.orders.get(req.orderid)
        if self.cancel_works and self.mode != "partial" and order is not None and order.is_active():
            order.status = Status.CANCELLED
            self.on_order(copy.copy(order))

    # ---- 测试辅助 ----
    def _log_reject(self) -> None:
        # 级别用 INFO 而不是 ERROR:真实 vnpy_ctp 的 write_error() 实际调用 write_log(),
        # 拒单是以 INFO 级别写出来的(2026-11-10 在 SimNow 上实测确认)。
        # 假网关若用 ERROR,就会掩盖"只认 ERROR 级别会漏掉全部拒单文本"这个真实缺陷。
        self.on_event(EVENT_LOG, LogData(msg=f"交易委托失败,信息:{self.reject_text}", level=INFO, gateway_name=self.gateway_name))

    def _apply_fill(self, order, volume: float | None = None) -> None:
        volume = order.volume if volume is None else volume
        self.on_trade(TradeData(
            symbol=order.symbol, exchange=order.exchange, orderid=order.orderid, tradeid=f"T{order.orderid}",
            direction=order.direction, offset=order.offset, price=order.price, volume=volume,
            datetime=datetime.now(), gateway_name=self.gateway_name,
        ))
        if order.offset == Offset.OPEN:
            self.positions[order.direction] += volume
        else:                                  # 平仓:减少相反方向的持仓
            opposite = Direction.SHORT if order.direction == Direction.LONG else Direction.LONG
            self.positions[opposite] = max(0.0, self.positions[opposite] - volume)
        if not self.position_lag:
            self.push_positions(order.symbol, order.exchange)

    def push_positions(self, symbol: str = "c2601", exchange: Exchange = Exchange.DCE) -> None:
        for direction, volume in self.positions.items():
            self.on_position(PositionData(
                symbol=symbol, exchange=exchange, direction=direction, volume=volume,
                price=2200.0, gateway_name=self.gateway_name,
            ))

    def set_position(self, direction: Direction, volume: float, symbol: str = "c2601") -> None:
        self.positions[direction] = volume
        self.push_positions(symbol)

    def push_contract(self, symbol: str = "c2601") -> None:
        self.on_contract(ContractData(
            symbol=symbol, exchange=Exchange.DCE, name=symbol, product=Product.FUTURES, size=10,
            pricetick=1.0, min_volume=1, gateway_name=self.gateway_name,
        ))

    def push_tick(self, symbol: str = "c2601", last: float = 2200.0, dt: datetime | None = None,
                  volume: float = 0.0, limit_up: float | None = None, limit_down: float | None = None) -> None:
        """涨跌停价默认取 ±5% 并取整到最小变动价位(1):真实交易所的涨跌停价一定是合法价位。"""
        finite = last == last and last not in (float("inf"), float("-inf"))
        self.on_tick(TickData(
            symbol=symbol, exchange=Exchange.DCE, datetime=dt or datetime.now(), name=symbol, last_price=last,
            volume=volume, open_interest=1000.0,
            limit_up=limit_up if limit_up is not None else (float(round(last * 1.05)) if finite else 0.0),
            limit_down=limit_down if limit_down is not None else (float(round(last * 0.95)) if finite else 0.0),
            gateway_name=self.gateway_name,
        ))

    def push_account(self, balance: float) -> None:
        self.on_account(AccountData(accountid="SIM", balance=balance, frozen=0.0, gateway_name=self.gateway_name))


class FakeDatabase:
    """只提供策略预热用的 load_bar_data;记录被查询的代码。"""

    def __init__(self, bars):
        self.bars = bars
        self.queried: list[str] = []

    def load_bar_data(self, symbol, exchange, interval, start, end):
        self.queried.append(symbol)
        return list(self.bars)


def history_bars(n=40, end_day=datetime(2026, 11, 6)):
    """逐日下跌 2 点的日线:快线在慢线下方,ATR 约 3 点。"""
    bars = []
    for i in range(n):
        day = end_day - timedelta(days=n - 1 - i)
        close = 2300.0 - 2 * i
        bars.append(BarData(symbol="C0", exchange=Exchange.DCE, interval=Interval.DAILY, datetime=day,
                            open_price=close + 2, high_price=close + 1, low_price=close - 1, close_price=close,
                            volume=1000, open_interest=1000, gateway_name="DB"))
    return bars
