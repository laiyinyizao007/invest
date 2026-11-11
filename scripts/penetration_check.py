"""期货公司看穿式监管(穿透式)测试演练:按步骤连接测试环境、做一遍标准动作,并生成**脱敏**的留痕报告。

做的事(对应期货公司常要求的"登录、订阅行情、报单、撤单、持仓查询"):
    1 交易服务器连接 → 授权验证(AppID/AuthCode) → 登录 → 结算确认 → 合约表
    2 行情服务器连接 → 登录
    3 订阅行情并等到 Tick(非交易时段收不到,只记为提示,不算失败)
    4 查询账户与持仓
    5 (可选 --with-order)报一张**跌停价买单**再立即撤单,验证报撤单通道
报告写到 `data/penetration/penetration-<时间>.md`(data/ 不入库),可作为给期货公司的操作留痕。

用法(在运行交易程序的那台 Windows 上,项目根目录,.env 已按期货公司给的测试环境参数填好):
    python scripts/penetration_check.py --confirm-broker-test
    python scripts/penetration_check.py --confirm-broker-test --with-order --symbol c2701.DCE

安全约束:
- 必须加 `--confirm-broker-test`;
- 只允许两种目标:`CTP_ENVIRONMENT=测试`(期货公司看穿式测试环境),或交易地址在 SimNow 白名单内(预演用);
  `CTP_ENVIRONMENT=实盘` 且地址不是 SimNow = 生产 API,**一律拒绝**(没有强制放行开关);
- 报告里账号只保留头尾、密码/授权码/AppID 全部替换为 ***;
- 报单只在 --with-order 且处于交易时段时进行,价格取跌停价(通常不成交),成交了会大声告警并以非零码退出。
"""
import argparse
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import bootstrap

sys.path.insert(0, str(bootstrap.ROOT / "scripts"))

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Direction, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK  # noqa: E402
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

from collect_reject_texts import Collector, check_positions, close_engine, in_trading_session  # noqa: E402
from simnow_guard import is_simnow_address, normalize_address  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402

REPORT_DIR = bootstrap.ROOT / "data" / "penetration"

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_CONFIG = 2
EXIT_LOGIN = 3
EXIT_POSITION = 5

# vnpy_ctp 网关写出的里程碑日志(措辞取自 vnpy_ctp 6.7.11.4 的 ctp_gateway.py)。
MILESTONES = (
    ("td_connected", "交易服务器连接成功"),
    ("td_auth", "交易服务器授权验证成功"),
    ("td_login", "交易服务器登录成功"),
    ("settlement", "结算信息确认成功"),
    ("contracts", "合约信息查询成功"),
    ("md_connected", "行情服务器连接成功"),
    ("md_login", "行情服务器登录成功"),
)
MILESTONE_LABELS = {
    "td_connected": "交易前置连接", "td_auth": "授权验证(AppID/AuthCode)", "td_login": "交易登录",
    "settlement": "结算单确认(仅网关日志,网关不校验回报错误)", "contracts": "合约表查询", "md_connected": "行情前置连接",
    "md_login": "行情登录",
}
# 这些日志表示流程已经失败,继续等没有意义
FAILURE_MARKERS = ("授权验证失败", "交易服务器登录失败", "行情服务器登录失败")


@dataclass
class Step:
    name: str
    status: str          # PASS / FAIL / WARN / SKIP
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Run:
    steps: list[Step] = field(default_factory=list)
    secrets: tuple[str, ...] = ()   # 控制台输出也要脱敏(CTP 错误文本可能带账号)

    def add(self, name: str, status: str, detail: str = "", seconds: float = 0.0) -> Step:
        step = Step(name, status, detail, seconds)
        self.steps.append(step)
        shown = redact(detail, list(self.secrets))
        print(f"  [{status}] {name}" + (f":{shown}" if shown else ""))
        return step

    def failed(self) -> bool:
        return any(s.status == "FAIL" for s in self.steps)


def check_target(environment: str, td_address: str, confirmed: bool) -> tuple[bool, str]:
    """目标环境闸门。返回 (是否放行, 说明)。生产 API 没有任何强制放行的出口。"""
    if not confirmed:
        return False, "未加 --confirm-broker-test。本脚本会连接并发出测试委托,请确认目标是期货公司的测试环境。"
    address = normalize_address(td_address)
    if environment == "测试":
        return True, f"目标:期货公司看穿式测试环境(柜台环境=测试),交易前置 {address}。"
    if is_simnow_address(address):
        return True, f"目标:SimNow 预演(交易前置 {address})。注意:SimNow 不等于期货公司的穿透测试。"
    return False, (f"柜台环境={environment!r} 且交易前置 {address} 不是 SimNow 地址:这是**生产 API**,"
                   "穿透测试只能连看穿式测试环境。请把 .env 的 CTP_ENVIRONMENT 改为「测试」并填测试前置地址。")


