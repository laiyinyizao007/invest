"""SimNow 实测演练:把运行手册第 5 节里"只能在真实 CTP 上确认"的条目做成可重复执行的子命令。

为什么需要它:第 5 节的每一项都要人在旁边一步步做,既容易漏步骤,也没留下可核对的记录。
这里把每一项做成一个场景:脚本自己连 SimNow、自己做动作、自己记录结论,
结果写进 `docs/simnow-drills.md`,人可以只核对结论。

场景(对应运行手册第 5 节):
    latency      第 3 项 成交后持仓回报多久刷新
    tick-stale   第 9 项 行情断流 → 看门狗是否告警
    disconnect   第 4 项 交易连接断开 → 是否告警、是否自动重连
    daily-loss   第 6 项 当日熔断 → 撤单 → 平仓
    hard-lock    第 7 项 累计死线 → 平仓 → 退出 → 重启拒绝交易
    overnight    第 8 项 夜盘首根 Tick 与交易日归属

用法(SimNow 上,**要在容器里跑**,见下):
    docker compose run --rm --entrypoint python trader scripts/drill.py list
    docker compose run --rm --entrypoint python trader scripts/drill.py all --confirm-simnow
    docker compose run --rm --entrypoint python trader scripts/drill.py latency --confirm-simnow --symbol c2701.DCE

⚠ 必须在容器里跑:运行时的"是否处于交易时段"判断用的是 `datetime.now()`(系统本地时区),
宿主机是 UTC-8,容器里 TZ=Asia/Shanghai。时段判断错位会让看门狗不触发、演练结论失真。

安全约束:
- 只在模拟盘上跑:SimNow 官方地址白名单 + `--confirm-simnow`(与 collect_reject_texts.py 同一套约束);
- **演练状态与实盘状态完全隔离**:独立的 `.vntrader_drill` 目录与独立的风控状态文件,
  演练里的熔断/硬锁定不会污染真实的 `data/state/account_guard.json`;
- `latency` 场景会真的开 1 手仓,量完延迟立刻平掉;平不掉就大声告警并以非零码退出;
- 演练期间用的是**空策略**,不会自己下单,避免和演练动作互相干扰。
"""
import argparse
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Callable

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap

# 演练的状态目录与实盘完全分开，且必须早于 vn.py logger 导入。
DRILL_ROOT = bootstrap.ROOT / ".vntrader_drill"
ISOLATED = bootstrap.isolate_state(DRILL_ROOT.name)

# scripts/ 目录里的模块(simnow_guard、run_headless)按同目录导入
sys.path.insert(0, str(bootstrap.ROOT / "scripts"))

from vnpy.trader.constant import Direction, Exchange, Offset, OrderType  # noqa: E402
from vnpy.trader.event import EVENT_POSITION, EVENT_TICK, EVENT_TRADE  # noqa: E402
from vnpy.trader.object import CancelRequest, OrderRequest  # noqa: E402
from vnpy.trader.utility import round_to  # noqa: E402
from vnpy_ctastrategy import CtaTemplate  # noqa: E402

from risk.account_guard import trading_day  # noqa: E402
from strategies.trend_atr_strategy import ENTRY_SESSIONS  # noqa: E402
from trading.runtime import EXIT_OK, RuntimeConfig, TradingRuntime, build_ctp_setting  # noqa: E402

BEGIN_MARK = "<!-- BEGIN DRILLS -->"
END_MARK = "<!-- END DRILLS -->"

SCENARIOS: dict[str, str] = {
    "latency": "第 3 项 成交后持仓回报多久刷新",
    "tick-stale": "第 9 项 行情断流 → 看门狗是否告警",
    "disconnect": "第 4 项 交易连接断开 → 是否告警、是否自动重连",
    "daily-loss": "第 6 项 当日熔断 → 撤单 → 平仓",
    "hard-lock": "第 7 项 累计死线 → 平仓 → 退出 → 重启拒绝交易",
    "overnight": "第 8 项 夜盘首根 Tick 与交易日归属",
}
# 会改变账户风控状态的场景放最后:硬锁定会让运行时退出并拒绝再次启动。
ORDER = ["overnight", "latency", "tick-stale", "disconnect", "daily-loss", "hard-lock"]

# 会**真的发出委托**的场景:
#   latency    开 1 手再平掉;
#   daily-loss 触发当日熔断后,运行时会撤单并平仓;
#   hard-lock  触发硬锁定后,运行时会平仓。
# 目标地址不在 SimNow 白名单时,这些场景要额外一道确认开关(见 main 里的检查)。
REAL_ORDER_SCENARIOS = frozenset({"latency", "daily-loss", "hard-lock"})


def risky_scenarios(names: list[str], td_address: str) -> list[str]:
    """在非 SimNow 地址上、且会真的发委托的场景。

    白名单内的地址(模拟盘)不拦;白名单外(可能是期货公司生产 API)才要额外确认。
    地址在这里**自己**归一化 —— 调用方忘了归一化时,白名单判断会失效
    (同一个地址的两种写法会得到不同的结论),那道闸门必须自己站得住。
    """
    from ctp_tools.simnow import SIMNOW_TD_ADDRESSES, normalize_address

    if normalize_address(td_address) in SIMNOW_TD_ADDRESSES:
        return []
    return sorted(set(names) & REAL_ORDER_SCENARIOS)

# 场景的前提条件:不满足时**跳过**而不是判失败 —— 例如看门狗只在交易时段内检查行情停更,
# 在收盘后跑"tick-stale"必然等不到告警,那是前提不满足,不是系统有问题。
# 这里用容器里的本地时间(容器 TZ=Asia/Shanghai)判断。
#
# 时段窗口直接取自策略的 ENTRY_SESSIONS —— 运行时判断"是否在交易时段"用的就是它
# (trading/runtime.py::_in_trading_session),两处必须一致:演练说"在交易时段里"
# 而运行时说"不在",看门狗就不会检查,场景必然失败,而且是假失败。
SESSION_WINDOWS = tuple(ENTRY_SESSIONS)
NIGHT_WINDOW = ((dtime(20, 30), dtime(23, 30)),)


def _in_windows(now: datetime, windows) -> bool:
    t = now.time()
    return any(start <= t < end for start, end in windows)


