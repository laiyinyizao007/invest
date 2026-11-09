"""策略在真实 CtaEngine + 风控链路 + 假网关上的实盘模式测试:Tick → 日线 → 信号 → 次交易时段发单 → 成交 → 本地止损 → 平仓。"""
import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import bootstrap  # noqa: E402,F401
import vnpy_ctastrategy.engine as cta_engine_module  # noqa: E402
from fakes import FakeDatabase, FakeGateway, SyncEventEngine, history_bars  # noqa: E402
from strategies.trend_atr_strategy import ENTRY_SESSIONS, TrendAtrStrategy  # noqa: E402
from trading.safe_risk import SafeRiskManagerApp  # noqa: E402
from vnpy.trader.constant import Direction, Exchange, Interval, Offset  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy_ctastrategy import CtaStrategyApp  # noqa: E402

SETTING = {"fast_window": 2, "slow_window": 3, "atr_mult": 1.5, "risk_budget": 1e9}


class LiveEnv:
    def __init__(self, monkeypatch, tmp_path, setting=None, bars=None, strategy_data=None, name="t1"):
        monkeypatch.setattr(cta_engine_module, "load_json", lambda filename: {})
        monkeypatch.setattr(cta_engine_module, "save_json", lambda *a, **k: None)   # 不污染真实的 .vntrader
        self.event_engine = SyncEventEngine()
        self.main = MainEngine(self.event_engine)
        self.gw: FakeGateway = self.main.add_gateway(FakeGateway)
        self.risk = self.main.add_app(SafeRiskManagerApp)
        self.risk.rules["账户风控"].update_setting({"state_file": str(tmp_path / "guard.json")})
        self.cta = self.main.add_app(CtaStrategyApp)
        self.db = FakeDatabase(bars if bars is not None else history_bars())
        self.cta.database = self.db
        self.cta.classes["TrendAtrStrategy"] = TrendAtrStrategy
        self.cta.register_event()
        if strategy_data:
            self.cta.strategy_data = copy.deepcopy(strategy_data)
        self.gw.push_contract()
        self.gw.mode = "fill"
        self.name = name
        self.cta.add_strategy("TrendAtrStrategy", name, "c2601.DCE", {**SETTING, **(setting or {})})
        self.strategy: TrendAtrStrategy = self.cta.strategies[name]

    def start(self) -> None:
        self.cta._init_strategy(self.name)
        self.strategy.trading = False        # 运行时在核对持仓前会强制停止(引擎可能把保存的 trading=True 恢复出来)
        self.cta.start_strategy(self.name)

    def tick(self, dt: datetime, price: float, volume: float = 0.0) -> None:
        self.gw.push_tick(dt=dt, last=price, volume=volume)

    def full_day(self, close: float, day=datetime(2026, 11, 10), first_hour=(21, 0), prev_day=None) -> None:
        """一个完整交易日:前一晚 21:00 起,到 15:00 收盘。最后一个 15:00 的 Tick 触发 14:59 分钟线推出,日线完成。"""
        prev = prev_day or (day - timedelta(days=1))
        self.tick(prev.replace(hour=first_hour[0], minute=first_hour[1], second=1), close + 3, 10)
        self.tick(day.replace(hour=9, minute=30, second=0), close + 5, 100)
        self.tick(day.replace(hour=14, minute=59, second=30), close, 200)
        self.tick(day.replace(hour=15, minute=0, second=0), close, 210)


@pytest.fixture
def make_env(monkeypatch, tmp_path):
    envs = []

    def factory(**kw):
        e = LiveEnv(monkeypatch, tmp_path, **kw)
        envs.append(e)
        return e

    yield factory
    for e in envs:
        e.main.close()


# ---------- 信号产生与延迟发单 ----------

def test_signal_at_close_is_deferred_to_next_session(make_env):
    env = make_env()
    env.start()
    env.full_day(close=2230.0)                                     # 下跌后大幅反弹 → 金叉
    assert env.strategy.pending_entry is not None and env.strategy.pending_entry[0] == 1
    assert env.gw.sent == []                                       # 收盘时刻已休市,不能下单
    env.tick(datetime(2026, 11, 10, 15, 30, 0), 2230.0)            # 休市后的 Tick 也不发
    assert env.gw.sent == []


