"""清场工具:把账户里的残留持仓平掉,让取证运行器的空仓闸门能放行。

为什么需要它
------------
取证运行器(`scripts/broker_test.py`)在会发单的步骤前有硬闸门:**账户必须空仓**
(ADR §5)。一旦某次演练/预演没能把仓位平掉,账户就会留下持仓,
之后每一次取证都会被闸门拦下,报告里所有下单类项目(2.1 稳定运行、报单、撤单…)
全部变成 FAIL —— 那是账户状态的问题,不是代码的问题,光看报告会误判。

手工去交易客户端点平仓当然可以,但这一步要能**重复执行、能留痕**:
谁在什么时候把哪个合约的多少手、以什么价平掉的,都写进 JSON 记录。

用法
----
    python scripts/flatten_positions.py                      # 只看不动(打印将发出的平仓单)
    python scripts/flatten_positions.py --confirm-flatten    # 真发平仓单
    python scripts/flatten_positions.py --profile .env.guoxin  # 指定档案(清的是那个档案的账户)
    # 计划里出现「买入平仓」时(净持仓账户的净多会被报成空头,那一单可能是加仓),
    # 必须先在交易客户端核对方向,再加 --confirm-direction 才能发:
    python scripts/flatten_positions.py --confirm-flatten --confirm-direction

**这个工具平的是账户里的**全部**持仓,不是 `--symbol` 指定的那个合约**
(`--symbol` 只是「额外订阅哪个合约的行情」)。要平哪个合约请在交易客户端里操作。

安全约束
--------
- 默认**只看不动**;真要发单必须显式加 `--confirm-flatten`;
- 计划里有「买入平仓」时还要 `--confirm-direction`:方向只在 stderr 提示一句是不够的,
  这个工具会动真实仓位,方向反了(净持仓账户的净多被报成空头 ⇒ 加仓)比不平更糟;
- 只允许连**模拟盘/测试盘**:交易前置与行情前置都必须在可信地址表内
  (国信测试前置或 SimNow)。生产 API **没有强制放行开关**,加什么参数都不行;
- 平仓方向与持仓方向相反,手数等于持仓手数;上期所/能源中心拆成今仓/昨仓两张单,
  **拆不出来时拒绝自动发单**(见 `split_is_reliable`),宁可让人工处理;
- 下单价取对手价(平多挂买一、平空挂卖一),主动打对手盘,确保能成交;
- 收尾必须再查一次持仓,为 0 才算成功;失败就把留在柜台上的单撤掉并大声告警;
- 发单过程中**任何异常**都要撤单清场(撤单要等到终态确认)。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402  必须最先导入:隔离 vn.py 状态目录 + 统一时区

FLATTEN_ROOT = bootstrap.ROOT / ".vntrader_flatten"
ISOLATED = bootstrap.isolate_state(FLATTEN_ROOT.name)

from ctp_tools.broker_targets import allowed_for_broker, resolve_allowed  # noqa: E402
from ctp_tools.collector import (  # noqa: E402
    Collector, close_engine, direction_text, merge_position_rows, normalize_direction, position_key,
    query_positions,
)
from ctp_tools.penetration import check_target, mask_account  # noqa: E402
from ctp_tools.profile import load_profile  # noqa: E402
from ctp_tools.simnow import normalize_address  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402
from risk.account_guard import beijing_now  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TICK  # noqa: E402
from vnpy.trader.object import OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_NO_POSITION = 3
EXIT_UNCONFIRMED = 4        # 查不到持仓(连接断/查询无回报),**不是**「没有持仓」

# 这些日志表示登录/授权已经失败,继续等下去没有意义。
LOGIN_FAILURE_MARKERS = ("交易服务器登录失败", "授权验证失败")


def login_failed(log_lines: list[str]) -> bool:
    """日志里是否已经出现登录/授权失败。"""
    return any(marker in line for line in log_lines for marker in LOGIN_FAILURE_MARKERS)


@dataclass
class ClosePlan:
    """一张平仓单的计划。"""

    vt_symbol: str
    code: str
    exchange: Exchange
    direction: Direction     # 平仓方向:SHORT=卖出平多,LONG=买入平空
    offset: Offset           # CLOSE / CLOSETODAY / CLOSEYESTERDAY
    volume: int
    price: float
    reason: str = ""        # 为什么是这个价(供留痕)

    def to_dict(self) -> dict:
        return {"vt_symbol": self.vt_symbol, "code": self.code, "exchange": self.exchange.value,
                "direction": direction_text(self.direction), "offset": self.offset.value,
                "volume": self.volume, "price": self.price, "reason": self.reason}


def split_is_reliable(pos) -> bool:
    """这个持仓的「今仓/昨仓」拆分是否可信(只有上期所/能源中心才需要拆)。

    vnpy_ctp 对上期所/能源中心的 `yd_volume` 只在**纯昨仓**时才写:
    `if data["YdPosition"] and not data["TodayPosition"]: position.yd_volume = data["Position"]`
    (vnpy_ctp/gateway/ctp_gateway.py)。所以:
    - 纯昨仓(昨=总手数)→ yd_volume == volume,拆得对,可以自动平;
    - 纯今仓 / 今昨混合 → yd_volume 恒为 0,拆出来的「平今手数」会超过实际今仓,
      柜台以「平今量超过今持仓量」拒单 —— 平仓平不掉,还容易被误判成柜台故障。

    拿不到可靠拆分时**拒绝自动发单**,交给人工处理:
    「平不掉」比「多让人工点两下」危险得多。
    """
    exchange = getattr(pos, "exchange", None)
    if exchange not in (Exchange.SHFE, Exchange.INE):
        return True
    volume = int(getattr(pos, "volume", 0) or 0)
    if volume <= 0:
        return True
    yd = int(getattr(pos, "yd_volume", 0) or 0)
    return yd == volume      # 只有「纯昨仓」这一种情况 yd_volume 才等于总手数


def _split_offsets(exchange: Exchange, volume: int, yd_volume: int) -> list[tuple[Offset, int]]:
    """上期所/能源中心区分今仓与昨仓,别的交易所"平仓"一张单就行。

    上期所/能源中心用普通平仓(Offset.CLOSE)会被交易所拒单,必须拆成
    平今(CLOSETODAY)/平昨(CLOSEYESTERDAY)两张单。

    调用前先用 `split_is_reliable()` 判断拆分是否可信——不可信时不要走到这里。
    """
    if exchange not in (Exchange.SHFE, Exchange.INE):
        return [(Offset.CLOSE, volume)]
    today = max(volume - yd_volume, 0)
    yesterday = min(yd_volume, volume)
    out: list[tuple[Offset, int]] = []
    if today:
        out.append((Offset.CLOSETODAY, today))
    if yesterday:
        out.append((Offset.CLOSEYESTERDAY, yesterday))
    return out


def plan_closes(positions: list, quotes: dict[str, tuple[float, float, float]],
                priceticks: dict[str, float] | None = None) -> list[ClosePlan]:
    """把持仓翻译成平仓单计划。

    positions:vn.py 的 PositionData 列表(只取 volume > 0 的)
    quotes:    {vt_symbol: (bid_price_1, ask_price_1, last_price)}
    priceticks:{vt_symbol: 最小变动价位}(取不到就按 1.0)

    - 平多(持仓方向 LONG)→ 卖出(SHORT),挂在**买一**上主动打买盘;
    - 平空(持仓方向 SHORT)→ 买入(LONG),挂在**卖一**上主动打卖盘;
    - 方向是 NET → **不猜**:方向不明就放弃,交给上层报错。
      注意:CTP 网关**不会**回报 NET —— `vnpy_ctp` 的 `DIRECTION_CTP2VT` 是从
      `DIRECTION_VT2CTP` 反推的,键 '1'(PosiDirection=净)与 D_Sell 撞车,
      于是**净持仓账户的净多会被报成 `Direction.SHORT`**(ctp_gateway.py 的
      `DIRECTION_CTP2VT[data["PosiDirection"]]`)。双边持仓账户不受影响。
      所以"平空"这一路在净持仓账户上可能是**加仓**——这个盲区没法在代码里判掉,
      只能由人在发单前拿交易客户端核对方向(见 `run()` 里的提示)。
    - 今昨仓拆分不可信(上期所/能源中心的纯今仓与混合仓)→ **不生成单**,
      见 `split_is_reliable`;这类持仓由 `manual_positions()` 列出来交给人工。
    """
    priceticks = priceticks or {}
    # 同一个 (合约, 方向) 只出一张单:一次查询窗口里可能出现**两次快照**的两行
    # (我们主动查的那次 + 网关约每 4 秒自动查的那次,2026-11-16 实测),
    # 两张单会各平一手 —— 3 手持仓就变成 6 手平仓单,超量的平仓单会被柜台整单拒绝
    # (错误码 30「平仓量超过持仓量」),仓位没平掉,取证流程照样卡住。
    # 这里再合一次是**发单前的最后一道闸**:上游合过了也要自己合。
    positions = merge_position_rows(positions)
    plans: list[ClosePlan] = []
    for pos in positions:
        volume = int(getattr(pos, "volume", 0) or 0)
        if volume <= 0:
            continue
        if not split_is_reliable(pos):
            continue
        vt_symbol = pos.vt_symbol
        code, _, product = vt_symbol.rpartition(".")
        exchange = getattr(pos, "exchange", None)
        if exchange is None or not code:
            continue
        bid, ask, last = quotes.get(vt_symbol, (0.0, 0.0, 0.0))
        # 分派也走**归一化后的方向键**,与合并键同源:合并用的是 `normalize_direction`
        # (枚举名),若这里按枚举相等分派,一行 `direction` 是字符串 "Long" 的持仓会被
        # 合进 LONG 键却在分派时被跳过 → 该持仓不进平仓计划,留在账户上
        # (2026-11-16 codex 复核 L1)。
        pos_dir = normalize_direction(pos.direction)
        if pos_dir == "LONG":
            direction, raw_price = Direction.SHORT, bid
            reason = "平多:卖出挂在买一"
        elif pos_dir == "SHORT":
            direction, raw_price = Direction.LONG, ask
            reason = "平空:买入挂在卖一"
        else:
            continue                      # NET 或认不出来的方向:交给上层报错/人工处理
        if raw_price <= 0:
            raw_price = last
            reason += "(盘口没有对手价,退回最新价)"
        # 最小变动价位必须来自合约:不同品种差别很大(玉米 1,国债 0.001),
        # 价格不是它的整数倍时柜台会以「价格非法」拒单,平仓就平不掉。
        tick = float(priceticks.get(vt_symbol, 0) or 0)
        if tick <= 0:
            tick = 1.0
            reason += "(没有合约信息,按最小变动 1 计价)"
        price = round_to(raw_price, tick) if raw_price > 0 else 0.0
        for offset, vol in _split_offsets(exchange, volume, int(getattr(pos, "yd_volume", 0) or 0)):
            plans.append(ClosePlan(
                vt_symbol=vt_symbol, code=code, exchange=exchange,
                direction=direction, offset=offset, volume=vol, price=price, reason=reason,
            ))
    return plans


def manual_positions(positions: list) -> list[str]:
    """必须人工处理的持仓,返回给人看的说明。

    三种情况:
    - 上期所/能源中心的今昨仓拆分不可靠
      (vnpy_ctp 只在纯昨仓时写 yd_volume,见 `split_is_reliable`);
    - **净持仓**(NET):方向认得出来,但净仓没有「多/空」可平,自动发单会变成开仓 ⇒ 人工处理;
    - 方向**认不出来**(归一化后既不是 LONG/SHORT/NET,包括方向为空的行):
      这类行进不了平仓计划(`plan_closes` 只认 LONG/SHORT),方向为空时连合并键都取不到
      (`position_key` 返回 None)会被 `merge_position_rows` 丢掉 ⇒ 既不进计划也不进对账,
      不点名的话它就「隐形」了(2026-11-16 codex 复核 L3)。
      vnpy_ctp 路径上净仓实际不可达(净仓被报成 SHORT),但工具不能靠「不可达」当保证。

    同一 (合约, 方向) 出现两行是常态(网关自动查 + 我们主动查,两份快照混在一个窗口里,
    见 `merge_position_rows` 的说明)。人工清单要用**合并后**的行,否则会重复列两行、
    而且手数取到较旧的那次快照(2026-11-16 codex 复核 L3)。
    但方向为空的行连合并键都取不到,它们会被合并层丢掉 —— 所以这类行**单独**保留,
    不能因为「已经合并过了」就没人点名。
    """
    latest: dict[tuple[str, str], object] = {}
    unkeyed: list = []
    for pos in positions:
        key = position_key(pos)
        if key is None:
            unkeyed.append(pos)
        else:
            latest[key] = pos          # 同键取**最后一次**那行(与 merge_position_rows 同口径)
    out: list[str] = []
    for pos in list(latest.values()) + unkeyed:
        if int(getattr(pos, "volume", 0) or 0) <= 0:
            continue
        if not split_is_reliable(pos):
            # 方向同样要**归一化后按白名单**显示:认不出来的方向显示「无方向」,
            # 别把内部枚举名原样漏给看报告的人(与 probe_position 一致,
            # 2026-11-16 codex 复核 L7)。
            known = normalize_direction(getattr(pos, "direction", None))
            shown = direction_text(pos.direction) if known in ("LONG", "SHORT", "NET") else "无方向"
            out.append(f"{pos.vt_symbol} {shown} {pos.volume} 手"
                       "(上期所/能源中心的今昨仓拆不出来,自动平仓会被柜台拒单,请人工平仓)")
            continue
        name = normalize_direction(getattr(pos, "direction", None))
        if name == "NET":
            out.append(f"{pos.vt_symbol} 净 {pos.volume} 手"
                       "(净持仓没有多空可平,自动发单会变成开仓,请人工核对后平仓)")
            continue
        if name not in ("LONG", "SHORT"):
            out.append(f"{pos.vt_symbol} 方向未知 {pos.volume} 手"
                       "(持仓方向认不出来,不会自动发平仓单,请人工核对方向后平仓)")
    return out


def _quotes_from(collector: Collector) -> dict[str, tuple[float, float, float]]:
    out: dict[str, tuple[float, float, float]] = {}
    with collector.lock:
        for symbol, tick in collector.ticks.items():
            out[symbol] = (float(tick.bid_price_1 or 0), float(tick.ask_price_1 or 0), float(tick.last_price or 0))
    return out


def _wait_positions(main_engine, collector, wait: float) -> tuple[list, bool]:
    """查一次持仓,返回 (volume>0 的 PositionData 列表, 数据是否可信)。

    **只看本次查询回报里的行,不看 OMS 快照**:
    vn.py 的 `OmsEngine.process_position_event` 只做 `self.positions[id] = position`,
    **从不删除条目**;而 vnpy_ctp 只在持仓查询回报 `last=True` 时推送**本次返回的**行。
    合起来的后果:平仓成功后 OMS 里那条旧持仓永远留着 —— 工具会说「未平干净」,
    取证运行器的空仓闸门会把后面所有发单步骤判成「账户已有持仓」而全部中止。
    仓库自己在 `trading/guard_rule.py:16` 记过同一件事:
    「按**成交量**对账,而不是反复读取 OMS 持仓」。

    **「查不到」和「没有」必须分开**:柜台没连上、持仓查询没有回报时,
    一样是空列表,但前者绝不能当成「账户是空的」。查询不可信时返回
    `( [], False )`,调用方不能据此下结论。
    """
    rows, status = query_positions(main_engine, collector, wait=min(wait, 5.0))
    if status == "untrusted":
        return [], False
    return [p for p in rows if int(getattr(p, "volume", 0) or 0) > 0], True


def reconcile_filled(initial: list, remaining: list, results: list) -> tuple[bool, list[str]]:
    """按**成交量**对账:平掉的量必须等于初始持仓量。

    返回 (是否对得上, 说明列表)。对不上的两种情况:
    1. 还有剩余持仓 → 根本没平干净;
    2. 查询显示持仓没了,但成交量对不上(有单没成交,或者仓位被别的操作平掉了)
       → 来历不明,升级人工核对。

    为什么不能只看「查询显示空仓」:持仓查询的回报和成交回报之间有延迟
    (CTP 的持仓回报约每 2 秒才刷新一次),只看一次会把「还没刷新」当成「已平空」。
    """
    def key_of(pos) -> tuple[str, str]:
        direction = getattr(pos, "direction", None)
        # 方向用**语言无关的枚举名**当键:vn.py 的 Direction 走 gettext,`.value`
        # 随 locale 变(英文 'Long' / 中文 '多'),拿它当键会让同一份持仓在两台机器上
        # 归出不同的键 → 对账两边对不上 → 报假差异。
        return pos.vt_symbol, normalize_direction(direction) or str(direction)

    def vol_of(pos) -> int:
        return int(getattr(pos, "volume", 0) or 0)

    # 键 = (合约, 持仓方向):双向持仓(多空同时有)不能互相覆盖。
    # 两侧都先走 `merge_position_rows`:发单计划与事后对账必须用**同一套**口径,
    # 否则会出现"计划按 5 手、对账按 3 手"这种自相矛盾的行数(2026-11-16 codex 复核提出)。
    initial_vol = {key_of(p): vol_of(p) for p in merge_position_rows(initial) if vol_of(p) > 0}
    left_vol = {key_of(p): vol_of(p) for p in merge_position_rows(remaining) if vol_of(p) > 0}
    # 平仓单的方向与持仓方向相反:卖出(空)平的是多头。
    # 留痕里的方向可能是旧格式的英文文案('Long'/'Short')或新的中文('多'/'空'),
    # 两种都要认得 → 统一先归一成枚举名。
    opposite = {normalize_direction(Direction.SHORT): normalize_direction(Direction.LONG),
                normalize_direction(Direction.LONG): normalize_direction(Direction.SHORT)}
    traded: dict[tuple[str, str], int] = {}
    for item in results:
        close_dir = normalize_direction(item.get("direction", "")) or item.get("direction", "")
        key = (item.get("vt_symbol", ""), opposite.get(close_dir, close_dir))
        traded[key] = traded.get(key, 0) + int(item.get("成交手数", 0) or 0)
    problems: list[str] = []
    for key, expected in initial_vol.items():
        symbol = f"{key[0]} {direction_text(key[1])}"
        left = left_vol.get(key, 0)
        done = traded.get(key, 0)
        if left > 0:
            problems.append(f"{symbol} 仍有 {left} 手(初始 {expected} 手,成交 {done} 手)")
        elif done != expected:
            if key not in traded:
                problems.append(f"{symbol} 查询显示已无持仓,但**没有为它发出过平仓单**"
                                f"(初始 {expected} 手):多半是今昨仓拆分不可靠、已转人工处理,"
                                f"请核对柜台上的实际仓位")
            else:
                problems.append(f"{symbol} 查询显示已无持仓,但只成交了 {done} 手(初始 {expected} 手),"
                                f"差额 {expected - done} 手来历不明(有单未成交或被其他操作平掉),请人工核对柜台")
    # 初始快照里没有、但现在冒出来的持仓(快照被截断/期间有人开了新仓):同样不能报「已平空」。
    for key, left in left_vol.items():
        if key not in initial_vol:
            problems.append(f"{key[0]} {direction_text(key[1])} 出现初始快照之外的持仓 {left} 手"
                            f"(快照不完整或期间有其他操作),请人工核对柜台")
    return (not problems), problems


def _send_and_wait(main_engine, collector: Collector, plan: ClosePlan, timeout: float) -> tuple[str, int, bool]:
    """发一张平仓单并等到终态。返回 (状态说明, 成交手数, 是否全部成交)。

    成交手数要单独带出来:收尾判定按**成交量**对账(见 `reconcile_filled`),
    只看「查询显示空仓」会把「持仓回报还没刷新」当成「已平空」。
    """
    req = OrderRequest(symbol=plan.code, exchange=plan.exchange,
                       direction=plan.direction, offset=plan.offset,
                       price=plan.price, volume=plan.volume, type=OrderType.LIMIT)
    order_id = main_engine.send_order(req, "CTP")
    if not order_id:
        return "send_order 没有返回委托号", 0, False
    order = collector.wait_order_final(order_id, timeout=timeout)
    if order is None:
        return f"{timeout:.0f}s 内没有收到终态回报(委托可能仍挂在柜台)", 0, False
    traded = int(getattr(order, "traded", 0) or 0)
    if order.status == Status.REJECTED:
        return f"被拒绝:{order.status_msg or '无原因'}", traded, False
    if traded and traded >= plan.volume:
        return f"全部成交 {traded} 手(状态 {order.status.value})", traded, True
    if traded:
        return f"部分成交 {traded}/{plan.volume} 手(状态 {order.status.value})", traded, False
    return f"未成交(状态 {order.status.value})", 0, False


def run(args: argparse.Namespace) -> int:
    # 档案优先:--profile 指定就用它,否则用项目 .env。
    # 「用哪个档案」必须由操作员明确说出来:一键流程会把 .env 换成国信档案,
    # 万一在换回来之前跑清场,平的就是**另一个账户**的仓位。
    try:
        profile = load_profile(args.profile, root=bootstrap.ROOT)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if profile.hard_conflicts:
        # 硬冲突:进程环境与项目 .env 打架,而且没有指定档案 —— 两个来源谁对谁错
        # 只能猜。猜错就会平到**另一个账户**的仓位上,所以停下。
        print("进程环境变量与项目 .env 不一致(又没有指定档案),无法判断该连哪台机器,已退出:"
              "请清掉这些环境变量(set VAR= 后重开终端),或用 --profile 明确指定档案。", file=sys.stderr)
        for c in profile.hard_conflicts:
            print(f"  - {c}", file=sys.stderr)
        return EXIT_CONFIG
    for note in profile.notes:
        print(f"⚠ {note}(已按档案取值)", file=sys.stderr)
    os_env = profile.env
    try:
        setting = build_ctp_setting(os_env)
    except ValueError as exc:
        print(f"配置错误:{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not ISOLATED:
        print("状态目录隔离未生效,拒绝连接(日志会写进实盘目录)。", file=sys.stderr)
        return EXIT_CONFIG

    # 前置地址闸门:白名单只能收窄不能放宽 —— 用户给的白名单与代码里写死的
    # 可信地址全集求交集,交集之外的地址一律拒绝(生产 API 没有任何放行开关)。
    # 可信地址全集**按 BrokerID 绑定**:国信测试档只认国信前置,SimNow 档只认
    # SimNow 前置 —— 否则「国信档案 + SimNow 地址」也能通过校验,等于把
    # 两个环境混在一起(清场平的不是取证要连的那个账户)。
    known_td, known_md = allowed_for_broker(setting["经纪商代码"])
    if not known_td:
        print(f"未知的经纪商代码 {setting['经纪商代码']!r}:没有与它绑定的可信前置地址表,拒绝连接。",
              file=sys.stderr)
        return EXIT_CONFIG
    allowed_td, dropped_td = resolve_allowed(os_env.get("BROKER_TEST_ALLOWED_TD", ""), known_td)
    allowed_md, dropped_md = resolve_allowed(os_env.get("BROKER_TEST_ALLOWED_MD", ""), known_md)
    for label, dropped in (("交易", dropped_td), ("行情", dropped_md)):
        if dropped:
            print(f"⚠ {label}前置白名单里的 {dropped} 不属于经纪商代码 {setting['经纪商代码']!r} "
                  "对应的可信地址表,已丢弃(白名单与地址同源,采信它等于自证清白)。", file=sys.stderr)
    address = normalize_address(setting["交易服务器"])
    md_address = normalize_address(setting["行情服务器"])
    environment = setting["柜台环境"]
    # confirmed=True:这里的「确认」由**地址表**完成 —— 白名单已经被限制在
    # 「代码里写死的、与 BrokerID 绑定的可信地址全集」内,交易前置不在表内
    # 就直接拒绝;真正要不要**发单**由 --confirm-flatten 决定(默认只看不动)。
    ok, note = check_target(environment, setting["交易服务器"], True, allowed_td)
    print(note)
    if not ok:
        return EXIT_CONFIG
    # 行情前置同样要查:平仓单的价格就是从行情前置的盘口来的,
    # 连错行情前置 = 用假价格平仓(可能平不掉,也可能成交在错价上)。
    if not allowed_md:
        print("行情前置白名单为空(BROKER_TEST_ALLOWED_MD 没配,或里面没有可信地址),拒绝连接。",
              file=sys.stderr)
        return EXIT_CONFIG
    if md_address not in allowed_md:
        print(f"行情前置 {md_address} 不在可信前置地址表内,拒绝连接:"
              "平仓价来自行情前置的盘口,不能靠「交易前置是白名单内的」来推定它也是。", file=sys.stderr)
        return EXIT_CONFIG

    symbol = args.symbol or os_env.get("LIVE_VT_SYMBOL", "")
    from vnpy.event import EventEngine
    from vnpy.trader.engine import MainEngine
    from vnpy_ctp import CtpGateway

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)
    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)
    event_engine.register(EVENT_POSITION, collector.on_position)
    event_engine.register(EVENT_ACCOUNT, collector.on_account)   # 持仓查询「没有行」时用账户回报证明柜台在应答

    record: dict = {"时间": beijing_now().strftime("%Y-%m-%d %H:%M:%S"),
                    "档案": args.profile or "(项目 .env)",
                    "交易前置": address, "行情前置": md_address, "柜台环境": environment,
                    "账号": mask_account(setting["用户名"]), "确认发单": bool(args.confirm_flatten),
                    "计划": [], "结果": [], "收尾持仓": []}
    out_dir = bootstrap.ROOT / "data" / "flatten"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"flatten-{beijing_now().strftime('%Y%m%d-%H%M%S')}.json"

    def save_record() -> None:
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"连接 {address}(账号 {record['账号']})...")
    main_engine.connect(setting, "CTP")
    try:
        # 等登录 + 合约表:vnpy_ctp 的持仓查询在合约表到达前直接返回,查不到任何东西。
        deadline = time.monotonic() + args.connect_timeout
        contract_ready = False
        while time.monotonic() < deadline:
            with collector.lock:
                logs = list(collector.log_lines)
            if any("合约信息查询成功" in line for line in logs):
                contract_ready = True
                break
            if login_failed(logs):
                print("登录失败,放弃。", file=sys.stderr)
                record["结论"] = "登录失败"
                save_record()
                return EXIT_CONFIG
            time.sleep(0.5)
        if not contract_ready:
            print(f"{args.connect_timeout:.0f} 秒内没有拿到合约表,持仓查询不可信,放弃。", file=sys.stderr)
            record["结论"] = "没有拿到合约表,持仓查询不可信"
            save_record()
            return EXIT_CONFIG

        positions, trusted = _wait_positions(main_engine, collector, args.position_wait)
        if not positions and trusted:
            # CTP 对空账户**不推送任何持仓事件**,所以「没有行」可能是
            # 「账户真的空」,也可能是「回报还没到」。再查一轮才下结论。
            positions, trusted = _wait_positions(main_engine, collector, args.position_wait)
        held = [f"{p.vt_symbol} {direction_text(p.direction)} {p.volume}" for p in positions]
        record["持仓"] = held
        if not trusted:
            # 「查不到」不是「没有」:连接断了或查询没有回报,这时说什么都是猜。
            # 注意这一条**不看有没有查到持仓**——查询不可信时,查到的那些行
            # 也不能当作完整的持仓清单(可能只到了一半)。
            print("查不到持仓(交易通道不在线或持仓查询没有回报),无法确认账户是否空仓,放弃。",
                  file=sys.stderr)
            record["结论"] = "持仓查询不可信,未做任何判断"
            save_record()
            return EXIT_UNCONFIRMED
        print(f"当前非零持仓 {len(held)} 项:{held if held else '(空)'}")
        if not positions:
            print("账户已空,不需要平仓。")
            record["结论"] = "账户本来就是空的"
            save_record()
            return EXIT_NO_POSITION

        manual = manual_positions(positions)
        if manual:
            print("以下持仓不能自动平仓,不会自动发单,请人工处理:", file=sys.stderr)
            for m in manual:
                print(f"  - {m}", file=sys.stderr)
            record["需人工处理"] = manual

        # 有几份持仓就订阅几个合约:只订阅 --symbol 指定的合约的话,
        # 别的持仓合约就拿不到对手价,定价会退化成 0 → 拒单。
        wanted = {p.vt_symbol for p in positions}
        if symbol:
            wanted.add(symbol)
        priceticks: dict[str, float] = {}
        for vt_symbol in sorted(wanted):
            contract = main_engine.get_contract(vt_symbol) or collector.contract_of(vt_symbol)
            if contract is None:
                print(f"  {vt_symbol}:没有合约信息,跳过订阅")
                continue
            main_engine.subscribe(SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange), "CTP")
            if getattr(contract, "pricetick", 0):
                priceticks[vt_symbol] = float(contract.pricetick)
        if wanted:
            time.sleep(min(args.tick_wait, 20.0))     # 等一根 Tick 拿对手价

        plans = plan_closes(positions, _quotes_from(collector), priceticks)
        if not plans:
            print("有持仓但生成不出平仓单(方向不明、合约信息缺失,或上期所/能源中心的今昨仓不可靠),"
                  "请人工在交易客户端平仓。", file=sys.stderr)
            record["结论"] = "无法生成平仓单,需人工处理"
            save_record()
            return EXIT_FAILED
        for plan in plans:
            print(f"  计划:{'卖出' if plan.direction == Direction.SHORT else '买入'}平仓 {plan.vt_symbol} "
                  f"{plan.volume} 手 @ {plan.price} ({plan.offset.value},{plan.reason})")
        # 「买入平仓」对应的是柜台回报的**空头**持仓。要点名的原因:CTP 网关把
        # 净持仓账户的**净多**也报成空头(PosiDirection=净 → Direction.SHORT),
        # 那种情况下这单是**加仓**。双边持仓账户不受影响,但我们分不出这两种情况
        # → 交给人在 --confirm-flatten 之前拿交易客户端核对方向。
        needs_direction_check = any(p.direction == Direction.LONG for p in plans)
        if needs_direction_check:
            print("⚠ 上面的「买入平仓」对应柜台回报的**空头**持仓。注意:净持仓账户的净多"
                  "会被 vnpy_ctp 报成空头,那种情况下这单是加仓。请先在交易客户端核对方向,"
                  "再决定要不要 --confirm-flatten。", file=sys.stderr)
            record["方向核对提示"] = ("柜台回报空头持仓;净持仓账户的净多也会被报成空头,"
                                     "已提示人工在发单前核对方向")
        record["计划"] = [p.to_dict() for p in plans]
        # 没有行情就定不出价:非交易时段订阅不到 Tick,挂出来的委托会被拒,
        # 挂在柜台上的单还可能变成"晚到的成交"。这里直接拒绝,让操作者到交易时段再跑。
        if any(p.price <= 0 for p in plans):
            print("没有行情(Tick),无法给平仓单定价——现在多半不在交易时段。"
                  "请在交易时段内重跑本脚本。", file=sys.stderr)
            record["结论"] = "无行情,无法定价,未发单"
            save_record()
            return EXIT_FAILED

        if not args.confirm_flatten:
            print("未加 --confirm-flatten:只打印计划,不发任何委托。")
            if needs_direction_check:
                print("  提示:计划里有「买入平仓」,真要发单还得再加 --confirm-direction"
                      "(先在交易客户端核对过方向)。", file=sys.stderr)
            record["结论"] = "dry-run"
            save_record()
            return EXIT_OK

        # 方向不明的「买入平仓」不许自动发:这个工具是**会动真实仓位**的,
        # 一句 stderr 提示不算闸门 —— 读完提示时单子已经在路上。
        # 净持仓账户的净多会被报成空头,那一单是**加仓**而不是平仓(方向反了比不平更糟)。
        # 所以:必须人工在交易客户端核对过方向,再加 --confirm-direction 才放行
        # (2026-11-16 codex 复核 M4)。
        if needs_direction_check and not args.confirm_direction:
            print("✗ 存在「买入平仓」计划,而净持仓账户的净多会被报成空头 ⇒ 这一单可能是**加仓**而不是平仓。"
                  "请先在交易客户端核对持仓方向;确认无误后再加 --confirm-direction 重跑。", file=sys.stderr)
            record["结论"] = "方向待人工核对(缺 --confirm-direction),未发单"
            save_record()
            return EXIT_CONFIG

        all_filled = True
        try:
            for plan in plans:
                status, traded, filled = _send_and_wait(main_engine, collector, plan, args.fill_timeout)
                print(f"  结果:{plan.vt_symbol} {plan.volume} 手 @ {plan.price} → {status}")
                record["结果"].append({**plan.to_dict(), "状态": status, "成交": filled, "成交手数": traded})
                all_filled = all_filled and filled
                if not filled:
                    # 没成交的单还挂在交易所上,过一会儿成交就会留下仓位 → 撤掉
                    _cancel_all(main_engine, collector)
        except Exception as exc:      # noqa: BLE001  发单途中出错也必须清场
            print(f"发单过程中出错({exc!r}),撤单清场。", file=sys.stderr)
            record["异常"] = repr(exc)
            still_active, failures = _cancel_all(main_engine, collector)
            record["撤单未确认"] = still_active
            record["撤单失败"] = failures
            if still_active:
                print(f"⚠ 还有 {len(still_active)} 张单可能挂在柜台上:{still_active}", file=sys.stderr)
            save_record()
            raise
        finally:
            # 正常收尾也要确认没有遗留挂单:撤单请求发出 ≠ 撤成功。
            still_active, failures = _cancel_all(main_engine, collector)
            if still_active or failures:
                record["撤单未确认"] = still_active
                record["撤单失败"] = failures
                print(f"⚠ 撤单没有全部确认(仍在活动:{still_active},失败:{failures})", file=sys.stderr)

        # 收尾检查:持仓回报的刷新比成交回报慢(CTP 约 2 秒一次),
        # 所以这里**轮询**到位置为止,不能只查一次就下结论。
        deadline = time.monotonic() + max(args.position_wait, 5.0)
        after: list = []
        after_trusted = True
        while True:
            after, after_trusted = _wait_positions(main_engine, collector, args.position_wait)
            if not after or not after_trusted or time.monotonic() >= deadline:
                break
            time.sleep(1.0)
        left = [f"{p.vt_symbol} {direction_text(p.direction)} {p.volume}" for p in after]
        record["收尾持仓"] = left
        print(f"收尾检查:剩余持仓 {len(left)} 项:{left if left else '(空)'}")
        if not after_trusted:
            print("⚠ 收尾的持仓查询不可信(通道不在线),无法确认账户已空,请人工核对柜台。", file=sys.stderr)
            record["结论"] = "收尾持仓查询不可信,请人工核对"
            save_record()
            return EXIT_UNCONFIRMED
        # 按**成交量**对账:查询显示空了还不够,平掉的量必须等于初始持仓量。
        matched, problems = reconcile_filled(positions, after, record["结果"])
        if not matched:
            print("⚠ 平仓对账不通过:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            record["结论"] = "未平干净或成交量对不上,请人工核对"
            record["对账问题"] = problems
            save_record()
            return EXIT_FAILED
        record["结论"] = "已平空" if all_filled else "持仓已清空(有单未成交,已撤单)"
        save_record()
        print(f"留痕:{out_path}")
        return EXIT_OK
    finally:
        close_engine(main_engine)


def _cancel_all(main_engine, collector: Collector, timeout: float = 15.0) -> list[str]:
    """撤掉本进程发出的、还挂在柜台上的委托(收尾兜底),并等到终态。

    撤单请求发出去不等于撤成功:委托可能已经成交(变成持仓),也可能还在柜台上。
    所以每张单都要等到终态才收工,没等到终态的要单独报出来——
    ADR §5「撤单要等确认终态」。

    返回 (仍挂在柜台上的委托号, 撤单失败的说明)。
    """
    with collector.lock:
        orders = list(collector.orders.values())
    still_active: list[str] = []
    failures: list[str] = []
    for order in orders:
        if not order.is_active():
            continue
        try:
            main_engine.cancel_order(order.create_cancel_request(), "CTP")
        except Exception as exc:      # 撤单失败不影响主结论,但要留下痕迹
            failures.append(f"{order.vt_orderid}:{exc!r}")
            continue
        final = collector.wait_order_final(order.vt_orderid, timeout=timeout)
        if final is None:
            failures.append(f"{order.vt_orderid}:撤单后 {timeout:.0f}s 内没有收到终态(可能还挂在柜台上)")
        elif final.is_active():
            still_active.append(order.vt_orderid)
    return still_active, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--profile", default="", help="用指定档案(如 .env.guoxin);默认用项目 .env")
    parser.add_argument("--symbol", default="", help="额外订阅这个合约的行情(默认取 .env 的 LIVE_VT_SYMBOL);"
                                                    "**平仓范围不受它影响**:本工具平的是账户里的全部持仓")
    parser.add_argument("--confirm-flatten", action="store_true", help="确认真的要发出平仓委托")
    parser.add_argument("--confirm-direction", action="store_true",
                        help="确认已在交易客户端核对过持仓方向(计划里出现「买入平仓」时必须一起给,"
                             "否则拒绝发单:净持仓账户的净多会被报成空头,那一单是加仓)")
    parser.add_argument("--connect-timeout", type=float, default=300.0,
                        help="等待合约表就绪的最长时间(秒);柜台的合约表查询实测要 90 秒以上")
    parser.add_argument("--position-wait", type=float, default=15.0)
    parser.add_argument("--tick-wait", type=float, default=20.0)
    parser.add_argument("--fill-timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    return run(args)


if __name__ == "__main__":
    code = main()
    # CTP 的原生线程是非守护线程:close_engine 超时后进程不会自己退出(实测会挂住)。
    # 强制退出,退出码照常返回。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
