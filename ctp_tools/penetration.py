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
import time
from dataclasses import dataclass, field
from datetime import datetime

from vnpy.trader.constant import Direction, Offset, OrderType, Status
from vnpy.trader.object import CancelRequest, OrderRequest, SubscribeRequest
from vnpy.trader.utility import round_to

from ctp_tools.collector import Collector, check_positions, close_engine, in_trading_session, trade_live
from notify.webhook import redact
from ctp_tools.simnow import is_simnow_address, normalize_address

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


def check_target(environment: str, td_address: str, confirmed: bool,
                 allowed_td: set[str] | frozenset[str] | None = None) -> tuple[bool, str]:
    """目标环境闸门。返回 (是否放行, 说明)。生产 API 没有任何强制放行的出口。

    allowed_td:本次允许连接的前置地址表(已归一化的 host:port)。

    **必须给,而且不能是空的**——以前 allowed_td 可以传 None,于是
    「柜台环境=测试」这一句就成了唯一依据:那台机器只按环境变量判断,
    连到哪台都不管,等于没有闸门。调用方应该用
    `ctp_tools.broker_targets.resolve_allowed()` 算出这张表
    (用户白名单 ∩ 代码里写死的可信地址全集),而不是直接把 `.env` 原样传进来。
    """
    if not confirmed:
        return False, "未加 --confirm-broker-test。本脚本会连接并发出测试委托,请确认目标是期货公司的测试环境。"
    address = normalize_address(td_address)
    if not allowed_td:
        return False, (f"没有可用的交易前置地址白名单,无法确认交易前置 {address} 是不是期货公司的测试机,拒绝连接。"
                       "白名单由 BROKER_TEST_ALLOWED_TD 指定(一键流程会自动注入),"
                       "并且只能取「可信地址全集」里的地址——白名单与地址同源,采信它等于自证清白。")
    if address not in allowed_td:
        return False, (f"交易前置 {address} 不在本次允许的前置地址表 {sorted(allowed_td)} 内,拒绝连接。"
                       "地址表由 BROKER_TEST_ALLOWED_TD 指定(一键流程会自动注入)。")
    if environment == "测试":
        return True, f"目标:期货公司看穿式测试环境(柜台环境=测试),交易前置 {address}。"
    if is_simnow_address(address):
        return True, f"目标:SimNow 预演(交易前置 {address})。注意:SimNow 不等于期货公司的穿透测试。"
    return False, (f"柜台环境={environment!r} 且交易前置 {address} 不是 SimNow 地址:这是**生产 API**,"
                   "穿透测试只能连看穿式测试环境。请把 .env 的 CTP_ENVIRONMENT 改为「测试」并填测试前置地址。")


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
    所以不能沿用 connect_status 判活(断线后它仍是 True)。
    判活逻辑统一放在 collector.trade_live,这里只做转发。"""
    return trade_live(main_engine)


def run_checks(args, symbol: str, main_engine, collector: Collector,
               secrets: tuple[str, ...] = (), session_func=in_trading_session) -> tuple[Run, int]:
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
    in_session = session_func(datetime.now())
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
    positions, trusted = check_positions(main_engine, wait=args.position_wait, early_exit=False,
                                         collector=collector)
    # 可信度以 check_positions 的第二个返回值为准(含「柜台没应答」),不能只看通道在线
    connected = trusted and trade_connected(main_engine)
    if not connected:
        run.add("查询持仓", "FAIL", "交易连接已断开或柜台没有应答,查询结果不可信", time.monotonic() - t3)
    else:
        run.add("查询持仓", "PASS", f"当前持仓 {len(positions)} 项", time.monotonic() - t3)

    code = EXIT_OK
    if args.with_order:
        if not in_session:
            run.add("报单/撤单", "SKIP", "非交易时段,报单会被拒绝,请在交易时段补测")
        elif tick is None:
            run.add("报单/撤单", "SKIP", "没有 Tick,无法取跌停价")
        elif positions or not connected:
            # 空仓闸门:与取证运行器/采集工具同一口径。账户上有持仓、或查不准时,
            # 一笔不发 —— 报单步骤在有持仓的账户上多一笔委托就多一分误伤的可能。
            why = f"账户已有持仓 {positions}" if positions else "交易连接不在线或柜台没有应答,无法确认账户空仓"
            run.add("报单/撤单", "FAIL", f"已中止:{why}(穿透演练的报单只在确认空仓后进行)")
            code = EXIT_STEP_FAILED
        else:
            code = do_order_cancel(run, main_engine, collector, contract, tick)
            after, after_trusted = check_positions(main_engine, wait=args.position_wait, early_exit=False,
                                                   collector=collector)
            ok = after_trusted and trade_connected(main_engine)
            if after:
                run.add("收尾持仓检查", "FAIL", f"发现持仓 {len(after)} 项,请立即人工核对并平仓")
                code = EXIT_POSITION
            elif not ok:
                run.add("收尾持仓检查", "WARN", "连接已断开或柜台没有应答,无法确认是否留下持仓,请人工核对")
    else:
        run.add("报单/撤单", "SKIP", "未加 --with-order")
    return run, code

