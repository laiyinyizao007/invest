"""交易运行时:把网关、风控、CTA 引擎与通知串成一条可无人值守运行的链路。

启动顺序(任何一步失败都不会进入交易):
1. 硬锁定检查:状态文件里有累计死线的硬锁定 → 告警后直接退出,不连接、不交易;
2. 连接网关,等待合约与账户到位(超时 → 启动失败);再主动查询持仓并等待其稳定;
3. 初始化策略并**强制停止**(引擎可能把保存的 trading=True 恢复出来,绕过下面的核对);
4. 对账:启动前已存在的活跃委托先撤销;账户里不属于本策略合约的持仓、策略恢复出的持仓与账户净持仓不一致、
   策略状态自相矛盾,三者任一成立都**拒绝启动**并告警,因为带着错账本交易比不交易更危险;
5. 启动策略。

运行中的看门狗:
- 策略被引擎异常停止(CtaEngine 捕获策略异常后只写一条日志):其本地止损随之失效,立即告警并紧急平仓;
- 行情停更(假死):本地止损依赖 Tick 触发,交易时段内长时间没有 Tick 要告警;首次越限还会强撤全部
  活动**开仓**委托(平仓单与紧急平仓单不动),新 Tick 到达后复位,可再次边沿触发,可用配置开关回退为只告警;
- 心跳:定时汇报存活、权益与持仓。

退出:硬锁定且已确认空仓 → 正常退出(退出码 0);硬锁定但平仓失败 → 进程保持存活并持续告警
(vn.py 的止损单是本地触发的,带着持仓杀进程会让持仓失去保护);退出时若仍有持仓会发 FATAL 告警。
"""
import threading
import time
from collections.abc import Callable
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG, EVENT_TICK, EVENT_TIMER, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.logger import ERROR
from vnpy.trader.utility import get_file_path
from vnpy_ctastrategy import CtaStrategyApp

from risk import approved_guard
from risk.positions import describe, signed_net, undirected
from strategies.trend_atr_strategy import ENTRY_SESSIONS, TrendAtrStrategy
from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK
from trading.graceful_exit import logout_ctp
from trading.guard_rule import ACCOUNT_RULE_NAME, GATEWAY_ERROR_MARKERS
from trading.safe_risk import SafeRiskManagerApp

EXIT_OK = 0
# 启动里程碑:名称取自 vnpy_ctp 的日志文本,升级 vnpy_ctp 时措辞若变,这里要同步。
MILESTONES = ("行情服务器登录成功", "交易服务器登录成功", "结算信息确认成功", "合约信息查询成功")
# 连接状态机的精确措辞(升级 vnpy_ctp 时措辞若变,这里要同步)。
# 关键:恢复只认「登录成功」。重连过程中还有「连接成功」但尚未认证登录的中间日志,
# 拿它当恢复凭据会让告警在「连上→未登录→再断」之间抖动。
DISCONNECT_MARKERS = {"交易服务器连接断开": "交易", "行情服务器连接断开": "行情"}
CONNECTED_MARKERS = {"交易服务器登录成功": "交易", "行情服务器登录成功": "行情"}
EXIT_STARTUP_FAILED = 3
EXIT_RECONCILE_REFUSED = 4


class Notifier(Protocol):
    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool: ...

    def close(self, drain_timeout: float = ...) -> None: ...


class NullNotifier:
    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool:
        return False

    def close(self, drain_timeout: float = 0.0) -> None:
        pass


