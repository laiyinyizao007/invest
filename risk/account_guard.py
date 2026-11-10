"""账户级风控的纯逻辑(不依赖 vn.py,便于测试):双层熔断 + 交易日判断。

口径(用户确认,见 docs/adr/20261109-account-risk-lock.md):
- 当日熔断(DAILY):账户权益相对"当日开盘权益"下降 >= daily_loss_limit(默认 600 元,含浮动盈亏),
  当日禁止新开仓,次交易日自动解除。
- 累计死线(HARD):账户权益相对"历史最高权益"回撤 >= max_drawdown(默认 2000 元)。
  从 1 万元起步时等价于跌破 8000 元;账户创新高后底线随之抬高。触发后永久锁定,必须人工解锁。

设计要点:
- 只对"新开仓"生效,平仓永远允许(持仓的离场不能被风控拦住)。
- 权益 <= 0 或 NaN 视为无效采样(CTP 启动/查询瞬间常报 0),不触发也不更新最高点,避免误熔断。
- 状态原子写入磁盘(含 fsync):进程重启后锁定状态不丢失,硬锁定不会因重启被"洗掉"。写盘失败不抛异常
  (不能让磁盘问题中断定时器上的熔断推进),而是记录在 save_error 里由调用方告警。
- 当日开盘权益 = 前一次采样的权益(持久化),而不是新交易日的第一次采样:进程在盘中重启时,
  第一次采样可能已经包含当日亏损,若以它为基准,当日熔断会被抬高而永远不触发。
- 交易日按夜盘归属下一交易日处理,不处理法定节假日(周五夜盘归下周一)。
"""
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


def trading_day(dt: datetime) -> date:
    """期货交易日:20 点之后的夜盘归属下一个工作日;凌晨 3 点前(跨午夜的夜盘)归属当天的交易日;周末顺延到周一。"""
    d = dt.date()
    if dt.hour >= 20:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


@dataclass(frozen=True)
class AccountLimits:
    daily_loss_limit: float = 600.0   # 相对当日开盘权益的下降额(元)
    max_drawdown: float = 2000.0      # 相对历史最高权益的回撤额(元)

    def __post_init__(self) -> None:
        if self.daily_loss_limit <= 0 or self.max_drawdown <= 0:
            raise ValueError("熔断阈值必须为正数")
        if self.daily_loss_limit > self.max_drawdown:
            raise ValueError("当日熔断阈值不应大于累计死线")


@dataclass(frozen=True)
class LockEvent:
    level: str           # "DAILY" 或 "HARD"
    reason: str
    equity: float
    day: date


