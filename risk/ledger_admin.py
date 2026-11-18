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

import contextlib
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


# 账本维护入口的锁等待上限(秒)。拿不到锁就**失败**:人工维护是低频操作,失败后重试即可,
# 默默等满半分钟只会让人以为命令卡死了。
ADMIN_LOCK_TIMEOUT = 5.0


@contextlib.contextmanager
def _ledger_lock(path: Path):
    """账本维护入口的跨进程锁——**与写入路径共用同一把锁**(`risk/ledger_chain.file_lock`)。

    第 40 轮(codex 的 High 3):此前 `migrate_approvals` / `reset_approvals` **一把锁都不拿**,
    而批准记录的另一条写路径(`scripts/research_sandbox.record_approval`)用的是**另一把**锁
    (`approvals.json.lock`)。两把锁互不相干 ⇒ "批准与重置并发"能静默丢掉一条已批准的记录,
    而链仍然自洽(重置按有效前缀重新定锚)。现在两边共用同一把锁,互斥才成立。

    第 43 轮:错误消息按 `LedgerChainError.reason` 分情形说(暂态占用 / 本进程重入 / 权限),
    不再一律说"被其他进程占用"——那句话对"重入"和"权限错误"都是**错的**。
    """
    # 第 45 轮:只翻译"**拿不到锁**"这一种失败。
    # 此前 `try` 圈住的是**整个临界区**(含 `yield`),于是临界区**内部**抛的
    # `LedgerChainError`(reason="chain",如锚点写失败/链对不上)也会被翻译成
    # "账本被其他进程占用"——**那句话是错的**(不是被占用,是链本身坏了)。
    # 这与第 42 轮在 `scripts/research_sandbox._approvals_lock` 修掉的是同一个问题:
    # 那一边改了,这一边漏了。
    # 做法与沙盒侧同款:手动 `__enter__`/`__exit__`,把"进不去"与"进去之后坏了"分开。
    # `stale_seconds` **显式**传出来(第 45 轮):它是**安全口径**(同一把锁文件的所有调用方
    # 必须用同一个值),不能靠 `file_lock` 的默认值——默认值是"只有不传参的人才用"的暗口径。
    manager = chain.file_lock(Path(path), timeout=ADMIN_LOCK_TIMEOUT,
                               stale_seconds=chain.DEFAULT_STALE_SECONDS)
    try:
        manager.__enter__()
    except chain.LedgerChainError as exc:
        raise AdminError(_ledger_lock_message(Path(path).name, exc)) from exc
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def _ledger_lock_message(name: str, exc: chain.LedgerChainError) -> str:
    """锁失败的对外说法(按 reason 分情形)。

    第 45 轮:`occupancy` 从"兜底分支"里**单独拎出来**。
    此前只有 `reentrancy` 与 `permission` 单独说,其余(含 `occupancy`)一律落进兜底,
    兜底那句话是"账本被其他进程占用"——于是 reason=`chain`/`generic` 也被说成
    "被其他进程占用",**说错了原因**(被占用 ⇒ 该重试;链坏了 ⇒ 重试没用,得人工处理)。
    兜底现在只说"锁获取失败(reason=...)",不替未知原因编一个原因。
    """
    reason = getattr(exc, "reason", "generic")
    if reason == "reentrancy":
        return f"账本锁被本进程同一线程重复获取(重入),拒绝维护: {name}({exc})"
    if reason == "permission":
        return f"账本锁文件无法创建(权限错误,不是被其他进程占用),拒绝维护: {name}({exc})"
    if reason == "occupancy":
        return (f"账本被其他进程占用(暂态占用,**请稍后重试**),拒绝维护: {name}({exc})")
    return f"账本锁获取失败(reason={reason}),拒绝维护: {name}({exc})"


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
    with _ledger_lock(path):
        # 读也在锁内:迁移会整份重写账本,先读后加锁会把期间追加的记录抹掉(第 40 轮)。
        rows = tl.parse(path)
        if any(k in row for row in rows for k in ("seq", "prev", "hash")):
            raise AdminError("账本里已有哈希链字段,拒绝按旧账本迁移")
        sealed, prev = [], chain.GENESIS
        for index, row in enumerate(rows, start=1):
            rec = chain.seal({"kind": row["kind"], "count": row["count"], "at": row.get("at")}, prev, index)
            sealed.append(rec)
            prev = rec["hash"]
        backup = _backup(path, "legacy")
        _write_lines(path, sealed)
        if sealed:
            chain.write_anchor(anchor, sealed[-1]["seq"], sealed[-1]["hash"],
                               total=sum(r["count"] for r in sealed))
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


