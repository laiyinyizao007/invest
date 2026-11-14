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
import subprocess
import time
import weakref
from datetime import datetime
from pathlib import Path
from types import ModuleType
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
    # **执行原语**(第 23 轮探针实测):这些模块能把"执行任意代码"这项能力送回给候选,
    # 而且**不经过 `__import__`**——上面那套"按路径裁决导入"(RepoImportGuard)
    # 只在候选调用 import 的那条路上生效,对它们**完全无效**。
    # 对照实验(同一个"运行期间写出来的模块",两条路):
    #   `import 它`        -> 路径规则拒绝:"该文件在候选开始运行之后才出现";
    #   `runpy.run_path(它)` -> 直接执行,辅助模块在**真实内建**下跑起来(标记文件被写出)。
    # 候选本来就不需要这些模块:仓库里的策略模板只 import
    # random/dataclasses/datetime/vnpy/strategies,禁掉它们没有误伤面。
    # 但名单式封堵**不收敛**——能力会从别的模块再导出(实测 numpy.ctypeslib 再导出 ctypes),
    # 见 ADR 残余风险。
    "runpy", "code", "pkgutil", "zipimport", "pty", "platform", "webbrowser",
    # **执行原语·第二批**(第 24 轮 codex 实测):这些模块**自己**就能执行任意代码,
    # 不需要"绕开 __import__",连模块本身都是执行入口:
    #   `timeit.timeit("open('x','w')")`  -> 直接把字符串当代码跑(真实内建);
    #   `doctest.run_docstring_examples` -> 把文档字符串里的例子跑起来;
    #   `pdb.run(...)` / `bdb` / `trace.run(...)` / `profile.run("...")` / `cProfile.run("...")`
    #   -> 同样是"把一段代码跑起来";
    #   `shelve` 底层是 pickle(反序列化即执行);`distutils.spawn` / `setuptools` / `pip`
    #   -> 能起子进程跑命令;`venv` / `ensurepip` -> 能装东西;
    #   `concurrent.futures.ProcessPoolExecutor` -> 在**新解释器**里跑候选代码(那里是真实内建);
    #   `socketserver` / `telnetlib` / `xmlrpc` -> 自己开监听端口。
    # 候选没有正当理由用它们,禁掉没有误伤面。
    "timeit", "doctest", "pdb", "bdb", "trace", "profile", "cProfile", "shelve",
    "distutils", "setuptools", "pip", "venv", "ensurepip", "concurrent",
    "socketserver", "telnetlib", "xmlrpc",
}

# ---------------------------------------------------------------------------
# 候选拿到的**模块视图**:候选 import 到的不是模块本身,而是一份视图。
#
# 为什么要有这一层(第 24 轮 codex 实测,不是推理):
#   `import logging` 之后 `logging.sys.modules["os"].system("...")` = 任意命令执行,
#   而名字名单、受限 `__import__`、按路径的裁决、受限内建**四层全部放行**。
# 根因很直白:**标准库里几十个模块把真实 sys/os 挂在自己属性上**
# (实测:logging/timeit/uuid/platform/typing/dataclasses/enum/statistics/collections/...),
# 而"继续往名单里加名字"这条路,第 12-14 轮已经验证过**不收敛**:
# 堵死 `logging`,候选改用 `typing.sys`;堵死 `typing`,还有 `dataclasses.sys`。
#
# 所以这里改成**按对象**判定,不按名字:属性值是危险模块 -> 不给;
# 属性值是会把 ctypes/subprocess 递出来的模块 -> 那个模块整个不给
# (实测 `numpy.ctypeslib` 把 `ctypes` 整个递出来,`numpy.f2py` 把 `subprocess` 递出来)。
# 模块叫什么名字不影响判定——只看它递出来的是什么。
#
# 这一层**不能**替代人工审批与容器隔离(它仍然挡不住"候选自己写的类里带着真实内建"
# 这类进程内残余),它只是把"一行 import 就拿到任意命令执行"这类最便宜的攻击收掉。
DANGEROUS_MODULE_NAMES = {
    # 进程/系统
    "os", "sys", "subprocess", "posix", "nt", "signal", "atexit",
    # 文件系统
    "shutil", "pathlib", "tempfile", "glob", "io", "zipfile", "gzip", "tarfile",
    # 反射与执行
    "builtins", "inspect", "gc", "operator", "importlib", "_importlib", "_imp",
    "runpy", "code", "pkgutil", "zipimport", "marshal", "pickle", "shelve",
    "timeit", "doctest", "pdb", "bdb", "trace", "profile", "cProfile",
    "multiprocessing", "concurrent", "threading", "asyncio",
    # 原生代码
    "ctypes",
    # 网络
    "socket", "socketserver", "telnetlib", "xmlrpc", "http", "httpx",
    "urllib", "urllib3", "requests", "ftplib", "smtplib",
    # 其它能力入口
    "unittest", "mock", "pty", "platform", "webbrowser", "distutils",
    "setuptools", "pip", "venv", "ensurepip",
    # **写能力与外传通道**(第 25 轮 codex 实测:`codecs.open` 仍能写文件,
    # 而静态层只拦**字符串字面量**里的 "open",`codecs.open` 是 ast.Attribute,
    # 于是"从内建里拿掉 open"根本没封住写能力;`ssl`/`httpx`/`urllib3` 则是外传通道):
    "codecs", "mmap", "sqlite3", "ssl", "bz2", "lzma",
}

# 出现这两个 = 那个模块能把"执行任意代码 / 调原生代码"直接递出来,整块不给。
CAPABILITY_MODULE_NAMES = frozenset({"ctypes", "subprocess"})

# 视图上唯一透传的下划线名字(其余下划线属性一律不给,包括 `__class__`/`__dict__`)。
# `__version__` 也在内(第 25 轮 codex 的 Low 11):只透传四个名字会让
# "记录库版本的候选"直接崩掉(np.__version__ -> AttributeError)。
_VIEW_FORWARDED = frozenset({"__all__", "__name__", "__doc__", "__package__", "__version__"})


