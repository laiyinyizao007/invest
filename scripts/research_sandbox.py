"""研究沙盒底座(纯逻辑,可单测):路径限制、静态筛查、子进程环境白名单、窗口限制、往返盈亏、结果存取。

威胁模型必须说清楚:这里的措施是**纵深防御,不是沙盒**。
- 策略代码在本机以当前用户权限运行,理论上能读任何本机文件;静态筛查拦得住明显的越权写法,拦不住刻意绕过(反射、字节码等)。
- 因此 4a「人工驱动」阶段的前提是:**策略代码在运行前由人读过**(CLI 要求 --confirm-reviewed,并把代码 sha256 写入结果)。
- 让 LLM 的产出在无人读过的情况下运行,需要容器级隔离(无网络、仅只读挂载数据、无 .env),那是阶段四 4b 的前置,不在此实现。
设计依据:docs/adr/20261110-bisheng-ai-researcher.md、docs/adr/20261112-research-sandbox-groundwork.md。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from risk import constitution as const

PENDING_DIR = "pending_strategies"
SEGMENTS = ("train", "valid")        # 测试段不可经由沙盒到达
KEYS = ("CADJ", "MAADJ")             # 目前只支持数据库里的日线复权品种

FORBIDDEN_MODULES = {
    "os", "sys", "subprocess", "socket", "ctypes", "shutil", "pathlib", "importlib", "pickle", "marshal",
    "requests", "urllib", "http", "ftplib", "smtplib", "asyncio", "multiprocessing", "threading", "signal",
    "builtins", "inspect", "gc", "tempfile", "glob", "io",
    "operator",     # operator.attrgetter('__dict__') 这类反射入口:属性名可以是字符串,绕开属性名检查
    # 风控与框架包本身:候选拿到 `risk.risk_guard` 就等于拿到风控函数的**定义**,
    # 改写它们(`rg.risk_per_trade = lambda ...: 0.0`)可以架空单笔预算,而静态层按
    # "属性名"判断、只看得到 `rg.risk_per_trade` 这个**结果**,看不到它是谁。
    # 第 9 轮 codex 实测:三行 monkey-patch 让四层防线全部放行。
    # 候选没有任何正当理由 import 风控模块——风控由基类固定,候选只写信号逻辑。
    # 注意**不能**把 `strategies` 一并禁掉:候选本来就要
    # `from strategies.trend_atr_15m_strategy import TrendAtr15mStrategy` 继承基类。
    "risk",
}
# 这些名字**任何引用**都拦,不只直接调用:`s = setattr` 之后 `s(self, "risk_budget", 1e9)` 的调用点是裸名字,
# AST 看不见被调的是谁。只拦调用等于给别名留了门。
FORBIDDEN_CALLS = {"exec", "eval", "compile", "open", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
                   "type"}      # type(self).__base__ 之类的反射入口
FORBIDDEN_ATTRS = {"__bases__", "__subclasses__", "__globals__", "__builtins__", "__dict__", "__code__", "__mro__", "__class__"}

# 这些名字以**字符串**形式出现时同样拒绝:它们是"按键名取内建"的钥匙——
# `__builtins__['setattr'](self, 'risk_budget', 1e12)` 能改写任意实例属性,
# 而调用点完全看不出被调的是谁(第 7 轮实测:静态层与沙盒层当时都返回空列表,
# 底线由 300 变成 1e12,within_budget 随之放行)。
# 名单只收两类:dunder 形状的名字(执行入口一定是这类),
# 以及"按名字改写/反射"的内建名(getattr/setattr/eval/globals 之类)。
# **不含** `open`/`type`:交易数据里 "status": "open" 这种字段名很常见,
# 按字符串一律拦会把正常候选整片拒掉——误报面远大于收益。
FORBIDDEN_STRINGS = FORBIDDEN_ATTRS | {
    "__import__", "__getattribute__", "__setattr__", "__delattr__",
    "setattr", "delattr", "getattr", "eval", "exec", "compile", "globals", "locals", "vars", "input",
}

ENV_ALLOW = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA",
             "LOCALAPPDATA", "PROGRAMDATA", "COMSPEC", "PATHEXT", "LANG", "LC_ALL", "PYTHONIOENCODING", "TZ", "VIRTUAL_ENV",
             "INVEST_C15_DATA", "RESEARCH_SANDBOX_CONFIG")      # 数据路径与配置路径(均不含凭据)

# 执行候选时**不给**的内建:属性改写、反射、动态执行、文件访问、类型系统入口。
#
# 为什么是"不给"而不是"再禁一批名字"(第 7 轮复核实测):
# 静态层能封住**已知的写法**,封不住**能力**——
#   e.__traceback__.tb_frame.f_builtins.setattr(self, 'risk_budget', 1e12)
# 这条路径上的每个名字(回溯、帧、内建字典)都不在禁用名单里,静态层与沙盒层都返回空列表;
# 而 `unittest.mock.patch.object(self, 'risk_budget', 1e12)` 只要能 import 就能改写实例属性。
# 与其继续枚举反射路径(枚举不完,而且每加一批都要配测试),不如把能力本身收走:
# 候选执行时的内建字典里**没有 setattr**,那么无论它怎么拿到内建字典都没用——
# 连 `frame.f_builtins` 也只会拿到这份受限字典。
#
# 代价:候选用到这些名字会得到 NameError(而不是静默放行),由人在批准前看出来。
UNSAFE_BUILTINS = frozenset({
    "setattr", "delattr", "getattr", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "breakpoint", "input",
    "type", "object",                       # 类型系统入口:__mro__/__subclasses__ 是万能钥匙
    "help", "exit", "quit", "license", "credits",
})

# 这些模块能改写任意对象的属性(`unittest.mock.patch.object`)或拿到内建本身,同样禁掉。
EXTRA_FORBIDDEN_MODULES = frozenset({"unittest", "unittest.mock", "mock"})


def safe_builtins(forbidden_modules: Sequence[str] = ()) -> dict:
    """候选执行时用的**最小内建集合**:去掉 `UNSAFE_BUILTINS`,并把 `__import__` 换成受限版。

    受限导入器只放行不在禁用名单里的模块——候选必须能 import(模板与策略基类就是这么来的),
    但 `import builtins` / `import unittest.mock` 这类"拿到内建或改写属性能力"的入口必须关上。
    """
    import builtins as _builtins

    blocked = set(FORBIDDEN_MODULES) | set(EXTRA_FORBIDDEN_MODULES) | set(forbidden_modules)
    real_import = _builtins.__import__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root in blocked:
            raise ImportError(f"禁止导入 {name}")
        return real_import(name, globals, locals, fromlist, level)

    safe = {k: v for k, v in _builtins.__dict__.items() if k not in UNSAFE_BUILTINS}
    safe["__import__"] = restricted_import
    return safe


class SandboxError(ValueError):
    pass


def resolve_pending(arg: str, root: Path, pending_dir: str = PENDING_DIR) -> Path:
    """只认 `<root>/<pending_dir>/` 下的 .py 文件(pending_dir 来自配置,默认 pending_strategies);裸文件名按该目录解析。"""
    pending = (root / pending_dir).resolve()
    candidate = Path(arg)
    if not candidate.is_absolute() and candidate.parts[:1] != (pending_dir,):
        candidate = Path(pending_dir) / candidate
    path = candidate if candidate.is_absolute() else root / candidate
    resolved = path.resolve()                      # 解开 .. 与符号链接后再判断
    if pending not in resolved.parents:
        raise SandboxError(f"策略文件必须位于 {pending_dir}/ 内: {arg!r}")
    if resolved.suffix != ".py":
        raise SandboxError("策略文件必须是 .py")
    if not resolved.is_file():
        raise SandboxError(f"策略文件不存在: {arg!r}")
    return resolved


def screen_source(source: str, extra_modules: Sequence[str] = (), extra_calls: Sequence[str] = ()) -> list[str]:
    """静态筛查:返回发现的可疑构造(中文说明);空列表表示未发现。**不是安全保证。**

    extra_* 来自配置,只能在内置禁用清单之上追加,不能删减。"""
    forbidden_modules = FORBIDDEN_MODULES | set(extra_modules)
    forbidden_calls = FORBIDDEN_CALLS | set(extra_calls)
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [f"语法错误: {exc.msg} (第 {exc.lineno} 行)"]
    findings: list[str] = []
    try:
        constitution = const.load()
    except const.ConstitutionError as exc:
        return [f"不可变宪法清单不可用(fail-closed): {exc}"]      # 清单坏了就不筛查,一律拒绝
    findings.extend(const.screen_source(source, constitution))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            if name.split(".")[0] in forbidden_modules:
                findings.append(f"第 {node.lineno} 行: 禁止导入 {name}")
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in forbidden_calls:
                # 拦**引用**而不是拦调用:`s = setattr` 之后 `s(self, ...)` 的调用点看不出被调的是谁。
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.id}(可绕过属性改写与反射检查)")
            elif node.id in FORBIDDEN_ATTRS:
                # 受禁属性名同样要拦**裸名**:`__builtins__` 是 exec 注入到 globals 里的字典,
                # 裸名就能拿到(`f = __builtins__['__import__']`),只拦 `x.__builtins__` 是不够的。
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.id}(可绕过反射检查)")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            findings.append(f"第 {node.lineno} 行: 禁止访问 {node.attr}")
        # 属性名当字符串用:operator.attrgetter('__dict__')(self) 能绕开"禁止访问 __dict__"的
        # 属性写法(调用点看不出访问的是什么)。这些名字作为字符串出现时一律拒绝。
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in FORBIDDEN_STRINGS:
            findings.append(f"第 {node.lineno} 行: 禁止引用 {node.value} 字符串")
    return findings


def sandbox_env(base: Mapping[str, str]) -> dict[str, str]:
    """子进程环境白名单:交易凭据(CTP_*)、Webhook、LIVE_* 与任何未列名的变量一律不传。"""
    allowed = {k.upper() for k in ENV_ALLOW}
    return {k: v for k, v in base.items() if k.upper() in allowed}


def segment_window(key: str, segment: str) -> tuple[datetime, datetime]:
    if segment == "test":
        raise SandboxError("测试段不可经由研究沙盒访问(只看一次的纪律由人工在 optimize*.py 里执行并登记)")
    if segment not in SEGMENTS:
        raise SandboxError(f"未知数据段 {segment!r},可用: {SEGMENTS}")
    if key not in KEYS:
        raise SandboxError(f"未知品种 {key!r},可用: {KEYS}")
    import optimize
    windows = optimize.windows_for(key)
    start, end = windows[segment]
    assert end < windows["test"][0], "沙盒窗口不得触及测试段"      # 防止常量被改动后悄悄越界
    return start, end


def round_trip_pnls(trades: Sequence, size: float) -> list[float]:
    """把开/平成交配对成逐笔往返盈亏(元,不含成本)。按手数 FIFO 在单方向持仓上配对;未平仓的开仓不计。"""
    pnls: list[float] = []
    open_lots: list[list[float]] = []      # [signed_direction, price, remaining_volume]
    for t in trades:
        direction = 1 if t.direction.name == "LONG" else -1       # 用枚举名:value 的文案随 vnpy 版本/语言变化
        if t.offset.name == "OPEN":
            open_lots.append([direction, float(t.price), float(t.volume)])
            continue
        remaining = float(t.volume)
        pnl = 0.0
        while remaining > 0 and open_lots:
            lot = open_lots[0]
            take = min(remaining, lot[2])
            pnl += lot[0] * (float(t.price) - lot[1]) * take * size
            lot[2] -= take
            remaining -= take
            if lot[2] <= 0:
                open_lots.pop(0)
        pnls.append(pnl)
    return pnls


# ---------- 人工批准:绑定到代码内容的哈希 ----------
# 批准动作只能由本机 CLI(research_api.py approve)触发;外层(毕昇)没有任何端点能写入批准记录。
# 批准的是内容而不是文件名:代码一改哈希就变,批准自动失效。

def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_approvals(approvals_file: Path) -> dict:
    if not Path(approvals_file).exists():
        return {}
    try:
        data = json.loads(Path(approvals_file).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxError(f"批准记录无法读取,拒绝继续(fail-closed): {exc}") from exc
    if not isinstance(data, dict):
        raise SandboxError("批准记录格式错误(应为对象)")
    return data


def is_approved(path: Path, approvals_file: Path) -> bool:
    return file_sha256(path) in _load_approvals(approvals_file)


def is_approved_sha(sha256: str, approvals_file: Path) -> bool:
    """按**哈希**查审批记录(不再读一次文件)。

    隐藏子入口用它:子进程拿到的是父进程算好的哈希,而 `import_candidate` 会再读一次文件
    并校验"当前内容 == 这个哈希"——所以被执行的字节一定是被批准的那一份,
    不必为了查审批再读一遍文件(两处读之间文件若被改,查的就是另一个版本的内容)。"""
    return sha256 in _load_approvals(approvals_file)


def record_approval(path: Path, approvals_file: Path, now: datetime | None = None) -> str:
    approvals_file = Path(approvals_file)
    data = _load_approvals(approvals_file)
    sha = file_sha256(path)
    data.setdefault(sha, {"name": Path(path).name, "at": (now or datetime.now()).isoformat(timespec="seconds")})
    approvals_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = approvals_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, approvals_file)
    return sha


_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def save_result(result: dict, directory: Path, now: datetime | None = None) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    run_id = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"{run_id}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({**result, "run_id": run_id}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return run_id


def load_result(run_id: str, directory: Path) -> dict:
    if not _ID.match(run_id or ""):
        raise SandboxError(f"非法的 run_id: {run_id!r}")
    path = directory / f"{run_id}.json"
    if not path.is_file():
        raise SandboxError(f"没有这条结果: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_results(directory: Path) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json")) if directory.is_dir() else []
