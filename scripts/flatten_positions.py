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

安全约束
--------
- 默认**只看不动**;真要发单必须显式加 `--confirm-flatten`;
- 只允许连**模拟盘**:交易前置必须在 SimNow 白名单内,或「柜台环境=测试」
  且地址在 `BROKER_TEST_ALLOWED_TD` 白名单内。
  生产 API(实盘 + 非白名单地址)**没有强制放行开关**,加什么参数都不行;
- 平仓方向与持仓方向相反,手数等于持仓手数;上期所/能源中心拆成今仓/昨仓两张单;
- 下单价取对手价(平多挂买一、平空挂卖一),主动打对手盘,确保能成交;
- 收尾必须再查一次持仓,为 0 才算成功;失败就把留在柜台上的单撤掉并大声告警。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402  必须最先导入:隔离 vn.py 状态目录 + 统一时区

FLATTEN_ROOT = bootstrap.ROOT / ".vntrader_flatten"
ISOLATED = bootstrap.isolate_state(FLATTEN_ROOT.name)

from ctp_tools.broker_targets import SIMNOW_MD  # noqa: E402
from ctp_tools.collector import Collector, check_positions, close_engine  # noqa: E402
from ctp_tools.penetration import check_target, mask_account  # noqa: E402
from ctp_tools.simnow import is_simnow_address, normalize_address  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK  # noqa: E402
from vnpy.trader.object import OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_CONFIG = 2
EXIT_NO_POSITION = 3


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
                "direction": self.direction.value, "offset": self.offset.value,
                "volume": self.volume, "price": self.price, "reason": self.reason}


def _split_offsets(exchange: Exchange, volume: int, yd_volume: int) -> list[tuple[Offset, int]]:
    """上期所/能源中心区分今仓与昨仓,别的交易所"平仓"一张单就行。

    上期所/能源中心用普通平仓(Offset.CLOSE)会被交易所拒单,必须拆成
    平今(CLOSETODAY)/平昨(CLOSEYESTERDAY)两张单。
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
    - 方向是 NET(净持仓账户)→ **不猜**:净持仓回报里没有多空之分,
      猜错方向就是"平仓变成开仓",直接放弃并让调用方报错。
    """
    priceticks = priceticks or {}
    plans: list[ClosePlan] = []
    for pos in positions:
        volume = int(getattr(pos, "volume", 0) or 0)
        if volume <= 0:
            continue
        vt_symbol = pos.vt_symbol
        code, _, product = vt_symbol.rpartition(".")
        exchange = getattr(pos, "exchange", None)
        if exchange is None or not code:
            continue
        bid, ask, last = quotes.get(vt_symbol, (0.0, 0.0, 0.0))
        if pos.direction == Direction.LONG:
            direction, raw_price = Direction.SHORT, bid
            reason = "平多:卖出挂在买一"
        elif pos.direction == Direction.SHORT:
            direction, raw_price = Direction.LONG, ask
            reason = "平空:买入挂在卖一"
        else:
            continue                      # NET:方向不明,交给上层报错
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


def _allowed_addresses(raw: str) -> set[str]:
    return {normalize_address(x) for x in raw.replace(";", ",").split(",") if x.strip()}


def _quotes_from(collector: Collector) -> dict[str, tuple[float, float, float]]:
    out: dict[str, tuple[float, float, float]] = {}
    with collector.lock:
        for symbol, tick in collector.ticks.items():
            out[symbol] = (float(tick.bid_price_1 or 0), float(tick.ask_price_1 or 0), float(tick.last_price or 0))
    return out


def _wait_positions(main_engine, symbol: str, wait: float) -> list:
    """查一次持仓,返回 volume>0 的 PositionData 列表(等满窗口,拿异步回报)。"""
    gateway = main_engine.get_gateway("CTP")
    try:
        gateway.query_position()
    except Exception:
        pass
    deadline = time.monotonic() + wait
    found: list = []
    while time.monotonic() < deadline:
        found = [p for p in main_engine.get_all_positions() if p.volume > 0]
        if found:
            break
        time.sleep(0.5)
    return found