def redact(text: str, secrets: list[str]) -> str:
    """把已知敏感值替换为 ***(长度不足 3 的值不替换,避免把普通字符误伤成一片星号)。"""
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def mask_account(account: str) -> str:
    """账号只保留头 2 位与尾 2 位;长度 < 8 的短账号全部掩码(保留比例过高等于没脱敏)。"""
    if len(account) < 8:
        return "*" * len(account)
    return account[:2] + "*" * (len(account) - 4) + account[-2:]


def milestone_status(log_lines: list[str]) -> dict[str, bool]:
    """从网关日志里判断各里程碑是否出现。"""
    return {key: any(text in line for line in log_lines) for key, text in MILESTONES}


def failure_lines(log_lines: list[str]) -> list[str]:
    return [line for line in log_lines if any(m in line for m in FAILURE_MARKERS)]


def limit_down_price(limit_down: float, pricetick: float, last_price: float = 0.0) -> float:
    """跌停价对齐最小变动价位;拿不到跌停价、或它不低于最新价(无涨跌停/异常值)时返回 0,由调用方跳过报单。"""
    if limit_down <= 0 or (last_price > 0 and limit_down >= last_price):
        return 0.0
    return round_to(limit_down, pricetick) if pricetick > 0 else limit_down


def render_report(run: Run, meta: dict[str, str], log_lines: list[str], secrets: list[str]) -> str:
    lines = [
        "# 看穿式监管测试演练报告", "",
        f"- 时间:{meta['time']}",
        f"- 目标:{meta['target']}",
        f"- 账号:{meta['account']}",
        f"- 合约:{meta['symbol']}",
        f"- 软件:vn.py {meta['vnpy']} / vnpy_ctp {meta['vnpy_ctp']} / Python {meta['python']}",
        f"- 终端:{meta['host']}", "",
        "## 步骤结果", "",
        "| 步骤 | 结果 | 说明 | 耗时(s) |", "|---|---|---|---|",
    ]
    for s in run.steps:
        lines.append(f"| {s.name} | {s.status} | {redact(s.detail, secrets) or '-'} | {s.seconds:.1f} |")
    lines += ["", "## 网关日志(已脱敏)", "", "```"]
    lines += [redact(line, secrets) for line in log_lines]
    lines += ["```", ""]
    return "\n".join(lines)


def package_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return "未装"


def wait_milestones(collector: Collector, timeout: float) -> tuple[dict[str, bool], list[str]]:
    """等到全部里程碑出现、出现失败日志或超时。返回 (里程碑状态, 失败日志)。"""
    deadline = time.monotonic() + timeout
    while True:
        with collector.lock:
            lines = list(collector.log_lines)
        status = milestone_status(lines)
        failures = failure_lines(lines)
        if all(status.values()) or failures or time.monotonic() >= deadline:
            return status, failures
        time.sleep(0.5)