@dataclass
class RuntimeConfig:
    vt_symbol: str                                   # 实盘交易的具体合约,如 c2701.DCE
    strategy_class: str = "TrendAtrStrategy"
    strategy_name: str = "trend_atr"
    strategy_setting: dict = field(default_factory=dict)
    gateway_name: str = "CTP"
    gateway_setting: dict = field(default_factory=dict)
    connect_timeout: float = 300.0                   # 等待合约与账户到位的最长时间(秒);网关登录后会下载全市场合约表(含期权),SimNow 上实测 20 秒~3 分钟以上
    position_settle_seconds: float = 5.0             # 账户到位后主动查询持仓并等待其稳定的时间(秒)
    init_timeout: float = 120.0
    heartbeat_seconds: float = 0.0                   # 0 = 不发心跳
    tick_stale_seconds: float = 60.0                 # 交易时段内超过该时间没有 Tick 就告警
    tick_stale_realert_seconds: float = 300.0
    # 假死处置开关:首次越过 tick_stale_seconds 时,除告警外是否强撤全部活动**开仓**委托
    # (平仓单/紧急平仓单不受影响)。置 False 回退为「只告警」的旧行为。
    stale_cancel_open_orders: bool = True
    drift_tolerance_seconds: float = 15.0            # 策略持仓与账户持仓不一致持续超过该时间 → 视为漂移,告警并紧急平仓
    heartbeat_file: Path | None = None               # 运行中每 10 秒刷新一次,供容器健康检查判断进程是否真的在工作

    # ---- 批准目录装载(阶段四 4c:approved/ 是运行时唯一认的目录)----
    # 全部为 None 时行为与从前**完全一致**(仍从 strategies/ 装载受信骨架)。
    # 一旦给了 approved_file,策略就从 approved/ 装载,且必须通过批准账本与宪法的复核,
    # 否则拒绝启动(EXIT_STARTUP_FAILED)——**不回退**到默认策略。
    approved_file: str | None = None                 # approved/ 里的文件名,如 "my_candidate.py"
    approved_dir: Path | None = None                 # 批准目录;None 时取 <approved_root>/approved(位置**钉死**,不可改指)
    approved_root: Path | None = None                # 批准目录与账本的共同根;None 时取本文件所在代码根
    approved_class_in_file: str = "Candidate"        # 批准文件里的类名(与 strategy_class 是两回事:那是引擎类表的键)
    approved_strategy_name: str | None = None        # 状态命名空间;None 时按批准哈希派生(换版本自动换命名空间)
    approved_template: str | None = None             # 骨架(模块:类);None 时用宪法指定的默认骨架
                                                      # ——默认那个是**仅研究回测**的 15m 骨架,会被拒绝装载,
                                                      # 实盘要用日线骨架:strategies.trend_atr_strategy:TrendAtrStrategy

    def __post_init__(self) -> None:
        # 等持仓回报稳定的时间设下限:配成 0 时,两次"未到回报的空快照"会被当成稳定而通过对账
        self.position_settle_seconds = max(self.position_settle_seconds, 2.0)