# ---------------------------------------------------------------------------
# 受信目录的完整性基准 = **仓库提交里的那份**(第 25 轮 codex 的 Critical 1)
#
# 为什么必须是提交而不是"每次导入重拍的快照":
# 快照是**每次导入重拍**的,它挡得住"同一次运行里现写现导",挡不住
# "上一轮写、这一轮导":候选用 `codecs.open('strategies/base.py', 'w')` 改写了一个
# **受信**模块;第 1 轮里 import 它会被快照正确拒绝(文件是运行期间写出来的),
# 但第 2 轮重拍的快照**已经把被篡改的文件收进去了** → 放行 → 那个模块在
# **导入机制**里执行(拿到真实内建)→ 任意命令执行(第 25 轮 codex 端到端复现,
# 标记文件确实被写了出来)。
#
# 基准只有一个够硬:**仓库提交里的那份**。候选拿不到 subprocess,
# 伪造一个内容哈希等于伪造 SHA-1 原像——不现实。
#
# 而"只读部署"里根本不需要它:容器里 /app 是**只读**的(rootfs read_only),
# 候选**写不了**那个目录,篡改压根不可能发生(写能力不存在,不是"被挡住了")。
# 所以规则按"那个目录能不能写"分岔:
#   能写 -> 必须有提交基准,取不到基准就**拒绝**(查不了等于没查,fail-closed);
#   不能写 -> 不需要基准(写不了就没有篡改)。
def _git_toplevel(repo_root: Path) -> str | None:
    """这个目录所在的 git 仓库顶层(取不到就不是 git 仓库)。"""
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "--show-toplevel"],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.decode("utf-8", "replace").strip()
    return top or None


def git_committed_paths(repo_root: Path, rel_dirs) -> tuple[str, set] | None:
    """(仓库顶层目录, HEAD 里这些目录下的**已提交**文件)——路径相对顶层目录。

    返回 None = 这台机器上取不到基准(不是 git 仓库 / 没装 git / HEAD 不存在)——
    调用方按"查不了等于没查"处理(fail-closed),**不**当作"没有基准、随便放行"。
    """
    top = _git_toplevel(repo_root)
    if top is None:
        return None
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "ls-tree", "-r", "--name-only",
                               "HEAD", "--", *rel_dirs],
                              capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    paths = {line.strip().replace("\\", "/")
             for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()}
    return top, paths


def git_dirty_paths(repo_root: Path, rel_dirs) -> set | None:
    """这些目录里**与 HEAD 不一致**的文件(相对仓库顶层目录)。

    用 git 自己的比较口径,而不是我们自己算哈希:git 会按 `core.autocrlf`
    与 `.gitattributes` 归一化换行后再比。自己按字节算哈希会把
    "工作区 CRLF / 提交里 LF"这种**内容其实一致**的情况误判成"被改写过"
    (Windows 上仓库默认就长这样)——那会让受信目录里的正常模块一律被拒。

    None = 查不到(不是 git 仓库 / git 报错)→ 调用方 fail-closed。
    """
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "status", "--porcelain", "-z",
                               "--no-renames", "-uall", "--", *rel_dirs],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: set = set()
    for record in proc.stdout.decode("utf-8", "replace").split("\0"):
        # porcelain -z 的记录是 "XY <路径>";重命名已用 --no-renames 关掉,不会多出第二条。
        if len(record) < 4:
            continue
        out.add(record[3:].replace("\\", "/"))
    return out


def _module_name(value) -> str:
    """模块对象的名字(按 `__name__` 判定,不靠身份——身份会被别名骗过)。"""
    if not isinstance(value, ModuleType):
        return ""
    return getattr(value, "__name__", "") or ""


def _is_dangerous_module(value) -> bool:
    """这个模块对象是不是危险模块(`DANGEROUS_MODULE_NAMES` 里那一份)。"""
    return _module_name(value) in DANGEROUS_MODULE_NAMES


def _capability_source(value) -> str | None:
    """这个对象是不是"执行能力"的提供者;是的话返回来源模块名。

    两种形态都算:
    - 模块本身是 `ctypes` / `subprocess`;
    - 对象**定义在** `ctypes` / `subprocess` 里(类、函数都行)——
      只递出类不递出模块一样能执行命令(`subprocess.Popen`、`ctypes.CDLL`)。

    判据用 `__module__`(**对象是从哪儿定义的**),不是"这个名字像不像"——
    所以"换个名字再递一次"骗不过它,这一条是收敛的(第 23 轮"名单式封堵不收敛"的正解)。
    """
    if isinstance(value, ModuleType):
        name = _module_name(value)
        return name if name in CAPABILITY_MODULE_NAMES else None
    try:
        origin = getattr(value, "__module__", "") or ""
    except Exception:                                    # noqa: BLE001 —— 取不到来源 = 认不准 = 当成"能执行"
        # 第 25 轮 codex 的 Medium 9:原来这里返回 None(判成"无害")。
        # "取不到来源"不等于"无害"——认不准就按**有能力**处理(拒绝交付)。
        return "未知来源"
    return origin if origin in CAPABILITY_MODULE_NAMES else None


_LEAK_CACHE: "weakref.WeakKeyDictionary[ModuleType, tuple]" = weakref.WeakKeyDictionary()


def _capability_leak(value) -> str | None:
    """这个模块会不会把"执行任意代码 / 调原生代码"直接递出来。

    判据:它自己有没有一个属性是 `ctypes`/`subprocess` 的模块**或**它们定义出来的对象。
    实测:`numpy.ctypeslib` 递出 `ctypes`、`numpy.f2py` 递出 `subprocess`。

    **按模块缓存**(第 25 轮 codex 的 Medium 7):原来每次属性访问都要全量扫 `vars(module)`,
    实测视图取一个属性 14.7µs、候选热点循环慢 19 倍(20000 次 `np.random.normal()`:
    沙盒 0.327s vs 普通 0.017s)。研究侧是"多候选 × 多 bar × 多参数"的放大场景,
    这一项能吃掉整轮预算。缓存键是模块本身(弱引用,回收即消失),
    失效条件用 `len(vars(module))`:模块成员变了,长度多半也变了;真变了长度却不变的情况
    (换了一个成员但数量不变)会用到旧判定——登记为已知残余。
    """
    if not isinstance(value, ModuleType):
        return None
    try:
        size = len(vars(value))
    except TypeError:                                    # 理论上模块都有 __dict__
        return None
    cached = _LEAK_CACHE.get(value)
    if cached is not None and cached[0] == size:
        return cached[1]
    try:
        members = list(vars(value).values())
    except TypeError:
        return None
    found = None
    for item in members:
        source = _capability_source(item)
        if source:
            found = source
            break
    _LEAK_CACHE[value] = (size, found)
    return found


