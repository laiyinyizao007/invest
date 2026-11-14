"""边界负向测试:批准记录只有一个写入者、DSH Preset 不挂危险工具、工具目录恰为三项。

对应 ADR 20261113-dsh-research-entry-and-out-of-band-approval 的「验证(实现期)」。
"""
import ast
import functools
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_DIR = ROOT / "tools" / "research_mcp"
PRESET = MCP_DIR / "dsh-research-preset.patch.yml"
APPROVAL_SCRIPT = ROOT / "scripts" / "research_approval_server.py"

# DSH 里**不允许**出现在 research Preset 的工具(ADR 第 3 条)
FORBIDDEN_IN_PRESET = (
    "tool-pwsh", "tool-bash", "tool-fs", "tool-fs-search", "tool-jobs", "tool-skill",
    "skill-filesystem", "dsh-tool-cordis", "dsh-tool-subagent", "tool-web",
    "dsh-experimental-auto-review",
)


def _read_source(path: Path) -> str:
    """读源码:容忍 UTF-8 BOM(Windows 上别的工具存过的文件),语法错误仍然直接报错。"""
    return path.read_text(encoding="utf-8-sig")


@functools.lru_cache(maxsize=1)
def _git_python_files() -> list[Path] | None:
    """git 视角的 .py 清单:已入库 + 未入库但没被 ignore。

    遍历整个文件系统会把测试夹具目录(reviewtmp/ 等,已 gitignore)里的临时 .py
    也扫进来:那些文件是别的测试跑出来的半成品(带 BOM、内容随机),会让这条
    边界断言变成随机失败(第 9 轮实跑撞到过一次)。`--others
    --exclude-standard` 正好是"入库 + 未入库未忽略"这一集,所以**新写的、还没
    提交的生产代码仍然会被扫到**,不会因为没入库就漏掉写入者。

    **根目录下以 `.` 开头的未入库目录不算仓库代码**(第 14 轮 codex 的 L2):
    评审脚手架目录(`.codex_tmp_r14/`、`.reviewtmp2/` 之类)**不保证被 .gitignore 覆盖**,
    里面若有写批准记录的生产代码,这条断言会去盯一个临时路径——CI 上的报错
    指向临时文件,人根本不知道该改哪里。判据与守卫(`tools/check_constitution_history.py`)
    一致:「**未入库** + **顶层目录名以 `.` 开头** + 该顶层目录里没有任何入库文件」。
    """
    try:
        done = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--", "*.py"],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=120,
        )
        others = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard", "--", "*.py"],
            cwd=ROOT, capture_output=True, text=True, check=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0 or others.returncode != 0:
        return None
    tracked = [rel for rel in done.stdout.split("\0") if rel]
    tracked_top = {rel.split("/", 1)[0] for rel in tracked}
    out: list[Path] = []
    seen: set[str] = set()
    for rel in tracked:
        path = ROOT / Path(*rel.split("/"))
        if path.is_file():
            out.append(path)
            seen.add(rel)
    for rel in [r for r in others.stdout.split("\0") if r and r not in seen]:
        top = rel.split("/", 1)[0]
        if top.startswith(".") and top not in tracked_top:
            continue                      # 未入库的隐藏目录:评审脚手架,不是仓库代码
        path = ROOT / Path(*rel.split("/"))
        if path.is_file():
            out.append(path)
    return out


def _repo_files() -> list[Path]:
    """仓库里的 .py(排除虚拟环境、缓存、本机私有目录与测试夹具)。"""
    tracked = _git_python_files()
    if tracked is not None:
        # git 视角已经天然排除了被 ignore 的夹具目录,这里只补一条 tests 根目录排除。
        out = []
        for path in tracked:
            try:
                rel = path.relative_to(ROOT).parts
            except ValueError:
                continue
            if rel and rel[0] == "tests":   # 测试里造批准记录是夹具,不是生产写入路径
                continue
            out.append(path)
        return out
    # 没有 git 时的退路:遍历文件系统,按目录名排除夹具目录。
    out = []
    for path in ROOT.rglob("*.py"):
        parts = path.parts
        if ".venv" in parts or "__pycache__" in parts or ".git" in parts or "node_modules" in parts:
            continue
        if ".local" in parts:                # 本机私有状态目录(已 gitignore),不入库不算生产路径
            continue
        if "reviewtmp" in parts:             # 测试跑出来的临时目录(已 gitignore)
            continue
        # 只排除**仓库根下**的 tests 目录:按目录名过滤会把任何一层叫 "tests" 的
        # 目录整块排除掉,那里要是藏着写批准记录的生产代码,这条断言就看不见
        # (第 9 轮 codex 的 L2)。
        try:
            rel = path.relative_to(ROOT).parts
        except ValueError:
            continue
        if rel and rel[0] == "tests":       # 测试里造批准记录是夹具,不是生产写入路径
            continue
        out.append(path)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---------- 批准记录的写入者 ----------

def _const_str(node) -> str | None:
    """能常量求值的字符串(常量本身,或两个常量的拼接)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _const_str(node.left), _const_str(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _module_aliases(tree: ast.AST) -> set[str]:
    """绑到 research_sandbox 模块本身的别名:`import research_sandbox as rs`。"""
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "research_sandbox":
                    out.add(alias.asname or alias.name)
    return out


def _module_dict_aliases(tree: ast.AST, module_aliases: set[str]) -> set[str]:
    """被赋值成「模块的字典」的变量名(第 14 轮 codex 的 M2)。

    `_D = rs.__dict__` 之后 `_D["record_approval"](...)` 照样写得成账本——
    原先只认「直接下标 / 直接方法调用」两种形态,**先把字典存进变量再取**整条漏掉。
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "__dict__" \
                and isinstance(value.value, ast.Name) and value.value.id in module_aliases:
            out.add(target.id)
        elif isinstance(value, ast.Call) and _callee_name(value.func) == "vars" \
                and any(isinstance(child, ast.Name) and child.id in module_aliases
                        for child in ast.walk(value)):
            out.add(target.id)
        elif isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr in {"copy", "items", "keys", "values"} \
                and isinstance(value.func.value, ast.Attribute) \
                and value.func.value.attr == "__dict__" \
                and isinstance(value.func.value.value, ast.Name) \
                and value.func.value.value.id in module_aliases:
            out.add(target.id)
        elif isinstance(value, ast.Call) and _callee_name(value.func) == "dict" \
                and any(isinstance(child, ast.Attribute) and child.attr == "__dict__"
                        and isinstance(child.value, ast.Name) and child.value.id in module_aliases
                        for child in ast.walk(value)):
            # `dict(rs.__dict__.items())`:把模块字典拷一份出来再下标调用
            # (第 14 轮 codex 的 M2 变体)。
            out.add(target.id)
    return out


def _record_approval_call_lines(tree: ast.AST) -> list[int]:
    """源码里所有**真正指向 research_sandbox.record_approval** 的调用点(行号)。

    认得这几种写法(第 6 轮 codex 的 B/M:只认函数名,换个名字就能写账本):
      - `rs.record_approval(...)`
      - `from research_sandbox import record_approval` 之后的 `record_approval(...)`
      - `from research_sandbox import record_approval as ra` 之后的 `ra(...)`
      - `getattr(rs, "record_approval")(...)`、`getattr(rs, "record_" + "approval")(...)`
      - `rs.__dict__["record_approval"](...)`(第 9 轮 codex 的 F11)
      - `rs.__dict__.get("record_approval")(...)`(第 13 轮 codex 的 M2)
      - `_D = rs.__dict__` 之后的 `_D["record_approval"](...)`(第 14 轮 codex 的 M2)
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("research_sandbox"):
            for alias in node.names:
                if alias.name == "record_approval":
                    aliases.add(alias.asname or alias.name)
    module_aliases = _module_aliases(tree)
    dict_aliases = _module_dict_aliases(tree, module_aliases)
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = None
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Call) and isinstance(func.func, ast.Name) and func.func.id == "getattr" \
                and len(func.args) > 1:
            name = _const_str(func.args[1])
        elif isinstance(func, ast.Subscript):
            # `rs.__dict__["record_approval"](...)`:绕过 getattr,换个门进来
            # (第 9 轮 codex 的 F11)。
            value = func.value
            if isinstance(value, ast.Attribute) and value.attr == "__dict__":
                name = _const_str(func.slice)
            elif isinstance(value, ast.Name) and value.id in dict_aliases:
                # `_D = rs.__dict__` 之后的 `_D["record_approval"](...)`
                # (第 14 轮 codex 的 M2)。
                name = _const_str(func.slice)
        elif (isinstance(func, ast.Call) and isinstance(func.func, ast.Attribute)
              and func.func.attr in {"get", "pop", "setdefault"}
              and isinstance(func.func.value, ast.Attribute)
              and func.func.value.attr == "__dict__"):
            # `rs.__dict__.get("record_approval")(...)`:同一扇门,换个取法
            # (第 13 轮 codex 的 M2)。
            name = _const_str(func.args[0]) if func.args else None
        if name == "record_approval" or name in aliases:
            hits.append(node.lineno)
    return hits


def _record_approval_calls(path: Path) -> list[str]:
    """(文件, 行号) 形式的批准记录写入点。"""
    tree = ast.parse(_read_source(path))     # 解析失败就报错,不许静默跳过
    return [f"{_rel(path)}:{lineno}" for lineno in _record_approval_call_lines(tree)]


def _callee_name(func: ast.AST) -> str | None:
    """一个调用目标的**名字**:`getattr` / `operator.attrgetter(...)(x)` 都能取到。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Call):
        return _callee_name(func.func)
    return None


# 「按名字取东西」的内建:传字符串进去的那个参数**就是名字本身**。
# 别的调用(getattr 之外的 print / logger.info / ValueError)传字符串只是**内容**,
# 不构成能力(第 12 轮 codex 实测的假阳性)。
_NAME_FETCHING_CALLS = frozenset({"getattr", "vars", "dir", "attrgetter"})


def _is_module_dict_subscript(node: ast.Subscript, module_aliases: set[str],
                              dict_aliases: set[str] | None = None) -> bool:
    """下标取的是不是「模块的字典」:`rs.__dict__[…]` / `vars(rs)[…]` / `_D[…]`。"""
    value = node.value
    if isinstance(value, ast.Attribute) and value.attr == "__dict__":
        base = value.value
        return isinstance(base, ast.Name) and base.id in module_aliases
    if isinstance(value, ast.Call) and _callee_name(value.func) == "vars":
        return any(isinstance(child, ast.Name) and child.id in module_aliases
                   for child in ast.walk(value))
    if dict_aliases and isinstance(value, ast.Name) and value.id in dict_aliases:
        return True
    return False


# 取模块字典的方法:字典里装的就是模块的全部属性,取出来就是拿到了写入函数
# (第 13 轮 codex 的 M2:`rs.__dict__.get("record_approval")` 原先整条漏掉)。
_MODULE_DICT_METHODS = frozenset({"get", "items", "values", "keys", "pop", "setdefault"})


def _is_module_dict_method_call(node: ast.Call, module_aliases: set[str],
                                dict_aliases: set[str] | None = None) -> bool:
    """`rs.__dict__.get("record_approval")` / `rs.__dict__.items()` 这类取用。"""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in _MODULE_DICT_METHODS):
        return False
    value = func.value
    if isinstance(value, ast.Attribute) and value.attr == "__dict__" \
            and isinstance(value.value, ast.Name) and value.value.id in module_aliases:
        return True
    return bool(dict_aliases and isinstance(value, ast.Name) and value.id in dict_aliases)