def scenario_precondition(name: str, now: datetime | None = None) -> tuple[bool, str]:
    """判断场景的前提是否满足。返回 (是否满足, 不满足时的说明)。"""
    now = now or datetime.now()
    if name in ("latency", "tick-stale", "disconnect"):
        if not _in_windows(now, SESSION_WINDOWS):
            return False, (f"当前时间 {now.strftime('%H:%M')} 不在交易时段内"
                           f"(夜盘 21:00-23:00,日盘 9:00-15:00),行情不推送、"
                           f"看门狗也不检查行情停更,这个场景在收盘后跑没有意义")
    if name == "overnight":
        if not _in_windows(now, NIGHT_WINDOW):
            return False, (f"当前时间 {now.strftime('%H:%M')} 不在夜盘时段(21:00-23:00)附近,"
                           f"等不到夜盘的首根 Tick;请在 20:30 之后再跑")
    return True, ""


class NoopStrategy(CtaTemplate):
    """演练用的空策略:参与启动与对账流程,但永远不交易。

    演练要观察的是**管道**(回报时序、看门狗、熔断链路),不是策略;
    用空策略可以保证演练过程中不会有策略自己的委托干扰观察。
    """

    author = "invest"

    def on_init(self) -> None:
        pass

    def on_start(self) -> None:
        pass

    def on_tick(self, tick) -> None:
        pass

    def state_consistent(self) -> bool:
        return True


@dataclass
class DrillResult:
    """一个场景的结论。"""

    name: str
    ok: bool
    summary: str
    details: dict = field(default_factory=dict)
    skipped: bool = False       # 场景本身有前提(例如必须处于夜盘),前提不满足时跳过而不是判失败


class RecordingNotifier:
    """包一层真实通知器:把每一条告警连同时间记下来。

    运行时的告警有两类来源 —— 守卫的 EVENT_GUARD_ALERT,以及运行时自己 notify 的
    ("行情停更""网关连接断开"等)。都只经过通知器,所以在这里统一截获最完整。
    底层仍然用真实的 WebhookNotifier:演练顺带把真实告警发出去,
    通知链路本身也在被检验。

    两件事必须分开记:
    - `alerts`:运行时**发出**了这条告警(不管底层有没有接受)。场景判定看这个 ——
      演练要证明的是"风控/看门狗有没有报警",不是"手机有没有响"。
    - `delivered`:底层通知器**接受**了这条(真正进了发送队列)。这才是"送达"。

    两者混在一起会得出错误的结论:底层被去重/过滤/禁用时 `send()` 返回 False,
    手机端其实没响,但记录里看着像"收到告警了"。所以每条记录都带 `delivered` 标记,
    汇总时分别统计。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.alerts: list[dict] = []
        self._lock = threading.Lock()

    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool:
        accepted = self.inner.send(level, title, message, extra)
        with self._lock:
            self.alerts.append({"time": datetime.now().isoformat(timespec="seconds"),
                                "level": level, "title": title, "message": message,
                                "delivered": bool(accepted)})
        return accepted

    def delivery_summary(self) -> dict:
        """告警发出数 / 底层接受数 / 未送达明细。

        注意:通知渠道**没有配置**时(WEBHOOK_URL 为空),底层对每条告警都返回 False,
        这不是"送达失败",而是"根本没地方可送"。这两种情况必须分开说,
        否则会把"没配 webhook"误报成"告警丢了"。
        """
        with self._lock:
            total = len(self.alerts)
            delivered = sum(1 for a in self.alerts if a.get("delivered"))
            missed = [a for a in self.alerts if not a.get("delivered")]
        configured = bool(getattr(self.inner, "enabled", True))
        return {"通知渠道已配置": configured,
                "发出": total, "底层接受": delivered, "未送达": len(missed),
                "未送达明细": [f"{a['level']} {a['title']}" for a in missed]}

    def close(self, drain_timeout: float = 10.0) -> None:
        self.inner.close(drain_timeout)

    def find(self, keyword: str, since: int = 0) -> list[dict]:
        with self._lock:
            return [a for a in self.alerts[since:]
                    if keyword in a["title"] or keyword in a["message"]]

    def mark(self) -> int:
        """当前告警条数。场景开始时记一下,等待告警时只看之后的,避免命中旧告警。"""
        with self._lock:
            return len(self.alerts)

    def wait_for(self, keyword: str, timeout: float, since: int = 0) -> dict | None:
        """等一条含关键字的告警出现(演练里所有等待都有上限,不会挂死)。

        `since` 是场景开始时的告警条数:断线/停更这类告警可能在场景动作之前就发过,
        不设下界就会把旧告警当成本次场景的结果(假阳性)。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            hits = self.find(keyword, since)
            if hits:
                return hits[0]
            time.sleep(0.2)
        return None