def _claimed_total(path: Path) -> int:
    """账本当前**声称**的累计试验次数(不看链)。

    第 40 轮(Medium 1):重置的"防变小"判据不能只看**有效前缀**——
    链在中间断掉时,有效前缀可能比真实历史少一大截,拿它当基准就等于"变小不算变小"。
    这里按**能解析出来的每一行**求和(链字段对不对不管),它是"账本声称的 N"的上界,
    宁可高估(要求人显式确认)也不低估(放过一次悄悄变小)。

    第 43 轮(codex 的 H2):这个上界**只覆盖"现在还在文件里的行"**——
    行一旦被删,基准也跟着变小,于是"删行"这种**最该防**的攻击反而检不出来。
    所以它必须与**锚点里记的累计次数**一起用(见 `_anchor_state`)。"""
    total = 0
    if not path.exists():
        return 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict):
                continue
            count = row.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                total += count
    except OSError as exc:
        # **读不了不许当 0**(第 55 轮之二十五,codex 第 2 轮 L4):
        # 上一版 `except OSError: return 0` ⇒ 读失败被当成「账本声称 0 次试验」
        # ⇒ 「防变小」的基准**归零** ⇒ 一次悄悄变小反而**看不出来**
        # ⇒ 那正是这段函数自己写的原则的反面
        # (「宁可高估也不低估,放过一次悄悄变小」)。
        # ⇒ 现在**报出去**(fail-closed):读不了就不许据此判断。
        raise AdminError(f"账本无法读取,拒绝据此判断累计次数: {path.name}({exc})") from exc
    return total


def _anchor_state(path: Path) -> tuple[int | None, int | None]:
    """锚点记的 `(seq, 累计次数)`;没有锚点 -> (None, None)。

    第 43 轮:锚点从"只有 seq+hash"扩成"seq+hash+累计次数"(旧的两段锚点仍读得了,
    只是拿不到累计次数)。**锚点格式不合法时直接报错**(fail-closed):
    锚点被改写过,这时候"猜一个基准"等于放水。"""
    anchor = chain.anchor_path_for(path)
    try:
        anchored = chain.read_anchor(anchor)
    except chain.LedgerChainError as exc:
        raise AdminError(f"锚点无法读取,拒绝维护(fail-closed): {exc}") from exc
    if anchored is None:
        return None, None
    seq, _head, total = anchored
    return seq, total