def _record_approval_reference_lines(tree: ast.AST) -> list[int]:
    """源码里**取到** record_approval 但**没有直接调用**的地方(行号)。

    「拿到函数引用」本身就已构成写批准记录的能力:
    `writer = rs.record_approval` 之后由框架代为调用,调用点根本不在这个文件里
    (第 10 轮 codex 的 M4)。

    第 11 轮 codex 的 F7 补了两头:
      - **假阳性**:原先**任何**含 `record_approval` 子串的字符串常量都算「持有」,
        错误提示与文档串一律误判 → 误报一多,这条断言迟早被人加白名单绕行。
        现在只在**取用语境**(调用实参 / 下标)里才算,文档串、字典键、赋值都不算。
      - **假阴性**:`getattr(rs, "record_" + chr(95) + "approval")` 这类
        **非常量折叠**的取法按名字看不见 → 现在按**能力**判定:
        只要对 research_sandbox 模块做「按名字取属性」(getattr/vars/dir/attrgetter),
        一律算持有(不指望名字对得上)。"""
    aliases: set[str] = set()           # from research_sandbox import record_approval as x
    module_aliases: set[str] = _module_aliases(tree)   # import research_sandbox as rs
    star_imported: bool = False         # from research_sandbox import *
    docstrings = _docstring_ids(tree) if isinstance(tree, ast.Module) else set()
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("research_sandbox"):
            for alias in node.names:
                if alias.name == "record_approval":
                    aliases.add(alias.asname or alias.name)
                elif alias.name == "*":
                    # `from research_sandbox import *`:之后裸写的 `record_approval`
                    # 就是那个写入函数(第 13 轮 codex 的 M2)。
                    star_imported = True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "research_sandbox":
                    module_aliases.add(alias.asname or alias.name)
    # **动态导入**:`rs = importlib.import_module("research_sandbox")` 与
    # `rs = __import__("research_sandbox")` —— 模块名是运行期才知道的,
    # 只扫 import 语句看不见(第 12 轮 codex 实测的假阴性:
    # `getattr(动态导入的模块, "record_approval")` 整条漏掉)。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Call):
            continue
        if _callee_name(node.value.func) not in {"import_module", "__import__"}:
            continue
        arg = node.value.args[0] if node.value.args else None
        if _const_str(arg) == "research_sandbox":
            module_aliases.add(target.id)
    # 「模块字典」的变量别名(`_D = rs.__dict__`):先把字典存进变量再取,
    # 原先整条漏掉(第 14 轮 codex 的 M2)。
    dict_aliases = _module_dict_aliases(tree, module_aliases)
    called: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            called.add(id(node.func))
    hits: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "record_approval":
            if id(node) not in called:
                hits.append(node.lineno)
        elif isinstance(node, ast.Name) and (node.id in aliases
                                            or (star_imported and node.id == "record_approval")):
            if id(node) not in called:
                hits.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            folded = _const_str(node)
            if folded is None or "record_approval" not in folded:
                continue
            # 只在「按名字取东西」的语境里算持有:
            #   - **取名调用**的实参(getattr(x, "…")/vars(x)/dir(x)/attrgetter("…"));
            #   - **模块字典**的下标(rs.__dict__["…"] / vars(rs)["…"])。
            # 字典键、赋值、错误提示、日志与打印里的字面量**都不算**
            # (第 12 轮 codex 实测:`raise ValueError("record_approval 写入失败")`、
            #  `logger.info("record_approval 已写")`、`print("record_approval")`
            #  三种原先都被误判成「持有写入函数」——误报一多,这条断言迟早被人
            #  加白名单绕行)。
            parent = parents.get(id(node))
            if isinstance(parent, ast.Call) and node in parent.args \
                    and _callee_name(parent.func) in _NAME_FETCHING_CALLS:
                hits.append(node.lineno)
            elif isinstance(parent, ast.Subscript) \
                    and _is_module_dict_subscript(parent, module_aliases, dict_aliases):
                hits.append(node.lineno)
    # 能力型判定:对 research_sandbox 模块「按名字取属性」的一律算持有。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _callee_name(node.func) not in _NAME_FETCHING_CALLS:
            continue
        if any(isinstance(child, ast.Name) and child.id in module_aliases
               for child in ast.walk(node)):
            hits.append(node.lineno)
    # 能力型判定之二:直接取模块的字典(`rs.__dict__[…]`),哪怕下标里的名字是
    # 折出来的(`'record_' + chr(95) + 'approval'`)——**取字典就是取它的全部属性**。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        if _is_module_dict_subscript(node, module_aliases, dict_aliases):
            hits.append(node.lineno)
    # 能力型判定之三:**用方法**取模块的字典(`rs.__dict__.get("record_approval")`、
    # `rs.__dict__.items()`)——字典里装的就是模块的全部属性,取出来就拿到了写入函数
    # (第 13 轮 codex 的 M2:原先只认下标形态,`.get()` 整条漏掉)。
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) \
                and _is_module_dict_method_call(node, module_aliases, dict_aliases):
            hits.append(node.lineno)
    # 能力型判定之四:**取模块的字典本身就是「持有写入函数」**——
    # `_D = rs.__dict__` 之后 `_D["record_approval"](...)` 照样写得成账本
    # (第 14 轮 codex 的 M2:原先只认「直接下标 / 直接方法调用」,
    #  先把字典存进变量再取,整条漏掉)。
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "__dict__" \
                and isinstance(node.value, ast.Name) and node.value.id in module_aliases:
            hits.append(node.lineno)
    return sorted(set(hits))


def test_the_approval_reference_detector_sees_string_indirect_access():
    """判定器自己的对照用例:字符串形态的间接取用也要被看见(第 10 轮 codex 的 M2)。

    只查 `X.record_approval` 与 `from … import record_approval` 的话,
    `getattr(rs, "record_approval")` 与 `rs.__dict__["record_approval"]` 两种
    写法会**整条漏掉**——而这两种写法都能拿到同一个写入函数。"""
    must_catch = {
        "属性": "import research_sandbox as rs\nw = rs.record_approval\n",
        "别名": "from research_sandbox import record_approval\nw = record_approval\n",
        "getattr": "import research_sandbox as rs\nw = getattr(rs, 'record_approval')\n",
        "常量折叠": "import research_sandbox as rs\nw = getattr(rs, 'record_' + 'approval')\n",
        "dunder": "import research_sandbox as rs\nw = rs.__dict__['record_approval']\n",
    }
    for name, source in must_catch.items():
        assert _record_approval_reference_lines(ast.parse(source)), name
    assert not _record_approval_reference_lines(
        ast.parse("import math\nw = math.sqrt\n")), "无关的代码不该被判成持有写入函数"


def test_the_approval_reference_detector_does_not_flag_plain_mentions():
    """**假阳性**对照:错误提示、文档串、字典键里提到 record_approval **不算**持有
    (第 11 轮 codex 的 F4)。

    原先任何含该名字的字符串常量都被算成「持有写入函数」——第三个文件里写一句
    `"record_approval 写入失败"` 就会把断言搞红;误报一多,这条断言迟早被人
    加白名单绕行(上一轮已经犯过一次同类错)。"""
    assert not _record_approval_reference_lines(
        ast.parse("msg = 'record_approval 写入失败'" + "\n")), "错误提示不该被判成持有写入函数"
    assert not _record_approval_reference_lines(
        ast.parse('"""本模块负责 record_approval 的调用。"""' + "\n")), "文档串不该被判成持有写入函数"
    assert not _record_approval_reference_lines(
        ast.parse("d = {'record_approval': 1}" + "\n")), "字典键不该被判成持有写入函数"


def test_the_approval_reference_detector_sees_dynamic_lookups():
    """**非常量折叠**的取法也要被看见(第 11 轮 codex 的 F7)。

    名字对不上不代表取不到:`getattr(rs, "record_" + chr(95) + "approval")`
    运行时取到的就是同一个写入函数。现在按**能力**判定:对 research_sandbox 模块
    做「按名字取属性」(getattr/vars/dir/attrgetter)一律算持有。"""
    for source in (
        "import research_sandbox as rs" + "\n" + "w = getattr(rs, 'record_' + chr(95) + 'approval')" + "\n",
        "import research_sandbox as rs" + "\n" + "w = vars(rs)['record_approval']" + "\n",
        "import operator" + "\n" + "import research_sandbox as rs" + "\n"
        + "w = operator.attrgetter('record_approval')(rs)" + "\n",
    ):
        assert _record_approval_reference_lines(ast.parse(source)), source


def test_the_approval_reference_detector_does_not_flag_mentions_inside_calls():
    """**假阳性**对照:错误提示 / 日志 / 打印里提到 record_approval **不算**持有。

    第 12 轮 codex 实测的三种误报:`raise ValueError("record_approval 写入失败")`、
    `logger.info("record_approval 已写")`、`print("record_approval")`——
    字符串在这三处只是**内容**,不是「按名字取东西」。
    判定器原先把「字符串出现在任何调用实参里」都算成持有,于是仓库里
    写一句日志就能把这条边界断言弄红——误报一多,这条断言迟早被人加白名单绕行。"""
    for source in (
        "raise ValueError('record_approval 写入失败')\n",
        "logger.info('record_approval 已写')\n",
        "print('record_approval')\n",
        "emit('record_approval')\n",
    ):
        assert not _record_approval_reference_lines(ast.parse(source)), source


def test_the_approval_reference_detector_sees_folded_module_dict_lookups():
    """**假阴性**对照:下标里的名字是折出来的,也要看见。

    `rs.__dict__['record_' + chr(95) + 'approval']` 里没有一个完整常量,
    按「字符串常量等于禁用名」根本看不见——但**取模块字典就是取它的全部属性**,
    所以这一整类(`rs.__dict__[…]`、`vars(rs)[…]`)按**能力**判。"""
    assert _record_approval_reference_lines(ast.parse(
        "import research_sandbox as rs\n"
        "w = rs.__dict__['record_' + chr(95) + 'approval']\n"))
    assert _record_approval_reference_lines(ast.parse(
        "import research_sandbox as rs\n"
        "w = vars(rs)['record_' + chr(95) + 'approval']\n"))


def test_the_approval_reference_detector_sees_dynamically_imported_modules():
    """**假阴性**对照:模块是**动态导入**的,也要看见。

    `rs = importlib.import_module("research_sandbox")` 的模块名运行期才知道,
    只扫 import 语句看不见;于是 `getattr(rs, "record_approval")` 整条漏掉。"""
    for source in (
        "import importlib\n"
        "rs = importlib.import_module('research_sandbox')\n"
        "w = getattr(rs, 'record_approval')\n",
        "rs = __import__('research_sandbox')\n"
        "w = getattr(rs, 'record_approval')\n",
    ):
        assert _record_approval_reference_lines(ast.parse(source)), source


def test_the_approval_reference_detector_sees_module_dict_methods():
    """**假阴性**对照:用**方法**取模块字典,也要看见(第 13 轮 codex 的 M2)。

    `rs.__dict__.get("record_approval")` 与 `rs.__dict__.items()` 取出来的
    就是模块的全部属性——原先判定器只认**下标**形态(`rs.__dict__["…"]`),
    `.get()` 那一整族整条漏掉,于是这个文件会被判成「没持有写入函数」。"""
    for source in (
        "import research_sandbox as rs\n"
        "w = rs.__dict__.get('record_approval')\n",
        "import research_sandbox as rs\n"
        "items = rs.__dict__.items()\n",
        "import research_sandbox as rs\n"
        "w = rs.__dict__.pop('record_approval')\n",
    ):
        assert _record_approval_reference_lines(ast.parse(source)), source


