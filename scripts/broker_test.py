"""国信看穿式/程序化测试取证运行器:在期货公司测试环境里逐项演示,留下脱敏、可追溯的证据与证据图。

做的事(对应国信《外部接入系统技术测试报告》):
    穿透式演示 ①登录 ②行情 ③委托(开/平仓) ④撤单 ⑤成交 ⑥流量控制 ⑦异常自动退出(20260717 版模板已无穿透专章,仅留存证据)
    外部接入 1.1 连通 1.2 基础交易 2.1 稳定运行(持续下单 200 笔)
    程序化补充 3.1 连接断开监测 3.2 报撤单笔数 3.3 重复报单(选测) 4.1 阈值与预警 5.1 指令检查 5.2 柜台错误提示
           6.1 暂停交易 6.2 批量撤单(未实现,选测) 7.1 日志
    (内部测试项 ID 仍是 P1.1…P6.1,与模板编号的对应见 broker_evidence.TEMPLATE_NO;--only 用内部 ID)
结果写到 `data/broker_test/<时间戳>/`:evidence.json + images/*.png,再用 `scripts/broker_report.py` 填进国信模板。

用法(在运行机上,项目根目录,.env 已按国信测试环境参数填好,CTP_ENVIRONMENT=测试):
    python scripts/broker_test.py --confirm-broker-test
    python scripts/broker_test.py --confirm-broker-test --interactive        # 含 3.1 断网演练(提示你断网/恢复)
    python scripts/broker_test.py --confirm-broker-test --resume data/broker_test/<目录>   # 非交易时段先跑一遍,交易时段补测

安全约束(同 penetration_check.py):
- 必须 `--confirm-broker-test`;只放行 `CTP_ENVIRONMENT=测试` 或 SimNow 预演地址;生产 API 无条件拒绝;
- 状态完全隔离:独立的 `.vntrader_brokertest/` 目录与独立的风控状态文件,不污染实盘 `account_guard.json` 与日志;
- 一次运行只登录一次,授权失败不重试(国信会检查"频繁登录/大量错误登录");运行期间请关闭运行机的自动重启;
- 会在测试环境真的开 1 手并平仓(测试环境无资金风险),开仓前先确认账户空仓;
- 报告里的"截图"是证据文本渲染成的图片,不是真实界面截图。
"""
import argparse
import os
import subprocess
import sys
import time
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

# 隔离必须发生在任何会导入 vn.py logger 的模块之前。
BT_ROOT = bootstrap.ROOT / ".vntrader_brokertest"
ISOLATED = bootstrap.isolate_state(BT_ROOT.name)


from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_TICK, EVENT_TRADE  # noqa: E402
from vnpy.trader.object import OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

import pause_trading  # noqa: E402
from broker_evidence import (  # noqa: E402
    FAIL, ITEMS, MANUAL, PASS, SKIP, Evidence, Shot, load_evidence, merge_evidence, render_all, save_connection_info, save_evidence,
)
from ctp_tools.collector import Collector, build_cases, check_positions, close_engine, in_trading_session  # noqa: E402
from ctp_tools.broker_targets import SIMNOW_MD  # noqa: E402
from ctp_tools.simnow import normalize_address  # noqa: E402
from ctp_tools.penetration import (  # noqa: E402
    MILESTONE_LABELS, MILESTONES, Run, check_target, do_order_cancel, limit_down_price, mask_account, redact,
    wait_milestones,
)
from trading.events import EVENT_GUARD_ALERT  # noqa: E402
from trading.guard_rule import ACCOUNT_RULE_NAME  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402

BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """北京时间(无时区信息)。交易时段按交易所所在时区判断,不能用运行机的本地时间(宿主机可能是 UTC-8)。"""
    return datetime.now(BEIJING).replace(tzinfo=None)


OUT_ROOT = bootstrap.ROOT / "data" / "broker_test"
DAILY_RULE, SIZE_RULE, DUP_RULE = "每日上限检查", "委托规模检查", "重复报单检查"
EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_LOGIN = 0, 1, 2, 3
# 会向柜台发委托的测试项:这些步骤之前必须先确认账户空仓
ORDER_ITEMS = frozenset({"T3", "T4", "T5", "P1.2", "P1.3", "P2.3", "P3.1", "T6", "P4.2", "P5.1"})
STABILITY_ORDERS = 200                     # 国信 2.1 稳定运行:程序化系统持续下单 200 笔(非程序化 50 笔)
FINAL_CANCEL_WAIT = 15.0                   # 收尾撤单:轮询等确认的上限(秒)
STABILITY_LIMIT = 10_000_000               # 稳定运行演示期间临时抬高的风控上限(演示后还原)


def _allowed_addresses(raw: str) -> set[str]:
    """解析 BROKER_TEST_ALLOWED_TD/MD:逗号(或分号)分隔的 host:port,统一归一化。"""
    return {normalize_address(x) for x in raw.replace(";", ",").split(",") if x.strip()}


# ======================= 上下文与工具 =======================

@dataclass
class Ctx:
    """一次取证运行的共享状态。步骤函数只依赖这里,便于用假网关离线测试。"""

    main_engine: object
    risk_engine: object
    collector: Collector
    symbol: str
    gateway_name: str = "CTP"
    secrets: tuple[str, ...] = ()
    interactive: bool = False
    connect_timeout: float = 120.0
    tick_timeout: float = 30.0
    disconnect_timeout: float = 120.0
    position_wait: float = 8.0           # 查询持仓的等待窗口(CTP 持仓回报是异步的)
    cancel_wait: float = 8.0             # 等撤单确认的时间
    probe_seq: int = 0                   # 演示委托的价格序号:每笔演示委托用不同价格,避免重复报单规则的累计计数跨步骤污染
    flat: bool | None = None             # 账户是否已确认空仓(None = 还没查)
    flat_reason: str = ""                # 空仓闸门失败的具体原因(有持仓 / 连接断开)
    started_wall: datetime = field(default_factory=datetime.now)   # 本次运行开始时刻,日志归因用
    alerts: list[dict] = field(default_factory=list)
    trades: list = field(default_factory=list)
    notifier: object | None = None
    state_dir: Path = BT_ROOT / "state"
    pump: Callable[[], None] = lambda: None       # 测试里用来推进定时器;真实运行里定时器自己在跑
    sleep: Callable[[float], None] = time.sleep
    contract: object | None = None
    tick: object | None = None
    in_session: bool = False
    clock: Callable[[], float] = time.monotonic   # 稳定运行计时用;测试里可注入
    stability_interval: float = 0.1               # 稳定运行逐笔下单间隔(秒),避开柜台流控
    stability_wait: float = 60.0                  # 等全部委托回报/撤单终态的上限

    def rule(self, name: str):
        return self.risk_engine.rules.get(name)

    def now(self) -> str:
        return beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    def clean(self, text: str) -> str:
        return redact(str(text), list(self.secrets))

    def wait_until(self, predicate: Callable[[], bool], timeout: float, step: float = 0.2) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            self.pump()
            if predicate():
                return True
            if time.monotonic() >= deadline:
                return False
            self.sleep(step)

    def alerts_since(self, mark: int) -> list[dict]:
        return self.alerts[mark:]

    def log_mark(self) -> int:
        with self.collector.lock:
            return len(self.collector.log_lines)

    def logs_since(self, mark: int, *needles: str) -> list[str]:
        with self.collector.lock:
            lines = self.collector.log_lines[mark:]
        return [ln for ln in lines if not needles or any(n in ln for n in needles)]


