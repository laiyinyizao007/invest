"""研究沙盒底座(纯逻辑,可单测):路径限制、静态筛查、子进程环境白名单、窗口限制、往返盈亏、结果存取。

威胁模型必须说清楚:这里的措施是**纵深防御,不是沙盒**。
- 策略代码在本机以当前用户权限运行,理论上能读任何本机文件;静态筛查拦得住明显的越权写法,拦不住刻意绕过(反射、字节码等)。
- 因此 4a「人工驱动」阶段的前提是:**策略代码在运行前由人读过**(CLI 要求 --confirm-reviewed,并把代码 sha256 写入结果)。
- 让 LLM 的产出在无人读过的情况下运行,需要容器级隔离(无网络、仅只读挂载数据、无 .env),那是阶段四 4b 的前置,不在此实现。
设计依据:docs/adr/20261110-bisheng-ai-researcher.md、docs/adr/20261112-research-sandbox-groundwork.md。
"""
from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import sys
import time
import types
import uuid
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

# 代码根放进 sys.path:`risk` 包才可导入。
# **不 import bootstrap**:那会把 vnpy 交易栈与 .env 拖进研究侧进程(边界声明见 ADR)。
_CODE_ROOT = Path(__file__).resolve().parents[1]
if str(_CODE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CODE_ROOT))
# scripts/ 也要在路径上:窗口常量在 scripts/segment_windows.py(纯数据模块)。
_SCRIPT_ROOT = Path(__file__).resolve().parent
if str(_SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_ROOT))

from risk import constitution as const  # noqa: E402

PENDING_DIR = "pending_strategies"
SEGMENTS = ("train", "valid")        # 测试段不可经由沙盒到达
KEYS = ("CADJ", "MAADJ")             # 目前只支持数据库里的日线复权品种
LOCK_STALE_SECONDS = 60.0            # 残留锁(持有者已崩)的判定上限

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
FORBIDDEN_ATTRS = {"__bases__", "__subclasses__", "__globals__", "__builtins__", "__dict__", "__code__", "__mro__", "__class__",
                   # 函数对象的**状态**(第 10 轮 codex 实测的 Critical):
                   # `within_budget.__defaults__ = (0.0,)` 把 volume 默认值改成 0,
                   # 单笔风险金额恒为 0,300 元预算形同虚设——改的不是名字,是对象的状态。
                   "__defaults__", "__kwdefaults__", "__annotations__", "__wrapped__",
                   # 类型判定入口:改掉 `__instancecheck__` 之后 isinstance 一律返回 False,
                   # 运行时第三层的家族判定直接失效。
                   "__instancecheck__", "__subclasscheck__", "__subclasshook__", "__prepare__",
                   # 模块的**加载器入口**(第 11 轮 codex 实测的 Critical):
                   # `mod.__spec__.loader.get_data(path)` 读任意文件(`.env`、网关令牌),
                   # `mod.__spec__.loader.set_data(path, b)` **写任意文件**——
                   # 能直接改 `approvals.json`,于是「人读过源码才批准」被候选单方面废掉
                   # (源码里看不见的写文件路径,静态审查抓不到)。
                   "__spec__", "__loader__", "__file__", "__path__", "__package__",
                   # 反射/序列化入口:同样是"按名字拿真东西"的钥匙。
                   "__reduce__", "__reduce_ex__", "__getattr__",
                   # **异常回溯帧**(第 11 轮 codex 实测的 Critical):候选在自己代码里
                   # `except BaseException as e:` 中顺着 `e.__traceback__.tb_frame.f_globals`
                   # 能拿回**真 os**(numpy 内部帧的全局命名空间里有 os),于是任意写文件
                   # (可伪造 approvals.json)、读文件(.env、网关令牌)、执行命令,
                   # 而**两层静态筛查零告警**——这条路上一个禁用名都不出现。
                   # 名字枚举不完(还能从 `__context__`/`__cause__` 绕),但**拿到帧**
                   # 必经这几个:它们是必经之路,不是可选路径。
                   "__traceback__", "__context__", "__cause__",
                   "tb_frame", "tb_next", "tb_lasti",
                   "f_globals", "f_locals", "f_builtins", "f_back", "f_code", "f_lineno"}

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

# 名字藏在**字符串**里也要看得见:静态层原先只判「字符串常量**等于**禁用名」,
# 于是 `"{0.__spec__}".format(np)`、`"{0.__globals__}".format(np.mean)` 零告警
# (第 11 轮 codex 的 F3)。这些是**强特征串**(dunder 形状),子串匹配不会误报;
# `open`/`type` 这类常见词仍然只做全等匹配,免得把正常候选整片拒掉。
FORBIDDEN_SUBSTRINGS = frozenset(
    a for a in FORBIDDEN_ATTRS
    if a.startswith("__") or a.startswith(("tb_", "f_"))
) | frozenset({
    "__traceback__", "__context__", "__cause__", "tb_frame", "tb_next",
    "f_globals", "f_locals", "f_builtins",
    "__globals__", "__code__", "__spec__", "__loader__", "__mro__",
    "__subclasses__", "__getattribute__", "__dict__", "__reduce__",
})