def test_the_approval_reference_detector_sees_star_imports():
    """**假阴性**对照:`from research_sandbox import *` 之后的裸名也要看见。

    星号导入把模块的公开名字全带进来,`record_approval` 之后就是一个裸名——
    只认 `from … import record_approval` 会整条漏掉(第 13 轮 codex 的 M2)。"""
    assert _record_approval_reference_lines(ast.parse(
        "from research_sandbox import *\n"
        "w = record_approval\n"))


def test_the_approval_call_detector_sees_module_dict_method_calls():
    """**假阴性**对照:用 `.get()` 从模块字典里取出写入函数并**直接调用**,
    也必须算一个写入点(第 13 轮 codex 的 M2)。"""
    assert _record_approval_call_lines(ast.parse(
        "import research_sandbox as rs\n"
        "rs.__dict__.get('record_approval')({}, None)\n"))


def test_the_call_site_detector_sees_calls_in_compound_statements():
    """**对照用例**:判定器不能只看「语句级」的调用(第 11 轮 codex 的 F1)。

    原先的 `_call_sites` 对整份 `research_api.py` 返回 **0 个调用点**
    (`import_candidate` 实际被调用 3 次)——于是「例外函数只被 `*_child` 调用」
    那条断言**永远不可能失败**,它是空转的,不是有效的约束。"""
    source = "def approve():" + "\n" + "    if True:" + "\n"         + "        cls = import_candidate(p, c, s)" + "\n"
    hits = [(scope, callee) for scope, _, callee in _call_sites_in_source(source)]
    assert ("approve", "import_candidate") in hits, hits


def test_the_trading_import_detector_sees_imports_in_compound_statements():
    """**对照用例**:藏在 `if`/`try` 里的交易栈 import 也要被看见
    (第 11 轮 codex 的 F2)。"""
    for source in (
        "def f():" + "\n" + "    if True:" + "\n" + "        import vnpy" + "\n",
        "def g():" + "\n" + "    try:" + "\n" + "        import vnpy_ctastrategy" + "\n"
        + "    except ImportError:" + "\n" + "        pass" + "\n",
        "if True:" + "\n" + "    import strategies" + "\n",
    ):
        assert _trading_import_sites_in_source(source), source


def test_the_child_entrypoint_exceptions_are_actually_seen():
    """**非空转**:真实文件里 `import_candidate` / `engine_runner` 的调用点必须被数出来,
    而且都在回测子进程入口里(第 11 轮 codex 的 F1)。"""
    sites = [s for s in _call_sites(ROOT / "scripts" / "research_api.py")
             if s[2] in {"import_candidate", "engine_runner"}]
    assert sites, "一个调用点都没数到 —— 这条断言就是空转的"
    assert all(scope.endswith("_child") for scope, _, _ in sites), sites


def test_approval_writers_are_only_human_facing_entrypoints():
    """能写批准记录的只有两个**人在场**的入口:带外批准进程与等价命令行。

    模型能碰到的一侧(毕昇、DSH、MCP)一律不许有写路径。
    """
    callers: list[str] = []
    for path in _repo_files():
        callers.extend(_record_approval_calls(path))
    # 按**相对路径**去重:按文件名去重会把不同目录下的同名文件混成一个写者。
    writers = sorted({c.rsplit(":", 1)[0] for c in callers})
    assert set(writers) == {_rel(APPROVAL_SCRIPT), "scripts/research_api.py"}, (
        f"批准记录的写入者必须只有带外批准进程与等价 CLI,实际:{writers}"
    )


def test_only_the_two_human_facing_entrypoints_even_mention_the_approval_writer():
    """连「拿到写入函数」都不许:引用即能力(第 10 轮 codex 的 M4)。

    `writer = rs.record_approval` 之后由框架代为调用,调用点不在这个文件里,
    只查调用点会漏掉这一类。"""
    allowed = {_rel(APPROVAL_SCRIPT), "scripts/research_api.py"}
    holders: list[str] = []
    for path in _repo_files():
        tree = ast.parse(_read_source(path))     # 解析失败就报错,不许静默跳过
        if _record_approval_reference_lines(tree):
            holders.append(_rel(path))
    assert set(holders) <= allowed, (
        f"只有两个带外入口可以拿到批准记录写入函数,实际:{sorted(set(holders))}"
    )