class Book:
    """按测试项累积证据(一个测试项可由多个步骤贡献图片与状态)。"""

    def __init__(self, clean: Callable[[str], str] | None = None) -> None:
        self.items: dict[str, Evidence] = {}
        self._clean = clean or (lambda text: text)       # 落盘即脱敏:evidence.json 里不留未脱敏文本

    def add(self, item: str, status: str, summary: str, shots: list[Shot], started_at: str, metrics: dict | None = None) -> None:
        summary = self._clean(summary)
        shots = [Shot(s.label, [self._clean(line) for line in s.lines], s.image) for s in shots]
        old = self.items.get(item)
        if old is None:
            self.items[item] = Evidence(item, status, summary, started_at, list(shots), dict(metrics or {}))
            return
        if metrics:
            old.metrics.update(metrics)
        old.shots.extend(shots)
        old.status = _combine(old.status, status)
        if summary and summary not in old.summary:
            old.summary = f"{old.summary};{summary}" if old.summary else summary

    def list(self) -> list[Evidence]:
        return [self.items[k] for k in ITEMS if k in self.items]


def _combine(a: str, b: str) -> str:
    for status in (FAIL, MANUAL, SKIP):
        if status in (a, b):
            return status
    return PASS


def lines_of(run: Run, ctx: Ctx) -> list[str]:
    return [ctx.clean(f"[{s.status}] {s.name}" + (f":{s.detail}" if s.detail else "") + f"({s.seconds:.1f}s)") for s in run.steps]


def limit_price(ctx: Ctx, which: str = "down") -> float:
    """不易成交的限价:跌停价(买单)。拿不到可信跌停价返回 0。"""
    tick = ctx.tick
    if tick is None or ctx.contract is None:
        return 0.0
    return limit_down_price(float(getattr(tick, "limit_down", 0) or 0), ctx.contract.pricetick, float(getattr(tick, "last_price", 0) or 0))


def probe_price(ctx: Ctx) -> float:
    """演示用的不易成交买价:跌停价起,每笔上浮一个最小变动价位(循环 20 档,仍远低于市价)。
    每笔不同价格,是为了不让 vnpy_riskmanager 的重复报单规则把不同步骤的委托累计成"重复"。"""
    tickp = ctx.contract.pricetick or 1.0
    base = limit_price(ctx) or round_to(float(getattr(ctx.tick, "last_price", 0) or 1000.0) * 0.5, tickp)
    n = ctx.probe_seq % 20
    ctx.probe_seq += 1
    return base + n * tickp


def ensure_flat(ctx: Ctx) -> bool:
    """所有会发单的步骤之前的统一闸门:账户必须空仓。有持仓(或连接断开查不准)就不发任何演示委托——
    "无仓位可平""暂停期间平仓"这类平仓单在有持仓时会真的平掉仓位。

    失败原因分两种,分开记,否则事后只看证据分不清是"账户真有持仓"还是"查不到持仓":
    - 有持仓:列出来是哪几个合约、多少手,人工去平;
    - 连接断开/查不到:柜台连接在查询期间断了,或持仓查询没有回报,这时平仓单更不能发。
    """
    held, connected = check_positions(ctx.main_engine, ctx.symbol, wait=ctx.position_wait, early_exit=False)
    if held:
        ctx.flat_reason = f"账户已有持仓:{'、'.join(held)}(请先人工平仓再重跑)"
    elif not connected:
        ctx.flat_reason = "交易连接断开,查不到持仓,无法确认账户空仓"
    else:
        ctx.flat_reason = ""
    ctx.flat = (not held) and connected
    return ctx.flat


def make_req(ctx: Ctx, *, direction=Direction.LONG, offset=Offset.OPEN, price: float, volume: float = 1, symbol: str | None = None) -> OrderRequest:
    c = ctx.contract
    return OrderRequest(symbol=symbol or c.symbol, exchange=c.exchange, direction=direction, type=OrderType.LIMIT,
                        volume=volume, price=price, offset=offset, reference="broker_test")


def send(ctx: Ctx, req: OrderRequest) -> str:
    """经风控链发单(与实盘路径一致);被本地风控拦下时返回空串。"""
    return ctx.main_engine.send_order(req, ctx.gateway_name)


def cancel_active(ctx: Ctx, vt_orderids: list[str]) -> None:
    for oid in vt_orderids:
        order = ctx.collector.order_of(oid)
        if order is not None and order.is_active():
            ctx.main_engine.cancel_order(order.create_cancel_request(), ctx.gateway_name)


# ======================= 步骤 =======================

def step_login(ctx: Ctx, book: Book) -> bool:
    """T1 / P1.1:连接→授权→登录→结算确认→合约表→行情登录。返回是否可继续后续步骤。"""
    started = ctx.now()
    t0 = time.monotonic()
    status, failures = wait_milestones(ctx.collector, ctx.connect_timeout)
    lines = [ctx.clean(f"{MILESTONE_LABELS[k]}: {'成功' if status[k] else '未出现'}") for k, _ in MILESTONES]
    lines.append(f"耗时 {time.monotonic() - t0:.1f}s")
    lines += [ctx.clean(f"失败日志: {f}") for f in failures]
    ok = status["td_login"] and status["md_login"] and not failures
    ev_status = PASS if ok else FAIL
    summary = "" if ok else (failures[-1] if failures else "登录全流程未在限时内完成")
    for item in ("T1", "P1.1"):
        book.add(item, ev_status, ctx.clean(summary), [Shot("", lines)], started)
    return ok and status["contracts"]


def step_market(ctx: Ctx, book: Book) -> None:
    """T2:订阅行情并取到 Tick。"""
    started = ctx.now()
    c = ctx.contract
    ctx.main_engine.subscribe(SubscribeRequest(symbol=c.symbol, exchange=c.exchange), ctx.gateway_name)
    got = ctx.wait_until(lambda: ctx.collector.tick_of(ctx.symbol) is not None, ctx.tick_timeout)
    ctx.tick = ctx.collector.tick_of(ctx.symbol)
    if ctx.tick is not None:
        t = ctx.tick
        lines = [f"合约 {ctx.symbol}", f"最新价 {t.last_price}  买一 {t.bid_price_1}  卖一 {t.ask_price_1}",
                 f"涨停 {t.limit_up}  跌停 {t.limit_down}  Tick 时间 {t.datetime}"]
        book.add("T2", PASS, "", [Shot("", lines)], started)
    elif ctx.in_session:
        book.add("T2", FAIL, f"交易时段内 {ctx.tick_timeout:.0f}s 没有收到 Tick", [Shot("", ["未收到 Tick"])], started)
    else:
        book.add("T2", SKIP, "非交易时段没有 Tick,请在交易时段补测", [Shot("", ["非交易时段,未收到 Tick"])], started)
    _ = got


