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

**锚点(第 16、17 轮 codex 的 H2)**:哈希链**没有密钥**,头指针与日志又在同一目录——"截断日志并同步改写头指针"得到的是一条
完全合法的更短的链,链内的所有不变式(`queries` +1、`best` 只升)它都满足,所以只靠链**检测不到一致回滚**(上一版注释与 ADR
说"回滚/截断 fail-closed"是过度声明)。现在可以给梯子配一个**锚点文件**(`anchor`,放在另一个目录,每次追加写一行
`<seq> <hash>`):读取时要求日志里第 `seq` 行的哈希恰好是锚点记下的那个——日志被截到锚点之前、或整条链被换成另一条,
都会对不上。锚点只会**落后**日志(崩溃在写锚点之前),不会超前,所以"锚点超前"就是回滚。

诚实的边界(第 17 轮更正):锚点是**误操作与部分篡改的探测器,不是回滚探测器**——它与日志同一个信任域,
把日志、头指针**和锚点一起截到同一位置**(不需要重算任何哈希)就是一条完全自洽的更短的链;只截日志或只换链才会被发现。
拥有文件系统写权限的人同时改写三者、或把三者全部删掉(整条梯子从头开始),**发现不了**;
真正的外部锚点需要另一个信任域(密钥不在候选与网关可写的位置,或把头指针镜像到只增的外部存储),这里没有。
候选代码碰不到这些文件(沙盒里没有 `open`、没有文件系统模块,见 ADR)。

并发:同一进程内用锁串行;**跨进程**也有互斥(第 19 轮加的 `_file_lock`,`submit`/`renew`/`reset` 互斥,
Windows `msvcrt.locking` / POSIX `fcntl.flock`;写入前还会重读磁盘核对尾部哈希),所以此前"reset 与 submit 并发
能把日志写成断链、梯子永久报废"的问题已解。剩下的窄口:网关用 `_busy` 锁串行作业,而**试验账本
`risk/trial_ledger.py` 仍是同进程内加锁、跨进程不加锁**——CLI 与网关并发可能漏记极少量试验(偏向少记),
列入 ADR 已知限制。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import threading
import time
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