class AccountGuard:
    """账户级熔断状态机。update() 每次传入最新权益,返回本次新触发的熔断事件(边沿触发,不重复)。"""

    def __init__(self, limits: AccountLimits = AccountLimits(), state_path: Path | None = None) -> None:
        self.limits = limits
        self.state_path = state_path
        self.peak_equity: float = 0.0
        self.day: date | None = None
        self.day_start_equity: float = 0.0
        self.daily_locked_day: date | None = None
        self.hard_locked: bool = False
        self.hard_reason: str = ""
        self.last_equity: float = 0.0          # 前一次有效采样的权益,作为新交易日的开盘基准
        self.account_id: str = ""              # 状态所属的账户;换账户时权益基准必须重置(SimNow 2000 万 vs 实盘 1 万)
        self.save_error: str = ""
        self._last_saved_at: datetime | None = None
        self._load()

    # ---------- 查询 ----------

    def is_daily_locked(self, day: date) -> bool:
        return self.daily_locked_day == day

    def can_open(self, day: date) -> bool:
        """是否允许新开仓。平仓不受此函数约束。"""
        return not self.hard_locked and not self.is_daily_locked(day)

    def status(self, day: date) -> str:
        if self.hard_locked:
            return "HARD_LOCKED"
        return "DAILY_LOCKED" if self.is_daily_locked(day) else "OK"

    # ---------- 账户绑定 ----------

    def bind_account(self, account_id: str) -> str | None:
        """把状态绑定到账户。首次绑定只记录;换了账户则重置权益基准(最高点、日初、当日锁定),返回说明文字。

        硬锁定**不会**因换账户而解除:它代表"需要人判断后才能恢复",只能用 unlock_hard()。
        """
        if not account_id or account_id == self.account_id:
            return None
        previous, self.account_id = self.account_id, account_id
        if not previous:               # 首次绑定(含旧版本的状态文件):沿用已有基准,只补记账户
            self._save()
            return None
        self.peak_equity = 0.0
        self.last_equity = 0.0
        self.day = None
        self.day_start_equity = 0.0
        self.daily_locked_day = None
        self._last_saved_at = None
        self._save()
        return f"账户由 {previous} 变更为 {account_id},权益基准已重置" + ("(硬锁定仍然保留,需人工解锁)" if self.hard_locked else "")

    # ---------- 更新 ----------

    def update(self, equity: float, now: datetime) -> list[LockEvent]:
        if equity is None or math.isnan(equity) or equity <= 0:
            return []  # 无效采样
        day = trading_day(now)
        events: list[LockEvent] = []
        changed = False

        if self.day != day:  # 新交易日:以前一次采样的权益作为当日开盘权益(没有历史时才用当前权益)
            self.day = day
            self.day_start_equity = self.last_equity if self.last_equity > 0 else equity
            changed = True
        if equity > self.peak_equity:
            self.peak_equity = equity
            changed = True

        if not self.hard_locked and self.peak_equity - equity >= self.limits.max_drawdown:
            self.hard_locked = True
            self.hard_reason = (f"权益 {equity:.0f} 距最高点 {self.peak_equity:.0f} 回撤 "
                                f"{self.peak_equity - equity:.0f} 元 >= 累计死线 {self.limits.max_drawdown:.0f} 元")
            events.append(LockEvent("HARD", self.hard_reason, equity, day))
            changed = True

        if self.daily_locked_day != day and self.day_start_equity - equity >= self.limits.daily_loss_limit:
            self.daily_locked_day = day
            reason = (f"当日权益 {equity:.0f} 较开盘 {self.day_start_equity:.0f} 下降 "
                      f"{self.day_start_equity - equity:.0f} 元 >= 当日熔断 {self.limits.daily_loss_limit:.0f} 元")
            events.append(LockEvent("DAILY", reason, equity, day))
            changed = True

        self.last_equity = equity
        # 状态有变化立即落盘;权益本身变化频繁,否则至少每 60 秒落盘一次,供重启后作为开盘基准
        if changed or self._last_saved_at is None or (now - self._last_saved_at).total_seconds() >= 60:
            self._save()
            self._last_saved_at = now
        return events

    def lock_daily(self, now: datetime, reason: str) -> LockEvent | None:
        """外部触发的当日锁定(如废单风暴);已锁定则返回 None。"""
        day = trading_day(now)
        if self.hard_locked or self.daily_locked_day == day:
            return None
        self.daily_locked_day = day
        self._save()
        return LockEvent("DAILY", reason, 0.0, day)

    def unlock_hard(self) -> None:
        """人工解除硬锁定,并把最高点重置为下一次采样的权益(否则会立刻再次触发)。"""
        self.hard_locked = False
        self.hard_reason = ""
        self.peak_equity = 0.0
        self._save()

    # ---------- 持久化 ----------

    def _save(self) -> None:
        if self.state_path is None:
            return
        state = {
            "account_id": self.account_id,
            "last_equity": self.last_equity,
            "peak_equity": self.peak_equity,
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "daily_locked_day": self.daily_locked_day.isoformat() if self.daily_locked_day else None,
            "hard_locked": self.hard_locked,
            "hard_reason": self.hard_reason,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(json.dumps(state, ensure_ascii=False, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
            self.save_error = ""
        except OSError as exc:
            self.save_error = repr(exc)

    def _load(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            s = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.account_id = str(s.get("account_id", ""))
            self.last_equity = float(s.get("last_equity", 0.0))
            self.peak_equity = float(s["peak_equity"])
            self.day = date.fromisoformat(s["day"]) if s["day"] else None
            self.day_start_equity = float(s["day_start_equity"])
            self.daily_locked_day = date.fromisoformat(s["daily_locked_day"]) if s["daily_locked_day"] else None
            self.hard_locked = bool(s["hard_locked"])
            self.hard_reason = str(s["hard_reason"])
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # 状态文件损坏:宁可保守地进入硬锁定,也不能当作"没有锁"继续交易
            self.hard_locked = True
            self.hard_reason = f"风控状态文件损坏({exc!r}),按硬锁定处理,请人工核对后解锁"