def _approval_call_line() -> int:
    tree = ast.parse(APPROVAL_SCRIPT.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "record_approval":
            return node.lineno
    raise AssertionError("批准进程里没有 record_approval 调用")


# ---------- 直接写批准记录的 AST 判定 ----------

# 写操作的判定要**够全**,也要**够准**:
#   - 只认 write_text 之类 → `shutil.copyfile` / `Path.touch` / `os.rename` /
#     `os.open(O_WRONLY)` 都能改文件(第 6 轮 codex 的 B/M);
#   - 把同名方法一律当成写 → `dict.copy` / `str.replace` / `set.remove` 满地误报,
#     误报一多,这条断言迟早被人加白名单绕过(第 9 轮 codex 的 M1)。
# 所以分三层:①明确是写文件的方法 ②带模块限定的文件函数 ③有歧义的名字(要求接收者像路径)。

# ① 明确是写文件的方法
_WRITE_METHODS = {
    "write_text", "write_bytes", "writelines",
    "touch", "unlink", "rmdir", "removedirs",
    "copyfile", "copy2", "copytree", "rmtree",
    "mkdir", "makedirs",
    "symlink_to", "hardlink_to", "link_to",
    "atomic_replace", "_atomic_replace",   # 本仓库自己的原子替换助手(第 9 轮 codex 的 H2)
}
# ③ 有歧义的方法名:同名方法在别的对象上完全无害(dict.copy / str.replace / set.remove)。
#    只有接收者**看起来是个路径**时才算写文件。
_WRITE_METHODS_AMBIGUOUS = {"write", "dump", "replace", "copy", "move", "remove", "connect"}
# ② 带模块限定的文件函数:(模块名, 函数名)
_WRITE_MODULE_FUNCS = {
    ("os", "replace"), ("os", "rename"), ("os", "remove"), ("os", "unlink"),
    ("os", "rmdir"), ("os", "removedirs"), ("os", "mkdir"), ("os", "makedirs"),
    ("os", "truncate"), ("os", "link"), ("os", "symlink"),
    ("shutil", "copy"), ("shutil", "copy2"), ("shutil", "copyfile"),
    ("shutil", "copytree"), ("shutil", "move"), ("shutil", "rmtree"),
}
# os.open 的标志位:O_WRONLY|O_RDWR|O_CREAT|O_APPEND|O_TRUNC
_OS_WRITE_FLAGS = 0o1 | 0o2 | 0o40 | 0o10 | 0o1000
_OS_FLAG_VALUES = {
    "O_WRONLY": 0o1, "O_RDWR": 0o2, "O_APPEND": 0o10,
    "O_CREAT": 0o40, "O_EXCL": 0o100, "O_TRUNC": 0o1000,
}
# 只允许这两个作用域写「与批准记录有关」的文件:
#   - record_approval:批准记录本身
#   - _approvals_lock:批准记录的独占锁文件
_ALLOWED_SCOPES = {("scripts/research_sandbox.py", "record_approval"),
                   ("scripts/research_sandbox.py", "_approvals_lock")}


def _scopes(tree: ast.Module):
    """逐个作用域:模块级语句、函数、类里的方法。"""
    def walk_body(body, prefix=""):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield prefix + node.name, node
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        yield prefix + node.name + "." + item.name, item
                    else:
                        yield prefix + node.name, item
            else:
                yield prefix + "<module>", node

    yield from walk_body(tree.body)


def _docstring_ids(tree: ast.Module) -> set[int]:
    """文档字符串所在的常量节点。

    文档字符串里提到 approvals 只是**解释**,不是引用(比如 `_atomic_replace`
    的说明里写了 approvals.json)。把它算成「这个作用域在碰批准记录」,
    就会把一个无辜的原子替换助手判成违规。
    """
    ids: set[int] = set()

    def collect(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr) \
                        and isinstance(node.body[0].value, ast.Constant):
                    ids.add(id(node.body[0].value))
                collect(node.body)

    collect(tree.body)
    return ids


@functools.lru_cache(maxsize=1)
def _module_string_constants() -> dict[str, str]:
    """仓库里模块级的字符串常量(名字 → 值),用来做常量折叠。

    `APPROVALS_FILE_NAME = "approvals.json"` 定义在 research_config,引用处只写
    `APPROVALS_FILE_NAME`——不做折叠就看不出这个作用域在碰批准记录
    (第 9 轮 codex 的 H2:常量折叠后的路径名也算)。
    """
    out: dict[str, str] = {}
    for path in _repo_files():
        try:
            tree = ast.parse(_read_source(path))
        except SyntaxError:
            continue
        for node in tree.body:
            target = None
            value_node = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value_node = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value_node = node.target, node.value
            if isinstance(target, ast.Name) and value_node is not None:
                value = _const_str(value_node)
                if value:
                    out.setdefault(target.id, value)
    return out


def _mentions_approvals(node, skip: set[int] | None = None,
                        consts: dict[str, str] | None = None) -> bool:
    skip = skip or set()
    consts = consts or {}
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            if "approvals" in sub.id.lower():
                return True
            value = consts.get(sub.id)
            if value and "approval" in value.lower():
                return True
        elif isinstance(sub, ast.Attribute) and "approvals" in sub.attr.lower():
            return True
        elif isinstance(sub, ast.arg) and "approvals" in sub.arg.lower():
            return True
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                and id(sub) not in skip and "approval" in sub.value.lower():
            return True
    return False


_PATH_WORDS = ("path", "file", "dir", "folder", "target", "dest", "dst", "approv")


def _is_pathlike(node) -> bool:
    """接收者看起来像个路径吗(只用于有歧义的方法名)。"""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in ("Path", "PurePath"):
            return True
        if isinstance(func, ast.Attribute) and func.attr in (
                "join", "Path", "parent", "resolve", "absolute", "expanduser"):
            return True
        return False
    if isinstance(node, ast.Name):
        return any(w in node.id.lower() for w in _PATH_WORDS)
    if isinstance(node, ast.Attribute):
        return any(w in node.attr.lower() for w in _PATH_WORDS)
    if isinstance(node, ast.Subscript):
        key = _const_str(node.slice)
        return bool(key) and any(w in key.lower() for w in _PATH_WORDS)
    return False


def _int_value(node):
    """能常量求值的整数(常量、os.O_XXX、或它们的按位或)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):
        return _OS_FLAG_VALUES.get(node.attr)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.BitOr, ast.Add)):
        left = _int_value(node.left)
        right = _int_value(node.right)
        if left is None or right is None:
            return None
        return left | right if isinstance(node.op, ast.BitOr) else left + right
    return None


def _open_mode(call, position: int) -> str:
    """open 类调用的写模式字符串(读模式返回 "")。

    open 家族的 mode 位置**不一样**:内建 open 是第 2 个位置参数,
    Path.open 是第 1 个(第 9 轮 codex 的 H2:统一按位置取)。
    """
    mode = ""
    for kw in call.keywords:
        if kw.arg == "mode":
            mode = _const_str(kw.value) or ""
    if not mode and len(call.args) > position:
        mode = _const_str(call.args[position]) or ""
    return mode


def _write_call_nodes(node) -> list[ast.Call]:
    """作用域内的写操作节点。"""
    hits: list[ast.Call] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            # 内建 open(path, mode):mode 在第 2 个位置参数
            if func.id == "open":
                mode = _open_mode(sub, 1)
                if any(flag in mode for flag in "wax+"):
                    hits.append(sub)
            continue
        if not isinstance(func, ast.Attribute):
            continue
        attr = func.attr
        base = func.value
        if isinstance(base, ast.Name) and (base.id, attr) in _WRITE_MODULE_FUNCS:
            hits.append(sub)
            continue
        if attr == "open":
            if isinstance(base, ast.Name) and base.id == "os":
                # os.open(path, os.O_CREAT | os.O_WRONLY):按标志位判断
                flags_node = None
                if len(sub.args) > 1:
                    flags_node = sub.args[1]
                for kw in sub.keywords:
                    if kw.arg == "flags":
                        flags_node = kw.value
                flags = _int_value(flags_node)
                if flags is not None and flags & _OS_WRITE_FLAGS:
                    hits.append(sub)
                continue
            # io.open 与内建 open 相同;Path.open(p) 的 mode 在第 1 个位置参数
            position = 1 if isinstance(base, ast.Name) and base.id == "io" else 0
            mode = _open_mode(sub, position)
            if any(flag in mode for flag in "wax+"):
                hits.append(sub)
            continue
        if attr in _WRITE_METHODS:
            hits.append(sub)
            continue
        if attr in _WRITE_METHODS_AMBIGUOUS and _is_pathlike(base):
            hits.append(sub)
    return hits


def _write_calls(node) -> list[int]:
    """作用域内的写操作行号。"""
    return [sub.lineno for sub in _write_call_nodes(node)]


# ---------- 两段式绕过:调用图口径(第 14 轮 codex 的 Low 8) ----------

def _names_in(node) -> set[str]:
    """表达式里出现的变量名。"""
    return {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}


def _local_tainted_names(node, docstrings: set[int], consts: dict[str, str]) -> set[str]:
    """本作用域里「值来自批准记录路径」的变量名。"""
    tainted: set[str] = set()
    changed = True
    while changed:
        changed = False
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Assign) or len(sub.targets) != 1:
                continue
            target = sub.targets[0]
            if not isinstance(target, ast.Name) or target.id in tainted:
                continue
            value = sub.value
            if _mentions_approvals(value, docstrings, consts) or (_names_in(value) & tainted):
                tainted.add(target.id)
                changed = True
    return tainted


def _scope_params(node) -> list[str]:
    """作用域的形参名(按位置)。"""
    a = node.args
    return [x.arg for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]


def _local_call_sites(node, names: set[str]):
    """本作用域里对**同文件**作用域的调用点。"""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name) and func.id in names:
            yield func.id, sub
        elif isinstance(func, ast.Attribute) and func.attr in names:
            yield func.attr, sub


def _approval_write_via_calls(tree, docstrings: set[int], consts: dict[str, str],
                               rel_path: str) -> list[tuple[str, int]]:
    """两段式绕过:「提到批准记录的值」被交给同文件的另一个作用域去写。

    第 14 轮 codex 的 Low 8:原判定是**作用域**口径,写操作与「提到 approvals」
    不在同一个作用域就漏——拆成两个函数就能绕过去:

        def _write(p): Path(p).write_text("{}")         # 本作用域不提 approvals
        def _go(paths): _write(paths["approvals_file"])  # 本作用域提 approvals 但不写

    这里改成**调用图**口径:调用点把「提到 approvals 的值」交给同文件的作用域,
    那个作用域(或它再调用的作用域)用这个参数写文件 ⇒ 违规。

    只跟**同文件**的调用:跨文件的写入者由各自文件里的判定器负责,
    混在一起会把「这个文件没有批准记录写入者」与「那个文件有」搅成一条噪声。
    """
    scopes = {scope: node for scope, node in _scopes(tree)
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    by_simple: dict[str, str] = {}
    for scope in scopes:
        by_simple.setdefault(scope.split(".")[-1], scope)
    names = set(scopes) | set(by_simple)

    def resolve(name: str) -> str | None:
        if name in scopes:
            return name
        return by_simple.get(name)

    def allowed(scope: str) -> bool:
        return (rel_path, scope) in _ALLOWED_SCOPES

    # (被调作用域, 被污染的形参名, 往下追了几层)
    queue: list[tuple[str, set[str], int]] = []
    for scope, node in scopes.items():
        if allowed(scope):
            continue                       # 合法写入者不算「把路径交给别人」
        tainted = _local_tainted_names(node, docstrings, consts)
        for callee_name, call in _local_call_sites(node, names):
            target = resolve(callee_name)
            if target is None or allowed(target):
                continue
            params = _scope_params(scopes[target])
            hit: set[str] = set()
            for idx, arg in enumerate(call.args):
                if idx < len(params) and (_mentions_approvals(arg, docstrings, consts)
                                         or _names_in(arg) & tainted):
                    hit.add(params[idx])
            for kw in call.keywords:
                if kw.arg in params and (_mentions_approvals(kw.value, docstrings, consts)
                                         or _names_in(kw.value) & tainted):
                    hit.add(kw.arg)
            if hit:
                queue.append((target, hit, 1))

    offenders: list[tuple[str, int]] = []
    while queue:
        target, tainted_params, depth = queue.pop()
        node = scopes[target]
        for call_node in _write_call_nodes(node):
            if _names_in(call_node) & tainted_params:
                offenders.append((target, call_node.lineno))
        if depth >= 2:
            continue
        for callee_name, call in _local_call_sites(node, names):
            callee = resolve(callee_name)
            if callee is None or allowed(callee):
                continue
            params = _scope_params(scopes[callee])
            hit: set[str] = set()
            for idx, arg in enumerate(call.args):
                if idx < len(params) and (_names_in(arg) & tainted_params):
                    hit.add(params[idx])
            for kw in call.keywords:
                if kw.arg in params and (_names_in(kw.value) & tainted_params):
                    hit.add(kw.arg)
            if hit:
                queue.append((callee, hit, depth + 1))
    return offenders


def test_nobody_writes_the_approvals_file_directly():
    """批准记录只能经 record_approval 写;不许有人绕过它直接写文件。

    只查 record_approval 的调用点是不够的:有人可以直接
    ``Path(approvals_file).write_text(...)`` 把批准记录改掉。

    判定用 **AST** 而不是逐行匹配(第 8 轮 codex 的 Low):按行匹配时,
    ``p = paths["approvals_file"]`` 写在上一行、``p.write_text(...)`` 写在下一行
    就能绕过。这里按**作用域**判定:某个函数/类方法里既有写调用、又在同一作用域里
    提到 approvals,就算违规。

    允许的只有 `research_sandbox.record_approval`(批准记录)与
    `research_sandbox._approvals_lock`(它的独占锁文件)——原来放行整个文件,
    那等于「这个文件里随便哪个函数都能写账本」(第 6 轮 codex 的 B/M)。
    """
    offenders: list[str] = []
    consts = _module_string_constants()
    for path in _repo_files():
        tree = ast.parse(_read_source(path))     # 解析失败就报错,不许静默跳过
        docstrings = _docstring_ids(tree)
        for scope, node in _scopes(tree):
            if not _mentions_approvals(node, docstrings, consts):
                continue
            if (_rel(path), scope) in _ALLOWED_SCOPES:
                continue
            for lineno in _write_calls(node):
                offenders.append(f"{_rel(path)}:{scope}:{lineno}")
        # 两段式:写操作落在**别的**作用域里(第 14 轮 codex 的 Low 8)
        for scope, lineno in _approval_write_via_calls(tree, docstrings, consts, _rel(path)):
            offenders.append(f"{_rel(path)}:{scope}:{lineno}(经调用)")
    assert offenders == [], offenders


# ---------- 写操作判定器自己的对照用例 ----------

# 判定器本身也要有测试:否则「判定器太松」这件事没有任何东西会发现
# (第 9 轮 codex 的 H2:四个绕过口子)。
_BYPASS_SNIPPETS = {
    "os.open 标志位": 'import os\nos.open(path, os.O_WRONLY | os.O_CREAT)\n',
    "Path.open 的 mode 在第一位": 'from pathlib import Path\nPath(p).open("w")\n',
    "本仓库的原子替换助手": 'rs._atomic_replace(tmp, approvals)\n',
    "常量折叠出来的路径名": 'p = "data/research-control/" + "approvals.json"\np.write_text(x)\n',
    "内建 open 的写模式": 'open(p, "w")\n',
    "shutil.copyfile": 'shutil.copyfile(a, b)\n',
    "os.replace": 'os.replace(a, b)\n',
    "Path.touch": 'Path(p).touch()\n',
}


@pytest.mark.parametrize("label", sorted(_BYPASS_SNIPPETS))
def test_the_write_detector_catches_the_known_bypasses(label):
    """判定器必须抓到这些写法(一个都不能漏)。"""
    tree = ast.parse(_BYPASS_SNIPPETS[label])
    hits = _write_calls(tree)
    assert hits, f"判定器漏掉了:{label}"


_FP_SNIPPETS = {
    "dict.copy": 'd.copy()\n',
    "str.replace": 's.replace("a", "b")\n',
    "set.remove": 's.remove("a")\n',
    "只读 open": 'open(p, "r")\n',
    "socket.connect": 'sock.connect(addr)\n',
}


@pytest.mark.parametrize("label", sorted(_FP_SNIPPETS))
def test_the_write_detector_does_not_cry_wolf(label):
    """同名但无害的调用不该被判成写(误报太多,这条断言迟早被绕过)。"""
    tree = ast.parse(_FP_SNIPPETS[label])
    assert _write_calls(tree) == [], f"判定器误报了:{label}"


def test_the_write_detector_folds_constant_path_names():
    """常量折叠:`APPROVALS_FILE_NAME` 那种引用也算「提到了批准记录」。"""
    consts = {"APPROVALS_FILE_NAME": "approvals.json"}
    tree = ast.parse('def f():\n    Path(APPROVALS_FILE_NAME).write_text(x)\n')
    scope = next(node for scope, node in _scopes(tree) if scope == "f")
    assert _mentions_approvals(scope, set(), consts)
    assert _write_calls(scope)


def test_research_api_can_still_approve_from_cli():
    """命令行的等价入口保留(research_api.py approve/review),不是被删掉的。"""
    source = (ROOT / "scripts" / "research_api.py").read_text(encoding="utf-8")
    assert "def approve_job" in source
    assert "def review_job" in source


@pytest.mark.parametrize("snippet", [
    'rs.record_approval(f, approvals)\n',
    'from research_sandbox import record_approval\nrecord_approval(f, approvals)\n',
    'from research_sandbox import record_approval as ra\nra(f, approvals)\n',
    'getattr(rs, "record_approval")(f, approvals)\n',
    'getattr(rs, "record_" + "approval")(f, approvals)\n',
    'rs.__dict__["record_approval"](f, approvals)\n',
    'import research_sandbox as rs\n_D = rs.__dict__\n_D["record_approval"](f, approvals)\n',
    'import research_sandbox as rs\n_D = rs.__dict__.copy()\n_D["record_approval"](f, approvals)\n',
    'import research_sandbox as rs\n_D = dict(rs.__dict__.items())\n_D["record_approval"](f, approvals)\n',
])
def test_the_writer_detector_recognises_every_known_spelling(snippet):
    """判定器要认得这些写法(第 9 轮 codex 的 F11:`__dict__` 那条是补上的)。

    认不出来 = 有人换个写法就能写账本,而这条断言还在响「通过」。
    """
    tree = ast.parse(snippet)
    assert _record_approval_call_lines(tree), f"判定器漏掉了:{snippet.strip()}"


def test_the_writer_detector_ignores_unrelated_calls():
    """对照:不是写账本的调用不该被算进去(误报会让这条断言失去意义)。"""
    tree = ast.parse('rs.record_result(x)\nresult["record_approval"] = 1\n')
    assert _record_approval_call_lines(tree) == []


_TWO_STEP_BYPASS = '''
from pathlib import Path


def _write(p):
    Path(p).write_text("{}")


def _go(paths):
    _write(paths["approvals_file"])
'''


def test_the_write_detector_catches_the_two_step_approval_bypass():
    """两段式绕过:写操作与「提到 approvals」不在同一个作用域(第 14 轮 codex 的 Low 8)。

    原判定是**作用域**口径,拆成两个函数就绕过去了:
    `_write` 里写文件但不提 approvals,`_go` 里提 approvals 但不写。
    """
    tree = ast.parse(_TWO_STEP_BYPASS)
    offenders = _approval_write_via_calls(tree, _docstring_ids(tree), {}, "probe.py")
    assert offenders, "两段式绕过没被抓住"


def test_the_write_detector_does_not_cry_wolf_on_two_step_unrelated_writes():
    """对照:同文件里「写别的文件」的两段式调用不算违规。"""
    snippet = '''
from pathlib import Path


def _write(p):
    Path(p).write_text("{}")


def _go(paths):
    _write(paths["results_dir"])
'''
    tree = ast.parse(snippet)
    assert _approval_write_via_calls(tree, _docstring_ids(tree), {}, "probe.py") == []


@pytest.mark.parametrize("snippet", [
    'try:\n    pass\nexcept* ImportError:\n    import risk.constitution\n',
    'match "go":\n    case "go":\n        import risk.constitution\n',
    'async with open("x") as fh:\n    import risk.constitution\n',
])
def test_every_import_walker_sees_imports_in_except_star_and_match(tmp_path, snippet):
    """四个 import walker 的语义必须一致(第 14 轮 codex 的 Low 1)。

    上一轮就是「三个 walker 语义不一致」,靠人工比对才发现——这次把对照用例钉住:
    `except*` / `match` / `async with` 里的 import,四个 walker 都得看见。
    """
    probe = tmp_path / "probe.py"
    probe.write_text(snippet, encoding="utf-8")

    tree = ast.parse(_read_source(probe))
    # ① 全位置闭包 walker:要能看见这个仓库内模块
    assert (ROOT / "risk" / "constitution.py") in _local_imports(probe), "全位置 walker 漏了 import"
    # ② 导入期 walker:模块级语句里的 import
    assert _import_time_imports(probe), "导入期 walker 漏了 import"
    # ③ 模块级 import 收集器
    assert _module_level_imports(tree), "模块级收集器漏了 import"
    # ④ 作用域 walker
    assert any(isinstance(node, (ast.Import, ast.ImportFrom))
               for _scope, node in _scoped_nodes(tree)), "作用域 walker 漏了 import"


def test_mcp_never_reaches_approval_writer():
    """MCP 侧没有任何路径能写到 approvals.json。

    用**仓库里那套判定器**(`_record_approval_call_lines`),不是自己另写一套词法禁令:
    词法禁令挡不住 `getattr(rs, "record_" + "approval")` 这种写法——两个常量都不含
    `record_approval` 子串,断言照样绿,却给出虚假安全感(第 10 轮 codex 的 H2)。
    """
    source = (MCP_DIR / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert _record_approval_call_lines(tree) == [], "MCP 不许调用 record_approval"
    # 再叠一层更严的词法约束:连"提到"都不许(提到就该有人来解释它是干什么的)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr != "record_approval", "MCP 不许碰 record_approval"
        elif isinstance(node, ast.Name):
            assert node.id != "record_approval", "MCP 不许碰 record_approval"
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "record_approval" not in node.value, "MCP 不许提到 record_approval"
    # 批准记录的文件路径只用来**读**
    assert "approvals_file" in source
    assert "is_approved_sha" in source


# ---------- 研究侧与交易栈的隔离 ----------

# 研究侧的清单是**自动发现的**,不是手写的:新加一个 scripts/research_*.py
# 就会自动进闭包,不会因为忘了登记而漏检(第 9 轮 codex 的 M2)。
RESEARCH_STACK_FILES = sorted(
    set(ROOT.glob("scripts/research_*.py")) | set((ROOT / "tools" / "research_mcp").glob("*.py"))
)


def _module_candidates(module: str, base: Path | None) -> list[Path]:
    """模块名 → 仓库里的候选文件(模块文件或包的 __init__)。"""
    out: list[Path] = []
    roots = [ROOT] if base is None else [base]
    for root in roots:
        stem = root / module.replace(".", "/")
        out.append(stem.with_suffix(".py"))
        out.append(stem / "__init__.py")
    return out


def _module_candidates_ext(module: str, base: Path | None = None) -> list[Path]:
    """像 `_module_candidates`,但**也**找 `scripts/` 下的顶层模块。

    研究侧彼此是 `import research_config` 这样的顶层导入(scripts/ 在 sys.path 上),
    只按仓库根找的话,`import research_config` 这种依赖根本不在闭包里——
    闭包少了文件,「不许 import 交易栈」的断言就少了眼睛(第 10 轮 codex 的 Low)。

    只给**导入期闭包**用:全位置闭包 `_research_import_closure` 里如果也这么找,
    子进程用的 `backtest_lib` / `walk_forward_optimizer` 会被拉进来,那两条
    「研究栈不许 import 交易栈」就会对**子进程侧**的文件误报。"""
    out = list(_module_candidates(module, base))
    if base is None:
        out.extend(_module_candidates(module, ROOT / "scripts"))
    return out


def _local_imports(path: Path) -> set[Path]:
    """这个文件 import 到的**仓库内**模块文件。

    函数体里的 import 也算(`from dashboard import safe` 写在函数里,照样在运行时生效);
    但**回测子进程**里的 import 不算:子进程(`run_child` / `run_gate_child` /
    `run_evaluate_child`)是唯一允许 vnpy 的地方,把它算进研究侧的闭包,
    就等于要求正式策略也不能 import vnpy。
    """
    tree = ast.parse(_read_source(path))
    out: set[Path] = set()

    def add(cands: list[Path]):
        for cand in cands:
            if cand.is_file():
                out.add(cand)

    def handle(node):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(_module_candidates(alias.name, None))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                module = node.module or ""
                add(_module_candidates(module, base))
                for alias in node.names:          # `from pkg import mod`:mod 可能是子模块
                    add(_module_candidates(f"{module}.{alias.name}", base))
            elif node.module:
                add(_module_candidates(node.module, None))
                for alias in node.names:
                    add(_module_candidates(f"{node.module}.{alias.name}", None))

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.endswith("_child"):
                    continue                  # 回测子进程:vnpy 在那里是需要的
                walk(node.body)
                continue
            if isinstance(node, ast.ClassDef):
                walk(node.body)
                continue
            handle(node)
            for field in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(node, field, None)
                if isinstance(block, list):
                    walk(block)
            for case in getattr(node, "cases", []) or []:
                walk(case.body)

    walk(tree.body)
    return out


def _research_import_closure() -> set[Path]:
    """研究栈的**传递** import 闭包。

    原来只查一份手写清单,漏掉了批准进程真正的 import 图
    (`research_approval_server` → `dashboard.safe` → `notify.redact`)——
    那两个文件将来 import bootstrap/vnpy 时,两条断言不会响(第 6 轮 codex 的 B/M)。
    """
    seen: set[Path] = set()
    queue = list(RESEARCH_STACK_FILES)
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        queue.extend(_local_imports(path) - seen)
    return seen


def _import_time_imports(path: Path) -> list[ast.stmt]:
    """**导入时**就会执行的 import 语句(模块级与类体级)。

    函数体里的 import 不算:运行时才执行,不会在 import 模块的那一刻把
    vnpy 与 .env 的交易凭据拖进研究进程。
    (宪法 risk/constitution.py 的 `load_template()` 就是这种:它只在**回测子进程**
    里被调用,而回测子进程本来就需要 vnpy。)
    """
    tree = ast.parse(_read_source(path))
    out: list[ast.stmt] = []

    def walk(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.ClassDef):
                walk(node.body)
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                out.append(node)
            for field in ("body", "orelse", "finalbody", "handlers"):
                block = getattr(node, field, None)
                if isinstance(block, list):
                    walk(block)
            for case in getattr(node, "cases", []) or []:
                walk(case.body)

    walk(tree.body)
    return out


def _module_constant(tree: ast.Module, name: str):
    """模块级常量 `name` 的赋值右值(找不到返回 None)。"""
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return node.value
    return None


def _has_resolve_call(node) -> bool:
    """这个表达式里有没有 `.resolve()` 调用(绝对化的标志)。"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                and sub.func.attr == "resolve":
            return True
    return False


def test_the_research_side_never_depends_on_the_process_working_directory():
    """研究侧不许依赖进程 CWD(第 14 轮 codex 的 Low 3)。

    `scripts/segment_ledger.py` 原先 import bootstrap,顺带带来 bootstrap 的
    `os.chdir(ROOT)` 副作用——import 那一刻就把 CWD 切到仓库根。解耦之后它不再
    import bootstrap(否则连 pytest 的全局 conftest 都要装交易环境),
    于是**pytest 进程的 CWD 不再被强制切到仓库根**,相对路径的基准改由启动目录决定。

    研究侧因此必须自己站住(这条断言就是那个「自己站住」的护栏):
      - 研究栈里不许出现 `os.chdir` / `os.getcwd`:谁也不许改或读 CWD;
      - 路径锚点必须经过 `.resolve()`(绝对路径),不能是相对字面量。

    交易侧不受影响:那边的脚本自己 import bootstrap,运行时 CWD 行为不变;
    `risk/live_admission.py` 的 `APPROVAL_RELATIVE` 也永远与 `bootstrap.ROOT` 一起拼。
    """
    for path in RESEARCH_STACK_FILES:
        tree = ast.parse(_read_source(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in {"chdir", "getcwd"} \
                    and isinstance(func.value, ast.Name) and func.value.id == "os":
                raise AssertionError(f"{_rel(path)} 不许依赖进程 CWD:{func.attr}")

    anchors = {
        "scripts/research_config.py": ("CODE_ROOT", "ROOT"),
        "scripts/research_sandbox.py": ("_CODE_ROOT", "_SCRIPT_ROOT"),
        "scripts/segment_ledger.py": ("ROOT",),
    }
    for rel, names in anchors.items():
        tree = ast.parse(_read_source(ROOT / rel))
        for name in names:
            value = _module_constant(tree, name)
            assert value is not None, f"{rel} 少了路径锚点 {name}"
            assert _has_resolve_call(value), f"{rel}:{name} 必须是绝对路径(.resolve())"


def test_the_research_stack_never_imports_bootstrap():
    """研究侧不 import bootstrap:那会把 vnpy 交易栈与 .env 的交易凭据拖进研究进程。

    bootstrap 在导入时就 `from vnpy.trader.setting import SETTINGS` 并 chdir 到项目根。
    研究进程一旦 import 它,内存里就有了 CTP 账号——与「研究侧不碰 .env 交易段」冲突。

    查的是**传递闭包**、且只看**导入时**执行的 import,不是一份手写清单。
    """
    for path in sorted(_research_import_closure()):
        for node in _import_time_imports(path):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "bootstrap" for alias in node.names), \
                    f"{_rel(path)} 不许 import bootstrap"
            else:
                assert (node.module or "").split(".")[0] != "bootstrap", \
                    f"{_rel(path)} 不许 from bootstrap import ..."


def test_the_research_stack_never_imports_vnpy():
    """研究侧不 import vnpy(哪怕是绕过 bootstrap 直接 import)。

    CHANGELOG/ADR 里写的是「不再 import vnpy」,那这条断言就该真的挡住 `import vnpy`。
    只看导入时执行的 import:回测子进程里的 vnpy 是需要的(见上一条说明)。
    """
    for path in sorted(_research_import_closure()):
        for node in _import_time_imports(path):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] != "vnpy" for alias in node.names), \
                    f"{_rel(path)} 不许 import vnpy"
            else:
                assert (node.module or "").split(".")[0] != "vnpy", \
                    f"{_rel(path)} 不许 from vnpy import ..."


