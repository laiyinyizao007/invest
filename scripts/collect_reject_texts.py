"""采集 SimNow 上**真实**的 CTP 废单错误文本(运行手册第 5 节第 1 项)。

为什么需要它:废单分类(`risk/order_policy.py`)靠关键词匹配,关键词是照着文档和经验写的,
没有在真实 CTP 上验证过。这里故意触发几类必定被拒的委托,把 CTP 返回的真实错误文本收集下来,
用于校准 `HARD_KEYWORDS` / `SOFT_KEYWORDS`。

用法(SimNow 上,项目根目录):
    python scripts/collect_reject_texts.py                    # 用 .env 的合约
    python scripts/collect_reject_texts.py --symbol c2701.DCE
    python scripts/collect_reject_texts.py --out docs/reject-texts-simnow.md

安全约束:
- 只在**模拟盘**上跑:必须显式加 `--confirm-simnow`,脚本会先把目标交易/行情地址打印出来,
  由人确认那是 SimNow 地址(CTP 的"柜台环境=实盘"SimNow 与期货公司生产 API 是同一个,
  没法靠环境变量区分,只能靠地址)。
- 触发的委托**全部设计为必被拒绝**:不存在的合约、越界价格、无持仓时平仓、超大手数。
  没有任何一个委托可能成交,因此不会留下持仓。
  **在交易时段内运行时**(脚本会自动判定),所有"价格合法"的场景一律改用越界价格,
  避免交易所万一没拒单而成交留下持仓。
- 只发单不撤单(被拒的委托不需要撤)。
- 跑完主动查询持仓:发现非零持仓就大声告警并以非零码退出。

输出:一份 Markdown 表格(场景 → CTP 原始错误文本),可直接贴进 `risk/order_policy.py` 的关键词表。
"""
import argparse
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import bootstrap

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK
from vnpy.trader.logger import ERROR
from vnpy.trader.object import ContractData, LogData, OrderData, OrderRequest, TickData
from vnpy.trader.utility import round_to

from trading.runtime import build_ctp_setting

# SimNow 官方公布的前置机地址(2026-11 官网 product.action)。
# 第一套与实盘交易时间一致(有夜盘),第二套 7x24(仅供 API 测试)。
# 脚本只允许连这些地址:**期货公司生产 API 的地址一定不在表里**,
# 这样"--confirm-simnow"就不再是一个可以随手点过去的布尔开关。
SIMNOW_TD_ADDRESSES = frozenset({
    "182.254.243.31:30001", "182.254.243.31:30002", "182.254.243.31:30003",
    "182.254.243.31:40001", "182.254.243.31:40002", "182.254.243.31:40003",
})
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
        # 事件线程写、主线程读:用一把锁把列表的读取变成一致的快照
        self.lock = threading.Lock()

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
    new = old[:begin] + text + old[end + len(END_MARK):]
    out.write_text(new, encoding="utf-8")


def close_engine(main_engine, timeout: float = 10.0) -> None:
    """关闭引擎,但不无限等。

    CTP 网关的 close() 会 join 原生 API 线程;在 4097 断连风暴下这个 join 可能长时间不返回,
    表现为脚本"跑完了但不退出"(2026-11-10 实测)。这里放到后台线程里限时等,
    剩下的由 __main__ 里的 os._exit 兜底。
    """
    t = threading.Thread(target=main_engine.close, daemon=True)
    t.start()
    t.join(timeout)


def positions_of_symbol(positions: list[str], symbol: str) -> list[str]:
    """从 check_positions 的结果里挑出属于某个合约的持仓。

    check_positions 返回的每条形如 "c2701.DCE 多 1"(第一段是合约代码)。
    """
    return [p for p in positions if p.split(" ")[0] == symbol]


