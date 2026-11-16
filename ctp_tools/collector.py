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

# 持仓回报「分批」的间隔阈值(秒):一次查询回报的行是连续推来的,两次回报之间
# 至少隔一个网络往返。取 0.3 秒:远小于一个往返,又宽到能容忍事件循环的抖动。
POSITION_BATCH_GAP = 0.3


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
    # 只收**交易网关**的错误日志:本地风控的预警/拒绝日志同样是 ERROR 级别
    # (trading/guard_rule.py 的 `_alert` 就写成 LogData(level=ERROR)),
    # 混进来会让"柜台错误提示"取到风控文案而不是柜台的拒单原因
    # (实测 2026-11-16 夜盘,P4.2 取到的是本地风控的「风控阈值触发」)。
    # 与 `trading/guard_rule.py:_on_log` 是同一条思路(网关名白名单),但这里**更严**:
    # 那边只按网关名过滤,这边还额外收了「没有网关名的 traceback」(见 TRACEBACK_MARKERS)。
    ERROR_GATEWAYS: tuple[str, ...] = ("CTP",)
    # 事件引擎自己写的 traceback **没有网关名**(vnpy 的 `EventEngine.on_exception` 用
    # `LogData(msg=traceback.format_exc(), level=ERROR)`,gateway_name 为空),
    # 那是网关回调线程里的异常(比如 vnpy_ctp 对不存在的合约抛 KeyError)——也算柜台侧的证据,
    # 不能被上面的「只收交易网关」一起滤掉。
    TRACEBACK_MARKERS: tuple[str, ...] = ("Traceback", "Error:")

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
        # 每行的到达时刻(下标 → 时刻):用来把行按「查询回报」分批。
        # 用**下标做键**而不是并行列表:有人绕过 `on_position` 直接往 `position_rows`
        # 里塞行时,并行列表会与行数错位(时刻张冠李戴),下标做键则是「查不到就
        # 不分批」——宁可少一层判据,也不能把行丢掉(丢掉 = 以为账户空仓 = 会去发单)。
        self.position_times: dict[int, float] = {}
        # 合并前的原始行数(由 `query_positions` 写):留痕用,不参与任何平仓计算
        self.position_rows_raw = 0
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
            self.position_times[len(self.position_rows) - 1] = self.now()

    def now(self) -> float:
        """取当前时刻(单独一个方法是为了让测试能控制时间)。"""
        return time.monotonic()

    def position_batches_since(self, mark: int) -> list[list]:
        """把 `mark` 之后收到的持仓行按「查询回报」分批。

        一次查询回报的行是**连续推来的**(网关在一个回调里 for 循环推完),
        两次回报之间至少隔一个网络往返。所以按到达时间的间隔分批:
        间隔超过 `POSITION_BATCH_GAP` 就当作新的一次回报。

        拿不到到达时刻的行(绕过 `on_position` 直接塞进 `position_rows` 的)→ **整段当一批**:
        分不了批就等于没有「最新一批」这个概念,退回旧行为(不丢行)。
        丢行 = 以为账户空仓 = 会去发单,所以这一层只能保守。
        """
        with self.lock:
            rows = list(self.position_rows[mark:])
            times = [self.position_times.get(i) for i in range(mark, mark + len(rows))]
        if any(t is None for t in times):
            return [rows] if rows else []
        batches: list[list] = []
        current: list = []
        last_t: float | None = None
        for row, t in zip(rows, times):
            if last_t is not None and t - last_t > POSITION_BATCH_GAP:
                batches.append(current)
                current = []
            current.append(row)
            last_t = t
        if current:
            batches.append(current)
        return batches

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
            if not self.is_counter_error(log):
                return
            if log.level >= ERROR or any(k in log.msg for k in self.ERROR_MARKERS):
                self.errors.append((time.monotonic(), log.msg))

    def is_counter_error(self, log: LogData) -> bool:
        """这条日志算不算**柜台侧**的错误。

        两条判据(都要):
        - 来自交易网关(`ERROR_GATEWAYS`),或者
        - 是事件引擎写的 traceback(没有网关名,见 `TRACEBACK_MARKERS`);
        本地风控/运行时的 ERROR 日志(gateway_name 是 `RiskManager` 之类)不算 ——
        实测 2026-11-16 夜盘,P4.2 取到的「柜台错误提示」就是本地风控的「风控阈值触发」。
        """
        if log.gateway_name in self.ERROR_GATEWAYS:
            return True
        if not log.gateway_name and any(k in log.msg for k in self.TRACEBACK_MARKERS):
            return True
        return False

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


