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
- 纪律问责(docs/adr/20261111-discipline-enforcement.md):硬锁定记录触发时间,人工解锁有冷却期并写入解锁历史
  (解锁后峰值重置 = 重新获得完整预算,必须留痕);运行中检测到的非策略委托按交易日计数。
"""
import json
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# 固定 UTC+8,**不用** `ZoneInfo("Asia/Shanghai")`:
# ZoneInfo 要读系统(或 pip 装的 `tzdata`)里的时区数据库,Windows 上常常只有用户目录里那份
# `tzdata` —— 而以 `-I` 隔离模式启动的进程(仪表盘冒烟 `scripts/status_server.py`)拿不到用户目录,
# 一 import 就 `ZoneInfoNotFoundError`,把仪表盘带崩(2026-11-16 全量 pytest 抓到:
# `tests/test_dashboard_boundaries.py` 多出 2 个失败)。中国最后一段夏令时是 1991-04-14 开始、
# **1991-09-15 结束**(不是 1991-04-14 就停用 —— 那是开始日,1991-09-14 仍是 UTC+9),
# 自那以后固定偏移与 `Asia/Shanghai` 在交易日口径上完全等价(1991 年 9 月以前的部分日期会差 1 小时,
# 交易日语义上无影响),而且不依赖任何外部数据(2026-11-16 codex 复核 L5)。
BEIJING = timezone(timedelta(hours=8), "北京时间")


def beijing_now() -> datetime:
    """北京时间(无时区信息)。

    期货交易日按**交易所所在时区**算,不能用运行机的本地时间:宿主机可能是 UTC-8,
    本地时间与北京时间在夜盘时段会分属不同的交易日(2026-11-16 codex 复核 M2:
    预检按北京时间算交易日、运行时按本机时间算,两者在 20:00 之后的几个小时里分叉,
    预检判「没锁」放行、运行时判「当日锁定」⇒ 白跑一次国信登录额度)。

    **测试注入契约**:本函数用的是**模块全局** `datetime` 名字,测试可以
    `monkeypatch.setattr(account_guard, "datetime", FixedDatetime)` 把整块时钟钉死
    (2026-11-16 codex 复核 L-6:这个契约原先只存在于测试代码的注释里,没写在源头)。
    改这里的实现前,先确认报告封面的日期用例还钉得住。
    """
    return datetime.now(BEIJING).replace(tzinfo=None)


def trading_day(dt: datetime) -> date:
    """期货交易日:20 点之后的夜盘归属下一个工作日;凌晨 3 点前(跨午夜的夜盘)归属当天的交易日;周末顺延到周一。"""
    d = dt.date()
    if dt.hour >= 20:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


UNLOCK_HISTORY_LIMIT = 20      # 状态文件里最多保留的解锁记录条数
MANUAL_DAYS_LIMIT = 60         # 非策略委托按日计数,最多保留的天数
HANDLED_REJECTS_DAYS = 3       # 「已处理过的废单委托号」最多保留的交易日数(登录回放只回放当日)
HANDLED_REJECTS_PER_DAY = 5000  # 每个交易日最多留多少个;超了丢最旧的(被丢的下次当新废单再判一次:多锁不漏锁)


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
        self.hard_locked_at: str = ""          # 硬锁定触发时间(ISO,北京时间);空 = 未知(旧状态文件/文件损坏),冷却期无法计算
        self.hard_locked_at_legacy: bool = False   # 旧状态文件里的 hard_locked_at 是**本机本地时间**,交易日不可信
        self.unlock_history: list[dict] = []   # 人工解锁记录:{at, locked_at, reason, forced}
        self.manual_orders: dict[str, int] = {}  # 交易日(ISO) -> 当日检测到的非策略委托笔数
        # 交易日(ISO) -> 已处理过(分类并做完锁定决策)的废单委托号。登录/重连后柜台会把当日委托整体回放,
        # 已处理的废单不能再判一遍(2026-11-18 国信实测:每次登录都重新 DAILY_LOCKED)。查不到 = 当新废单照常判定。
        self.handled_rejects: dict[str, list[str]] = {}
        self.last_equity: float = 0.0          # 前一次有效采样的权益,作为新交易日的开盘基准
        # 该权益『被采样』的时刻(账户回报到达柜台网关的时间),不是文件写入时间。只供状态页判断权益是否新鲜:
        # 网关断开后 vn.py 仍缓存着旧账户对象,定时器照常读到它并落盘,文件 mtime 因此一直是新的。纯可观测字段,不参与任何熔断判断。
        self.last_sampled_at: datetime | None = None
        # 读盘时**用不了**的采样时刻原样留一份(纯留痕):值被丢掉是保守的(状态页显示
        # 「无法确认」而不是「新鲜」),但「值曾经存在、只是格式不对」这件事不该被抹掉 ——
        # 否则运维看到状态页变陈旧,却查不到任何线索(2026-11-16 codex 复核 L3)。
        self.sampled_at_unusable: str = ""
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
            # 这一支**故意保留** `sampled_at_unusable`:旧状态文件没有 account_id,
            # 那条「采样时刻用不了」的留痕是**无主**的(不属于任何旧账户),而这一支
            # 本来就整体沿用旧文件的基准(peak_equity / day_start_equity / last_sampled_at
            # 一个都不清)⇒ 只把留痕单独抹掉会让状态变成「旧基准 + 新解释」的半新半旧。
            # 留痕本来就是「这里曾经有个值、只是格式不对」的审计线索,抹掉反而丢证据
            # (2026-11-16 codex 第十轮复核 L-7:两个选项里取「有意保留 + 写明」)。
            # 该字段只出现在 _save/_load,**不参与任何判定**(can_open / status /
            # is_daily_locked 都不读它)⇒ 对实盘防线零影响。
            self._save()
            return None
        self.peak_equity = 0.0
        self.last_equity = 0.0
        self.last_sampled_at = None           # 换了账户:旧账户的采样时间不属于新账户
        # 旧账户那条「采样时刻用不了」的留痕也不属于新账户 ⇒ 一并作废,
        # 否则状态文件里会出现「新账户 + 旧账户的坏采样理由」这种自相矛盾的组合
        # (2026-11-16 codex 第九轮复核 L4)。
        self.sampled_at_unusable = ""
        self.day = None
        self.day_start_equity = 0.0
        self.daily_locked_day = None
        self._last_saved_at = None
        self._save()
        return f"账户由 {previous} 变更为 {account_id},权益基准已重置" + ("(硬锁定仍然保留,需人工解锁)" if self.hard_locked else "")

    # ---------- 更新 ----------

    def update(self, equity: float, now: datetime, sampled_at: datetime | None = None) -> list[LockEvent]:
        """sampled_at:这笔权益的采样时刻(调用方知道账户回报何时到达)。**调用方没有就传 None,此时保持原值**——
        绝不用 now 顶替:重启后 vn.py 可能仍返回缓存的旧账户对象,把它写成『刚采样』就是虚假的新鲜。"""
        if equity is None or math.isnan(equity) or equity <= 0:
            return []  # 无效采样
        if sampled_at is not None:
            self.last_sampled_at = sampled_at
            self.sampled_at_unusable = ""      # 有了新的可用采样时刻,旧的「用不了」留痕作废
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
            # 触发时刻一律走 `_stamp_beijing()`:写出来一定是 `+08:00`,
            # 不依赖调用方传进来的 `now` 带什么偏移(旧代码原样写 `now.isoformat()`,
            # 调用方若传了非北京偏移,存下来的偏移会被后面
            # `trading_day(datetime.fromisoformat(...))` 当成北京时间比交易日 ⇒ 冷却期被放宽,
            # 与「旧状态文件里的本机本地时间」是同一类 fail-open)。
            self.hard_locked_at = self._stamp_beijing(now)   # `now` 必填 ⇒ 一定有值
            # 新发生的锁,触发时刻是**我们自己**按北京时间写的 ⇒ 不再是「旧文件里的未知时间」。
            # 不复位的话:从旧状态文件升级上来之后,新锁会继承 legacy 标记,
            # 冷却期永远要 `--force`(方向是保守的,但属于「卡住」)。
            self.hard_locked_at_legacy = False
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

    def unlock_cooldown(self, now: datetime) -> str:
        """解锁冷却:硬锁定发生的交易日内不允许解锁(需要隔夜冷静)。返回空串 = 可以解锁,否则是拒绝理由。

        触发时间未知(旧状态文件、状态文件损坏)时无法证明已过冷却期,**按冷却期内处理**:必须 --force 交互确认,
        否则"把状态文件弄坏"就成了绕过冷却的办法。
        """
        unknown = "硬锁定触发时间未知(旧状态文件或状态文件损坏),无法证明已过冷却期,解锁需在核对后用 --force 强制"
        if not self.hard_locked_at:
            return unknown
        if self.hard_locked_at_legacy:
            return ("硬锁定触发时间来自旧状态文件,记录的是本机本地时间,无法证明已过冷却期,"
                    "解锁需在核对后用 --force 强制")
        try:
            locked_day = trading_day(datetime.fromisoformat(self.hard_locked_at))
        except ValueError:
            return unknown
        if trading_day(now) > locked_day:
            return ""
        return f"硬锁定发生在交易日 {locked_day},解锁需等到下一个交易日(隔夜冷静),或在核对后用 --force 强制"

    def unlock_hard(self, now: datetime | None = None, forced: bool = False) -> None:
        """人工解除硬锁定,并把最高点重置为下一次采样的权益(否则会立刻再次触发)。

        重置峰值等于重新获得完整的累计死线预算,所以每次解锁都写入 unlock_history(含是否越过冷却)。
        """
        now = now or beijing_now()
        before = (self.hard_locked, self.hard_reason, self.hard_locked_at, self.hard_locked_at_legacy,
                  self.peak_equity, list(self.unlock_history))
        self.unlock_history = (self.unlock_history + [{
            "at": now.isoformat(timespec="seconds"), "locked_at": self.hard_locked_at,
            "reason": self.hard_reason, "forced": forced,
        }])[-UNLOCK_HISTORY_LIMIT:]
        self.hard_locked = False
        self.hard_reason = ""
        self.hard_locked_at = ""
        self.hard_locked_at_legacy = False
        self.peak_equity = 0.0
        self._save()
        if self.save_error:
            # 没写进磁盘就不算解锁:内存状态回滚,与磁盘保持一致(调用方据 save_error 报错)
            self.hard_locked, self.hard_reason, self.hard_locked_at, self.hard_locked_at_legacy, \
                self.peak_equity, self.unlock_history = before

    # ---------- 非策略委托计数 ----------

    def record_manual_order(self, now: datetime) -> int:
        """记一笔非策略委托(不是 CTA 策略、也不是紧急平仓发出的),返回当日累计笔数。只计数,不触发锁定。"""
        key = trading_day(now).isoformat()
        self.manual_orders[key] = self.manual_orders.get(key, 0) + 1
        for old in sorted(self.manual_orders)[:-MANUAL_DAYS_LIMIT]:
            del self.manual_orders[old]
        self._save()
        return self.manual_orders[key]

    # ---------- 已处理的废单(抗登录/重连回放) ----------

    def is_reject_handled(self, day: date, key: str) -> bool:
        """key = 委托号 + 指纹(见 AccountGuardRule._reject_key)。只有委托号会在柜台复用 SessionID、
        客户端 OrderRef 重数时撞号,把另一笔【新】废单当成已处理 ⇒ 漏锁;指纹不符就查不到 = 当新废单。"""
        return key in self.handled_rejects.get(day.isoformat(), ())

    def mark_reject_handled(self, day: date, key: str) -> None:
        """记下「这笔废单已处理完」。必须在锁定决策**之后**调用:先记后锁,中间崩溃会让该废单永远不再触发锁定。"""
        ids = self.handled_rejects.setdefault(day.isoformat(), [])
        if key in ids:
            return
        ids.append(key)
        del ids[:-HANDLED_REJECTS_PER_DAY]
        for old in sorted(self.handled_rejects)[:-HANDLED_REJECTS_DAYS]:
            del self.handled_rejects[old]
        self._save()

    # ---------- 持久化 ----------

    def _save(self) -> None:
        if self.state_path is None:
            return
        state = {
            "account_id": self.account_id,
            "last_equity": self.last_equity,
            # 采样时刻是**北京时间的 naive 值**(beijing_now()),不能直接 `.astimezone()`:
            # 对 naive 值,`.astimezone()` 一律按**本机时区**解释,于是落盘时刻被平移了
            # (北京 − 本地)—— 非北京宿主机上状态页的「权益新鲜度」会一直显示陈旧,
            # 而那是操作员判断「风控还活着吗」的唯一依据(2026-11-16 codex 复核 M5)。
            # 统一走 _stamp_beijing():naive 一律按北京时间解释,带时区的一律换算到北京时间,
            # 写出来永远是 +08:00(2026-11-16 codex 复核 M1:旧的 `.replace(tzinfo=BEIJING)`
            # 对**带时区**的值是「换标签不换时区」,旧文件(-08:00)会被平移 16 小时)。
            "sampled_at": self._stamp_beijing(self.last_sampled_at),   # 带时区
            # 用不了的采样时刻原样留一份(纯留痕,不参与任何判断):值被丢掉时状态页显示
            # 「无法确认」(保守),但值本身留着,运维查得到「格式不对」这个事实。
            "sampled_at_unusable": self.sampled_at_unusable or None,
            # 状态文件格式版本:2 = 时刻字段一律北京时间。旧文件(无此字段)里的
            # hard_locked_at 是**本机本地时间**,混着比交易日会把冷却期放宽(fail-open),
            # 所以旧文件按「触发时间未知」处理(2026-11-16 codex 复核 L2)。
            "state_version": 2,
            "peak_equity": self.peak_equity,
            "day": self.day.isoformat() if self.day else None,
            "day_start_equity": self.day_start_equity,
            "daily_locked_day": self.daily_locked_day.isoformat() if self.daily_locked_day else None,
            "hard_locked": self.hard_locked,
            "hard_reason": self.hard_reason,
            "hard_locked_at": self.hard_locked_at,
            # 旧文件里的 hard_locked_at 是本机本地时间 ⇒ 冷却期算不准。这个标记必须**跟着文件一起存**,
            # 否则下一次 _save() 写上 state_version=2,旧时间戳就被当成北京时间了(标记丢失 = fail-open)。
            "hard_locked_at_legacy": self.hard_locked_at_legacy,
            "unlock_history": self.unlock_history,
            "manual_orders": self.manual_orders,
            "handled_rejects": self.handled_rejects,
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

    @staticmethod
    def _stamp_beijing(ts: datetime | None) -> str | None:
        """把时刻写成**带 +08:00 偏移**的 ISO 串。

        naive 一律按**北京时间**解释(内部契约:naive = 北京时间,见 `beijing_now()`);
        已经带时区的一律换算到北京时区(同一瞬时,换算不丢信息)。
        """
        if ts is None:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=BEIJING)
        else:
            ts = ts.astimezone(BEIJING)
        return ts.isoformat(timespec="seconds")

    @staticmethod
    def _parse_sampled_at(raw) -> datetime | None:
        """纯可观测字段:坏值只丢弃,**绝不能**因此触发『状态文件损坏→硬锁定』。要求带时区,否则无法与其他时刻比较。

        归一成**北京时间 naive**:内部契约一律是 naive 北京时间(见 `beijing_now()`),
        落盘时再统一盖上 +08:00。早先版本落盘用的是 `.astimezone()`,写出来的是**本机偏移**
        (UTC-8 宿主机上是 `-08:00`);若原样带着那个偏移回来,`_save()` 里的
        `.replace(tzinfo=BEIJING)` 就是「换标签不换时区」⇒ 瞬时值平移 16 小时,
        状态页「权益新鲜度」立刻显示成陈旧值 —— 正是当初要修的那个症状
        (2026-11-16 codex 复核 M1)。这里先 `astimezone(BEIJING)` 换算成同一瞬时,再去掉时区。
        """
        if not isinstance(raw, str):
            return None
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if value.tzinfo is None:
            return None
        return value.astimezone(BEIJING).replace(tzinfo=None)

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
            # 以下为后加字段:旧状态文件没有,缺失时取默认值(向后兼容)
            self.hard_locked_at = str(s.get("hard_locked_at", ""))
            # 旧状态文件(没有 state_version)里的 hard_locked_at 是**本机本地时间**(旧代码用
            # datetime.now())。拿它跟北京时间的 now 比交易日,宿主机不在北京时可能**早一个交易日**
            # 判成「已过冷却期」⇒ 冷却期被放宽(fail-open)。生产跑在 TZ=Asia/Shanghai 的容器里
            # (本地 == 北京)影响 ≈ 0,但运维机上有影响。旧文件一律按「触发时间未知」处理:
            # 保守拒绝,需 --force(与既有的「触发时间未知」口径一致)(2026-11-16 codex 复核 L2)。
            # `state_version` 读成数字:字段缺失/为 null/是垃圾值,一律按**旧文件**处理(版本 1)。
            # 不能让一个坏字段把**完好的**状态文件判成「损坏」⇒ 误触硬锁定(误停交易,原因还误导)
            # (2026-11-16 codex 复核 L3)。
            try:
                state_version = int(s.get("state_version") or 1)
            except (TypeError, ValueError):
                state_version = 1
            self.hard_locked_at_legacy = bool(s.get("hard_locked_at_legacy", False)) or (
                bool(self.hard_locked_at) and state_version < 2)
            self.unlock_history = [dict(h) for h in s.get("unlock_history", [])][-UNLOCK_HISTORY_LIMIT:]
            self.manual_orders = {str(k): int(v) for k, v in s.get("manual_orders", {}).items()}
            # 形状不对(损坏/手改)⇒ 一个都没处理过(fail-closed:废单照常判定),不让它把整份状态判成损坏
            raw_handled = s.get("handled_rejects")
            self.handled_rejects = ({str(k): [str(x) for x in v][-HANDLED_REJECTS_PER_DAY:]
                                     for k, v in raw_handled.items() if isinstance(v, list)}
                                    if isinstance(raw_handled, dict) else {})
            raw_sampled = s.get("sampled_at")
            self.last_sampled_at = self._parse_sampled_at(raw_sampled)
            # 用不了的采样时刻**留痕**(纯可观测):值被丢掉本身是保守的(状态页显示「无法确认」),
            # 但值曾经存在、只是格式不对这件事不该被抹掉(2026-11-16 codex 复核 L3)。
            # 留痕必须**跨落盘存活**:只写不读的话,一次 `_save()`(或重启)就把它抹了,
            # 恰好死在运维最需要它的时候(2026-11-16 codex 第八轮复核 L1)。
            if isinstance(raw_sampled, str) and raw_sampled and self.last_sampled_at is None:
                self.sampled_at_unusable = raw_sampled
            else:
                saved_unusable = s.get("sampled_at_unusable")
                self.sampled_at_unusable = (saved_unusable if isinstance(saved_unusable, str)
                                            and saved_unusable else "")
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # 状态文件损坏:宁可保守地进入硬锁定,也不能当作"没有锁"继续交易
            self.hard_locked = True
            self.hard_reason = f"风控状态文件损坏({exc!r}),按硬锁定处理,请人工核对后解锁"