class ModuleView:
    """候选看到的模块**视图**:危险模块不递出去,会把危险模块递出来的模块也不给。

    为什么要有这一层(第 24 轮 codex 实测,不是推理出来的):
    `import logging` 之后 `logging.sys.modules["os"].system("...")` = 任意命令执行,
    而名字名单、受限 `__import__`、按路径的裁决、受限内建**四层全部放行**——
    因为它们全都只看"名字",而这一条走的是"别的模块把危险模块当属性递出来"。

    视图的规矩:
    - 属性值是危险模块 -> 不给(`AttributeError`,并写明是哪一条规则);
    - 属性值是会递出 ctypes/subprocess 的模块 -> 那个模块整个不给;
    - 属性值是别的模块 -> 给一份**视图**(递归生效),不给模块本身;
    - 下划线属性(`__class__`/`__dict__`/`_view_module_ref`…)一律不给,
      否则候选能从视图里摸回真模块,这一层就白做。

    这不是边界:候选自己写的类、第三方模块的帧里仍然是真实内建(见 ADR 残余风险)。
    它收掉的是"一行 import 就拿到任意命令执行"这类最便宜的攻击。
    """

    __slots__ = ("_view_module_ref",)

    def __init__(self, module):
        # **弱引用**持有模块(第 24 轮自查修):视图缓存是 `WeakKeyDictionary`,
        # 可视图若**强引用**模块,value→key 就把弱键托住了,条目永不回收,
        # 每个导入过的模块都会永久驻留(无界增长)。改成弱引用后,
        # 模块本身由 `sys.modules` 持有,视图照样能用,而缓存条目可以被回收。
        object.__setattr__(self, "_view_module_ref", weakref.ref(module))

    def __getattribute__(self, name):
        if name.startswith("_") and name not in _VIEW_FORWARDED:
            raise AttributeError(name)
        module = object.__getattribute__(self, "_view_module_ref")()
        if module is None:
            raise AttributeError("模块视图对应的模块已经被回收")
        if name in _VIEW_FORWARDED:
            return getattr(module, name)
        return _view_value(module, name, getattr(module, name))

    def __dir__(self):
        module = object.__getattribute__(self, "_view_module_ref")()
        if module is None:
            return []
        return [n for n in dir(module) if not n.startswith("_")]


def _view_value(module, name, value):
    """视图的属性取值:危险的一律拦下,模块一律换成视图。"""
    if isinstance(value, ModuleType):
        if _is_dangerous_module(value):
            raise AttributeError(
                f"禁止访问 {module.__name__}.{name}:模块视图不把危险模块"
                f" {value.__name__} 交给候选(规则:模块视图)")
        leak = _capability_leak(value)
        if leak:
            raise AttributeError(
                f"禁止访问 {module.__name__}.{name}:它把 {leak} 递出来,"
                f"等于把执行能力递给候选(规则:能力泄漏)")
        return _view_for(value)
    # 非模块的值:如果它**定义在** ctypes/subprocess 里(`subprocess.Popen`、
    # `ctypes.CDLL`),递出去一样能执行命令——按对象来源判,不看名字。
    source = _capability_source(value)
    if source:
        raise AttributeError(
            f"禁止访问 {module.__name__}.{name}:它来自 {source},"
            f"递出去等于把执行能力递给候选(规则:能力泄漏)")
    return value


_VIEW_CACHE: "weakref.WeakKeyDictionary[ModuleType, ModuleView]" = weakref.WeakKeyDictionary()


def _view_for(module):
    """同一个模块永远给同一份视图(这样 `np is np` 之类还是成立的)。"""
    view = _VIEW_CACHE.get(module)
    if view is None:
        view = ModuleView(module)
        _VIEW_CACHE[module] = view
    return view
# 这些名字**任何引用**都拦,不只直接调用:`s = setattr` 之后 `s(self, "risk_budget", 1e9)` 的调用点是裸名字,
# AST 看不见被调的是谁。只拦调用等于给别名留了门。
FORBIDDEN_CALLS = {"exec", "eval", "compile", "open", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars", "input",
                   "type"}      # type(self).__base__ 之类的反射入口
FORBIDDEN_ATTRS = {"__bases__", "__subclasses__", "__globals__", "__builtins__", "__dict__", "__code__", "__mro__", "__class__",
                   # 函数对象的**状态**(第 10 轮 codex 实测的 Critical):
                   # `within_budget.__defaults__ = (0.0,)` 把 volume 默认值改成 0,
                   # 单笔风险金额恒为 0,300 元预算形同虚设——改的不是名字,是对象的状态。
                   "__defaults__", "__kwdefaults__", "__annotations__", "__wrapped__",
                   # 类型判定入口:改掉 `__instancecheck__` 之后 isinstance 一律返回 False,
                   # 运行时第三层的家族判定直接失效。
                   "__instancecheck__", "__subclasscheck__", "__subclasshook__", "__prepare__",
                   # **走栈**能力(第 24 轮探针实测:第三方模块的帧里就有 `os`——
                   # numpy 自己的 traceback 里印着 `open(os.fspath(file), "rb")`。
                   # 我们只能保证"自己那一帧"的 globals 干净,管不了第三方帧;
                   # 所以这里禁掉的是"顺着异常链走栈"这个**能力本身**):
                   # 异常对象上的 traceback/context/cause,以及帧上的
                   # f_globals/f_locals/f_builtins/f_code/f_back/gi_frame/cr_frame/ag_frame。
                   "__traceback__", "__context__", "__cause__", "__suppress_context__",
                   "tb_frame", "tb_next", "tb_lasti", "tb_lineno",
                   "f_globals", "f_locals", "f_builtins", "f_code", "f_back", "f_lineno",
                   "gi_frame", "gi_code", "cr_frame", "cr_code", "ag_frame", "ag_code",
                   # 属性名筛查的绕法:拿到任意对象后用 `obj.__getattribute__(拼出来的名字)` 取属性。
                   # `getattr` 早已不提供,这里把 `__getattribute__` 也补上(字符串形式早就禁了)。
                   "__getattribute__",
                   # 闭包单元(第 24 轮实测):受限导入器把真导入器藏在闭包单元里,
                   # 而闭包单元在 f_locals 里看得见——`f.__closure__[0].cell_contents`
                   # 就是真导入器。这两个名字静态层一律不许引用。
                   "__closure__", "cell_contents"}

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
    # 危险模块的名字,**按键名**取法也要挡(第 24 轮实测的老洞就是这么走的:
    # 候选顺着异常链摸到某一帧的 f_globals,再 `g["os"]` 把 os 拿走)。
    # 这些名字作为**属性名**本来就 import 不到;这里补的是"字符串当钥匙"这条路。
    # 注意:短名(`os`/`sys`/`nt`/`posix`)另见 `FORBIDDEN_KEY_STRINGS`——
    # 它们与普通词(标签、列名、打印内容)长得太像,全局禁字符串会误伤合法写法。
    "subprocess", "ctypes", "real_import", "_os",
}