def step_basic_trade(ctx: Ctx, book: Book) -> None:
    """T3 / T5 / P1.2:对手价买开 1 手 → 成交 → 平仓。先确认账户空仓,收尾再确认。"""
    started = ctx.now()
    c, tick = ctx.contract, ctx.tick
    if tick is None or not ctx.in_session:
        reason = "非交易时段或没有 Tick,请在交易时段补测"
        for item in ("T3", "T5", "P1.2"):
            book.add(item, SKIP, reason, [Shot(lbl, [reason]) for lbl in (("开仓", "平仓") if item == "P1.2" else ("",))], started)
        return
    held, connected = check_positions(ctx.main_engine, ctx.symbol, wait=ctx.position_wait, early_exit=False)
    if held or not connected:
        reason = f"账户已有持仓 {held}" if held else "交易连接断开,无法确认账户空仓"
        for item in ("T3", "T5", "P1.2"):
            book.add(item, FAIL, f"已中止开平仓演示:{reason}", [Shot("", [reason])], started)
        return
    tickp = c.pricetick or 1.0
    buy_px = round_to(float(tick.ask_price_1 or tick.last_price), tickp)
    open_id = send(ctx, make_req(ctx, price=buy_px))
    open_order = ctx.collector.wait_order_final(open_id, timeout=15.0) if open_id else None
    open_ok = open_order is not None and open_order.status == Status.ALLTRADED
    open_lines = [f"买开 1 手 @ {buy_px}(对手价) 委托号 {open_id or '-'}",
                  f"终态 {open_order.status.value if open_order else '无回报'}  成交 {getattr(open_order, 'traded', 0)} 手"]
    if not open_ok:
        cancel_active(ctx, [open_id] if open_id else [])
        for item, label in (("T3", ""), ("T5", ""), ("P1.2", "开仓")):
            book.add(item, FAIL, "开仓未成交", [Shot(label, open_lines)], started)
        return
    sell_px = round_to(float(tick.bid_price_1 or tick.last_price), tickp)
    close_offset = Offset.CLOSETODAY if c.exchange in (Exchange.SHFE, Exchange.INE) else Offset.CLOSE
    close_id = send(ctx, make_req(ctx, direction=Direction.SHORT, offset=close_offset, price=sell_px))
    close_order = ctx.collector.wait_order_final(close_id, timeout=15.0) if close_id else None
    close_ok = close_order is not None and close_order.status == Status.ALLTRADED
    close_lines = [f"卖平 1 手 @ {sell_px}(对手价) 委托号 {close_id or '-'}",
                   f"终态 {close_order.status.value if close_order else '无回报'}  成交 {getattr(close_order, 'traded', 0)} 手"]
    after, _ = check_positions(ctx.main_engine, ctx.symbol, wait=ctx.position_wait, early_exit=False)
    flat = not after
    close_lines.append("平仓后账户空仓" if flat else f"⚠ 平仓后仍有持仓 {after},请立即人工平仓")
    trade_lines = [ctx.clean(f"成交回报 {t.vt_symbol} {t.direction.value}{t.offset.value} {t.volume}@{t.price} 成交号 {t.tradeid}")
                   for t in ctx.trades[-4:]] or ["(未收到成交回报)"]
    everything = open_ok and close_ok and flat
    book.add("T3", PASS if everything else FAIL, "" if everything else "平仓未完成或未回到空仓", [Shot("", open_lines + close_lines)], started)
    book.add("T5", PASS if ctx.trades else FAIL, "" if ctx.trades else "没有收到成交回报", [Shot("", trade_lines)], started)
    book.add("P1.2", PASS if everything else FAIL, "" if everything else "平仓未完成或未回到空仓",
             [Shot("开仓", open_lines), Shot("平仓", close_lines)], started)


def step_cancel(ctx: Ctx, book: Book) -> None:
    """T4 与 P1.2 的「撤单」:跌停价买单 → 撤单。"""
    started = ctx.now()
    if ctx.tick is None or not ctx.in_session:
        reason = "非交易时段或没有 Tick,请在交易时段补测"
        book.add("T4", SKIP, reason, [Shot("", [reason])], started)
        book.add("P1.2", SKIP, reason, [Shot("撤单", [reason])], started)
        return
    run = Run(secrets=ctx.secrets)
    code = do_order_cancel(run, ctx.main_engine, ctx.collector, ctx.contract, ctx.tick)
    lines = lines_of(run, ctx)
    failed = run.failed() or code != 0
    summary = "" if not failed else "报单/撤单步骤未全部通过"
    book.add("T4", FAIL if failed else PASS, summary, [Shot("", lines)], started)
    book.add("P1.2", FAIL if failed else PASS, summary, [Shot("撤单", lines)], started)


def step_counts(ctx: Ctx, book: Book) -> None:
    """P2.2:读风控规则的报单/撤单/成交计数(本次运行内)。"""
    started = ctx.now()
    daily = ctx.rule(DAILY_RULE)
    if daily is None:
        book.add("P2.2", FAIL, f"找不到「{DAILY_RULE}」规则", [Shot("报单笔数统计", ["规则缺失"]), Shot("撤单笔数统计", ["规则缺失"])], started)
        return
    orders, cancels, trades = daily.total_order_count, daily.total_cancel_count, daily.total_trade_count
    per = {k: v for k, v in dict(daily.contract_order_count).items()}
    lines_order = [f"统计时间 {ctx.now()}", f"汇总报单笔数 {orders}(上限 {daily.total_order_limit})",
                   f"分合约报单笔数 {per or '-'}", f"汇总成交笔数 {trades}"]
    lines_cancel = [f"统计时间 {ctx.now()}", f"汇总撤单笔数 {cancels}(上限 {daily.total_cancel_limit})",
                    f"分合约撤单笔数 {dict(daily.contract_cancel_count) or '-'}"]
    have = orders > 0
    if not have:
        book.add("P2.2", SKIP, "本次运行还没有产生委托(非交易时段?),计数为 0", [Shot("报单笔数统计", lines_order), Shot("撤单笔数统计", lines_cancel)], started)
    else:
        book.add("P2.2", PASS, "", [Shot("报单笔数统计", lines_order), Shot("撤单笔数统计", lines_cancel)], started)


def step_duplicate(ctx: Ctx, book: Book) -> dict:
    """P2.3:连发要素完全相同的委托,看重复报单监测是否拦下并预警。返回给 P3.1 复用的结果。"""
    started = ctx.now()
    rule = ctx.rule(DUP_RULE)
    price = probe_price(ctx)           # 四次发送用同一个价格(=要素完全相同),但不与其它步骤共用
    original = rule.duplicate_order_limit if rule else None
    sent: list[str] = []
    lines, blocked = [], False
    mark, alert_mark = ctx.log_mark(), len(ctx.alerts)
    if rule is not None:
        rule.duplicate_order_limit = 2
        lines.append("设置重复报单阈值:相同委托累计达到 2 笔即拦截")
        try:
            for n in range(1, 5):
                oid = send(ctx, make_req(ctx, price=price))
                lines.append(f"第 {n} 笔相同委托(买开 1 手 @ {price}):" + ("通过风控" if oid else "被拦截"))
                if oid:
                    sent.append(oid)
                else:
                    blocked = True
                    break
        finally:
            rule.duplicate_order_limit = original         # 无论如何还原阈值,后面的步骤还在同一个进程里用这条规则
            cancel_active(ctx, sent)
    ctx.wait_until(lambda: any(a["title"] == "风控阈值触发" and "重复报单" in a["message"] for a in ctx.alerts_since(alert_mark)), 4.0)
    hits = ctx.logs_since(mark, "重复报单笔数")
    alerts = [a for a in ctx.alerts_since(alert_mark) if "重复报单" in a["message"]]
    lines += [ctx.clean(f"风控日志:{h[:200]}") for h in hits[:2]]
    lines += [ctx.clean(f"预警:[{a['level']}] {a['title']} {a['message'][:160]}") for a in alerts[:1]]
    forward_alerts(ctx, alerts)
    ok = rule is not None and blocked and bool(alerts)
    status = PASS if ok else FAIL
    summary = "" if ok else ("找不到重复报单规则" if rule is None else "没有触发重复报单拦截或预警")
    book.add("P2.3", status, summary, [Shot("", lines)], started)
    return {"ok": ok, "lines": lines}