# vn.py 的 `Direction` 是**被 gettext 翻译过**的枚举(`LONG = _("多")`):
# 英文 locale 下 `Direction.LONG.value == 'Long'`,中文/`C` locale 下是 `'多'`。
# 实测三种 locale(en_US.UTF-8 / zh_CN.UTF-8 / C)给出三种文案。
# 所以:
#   - **键**一律用**枚举名**('LONG'/'SHORT'/'NET')—— 与语言无关,机器换 locale 键不变;
#   - **给人看的文案**用固定中文('多'/'空'/'净')—— 报告交到国信手里,不能因为
#     跑报告的机器 locale 不同就变成英文。
# 仓库里既有的做法也是用枚举名(`scripts/research_sandbox.py`:
# 「用枚举名:value 的文案随 vnpy 版本/语言变化」)。
_DIRECTION_NAMES = {"多": "LONG", "空": "SHORT", "净": "NET"}
_DIRECTION_TEXTS = {"LONG": "多", "SHORT": "空", "NET": "净"}


def normalize_direction(direction) -> str | None:
    """把方向归一成**语言无关**的枚举名('LONG'/'SHORT'/'NET');认不出来就返回 None。

    为什么不能用 `direction.value`:见上面 `_DIRECTION_NAMES` 的注释 —— 那个值随 locale 变,
    拿它当合并键就等于「同一份持仓在不同机器上归出不同的键」→ 合并漏行 → 该平的没平。

    裸字符串输入(测试替身、离线留痕)按「先当枚举名、再当枚举值、最后当中文文案」认:
      'LONG' → LONG;  'Long' → LONG;  '多' → LONG。
    实在认不出来的原样大写保留(仍与语言无关),不丢行。
    """
    name = getattr(direction, "name", None)
    if isinstance(name, str) and name:
        return name.upper()
    text = str(direction or "").strip()
    if not text:
        return None
    if text in _DIRECTION_NAMES:
        return _DIRECTION_NAMES[text]
    try:
        return Direction[text].name          # 枚举名:'LONG'
    except KeyError:
        pass
    try:
        return Direction(text).name          # 枚举值:'Long'(英文 locale)
    except ValueError:
        pass
    return text.upper()


def direction_text(direction) -> str:
    """方向的中文文案(固定 '多'/'空'/'净'),与跑报告的机器 locale 无关。"""
    name = normalize_direction(direction)
    if not name:
        return ""
    return _DIRECTION_TEXTS.get(name, name)


def position_key(row) -> tuple[str, str] | None:
    """取一行的 (合约, 方向) 键;认不出来就返回 None。

    方向用 `normalize_direction()` 归一(枚举名),所以 `Direction.LONG`、裸字符串
    `"Long"`、裸字符串 `"多"` 会归到同一个键 `'LONG'`,而且**换台 locale 不同的
    机器也归到同一个键** —— 键随语言变的话,合并会漏行(该平的没平)。
    """
    symbol = str(getattr(row, "vt_symbol", "") or "")
    direction = getattr(row, "direction", None)
    if not symbol or direction is None:
        return None
    name = normalize_direction(direction)
    if not name:
        return None
    return symbol, name


def select_latest_snapshot(batches: list[list]) -> list:
    """从若干批持仓回报里挑出**最新一次查询回报**的行。

    背景:一次持仓查询的回报是**一批行**,但采集窗口里可能落进两批(我们主动查的
    那次 + 网关约每 4 秒自动查的那次)。柜台对**已经平掉**的合约不会再推持仓事件,
    所以后一批快照里**消失**的键 = 那个合约已经平掉了。

    判据(刻意保守):
    - 只有一批 → 就是最新快照,全要;
    - 多批,且**最后一批的键集合是紧邻前一批的子集** → 说明最后一批是**完整的新快照**
      (有持仓被平掉了,又没冒出新键)→ 只留最后一批;
    - 否则 → **判不出来**(最可能是同一次回报被事件循环卡顿切成了两批)→ 保留全部键。
      宁可多留一行(多平一手会被柜台整单拒),也不能少平一手。

    为什么只跟**紧邻的前一批**比(而不是跟前面所有批的并集比):
    一次回报内部各行的 (合约, 方向) 键是**互不相同**的(网关用键去重),所以
    「被切成两半的同一次回报」的两批键集合**必然不相交** → 拿不相交的前一批去比,
    永远判不出「子集」→ 不会误丢。而如果跟前一批的并集比,「前一批恰好是更早的一次快照」
    就会让**同一次回报被切成三批**的情形(第一批={A,B}、第二批={A}、第三批={B})
    判成「最后一批 ⊆ 并集」→ 丢掉还在的 A(去平一个已经平掉的合约方向 ⇒ 少平)。
    与紧邻前一批比,这种情形会被判成「不相交 → 保留全部」,方向安全。

    盲区(必须知道):空快照推不出任何行,所以「后一批是空的」这种情况**看不出来**。
    这一条由调用方的「轮询到空为止」兜住:每次查询都重新记 mark,旧批次不会一直
    留在窗口里(见 `scripts/flatten_positions.py` 的收尾轮询)。
    """
    if not batches:
        return []
    if len(batches) == 1:
        return list(batches[0])
    last_keys = {key for key in (position_key(p) for p in batches[-1]) if key}
    prev_keys = {key for key in (position_key(p) for p in batches[-2]) if key}
    if last_keys and last_keys <= prev_keys:
        return list(batches[-1])
    return [row for batch in batches for row in batch]


