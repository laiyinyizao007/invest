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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK
from vnpy.trader.logger import ERROR
from vnpy.trader.object import ContractData, LogData, OrderData, OrderRequest, TickData
from vnpy.trader.utility import round_to

from trading.graceful_exit import logout_ctp
from trading.runtime import build_ctp_setting

# SimNow 地址白名单放在 scripts/simnow_guard.py,采集脚本与演练脚本共用同一套约束。
from ctp_tools.simnow import SIMNOW_TD_ADDRESSES, check_simnow_address, normalize_address
from ctp_tools.collector import (
    Case, Collector, build_cases, check_positions, close_engine, in_trading_session,
    reference_price, symbol_has_position, write_collected, positions_of_symbol,
    BEGIN_MARK, END_MARK,
)

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

    bootstrap.load_env()
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
    td_address = normalize_address(gateway_setting["交易服务器"])
    allowed, note = check_simnow_address(td_address, args.allow_any_address)
    if not allowed:
        print(note, file=sys.stderr)
        return 2
    print(f"目标交易服务器 {td_address},行情服务器 {gateway_setting['行情服务器']}。")
    if td_address not in SIMNOW_TD_ADDRESSES:
        print(note, file=sys.stderr)

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