def forward_alerts(ctx: Ctx, alerts: list[dict]) -> None:
    """若配置了 Webhook(如 Telegram),把演示触发的预警真的推出去——预警方式的实证。"""
    if ctx.notifier is None:
        return
    for a in alerts:
        try:
            ctx.notifier.send(a["level"], ctx.clean(a["title"]), ctx.clean(a["message"]))
        except Exception:   # noqa: BLE001  推送失败不影响取证
            pass


def step_threshold(ctx: Ctx, book: Book, dup: dict) -> None:
    """P3.1 / T6:报单总笔数、撤单总笔数、重复报单总笔数三类阈值的设置与预警。"""
    started = ctx.now()
    daily = ctx.rule(DAILY_RULE)
    shots, oks = [], []

    def metric(label: str, key_word: str, set_limit: Callable[[int], None], base: int, restore: Callable[[], None], need_cancel: bool) -> None:
        mark, alert_mark = ctx.log_mark(), len(ctx.alerts)
        lines, made = [], []
        limit = base + 1
        set_limit(limit)
        lines.append(f"设置{label}阈值 = {limit}(当前已有 {base})")
        try:
            a = send(ctx, make_req(ctx, price=probe_price(ctx)))
            if a:
                made.append(a)
            lines.append(f"阈值内委托:{'通过' if a else '被拦截(意外)'}")
            if need_cancel and a:
                order_a = ctx.collector.order_of(a)
                if order_a is not None:
                    ctx.main_engine.cancel_order(order_a.create_cancel_request(), ctx.gateway_name)
                cancelled = ctx.wait_until(lambda: ctx.collector.order_of(a) is not None and not ctx.collector.order_of(a).is_active(),
                                           ctx.cancel_wait)
                final = ctx.collector.order_of(a)
                if cancelled and final is not None and final.status == Status.CANCELLED:
                    lines.append("已撤销该委托,累计撤单数达到阈值")
                else:
                    lines.append(f"⚠ 撤单未确认(终态 {final.status.value if final else '无回报'}),撤单计数没有达到阈值")
            b = send(ctx, make_req(ctx, price=probe_price(ctx)))
            if b:
                made.append(b)
            lines.append(f"超过阈值的委托:{'被拦截' if not b else '未被拦截(意外)'}")
            ctx.wait_until(lambda: any(al["title"] == "风控阈值触发" and key_word in al["message"] for al in ctx.alerts_since(alert_mark)), 4.0)
            alerts = [al for al in ctx.alerts_since(alert_mark) if key_word in al["message"]]
            lines += [ctx.clean(f"风控日志:{h[:200]}") for h in ctx.logs_since(mark, key_word)[:1]]
            lines += [ctx.clean(f"预警:[{al['level']}] {al['title']} {al['message'][:160]}") for al in alerts[:1]]
            forward_alerts(ctx, alerts)
        finally:
            restore()                      # 无论如何还原阈值并撤掉演示委托,不留下挂单
            cancel_active(ctx, made)
        ok = bool(a) and not b and bool(alerts)
        oks.append(ok)
        shots.append(Shot(label, lines))

    if daily is None:
        book.add("P3.1", FAIL, f"找不到「{DAILY_RULE}」规则", [Shot("", ["规则缺失"])], started)
        book.add("T6", FAIL, f"找不到「{DAILY_RULE}」规则", [Shot("", ["规则缺失"])], started)
        return
    orig_o, orig_c = daily.total_order_limit, daily.total_cancel_limit
    metric("报单总笔数", "汇总委托笔数", lambda n: setattr(daily, "total_order_limit", n), daily.total_order_count,
           lambda: setattr(daily, "total_order_limit", orig_o), need_cancel=False)
    order_ok = oks[-1]
    if ctx.in_session:
        metric("撤单总笔数", "汇总撤单笔数", lambda n: setattr(daily, "total_cancel_limit", n), daily.total_cancel_count,
               lambda: setattr(daily, "total_cancel_limit", orig_c), need_cancel=True)
        cancel_ok = oks[-1]
    else:
        shots.append(Shot("撤单总笔数", ["非交易时段委托不会进入可撤状态,撤单阈值无法演示,请在交易时段补测"]))
        cancel_ok = None
    shots.append(Shot("重复报单总笔数", dup.get("lines", ["(重复报单演示未执行)"])))
    dup_ok = bool(dup.get("ok"))
    if order_ok and dup_ok and cancel_ok:
        status, summary = PASS, ""
    elif cancel_ok is None and order_ok and dup_ok:
        status, summary = SKIP, "撤单总笔数阈值需在交易时段补测"
    else:
        status, summary = FAIL, "有阈值未能触发拦截与预警"
    book.add("P3.1", status, summary, shots, started)
    t6_status = PASS if order_ok else FAIL
    book.add("T6", t6_status, "" if order_ok else "报单阈值未能限制下单", [Shot("", shots[0].lines)], started)


def step_validity(ctx: Ctx, book: Book) -> None:
    """P4.1:错误合约代码、不合法价位、超单笔手数,都应被本地风控在发出前拦下。"""
    started = ctx.now()
    c = ctx.contract
    base = float(getattr(ctx.tick, "last_price", 0) or 0) or 1000.0
    tickp = c.pricetick or 1.0
    fair = round_to(base, tickp)
    size_rule = ctx.rule(SIZE_RULE)
    big = int(size_rule.order_volume_limit) + 1 if size_rule is not None else int(getattr(c, "max_volume", 0) or 1) + 1
    cases = [("合约代码", make_req(ctx, price=fair, symbol="c9999"), "不存在"),
             ("合约最小变动价位", make_req(ctx, price=fair + tickp * 0.5), "最小变动价位"),
             ("单笔委托最大手数", make_req(ctx, price=fair, volume=big), "超过上限")]
    shots, oks = [], []
    for label, req, needle in cases:
        mark = ctx.log_mark()
        oid = send(ctx, req)
        def blocked_lines() -> list[str]:
            return [ln for ln in ctx.logs_since(mark, needle) if "委托被拦截" in ln]      # 只认风控引擎的拦截日志

        ctx.wait_until(lambda: bool(blocked_lines()), 2.0)
        hit = blocked_lines()
        ok = (not oid) and bool(hit)
        oks.append(ok)
        shots.append(Shot(label, [f"发送错误委托:{ctx.clean(str(req.symbol))} 买开 {req.volume} 手 @ {req.price}",
                                  "结果:本地风控已拒绝,未发往柜台" if not oid else "⚠ 委托没有被拦截",
                                  *[ctx.clean(f"风控日志:{h[:200]}") for h in hit[:1]]]))
        cancel_active(ctx, [oid] if oid else [])
    ok_all = all(oks)
    book.add("P4.1", PASS if ok_all else FAIL, "" if ok_all else "有错误指令没有被本地检查拦截", shots, started)


