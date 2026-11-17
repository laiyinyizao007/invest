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
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

# 隔离必须发生在任何会导入 vn.py logger 的模块之前。
BT_ROOT = bootstrap.ROOT / ".vntrader_brokertest"
ISOLATED = bootstrap.isolate_state(BT_ROOT.name)


from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import EVENT_ACCOUNT, EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER, EVENT_POSITION, EVENT_TICK, EVENT_TRADE  # noqa: E402
from vnpy.trader.object import OrderRequest, SubscribeRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402

import pause_trading  # noqa: E402
from broker_evidence import (  # noqa: E402
    FAIL, ITEMS, MANUAL, PASS, SKIP, Evidence, Shot, load_evidence, merge_evidence, render_all, save_connection_info, save_evidence,
)
from ctp_tools.collector import (  # noqa: E402
    Collector, build_cases, check_positions, close_engine, direction_text, in_trading_session,
)
from ctp_tools.broker_targets import GUOXIN_TEST_BROKER_ID, resolve_allowed_for_broker  # noqa: E402
from ctp_tools.simnow import normalize_address  # noqa: E402
from ctp_tools.profile import load_profile  # noqa: E402
from ctp_tools.penetration import (  # noqa: E402
    MILESTONE_LABELS, MILESTONES, Run, check_target, do_order_cancel, limit_down_price, mask_account, redact,
    wait_milestones,
)
from trading.events import EVENT_GUARD_ALERT  # noqa: E402
from trading.guard_rule import ACCOUNT_RULE_NAME  # noqa: E402
from risk.account_guard import AccountGuard, AccountLimits, beijing_now, trading_day  # noqa: E402
from trading.runtime import build_ctp_setting  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402

# 北京时间来自 `risk.account_guard`:预检(这里)与运行时(`trading.guard_rule`)必须用**同一个**
# 时钟函数,否则两边算出的期货交易日可能不同(2026-11-16 codex 复核 M2)。