def reset_trials(path: Path, *, add_trials: int = 0, at: float | None = None,
                 allow_reduce_n: bool = False, reason: str = "") -> dict:
    """链断之后的人工出路:保留**链上仍然有效的前缀**,丢弃其后内容,按保留部分重新定锚。

    第 40 轮(Medium 1):**不许悄悄把 N 变小**。N 变小 = DSR 的分母变小 = 去通胀失效
    (年化 Sharpe 3.5:N=1620 时 DSR 0.6397 不过,N=1 时 0.9999 过——见 risk/trial_ledger.py 的说明)。
    所以:重置后的累计次数若**小于**账本当前声称的总数,必须显式加 `allow_reduce_n`
    (CLI: `--allow-reduce-n`)才允许;并且重置会把 before/after 写进**链里**(kind=`ledger_reset`),
    "曾经变小过"这件事本身可审计。

    第 43 轮(codex 的 H2):基准从"文件当前自称的次数"改成
    **max(文件自称的次数, 锚点记的累计次数)**。差别在于**删行**:
    此前把账本截短(60 → 10)再重置,基准也跟着变成 10,于是"缩小"完全检不出来,
    而重置记录还会把 before/after 都写成 10,把丢失**洗白**。
    现在锚点记着"最后一次合法写入时的累计次数",删行 ⇒ 基准仍是 60 ⇒ 必须显式 `--allow-reduce-n`;
    并且因为锚点带累计次数,**丢了多少次是精确可算的**(不是估计),重置记录里也会写上。"""
    path = Path(path)
    if isinstance(add_trials, bool) or not isinstance(add_trials, int) or not 0 <= add_trials <= tl.MAX_ENTRY_COUNT:
        raise AdminError(f"--add-trials 必须是 0..{tl.MAX_ENTRY_COUNT} 的整数")
    with _ledger_lock(path):
        kept = _valid_prefix_trials(path)
        before = _claimed_total(path)
        anchor_seq, anchor_total = _anchor_state(path)
        if anchor_total is not None:
            before = max(before, anchor_total)
        # **精确**算出行被删掉了多少:锚点覆盖 rows 1..anchor_seq,它们的次数合计是 anchor_total;
        # 保留的有效前缀只有 len(kept) 行 ⇒ 缺的那些行的次数合计 = anchor_total - 前缀合计。
        lost_known = 0
        if anchor_total is not None and anchor_seq is not None and len(kept) < anchor_seq:
            lost_known = max(0, anchor_total - sum(r["count"] for r in kept))
        if lost_known and add_trials < lost_known and not allow_reduce_n:
            raise AdminError(
                f"账本里有 {lost_known} 次试验**被删掉了**(锚点可证:锚点记着累计 {anchor_total},"
                f"保留的有效前缀只有 {sum(r['count'] for r in kept)})。"
                f"重置会让累计试验次数从 {before} 降到 {before - lost_known} + {add_trials}"
                f"(变小会让 DSR 的分母变小、去通胀失效)。"
                f"请用 --add-trials {lost_known} 补记,或显式加 --allow-reduce-n 并写清 --reason")
        if add_trials:
            prev = kept[-1]["hash"] if kept else chain.GENESIS
            kept.append(chain.seal({"kind": "gate_run", "count": add_trials, "at": at if at is not None else time.time()},
                                   prev, len(kept) + 1))
        after = sum(r["count"] for r in kept)
        if after < before and not allow_reduce_n:
            raise AdminError(
                f"重置会把累计试验次数从 {before} 降到 {after}(变小会让 DSR 的分母变小、去通胀失效)。"
                f"确认要变小请显式加 --allow-reduce-n(并写清 --reason);"
                f"若丢失的试验次数未知,按估计补记 --add-trials(宁多勿少)")
        # 把"重置前后的 N"记进链里:重置记录 count=0(它不是一次试验),只带 before/after/reason。
        # 第 43 轮:另带 `lost`(被删掉的行对应的次数,锚点可证)与 `anchor_seq`,
        # 免得"曾经变小过"这件事只存在于 before/after 两个数里(那时它们可能相等,等于洗白)。
        prev = kept[-1]["hash"] if kept else chain.GENESIS
        kept.append(chain.seal({"kind": tl.RESET_KIND, "count": 0,
                                "at": at if at is not None else time.time(),
                                "before": before, "after": after, "lost": lost_known,
                                "anchor_seq": anchor_seq if anchor_seq is not None else 0,
                                "reason": reason[:200]},
                               prev, len(kept) + 1))
        backup = _backup(path, "reset")
        anchor = chain.anchor_path_for(path)
        _write_lines(path, kept)
        chain.write_anchor(anchor, kept[-1]["seq"], kept[-1]["hash"], total=after)
    return {"kept": len(kept) - 1, "recorded": len(kept), "total": after, "before": before,
            "lost": lost_known, "backup": backup.name if backup else None}


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
    """批准账本的落盘。

    **持久性口径与试验账本一致**(第 55 轮之二十五,codex 第 2 轮 L3):
    上一版是 `write_text` + `os.replace` ⇒ **没有 flush + fsync**
    ⇒ 断电/崩溃时可能落下一个**内容不完整**的文件(而试验账本那侧 `_write_lines`
    是有的)⇒ 同一套维护动作的持久性口径**不一致**。
    ⇒ 现在走同一套:唯一临时名 + `flush` + `os.fsync` + `os.replace`。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".admin.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps({r["sha"]: r for r in rows}, ensure_ascii=False,
                                indent=2, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
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
    # 迁移是**改写**批准记录:读与写都在锁内,必须与批准写入路径互斥(第 40 轮 High 3)。
    with _ledger_lock(path):
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
    unreadable = False
    # 读与写**都在锁内**:重置会丢弃链后段的批准记录,若先读后加锁,中间夹进来的批准会被静默丢掉
    # (第 40 轮 High 3)。
    with _ledger_lock(path):
        if path.exists():
            try:
                data = _load_dict(path)
            except AdminError:
                # 容错(与 reset_trials 同口径):读不出 = 没有任何有效前缀,备份原文件后按创世重新定锚。
                # 严格读取会把"文件被写坏"变成"人工出路也被卡死"(第 32 号 PR 复审 High)。
                data = {}
                unreadable = True
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
            if unreadable:
                dropped = ["<整个文件无法解析,已按创世重新定锚;原文件已备份>"]
        backup = _backup(path, "reset")
        anchor = chain.anchor_path_for(path)
        if kept:
            _write_dict(path, kept)
            chain.write_anchor(anchor, kept[-1]["seq"], kept[-1]["hash"])
        else:
            anchor.unlink(missing_ok=True)
    return {"kept": len(kept), "dropped": dropped, "backup": backup.name if backup else None}
