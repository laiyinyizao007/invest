"""收菜:把**已被人批准**的候选策略收进 `approved/`(运行时唯一认的目录)。

这是一个**独立脚本**,不是 `research_api.py` 的子命令——这是有意的:
  `scripts/research_gateway.py` 会 import `research_api`,两者在同一个进程里。
  收菜是**改变「运行时唯一认的目录」内容**的动作,是整个沙盒里最危险的一步,
  所以它不能和网关可达的代码放在同一个可导入的模块里。
  网关的端点白名单(`research_gateway.ROUTES`)里没有任何收菜入口,外层(毕昇/DSH)
  **请求不到**收菜——它只能请求批准,不能请求收菜,更不能自己批准。

收菜的**唯一**合法性来源:批准账本 `data/research-control/approvals.json`。
账本里没有这份字节 → 拒绝。人批准过(账本命中)→ 才谈得上收进 `approved/`。

四道校验(缺一不可):
  1. 源文件当前字节的 sha256 **必须在账本里**——收菜不产生批准,只搬运已批准的东西;
  2. 人确认之后**再算一次** sha256,必须仍等于账本里的那份——防「批准后文件被换掉」(TOCTOU);
  3. 写入 `approved/` 之后**复算**目标文件的 sha256,必须仍等于账本里的那份——复制必须逐字节一致;
  4. 目标路径固定为 `approved/<原文件名>`,**不接受**任何路径参数——防路径穿越。

人的门槛与批准一致:交互式终端(TTY)+ 粘贴**完整 64 位**哈希。
敲 `y` 不算——任何程序都能喂一个 `y`。非交互调用一律拒绝。

退出码沿用 `research_api.py` 的语义:0 成功 / 2 被拒绝 / 3 失败。
stdout 只出 JSON(模型可见通道的候选,出口统一过 `rs.scrub_model_visible`);
人要看的哈希与提示走 stderr。

状态机(`docs/roadmap.md` §2.2):
    pending_strategies/  ──(人批准,写账本)──>  批准账本
                                                │
                                      (本机收菜,本脚本)
                                                ↓
                                            approved/  ──(人工装载)──>  runtime
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

import research_config as rc
import research_sandbox as rs
from risk import approval_ledger
from risk import constitution as const
from risk.approved_guard import SAFE_FILE_NAME

EXIT_OK, EXIT_REFUSED, EXIT_FAILED = 0, 2, 3


class CollectError(Exception):
    """收菜被拒绝。`code` 是退出码。"""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def settings() -> tuple[dict, dict]:
    """(配置, 解析后的路径)。与 research_api 同一套路径来源。"""
    cfg = rc.load()
    return cfg, rc.resolve_paths(cfg, rc.ROOT)


def _model_visible_secrets() -> tuple[str, ...]:
    """与 `research_api` 同一份凭据清单(按值脱敏)。

    清单本体在 `research_config.known_secret_values`——网关 / MCP / CLI 三条模型可见通道共用这一份,
    口径不共享就会漂移,漏的那一条就是漏的那一条。
    """
    return rc.known_secret_values(label="research-collect")


def emit(obj) -> None:
    """stdout 的**唯一**出口:出去的 JSON 一律先过模型可见通道的清洗。

    与 `research_api.emit` 同一口径:CLI 的 stdout 是「模型可见通道的候选」,
    哈希不能从这里出去(模型拿到哈希就能在聊天里替人念哈希)。
    """
    print(json.dumps(rs.scrub_model_visible(obj, secrets=_model_visible_secrets()),
                     ensure_ascii=False, indent=2))


def resolve_source(strategy: str, cfg: dict, paths: dict) -> Path:
    """把 `--strategy` 解析成 `pending_strategies/` 里的一个文件。

    只接受**文件名**:含路径分隔符、含 `..`、不匹配 `candidates.name_pattern` 的一律拒绝。
    """
    if not isinstance(strategy, str) or not strategy.strip():
        raise CollectError("--strategy 必须是非空文件名")
    name = strategy.strip()
    if name != Path(name).name or name in (".", ".."):
        raise CollectError(f"--strategy 只接受文件名(不含路径): {name!r}")
    # 与运行时装载共用同一份文件名规则(risk/approved_guard.SAFE_FILE_NAME):
    # 收得进却装不上是最难查的一类错——两边必须同一条规则。
    if not SAFE_FILE_NAME.fullmatch(name):
        raise CollectError(f"策略文件名不符合约定 {SAFE_FILE_NAME.pattern}: {name!r}")
    source = paths["pending_dir"] / name
    if not source.is_file():
        raise CollectError(f"候选文件不存在: {name}")
    return source


def _remove_file(path: Path) -> None:
    """删掉一个刚写出来、但没通过校验的文件。"""
    try:
        path.unlink()
    except OSError:
        pass


def _atomic_write(target: Path, payload: bytes) -> None:
    """原子写:唯一临时名 + `os.replace`,Windows 上目标被占用时重试。

    写法与 `research_sandbox.record_approval` 同款:临时名带 pid **和随机后缀**
    (只带 pid 的话,同一进程里两次并发收菜会撞同一个临时文件名),
    替换用 `os.replace` 并在 PermissionError 时重试(Windows 上目标文件正被读着会这样)。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(payload)
        for index in range(5):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if index == 4:
                    raise
                time.sleep(0.1 * (index + 1))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def collect_job(strategy: str, input_fn=None, write=None, replace: bool = False) -> dict:
    """收菜:校验 → 人确认 → 原子写入 → 复算。

    `input_fn`/`write` 是给测试用的注入点(与 `research_api.approve_job` 同一手法):
    正式入口不传,于是走 `input()`/`stderr`。

    `replace`:目标文件已存在**且内容不同**时,默认**拒绝**——
    覆盖 approved/ 里的旧版本等于换掉运行时正在认的策略,这是另一件需要显式授权的事。
    带 `--replace` 才覆盖,并把旧版本留成 `<名>.prev`(字节一致),便于回溯。
    """
    cfg, paths = settings()
    source = resolve_source(strategy, cfg, paths)
    approvals_file = paths["approvals_file"]
    approved_dir = paths["approved_dir"]

    # ① 账本必须命中「这份字节」——收菜不产生批准,只搬运已批准的字节。
    # **只读一次字节**:哈希、筛查、落盘全部基于同一份 raw(否则「按读 A 授权、落盘读 B」,
    # 两者可以不是同一份字节——第 1 轮 codex 复核指出过这个缝)。
    raw = source.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    ledger = approval_ledger.load_approvals(approvals_file)
    if sha not in ledger:
        raise CollectError(
            f"{source.name} 当前内容没有人工批准记录,拒绝收菜"
            f"(先在本机终端里批准:python scripts/research_api.py review --strategy {source.name})")

    # 静态筛查复检:批准时筛过一遍,收菜时再筛一遍(批准与收菜之间文件可能被换过,
    # 而账本命中只保证「这份字节被批准过」,不保证「这份字节符合沙盒规则」)
    findings = const.screen_source(raw.decode("utf-8", errors="replace"))
    if findings:
        raise CollectError("静态筛查未通过,拒绝收菜:" + "\n  " + "\n  ".join(findings))

    say = write or (lambda text: print(text, file=sys.stderr))

    # ② 人的门槛:交互式终端 + 完整 64 位哈希(与批准同一口径)
    if not sys.stdin.isatty():
        raise CollectError("collect 必须在交互式终端里执行(当前 stdin 不是终端);"
                           "自动化调用不得把代码收进运行时认的目录。")
    say(f"文件: {source.name}   大小: {len(raw)} 字节   sha256: {sha}")
    say("⚠ 这行 sha256 只用在本机终端里核对,**不要抄进聊天窗口**(抄出去就等于把收菜权交出去)。")
    # **把源码打出来**:收菜是「这段代码从此有权被运行时执行」的最后一道人工关口。
    # 批准那一步的形式是「粘贴哈希」,不等于「读懂」;此处的源码是让人真正看一眼的机会
    # (第 1 轮 codex 复核指出:批准升格成执行权,批准环节的强度却没同步升格)。
    say("-" * 60)
    for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
        say(f"{number:4d} | {line}")
    say("-" * 60)
    say("要收进 approved/ 的话,把上面的 sha256 完整粘贴回来: ")
    try:
        answer = (input_fn or input)("")
    except (EOFError, OSError):
        answer = ""
    if answer.strip().lower() != sha:
        raise CollectError("哈希不匹配,未收菜。")

    # ③ 人确认之后重算一次:防「批准后、收菜前」文件被换掉(TOCTOU)
    current = approval_ledger.file_sha256(source)
    if current != sha:
        raise CollectError("文件在确认期间被改动,未收菜;请重新运行 collect")

    # ④ 原子写入 + 复算:复制必须逐字节一致
    target = approved_dir / source.name
    already = target.exists() and approval_ledger.file_sha256(target) == sha
    if target.exists() and not already and not replace:
        # 目标位置已有**另一份**已批准的字节:换掉它等于换掉运行时正在认的策略,
        # 那是另一件需要显式授权的事(而且运行中的进程可能正持有它)。
        raise CollectError(
            f"{target.name} 已在批准目录里且内容不同,拒绝覆盖"
            f"(要换版本请显式加 --replace;换版本时也要同时换运行时的 strategy_name,"
            f"否则旧版本的持久化状态会被新版本继承)")
    previous = None
    if target.exists() and not already and replace:
        previous = target.with_name(f"{target.name}.prev")
        _atomic_write(previous, target.read_bytes())
    _atomic_write(target, raw)
    written = approval_ledger.file_sha256(target)
    if written != sha:
        _remove_file(target)
        raise CollectError(f"写入后校验不一致,已删除目标文件(未收菜): {source.name}",
                           code=EXIT_FAILED)
    say(f"已收菜: {target.name}(绑定到上面的 sha256;代码改动后需要重新批准并重新收菜)")
    if previous is not None:
        say(f"旧版本已保留为: {previous.name}")
    _mark_collected(paths["collected_file"], sha, source.name)
    return {"collected": True, "already": already, "name": source.name,
            "approved_dir": approved_dir.name, "replaced": previous is not None}