def _send_and_wait(main_engine, collector: Collector, plan: ClosePlan, timeout: float) -> tuple[str, str]:
    """发一张平仓单并等到终态。返回 (状态说明, 是否成交)。"""
    req = OrderRequest(symbol=plan.code, exchange=plan.exchange,
                       direction=plan.direction, offset=plan.offset,
                       price=plan.price, volume=plan.volume, type=OrderType.LIMIT)
    order_id = main_engine.send_order(req, "CTP")
    if not order_id:
        return "send_order 没有返回委托号", False
    order = collector.wait_order_final(order_id, timeout=timeout)
    if order is None:
        return f"{timeout:.0f}s 内没有收到终态回报(委托可能仍挂在柜台)", False
    if order.status == Status.REJECTED:
        return f"被拒绝:{order.status_msg or '无原因'}", False
    if order.traded and order.traded >= plan.volume:
        return f"全部成交 {order.traded} 手(状态 {order.status.value})", True
    if order.traded:
        return f"部分成交 {order.traded}/{plan.volume} 手(状态 {order.status.value})", False
    return f"未成交(状态 {order.status.value})", False


def run(args: argparse.Namespace) -> int:
    env = bootstrap.load_env()
    os_env = os.environ
    try:
        setting = build_ctp_setting(os_env)
    except ValueError as exc:
        print(f"配置错误:{exc}", file=sys.stderr)
        return EXIT_CONFIG
    if not ISOLATED:
        print("状态目录隔离未生效,拒绝连接(日志会写进实盘目录)。", file=sys.stderr)
        return EXIT_CONFIG

    allowed_td = _allowed_addresses(os_env.get("BROKER_TEST_ALLOWED_TD", "")) or None
    allowed_md = _allowed_addresses(os_env.get("BROKER_TEST_ALLOWED_MD", "")) or None
    address = normalize_address(setting["交易服务器"])
    md_address = normalize_address(setting["行情服务器"])
    environment = setting["柜台环境"]
    # 目标闸门:SimNow 白名单内,或「测试」环境 + 前置地址白名单。生产 API 一律拒绝。
    if is_simnow_address(address):
        # SimNow 有自己的官方地址白名单,不用本次的前置地址表来卡它
        # (那张表是给期货公司测试前置用的,里面没有 SimNow 地址)。
        allowed_td = None
    elif environment != "测试" or allowed_td is None or address not in allowed_td:
        print(f"交易前置 {address}(柜台环境={environment})不是 SimNow 地址,"
              "也不是白名单内的测试前置:拒绝连接。平仓是**真的下委托**,生产 API 上没有强制放行开关。",
              file=sys.stderr)
        return EXIT_CONFIG
    # 行情前置同样要查:平仓单的价格就是从行情前置的盘口来的,
    # 连错行情前置 = 用假价格平仓(可能平不掉,也可能成交在错价上)。
    if allowed_md:
        if md_address not in allowed_md:
            print(f"行情前置 {md_address} 不在允许的前置地址表 {sorted(allowed_md)} 内,拒绝连接。", file=sys.stderr)
            return EXIT_CONFIG
    elif md_address not in SIMNOW_MD:
        print(f"行情前置 {md_address} 不在任何白名单内(也没有配置 BROKER_TEST_ALLOWED_MD),拒绝连接:"
              "平仓价来自行情前置的盘口,不能靠「交易前置是白名单内的」来推定它也是。", file=sys.stderr)
        return EXIT_CONFIG
    ok, note = check_target(environment, setting["交易服务器"], True, allowed_td)
    print(note)
    if not ok:
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

    record: dict = {"时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "交易前置": address, "柜台环境": environment,
                    "账号": mask_account(setting["用户名"]), "确认发单": bool(args.confirm_flatten),
                    "计划": [], "结果": [], "收尾持仓": []}
    out_dir = bootstrap.ROOT / "data" / "flatten"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"flatten-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

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
            if any(marker in line for marker in ("交易服务器登录失败", "授权验证失败")):
                print("登录失败,放弃。", file=sys.stderr)
                return EXIT_CONFIG
            time.sleep(0.5)
        if not contract_ready:
            print(f"{args.connect_timeout:.0f} 秒内没有拿到合约表,持仓查询不可信,放弃。", file=sys.stderr)
            return EXIT_CONFIG

        positions = _wait_positions(main_engine, symbol, args.position_wait)
        held = [f"{p.vt_symbol} {p.direction.value} {p.volume}" for p in positions]
        print(f"当前非零持仓 {len(held)} 项:{held if held else '(空)'}")
        record["持仓"] = held
        if not positions:
            print("账户已空,不需要平仓。")
            record["结论"] = "账户本来就是空的"
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return EXIT_NO_POSITION

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
            print("有持仓但生成不出平仓单(方向不明或合约信息缺失),请人工在交易客户端平仓。", file=sys.stderr)
            record["结论"] = "无法生成平仓单,需人工处理"
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return EXIT_FAILED
        for plan in plans:
            print(f"  计划:{'卖出' if plan.direction == Direction.SHORT else '买入'}平仓 {plan.vt_symbol} "
                  f"{plan.volume} 手 @ {plan.price} ({plan.offset.value},{plan.reason})")
        record["计划"] = [p.to_dict() for p in plans]
        # 没有行情就定不出价:非交易时段订阅不到 Tick,挂出来的委托会被拒,
        # 挂在柜台上的单还可能变成"晚到的成交"。这里直接拒绝,让操作者到交易时段再跑。
        if any(p.price <= 0 for p in plans):
            print("没有行情(Tick),无法给平仓单定价——现在多半不在交易时段。"
                  "请在交易时段内重跑本脚本。", file=sys.stderr)
            record["结论"] = "无行情,无法定价,未发单"
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return EXIT_FAILED

        if not args.confirm_flatten:
            print("未加 --confirm-flatten:只打印计划,不发任何委托。")
            record["结论"] = "dry-run"
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return EXIT_OK

        all_filled = True
        for plan in plans:
            status, filled = _send_and_wait(main_engine, collector, plan, args.fill_timeout)
            print(f"  结果:{plan.vt_symbol} {plan.volume} 手 @ {plan.price} → {status}")
            record["结果"].append({**plan.to_dict(), "状态": status, "成交": filled})
            all_filled = all_filled and filled
            if not filled:
                # 没成交的单还挂在交易所上,过一会儿成交就会留下仓位 → 撤掉
                _cancel_all(main_engine, collector)

        after = _wait_positions(main_engine, symbol, args.position_wait)
        left = [f"{p.vt_symbol} {p.direction.value} {p.volume}" for p in after]
        record["收尾持仓"] = left
        print(f"收尾检查:剩余持仓 {len(left)} 项:{left if left else '(空)'}")
        if left:
            print("⚠ 账户仍有持仓,请立即人工核对。", file=sys.stderr)
            record["结论"] = "未平干净,仍有持仓"
            out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return EXIT_FAILED
        record["结论"] = "已平空" if all_filled else "持仓已清空(有单未成交,已撤单)"
        out_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"留痕:{out_path}")
        return EXIT_OK
    finally:
        close_engine(main_engine)


def _cancel_all(main_engine, collector: Collector) -> None:
    """撤掉本进程发出的、还挂在柜台上的委托(收尾兜底)。"""
    with collector.lock:
        orders = list(collector.orders.values())
    for order in orders:
        if not order.is_active():
            continue
        try:
            main_engine.cancel_order(order.create_cancel_request(), "CTP")
        except Exception as exc:      # 撤单失败不影响主结论,但要留下痕迹
            print(f"撤单失败({order.vt_orderid}):{exc!r}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="只看这个合约(默认取 .env 的 LIVE_VT_SYMBOL;空 = 全部)")
    parser.add_argument("--confirm-flatten", action="store_true", help="确认真的要发出平仓委托")
    parser.add_argument("--connect-timeout", type=float, default=180.0)
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