# 私有名里藏着的**能力入口**:运行期一律取不到(`name.startswith("_")` 就拒),
# 静态层也要报——否则模型看到的是「筛查说没问题、运行时才炸」的口径不一致
# (第 11 轮 codex 的 F9)。
DANGEROUS_UNDERSCORE_ATTRS = frozenset({
    "os", "sys", "io", "spec", "loader", "module", "dict", "globals",
    "code", "builtins", "file", "path", "traceback", "frame", "state",
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
# 代价:候选用到这些名字会得到 NameError(而不是静默放行),由人在批准前看出来。
UNSAFE_BUILTINS = frozenset({
    "setattr", "delattr", "getattr", "eval", "exec", "compile", "open",
    "globals", "locals", "vars", "breakpoint", "input",
    "type", "object",                       # 类型系统入口:__mro__/__subclasses__ 是万能钥匙
    "help", "exit", "quit", "license", "credits",
})

# 这些模块能改写任意对象的属性(`unittest.mock.patch.object`)或拿到内建本身,同样禁掉。
EXTRA_FORBIDDEN_MODULES = frozenset({"unittest", "unittest.mock", "mock"})

# **运行时导入白名单**(第 7 轮 codex 的 F1:黑名单是不完整的,补不完)。
#
# 之前只按**模块名**黑名单:候选 `import zipfile` 之后,`zipfile.os.system(...)` 就拿到了
# 任意命令执行——而 `zipfile` 自己 import 了 os 并把它挂在模块属性上。
# 实测:`zipfile`/`tarfile`/`configparser`/`socketserver`/`mimetypes`/`netrc` 都这样;
# `shelve`/`sqlite3`/`xmlrpc` 也各自握着别的入口。黑名单是打地鼠:下一个新名字照样漏。
#
# 改成白名单:候选**只能** import 这几个根模块。白名单里的都是纯计算/数据结构类模块,
# **但"纯计算"不等于"干净"**(第 10 轮 codex 实测的 Critical):
# `enum.sys` / `typing.sys` / `dataclasses.sys` / `statistics.sys` / `fractions.sys` /
# `collections._sys` / `random._os` 都是**真的 os/sys 模块**,挂在模块自己的属性上。
# 候选 `import enum` 之后 `enum.sys.modules["os"].system("...")` 就能执行任意命令,
# 而静态筛查对这一形态原本零告警(它只认「X.os」这一层,不是任意深度的属性链)。
#
# 所以白名单有两个配套机制:
#   1) **静态层**:属性链里任何一段命中危险名就报(不限深度),`from X import os` 也报;
#   2) **运行层**:受限导入器把会漏的模块包成**代理**,危险属性一律取不到
#      (见 `_GuardedModule` 与 `safe_builtins`)。
# `logging` **不在**白名单里:它自己 import 了 os 并暴露成 `logging.os`——
# 与其为了它开一个会漏的口子,不如让候选别用(静态筛查会把原因告诉模型,让它改写)。
#
# 需要更多模块时走配置 `screening.allowed_modules`(追加,不放宽内置白名单以外的东西);
# 配置追加的模块**额外**要过准入审计:自己暴露危险能力的模块一律拒绝(见 `audit_module`)。
ALLOWED_MODULE_ROOTS = frozenset({
    "math", "statistics", "collections", "datetime", "typing", "enum", "dataclasses",
    "itertools", "functools", "abc", "decimal", "fractions", "random", "copy", "re",
    "string", "time", "json", "numpy", "pandas", "strategies", "vnpy", "vnpy_ctastrategy",
})

# 候选**不得**从导入进来的模块上取这些属性:`X.os` / `X.shutil` / `X.subprocess` ……
# 有些模块自己 import 了 os/sys 并挂在属性上(见上面的实测清单),静态层按
# 「取模块的危险属性」这一形态一律拒绝,而且**不限深度**:`enum.sys.modules` 里
# 那一段 `sys` 同样命中(第 10 轮 codex 的 Critical)。
#
# `sys`/`_sys`/`_os` 必须在册:它们是"stdlib 里最常见的间接入口"
# (enum/typing/statistics/fractions 挂 `sys`,collections 挂 `_sys`,random 挂 `_os`)。
LEAKY_MODULE_ATTRS = frozenset({
    "os", "sys", "_os", "_sys", "shutil", "subprocess", "importlib", "ctypes", "socket",
    "tempfile", "glob", "pickle", "marshal", "builtins", "pathlib", "imp", "code", "codeop",
    "runpy", "io", "pty", "commands", "posix", "nt", "signal", "atexit", "gc", "inspect",
    "threading", "multiprocessing", "platform", "sysconfig", "requests", "urllib", "http",
})


def audit_module(module) -> list[str]:
    """模块自己暴露出来的**危险属性名**(它 import 了 os/sys 并挂在属性上)。

    空列表 = 干净。第 10 轮 codex 实测:`enum.sys` / `random._os` / `statistics.sys`
    都是真的 os/sys 模块,静态层按「X.os」那一层根本看不见。"""
    out: list[str] = []
    prefix = getattr(module, "__name__", "") + "."
    for name in LEAKY_MODULE_ATTRS:
        if not hasattr(module, name):
            continue
        value = getattr(module, name)
        # **子模块不算泄漏的能力**:`pandas.io.pickle` 是 `pandas.io` 的子模块,
        # 不是它 import 进来的能力入口。按名字一律拒会把整个 `pandas.io` 打死
        # (候选用 `pandas.io.parsers` 就用不了)——第 11 轮 codex 的 F8。
        if isinstance(value, types.ModuleType)                 and str(getattr(value, "__name__", "")).startswith(prefix):
            continue
        out.append(name)
    return sorted(out)


# 代理**唯一**放行的下划线开头的名字。其余(dunder 与私有名)一律取不到:
#   `mod.__spec__.loader.get_data(path)`  → 读任意文件(`.env`、网关令牌)
#   `mod.__spec__.loader.set_data(path,b)` → **写任意文件**(能改 `approvals.json`)
#   `mod.__dict__` / `mod.__getattribute__` → 拿到真模块,上面的入口又回来了
# 这些写法在源码里**看不见**(不像 `open(...)` 那样能被静态审查一眼抓到),
# 于是「人读过源码才批准」这道仪式会被绕过(第 11 轮 codex 的 Critical)。
PROXY_SAFE_NAMES = frozenset({"__name__", "__doc__", "__all__", "__version__",
                              "__annotations__", "__qualname__"})


class _GuardedModule:
    """白名单模块的**代理**:候选拿到的不是真模块,而是它。

    运行期的第二道防线(第 10 轮 codex 的 Critical):静态层能封住已知的写法,
    封不住"经模块属性间接拿到 os"。`import enum` 拿到的是代理,`enum.sys` 抛 ImportError。

    第 11 轮 codex 的 Critical 又补了一条更狠的:顺着
    `mod.__spec__.loader.get_data/set_data` 能**读写任意文件**——
    `set_data` 能直接改 `approvals.json`,于是「人读过源码才批准」这道仪式
    被候选单方面废掉(源码里看不见的写文件路径)。
    所以这里不是"列几个危险名字",而是:
      - 模块自己泄漏的能力(os/sys/subprocess…)一律取不到;
      - **dunder 与下划线开头的属性一律取不到**(只放行少数几个安全的);
      - 取到的**子模块**也包成代理(`pandas.io.common.os` 这条链不能漏)。
    """

    def __init__(self, module, blocked: frozenset[str]):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_blocked", blocked)

    def __getattribute__(self, name):
        blocked = object.__getattribute__(self, "_blocked")
        if name in blocked:
            raise ImportError(f"研究沙盒:模块属性 {name} 不可用"
                              "(该模块把 os/sys 之类挂在属性上)")
        if name.startswith("_") and name not in PROXY_SAFE_NAMES:
            # dunder(`__spec__`/`__loader__`/`__dict__`/`__getattribute__`…)与私有名一律不给:
            # 前者能读写任意文件或拿到真模块,后者是代理自己的内部状态。
            raise ImportError(f"研究沙盒:模块属性 {name} 不可用"
                              "(经它可以读写任意文件或拿到真模块)")
        module = object.__getattribute__(self, "_module")
        value = getattr(module, name)
        if isinstance(value, types.ModuleType):
            # 子模块也包代理:`pkg.sub.os` 这条链不能漏(递归,带缓存保身份稳定)
            return _proxy_for(value)
        return value

    def __dir__(self):
        module = object.__getattribute__(self, "_module")
        blocked = object.__getattribute__(self, "_blocked")
        return [n for n in dir(module)
                if n not in blocked and (not n.startswith("_") or n in PROXY_SAFE_NAMES)]


# 代理缓存:模块 → 代理(同一个模块永远给同一个代理,免得 `is` 判断失真)。
_PROXY_CACHE: dict[object, "_GuardedModule"] = {}


def _proxy_for(module):
    """给模块(以及它的子模块)包一层代理。

    **不改 `sys.modules`**:宿主进程自己的代码拿到的仍是真模块,
    只有候选(经受限导入器)拿到代理。"""
    proxy = _PROXY_CACHE.get(module)
    if proxy is None:
        blocked = frozenset(audit_module(module))
        proxy = _GuardedModule(module, blocked)
        _PROXY_CACHE[module] = proxy
    return proxy


def _guard_module(module):
    """模块包成代理(兼容旧名)。"""
    return _proxy_for(module)


def restore_guarded() -> None:
    """清掉代理缓存(候选执行结束后调用)。

    代理**不进 `sys.modules`**,所以没有"换回去"这一步;
    清缓存只是不让上一轮的代理对象继续被引用。"""
    _PROXY_CACHE.clear()


@contextlib.contextmanager
def guarded_scope():
    """候选执行期上下文:退出时无条件还原代理。

    用它包住 `exec`:代理只在该候选运行期间生效,宿主机自己的代码不该看到代理
    (第 10 轮 codex 的 Critical 修复)。"""
    try:
        yield
    finally:
        restore_guarded()


def safe_builtins(forbidden_modules: Sequence[str] = (), allowed_modules: Sequence[str] = ()) -> dict:
    """候选执行时用的**最小内建集合**:去掉 `UNSAFE_BUILTINS`,并把 `__import__` 换成受限版。

    受限导入器按**白名单**放行:候选必须能 import(模板与策略基类就是这么来的),
    但只有白名单里的根模块能进——`import builtins` / `import unittest.mock` 这类
    "拿到内建或改写属性能力"的入口,以及 `import zipfile` 这种"间接拿到 os"的入口,都关上。

    运行期还有一道:白名单里**自己会漏**的模块(enum/typing/random…)包成代理,
    危险属性取不到(第 10 轮 codex 的 Critical);配置追加的模块则**导进来就审计**,
    暴露危险能力的直接拒绝(不能靠配置把防线拆掉)。
    """
    import builtins as _builtins

    blocked = set(FORBIDDEN_MODULES) | set(EXTRA_FORBIDDEN_MODULES) | set(forbidden_modules)
    allowed = set(ALLOWED_MODULE_ROOTS) | set(allowed_modules)
    real_import = _builtins.__import__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".")[0]
        if root in blocked:
            raise ImportError(f"禁止导入 {name}")
        if root not in allowed:
            raise ImportError(f"未列入白名单的导入 {name}(研究沙盒只放行白名单里的模块)")
        module = real_import(name, globals, locals, fromlist, level)
        # 配置追加的模块:准入审计——自己就把 os/shutil 挂在属性上的模块不许进。
        # 审计用的是**刚导进来的那个模块对象**(模块已经在 sys.modules 里),
        # 不再额外动态导入一次:本模块必须保持「不是执行候选的入口」——
        # 宪法守卫(`tools/check_constitution_history.py`)把带 `importlib.import_module`
        # 的模块判成执行入口,要求它自己走第四层(快照 + 指纹比对),那不是本模块的职责。
        if root in allowed_modules and root not in ALLOWED_MODULE_ROOTS:
            imported = sys.modules.get(root)
            if imported is not None and not isinstance(imported, _GuardedModule):
                leaked = audit_module(imported)
                if leaked:
                    raise ImportError(f"模块 {root} 暴露了 {','.join(leaked)},不能加入白名单")
        if isinstance(module, _GuardedModule):
            return module
        # 候选拿到的**一律是代理**:
        #   - 根模块(`enum.sys` / `random._os` 取不到);
        #   - 子模块(`pandas.io.common.os` 这条链:代理在取属性时递归包住子模块)。
        # 原来这里对叶子模块做的是「泄漏就整个拒绝导入」——那会把 numpy 打死
        # (numpy 自己的内部模块 import 了 os),策略代码最常用的归约方法全部失效
        # (第 11 轮 codex 的 High)。现在改成**属性层面**挡:模块照常能用,
        # 危险属性取不到——安全效果不减,可用性回来了。
        return _proxy_for(module)

    safe = {k: v for k, v in _builtins.__dict__.items() if k not in UNSAFE_BUILTINS}
    safe["__import__"] = restricted_import
    return safe


class SandboxError(ValueError):
    pass


# 本机路径不该出现在**会外发的文本**里(网关响应 → 毕昇工作流 → 模型)。
# 只盖**绝对路径**:Windows 盘符路径(含空格)、UNC、扩展路径、POSIX 绝对路径
# (多段,以及常见单段根目录如 /tmp)。
# 引号里的裸文件名不盖——那是人要看的信息(候选叫什么),不是本机结构。
#
# 盘符那一段有两个否定前瞻/后顾,免得误伤 URL:
#   - `(?<![A-Za-z0-9])`:`https:` 里的 `s:` 前面是字母,不算盘符。
#   - `(?!//)`:`file://` 这种双斜杠不是盘符路径。
# 分隔符允许**一个或两个**反斜杠:JSON/repr 形态的路径(`"C:\\Users\\x.py"`)是两个,
# 只认一个会把尾部段漏在外面(第 7 轮 codex 的 F9)。
# 第 12 轮 codex 的 M1:路径这一层原来有三个可复现的漏——
#   ①`(?<![A-Za-z0-9])` 这个为躲 URL 加的否定前瞻,把「盘符前紧贴一个 ASCII 字母」
#     (`openC:\Users\alice\.env`)整条排除在外;
#   ②`[^\s"']*` 遇空格即停,于是 `C:\Users\John Doe\.env` 只盖掉前半段,
#     **OS 用户名**留在文本里;
#   ③UNC 的主机名不允许空格,`\\My Server\share\x.txt` 原样出去。
#
# 现在改成「**扫描式**」:先找到路径的**起点**(盘符或 UNC),再一路扫描到路径真的结束。
# 空格算不算路径的一部分,看**下一个词后面是不是分隔符**:
#   - `C:\Users\John Doe\.env` → 「Doe」后面是 `\`,空格在路径里 → 整条盖掉;
#   - `C:\Users\alice\.env failed` → 「failed」后面没有分隔符 → 空格是散文 → 路径到 `.env` 为止;
#   - `C:\Users\John Doe`(profile 目录,后面没有别的段)→ 额外多吃一个词(OS 用户名要紧);
#   - `C:\Program Files\app.exe` → 同上,「Files」后面是分隔符 → 整条盖掉。
# 盘符前的否定前瞻整个去掉:URL 靠 `_URL_RE` 先摘出来保护(见 `redact_paths`),
# 而 `https:` 的 `://` 形态由 `(?!//)` 单独排除,不再需要「前一字符不能是字母」这条。
_PATH_START_WIN = re.compile(r"[A-Za-z]:(?!//)[\\/]{1,2}")
# UNC 起点:反斜杠(真实路径 2 个、JSON/repr 形态 4 个)+ 主机名 + 一个分隔符。
# 「主机名后面必须跟分隔符」这条是刻意的:否则 repr 里的 `\n`(两个反斜杠)会被当成
# UNC 起点,把后面的正文整段盖掉(过度遮蔽会把诊断信息也吃掉)。
# 主机名允许**一个空格**(`\\My Server\share`,第 12 轮 codex 的 M1 ③)。
_PATH_START_UNC = re.compile(
    r"(?<![A-Za-z0-9])\\{2,4}[^\s\\\"<>|]+(?: [^\s\\\"<>|]+)?[\\/]")
_PATH_RE_POSIX = re.compile(r"(?<![\w.])/(?:[^\s\"']+/)+[^\s\"']*")
# 空格后面跟着「词 + 分隔符」→ 这个空格在路径里面。
# 分隔符**按这条路径自己的风格**认:反斜杠路径只认 `\`,正斜杠路径只认 `/`。
# 两种一视同仁就会出事——散文里的相对路径(`pending_strategies/cand_1.py`)前面
# 常跟着一段反斜杠路径,认了正斜杠就会把散文里的文件名一起吃掉
# (第 13 轮 codex 实测出来的回归);反过来,正斜杠路径
# (`C:/Documents and Settings/x.txt`,第 14 轮 codex 的 M1)认不到正斜杠,
# 目录名与文件名就整段漏在模型通道里。
_PATH_SPACE_THEN_SEP = {
    "\\": re.compile(r" +([^\s\\/:*?\"<>|]+)\\"),
    "/": re.compile(r" +([^\s\\/:*?\"<>|]+)/"),
}
# 「多词路径段」:`C:\Program Files (x86)\app.exe` 里,第一个空格后面的词
# **后面并没有紧跟分隔符**(它后面是空格 + `(x86)\app.exe`),于是原先的扫描在
# `Program` 处就停了——`Files (x86)\app.exe` 整段留在模型通道里(第 13 轮 codex 的 M1)。
# 判据:从空格往后看,只要**在遇到句子终止符之前**还能看到分隔符(`\` 或 `/`),
# 中间这些词就都属于路径。终止符里必须包含 `:`——否则散文里的 URL
# (`详见 https://example.com/a/b`)会把后面的散文一起当成路径吃掉。
_PATH_LOOKAHEAD_TERMINATORS = "。！？；，、;:!?\r\n\"'<>|\x00"
# **中文句子终止符**:中文语境下候选 raise 出来的文本常带句号(「…失败。后面还有…」)。
# 主扫描必须在这里停,否则「。后面」这样的正文会被整段吃掉(第 14 轮 codex 的 L4)。
# 这个集合同时用于「取词」——词里含句号说明那已经是句子的另一截,不是路径的一部分。
_PATH_SENTENCE_PUNCT = "。！？；，、"
# 「像文件名」的词:`My File.txt` 里的 `File.txt`。扩展名只认**字母**
# (1~8 个)——`v1.2` 这种带数字的散文词不算,免得把版本号吃掉。
_PATH_FILE_SHAPED = re.compile(r"^[^\s\\/:*?\"<>|]*\.[A-Za-z]{1,8}$")
# profile 目录(`C:\Users\<名字>`)后面还可以有一个带空格的词(`John Doe`)。
# 第 13 轮 codex 的 M1:这条规则的**搜索起点是盘符之后**(`C:` 后面),
# 于是要求「Users 前面必须有一个分隔符」的老写法**永远匹配不上**
# (`Users\John` 前面是 `:` 而不是分隔符)——于是 `C:\Users\John Doe`
# 这种「profile 目录本身就带空格」的形态只盖掉 `John`,**姓氏原样出去**。
# 现在改成「Users 前面不能紧跟字母数字」,起点在 `:` 后面也能匹配上。
# 用户名本体只认 `[A-Za-z0-9._-]+`:带冒号(`C:\Users\alice:`)的形态不是用户名,
# 那时不吃后面那个词,散文(`retry later`)才留得住(第 13 轮 codex 的 L4 ②)。
_PATH_USERS_TAIL = re.compile(
    r"(?<![A-Za-z0-9])Users[\\/](?P<user>[A-Za-z0-9._-]+)$", re.IGNORECASE)
# 单段 POSIX 绝对路径(`/tmp`、`/etc`、`/data`):上面的 POSIX 规则要求至少两段,单段会漏。
# 只认「根名 + 一段路径」这种形态,免得把句子里的单独斜杠也盖掉(第 7 轮 codex 的 F9)。
# **后面不能再跟 "/"**:跟了的话多段路径会被这条规则先吃掉一段、
# 剩下的又被 POSIX 规则吃掉一次,同一个路径盖出两个 `<路径>`(第 11 轮 codex 的 Low)。
_PATH_RE_POSIX_ROOT = re.compile(
    r"(?<![\w.])/(?:tmp|var|etc|home|usr|opt|root|mnt|media|srv|run|dev|proc|sys|data|bin|sbin)"
    r"(?=[\s\"']|$)")
# URL 先摘出来保护:否则 `https://host/a/b` 里的 `/host/a/b` 会被当成 POSIX 路径盖掉。
_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"']+")
# 64 位十六进制:候选文件的 sha256。它**不能**进模型通道——模型拿到哈希就能在聊天里
# 替人念哈希,「人核对完整 64 位哈希」就退化成橡皮章。
# 键名是「sha256」的整条记录由网关单独剔除;这里管的是**字符串里**夹着的哈希
# (失败 Trace 的自由文本、错误消息、作业报告都可能夹着)。
# 第 12 轮 codex 的 L2:原来要求两侧不是十六进制字符,于是 64 位摘要**藏在一个更长的
# 十六进制串里**(`'x=' + '0'*32 + sha`)就整条原样出去——而那正是最常见的拼接形态
# (前面拼一段前缀、后面拼一段后缀)。现在改成「**连续 64 位以上的十六进制一律隐去**」,
# 更长的串(sha512 之类)整条盖掉,不会只盖掉中间的 64 位。
_DIGEST_RE = re.compile(r"[0-9a-fA-F]{64,}")
# 批准命令:`python scripts/research_api.py approve --strategy x.py` 或批准进程自己的
# `research_approval_server.py --strategy x`。念给模型听,模型就能诱导人去跑终端里的
# 批准流程——那条路上没有「只批本轮候选」的约束。
_APPROVE_CMD_RE = re.compile(
    r"(?i)\b[\w./\\-]*(?:research_api\.py\s+approve|research_approval_server\.py)\b[^\n]*")
