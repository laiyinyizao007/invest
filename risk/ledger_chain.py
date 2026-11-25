"""账本防篡改的共用件:哈希链 + 外置锚点(试验账本与批准账本共用,Issue #31)。

口径与 `risk/ladder.py`(feat/21-stage3 分支)一致,不另起第三套:
- 每条记录含 `seq`(从 1 起)、`prev`(上一条的 hash,创世为 64 个 0)、`hash`
  (= sha256(prev + "|" + 规范化 JSON(记录去掉 hash)),键排序、紧凑分隔、ensure_ascii=False);
- 锚点文件(`<seq> <hash>` 一行)放在**与账本不同的目录**(`data/ledger_anchor/`),写在账本之后;
- 读侧逐条核对链,再用锚点核对"头":链断/回滚/截断/整条被换掉 -> `LedgerChainError`(fail-closed)。

诚实的边界(与 ladder 同,别写成"防篡改"之外的东西):
- 哈希链**没有密钥**:能写账本与锚点两处的人,可以把两者一起改成一条自洽的链。
  本层只拦"只改了其中一处"(候选经 numpy/pandas 往账本写字节、却够不到锚点,或没想到要改锚点)。
  **网关容器内锚点目录与账本同在可写区**(Issue #73:容器要写自己的试验锚点):候选在容器里"够不到锚点"
  靠的是研究沙盒的审计钩子(写入白名单),与保护账本本身的是同一道,不是独立信任域。
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
import threading
import time
import uuid
from pathlib import Path

GENESIS = "0" * 64
ANCHOR_DIRNAME = "ledger_anchor"
_HEX64 = re.compile(r"[0-9a-f]{64}")

# 残留锁的**年龄**兜底阈值(秒)。**同一把锁文件的所有调用方必须用这一个值**
# (第 43 轮,codex 的 M2):此前批准路径传 60 秒、账本维护入口用 30 秒,
# 而两者锁的是同一个 `approvals.json.chainlock` ——口径不一致时,维护入口能按
# 自己的口径把批准路径的**活锁**当成陈旧锁抢走。
# 注意:这只在**读不到持有者 PID** 时才当兜底;持有者还活着就一律等它(见 `file_lock`)。
DEFAULT_STALE_SECONDS = 30.0

# 拿不到锁时**等多久就放弃**(秒)。
#
# 第 45 轮:此前它只是 `file_lock` 的默认参数值(30.0),而三个调用方里有两个显式传了 5.0
# ——"默认的 30 秒"于是成了一个**只有不传参的人才用**的暗口径。
# 现在提为具名常量,并把两种口径的分工写清:
#   - `timeout`(本常量)= **调用方策略**:等不到就失败。人工维护入口故意取小
#     (失败后重试即可,默默等半分钟只会让人以为命令卡死了);写入路径取大
#     (宁可多等,别把并发写入变成一次失败)。
#   - `stale_seconds`(`DEFAULT_STALE_SECONDS`)= **安全口径**:多久算残留锁,
#     **同一把锁文件的所有调用方必须用同一个值**(见上面的说明)。这个不许有策略差异。
#
# 两者**不是**一回事,不要为了"看起来一致"把 timeout 也统一成一个数。
DEFAULT_LOCK_TIMEOUT = 30.0

# **每线程**的随机 token:锁文件里除 PID 外还写它,用来识别"**同一个线程**又来拿这把锁"(重入)。
#
# 为什么是**每线程**而不是每进程(第 42 轮改的):同进程里**另一个线程**持有/等待同一把锁是
# **真并发**,必须"等",不能当成重入——第一版按进程分组,把 6 线程并发批准误拒成"重入"
# (用例 test_concurrent_approvals_do_not_lose_entries 直接打挂)。
# 有了它就不会把"PID 被复用"误判成自己的锁(PID 相同但 token 不同 ⇒ 不是本线程)。
_THREAD_LOCAL = threading.local()


def _thread_token() -> str:
    """本线程的随机 token(第一次用到时生成,之后复用)。"""
    token = getattr(_THREAD_LOCAL, "token", None)
    if token is None:
        token = uuid.uuid4().hex
        _THREAD_LOCAL.token = token
    return token


class LedgerChainError(Exception):
    """账本的哈希链/锚点核对失败(fail-closed)。消息里带人工出路。

    `reason` 让**调用方**能按情形说对话(第 43 轮,codex 的 M3):
    - `"occupancy"` —— 锁被**别的**持有者占着(暂态占用,该等/该重试);
    - `"reentrancy"` —— **本进程同一线程**又来拿这把锁(调用链有环,重试没用);
    - `"permission"` —— 锁文件建不出来(权限问题,重试没用);
    - `"chain"` —— 链/锚点核对失败(人工出路在消息里);
    - `"generic"` —— 其余。
    上层(批准路径、账本维护入口)按 `reason` 翻译消息——把"重入"说成"被其他进程占用"
    是**说错话**(第 42 轮已修过一次同类问题)。"""

    def __init__(self, message: str, *, reason: str = "generic"):
        super().__init__(message)
        self.reason = reason


def pid_alive(pid: int) -> bool:
    """进程还在不在。

    **不能**用 `os.kill(pid, 0)`:Windows 上 CPython 把它实现成 TerminateProcess(pid, 0)
    ——那会把别人的进程杀掉(第 40 轮把这段从 `scripts/research_sandbox.py` 搬过来时保留的坑)。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        # **必须** use_last_error=True:否则 get_last_error() 拿到的是线程的旧值,
        # 不是这次 OpenProcess 失败的原因(第 5 轮 codex 的 L3)。
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # **必须**显式声明 restype/argtypes(第 6 轮 codex 的 M4):
        # 默认 restype 是 c_int,HANDLE 是 64 位指针,会被截断成 32 位——
        # 截断后的值可能为 0(活进程被误判成已死 → 残留锁被误抢),
        # 更糟的是拿截断后的句柄去 CloseHandle,可能关掉**别的**进程的句柄。
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return ctypes.get_last_error() == 5          # 5 = ACCESS_DENIED:进程在,只是没权限查
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    """锚点位置:账本所在目录的**上一级**下的 `ledger_anchor/`,文件名带**账本相对该上一级的路径**的短哈希。

    Issue #73:以前哈希的是账本**绝对路径**,而同一份账本在宿主机(`C:/.../data/research/`)与网关容器
    (`/app/data/research/`)里绝对路径不同 ⇒ 两边算出不同的锚点文件名,一方写的锚点另一方永远读不到
    (容器里批准账本、试验账本因此一律 fail-closed)。现在只取 `<账本所在目录名>/<账本文件名>`(转小写),
    两边一致;不同账本仍各有各的锚点。不同的根目录各有各的 `ledger_anchor/`,测试之间互不串账。
    **如实登记**:这个目录在网关容器里与账本同在可写区,候选受审计钩子约束的程度与对账本一样,不是独立信任域。

    **如实说明**(第 32 号 PR 复审实测):这个目录**不是**"候选碰不到"的位置——没有审计钩子时候选能往
    `data/ledger_anchor/` 写文件,也能写账本本身。锚点真正的保护有两层:
    ① 文件名里的 `sha256(相对路径)` 短哈希:候选算不出 sha256(pickle/hashlib 都不在白名单)、也列不出目录,
       猜不到该写哪个文件(相对路径比绝对路径好猜,但容器内能列目录的候选本来就不靠这一层);
       ② 读侧要求账本头与锚点**同时**一致。两者都不是密码学意义的保护(见模块文件头"诚实的边界")。
    各账本共用同一个 `ledger_anchor/` 目录。"""
    ledger = Path(ledger_path).resolve()
    relative = f"{ledger.parent.name}/{ledger.name}".lower()
    tag = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]     # 64 位:比旧的 40 位难碰撞
    return ledger.parent.parent / ANCHOR_DIRNAME / f"{ledger.stem.lower()}-{tag}.anchor"