# 常驻研究进程:批准进程 / 网关 / MCP。这三个进程**任何位置**(含函数体)都不许
# import 交易栈——它们一 import,进程内存里就有 CTP 账号,与「研究侧不碰 .env 交易段」
# 的边界声明冲突。回测子进程不在此列(它本来就要跑策略)。
RESIDENT_PROCESS_FILES = (
    ROOT / "scripts" / "research_approval_server.py",
    ROOT / "scripts" / "research_gateway.py",
    MCP_DIR / "server.py",
)
TRADING_STACK_ROOTS = ("vnpy", "vnpy_ctastrategy", "bootstrap", "strategies")
# 会把交易栈**拖进来**的中间层模块(它们自己在导入期就 import vnpy/strategies)。
# 研究侧碰这些模块,等于碰交易栈。
TRADING_SIDE_MODULES = ("backtest_lib", "walk_forward_optimizer", "optimize")


def test_the_resident_research_processes_never_import_the_trading_stack():
    """常驻研究进程里**任何位置**(含函数体内)都不许 import 交易栈。

    上面两条只查**导入时**执行的 import:函数体里的 `import strategies.x` 一样会把
    vnpy 与 .env 的 CTP 凭据拖进常驻进程,断言不会响(第 10 轮 codex 的 M5)。"""
    for path in RESIDENT_PROCESS_FILES:
        for scope, lineno, root in _trading_import_sites(path):
            assert root not in TRADING_STACK_ROOTS and root not in TRADING_SIDE_MODULES, \
                f"{_rel(path)}:{lineno}({scope})不许 import {root}(会把交易栈与 .env 凭据拖进常驻研究进程)"