# 批准页地址:`http://127.0.0.1:<port>/a/<192 位令牌>`。
# 页面里同时有 csrf 令牌与候选哈希:模型拿到地址就能自己 GET 一次、把两样都凑齐并 POST,
# 「人批准」当场变成「模型自我批准」(第 7 轮 codex 的 F3)。纵深防御:即便它混进文本也盖掉。
# 第 12 轮 codex 的 L4:scheme 可选——`127.0.0.1:8765/a/TOKEN`(不带 `http://`)
# 也是模型能直接用的地址,原来只按带 scheme 的形态隐去。
_APPROVAL_URL_RE = re.compile(r"(?i)(?:https?://)?(?:127\.0\.0\.1|localhost)(?::\d+)?/a/[A-Za-z0-9_\-]+")
# 文本里的**凭据**:赋值形态(`CTP_PASSWORD=xxx`、`"api_key": "xxx"`)、Authorization/Cookie 头、
# Bearer 令牌。候选一旦读到 .env 并把它写进异常文本,凭据就跟着进了模型通道
# (第 7 轮 codex 的 F5)。规则与 dashboard/safe.py 同源,但研究侧不 import 面板模块
# (面板属于状态页那一侧,研究侧只借规则、不借依赖)。
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|passphrase|secret|token|api[_-]?key|auth[_-]?code|app[_-]?id|credential|CTP_[A-Z0-9_]+)\b"
    r"(\s*[\"']?\s*[=:]\s*[\"']?)[^\s;,\"'&]+")