def legacy_anchor_path_for(ledger_path) -> Path:
    """旧口径(绝对路径哈希)的锚点位置,**只给迁移工具用**:读侧不认它。"""
    ledger = Path(ledger_path)
    resolved = str(ledger.resolve()).replace("\\", "/").lower()
    tag = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    return ledger.resolve().parent.parent / ANCHOR_DIRNAME / f"{ledger.stem}-{tag}.anchor"


def read_anchor(anchor: Path) -> tuple[int, str, int | None] | None:
    """读锚点;不存在 -> None;存在但格式不对 -> LedgerChainError(被改写过)。

    锚点有两段(`<seq> <hash>`)或三段(`<seq> <hash> <total>`)。第三段是**该锚点时刻的
    累计试验次数**(第 43 轮,codex 的 H2):只有哈希时,"账本被删掉几行"谁也看不出来——
    读侧只能发现"比锚点短",人工重置却会**静默**把 N 变小(DSR 的分母变小 = 去通胀失效)。
    带上累计次数,删行就**精确**可算(丢了多少),重置必须显式补记。"""
    anchor = Path(anchor)
    if not anchor.exists():
        return None
    try:
        text = anchor.read_text(encoding="ascii").strip()
    except (OSError, ValueError) as exc:
        raise LedgerChainError(f"锚点无法读取(fail-closed): {exc}", reason="chain") from exc
    parts = text.split(" ")
    if len(parts) not in (2, 3) or not parts[0].isdigit() or int(parts[0]) < 1 or not _HEX64.fullmatch(parts[1]):
        raise LedgerChainError("锚点格式不合法(fail-closed):锚点被改写或写到一半", reason="chain")
    total = None
    if len(parts) == 3:
        if not parts[2].isdigit():
            raise LedgerChainError("锚点格式不合法(fail-closed):锚点被改写或写到一半", reason="chain")
        total = int(parts[2])
    return int(parts[0]), parts[1], total