def _mark_collected(collected_file: Path, sha: str, name: str) -> None:
    """写「已收菜」记录:这段字节**经过了收菜那道关口**。

    运行时要求这条记录存在——人工 `cp` 把文件拷进 `approved/` 就绕不过收菜
    (收菜时会把源码打给人看、并要求粘贴完整哈希)。
    """
    path = Path(collected_file)
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}                     # 读不出来就当空,下面整份重写
    data[sha] = {"name": name, "collected_at": datetime.now().isoformat(timespec="seconds")}
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))


def list_job(write=None) -> dict:
    """列出 `approved/` 里的文件与它们的账本状态。

    stdout 只给名字与状态位;**哈希只走 stderr**(人读通道)——
    stdout 是模型可见通道的候选,哈希不能从那里出去(模型拿到哈希就能替人念哈希)。
    """
    cfg, paths = settings()
    approved_dir = paths["approved_dir"]
    approvals_file = paths["approvals_file"]
    say = write or (lambda text: print(text, file=sys.stderr))
    entries: list[dict] = []
    if approved_dir.is_dir():
        ledger = approval_ledger.load_approvals(approvals_file)
        for path in sorted(approved_dir.glob("*.py")):
            sha = approval_ledger.file_sha256(path)
            in_ledger = sha in ledger
            entries.append({"name": path.name, "size": path.stat().st_size,
                            "approved": in_ledger})
            say(f"{path.name}   {path.stat().st_size} 字节   sha256: {sha}   "
                f"账本命中: {'是' if in_ledger else '否'}")
    return {"approved_dir": approved_dir.name, "entries": entries}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="收菜:把已被人批准的候选策略收进 approved/(运行时唯一认的目录)")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="把一个已批准的候选收进 approved/")
    collect.add_argument("--strategy", required=True, help="候选文件名(不含路径)")
    collect.add_argument("--replace", action="store_true",
                         help="允许覆盖批准目录里已存在的**另一版本**(旧版本保留为 <名>.prev)")
    sub.add_parser("list", help="列出 approved/ 里的文件与账本状态")
    args = parser.parse_args(argv)

    try:
        if args.command == "collect":
            emit(collect_job(args.strategy, replace=args.replace))
        else:
            emit(list_job())
    except CollectError as exc:
        print(("拒绝: " if exc.code == EXIT_REFUSED else "失败: ") + exc.message, file=sys.stderr)
        return exc.code
    except rs.SandboxError as exc:
        print("拒绝: " + str(exc), file=sys.stderr)
        return EXIT_REFUSED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