# 危险模块的**短名**只在"按键取"的语境里拒(第 24 轮自查修):
# 之前把这四个短名放进 `FORBIDDEN_STRINGS` 是**全局**禁字符串,
# 于是策略里只要出现一个恰好等于 "os" 的字符串字面量(状态名、列名、打印内容)就被拒——
# 拿一条本该封死的洞,换了一大片误伤面。
# 而那条洞(`g["os"]`)真正要封的是"**按键取**"这个动作,不是这些字符本身:
# 于是改成按语境拦(见 `screen_source`):下标键、字典取值方法的参数、关键字参数名。
FORBIDDEN_KEY_STRINGS = frozenset({"os", "sys", "nt", "posix"})

# 这些方法名是"按键取"的常见形态:`d.get("os")`、`d.pop("os")`、`d["os"] = ...`。
_KEY_ACCESSOR_NAMES = frozenset({
    "get", "pop", "setdefault", "__getitem__", "update", "append", "extend", "insert",
})

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
# **但这句话只对"候选自己那些帧"成立**(第 22 轮补记,Issue #26):
# 别人的帧——导入机制的帧、第三方库(如 numpy)的帧——里的 f_builtins 仍然是**真实内建**,
# 候选顺着异常链能摸到。所以这一层是纵深防御,**不是边界**:
# 真正的边界是人工审批 + 容器隔离。别把"受限内建"当成"进程里没有 setattr"。
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

# 按**全名**禁掉的子模块(根名字不禁,但这几个具体角落不禁不行):
# - `numpy.ctypeslib`:把 `ctypes` 整个再导出一遍(实测),于是候选能直接调原生代码;
# - `numpy.f2py`:**import 它就会跑 f2py 的主程序**(实测:import 即打印 f2py 帮助并可能直接结束进程);
# - `numpy.distutils`:能起子进程编译东西。
# 根名字"numpy"照常放行——候选正当需要 numpy;这里只堵这几个角落。
FORBIDDEN_MODULE_PATHS = frozenset({
    "numpy.ctypeslib", "numpy.f2py", "numpy.distutils",
})

# ---------------------------------------------------------------------------
# 仓库内允许被候选导入的目录(相对仓库根)。
#
# 为什么要按**路径**而不是按名字管导入(第 21 轮实测):
# 禁用名单是黑名单,而"候选写一个辅助模块再 import 它"这条逃逸**根本不经过名单**——
# 候选用不受限的第三方库(如 numpy.savetxt)把 .py 写进可写目录,
# 再 import 它。辅助模块是在 import 机制里执行的,拿到的是**真实内建**,
# 于是 os/sys/subprocess 全都有了:静态筛查(它只筛候选那份源码)与
# 受限内建(它只对候选那份源码生效)对它都不起作用。
#
# 按名字堵是不收敛的(第 12-14 轮已验证:堵一个名字就出现下一个,而且会误伤
# 候选本来就要用的 `from strategies.<模板> import <基类>`)。
# 所以规则改成:**仓库内的模块,只有落在允许目录里、且在候选开始运行之前就已存在的才可导入。**
ALLOWED_REPO_IMPORT_DIRS = ("strategies",)


def _spec_chain(name: str):
    """逐段解析一个模块名,返回 (最后一段的 spec, 解析时用的路径表)。

    **不执行**被检查的模块,也**不执行**它的父包——这一点是刻意的:
    用 `importlib.util.find_spec` 定位子模块会真的 import 父包(跑父包的代码),
    探测动作本身就变成了执行入口。这里用底层的 `PathFinder.find_spec`,
    它只在给定的路径表里搜文件,不 import 任何东西。
    """
    import importlib.machinery as _machinery

    parts = name.split(".")
    path = None
    for index, part in enumerate(parts):
        try:
            spec = _machinery.PathFinder.find_spec(part, path)
        except Exception:                                       # noqa: BLE001 - 探测失败按"查不到"处理
            return None, None
        if spec is None:
            return None, None
        if index == len(parts) - 1:
            return spec, path
        locations = spec.submodule_search_locations
        if locations is None:
            return None, None                                   # 不是包,更深的导入本来也不成立
        path = list(locations)
    return None, None


def module_origin(name: str) -> str | None:
    """解析一个模块的来源文件路径;解析不出来返回 None(命名空间包没有文件,也是 None)。"""
    spec, _ = _spec_chain(name)
    return spec.origin if spec is not None else None


def module_locations(name: str) -> list[str]:
    """一个**包**的名字空间位置(子模块可以从哪些目录里找);不是包或查不到返回空表。"""
    spec, _ = _spec_chain(name)
    if spec is None or not spec.submodule_search_locations:
        return []
    return list(spec.submodule_search_locations)


def snapshot_allowed_files(repo_root: Path, allowed_dirs: Sequence[str] = ALLOWED_REPO_IMPORT_DIRS) -> dict:
    """候选开始运行**之前**拍一张"允许目录里的文件"快照:路径 -> (mtime_ns, size, sha256)。

    这张快照是"文件必须早就存在、且此后没被改过"这条规则的依据。
    用 mtime 而不是"只记有哪些文件":候选把既有文件**改写**再导入,
    名字还在清单里,只有 mtime 变了才认得出来。

    第 24 轮 codex 实测补了一条:mtime **单独**不够——粗粒度文件系统(有些网络盘 1 秒一档)
    上"改写内容再把 mtime 还原"能骗过只比 mtime 的检查。所以这里连**大小与内容哈希**一起记:
    `strategies/` 只有几个文件,哈希成本可以忽略。
    """
    root = Path(repo_root).resolve()
    snapshot: dict[str, tuple] = {}
    for name in allowed_dirs:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            try:
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_mtime_ns, stat.st_size, digest)
    return snapshot


def _timestamp_granularity(value_ns: int) -> int:
    """从文件自身的时间戳反推文件系统的时间粒度(纳秒)。

    文件系统写时间戳时会把时间**向下取整**到自己的粒度(NTFS 是 100ns,有些网络盘是 1ms 甚至 1s)。
    判断"这个文件是不是候选运行期间写出来的"必须按同一粒度取整,否则会漏:
    候选在开始后 0.5ms 写文件,时间戳被向下取整到开始时刻**之前**,直接比大小就判不出来。
    """
    for step in (1_000_000_000, 1_000_000, 10_000, 100):
        if value_ns % step == 0:
            return step
    return 1


def _floor_to_granularity(value_ns: int, granularity: int) -> int:
    return value_ns - (value_ns % granularity) if granularity > 1 else value_ns


# 受信目录的提交基准缓存:键 = (仓库根, 允许目录),值 = (仓库顶层, 已提交文件集合) 或 None(取不到)。
_GIT_BASELINE_CACHE: dict = {}