def step_counter_errors(ctx: Ctx, book: Book) -> None:
    """P4.2:**绕过本地风控**直发,采集真实柜台错误提示(资金不足 / 无仓位可平 / 市场状态不允许)。"""
    started = ctx.now()
    c = ctx.contract
    base = float(getattr(ctx.tick, "last_price", 0) or 0) or 1000.0
    cases = {name: req for name, req in build_cases(ctx.symbol, c.exchange, base, int(getattr(c, "max_volume", 0) or 1),
                                                   pricetick=c.pricetick, in_session=ctx.in_session)}
    low = round_to(base * 0.5, c.pricetick or 1.0)
    fund = make_req(ctx, price=low if ctx.in_session else round_to(base, c.pricetick or 1.0), volume=max(1, int(getattr(c, "max_volume", 0) or 1)))
    plan = [("无足够资金开仓", fund, "资金"), ("无仓位可平", cases.get("无持仓时平仓"), ""),
            ("市场状态不允许", cases.get("非交易时段开仓"), "")]
    gateway = ctx.main_engine.get_gateway(ctx.gateway_name)
    shots, oks, notes = [], [], []
    for label, req, expect in plan:
        if req is None:
            shots.append(Shot(label, ["当前处于交易时段,该场景无法演示(需非交易时段),请在非交易时段补测"]))
            notes.append(f"{label}需在非交易时段补测")
            oks.append(None)
            continue
        ts, since = time.monotonic(), ctx.collector.error_index()
        oid = gateway.send_order(req)
        order = ctx.collector.wait_order_final(oid, timeout=8.0) if oid else None
        text = ctx.collector.wait_error(ts, timeout=5.0, since=since)
        ok = bool(text) and (not expect or expect in text)
        shots.append(Shot(label, [ctx.clean(f"直发委托(绕过本地风控):{req.symbol} {req.direction.value}{req.offset.value} {req.volume}手 @ {req.price}"),
                                  ctx.clean(f"委托终态:{order.status.value if order else '无回报'}"),
                                  ctx.clean(f"柜台错误提示:{text or '(未收到错误文本)'}")]))
        if not ok and expect:
            notes.append(f"{label}:柜台没有返回含「{expect}」的错误(账户资金充裕时无法触发资金不足)")
        oks.append(ok)
        cancel_active(ctx, [oid] if oid else [])
    if all(o is True for o in oks):
        status, summary = PASS, ""
    elif any(o is False for o in oks):
        status, summary = FAIL, ";".join(notes) or "有场景没有收到柜台错误提示"
    else:
        status, summary = SKIP, ";".join(notes)
    book.add("P4.2", status, summary, shots, started)


def step_pause(ctx: Ctx, book: Book) -> None:
    """P5.1:暂停标志 → 开仓被拒、平仓放行 → 解除后恢复。"""
    started = ctx.now()
    rule = ctx.rule(ACCOUNT_RULE_NAME)
    state_dir = rule.guard.state_path.parent if rule is not None and rule.guard.state_path is not None else None
    if state_dir is None:
        book.add("P5.1", FAIL, "风控规则没有状态目录,暂停标志不可用", [Shot("", ["规则缺失或无状态文件"])], started)
        return
    mark, alert_mark = ctx.log_mark(), len(ctx.alerts)
    lines, made = [], []
    pause_trading.pause(state_dir, "国信测试演示")
    try:
        lines.append(f"{ctx.now()} 执行 pause_trading on(暂停新开仓)")
        ctx.wait_until(lambda: any(a["title"] == "交易已暂停" for a in ctx.alerts_since(alert_mark)), 4.0)
        opened = send(ctx, make_req(ctx, price=probe_price(ctx)))
        closed = send(ctx, make_req(ctx, direction=Direction.SHORT, offset=Offset.CLOSE, price=probe_price(ctx)))
        for oid in (opened, closed):
            if oid:
                made.append(oid)
        lines.append("暂停期间开仓委托:" + ("被拒绝" if not opened else "⚠ 未被拒绝"))
        lines.append("暂停期间平仓委托:" + ("放行(平仓永远放行)" if closed else "⚠ 被拒绝"))
        lines += [ctx.clean(f"风控日志:{h[:160]}") for h in ctx.logs_since(mark, "已手动暂停")[:1]]
    finally:
        pause_trading.resume(state_dir)                   # 无论如何解除暂停标志:不能让演示遗留一个"暂停中"的运行机
        cancel_active(ctx, made)
    lines.append(f"{ctx.now()} 执行 pause_trading off(恢复)")
    ctx.wait_until(lambda: any(a["title"] == "交易已恢复" for a in ctx.alerts_since(alert_mark)), 4.0)
    resumed = send(ctx, make_req(ctx, price=probe_price(ctx)))
    if resumed:
        made.append(resumed)
    lines.append("恢复后开仓委托:" + ("通过风控" if resumed else "⚠ 仍被拒绝"))
    alerts = [a for a in ctx.alerts_since(alert_mark) if a["title"] in ("交易已暂停", "交易已恢复")]
    lines += [ctx.clean(f"告警:[{a['level']}] {a['title']}") for a in alerts]
    forward_alerts(ctx, alerts)
    cancel_active(ctx, made)
    ok = (not opened) and bool(closed) and bool(resumed) and len(alerts) >= 2
    book.add("P5.1", PASS if ok else FAIL, "" if ok else "暂停/恢复行为或告警不符合预期", [Shot("", lines)], started)


