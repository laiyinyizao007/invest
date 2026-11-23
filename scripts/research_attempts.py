"""研究尝试记录(Issue #67):每一次研究尝试都追加一行,**含被拒绝/失败的**。

为什么要有它:自动批准打开后不再有人逐次过目,事后能回看的只有这份记录。试验账本只记「提交了几次」
(DSR 的分母),不记想法、候选哈希、怎么批的、门槛怎么说——这里补上。
- 追加写 + 哈希链(口径同 `risk/ledger_chain.py`),只增不减;链断/被篡改读侧抛错(fail-closed)。
- 与批准账本、试验账本一样,位于候选执行窗口的写禁区之外(审计钩子只放行临时目录)。
- 本模块**不做任何批准判定**,也不被运行侧读取;它只是旁路记录。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[1]
if str(CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(CODE_ROOT))

from risk import ledger_chain as chain  # noqa: E402

EVENTS = ("start", "screening_rejected", "approval_requested", "manual_approved", "auto_approved",
          "advanced", "failure", "note")
MAX_TEXT = 600
_HEX64 = re.compile(r"[0-9a-f]{64}")
TOOL_HINT = "尝试记录链断/被篡改:先人工核对,保留原文件备份后再重新开始记录。"
_lock = threading.Lock()


class AttemptsError(Exception):
    """尝试记录损坏、被篡改、不可读或写入参数非法。"""


def _clip(value: object) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + "…"


def _check_row(data: object) -> dict:
    if not isinstance(data, dict) or data.get("event") not in EVENTS:
        raise ValueError("尝试记录条目格式不对")
    return data


def parse(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    rows: list[dict] = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(_check_row(json.loads(line)))
    except (OSError, ValueError) as exc:
        raise AttemptsError(f"尝试记录损坏或无法读取(fail-closed): {exc}") from exc
    return rows


def read(path: Path) -> list[dict]:
    try:
        return chain.read_verified(Path(path), parse, what="尝试记录", allow_ahead=True, tool_hint=TOOL_HINT)
    except chain.LedgerChainError as exc:
        raise AttemptsError(str(exc)) from exc


def record_attempt(path: Path, event: str, *, session: str | None = None, idea: str | None = None,
                   candidate: str | None = None, sha256: str | None = None, via: str | None = None,
                   note: str | None = None) -> int:
    """追加一条并返回追加后的总行数。调用方传入的文本**必须已过模型可见通道的清洗**。"""
    if event not in EVENTS:
        raise AttemptsError(f"未知的尝试事件: {event!r}")
    if sha256 is not None and not _HEX64.fullmatch(str(sha256)):
        raise AttemptsError("sha256 格式不对")
    path = Path(path)
    body = {"event": event, "session": _clip(session), "idea": _clip(idea), "candidate": _clip(candidate),
            "sha": sha256 or "", "via": _clip(via), "note": _clip(note),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with chain.file_lock(path, timeout=chain.DEFAULT_LOCK_TIMEOUT, stale_seconds=chain.DEFAULT_STALE_SECONDS):
                rows = read(path)
                prev = rows[-1]["hash"] if rows else chain.GENESIS
                record = chain.seal(body, prev, len(rows) + 1)
                try:
                    with open(path, "a", encoding="utf-8", newline="") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as exc:
                    raise AttemptsError(f"尝试记录写入失败(fail-closed): {exc}") from exc
                chain.write_anchor(chain.anchor_path_for(path), record["seq"], record["hash"])
        except chain.LedgerChainError as exc:
            raise AttemptsError(str(exc)) from exc
    return len(rows) + 1