def _replace_retry(tmp: Path, target: Path, attempts: int = 20) -> None:
    for index in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if index == attempts - 1:
                raise
            time.sleep(0.05 * (index + 1))


def write_anchor(anchor: Path, seq: int, head_hash: str, total: int | None = None) -> None:
    """原子写锚点(唯一临时名 + 替换 + fsync)。失败抛 LedgerChainError:写不下锚点就不能放出"账本已更新"。

    `total`:该锚点时刻的**累计试验次数**(可选)。试验账本写,批准账本不写(那边没有次数)。"""
    anchor = Path(anchor)
    tmp = anchor.with_name(f"{anchor.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    payload = f"{seq} {head_hash}" + (f" {total}" if total is not None else "") + "\n"
    try:
        anchor.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="ascii", newline="") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_retry(tmp, anchor)
    except OSError as exc:
        raise LedgerChainError(f"锚点写入失败(fail-closed): {exc}", reason="chain") from exc
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def verify(rows: list[dict], anchor: Path, *, what: str, allow_ahead: bool, tool_hint: str) -> None:
    """逐条核对链并用锚点核对头;任何一处对不上就抛 LedgerChainError。

    `rows` 必须已按 seq 升序、且每条都是对象。`what` 只用于消息。"""
    hint = f"人工出路:{tool_hint}"
    prev_hash = GENESIS
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not all(k in row for k in ("seq", "prev", "hash")):
            raise LedgerChainError(f"{what}第 {index} 条没有哈希链字段(fail-closed):旧账本或被外部写入;{hint}",
                                   reason="chain")
        if row["seq"] != index or row["prev"] != prev_hash:
            raise LedgerChainError(f"{what}哈希链在第 {index} 条断开(fail-closed):记录被改动、插入或删除;{hint}",
                                   reason="chain")
        body = {k: v for k, v in row.items() if k != "hash"}
        try:
            expected = digest(prev_hash, body)
        except (TypeError, ValueError) as exc:
            raise LedgerChainError(f"{what}第 {index} 条无法计算哈希(fail-closed): {exc}", reason="chain") from exc
        if row["hash"] != expected:
            raise LedgerChainError(f"{what}第 {index} 条的哈希对不上(fail-closed):记录被改动;{hint}", reason="chain")
        prev_hash = row["hash"]
    anchored = read_anchor(anchor)
    if anchored is None:
        if rows:
            raise LedgerChainError(f"{what}有记录却没有锚点(fail-closed):锚点被删过,或账本是被换进来的;"
                                   f"若刚从按绝对路径命名锚点的旧版本升级,先 `python scripts/research_ledger.py rename-anchors`;{hint}",
                                   reason="chain")
        return
    seq, head, total = anchored
    if len(rows) < seq or rows[seq - 1]["hash"] != head:
        raise LedgerChainError(f"{what}与锚点对不上(fail-closed):账本被回滚、截断或整条换掉;{hint}",
                               reason="chain")
    if not allow_ahead and len(rows) != seq:
        raise LedgerChainError(f"{what}比锚点多出 {len(rows) - seq} 条(fail-closed):多出的记录没有经过合法的写入路径;{hint}",
                               reason="chain")
    if total is not None:
        # 锚点记着"那一时刻的累计次数":对不上 ⇒ 有行被删/被改(fail-closed)。
        # 第 43 轮(codex 的 H2):只靠"账本比锚点短"只能发现**截断**,
        # 发现不了"删掉中间几行、后面几行接着写"——那时长度不变、只有**次数**对不上。
        counted = 0
        for row in rows[:seq]:
            count = row.get("count")
            if isinstance(count, int) and not isinstance(count, bool):
                counted += count
        if counted != total:
            raise LedgerChainError(
                f"{what}的累计次数与锚点对不上(fail-closed):锚点记 {total},"
                f"账本前 {seq} 条合计 {counted}(有行被删除或改写);{hint}", reason="chain")


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