def step_stability(ctx: Ctx, book: Book, total: int | None = None) -> None:
    """P1.3(模板 2.1 稳定运行):经风控链持续下单 N 笔(程序化系统 200 笔),记录下单到全部回报的耗时。

    演示期间临时抬高会被 200 笔拦住的风控上限(日委托/撤单、活动委托、重复报单、撤单锁定),演示后还原——
    取证状态目录是隔离的,这里验证的是「持续下单是否稳定」而不是风控阈值(阈值由 4.1 单独演示)。
    撤单锁定的计数(cancel_count)也一并回退,否则清场撤单会让后续步骤落入「当日撤单锁定」。
    """
    started = ctx.now()
    total = total or STABILITY_ORDERS
    if ctx.tick is None or not ctx.in_session:
        reason = "非交易时段或没有 Tick,委托不会被柜台正常接受,请在交易时段补测"
        book.add("P1.3", SKIP, reason, [Shot(f"{total}笔", [reason])], started)
        return
    lifted: list[tuple[object, str, object]] = []

    def lift(rule, attr: str) -> None:
        if rule is not None and hasattr(rule, attr):
            lifted.append((rule, attr, getattr(rule, attr)))
            setattr(rule, attr, STABILITY_LIMIT)

    daily, active, dup, guard = ctx.rule(DAILY_RULE), ctx.rule("活动委托检查"), ctx.rule(DUP_RULE), ctx.rule(ACCOUNT_RULE_NAME)
    saved_cancel_count = getattr(guard, "cancel_count", None)
    ids: list[str] = []
    blocked = 0
    elapsed_acked = elapsed_sent = 0.0
    final: dict[str, int] = {}
    problems: list[str] = []

    def unresolved() -> int:
        """没有回报或仍可撤的演示委托数:拿不到委托数据的一律按「未清场」算,不当成已撤。"""
        count = 0
        for oid in ids:
            order = ctx.collector.order_of(oid)
            if order is None or order.is_active():
                count += 1
        return count

    def cleanup() -> None:
        try:
            cancel_active(ctx, ids)                                # 无论如何清场:不留 200 笔挂单
            ctx.wait_until(lambda: unresolved() == 0, ctx.stability_wait)
        except Exception as exc:    # noqa: BLE001  清场出错也必须继续还原上限
            problems.append(f"清场出错:{exc!r}")

    try:                                                           # 抬高上限之前就进入保护区:中途异常也能还原
        for rule, attrs in ((daily, ("total_order_limit", "contract_order_limit", "total_cancel_limit", "contract_cancel_limit")),
                            (active, ("active_order_limit",)), (dup, ("duplicate_order_limit",)), (guard, ("cancel_lock_limit",))):
            for attr in attrs:
                lift(rule, attr)
        try:
            t0 = ctx.clock()
            for n in range(total):
                oid = send(ctx, make_req(ctx, price=probe_price(ctx)))
                if oid:
                    ids.append(oid)
                else:
                    blocked += 1
                if ctx.stability_interval:
                    ctx.sleep(ctx.stability_interval)
            elapsed_sent = ctx.clock() - t0

            def all_acked() -> bool:
                for oid in ids:
                    order = ctx.collector.order_of(oid)
                    if order is None or order.status == Status.SUBMITTING:
                        return False
                return True

            ctx.wait_until(all_acked, ctx.stability_wait)
            elapsed_acked = ctx.clock() - t0
            for oid in ids:
                order = ctx.collector.order_of(oid)
                key = order.status.value if order is not None else "无回报"
                final[key] = final.get(key, 0) + 1
        except Exception as exc:    # noqa: BLE001  转成失败的证据:不让异常冒出去丢掉整次运行的证据
            problems.append(f"下单过程出错:{exc!r}")
        finally:
            cleanup()
    finally:
        for rule, attr, value in lifted:
            try:
                setattr(rule, attr, value)
            except Exception as exc:    # noqa: BLE001  单项还原失败不影响其余项
                problems.append(f"还原 {attr} 失败:{exc!r}")
        if guard is not None and saved_cancel_count is not None:
            guard.cancel_count = saved_cancel_count
    leftover = unresolved()
    rejected = final.get(Status.REJECTED.value, 0)
    silent = final.get("无回报", 0) + final.get(Status.SUBMITTING.value, 0)
    ok = len(ids) == total and rejected == 0 and silent == 0 and leftover == 0 and not problems
    lines = [f"{ctx.now()} 持续下单 {total} 笔(买开 1 手,不易成交的限价,逐笔间隔 {ctx.stability_interval}s)",
             f"经风控放行 {len(ids)} 笔,被本地风控拦截 {blocked} 笔",
             f"下单耗时 {elapsed_sent:.1f} 秒;至全部回报到达共 {elapsed_acked:.1f} 秒",
             "委托终态分布:" + ("、".join(f"{k} {v}" for k, v in sorted(final.items())) or "-"),
             "清场:演示委托已全部撤销" if not leftover else f"⚠ 仍有 {leftover} 笔演示委托未确认撤销,请人工处理"]
    lines += [f"⚠ {text}" for text in problems]
    summary = "" if ok else (f"放行 {len(ids)}/{total} 笔,拒单 {rejected},无回报 {silent},未清场 {leftover}"
                             + (";" + ";".join(problems) if problems else ""))
    book.add("P1.3", PASS if ok else FAIL, summary, [Shot(f"{total}笔", lines)], started,
             metrics={"orders": total, "accepted": len(ids), "rejected": rejected, "elapsed_seconds": round(elapsed_acked, 1),
                      "send_seconds": round(elapsed_sent, 1)})


def step_connection(ctx: Ctx, book: Book) -> None:
    """P2.1:半自动——提示操作者断开网络再恢复,从网关日志读检测时间线。"""
    started = ctx.now()
    if not ctx.interactive:
        book.add("P2.1", MANUAL, "需要人工断网演练:加 --interactive 运行,或在运行中手动断开/恢复网络后用 --resume 补测",
                 [Shot("", ["未执行断网演练(非交互模式)"])], started)
        return
    print("\n[3.1 连接断开监测] 请在提示后**断开运行机的网络**(拔网线/禁用网卡),"
          f"约 10-30 秒后**恢复**;脚本最多监测 {ctx.disconnect_timeout:.0f} 秒。")
    input("准备好后按回车开始监测...")
    mark = ctx.log_mark()
    t0 = time.monotonic()
    ctx.wait_until(lambda: bool(ctx.logs_since(mark, "连接断开")), ctx.disconnect_timeout)
    down_at = time.monotonic() - t0
    ctx.wait_until(lambda: bool(ctx.logs_since(mark, "登录成功", "连接成功")) and bool(ctx.logs_since(mark, "连接断开")),
                   max(5.0, ctx.disconnect_timeout - down_at))
    downs, ups = ctx.logs_since(mark, "连接断开"), ctx.logs_since(mark, "登录成功", "连接成功")
    lines = [f"监测起点 {started}"] + [ctx.clean(f"检测到断开:{d}") for d in downs[:2]] + [ctx.clean(f"检测到恢复:{u}") for u in ups[:2]]
    ok = bool(downs) and bool(ups)
    book.add("P2.1", PASS if ok else FAIL, "" if ok else "没有同时检测到断开与恢复", [Shot("", lines)], started)


def step_exit(ctx: Ctx, book: Book) -> None:
    """T7:异常时自动退出——跑运行时的退出语义离线测试(代码级验证,假网关),如实标注。"""
    started = ctx.now()
    tests = bootstrap.ROOT / "tests" / "test_runtime.py"
    if not tests.exists():
        book.add("T7", MANUAL, "运行机上没有 tests/ 目录,无法执行退出语义测试", [Shot("", ["tests/test_runtime.py 不存在"])], started)
        return
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests), "-k", "hard_lock or refuses or shutdown"]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300, cwd=bootstrap.ROOT, check=False)
        tail = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-6:]
        ok = proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        tail, ok = [repr(exc)], False
    lines = ["说明:代码级验证(假网关离线端到端测试),验证运行时在硬锁定/启动失败/对账失败等异常下按退出码语义自动退出;",
             "退出码:0 正常/硬锁定后确认空仓(不自动重启);2 配置错误;3 启动失败;4 对账失败。",
             "命令:pytest tests/test_runtime.py -k \"hard_lock or refuses or shutdown\""] + [ctx.clean(x) for x in tail]
    book.add("T7", PASS if ok else FAIL, "" if ok else "退出语义测试未通过", [Shot("", lines)], started)


LOG_CATEGORIES = {
    "交易日志": ("Send new order", "成交", "委托"),
    "系统运行记录": ("启动", "连接成功", "登录成功", "结算信息确认成功", "合约信息查询成功", "状态文件", "加载成功"),
    "监测记录": ("风控阈值触发", "连接断开", "交易已暂停", "交易已恢复"),
    "错误提示记录": ("交易委托失败", "委托被拦截", "[ERROR]", "错误"),
}