# Authorization 头:`Authorization: Bearer <令牌>` 这种形态,头名之后是 **scheme + 凭据**,
# 只吞到空白为止会把令牌留在外面(第 10 轮 codex 的 High)。
# 所以值这一段允许 "scheme 空格 凭据" 一起被吞掉。
_SECRET_HEADER_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization)\b(\s*[=:]\s*)"
    r"(?:[A-Za-z][A-Za-z0-9\-]*\s+)?[^\s;,]+")
# Cookie 头:**整条**隐去(第 12 轮 codex 的 L5:原来只隐去第一个值,
# `Cookie: session=abc123def; other=1` 里的 `other=1` 原样出去)。
# Cookie 头的值是一串「名=值;名=值」,第一个值后面的那些同样是凭据,
# 所以吃到**行尾**为止(cookie 头不会与别的头同行)。
_SECRET_COOKIE_RE = re.compile(r"(?i)\b(cookie|set-cookie)\b(\s*[=:]\s*)[^\r\n]*")
# Bearer 令牌:**先**于头规则处理——头规则一旦先把 "Bearer" 吞掉,令牌就成了孤儿
# (第 10 轮 codex 的 High:`Authorization: Bearer abc...` 实测令牌原样保留)。
_SECRET_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{4,}")
# CTP 凭据:env 行(`CTP_PASSWORD=hunter2`)与 JSON 行(`"CTP_PASSWORD": "hunter2"`)都要盖住。
# JSON 形态里 `:` 前面多一个引号,旧的 `CTP_[A-Z0-9_]+\s*[=:]` 够不着(第 10 轮 codex 的 Medium)。
_SECRET_CTP_RE = re.compile(r"(?i)\b(CTP_[A-Z0-9_]+)(\s*[\"']?\s*[=:]\s*[\"']?)[^\s;,\"'}]+")


def redact_digests(text: str) -> str:
    """把文本里的 64 位十六进制(内容哈希)换成 `<sha256 已隐去>`。

    网关与 MCP 两条模型可见通道共用这一条口径:一边剥一边不剥,
    等于「哪条通道漏了就是漏了」(第 6 轮 codex 的 H1)。
    """
    if not text:
        return text
    return _DIGEST_RE.sub("<sha256 已隐去>", text)