def _lock_holder_pid(lock: Path) -> int | None:
    """锁文件里记的持有者 PID;读不到(刚建还没写完/内容损坏)= None。"""
    try:
        content = lock.read_text(encoding="ascii", errors="ignore").split()
        pid = int(content[0]) if content else -1
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _lock_holder_gone(lock: Path) -> bool:
    """锁的持有者进程**已经不在了**吗?(第 40 轮)

    锁文件里写着持有者的 PID(见 `file_lock`)。持有者崩了不会清锁文件,只按年龄判定的话
    每次都要干等 `stale_seconds`(默认 30 秒)才能抢锁——批准路径上这是实打实的卡顿。
    读不到 PID(刚创建还没写完/内容损坏)时**保守地当作"还在"**:宁可等超时,也不抢活人的锁。"""
    pid = _lock_holder_pid(lock)
    return pid is not None and not pid_alive(pid)


def _lock_held_by_this_thread(lock: Path) -> bool:
    """这个锁文件是不是**本线程**自己持有着?(第 42 轮)

    锁文件里写着 `<pid> <时间戳> <每线程 token>`;PID 与 token 都等于本线程的才算。
    - PID 相同而 token 不同 ⇒ 是"PID 被复用"或**别的线程**的锁 ⇒ **不算**重入;
    - 同进程的另一个线程拿着锁 ⇒ 不是重入(那是真并发,该等)。"""
    try:
        content = lock.read_text(encoding="ascii", errors="ignore").split()
    except OSError:
        return False
    if len(content) < 3:
        return False
    try:
        pid = int(content[0])
    except ValueError:
        return False
    return pid == os.getpid() and content[2] == _thread_token()