@contextlib.contextmanager
def _file_lock(path: Path, timeout: float = 10.0):
    """跨进程互斥锁(第 19 轮 codex 的 M2)。

    同一进程内已有 `threading.Lock`,这一层补的是**跨进程**的:CLI 的 `reset`/`renew` 与网关的 `submit`
    可能同时操作同一个梯子。少了它,`reset` 把日志挪走之后,网关里那次已经读到旧状态的 `submit`
    会用陈旧的 `rows` 算出 seq=4 写进空日志,之后每次读都报"seq 不连续"——**梯子永久报废**,
    而这恰好是运维看到 500 之后最可能走的那条修砖路径。

    锁的是梯子自己的状态文件旁边的一个 `.lock` 文件,不是梯子状态本身(锁文件不参与对账,
    删掉它也只是退回到"跨进程不加锁"的老口径)。Windows 用 `msvcrt.locking`,POSIX 用 `fcntl.flock`。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(path, "a+b")
    except OSError as exc:
        # 锁文件建不出来(目录被占、权限不足……)也必须 fail-closed,且**不泄露本地路径**。
        raise LadderError(f"梯子锁文件无法创建(fail-closed,未做任何修改): {_why(exc)}") from exc
    try:
        # 锁的是**第 0 个字节**。Windows 的 msvcrt.locking 锁的是字节区间,越界会报错,
        # 所以文件至少要有 1 个字节;写入后必须 flush 并落盘,否则另一个进程可能按尚未落盘的长度去锁。
        # (两个进程同时初始化时可能各写一个字节,文件长到 2 字节——不影响锁的语义。)
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            try:
                stream.write(bytes([0]))
                stream.flush()
                os.fsync(stream.fileno())
            except OSError as exc:
                # 锁文件的字节写不进去(磁盘满、权限、共享冲突……)时必须 fail-closed,
                # 且报的是"梯子不可用"而不是别的——第 20 轮 codex 的 L2:
                # 这里原本裸调 `stream.write`,OSError 会一路冒到网关,变成"内部错误",
                # 运维看不出是梯子锁坏了。
                raise LadderError(f"梯子锁文件无法写入(fail-closed,未做任何修改): {_why(exc)}") from exc
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise LadderError("梯子正被另一个进程占用(fail-closed,未做任何修改)")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        stream.close()


class LadderExhausted(LadderError):
    """总查询预算已用尽:不再给出任何汇报值,需要人工续期(`renew`)。"""


def _why(exc: BaseException) -> str:
    return type(exc).__name__ if isinstance(exc, OSError) else str(exc)


# Windows 上"别的进程正打开着这个文件"是**瞬时**竞争,不是权限问题:
# 写者的 `os.replace` 会 PermissionError,替换进行中的读者打开也会 PermissionError。
# 两边都短重试一下就好;真的没权限/文件损坏,重试完照样 fail-closed。
_TRANSIENT_ATTEMPTS = 10
_TRANSIENT_DELAY = 0.02


def _replace_atomic(src: Path, dst: Path) -> None:
    """原子替换,顺带把 Windows 的瞬时占用重试掉(见上面的说明)。"""
    last: OSError | None = None
    for _ in range(_TRANSIENT_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:            # Windows:目标文件正被别的进程打开
            last = exc
            time.sleep(_TRANSIENT_DELAY)
    raise last if last is not None else OSError("原子替换失败")


def _read_bytes_retry(path: Path) -> bytes:
    """读文件,顺带把 Windows 的瞬时占用重试掉(见上面的说明)。"""
    last: OSError | None = None
    for _ in range(_TRANSIENT_ATTEMPTS):
        try:
            return path.read_bytes()
        except PermissionError as exc:            # Windows:文件正被替换
            last = exc
            time.sleep(_TRANSIENT_DELAY)
    raise last if last is not None else OSError("读取失败")


def _read_text_retry(path: Path) -> str:
    """读文本文件,顺带把 Windows 的瞬时占用重试掉(见上面的说明)。"""
    last: OSError | None = None
    for _ in range(_TRANSIENT_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError as exc:            # Windows:文件正被替换
            last = exc
            time.sleep(_TRANSIENT_DELAY)
    raise last if last is not None else OSError("读取失败")


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


def _split_lines(data: bytes) -> tuple[list[str], bool]:
    """按"记录以 \\n 结尾"的规则切分字节内容(第 20 轮 codex 的 L3)。

    返回 `(行列表, 文件是否以换行结尾)`。

    - **只有 \\n 是行分隔符**:与按字节找最后一个 \\n 的撕裂判定(`_trim_torn_tail`)同一口径。
      之前读日志/锚点走的是文本模式的通用换行,单独的 \\r 也会被当成行分隔——
      于是"读"与"按字节截尾"两边对同一个文件的行数看法不一致:锚点最后一条**完整**见证
      被当成撕裂截掉,梯子从此对不上账。
    - 行尾允许一个 \\r(CRLF 兼容),行内的 \\r 不特殊处理,交给解析器判它不合法。"""
    text = data.decode("utf-8")           # 解码失败是 ValueError,交给调用方 fail-closed
    ended = not text or text.endswith(chr(10))
    parts = text.split(chr(10))
    if parts and parts[-1] == "":
        parts.pop()                       # 文件以换行结尾:最后一段是空的
    return [p[:-1] if p.endswith(chr(13)) else p for p in parts], ended


def _parse_log_line(text: str) -> dict:
    """一行日志是不是**完整**的记录(第 19 轮 M1:用来区分"撕裂"与"完整但缺换行")。

    撕裂(崩在追加中途)截出来的是 JSON 的前缀,`json.loads` 会抛;
    完整但缺换行的那一行 `json.loads` 是好的——两者靠"能不能解析"分开。
    这里还额外要求字段齐全:解析成功但字段不全的半条记录不算完整。"""
    row = json.loads(text)
    if not isinstance(row, dict) or set(row) != _RECORD_KEYS:
        raise ValueError("日志行的字段不完整")
    return row


def _parse_anchor_line(text: str) -> tuple[int, str]:
    """一行锚点是不是**完整**的 `<seq> <hash>`(第 19 轮 L4,同上)。

    行尾允许一个 \\r(第 20 轮 codex 的 L3):撕裂判定是按字节找最后一个 \\n,
    "完整但缺换行"的末行原文会带着 \\r 进来,不容忍它就会把**完整**的末行误判成撕裂并截掉。"""
    parts = text.strip(chr(13) + chr(10)).split(" ")
    if len(parts) != 2 or not parts[0].isdigit() or not re.fullmatch(r"[0-9a-f]{64}", parts[1]):
        raise ValueError("锚点行不完整")
    seq = int(parts[0])
    if seq < 1:
        raise ValueError("锚点序号必须从 1 起")
    return seq, parts[1]


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
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")   # 跨进程互斥锁(第 19 轮 M2)
        self.anchor = Path(anchor) if anchor is not None else None
        self.eta = float(eta)
        self.budget = budget

    # ---------- 读:日志 + 头指针互相对账 ----------

    def _read_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        try:
            raw, ended = _split_lines(_read_bytes_retry(self.log_path))
            lines = [line for line in raw if line.strip()]
            rows: list[dict] = []
            for index, line in enumerate(lines):
                try:
                    rows.append(_parse_log_line(line))
                except ValueError as exc:
                    # 只有**末行**、且文件不以换行结尾时才是撕裂(崩在追加中途),那半行不算(第 17 轮 M3);
                    # 其余情况一律损坏(fail-closed)。"完整但缺换行"解析得出来,会被保留(第 19 轮 M1)。
                    if index == len(lines) - 1 and not ended:
                        break
                    raise LadderError(f"梯子日志损坏或无法读取(fail-closed): {_why(exc)}") from exc
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

    def _head_token(self) -> tuple[int, str] | None:
        """头指针的**变更令牌** `(seq, hash)`(第 20 轮 codex 的 M1)。

        用来判断"快照读完之后梯子有没有被动过":头指针是独立的小文件,读一次很便宜,
        不用像日志那样整条重读并逐条重算 SHA-256(第 19 轮 codex 的 L3 的性能目标因此得以保留)。
        梯子被 `reset` 之后头指针被挪走,令牌变成 None——这正好也是"快照已过期"的信号。"""
        head = self._head()
        return None if head is None else (head["seq"], head["hash"])

    def _head(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            head = json.loads(_read_text_retry(self.path))
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子头指针损坏或无法读取(fail-closed): {_why(exc)}") from exc
        if not isinstance(head, dict) or set(head) != _HEAD_KEYS or head["version"] != STATE_VERSION:
            raise LadderError("梯子头指针不合法(fail-closed)")
        return head

    def _read_anchor(self) -> tuple[tuple[int, str] | None, str | None]:
        """返回 ((seq, hash) 或 None, 撕裂的末行碎片原文 或 None)。

        碎片原文要留着:锚点末行崩在追加中途时,那半行里仍然带着**它本来要见证的那条记录的哈希前缀**
        (锚点写的是 `<seq> <hash>`)。用它跟日志里的哈希对前缀,就能知道"锚点本来见证的是哪一条、
        日志那一条有没有被改过"——而不是把日志自己的哈希当成见证(那是拿日志证明日志,白查)。"""
        if self.anchor is None or not self.anchor.exists():
            return None, None
        try:
            raw, ended = _split_lines(_read_bytes_retry(self.anchor))
        except (OSError, ValueError) as exc:
            raise LadderError(f"梯子锚点无法读取(fail-closed): {_why(exc)}") from exc
        last_seq, last = 0, None
        torn: str | None = None
        items = [item for item in raw if item.strip()]
        for index, line in enumerate(items):
            try:
                seq, digest = _parse_anchor_line(line)
            except ValueError as exc:
                # 只有**末行**、且文件不以换行结尾时才是撕裂(崩在追加中途),那半行不算(第 17 轮 M3);
                # "完整但缺换行"解析得出来,会被保留(第 19 轮 L4)。
                if index == len(items) - 1 and not ended:
                    torn = line
                    break
                raise LadderError(f"梯子锚点格式不合法(fail-closed): {_why(exc)}") from exc
            if seq <= last_seq:                              # 第 17 轮:锚点自身必须严格递增、从 1 起(seq=0 会把下标读成 rows[-1])
                raise LadderError("梯子锚点的序号不是严格递增的正整数(fail-closed)")
            last_seq, last = seq, digest
        return (None if last is None else (last_seq, last)), torn

    def _check_anchor(self, rows: list[dict], repair: bool, anchored=None, torn=None) -> None:
        """核对锚点见证的记录。

        `anchored`/`torn` 由调用方传入(读路径已经先读过一次锚点,见 `_load` 的顺序说明);
        不传时这里自己读一次(保持函数自足)。"""
        if self.anchor is None:
            return
        if anchored is None and torn is None:
            anchored, torn = self._read_anchor()
        if anchored is None:
            if not rows:
                return
            if len(rows) == 1 and torn is not None:
                # 锚点**只有**一行且那行是撕裂的:它本来要见证的就是第 1 条。
                # 用碎片里的哈希前缀对日志第 1 条——对不上就是日志被改过(fail-closed)。
                self._check_torn_witness(rows, 1, torn)
                if repair:
                    self._write_anchor(rows[0])
                return
            if len(rows) == 1:                          # 第一条日志已追加、锚点还没写完就崩了
                if repair:
                    self._write_anchor(rows[0])
                return
            raise LadderError("梯子有日志却没有锚点(fail-closed):锚点被删过,或日志是被换进来的")
        seq, digest = anchored
        # 见证范围**只用锚点自己记下的那一条**。第 19 轮曾在这里用"日志里那条的哈希"把范围补大一号,
        # 那是拿日志证明日志:补大之后比对的是 rows[seq] 与 rows[seq] 自己,**锚点真正的见证从未被核对**,
        # 反而给"截断锚点+改写日志"留了一条绕过路径。锚点的全部价值就在这一条比对上,不能丢。
        if len(rows) < seq or rows[seq - 1]["hash"] != digest:
            raise LadderError("梯子日志与锚点对不上(fail-closed):日志被回滚、截断或整条换掉;"
                              "确认要从头开始请由人执行 `scripts/research_ladder.py reset --reason ...`")
        if torn is not None:
            # 锚点末行撕裂:它本来要见证的是第 seq+1 条。用碎片里的哈希**前缀**与日志那一条比对——
            # 前缀对不上,说明日志那一条被改过(fail-closed);对得上,见证范围才真的补到 seq+1。
            self._check_torn_witness(rows, seq + 1, torn)

    def _check_torn_witness(self, rows: list[dict], seq: int, torn: str) -> None:
        """核对锚点撕裂末行**本来要见证**的那一条日志(第 19 轮 L4 的更正)。

        锚点行是 `<seq> <hash>`,而且**日志那一条先 fsync、锚点行后写**——所以碎片里的 seq
        一定是 `seq`,而且日志里必须有那一条。碎片是那次写入的**前缀**,因此它带的哈希前缀
        必须与日志那一条的哈希前缀一致。

        - 碎片里的 seq 不是 `seq`(锚点被改写)→ fail-closed;
        - 日志里没有第 seq 条(日志被截断/回滚)→ fail-closed;
        - 碎片带哈希前缀而与日志对不上(那一条被改过)→ fail-closed;
        - 碎片只写到 seq 就崩了(还没写哈希)→ 无从核对,见证范围停在最后一条**完整**锚点行。"""
        head, _, prefix = torn.partition(" ")
        if not head.isdigit() or int(head) != seq:
            raise LadderError("梯子锚点末行与日志对不上(fail-closed):锚点被改写或日志被换掉")
        if len(rows) < seq:
            raise LadderError("梯子锚点指向的记录在日志里不存在(fail-closed):日志被截断或回滚")
        if prefix and not rows[seq - 1]["hash"].startswith(prefix):
            raise LadderError("梯子锚点末行与日志对不上(fail-closed):日志第 "
                              f"{seq} 条的哈希与锚点碎片不一致(该条被改过)")

    @staticmethod
    def _trim_torn_tail(path: Path, parse=None) -> None:
        """文件不以换行结尾 = 上次追加没写完。**先试解析**(第 19 轮 M1/L4):

        - 解析得出来 → 这一行是**完整的,只是缺换行**,补一个换行就留下它
          (第 17 轮的做法是直接截掉,于是"完整但缺换行"被当成撕裂,整条梯子永久报废——第 19 轮 codex 实测的回归);
        - 解析不出来 → 真的撕裂(崩在追加中途),截掉(第 17 轮 M3)。"""
        if not path.exists():
            return
        data = path.read_bytes()
        if data and not data.endswith(bytes([10])):
            tail = data[data.rfind(bytes([10])) + 1:]
            complete = False
            if parse is not None and tail.strip():
                try:
                    parse(tail.decode("utf-8"))
                    complete = True
                except (ValueError, UnicodeDecodeError):
                    complete = False
            if complete:
                with open(path, "ab") as stream:          # 补上换行:这一行是完整的,不能丢
                    stream.write(bytes([10]))
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                with open(path, "r+b") as stream:
                    stream.truncate(data.rfind(bytes([10])) + 1)

    def _write_anchor(self, record: dict) -> None:
        if self.anchor is None:
            return
        try:
            self.anchor.parent.mkdir(parents=True, exist_ok=True)
            self._trim_torn_tail(self.anchor, parse=_parse_anchor_line)
            with open(self.anchor, "a", encoding="utf-8", newline="") as stream:
                stream.write(f"{record['seq']} {record['hash']}" + chr(10))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise LadderError(f"梯子锚点写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc

    def _load(self, repair: bool = True) -> list[dict]:
        """读日志并与头指针、锚点对账。两者都不存在 = 全新的梯子;任何对不上 = 损坏/篡改/回滚,抛 LadderError。

        `repair=False` 用于只读入口(status/has/preview):它们**不写任何文件**(第 16 轮 L2:"只读"的命令不该改状态)。"""
        # **先读"见证",再读日志**(第 22 轮 codex 的 High 2):写者的顺序是 日志 → 头指针 → 锚点,
        # 读者的顺序必须反过来。否则一次**正常**的并发写会被读成篡改:读者先读到旧日志,
        # 写者这时追加并写了锚点,读者再读锚点就看到"锚点比日志新",`_check_anchor` 判成
        # "日志被回滚、截断或整条换掉"——而那条错误消息会把运维指向 `research_ladder.py reset`,
        # 也就是把 best 归零、把全部历史揭示存档归档作废(可用性故障被误判成安全事件,处方还是破坏性的)。
        # 实测:两个真进程,读者循环 11 秒 767 次读里有 21 次报错(锚点超前/头指针超前/Windows 上的 PermissionError)。
        # 顺序反过来之后,见证永远**不晚于**它见证的那条日志,良性并发不再误报;
        # 而"日志被截断或被换掉"仍然被抓住(见证比日志新 → 对不上,照旧 fail-closed)。
        head = self._head()
        anchored, torn = self._read_anchor()
        rows = self._read_log()
        self._check_anchor(rows, repair, anchored=anchored, torn=torn)
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
        # 头指针是**缓存**,日志才是事实来源。读者先读见证、再读日志,于是头指针可能**落后**日志:
        # 那是写者正在写的正常中间态(写者顺序:日志 → 头指针 → 锚点),不是损坏,更不是篡改。
        # 早先这里只容忍"落后一行",落后两行就 fail-closed——可写者在这两次读之间完全可能
        # 写完好几轮,于是**正常**的并发写被报成"头指针被回滚、截断或单独改写过"。
        # 只有"头指针指向的东西与日志对不上"才是篡改:
        #  - 头指针比日志**新** → 日志被截断/回滚(fail-closed,这条不能放宽);
        #  - 头指针指向的那条记录的哈希或数值与日志对不上 → 头指针被单独改写过(fail-closed);
        #  - 头指针落在创世(seq=0)而日志里已有记录 → 头指针被回滚到创世(fail-closed)。
        if head["seq"] > last["seq"]:
            raise LadderError("梯子头指针与日志对不上(fail-closed):被回滚、截断或单独改写过")
        if head["seq"] == 0:
            raise LadderError("梯子头指针与日志对不上(fail-closed):头指针被回滚到了创世状态")
        witnessed = rows[head["seq"] - 1]
        if head["hash"] != witnessed["hash"]:
            raise LadderError("梯子头指针与日志对不上(fail-closed):被回滚、截断或单独改写过")
        if not (head["queries"] == witnessed["queries"] and head["best"] == witnessed["best"]
                and head["budget"] == witnessed["budget"]):
            raise LadderError("梯子头指针里的 queries/best/budget 与它见证的那条日志对不上(fail-closed):头指针被单独改写过")
        if head["seq"] != last["seq"] and repair:
            self._write_head(last)                      # 头指针落后:按日志修复(写者还没来得及更新它)
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
            _replace_atomic(tmp, self.path)
        except OSError as exc:
            raise LadderError(f"梯子头指针写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc

    def _append(self, rows: list[dict], event: str, *, run_id: str | None, queries: int, best: float, note: str | None) -> dict:
        # 写之前再核对一次磁盘上的现状(第 19 轮 M2):调用方给的 `rows` 可能已经过时——
        # 期间若发生过 `reset`(日志被挪走)或别的进程追加过,拿陈旧的 seq 去写就会写出一条断链的记录,
        # 之后每次读都报"seq 不连续",梯子永久报废。这里 fail-closed:拒绝这次写入,让人重试。
        on_disk = self._read_log()
        expect = rows[-1]["hash"] if rows else GENESIS
        actual = on_disk[-1]["hash"] if on_disk else GENESIS
        if expect != actual:
            raise LadderError("梯子状态在本次操作期间被改动(fail-closed):期间发生过 reset 或并发写入,请重试")
        prev = rows[-1] if rows else {"seq": 0, "hash": GENESIS, "budget": self.budget}
        record = {"seq": prev["seq"] + 1, "event": event, "run_id": run_id, "queries": queries, "best": best,
                  "budget": min(prev["budget"], self.budget), "note": note, "prev": prev["hash"], "hash": ""}
        record["hash"] = _digest(prev["hash"], _body(record))
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._trim_torn_tail(self.log_path, parse=_parse_log_line)
        except OSError as exc:
            raise LadderError(f"梯子日志写入失败(fail-closed,不放出汇报值): {_why(exc)}") from exc
        size_before = self.log_path.stat().st_size if self.log_path.exists() else 0
        try:
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

    def orphaned_run_ids(self) -> list[str]:
        """被 `reset` 孤立掉的 run_id(第 19 轮 codex 的 H1/L2)。

        `reset` 把梯子清空之后,这些 run 的揭示存档还在,但梯子日志里已经没有它们——
        回放按"被伪造"fail-closed,于是这些 run **永远读不出来**,`/feedback` 也就静默变空。
        外层必须能分辨"这个 run 从未揭示过"与"这个 run 被 reset 孤立了",所以这里把名单读出来。

        **这份名单只用于"分类",不用于"放行"**(第 20 轮 codex 的 H2):
        被孤立的 run 一样读不出任何数据——区别只在于跳过时说的是"被人工重置孤立"
        还是"被篡改"。所以即使这份名单被伪造,后果也只是**跳过原因**被说错,
        不会泄漏任何验证窗口信息;真正的篡改信号(数据对不上)一条都不放松。"""
        log_path = self.path.parent / "ladder.reset.log"
        if not log_path.exists():
            return []
        try:
            stat = log_path.stat()
            token = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            token = None
        cached = getattr(self, "_orphans_cache", None)
        if cached is not None and cached[0] == token:
            return list(cached[1])               # 名单只增不减:文件没变就直接用缓存
        try:
            data = log_path.read_bytes()
        except OSError as exc:
            # 读不到名单是**可用性**故障,不是"没有孤立项"(第 21 轮 codex 的 M2/L1):
            # 此前这里返回 [],于是"名单读不出来"与"确实没有孤立项"长得一样——
            # 外层会把一次合法的人工重置记成篡改(第 20 轮刚修好的分类又被绕回去)。
            raise LadderError(f"重置日志暂时读不到: {type(exc).__name__}") from exc
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            # 名单文件损坏:同样分不清"有孤立项"与"没有",按 fail-closed 报不可用,
            # 绝不把损坏当成"不是孤立"去原谅任何一条记录。
            raise LadderError("重置日志损坏(fail-closed,不当作孤立)") from exc
        names: list[str] = []
        seen: set[str] = set()
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("event") == "reset-intent":
                for run_id in entry.get("orphaned_run_ids") or []:
                    if isinstance(run_id, str) and run_id not in seen:
                        seen.add(run_id)
                        names.append(run_id)
        self._orphans_cache = (token, list(names), set(names))
        return list(names)

    def orphaned_run_id_set(self) -> set[str]:
        """孤立名单的**集合**形式(带缓存,只读):按 run 查"是不是被孤立"是热路径上的高频操作,
        每次重建一个 set 是 O(结果数 × 名单长)(第 21 轮 codex 的 L3)。

        返回的是缓存里的那一个 set,**调用方不得改动它**。"""
        self.orphaned_run_ids()                     # 读一次并刷新缓存(读不到会抛 LadderError)
        cached = getattr(self, "_orphans_cache", None)
        if not cached or len(cached) < 3:
            return set()
        return cached[2]

    def _run_ids_best_effort(self) -> list[str]:
        """尽力从**原始日志**里捞出 run_id(第 21 轮 codex 的 High-2)。

        对账失败不等于 run_id 读不出来:日志行本身是 JSON,即便哈希链或锚点对不上,
        逐行 `json.loads` 仍能取到 `run_id`。读不懂的行直接跳过(不猜、不补)。
        这条只在 `reset` 的"梯子已经坏了"分支上用到——那里 `_load()` 必然抛错。
        """
        try:
            raw, _ended = _split_lines(self.log_path.read_bytes())
        except OSError:
            return []
        names: list[str] = []
        seen: set[str] = set()
        for line in raw:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            run_id = row.get("run_id")
            if isinstance(run_id, str) and run_id not in seen:
                seen.add(run_id)
                names.append(run_id)
        return names

    def snapshot(self) -> "LadderSnapshot":
        """一次读入的只读快照(第 19 轮 codex 的 L3)。

        `/feedback` 要扫很多 run,每扫一个都 `has()`/`preview()` 一次——每次都是**整条日志重读 + 逐条
        SHA-256 校验**(实测 400 个结果 + 60 条日志:冷 2.4 s / 热 0.52 s,而 `ladder.has()` 全量占 305 ms/300 个)。
        这是进化回路每次迭代都要打的热路径。快照把状态读一次,之后 `has`/`preview` 走内存。

        快照是**只读**的:要消耗查询的 `submit` 仍然走真梯子(它必须写)。

        **令牌先读、状态后读**(第 20 轮 codex 的 M1):这样"快照里的行"永远不早于令牌,
        令牌与当前头指针一比就能判断快照是不是过期了(过期就重读,见 `LadderSnapshot._fresh_rows`)。"""
        with _lock:
            token = self._head_token()
            rows = self._load(repair=False)
            return LadderSnapshot(self, rows, token)

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
        with _lock, _file_lock(self.lock_path):
            rows = self._load()
            plan = self._plan(rows, sharpe, run_id)
            if plan["replay"]:
                return plan
            self._append(rows, "submit", run_id=run_id, queries=plan["queries_used"], best=plan["reported"], note=None)
            return plan

    def reset(self, reason: str, *, force: bool = False, outward: Path | None = None) -> dict:
        """**人工**从头开始(第 17 轮 H2):把日志、头指针、锚点各自改名归档(`.bak-<序号>`),梯子回到全新状态。

        什么时候要用:清理/恢复结果目录时只带走了一半文件(锚点在 `data/ladder_anchor/`,日志在 `data/research/ladder/`),
        梯子因"日志与锚点对不上"而 fail-closed。

        **代价必须说清**(第 19 轮 codex 的 L2):`best` 归零、查询数归零——已经泄漏的信息收不回来;
        **全部历史揭示存档也一并作废**(梯子日志里没有这些 run 了,回放会按"被伪造"fail-closed,
        `/feedback` 因此会返回空 traces——这也是为什么 `outward` 目录会被一起归档,不留死档);
        所以它是比 `renew` 更重的操作,只留给人。

        **护栏**(第 19 轮 L1):梯子**状态正常**时默认拒绝,必须显式 `--force`——
        `renew` 有"预算用尽才允许"这道护栏,比它更重的 `reset` 却没有,等于"总预算"随时可清零。

        **审计顺序**(第 19 轮 M3):**先写意图行**(含被丢弃的 `best`/`queries`、原因、将被孤立的 run_id),
        再挪文件,最后写完成行——中途失败也留下"有人动过手"的痕迹(第 15 轮给 `renew` 修掉的正是这个)。
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_NOTE:
            raise LadderError(f"reset 必须给出 1..{MAX_NOTE} 字符的原因")
        files = [f for f in (self.path, self.log_path, self.anchor) if f is not None]
        if outward is not None:
            files.append(Path(outward))
        with _lock, _file_lock(self.lock_path):
            # 健康检查:梯子**能正常对账且有状态**时,重置会真的毁掉东西,默认拒绝(第 19 轮 L1)。
            # 已经损坏(日志/头指针/锚点对不上)时反而允许——那正是 reset 的用武之地。
            healthy = True
            dropped = {"best": None, "queries": None, "run_ids": []}
            try:
                rows = self._load(repair=False)        # 完整对账:损坏在这里抛 LadderError
                if rows:
                    healthy = True
                    dropped["best"] = rows[-1]["best"]
                    dropped["queries"] = rows[-1]["queries"]
                    dropped["run_ids"] = [r["run_id"] for r in rows if r.get("run_id")]
                else:
                    healthy = False                     # 空的梯子:重置不损失任何东西,放行
            except LadderError:
                healthy = False                          # 已经坏了:reset 正是这时候的出路
                # 但**名单照样要捞出来**(第 21 轮 codex 的 High-2):
                # "对账失败"不等于"run_id 读不出来"。此前这条分支里名单恒为空——
                # 而"梯子已经坏了"恰恰是 `reset` 的用武之地(docstring 就这么写的),
                # 于是 H-2 那条"合法人工重置不算篡改"只在"梯子健康 + --force"
                # 这条被劝阻的路径上生效,在正经路径上原样未修。
                dropped["run_ids"] = self._run_ids_best_effort()
            if healthy and not force:
                # 拒绝也要留痕(第 19 轮 codex 的 MEDIUM):有人试图重置一件状态正常的梯子,
                # 这本身就是"动过手"的证据——不记下来,事后只看归档文件是看不出有人试过的。
                log_path = self.path.parent / "ladder.reset.log"
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._append_reset_log(log_path, {"event": "reset-refused", "archive": None,
                                                      "reason": reason.strip(),
                                                      "note": "梯子状态正常,未显式 force,已拒绝"})
                except OSError:
                    pass                                # 连审计都写不下去时,拒绝本身仍然生效
                raise LadderError("梯子状态正常,不得重置;确需从头开始请显式 force"
                                  "(重置会清零 best 与查询数,并作废全部历史揭示存档)")
            moved = []
            # 归档编号:在锁内算(第 19 轮的跨进程锁保证这一步与挪文件之间没有别的进程插手)。
            # 从 1 起,取第一个"所有目标文件都没被占用的"编号。
            n = 1
            while True:
                if not any(Path(str(f) + f".bak-{n}").exists() for f in files):
                    break
                n += 1
            log_path = self.path.parent / "ladder.reset.log"
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 意图行先落盘(第 19 轮 M3):哪怕后面某一步抛错,审计里也有"动过手"的记录
            # **不截断**(第 20 轮 codex 的 M4):此前只记前 200 个被孤立的 run_id,
            # 返回值却报总数(300 报 200)——"被孤立"与"没被孤立"因此可能同时成立,
            # 判定"这次重置孤立了哪些 run"就不可信了。名单与梯子日志里的 run 同量级,
            # 一次写全不引入新的量级风险,另记 `orphaned_count` 便于事后核对。
            intent = {"event": "reset-intent", "archive": n, "reason": reason.strip(),
                      "discarded_best": dropped["best"], "discarded_queries": dropped["queries"],
                      "orphaned_count": len(dropped["run_ids"]),
                      "orphaned_run_ids": list(dropped["run_ids"])}
            # **意图行写不进去就不许动东西**(第 19 轮 codex 的 MEDIUM):审计先落盘是这次排序的全部意义,
            # 意图行静默丢失之后照样挪文件,等于"审计先落盘"名存实亡——事后只剩归档文件,看不出谁动过手。
            self._append_reset_log(log_path, intent, required=True)
            try:
                for f in files:
                    if f.exists():
                        target = Path(str(f) + f".bak-{n}")
                        _replace_atomic(f, target)   # Windows 瞬时占用重试(见 _replace_atomic 说明)
                        moved.append(target.name)
                        self._append_reset_log(log_path, {"event": "reset-archived", "archive": n,
                                                          "name": target.name, "reason": reason.strip()})
            except OSError as exc:
                self._append_reset_log(log_path, {"event": "reset-failed", "archive": n,
                                                  "error": _why(exc), "reason": reason.strip()})
                raise LadderError(f"reset 失败: {_why(exc)}") from exc
            self._append_reset_log(log_path, {"event": "reset-done", "archive": n,
                                              "files": moved, "reason": reason.strip()})
        # 返回值与**审计意图行逐键一致**(第 20 轮自查更正):此前这里写的是
        # `"orphaned_run_ids": len(...)`——同一个键名在返回值里是**个数**、在审计行里是**名单**,
        # 两边对不上,而且"返回值与落盘内容一致"这句话当时并不成立。改为键名与类型都一致:
        # `orphaned_count` 是个数,`orphaned_run_ids` 是名单。
        return {"archived": moved, "archive_number": n, "discarded_best": dropped["best"],
                "discarded_queries": dropped["queries"], "orphaned_count": len(dropped["run_ids"]),
                "orphaned_run_ids": list(dropped["run_ids"])}

    @staticmethod
    def _append_reset_log(path: Path, entry: dict, *, required: bool = False) -> None:
        """审计日志的每一次追加都 fsync——它就是"动过手"的唯一证据,不能自己先丢。

        `required=True` 用于**动手之前**的那一行(意图行):写不进去就抛错停手,
        因为"审计先落盘"是这套排序的全部意义,意图行丢了还照样挪文件等于名存实亡。
        动手之后的行(archived/done)写不进去不该把已经完成的操作变成失败,继续吞掉。"""
        try:
            with open(path, "a", encoding="utf-8", newline="") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + chr(10))
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            if required:
                raise LadderError(f"梯子重置审计无法写入(fail-closed,未做任何修改): {_why(exc)}") from exc
            pass                                    # 审计写不进去不该把已经完成的操作变成失败

    def renew(self, note: str, *, force: bool = False) -> dict:
        """人工续期:`queries` 归零、**保留 `best`**,必须给出非空原因,留一条审计记录。

        默认**只在预算用尽之后**才允许(第 16 轮 M3:否则"总预算"其实是"每次续期的量",随时可以重置);
        `force=True` 强制续期,审计记录里带"[强制]"前缀。"""
        if not isinstance(note, str) or not note.strip() or len(note) > MAX_NOTE:
            raise LadderError(f"续期必须给出 1..{MAX_NOTE} 字符的原因")
        with _lock, _file_lock(self.lock_path):
            rows = self._load()
            state = self._state(rows, self.budget)
            if not force and state["queries"] < state["budget"]:
                raise LadderError(f"预算还没用尽({state['queries']}/{state['budget']}),不得续期;确需提前续期请显式 force(审计里会标注)")
            early = state["queries"] < state["budget"]                 # 第 17 轮 L1:只有真的提前续期才标"强制"
            text = "[强制] " + note.strip() if (force and early) else note.strip()
            if len(text) > MAX_NOTE:
                raise LadderError(f"续期原因加前缀后超过 {MAX_NOTE} 字符")
            record = self._append(rows, "renew", run_id=None, queries=0, best=state["best"], note=text)
            return {"reported": float(record["best"]), "step": self.eta, "queries_used": 0,
                    "queries_left": record["budget"], "budget": record["budget"]}


class LadderSnapshot:
    """梯子的只读快照:一次读入,之后 `has`/`preview` 走内存(第 19 轮 codex 的 L3)。

    只暴露只读查询。消耗查询的写操作**不在快照里做**——`submit` 只是转交真梯子
    (写日志、头指针与锚点都由真梯子负责)。

    **快照会过期**(第 20 轮 codex 的 M1):读完之后梯子可能被 `reset`(日志与头指针被挪走)、
    也可能被别的进程提交过。过期快照里的行留着**重置前**的 best 与查询数——
    拿它回放就会把重置前的成绩当成"现在的话"报出去(泄漏),`queries_left` 也是旧账。
    所以每次用之前先对一下**变更令牌**(头指针的 `seq`/`hash`),对不上就重读。"""

    def __init__(self, ladder: Ladder, rows: list[dict], token: tuple[int, str] | None = None) -> None:
        self._ladder = ladder
        self._rows = rows
        self._token = token
        self._run_ids = {r["run_id"] for r in rows if r.get("run_id") is not None}

    def _fresh_rows(self) -> list[dict]:
        """快照过期就重读(第 20 轮 codex 的 M1)。

        令牌取自**头指针**:梯子被 `reset` 之后头指针被挪走,令牌变成 None,
        于是"重置过"这件事一定会让快照作废——否则就会拿重置前的 best 与查询数继续回放。"""
        token = self._ladder._head_token()
        if token != self._token:
            self._rows = self._ladder._load(repair=False)
            self._run_ids = {r["run_id"] for r in self._rows if r.get("run_id") is not None}
            self._token = token
        return self._rows

    def has(self, run_id: str) -> bool:
        with _lock:
            self._fresh_rows()
            return run_id in self._run_ids

    def preview(self, sharpe, run_id: str | None = None) -> dict:
        with _lock:
            return self._ladder._plan(self._fresh_rows(), sharpe, run_id)

    def orphaned_run_ids(self) -> list[str]:
        """被人工 `reset` 孤立的 run 名单:读**真梯子**的(名单只增不减,快照里的行会过期)。"""
        return self._ladder.orphaned_run_ids()

    def orphaned_run_id_set(self) -> set[str]:
        """孤立名单的集合形式(只读):转交真梯子的缓存,不在快照里另存一份。"""
        return self._ladder.orphaned_run_id_set()

    def submit(self, sharpe, run_id: str | None = None) -> dict:
        """消耗查询的写操作**不走快照**,回真梯子(它要写日志、头指针与锚点)。

        快照只用来做只读判断;真要扣查询时,真梯子会重新读一遍当前状态——同一个 `run_id`
        的重复提交是幂等的(只回放当时的汇报值,不重复扣查询),所以这里不会多扣。

        **已经提交过的 run 直接用快照回答**(第 19 轮 codex 的 L3 收尾):这种提交是**纯回放**——
        不写日志、不扣查询、汇报值就是当初那一条。走真梯子只会把整条日志重读并重校验一遍,
        而 `/feedback` 每次迭代都要扫几十上百个 run,那是进化回路的热路径(实测 400 个结果
        要重读 400 次)。

        **回放值以"快照读到的那些行"为准,并且快照过期就重读**(第 20 轮 codex 的 M1):
        此前这句话写的是"回放值与真梯子一致"——那只在**没人动过梯子**时成立:
        `reset` 之后快照里还留着重置前的 best 与查询数,拿它回放就是把已经作废的成绩
        当成现在的话报出去(泄漏)。现在每次回放前都对一次变更令牌,过期就重读。"""
        with _lock:
            rows = self._fresh_rows()
            if run_id is not None and run_id in self._run_ids:
                return self._ladder._plan(rows, sharpe, run_id)             # 纯回放:不写、不扣
        return self._ladder.submit(sharpe, run_id=run_id)