def redact_approve_commands(text: str) -> str:
    """把文本里的批准命令换成 `<批准命令已隐去>`。

    网关与 MCP 共用:批准命令一旦回到模型通道,「模型只请求批准、不安排批准」这条就破了。
    """
    if not text:
        return text
    return _APPROVE_CMD_RE.sub("<批准命令已隐去>", text)


def redact_approval_urls(text: str) -> str:
    """把批准页地址换成 `<批准页地址已隐去>`。

    地址里带着一次性令牌:模型拿到就能自己去 GET 页面、把 csrf 与哈希都凑齐并 POST。
    """
    if not text:
        return text
    return _APPROVAL_URL_RE.sub("<批准页地址已隐去>", text)


def redact_secrets(text: str) -> str:
    r"""把文本里的凭据形态替换掉(赋值、Authorization/Cookie 头、Bearer 令牌、CTP_ 变量)。

    顺序有讲究(第 10 轮 codex 的 High、第 11 轮的 Medium):**Bearer 最先**——
    ①头规则只吞到空白为止,`Authorization: Bearer <令牌>` 里的 "Bearer" 先被吞掉的话,
    令牌就成了孤儿,原样留在文本里;
    ②赋值规则的取值字符类 `[^\s;,"'&]+` 也会把 "Bearer" 当成值吞掉,
    真正的令牌(空格之后)留在原地——`token: Bearer abc123` 实测原样保留。
    两条规则都靠"Bearer"这个词定位令牌,所以它必须先跑。"""
    if not text:
        return text
    text = _SECRET_BEARER_RE.sub("Bearer <令牌已隐去>", text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: m.group(1) + m.group(2) + "<凭据已隐去>", text)
    text = _SECRET_HEADER_RE.sub(lambda m: m.group(1) + m.group(2) + "<凭据已隐去>", text)
    text = _SECRET_COOKIE_RE.sub(lambda m: m.group(1) + m.group(2) + "<凭据已隐去>", text)
    text = _SECRET_CTP_RE.sub(lambda m: m.group(1) + m.group(2) + "<凭据已隐去>", text)
    return text


def scrub_text(text: str, secrets: Sequence[str] = ()) -> str:
    """**模型可见文本的唯一清洗口径**:哈希、批准命令、批准页地址、凭据、本机路径。

    网关、MCP、CLI 三条通道共用这一条(第 7 轮 codex 的 F2/F4/F5:三套实现就会出现口径漂移,
    漂移的那一条就是漏的那一条)。

    `secrets` 是**按值**清洗的清单:清洗规则本来按**名字**认凭据
    (`CTP_PASSWORD=…`),可候选一旦把值单独写进异常文本(不带名字),名字那一层就认不出来
    ——按值替换是最后一道(第 11 轮 codex 的 F6:网关侧原先有,MCP 侧没有)。"""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "<凭据已隐去>")
    return redact_paths(redact_approval_urls(redact_secrets(redact_approve_commands(redact_digests(text)))))


def scrub_model_visible(payload, secrets: Sequence[str] = ()):
    """把结构化的载荷洗成「可以外发的形状」:哈希键整个去掉,其余按 `scrub_text` 洗。

    网关与 CLI 的 stdout 共用:值里的哈希(自由文本、Trace、报告)与键名里的哈希
    都要处理——键名是 `strategy_sha256`、值是 64 位十六进制,两条路都要堵。

    `secrets` 是**按值**清洗的清单(第 12 轮 codex 的 M5):三条模型可见通道
    (网关 / MCP / CLI)原来只有网关与 MCP 有这一层,CLI 的 stdout 只有「按名字」认凭据,
    候选把网关令牌**连名字一起**塞进异常文本时,网关与 MCP 会被按值盖掉、CLI 不会——
    三条通道口径不一致,漏的那一条就是漏的那一条。
    """
    if isinstance(payload, dict):
        return {k: scrub_model_visible(v, secrets) for k, v in payload.items()
                if not (isinstance(k, str) and ("sha256" in k.lower() or "checksum" in k.lower()))}
    if isinstance(payload, (list, tuple)):
        return [scrub_model_visible(v, secrets) for v in payload]
    if isinstance(payload, str):
        return scrub_text(payload, secrets=secrets)
    return payload


def _space_word(text: str, i: int) -> tuple[str, int] | None:
    """从 `i`(一个空格处)起取下一个词,返回 (词, 词尾下标);取不到返回 None。

    词里**不含中文句子终止符**:「不可写。参考」是两个词,不是一个词——
    词里带句号说明那已经是句子的另一截,再往后看只会把散文一起当成路径。
    """
    m = re.compile(r" +([^\s\\/:*?\"<>|" + _PATH_SENTENCE_PUNCT + r"]+)").match(text, i)
    if not m:
        return None
    return m.group(1), m.end()


def _segment_continues(text: str, i: int, sep: str) -> int | None:
    """判断空格后面的若干个词是不是**同一个路径段**的一部分。

    `C:\\Program Files (x86)\\app.exe`:第一个空格后面的 `Files` **并没有**紧跟分隔符
    (它后面是空格 + `(x86)\\app.exe`),只看「词后面有没有分隔符」会把整段留在外面。
    判据:从空格往后逐个词看,**在遇到句子终止符之前**能不能看到**这条路径自己的分隔符**
    ——能,这些词就都属于路径,返回「应当继续的位置」。
    分隔符由调用方给(`\\` 或 `/`):只认自己这条路径的风格,否则散文里的相对路径
    (`与 pending_strategies/cand_1.py`)会被当成路径段,连候选文件名一起吃掉
    (第 13 轮 codex 实测出来的回归);反过来,正斜杠路径
    (`C:/Documents and Settings/x.txt`)要能认出正斜杠,否则目录名整段漏在模型通道里
    (第 14 轮 codex 的 M1)。
    终止符里必须含 `:` 与 `\x00`:否则散文里的 URL(`详见 https://example.com/a/b`)
    与 URL 占位符会把后面的散文一起当成路径吃掉。
    最多看 8 个词(第 14 轮 codex 的 L6:4 个词的上限会让 `a b c d e f\\g.txt` 整段漏掉),
    免得把整段散文扫进来。
    """
    pos = i
    for _ in range(8):
        got = _space_word(text, pos)
        if not got:
            return None
        word, word_end = got
        if word_end < len(text) and text[word_end] == sep:
            return word_end                      # 这个词的后面就是分隔符 → 路径继续
        if word_end < len(text) and text[word_end] == " ":
            nxt = text[word_end:].lstrip(" ")
            if nxt and nxt[0] in _PATH_LOOKAHEAD_TERMINATORS:
                return None                       # 下一段是句子的另一截 → 空格是散文
            pos = word_end
            continue
        return None
    return None


def _looks_like_display_name(word: str, text: str, word_end: int) -> bool:
    """这个空格后面的词像不像 **OS 显示名**(`John Doe`)而不是散文词(`failed`/`has`)。

    显示名通常**首字母大写**或**含非 ASCII 字符**;散文词一律小写。
    例外:词已经是文本里最后一个词时照样吃——那时「路径到此为止」更可能是真的
    (`C:\\Users\\john doe` 后面没有别的词了),而散文(`failed to load`)
    的那个词后面还有词,不吃(第 14 轮 codex 的 L5)。
    """
    if any(ch.isupper() for ch in word) or any(ord(ch) > 127 for ch in word):
        return True
    return not text[word_end:].strip()