def lines_since(lines: list[str], start: datetime) -> list[str]:
    """只保留 start 之后的日志行(同一天多次运行共用一个日志文件,旧运行的日志不能当成本次证据)。
    没有时间戳的续行(如异常栈)跟随它前一行的取舍。"""
    kept, keeping = [], True
    for ln in lines:
        try:
            keeping = datetime.strptime(ln[:23], "%Y-%m-%d %H:%M:%S.%f") >= start
        except ValueError:
            pass
        if keeping:
            kept.append(ln)
    return kept


def collect_log_lines(ctx: Ctx) -> list[str]:
    """优先读本次运行的日志文件(vn.py 每日日志,只取本次运行开始之后的行),读不到退回网关事件日志。"""
    files = sorted((BT_ROOT / "log").glob("vt_*.log")) if (BT_ROOT / "log").exists() else []
    if files:
        try:
            return lines_since(files[-1].read_text(encoding="utf-8", errors="replace").splitlines(), ctx.started_wall)
        except OSError:
            pass
    with ctx.collector.lock:
        return list(ctx.collector.log_lines)


def step_logs(ctx: Ctx, book: Book) -> None:
    """P6.1:交易日志 / 系统运行记录 / 监测记录 / 错误提示记录 四类日志各摘录一段。"""
    started = ctx.now()
    all_lines = collect_log_lines(ctx)
    shots, missing = [], []
    for label, needles in LOG_CATEGORIES.items():
        hits = [ctx.clean(ln[:220]) for ln in all_lines if any(n in ln for n in needles)]
        if not hits:
            missing.append(label)
        shots.append(Shot(label, (hits[-12:] if hits else ["(本次运行没有这类日志)"])))
    book.add("P6.1", PASS if not missing else FAIL, "" if not missing else f"缺少日志类别:{'、'.join(missing)}", shots, started)


def step_batch_cancel(ctx: Ctx, book: Book) -> None:
    book.add("P5.2", SKIP, "批量撤单(选测)尚未实现,见 docs/roadmap.md §2.6", [Shot("", ["未实现(选测项,报告标「未测试」)"])], ctx.now())


def final_safety(ctx: Ctx) -> list[str]:
    """收尾兜底:撤掉所有遗留委托,再确认账户没有留下持仓。演示用的限价单在极端行情下可能意外成交,不能不查。"""
    notes: list[str] = []
    try:
        active = list(ctx.main_engine.get_all_active_orders())
        for order in active:
            ctx.main_engine.cancel_order(order.create_cancel_request(), order.gateway_name)
        if active:
            ctx.wait_until(lambda: not list(ctx.main_engine.get_all_active_orders()), FINAL_CANCEL_WAIT)
            left = len(list(ctx.main_engine.get_all_active_orders()))
            if left:
                notes.append(f"⚠⚠ 收尾:发出撤单请求后仍有 {left}/{len(active)} 笔委托未确认撤销,请立即人工撤单!")
            else:
                notes.append(f"收尾:撤销了 {len(active)} 笔遗留委托(已确认)")
        held, _ = check_positions(ctx.main_engine, ctx.symbol, wait=ctx.position_wait, early_exit=False)
        if held:
            notes.append(f"⚠⚠ 收尾发现账户有持仓 {held},请立即人工平仓!")
    except Exception as exc:     # noqa: BLE001  兜底本身不能崩
        notes.append(f"收尾检查出错:{exc!r},请人工核对账户是否有遗留委托/持仓")
    return notes


def run_steps(ctx: Ctx, book: Book, only: set[str] | None = None) -> bool:
    """按序执行全部步骤。登录失败则后续依赖连接的步骤不再执行。返回是否登录成功。"""
    def want(*items: str) -> bool:
        return only is None or any(i in only for i in items)

    logged_in = step_login(ctx, book)
    if not logged_in:
        return False
    c = ctx.collector.contract_of(ctx.symbol)
    if c is None:
        book.add("T2", FAIL, f"合约表里没有 {ctx.symbol}", [Shot("", ["合约不存在"])], ctx.now())
        return True
    ctx.contract = c
    ctx.in_session = in_trading_session(beijing_now())
    if want("T2", "T3", "T4", "T5"):
        step_market(ctx, book)
    order_items = ORDER_ITEMS if only is None else (only & ORDER_ITEMS)
    gated = False
    if order_items:
        gated = not ensure_flat(ctx)
        if gated:
            reason = (f"{ctx.flat_reason}:已中止所有会发单的演示"
                      "(平仓类演示单会真的平掉仓位),请先人工处理后重跑")
            for name in sorted(order_items):
                book.add(name, FAIL, reason, [Shot("", [reason])], ctx.now())
    if not gated and want("T3", "T5", "P1.2"):
        step_basic_trade(ctx, book)
        if ctx.in_session and ctx.tick is not None and not ensure_flat(ctx):
            gated = True                                   # 平仓演示没把账户带回空仓:后面的发单演示全部中止
            book.add("P1.2", FAIL, "开平仓演示后账户没有回到空仓,已中止后续发单演示", [Shot("", ["账户未回到空仓"])], ctx.now())
    if not gated and want("T4", "P1.2"):
        step_cancel(ctx, book)
    if want("P2.2"):
        step_counts(ctx, book)
    dup = {}
    if not gated and want("P2.3", "P3.1", "T6"):
        dup = step_duplicate(ctx, book)
    if not gated and want("P3.1", "T6"):
        step_threshold(ctx, book, dup)
    if want("P4.1"):
        step_validity(ctx, book)
    if not gated and want("P4.2"):
        step_counter_errors(ctx, book)
    if not gated and want("P5.1"):
        step_pause(ctx, book)
    if not gated and want("P1.3"):
        step_stability(ctx, book)
    if want("P2.1"):
        step_connection(ctx, book)
    if want("T7"):
        step_exit(ctx, book)
    if want("P5.2"):
        step_batch_cancel(ctx, book)
    if want("P6.1"):
        step_logs(ctx, book)         # 最后做:前面各步产生的日志才齐
    return True


# ======================= 入口 =======================

def print_summary(evs: list[Evidence]) -> None:
    from broker_evidence import STATUS_LABEL
    print("\n=== 取证结果 ===")
    for ev in evs:
        print(f"  {ev.item:<5} {STATUS_LABEL.get(ev.status, ev.status):<4} {ev.title}" + (f"  — {ev.summary}" if ev.summary else ""))
    missing = [e for e in evs if e.status != PASS]
    if missing:
        print(f"\n{len(missing)} 项不是「通过」:SKIP 的在交易时段用 --resume 补测;MANUAL 的需人工演练;FAIL 的先排查。")