def test_order_sent_on_first_tick_of_next_session_and_passes_risk_chain(make_env):
    env = make_env()
    env.start()
    env.full_day(close=2230.0)
    env.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    assert len(env.gw.sent) == 1
    req = env.gw.sent[0]
    assert (req.direction, req.offset) == (Direction.LONG, Offset.OPEN)
    assert req.price == round(req.price) and req.price <= round(2231.0 * 1.05)   # 取整到最小变动价位,且不越过涨停
    assert env.strategy.pending_entry is None                      # 只发一次
    assert env.strategy.pos == 1                                   # fill 模式:立即成交


def test_pending_entry_not_resent_on_later_ticks(make_env):
    env = make_env()
    env.gw.mode = "accept"                                         # 挂单不成交
    env.start()
    env.full_day(close=2230.0)
    for sec in (2, 3, 4):
        env.tick(datetime(2026, 11, 10, 21, 0, sec), 2231.0)
    assert len(env.gw.sent) == 1


def test_no_signal_when_budget_exceeded(make_env):
    env = make_env(setting={"risk_budget": 1.0})
    env.start()
    env.full_day(close=2230.0)
    assert env.strategy.pending_entry is None and env.strategy.skipped_by_budget >= 1


# ---------- 整条链路 ----------

def test_full_cycle_entry_local_stop_and_exit(make_env):
    env = make_env()
    env.start()
    env.full_day(close=2230.0)
    env.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    s = env.strategy
    assert s.pos == 1 and s.entry_price > 0
    assert len(env.cta.stop_orders) == 1                           # 本地保护性止损已挂出
    stop = next(iter(env.cta.stop_orders.values()))
    assert stop.direction == Direction.SHORT and stop.price < s.entry_price
    assert stop.price >= s.stop_price                              # 取整向"更紧"的一侧

    env.tick(datetime(2026, 11, 10, 21, 5, 0), s.entry_price - 1)  # 未触及止损
    assert s.pos == 1
    env.tick(datetime(2026, 11, 10, 21, 10, 0), stop.price - 1)    # 跌破止损 → 本地止损触发
    closing = env.gw.sent[-1]
    assert (closing.direction, closing.offset) == (Direction.SHORT, Offset.CLOSE)
    assert s.pos == 0 and env.cta.stop_orders == {}
    assert s.trail.side == 0 and s.entry_price == 0                # 状态已清理
    assert s.pending_dist == 0                                     # 陈旧的止损距离也被清掉


def test_risk_lock_blocks_entry_but_not_the_stop_exit(make_env):
    env = make_env()
    env.start()
    env.full_day(close=2230.0)
    env.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    assert env.strategy.pos == 1
    rule = env.risk.rules["账户风控"]
    rule.guard.hard_locked = True                                  # 触发硬锁定
    stop = next(iter(env.cta.stop_orders.values()))
    env.tick(datetime(2026, 11, 10, 21, 10, 0), stop.price - 1)
    assert env.strategy.pos == 0                                   # 止损平仓不受锁定影响


def test_rejected_entry_by_risk_lock_is_not_retried(make_env):
    env = make_env()
    env.start()
    env.risk.rules["账户风控"].guard.hard_locked = True
    env.full_day(close=2230.0)
    env.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    env.tick(datetime(2026, 11, 10, 21, 0, 3), 2231.0)
    assert env.gw.sent == [] and env.strategy.pos == 0 and env.strategy.pending_entry is None


# ---------- 盘中启动 ----------

def test_mid_session_start_discards_partial_daily_bar(make_env):
    env = make_env()
    env.start()
    before = env.strategy.am.count
    day = datetime(2026, 11, 10)
    env.tick(day.replace(hour=10, minute=31, second=0), 2230.0, 10)      # 盘中才开始收到行情
    env.tick(day.replace(hour=14, minute=59, second=30), 2235.0, 100)
    env.tick(day.replace(hour=15, minute=0, second=0), 2235.0, 110)
    assert env.strategy.am.count == before                                # 这根不完整的日线被丢弃
    assert env.strategy.pending_entry is None


def test_full_day_after_partial_day_is_used(make_env):
    env = make_env()
    env.start()
    before = env.strategy.am.count
    d1 = datetime(2026, 11, 10)
    env.tick(d1.replace(hour=10, minute=31), 2230.0, 10)
    env.tick(d1.replace(hour=14, minute=59, second=30), 2230.0, 15)
    env.tick(d1.replace(hour=15, minute=0), 2230.0, 20)                   # 不完整,丢弃
    env.full_day(close=2250.0, day=datetime(2026, 11, 11), prev_day=d1)   # 次日完整
    assert env.strategy.am.count == before + 1