@contextlib.contextmanager
def file_lock(target: Path, timeout: float = DEFAULT_LOCK_TIMEOUT,
              stale_seconds: float = DEFAULT_STALE_SECONDS,
              permission_grace: float = 3.0):
    """跨进程独占锁(锁文件 + O_EXCL)。账本一旦有哈希链,"两个写者读到同一个尾巴各追加一条"
    就会把链写成分叉——所以试验账本也得跨进程互斥(此前只有同进程锁)。

    **陈旧判据(第 43 轮,codex 的 M2)**:现在**以"持有者进程还在不在"为主**,
    年龄只在**读不到持有者 PID** 时才当兜底。此前是"年龄到了就抢"——持有者只是写得慢一点
    (活着的进程!)也会被抢走锁,于是"批准与重置并发"照样能静默丢记录。
    后果:持有者崩了 ⇒ 立刻可抢(不必干等 `stale_seconds`);持有者活着 ⇒ 一直等到超时。
    读不到 PID(锁文件刚建、还没写完)⇒ 按年龄兜底,**mtime 在未来**同样算陈旧
    (否则一个时间戳在未来的 .chainlock 会让写账本阻塞满超时)。

    **同一把锁文件的所有调用方必须用同一个 `stale_seconds`**(第 43 轮):此前
    批准路径传 60 秒、账本维护入口用默认 30 秒,而两者锁的是**同一个** `approvals.json.chainlock`
    ——于是维护入口能按 30 秒的口径偷走批准路径的活锁。统一到 `DEFAULT_STALE_SECONDS`。

    两类失败**分开报**(第 37 轮复审):
    - **被占用**:`FileExistsError`,或 Windows 上锁文件刚被持有者 unlink、处于"删除待决"时 `O_EXCL` 抛的
      `PermissionError`(压测复现的真实竞态)——重试;
    - **真权限错误**(锁路径是目录,或 PermissionError 连续超过 `permission_grace` 秒不消失):原样报"权限错误",
      不等满 timeout、也不冒充成"被其他进程占用"。

    **第 40 轮(High 3)**:锁文件里现在写**持有者的 PID**,残留锁多一条判据——"持有者进程已经不在了",
    不必等满 `stale_seconds` 才允许抢锁(此前只有年龄判据,持有者崩掉之后批准要干等 30 秒)。
    这条判据对 Windows 尤其重要:`os.kill(pid, 0)` 在 Windows 上会**真的杀掉**那个进程,
    所以判活必须走 `pid_alive`(OpenProcess + PROCESS_QUERY_LIMITED_INFORMATION)。"""
    target = Path(target)
    lock = target.with_name(target.name + ".chainlock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    started_at = deadline - timeout
    perm_since: float | None = None
    while True:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {time.time():.3f} {_thread_token()}\n".encode("ascii"))
            finally:
                os.close(fd)
            break
        except (FileExistsError, PermissionError) as exc:
            if isinstance(exc, PermissionError) and not isinstance(exc, FileExistsError):
                now = time.monotonic()
                perm_since = perm_since if perm_since is not None else now
                if lock.is_dir() or now - perm_since > permission_grace:
                    raise LedgerChainError(f"账本锁文件无法创建(权限错误,不是被其他进程占用): {exc}",
                                           reason="permission") from exc
            else:
                perm_since = None
            holder = _lock_holder_pid(lock)
            if holder is None:
                # 读不到持有者(刚建还没写完/内容损坏)⇒ 只能按**年龄**兜底判陈旧。
                try:
                    age = time.time() - lock.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > stale_seconds or age < -stale_seconds:
                    lock.unlink(missing_ok=True)
                    continue
            elif not pid_alive(holder):
                # 持有者进程已经不在 ⇒ 锁是残留,可抢(不必等满 stale_seconds)。
                lock.unlink(missing_ok=True)
                continue
            # 持有者还活着 ⇒ 锁**不是**陈旧的:哪怕 age 超过 stale_seconds 也不抢。
            # **重入**(第 42 轮):占用者就是**本线程** ⇒ 立刻失败,别空转到超时,
            # 也别报"被其他进程占用"(那句话在这里是错的)。
            # 注意:判定是**线程级**的——同进程的**另一个**线程拿着锁是**真并发**,
            # 该"等",不能当成重入(第 42 轮第三版才把这条区分开)。
            if _lock_held_by_this_thread(lock):
                raise LedgerChainError(
                    f"账本锁被**本进程同一线程**重复获取(重入),拒绝写入: {target.name}"
                    "——同一把锁不能嵌套持有(这通常意味着调用链上有个自己没发现的循环)",
                    reason="reentrancy") from None
            if time.monotonic() >= deadline:
                waited = time.monotonic() - started_at
                who = f"PID {holder}" if holder else "持有者未知"
                raise LedgerChainError(
                    f"账本被其他进程占用,拒绝写入: {target.name}"
                    f"(持有者 {who},已等待 {waited:.1f} 秒;这是暂态占用,请稍后重试)",
                    reason="occupancy") from None
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
