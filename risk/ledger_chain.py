"""账本防篡改的共用件:哈希链 + 外置锚点(试验账本与批准账本共用,Issue #31)。

口径与 `risk/ladder.py`(feat/21-stage3 分支)一致,不另起第三套:
- 每条记录含 `seq`(从 1 起)、`prev`(上一条的 hash,创世为 64 个 0)、`hash`
  (= sha256(prev + "|" + 规范化 JSON(记录去掉 hash)),键排序、紧凑分隔、ensure_ascii=False);
- 锚点文件(`<seq> <hash>` 一行)放在**与账本不同的目录**(`data/ledger_anchor/`),写在账本之后;
- 读侧逐条核对链,再用锚点核对"头":链断/回滚/截断/整条被换掉 -> `LedgerChainError`(fail-closed)。

诚实的边界(与 ladder 同,别写成"防篡改"之外的东西):
- 哈希链**没有密钥**:能写账本与锚点两处的人,可以把两者一起改成一条自洽的链。
  本层只拦"只改了其中一处"(候选经 numpy/pandas 往账本写字节、却够不到锚点,或没想到要改锚点)。
  同时删掉账本与锚点 = 回到"从未有过账本",也发现不了。
- 真正的外部锚点需要另一个信任域(Issue #29 独立进程 / 容器只读挂载);这里没有。
- 锚点**只会落后于账本**(崩在写锚点之前),所以"账本比锚点多几条"在 `allow_ahead=True`
  时按良性处理(试验账本:多出来的条目只会让 N 变大,偏保守);批准账本用 `allow_ahead=False`
  (严格相等),因为"账本里多出一条自洽的批准"正是要拦的攻击。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

GENESIS = "0" * 64
ANCHOR_DIRNAME = "ledger_anchor"
_HEX64 = re.compile(r"[0-9a-f]{64}")


class LedgerChainError(Exception):
    """账本的哈希链/锚点核对失败(fail-closed)。消息里带人工出路。"""


def digest(prev: str, body: dict) -> str:
    payload = prev + "|" + json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seal(record: dict, prev_hash: str, seq: int) -> dict:
    """给一条记录封链:返回带 seq/prev/hash 的新字典。"""
    body = {k: v for k, v in record.items() if k not in ("seq", "prev", "hash")}
    body["seq"] = seq
    body["prev"] = prev_hash
    return {**body, "hash": digest(prev_hash, body)}


def anchor_path_for(ledger_path) -> Path:
    """锚点位置:账本所在目录的**上一级**下的 `ledger_anchor/`,文件名带账本绝对路径的短哈希。

    与账本不同目录(候选可写/读写的 data/research、data/research-control 都碰不到);
    文件名带路径哈希,是为了测试里多个 tmp 目录共享同一个上级时互不串账。"""
    ledger = Path(ledger_path)
    resolved = str(ledger.resolve()).replace("\\", "/").lower()
    tag = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    return ledger.resolve().parent.parent / ANCHOR_DIRNAME / f"{ledger.stem}-{tag}.anchor"


def read_anchor(anchor: Path) -> tuple[int, str] | None:
    """读锚点;不存在 -> None;存在但格式不对 -> LedgerChainError(被改写过)。"""
    anchor = Path(anchor)
    if not anchor.exists():
        return None
    try:
        text = anchor.read_text(encoding="ascii").strip()
    except (OSError, ValueError) as exc:
        raise LedgerChainError(f"锚点无法读取(fail-closed): {exc}") from exc
    parts = text.split(" ")
    if len(parts) != 2 or not parts[0].isdigit() or int(parts[0]) < 1 or not _HEX64.fullmatch(parts[1]):
        raise LedgerChainError("锚点格式不合法(fail-closed):锚点被改写或写到一半")
    return int(parts[0]), parts[1]


def _replace_retry(tmp: Path, target: Path, attempts: int = 8) -> None:
    for index in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if index == attempts - 1:
                raise
            time.sleep(0.05 * (index + 1))


def write_anchor(anchor: Path, seq: int, head_hash: str) -> None:
    """原子写锚点(唯一临时名 + 替换 + fsync)。失败抛 LedgerChainError:写不下锚点就不能放出"账本已更新"。"""
    anchor = Path(anchor)
    tmp = anchor.with_name(f"{anchor.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        anchor.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="ascii", newline="") as stream:
            stream.write(f"{seq} {head_hash}\n")
            stream.flush()
            os.fsync(stream.fileno())
        _replace_retry(tmp, anchor)
    except OSError as exc:
        raise LedgerChainError(f"锚点写入失败(fail-closed): {exc}") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def verify(rows: list[dict], anchor: Path, *, what: str, allow_ahead: bool, tool_hint: str) -> None:
    """逐条核对链并用锚点核对头;任何一处对不上就抛 LedgerChainError。

    `rows` 必须已按 seq 升序、且每条都是对象。`what` 只用于消息。"""
    hint = f"人工核对后可用 `{tool_hint}` 处理(旧账本迁移用 migrate,确认从当前内容重新起算用 reset)"
    prev_hash = GENESIS
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not all(k in row for k in ("seq", "prev", "hash")):
            raise LedgerChainError(f"{what}第 {index} 条没有哈希链字段(fail-closed):旧账本或被外部写入;{hint}")
        if row["seq"] != index or row["prev"] != prev_hash:
            raise LedgerChainError(f"{what}哈希链在第 {index} 条断开(fail-closed):记录被改动、插入或删除;{hint}")
        body = {k: v for k, v in row.items() if k != "hash"}
        try:
            expected = digest(prev_hash, body)
        except (TypeError, ValueError) as exc:
            raise LedgerChainError(f"{what}第 {index} 条无法计算哈希(fail-closed): {exc}") from exc
        if row["hash"] != expected:
            raise LedgerChainError(f"{what}第 {index} 条的哈希对不上(fail-closed):记录被改动;{hint}")
        prev_hash = row["hash"]
    anchored = read_anchor(anchor)
    if anchored is None:
        if rows:
            raise LedgerChainError(f"{what}有记录却没有锚点(fail-closed):锚点被删过,或账本是被换进来的;{hint}")
        return
    seq, head = anchored
    if len(rows) < seq or rows[seq - 1]["hash"] != head:
        raise LedgerChainError(f"{what}与锚点对不上(fail-closed):账本被回滚、截断或整条换掉;{hint}")
    if not allow_ahead and len(rows) != seq:
        raise LedgerChainError(f"{what}比锚点多出 {len(rows) - seq} 条(fail-closed):多出的记录没有经过合法的写入路径;{hint}")


def read_verified(path: Path, parse_line, *, what: str, allow_ahead: bool, tool_hint: str,
                  retries: int = 3) -> list[dict]:
    """读 JSONL 账本并核链。读到的瞬间可能撞上写者(账本已追加、锚点还没写),所以对账失败时短暂重试几次。"""
    last: LedgerChainError | None = None
    for attempt in range(retries):
        rows = parse_line(path)
        try:
            verify(rows, anchor_path_for(path), what=what, allow_ahead=allow_ahead, tool_hint=tool_hint)
            return rows
        except LedgerChainError as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(0.05)
    assert last is not None
    raise last


@contextlib.contextmanager
def file_lock(target: Path, timeout: float = 10.0, stale_seconds: float = 30.0):
    """跨进程独占锁(锁文件 + O_EXCL)。账本一旦有哈希链,"两个写者读到同一个尾巴各追加一条"
    就会把链写成分叉——所以试验账本也得跨进程互斥(此前只有同进程锁)。
    残留锁按**年龄**判定(持有者崩了不会清);操作都在毫秒级,30 秒足够保守。"""
    target = Path(target)
    lock = target.with_name(target.name + ".chainlock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > stale_seconds:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise LedgerChainError(f"账本被其他进程占用,拒绝写入: {target.name}") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        for index in range(10):
            try:
                lock.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.02 * (index + 1))