def test_daily_bar_is_force_completed_when_last_minute_has_no_tick(make_env):
    """14:59 那一分钟没有任何 Tick:日线不会自然完成,不能静默并入下一交易日,必须强制完成。"""
    env = make_env()
    env.start()
    before = env.strategy.am.count
    day = datetime(2026, 11, 10)
    env.tick(day.replace(hour=0, minute=0, second=0) - timedelta(hours=3), 2230.0, 10)     # 前一晚 21:00
    env.tick(day.replace(hour=13, minute=0, second=0), 2232.0, 20)
    env.tick(day.replace(hour=14, minute=58, second=0), 2234.0, 30)                        # 之后到收盘都没有 Tick
    assert env.strategy.am.count == before                                                  # 日线尚未完成
    env.tick(day.replace(hour=21, minute=0, second=1), 2236.0, 40)                         # 下一交易日夜盘的第一个 Tick
    env.tick(day.replace(hour=21, minute=1, second=1), 2236.0, 50)                         # 夜盘第一根分钟线在下一分钟才推出
    assert env.strategy.am.count == before + 1                                              # 上一根被强制完成,没有跨日合并
    assert env.strategy._daily_open_dt.time().hour == 21                                    # 新的一天从夜盘开始合成


# ---------- 历史预热 ----------

def test_history_comes_from_configured_symbol(make_env):
    env = make_env(setting={"history_vt_symbol": "CADJ.DCE"})
    env.start()
    assert env.db.queried == ["CADJ"]
    assert env.strategy.am.inited


def test_history_defaults_to_own_symbol(make_env):
    env = make_env()
    env.start()
    assert env.db.queried == ["c2601"]


# ---------- 重启恢复 ----------

def test_state_survives_restart_and_stop_is_rearmed(make_env):
    env1 = make_env()
    env1.start()
    env1.full_day(close=2230.0)
    env1.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    s1 = env1.strategy
    assert s1.pos == 1
    saved = copy.deepcopy(env1.cta.strategy_data)                  # sync_data 保存的内容
    assert saved["t1"]["pos"] == 1 and saved["t1"]["trail_side"] == 1

    env2 = make_env(strategy_data=saved)                           # 模拟进程重启:全新引擎,只带着保存的状态
    env2.start()
    s2 = env2.strategy
    assert s2.pos == 1 and s2.state_consistent()
    assert (s2.entry_price, s2.stop_price, s2.trail.stop) == (s1.entry_price, s1.stop_price, s1.trail.stop)
    assert env2.cta.stop_orders == {}                              # 本地止损单不会随进程保存,此刻尚未重挂
    env2.tick(datetime(2026, 11, 10, 21, 20, 0), s1.entry_price + 1)
    assert len(env2.cta.stop_orders) == 1                          # 第一个 Tick 重新挂出


def test_restart_below_stop_exits_immediately(make_env):
    env1 = make_env()
    env1.start()
    env1.full_day(close=2230.0)
    env1.tick(datetime(2026, 11, 10, 21, 0, 2), 2231.0)
    saved = copy.deepcopy(env1.cta.strategy_data)
    stop_price = env1.strategy.stop_price

    env2 = make_env(strategy_data=saved)
    env2.start()
    env2.gw.set_position(Direction.LONG, 1)
    env2.tick(datetime(2026, 11, 11, 9, 0, 5), stop_price - 10)    # 休眠期间价格已跌破止损
    assert env2.gw.sent and env2.gw.sent[-1].offset == Offset.CLOSE
    assert env2.strategy.pos == 0


def test_state_consistency_check():
    class Stub(TrendAtrStrategy):
        def __init__(self):                                        # 只测纯状态判断,不需要引擎
            self.pos = 0
            from risk.risk_guard import TrailingStop
            self.trail = TrailingStop()

    s = Stub()
    assert s.state_consistent()                                    # 空仓、无止损
    s.trail.side, s.trail.stop = 1, 2200.0
    assert not s.state_consistent()                                # 空仓却残留止损
    s.pos = 1
    assert s.state_consistent()                                    # 多头持仓 + 多头止损
    s.pos = -1
    assert not s.state_consistent()                                # 方向不一致
    s.pos, s.trail.stop = 1, 0.0
    assert not s.state_consistent()                                # 有持仓却没有止损价


def test_entry_sessions_cover_expected_windows():
    from datetime import time
    assert any(a <= time(21, 0, 2) < b for a, b in ENTRY_SESSIONS)
    assert not any(a <= time(15, 0, 0) < b for a, b in ENTRY_SESSIONS)       # 收盘时刻不在交易时段
    assert not any(a <= time(12, 0, 0) < b for a, b in ENTRY_SESSIONS)       # 午休