class RepoImportGuard:
    """按**路径**裁决候选能不能导入某个模块。

    规则(按顺序):
    - 模块来源不是磁盘上的文件(标准库内建、命名空间包、找不到) -> 不由本规则裁决;
    - 在仓库根之下、但不在允许目录里 -> 拒绝(框架、风控、回测模块一律不许碰);
    - 在允许目录里 -> 还必须"候选开始运行之前就存在、且此后没被改过",否则拒绝;
    - 在仓库根**之外**(标准库、站点包)-> 也必须"候选开始运行之前就存在":
      否则候选往站点包里写一个辅助模块就能绕开仓库内的路径规则(站点包在宿主机上通常可写)。
    """

    def __init__(self, repo_root: Path, allowed_dirs: Sequence[str] = ALLOWED_REPO_IMPORT_DIRS,
                 snapshot: Mapping[str, int] | None = None, started_at_ns: int | None = None,
                 verify_trusted_against_git: bool = True):
        self.repo_root = Path(repo_root).resolve()
        self.allowed_dirs = tuple(allowed_dirs)
        self.snapshot = dict(snapshot or {})
        self.started_at_ns = started_at_ns if started_at_ns is not None else time.time_ns()
        # 受信目录要不要与**仓库提交**核对(第 25 轮 codex 的 Critical 1)。
        # 默认开(查不了等于没查,fail-closed);单元测试用临时目录模拟仓库,那里没有 git 基准,
        # 所以测试显式关掉这条(另有一条用真 git 夹具的用例专门盯它)。
        self.verify_trusted_against_git = verify_trusted_against_git

    def _git_baseline(self) -> tuple[str, set] | None:
        """受信目录的**提交基准**(进程内缓存:起一次 git 就够,不必每个候选重起一次)。

        None = 取不到基准(不是 git 仓库 / 没装 git / HEAD 不存在)→ 调用方 fail-closed。
        """
        key = (str(self.repo_root), self.allowed_dirs)
        if key not in _GIT_BASELINE_CACHE:
            _GIT_BASELINE_CACHE[key] = git_committed_paths(self.repo_root, list(self.allowed_dirs))
        return _GIT_BASELINE_CACHE[key]

    def _trusted_dir_writable(self, resolved: Path) -> bool:
        """受信目录**能不能被写**。

        写不了 -> 篡改压根不可能发生(容器里 /app 是只读的),不需要提交基准;
        能写   -> 必须用提交基准核对(宿主机上跑 CLI 的情形)。
        """
        for name in self.allowed_dirs:
            base = self.repo_root / name
            if base == resolved or base in resolved.parents:
                return os.access(base, os.W_OK)
        return False

    def _under_allowed_dir(self, resolved: Path) -> bool:
        for name in self.allowed_dirs:
            base = self.repo_root / name
            if base == resolved or base in resolved.parents:
                return True
        return False

    def check(self, name: str) -> str | None:
        """返回拒绝理由(None 表示放行)。"""
        origin = module_origin(name)
        if not origin:
            # 解析不到磁盘文件(命名空间包、zip 成员、动态模块……)。
            # 第 25 轮 codex 的 Medium 9:原来这里直接放行——"查不到来源"被当成了"没有来源问题"。
            # 但命名空间包**有**来源(它是一串目录),只是没有单文件;仓库内的命名空间包
            # (比如没有 __init__.py 的 pending_strategies/)正是候选写辅助模块的地方。
            # 判据:这个包的名字空间位置落在仓库内、且不在允许目录里 -> 拒。
            reason = self.check_package(name)
            if reason:
                return reason
            return None                                        # 仓库外的命名空间包(站点包等)照旧放行
        if origin in ("built-in", "frozen", "namespace"):
            return None                                        # 同上(内建/冻结/命名空间包:没有文件可查)
        try:
            resolved = Path(origin).resolve()
            stat = resolved.stat()
        except OSError:
            # **查不了等于没查(fail-closed)**。第 24 轮 codex 实测:zip 里的成员 origin 形如
            # "...\evil.zip\mod.py",stat 必然失败,而这一支原先直接放行——
            # 等于给"从 zip 里执行模块"开了一个后门,与本文件其它地方的 fail-closed 纪律相反。
            return (f"禁止导入 {name}:无法确认来源文件的状态"
                    f"(查不了等于没查,fail-closed)")
        if self.repo_root != resolved and self.repo_root not in resolved.parents:
            # 仓库之外(标准库/站点包):仍要求"候选开始运行之前就存在"。
            # 站点包在宿主机上通常可写,候选往里写一个辅助模块就能绕开仓库内的规则。
            reason = self._written_during_run(resolved)
            if reason:
                return f"禁止导入 {name}:{reason}"
            return None
        if not self._under_allowed_dir(resolved):
            return (f"禁止导入仓库内模块 {name}:它不在允许的目录 {list(self.allowed_dirs)} 内"
                    f"(候选不得导入框架、风控或回测模块)")
        key = str(resolved)
        known = self.snapshot.get(key)
        if known is None:
            return (f"禁止导入 {name}:该文件在候选开始运行之前并不存在"
                    f"(候选运行期间写出的文件一律不得被导入)")
        if self._changed(known, resolved, stat):
            return f"禁止导入 {name}:该文件在候选运行期间被改动过"
        # **跨轮次**(第 25 轮 codex 的 Critical 1):快照是每次导入重拍的,
        # 挡不住"上一轮写、这一轮导"(上一轮被改写的文件,这一轮的快照已经收进去了)。
        # 受信目录必须与**仓库提交**一致:要么这个文件根本不在提交里(候选写的),
        # 要么内容与提交不一致(候选改写的)——两种都拒绝导入。
        if self.verify_trusted_against_git and self._trusted_dir_writable(resolved):
            baseline = self._git_baseline()
            if baseline is None:
                return (f"禁止导入 {name}:无法验证受信目录的完整性"
                        f"(取不到仓库提交基准,查不了等于没查,fail-closed)")
            top, committed = baseline
            try:
                rel = os.path.relpath(str(resolved), top).replace("\\", "/")
            except ValueError:                        # 不在同一个盘上(理论上不可能)
                rel = self._rel(resolved).replace("\\", "/")
            if rel not in committed:
                return (f"禁止导入 {name}:该文件不在仓库提交里"
                        f"(候选运行期间写出的文件一律不得被导入)")
            dirty = git_dirty_paths(self.repo_root, list(self.allowed_dirs))
            if dirty is None:
                return (f"禁止导入 {name}:无法验证受信目录的完整性"
                        f"(查不到仓库状态,查不了等于没查,fail-closed)")
            if rel in dirty:
                return (f"禁止导入 {name}:该文件与仓库提交的内容不一致"
                        f"(候选运行期间被改动过)")
        return None

    @staticmethod
    def _changed(known, resolved: Path, stat) -> bool:
        """快照里的 (mtime_ns, size, sha256) 与现在对不上,就算"被改动过"。

        第 24 轮 codex 实测:只比 mtime 会被"改写内容再把 mtime 还原"骗过
        (粗粒度文件系统上同秒改写,mtime 根本不变)。所以三项一起比。
        """
        known_mtime, known_size, known_sha = known
        if stat.st_mtime_ns != known_mtime or stat.st_size != known_size:
            return True
        try:
            digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError:
            return True                                   # 读不出来 = 认不准 = 当被改过
        return digest != known_sha

    def _written_during_run(self, path: Path) -> str | None:
        """这个文件是不是候选运行期间才出现的(仓库之外那套口径)。

        比较前按文件系统粒度取整,否则会漏掉"刚写出来就被取整到开始时刻之前"的文件
        (文件系统写时间戳时向下取整:NTFS 100ns,有些网络盘 1ms 甚至 1s)。
        """
        try:
            stat = path.stat()
        except OSError:
            return "该文件的状态查不到(查不了等于没查,fail-closed)"
        granularity = _timestamp_granularity(stat.st_mtime_ns)
        if stat.st_mtime_ns >= _floor_to_granularity(self.started_at_ns, granularity):
            return (f"该文件在候选开始运行之后才出现"
                    f"(候选运行期间写出的文件一律不得被导入)")
        return None

    def _rel(self, path: Path) -> str:
        """给拒绝理由用的**相对**路径(不把本机绝对路径泄给候选)。"""
        try:
            return str(path.resolve().relative_to(self.repo_root))
        except (ValueError, OSError):
            return path.name

    def check_package(self, name: str) -> str | None:
        """裁决"从一个**包**里 import 子模块"这种写法;返回拒绝理由(None 表示放行)。

        为什么必须有这一层(第 22 轮实测,不是推理出来的):
        `from <包> import <子模块>` 的子模块 import 是在**导入机制内部**完成的,
        根本不经过 `check()`——守卫只在候选的 import 语句**入口**上生效。
        而命名空间包(`scripts/`、`pending_strategies/` 这种没有 `__init__.py` 的目录)
        的 origin 是 None,按"看 origin"的规则会被放行。于是:

        - `from scripts import research_api` -> 候选拿到带 `subprocess` 的框架模块
          (等于任意命令执行),四层防线全绿;
        - `from pending_strategies import evil` -> 候选自己写的辅助模块**被执行**,
          拿到真实内建——"按路径裁决"整条规则被绕开。

        修法:包的**名字空间位置**只要落在仓库内、且不在允许目录里,就拒绝这种导入。

        这条与"逐项裁决"是**互补**而不是重复:逐项裁决只管**模块**,
        候选 `from <包> import <某个常量>`(不是模块)时逐项裁决看不见任何东西,
        只有这一条能把那个包整个挡在门外。
        """
        for loc in module_locations(name):
            try:
                resolved = Path(loc).resolve()
            except OSError:
                # 第 25 轮 codex 的 Medium 9:原来这里 continue(跳过)——
                # "查不出来"被当成了"没问题"。与本文件其它地方的 fail-closed 口径相反。
                return (f"禁止从 {name} 导入:无法确认包位置的状态"
                        f"(查不了等于没查,fail-closed)")
            if self.repo_root != resolved and self.repo_root not in resolved.parents:
                continue                                        # 仓库之外(站点包)照旧放行
            if not self._under_allowed_dir(resolved):
                return (f"禁止从 {name} 导入:包位置 {self._rel(resolved)} 不在允许的目录 "
                        f"{list(self.allowed_dirs)} 内(候选不得导入框架、风控或回测模块)")
        return None

    def check_package_wildcard(self, name: str) -> str | None:
        """裁决 `from <包> import *`(通配符)。

        为什么这条必须**独立**存在(第 22 轮用探针实测出来的,不是推理):
        通配符形式不会把 fromlist 里的名字逐个交给我们裁决——**导入机制自己**
        按包的 `__all__`(有的话)去 import 那些子模块,整个过程**不经过守卫**。
        于是只要包里有 `__all__`,候选把一个同名模块写进这个包,
        `from 包 import *` 就会把它**在导入机制里执行**(拿到真实内建),
        而逐项裁决对 `"*"` 一无所知。

        探针实证:辅助模块里只有一行 `open(...).write("x")`——`open` 在候选自己的帧里
        是被收走的,只有**在导入机制里执行**才拿得到真内建,标记文件因此被写了出来。

        修法:通配符这一路改成"扫目录"——包内(仓库侧)的每个 `.py` 都必须在快照里、
        且此后没被改过;只要多出一个文件(候选写的)就拒。
        """
        for loc in module_locations(name):
            try:
                resolved = Path(loc).resolve()
            except OSError:
                continue
            if self.repo_root != resolved and self.repo_root not in resolved.parents:
                # 仓库之外(站点包):**同样**要过"运行之前就存在"这一关。
                # 站点包在宿主机上通常可写,候选往里面写一个模块,
                # 而包自己的 `__all__` 正好列着那个名字——通配符导入会由
                # **导入机制**去 import 它,整条路不经过守卫(与仓库内同一条洞)。
                for path in sorted(resolved.rglob("*.py")):
                    reason = self._written_during_run(path)
                    if reason:
                        return f"禁止从 {name} 导入 *:包内 {path.name} {reason}"
                continue
            if not self._under_allowed_dir(resolved):
                return (f"禁止从 {name} 导入 *:包位置 {self._rel(resolved)} 不在允许的目录 "
                        f"{list(self.allowed_dirs)} 内(候选不得导入框架、风控或回测模块)")
            for path in sorted(resolved.rglob("*.py")):
                try:
                    key = str(path.resolve())
                    stat = path.stat()
                except OSError:
                    # 第 25 轮 codex 的 Medium 9:stat 不出来的文件原来被**跳过**,
                    # 而 `check()` 那一侧是"查不到就拒"——两处口径相反。
                    return (f"禁止从 {name} 导入 *:包内 {path.name} 的状态查不到"
                            f"(查不了等于没查,fail-closed)")
                known = self.snapshot.get(key)
                if known is None:
                    return (f"禁止从 {name} 导入 *:包内 {path.name} 在候选开始运行之前并不存在"
                            f"(候选运行期间写出的文件一律不得被导入)")
                if self._changed(known, path, stat):
                    return f"禁止从 {name} 导入 *:包内 {path.name} 在候选运行期间被改动过"
        return None