def merge_position_rows(rows: list) -> list:
    """把**同一个 (合约, 方向) 的多行**合并成一行,取**最后一次出现**的那行。

    为什么会出现多行(2026-11-16 夜盘实测,已核对 `vnpy_ctp` 源码):
    - `vnpy_ctp` 的 `onRspQryInvestorPosition` 用 `(InstrumentID, PosiDirection)` 做键、
      在 `last=True` 时推送字典的值然后 `self.positions.clear()`(ctp_gateway.py:551/591-595),
      所以**一次回报不可能推出两行同键**;
    - 但网关自己会**定期自动查持仓**:`process_timer_event` 每 2 秒轮一次
      `query_functions = [query_account, query_position]`(ctp_gateway.py:229-245),
      即约每 4 秒一次自动查询;
    - 采集器把持仓回报记成**没有请求/响应边界的平铺列表**,于是「我们主动查的那次」
      与「网关自动查的那次」的两份快照被混进同一个窗口 → 同一个 (合约, 方向) 出现两行。
      实测表现为**间歇性**(留痕 `data/flatten/flatten-20261116-213827.json` 两行,
      而 `-215520/-215903` 各一行),这正是定时器竞态的特征,不是柜台固定重复回报。

    取**最后一次**那行,而不是成交量最大的:
    - 两行来自**两次快照**,快照之间持仓可能变小(例如刚平掉 2 手)。取 max 就是
      **故意挑一个更旧、更大的数**去平 —— 方向和"超平才真金白银"相反;
    - 取最后一次 = 平仓量 ≤ 最近一次快照看到的持仓量,这是我们唯一能守住的单向不变量。

    这个不变量有个前提:**到达顺序 = 时间顺序**。CTP 的每条流是 FIFO 的,响应按请求
    顺序回来,每次响应反映的是响应那一刻的状态 → 到达顺序就是新旧顺序,成立。
    如果哪天真出现乱序(比如回报被重放),这里会退化成"取最后一次到达的",
    仍然不会求和、不会取大,最坏情况是取到一个更旧的数(保守方向:可能少平,不超平)。

    **绝不能求和**:两份快照求和会把平仓量翻倍(3 手 → 平 6 手)。超量的平仓单
    会被柜台**整单拒绝**(实测错误码 30「CTP:平仓量超过持仓量」),不会开出反向仓位,
    但整单被拒意味着仓位没平掉,取证流程照样卡住。

    同一次回报内的多笔持仓(投机 + 套保)不用在这里操心:`vnpy_ctp` 的键不含 HedgeFlag,
    会在同一次回报里先**累加**(`position.volume += data["Position"]`, ctp_gateway.py:577)
    再推一行,所以那种情况我们只看到一行、量已经是合计值。
    """
    latest: dict[tuple[str, str], object] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = position_key(row)
        if key is None:
            continue                       # 认不出键的行不能合并(也不能安全地平):宁可漏掉,不要凭空造一个键
        if key not in latest:
            order.append(key)
        latest[key] = row                 # 后出现的覆盖先出现的 = 取最后一次快照
    return [latest[key] for key in order]


def _latest_snapshot_rows(collector, mark: int, rows: list) -> list:
    """挑出最新一批回报的行。

    采集器支持分批(`position_batches_since`)时按「子集」判据挑;
    只提供 `positions_since` 的老接口/测试替身 → 当成一批(等价于旧行为,只是少了
    「平掉的合约自动消失」这一层)。
    """
    batches = getattr(collector, "position_batches_since", None)
    if batches is None:
        return rows
    return select_latest_snapshot(batches(mark))


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

    返回的行按 (合约, 方向) 合并过(见 `merge_position_rows`):同一 (合约, 方向) 在
    一个窗口里可能出现**两次快照**的两行(我们主动查的那次 + 网关约每 4 秒自动查的
    那次),这里只给**最后一次快照**那一行,调用方不会因为重复快照而多平一手。
    两批快照还会先过 `select_latest_snapshot`:后一批里**消失**的合约 = 已经平掉了,
    不该再去平(否则柜台整单拒)。合并前的原始行数记在 `collector.position_rows_raw`,
    供留痕区分"真的多了一行"与"合并掉了"。
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
            collector.position_rows_raw = len(rows)      # 留痕:合并前几行 → 合并后几项
            # 窗口里可能落进两批(我们主动查的那次 + 网关自动查的那次):
            # 先挑出**最新一批**(后一批里消失的合约 = 已经平掉了),再按 (合约,方向) 合并。
            return merge_position_rows(_latest_snapshot_rows(collector, mark, rows)), "ok"
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
        found = [f"{p.vt_symbol} {direction_text(p.direction)} {p.volume}"
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
            found = [f"{p.vt_symbol} {direction_text(p.direction)} {p.volume}"
                     for p in rows if getattr(p, "volume", 0) > 0]
            return found, trade_live(main_engine)
        if status == "untrusted":
            return [], False
    return [], trade_live(main_engine)