def do_order_cancel(run: Run, main_engine, collector: Collector, contract, tick) -> int:
    """跌停价买单 + 立即撤单。返回 EXIT_* 码(EXIT_POSITION 表示意外成交)。"""
    price = limit_down_price(float(getattr(tick, "limit_down", 0) or 0), contract.pricetick,
                             float(getattr(tick, "last_price", 0) or 0))
    if price <= 0:
        run.add("报单/撤单", "SKIP", "Tick 里没有可信的跌停价(缺失或不低于最新价),无法构造不易成交的委托")
        return EXIT_OK
    req = OrderRequest(symbol=contract.symbol, exchange=contract.exchange, direction=Direction.LONG,
                       type=OrderType.LIMIT, volume=1, price=price, offset=Offset.OPEN, reference="penetration")
    t0 = time.monotonic()
    vt_orderid = main_engine.send_order(req, "CTP")
    if not vt_orderid:
        run.add("报单", "FAIL", "send_order 没有返回委托号(请求发送失败)", time.monotonic() - t0)
        return EXIT_STEP_FAILED
    order = None
    while time.monotonic() - t0 < 10:
        order = collector.order_of(vt_orderid)
        if order and order.status != Status.SUBMITTING:
            break
        time.sleep(0.1)
    if order is not None and order.status == Status.REJECTED:
        run.add("报单", "FAIL", f"委托被拒绝(价格 {price})", time.monotonic() - t0)
        return EXIT_STEP_FAILED
    if order is None or order.status == Status.SUBMITTING:
        # 回报只是慢,委托随后仍可能进入交易所:照样发撤单,不能因为"没看到回报"就放着不管
        run.add("报单", "FAIL", f"10 秒内没有收到委托回报({vt_orderid}),已尝试撤单兜底", time.monotonic() - t0)
        cancel_req = (order.create_cancel_request() if order is not None else
                      CancelRequest(orderid=vt_orderid.split(".", 1)[-1], symbol=contract.symbol,
                                    exchange=contract.exchange))
        main_engine.cancel_order(cancel_req, "CTP")
        collector.wait_order_final(vt_orderid, timeout=10.0)
        return EXIT_STEP_FAILED
    run.add("报单", "PASS", f"跌停价买 1 手 @ {price},状态 {order.status.value}", time.monotonic() - t0)

    if order.is_active():
        t1 = time.monotonic()
        main_engine.cancel_order(order.create_cancel_request(), "CTP")
        final = collector.wait_order_final(vt_orderid, timeout=10.0)
        # 先判成交再判撤单:"部分成交后撤单成功"状态是 CANCELLED,但已经留下了持仓
        if final is not None and final.traded > 0:
            run.add("撤单", "FAIL", f"委托已成交 {final.traded} 手(状态 {final.status.value}),留下了持仓",
                    time.monotonic() - t1)
            return EXIT_POSITION
        if final is not None and final.status == Status.CANCELLED:
            run.add("撤单", "PASS", "委托已撤销", time.monotonic() - t1)
        else:
            run.add("撤单", "FAIL", f"10 秒内没有确认撤销(状态 {final.status.value if final else '无回报'})"
                    f"——委托可能仍挂在柜台,请立即人工撤单", time.monotonic() - t1)
    elif order.traded > 0:
        run.add("撤单", "FAIL", f"委托已成交 {order.traded} 手,留下了持仓")
        return EXIT_POSITION
    return EXIT_OK


def trade_connected(main_engine) -> bool:
    """交易通道是否仍在线。vnpy_ctp 断线只清 login_status、不清 connect_status,
    所以不能沿用 check_positions 返回的 connected(它读 connect_status,断线后仍为 True)。"""
    td_api = getattr(main_engine.get_gateway("CTP"), "td_api", None)
    return bool(getattr(td_api, "login_status", False))