def _build_restricted_import(safe: dict, real_import, blocked: set, repo_guard):
    """造一个受限导入器。敏感值一律**闭包单元**(不在 f_locals、不在 f_globals)。

    返回的函数还不能直接用——它的 `__globals__` 是**本模块**的全局字典,
    里面全是敏感值。调用方要用 `types.FunctionType` 换一份干净的 globals
    (见 `safe_builtins`)。
    """
    _mt = ModuleType
    _paths = FORBIDDEN_MODULE_PATHS
    _leak = _capability_leak
    _danger = _is_dangerous_module
    _view = _view_for
    import sys as _sys
    _modules = _sys.modules                 # 真模块表:导入机制回退时会从这里取"真模块"
    # 这一帧的 globals 是**干净的**(只剩受限内建),里面没有 `object`——
    # 所以属性读取走闭包单元,不走全局名。
    _get = object.__getattribute__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        fromlist = fromlist or ()          # 导入机制对普通 import 传的是 None
        root = name.split(".")[0]
        if root in blocked:
            raise ImportError(f"禁止导入 {name}")
        for path in _paths:
            if name == path or name.startswith(path + "."):
                raise ImportError(f"禁止导入 {name}:这个模块把执行能力递给了候选(规则:按全名禁用)")
        if repo_guard is not None:
            # 相对导入(level>0)按当前模块的包还原成绝对名字再裁决,
            # 否则 `from . import x` 会绕过路径规则(名字看起来像仓库内的相对路径)。
            target = name
            if level:
                package = (globals or {}).get("__package__") or ""
                if not package:
                    raise ImportError(f"禁止相对导入 {name}:当前模块不在任何包内")
                base = package.split(".")
                if level > 1:
                    base = base[: -(level - 1)]
                target = ".".join([p for p in base if p] + ([name] if name else []))
            try:
                reason = repo_guard.check(target)
            except Exception as err:                    # noqa: BLE001 —— 守卫自身出错也是"拒"
                # 守卫的帧一旦出现在候选看得见的 traceback 里,那一帧的 f_globals
                # 就是本模块的全局字典(里面有 os)。所以任何意外异常都在这里
                # 换成"干净的" ImportError(fail-closed):守卫的帧不进 traceback。
                raise ImportError(f"禁止导入 {target}:路径裁决出错({type(err).__name__}),"
                                  f"查不了等于没查,fail-closed") from None
            if reason:
                raise ImportError(reason)
            if fromlist:
                # fromlist 非空 = "从包里 import 若干个子模块"。
                # 那些子模块的 import 是在**导入机制内部**完成的,不经过 `check()`,
                # 所以必须在这里预先裁一遍(第 22 轮实测的绕过:命名空间包 + fromlist)。
                try:
                    reason = repo_guard.check_package(target)
                except Exception as err:                # noqa: BLE001
                    raise ImportError(f"禁止导入 {target}:包裁决出错({type(err).__name__}),"
                                      f"查不了等于没查,fail-closed") from None
                if reason:
                    raise ImportError(reason)
                if "*" in fromlist:
                    # 通配符:`"*"` 不是一个模块名,逐项裁决对它无能为力——
                    # 导入机制按包的 `__all__` 自己 import 子模块,整条路都不经过守卫。
                    # 所以这一路改成"扫包目录":多出一个文件就拒(第 22 轮探针实证)。
                    try:
                        reason = repo_guard.check_package_wildcard(target)
                    except Exception as err:            # noqa: BLE001
                        raise ImportError(f"禁止从 {target} 导入 *:包扫描出错"
                                          f"({type(err).__name__}),查不了等于没查,fail-closed") from None
                    if reason:
                        raise ImportError(reason)
                for item in fromlist:
                    if item == "*":
                        continue            # 通配符:上面已经按目录整体裁过
                    try:
                        reason = repo_guard.check(f"{target}.{item}")
                    except Exception as err:            # noqa: BLE001
                        raise ImportError(f"禁止导入 {target}.{item}:路径裁决出错"
                                          f"({type(err).__name__}),查不了等于没查,fail-closed") from None
                    if reason:
                        raise ImportError(reason)
        try:
            module = real_import(name, globals, locals, fromlist, level)
        except ImportError:
            # **不要**把导入机制的异常原样抛给候选(第 21 轮实测的逃逸之二):
            # 那个异常的 traceback 里带着导入机制的帧,候选顺着
            # `e.__traceback__.tb_frame.f_builtins` 就能拿到**真实内建**。
            # 这里换一个"干净的"异常:抛出点在下面(不在 except 里),
            # 于是新异常的 traceback 只有"我们自己那一帧 + 候选自己的帧",
            # 帧的内建都是受限的那一份。
            reason = f"导入 {name} 失败"
            raise ImportError(reason) from None
        # **不**在这里整块拒绝"会把能力递出来的模块"(第 25 轮 codex 的 High 3):
        # 能力交付是由**属性层的模块视图**负责的(危险属性不给、会递出 ctypes/subprocess 的模块整个不给)。
        # 导入门那一层再拒一遍,口径就比视图层**更严**,于是合法的候选直接跑不起来:
        # `matplotlib` 与 `psutil` 的命名空间里都有 `subprocess`(matplotlib 建字体缓存要用),
        # 实测 `import matplotlib` -> ImportError:禁止导入 matplotlib:它把 subprocess 递出来。
        # 候选只想画个图,却整个模块都用不了——误伤比漏检更糟(它让人以为"沙盒更严了")。
        #
        # fromlist 里的子模块:导入机制自己 import 的,不看它的属性就会漏。
        for item in fromlist:
            if item == "*":
                continue
            try:
                sub = _get(module, item)
            except AttributeError:
                sub = None
            if isinstance(sub, _mt):
                if _danger(sub):
                    raise ImportError(f"禁止导入 {name}.{item}:模块视图不把危险模块 "
                                      f"{sub.__name__} 交给候选(规则:模块视图)")
            # **High 2(视图的"拒绝"会被导入机制的回退绕过)**:
            # 视图用"抛 AttributeError"表示不给,而 CPython 的 `import_from` 在
            # `getattr(v, name)` 失败时会**清掉异常并回退到 `sys.modules["pkg.name"]`**——
            # 于是"拒绝"恰好触发了"交出真模块"。第 25 轮 codex 实测:
            # 构造一个 `__init__.py` 里 `del sys.modules['pkgx'].sub` 的包,
            # `from pkgx import sub` 拿到的就是**真模块**(isinstance(view) 为 False)。
            # 所以这里显式查一次 sys.modules:命中的是真模块,就按真模块的规矩裁决。
            real = _modules.get(f"{name}.{item}")
            if real is not None and isinstance(real, _mt):
                if _danger(real):
                    raise ImportError(f"禁止导入 {name}.{item}:模块视图不把危险模块 "
                                      f"{real.__name__} 交给候选(规则:模块视图)")
                leak = _leak(real)
                if leak:
                    raise ImportError(f"禁止导入 {name}.{item}:它把 {leak} 递出来,"
                                      f"等于把执行能力递给候选(规则:能力泄漏)")
        # 候选拿到的是**视图**,不是模块本身(见 ModuleView 的说明)。
        return _view(module) if isinstance(module, _mt) else module

    return restricted_import


