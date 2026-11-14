"""梯子式量化(Ladder,Blum & Hardt 2015):限制验证窗口的信息经由"反复查询"泄漏给进化回路。

背景见 docs/adr/20261113-evolution-engine.md「阶段 3 修订」:验证窗口一旦被 LLM 反复看到就变成训练集,
DSR 只惩罚试验次数、不惩罚每次试验带回的信息量,而测试段已经消耗过一次,目前没有干净的留出集。
所以对**验证窗口的夏普**,只允许经本模块量化之后才能交给进化回路:

- 仅当新成绩比历史最佳**高出至少一个步长 η** 才更新汇报值(我们是"越大越好"的夏普,与原文方向相反);
- 汇报值取整到 η 的倍数;否则**重复上一次的汇报值**——查询不泄漏任何新信息;
- 另设**总查询预算**:用尽即停止回答(Thresholdout 的思路),续期必须显式、留痕;
- 状态**损坏、被篡改或被回滚即 fail-closed**:读不出可信的状态就不汇报,而不是从头开始。

η = 1.10 夏普,在 ADR「阶段 3 前置」里登记于任何候选生成之前(`scripts/calc_ladder_eta.py` 可复跑)。
它和 DSR ≥ 0.95 是同一类东西——看到结果之前定死的量。所以:
- η 与预算上限写在**代码常量**里;状态里的值只能**等于/更紧**,更松一律按损坏处理(与宪法清单"只能追加"同一思路);
- η 是**汇报阈值**,不是晋级门槛,与 DSR 互相独立。

起点:汇报值从 0.0 开始(含义是"还没有超过 0 一个步长"),所以夏普不到 η 的候选始终读到 0.0。

**持久化(第 15 轮 codex 的 High)**:状态曾是一个 JSON 文件,只校验形状——把 `queries` 改小、把 `best` 改小都被接受,
等于每回滚一次就多买一次验证窗口的量化信息,而且续期与回滚无法区分。现在是:
- `ladder.jsonl`:**只追加、带哈希链**的日志,每次提交/续期追加一行(`seq`、`event`、`run_id`、`queries`、`best`、`budget`、`note`、
  `prev`、`hash`);读取时逐行核对哈希链与状态转移(`queries` 每次恰好 +1、`best` 只升且每次至少一个台阶、续期不改 `best`);
- `ladder.json`:头指针(`seq` 与最后一行的哈希),必须与日志对得上——改了其中一个就对不上;日志被截断(头指针超前)同样被发现;
- 头指针只**落后一行**(日志已追加、头指针还没写完就崩了)时按日志修复头指针——日志是事实来源;
- **续期是显式的**:`renew(note)` 追加一条 `renew` 事件,重置 `queries`、**保留 `best`**(已经泄漏的信息收不回来),
  必须给出非空的原因——"人工续期没有审计钩子"这条残余因此关掉了。
- **同一个 `run_id` 的提交是幂等的**:已经提交过的 run 只回放当时的汇报值,不再扣查询。这让"揭示存档写失败后重试"安全
  (不会重复扣查询、也没有新的信息泄漏——重试本来就拿得到同样的值),见 risk/outward.py。

**锚点(第 16 轮 codex 的 H2)**:哈希链**没有密钥**,头指针与日志又在同一目录——"截断日志并同步改写头指针"得到的是一条
完全合法的更短的链,链内的所有不变式(`queries` +1、`best` 只升)它都满足,所以只靠链**检测不到一致回滚**(上一版注释与 ADR
说"回滚/截断 fail-closed"是过度声明)。现在可以给梯子配一个**锚点文件**(`anchor`,放在另一个目录,每次追加写一行
`<seq> <hash>`):读取时要求日志里第 `seq` 行的哈希恰好是锚点记下的那个——日志被截到锚点之前、或整条链被换成另一条,
都会对不上。锚点只会**落后**日志(崩溃在写锚点之前),不会超前,所以"锚点超前"就是回滚。

诚实的边界:锚点不是信任边界——它只是把"一致回滚"从**改两个文件**变成**改三个、其中一个在别的目录**,并让部分篡改与
误操作被发现。拥有文件系统写权限的人**同时**改写日志、头指针与锚点、或把三者全部删掉(整条梯子从头开始),**仍然发现不了**;
真正的外部锚点需要另一个信任域(密钥不在候选与网关可写的位置,或把头指针镜像到只增的外部存储),这里没有。
候选代码碰不到这些文件(沙盒里没有 `open`、没有文件系统模块,见 ADR)。

并发:同一进程内用锁串行;跨进程不加锁。网关用 `_busy` 锁串行执行作业,CLI 与网关并发才可能漏记查询(偏向少记),
与试验账本同一口径,列入 ADR 已知限制。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path

LADDER_ETA = 1.10              # ADR 已登记(2026-11-13),不得为让候选更容易汇报而改小
DEFAULT_QUERY_BUDGET = 60      # 总查询预算上限:状态里的预算只能 ≤ 它(实现时所定,规格没给数字,见 ADR)
STATE_VERSION = 2
GENESIS = "0" * 64
MAX_NOTE = 200
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_EPS = 1e-9
_RECORD_KEYS = {"seq", "event", "run_id", "queries", "best", "budget", "note", "prev", "hash"}
_HEAD_KEYS = {"version", "seq", "hash", "queries", "best", "budget", "eta"}
_lock = threading.Lock()


def anchor_path_for(results_dir) -> Path:
    """网关与命令行共用的锚点位置:与结果目录**不同**的目录(`data/ladder_anchor/`),候选与结果读写路径都碰不到。"""
    return Path(results_dir).parent / "ladder_anchor" / "ladder.anchor"


class LadderError(Exception):
    """梯子状态损坏、被篡改或回滚、写入失败,或参数想放宽已登记的量。"""


class LadderExhausted(LadderError):
    """总查询预算已用尽:不再给出任何汇报值,需要人工续期(`renew`)。"""


def _why(exc: BaseException) -> str:
    return type(exc).__name__ if isinstance(exc, OSError) else str(exc)


def _is_multiple(value: float, eta: float) -> bool:
    steps = value / eta
    return abs(steps - round(steps)) < 1e-6


def round_to_step(value: float, eta: float = LADDER_ETA) -> float:
    """取整到 η 的最近倍数(去掉浮点尾巴)。"""
    return round(round(value / eta) * eta, 10)


def _digest(prev: str, body: dict) -> str:
    payload = prev + "|" + json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _body(record: dict) -> dict:
    return {k: record[k] for k in _RECORD_KEYS - {"hash"}}


class Ladder:
    """持久化的梯子。`path` 是头指针文件;日志在同目录同名的 `.jsonl`。"""

    def __init__(self, path: Path, *, eta: float = LADDER_ETA, budget: int = DEFAULT_QUERY_BUDGET,
                 anchor: Path | None = None) -> None:
        if isinstance(eta, bool) or not isinstance(eta, (int, float)) or not math.isclose(eta, LADDER_ETA, abs_tol=1e-12):
            raise LadderError(f"η 已登记为 {LADDER_ETA},不得更改(看到候选之后改它等于放宽阈值)")
        if isinstance(budget, bool) or not isinstance(budget, int) or not 1 <= budget <= DEFAULT_QUERY_BUDGET:
            raise LadderError(f"总查询预算必须是 1..{DEFAULT_QUERY_BUDGET} 的整数(上限已登记,不得放宽)")
        self.path = Path(path)
        self.log_path = self.path.with_suffix(".jsonl")
        self.anchor = Path(anchor) if anchor is not None else None
        self.eta = float(eta)
        self.budget = budget

    # ---------- 读:日志 + 头指针互相对账 ----------

    def _read_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            lines = [line for line in self.log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            rows = [json.loads(line) for line in lines]
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子日志损坏或无法读取(fail-closed): {_why(exc)}") from exc
        prev_hash, prev = GENESIS, {"queries": 0, "best": 0.0, "budget": DEFAULT_QUERY_BUDGET, "seq": 0}
        for row in rows:
            self._check_record(row, prev, prev_hash)
            prev_hash, prev = row["hash"], row
        return rows

    def _check_record(self, row: object, prev: dict, prev_hash: str) -> None:
        def bad(reason: str):
            raise LadderError(f"梯子日志不合法(fail-closed): {reason}")

        if not isinstance(row, dict) or set(row) != _RECORD_KEYS:
            bad("字段不符")
        for name in ("seq", "queries", "budget"):
            if isinstance(row[name], bool) or not isinstance(row[name], int):
                bad(f"{name} 不是整数")
        best = row["best"]
        if isinstance(best, bool) or not isinstance(best, (int, float)) or not math.isfinite(best) or best < 0 \
                or not _is_multiple(best, LADDER_ETA):
            bad("best 必须是 η 的非负整数倍")
        if row["event"] not in ("submit", "renew"):
            bad("未知事件")
        if row["run_id"] is not None and (not isinstance(row["run_id"], str) or not RUN_ID.fullmatch(row["run_id"])):
            bad("run_id 不合法")
        if row["note"] is not None and (not isinstance(row["note"], str) or len(row["note"]) > MAX_NOTE):
            bad("note 不合法")
        if row["seq"] != prev["seq"] + 1:
            bad("seq 不连续(日志被截断或插入)")
        if row["prev"] != prev_hash or row["hash"] != _digest(prev_hash, _body(row)):
            bad("哈希链对不上(日志被改写)")
        if not 1 <= row["budget"] <= min(DEFAULT_QUERY_BUDGET, prev["budget"]):
            bad("预算超出已登记的上限或比之前放宽了(只能更紧,不能更松)")
        if row["event"] == "submit":
            if row["queries"] != prev["queries"] + 1 or row["queries"] > row["budget"]:
                bad("queries 必须每次恰好 +1 且不超过预算(被回滚或越权)")
            if row["best"] != prev["best"] and row["best"] < prev["best"] + LADDER_ETA - _EPS:
                bad("best 只能不变或至少升一个台阶")
            if row["note"] is not None:
                bad("提交事件不带 note")
        else:
            if row["best"] != prev["best"]:
                bad("续期不得改动 best(已经泄漏的信息收不回来)")
            if row["queries"] != 0 or not row["note"] or not row["note"].strip():
                bad("续期必须把 queries 归零并给出非空原因")
            if row["run_id"] is not None:
                bad("续期事件不带 run_id")

    def _head(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            head = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子头指针损坏或无法读取(fail-closed): {_why(exc)}") from exc
        if not isinstance(head, dict) or set(head) != _HEAD_KEYS or head["version"] != STATE_VERSION:
            raise LadderError("梯子头指针不合法(fail-closed)")
        return head

    def _read_anchor(self) -> tuple[int, str] | None:
        if self.anchor is None or not self.anchor.exists():
            return None
        try:
            lines = [line for line in self.anchor.read_text(encoding="utf-8").splitlines() if line.strip()]
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子锚点无法读取(fail-closed): {_why(exc)}") from exc
        if not lines:
            return None
        parts = lines[-1].split(" ")
        if len(parts) != 2 or not parts[0].isdigit() or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
            raise LadderError("梯子锚点格式不合法(fail-closed)")
        return int(parts[0]), parts[1]

    def _check_anchor(self, rows: list[dict], repair: bool) -> None:
        if self.anchor is None:
            return
        anchored = self._read_anchor()
        if anchored is None:
            if not rows:
                return
            if len(rows) == 1:                          # 第一条日志已追加、锚点还没写完就崩了
                if repair:
                    self._write_anchor(rows[0])
                return
            raise LadderError("梯子有日志却没有锚点(fail-closed):锚点被删过,或日志是被换进来的")
        seq, digest = anchored
        if len(rows) < seq or rows[seq - 1]["hash"] != digest:
            raise LadderError("梯子日志与锚点对不上(fail-closed):日志被回滚、截断或整条换掉")

    def _write_anchor(self, record: dict) -> None:
        if self.anchor is None:
            return
        try:
            self.anchor.parent.mkdir(parents=True, exist_ok=True)
            with open(self.anchor, "a", encoding="utf-8", newline="") as stream:
                stream.write(f"{record['seq']} {record['hash']}" + chr(10))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise LadderError(f"梯子锚点写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc

    def _load(self, repair: bool = True) -> list[dict]:
        """读日志并与头指针、锚点对账。两者都不存在 = 全新的梯子;任何对不上 = 损坏/篡改/回滚,抛 LadderError。

        `repair=False` 用于只读入口(status/has/preview):它们**不写任何文件**(第 16 轮 L2:"只读"的命令不该改状态)。"""
        rows = self._read_log()
        self._check_anchor(rows, repair)
        head = self._head()
        if not rows and head is None:
            return []
        if head is None:
            if len(rows) == 1:                          # 第一条日志已追加、头指针还没写完就崩了:按日志修复
                if repair:
                    self._write_head(rows[0])
                return rows
            raise LadderError("梯子有日志却没有头指针(fail-closed):状态被删改过")
        if not rows:
            raise LadderError("梯子有头指针却没有日志(fail-closed):日志被删除或截断")
        last = rows[-1]
        if head["seq"] == last["seq"] and head["hash"] == last["hash"] and not (
                head["queries"] == last["queries"] and head["best"] == last["best"] and head["budget"] == last["budget"]):
            raise LadderError("梯子头指针里的 queries/best/budget 与日志最后一行对不上(fail-closed):头指针被单独改写过")
        if head["seq"] == last["seq"] - 1 and len(rows) >= 2 and head["hash"] == rows[-2]["hash"]:
            if repair:
                self._write_head(last)                  # 头指针只落后一行:日志已追加、头指针还没写完——按日志修复
        elif head["seq"] != last["seq"] or head["hash"] != last["hash"]:
            raise LadderError("梯子头指针与日志对不上(fail-closed):被回滚、截断或单独改写过")
        return rows

    # ---------- 写 ----------

    def _write_head(self, last: dict) -> None:
        head = {"version": STATE_VERSION, "seq": last["seq"], "hash": last["hash"], "queries": last["queries"],
                "best": last["best"], "budget": last["budget"], "eta": self.eta}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(json.dumps(head, ensure_ascii=False, allow_nan=False))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            raise LadderError(f"梯子头指针写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc

    def _append(self, rows: list[dict], event: str, *, run_id: str | None, queries: int, best: float, note: str | None) -> dict:
        prev = rows[-1] if rows else {"seq": 0, "hash": GENESIS, "budget": self.budget}
        record = {"seq": prev["seq"] + 1, "event": event, "run_id": run_id, "queries": queries, "best": best,
                  "budget": min(prev["budget"], self.budget), "note": note, "prev": prev["hash"], "hash": ""}
        record["hash"] = _digest(prev["hash"], _body(record))
        size_before = self.log_path.stat().st_size if self.log_path.exists() else 0
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8", newline="") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + chr(10))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # 那一行可能已经写进了文件(fsync 才失败):截回去。否则调用方拿到的是错误、状态却悄悄前进了——
            # 等于一次没放出汇报值却扣了的查询,还会把这次成绩的量化下界带给下一次(第 15 轮 codex 的 High 2 同类)。
            try:
                with open(self.log_path, "r+b") as stream:
                    stream.truncate(size_before)
            except OSError:
                pass                                    # 截不回去也不能假装成功:下面照样抛错;同一个 run 重试按 run_id 幂等回放
            raise LadderError(f"梯子日志写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc
        self._write_head(record)
        self._write_anchor(record)
        return record

    # ---------- 状态与提交 ----------

    @staticmethod
    def _state(rows: list[dict], budget: int) -> dict:
        if not rows:
            return {"queries": 0, "best": 0.0, "budget": budget}
        last = rows[-1]
        return {"queries": last["queries"], "best": float(last["best"]), "budget": min(last["budget"], budget)}

    def _step(self, state: dict, sharpe) -> tuple[float, bool]:
        best = state["best"]
        if (isinstance(sharpe, (int, float)) and not isinstance(sharpe, bool) and math.isfinite(sharpe)
                and sharpe >= best + self.eta - _EPS):
            candidate = round_to_step(sharpe, self.eta)
            if candidate >= best + self.eta - _EPS:             # 取整后仍必须真的上了一个台阶
                return candidate, True
        return best, False

    def _result(self, state: dict, best: float, updated: bool, queries: int, replay: bool = False) -> dict:
        return {"reported": best, "updated": updated, "step": self.eta, "queries_used": queries,
                "queries_left": state["budget"] - queries, "budget": state["budget"], "replay": replay}

    def status(self) -> dict:
        """当前状态(只读,**不消耗查询**):汇报值、已用/剩余查询数。"""
        with _lock:
            state = self._state(self._load(repair=False), self.budget)
        return {"reported": state["best"], "step": self.eta, "queries_used": state["queries"],
                "queries_left": state["budget"] - state["queries"], "budget": state["budget"]}

    def has(self, run_id: str) -> bool:
        """这个 run 是否已经提交过(只读,不消耗查询)。"""
        with _lock:
            return any(r["run_id"] == run_id for r in self._load(repair=False))

    def preview(self, sharpe, run_id: str | None = None) -> dict:
        """`submit` 会返回什么——但**不写任何东西、不消耗查询**。预算用尽(且这个 run 没提交过)抛 LadderExhausted。"""
        with _lock:
            rows = self._load(repair=False)
            return self._plan(rows, sharpe, run_id)

    def _plan(self, rows: list[dict], sharpe, run_id: str | None) -> dict:
        if run_id is not None:
            for row in rows:
                if row["run_id"] == run_id:                      # 幂等:同一个 run 只回放当时的汇报值,不再扣查询
                    state = self._state(rows, self.budget)
                    return self._result(state, float(row["best"]), False, state["queries"], replay=True)
        state = self._state(rows, self.budget)
        if state["queries"] >= state["budget"]:
            raise LadderExhausted(f"梯子的总查询预算({state['budget']} 次)已用尽,需要人工续期(renew,会留痕)")
        best, updated = self._step(state, sharpe)
        return self._result(state, best, updated, state["queries"] + 1)

    def submit(self, sharpe, run_id: str | None = None) -> dict:
        """提交一次验证窗口夏普,返回量化后的汇报值。每次调用消耗一次查询;预算用尽抛 LadderExhausted。

        - 新成绩 ≥ 历史最佳 + η:更新,汇报值取整到 η 的倍数;否则(含负数、NaN、inf):汇报值不变。
          非有限输入按"没有提升"处理,但**仍计入一次查询**;
        - 给了 `run_id`:同一个 run 重复提交是**幂等**的(回放当时的汇报值,不扣查询)。"""
        if run_id is not None and (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)):
            raise LadderError("run_id 不合法")
        with _lock:
            rows = self._load()
            plan = self._plan(rows, sharpe, run_id)
            if plan["replay"]:
                return plan
            self._append(rows, "submit", run_id=run_id, queries=plan["queries_used"], best=plan["reported"], note=None)
            return plan

    def renew(self, note: str, *, force: bool = False) -> dict:
        """人工续期:`queries` 归零、**保留 `best`**,必须给出非空原因,留一条审计记录。

        默认**只在预算用尽之后**才允许(第 16 轮 M3:否则"总预算"其实是"每次续期的量",随时可以重置);
        `force=True` 强制续期,审计记录里带"[强制]"前缀。"""
        if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE:
            raise LadderError(f"续期必须给出 1..{MAX_NOTE} 字符的原因")
        with _lock:
            rows = self._load()
            state = self._state(rows, self.budget)
            if not force and state["queries"] < state["budget"]:
                raise LadderError(f"预算还没用尽({state['queries']}/{state['budget']}),不得续期;确需提前续期请显式 force(审计里会标注)")
            text = note.strip() if not force else "[强制] " + note.strip()
            if len(text) > MAX_NOTE:
                raise LadderError(f"续期原因加前缀后超过 {MAX_NOTE} 字符")
            record = self._append(rows, "renew", run_id=None, queries=0, best=state["best"], note=text)
            return {"reported": float(record["best"]), "step": self.eta, "queries_used": 0,
                    "queries_left": record["budget"], "budget": record["budget"]}