def connection_info(setting: dict, now: datetime | None = None) -> dict:
    """本次连接的环境事实。不含用户名/密码/授权编码;AppID 只留掩码。"""
    return {
        "at": (now or datetime.now()).isoformat(timespec="seconds"),
        "environment": setting["柜台环境"],
        "trade_front": setting["交易服务器"],
        "market_front": setting["行情服务器"],
        "broker_id": setting["经纪商代码"],
        "app_id": mask_account(setting["产品名称"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="测试合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--confirm-broker-test", action="store_true", help="确认目标是期货公司的测试环境")
    parser.add_argument("--interactive", action="store_true", help="执行需要人工配合的 3.1 断网演练")
    parser.add_argument("--resume", type=Path, default=None, help="在已有证据目录上增量补测(新的 SKIP 不覆盖旧的 PASS/FAIL)")
    parser.add_argument("--out-dir", type=Path, default=None, help="证据输出目录(默认 data/broker_test/<时间戳>)")
    parser.add_argument("--only", default="", help="只跑指定测试项,逗号分隔,如 T3,P4.1")
    parser.add_argument("--connect-timeout", type=float, default=120.0)
    parser.add_argument("--disconnect-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    bootstrap.load_env()
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
    if not ISOLATED:
        print("状态目录隔离未生效(vnpy 日志模块在设置 TEMP_DIR 之前已被导入),取证日志会写进实盘目录,拒绝运行。"
              "请直接用 python scripts/broker_test.py 启动,不要从别的模块导入它。", file=sys.stderr)
        return EXIT_CONFIG
    # 前置地址白名单:由一键流程注入,或写在 .env 的 BROKER_TEST_ALLOWED_TD/MD 里。
    # 没有白名单时,「柜台环境=测试」这一句不足以证明连的是期货公司的测试机——
    # 那台机器只按环境变量判断,不校验地址,所以这里必须自己校验。
    allowed_td = _allowed_addresses(env.get("BROKER_TEST_ALLOWED_TD", "")) or None
    allowed_md = _allowed_addresses(env.get("BROKER_TEST_ALLOWED_MD", "")) or None
    ok, note = check_target(setting["柜台环境"], setting["交易服务器"], args.confirm_broker_test, allowed_td)
    print(note, file=sys.stderr if not ok else sys.stdout)
    if not ok:
        return EXIT_CONFIG
    if setting["柜台环境"] == "测试" and not allowed_td:
        print("柜台环境=测试 但没有配置前置地址白名单(BROKER_TEST_ALLOWED_TD),拒绝连接:"
              "「测试」这个标记不说明连的是哪台机器。请用一键流程(它会注入白名单),"
              "或在 .env 里写 BROKER_TEST_ALLOWED_TD=host:port[,host:port]。", file=sys.stderr)
        return EXIT_CONFIG
    md_address = normalize_address(setting["行情服务器"])
    if allowed_td and not allowed_md:
        # 交易前置查了、行情前置没查,等于留了半个口子:行情前置喂回来的盘口价
        # 会**直接变成委托价**,连错行情前置 = 用假价格下单(演示委托可能因此成交,留下持仓)。
        print("交易前置白名单已配置但行情前置白名单(BROKER_TEST_ALLOWED_MD)没配置,拒绝连接:"
              "行情前置的盘口价会直接变成委托价,必须和交易前置一样校验。请用一键流程"
              "(它会同时注入两个白名单),或在 .env 里写 BROKER_TEST_ALLOWED_MD=host:port[,host:port]。",
              file=sys.stderr)
        return EXIT_CONFIG
    if allowed_md:
        if md_address not in allowed_md:
            print(f"行情前置 {md_address} 不在允许的前置地址表 {sorted(allowed_md)} 内,拒绝连接。", file=sys.stderr)
            return EXIT_CONFIG
    elif md_address not in SIMNOW_MD:
        print(f"行情前置 {md_address} 不在任何白名单内(也没有配置 BROKER_TEST_ALLOWED_MD),拒绝连接。"
              "行情前置喂回来的盘口价会直接变成委托价,不能靠「交易前置是白名单内的」来推定它也是。",
              file=sys.stderr)
        return EXIT_CONFIG
    print(f"交易前置 {setting['交易服务器']} / 行情前置 {setting['行情服务器']} —— "
          f"白名单校验通过(BROKER_TEST_ALLOWED_TD={sorted(allowed_td) if allowed_td else '未设置,SimNow 场景按 SimNow 白名单'}、"
          f"BROKER_TEST_ALLOWED_MD={sorted(allowed_md) if allowed_md else '未设置'})。")
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    unknown = (only or set()) - set(ITEMS)
    if unknown:
        print(f"未知测试项:{sorted(unknown)};可用:{list(ITEMS)}", file=sys.stderr)
        return EXIT_CONFIG

    event_engine = EventEngine()
    main_engine = MainEngine(event_engine)
    from vnpy_ctp import CtpGateway
    if "柜台环境" not in CtpGateway.default_setting:
        print("当前 vnpy_ctp 不支持「柜台环境」设置项:无法保证「测试」走测试版 API,拒绝连接。请用项目 .venv 运行。", file=sys.stderr)
        return EXIT_CONFIG
    main_engine.add_gateway(CtpGateway)
    risk_engine = main_engine.add_app(SafeRiskManagerApp)
    rule = risk_engine.rules[ACCOUNT_RULE_NAME]
    rule.update_setting({"state_file": str(BT_ROOT / "state" / "guard.json")})   # 隔离:不碰实盘的 data/state/account_guard.json

    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)
    secrets = tuple(s for s in (env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""), env.get("CTP_APP_ID", ""),
                                env.get("CTP_USERNAME", ""), env.get("WEBHOOK_URL", ""), env.get("WEBHOOK_TELEGRAM_CHAT_ID", "")) if s)
    from run_headless import build_notifier
    notifier = build_notifier(env)
    ctx = Ctx(main_engine=main_engine, risk_engine=risk_engine, collector=collector, symbol=symbol, secrets=secrets,
              interactive=args.interactive, connect_timeout=args.connect_timeout, disconnect_timeout=args.disconnect_timeout,
              notifier=notifier if notifier.enabled else None)
    event_engine.register(EVENT_GUARD_ALERT, lambda e: ctx.alerts.append(e.data))
    event_engine.register(EVENT_TRADE, lambda e: ctx.trades.append(e.data))

    started_at = datetime.now()
    print(f"连接 {symbol}(账号 {mask_account(env.get('CTP_USERNAME', ''))})...")
    main_engine.connect(setting, "CTP")
    book = Book(clean=ctx.clean)
    closed = False
    logged_in = False
    notes: list[str] = []
    try:
        logged_in = run_steps(ctx, book, only)
    finally:
        if ctx.contract is not None:                      # 已登录并拿到合约才有东西可兜底
            notes = final_safety(ctx)
        closed = close_engine(main_engine)
        if ctx.notifier is not None:
            ctx.notifier.close()
    out_dir = args.resume or args.out_dir or (OUT_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S"))
    evidences = merge_evidence(load_evidence(out_dir) if args.resume else {}, book.list())
    cjk = render_all(evidences, out_dir, secrets)
    save_evidence(out_dir, evidences)
    save_connection_info(out_dir, connection_info(setting, started_at))
    if not cjk:
        print("⚠ 没找到支持中文的字体(msyh.ttc/simhei.ttf),证据图里的中文会显示为方框;可设环境变量 BROKER_REPORT_FONT 指定字体。", file=sys.stderr)
    for note in notes:
        print(note, file=sys.stderr)
    if not closed:
        print("⚠ 引擎关闭超时,进程将被强制结束:国信「退出后是否正常注销」可能受影响,请观察。", file=sys.stderr)
    print(f"证据已写入 {out_dir}")
    print_summary(evidences)
    if not logged_in:
        return EXIT_LOGIN
    if any("⚠⚠" in n for n in notes):
        return EXIT_FAILED
    return EXIT_FAILED if any(e.status == FAIL for e in evidences) else EXIT_OK


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
        os._exit(code)         # CTP 原生线程在断连风暴下可能不退出,强制结束(关闭是否正常已在上面如实告警)