OUT_ROOT = bootstrap.ROOT / "data" / "broker_test"
DAILY_RULE, SIZE_RULE, DUP_RULE = "每日上限检查", "委托规模检查", "重复报单检查"
EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_LOGIN, EXIT_CRASH = 0, 1, 2, 3, 4
# 会向柜台发委托的测试项:这些步骤之前必须先确认账户空仓
ORDER_ITEMS = frozenset({"T3", "T4", "T5", "P1.2", "P1.3", "P2.3", "P3.1", "T6", "P4.2", "P5.1"})
STABILITY_ORDERS = 200                     # 国信 2.1 稳定运行:程序化系统持续下单 200 笔(非程序化 50 笔)
FINAL_CANCEL_WAIT = 15.0                   # 收尾撤单:轮询等确认的上限(秒)
STABILITY_LIMIT = 10_000_000               # 稳定运行演示期间临时抬高的风控上限(演示后还原)
SETTLE_MIN_ROUNDS = 200                    # settle_counter 的轮数下限:注入的时钟不前进时也不死循环
SETTLE_STEP = 0.1                          # settle_counter 每轮之间的间隔(秒)


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
    connect_timeout: float = 300.0
    tick_timeout: float = 30.0
    disconnect_timeout: float = 120.0
    position_wait: float = 8.0           # 查询持仓的等待窗口(CTP 持仓回报是异步的)
    cancel_wait: float = 8.0             # 等撤单确认的时间
    attrib_wait: float = 5.0              # 等风控拦截日志到位的时间(日志是异步投递的)
    probe_seq: int = 0                   # 演示委托的价格序号:每笔演示委托用不同价格,避免重复报单规则的累计计数跨步骤污染
    probe_fallback_base: float | None = None   # 演示委托的**兜底基准价**(拿不到跌停价时用):冻结一次,全轮共用(不冻结会随最新价漂移 ⇒ 两笔同价 ⇒ 归因张冠李戴)
    flat: bool | None = None             # 账户是否已确认空仓(None = 还没查)
    flat_reason: str = ""                # 空仓闸门失败的具体原因(有持仓 / 连接断开)
    # 本次运行开始的**墙钟时刻**:只用来把 vn.py 的日志行归到「本次运行」里。
    # 它**必须留在本机本地时间**:vn.py 的日志时间戳来自 loguru 的本地时间,
    # 两边基准必须一致 —— 改成北京时间会把本次运行的 P6.1 日志证据**全部过滤掉**。
    # (同一 dataclass 里的 `main()` 的 `started_at` 是北京时间,那是给证据目录用的,两者语义不同。)
    started_wall: datetime = field(default_factory=datetime.now)
    alerts: list[dict] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)      # 抛了异常的步骤:证据不完整,main 据此返回崩溃码
    trades: list = field(default_factory=list)
    notifier: object | None = None
    pump: Callable[[], None] = lambda: None       # 测试里用来推进定时器;真实运行里定时器自己在跑
    sleep: Callable[[float], None] = time.sleep
    contract: object | None = None
    tick: object | None = None
    in_session: bool = False
    clock: Callable[[], float] = time.monotonic   # 计时基准:稳定运行耗时 + 各类等待的超时;测试里可注入
    stability_interval: float = 0.1               # 稳定运行逐笔下单间隔(秒),避开柜台流控
    stability_wait: float = 60.0                  # 等全部委托回报/撤单终态的上限

    def rule(self, name: str):
        return self.risk_engine.rules.get(name)

    def now(self) -> str:
        return beijing_now().strftime("%Y-%m-%d %H:%M:%S")

    def clean(self, text: str) -> str:
        return redact(str(text), list(self.secrets))

    def wait_until(self, predicate: Callable[[], bool], timeout: float, step: float = 0.2) -> bool:
        # 超时用**可注入的时钟**(`self.clock`)算,不是模块级 `time.monotonic`:
        # 测试里注入假时钟后不必真等满 timeout(整个测试套件因此快了 60% 以上),
        # 生产里 `clock` 就是 `time.monotonic`,超时语义完全不变。
        deadline = self.clock() + timeout
        while True:
            self.pump()
            if predicate():
                return True
            if self.clock() >= deadline:
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
    每笔不同价格,是为了不让 vnpy_riskmanager 的重复报单规则把不同步骤的委托累计成"重复"。

    兜底基准价**冻结**:拿不到跌停价时用「最新价的一半」兜底,而最新价**随行情漂移**
    ⇒ 同一轮里前后两笔可能算出**同一个价格** ⇒ 归因的「按价格认领日志」就张冠李戴
    (2026-11-16 codex 第十二轮复核 M-1:实测最新价 2000 → 1998 时两笔算出同一个价)。
    所以兜底基准**第一次算出来就冻结**,全轮共用 ⇒ 同一轮里两笔的价格必然差一个 tick。
    (跌停价那条路本身不漂移 —— 那是柜台给的静态价格。)
    """
    tickp = ctx.contract.pricetick or 1.0
    base = limit_price(ctx)
    if not base:
        if ctx.probe_fallback_base is None:
            ctx.probe_fallback_base = round_to(
                float(getattr(ctx.tick, "last_price", 0) or 1000.0) * 0.5, tickp)
        base = ctx.probe_fallback_base
    n = ctx.probe_seq % 20
    ctx.probe_seq += 1
    return base + n * tickp


def log_belongs_to(h: str, price: float) -> bool:
    """这条风控日志是不是**这一笔**的?

    风控日志原文带 `OrderRequest` 的 repr —— `vnpy_riskmanager` 的
    `DailyLimitRule.check_allowed` 写的是 `write_log(f"...达到上限{limit}：{req}")`,
    `{req}` 的 dataclass repr 里含 `price=<这一笔的价格>`;而演示单**每笔价格不同**
    (`probe_price` 逐笔上浮一个最小变动价位)⇒ 可以用价格把日志**认回具体那一笔**,
    不再依赖「日志什么时候到」。

    这是「按**归属**匹配」而不是「按**位置**猜」:位置猜有一个残余面 ——
    控制那笔自己的日志迟到落在第二笔的窗口里(2026-11-16 codex 第十轮复核 M-1),
    按位置就会把它的日志当成第二笔的归因。

    认不出的日志(规则没把 req 写进日志)返回 False —— 那种日志只能退回按位置找。

    匹配**必须带边界**:`repr(2091.0)`="2091.0" 是 `repr(2091.05)`="2091.05" 的**前缀**,
    裸子串匹配会把「第二笔(2091.05)」的日志误认成「对照那笔(2091.0)」的
    (2026-11-16 codex 复核 L-2)。因此要求价格后面不能再跟数字/小数点。
    c2601.DCE 的 pricetick=1.0 撞不上,但国债/能源那种 0.005 级别的最小变动价位会撞。
    """
    return bool(re.search(rf"price={re.escape(repr(price))}(?![\w.])", h))


def replace_attribution_line(lines: list[str], idx: int, old_text: str, new_text: str,
                             label: str = "") -> None:
    """把归因行**就地替换**成补记后的那句话,下标失效或内容对不上时**显式炸出来**。

    为什么不能直接 `lines[idx] = text`:下标一旦失效,`lines[-1] = ...` 会**静默覆盖末行**
    并正常返回 —— 注释里承诺的「宁可这轮取证失败,也不要写出一份归因错的报告」就落不了地
    (2026-11-16 codex 第十轮复核 L-2:注释与实际行为不符)。这里把「失效」变成一次显式的失败。

    还要校**内容**:下标有效不代表那一行还是「我们以为的那一行」—— 若将来有人在
    这一段改成**头插**(`lines.insert(0, …)`)或删行,下标会整体错位,替换就会写到**别的行**上
    (静默产出一份归因错的报告)。所以这里同时校验那一行的**原文**:
    对不上 ⇒ 同样是显式失败。

    单独抽成函数,是为了让这两个防御分支**可以被用例直接验证**(不能只靠注释承诺)。

    失败消息带上**指标名**与那一行的开头:现场只看到「下标 12」不易定位
    (2026-11-16 codex 第十二轮复核 nit-3)。带出去的是我们自己写的归因行,
    且日志引用已经过 `ctx.clean` 脱敏 ⇒ 不含账号/口令。
    """
    who = f"[{label}]" if label else ""
    if idx < 0 or idx >= len(lines):
        raise RuntimeError(
            f"归因行下标失效({idx}){who},内部状态异常:宁可这轮取证失败,也不要写出一份归因错的报告")
    if lines[idx] != old_text:
        raise RuntimeError(
            f"归因行内容对不上(下标 {idx}){who},那一行是:{lines[idx][:60]!r};"
            "内部状态异常:宁可这轮取证失败,也不要写出一份归因错的报告")
    lines[idx] = new_text


def ensure_flat(ctx: Ctx) -> bool:
    """所有会发单的步骤之前的统一闸门:账户必须空仓。有持仓(或连接断开查不准)就不发任何演示委托——
    "无仓位可平""暂停期间平仓"这类平仓单在有持仓时会真的平掉仓位。

    失败原因分两种,分开记,否则事后只看证据分不清是"账户真有持仓"还是"查不到持仓":
    - 有持仓:列出来是哪几个合约、多少手,人工去平;
    - 连接断开/查不到:柜台连接在查询期间断了,或持仓查询没有回报,这时平仓单更不能发。
    """
    held, connected = check_positions(ctx.main_engine, wait=ctx.position_wait, early_exit=False,
                                      collector=ctx.collector)
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


def cancel_and_note(ctx: Ctx, vt_orderids: list[str], lines: list[str]) -> list[str]:
    """撤单,并把**撤不掉**的委托写进证据行。

    撤不掉 = 撤单后仍在活动,或本地根本查不到(状态未知就当还挂着)。
    这些委托必须进步骤证据,不能只在整轮收尾时兜底
    (2026-11-16 codex 复核 L2:原先 8 个调用点里只有 1 个用了返回值,
    T3/T5/P1.2、T4、P4.1、P5.1、重复报单撤不掉的委托都不进证据)。
    """
    leftover = cancel_active(ctx, vt_orderids)
    if leftover:
        lines.append(f"⚠ 清场后仍在活动或本地查不到的委托:{', '.join(leftover)}(可能还挂在柜台上)")
    return leftover


def cancel_active(ctx: Ctx, vt_orderids: list[str]) -> list[str]:
    """撤掉还挂着的委托,并**等到终态**;返回**仍在活动(或本地查不到)**的委托号。

    等终态有两个用处:
    - 撤单回报到达后,风控的「汇总撤单笔数」才 +1。不等的话,下一项指标的计数**基数**
      可能取到旧值(还有撤单回报在路上)→ 阈值对照拿旧基数去比,结论不可靠
      (2026-11-16 codex 复核提出:基数取旧会让「阈值内」那一笔被直接拦下)。
    - 撤单请求发出 ≠ 撤成功:还挂着的单会变成"晚到的成交"。

    返回仍在活动的委托号:撤不掉的东西必须**进证据**,不能只在日志里说一句
    (2026-11-16 codex 复核 L3:原先这个函数什么都不返回,步骤证据里一个字都没有,
    只能靠整轮结束时的 final_safety 兜底)。
    本地**查不到**的委托号也算「撤不掉」:查不到不等于已平,状态未知就当还挂着。
    """
    for oid in vt_orderids:
        order = ctx.collector.order_of(oid)
        if order is not None and order.is_active():
            ctx.main_engine.cancel_order(order.create_cancel_request(), ctx.gateway_name)

    def all_terminal() -> bool:
        for oid in vt_orderids:
            order = ctx.collector.order_of(oid)
            if order is not None and order.is_active():
                return False
        return True

    if vt_orderids:
        ctx.wait_until(all_terminal, ctx.cancel_wait)
    leftover = []
    for oid in vt_orderids:
        order = ctx.collector.order_of(oid)
        if order is None:
            # 本地**查不到**这张单:要么它压根没被回报(不该发生——我们刚发出去),
            # 要么本地缓存已经把它回收了。两种情况我们都无法确认它已经不在柜台上,
            # 按「判不出来 = 可能还挂着」处理,一起进证据(宁可多报,不可漏报)。
            leftover.append(oid)
        elif order.is_active():
            leftover.append(oid)
    return leftover


# ======================= 计数器等待 =======================
#
# 报单/撤单计数是在**委托回报事件**里更新的(事件线程),而 `send()` 立刻返回。
# 不等计数涨上去就发下一笔,下一笔会因为计数还停在旧值而被放行 ——
# 2026-11-16 夜盘实测就是这样把「超过阈值的委托」放过去的。
# 这三个函数单独抽出来,是为了能离线测「先等计数、再发下一笔」这个次序。


def counter_value(rule, attr: str) -> int:
    """读风控规则的计数器:属性缺失或为 None 都按 0 算。"""
    return int(getattr(rule, attr, 0) or 0)


def settle_rounds(timeout: float) -> int:
    """等待窗口对应的轮数上限:每轮最多推进 SETTLE_STEP 秒,所以轮数要跟着 timeout 走。

    上限写死(200 轮 = 20 秒)而 timeout 调大时,`settle_counter` 会在 20 秒处**静默**提前返回,
    返回值与「到点放弃」无法区分(2026-11-16 codex 复核 L1)。
    """
    return max(SETTLE_MIN_ROUNDS, int(timeout / SETTLE_STEP) + 2)


def settle_counter(ctx: Ctx, rule, attr: str, timeout: float = 5.0) -> int:
    """读计数**之前**先等它稳定下来(连续 1 秒不变才算数)。

    刚撤掉的委托可能还在路上,基数取早了,「阈值内」的那一笔会被直接拦下,对照就做不成。
    「稳定 1 秒」按**时钟的差**算,不是按循环次数折算 —— 折算出来的是「转了 10 圈」,
    机器慢一点就变成 3 秒,口径会飘。

    时钟取 `ctx.clock`(可注入):生产里就是 `time.monotonic`,语义不变;
    测试里注入假时钟,不必真等 1 秒(2026-11-16 codex 复核:整个测试套件因此慢了 63%)。
    每轮先 `ctx.pump()`:计数是在**委托回报事件**里更新的,不推事件就永远等不到它落下来。
    轮数另设上限:注入的时钟不前进时也不至于死循环。
    """
    clock = ctx.clock
    deadline = clock() + timeout
    last, stable_since = -1, clock()
    # 轮数上限跟着 timeout 走(见 settle_rounds):写死的上限会在等待窗口还没走完时
    # **静默**提前返回,而返回值与「到点放弃」无法区分(2026-11-16 codex 复核 L1)。
    rounds = settle_rounds(timeout)
    for _ in range(rounds):
        ctx.pump()
        cur = counter_value(rule, attr)
        now = clock()
        if cur != last:
            last, stable_since = cur, now
        elif now - stable_since >= 1.0:
            return cur
        if now >= deadline:
            break                       # 到点就返回,不能把取证流程挂死
        ctx.sleep(SETTLE_STEP)
    return counter_value(rule, attr)


def wait_counter_at_least(ctx: Ctx, rule, attr: str, target: int, timeout: float) -> bool:
    """等计数器涨到 `target`;返回 False 表示超时(计数没推进,后面的对照不可靠)。"""
    return ctx.wait_until(lambda: counter_value(rule, attr) >= target, timeout)


# ======================= 步骤 =======================

def guard_state_file() -> Path:
    """隔离档案里的账户风控状态文件路径。

    写成函数而不是常量:`BT_ROOT` 可以在测试里被替换,常量会在导入时就固定下来。
    """
    return BT_ROOT / "state" / "guard.json"


def locked_state_note(state_file: Path) -> str:
    """取证档案的账户风控状态:被锁时**不要**白跑一次登录。

    状态文件是隔离的测试档案(`.vntrader_brokertest/state/guard.json`)。
    锁定状态下开仓委托会被本地风控**全部**拦下:200 笔稳定运行一笔都发不出去,
    这一轮取证白费一次券商登录额度(国信会查登录次数),证据还只剩一堆"被拦下"。
    返回空串 = 没锁,可以跑。

    判据**直接复用运行时那一份**(`risk.account_guard.AccountGuard`):
    自己另写一套解析,必然比运行时松 —— 2026-11-16 codex 复核复现过:状态文件只写
    `{"hard_locked": false}` 时,本函数判「没锁」放行,而运行时 `AccountGuard._load`
    因缺必填字段直接判**硬锁定** ⇒ 白跑一次国信登录额度,委托全被拒,证据只剩
    一堆"被拦下"。两份判据必须同源。

    文件**不存在**是头一次跑的正常情况(还没有状态),那种才放行;
    文件损坏/缺字段 → 运行时按硬锁定处理(保守),这里照它说,不另判。
    """
    if not state_file.exists():
        return ""                          # 从来没锁过:没有状态文件,正常
    try:
        guard = AccountGuard(AccountLimits(), state_file)
    except Exception as exc:  # noqa: BLE001  状态文件不是 JSON 对象等异常形态:同样按「判不出来」拒绝
        return f"账户风控状态读不出来({exc!r}),无法确认是否被锁,先修好或删掉该文件再跑"
    if guard.hard_locked:
        return (f"账户处于硬锁定状态({guard.hard_reason or '原因未记录'}),"
                "取证委托会被本地风控全部拦下,请先人工解锁再跑")
    day = trading_day(beijing_now())
    if guard.is_daily_locked(day):
        return (f"账户处于当日锁定状态(交易日 {day.isoformat()}),开仓委托会被本地风控全部拦下,"
                "取证会白跑一次登录,请先解除锁定或等下一个交易日再跑")
    return ""


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
    held, connected = check_positions(ctx.main_engine, wait=ctx.position_wait, early_exit=False,
                                      collector=ctx.collector)
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
        cancel_and_note(ctx, [open_id] if open_id else [], open_lines)
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
    after, after_connected = check_positions(ctx.main_engine, wait=ctx.position_wait, early_exit=False,
                                          collector=ctx.collector)
    flat = not after and after_connected
    if after:
        close_lines.append(f"⚠ 平仓后仍有持仓 {after},请立即人工平仓")
    elif not after_connected:
        # 通道断了 → 这次查询是空的,不能说「平仓后账户空仓」
        close_lines.append("⚠ 平仓后交易通道不在线,持仓查询不可信,无法确认账户已空,请人工核对")
    else:
        close_lines.append("平仓后账户空仓")
    trade_lines = [ctx.clean(f"成交回报 {t.vt_symbol} {direction_text(t.direction)}{t.offset.value} {t.volume}@{t.price} 成交号 {t.tradeid}")
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
            cancel_and_note(ctx, sent, lines)
    ctx.wait_until(lambda: any(a["title"] == "风控阈值触发" and "重复报单" in a["message"] for a in ctx.alerts_since(alert_mark)), 4.0)
    hits = ctx.logs_since(mark, "重复报单笔数")
    alerts = [a for a in ctx.alerts_since(alert_mark) if "重复报单" in a["message"]]
    lines += [ctx.clean(f"风控日志:{h[:200]}") for h in hits[:2]]
    lines += [ctx.clean(f"预警:[{a['level']}] {a['title']} {a['message'][:160]}") for a in alerts[:1]]
    forward_alerts(ctx, alerts)
    ok = rule is not None and blocked and bool(alerts)
    status = PASS if ok else FAIL
    # 失败原因要分开说:「压根没被拦」与「被拦了但没预警」是两种不同的结论,
    # 混成一句「没有触发重复报单拦截或预警」等于没给结论(2026-11-16 codex 复核 M3)。
    # `reason` 不带「重复报单」前缀:回给 P3.1 时由那边统一加前缀(和其它两个指标
    # 的「报单:…」「撤单:…」格式一致),免得拼出「重复报单:重复报单没有被拦截」这种
    # 重复措辞(2026-11-16 codex 复核 L8)。
    # `summary` 是 P2.3 自己那条证据的措辞(完整短语,单独看也读得懂);
    # `reason` 是回给 P3.1 的**短原因**(不带「重复报单」前缀,由 P3.1 统一补,
    # 免得拼出「重复报单:重复报单没有被拦截」这种重复措辞 —— 2026-11-16 codex 复核 L8)。
    if rule is None:
        reason, summary = "找不到规则", "找不到重复报单规则"
    elif not blocked:
        reason, summary = "没有被拦截", "重复报单没有被拦截"
    elif not alerts:
        reason, summary = "被拦截但没有收到预警", "重复报单被拦截但没有收到预警"
    else:
        reason, summary = "", ""
    book.add("P2.3", status, summary, [Shot("", lines)], started)
    # 把原因一并回给 P3.1:三个阈值指标里,重复报单原来只回一个布尔,
    # 失败时 P3.1 的摘要会退化成「有阈值未能触发拦截与预警」—— 一个原因都没有。
    return {"ok": ok, "lines": lines, "reason": reason}


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
    stall_reasons: list[str] = []          # 每个指标:「判不出来」的具体原因(空 = 能判)
    hard_flags: list[str] = []             # 每个指标:硬失败的说明(空 = 没有硬失败)
    fill_notes: list[str] = []             # 每个指标:演示单的成交情况(空 = 没成交)
    block_attribs: list[str] = []          # 每个指标:超过阈值那笔的**拦截归因**(ours/other/none/空)
    block_details: list[str] = []          # 每个指标:归因的人话 + 原文引用
    may_hang_flags: list[bool] = []       # 每个指标:演示单**可能还挂在柜台上**(结构化标记)

    def count_of(attr: str) -> int:
        return counter_value(daily, attr)

    def metric(label: str, key_word: str, set_limit: Callable[[int], None], counter_attr: str,
               restore: Callable[[], None], need_cancel: bool) -> None:
        mark, alert_mark = ctx.log_mark(), len(ctx.alerts)
        lines, made = [], []
        base = settle_counter(ctx, daily, counter_attr)
        limit = base + 1
        set_limit(limit)
        lines.append(f"设置{label}阈值 = {limit}(当前已有 {base})")
        advanced = False
        cancel_confirmed: bool | None = None     # 撤单指标:我们**自己**那张单撤成功了吗
        cancel_filled = False                      # 撤单指标:演示单其实**成交了**(账户可能多出持仓)
        cancel_rejected = False                   # 撤单指标:演示单是**废单**(被柜台拒单 ⇒ 没有挂单、账户无持仓变化)
        cancel_may_hang = False                   # 撤单指标:演示单**可能还挂在柜台上**(结构化标记,不用整串关键字判断)
        fill_note = ""                             # 成交/部分成交的人话(空 = 没成交)
        stale_base = False                          # 计数基数取旧 → 对照做不成
        blocked_detail = ""                        # 「对照用的那一笔」没发出去时的归因说明
        block_attrib = ""                          # 超过阈值那笔的拦截归因:ours=本项阈值/other=别的规则/none=没有日志
        block_detail = ""                          # 归因的人话(带日志原文引用)
        first_no_log = False                       # 「对照用的那一笔」归因时**没看到**拦截日志
        first_count_txt = ""                       # 那时候读到的计数措辞(补记时要用)
        first_log_idx = -1                         # 那条归因行在 lines 里的下标(迟到日志补记时**替换**它)
        first_log_text = ""                        # 那条归因行的**原文**(替换时要核对内容,防止下标错位)
        a = b = ""
        alerts: list[dict] = []
        try:
            # 设完阈值**重新读一次**计数:此刻计数已经 ≥ 阈值 = 刚才那个基数取旧了
            # (有别的委托回报在途中)→ 这一轮对照做不成 → 判不出来(SKIP),不是「阈值失灵」。
            now_count = count_of(counter_attr)
            if now_count >= limit:
                stale_base = True
                lines.append(f"⚠ 设置阈值时计数已经是 {now_count}(≥ 阈值 {limit}),基数取旧了,本次对照不可靠")
            # 这两笔的价格各记一份:风控日志里带 `OrderRequest` 的 repr(含 `price=…`),
            # 而演示单每笔价格不同 ⇒ 可以用价格把日志**认回具体那一笔**(见 `log_belongs_to`),
            # 不必再靠「日志什么时候到」猜是哪一笔的。
            price_a = probe_price(ctx)
            a = send(ctx, make_req(ctx, price=price_a))
            if a:
                made.append(a)
            lines.append(f"阈值内委托:{'通过' if a else '未发出'}")
            if not a:
                # 「对照用的那一笔」没发出去 —— 必须查清**是谁拦的**,更不能一口咬定
                # 「阈值设得太紧」:委托规模超限、通道断开、账户被锁都会落到同一条分支,
                # 那样写等于对风控系统栽赃(2026-11-16 codex 复核 H1 复现:
                # 风控日志写的是「委托价值20900.0超过上限」,报告却断言「阈值太紧」)。
                # 归因只看**证据**,优先级从硬到软:
                #   ①风控日志点名了**我们这一项**的指标(「汇总委托笔数N达到上限M」)
                #     ⇒ 就是本项阈值拦的 ⇒ 计数基数取旧 ⇒ 判不出来;
                #   ②有拦截日志但没点名我们这项 ⇒ 是**别的规则**拦的 ⇒ 实打实的失败,
                #     但结论只能说「委托没发出去」并引用日志,不能说「阈值太紧」;
                #   ③压根没有拦截日志 ⇒ 通道断开/账户被锁/别的规则 ⇒ 失败,中性措辞;
                #   ④只有在**没有日志**时,才退回用计数推断(计数已到阈值 ⇒ 可能是本项拦的)。
                ctx.wait_until(lambda: bool(ctx.logs_since(mark, "委托被拦截")), ctx.attrib_wait)
                block_logs = ctx.logs_since(mark, "委托被拦截")
                ours = [h for h in block_logs if key_word in h]
                count_now = count_of(counter_attr)
                # 「读取计数」是**读到**那一刻的值,不是「发单那一刻」的值 —— 措辞得对得上,
                # 而且不等式只在真的成立时才写:否则会打出「计数 1 < 阈值 1」这种假话
                # (2026-11-16 codex 复核 M2)。这种句子会原样进国信的取证报告。
                count_txt = (f"读取计数 {count_now} < 本项阈值 {limit}" if count_now < limit
                             else f"读取计数 {count_now}(本项阈值 {limit})")
                if ours:
                    stale_base = True
                    blocked_detail = (f"是本项阈值拦的({key_word}达到上限{limit},风控日志:"
                                      f"{ctx.clean(ours[-1][:160])})⇒ 计数基数取旧,这轮对照做不成")
                elif block_logs:
                    blocked_detail = (f"委托没发出去,{count_txt},不是本项阈值拦的"
                                      f"(风控日志:{ctx.clean(block_logs[-1][:160])})")
                elif count_now >= limit:
                    stale_base = True
                    # 这一支的前提恰恰是**没有拦截日志**,而且计数是发单 2 秒后才读的
                    # ⇒ 只能说「可能是本项阈值拦的」,不能断言「是本项阈值拦的」
                    # (2026-11-16 codex 复核 L6)。
                    blocked_detail = (f"读取计数已到 {count_now} ≥ 本项阈值 {limit}"
                                      "(归因窗口内没有拦截日志,可能是本项阈值拦的)"
                                      " ⇒ 计数基数取旧,这轮对照做不成")
                else:
                    first_no_log = True
                    first_count_txt = count_txt
                    blocked_detail = (f"委托没发出去,{count_txt},不是本项阈值拦的"
                                      "(没有风控拦截日志:可能是通道断开/账户被锁/委托被其它规则拦下)")
                first_log_idx = len(lines)          # 这一行待会儿可能被**替换**(见下面的迟到日志补记)
                first_log_text = f"⚠ {blocked_detail}"
                lines.append(first_log_text)
            # 计数是**异步**更新的:send() 立刻返回,而规则的计数要等委托回报事件到达才 +1。
            # 不等计数涨上去就发第二笔,第二笔会因为计数还停在旧值而被放行 ——
            # 2026-11-16 夜盘实测就是这样把「超过阈值的委托」放过去的。
            #
            # 要等的是**本指标管的那个计数**:撤单指标管的是「汇总撤单笔数」,
            # 它只在**撤单回报**到达后才 +1 —— 发委托时它一动不动,这时候先等它
            # 是白等 5 秒,还会把「计数没推进」这个结论安错地方(撤单指标就永远 SKIP 了)。
            # 所以撤单指标的推进判断放到撤单之后(见下面)。
            if not need_cancel:
                if a:
                    advanced = wait_counter_at_least(ctx, daily, counter_attr, base + 1, 5.0)
                    if not advanced:
                        lines.append(f"⚠ {label}计数在 5 秒内没有推进(仍为 {count_of(counter_attr)}),下面的对照不可靠")
                    elif count_of(counter_attr) > base + 1:
                        # 一次跳了不止 +1 = 有**别的**委托回报混进来了 → 基数不可信 → 判不出来。
                        stale_base = True
                        lines.append(f"⚠ {label}计数一次跳到 {count_of(counter_attr)}(不止 +1),"
                                     "有别的委托回报混进来,本次对照不可靠")
                elif count_of(counter_attr) >= base + 1:
                    # 委托没发出去,但计数已经到阈值(可能就是刚才被阈值拦下的那一次):
                    # 「超阈值那笔真的发出去了」仍然是硬失败(见下面 hard_reason),
                    # 不能因为 a 为空就把这条证据丢掉。
                    # 这里**不能**动 advanced:a 为空时下面的 `elif not a:` 必然先命中,
                    # advanced 只在 a 非空时被读 —— 原先在这里写 advanced=True 是死代码,
                    # 留着会让人以为它改变过结论(2026-11-16 codex 复核 L1)。
                    pass
            if need_cancel and a:
                order_a = ctx.collector.order_of(a)
                if order_a is not None:
                    ctx.main_engine.cancel_order(order_a.create_cancel_request(), ctx.gateway_name)
                cancelled = ctx.wait_until(lambda: ctx.collector.order_of(a) is not None and not ctx.collector.order_of(a).is_active(),
                                           ctx.cancel_wait)
                final = ctx.collector.order_of(a)
                # 撤单**确认**才算数:撤单请求发出 ≠ 撤成功。没确认就说明这一轮的触发
                # 不是我们想要的那次 → 判不出来(SKIP),不能算「阈值生效」。
                cancel_confirmed = bool(cancelled and final is not None and final.status == Status.CANCELLED)
                if cancel_confirmed:
                    lines.append("已撤销该委托,累计撤单数达到阈值")
                else:
                    lines.append(f"⚠ 撤单未确认(终态 {final.status.value if final else '无回报'}),撤单计数没有达到阈值")
                    if final is not None and final.status == Status.ALLTRADED:
                        # 终态是「成交」而不是「已撤销」:撤单没赶上,单子真成交了。
                        # 这不是「撤单成功」,账户可能因此多出一笔持仓,必须点出来
                        # (2026-11-16 codex 复核 L4:原先只写「已撤销」,成交会被当成撤销)。
                        cancel_filled = True
                        fill_note = "演示单已成交"
                        lines.append("⚠ 演示单**成交了**(不是撤单成功),账户可能多出持仓,请人工核对")
                    elif final is not None and final.status == Status.PARTTRADED:
                        # 部分成交:账户已经多出一部分持仓,**同时**余量可能还挂在柜台上 ——
                        # 两个后果都要说。只说「还挂着」会把操作员引去柜台撤单,而忘了核持仓
                        # (2026-11-16 codex 复核 M6)。
                        cancel_filled = True
                        fill_note = "演示单部分成交(余量可能仍挂在柜台上)"
                        lines.append("⚠ 演示单**部分成交**,账户可能多出一部分持仓,请人工核对持仓")
                        lines.append("⚠ 演示单余量可能仍挂在柜台上,请人工去柜台核对")
                    elif final is not None and final.status == Status.REJECTED:
                        # 废单:委托被柜台拒了,**压根没上柜台**。说「可能还挂在柜台上」是事实错误
                        # (2026-11-16 codex 复核 L1)。账户没有持仓变化,但撤单确实没成功。
                        cancel_rejected = True
                        lines.append("⚠ 演示单被拒单(废单):没有挂单,账户无持仓变化")
                    # 「可能还挂在柜台上」要用**结构化标记**,不能靠「撤单没确认」笼统推:
                    # 全部成交(ALLTRADED)⇒ 单子已经成交完,柜台上**没有**它了(说还挂着是假的);
                    # 废单(REJECTED)⇒ 压根没上柜台;已撤销(CANCELLED)⇒ 也不挂着。
                    # 只有「部分成交」或「没有终态回报」才可能还挂着。
                    # (2026-11-16 codex 第十轮复核 L-1:原来用 `elif` 让成交告警把这条短路掉,
                    #  改成两条独立 `if` 之后又会误报「还挂着」—— 用这个标记两条告警才能既
                    #  互不排斥、又不互相矛盾。)
                    if final is None or final.status not in (Status.ALLTRADED, Status.REJECTED, Status.CANCELLED):
                        cancel_may_hang = True
                # 撤单计数同样要等回报到达才 +1(撤单回报 → 规则 on_order → 计数 +1)
                advanced = wait_counter_at_least(ctx, daily, counter_attr, base + 1, ctx.cancel_wait)
                if not advanced:
                    lines.append(f"⚠ {label}计数在 {ctx.cancel_wait} 秒内没有推进"
                                 f"(仍为 {count_of(counter_attr)}),下面的对照不可靠")
            # 发第二笔之前再取一次计数:它就是规则**决定放行还是拦截**时看到的那份数字。
            # 「计数已经 ≥ 阈值,而超阈值那笔还是发出去了」= 阈值确实没生效(硬失败)。
            count_before_b = count_of(counter_attr)
            mark_b = ctx.log_mark()          # 归因只看**这一笔之后**的日志(别把上一笔的算进来)
            price_b = probe_price(ctx)
            # 两笔**同价** ⇒ 「按价格认领日志」不再成立(认不出哪条日志属于哪一笔):
            # 这时**不用**归属匹配,退回按位置归因,并在证据行里写明为什么退回
            # (2026-11-16 codex 第十二轮复核 M-1:兜底基准不冻结时最新价漂移会让两笔同价)。
            # 正常情况下两笔必然差一个 tick(`probe_price` 的基准已冻结),这一支是兜底。
            ownership_ok = price_a != price_b
            if not ownership_ok:
                lines.append(f"⚠ 两笔演示单价格相同({price_a}),按价格认领日志不可用,"
                             "本次归因退回按位置(可能认错人,但不会漏掉我们自己的日志)")
            b = send(ctx, make_req(ctx, price=price_b))
            if b:
                made.append(b)
            lines.append(f"超过阈值的委托:{'被拦截' if not b else '未被拦截(意外)'}")
            # 归因:超过阈值的那一笔到底是**本项阈值**拦的,还是**别的规则**先拦的?
            # 风控规则按顺序判定(ActiveOrderRule 在 DailyLimitRule 之前),别人先拦 ⇒
            # 本项阈值压根没参与,这次对照**做不成** —— 那样就不能说「阈值拦住了委托」
            # (2026-11-16 codex 复核 M2:旧文案在无证据的情况下断言「阈值拦住了委托」)。
            if not b:
                # 风控日志是**异步**投递的:`vnpy_riskmanager` 的 `write_log` 走
                # `event_engine.put(Event(EVENT_LOG, ...))`,采集器在事件线程里收到 ⇒
                # `send()` 返回时日志通常**还没到**。不等就归因 ⇒ 有证据的
                # 「本项阈值拦的」会被降级成「没有风控拦截日志」(判不出来),
                # 而下面又把日志原文写进证据行 ⇒ 摘要与证据**自相矛盾**
                # (2026-11-16 codex 复核 M-1)。测试用同步事件引擎 + 手工 append 日志,
                # 抓不到这个竞态,所以这里必须显式等。
                ctx.wait_until(lambda: bool(ctx.logs_since(mark_b, "委托被拦截")), ctx.attrib_wait)
            # 归因与证据行用**同一次读**(一份快照):分开读两次,日志可能正好在两次之间
            # 到达 ⇒ 摘要说「没有风控拦截日志」、证据行里却那条日志都在 ⇒ 又自相矛盾
            # (2026-11-16 codex 第八轮复核 L2/L3)。
            # 快照起点是 mark(本指标开始),`mark_b - mark` 就是「第二笔之后」的下标偏移。
            snapshot = ctx.logs_since(mark)
            # 第二笔的归因窗口里要**排除控制那笔自己的日志**:它可能迟到落在窗口里
            # (位置切分的残余面 —— 2026-11-16 codex 第十轮复核 M-1),
            # 不排除就会把「控制那笔的日志」当成「第二笔的归因」⇒ 张冠李戴。
            # 认领方式是**按归属**(价格),不是按位置(见 `log_belongs_to`)。
            block_logs_b = [h for h in snapshot[mark_b - mark:]
                            if "委托被拦截" in h
                            and not (ownership_ok and log_belongs_to(h, price_a))]
            ours_b = [h for h in block_logs_b if key_word in h]
            # 「对照用的那一笔」归因时没看到日志、但快照里有了 ⇒ 补记它。
            #
            # **判定只认证据,不认日志什么时候到**。「对照那笔是不是被**本项阈值**拦的」
            # 只由一件事决定:对照发单那一刻计数到没到阈值 ——
            #   · `DailyLimitRule.check_allowed` 只在「计数 ≥ 阈值」时才拦单、才写日志,
            #     计数只增不减 ⇒ 对照发单时的计数 ≤ 归因时读到的计数;
            #   · 所以读到的计数 < 阈值 ⇒ 对照发单时也 < 阈值 ⇒ **本项阈值压根没参与判定**,
            #     对照那笔不是被本项阈值拦的(它是被别的东西拦的 ⇒ 实打实的失败)。
            # 换句话说:这条日志点名我们这一项时,它**不可能**是对照那笔的 ——
            # 它属于**后来那笔**委托(计数后来涨到了阈值)。
            # 把它当成对照那笔的,等于凭空给一次真实失败盖上「判不出来」的章
            # (2026-11-16 codex 第九轮复核:该分支对对照那笔不可达)。
            #
            # 所以这里**不按「日志什么时候到」改判定**:晚到只影响「什么时候看到」,
            # 判定仍然由证据(计数 + 日志点名的是哪一项)决定:
            #   ①点名**别的规则** ⇒ 对照那笔是被别的东西拦的 ⇒ 失败(FAIL);
            #   ②点名**我们这一项** ⇒ 对照那笔被本项阈值拦下 ⇒ 计数基数取旧 ⇒ 判不出来(SKIP)。
            # (②在生产里不可达,按上面的计数判据;保留作防御分支。)
            #
            # 扫描范围:**先按归属找**(用价格认出控制那笔自己的日志,不管它什么时候到),
            # 认不出(规则没把 req 写进日志)才退回按**位置**找 —— 「第二笔之前」那段
            # 窗口(`snapshot[:mark_b - mark]`)里的拦截日志只可能属于控制那笔
            # (这个指标在两笔之间只发过它这一笔),「后来那笔」的日志在窗口之外
            # (2026-11-16 codex 第九轮复核 L3)。
            # 按位置找有残余面:控制那笔的日志迟到落在第二笔窗口里(第十轮复核 M-1),
            # 所以第一优先是按**归属**找 —— `log_belongs_to` 用 `OrderRequest` repr 里的
            # 价格认领(演示单每笔价格不同)。
            #
            # 判定与措辞**照抄上面「日志准时到达」时的那一支**(同一份证据 ⇒ 同一个判定 ⇒
            # 同一个措辞):日志晚到只影响「什么时候看到」。
            #
            # 补记时**替换**原来那一行(而不是再追加一行):原来那句写着「没有风控拦截日志」,
            # 补记后它就成了假话,证据块里两句互斥 ⇒ 报告自相矛盾
            # (2026-11-16 codex 第九轮复核 M1:这正是本机制要关掉的那一类)。
            if first_no_log:
                late_logs = [h for h in snapshot
                             if "委托被拦截" in h
                             and ownership_ok and log_belongs_to(h, price_a)]
                if not late_logs:
                    late_logs = [h for h in snapshot[:max(mark_b - mark, 0)] if "委托被拦截" in h]
                if late_logs:
                    late = late_logs[-1]
                    if key_word in late:
                        stale_base = True
                        blocked_detail = (f"是本项阈值拦的({key_word}达到上限{limit},风控日志:"
                                          f"{ctx.clean(late[:160])})⇒ 计数基数取旧,这轮对照做不成")
                    else:
                        blocked_detail = (f"委托没发出去,{first_count_txt},不是本项阈值拦的"
                                          f"(风控日志:{ctx.clean(late[:160])})")
                    # `first_log_idx` 是刚追加的那条归因行的下标,**必然在范围内**:
                    # 它与 `first_no_log` 在同一个分支里置位,之后对 `lines` 只有 `append`
                    # (不改已有下标)⇒ 下标不会失效。这里**直接替换**,但把「失效」变成
                    # 一次显式的失败(`replace_attribution_line` 负责)——
                    # 宁可这轮取证失败,也不要写出一份归因错的报告
                    # (2026-11-16 codex 第十轮复核 L2)。
                    # 替换时**同时核对那一行的原文**:下标有效不等于那行还是我们以为的那行
                    # (将来若有人改成头插/删行,下标会整体错位 ⇒ 写到别的行上)。
                    replace_attribution_line(lines, first_log_idx, first_log_text,
                                             f"⚠ {blocked_detail}(日志在等待窗口之后才到)",
                                             label=label)
            if b:
                block_attrib, block_detail = "", ""
            elif ours_b:
                block_attrib = "ours"
                block_detail = f"本项阈值拦的(风控日志:{ctx.clean(ours_b[-1][:160])})"
            elif block_logs_b:
                block_attrib = "other"
                block_detail = f"被其它规则拦下,不是本项阈值(风控日志:{ctx.clean(block_logs_b[-1][:160])})"
            else:
                block_attrib = "none"
                block_detail = "没有风控拦截日志(可能是通道断开/账户被锁)"
            if not b:
                lines.append(f"⚠ 超过阈值的委托被拦下,{block_detail}")
            ctx.wait_until(lambda: any(al["title"] == "风控阈值触发" and key_word in al["message"] for al in ctx.alerts_since(alert_mark)), 4.0)
            alerts = [al for al in ctx.alerts_since(alert_mark) if key_word in al["message"]]
            lines += [ctx.clean(f"风控日志:{h[:200]}") for h in snapshot if key_word in h][:1]
            lines += [ctx.clean(f"预警:[{al['level']}] {al['title']} {al['message'][:160]}") for al in alerts[:1]]
            forward_alerts(ctx, alerts)
        finally:
            restore()                      # 无论如何还原阈值并撤掉演示委托,不留下挂单
            cancel_and_note(ctx, made, lines)
        # 「阈值失效」是**硬结论**:计数确实到过阈值(base+1),而超阈值那笔却发出去了。
        # 它不能被「判不出来」盖掉 —— 否则报告会写成「未测试」,把一次真实的阈值失效
        # 洗成「没法测」(2026-11-16 codex 复核 H1:证据行字面写着「未被拦截(意外)」,
        # 而 P3.1/T6 判成了 SKIP)。
        hard_reason = ""
        # 「判不出来」有四种:①计数**基数取旧**(有别的回报混进来)—— 拿旧基数比出来的结论不算数;
        # ②委托发出去了但计数没推进(回报没到/没回);
        # ③撤单指标里**我们自己的撤单没确认** —— 触发不是我们想要的那一次;
        # ④「对照用的那一笔」被**本项阈值**拦下(计数已到阈值)—— 基数取旧,这轮做不成。
        # 「对照用的那一笔」压根没发出去**不一定**是失败:被本项阈值拦下说明基数取旧
        # (判不出来),被别的东西拦下才是失败 —— 但失败也只能说「委托没发出去」,
        # 不能说「阈值设得太紧」:那样写是在指控一个没有证据支持的原因
        # (2026-11-16 codex 复核 H1:委托规模超限/断连都被写成「阈值太紧」)。
        stall_reason = ""
        # 「可能还挂在柜台上」用**结构化标记** `cancel_may_hang`:它在撤单分支里按**终态**定下来
        # (部分成交 / 没有终态回报 ⇒ 可能还挂着;全部成交 / 废单 / 已撤销 ⇒ 不挂着),
        # 不再用「撤单没确认」笼统推 —— 全部成交时说「还挂着」是**假话**
        # (2026-11-16 codex 第十轮复核 L-1)。
        if bool(b) and count_before_b >= limit:
            hard_reason = "超过阈值的委托没有被本地风控拦下(阈值未生效)"
        elif not a:
            if stale_base:
                stall_reason = f"对照用的委托没能发出({blocked_detail})"
            else:
                hard_reason = f"对照用的委托没能发出({blocked_detail})"
        elif stale_base:
            stall_reason = "计数基数取旧(有别的回报混进来)"
        elif need_cancel and not cancel_confirmed:
            # 撤单没确认的**后果**分两种:演示单成交了(账户可能多出持仓,要平仓核对)
            # 与演示单可能还挂着(要去柜台撤)。摘要必须分开说,否则会把人引到错的动作上
            # (2026-11-16 codex 复核 M-1)。
            stall_reason = (f"撤单未确认({fill_note},账户可能多出持仓,请人工核对持仓)" if cancel_filled
                            else ("撤单未确认(演示单被拒单:没有挂单,账户无持仓变化)" if cancel_rejected
                                  else "撤单未确认(演示单可能仍在柜台上)"))
        elif not advanced:
            stall_reason = f"{label}计数未推进(限时内没有推进)"
        if hard_reason:
            lines.append(f"✗ {hard_reason}")
        ok = bool(a) and not b and bool(alerts) and not stale_base and (not need_cancel or cancel_confirmed)
        oks.append(ok)
        stall_reasons.append(stall_reason)
        hard_flags.append(hard_reason)
        fill_notes.append(fill_note)
        block_attribs.append(block_attrib)
        block_details.append(block_detail)
        may_hang_flags.append(cancel_may_hang)
        shots.append(Shot(label, lines))

    if daily is None:
        book.add("P3.1", FAIL, f"找不到「{DAILY_RULE}」规则", [Shot("", ["规则缺失"])], started)
        book.add("T6", FAIL, f"找不到「{DAILY_RULE}」规则", [Shot("", ["规则缺失"])], started)
        return
    orig_o, orig_c = daily.total_order_limit, daily.total_cancel_limit
    metric("报单总笔数", "汇总委托笔数", lambda n: setattr(daily, "total_order_limit", n), "total_order_count",
           lambda: setattr(daily, "total_order_limit", orig_o), need_cancel=False)
    order_ok = oks[-1]
    order_stalled = stall_reasons[-1]
    order_hard = hard_flags[-1]
    order_fill = fill_notes[-1]
    order_attrib = block_attribs[-1]
    order_block = block_details[-1]
    if ctx.in_session:
        metric("撤单总笔数", "汇总撤单笔数", lambda n: setattr(daily, "total_cancel_limit", n), "total_cancel_count",
               lambda: setattr(daily, "total_cancel_limit", orig_c), need_cancel=True)
        cancel_ok = oks[-1]
        cancel_stalled = stall_reasons[-1]
        cancel_hard = hard_flags[-1]
        cancel_fill = fill_notes[-1]
        cancel_block = block_details[-1]
        cancel_may_hang = may_hang_flags[-1]
    else:
        shots.append(Shot("撤单总笔数", ["非交易时段委托不会进入可撤状态,撤单阈值无法演示,请在交易时段补测"]))
        cancel_ok = None
        cancel_stalled = ""
        cancel_hard = ""
        cancel_fill = ""
        cancel_block = ""
        cancel_may_hang = False
    shots.append(Shot("重复报单总笔数", dup.get("lines", ["(重复报单演示未执行)"])))
    dup_ok = bool(dup.get("ok"))
    # 原因串**不带**「重复报单」前缀(step_duplicate 里已去掉),这里统一补上,
    # 与其它两个指标的「报单:…」「撤单:…」格式对齐;不补前缀会与上面同段的
    # 「报单:…」「撤单:…」并列时看不出是哪一项(2026-11-16 codex 复核 L8)。
    dup_note = f"重复报单:{dup['reason']}" if (not dup_ok and dup.get("reason")) else ""
    # 计数没推进 = 演示**不可靠**,不是「阈值没生效」:这两种情况必须分开,
    # 否则证据会把「计数异步没跟上」写成「阈值失灵」(2026-11-16 夜盘就踩过这个坑:
    # 计数在事件线程里更新,send() 立刻返回,不等就会误判)。
    # 注意只有「委托发出去了但计数没动」才是判不出来;委托压根没发出去是实打实的失败。
    #
    # 优先级:**阈值确实失效(硬失败) > 判不出来(SKIP) > 其它**。
    # 硬失败不能被 SKIP 盖掉:超阈值那笔真的发出去了,这是「阈值没生效」的确定结论,
    # 写成「未测试」就是把失败洗成没法测(2026-11-16 codex 复核 H1)。
    if order_hard or cancel_hard:
        # 两个指标的硬失败**都**要收集:只取第一个会把另一个指标的真实失败吞掉
        # (2026-11-16 codex 复核 M1)。硬失败优先,但另一个指标「判不出来」的原因
        # 也要带上 —— 它解释了这一项为什么没测出来。
        hards = [f"报单:{h}" for h in (order_hard,) if h] + [f"撤单:{h}" for h in (cancel_hard,) if h]
        stalls = [f"报单:{s}" for s in (order_stalled,) if s] + [f"撤单:{s}" for s in (cancel_stalled,) if s]
        status, summary = FAIL, ";".join(hards)
        if stalls:
            summary += f"(另:{';'.join(stalls)})"
        if dup_note:
            summary += f";{dup_note}"
        if order_fill or cancel_fill:
            summary += f" ⚠ {order_fill or cancel_fill},账户可能多出持仓,请人工核对持仓"
        if cancel_may_hang:
            # 硬失败这一支原来**只**消费了「成交」告警,漏掉「撤单没确认 ⇒ 单子可能还挂着」
            # ⇒ 真的失败时反而看不到要去柜台撤的动作提示(2026-11-16 codex 第九轮复核 L2)。
            #
            # 这一支与上面那条**互不排斥**:「部分成交 + 余量还挂在柜台上」是**双重后果**,
            # 两条告警都要出现 —— 写成 `elif` 就会让成交告警把柜台核对提示短路掉
            # (2026-11-16 codex 第十轮复核 L-1)。
            #
            # 但也不能笼统提示:全部成交的演示单**不在柜台上**(说还挂着是假话)⇒
            # 用 `cancel_may_hang` 这个按**终态**定下来的结构化标记(见撤单分支),
            # 而不是「撤单没确认」。废单同理(压根没上柜台)。
            summary += " ⚠ 演示单可能仍挂在柜台上,请人工去柜台核对"
    elif order_stalled or cancel_stalled:
        # 「判不出来」的原因也要**都**收上:只取第一个,另一个指标的原因就整条消失了
        # (2026-11-16 codex 复核 M1:报单指标判不出来时,撤单指标那条
        #  「演示单已成交 ⇒ 账户可能多出持仓」就从摘要里消失了)。
        reasons = [r for r in (order_stalled, cancel_stalled) if r]
        if dup_note:
            reasons.append(dup_note)
        reason = ";".join(reasons)
        status, summary = SKIP, f"阈值对照不可靠({reason}),请在委托回报正常时补测"
        # 摘要必须说清**后果**:演示单成交了 ⇒ 账户可能多出持仓(要去平仓核对);
        # 撤单没确认 ⇒ 单子可能还挂着(要去柜台撤)。混成一句会把人引到错的动作上
        # (2026-11-16 codex 复核 M-1)。成交告警**无条件**补:它跟「为什么判不出来」无关,
        # 只跟「账户现在可能多了持仓」有关,漏掉就是把真风险写没了。
        if order_fill or cancel_fill:
            summary += f" ⚠ {order_fill or cancel_fill},账户可能多出持仓,请人工核对持仓"
        if cancel_may_hang:
            # 「可能还挂在柜台上」按**终态**判断(结构化标记 `cancel_may_hang`):
            # 废单(被拒)没上柜台、全部成交的也不在柜台上 ⇒ 都不提示;
            # 部分成交或没有终态回报才提示。不用整串关键字匹配 ——
            # 关键字匹配会被别的指标的原因里出现的「拒单」二字误伤(2026-11-16 codex 复核 L3)。
            #
            # 与上面那条成交告警**互不排斥**:部分成交 + 余量还挂着是双重后果,两条都要说
            # (2026-11-16 codex 第十轮复核 L-1)。
            summary += " ⚠ 演示单可能仍挂在柜台上,请人工去柜台核对"
    elif order_ok and dup_ok and cancel_ok:
        status, summary = PASS, ""
    elif cancel_ok is None and order_ok and dup_ok:
        status, summary = SKIP, "撤单总笔数阈值需在交易时段补测"
    else:
        # 三个指标(报单/撤单/重复报单)里任何一个没过,都要说清**是哪一项、因为什么**:
        # 只写一句「有阈值未能触发拦截与预警」等于没给结论
        # (2026-11-16 codex 复核 M3:重复报单原来只回一个布尔,失败时一个原因都没有)。
        reasons = []
        if not order_ok:
            reasons.append(f"报单:{order_block or '阈值对照未通过'}")
        if dup_note:
            reasons.append(dup_note)
        if cancel_ok is False:
            reasons.append(f"撤单:{cancel_block or '阈值对照未通过'}")
        status = FAIL
        summary = ("有阈值未能触发拦截与预警(" + ";".join(reasons) + ")") if reasons else "有阈值未能触发拦截与预警"
    book.add("P3.1", status, summary, shots, started)
    if order_hard:
        # 摘要**不许**预设原因:硬失败的措辞已经写明了真实原因(「阈值未生效」是证据支持的,
        # 「对照用的委托没能发出(…)」不是),这里只做中性前缀(2026-11-16 codex 复核 H1)。
        t6_status, t6_summary = FAIL, f"报单阈值对照未通过:{order_hard}"
    elif order_stalled:
        t6_status, t6_summary = SKIP, f"报单阈值对照不可靠({order_stalled})"
    else:
        t6_status = PASS if order_ok else FAIL
        # 走到这里还没被硬失败/判不出来覆盖:报单这一项的对照**没通过**,但失败原因必须
        # **按证据**说(2026-11-16 codex 复核 M2:旧文案直接断言「阈值拦住了委托」,
        # 而那一笔可能是被**别的规则**先拦下的 —— ActiveOrderRule 在 DailyLimitRule 之前判定,
        # 委托规模检查也会拦。没有证据就断言原因 = 冤枉风控系统,那句话会进证据与摘要)。
        # 判定仍然是 FAIL(必测项没过就是没过),只是原因不再预设。
        if order_ok:
            t6_summary = ""
        elif order_attrib == "ours":
            t6_summary = "报单阈值对照未通过(本项阈值拦住了委托但没有收到预警告警)"
        elif order_attrib == "other":
            t6_summary = f"报单阈值对照未通过(超过阈值的委托{order_block},本项阈值是否生效未经验证)"
        else:
            t6_summary = ("报单阈值对照未通过(超过阈值的委托没发出去且没有风控拦截日志,"
                          "判不出是不是本项阈值拦的)")
    book.add("T6", t6_status, t6_summary, [Shot("", shots[0].lines)], started)


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
        cancel_and_note(ctx, [oid] if oid else [], shots[-1].lines)
    ok_all = all(oks)
    book.add("P4.1", PASS if ok_all else FAIL, "" if ok_all else "有错误指令没有被本地检查拦截", shots, started)


def step_counter_errors(ctx: Ctx, book: Book) -> None:
    """P4.2:**绕过本地风控**直发,采集真实柜台错误提示。

    演示哪些场景、为什么不演示某些场景(2026-11-16 夜盘实测的教训):
    - **无仓位可平**:平仓单,柜台回「平仓量超过持仓量」(错误码 30)。**平仓被拒不会锁账户**
      (trading/guard_rule.py 的 `_handle_reject`:平仓委托被拒只告警,不锁),
      所以这个场景可以安全演示,而且它证明的正是「系统确实收到了柜台的错误提示」。
    - **无足够资金开仓**:**故意不做**。制造资金不足只有两条路,两条都会毁掉这一轮取证:
      (a) 用超出手数上限的委托 —— 期货只收保证金,柜台**可能直接接受**,那就真的开出几千手仓位;
      (b) 用跌停价以下的价钱 —— 柜台按「价格跌破跌停板」**硬拒绝**,而硬拒绝会触发
      账户风控的「当日禁止开仓」,这一轮剩下的发单项全部被挡下
      (2026-11-16 夜盘实测:200 笔稳定运行只放行了 9 笔,其余全被本地风控拦在
      「账户处于DAILY_LOCKED」上)。这个场景留给专用小资金账户补测。
    - **市场状态不允许**:非交易时段才能演示,交易时段跳过。
    """
    started = ctx.now()
    c = ctx.contract
    base = float(getattr(ctx.tick, "last_price", 0) or 0) or 1000.0
    cases = {name: req for name, req in build_cases(ctx.symbol, c.exchange, base, int(getattr(c, "max_volume", 0) or 1),
                                                   pricetick=c.pricetick, in_session=ctx.in_session)}
    plan = [("无足够资金开仓", None, "资金"), ("无仓位可平", cases.get("无持仓时平仓"), ""),
            ("市场状态不允许", cases.get("非交易时段开仓"), "")]
    gateway = ctx.main_engine.get_gateway(ctx.gateway_name)
    shots, oks, notes = [], [], []
    for label, req, expect in plan:
        if label == "无足够资金开仓":
            shots.append(Shot(label, ["本场景**故意不演示**:制造资金不足要么让柜台接受委托(真的开出大仓),"
                                       "要么用跌停价以下的价钱触发柜台硬拒绝 —— 硬拒绝会让账户当日禁止开仓,"
                                       "本轮剩下的发单项全被挡下(2026-11-16 夜盘实测)。",
                                       "需在专用小资金账户补测,不能放在正式取证这一轮里做。"]))
            notes.append(f"{label}需在专用小资金账户补测")
            oks.append(None)
            continue
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
        shots.append(Shot(label, [ctx.clean(f"直发委托(绕过本地风控):{req.symbol} {direction_text(req.direction)}{req.offset.value} {req.volume}手 @ {req.price}"),
                                  ctx.clean(f"委托终态:{order.status.value if order else '无回报'}"),
                                  ctx.clean(f"柜台错误提示:{text or '(未收到柜台错误文本)'}")]))
        if not ok and expect:
            notes.append(f"{label}:柜台没有返回含「{expect}」的错误(账户资金充裕时无法触发资金不足)")
        oks.append(ok)
        cancel_and_note(ctx, [oid] if oid else [], shots[-1].lines)
    if any(o is True for o in oks):
        # 只要真收到了柜台的错误提示,「接收柜台错误提示」这个功能就算演示到了;
        # 没演示的场景按「需补测」记在说明里,不能当成失败。
        # 说明里要保留这些「需补测」——报告要能看出这一项**没有**覆盖全部场景。
        status, summary = PASS, ";".join(notes)
    elif any(o is False for o in oks):
        status, summary = FAIL, ";".join(notes) or "没有收到柜台错误提示"
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
        cancel_and_note(ctx, made, lines)
    lines.append(f"{ctx.now()} 执行 pause_trading off(恢复)")
    ctx.wait_until(lambda: any(a["title"] == "交易已恢复" for a in ctx.alerts_since(alert_mark)), 4.0)
    resumed = send(ctx, make_req(ctx, price=probe_price(ctx)))
    if resumed:
        made.append(resumed)
    lines.append("恢复后开仓委托:" + ("通过风控" if resumed else "⚠ 仍被拒绝"))
    alerts = [a for a in ctx.alerts_since(alert_mark) if a["title"] in ("交易已暂停", "交易已恢复")]
    lines += [ctx.clean(f"告警:[{a['level']}] {a['title']}") for a in alerts]
    forward_alerts(ctx, alerts)
    cancel_and_note(ctx, made, lines)
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
    guard = ctx.rule(ACCOUNT_RULE_NAME)
    if guard is not None:
        # 账户被锁(当日锁定/硬锁定)时,开仓委托会被本地风控**全部**拦下:
        # 那样 200 笔一笔都发不出去,证据只会显示"放行 0 笔",看不出是账户状态问题。
        # 这里提前判成「需补测」,并写明锁定状态。
        state = guard.guard.status(trading_day(beijing_now()))
        if state != "OK":
            reason = (f"账户处于{state}状态,开仓委托会被本地风控全部拦下,本项无法演示;"
                      "需先解除锁定(等下一个交易日,或人工解除)再补测")
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
             "演示期间临时放宽的风控上限(演示后已还原):"
             + ("、".join(f"{getattr(r, 'name', r)}.{a} {v}→{STABILITY_LIMIT}" for r, a, v in lifted) or "无"),
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
        held, connected = check_positions(ctx.main_engine, wait=ctx.position_wait, early_exit=False,
                                      collector=ctx.collector)
        if held:
            notes.append(f"⚠⚠ 收尾发现账户有持仓 {held},请立即人工平仓!")
        elif not connected:
            # 「查不到持仓」不等于「没有持仓」:通道不在线时这次查询是空的,
            # 不能当成「账户是空的」交差(那会让一次断线的运行看起来是干净的)。
            notes.append("⚠⚠ 收尾:交易通道不在线,持仓查询不可信,无法确认账户是否空仓,请人工核对!")
    except Exception as exc:     # noqa: BLE001  兜底本身不能崩
        notes.append(f"收尾检查出错:{exc!r},请人工核对账户是否有遗留委托/持仓")
    return notes


def run_steps(ctx: Ctx, book: Book, only: set[str] | None = None) -> bool:
    """按序执行全部步骤。登录失败则后续依赖连接的步骤不再执行。返回是否登录成功。"""
    ctx.failed_steps = []
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
    failed_steps = ctx.failed_steps

    def guarded(items: tuple[str, ...], fn, *fn_args):
        """单步抛异常不能带走整次取证:已采到的证据要保住,这一步记 FAIL,继续往下。

        返回 (是否抛了异常, 步骤返回值)。
        """
        try:
            return False, fn(*fn_args)
        except Exception as exc:      # noqa: BLE001  任何异常都转成证据
            reason = ctx.clean(f"步骤 {fn.__name__} 抛出异常:{type(exc).__name__}: {exc}")[:300]
            failed_steps.append(fn.__name__)
            for name in items:
                if name in ITEMS:
                    book.add(name, FAIL, reason, [Shot("", [reason])], ctx.now())
            return True, None

    if want("T2", "T3", "T4", "T5"):
        guarded(("T2",), step_market, ctx, book)
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
        crashed, _ = guarded(("T3", "T5", "P1.2"), step_basic_trade, ctx, book)
        if crashed:
            gated = True                       # 异常时委托/持仓状态不明:后面的发单演示一律中止
        elif ctx.in_session and ctx.tick is not None and not ensure_flat(ctx):
            gated = True                                   # 平仓演示没把账户带回空仓:后面的发单演示全部中止
            book.add("P1.2", FAIL, "开平仓演示后账户没有回到空仓,已中止后续发单演示", [Shot("", ["账户未回到空仓"])], ctx.now())
    if not gated and want("T4", "P1.2"):
        gated = guarded(("T4", "P1.2"), step_cancel, ctx, book)[0] or gated
    if want("P2.2"):
        guarded(("P2.2",), step_counts, ctx, book)
    dup = {}
    if not gated and want("P2.3", "P3.1", "T6"):
        crashed, result = guarded(("P2.3",), step_duplicate, ctx, book)
        dup = result or {}
        gated = crashed or gated
    if not gated and want("P3.1", "T6"):
        gated = guarded(("P3.1", "T6"), step_threshold, ctx, book, dup)[0] or gated
    if want("P4.1"):
        guarded(("P4.1",), step_validity, ctx, book)
    if not gated and want("P4.2"):
        gated = guarded(("P4.2",), step_counter_errors, ctx, book)[0] or gated
    if not gated and want("P5.1"):
        gated = guarded(("P5.1",), step_pause, ctx, book)[0] or gated
    if not gated and want("P1.3"):
        gated = guarded(("P1.3",), step_stability, ctx, book)[0] or gated
    if want("P2.1"):
        guarded(("P2.1",), step_connection, ctx, book)
    if want("T7"):
        guarded(("T7",), step_exit, ctx, book)
    if want("P5.2"):
        guarded(("P5.2",), step_batch_cancel, ctx, book)
    if want("P6.1"):
        guarded(("P6.1",), step_logs, ctx, book)         # 最后做:前面各步产生的日志才齐
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


def connection_info(setting: dict, now: datetime | None = None, rehearsal: bool = False) -> dict:
    """本次连接的环境事实。不含用户名/密码/授权编码;AppID 只留掩码。

    `mode` 写明这份证据是「国信测试环境」还是「预演」——证据目录本身要能自证,
    不能靠人记得它是在哪台机器上跑出来的。
    """
    broker_id = str(setting["经纪商代码"])
    return {
        # 时间戳一律**北京时间**:证据目录里的其它条目(证据行、清场留痕)都是北京时间,
        # 只有这一处写本机本地时间的话,同一个目录里的时间线会差 16 小时,事后对时间线会打架
        # (2026-11-16 codex 复核 L3)。
        "at": (now or beijing_now()).isoformat(timespec="seconds"),
        "environment": setting["柜台环境"],
        "trade_front": setting["交易服务器"],
        "market_front": setting["行情服务器"],
        "broker_id": broker_id,
        "app_id": mask_account(setting["产品名称"]),
        # 以 --rehearsal 为准,并与经纪商代码交叉校验:对 8030 跑预演也必须标成预演
        "mode": "guoxin" if (broker_id == GUOXIN_TEST_BROKER_ID and not rehearsal) else "rehearsal",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--symbol", default="", help="测试合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--confirm-broker-test", action="store_true", help="确认目标是期货公司的测试环境")
    parser.add_argument("--interactive", action="store_true", help="执行需要人工配合的 3.1 断网演练")
    parser.add_argument("--resume", type=Path, default=None, help="在已有证据目录上增量补测(新的 SKIP 不覆盖旧的 PASS/FAIL)")
    parser.add_argument("--out-dir", type=Path, default=None, help="证据输出目录(默认 data/broker_test/<时间戳>)")
    parser.add_argument("--only", default="", help="只跑指定测试项,逗号分隔,如 T3,P4.1")
    parser.add_argument("--connect-timeout", type=float, default=300.0,
                        help="等待登录全流程(含合约表查询)的最长时间(秒);柜台的合约表查询实测要 90 秒以上")
    parser.add_argument("--disconnect-timeout", type=float, default=120.0)
    parser.add_argument("--profile", default="", help="用指定档案(如 .env.guoxin);默认用项目 .env")
    parser.add_argument("--rehearsal", action="store_true",
                        help="明确这是预演(连的不是国信测试环境)。证据会被标成 rehearsal,"
                             "产出的报告不能提交国信")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")

    # 配置解析只走 ctp_tools.profile:清场工具、穿透检查、取证运行器必须
    # 用**同一套**优先级(档案 > .env > 进程环境),否则「清场平的账户」和
    # 「取证连的账户」可能不是同一个。
    try:
        profile = load_profile(args.profile, root=bootstrap.ROOT)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_CONFIG
    if profile.hard_conflicts:
        print("进程环境变量与项目 .env 不一致(又没有指定档案),无法判断该连哪台机器,已退出:"
              "请清掉这些环境变量(set VAR= 后重开终端),或用 --profile 明确指定档案。", file=sys.stderr)
        for c in profile.hard_conflicts:
            print(f"  - {c}", file=sys.stderr)
        return EXIT_CONFIG
    for note in profile.notes:
        print(f"⚠ {note}(已按档案取值)", file=sys.stderr)
    env = profile.env
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
    # 「柜台环境=测试」这一句不足以证明连的是期货公司的测试机——那台机器只按
    # 环境变量判断,不校验地址,所以这里必须自己校验。
    #
    # 而且白名单**只能收窄不能放宽**:BROKER_TEST_ALLOWED_TD 与 CTP_TD_ADDRESS
    # 同在 .env 里,直接采信它等于自己给自己发通行证(改两个键就能把账号密码
    # 发给任意机器)。所以这里与代码里写死的可信地址全集求交集,
    # 交集之外的地址一律拒绝(见 ctp_tools.broker_targets.resolve_allowed)。
    allowed_td, allowed_md, dropped_td, dropped_md = resolve_allowed_for_broker(
        setting["经纪商代码"], env.get("BROKER_TEST_ALLOWED_TD", ""), env.get("BROKER_TEST_ALLOWED_MD", ""))
    for label, dropped in (("交易", dropped_td), ("行情", dropped_md)):
        if dropped:
            print(f"⚠ {label}前置白名单里的 {dropped} 不属于经纪商代码 {setting['经纪商代码']!r} "
                  "对应的可信地址表,已丢弃(白名单与地址同源,采信它等于自证清白)。", file=sys.stderr)
    ok, note = check_target(setting["柜台环境"], setting["交易服务器"], args.confirm_broker_test, allowed_td)
    print(note, file=sys.stderr if not ok else sys.stdout)
    if not ok:
        return EXIT_CONFIG
    if setting["经纪商代码"] != GUOXIN_TEST_BROKER_ID and not args.rehearsal:
        # 这个取证运行器出的是**国信**的报告:连着别的环境(SimNow 之类)生成的证据,
        # 目录名和内容都长得像正式证据,事后很容易被当成国信环境的结果交上去。
        print(f"目标不是国信测试环境(BrokerID={setting['经纪商代码']!r}),已退出:"
              "这个取证运行器产出的证据会填进国信的报告模板。确属预演请加 --rehearsal"
              "(证据会被标成 rehearsal,报告不能提交国信)。", file=sys.stderr)
        return EXIT_CONFIG
    md_address = normalize_address(setting["行情服务器"])
    # 行情前置同样要查:行情前置喂回来的盘口价会**直接变成委托价**,
    # 连错行情前置 = 用假价格下单(演示委托可能因此成交,留下持仓)。
    if not allowed_md:
        print("行情前置白名单为空(BROKER_TEST_ALLOWED_MD 没配,或里面没有可信地址),拒绝连接:"
              "行情前置的盘口价会直接变成委托价,必须和交易前置一样校验。", file=sys.stderr)
        return EXIT_CONFIG
    if md_address not in allowed_md:
        print(f"行情前置 {md_address} 不在可信前置地址表 {sorted(allowed_md)} 内,拒绝连接。"
              "行情前置的盘口价会直接变成委托价,不能靠「交易前置是白名单内的」来推定它也是。",
              file=sys.stderr)
        return EXIT_CONFIG
    print(f"交易前置 {setting['交易服务器']} / 行情前置 {setting['行情服务器']} —— "
          f"白名单校验通过(允许的交易前置 {sorted(allowed_td)}、行情前置 {sorted(allowed_md)})。")
    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    unknown = (only or set()) - set(ITEMS)
    if unknown:
        print(f"未知测试项:{sorted(unknown)};可用:{list(ITEMS)}", file=sys.stderr)
        return EXIT_CONFIG
    locked = locked_state_note(guard_state_file())
    if locked:
        print(f"拒绝连接:{locked}", file=sys.stderr)
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
    rule.update_setting({"state_file": str(guard_state_file())})   # 隔离:不碰实盘的 data/state/account_guard.json

    collector = Collector()
    event_engine.register(EVENT_LOG, collector.on_log)
    event_engine.register(EVENT_ORDER, collector.on_order)
    event_engine.register(EVENT_TICK, collector.on_tick)
    event_engine.register(EVENT_CONTRACT, collector.on_contract)
    event_engine.register(EVENT_POSITION, collector.on_position)
    event_engine.register(EVENT_ACCOUNT, collector.on_account)   # 持仓查询「没有行」时用账户回报证明柜台在应答
    secrets = tuple(s for s in (env.get("CTP_PASSWORD", ""), env.get("CTP_AUTH_CODE", ""), env.get("CTP_APP_ID", ""),
                                env.get("CTP_USERNAME", ""), env.get("WEBHOOK_URL", ""), env.get("WEBHOOK_TELEGRAM_CHAT_ID", "")) if s)
    from run_headless import build_notifier
    notifier = build_notifier(env)
    ctx = Ctx(main_engine=main_engine, risk_engine=risk_engine, collector=collector, symbol=symbol, secrets=secrets,
              interactive=args.interactive, connect_timeout=args.connect_timeout, disconnect_timeout=args.disconnect_timeout,
              notifier=notifier if notifier.enabled else None)
    event_engine.register(EVENT_GUARD_ALERT, lambda e: ctx.alerts.append(e.data))
    event_engine.register(EVENT_TRADE, lambda e: ctx.trades.append(e.data))

    started_at = beijing_now()
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
    # 证据目录名也是**北京时间**:目录里的内容(条目时间、连接记录、清场留痕)都是北京时间,
    # 目录名若用本机本地时间,同一目录里就会差 16 小时(2026-11-16 codex 复核 L5)。
    out_dir = args.resume or args.out_dir or (OUT_ROOT / beijing_now().strftime("%Y%m%d-%H%M%S"))
    evidences = merge_evidence(load_evidence(out_dir) if args.resume else {}, book.list())
    cjk = render_all(evidences, out_dir, secrets)
    save_evidence(out_dir, evidences)
    save_connection_info(out_dir, connection_info(setting, started_at, rehearsal=args.rehearsal))
    if not cjk:
        print("⚠ 没找到支持中文的字体(msyh.ttc/simhei.ttf),证据图里的中文会显示为方框;可设环境变量 BROKER_REPORT_FONT 指定字体。", file=sys.stderr)
    for note in notes:
        print(note, file=sys.stderr)
    if not closed:
        print("⚠ 引擎关闭超时,进程将被强制结束:国信「退出后是否正常注销」可能受影响,请观察。", file=sys.stderr)
    print(f"证据已写入 {out_dir}")
    print_summary(evidences)
    return exit_code_for(logged_in, ctx.failed_steps, notes, evidences)


def exit_code_for(logged_in: bool, failed_steps: list[str], notes: list[str], evidences: list[Evidence]) -> int:
    """取证运行的退出码。一键流程只对 0/1 出报告,所以「证据不完整」必须与 1 分开。"""
    if not logged_in:
        return EXIT_LOGIN
    if failed_steps:
        # 有步骤抛了异常:证据不完整,不能当报告用 —— 与「跑完但有失败项(1)」分开
        print(f"⚠ 步骤抛出异常 {failed_steps},证据不完整。", file=sys.stderr)
        return EXIT_CRASH
    if any("⚠⚠" in n for n in notes):
        return EXIT_FAILED
    return EXIT_FAILED if any(e.status == FAIL for e in evidences) else EXIT_OK


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except SystemExit as exc:
        # sys.exit() / sys.exit("msg") 不能映成 1(=跑完有失败项,一键流程会拿半份证据出报告)
        code = exc.code if isinstance(exc.code, int) else EXIT_CRASH
    except BaseException:      # noqa: BLE001  崩溃/被中断:必须与「有失败项(1)」区分,
        import traceback       # 一键流程只对 0/1 出报告,半份证据不能当报告用
        traceback.print_exc()
        code = EXIT_CRASH
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)         # CTP 原生线程在断连风暴下可能不退出,强制结束(关闭是否正常已在上面如实告警)