def _imported_roots(node: ast.stmt) -> list[str]:
    """一条 import 语句引入的**顶层**模块名。"""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [(node.module or "").split(".")[0]]
    return []


def _imported_modules(node: ast.stmt) -> list[str]:
    """一条 import 语句引入的模块名(完整,用于解析仓库内文件)。"""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        out = []
        if node.module:
            out.append(node.module)
            out.extend(f"{node.module}.{alias.name}" for alias in node.names)
        return out
    return []


def _import_time_closure(roots) -> set[Path]:
    """roots 的**导入期** import 闭包。

    只算「import 这个模块时就会执行」的 import:函数体里的 import 不算,
    那是**运行到那里**才发生的事,由另一条断言(子进程入口)管。"""
    seen: set[Path] = set()
    queue = list(roots)
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        for node in _import_time_imports(path):
            for module in _imported_modules(node):
                for cand in _module_candidates_ext(module, None):
                    if cand.is_file() and cand not in seen:
                        queue.append(cand)
    return seen


def test_the_resident_processes_never_load_the_trading_stack_through_their_dependencies():
    """常驻研究进程的**导入期闭包**里也不许出现交易栈(第 10 轮 codex 的 Low)。

    原来只查这三个文件**自己**:它们 import 进来的东西(风险守卫、批准进程、
    网关依赖)不在检查范围里——那些文件哪天 import 了 bootstrap,断言不会响。"""
    # research_api 是网关**启动时**才 import 的(在 `_default_jobs` 里),
    # 所以常驻进程的依赖链要连研究栈的文件一起算。
    closure = _import_time_closure(list(RESIDENT_PROCESS_FILES) + list(RESEARCH_STACK_FILES))
    for path in sorted(closure):
        for node in _import_time_imports(path):
            for root in _imported_roots(node):
                assert root not in TRADING_STACK_ROOTS, \
                    f"{_rel(path)} 不许 import {root}(常驻研究进程的依赖链会把交易栈拖进来)"


def test_the_resident_import_closure_is_actually_computed():
    """**对照用例**:闭包不是空的、也不是只有那几个手写文件。"""
    closure = _import_time_closure(list(RESIDENT_PROCESS_FILES) + list(RESEARCH_STACK_FILES))
    assert ROOT / "scripts" / "research_sandbox.py" in closure
    assert ROOT / "scripts" / "research_config.py" in closure
    assert ROOT / "risk" / "constitution.py" in closure


# 研究侧文件里**允许**出现交易栈 import 的非 `_child` 函数:只有一个,
# 而且它只被回测子进程调用(下面那条断言会核)。
RESEARCH_TRADING_IMPORT_EXCEPTIONS = {
    "import_candidate": "只被 run_child / run_gate_child / run_evaluate_child 调用",
    "engine_runner.runner": "真实执行器,只被回测子进程(run_gate_child / run_evaluate_child)注入",
}


def _scoped_nodes(tree: ast.AST):
    """产出 (scope, node):scope 是该节点所在的函数/类限定名(模块级记作 "<module>")。

    **一个 walker 走遍所有语句**(第 11 轮 codex 的 F1/F2 的共同根因):
    之前的两个 walker(`_trading_import_sites` 与 `_call_sites`)只看函数体/模块体里的
    **语句级**节点——藏在 `if`/`for`/`try`/`with` 里的 import 与调用**一个都看不见**。
    实测:`_call_sites(scripts/research_api.py)` 对整份文件返回 **0 个调用点**
    (而 `import_candidate` 实际被调用 3 次),于是「例外函数只被 `*_child` 调用」
    那条断言**永远不可能失败**——它是空转的,不是有效的约束。"""
    def scope_name(scope: str, name: str) -> str:
        return f"{scope}.{name}" if scope else name

    def walk_expr(expr, scope: str):
        """表达式里的 import 与调用(赋值右值、关键字实参、推导式、lambda…)。"""
        for child in ast.walk(expr):
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                yield (scope, child)

    def walk_body(body, scope: str):
        for node in body:
            yield from walk_stmt(node, scope)

    def walk_stmt(node, scope: str):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 默认值与装饰器属于**外层**作用域
            for expr in (list(node.decorator_list)
                         + [a for a in node.args.defaults if a is not None]
                         + [d for d in node.args.kw_defaults if d is not None]):
                yield from walk_expr(expr, scope)
            yield from walk_body(node.body, scope_name(scope, node.name))
            return
        if isinstance(node, ast.ClassDef):
            for expr in list(node.decorator_list) + list(node.bases):
                yield from walk_expr(expr, scope)
            yield from walk_body(node.body, scope_name(scope, node.name))
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield (scope, node)
            return
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try,
                             ast.TryStar, ast.With, ast.AsyncWith, ast.Match)):
            # 表达式部分(test / iter / items)属于当前作用域
            for child in ast.iter_child_nodes(node):
                if not isinstance(child, ast.stmt):
                    yield from walk_expr(child, scope)
            for field in ("body", "orelse", "finalbody"):
                for sub in getattr(node, field, []) or []:
                    yield from walk_stmt(sub, scope)
            for handler in getattr(node, "handlers", []) or []:
                yield from walk_body(handler.body, scope)
            # `match` 的 case 体不在 body/orelse 里,得单独走(第 14 轮 codex 的 B-L1)
            for case in getattr(node, "cases", []) or []:
                yield from walk_body(case.body, scope)
                if getattr(case, "guard", None) is not None:
                    yield from walk_expr(case.guard, scope)
            return
        # 普通语句:只取它的**表达式**部分(语句子节点在复合语句分支里处理)
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                continue
            yield from walk_expr(child, scope)

    seen: set[int] = set()
    for scope, node in walk_body(tree.body, ""):
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield (scope or "<module>", node)