def _scan_windows_path(text: str, start: int, sep: str) -> int:
    """从 `start`(路径起点之后)扫描到路径**真的结束**的位置,返回结束下标。

    判据:**空格是不是路径的一部分**。四条规则,依次放宽:
      - `C:\\Users\\John Doe\\.env` → 「Doe」后面是 `\\` → 空格在路径里 → 整条盖掉;
      - `C:\\Program Files (x86)\\app.exe` → 中间还能看到分隔符 → 整段是一个路径段;
      - `C:\\Users\\alice\\Documents\\My File.txt` → `File.txt` **像文件名** → 路径继续;
      - `C:\\Users\\John Doe`(profile 目录,后面没有别的段)→ 额外多吃一个词(OS 用户名要紧);
      而 `C:\\Users\\alice\\.env failed` → 「failed」后面没有分隔符、也不是文件名
      → 空格是散文 → 路径到 `.env` 止,句子照旧保留。
    UNC(`\\\\My Server\\share\\x.txt`)与扩展路径(`\\\\?\\C:\\dir`)走同一套扫描:
    起点之后就是主机名/盘符,规则完全一样。
    `sep` 是这条路径自己的分隔符(`\\` 或 `/`):正斜杠路径用同一套规则,
    否则 `C:/Documents and Settings/x.txt` 的目录名与文件名会整段漏掉
    (第 14 轮 codex 的 M1)。
    """
    i = start
    n = len(text)
    users_extra_used = False
    while i < n:
        ch = text[i]
        if ch in "\\/":
            i += 1
            continue
        if ch == " ":
            m = _PATH_SPACE_THEN_SEP[sep].match(text, i)
            if m:                      # 下一个词后面紧跟分隔符 → 路径继续
                i = m.end()
                continue
            nxt = _segment_continues(text, i, sep)
            if nxt is not None:        # 同一段里还有分隔符 → 这些词都属于路径
                i = nxt
                continue
            got = _space_word(text, i)
            if got and _PATH_FILE_SHAPED.match(got[0]):
                i = got[1]             # 末尾那一段像文件名(`My File.txt`)→ 路径继续
                continue
            # profile 目录:`C:\Users\John Doe`——OS 用户名不能漏,
            # 这里额外允许**一个**带空格的词(再多就会把后面的散文也吃掉)。
            # 但那个词得像**显示名**(`John Doe`),不能是散文词(`failed`/`has`)
            # ——第 14 轮 codex 的 L5。
            if not users_extra_used and _PATH_USERS_TAIL.search(text, start, i):
                if got and _looks_like_display_name(got[0], text, got[1]):
                    users_extra_used = True
                    i = got[1]
                    continue
            break
        if ch in "\"'<>|\r\n" or ch in _PATH_SENTENCE_PUNCT:
            break
        i += 1
    return i


def _scan_posix_path(text: str, start: int) -> int:
    """POSIX 绝对路径的结束位置(带空格的目录名也算路径的一部分)。

    与 Windows 那套对齐:`/home/john doe/secret.txt` 是一个路径,
    「john」后面紧跟 `/` 说明空格在路径里(第 14 轮 codex 的 L7)。
    这里**只认「空格 + 一个词 + `/`」**这一种推进,不认多词段——
    POSIX 路径出现在散文里的概率更高,多词段那套判据在散文里更容易误伤。
    """
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == " ":
            m = _PATH_SPACE_THEN_SEP["/"].match(text, i)
            if m:
                i = m.end()
                continue
            break
        if ch in "\"'<>|\r\n" or ch in _PATH_SENTENCE_PUNCT:
            break
        i += 1
    return i


def _mask_posix_paths(text: str) -> str:
    """把 POSIX 绝对路径整条换成 `<路径>`(带空格的目录名也算进去)。"""
    out: list[str] = []
    pos = 0
    for m in _PATH_RE_POSIX.finditer(text):
        if m.start() < pos:
            continue
        end = _scan_posix_path(text, m.start())
        if end <= m.start():
            continue
        out.append(text[pos:m.start()])
        out.append("<路径>")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _mask_windows_paths(text: str) -> str:
    """把文本里的盘符路径、UNC、扩展路径整条换成 `<路径>`。"""
    out: list[str] = []
    pos = 0
    unc_starts = []
    for m in _PATH_START_UNC.finditer(text):
        group = m.group(0)
        # UNC 起点那一段是「反斜杠串 + 主机名 + 一个分隔符」,主机名从反斜杠串之后开始
        host_start = m.start() + (len(group) - len(group.lstrip("\\")))
        unc_starts.append((m.start(), m.end(), "unc", host_start, "\\"))
    # 盘符路径的分隔符风格由**盘符后面那个分隔符**决定:`C:\…` 是反斜杠风格,
    # `C:/…` 是正斜杠风格。两条路径各按各的风格扫(第 14 轮 codex 的 M1)。
    win_starts = [
        (m.start(), m.end(), "win", m.start(), "/" if "/" in m.group(0) else "\\")
        for m in _PATH_START_WIN.finditer(text)
    ]
    starts = sorted(unc_starts + win_starts, key=lambda item: item[0])
    for start, body_start, kind, host_start, sep in starts:
        if start < pos:                       # 已经被前一条路径盖掉了
            continue
        if kind == "unc" and _looks_like_repr_escape(text, start, host_start):
            continue                          # repr 里的 `\\n`,不是 UNC 主机名
        end = _scan_windows_path(text, body_start, sep)
        if end <= body_start:                 # 没扫到路径内容(例如 `C:` 单独出现)
            continue
        out.append(text[pos:start])
        out.append("<路径>")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _looks_like_repr_escape(text: str, start: int, host_start: int) -> bool:
    """这个「UNC 起点」是不是 repr/JSON 里的 `\\n` 转义,而不是真的 UNC 主机名。

    `{"error": "\\\\nline2\\\\nline3"}` 里的 `\\\\n` 会被 UNC 规则当成
    `\\\\主机名\\` —— 主机名取到 `nline2`,于是**后面整段正文被吃掉**
    (第 13 轮 codex 的 L4 ①:过度遮蔽同样是信息损失)。
    判据:反斜杠紧跟在引号后面(说明这是字符串字面量里的转义),
    且主机名以 `n` 开头(`\\n` 是转义里唯一会以 `n` 开头的那个)。
    真 UNC(`\\\\nas\\share`)前面是空格或行首,主机名不以 `n` 开头时不受影响。
    """
    if start == 0:
        return False
    if text[start - 1] not in "\"'":
        return False
    # repr/JSON 里的换行转义只有**两个**反斜杠;UNC 的 repr/JSON 形态
    # 是四个反斜杠 + 主机名。不按个数区分,主机名以 n 开头的 UNC
    # (nas / net …)就整条漏盖(第 16 轮 codex 的 H1)。
    if host_start - start > 2:
        return False
    return host_start < len(text) and text[host_start] == "n"


def redact_paths(text: str) -> str:
    """把文本里的本机绝对路径替换成 `<路径>`。

    为什么要有这一层:错误消息里常常夹着本机路径(`批准记录被其他进程占用,拒绝写入:
    C:\\...\\approvals.json`)。这些消息会经网关响应回到模型——模型看到本机目录结构
    没有任何用处,却多了一条可用于指纹识别的信息。

    URL 原样保留(那是诊断信息,不是本机结构)。
    """
    if not text:
        return text
    urls: list[str] = []

    def stash(match: re.Match) -> str:
        url = match.group(0)
        # `file://` 后面跟的就是**本机绝对路径**——不能像别的 URL 那样原样保护
        # (第 13 轮 codex 的 L1:`file:///C:/Users/alice/.env` 整条原样出去)。
        # 这一类直接盖成 `<路径>`。
        if url.lower().startswith("file:"):
            return "<路径>"
        urls.append(url)
        return f"\x00{len(urls) - 1}\x00"

    text = _URL_RE.sub(stash, text)
    text = _mask_windows_paths(text)
    text = _PATH_RE_POSIX_ROOT.sub("<路径>", text)
    text = _mask_posix_paths(text)
    return re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], text)


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