class Drill:
    """一次演练的上下文:运行时 + 观察器。"""

    def __init__(self, args, env: dict[str, str], symbol: str, notifier) -> None:
        self.args = args
        self.env = env
        self.symbol = symbol
        self.notifier = notifier
        self.runtime: TradingRuntime | None = None
        self.ticks: list[tuple[float, object]] = []      # (到达时刻, TickData)
        self.trades: list[tuple[float, object]] = []
        self.positions: list[tuple[float, object]] = []
        self._lock = threading.Lock()
        # 进场前该合约的持仓总手数;None = 还没记录,不做总手数校验。
        # 净持仓为 0 时可能还有锁仓,收尾判断必须看总手数,所以单独记一份。
        self.gross_baseline: int | None = None
        # **演练自己开出去的仓位**(方向 + 手数)。
        # 净持仓账户(CTP 的"净"持仓方向)里,演练开的多仓回报上来是 Direction.NET,
        # 净额算出来是 0 —— 只看净额会以为"没有仓位要平",于是既不平仓、
        # 又把留仓手数误报成 0。演练知道自己开了多少,平仓要按这个来。
        self.owned_volume: int = 0
        self.owned_direction: Direction | None = None

    # ---------- 生命周期 ----------

    def start(self) -> bool:
        """连接并完成启动对账。返回是否成功进入运行状态。"""
        from vnpy_ctp import CtpGateway

        config = RuntimeConfig(
            vt_symbol=self.symbol,
            strategy_class="NoopStrategy",
            strategy_name="drill_noop",
            gateway_setting=build_ctp_setting(self.env),
            connect_timeout=self.args.connect_timeout,
            heartbeat_file=DRILL_ROOT / "heartbeat",
            # 演练会自己造出持仓(延迟场景),把漂移容忍时间拉长,免得看门狗把演练动作当成异常
            drift_tolerance_seconds=3600.0,
            tick_stale_seconds=self.args.tick_stale_seconds,
        )
        self.runtime = TradingRuntime(config, CtpGateway, notifier=self.notifier,
                                      strategy_classes={"NoopStrategy": NoopStrategy})
        # 风控状态文件指向演练目录:演练里的熔断/硬锁定不会污染实盘状态
        self.runtime.rule.update_setting({"state_file": str(DRILL_ROOT / "account_guard.json")})
        engine = self.runtime.event_engine
        engine.register(EVENT_TICK, self._on_tick)
        engine.register(EVENT_TRADE, self._on_trade)
        engine.register(EVENT_POSITION, self._on_position)
        code = self.runtime.start()
        return code is None

    def stop(self) -> None:
        if self.runtime is not None:
            self.runtime.request_stop(EXIT_OK, "演练结束")
            # 关闭可能不返回:CTP 网关的 close() 会 join 原生线程,断连时可能长时间挂着。
            # 放到后台线程里限时等,剩下的交给 __main__ 的 os._exit 兜底。
            thread = threading.Thread(target=self.runtime.shutdown, daemon=True)
            thread.start()
            thread.join(10.0)

    # ---------- 观察 ----------

    def _on_tick(self, event) -> None:
        data = event.data
        if data.vt_symbol == self.symbol:
            with self._lock:
                self.ticks.append((time.monotonic(), data))

    def _on_trade(self, event) -> None:
        with self._lock:
            self.trades.append((time.monotonic(), event.data))

    def _on_position(self, event) -> None:
        with self._lock:
            self.positions.append((time.monotonic(), event.data))

    def last_tick(self):
        with self._lock:
            return self.ticks[-1] if self.ticks else None

    def wait_for_tick(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tick = self.last_tick()
            if tick is not None:
                return tick
            time.sleep(0.2)
        return None

    def wait_for_trade(self, timeout: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self.trades:
                    return self.trades[-1]
            time.sleep(0.1)
        return None

    def net_position(self) -> int:
        """该合约的净持仓(多 − 空)。

        Direction.NET(净持仓账户)方向不明,既不是多也不是空 —— 单列处理,
        不计入净额,免得把净持仓账户当成空头去平仓。
        """
        if self.runtime is None:
            return 0
        total = 0
        for p in self.runtime.main.get_all_positions():
            if p.vt_symbol != self.symbol or p.volume <= 0:
                continue
            if p.direction == Direction.LONG:
                total += p.volume
            elif p.direction == Direction.SHORT:
                total -= p.volume
        return total

    def gross_position(self) -> int:
        """该合约所有方向的持仓总手数(多 + 空,含方向不明的 NET)。

        净持仓为 0 **不代表**没有仓位:多空锁仓(各 1 手)净额是 0,总手数却是 2。
        判断"账户上还有没有这个合约的仓位"必须看总手数。
        """
        if self.runtime is None:
            return 0
        return sum(p.volume for p in self.runtime.main.get_all_positions()
                   if p.vt_symbol == self.symbol and p.volume > 0)

    def has_position(self) -> bool:
        """该合约上是否存在任何方向的持仓。"""
        return self.gross_position() > 0

    def at_baseline(self, baseline: int) -> bool:
        """持仓是否已经回到进场前的状态。

        净持仓回到 baseline 只是必要条件:锁仓与方向不明的仓位都不体现在净额里,
        所以还要看总手数(知道进场时的总手数时才查)。
        """
        if self.net_position() != baseline:
            return False
        if self.gross_baseline is None:
            return True
        return self.gross_position() <= self.gross_baseline

    def wait_flat(self, timeout: float, baseline: int = 0) -> bool:
        """等到持仓回到 `baseline`(默认 0 = 空仓)。

        baseline 不是 0 时表示"演练之前账户上就有仓位",平仓只要求回到那个起点,
        而不是要求空仓 —— 否则会把不属于演练的仓位也平掉。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.at_baseline(baseline):
                return True
            time.sleep(0.3)
        return self.at_baseline(baseline)


# ---------------- 场景 ----------------


def scenario_latency(drill: Drill) -> DrillResult:
    """成交后持仓回报多久刷新(运行手册第 5 节第 3 项)。

    做法:以对手价开 1 手 → 记录成交时刻 → 记录持仓回报到达的时刻 → 差值就是延迟。
    紧急平仓按成交量对账时等待 `position_refresh_timeout`(默认 6 秒),这个延迟必须小于它。

    演练只碰**自己开的那 1 手**:进场前要求该合约**一手都不能有**
    (只看净持仓会被"多空锁仓、净额为 0"骗过去),
    收尾只平掉净持仓相对 baseline 的增量,绝不平掉演练之前就存在的仓位。
    """
    runtime = drill.runtime
    assert runtime is not None
    tick_entry = drill.wait_for_tick(drill.args.tick_timeout)
    if tick_entry is None:
        return DrillResult("latency", False, f"{drill.args.tick_timeout:.0f}s 内没有收到 {drill.symbol} 的行情")
    _, tick = tick_entry
    contract = runtime.main.get_contract(drill.symbol)
    if contract is None:
        return DrillResult("latency", False, f"没有拿到 {drill.symbol} 的合约信息,无法构造委托")
    pricetick = contract.pricetick or 1.0
    buy_price = round_to(tick.ask_price_1 or tick.last_price, pricetick)
    if buy_price <= 0:
        return DrillResult("latency", False, "行情里没有可用的买价/最新价,无法构造委托")

    exchange = contract.exchange
    code = drill.symbol.split(".")[0]
    # 进场前账户上就已经有同品种持仓 → 这个场景的"平掉演练开的仓"没法与既有持仓区分,
    # 强行平仓会误伤别人的仓。直接拒绝跑,而不是猜。
    # 只看**净**持仓不够:多空锁仓(各 1 手)净额是 0,但平仓时会动到既有仓位。
    # 所以按总手数判断:该合约上一手都不能有。
    baseline = drill.net_position()
    gross = drill.gross_position()
    if gross > 0:
        return DrillResult("latency", False,
                           f"进场前账户上已有 {drill.symbol} 持仓 {gross} 手(净 {baseline} 手),"
                           f"无法区分演练开的仓位,拒绝演练(避免误平既有持仓)")
    drill.gross_baseline = gross      # 进场时该合约总手数为 0,收尾必须回到 0
    req = OrderRequest(symbol=code, exchange=exchange, direction=Direction.LONG, offset=Offset.OPEN,
                       price=buy_price, volume=1, type=OrderType.LIMIT)
    drill.trades.clear()
    drill.positions.clear()
    sent_at = time.monotonic()
    order_id = runtime.main.send_order(req, "CTP")
    # 记下"演练自己开了 1 手多仓":净持仓账户的持仓回报是 Direction.NET,
    # 净额看不出来 —— 收尾要按这个手数去平,否则仓位会留在账户上。
    drill.owned_volume = 1
    drill.owned_direction = Direction.LONG

    trade = drill.wait_for_trade(drill.args.fill_timeout)
    if trade is None:
        # 没成交就立刻撤单:否则这张单会一直挂着,可能几分钟后才成交,
        # 那时候演练已经结束,仓位会被留到下一次运行里(演练污染实盘状态)。
        _cancel_order(runtime, order_id, code, exchange)
        # 撤单是**异步**的:撤单请求发出前后,订单仍然可能成交。
        # 所以"没等到成交回报"不等于"没有仓位" —— 必须再等一段时间,
        # 确认净持仓确实回到了 baseline,才能断言账户干净。
        # 撤单后仍然出现仓位,说明撤单与成交撞上了:仓位留在账户上,
        # 必须平掉并按留仓发 FATAL 告警,不能"没成交"就当作没事发生。
        if not drill.wait_flat(drill.args.close_timeout, baseline=baseline):
            # 留了多少手要看**总手数**:净持仓账户的持仓方向是 NET,
            # 按净额算出来永远是 0,会把"账户还留着 1 手"报成"多出 0 手"。
            leftover = drill.gross_position() - (drill.gross_baseline or 0)
            closed = _close_position(drill, pricetick, baseline=baseline)
            drill.notifier.send("FATAL", "演练未能平仓",
                                 f"{drill.symbol} 撤单后仍然成交,账户多出 {leftover} 手;"
                                 f"{'已在 ' + str(drill.args.close_timeout) + 's 内平掉' if closed else '没能平掉,账户仍留仓'}。"
                                 f"请人工核对账户。")
            return DrillResult("latency", False,
                               f"撤单与成交撞车,账户多出 {leftover} 手"
                               f"({'已平掉' if closed else '没能平掉,仍留仓'}),已发 FATAL 告警",
                               {"撤单后残留手数": leftover, "已平掉": closed})
        drill.owned_volume = 0
        drill.owned_direction = None
        return DrillResult("latency", False,
                           f"{drill.args.fill_timeout:.0f}s 内没有成交(价格 {buy_price}),已撤单并确认账户无仓位")
    trade_ts, trade_data = trade

    # 持仓回报:CTP 对持仓是异步推送的,成交回报与持仓回报之间有一个间隔
    position_ts = None
    deadline = time.monotonic() + drill.args.fill_timeout
    while time.monotonic() < deadline:
        with drill._lock:
            for ts, pos in drill.positions:
                if pos.vt_symbol == drill.symbol and pos.volume > 0:
                    position_ts = ts
                    break
        if position_ts is not None:
            break
        time.sleep(0.05)
    latency = (position_ts - trade_ts) if position_ts else None

    # 收尾:把演练开的仓位平掉,平不掉就大声告警
    closed = _close_position(drill, pricetick, baseline=baseline)

    details = {
        "成交价": buy_price,
        "成交时间": trade_data.datetime if hasattr(trade_data, "datetime") else "",
        "成交→持仓回报延迟(秒)": round(latency, 3) if latency is not None else "未收到持仓回报",
        "平仓成功": closed,
        "进场前净持仓": baseline,
    }
    if latency is None:
        return DrillResult("latency", False, "成交了但没收到持仓回报", details)
    if not closed:
        # 留仓 = 演练在账户上留了 1 手仓。必须让场景判失败、发 FATAL 告警、进程非零退出,
        # 否则"演练通过"却把仓位留在模拟账户里,后面的对账与无人值守证据全部失真。
        drill.notifier.send("FATAL", "演练未能平仓",
                             f"{drill.symbol} 演练开的 1 手仓没有在 {drill.args.close_timeout:.0f}s 内平掉,"
                             f"账户上留了仓位。请人工核对并平仓。")
        return DrillResult("latency", False, f"演练开的仓位没能平掉(留仓 {drill.net_position()} 手),已发 FATAL 告警", details)
    return DrillResult("latency", True, f"成交→持仓回报延迟 {latency:.3f} 秒", details)


def _cancel_order(runtime, order_id: str, symbol: str, exchange: Exchange) -> None:
    """撤掉一张没成交的委托。撤单失败也不该让演练崩掉,只记录。"""
    if not order_id:
        return
    try:
        req = CancelRequest(orderid=order_id, symbol=symbol, exchange=exchange)
        runtime.main.cancel_order(req, "CTP")
    except Exception as exc:      # 撤单只是善后,失败不影响主结论
        print(f"撤单失败({order_id}): {exc!r}", file=sys.stderr)


def _close_position(drill: Drill, pricetick: float, baseline: int = 0) -> bool:
    """平掉**演练自己开的那部分**仓位。返回是否已平回 baseline。

    `baseline` 是演练进场前的账户净持仓(演练之前就存在的仓位)。
    平仓量 = 当前净持仓 − baseline —— 只动演练自己的增量,
    绝不按账户净持仓一刀切去平:那样会把别人(或之前遗留)的仓位一起平掉。

    **净持仓账户(CTP 的"净"持仓方向)例外**:演练开的多仓回报上来是 `Direction.NET`,
    净额算出来是 0 —— 只看净额会以为"没有仓位要平",于是既不发平仓单、
    又把留仓手数误报成 0,仓位留在账户上下次运行还得接着还。
    所以演练记录**自己开了多少、什么方向**(`owned_volume` / `owned_direction`),
    净额看不出来时按这个来平。
    """
    runtime = drill.runtime
    assert runtime is not None
    net = drill.net_position()
    delta = net - baseline
    owned = drill.owned_volume
    if delta == 0 and owned > 0:
        # 净额看不出来(净持仓账户),但演练自己开了仓 → 按自己开的量平。
        # 平仓方向与持仓方向**相反**:开的是多仓就用 SHORT(卖出)平。
        volume = owned
        position_direction = drill.owned_direction or Direction.LONG
        direction = Direction.SHORT if position_direction == Direction.LONG else Direction.LONG
    elif delta != 0:
        # 净持仓为正 = 多头,平仓要用 SHORT(卖出);为负 = 空头,平仓要用 LONG(买入)。
        volume = abs(delta)
        direction = Direction.SHORT if delta > 0 else Direction.LONG
    else:
        # 没有可平的增量。但方向不明的仓位(NET)与多空锁仓都不体现在净额里 ——
        # 再看一次总手数,免得把"还留着仓"当成"已经平干净"。
        return drill.at_baseline(baseline)
    tick_entry = drill.last_tick()
    price = 0.0
    if tick_entry is not None:
        tick = tick_entry[1]
        # 平多(卖出)挂在**买一**(bid_price_1)上,主动打买盘成交;
        # 平空(买入)挂在**卖一**(ask_price_1)上,主动打卖盘成交。
        # 用错一边就会挂在对手价上等不到成交,演练变成"平仓超时"。
        raw = tick.bid_price_1 if direction == Direction.SHORT else tick.ask_price_1
        price = round_to(raw or 0, pricetick)
        if price <= 0:
            price = round_to(tick.last_price, pricetick)
    contract = runtime.main.get_contract(drill.symbol)
    if contract is None:
        return False
    code = drill.symbol.split(".")[0]
    # 上期所/能源中心区分今仓与昨仓:用普通平仓(Offset.CLOSE)会被交易所拒单。
    # 演练开的是**当日**仓,所以要用 CLOSETODAY。
    offset = Offset.CLOSETODAY if contract.exchange in (Exchange.SHFE, Exchange.INE) else Offset.CLOSE
    req = OrderRequest(symbol=code, exchange=contract.exchange, direction=direction,
                       offset=offset, price=price, volume=volume, type=OrderType.LIMIT)
    close_order_id = runtime.main.send_order(req, "CTP")
    flat = drill.wait_flat(drill.args.close_timeout, baseline=baseline)
    if flat:
        drill.owned_volume = 0
        drill.owned_direction = None
    else:
        # 平仓单没成交:它还挂在交易所上,过一会儿成交就会留下一笔"晚到的"持仓,
        # 账户状态与演练结论就不一致了。撤掉它。
        _cancel_order(runtime, close_order_id, code, contract.exchange)
    return flat


def scenario_tick_stale(drill: Drill) -> DrillResult:
    """行情断流 → 看门狗是否告警(运行手册第 5 节第 9 项)。

    做法:等第一根 Tick,然后关掉行情连接,看多久之后收到"行情停更"告警。
    本地止损依赖 Tick 触发,所以这段时间内止损是失效的,必须知道它有多长。
    """
    runtime = drill.runtime
    assert runtime is not None
    tick_entry = drill.wait_for_tick(drill.args.tick_timeout)
    if tick_entry is None:
        return DrillResult("tick-stale", False, f"{drill.args.tick_timeout:.0f}s 内没有收到行情")
    last_ts, _ = tick_entry

    since = drill.notifier.mark()             # 只认本次断流之后的告警
    runtime.gateway.md_api.close()          # 断掉行情
    alert = drill.notifier.wait_for("行情停更", drill.args.stale_timeout, since)
    details = {"最后 Tick 距告警(秒)": None, "看门狗阈值(秒)": drill.args.tick_stale_seconds}
    if alert is None:
        return DrillResult("tick-stale", False,
                           f"断掉行情后 {drill.args.stale_timeout:.0f}s 内没有收到'行情停更'告警", details)
    details["最后 Tick 距告警(秒)"] = round(time.monotonic() - last_ts, 1)
    details["告警"] = f"{alert['level']} {alert['title']}: {alert['message']}"
    return DrillResult("tick-stale", True, "行情断流后收到了看门狗告警", details)


def scenario_disconnect(drill: Drill) -> DrillResult:
    """交易连接断开 → 是否告警、是否自动重连(运行手册第 5 节第 4 项)。

    做法:关掉交易连接,观察有没有"连接断开"告警,以及之后有没有重新"登录成功"。
    这条结论决定实盘要不要自己做重连(CTP 客户端不重连的话,得靠外层守护进程重启)。
    """
    runtime = drill.runtime
    assert runtime is not None
    since = drill.notifier.mark()             # 只认本次断线之后的告警
    runtime.gateway.td_api.close()
    disconnect_alert = drill.notifier.wait_for("连接断开", drill.args.disconnect_timeout, since)
    reconnected = drill.notifier.wait_for("登录成功", drill.args.reconnect_timeout, since)

    def fmt_state() -> str:
        state = getattr(runtime, "connection_state", None)
        if not state:
            return "不可用"
        return "、".join(f"{ch}:{'已连接' if ok else '断开'}" for ch, ok in sorted(state.items()))

    # 断连期间若策略恰好发单,CTP 本地快速失败并写「委托请求发送失败」(经网关错误告警露出)。
    # 框架不缓存订单,重连后无从补发;确定性语义由离线测试 tests/test_disconnect_signal.py 锁定,
    # 这里只记录现场旁证:窗口内出现了几条发单失败告警。
    send_failures = drill.notifier.find("委托请求发送失败", since)
    details = {
        "断线告警": (f"{disconnect_alert['level']} {disconnect_alert['title']}: {disconnect_alert['message']}"
                    if disconnect_alert else "无"),
        "自动重连": "是" if reconnected else "否",
        "重连等待(秒)": drill.args.reconnect_timeout,
        "重连后连接状态": fmt_state(),
        "窗口内发单失败告警(条)": len(send_failures),
        "信号积压补发": "无补发:断连发单本地失败、框架不排队(离线测试 test_disconnect_signal 锁定语义)",
    }
    if disconnect_alert is None:
        return DrillResult("disconnect", False, "断开交易连接后没有收到告警", details)
    if reconnected is None:
        return DrillResult("disconnect", True, "有断线告警,但观察窗口内没有自动重连", details)
    return DrillResult("disconnect", True, "有断线告警,并且自动重连成功", details)


class _InjectedAccount:
    """注入的账户视图:余额由外部控制,账户号沿用真实账户(避免触发"账户变更"告警)。"""

    def __init__(self, accountid: str, holder: dict) -> None:
        self.accountid = accountid
        self._holder = holder

    @property
    def balance(self) -> float:
        return self._holder["balance"]


def inject_equity(drill: Drill, balance: float) -> float:
    """把账户权益替换成一个指定值(模拟亏损/回撤),返回真实的原权益。

    只改**这个演练进程里**的权益来源:守卫判定、撤单、告警、状态落盘全都照常跑,
    但真实账户的权益不受任何影响,下一次行情刷新就恢复原值。
    """
    rule = drill.runtime.rule
    real = rule._account()
    holder = {"balance": balance}
    rule._account = lambda: _InjectedAccount(real.accountid if real else "", holder)
    return float(real.balance) if real else 0.0


def restore_equity(drill: Drill) -> None:
    """把权益来源换回真实的账户查询。"""
    rule = drill.runtime.rule
    rule._account = type(rule)._account.__get__(rule)


def scenario_daily_loss(drill: Drill) -> DrillResult:
    """当日熔断链路(运行手册第 5 节第 6 项)。

    做法:把账户权益"注入"一个比开盘基准低 daily_loss_limit 以上的值(模拟亏损),
    其余全是真实的:守卫判定、撤单、告警、状态落盘都是真的在跑。
    注意注入的权益只存在于这个演练进程里,不会改动真实账户。
    """
    runtime = drill.runtime
    assert runtime is not None
    rule = runtime.rule
    limit = float(rule.daily_loss_limit)
    real_account = rule._account()
    real_balance = float(real_account.balance) if real_account else 0.0
    injected = max(real_balance - limit - 100.0, 1.0)

    # 先打标记再注入权益:守卫的定时器线程随时可能在注入的瞬间就发出告警,
    # 顺序反了会让这条告警落在标记之前,被 since 过滤掉 → 假阴性。
    since = drill.notifier.mark()
    balance = inject_equity(drill, injected)
    try:
        alert = drill.notifier.wait_for("当日熔断", drill.args.lock_timeout, since)
        locked = rule.guard.daily_locked_day is not None
        details = {
            "注入权益": injected,
            "真实权益": balance,
            "当日熔断阈值(元)": limit,
            "锁定状态": rule.lock_status,
            "告警": alert["message"] if alert else "无",
        }
    finally:
        restore_equity(drill)
    if alert is None or not locked:
        return DrillResult("daily-loss", False, "注入亏损后没有触发当日熔断", details)
    return DrillResult("daily-loss", True, "当日熔断触发,撤单与告警链路正常", details)


def scenario_hard_lock(drill: Drill) -> DrillResult:
    """累计死线(硬锁定)链路(运行手册第 5 节第 7 项)。

    做法:注入一个比权益最高点低 max_drawdown 以上的权益 → 硬锁定 → 平仓 → 退出码 0。
    重启后"硬锁定生效中,拒绝启动"由另一次演练验证(硬锁定会写进演练的风控状态文件)。
    """
    runtime = drill.runtime
    assert runtime is not None
    rule = runtime.rule
    limit = float(rule.max_drawdown)
    peak = float(rule.guard.peak_equity or 0.0)
    real = rule._account()
    balance = float(real.balance) if real else 0.0
    base = max(peak, balance)
    injected = max(base - limit - 100.0, 1.0)

    # 先打标记再注入权益:守卫的定时器线程随时可能在注入的瞬间就发出告警,
    # 顺序反了会让这条告警落在标记之前,被 since 过滤掉 → 假阴性。
    since = drill.notifier.mark()
    inject_equity(drill, injected)
    try:
        alert = drill.notifier.wait_for("累计死线", drill.args.lock_timeout, since)
        # 硬锁定 → 平仓 → 运行时请求停止(退出码 0)。等待退出请求。
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and runtime.exit_code is None:
            time.sleep(0.2)
    finally:
        # 无论是否触发成功,都把权益来源换回真实账户
        restore_equity(drill)
    details = {
        "注入权益": injected,
        "权益最高点": base,
        "累计死线(元)": limit,
        "退出码": runtime.exit_code,
        "告警": alert["message"] if alert else "无",
    }
    if alert is None:
        return DrillResult("hard-lock", False, "注入回撤后没有触发硬锁定", details)
    if runtime.exit_code != EXIT_OK:
        return DrillResult("hard-lock", False, f"硬锁定后退出码是 {runtime.exit_code}(期望 0)", details)
    # 重启拒绝交易:硬锁定必须落盘,下次启动时 runtime.start() 才会拒绝进场。
    # 这里直接读演练的风控状态文件确认已持久化 —— 不落盘的话,"重启拒绝交易"只是文档里的说法。
    state_path = getattr(rule.guard, "state_path", None) or (DRILL_ROOT / "account_guard.json")
    details["状态文件"] = str(state_path)
    details["重启后拒绝交易"] = _hard_lock_persisted(state_path)
    if not details["重启后拒绝交易"]:
        return DrillResult("hard-lock", False, "硬锁定没有写进状态文件,重启后不会拒绝交易", details)
    return DrillResult("hard-lock", True, "硬锁定触发,平仓后以退出码 0 停止", details)


def _hard_lock_persisted(path=None) -> bool:
    """演练的风控状态文件里是否已经记下硬锁定。"""
    path = path or (DRILL_ROOT / "account_guard.json")
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(state.get("hard_locked"))


def scenario_overnight(drill: Drill) -> DrillResult:
    """夜盘首根 Tick 与交易日归属(运行手册第 5 节第 8 项)。

    做法:等夜盘的第一根 Tick,记录它的时刻与按 `trading_day()` 算出的交易日归属。
    夜盘的第一根 Tick 属于**下一个交易日**(周一凌晨的夜盘属于周二),这里一并核对。
    """
    tick_entry = drill.wait_for_tick(drill.args.overnight_timeout)
    if tick_entry is None:
        return DrillResult("overnight", False,
                           f"{drill.args.overnight_timeout / 60:.0f} 分钟内没有等到夜盘第一根 Tick")
    _, tick = tick_entry
    dt = tick.datetime
    day = trading_day(dt)
    expected = "下一个自然日" if dt.hour >= 21 else "当日"
    details = {
        "首根 Tick 时刻": dt.isoformat(timespec="seconds"),
        "归属交易日": day.isoformat(),
        "说明": f"21:00 之后的 Tick 属于下一交易日(此处为{expected})",
    }
    return DrillResult("overnight", True, f"夜盘首根 Tick {dt.isoformat(timespec='seconds')},归属交易日 {day}", details)


# ---------------- 输出 ----------------


def _drain_budget(notifier) -> float:
    """等待后台线程把告警发完所需的上限。

    通知器自己会重试:最坏情况 = (1 + 重试次数) × 单次超时 + 全部退避时间,
    这是**单条**消息的上限;队列里排着 N 条时要乘 N(消息是串行发的)。
    所以优先用通知器的 `drain_timeout`(它知道队列里还有几条),
    没有这个属性时退回单条上限。
    close() 短于这个上限就返回,hard-lock 的 FATAL 告警会随进程退出一起丢掉 ——
    而"硬锁定要告警到手机"恰恰是这个场景的验收点。
    """
    inner = getattr(notifier, "inner", notifier)
    for attr in ("drain_timeout", "flush_timeout"):
        value = getattr(inner, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return 35.0


def render_results(results: list[DrillResult]) -> str:
    lines = ["| 场景 | 结论 | 明细 |", "|---|---|---|"]
    for r in results:
        detail = "; ".join(f"{k}={v}" for k, v in r.details.items())
        mark = "⏭" if r.skipped else ("✅" if r.ok else "❌")
        lines.append(f"| {r.name} | {mark} {r.summary} | {detail} |")
    return "\n".join(lines)


def write_results(out: Path, results: list[DrillResult], stamp: str,
                  warnings: list[str] | None = None) -> None:
    """把结果写进 Markdown(只替换标记之间的内容,人工段落保留)。"""
    body = f"> 采集时间:{stamp}\n\n"
    for w in warnings or []:
        body += f"> ⚠ {w}\n"
    if warnings:
        body += "\n"
    body += render_results(results)
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        title = ("# SimNow 实测演练记录\n\n"
                 "> 本文件由 `scripts/drill.py` 生成,标记之间的内容会被覆盖;"
                 "结论与解读写在标记之外。\n\n")
        out.write_text(f"{title}{BEGIN_MARK}\n{body}\n{END_MARK}\n", encoding="utf-8")
        return
    old = out.read_text(encoding="utf-8")
    begin, end = old.find(BEGIN_MARK), old.find(END_MARK)
    if begin < 0 or end < 0:
        out.with_name(out.stem + "-raw.md").write_text(body, encoding="utf-8")
        return
    # 标记必须**原样写回去**:只写 body 的话 END_MARK 会消失,
    # 下一次运行 old.find(END_MARK) 返回 -1 → 主演练记录从此不再更新,只剩 -raw.md。
    out.write_text(old[:begin] + BEGIN_MARK + "\n" + body + "\n" + END_MARK
                   + old[end + len(END_MARK):], encoding="utf-8")


def default_out_path() -> Path:
    """默认的输出文件。

    容器镜像里**没有** `docs/`(Dockerfile 只 COPY 了代码目录),所以在容器里跑演练时
    `docs/simnow-drills.md` 写不进去。容器里 `data/` 是挂载出来的卷(`/app/data` → 宿主机 `runtime/data`),
    退到那里,跑完再拷回仓库的 `docs/`。
    """
    docs = bootstrap.ROOT / "docs"
    if docs.is_dir():
        return docs / "simnow-drills.md"
    fallback = bootstrap.ROOT / "data" / "simnow-drills.md"
    print(f"容器内没有 docs/ 目录,结果改写到 {fallback}(容器退出后拷回仓库的 docs/simnow-drills.md)",
          file=sys.stderr)
    return fallback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenario", nargs="?", default="all",
                        help="演练场景:all 或 " + "/".join(SCENARIOS))
    parser.add_argument("--list", action="store_true", help="列出所有场景")
    parser.add_argument("--symbol", default="", help="合约,如 c2701.DCE(默认取 .env 的 LIVE_VT_SYMBOL)")
    parser.add_argument("--out", default="",
                        help="结果写入的 Markdown 文件(默认:仓库 docs/simnow-drills.md;容器内退到 data/)")
    parser.add_argument("--connect-timeout", type=float, default=300.0, help="等待合约与账户到位的最长时间(秒)")
    parser.add_argument("--tick-timeout", type=float, default=120.0, help="等待第一根 Tick 的最长时间(秒)")
    parser.add_argument("--fill-timeout", type=float, default=30.0, help="等待成交/持仓回报的最长时间(秒)")
    parser.add_argument("--close-timeout", type=float, default=60.0, help="等待平仓的最长时间(秒)")
    parser.add_argument("--tick-stale-seconds", type=float, default=60.0, help="看门狗的行情停更阈值(秒)")
    parser.add_argument("--stale-timeout", type=float, default=180.0, help="等待'行情停更'告警的最长时间(秒)")
    parser.add_argument("--reconnect-timeout", type=float, default=90.0, help="观察自动重连的窗口(秒)")
    parser.add_argument("--disconnect-timeout", type=float, default=60.0, help="等待断线告警的最长时间(秒)")
    parser.add_argument("--lock-timeout", type=float, default=30.0, help="等待熔断/硬锁定告警的最长时间(秒)")
    parser.add_argument("--overnight-timeout", type=float, default=3600.0, help="等待夜盘首根 Tick 的最长时间(秒)")
    parser.add_argument("--confirm-simnow", action="store_true", help="确认目标是 SimNow 模拟盘")
    parser.add_argument("--allow-any-address", action="store_true",
                        help="⚠ 危险:交易服务器地址不在已知 SimNow 地址表里时强制放行,可能连上真实资金账户")
    parser.add_argument("--confirm-any-address-trades", action="store_true",
                        help="⚠⚠ 危险:配合 --allow-any-address,允许在非白名单地址上执行会真实下单的场景")
    args = parser.parse_args(argv)

    # `drill.py list` 与 `drill.py --list` 都认:运行手册里写的是前者。
    if args.list or args.scenario == "list":
        for name, desc in SCENARIOS.items():
            print(f"{name:12s} {desc}")
        return 0

    bootstrap.load_env()
    env = dict(os.environ)

    symbol = args.symbol or env.get("LIVE_VT_SYMBOL", "")
    if not symbol:
        print("未指定合约:--symbol 或 .env 的 LIVE_VT_SYMBOL", file=sys.stderr)
        return 2
    if not args.confirm_simnow:
        print("本脚本只在**模拟盘**上跑。确认是 SimNow 模拟盘请加 --confirm-simnow。", file=sys.stderr)
        return 2

    try:
        gateway_setting = build_ctp_setting(env)
    except ValueError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    from ctp_tools.simnow import SIMNOW_TD_ADDRESSES, check_simnow_address, normalize_address

    td_address = normalize_address(gateway_setting["交易服务器"])
    allowed, note = check_simnow_address(td_address, args.allow_any_address)
    if not allowed:
        print(note, file=sys.stderr)
        return 2
    print(f"目标交易服务器 {td_address},行情服务器 {gateway_setting['行情服务器']}。")
    if td_address not in SIMNOW_TD_ADDRESSES:
        print(note, file=sys.stderr)

    names = list(ORDER) if args.scenario == "all" else [args.scenario]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"未知场景 {unknown};可用:all 或 {list(SCENARIOS)}", file=sys.stderr)
        return 2

    # 逃生舱的强度:地址不在白名单时,--allow-any-address 只是"我确认过"的一次声明。
    # 会真的下单/撤单/平仓的场景(latency 开平仓;daily-loss 与 hard-lock 触发运行时撤单与强平)
    # 后果是真金白银,所以对这类场景再加一道开关,并把警告写进结果文件当审计记录。
    risky = risky_scenarios(names, td_address)
    if risky and not args.confirm_any_address_trades:
        print(f"目标地址不在 SimNow 白名单内,而场景 {risky} 会真的发委托。"
              f"如确认这个环境安全(不会连到真实资金账户),再加 --confirm-any-address-trades。", file=sys.stderr)
        return 2

    from notify.webhook import WebhookNotifier
    from run_headless import build_notifier

    notifier = RecordingNotifier(build_notifier(env))
    drill = Drill(args, env, symbol, notifier)
    results: list[DrillResult] = []
    # 审计记录:用了逃生舱(目标地址不在 SimNow 白名单)必须留在结果文件里,不能只打在终端上
    warnings: list[str] = []
    if td_address not in SIMNOW_TD_ADDRESSES:
        warnings.append(f"目标交易服务器 {td_address} **不在**已知的 SimNow 地址表内,"
                        f"本次由 --allow-any-address 强制放行。"
                        f"若该地址属于期货公司生产 API,本次演练的委托会发到**真实资金账户**。")
    try:
        if not drill.start():
            print("启动失败:没有进入运行状态(见上方告警)", file=sys.stderr)
            return 3
        for name in names:
            func: Callable[[Drill], DrillResult] = SCENE_FUNCS[name]
            print(f"\n=== 演练:{name}({SCENARIOS[name]})===")
            allowed, why = scenario_precondition(name)
            if not allowed:
                result = DrillResult(name, True, f"跳过:{why}", {"前提": "不满足"}, skipped=True)
            else:
                try:
                    result = func(drill)
                except Exception as exc:      # 单个场景失败不影响其它场景
                    result = DrillResult(name, False, f"场景执行异常:{exc!r}")
            results.append(result)
            mark = "⏭" if result.skipped else ("✅" if result.ok else "❌")
            print(f"{mark} {name}: {result.summary}")
            for k, v in result.details.items():
                print(f"    {k}: {v}")
            if name == "hard-lock":
                break       # 硬锁定会让运行时退出,后面的场景不再有意义
    finally:
        drill.stop()
        # 等待要够长,后台线程才可能把队列里的告警发完(尤其 hard-lock 的 FATAL)。
        # 默认的 10 秒短于最坏情况(超时×重试次数 + 退避),告警会随进程退出一起丢掉。
        notifier.close(drain_timeout=_drain_budget(notifier))

    # 告警"发出"与"送达"是两件事:底层拒绝(去重/过滤/队列满)时手机端并没有响。
    # 渠道压根没配时这不算"丢告警",但也不能算"验证过送达"。
    delivery = notifier.delivery_summary()
    if not delivery["通知渠道已配置"]:
        warnings.append("通知渠道未配置(WEBHOOK_URL 为空):本次只验证了"
                        "「运行时确实发出了告警」,没有验证「告警送达手机」。")
    elif delivery["未送达"]:
        warnings.append(f"有 {delivery['未送达']} 条告警没有被通知器接受(去重/过滤/队列满):"
                        f"{'、'.join(delivery['未送达明细'][:5])}。")

    stamp = datetime.now().isoformat(timespec="seconds")
    out = Path(args.out) if args.out else default_out_path()
    write_results(out, results, stamp, warnings=warnings)
    print(f"\n结果已写入 {out}")
    print(render_results(results))
    return 0 if all(r.ok for r in results) else 1


SCENE_FUNCS: dict[str, Callable[[Drill], DrillResult]] = {
    "latency": scenario_latency,
    "tick-stale": scenario_tick_stale,
    "disconnect": scenario_disconnect,
    "daily-loss": scenario_daily_loss,
    "hard-lock": scenario_hard_lock,
    "overnight": scenario_overnight,
}


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
        os._exit(code)