class TradingRuntime:
    def __init__(
        self,
        config: RuntimeConfig,
        gateway_class: type[BaseGateway],
        notifier: Notifier | None = None,
        event_engine: EventEngine | None = None,
        strategy_classes: dict[str, type] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.notifier: Notifier = notifier or NullNotifier()
        self._sleep, self._mono, self._now = sleep, monotonic, now

        self.event_engine = event_engine or EventEngine()
        self.main = MainEngine(self.event_engine)
        self.gateway: BaseGateway = self.main.add_gateway(gateway_class, config.gateway_name)
        self.risk = self.main.add_app(SafeRiskManagerApp)
        self.cta = self.main.add_app(CtaStrategyApp)
        self.rule = self.risk.rules[ACCOUNT_RULE_NAME]
        # 把交易时段判定注入规则:规则自身的 120s 超时撤单据此剔除午休/夜盘休市时间;
        # 同时同步时段基线,与运行时自身的 _in_session 保持同一份初始认知(单一判定函数,不会发散)
        self.rule.session_active = self._in_trading_session
        self.rule._was_in_session = self._in_trading_session()
        self.cta.classes = strategy_classes or {"TrendAtrStrategy": TrendAtrStrategy}
        self.cta.load_strategy_data()                # 恢复上次保存的策略状态(持仓/止损);策略配置每次启动都由我们给定
        self.cta.register_event()

        self.exit_code: int | None = None
        self.strategy = None
        self._stop_event = threading.Event()
        self._shutdown_done = False
        self._started = False
        self._crash_reported = False
        self._last_tick: float | None = None
        self._t_connect: float | None = None
        self._milestones: dict[str, float] = {}
        # 通道 → 是否已连接;键缺失表示尚未见过任何连接事件(未知),True/False 为已知状态
        self._channel_connected: dict[str, bool] = {}
        self._last_stale_alert = -1e9
        self._stale_cancel_fired = False              # 假死撤单边沿标志:新 Tick 到达后复位
        self._in_session: bool | None = None          # 上一次定时器看到的时段状态;None = 尚未观测过
        self._last_heartbeat = self._mono()
        self._last_touch = -1e9
        self._drift_since: float | None = None
        self._drift_reported = False

        self.event_engine.register(EVENT_GUARD_ALERT, self._on_guard_alert)
        self.event_engine.register(EVENT_GUARD_HARD_LOCK, self._on_hard_lock)
        self.event_engine.register(EVENT_TRADE, self._on_trade)
        self.event_engine.register(EVENT_LOG, self._on_log)
        self.event_engine.register(EVENT_TICK, self._on_tick)
        self.event_engine.register(EVENT_TIMER, self._on_timer)

    # ---------- 通知 ----------

    def notify(self, level: str, title: str, message: str, extra: dict | None = None) -> None:
        self.main.write_log(f"[{level}] {title}: {message}", source="Runtime")
        try:
            self.notifier.send(level, title, message, extra)
        except Exception as exc:   # 通知失败绝不能影响交易
            self.main.write_log(f"通知发送异常: {exc!r}", source="Runtime")

    def _on_guard_alert(self, event: Event) -> None:
        a = event.data
        try:
            self.notifier.send(a["level"], a["title"], a["message"])
        except Exception:
            pass

    def _on_trade(self, event: Event) -> None:
        t = event.data
        self.notify("INFO", "成交", f"{t.vt_symbol} {t.direction.value}{t.offset.value} {t.volume}@{t.price}")

    def _on_log(self, event: Event) -> None:
        log = event.data
        if log.gateway_name != self.config.gateway_name:
            return
        msg = log.msg
        if self._t_connect is not None:
            for m in MILESTONES:
                if m in msg:
                    self._milestones.setdefault(m, self._mono() - self._t_connect)
        channel_off = next((ch for phrase, ch in DISCONNECT_MARKERS.items() if phrase in msg), None)
        channel_on = next((ch for phrase, ch in CONNECTED_MARKERS.items() if phrase in msg), None)
        if channel_off is not None:
            # 边沿通知:断连后 CTP 可能反复写同一条断开日志,状态未变就不重复告警
            if self._channel_connected.get(channel_off) is not False:
                self._channel_connected[channel_off] = False
                self.notify("ERROR", "网关连接断开", f"{channel_off}服务器:{msg}")
        elif channel_on is not None:
            # 交易/行情两条通道各自迁移、各自通知一次;「连接成功」但未登录的中间态不在标记表里,不驱动状态
            if self._channel_connected.get(channel_on) is not True:
                self._channel_connected[channel_on] = True
                self.notify("INFO", "网关已连接", f"{channel_on}服务器:{msg}")
        elif log.level >= ERROR or any(k in msg for k in GATEWAY_ERROR_MARKERS):
            # CTP 的拒单是以 INFO 级别写的(vnpy_ctp 的 write_error 实际调用 write_log),
            # 只看 level 会漏掉"交易委托失败,代码：21,信息：..."这类关键日志。
            # 这里用宽措辞:只是给人看的告警,分类错误没有后果,所以"登录失败""订阅失败"也一并提示。
            self.notify("WARN", "网关错误", msg)

    def _on_tick(self, event: Event) -> None:
        if event.data.vt_symbol == self.config.vt_symbol:
            self._last_tick = self._mono()
            self._stale_cancel_fired = False          # 行情恢复:复位假死边沿,允许下一次再触发

    @property
    def connection_state(self) -> dict[str, bool]:
        """交易/行情通道当前连接状态的只读副本(如 {"交易": False, "行情": True});未见过事件的通道不出现。"""
        return dict(self._channel_connected)

    # ---------- 启动 ----------

    def start(self) -> int | None:
        """返回 None 表示已进入运行状态;返回整数表示拒绝运行(该值即进程退出码)。"""
        if self.rule.guard.hard_locked:
            self.notify("FATAL", "硬锁定生效中,不启动",
                        f"{self.rule.guard.hard_reason}。请人工核对账户后,用 scripts/unlock_account.py 解锁。")
            return EXIT_OK
        problem = self._check_state_writable()
        if problem:
            self.notify("FATAL", "启动失败", problem)
            return EXIT_STARTUP_FAILED
        # 批准目录装载**先于**连网关:策略没过批准复核就不该登录 CTP、更不该下单。
        problem = self._load_approved_strategy()
        if problem:
            self.notify("FATAL", "策略装载被拒,拒绝启动", problem)
            return EXIT_STARTUP_FAILED
        self._t_connect = self._mono()
        try:
            self.main.connect(self.config.gateway_setting, self.config.gateway_name)
        except Exception as exc:
            self.notify("FATAL", "网关连接失败", repr(exc))
            return EXIT_STARTUP_FAILED
        if not self._wait_for(self._contract_and_account_ready, self.config.connect_timeout):
            if self._stop_event.is_set():
                return EXIT_OK
            self.notify("FATAL", "启动失败", f"{self.config.connect_timeout:.0f}s 内未收到合约 {self.config.vt_symbol} 或账户信息(网关登录后要先下载全市场合约表,前置机繁忙时会很慢;账户查询要等合约表完成)。已完成: {self._milestone_report()}")
            return EXIT_STARTUP_FAILED
        self.main.write_log(f"启动耗时(自发起连接): {self._milestone_report()}, 合约与账户到位={self._mono() - self._t_connect:.1f}s", source="Runtime")
        if not self._stable_positions():
            if self._stop_event.is_set():
                return EXIT_OK
            self.notify("FATAL", "对账失败,拒绝启动", "持仓回报反复变化,无法确定账户真实持仓。请人工核对账户")
            return EXIT_RECONCILE_REFUSED

        refused = self._init_and_reconcile()
        if refused is not None:
            return refused
        if self._stop_event.is_set():
            return EXIT_OK
        self.cta.start_strategy(self.config.strategy_name)
        self._started = True
        self._last_tick = None
        self._in_session = self._in_trading_session()       # 初始基线:首个定时器不应被当成时段边沿
        self._last_heartbeat = self._mono()
        self.notify("INFO", "已启动", f"{self.config.strategy_name} 运行于 {self.config.vt_symbol},权益 {self._equity():.0f}")
        return None

    def _milestone_report(self) -> str:
        """启动各阶段距离发起连接的秒数,用于判断慢在哪一步(登录/结算确认/全市场合约表下载)。

        里程碑由事件线程写入,这里读取时先复制一份快照,避免遍历时字典被并发改动。
        """
        snapshot = dict(self._milestones)
        if not snapshot:
            return "无任何里程碑"
        return ", ".join(f"{k}={v:.1f}s" for k, v in sorted(snapshot.items(), key=lambda kv: kv[1]))

    def _load_approved_strategy(self) -> str | None:
        """从 `approved/` 装载策略(配置了 approved_file 时)。

        返回 None 表示成功(或本次没要求装载);返回一段说明表示**拒绝**。
        拒绝的原因只有一种:这份代码凭什么有权被执行?答案必须是「人批准过这段字节」。
        校验不过就拒绝启动,**绝不**回退到 `strategies/` 里的默认策略——
        回退等于「装不上就换一个照样跑」,那批准就白批了。
        """
        cfg = self.config
        if not cfg.approved_file:
            return None
        root = Path(cfg.approved_root) if cfg.approved_root else Path(__file__).resolve().parents[1]
        approved_dir = Path(cfg.approved_dir) if cfg.approved_dir else root / "approved"
        # 批准目录的位置**钉死**:解析后必须就是 `<root>/approved`。
        # 把它配成别处(尤其 `strategies/`)等于「候选目录与受信目录混为一谈」——
        # 研究侧用同一份受保护目录名单把这条堵死(见 research_config.RESERVED_DIRS)。
        if approved_dir.resolve() != (root / "approved").resolve():
            return (f"批准目录必须固定为 {root / 'approved'},当前是 {approved_dir};"
                    f"把它指到别处会让候选目录与运行时认的目录混为一谈")
        approvals_file = root / "data" / "research-control" / "approvals.json"
        # 批准目录整体自检:里面有任何一个文件与账本不一致,就拒绝启动(fail-closed)。
        problems = approved_guard.check_approved_consistency(
            approved_dir=approved_dir, approvals_file=approvals_file)
        if problems:
            return ("批准目录与批准账本不一致,拒绝启动:" + "; ".join(problems)
                    + "。请重新收菜(approved/ 里的文件被改过或从未批准)")
        try:
            cls, sha = approved_guard.load_approved_strategy(
                approved_dir=approved_dir,
                approvals_file=approvals_file,
                file_name=cfg.approved_file,
                class_name=cfg.approved_class_in_file,
                setting=cfg.strategy_setting,
                template_name=cfg.approved_template,
            )
        except approved_guard.ApprovedLoadError as exc:
            return str(exc)
        # 状态命名空间按**批准哈希**派生:换批准版本就换命名空间,
        # 旧版本的持仓/止损状态不会被新版本继承(不靠操作者记得改 --strategy-name)。
        if not cfg.approved_strategy_name:
            cfg.strategy_name = f"approved_{sha[:8]}"
        # 注册进 CTA 引擎的类表:键名沿用 strategy_class,引擎按名字取类
        self.cta.classes[cfg.strategy_class] = cls
        self.main.write_log(
            f"已从批准目录装载策略: {cfg.approved_file} -> {cfg.strategy_class}"
            f"(状态命名空间 {cfg.strategy_name})", source="Runtime")
        return None

    def _check_state_writable(self) -> str | None:
        """状态目录必须可写,否则风控状态(尤其是硬锁定)写不进磁盘,重启后会被"洗掉"。Docker 挂载卷默认属 root,容器内 uid 1000 写不进。"""
        dirs = []
        if self.rule.guard.state_path is not None:
            dirs.append(self.rule.guard.state_path.parent)
        if self.config.heartbeat_file is not None:
            dirs.append(self.config.heartbeat_file.parent)
        # CTA 策略数据文件(持仓/止损状态)所在目录:若不可写,首笔成交后 sync_data 抛异常 → 引擎停掉策略 → 看门狗盘中强制平仓
        dirs.append(get_file_path(self.cta.data_filename).parent)
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write_test"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink()
            except OSError as exc:
                return (f"状态目录不可写: {d} ({exc})。Docker 部署请先 `mkdir -p runtime/vntrader runtime/data "
                        "&& chown -R 1000:1000 runtime`")
        return None

    def _position_snapshot(self) -> list[tuple]:
        return sorted((p.vt_symbol, p.direction.value, p.volume) for p in self.main.get_all_positions() if p.volume > 0)

    def _stable_positions(self, rounds: int = 3) -> bool:
        """主动查询持仓,直到连续两次快照一致。CTP 的持仓回报可能迟到,不能只等一个固定时间就对账。"""
        half = max(self.config.position_settle_seconds / 2, 0.0)
        previous = None
        for _ in range(rounds + 1):
            self.gateway.query_position()
            self._wait_for(lambda: False, half)
            snapshot = self._position_snapshot()
            if snapshot == previous:
                return True
            previous = snapshot
        return False

    def _contract_and_account_ready(self) -> bool:
        return self.main.get_contract(self.config.vt_symbol) is not None and bool(self.main.get_all_accounts())

    def _wait_for(self, cond: Callable[[], bool], timeout: float) -> bool:
        deadline = self._mono() + timeout
        while self._mono() < deadline:
            if cond():
                return True
            if self._stop_event.is_set():     # 启动阶段收到停止信号要尽快响应
                return False
            self._sleep(0.2)
        return cond()

    def _init_and_reconcile(self) -> int | None:
        cfg = self.config
        self.cta.add_strategy(cfg.strategy_class, cfg.strategy_name, cfg.vt_symbol, cfg.strategy_setting)
        self.strategy = self.cta.strategies.get(cfg.strategy_name)
        if self.strategy is None:
            self.notify("FATAL", "启动失败", f"创建策略失败: {cfg.strategy_class}")
            return EXIT_STARTUP_FAILED
        # CtaEngine 会把持久化的 variables(含 trading/inited)恢复到策略。在 init 之前就把这两个键删掉,
        # 而不是 init 完成后再强制置假——后者在 init 完成到强制置假之间有一个很小的可交易时间窗。
        saved = self.cta.strategy_data.get(cfg.strategy_name)
        if saved:
            saved.pop("trading", None)
            saved.pop("inited", None)
        future = self.cta.init_strategy(cfg.strategy_name)
        # 等待的是线程池里的真实线程,用真实时间分段等待,以便及时响应停止信号(Future.result 的阻塞等待不能被打断)
        deadline = time.monotonic() + cfg.init_timeout
        while True:
            if self._stop_event.is_set():
                return EXIT_OK
            done, _ = futures_wait([future], timeout=0.5)
            if done:
                break
            if time.monotonic() > deadline:
                self.notify("FATAL", "策略初始化失败", f"{cfg.init_timeout:.0f}s 内未完成")
                return EXIT_STARTUP_FAILED
        try:
            future.result()
        except Exception as exc:
            self.notify("FATAL", "策略初始化失败", repr(exc))
            return EXIT_STARTUP_FAILED
        self.strategy.trading = False       # 双保险:对账完成前绝不允许交易

        stale_orders = list(self.main.get_all_active_orders())
        if stale_orders:
            for order in stale_orders:
                self.main.cancel_order(order.create_cancel_request(), order.gateway_name)
            self.notify("WARN", "启动时撤销遗留委托", f"{len(stale_orders)} 笔")
            # 撤单是异步的,必须确认遗留委托都已终结:撤单前就成交的委托会产生"没有策略记录、没有止损的无主持仓"
            cleared = self._wait_for(lambda: not self.main.get_all_active_orders(), 10.0)
            if self._stop_event.is_set():
                return EXIT_OK
            if not cleared:
                self.notify("FATAL", "对账失败,拒绝启动",
                            f"{len(stale_orders)} 笔遗留委托在 10s 内未能撤销(可能已成交或撤单被拒)。请人工核对账户")
                return EXIT_RECONCILE_REFUSED
            if not self._stable_positions():          # 撤单期间可能有成交,持仓要重新确认
                if self._stop_event.is_set():
                    return EXIT_OK
                self.notify("FATAL", "对账失败,拒绝启动", "撤销遗留委托后持仓回报反复变化,请人工核对账户")
                return EXIT_RECONCILE_REFUSED

        positions = [p for p in self.main.get_all_positions() if p.volume > 0]
        foreign = [p for p in positions if p.vt_symbol != cfg.vt_symbol]
        net = signed_net(positions, cfg.vt_symbol)
        problems = []
        blind = undirected(positions)
        if blind:
            problems.append("持仓方向不明(净持仓账户?),无法核对多空与止损方向,为防算反拒绝启动: " + describe(blind))
        if foreign:
            problems.append("账户存在不属于本策略合约的持仓: " + ", ".join(f"{p.vt_symbol} {p.direction.value}{p.volume}" for p in foreign))
        if self.strategy.pos != net:
            problems.append(f"策略恢复出的持仓 {self.strategy.pos} 与账户净持仓 {net} 不一致")
        if not self.strategy.state_consistent():
            problems.append("策略状态自相矛盾(持仓与止损状态不匹配)")
        if problems:
            self.notify("FATAL", "对账失败,拒绝启动", ";".join(problems) + "。请人工核对账户与 .vntrader/cta_strategy_data.json")
            return EXIT_RECONCILE_REFUSED
        self.rule.arm_manual_detection()      # 对账通过:此后出现的非策略委托才计为人工干预
        return None

    def _equity(self) -> float:
        accounts = self.main.get_all_accounts()
        return accounts[0].balance if accounts else 0.0

    # ---------- 运行与退出 ----------

    def wait(self) -> int:
        while not self._stop_event.wait(0.5):      # 分段等待:无超时的 Event.wait() 在 Windows 上不可被 Ctrl+C 中断
            pass
        return self.exit_code if self.exit_code is not None else EXIT_OK

    def request_stop(self, code: int = EXIT_OK, reason: str = "") -> None:
        if self.exit_code is None:
            self.exit_code = code
        if reason:
            self.notify("INFO", "停止", reason)
        self._stop_event.set()

    def run(self) -> int:
        try:
            refused = self.start()
            code = refused if refused is not None else self.wait()
        except KeyboardInterrupt:
            code = EXIT_OK
        finally:
            self.shutdown()
        return code

    def shutdown(self) -> None:
        if self._shutdown_done:
            return
        self._shutdown_done = True
        # 先关掉看门狗:stop_all_strategies 会把 strategy.trading 置假,看门狗会把它误判为"策略被异常停止"而紧急平仓
        self._started = False
        try:
            held = [p for p in self.main.get_all_positions() if p.volume > 0]
            if held:
                self.notify("FATAL", "退出时仍有持仓",
                            "本地止损将随进程退出而失效,请立即人工处理: " + ", ".join(f"{p.vt_symbol} {p.direction.value}{p.volume}" for p in held))
            self.cta.stop_all_strategies()
        except Exception as exc:
            self.main.write_log(f"停止策略时出错: {exc!r}", source="Runtime")
        finally:
            try:
                self._logout_gateway()           # 先登出再关引擎:vnpy_ctp 的 close() 只断开连接、不发登出请求
                self.main.close()
            finally:
                self._close_notifier()

    def _logout_gateway(self) -> None:
        """退出前向前置发登出请求(国信「退出后是否正常注销」);任何失败只记日志,不打断退出。"""
        try:
            results = logout_ctp(self.main, self.config.gateway_name, sleep=self._sleep)
            self.main.write_log("退出登出: " + ";".join(results), source="Runtime")
        except Exception as exc:       # noqa: BLE001
            self.main.write_log(f"退出登出出错: {exc!r}", source="Runtime")

    def _close_notifier(self) -> None:
        """关闭通知器,并给"退出时仍有持仓"这类 FATAL 留出足够的发送时间。

        关闭通知器的默认预算是 10 秒,而一条消息的最坏耗时是
        (1 + 重试次数) × 单次超时 + 全部退避时间(默认 34 秒)——
        对端慢一点,"退出时有持仓"这条最关键的告警就会随进程退出一起丢掉,
        而它恰恰是无人值守最后的兜底手段。所以这里按通知器自己的排空预算来关。
        """
        budget = getattr(self.notifier, "drain_timeout", None)
        if not isinstance(budget, (int, float)) or budget <= 0:
            budget = 10.0
        try:
            self.notifier.close(drain_timeout=float(budget))
        except TypeError:
            # 通知器不是本项目的实现(签名里没有 drain_timeout):退回不带参数的关闭,
            # 绝不能让"签名对不上"把退出流程本身打断 —— 那会让告警根本发不出去。
            try:
                self.notifier.close()
            except Exception as exc:       # noqa: BLE001
                # 通知器自己的关闭再出错也不能打断 shutdown:退出码、对账、
                # 后续清理都还在这条路径上。
                self._log_notifier_error(exc)
        except Exception as exc:           # noqa: BLE001
            # 同上:关闭通知器失败不能把退出流程打断。
            self._log_notifier_error(exc)

    def _log_notifier_error(self, exc: Exception) -> None:
        """记录通知器关闭失败。写日志本身也不能再抛 —— 这已经在退出路径上了。"""
        try:
            self.main.write_log(f"通知器关闭失败(已忽略): {exc!r}", source="Runtime")
        except Exception:
            pass

    def _on_hard_lock(self, event: Event) -> None:
        data = event.data
        if data.get("flat"):
            self.request_stop(EXIT_OK, "累计死线触发且已确认空仓,停止交易并退出。解锁请用 scripts/unlock_account.py")
        else:
            self.notify("FATAL", "硬锁定但平仓未完成",
                        f"{data.get('reason', '')}。进程保持存活以便持续告警,请立即人工处理持仓。")

    # ---------- 看门狗与心跳 ----------

    def _touch_heartbeat_file(self, now: float) -> None:
        path = self.config.heartbeat_file
        if path is None or now - self._last_touch < 10:
            return
        self._last_touch = now
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{time.time():.0f}", encoding="utf-8")
        except OSError:
            pass                      # 心跳文件写失败只会让健康检查变红,不能影响交易

    def _position_drift(self) -> str | None:
        # 局限:按净持仓比较,同一合约同时持有多空时净额为 0,与 strategy.pos=0 一致而不判漂移(单策略单腿场景影响极小)
        positions = [p for p in self.main.get_all_positions() if p.volume > 0]
        foreign = [p for p in positions if p.vt_symbol != self.config.vt_symbol]
        net = signed_net(positions, self.config.vt_symbol)
        blind = undirected(positions)
        if blind:
            return "持仓方向不明(净持仓账户?),无法核对多空: " + describe(blind)
        if foreign:
            return "账户存在不属于本策略合约的持仓: " + ", ".join(f"{p.vt_symbol} {p.direction.value}{p.volume}" for p in foreign)
        if self.strategy is not None and self.strategy.pos != net:
            return f"策略持仓 {self.strategy.pos} 与账户净持仓 {net} 不一致"
        return None

    def _check_position_drift(self, now: float) -> None:
        """运行中持仓漂移(人工下单、启动时遗留委托竞态成交、回报丢失等):账户里有策略不知道的持仓,
        它没有本地止损保护。持续超过容忍时间 → FATAL 告警并紧急平仓。紧急平仓进行中不检查。"""
        if self.strategy is None or self.rule.flatten_active or self._crash_reported:
            return
        drift = self._position_drift()
        if drift is None:
            self._drift_since, self._drift_reported = None, False
            return
        if self._drift_since is None:
            self._drift_since = now
        elif now - self._drift_since > self.config.drift_tolerance_seconds and not self._drift_reported:
            self._drift_reported = True
            self.notify("FATAL", "持仓漂移", f"{drift},已持续 {now - self._drift_since:.0f}s。账户持仓没有本地止损保护,立即紧急平仓。")
            self.rule.emergency_flatten(f"持仓漂移:{drift}")

    def _in_trading_session(self) -> bool:
        t = self._now().time()
        return any(start <= t < end for start, end in ENTRY_SESSIONS)

    def _on_timer(self, event: Event) -> None:
        if not self._started or self._shutdown_done:
            return
        now = self._mono()
        self._touch_heartbeat_file(now)
        self._check_position_drift(now)

        if (self.strategy is not None and not self.strategy.trading and not self._crash_reported
                and not self._stop_event.is_set()):
            self._crash_reported = True
            self.notify("FATAL", "策略已停止运行",
                        "策略被引擎异常停止(通常是策略代码抛出异常),其本地止损已失效。立即撤单并平掉持仓。")
            self.rule.emergency_flatten("策略异常停止,本地止损失效")

        in_session = self._in_trading_session()
        if in_session and self._in_session is not True:
            # 非交易→交易时段边沿(午休/夜盘休市后重入):休市期间没有 Tick 是正常的。
            # 重置计时并解除边沿,等本时段首 Tick 之后才开始计静默,
            # 否则定时器若先于首 Tick 触发,会把整个休市时间误算成假死(实测可达 2 万秒),误撤刚挂出的订单。
            self._last_tick = None
            self._stale_cancel_fired = False
        self._in_session = in_session
        if in_session:
            silent_for = now - self._last_tick if self._last_tick is not None else None
            if silent_for is not None and silent_for > self.config.tick_stale_seconds:
                # 边沿触发:每次连续停更只强撤一次,新 Tick 复位后才允许下一次;防止每秒重复撤单
                if not self._stale_cancel_fired:
                    self._stale_cancel_fired = True
                    if self.config.stale_cancel_open_orders:
                        self.rule.cancel_open_orders_on_stale_tick()
                        self.notify("ERROR", "行情假死",
                                    f"交易时段内 {silent_for:.0f}s 没有收到 {self.config.vt_symbol} 的 Tick,"
                                    "已撤销全部开仓挂单(平仓单不动);本地止损暂时无法触发")
                    else:
                        self.notify("ERROR", "行情假死",
                                    f"交易时段内 {silent_for:.0f}s 没有收到 {self.config.vt_symbol} 的 Tick(配置为只告警,未撤单)")
                # 周期性 WARN 保留节流:假死持续时按 tick_stale_realert_seconds 再提醒
                if now - self._last_stale_alert > self.config.tick_stale_realert_seconds:
                    self._last_stale_alert = now
                    self.notify("WARN", "行情停更", f"交易时段内 {silent_for:.0f}s 没有收到 {self.config.vt_symbol} 的 Tick,本地止损暂时无法触发")

        hb = self.config.heartbeat_seconds
        if hb > 0 and now - self._last_heartbeat >= hb:
            self._last_heartbeat = now
            pos = self.strategy.pos if self.strategy is not None else 0
            self.notify("INFO", "心跳", f"运行中。权益 {self._equity():.0f},策略持仓 {pos},风控状态 {self.rule.lock_status}")


def build_ctp_setting(env: dict[str, str]) -> dict[str, Any]:
    """把环境变量整理成 vnpy_ctp 的连接参数(键是中文)。缺少必填项时抛出 ValueError,且不泄露其值。"""
    required = {
        "用户名": "CTP_USERNAME", "密码": "CTP_PASSWORD", "经纪商代码": "CTP_BROKER_ID",
        "交易服务器": "CTP_TD_ADDRESS", "行情服务器": "CTP_MD_ADDRESS",
        "产品名称": "CTP_APP_ID", "授权编码": "CTP_AUTH_CODE",
    }
    missing = [var for var in required.values() if not env.get(var)]
    if missing:
        raise ValueError("缺少环境变量: " + ", ".join(missing))
    setting: dict[str, Any] = {key: env[var] for key, var in required.items()}
    # "实盘" = 生产版 API(SimNow 与实盘使用);"测试" = 期货公司看穿式监管测试环境
    environment = (env.get("CTP_ENVIRONMENT", "实盘") or "实盘").strip()
    if environment not in ("实盘", "测试"):
        # 写法不规范(尾随空格、live、PROD…)时不能悄悄当成别的环境:实盘准入检查按精确值判断
        raise ValueError("CTP_ENVIRONMENT 只能是「实盘」或「测试」")
    setting["柜台环境"] = environment
    return setting