def safe_builtins(forbidden_modules: Sequence[str] = (), repo_guard: "RepoImportGuard | None" = None) -> dict:
    """候选执行时用的**最小内建集合**:去掉 `UNSAFE_BUILTINS`,并把 `__import__` 换成受限版。

    受限导入器只放行不在禁用名单里的模块——候选必须能 import(模板与策略基类就是这么来的),
    但 `import builtins` / `import unittest.mock` 这类"拿到内建或改写属性能力"的入口必须关上。

    `repo_guard` 是**按路径**的第二道裁决(见 `RepoImportGuard`):
    名单管不住"候选自己写一个辅助模块再 import 它"这条逃逸,路径规则管得住。

    **为什么这一段要这么写**(第 24 轮探针实测,不是推理):
    候选顺着 ImportError 的异常链能走到**受限导入器自己那一帧**。
    第 21 轮只把那一帧的 `f_builtins` 换成受限字典,可那一帧的 `f_globals` 里
    还放着 `real_import`——候选一句 `tb_frame.f_globals["real_import"]` 就把真导入器拿走,
    接着 `real_import("os")` 拿到任意命令执行(实测标记文件被写出来)。
    所以这里:
    - 敏感值(真导入器、禁用名单、路径规则)一律放进**闭包单元**:
      闭包单元不在 `f_locals` 里,只有 `__closure__` 能摸到,而那个名字静态层已禁;
    - 受限导入器的 `f_globals` 换成一份**干净的新字典**(只含受限内建):
      候选再摸 `f_globals` 也只能拿到 `{"__builtins__": 受限内建}`。

    踩过的坑(留给下一个改这里的人):**嵌套函数的 `__globals__` 是定义它的模块的
    全局字典**,不是外层帧的 locals。想清空"外层那一帧"是清不掉的——
    直接 `__globals__.clear()` 会把整个模块的命名空间抹掉(实测:模块级类全丢)。
    所以要走 `types.FunctionType(code, 新globals, ..., closure)` 重造一份函数。
    """
    import builtins as _builtins
    import types as _types

    # **High 4**:危险名单原先只在**属性层**生效,导入门放行了一大片
    # (实测 httpx / urllib3 / ssl / codecs / gzip / tarfile / zipfile / sqlite3 / mmap / atexit 全部放行):
    # 网络外传(`httpx.get(...)`)与文件写(`codecs.open`)的能力都还在。
    # 现在导入门复用**同一份**危险名单——两层口径必须一致,否则"名单"只是名字上的说法。
    blocked = (set(FORBIDDEN_MODULES) | set(EXTRA_FORBIDDEN_MODULES)
               | set(DANGEROUS_MODULE_NAMES) | set(forbidden_modules))
    real_import = _builtins.__import__

    safe = {k: v for k, v in _builtins.__dict__.items() if k not in UNSAFE_BUILTINS}

    raw = _build_restricted_import(safe, real_import, blocked, repo_guard)
    # 重造一份:代码与闭包不变,`f_globals` 换成干净的。
    safe["__import__"] = _types.FunctionType(raw.__code__,
                                            {"__builtins__": safe},
                                            raw.__name__,
                                            raw.__defaults__,
                                            raw.__closure__)
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
            # `from numpy import ctypeslib` 与 `import numpy.ctypeslib` 是**同一件事**,
            # 但原实现只看 `node.module`(numpy),看不见 names 里的 ctypeslib,
            # 于是这条写法**放行**(第 25 轮 codex 的 Medium 6 实测)。
            # 而运行期的拒绝发生在 import **之后**——模块已经在导入机制里以真实内建执行过,副作用已经发生。
            names = [node.module or ""]
            for alias in node.names:
                if node.module and alias.name != "*":
                    names.append(f"{node.module}.{alias.name}")
        else:
            names = []
        for name in names:
            if name.split(".")[0] in forbidden_modules:
                findings.append(f"第 {node.lineno} 行: 禁止导入 {name}")
            elif any(name == path or name.startswith(path + ".") for path in FORBIDDEN_MODULE_PATHS):
                findings.append(f"第 {node.lineno} 行: 禁止导入 {name}"
                                f"(这个模块把执行能力递给了候选)")
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
    findings.extend(_screen_key_usage(tree))
    return findings


