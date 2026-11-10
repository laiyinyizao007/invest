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
- 行情停更:本地止损依赖 Tick 触发,交易时段内长时间没有 Tick 要告警;
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
from vnpy.trader.constant import Direction
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_LOG, EVENT_TICK, EVENT_TIMER, EVENT_TRADE
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.logger import ERROR
from vnpy.trader.utility import get_file_path
from vnpy_ctastrategy import CtaStrategyApp

from strategies.trend_atr_strategy import ENTRY_SESSIONS, TrendAtrStrategy
from trading.events import EVENT_GUARD_ALERT, EVENT_GUARD_HARD_LOCK
from trading.safe_risk import SafeRiskManagerApp

EXIT_OK = 0
EXIT_STARTUP_FAILED = 3
EXIT_RECONCILE_REFUSED = 4


class Notifier(Protocol):
    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool: ...

    def close(self, flush_timeout: float = ...) -> None: ...


class NullNotifier:
    def send(self, level: str, title: str, message: str, extra: dict | None = None) -> bool:
        return False

    def close(self, flush_timeout: float = 0.0) -> None:
        pass


@dataclass
class RuntimeConfig:
    vt_symbol: str                                   # 实盘交易的具体合约,如 c2701.DCE
    strategy_class: str = "TrendAtrStrategy"
    strategy_name: str = "trend_atr"
    strategy_setting: dict = field(default_factory=dict)
    gateway_name: str = "CTP"
    gateway_setting: dict = field(default_factory=dict)
    connect_timeout: float = 120.0                   # 等待合约与账户到位的最长时间(秒);SimNow 的"结算信息确认"耗时波动大(实测 4~60+ 秒)
    position_settle_seconds: float = 5.0             # 账户到位后主动查询持仓并等待其稳定的时间(秒)
    init_timeout: float = 120.0
    heartbeat_seconds: float = 0.0                   # 0 = 不发心跳
    tick_stale_seconds: float = 60.0                 # 交易时段内超过该时间没有 Tick 就告警
    tick_stale_realert_seconds: float = 300.0
    drift_tolerance_seconds: float = 15.0            # 策略持仓与账户持仓不一致持续超过该时间 → 视为漂移,告警并紧急平仓
    heartbeat_file: Path | None = None               # 运行中每 10 秒刷新一次,供容器健康检查判断进程是否真的在工作

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
        self.rule = self.risk.rules["账户风控"]
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
        self._last_stale_alert = -1e9
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
        if "连接断开" in msg:
            self.notify("ERROR", "网关连接断开", msg)
        elif "登录成功" in msg:
            self.notify("INFO", "网关已连接", msg)
        elif log.level >= ERROR:
            self.notify("WARN", "网关错误", msg)

    def _on_tick(self, event: Event) -> None:
        if event.data.vt_symbol == self.config.vt_symbol:
            self._last_tick = self._mono()

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
        try:
            self.main.connect(self.config.gateway_setting, self.config.gateway_name)
        except Exception as exc:
            self.notify("FATAL", "网关连接失败", repr(exc))
            return EXIT_STARTUP_FAILED
        if not self._wait_for(self._contract_and_account_ready, self.config.connect_timeout):
            if self._stop_event.is_set():
                return EXIT_OK
            self.notify("FATAL", "启动失败", f"{self.config.connect_timeout:.0f}s 内未收到合约 {self.config.vt_symbol} 或账户信息")
            return EXIT_STARTUP_FAILED
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
        self._last_heartbeat = self._mono()
        self.notify("INFO", "已启动", f"{self.config.strategy_name} 运行于 {self.config.vt_symbol},权益 {self._equity():.0f}")
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
        net = sum(p.volume if p.direction == Direction.LONG else -p.volume for p in positions if p.vt_symbol == cfg.vt_symbol)
        problems = []
        if foreign:
            problems.append("账户存在不属于本策略合约的持仓: " + ", ".join(f"{p.vt_symbol} {p.direction.value}{p.volume}" for p in foreign))
        if self.strategy.pos != net:
            problems.append(f"策略恢复出的持仓 {self.strategy.pos} 与账户净持仓 {net} 不一致")
        if not self.strategy.state_consistent():
            problems.append("策略状态自相矛盾(持仓与止损状态不匹配)")
        if problems:
            self.notify("FATAL", "对账失败,拒绝启动", ";".join(problems) + "。请人工核对账户与 .vntrader/cta_strategy_data.json")
            return EXIT_RECONCILE_REFUSED
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
                self.main.close()
            finally:
                self.notifier.close()

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
        net = sum(p.volume if p.direction == Direction.LONG else -p.volume for p in positions if p.vt_symbol == self.config.vt_symbol)
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

        if self._in_trading_session():
            silent_for = now - self._last_tick if self._last_tick is not None else None
            if (silent_for is not None and silent_for > self.config.tick_stale_seconds
                    and now - self._last_stale_alert > self.config.tick_stale_realert_seconds):
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
    setting["柜台环境"] = env.get("CTP_ENVIRONMENT", "实盘")
    return setting