def _folded_str(node: ast.AST) -> str | None:
    """把**常量拼接**折叠成一个字符串常量;折不出来就返回 None。

    第 12 轮 codex 的 M4:`ast` 只折叠**相邻**字面量,`'{0.__gl' + 'obals__}'`
    是两个独立 Constant,于是「字符串/模板形态都拦」这条规则**逐常量**判定、
    一行拼接即绕过。现在先把 `BinOp(Add)` 递归折叠,再走等值 + 子串判定。

    第 13 轮 codex 的 L3:只折 `+` 还不够,`"".join([...])` 与 `bytes` 字面量
    也照样绕过(`b"__globals__"`、`"".join(["__gl","obals__"])`)。这两种也折。

    第 14 轮 codex 的 L9:再补三种常见拼法——**f-string**(`f"__gl{chr(95)}obals__"` 的
    常量部分)、**`%` 格式化**(`"%s%s" % ("__gl","obals__")`)、**`str.format`**
    (`"{0}{1}".format("__gl","obals__")`)。分隔符是**模块级常量**的那种
    (`SEP.join([...])`)折不出来(要模块级的常量表),记为已知残留。
    注意:这一层只是**纵深防御**——即便折不出来,「按键名取东西」的原语
    (`getattr`/`vars`/`attrgetter`/`__getattribute__`)在静态层与运行期都已被封住,
    没有可利用的绕过路径。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return node.value.decode("latin-1", errors="ignore")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _folded_str(node.left)
        right = _folded_str(node.right)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        # `"%s%s" % (a, b)`:格式串与实参都是常量时折出来。
        template = _folded_str(node.left)
        if template is not None and isinstance(node.right, (ast.Tuple, ast.List)):
            parts = [_folded_str(elt) for elt in node.right.elts]
            if parts and all(p is not None for p in parts):
                try:
                    return template % tuple(parts)
                except (TypeError, ValueError):
                    return None
    if isinstance(node, ast.JoinedStr):
        # f-string:常量部分拼起来;带表达式的部分折不出来就整体放弃。
        chunks: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                chunks.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                folded = _folded_str(value.value)
                if folded is None:
                    return None
                chunks.append(folded)
            else:
                return None
        return "".join(chunks)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "join" and len(node.args) == 1:
        sep = _folded_str(node.func.value)
        arg = node.args[0]
        if sep is not None and isinstance(arg, (ast.List, ast.Tuple)):
            parts = [_folded_str(elt) for elt in arg.elts]
            if parts and all(p is not None for p in parts):
                return sep.join(parts)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "format" and node.func.value is not None:
        template = _folded_str(node.func.value)
        if template is not None:
            args = [_folded_str(arg) for arg in node.args]
            if all(arg is not None for arg in args):
                try:
                    return template.format(*args)
                except (IndexError, KeyError, ValueError):
                    return None
    return None


def screen_source(source: str, extra_modules: Sequence[str] = (), extra_calls: Sequence[str] = (),
                 extra_allowed: Sequence[str] = ()) -> list[str]:
    """静态筛查:返回发现的可疑构造(中文说明);空列表表示未发现。**不是安全保证。**

    extra_* 来自配置:禁用项只能**追加**,白名单也只能**追加**,不能删减内置的。"""
    forbidden_modules = FORBIDDEN_MODULES | set(extra_modules)
    forbidden_calls = FORBIDDEN_CALLS | set(extra_calls)
    allowed_roots = ALLOWED_MODULE_ROOTS | set(extra_allowed)
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
    # 导入进来的**名字**(模块别名):用来判「X.os」这种形态。
    imported_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported_aliases.add(alias.asname or alias.name)
    # 链式属性:`a.b.c` 的根是 `a`、段是 [b, c]。
    # 第 10 轮 codex 的 Critical:只看「X.os」这一层不够——
    # `enum.sys.modules["os"].system(...)` 的危险段在**中间**,
    # `random._os.system(...)` 的危险段是 `_os`。这里按**任意深度**取段。
    def _chain(node: ast.AST) -> tuple[str | None, list[str]]:
        segments: list[str] = []
        current = node
        while isinstance(current, ast.Attribute):
            segments.append(current.attr)
            current = current.value
        root = current.id if isinstance(current, ast.Name) else None
        return root, segments

    reported: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            names = []
        for name in names:
            root = name.split(".")[0]
            if root in forbidden_modules:
                findings.append(f"第 {node.lineno} 行: 禁止导入 {name}")
            elif root and root not in allowed_roots:
                # 白名单之外的导入一律报告:运行时的受限导入器同样会拒(第 7 轮 codex 的 F1)。
                findings.append(f"第 {node.lineno} 行: 未列入白名单的导入 {name}"
                                "(研究沙盒只放行纯计算类模块)")
        # `from X import os` / `from X import subprocess`:直接就把危险入口拿成裸名。
        # 之前的规则只看属性取用(`X.os`),这一形态整个漏掉(第 10 轮 codex 的 High:
        # `from zipfile import os; os.system(...)` 零告警且真的执行了命令)。
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in LEAKY_MODULE_ATTRS:
                    findings.append(f"第 {node.lineno} 行: 禁止从 {node.module or '?'} 导入 {alias.name}"
                                    "(该名字是 os/subprocess 之类的能力入口)")
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
        elif (isinstance(node, ast.Attribute) and node.attr.startswith("_")
                and node.attr.lstrip("_").lower() in DANGEROUS_UNDERSCORE_ATTRS):
            # 私有名藏能力入口(`numpy._module`):运行期取不到,静态层也要报。
            #
            # **只对「属性链的根是导入进来的模块」生效**(第 12 轮 codex 的 L1):
            # 运行期的模块代理只拦**模块**属性,`self._state` 在运行期完全正常;
            # 静态层原先一刀切,把候选自己的 `self._state` / `self._file` 也拒了——
            # 那正是 LLM 写策略时最常见的私有字段名。口径要与运行期一致。
            _root, _segments = _chain(node)
            if _root is not None and _root in imported_aliases:
                findings.append(f"第 {node.lineno} 行: 禁止访问 {node.attr}")
        # 从导入进来的模块上取「危险模块」:`zipfile.os.system(...)`、`enum.sys.modules[...]`。
        # 有些模块自己 import 了 os/sys 并挂在属性上,不按这一形态拦就等于把 os 送出去
        # (第 7 轮 codex 的 F1;第 10 轮 codex 的 Critical:必须按**任意深度**取段,
        #  `enum.sys` / `random._os` 都是真模块)。
        if isinstance(node, ast.Attribute):
            root, segments = _chain(node)
            if root is not None and root in imported_aliases:
                hit = root if root in LEAKY_MODULE_ATTRS else next(
                    (s for s in segments if s in LEAKY_MODULE_ATTRS), None)
                if hit is not None and (node.lineno, root) not in reported:
                    reported.add((node.lineno, root))
                    findings.append(f"第 {node.lineno} 行: 禁止访问模块 {root} 上的 {hit}"
                                    "(该模块把 os/sys/subprocess 之类挂在属性上)")
        # 属性名当字符串用:operator.attrgetter('__dict__')(self) 能绕开"禁止访问 __dict__"的
        # 属性写法(调用点看不出访问的是什么)。这些名字作为字符串出现时一律拒绝。
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN_STRINGS:
                findings.append(f"第 {node.lineno} 行: 禁止引用 {node.value} 字符串")
            else:
                hit = next((s for s in FORBIDDEN_SUBSTRINGS if s in node.value), None)
                if hit is not None:
                    findings.append(f"第 {node.lineno} 行: 禁止引用含 {hit} 的字符串")
        # **常量拼接**出来的字符串同样要判(第 12 轮 codex 的 M4):
        # `'{0.__gl' + 'obals__}'` 这种一行拼接,逐常量判是看不见的——
        # 而这条规则的全部价值就是「人批准前能看见告警」。
        # 第 13 轮 codex 的 L3:`bytes` 字面量与 `"".join([...])` 也折进来。
        elif (isinstance(node, ast.BinOp) or isinstance(node, ast.Call)
              or (isinstance(node, ast.Constant) and isinstance(node.value, bytes))):
            folded = _folded_str(node)
            if folded is not None:
                if folded in FORBIDDEN_STRINGS:
                    findings.append(f"第 {node.lineno} 行: 禁止引用 {folded} 字符串")
                else:
                    hit = next((s for s in FORBIDDEN_SUBSTRINGS if s in folded), None)
                    if hit is not None:
                        findings.append(f"第 {node.lineno} 行: 禁止引用含 {hit} 的字符串")
    # 配置追加的白名单模块:**准入审计放在运行期的导入器里**
    # (`safe_builtins` → `restricted_import`:导进来就审计,自己就把 os/shutil 挂在
    #  属性上的模块直接拒绝)。静态层**不做**这件事——它需要动态导入才能看到模块属性,
    # 而动态导入会让本模块被宪法守卫判成「执行候选的入口」(要求它自己走第四层),
    # 与本模块的职责不符。候选代码侧的效果不变:配置放行的模块自己泄漏能力时,
    # 静态层照样按属性链报违规(上面的任意深度属性链规则),运行期则整个拒绝导入。
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
    # 窗口常量取自 segment_windows(纯数据):**不 import optimize**——
    # optimize 会 import bootstrap → vnpy,研究侧为了查一段日期拖进整个交易栈,
    # 连 CI 上不装交易环境的研究侧闸门都收集不了(第 7 轮 codex 的 C1/H1)。
    import segment_windows
    windows = segment_windows.windows_for(key)
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
# 批准记录有**两个写者**,都是本机进程、都带人工门槛(第 6 轮 codex 的 M7):
#   1) `scripts/research_approval_server.py`(带外批准页):人在本机网页里读代码、核对 sha256 后确认;
#   2) `research_api.py approve/review`(本机 CLI):人在终端里确认。
# 两边都**不能**被模型通道触达:毕昇工作流/网关/MCP 都没有写批准记录的端点,
# MCP 的「请求批准」只负责拉起批准进程,自己一个字都不写。
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


def record_approval(path: Path, approvals_file: Path, now: datetime | None = None, sha256: str | None = None) -> str:
    """记一次人工批准,返回被批准的哈希。

    `sha256`:调用方**已经核对过**的那份哈希。传了就直接拿它当键,不再重读文件——
    否则"人核对的内容"与"记下来的内容"之间会有时间差(TOCTOU:两次读之间文件被改,
    记下的其实是另一个版本的哈希)。不传才自己算(给不需要预读哈希的调用点用)。

    写入用「独占锁 + 唯一临时名 + 原子替换」:两个批准进程同时提交时不会互相覆盖
    (先读后写的丢更新),也不会出现半截文件。
    """
    approvals_file = Path(approvals_file)
    sha = str(sha256).strip().lower() if sha256 else file_sha256(path)
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise SandboxError(f"批准的哈希格式不对(应为 64 位小写十六进制): {sha!r}")
    # 传进来的哈希必须**真的是这份文件当前内容的哈希**(第 7 轮 codex 的 F12):
    # 「低层唯一写入者」是信任边界,它不该替调用方担保——调用方算错了、或者拿错了另一份
    # 内容的哈希,记下来的批准就绑在了没被人看过的字节上。
    try:
        actual = file_sha256(path)
    except OSError as exc:
        raise SandboxError(f"策略文件无法读取,拒绝记录批准: {exc}") from exc
    if actual != sha:
        raise SandboxError("批准记录的哈希与文件当前内容不一致,拒绝记录(文件可能已被改动)")
    approvals_file.parent.mkdir(parents=True, exist_ok=True)
    with _approvals_lock(approvals_file):
        data = _load_approvals(approvals_file)
        data.setdefault(sha, {"name": Path(path).name, "at": (now or datetime.now()).isoformat(timespec="seconds")})
        tmp = approvals_file.with_name(
            f"{approvals_file.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            _atomic_replace(tmp, approvals_file)
        except OSError as exc:
            # 写不进去必须变成"拒绝"而不是裸异常:调用方(批准页)只认 SandboxError,
            # 裸 OSError 会一路冒到 HTTP handler,浏览器只看到连接被重置(第 8 轮 codex 的 M2)。
            raise SandboxError(f"批准记录写入失败: {exc}") from exc
        finally:
            # 临时名**带随机后缀**:只带 pid 的话,同一进程里两次并发批准会用同一个
            # 临时文件,Windows 上就是「另一个进程正占用这个文件」→ PermissionError,
            # 批准直接失败(第 9 轮 codex 的 M3 的并发用例把它逼出来了)。
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass          # 删不掉只是个残留文件,不该把这次批准变成失败
    return sha


def _atomic_replace(tmp: Path, target: Path, attempts: int = 5) -> None:
    """原子替换,Windows 上短暂占用(别的进程正开着目标文件)时重试几次。

    `os.replace` 在 Windows 上遇到目标文件被占用会抛 PermissionError(WinError 5):
    状态页/MCP 偶尔正读着 approvals.json。重试几次通常就过去了。
    """
    for index in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError:
            if index == attempts - 1:
                raise
            time.sleep(0.1 * (index + 1))


def _pid_alive(pid: int) -> bool:
    """进程还在不在。

    **不能**用 `os.kill(pid, 0)`:Windows 上 CPython 把它实现成 TerminateProcess(pid, 0)
    ——那会把别人的进程杀掉。
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


