"""账本的**人工**维护入口(迁移旧账本 / 链断后重置),只给 scripts/research_ledger.py 用(Issue #31)。

与 `risk/trial_ledger.py` 分开,是因为后者的承诺是"只增不减,没有删除/回退接口"——
迁移与重置恰恰是会改写账本的动作,不能挂在那个模块上。候选与网关没有任何入口调用本模块。

两个动作的口径:
- `migrate_*`:无链的旧账本 -> 哈希链 + 锚点,**原文件先备份**;已有锚点/已有链字段的账本拒绝迁移
  (那种状态要么正常、要么被篡改,不该被"迁移"洗白)。
- `reset_*`:链断/回滚/截断之后的人工出路。保留**链上仍然有效的前缀**,丢弃其后内容,按保留部分重新定锚;
  原文件备份。**重置不会把丢失的数据找回来**:试验账本被抹掉的部分用 `--add-trials` 按估计值补记(宁多勿少),
  批准账本被丢弃的批准需要人重新批准。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from risk import ledger_chain as chain
from risk import trial_ledger as tl

HEX64 = re.compile(r"[0-9a-f]{64}")


class AdminError(Exception):
    pass


def _backup(path: Path, tag: str) -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.{tag}-{int(time.time())}.bak")
    if target.exists():
        raise AdminError(f"备份文件已存在,拒绝覆盖: {target.name}")
    os.replace(path, target)
    return target


def _write_lines(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".admin.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


# ---------- 试验账本 ----------

def migrate_trials(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise AdminError("试验账本不存在,无需迁移")
    anchor = chain.anchor_path_for(path)
    if anchor.exists():
        raise AdminError("该账本已有锚点,拒绝迁移(已迁移过,或锚点是被放进来的;确认后用 reset)")
    rows = tl.parse(path)
    if any(k in row for row in rows for k in ("seq", "prev", "hash")):
        raise AdminError("账本里已有哈希链字段,拒绝按旧账本迁移")
    with chain.file_lock(path):
        sealed, prev = [], chain.GENESIS
        for index, row in enumerate(rows, start=1):
            rec = chain.seal({"kind": row["kind"], "count": row["count"], "at": row.get("at")}, prev, index)
            sealed.append(rec)
            prev = rec["hash"]
        backup = _backup(path, "legacy")
        _write_lines(path, sealed)
        if sealed:
            chain.write_anchor(anchor, sealed[-1]["seq"], sealed[-1]["hash"])
    return {"migrated": len(sealed), "total": sum(r["count"] for r in sealed),
            "backup": backup.name if backup else None}


def _valid_prefix_trials(path: Path) -> list[dict]:
    kept: list[dict] = []
    prev = chain.GENESIS
    if not path.exists():
        return kept
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = tl.check_row(json.loads(line))
            ok = (row.get("seq") == len(kept) + 1 and row.get("prev") == prev
                  and row.get("hash") == chain.digest(prev, {k: v for k, v in row.items() if k != "hash"}))
        except (ValueError, TypeError):
            ok = False
        if not ok:
            break
        kept.append(row)
        prev = row["hash"]
    return kept


def reset_trials(path: Path, *, add_trials: int = 0, at: float | None = None) -> dict:
    path = Path(path)
    if isinstance(add_trials, bool) or not isinstance(add_trials, int) or not 0 <= add_trials <= tl.MAX_ENTRY_COUNT:
        raise AdminError(f"--add-trials 必须是 0..{tl.MAX_ENTRY_COUNT} 的整数")
    with chain.file_lock(path):
        kept = _valid_prefix_trials(path)
        if add_trials:
            prev = kept[-1]["hash"] if kept else chain.GENESIS
            kept.append(chain.seal({"kind": "gate_run", "count": add_trials, "at": at if at is not None else time.time()},
                                   prev, len(kept) + 1))
        backup = _backup(path, "reset")
        anchor = chain.anchor_path_for(path)
        if kept:
            _write_lines(path, kept)
            chain.write_anchor(anchor, kept[-1]["seq"], kept[-1]["hash"])
        else:
            anchor.unlink(missing_ok=True)
    return {"kept": len(kept), "total": sum(r["count"] for r in kept), "backup": backup.name if backup else None}


# ---------- 批准账本({sha: 记录} 的 JSON 对象,记录带 sha/name/at/seq/prev/hash) ----------

def _load_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdminError(f"批准记录无法读取: {exc}") from exc
    if not isinstance(data, dict):
        raise AdminError("批准记录格式错误(应为对象)")
    return data


def _write_dict(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".admin.tmp")
    tmp.write_text(json.dumps({r["sha"]: r for r in rows}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def migrate_approvals(path: Path) -> dict:
    """旧批准记录({sha: {name, at}})-> 链。**这是人工把现有内容认作"已批准"**:
    迁移之前请人先确认里面没有不认识的 sha(旧账本里本来就分不出哪些是人批的)。"""
    path = Path(path)
    if not path.exists():
        raise AdminError("批准记录不存在,无需迁移")
    anchor = chain.anchor_path_for(path)
    if anchor.exists():
        raise AdminError("该批准记录已有锚点,拒绝迁移(已迁移过,或锚点是被放进来的;确认后用 reset)")
    data = _load_dict(path)
    for key, value in data.items():
        if not HEX64.fullmatch(str(key)) or not isinstance(value, dict):
            raise AdminError(f"记录 {str(key)[:12]}… 不是 {{sha256: 对象}} 形态,拒绝迁移")
        if any(k in value for k in ("seq", "prev", "hash")):
            raise AdminError("记录里已有哈希链字段,拒绝按旧账本迁移")
    ordered = sorted(data.items(), key=lambda kv: (str(kv[1].get("at", "")), kv[0]))
    sealed, prev = [], chain.GENESIS
    for index, (sha, value) in enumerate(ordered, start=1):
        rec = chain.seal({"sha": sha, "name": value.get("name", ""), "at": value.get("at", "")}, prev, index)
        sealed.append(rec)
        prev = rec["hash"]
    backup = _backup(path, "legacy")
    _write_dict(path, sealed)
    if sealed:
        chain.write_anchor(anchor, sealed[-1]["seq"], sealed[-1]["hash"])
    return {"migrated": len(sealed), "backup": backup.name if backup else None}


def reset_approvals(path: Path) -> dict:
    """链断之后:只保留链上仍然有效的前缀,其余(含被外部写进来的)丢弃,按保留部分重新定锚。"""
    path = Path(path)
    kept: list[dict] = []
    dropped: list[str] = []
    if path.exists():
        data = _load_dict(path)
        rows = [v for v in data.values() if isinstance(v, dict) and isinstance(v.get("seq"), int)]
        rows.sort(key=lambda r: r["seq"])
        prev = chain.GENESIS
        for row in rows:
            body = {k: v for k, v in row.items() if k != "hash"}
            try:
                ok = (row["seq"] == len(kept) + 1 and row.get("prev") == prev
                      and row.get("hash") == chain.digest(prev, body) and data.get(row.get("sha")) is row)
            except (TypeError, ValueError):
                ok = False
            if not ok:
                break
            kept.append(row)
            prev = row["hash"]
        dropped = [str(k)[:12] + "…" for k in data if k not in {r["sha"] for r in kept}]
    backup = _backup(path, "reset")
    anchor = chain.anchor_path_for(path)
    if kept:
        _write_dict(path, kept)
        chain.write_anchor(anchor, kept[-1]["seq"], kept[-1]["hash"])
    else:
        anchor.unlink(missing_ok=True)
    return {"kept": len(kept), "dropped": dropped, "backup": backup.name if backup else None}