def run_checks(args, symbol: str, main_engine, collector: Collector,
               secrets: tuple[str, ...] = ()) -> tuple[Run, int]:
    run = Run(secrets=tuple(secrets))
    t0 = time.monotonic()
    status, failures = wait_milestones(collector, args.connect_timeout)
    elapsed = time.monotonic() - t0
    with collector.lock:
        disconnects = [line for line in collector.log_lines if "连接断开" in line]
    timeout_note = f"{args.connect_timeout:.0f} 秒内未出现"
    if disconnects:
        timeout_note += f"(最近一次断开:{disconnects[-1]};4097 排查见 docs/runbook-simnow.md §8)"
    for key, _text in MILESTONES:
        run.add(MILESTONE_LABELS[key], "PASS" if status[key] else "FAIL",
                "" if status[key] else (failures[-1] if failures else timeout_note), elapsed)
    if failures or not (status["td_login"] and status["contracts"]):
        return run, EXIT_LOGIN

    if not trade_connected(main_engine):
        run.add("交易通道在线", "FAIL", "里程碑已出现但交易通道当前已断开(见网关日志里的「连接断开」),后续查询不可信")
        return run, EXIT_LOGIN

    contract = collector.contract_of(symbol)
    if contract is None:
        run.add("合约存在", "FAIL", f"合约表里没有 {symbol}")
        return run, EXIT_STEP_FAILED
    run.add("合约存在", "PASS", f"{symbol},最小变动 {contract.pricetick},单笔上限 {contract.max_volume}")

    main_engine.subscribe(SubscribeRequest(symbol=contract.symbol, exchange=contract.exchange), "CTP")
    t1 = time.monotonic()
    tick = None
    while time.monotonic() - t1 < args.tick_timeout:
        tick = collector.tick_of(symbol)
        if tick is not None:
            break
        time.sleep(0.5)
    in_session = in_trading_session(datetime.now())
    if tick is not None:
        run.add("订阅行情", "PASS", f"收到 Tick,最新价 {tick.last_price}", time.monotonic() - t1)
    elif in_session:
        run.add("订阅行情", "FAIL", f"交易时段内 {args.tick_timeout:.0f} 秒没有收到 Tick", time.monotonic() - t1)
    else:
        run.add("订阅行情", "WARN", "非交易时段,没有 Tick(请在交易时段补测)", time.monotonic() - t1)

    t2 = time.monotonic()
    gateway = main_engine.get_gateway("CTP")
    try:
        gateway.query_account()
    except Exception:
        pass
    while time.monotonic() - t2 < 10 and not main_engine.get_all_accounts():
        time.sleep(0.5)
    accounts = main_engine.get_all_accounts()
    run.add("查询账户", "PASS" if accounts else "FAIL",
            "已收到账户资金回报" if accounts else "10 秒内没有收到账户资金回报", time.monotonic() - t2)

    t3 = time.monotonic()
    positions, _ = check_positions(main_engine, symbol, wait=args.position_wait, early_exit=False)
    connected = trade_connected(main_engine)
    if not connected:
        run.add("查询持仓", "FAIL", "交易连接已断开,查询结果不可信", time.monotonic() - t3)
    else:
        run.add("查询持仓", "PASS", f"当前持仓 {len(positions)} 项", time.monotonic() - t3)

    code = EXIT_OK
    if args.with_order:
        if not in_session:
            run.add("报单/撤单", "SKIP", "非交易时段,报单会被拒绝,请在交易时段补测")
        elif tick is None:
            run.add("报单/撤单", "SKIP", "没有 Tick,无法取跌停价")
        else:
            code = do_order_cancel(run, main_engine, collector, contract, tick)
            after, _ = check_positions(main_engine, symbol, wait=args.position_wait, early_exit=False)
            ok = trade_connected(main_engine)
            if after:
                run.add("收尾持仓检查", "FAIL", f"发现持仓 {len(after)} 项,请立即人工核对并平仓")
                code = EXIT_POSITION
            elif not ok:
                run.add("收尾持仓检查", "WARN", "连接已断开,无法确认是否留下持仓,请人工核对")
    else:
        run.add("报单/撤单", "SKIP", "未加 --with-order")
    return run, code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="测试合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--confirm-broker-test", action="store_true", help="确认目标是期货公司的测试环境")
    parser.add_argument("--with-order", action="store_true", help="交易时段内报一张跌停价买单并撤销")
    parser.add_argument("--connect-timeout", type=float, default=120.0, help="等待登录全流程的最长时间(秒)")
    parser.add_argument("--tick-timeout", type=float, default=30.0, help="等待 Tick 的最长时间(秒)")
    parser.add_argument("--position-wait", type=float, default=8.0, help="持仓查询等待窗口(秒)")
    parser.add_argument("--out-dir", default="", help=f"报告目录(默认 {REPORT_DIR})")
    args = parser.parse_args(argv)
    # 非 UTF-8 控制台(管道/英文系统)下中文输出不应让脚本崩溃
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    from dotenv import load_dotenv
    load_dotenv(bootstrap.ROOT / ".env", override=False)
    env = os.environ

    symbol = args.symbol or env.get("LIVE_VT_SYMBOL", "")
    if not symbol:
        print("未指定合约:--symbol 或 .env 的 LIVE_VT_SYMBOL", file=sys.stderr)
        return EXIT_CONFIG
    try:
        setting = build_ctp_setting(env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    ok, note = check_target(setting["柜台环境"], setting["交易服务器"], args.confirm_broker_test)
    print(note, file=sys.stderr if not ok else sys.stdout)
    if not ok:
        return EXIT_CONFIG

    secrets = [env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""), env.get("CTP_APP_ID", ""),
               env.get("CTP_USERNAME", ""), env.get("WEBHOOK_URL", "")]

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    from vnpy_ctp import CtpGateway
    if "柜台环境" not in CtpGateway.default_setting:
        print("当前 vnpy_ctp 不支持「柜台环境」设置项(版本过旧或解析到了别的 Python 环境):"
              "无法保证「测试」会走测试版 API,拒绝连接。请用项目 .venv 运行。", file=sys.stderr)
        return EXIT_CONFIG
    main_engine.add_gateway(CtpGateway)
    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)

    print(f"连接 {symbol} ...")
    main_engine.connect(setting, "CTP")
    try:
        run, code = run_checks(args, symbol, main_engine, collector, tuple(secrets))
    finally:
        close_engine(main_engine)

    import platform
    import socket
    meta = {
        "time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "target": note,
        "account": mask_account(env.get("CTP_USERNAME", "")), "symbol": symbol,
        "vnpy": package_version("vnpy"), "vnpy_ctp": package_version("vnpy_ctp"),
        "python": platform.python_version(), "host": socket.gethostname(),
    }
    with collector.lock:
        log_lines = list(collector.log_lines)
    report = render_report(run, meta, log_lines, secrets)
    out_dir = Path(args.out_dir) if args.out_dir else REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"penetration-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.write_text(report, encoding="utf-8")
    print(f"\n报告已写入 {out}")
    if code == EXIT_OK and run.failed():
        code = EXIT_STEP_FAILED
    return code


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # CTP 原生线程在断连风暴下可能不退出,强制结束进程
        os._exit(code)