def _lock_is_stale(lock_path: Path) -> bool:
    """锁是不是**残留**的(持有者已经不在了)。

    进程崩了不会清锁文件:不认残留锁的话,一次崩溃就会让**之后所有**批准永久失败
    (第 8 轮 codex 的 M1)。锁文件里写着持有者的 PID 与时间戳。
    """
    try:
        content = lock_path.read_text(encoding="ascii", errors="ignore").split()
        pid = int(content[0]) if content else -1
        age = time.time() - lock_path.stat().st_mtime
    except (OSError, ValueError):
        return False
    if age > LOCK_STALE_SECONDS:
        return True
    return pid > 0 and not _pid_alive(pid)


def _unlink_quiet(path: Path, attempts: int = 10) -> bool:
    """删文件;Windows 上别的进程正开着它时重试几次。

    删不掉就留着(比如别的进程正读着它):留着只是残留,不像**误删**那样危险。
    但释放锁时必须重试到位——锁文件留着 = 批准记录被锁住,之后所有批准都失败。
    """
    for index in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except OSError:
            time.sleep(0.02 * (index + 1))
    return False


@contextlib.contextmanager
def _approvals_lock(approvals_file: Path, timeout: float = 5.0):
    """批准记录的跨进程独占锁(锁文件 + O_EXCL)。

    拿不到锁就**失败**,不等待也不绕过:批准记录写错一条就是"没批准却生效"。
    残留锁(持有者已崩)会被识别并抢走——否则一次崩溃就让批准永久瘫痪。
    """
    lock_path = approvals_file.with_name(approvals_file.name + ".lock")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {time.time():.3f}\n".encode("ascii"))
            finally:
                os.close(fd)
            break
        except FileExistsError:
            if _lock_is_stale(lock_path):
                _unlink_quiet(lock_path)            # 抢锁:持有者已经不在了
                continue
            if time.monotonic() >= deadline:
                raise SandboxError(f"批准记录被其他进程占用,拒绝写入: {approvals_file}")
            time.sleep(0.05)
    try:
        yield
    finally:
        # 释放锁必须重试:Windows 上有别的线程正读着锁文件时,删会失败
        # (WinError 32)。删不掉 = 锁留着 = 之后所有批准都被锁住
        # (第 9 轮 codex 的 M3 的并发用例把它逼出来了)。
        _unlink_quiet(lock_path)


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
