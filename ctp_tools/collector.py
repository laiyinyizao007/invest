"""共享的 CTP 采集工具。

CLI 和环境闸门保留在 ``scripts/collect_reject_texts.py``；本模块只提供
Collector、委托场景构造、持仓检查和引擎关闭等可复用逻辑。
"""
import threading
import time
import sys
import bootstrap
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vnpy.event import Event
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from logging import ERROR  # 标准库的级别常量:不要从 vnpy.trader.logger 引,
# 那会把日志模块(在导入时就按 TEMP_DIR 建好文件 sink)提前拉进来,隔离目录就名不副实了
from vnpy.trader.object import ContractData, LogData, OrderData, OrderRequest, TickData
from vnpy.trader.utility import round_to

from trading.graceful_exit import logout_ctp
# 交易时段(本地时间,分钟数)。用于判断"能不能用合法价格下单":
# 交易时段内下单是可能成交的,必须改用越界价格才安全。
# 日盘 09:00-10:15 / 10:30-11:30 / 13:00-15:00;夜盘 21:00 起,不同品种收在 23:00、01:00 或 02:30。
# 13:00 起的下午盘覆盖了 CFFEX(股指 13:00 开、国债 13:00 开)与商品期货(13:30 开)。
# 这里**保守取并集**:只要某个品种可能还在交易,就算"在时段内"→ 用越界价格。
# 漏判(把时段内当成时段外)会让合法价委托成交留下持仓;误判(把时段外当成时段内)
# 只是少采一个场景,安全方向相反。
DAY_SESSION_WINDOWS = ((9 * 60, 10 * 60 + 15), (10 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
NIGHT_SESSION_START = 21 * 60          # 21:00
NIGHT_SESSION_END = 2 * 60 + 30         # 次日 02:30(最晚的品种)


def in_trading_session(dt: datetime) -> bool:
    """是否可能处于交易时段(保守判断)。

    - 工作日:日盘窗口内、或 21:00 之后(夜盘,含跨午夜到次日 02:30);
    - 周六 00:00-02:30:属于周五夜盘的延续段(部分品种到 01:00/02:30),算在时段内;
    - 周日全天、周一 00:00-02:30:没有夜盘(周日夜盘不存在),不在时段内。

    法定节假日判断不了:假日交易所休市,委托会被拒不会成交,所以误判成"非交易时段"是安全的。
    """
    weekday = dt.weekday()          # 周一=0 … 周六=5,周日=6
    minute = dt.hour * 60 + dt.minute
    if weekday == 6:                 # 周日:没有夜盘
        return False
    if weekday == 5:                 # 周六:只有周五夜盘的延续段
        return minute < NIGHT_SESSION_END
    if minute >= NIGHT_SESSION_START:
        return True                  # 21:00 之后的夜盘(可能跨午夜)
    if minute < NIGHT_SESSION_END:
        return weekday != 0          # 00:00-02:30 属于前一交易日夜盘;周一夜盘不存在
    return any(start <= minute < end for start, end in DAY_SESSION_WINDOWS)

@dataclass
class Case:
    """一次采集:场景名 + 发出的委托 + 收到的错误文本。"""

    name: str
    detail: str
    error_text: str = ""
    order_status: str = ""
    order_id: str = ""


class Collector:
    """订阅 CTP 的订单与日志事件,把错误文本按时间关联到委托上。

    注意:vnpy_ctp 的 `write_error()` 实际调用的是 `write_log()`,也就是**以 INFO 级别**输出
    "交易委托失败，代码：21，信息：..."。只收 ERROR 级别的日志会漏掉全部委托失败文本,
    所以这里收全部网关日志,再用关键词挑出错误类。
    """

    # CTP/vn.py 用来表示"这笔委托被拒"的日志措辞
    # "状态更新"覆盖**交易所级别**的拒单:CTP 拒单走 `onRtnOrder`,vnpy_ctp 把状态映射成 REJECTED,
    # 原因写在 StatusMsg 里,日志措辞是"委托 {委托号} 状态更新，{原因}"(ctp_gateway.py:713-719)。
    # 这条路径不经过 `onRspOrderInsert`,所以"交易委托失败"收不到它。
    # "Traceback"/"Error:" 用来抓网关回调线程里的异常(如 vnpy_ctp 对不存在的合约抛 KeyError,
    # 事件引擎把 traceback 写成一条日志,措辞是英文)。
    ERROR_MARKERS = ("失败", "错误", "被拒绝", "拒绝", "不支持", "非法", "超限", "不足", "不存在",
                     "状态更新", "Traceback", "Error:")

    def __init__(self) -> None:
        self.errors: list[tuple[float, str]] = []
        self.orders: dict[str, OrderData] = {}
        self.ticks: dict[str, TickData] = {}
        self.contracts: dict[str, ContractData] = {}
        self.log_lines: list[str] = []
        # 登录失败的日志文本:等合约表的时候如果登录就失败了,再等下去只是浪费时间
        self.login_failures: list[str] = []
        # **持仓查询回报的行**(不是 OMS 快照,见 `query_positions` 的说明):
        # 网关每次查询回报只推送**本次返回的**行,已平掉的合约不会再出现。
        self.position_rows: list = []
        # 账户资金回报计数:持仓查询「没有行」时,用它证明柜台确实在应答(见 query_positions)
        self.account_count = 0
        # 事件线程写、主线程读:用一把锁把列表的读取变成一致的快照
        self.lock = threading.Lock()

    def on_position(self, event: Event) -> None:
        """收持仓查询回报的行。

        必须自己收:vn.py 的 OMS 只做 `self.positions[id] = position`,**从不删除条目**,
        所以平掉仓之后 OMS 里那条旧持仓永远还在(见 `query_positions` 的说明)。
        """
        with self.lock:
            self.position_rows.append(event.data)

    def on_account(self, event: Event) -> None:
        with self.lock:
            self.account_count += 1

    def mark_positions(self) -> int:
        """记下当前已收到的持仓行数(发查询前记,之后 `positions_since` 只看新的)。"""
        with self.lock:
            return len(self.position_rows)

    def positions_since(self, mark: int) -> list:
        """取 `mark` 之后收到的持仓行(=某次查询回报返回的全部行)。"""
        with self.lock:
            return list(self.position_rows[mark:])

    def on_log(self, event: Event) -> None:
        log: LogData = event.data
        with self.lock:
            self.log_lines.append(log.msg)
            if "登录失败" in log.msg:
                self.login_failures.append(log.msg)
            if log.level >= ERROR or any(k in log.msg for k in self.ERROR_MARKERS):
                self.errors.append((time.monotonic(), log.msg))

    def login_failed(self) -> str:
        """最近的登录失败文本(没有则空串)。"""
        with self.lock:
            return self.login_failures[-1] if self.login_failures else ""

    def on_order(self, event: Event) -> None:
        order: OrderData = event.data
        with self.lock:
            self.orders[order.vt_orderid] = order

    def on_tick(self, event: Event) -> None:
        tick: TickData = event.data
        with self.lock:
            self.ticks[tick.vt_symbol] = tick

    def on_contract(self, event: Event) -> None:
        contract: ContractData = event.data
        with self.lock:
            self.contracts[contract.vt_symbol] = contract

    def error_near(self, ts: float, window: float = 6.0, since: int = 0) -> str:
        """取时间上最接近的网关错误文本。

        since:只看 errors[since:] —— 发单前记下当前条数,就不会把**上一个场景**的旧错误
        当成这次的错误文本(旧错误离 ts 更近时会先被选中,表格就被污染了)。
        """
        with self.lock:
            pool = self.errors[since:]
        best = min(pool, key=lambda e: abs(e[0] - ts), default=None)
        if best is not None and abs(best[0] - ts) <= window:
            return best[1]
        return ""

    def error_index(self) -> int:
        """当前已收集的错误条数(发单前记下来,作为 since 下标)。"""
        with self.lock:
            return len(self.errors)

    def contract_of(self, symbol: str) -> ContractData | None:
        with self.lock:
            return self.contracts.get(symbol)

    def tick_of(self, symbol: str) -> TickData | None:
        with self.lock:
            return self.ticks.get(symbol)

    def order_of(self, vt_orderid: str) -> OrderData | None:
        with self.lock:
            return self.orders.get(vt_orderid)

    def wait_error(self, ts: float, timeout: float = 5.0, window: float = 6.0, since: int = 0) -> str:
        """等错误日志到达后再取文本,取不到就返回空串。

        为什么必须等:vnpy_ctp 的 `onRspOrderInsert` 是**先** `gateway.on_order(order)`
        (订单回报) **后** `gateway.write_error(...)` (错误日志),见
        `.venv/Lib/site-packages/vnpy_ctp/gateway/ctp_gateway.py:503-522`。
        事件引擎按 put 顺序派发,所以订单回报一定先到;采集脚本一拿到拒单回报就去翻日志,
        日志多半还没入队,会误记成"(无错误日志)"——2026-11-10 首次采集就踩了这个坑,
        白跑一趟 SimNow 还以为 CTP 没给错误文本。
        """
        deadline = time.monotonic() + timeout
        while True:
            text = self.error_near(ts, window, since)
            if text:
                return text
            if time.monotonic() >= deadline:
                return ""
            time.sleep(0.05)

    def wait_order_final(self, vt_orderid: str, timeout: float = 15.0) -> OrderData | None:
        """等委托进入终态(全部成交/部分成交/已撤销/拒单)。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            order = self.order_of(vt_orderid)
            if order and not order.is_active():
                return order
            time.sleep(0.1)
        return self.order_of(vt_orderid)


def reference_price(symbol: str) -> tuple[float, str]:
    """取一个基准价用于构造委托。非交易时段没有 Tick,退回本地日线的最近收盘价。

    返回 (价格, 来源说明)。
    """
    from vnpy.trader.database import get_database
    from vnpy.trader.constant import Interval

    database = get_database(bootstrap.ROOT / ".vntrader" / "database.db")
    overview = database.get_bar_overview(symbol)
    if overview:
        bars = database.load_bar_data(symbol, overview[0].interval, overview[0].start, overview[0].end)
        if bars:
            return float(bars[-1].close_price), f"本地日线最后收盘({bars[-1].datetime:%Y-%m-%d})"
    raise RuntimeError(f"拿不到 {symbol} 的基准价(本地日线为空),请先跑 build_continuous.py --save")


def build_cases(symbol: str, exchange: Exchange, last_price: float, max_volume: int,
                pricetick: float = 1.0, in_session: bool = False) -> list[tuple[str, OrderRequest]]:
    """构造几个必定被拒的委托。

    max_volume 来自合约信息(ContractData.max_volume),用来构造"超大手数"。
    pricetick 用于把价格对齐到最小变动价位——价格不是 tick 的整数倍时 CTP 会按"价格非法"拒单,
    会把"想测的场景"变成另一个场景,拿到的错误文本没有意义。
    in_session=True(当前处于交易时段)时,**价格合法的场景一律改用越界价格**:
    交易时段内合法价格的委托是可能成交的,一旦交易所没按预期拒单就会留下持仓。
    """
    code = symbol.split(".")[0]
    base = max(float(last_price), 1.0)
    tick = pricetick if pricetick and pricetick > 0 else 1.0
    # 越界价:买价远高于市场 / 远低于市场,两种都不可能成交
    high = round_to(base * 1.3, tick)
    low = round_to(base * 0.5, tick)
    fair = round_to(base, tick)

    cases: list[tuple[str, OrderRequest]] = []

    # ① 不存在的合约:合约本身不存在,不可能成交
    cases.append(("不存在的合约", OrderRequest(
        symbol="c9999", exchange=exchange, direction=Direction.LONG, offset=Offset.OPEN,
        price=fair, volume=1, type=OrderType.LIMIT)))

    # ② 非交易时段开仓:靠时段被拒。交易时段内跑这个场景没有意义(而且会成交),所以跳过。
    if not in_session:
        cases.append(("非交易时段开仓", OrderRequest(
            symbol=code, exchange=exchange, direction=Direction.LONG, offset=Offset.OPEN,
            price=fair, volume=1, type=OrderType.LIMIT)))

    # ③ 价格明显超出涨跌停:往上抬 30%
    cases.append(("价格超出涨跌停", OrderRequest(
        symbol=code, exchange=exchange, direction=Direction.LONG, offset=Offset.OPEN,
        price=high, volume=1, type=OrderType.LIMIT)))

    # ④ 无持仓时平仓:没有持仓就不可能成交
    cases.append(("无持仓时平仓", OrderRequest(
        symbol=code, exchange=exchange, direction=Direction.SHORT, offset=Offset.CLOSE,
        price=fair, volume=1, type=OrderType.LIMIT)))

    # ⑤ 超大手数(超过合约单笔上限)。交易时段内用越界低价,保证不会成交。
    cases.append(("超大手数", OrderRequest(
        symbol=code, exchange=exchange, direction=Direction.LONG, offset=Offset.OPEN,
        price=low if in_session else fair, volume=max(1, max_volume) + 1, type=OrderType.LIMIT)))
    return cases


BEGIN_MARK = "<!-- BEGIN COLLECTED"
END_MARK = "<!-- END COLLECTED -->"


def write_collected(out: Path, text: str) -> None:
    """把采集表写进 Markdown 文件,**不覆盖人工写的分析段落**。

    自动采集的表格用 BEGIN/END 标记圈起来:文件已存在且带标记就只替换标记之间的内容,
    标记之外的人工分析(根因、结论、待办)原样保留。文件不存在就整篇写入。
    """
    if not out.exists():
        out.write_text(text, encoding="utf-8")
        return
    old = out.read_text(encoding="utf-8")
    begin = old.find(BEGIN_MARK)
    end = old.find(END_MARK)
    if begin < 0 or end < 0:
        # 没有标记:不覆盖人工写的整篇文档,另存一份带后缀的原始文件
        alt = out.with_name(out.stem + "-raw.md")
        alt.write_text(text, encoding="utf-8")
        print(f"{out} 里没有自动采集标记(可能是人工维护的文档),已改写 {alt}", file=sys.stderr)
        return
    # BEGIN 标记那一行(含"重新采集会替换这一段"这句人话)必须**原样写回去**:
    # 丢了下一次运行 old.find(BEGIN_MARK) 就找不到标记 → 采集结果只会落到 -raw.md,
    # 主文档里的表格从此不再更新。
    line_start = old.rfind("\n", 0, begin) + 1
    line_end = old.find("\n", begin)
    if line_end < 0:
        line_end = len(old)
    marker_line = old[line_start:line_end]
    if line_start != begin or END_MARK in marker_line:
        # BEGIN 标记不在行首,或者两个标记挤在同一行 → 用规范写法,
        # 别把上一轮的旧内容留在标记行里。
        marker_line = BEGIN_MARK + " -->"
    if END_MARK not in text:
        # 调用方给的正文里没有结束标记 → 补上,否则下一次运行会认为"没有标记"
        text = text.rstrip("\n") + "\n" + END_MARK
    new = old[:begin] + marker_line + "\n" + text + old[end + len(END_MARK):]
    out.write_text(new, encoding="utf-8")


def close_engine(main_engine, timeout: float = 10.0) -> bool:
    """关闭引擎,但不无限等。

    CTP 网关的 close() 会 join 原生 API 线程;在 4097 断连风暴下这个 join 可能长时间不返回,
    表现为脚本"跑完了但不退出"(2026-11-10 实测)。这里放到后台线程里限时等,
    剩下的由 __main__ 里的 os._exit 兜底。
    """
    def shutdown() -> None:
        logout_ctp(main_engine)            # 先登出再关闭:vnpy_ctp 的 close() 只断开连接、不发登出请求
        main_engine.close()

    t = threading.Thread(target=shutdown, daemon=True)
    t.start()
    t.join(timeout)
    return not t.is_alive()      # False = 限时内没关完,调用方应当如实记录而不是假装正常退出


def positions_of_symbol(positions: list[str], symbol: str) -> list[str]:
    """从 check_positions 的结果里挑出属于某个合约的持仓。

    check_positions 返回的每条形如 "c2701.DCE 多 1"(第一段是合约代码)。
    """
    return [p for p in positions if p.split(" ")[0] == symbol]


def symbol_has_position(main_engine, symbol: str, wait: float = 15.0,
                        collector=None) -> tuple[bool, bool]:
    """发单前确认账户是否**已有持仓**。

    返回 (是否有持仓, 连接是否正常)。发单前必须查:
    "无持仓时平仓"场景发的是一张平仓单,账户上如果本来就有这个合约的持仓,
    这张单**会真的平掉仓位**——采集脚本会顺手毁掉用户的持仓。
    连接断了就查不准,调用方看到 connected=False 应当直接放弃采集。

    判定放宽到**账户有持仓就中止**(不只目标合约):采集脚本会发出各种委托,
    账户里有别人的仓位时,任何一次误判都可能动到它。

    **必须等满整个等待窗口**(`early_exit=False`):CTP 的持仓查询是异步的,
    持仓回报到达前 OMS 里可能还是上一次的旧数据,或者干脆是空的。
    早退(看到别的合约有持仓就立刻返回)会漏掉"本合约的持仓回报还没到"的情况,
    于是脚本以为空仓、发出一张平仓单,把用户的仓位真的平掉。

    而且要**查两轮**:CTP 对空账户**不推送任何持仓事件**,所以"没收到回报"既可能是
    真的空仓,也可能是回报还没到。只查一轮的话,SimNow 高负载下回报延迟就会造成误判。
    第二轮再查一次,任一轮看到持仓就中止。

    collector:给了就走**查询回报**这条路(权威,见 `query_positions`);
    不给只能退回 OMS 快照(可能陈旧 —— 陈旧只会**误报有持仓**、从而中止采集,
    属于安全方向,但会白白挡住一次采集)。
    """
    seen: list[str] = []
    connected = True
    for round_wait in (wait, wait / 2):
        positions, round_ok = check_positions(main_engine, wait=round_wait, early_exit=False,
                                              collector=collector)
        connected = connected and round_ok       # 任一轮不可信 = 整体不可信
        seen.extend(positions)
        if positions:
            break
    return bool(seen), connected


def trade_live(main_engine) -> bool:
    """交易通道当前是否在线(可作为「查到的数据可信」的判据)。

    必须看 `login_status`,不能看 `connect_status`:vnpy_ctp 的
    `onFrontDisconnected` 只把 `login_status` 清掉,`connect_status` 一直是 True
    (见 vnpy_ctp/gateway/ctp_gateway.py)。用 `connect_status` 判活的话,
    断线之后查询照样"成功",返回空持仓 —— 空仓闸门就成了摆设:
    明明连不上柜台,却被判成"账户是空的",于是照常发演示委托。

    拿不到 `login_status` 属性 = 不知道通道状态 → 判不在线(失败关闭):
    退回 `connect_status` 的话,断线后它恒为 True,空仓闸门就失效了。
    """
    td_api = getattr(main_engine.get_gateway("CTP"), "td_api", None)
    if td_api is None or not hasattr(td_api, "login_status"):
        return False
    return bool(td_api.login_status)


def query_positions(main_engine, collector, wait: float = 5.0,
                    quiet: float = 1.2) -> tuple[list, str]:
    """按「**本次查询回报返回的行**」取持仓,而不是 OMS 快照。

    为什么不能只看 OMS(vn.py 的 `OmsEngine`):
    - `vnpy_ctp` 只在持仓查询回报的 `last=True` 时推送**本次返回的**行
      (ctp_gateway.py:`if last: for position in self.positions.values(): on_position(...)`;
      回报结束后 `self.positions.clear()`),所以已经平掉的合约**不会再出现**;
    - 而 `OmsEngine.process_position_event` 只做 `self.positions[id] = position`,
      **从不删除条目**。
    合起来的后果:平仓成功后,OMS 里那条旧持仓永远留着 —— 平仓工具会说
    「未平干净,仍有持仓」,取证运行器的空仓闸门会把**后面所有发单步骤**判成
    「账户已有持仓」而全部中止。仓库自己在 `trading/guard_rule.py:16` 记过同一件事:
    「按**成交量**对账,而不是反复读取 OMS 持仓」。

    返回 (持仓行, 状态):
    - "ok":收到了本次查询的回报(可能为空行=账户确实没有持仓);
    - "empty":没有收到行,但通道在线(CTP 对空账户**不推送任何持仓事件**,
      所以「没有行」在通道在线时就是「没有持仓」);
    - "untrusted":查询发不出去或通道不在线 —— 这时说什么都是猜。
    """
    gateway = main_engine.get_gateway("CTP")
    mark = collector.mark_positions()
    try:
        gateway.query_position()
    except Exception:      # noqa: BLE001  查询发不出去 = 不可信
        return [], "untrusted"
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        rows = collector.positions_since(mark)
        if rows:
            # 网关在 last=True 时一次性推送全部行;等一小段「安静窗口」,
            # 确认回报已经发完(否则可能只拿到前半截)。
            end = time.monotonic() + quiet
            while time.monotonic() < end:
                time.sleep(0.05)
                more = collector.positions_since(mark)
                if len(more) > len(rows):
                    rows = more
                    end = time.monotonic() + quiet
            return rows, "ok"
        time.sleep(0.1)
    if not trade_live(main_engine):
        return [], "untrusted"
    # 「没有行」有两种可能:账户真的空 / 柜台根本没应答。vnpy_ctp 的 TD 侧
    # 没有 onRspError、也丢弃 reqQryInvestorPosition 的返回码,被流控拒掉时
    # 零行零日志 —— 看起来和空仓一模一样。所以要拿一个**可观测的应答**证明
    # 柜台在说话:查一次账户资金,收到回报才算「确实空仓」。
    if not _probe_counter_replies(main_engine, collector):
        return [], "untrusted"
    return [], "empty"


def _probe_counter_replies(main_engine, collector, wait: float = 4.0) -> bool:
    """发一次账户资金查询,等回报到达;到了 = 柜台在应答(持仓查询没回行就是真的没有)。

    采集器没有 `account_count`(旧桩)→ 无法证明 → 判不可信(失败关闭)。
    查询流控是每秒一次,所以先隔 1.1s 再发,免得探针本身被拒。
    """
    if not hasattr(collector, "account_count"):
        return False
    time.sleep(1.1)
    with collector.lock:
        before = collector.account_count
    try:
        main_engine.get_gateway("CTP").query_account()
    except Exception:      # noqa: BLE001
        return False
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with collector.lock:
            if collector.account_count > before:
                return trade_live(main_engine)
        time.sleep(0.1)
    return False


def check_positions(main_engine, wait: float = 3.0,
                    early_exit: bool = True, collector=None) -> tuple[list[str], bool]:
    """查询并返回**账户级**的非零持仓(本脚本不应留下任何持仓,有就是意外)。

    **这是账户级的,不是某个合约的**:空仓闸门要的是「账户上没有任何持仓」。
    以前这里有个 `symbol` 形参,但所有调用方都要的是账户级结果,而这个参数
    被**完全忽略**——留着它只会让人以为返回的是那个合约的持仓。
    形参已经删掉:要按合约过滤,自己用 `positions_of_symbol()`。

    注意:**连接断了的时候这个检查没有意义**——查不到持仓不等于没有持仓。
    连接状态在返回里一并给出(第二个元素),调用方据此提示人工核对。
    判活看 `login_status`(见 `trade_live` 的说明),断线时这个值是 False。

    early_exit=True(默认):一看到非零持仓就返回,适合"跑完后的收尾检查"。
    early_exit=False:始终等满 wait 窗口,适合"发单前的安全检查"——
    给 CTP 的异步持仓回报留出到达时间,避免拿到过期的空仓结论。

    collector:给了就用**查询回报**里的行(权威,见 `query_positions`);
    没给只能退回 OMS 快照(可能陈旧,只用于没有采集器的场景)。
    """
    if collector is not None and hasattr(collector, "mark_positions"):
        return _check_positions_by_query(main_engine, wait, collector)
    gateway = main_engine.get_gateway("CTP")
    try:
        gateway.query_position()
    except Exception:      # noqa: BLE001  查询发不出去 = 不可信,不能继续用 OMS 快照当结论
        return [], False
    deadline = time.monotonic() + wait
    found: list[str] = []
    while time.monotonic() < deadline:
        found = [f"{p.vt_symbol} {p.direction.value} {p.volume}"
                 for p in main_engine.get_all_positions() if p.volume > 0]
        if found and early_exit:
            break
        time.sleep(0.5)
    # 判活放在**查询之后**:查询期间断线的话,这次查到的持仓就是旧的/空的,
    # 拿它当"账户是空的"用,等于在断线的柜台上下单。
    return found, trade_live(main_engine)


def _check_positions_by_query(main_engine, wait: float, collector) -> tuple[list[str], bool]:
    """按查询回报判断持仓;「没有行」要查两轮才算空仓。

    CTP 对空账户**不推送任何持仓事件**,所以第一轮没有行时,分不清是
    「账户真的空」还是「回报还没到」。查两轮(两轮都没有行且通道在线)才算空。
    """
    round_wait = max(1.0, min(wait, 5.0))
    for _round in range(2):
        rows, status = query_positions(main_engine, collector, wait=round_wait)
        if status == "ok":
            found = [f"{p.vt_symbol} {p.direction.value} {p.volume}"
                     for p in rows if getattr(p, "volume", 0) > 0]
            return found, trade_live(main_engine)
        if status == "untrusted":
            return [], False
    return [], trade_live(main_engine)


