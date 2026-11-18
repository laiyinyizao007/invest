"""试验次数账本:记录进化/选参一共"试"过多少次,供 DSR 去通胀使用。

规则见 docs/adr/20261113-evolution-engine.md:
- 每个 param_grid 格子、每次候选提交各计 1 次试验;被淘汰的候选同样计数(幸存者偏差正是 DSR 要惩罚的对象)。
- 只增不减:本模块不提供删除/回退接口(人工迁移/重置在 `risk/ledger_admin.py`,只给 scripts/research_ledger.py 用)。
- 账本损坏或无法读取一律抛 TrialLedgerError(fail-closed):少记等于放水,宁可拒绝评估。
- 追加前先校验整个账本,不在已损坏的账本上继续累计。

**防篡改(Issue #31)**:每条记录带 `seq`/`prev`/`hash`(哈希链,口径见 `risk/ledger_chain.py`),头指针写在**另一个目录**的
锚点文件里;读侧发现链断/回滚/截断/整条被换掉一律抛 TrialLedgerError(fail-closed)。起因:白名单里的 numpy/pandas
能把任意字节写到任意路径,清空或抹小本账本就让 DSR 的 N 偏小(年化 Sharpe 3.5:N=1620 时 DSR 0.6397 不过,N=1 时 0.9999 过)。
边界见 `risk/ledger_chain.py`:链没有密钥,同时改账本与锚点的人骗得过它。**候选写账本的防线分两层,都有前提,别读成"已闭合"**:
① 执行窗口里的审计钩子(scripts/research_sandbox.py)——只罩"候选正在跑"这一段,**窗口外候选若还能执行代码,写文件不受限**
(codex 实测:`np.seterrcall(cb)` + `np.seterr(all="call")` 的回调在窗口外触发时写入成功)。今天端到端打不通,
依赖的是**宪法的 `__del__` 禁令**(risk/constitution.py `BUILTIN_FORBIDDEN_DUNDERS`)与 atexit/threading/weakref/gc 在候选侧取不到
——这个依赖是显式残余,不是本层的能力;② 本层的读侧核链:无论怎么写进来的,链断/回滚/截断/无链记录都会被发现(同时改锚点除外)。旧账本(无链)不会被静默采信:
用 `python scripts/research_ledger.py migrate` 一次性迁移。

阶段 1 只做全局累计(保守:家族划分随阶段 3 的谱系再引入)。同一进程内用线程锁、跨进程用锁文件串行写
(有了哈希链,两个写者读到同一个尾巴会把链写成分叉,所以跨进程互斥不再可省)。

**锚点里还记着"那一时刻的累计次数"**(第 43 轮):只有哈希时,"删掉账本中间几行"既不改长度也不改哈希链,
读侧看不出来、人工重置还会**静默**把 N 变小(DSR 的分母变小 = 去通胀失效)。带上累计次数后,
删行**精确**可算(丢了多少次),读侧 fail-closed、人工重置必须显式补记。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from risk import ledger_chain as chain

KINDS = ("submission", "grid_cell", "gate_run")
# **人工重置留下的记录**(第 40 轮,Medium 1):`count` 恒为 0(重置本身不是一次试验),
# 但它把"重置前后的累计试验次数"写进**链里**,于是"重置把 N 变小"这件事本身可审计。
# 只由 `risk/ledger_admin.reset_trials` 写;`record_trials` 不许写这一种(见下)。
RESET_KIND = "ledger_reset"
READ_KINDS = KINDS + (RESET_KIND,)
# 上限:count 过大时 DSR 的尾概率 1-1/N 会舍入成 1.0 并抛 StatisticsError,账本在语法上有效却让评估永久失败
# 拿不到账本锁时的等待上限(秒)。写入路径的策略:宁可多等一会儿,
# 别把一次并发写入变成一次失败(与人工维护入口的 5 秒**故意不同**——
# 那是调用方策略,不是安全口径;安全口径是 stale_seconds,见 risk/ledger_chain.py)。
LOCK_TIMEOUT = chain.DEFAULT_LOCK_TIMEOUT
MAX_ENTRY_COUNT = 10**9
MAX_TOTAL = 10**12
TOOL_HINT = ('旧账本(无链):`python scripts/research_ledger.py migrate trials`;链断/回滚/截断:'
             '先人工核对,再 `python scripts/research_ledger.py reset trials --reason "<原因>" --add-trials <估计丢失的试验次数> --force`'
             '(保留有效前缀、重新定锚,原文件备份)')
_lock = threading.Lock()


class TrialLedgerError(Exception):
    """试验账本损坏、被篡改、不可读或写入参数非法。"""


def check_row(data: object) -> dict:
    """单条记录的语法与取值范围(不核链)。"""
    if not isinstance(data, dict):
        raise ValueError("记录不是对象")
    count = data.get("count")
    kind = data.get("kind")
    if kind not in READ_KINDS:
        raise ValueError("未知的 kind")
    if kind == RESET_KIND:
        # 重置记录不是一次试验:count 恒为 0(它只记 before/after)。
        if isinstance(count, bool) or not isinstance(count, int) or count != 0:
            raise ValueError("重置记录的 count 必须是 0")
        return data
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
        raise ValueError(f"count 必须是 1..{MAX_ENTRY_COUNT} 的整数")
    return data


def parse(path: Path) -> list[dict]:
    """只做语法与取值范围校验(不核链)。账本不存在 = 空。"""
    if not path.exists():
        return []
    entries: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entries.append(check_row(json.loads(line)))
        if sum(e["count"] for e in entries) > MAX_TOTAL:
            raise ValueError(f"累计试验次数超过上限 {MAX_TOTAL}")
    except (OSError, ValueError) as exc:          # JSONDecodeError 是 ValueError 的子类
        raise TrialLedgerError(f"试验账本损坏或无法读取(fail-closed): {exc}") from exc
    return entries


def _entries(path: Path) -> list[dict]:
    """读并核链。试验账本允许"账本比锚点多几条"(崩在写锚点之前;多出的条目只会让 N 变大,偏保守)。"""
    try:
        return chain.read_verified(path, parse, what="试验账本", allow_ahead=True, tool_hint=TOOL_HINT)
    except chain.LedgerChainError as exc:
        raise TrialLedgerError(str(exc)) from exc


def total_trials(path: Path) -> int:
    """累计试验次数;账本不存在视为 0,损坏/被篡改则抛 TrialLedgerError。"""
    return sum(e["count"] for e in _entries(Path(path)))


def record_trials(path: Path, kind: str, count: int, *, at: float) -> int:
    """追加一条试验记录并返回追加后的累计次数。"""
    if kind not in KINDS:
        # `ledger_reset` 只能由人工重置入口写:普通写入路径不许伪造"重置记录"。
        raise TrialLedgerError(f"未知的试验类型: {kind!r}")
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_ENTRY_COUNT:
        raise TrialLedgerError(f"试验次数必须是 1..{MAX_ENTRY_COUNT} 的整数")
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # 等待上限**显式**写出来(第 45 轮):此前靠 `file_lock` 的默认值,
            # 而默认值是"只有不传参的人才用"的暗口径——另两个调用方都显式传 5.0,
            # 这里不传就成了 30 秒。写入路径的策略与人工维护入口**故意不同**
            # (宁可多等,别把一次并发写入变成一次失败),但**必须写在明面上**。
            with chain.file_lock(path, timeout=LOCK_TIMEOUT,
                                 stale_seconds=chain.DEFAULT_STALE_SECONDS):
                rows = _entries(path)                        # 先校验链与锚点,损坏/被篡改则不追加
                existing = sum(e["count"] for e in rows)
                if existing + count > MAX_TOTAL:
                    raise TrialLedgerError(f"累计试验次数将超过上限 {MAX_TOTAL}")
                prev = rows[-1]["hash"] if rows else chain.GENESIS
                record = chain.seal({"kind": kind, "count": count, "at": at}, prev, len(rows) + 1)
                try:
                    with open(path, "a", encoding="utf-8", newline="") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as exc:
                    raise TrialLedgerError(f"试验账本写入失败(fail-closed): {exc}") from exc
                chain.write_anchor(chain.anchor_path_for(path), record["seq"], record["hash"],
                                   total=existing + count)
        except chain.LedgerChainError as exc:
            raise TrialLedgerError(str(exc)) from exc
    return existing + count