def _screen_key_usage(tree: ast.AST) -> list[str]:
    """危险模块的**短名**当作键来取时拒掉(第 24 轮自查修)。

    只拦"按键取"这个动作,不拦这些字符本身:
    - `d["os"]` —— 下标键;
    - `d.get("os")` / `d.pop("os")` / `d.setdefault("os", ...)` —— 字典取值方法的参数;
    - `dict(os=...)` —— 关键字参数名。

    拦的是"用这些名字当钥匙去取东西"这个动作:第 24 轮实测的老洞正是
    `tb_frame.f_globals["os"]`(顺着异常链摸到某一帧的 globals 再按键取值)。
    而把它们从全局字符串禁令里挪出来,是为了不误伤
    "恰好等于这些词的普通字符串"(策略里的状态名、列名、打印内容)。
    """
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Index):                    # 兼容 3.8 的写法
                key = key.value
            if (isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and key.value in FORBIDDEN_KEY_STRINGS):
                findings.append(f"第 {node.lineno} 行: 禁止用 {key.value!r} 作字典键"
                                f"(按键取危险模块)")
        elif isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Attribute):
                name = callee.attr
            elif isinstance(callee, ast.Name):
                name = callee.id
            else:
                name = ""
            if name in _KEY_ACCESSOR_NAMES:
                for arg in node.args:
                    if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and arg.value in FORBIDDEN_KEY_STRINGS):
                        findings.append(f"第 {node.lineno} 行: 禁止用 {arg.value!r} 作字典键"
                                        f"(按键取危险模块)")
            for kw in node.keywords:
                if kw.arg in FORBIDDEN_KEY_STRINGS:
                    findings.append(f"第 {node.lineno} 行: 禁止用 {kw.arg!r} 作关键字参数名"
                                    f"(按键取危险模块)")
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