def symbol_has_position(main_engine, symbol: str, wait: float = 15.0) -> tuple[bool, bool]:
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
    """
    seen: list[str] = []
    connected = True
    for round_wait in (wait, wait / 2):
        positions, connected = check_positions(main_engine, symbol, wait=round_wait, early_exit=False)
        seen.extend(positions)
        if positions:
            break
    return bool(seen), connected


def check_positions(main_engine, symbol: str, wait: float = 3.0,
                    early_exit: bool = True) -> tuple[list[str], bool]:
    """查询并返回非零持仓(本脚本不应留下任何持仓,有就是意外)。

    注意:**连接断了的时候这个检查没有意义**——查不到持仓不等于没有持仓。
    连接状态在返回里一并给出(第二个元素),调用方据此提示人工核对。

    early_exit=True(默认):一看到非零持仓就返回,适合"跑完后的收尾检查"。
    early_exit=False:始终等满 wait 窗口,适合"发单前的安全检查"——
    给 CTP 的异步持仓回报留出到达时间,避免拿到过期的空仓结论。
    """
    gateway = main_engine.get_gateway("CTP")
    connected = bool(getattr(getattr(gateway, "td_api", None), "connect_status", False))
    try:
        gateway.query_position()
    except Exception:
        pass
    deadline = time.monotonic() + wait
    found: list[str] = []
    while time.monotonic() < deadline:
        found = [f"{p.vt_symbol} {p.direction.value} {p.volume}"
                 for p in main_engine.get_all_positions() if p.volume > 0]
        if found and early_exit:
            break
        time.sleep(0.5)
    return found, connected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="要测试的合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--out", default="", help="把结果写到这个 Markdown 文件(默认只打印)")
    parser.add_argument("--connect-timeout", type=float, default=180.0, help="等待合约与账户到位的最长时间(秒)")
    parser.add_argument("--position-check-timeout", type=float, default=15.0,
                        help="发单前查询持仓的最长等待(秒)。CTP 的持仓回报是异步的,"
                             "必须等满这个窗口才能确认账户真的空仓")
    parser.add_argument("--confirm-simnow", action="store_true", help="确认目标是 SimNow 模拟盘(不是期货公司生产 API)")
    parser.add_argument("--allow-any-address", action="store_true",
                        help="⚠ 危险:交易服务器地址不在已知 SimNow 地址表里时,强制放行。"
                             "期货公司生产 API 的地址一定不在表内,加了这个开关就可能连上**真实资金账户**。"
                             "只有在你确信目标是别的模拟/测试环境时才用")
    args = parser.parse_args(argv)

    from dotenv import load_dotenv
    load_dotenv(bootstrap.ROOT / ".env", override=False)
    env = os.environ

    symbol = args.symbol or env.get("LIVE_VT_SYMBOL", "")
    if not symbol:
        print("未指定合约:--symbol 或 .env 的 LIVE_VT_SYMBOL", file=sys.stderr)
        return 2

    # 只在模拟盘上跑。CTP 的"柜台环境=实盘"是 SimNow 与期货公司生产 API 共用的,
    # 没法靠环境变量区分 SimNow 和真券商 —— 只能靠地址。
    environment = env.get("CTP_ENVIRONMENT", "实盘")
    if not args.confirm_simnow:
        print(f"当前 CTP_ENVIRONMENT={environment!r}。本脚本只在**模拟盘**上运行;"
              f"确认是 SimNow 模拟盘请加 --confirm-simnow。", file=sys.stderr)
        return 2

    try:
        gateway_setting = build_ctp_setting(env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    # 地址白名单:SimNow 官方公布的前置机地址。期货公司生产 API 的地址不在表里,
    # 布尔开关("--confirm-simnow")挡不住"配置写错"这种情况,所以这里硬校验。
    td_address = gateway_setting["交易服务器"]
    if td_address not in SIMNOW_TD_ADDRESSES and not args.allow_any_address:
        print(f"交易服务器 {td_address} 不在已知的 SimNow 地址表里。"
              f"期货公司生产 API 的地址一定不在表内,因此本脚本拒绝连接。"
              f"确认这是安全的测试环境,可以加 --allow-any-address 强制运行(会再打一次警告)。",
              file=sys.stderr)
        return 2
    print(f"目标交易服务器 {td_address},行情服务器 {gateway_setting['行情服务器']}。")
    if td_address not in SIMNOW_TD_ADDRESSES:
        print(f"⚠⚠ 危险警告:交易服务器 {td_address} 不是已知的 SimNow 地址,"
              f"本次运行由 --allow-any-address 强制放行。"
              f"若这个地址属于期货公司生产 API,本脚本会连上**真实资金账户**并发出委托。",
              file=sys.stderr)

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    from vnpy_ctp import CtpGateway
    main_engine.add_gateway(CtpGateway)

    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)

    print(f"连接 {symbol} ...")
    main_engine.connect(gateway_setting, "CTP")

    # 从这里开始会用到 main_engine,任何返回路径都必须关引擎(否则 CTP 原生线程不退出)
    try:
        return _collect(args, symbol, gateway_setting, event_engine, main_engine, collector)
    finally:
        close_engine(main_engine)


def _collect(args, symbol: str, gateway_setting, event_engine, main_engine, collector) -> int:
    """真正跑采集。单独拆出来是为了让 main() 用 try/finally 保证引擎一定被关掉。"""
    # 等合约表到位(否则发单会因为合约不存在而全被拒,测不到想测的东西)
    deadline = time.monotonic() + args.connect_timeout
    contract: ContractData | None = None
    while time.monotonic() < deadline:
        contract = collector.contract_of(symbol)
        if contract is not None:
            break
        # 登录失败时合约表永远不会来(第二套环境对新注册账号会回"不合法的登录",
        # 账号要到第三个交易日才能用),继续等满超时只是浪费时间,立刻报错退出。
        reason = collector.login_failed()
        if reason:
            print(f"交易登录失败,无法采集:{reason}", file=sys.stderr)
            print("提示:SimNow 第二套(7x24)对新注册账号要等到第三个交易日才能用;"
                  "第一套与实盘交易时间一致,约 16:00 关闭、21:00 开盘。", file=sys.stderr)
            return 6
        time.sleep(0.5)
    if contract is None:
        print(f"{args.connect_timeout:.0f} 秒内没有拿到 {symbol} 的合约信息。", file=sys.stderr)
        return 3

    # 发单前先查持仓:"无持仓时平仓"场景发的是平仓单,账户上若本来就有这个合约的持仓,
    # 那张单会**真的把仓位平掉**。所以有持仓就中止采集,不做任何委托。
    has_pos, pos_connected = symbol_has_position(
        main_engine, symbol, wait=float(getattr(args, "position_check_timeout", 15.0)))
    if has_pos:
        print(f"⚠ 账户上已有持仓,已中止采集(平仓场景会真的平掉仓位)。"
              f"请先确认并清空持仓后再采集。", file=sys.stderr)
        return 5
    if not pos_connected:
        print("⚠ 交易连接断开,无法确认账户持仓,已中止采集(不能据此认为没有持仓)。", file=sys.stderr)
        return 5

    # 订阅行情:有 Tick 时用真实最新价,没有就用本地日线收盘价
    from vnpy.trader.object import SubscribeRequest
    main_engine.subscribe(SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange), "CTP")
    tick_deadline = time.monotonic() + 30
    while time.monotonic() < tick_deadline and collector.tick_of(symbol) is None:
        time.sleep(0.5)

    # 基准价:有 Tick 用 Tick,非交易时段退回本地日线收盘价
    tick = collector.tick_of(symbol)
    if tick is not None:
        last_price = float(tick.last_price)
        source = "最新价"
    else:
        last_price, source = reference_price(symbol)
    now = datetime.now()
    in_session = in_trading_session(now)
    print(f"基准价 {last_price}(来源:{source}),合约单笔上限 {contract.max_volume},"
          f"当前{'处于' if in_session else '不在'}交易时段({now:%Y-%m-%d %H:%M:%S})")
    if in_session:
        print("  注意:当前在交易时段内,所有'价格合法'的场景改用越界价格,避免成交留下持仓;"
              "'非交易时段开仓'场景跳过。", file=sys.stderr)

    results: list[Case] = []
    deadline_all = time.monotonic() + 180
    for name, req in build_cases(symbol, contract.exchange, last_price, contract.max_volume,
                                 pricetick=contract.pricetick, in_session=in_session):
        if time.monotonic() > deadline_all:
            print("  (总时长到上限,剩余场景未采集)", file=sys.stderr)
            break
        ts = time.monotonic()
        # 记下当前错误条数:只在这一条之后的错误里找本次委托的原因,
        # 免得把**上一个场景**的旧错误当成这次的(旧错误离 ts 更近时会先被选中)。
        since = collector.error_index()
        vt_orderid = ""
        try:
            vt_orderid = main_engine.send_order(req, "CTP")
        except Exception as exc:
            # vn.py 的 CTP 网关对"合约不存在"会自己抛 KeyError(在 onRspOrderInsert 里),
            # 根本走不到 CTP 的错误文本 —— 这本身就是要记录的观察结果。
            results.append(Case(name=name,
                                detail=f"{req.symbol} {req.direction.value} {req.offset.value} {req.volume}@{req.price}",
                                error_text=f"[网关自身异常] {type(exc).__name__}: {exc}",
                                order_status="发单异常"))
            print(f"  {name}: 发单异常 {type(exc).__name__}: {exc}")
            continue
        order = collector.wait_order_final(vt_orderid, timeout=10.0)
        # 必须等错误日志:CTP 先推订单回报、后写错误日志(见 Collector.wait_error 的说明)
        text = collector.wait_error(ts, timeout=5.0, since=since)
        results.append(Case(
            name=name,
            detail=f"{req.symbol} {req.direction.value} {req.offset.value} {req.volume}@{req.price}",
            error_text=text,
            order_status=order.status.value if order else "无终态回报",
            order_id=vt_orderid,
        ))
        print(f"  {name}: 状态={results[-1].order_status} 错误={text or '(无错误日志)'}")
        time.sleep(1.0)

    # 跑完查持仓:本脚本的所有委托都不该成交,有非零持仓就是意外,必须告警
    positions, connected = check_positions(main_engine, symbol)
    if positions:
        print(f"⚠ 发现未平持仓 {positions},请立即人工核对并平仓!", file=sys.stderr)
    elif not connected:
        print("⚠ 连接已断开,无法查询持仓——**不能据此认为没有持仓**,请到 SimNow 页面人工确认。", file=sys.stderr)

    lines = [
        "<!-- BEGIN COLLECTED: 下面是脚本自动采集的原始表格,重新采集会替换这一段 -->",
        "",
        f"> 采集时间:{datetime.now():%Y-%m-%d %H:%M:%S}(SimNow 模拟盘);脚本:`scripts/collect_reject_texts.py`",
        f"> 合约:{symbol}",
        "",
        "| 场景 | 委托 | 订单状态 | CTP 错误文本 |",
        "|---|---|---|---|",
    ]
    for c in results:
        lines.append(f"| {c.name} | `{c.detail}` | {c.order_status} | {c.error_text or '(未捕获)'} |")
    lines.append("")
    lines.append("<!-- END COLLECTED -->")
    text = "\n".join(lines)
    print("\n" + text)
    if args.out:
        out = Path(args.out)
        write_collected(out, text)
        print(f"\n已写入 {out}")
    return 4 if positions else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as exc:
        # argparse 之类的参数错误会走 SystemExit,把它的退出码带出来
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # CTP 原生线程在断连风暴下可能不退出,强制结束进程(否则脚本"跑完了但不退出")
        os._exit(code)