def _trading_import_sites_in_source(source: str) -> list[tuple[str, int, str]]:
    """(所在作用域, 行号, 导入的顶层名)。模块级的记作 "<module>"。"""
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope, node in _scoped_nodes(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        for root in _imported_roots(node):
            if root in TRADING_STACK_ROOTS or root in TRADING_SIDE_MODULES:
                out.append((scope, node.lineno, root))
    return out


def _trading_import_sites(path: Path) -> list[tuple[str, int, str]]:
    """(所在函数的限定名, 行号, 导入的顶层名)。模块级的记作 "<module>"。

    嵌套函数用点号连起来(`engine_runner.runner`):只看最内层名字会丢掉
    「它挂在谁下面」这条信息,而这正是判断「是不是只在子进程里跑」的关键。"""
    return _trading_import_sites_in_source(_read_source(path))


def test_research_files_only_reach_the_trading_stack_from_child_entrypoints():
    """研究侧文件里的交易栈 import 只能出现在**回测子进程入口**里。

    原来只查「导入时」的 import:函数体里的 `from strategies... import ...`
    一样能把交易栈拖进来。回测子进程(`run_child` / `run_gate_child` /
    `run_evaluate_child`)是唯一允许碰交易栈的地方,其余位置一律不许
    (第 10 轮 codex 的 Low)。"""
    for path in sorted(RESEARCH_STACK_FILES):
        for scope, lineno, root in _trading_import_sites(path):
            if scope.endswith("_child"):
                continue
            assert scope in RESEARCH_TRADING_IMPORT_EXCEPTIONS, \
                f"{_rel(path)}:{lineno} 的 {scope} 不许 import {root}(只有回测子进程能碰交易栈)"


def test_the_trading_import_exception_is_only_called_from_child_entrypoints():
    """例外函数只能在回测子进程里被调用(第 10 轮 codex 的 Low)。

    没有这一条,`import_candidate` 就是常驻研究进程里的一条绕过路径。"""
    for path in sorted(RESEARCH_STACK_FILES):
        for scope, lineno, callee in _call_sites(path):
            if callee not in {key.split(".")[0] for key in RESEARCH_TRADING_IMPORT_EXCEPTIONS}:
                continue
            assert scope.endswith("_child"), \
                f"{_rel(path)}:{lineno} 的 {scope} 调用了 {callee};它只能在回测子进程里被调用"


def _call_sites_in_source(source: str) -> list[tuple[str, int, str]]:
    """(所在作用域, 行号, 被调用的名字)。模块级的记作 "<module>"。"""
    tree = ast.parse(source)
    out: list[tuple[str, int, str]] = []
    for scope, node in _scoped_nodes(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            out.append((scope, node.lineno, func.id))
        elif isinstance(func, ast.Attribute):
            out.append((scope, node.lineno, func.attr))
    return out


def _call_sites(path: Path) -> list[tuple[str, int, str]]:
    """(所在函数的限定名, 行号, 被调用的名字)。模块级的记作 "<module>"。"""
    return _call_sites_in_source(_read_source(path))


def test_the_research_import_closure_is_actually_computed():
    """**对照用例**:证明闭包不是空的、也不是只有那几个手写文件。

    闭包算错(比如相对导入没解析)时,上面两条断言会「因为没查到东西」而假绿。
    """
    closure = _research_import_closure()

    assert ROOT / "dashboard" / "safe.py" in closure, "批准进程的传递依赖没进闭包"
    assert ROOT / "notify" / "redact.py" in closure, "dashboard.safe 的传递依赖没进闭包"
    assert ROOT / "scripts" / "research_sandbox.py" in closure


def test_the_research_stack_never_loads_the_trading_dotenv():
    """研究侧不许把 .env 读进进程环境(load_dotenv 是 bootstrap.load_env 的活)。

    只看**代码**:注释里提到 load_dotenv 是解释为什么不用它,不算违规。
    """
    for path in sorted(_research_import_closure()):
        tree = ast.parse(_read_source(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in ("load_dotenv", "load_env"), \
                f"{_rel(path)} 不许调用 {name}(会把 .env 的交易凭据读进研究进程)"


# ---------- DSH Preset ----------

def test_preset_file_exists():
    assert PRESET.is_file()


def test_preset_has_no_forbidden_tools():
    """注释里可以提这些名字,但**配置条目**里不能出现。"""
    text = PRESET.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for name in FORBIDDEN_IN_PRESET:
        assert name not in body, f"research Preset 不允许挂 {name}"


def test_preset_mounts_only_persona_and_mcp():
    text = PRESET.read_text(encoding="utf-8")
    ids = re.findall(r"^\s+- id: ([a-z0-9-]+)$", text, re.MULTILINE)
    assert ids == ["preset-research", "persona", "research-mcp"], ids


def test_preset_contains_no_plaintext_secret():
    """密钥只能用环境变量引用,不能写死在配置里。"""
    text = PRESET.read_text(encoding="utf-8")
    assert "bs-sak-" not in text
    assert "bs-pat-" not in text
    # 密钥必须以 process.env 引用
    assert "process.env.BISHENG_OPENAPI_KEY" in text


def test_preset_disables_auto_review():
    """Auto review 会让模型自己审查自己写的代码,必须不启用。"""
    text = PRESET.read_text(encoding="utf-8")
    assert "auto-review" not in text


# ---------- Preset 的**结构**(不是文本 grep) ----------

def _load_preset() -> dict:
    """把 Preset 补丁**解析成结构**。

    `!!js expr` 是 DSH 的自定义标签:这里保留**表达式原文**,
    这样才能断言「密钥是用 process.env 引用的、机器上的绝对路径没写死」。
    """
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_multi_constructor(
        "tag:yaml.org,2002:js",
        lambda loader, suffix, node: loader.construct_scalar(node),
    )
    return yaml.load(PRESET.read_text(encoding="utf-8"), Loader=Loader)


def _walk(node):
    """遍历结构里的所有键与所有字符串值。"""
    if isinstance(node, dict):
        for key, value in node.items():
            yield ("key", str(key))
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
    elif isinstance(node, str):
        yield ("value", node)


def test_the_rendered_preset_mounts_only_persona_and_mcp():
    """按**结构**断言,不是按文本 grep(第 9 轮 codex 的 F6)。

    文本 grep 只能证明「文件里没出现这个词」;结构断言才能证明
    「这个 Preset 挂的就是这两个东西,别的一概没挂」——
    换个键名、换层嵌套、换个写法,结构断言都还认得。
    """
    data = _load_preset()
    # 顶层是一个列表(补丁文件本身就是「一段补丁」)
    entries = data[0]["insert"]
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry["id"] == "preset-research"
    assert entry["name"] == "@deepseek-ai/dsh-agent-preset"
    config = entry["config"]
    assert config["id"] == "research"
    assert [p["id"] for p in config["plugins"]] == ["persona", "research-mcp"]
    assert [p["name"] for p in config["plugins"]] == [
        "@deepseek-ai/dsh-persona", "@deepseek-ai/dsh-mcp-client"]


def test_the_rendered_preset_has_no_tool_entries():
    """结构里没有危险工具的条目(注释里提到不算)。

    **键和值都要看**:插件的 id 落在 value 上(`- id: tool-pwsh`),只扫键的话
    这条断言形同虚设(第 10 轮 codex 的 H3)。"""
    tokens = [value for kind, value in _walk(_load_preset()) if kind in ("key", "value")]
    for name in FORBIDDEN_IN_PRESET:
        assert not any(name in token for token in tokens), f"research Preset 的配置里出现了 {name}"


# --- Preset 的**结构白名单** -------------------------------------------------
# 第 10 轮 codex 的 M1:名字黑名单挡不住「换个名字的同一个东西」。实测变体 v2:
# 只在 persona 插件的 config 下加一行 `tools: [tool-exec]`,**六条 Preset 断言
# 全部 PASS**。所以这里改成白名单:能进 Preset 的东西逐项批准。
#
# ① 能力类键(tools/skills/mcpServers/hooks/agents/…)一律不许出现;
# ② 字符串值不许以工具/技能前缀开头;
# ③ Preset 条目 / 插件条目 / 插件 config 的键都必须落在已知集合里;
# ④ 插件的**包名**也只允许那两个。
PRESET_CAPABILITY_KEYS = {
    "tool", "tools", "skill", "skills", "mcpservers", "toolallowlist",
    "allowedtools", "hooks", "agents", "subagents", "autoreview",
}
PRESET_ENTRY_KEYS = {"id", "name", "config"}
PRESET_CONFIG_KEYS = {"id", "name", "description", "order", "plugins"}
PRESET_PLUGIN_KEYS = {"id", "name", "config"}
PRESET_PLUGIN_CONFIG_KEYS = {
    "persona": {"prefix"},
    "research-mcp": {
        "transport", "serverName", "command", "args", "cwd", "env",
        "startupTimeoutMs", "toolCallTimeoutMs", "failOnStartupError",
    },
}
PRESET_PLUGIN_PACKAGES = {
    "@deepseek-ai/dsh-persona", "@deepseek-ai/dsh-mcp-client",
}
PRESET_FORBIDDEN_VALUE_PREFIXES = (
    "tool-", "dsh-tool-", "skill-", "dsh-skill-", "dsh-experimental-",
    "@deepseek-ai/dsh-tool", "@deepseek-ai/dsh-skill",
)


def _normalized_key(key: str) -> str:
    """键名归一:去掉分隔符与大小写差异(`Mcp-Servers` 与 `mcp_servers` 是同一件事)。"""
    return re.sub(r"[-_\s]", "", str(key)).lower()


def _check_preset_structure(data: dict) -> None:
    """Preset 的结构白名单。违规就抛 AssertionError。

    抽成独立函数,是为了让**负向用例**能直接复用它(第 11 轮 codex 的 F5:
    白名单里的两条通用分支原本在仓库自带用例里一次都没被走到,
    可以被无声删掉——删掉之后 `serverName: dsh-tool-x` 这类注入全绿通过)。"""
    for entry in data[0]["insert"]:
        assert set(entry) <= PRESET_ENTRY_KEYS, \
            f"Preset 条目多了键: {sorted(set(entry) - PRESET_ENTRY_KEYS)}"
        config = entry["config"]
        assert set(config) <= PRESET_CONFIG_KEYS, \
            f"Preset config 多了键: {sorted(set(config) - PRESET_CONFIG_KEYS)}"
        for plugin in config["plugins"]:
            assert set(plugin) <= PRESET_PLUGIN_KEYS, \
                f"插件 {plugin.get('id')} 多了键: {sorted(set(plugin) - PRESET_PLUGIN_KEYS)}"
            assert plugin["name"] in PRESET_PLUGIN_PACKAGES, \
                f"出现了未批准的插件包: {plugin['name']}"
            allowed = PRESET_PLUGIN_CONFIG_KEYS.get(plugin["id"])
            assert allowed is not None, f"出现了未批准的插件: {plugin['id']}"
            assert set(plugin["config"]) <= allowed, \
                f"插件 {plugin['id']} 的 config 多了键: {sorted(set(plugin['config']) - allowed)}"

    for kind, token in _walk(data):
        if kind == "key":
            assert _normalized_key(token) not in PRESET_CAPABILITY_KEYS, \
                f"Preset 里出现了能力类键 {token}"
        else:
            for prefix in PRESET_FORBIDDEN_VALUE_PREFIXES:
                assert not token.startswith(prefix), \
                    f"Preset 的值 {token!r} 以 {prefix} 开头(工具/技能条目)"


def test_the_rendered_preset_matches_a_structural_whitelist():
    """结构白名单:能进 Preset 的东西逐项批准(第 10 轮 codex 的 M1)。

    白名单比黑名单强的地方:**新增任何东西都会红**——多一个插件、多一个 config 键、
    多一行 `tools:` 都躲不过去;而黑名单只能挡住「已经想到的那个名字」。"""
    _check_preset_structure(_load_preset())


@pytest.mark.parametrize("mutation", [
    "能力键", "大小写变体", "分隔符变体", "值前缀", "未知配置键", "未知插件", "未知插件包",
])
def test_the_preset_whitelist_rejects_every_injected_shape(mutation):
    """**负向用例**:白名单的每一条分支都要真的能挡住东西(第 11 轮 codex 的 F5)。

    白名单里有两条「通用分支」(能力类键归一、字符串值前缀)在仓库自带用例里
    **一次都没被走到**——删掉它们,`serverName: dsh-tool-x` 这类注入会全绿通过。
    这里在**内存里**构造被改坏的 Preset(不落盘),逐条要求判定器报错。"""
    import copy

    data = copy.deepcopy(_load_preset())
    plugins = data[0]["insert"][0]["config"]["plugins"]
    persona = next(p for p in plugins if p["id"] == "persona")
    mcp = next(p for p in plugins if p["id"] == "research-mcp")
    if mutation == "能力键":
        persona["config"]["tools"] = ["tool-exec"]
    elif mutation == "大小写变体":
        persona["config"]["Tools"] = []
    elif mutation == "分隔符变体":
        persona["config"]["Mcp-Servers"] = []
    elif mutation == "值前缀":
        mcp["config"]["serverName"] = "dsh-tool-x"
    elif mutation == "未知配置键":
        mcp["config"]["shell"] = True
    elif mutation == "未知插件":
        plugins.append({"id": "extra", "name": "@deepseek-ai/dsh-persona", "config": {}})
    elif mutation == "未知插件包":
        persona["name"] = "@deepseek-ai/dsh-tool-x"
    with pytest.raises(AssertionError):
        _check_preset_structure(data)


def _dynamic_trading_imports(path: Path) -> list[tuple[str, int, str]]:
    """(作用域, 行号, 交易栈根模块名):**字面量**的动态导入。"""
    tree = ast.parse(_read_source(path))
    out: list[tuple[str, int, str]] = []
    for scope, node in _scoped_nodes(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = _callee_name(node.func)
        if callee not in {"import_module", "__import__"}:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        root = first.value.split(".")[0]
        if root in TRADING_STACK_ROOTS or root in TRADING_SIDE_MODULES:
            out.append((scope, node.lineno, root))
    return out


def test_the_research_files_never_import_the_trading_stack_dynamically():
    """**动态导入**也不许把交易栈拖进常驻进程(第 11 轮 codex 的 F3)。

    静态不变量只看得见 `import` 语句;`importlib.import_module("vnpy.trader.setting")`
    与 `__import__("vnpy")` 是完全隐形的。本仓库里**已经有**动态导入的用法
    (`risk/constitution.py` 的模板载入),所以这条断言盯住的是:
    **动态导入只许出现在回测子进程入口里**。"""
    for path in sorted(set(RESEARCH_STACK_FILES) | set(RESIDENT_PROCESS_FILES)):
        for scope, lineno, root in _dynamic_trading_imports(path):
            assert scope.endswith("_child"), (
                f"{_rel(path)}:{lineno} 的 {scope} 动态导入了 {root}"
                "(动态导入只许出现在回测子进程入口里)")


def test_the_rendered_preset_references_secrets_only_through_the_environment():
    """密钥只能用环境变量引用,且**不能写死任何机器上的绝对路径**。"""
    config = _load_preset()[0]["insert"][0]["config"]
    mcp = next(p for p in config["plugins"] if p["id"] == "research-mcp")["config"]
    env = mcp["env"]
    assert set(env) == {"INVEST_ROOT", "BISHENG_OPENAPI_KEY",
                        "BISHENG_BASE_URL", "BISHENG_WORKFLOW_ID"}
    for key, value in env.items():
        assert "process.env." in value, f"{key} must be an env reference: {value!r}"
        # 每一段兜底都只能是环境变量或工作目录,**不许出现明文字面量**:
        # `process.env.KEY || 'plain-secret'` 这种混合写法,子串判断会放它过去
        # (第 10 轮 codex 的 L4)。
        for branch in value.split("||"):
            branch = branch.strip()
            assert branch.startswith("process.env.") or branch.startswith("process.cwd()"), \
                f"{key} fallback must be env or cwd: {value!r}"
        assert not re.search(r"['\"][^'\"]*['\"]", value), \
            f"{key} must not carry a literal fallback: {value!r}"

    values = [value for kind, value in _walk(_load_preset()) if kind == "value"]
    joined = "\n".join(values)
    assert "bs-sak-" not in joined and "bs-pat-" not in joined
    assert not re.search(r"[A-Za-z]:[\\/]", joined), "Preset 里不许写死盘符绝对路径"


# ---------- 工具目录 ----------

def test_mcp_server_exposes_exactly_three_tools():
    sys_path = str(MCP_DIR)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import asyncio

    import server as mcp_server

    assert Path(mcp_server.__file__).resolve() == MCP_DIR / "server.py"

    async def names():
        return sorted(t.name for t in await mcp_server.mcp.list_tools())

    assert asyncio.run(names()) == ["request_approval", "research_status", "start_research"]


@pytest.mark.parametrize("name", ["record_approval", "backtest", "deploy", "write_file"])
def test_mcp_has_no_dangerous_tool(name):
    sys_path = str(MCP_DIR)
    import sys
    if sys_path not in sys.path:
        sys.path.insert(0, sys_path)
    import asyncio

    import server as mcp_server

    async def names():
        return [t.name for t in await mcp_server.mcp.list_tools()]

    assert name not in asyncio.run(names())


# ---------- 交易栈侧的导入顺序(解耦带出来的回归,已修) ----------

def _module_level_imports(tree: ast.Module) -> list[ast.AST]:
    """**模块级**的 import 语句(不进入函数体与类体)。

    函数体里的 import 是**调用时**才执行的,属于另一类时序,这里不收。
    `try/except`、`if`、`with`、`for`、`while` 里的 import 也算模块级
    (它们的执行时点仍然是模块导入期)——第 12 轮 codex 的 L6:
    收集器原先只走进 `try`,于是 `if TYPE_CHECKING or True: import bootstrap`
    这种**合法**写法会被误判成「没有在模块导入期 import bootstrap」。
    第 13 轮 codex 的 L5:再补 `async with` / `match` / `except*` 三种写法
    (`ast.AsyncWith` / `ast.Match` / `ast.TryStar`)。"""
    found: list[ast.AST] = []

    def walk_body(body: list) -> None:
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.append(node)
            elif isinstance(node, ast.Try):
                walk_body(node.body)
                walk_body(node.orelse)
                walk_body(node.finalbody)
                for handler in node.handlers:
                    walk_body(handler.body)
            elif isinstance(node, ast.TryStar):          # `except* E:`(3.11+)
                walk_body(node.body)
                walk_body(node.orelse)
                walk_body(node.finalbody)
                for handler in node.handlers:
                    walk_body(handler.body)
            elif isinstance(node, ast.AsyncWith):
                # `async with` 没有 orelse 分支(第 14 轮 codex 的 Low 1 顺带发现:
                # 它原先与 If/For/While 混在一个分支里,一走进来就 AttributeError)。
                walk_body(node.body)
            elif isinstance(node, (ast.If, ast.With, ast.For, ast.AsyncFor, ast.While)):
                walk_body(node.body)
                walk_body(node.orelse)
            elif isinstance(node, ast.Match):             # `match … case …:`
                for case in node.cases:
                    walk_body(case.body)

    walk_body(tree.body)
    return found


def _imports_bootstrap(node: ast.AST) -> bool:
    """这条 import 是不是 `import bootstrap`(顶层名)。"""
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == "bootstrap" for a in node.names)
    return False


def _module_level_bootstrap_guard(tree: ast.Module) -> ast.If | None:
    """找到「模块级、由一个 `if` 守着」的 `import bootstrap` 那个 `if`。

    只查「有没有 import bootstrap」是不够的(第 13 轮 codex 的 L4):
    守卫谓词被写反(`is not None` 写成 `is None`)时,断言照样绿,
    但运行时 bootstrap **永远不会被导入**——正是它要守的那件事被改坏而断言不响。"""
    def walk(body: list) -> ast.If | None:
        for node in body:
            if isinstance(node, ast.If):
                if any(_imports_bootstrap(inner) for inner in node.body):
                    return node
                found = walk(node.body)
                if found is not None:
                    return found
                found = walk(node.orelse)
                if found is not None:
                    return found
            elif isinstance(node, (ast.With, ast.AsyncWith, ast.For,
                                   ast.AsyncFor, ast.While, ast.Try, ast.TryStar)):
                found = walk(node.body)
                if found is not None:
                    return found
                found = walk(getattr(node, "orelse", []))
                if found is not None:
                    return found
        return None

    return walk(tree.body)


def _is_find_spec_vnpy_is_not_none(node: ast.AST) -> bool:
    """谓词是不是 `importlib.util.find_spec("vnpy") is not None`。"""
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1
            and isinstance(node.ops[0], ast.IsNot) and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value is None):
        return False
    call = node.left
    if not (isinstance(call, ast.Call) and _callee_name(call.func) == "find_spec"):
        return False
    return bool(call.args) and _const_str(call.args[0]) == "vnpy"


@pytest.mark.parametrize("name", ["test_constitution_round11.py", "test_constitution_round12.py"])
def test_the_trading_side_constitution_tests_import_bootstrap_before_touching_vnpy(name):
    """交易栈侧的宪法回归用例必须在**模块导入期**先 `import bootstrap`。

    这两份用例会经 `const.snapshot_protected_state()` 预加载
    `vnpy_ctastrategy.backtesting`,连带加载 `vnpy.trader.database`;
    而 `scripts/bootstrap.py` 有「bootstrap 必须先于 vnpy.trader.database 导入」
    的顺序检查(否则时区设置不生效,订单簿时间会整体偏移)。
    以前这个顺序是靠 `conftest → segment_ledger → bootstrap` **间接兜住**的——
    登记文件与交易栈解耦(`scripts/segment_ledger.py` 不再 import bootstrap)后,
    那层兜底消失,两份文件**合跑**就会失败 10 项(取 origin/main 对比可确认是
    本分支引入的,不是环境问题)。

    这里把它钉成一条**可在研究侧 CI 上跑的**断言(纯 AST,不 import vnpy):
    兜底消失后,谁忘了补 `import bootstrap` 就会在这里红,而不是等到
    「两份文件合跑」才炸出来。"""
    path = Path(__file__).resolve().parent / name
    tree = ast.parse(_read_source(path))
    imports = _module_level_imports(tree)

    bootstrap_lines = [
        node.lineno for node in imports
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "bootstrap" for a in node.names)
    ]
    assert bootstrap_lines, f"{name} 没有在模块导入期 import bootstrap(交易栈的时区前置会被绕过)"

    # 守卫**谓词**也要对(第 13 轮 codex 的 L4):原先这里只查「有没有 import bootstrap」,
    # 谓词被写反(`is not None` → `is None`)时断言照样绿,
    # 而运行时 bootstrap 永远不会被导入——正是它要守的那件事被改坏而断言不响。
    guard = _module_level_bootstrap_guard(tree)
    assert guard is not None, (
        f"{name}: `import bootstrap` 应当在一个 `if` 守卫里"
        "(没有交易栈时不导入,交易栈装坏时不吞异常)")
    assert _is_find_spec_vnpy_is_not_none(guard.test), (
        f"{name}: 守卫谓词必须仍是 `importlib.util.find_spec(\"vnpy\") is not None`"
        "(谓词被写反 → bootstrap 永远不会被导入)")

    vnpy_lines = [
        node.lineno for node in imports
        if (isinstance(node, ast.Import) and any(a.name.split(".")[0].startswith("vnpy") for a in node.names))
        or (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0].startswith("vnpy"))
    ]
    # 目前这两份文件里**没有**模块级的 `import vnpy`(vnpy 是被
    # `const.snapshot_protected_state()` 间接加载的),所以这半截当前是**空转**的:
    # 留着是为了「哪天有人加了模块级 vnpy 导入,顺序约束就自动生效」。
    for line in vnpy_lines:
        assert line > min(bootstrap_lines), (
            f"{name}:{line} 在 bootstrap 之前就 import 了 vnpy 族模块")


def test_the_bootstrap_guard_detector_ignores_imports_inside_functions():
    """对照:函数体里的 `import bootstrap` 不算模块级导入(那是调用时才执行的)。"""
    source = (
        "import importlib.util\n"
        "def f():\n"
        "    if importlib.util.find_spec('vnpy') is not None:\n"
        "        import bootstrap\n"
    )
    tree = ast.parse(source)
    assert not [n for n in _module_level_imports(tree) if _imports_bootstrap(n)]
    assert _module_level_bootstrap_guard(tree) is None


def test_the_bootstrap_guard_detector_knows_a_flipped_predicate():
    """对照:谓词写成 `is None` 时,守卫判定器要认得出它**不是**那条谓词。"""
    ok = ast.parse(
        "import importlib.util\n"
        "if importlib.util.find_spec('vnpy') is not None:\n"
        "    import bootstrap\n"
    ).body[1]
    flipped = ast.parse(
        "import importlib.util\n"
        "if importlib.util.find_spec('vnpy') is None:\n"
        "    import bootstrap\n"
    ).body[1]
    assert _is_find_spec_vnpy_is_not_none(ok.test)
    assert not _is_find_spec_vnpy_is_not_none(flipped.test)
