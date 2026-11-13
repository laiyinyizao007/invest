"""梯子式量化(Ladder,Blum & Hardt 2015):限制验证窗口的信息经由"反复查询"泄漏给进化回路。

背景见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:验证窗口一旦被 LLM 反复看到就变成训练集,
DSR 只惩罚试验次数、不惩罚每次试验带回的信息量,而测试段已经消耗过一次,目前没有干净的留出集。
所以对**验证窗口的夏普**,只允许经本模块量化之后才能交给进化回路:

- 仅当新成绩比历史最佳**高出至少一个步长 η** 才更新汇报值(我们是"越大越好"的夏普,与原文方向相反);
- 汇报值取整到 η 的倍数;否则**重复上一次的汇报值**——查询不泄漏任何新信息;
- 另设**总查询预算**:用尽即停止回答(Thresholdout 的思路),需要人工续期,不会静默放开;
- 状态(历史最佳、已用查询数)持久化,**损坏即 fail-closed**:读不出状态就不汇报,而不是从头开始。

η = 1.10 夏普,在 ADR「阶段 3 前置」里登记于任何候选生成之前(`scripts/calc_ladder_eta.py` 可复跑)。
它和 DSR ≥ 0.95 是同一类东西——看到结果之前定死的量。所以:
- η 与预算上限写在**代码常量**里;状态文件里的值只能**等于/更紧**,更松一律按损坏处理(与宪法清单"只能追加"同一思路);
- η 是**汇报阈值**,不是晋级门槛,与 DSR 互相独立。

起点:汇报值从 0.0 开始(含义是"还没有超过 0 一个步长"),所以夏普不到 η 的候选始终读到 0.0——
这正是想要的:进化回路读不到验证窗口里低于一个步长的差异。

并发:同一进程内用锁串行;跨进程不加锁。网关用 `_busy` 锁串行执行作业,CLI 与网关并发才可能,
方向是**可能漏记查询**(偏向少记),与试验账本同一口径,列入 ADR 已知限制。
"""
from __future__ import annotations

import json
import math
import os
import threading
from pathlib import Path

LADDER_ETA = 1.10              # ADR 已登记(2026-11-13),不得为让候选更容易汇报而改小
DEFAULT_QUERY_BUDGET = 60      # 总查询预算上限:状态文件里的预算只能 ≤ 它
STATE_VERSION = 1
_EPS = 1e-9
_lock = threading.Lock()


class LadderError(Exception):
    """梯子状态损坏、不可读、写入失败或参数想放宽已登记的量。"""


class LadderExhausted(LadderError):
    """总查询预算已用尽:不再给出任何汇报值,需要人工续期。"""


def _is_multiple(value: float, eta: float) -> bool:
    steps = value / eta
    return abs(steps - round(steps)) < 1e-6


def round_to_step(value: float, eta: float = LADDER_ETA) -> float:
    """取整到 η 的最近倍数(去掉浮点尾巴)。"""
    return round(round(value / eta) * eta, 10)


class Ladder:
    """持久化的梯子。每次 `submit` 消耗一次查询,先落盘再返回汇报值。"""

    def __init__(self, path: Path, *, eta: float = LADDER_ETA, budget: int = DEFAULT_QUERY_BUDGET) -> None:
        if isinstance(eta, bool) or not isinstance(eta, (int, float)) or not math.isclose(eta, LADDER_ETA, abs_tol=1e-12):
            raise LadderError(f"η 已登记为 {LADDER_ETA},不得更改(看到候选之后改它等于放宽阈值)")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= DEFAULT_QUERY_BUDGET:
            raise LadderError(f"总查询预算必须是 1..{DEFAULT_QUERY_BUDGET} 的整数(上限已登记,不得放宽)")
        self.path = Path(path)
        self.eta = float(eta)
        self.budget = budget

    # ---------- 状态读写 ----------

    def _fresh(self) -> dict:
        return {"version": STATE_VERSION, "eta": self.eta, "budget": self.budget, "queries": 0, "best": 0.0}

    def _load(self) -> dict:
        """读状态;文件不存在 = 全新的梯子;存在但不合法 = 损坏,抛 LadderError(fail-closed)。"""
        if not self.path.exists():
            return self._fresh()
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子状态损坏或无法读取(fail-closed): {type(exc).__name__ if isinstance(exc, OSError) else exc}") from exc
        self._validate(state)
        return state

    def _validate(self, state: object) -> None:
        def bad(reason: str):
            raise LadderError(f"梯子状态损坏(fail-closed): {reason}")

        if not isinstance(state, dict) or set(state) != {"version", "eta", "budget", "queries", "best"}:
            bad("字段不符")
        if state["version"] != STATE_VERSION:
            bad("版本不符")
        eta = state["eta"]
        if isinstance(eta, bool) or not isinstance(eta, (int, float)) or not math.isclose(eta, LADDER_ETA, abs_tol=1e-12):
            bad(f"η 与已登记的 {LADDER_ETA} 不一致")
        budget, queries, best = state["budget"], state["queries"], state["best"]
        for name, value in (("budget", budget), ("queries", queries)):
            if isinstance(value, bool) or not isinstance(value, int):
                bad(f"{name} 不是整数")
        if not 1 <= budget <= DEFAULT_QUERY_BUDGET:
            bad("预算超出已登记的上限(只能更紧,不能更松)")
        if not 0 <= queries <= budget:
            bad("已用查询数越界")
        if isinstance(best, bool) or not isinstance(best, (int, float)) or not math.isfinite(best) or best < 0:
            bad("历史最佳不是非负有限数")
        if not _is_multiple(best, LADDER_ETA):
            bad("历史最佳不是 η 的整数倍(汇报值只能是步长的倍数)")

    def _save(self, state: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(state, ensure_ascii=False, allow_nan=False))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise LadderError(f"梯子状态写入失败(fail-closed,不放出汇报值): {type(exc).__name__}") from exc

    # ---------- 对外接口 ----------

    def status(self) -> dict:
        """当前状态(只读,**不消耗查询**):汇报值、已用/剩余查询数。"""
        with _lock:
            state = self._load()
        return {"reported": float(state["best"]), "step": self.eta, "queries_used": state["queries"],
                "queries_left": state["budget"] - state["queries"], "budget": state["budget"]}

    def submit(self, sharpe: float) -> dict:
        """提交一次验证窗口夏普,返回量化后的汇报值。每次调用消耗一次查询;预算用尽抛 LadderExhausted。

        - 新成绩 ≥ 历史最佳 + η:更新,汇报值取整到 η 的倍数;
        - 否则(含负数、NaN、inf):汇报值不变。非有限输入按"没有提升"处理,但**仍计入一次查询**。
        """
        with _lock:
            state = self._load()
            if state["queries"] >= state["budget"]:
                raise LadderExhausted(f"梯子的总查询预算({state['budget']} 次)已用尽,需要人工续期")
            best = float(state["best"])
            updated = False
            if (isinstance(sharpe, (int, float)) and not isinstance(sharpe, bool) and math.isfinite(sharpe)
                    and sharpe >= best + self.eta - _EPS):
                candidate = round_to_step(sharpe, self.eta)
                if candidate >= best + self.eta - _EPS:         # 取整后仍必须真的上了一个台阶
                    best, updated = candidate, True
            state = {**state, "queries": state["queries"] + 1, "best": best}
            self._save(state)                                  # 先落盘再放出汇报值
        return {"reported": best, "updated": updated, "step": self.eta, "queries_used": state["queries"],
                "queries_left": state["budget"] - state["queries"], "budget": state["budget"]}
