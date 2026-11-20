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
    """能写批准记录的只有三个**人在场**的入口:带外批准进程、等价命令行、
    会话内哈希回贴(Issue #59 方案 A:人**亲自**把哈希粘贴进会话;
    哈希绑定与唯一写入者不变,削弱面由 ADR 20261120 记录、用户已接受)。

    毕昇与网关仍一律不许有写路径。
    """
    callers: list[str] = []
    for path in _repo_files():
        callers.extend(_record_approval_calls(path))
    # 按**相对路径**去重:按文件名去重会把不同目录下的同名文件混成一个写者。
    writers = sorted({c.rsplit(":", 1)[0] for c in callers})
    assert set(writers) == {_rel(APPROVAL_SCRIPT), "scripts/research_api.py",
                            "tools/research_mcp/server.py"}, (
        f"批准记录的写入者必须只有带外批准进程、等价 CLI 与会话内回贴入口,实际:{writers}"
    )


def test_only_the_two_human_facing_entrypoints_even_mention_the_approval_writer():
    """除了三个人在场入口,别处连「拿到写入函数」都不许:引用即能力(第 10 轮 codex 的 M4)。

    `writer = rs.record_approval` 之后由框架代为调用,调用点不在这个文件里,
    只查调用点会漏掉这一类。"""
    allowed = {_rel(APPROVAL_SCRIPT), "scripts/research_api.py",
               "tools/research_mcp/server.py"}
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
    'with open("x") as fh, open("y") as gh:\n    import risk.constitution\n',
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
    """MCP 写 approvals.json 的**唯一**路径:会话内哈希回贴的第二阶段(Issue #59 方案 A)。

    原不变量「MCP 完全无写路径」被 Issue #59 **有意修订**(用户 2026-11-20 明确接受削弱,
    ADR 20261120 记录)。修订后的形态约束:
      - `record_approval` 调用点**恰好一个**,且带 `via="inline-chat"`(唯一写入者仍只有
        `research_sandbox.record_approval`,MCP 只是它的新调用方之一);
      - 不许有第二条门:判定器认得 `getattr(rs, "record_" + "approval")`、`__dict__` 下标、
        别名持有等形态——出现任何一种,调用点计数就会超过 1,这里立刻红;
      - 回贴值必须先过**常数时间比较**才落账(防时序侧信道);
      - 页面模式的 URL 照旧不进返回值(由另一条测试管)。

    用**仓库里那套判定器**(`_record_approval_call_lines`),不是自己另写一套词法禁令:
    词法禁令挡不住 `getattr(rs, "record_" + "approval")` 这种写法——两个常量都不含
    `record_approval` 子串,断言照样绿,却给出虚假安全感(第 10 轮 codex 的 H2)。
    """
    source = (MCP_DIR / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    lines = _record_approval_call_lines(tree)
    assert len(lines) == 1, f"record_approval 调用点必须恰好一个(inline 回贴),实际:{lines}"
    assert 'via="inline-chat"' in source, "唯一调用点必须带 via=inline-chat(审计批准通道)"
    assert "secrets.compare_digest" in source, "回贴哈希必须先过常数时间比较再落账"
    # 批准记录的文件路径既用于读(is_approved_sha)也只流向唯一写入者
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
            # 第 16 轮 B 的 M-5:不只认 `os.chdir`——任意接收者的 chdir/getcwd/cwd
            # (`o.chdir` 别名、`os.path.getcwd`、`Path.cwd()`)与裸名(`from os import chdir`)都算。
            name = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else "")
            if name in {"chdir", "getcwd", "cwd", "getcwdb"}:
                raise AssertionError(f"{_rel(path)} 不许依赖进程 CWD:{name}")

    anchors = {
        "scripts/research_config.py": ("CODE_ROOT", "ROOT"),
        "scripts/research_sandbox.py": ("_CODE_ROOT", "_SCRIPT_ROOT"),
        "scripts/segment_ledger.py": ("ROOT",),
        "tools/research_mcp/server.py": ("CODE_ROOT", "ROOT"),    # 第 16 轮 B 的 M-4
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
    # DSH_HOME(Issue #57):DSH 给子进程的环境会剔除所有 DSH_*,research-mcp 要靠它判断是否在严格模式下,
    # 必须显式转发;它同样只能是环境变量引用(下面的逐值检查对它一视同仁)。
    assert set(env) == {"INVEST_ROOT", "BISHENG_OPENAPI_KEY",
                        "BISHENG_BASE_URL", "BISHENG_WORKFLOW_ID", "DSH_HOME"}
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
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                # `with` / `async with` 都没有 orelse 分支(第 14 轮 Low 1 只拆了 AsyncWith,
                # 第 16 轮 B 的 H-1:`with` 还留在带 orelse 的那一组里,一走进来就 AttributeError)。
                walk_body(node.body)
            elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
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


# ---------------------------------------------------------------------------
# CI 闸门接线:核心防线套件必须**真的**被某个 job 跑
# ---------------------------------------------------------------------------

CLOSURE_SUITE = ROOT / "tests" / "test_fs_capability_closure.py"
# 本文件自己:守卫也要有守卫(第 46 轮)——否则"守卫文件被整份跳过"没人拦。
BOUNDARY_SUITE = Path(__file__).resolve()
# 生产入口:纪律守卫要**对着它**核对"硬依赖交易栈"这件事(见下面 `_production_entry_…`)。
PRODUCTION_API = ROOT / "scripts" / "research_api.py"

# 被标成"需要交易栈"但源码里**看不出**依赖的用例:它们对交易栈的依赖是**间接**的
# (子进程要 vnpy / 要读到行情库 / 要扫描本机已装的包),静态看不出来。逐条登记理由——
# 登记项有名字与理由,改动时要一起改,不能悄悄多标一条把 CI 的覆盖偷掉。
TRADING_STACK_INDIRECT = {
    "test_run_window_wraps_the_gate_evaluate_and_backtest_children":
        "子进程走 gate/evaluate/backtest 三个入口,那三个入口要交易栈",
    "test_bars_are_fixed_before_the_candidate_is_imported":
        "要真的取到日线(读行情库),交易栈在位才有数据可取",
    "test_systematic_scan_no_whitelisted_module_hands_out_a_dangerous_origin":
        "扫描本机**已安装**的 vnpy 族包,没装就无从扫起",
    "test_thread_and_process_executors_reexported_by_whitelisted_modules_are_refused":
        "同上:要真模块才能拿到再导出的成员",
    "test_run_child_opens_the_vnpy_database_before_the_window_and_registers_nothing":
        "在**子进程**里 import vnpy.trader.database(peewee 的库对象是进程级单例,"
        "同进程里别的用例建过连接就判不清先后),静态看不出来",
}


def _closure_suite_tests() -> dict[str, ast.FunctionDef]:
    """核心防线套件里的测试函数:名字 → 函数节点。"""
    tree = ast.parse(_read_source(CLOSURE_SUITE))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")
    }


def _is_marked_trading_stack(node: ast.FunctionDef) -> bool:
    """这个用例带 `@requires_trading_stack` 吗?"""
    return any(
        getattr(dec, "id", None) == "requires_trading_stack"
        or (isinstance(dec, ast.Attribute) and dec.attr == "requires_trading_stack")
        for dec in node.decorator_list
    )


def _imports_trading_stack(node: ast.FunctionDef) -> bool:
    """这个用例**自己**在运行时 import 交易栈吗?(直接依赖)

    只看真正的 `import vnpy…` / `from vnpy… import …` 语句。
    """
    for stmt in ast.walk(node):
        if isinstance(stmt, ast.Import):
            if any(alias.name.split(".")[0] == "vnpy" or alias.name.startswith("vnpy_ctastrategy")
                   for alias in stmt.names):
                return True
        elif isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            if module.split(".")[0] == "vnpy" or module.startswith("vnpy_ctastrategy"):
                return True
    return False


def _production_entry_calls(node: ast.FunctionDef) -> list[ast.Call]:
    """这个用例里对生产入口(`run_candidate` → `api.import_candidate`)的调用点。"""
    return [
        stmt for stmt in ast.walk(node)
        if (isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Name)
            and stmt.func.id == "run_candidate")
    ]


def _call_parents(node: ast.FunctionDef) -> dict:
    """函数体内每个节点的父节点(用于判断一次调用处在什么上下文里)。"""
    parents: dict = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


# `except*`(3.11+)用的 `ast.TryStar` **不是** `ast.Try` 的子类——不单独认它,
# `try/except*:` 里的调用就会被判成「没被包住」(漏判 ⇒ 守卫被绕过)。
# 3.10 上没有 `TryStar`,退化即可(CI 跑 3.11/3.12,都有)。
_TRY_NODES = (ast.Try, getattr(ast, "TryStar", ast.Try))


def _handler_reraises(handler) -> bool:
    """这个 except 处理器是不是**原样把异常抛回去**?

    `except X: raise` / `except X: raise` (裸 raise)⇒ 不吞;
    处理器里有**别的**语句 ⇒ 可能吞(保守:按吞处理)。
    """
    saw_raise = False
    for stmt in handler.body:
        if isinstance(stmt, ast.Raise):
            saw_raise = True
            continue
        return False
    return saw_raise


def _try_swallows(try_node) -> bool:
    """这个 `try` 会不会把异常**吞掉**?

    - **没有 handler**(只有 `finally`)⇒ **不吞**:异常照样往上抛;
    - handler 全部**原样重抛**⇒ **不吞**;
    - 其余 ⇒ 吞。
    """
    handlers = getattr(try_node, "handlers", []) or []
    if not handlers:
        return False
    for handler in handlers:
        if not _handler_reraises(handler):
            return True
    return False


def _swallowing_context(expr) -> bool:
    """这个 with 的上下文是不是「会吞异常」的?

    - `pytest.raises(...)` / `raises(...)`(裸名)⇒ 用例期待的是**拒绝**⇒ 吞;
    - `contextlib.suppress(...)` ⇒ 吞;
    - `contextlib.ExitStack()` ⇒ **不一定**吞(里面可以注册任何回调)⇒ 静态判不准 ⇒
      按"判不准 = 当它吞"处理(保守方向,见下)。
    """
    if not isinstance(expr, ast.Call):
        return False
    func = expr.func
    if isinstance(func, ast.Name):
        return func.id in ("raises", "suppress", "ExitStack")
    if isinstance(func, ast.Attribute):
        return func.attr in ("raises", "suppress", "ExitStack")
    return False


def _is_failure_swallowing(call: ast.Call, parents: dict) -> bool:
    """这次调用是不是**被包住**了——失败路径被吞掉。

    「被包住」= 调用点在 `pytest.raises(...)` 的**块体**里,或在 `try:` 的**体**里
    (且那个 try 真的会吞)。此时用例期待的是入口**拒绝**候选:静态筛查先拒,或候选自己先炸,
    生产代码里那句 `from vnpy_ctastrategy import CtaTemplate` **根本没走到**。

    漏判的方向很关键:漏判成「没被包住」⇒ 那次调用被当成"离不开交易栈" ⇒
    **一条其实不需要交易栈的用例也能挂上标记**⇒ 纪律守卫被绕过。
    所以这里对「被包住」的判定必须**宽**,对「没被包住」的判定必须**严**
    (判不准 ⇒ 往「被包住」那边倒)。

    第 48 轮(codex 第 2 轮复核的 M2,即 must-close MC2)补的四种形态:
      - `contextlib.suppress(...)` / `ExitStack()`;
      - `try` **没有 handler**(只有 finally)⇒ **不吞**(旧实现一律当吞,过严);
      - handler **原样重抛**⇒ **不吞**;
      - 调用在**嵌套的辅助函数**里 ⇒ 静态判不准 ⇒ 按「被包住」处理。
    """
    prev = call
    cur = parents.get(call)
    while cur is not None:
        if isinstance(cur, _TRY_NODES):
            # 只有 **try 体**里的调用算被保护(orelse / finally 里的调用不在保护范围里)
            if prev in cur.body and _try_swallows(cur):
                return True
        elif isinstance(cur, ast.With):
            # 只有 **with 体**里的调用算被包住(上下文表达式本身不算)
            if prev in cur.body:
                for item in cur.items:
                    if _swallowing_context(item.context_expr):
                        return True
        elif isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 调用在**嵌套的辅助函数**里 ⇒ 静态判不准 ⇒ 按「被包住」处理(保守方向)。
            # (根函数不在 `parents` 里有父节点,嵌套辅助函数有 ⇒ 用它区分。)
            if parents.get(cur) is not None:
                return True
        prev = cur
        cur = parents.get(cur)
    return False


def _calls_production_entry(node: ast.FunctionDef) -> bool:
    """这个用例**离不开**交易栈吗?(走生产入口,且那次调用没被包住)

    候选源码样板里写着 `from vnpy_ctastrategy import CtaTemplate` **不算**理由——
    第 46 轮(codex 第二轮 M2,Medium):那条判据太弱,候选样板里的字符串就能满足它,
    于是"给一条根本不需要交易栈的用例加上标记"也能通过纪律守卫。
    真正成立的理由是**这条用例走的是生产入口**,而生产入口**硬依赖**交易栈——
    而"硬依赖"这件事要**对着生产代码核对**,不能写死成一句会过期的断言。

    第 47 轮再收紧一层:**那次调用不能被 try/except 或 pytest.raises 包住**。
    实测(codex 第 2 轮点名的 4 条「样板型」用例):它们期待的正是入口**拒绝**候选,
    生产代码里那句 import 根本没走到 ⇒ 在无 vnpy 环境里它们**全绿**。
    没被包住则只有两种可能——走到了那句 import(真的要交易栈),
    或异常直接逃出去、用例失败;两者都说明它**离不开**交易栈。
    """
    parents = _call_parents(node)
    return any(not _is_failure_swallowing(call, parents)
               for call in _production_entry_calls(node))


def _production_entry_hard_requires_the_trading_stack() -> bool:
    """生产入口 `scripts/research_api.py::import_candidate` 是否**硬依赖**交易栈?

    核对的是**生产代码本身**:那里有一条在 `try` **之外**的
    `from vnpy_ctastrategy import CtaTemplate` ⇒ 没装 vnpy 时这条 import 直接抛
    ImportError(不会被 except 吞掉)⇒ 整条生产入口在研究侧 CI 上不可用。

    为什么要每次都去读生产代码,而不是写死一句"生产入口要交易栈":
    写死的那句会随生产代码改动而**悄悄过期**——哪天 vnpy 依赖被拿掉,
    纪律守卫还在说"这些用例需要交易栈",CI 上的覆盖被无声地偷掉,而没人会发现。
    这里让守卫**每次都核对生产代码**:依赖没了,守卫立刻变红。

    第 48 轮(codex 第 2 轮复核的 L1):原来比的是**完整模块名**
    (`module == "vnpy_ctastrategy"`)——生产代码哪天改成
    `from vnpy_ctastrategy.template import CtaTemplate`,守卫就会翻成 False ⇒
    31 条已登记的标记**全被**判成「无理由」⇒ 变红(fail-closed,但是个脆绊线:
    改一行无害的生产代码就把纪律守卫打挂)。现在按**根包名**比
    (`module.split(".")[0] == "vnpy_ctastrategy"`)⇒ 子模块写法同样算硬依赖。
    """
    tree = ast.parse(_read_source(PRODUCTION_API))
    fn = next((node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == "import_candidate"), None)
    if fn is None:
        return False
    # `try` 内部的 import 会被 except 吞掉 ⇒ 那不算硬依赖
    swallowed = set()
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Try):
            for sub in ast.walk(stmt):
                swallowed.add(id(sub))
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.ImportFrom) and (stmt.module or "").split(".")[0] == "vnpy_ctastrategy":
            if id(stmt) not in swallowed:
                return True
    return False


def test_the_trading_stack_marks_are_justified_and_never_blanket():
    """`@requires_trading_stack` 要**逐条有理由**,不许一刀切。

    两头都查:
      - 标了的用例必须**直接**引用交易栈,或登记在 `TRADING_STACK_INDIRECT` 里(附理由);
        否则就是"多标一条,顺手把 CI 的覆盖偷掉"——那比不接线还糟。
      - 未标的用例必须**至少有**一条(整份文件一刀切跳过 = 防线等于没建)。
      - `TRADING_STACK_INDIRECT` 里不许有陈旧条目(用例改名/删除后要一起清)。
    """
    tests = _closure_suite_tests()
    assert tests, f"{CLOSURE_SUITE.name} 里没找到测试函数(解析器改了吗?)"

    marked = {name for name, node in tests.items() if _is_marked_trading_stack(node)}
    unmarked = set(tests) - marked
    assert marked, "没有任何用例被标成需要交易栈——检查是不是把标记删光了"
    assert unmarked, "整份套件都被标成需要交易栈:等于在 CI 上一次都不跑(防线等于没建)"

    # 三档**可核对**的理由(第 46 轮,codex 第二轮 M2):
    #   ① 用例自己 import 交易栈;
    #   ② 用例走生产入口,而生产入口的硬依赖**已对着生产代码核对过**;
    #   ② 用例走生产入口(且那次调用**没被 try/except 包住**),
    #     而生产入口的硬依赖**已对着生产代码核对过**;
    #   ③ 登记在 TRADING_STACK_INDIRECT 里(依赖是间接的,静态看不出来)。
    # **候选源码样板里提到 vnpy 不再算理由**——那太弱,候选样板能满足它。
    production_needs_stack = _production_entry_hard_requires_the_trading_stack()
    unjustified = {
        name for name in marked
        if not (
            _imports_trading_stack(tests[name])
            or (_calls_production_entry(tests[name]) and production_needs_stack)
            or name in TRADING_STACK_INDIRECT
        )
    }
    assert not unjustified, (
        f"这些用例标了 @requires_trading_stack,却既没自己 import 交易栈、"
        f"也没走(硬依赖交易栈的)生产入口、也没登记在 TRADING_STACK_INDIRECT: {sorted(unjustified)}"
        "(多标一条 = 在 CI 上少跑一条防线)")

    stale = set(TRADING_STACK_INDIRECT) - marked
    assert not stale, f"TRADING_STACK_INDIRECT 里有陈旧条目(用例已改名或不再被标记): {sorted(stale)}"


# ---------------------------------------------------------------------------
# 第 46 轮(codex 第二轮 must-close 1/2):核心防线套件不许被"整份一刀切跳过";
#           标记纪律的理由必须**可核对**,且被跳过的用例要有**显式名单**
# ---------------------------------------------------------------------------

# 平台相关的跳过必须逐条登记(用例名 → 理由):改这个清单要一起改。
# 登记的目的:让"CI 上少跑了哪几条"变成一份**显式的、可评审的**清单。
PLATFORM_SKIPS = {
    "test_the_spawn_exemption_is_one_shot_not_sticky":
        "_winapi.CreateProcess 只在 Windows 上存在",
    "test_winapi_createfile_with_write_access_is_judged_by_path":
        "_winapi 只在 Windows 上存在",
    "test_registry_writes_are_denied_inside_the_window":
        "winreg 只在 Windows 上存在",
    "test_file_lock_reports_a_real_permission_error_instead_of_waiting_the_full_timeout":
        "Windows 上对目录做 O_EXCL 创建抛 PermissionError(POSIX 上不会)",
    "test_sqlite3_backed_dbm_and_shelve_are_denied_outside_the_allowed_directory":
        "POSIX 上 dbm 的默认后端是 C 实现(ndbm/gdbm),写入在 C 层发生、不发 Python 审计事件 ⇒ "
        "audit:open-write 规则看不见这次写(真实缺口见 Issue #51);Windows 上默认后端是纯 Python 的 "
        "dbm.dumb ⇒ 审计事件会触发、规则生效。平台无关的那一半(sqlite3.connect)已拆成独立用例。",
}

# 标了 `@requires_trading_stack` 的用例名单(**只增不减**)。
# 纪律守卫判的是"依赖**是不是**真的",判不了"这条用例**该不该**依赖交易栈"——
# 名单把"研究侧 CI 上被跳过了哪几条"变成一份显式清单:
# 多标一条要在这里登记(评审时看得见),少标一条(其实需要)也要在这里补。
TRADING_STACK_MARKED = {
    "test_backtest_reads_market_data_before_the_candidate_is_constructed",
    "test_bars_are_fixed_before_the_candidate_is_imported",
    "test_capability_library_classes_reexported_by_whitelisted_modules_are_refused",
    "test_forward_ref_evaluate_is_denied_by_the_audit_hook",
    "test_get_type_hints_is_refused_with_its_rule_name",
    "test_import_time_pickle_attack_is_denied_through_the_production_entry",
    "test_import_time_write_is_denied_through_the_production_entry",
    "test_importlib_import_module_reexport_is_refused_by_the_object_layer",
    "test_legitimate_typing_usage_still_works",
    "test_library_read_primitives_are_refused_by_name_and_the_rule_is_named",
    "test_normal_candidate_still_imports_through_the_production_entry",
    "test_production_path_candidate_cannot_vacuum_into_through_the_framework_database",
    "test_production_path_refuses_import_module_and_leaves_framework_modules_untouched",
    "test_run_child_opens_the_vnpy_database_before_the_window_and_registers_nothing",
    "test_run_window_wraps_the_gate_evaluate_and_backtest_children",
    "test_save_json_style_primitives_are_not_reachable_by_the_candidate",
    "test_systematic_handle_scan_every_unblocked_handle_returning_member_is_reviewed",
    "test_systematic_scan_no_whitelisted_module_hands_out_a_dangerous_origin",
    "test_the_candidate_cannot_obtain_the_database_handle_through_any_reexport",
    "test_the_candidate_cannot_read_test_segment_bars_through_the_database",
    "test_the_candidate_cannot_read_the_database_through_any_reexport",
    "test_the_candidate_cannot_read_the_process_environment",
    "test_the_data_primitive_rule_applies_to_module_members_only_not_to_template_methods",
    "test_the_library_still_evaluates_its_own_generated_code",
    "test_the_optional_get_args_route_cannot_reach_the_evaluator",
    "test_the_production_import_cannot_rewrite_the_csv_in_the_first_place",
    "test_the_production_import_path_cannot_call_save_json",
    "test_thread_and_process_executors_reexported_by_whitelisted_modules_are_refused",
    "test_threads_processes_paths_reexported_by_whitelisted_modules_are_refused",
    "test_typing_eval_type_is_refused_by_the_module_proxy_at_runtime",
    "test_vnpy_alpha_is_refused_by_the_runtime_import_gate",
}


def _module_level_pytestmark(tree: ast.Module) -> list[ast.AST]:
    """模块级 `pytestmark = …` 的**全部**值节点(没有则空列表)。

    第 48 轮(codex 第 2 轮复核的 H1,即 must-close MC1)修的洞:旧实现只看 `tree.body` 的**顶层**赋值 ⇒
    `if os.name == "nt": pytestmark = pytest.mark.skipif(True)` 这种**包一层**的写法
    整份被漏掉——而那正是最像"顺手写的"一种(想按平台跳过,写成条件赋值)。
    现在递归进 `if`/`try`/`with`/`for`/`while` 的**模块级**体,
    但**不进**函数/类(函数里的 `pytestmark` 对 pytest 无效)。
    """
    found: list[ast.AST] = []

    def walk_body(body) -> None:
        for stmt in body:
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name) and target.id == "pytestmark":
                        found.append(stmt.value)
            elif isinstance(stmt, ast.AnnAssign):
                if isinstance(stmt.target, ast.Name) and stmt.target.id == "pytestmark":
                    found.append(stmt.value)
            elif isinstance(stmt, (ast.If, ast.While, ast.For, ast.AsyncFor)):
                walk_body(stmt.body)
                walk_body(stmt.orelse)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                walk_body(stmt.body)
            elif isinstance(stmt, _TRY_NODES):
                walk_body(stmt.body)
                walk_body(stmt.orelse)
                walk_body(stmt.finalbody)
                for handler in stmt.handlers:
                    walk_body(handler.body)

    walk_body(tree.body)
    return found


def _pytest_mark_aliases(tree: ast.Module) -> tuple[set[str], set[str]]:
    """本地名字 → (标记**命名空间**别名, 标记**工厂**别名)。

    第 48 轮(codex 第 2 轮复核的 M3,即 must-close MC3 的一半)修的洞:旧的 `_mark_names` 只认 `<x>.mark.<y>`,
    于是下面这几种写法全都**看不见** ⇒ 一条"整份一刀切跳过"换个写法就绕过守卫:
        from pytest import mark            → `@mark.skipif(...)`
        from pytest import skipif          → `@skipif(...)`
        skipif = pytest.mark.skipif        → `@skipif(...)`
        mark = pytest.mark                 → `@mark.skip(...)`
    返回两副名单:命名空间别名(像 `mark`,用法是 `mark.skipif`)与工厂别名
    (像 `skipif`,用法是 `skipif(...)`)。
    """
    namespaces: set[str] = set()
    factories: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] == "pytest":
                for alias in node.names:
                    local = alias.asname or alias.name
                    if alias.name == "mark":
                        namespaces.add(local)
                    elif alias.name in ("skip", "skipif", "xfail"):
                        factories.add(local)
        elif isinstance(node, ast.Assign):
            value = node.value
            if not isinstance(value, ast.Attribute):
                continue
            # `X = pytest.mark` / `X = pytest.mark.skipif`
            if (isinstance(value.value, ast.Attribute) and value.value.attr == "mark"
                    and isinstance(value.value.value, ast.Name)
                    and value.value.value.id in ("pytest", "pytest_", "_pytest")):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if value.attr == "mark":
                            namespaces.add(target.id)
                        else:
                            factories.add(target.id)
            # `X = mark` (命名空间别名再赋值)
            elif isinstance(value.value, ast.Name) and value.value.id in namespaces:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        namespaces.add(target.id)
    return namespaces, factories


def _mark_names(node, namespaces: set[str] | None = None,
                factories: set[str] | None = None) -> set[str]:
    """节点里出现的 pytest **标记名**集合(认得别名写法)。

    `namespaces` / `factories` 来自 `_pytest_mark_aliases`;
    认不认别名差别很大:不认 ⇒ `@mark.skipif(...)` 这类写法整条漏掉。
    """
    namespaces = namespaces or set()
    factories = factories or set()
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            # `pytest.mark.<名>` / `<别名>.mark.<名>`
            if isinstance(sub.value, ast.Attribute) and sub.value.attr == "mark":
                names.add(sub.attr)
            # `<命名空间别名>.<名>`(如 `mark.skipif`)
            elif isinstance(sub.value, ast.Name) and sub.value.id in namespaces:
                names.add(sub.attr)
        elif isinstance(sub, ast.Name) and sub.id in factories:
            names.add(sub.id)
    return names


def _skip_calls(node, namespaces: set[str] | None = None,
                factories: set[str] | None = None) -> list[ast.Call]:
    """节点里所有**跳过类**的调用(`skip` / `skipif`),认得别名写法。"""
    namespaces = namespaces or set()
    factories = factories or set()
    calls: list[ast.Call] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Attribute):
            # `pytest.mark.skip` / `mark.skipif` / `pytest.skipif`
            is_mark = ((isinstance(func.value, ast.Attribute) and func.value.attr == "mark")
                       or (isinstance(func.value, ast.Name) and func.value.id in namespaces))
            if func.attr in ("skip", "skipif") and is_mark:
                calls.append(sub)
            # `pytest.skip(...)`:运行期无条件跳过(在用例体里直接退出这条用例)。
            #
            # 第 49 轮(codex 第 2 轮 H1):这一支此前是**死代码**——
            # 外层 `if isinstance(func, ast.Attribute)` 已经把 `func.attr == "skip"`
            # 吃掉了,`elif` 永远轮不到 ⇒ 运行期 `pytest.skip()` **完全不可见**。
            elif func.attr == "skip" and isinstance(func.value, ast.Name) and func.value.id == "pytest":
                calls.append(sub)
        elif isinstance(func, ast.Name) and func.id in factories:
            calls.append(sub)
    return calls


def _skip_calls_under_a_condition(node, namespaces: set[str] | None = None,
                                  factories: set[str] | None = None) -> set[int]:
    """**被某个 if 条件包住**的跳过调用的 id 集合。

    第 54 轮(codex H4 的配套):运行期的 `pytest.skip("...")` 本身**没有条件**,
    但它写在 `if 库里没有账本:` 里时,跳过与否取决于**数据**——
    那不是「这条防线永远不跑」。要区分这两种情况,就得看它**被什么包着**。

    返回 `id()` 集合(id 唯一且对象在树存活期间有效 ⇒ 可用来做成员判断)。
    """
    guarded: set[int] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.If):
            continue
        for call in _skip_calls(sub, namespaces, factories):
            guarded.add(id(call))
    return guarded


def _bare_skip_marks(node, namespaces: set[str] | None = None,
                     factories: set[str] | None = None) -> list:
    """节点里**裸**的跳过装饰器(`@pytest.mark.skip` / `@skip`)。

    第 49 轮(codex 第 2 轮 H1):`_skip_calls` 只收 `ast.Call`,
    而裸装饰器 `@pytest.mark.skip` 是 `ast.Attribute` ⇒ **收不到**。
    后果:一条**已经登记**在 `PLATFORM_SKIPS` 的用例可以被加上裸装饰器
    (⇒ 整条永远不跑),而纪律守卫**全绿**(实测:93 条全绿)。
    登记的是「平台差异(有条件)」,裸装饰器是**无条件** ⇒ 一律不许。

    带括号的形态(`@pytest.mark.skip(reason=...)`)是 `ast.Call` ⇒ 走 `_skip_calls`,
    这里要**排除**掉(否则同一条会被数两遍)。
    """
    namespaces = namespaces or set()
    factories = factories or set()
    call_funcs = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            call_funcs.add(id(sub.func))
    found: list = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute):
            if sub.attr != "skip" or id(sub) in call_funcs:
                continue
            value = sub.value
            if ((isinstance(value, ast.Attribute) and value.attr == "mark")
                    or (isinstance(value, ast.Name) and value.id in namespaces)):
                found.append(sub)
        elif isinstance(sub, ast.Name):
            if sub.id != "skip" or id(sub) in call_funcs:
                continue
            if sub.id in factories:
                found.append(sub)
    return found


def _is_unconditional_skip(call: ast.Call) -> bool:
    """这个跳过调用是不是**无条件**的?

    `skipif(True)`、`skipif(1)`、`skipif(condition=True)`、`pytest.mark.skip(...)`
    都是无条件跳过——**登记在 PLATFORM_SKIPS 里也救不了它**:
    登记的是**平台差异**(有条件),不是"这条防线不跑"。

    第 48 轮(codex 第 2 轮复核的 M4,即 must-close MC3 的另一半)把旧实现(只看 `dec.args[0] is True`)补全成:
    关键字形式 `condition=True`、非布尔真值 `1`、以及**裸** `pytest.mark.skip`。
    """
    func = call.func
    if isinstance(func, ast.Name):
        # `from pytest import skip` ⇒ 无条件
        return func.id == "skip"
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "skip":
        return True                       # 裸 `pytest.mark.skip` 本身就是无条件
    if func.attr != "skipif":
        return False
    condition = call.args[0] if call.args else None
    for keyword in call.keywords:
        if keyword.arg == "condition":
            condition = keyword.value
    if isinstance(condition, ast.Constant):
        return bool(condition.value)      # `skipif(True)` / `skipif(1)` ⇒ 无条件
    return False


def test_the_core_closure_suite_cannot_be_skipped_wholesale():
    """核心防线套件不许被"整份一刀切跳过"(第 46 轮,codex 第二轮 M1,Medium-High)。

    模块级加一行 `pytestmark = pytest.mark.skipif(True, reason="…")` ⇒
    整份文件 169 条全 skipped、pytest 退出码 **0** ⇒ **CI 步骤绿着不跑**。
    这与本轮要消灭的失败模式("闸门看上去配好了,实际没跑")完全同类,
    而且只需要改一行——所以必须有一条守卫盯着它。

    第 48 轮补强(codex 第 2 轮复核的 H1/MC1):旧实现只看**顶层**的 `pytestmark = …`,
    `if 条件: pytestmark = …` 这种**包一层**的写法整份漏掉;现在模块级体**递归**扫。
    """
    for path in _guarded_suites():
        tree = ast.parse(_read_source(path))
        namespaces, factories = _pytest_mark_aliases(tree)
        for value in _module_level_pytestmark(tree):
            skipped = _mark_names(value, namespaces, factories) & {"skip", "skipif"}
            assert not skipped, (
                f"{path.name}: 模块级 pytestmark 里有跳过类标记 {sorted(skipped)}"
                "——整份一刀切跳过 = CI 步骤绿着不跑")


def test_the_core_closure_suite_only_skips_for_registered_reasons():
    """除了 `@requires_trading_stack` 与**登记过的**平台跳过,不许再有别的跳过。

    两个方向都要挡:
      - 换个标记名(`pytest.mark.skip`、参数级 `pytest.param(..., marks=[skipif])`)
        偷跑——那些同样归到"跳过类标记"里;
      - 无条件跳过(`skipif(True)`)——登记也救不了,平台跳过必须是**有条件的**。

    第 48 轮补强(codex 第 2 轮复核的 M3/M4,即 must-close MC3):
      - **认别名**(`from pytest import mark` / `from pytest import skipif` /
        `skipif = pytest.mark.skipif`)——旧实现只认 `<x>.mark.<y>`,换个写法就绕过;
      - **无条件**的判定补全:`skipif(True)`、`skipif(1)`、`skipif(condition=True)`、
        裸 `pytest.mark.skip`(带不带 reason 都一样)。
    """
    for path in _guarded_suites():
        tree = ast.parse(_read_source(path))
        namespaces, factories = _pytest_mark_aliases(tree)
        rel_path = path.relative_to(ROOT).as_posix()
        allowed_skip = _allowed_to_skip(rel_path)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            marks = _mark_names(node, namespaces, factories) & {"skip", "skipif"}
            calls = _skip_calls(node, namespaces, factories)
            bare = _bare_skip_marks(node, namespaces, factories)
            # 运行期 `pytest.skip(...)` **不在** `_mark_names` 里(它不是标记),
            # 所以判据不能只看 `marks`——否则那一条根本走不到下面的检查。
            if not (marks or calls or bare):
                continue
            registered = node.name in PLATFORM_SKIPS
            # 第 54 轮(codex H4 的配套):「允许跳过」(ALLOW_SKIPPED_JUSTIFICATION)
            # 是**数据依赖**的跳过——CI 上没有本地账本就跳过那条一致性对照。
            # 它是**另一类**登记:平台跳过登记的是「有条件的平台差异」,
            # 允许跳过登记的是「有条件的运行期退出」。两者都要求**有条件**。
            allowed_here = node.name in allowed_skip
            assert registered or allowed_here, (
                f"{path.name}:{node.lineno} {node.name} 用了跳过类标记 {sorted(marks)},"
                "但没登记在 PLATFORM_SKIPS 里(平台相关跳过要登记理由;"
                "没登记的跳过 = CI 上少跑一条防线)")
            if allowed_here and not registered:
                # 「允许跳过」**只**豁免**运行期**的跳过,且必须**写在 if 条件里**
                # (数据依赖)——标记类跳过(裸装饰器 / skipif)一律不许:
                # 那不是数据依赖,是整条永远不跑。
                assert not marks and not bare, (
                    f"{path.name}:{node.lineno} {node.name} 用的是**标记类**跳过"
                    f"({sorted(marks) or [ast.unparse(bare[0])[:60]] if bare else sorted(marks)}):"
                    "「允许跳过」只允许运行期、数据依赖的跳过,标记类一律不许")
                guarded = _skip_calls_under_a_condition(node, namespaces, factories)
                for call in calls:
                    assert id(call) in guarded, (
                        f"{path.name}:{node.lineno} {node.name} 的运行期跳过"
                        f"({ast.unparse(call)[:60]})没有写在任何 if 条件里"
                        "⇒ 那不是数据依赖,是无条件不跑这条防线")
                continue
            for call in calls:
                assert not _is_unconditional_skip(call), (
                    f"{path.name}:{node.lineno} {node.name} 是**无条件**跳过"
                    f"({ast.unparse(call)[:80]}):那不是平台差异,是不跑这条防线")
            assert not bare, (
                f"{path.name}:{node.lineno} {node.name} 用了**裸**跳过装饰器"
                f"({ast.unparse(bare[0])[:60]}):裸装饰器没有条件 ⇒ 整条永远不跑,"
                "登记在 PLATFORM_SKIPS 里也救不了它(登记的是**有条件**的平台差异)")


# 探测器自己的探针:这两条是**规则自己**的证据
# (codex 第 2 轮 M3 的教训:「跑红了」不等于「是为这条规则红的」,
#  所以每条规则都要能指出**哪条用例**为它红)。

_BARE_SKIP_SNIPPET = """
import pytest

@pytest.mark.skip
def test_a():
    pass
"""

_RUNTIME_SKIP_SNIPPET = """
import pytest

def test_a():
    pytest.skip("mutation")
"""


def _first_function(tree) -> ast.FunctionDef:
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef))


def test_the_skip_detector_sees_a_bare_skip_decorator():
    """裸的 `@pytest.mark.skip` 必须被探测器看见(第 49 轮 codex H1)。"""
    tree = ast.parse(_BARE_SKIP_SNIPPET)
    node = _first_function(tree)
    namespaces, factories = _pytest_mark_aliases(tree)
    found = _bare_skip_marks(node, namespaces, factories)
    assert found, "裸的 @pytest.mark.skip 没被看见 ⇒ 已登记的用例可以被整条跳过而不被发现"


def test_the_skip_detector_sees_a_runtime_pytest_skip_call():
    """运行期的 `pytest.skip(...)` 必须被看见,而且按**无条件**处理。

    静态判不出它外面有没有条件 ⇒ 往严的那边倒(仓库一贯口径):
    平台差异请用 `@pytest.mark.skipif(条件)`。
    """
    tree = ast.parse(_RUNTIME_SKIP_SNIPPET)
    node = _first_function(tree)
    namespaces, factories = _pytest_mark_aliases(tree)
    calls = _skip_calls(node, namespaces, factories)
    assert calls, "运行期 pytest.skip(...) 没被看见 ⇒ 用例体里的一刀切退出对守卫是隐形的"
    assert all(_is_unconditional_skip(call) for call in calls), (
        "运行期跳过必须按无条件处理(静态判不出条件)")


def test_the_guarded_suites_do_not_hide_tests_inside_a_class():
    """第 48 轮(codex 第 2 轮复核的 L3):**类**形式的用例不在守卫视野里。

    守卫现在只认**模块级**的东西:`_closure_suite_tests()` 收的是模块级的
    `def test_…`;`_module_level_pytestmark()` 扫的是模块层的 `pytestmark` 绑定。
    于是两种形态会**静默逃过**所有守卫:
      - `class TestX:` 里的用例(pytest 只收 `Test*` 开头的类)——
        它不在名单里 ⇒ `tools/check_ci_ran.py` 按名单核对时看不见它;
      - **类体里的** `pytestmark = pytest.mark.skipif(True)`——pytest **会**按它
        跳过整个类,而模块级那道守卫只扫模块层 ⇒ 看不见。

    今天两个套件里都**没有**测试类,所以这是个**潜伏洞**。
    这里的选择是**禁用**这个构造,而不是假装它被管住了:
    要用类写用例,先把守卫的视野补上(并同步 `tests/guard_roster.json`)。
    """
    for path in _guarded_suites():
        tree = ast.parse(_read_source(path))
        offenders = []
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            has_test = any(isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                           and sub.name.startswith("test_") for sub in node.body)
            if node.name.startswith("Test") or has_test:
                offenders.append(node.name)
        assert not offenders, (
            f"{path.name}: 这些类里藏着测试用例 {offenders}——"
            "纪律守卫只看模块级函数(名单核对、标记核对、模块级 pytestmark 都看不见类里的东西):"
            "要么改成模块级函数,要么先把守卫的视野补上再改用类")


def test_the_trading_stack_marks_match_the_registered_roster():
    """被标成"需要交易栈"的用例集合必须与 `TRADING_STACK_MARKED` **完全一致**。

    纪律守卫判的是"依赖**是不是**真的",判不了"这条用例**该不该**依赖交易栈"——
    名单把"研究侧 CI 上少跑了哪几条"变成一份显式清单:
    多标一条要在这里加一行(评审时看得见),少标一条(其实需要)也要补。
    """
    tests = _closure_suite_tests()
    marked = {name for name, node in tests.items() if _is_marked_trading_stack(node)}
    unregistered = marked - TRADING_STACK_MARKED
    assert not unregistered, (
        "这些用例标了 @requires_trading_stack 但没登记在 TRADING_STACK_MARKED 里: "
        f"{sorted(unregistered)}(多标一条 = 在 CI 上少跑一条防线;要标就得在这里登记)")
    stale = TRADING_STACK_MARKED - marked
    assert not stale, (
        f"TRADING_STACK_MARKED 里有陈旧条目(用例已改名或不再被标记): {sorted(stale)}")


# ---------------------------------------------------------------------------
# 第 48 轮(codex 第 2 轮复核 39/50 的 MC1/MC4(=M5/M6)):「谁决定收集到哪几条」也要有守卫,
#           并且把「CI 上必须真跑的用例」变成一份**机器可读**的名单
# ---------------------------------------------------------------------------

# 「CI 上必须真跑的用例」名单(机器可读):CI 侧 `tools/check_ci_ran.py` 与本文件共读同一份。
# 为什么要有它:pytest 的退出码分不出「全跑了」与「整份被跳过」;
# 第 46~47 轮在 CI 侧加了 `--junitxml` 兜底(一条没跑 / 全被跳过 ⇒ 红),
# 但那只能看**总数**——「某条防线被摘掉了、其余还在跑」这种形态仍然看不出来。
# 名单把「哪几条必须真跑」写成显式清单 ⇒ 那条防线一旦没跑,CI 直接红。
GUARD_ROSTER = ROOT / "tests" / "guard_roster.json"

def _guarded_suites() -> list[Path]:
    """纪律守卫**看得见**的套件(第 49 轮:五份)。

    两份核心防线套件 + 三份「守卫工具自己」的用例。
    为什么后三份也要进视野:它们各自守着一个判定点(分档器 / 「真的跑了吗」
    闸门 / 变异脚手架)——守卫的守卫自己没有兜底时,「守卫」这个词就只是自我安慰。
    """
    return [ROOT / rel for rel in GUARD_SUITE_ROSTER]


# 必须在 `tests/guard_roster.json` 里的套件(第 49 轮起是**五份**):
# 两份核心防线套件 + **三份「守卫工具自己」的用例**。
#
# 为什么后三份也要进名单(第 49 轮的自查发现):它们各自守着一个**判定点**
# ——分档器(`check_domain.py`)、「真的跑了吗」闸门(`check_ci_ran.py`)、
# 变异脚手架(`check_guard_mutations.py`)。
# 此前它们**不在**名单里 ⇒ 整份被模块级 `pytestmark` 一刀切跳过、
# 或者某条守卫用例被删掉,CI 都**看不出来**——
# 也就是「守卫的守卫自己没有被兜底」(仓库教训:新写的守卫工具自己也要有人守)。
GUARD_SUITE_ROSTER = (
    "tests/test_fs_capability_closure.py",
    "tests/test_research_boundary.py",
    "tests/test_check_domain.py",
    "tests/test_check_ci_ran.py",
    "tests/test_check_guard_mutations.py",
    # 第 55 轮之二十:宪法层「字符串里藏着的执行原语」那条规则的用例——
    # 它守的是宪法层自己的判定点 ⇒ 同样不许被整份跳过。
    "tests/test_constitution_string_programs.py",
    # 第 55 轮:接线自检工具(`tools/check_ci_wiring.py`)自己的用例——
    # 它守的是「CI 会不会红」这件事本身,同样不许被整份跳过。
    "tests/test_ci_wiring.py",
    # 第 54 轮(codex H4):两份**真钱链路**上的关口也要**逐条点名**核对。
    # 此前它们只能拿到「文件级」核对(出现过、没整份跳过)——
    # 「批准账本只读」那条不变式**单独**被跳过时,闸门照样放行。
    "tests/test_approval_ledger.py",
    "tests/test_research_collect.py",
    # Issue #57:research profile 白名单检查器(`tools/research_mcp/profile_check.py`)自己的用例。
    # 它守的是**模型看得见的工具边界**这个判定点——检查器被悄悄放松(白名单被放宽、
    # 检查被跳过)时,批准链的前提「模型做不到自批」就没人核对了。
    # ⇒ 进视野 + 进名单(逐条点名),不许被整份或逐条跳过。
    "tests/test_research_profile_check.py",
)

# 「允许被跳过」的用例:必须**逐条登记理由**(第 54 轮 codex H4)。
#
# 为什么需要这一段:真钱那两份里有一条用例是**数据依赖**的跳过
# (本地没有账本就跳过)⇒ 它进不了「必须真跑」的名单。
# 此前只有两条路:要么整份不进名单(丢掉逐条核对),
# 要么进名单(闸门在 CI 上永远红)。现在分成两段:
# `must_run` = 必须出现**且不能被跳过**;`allow_skipped` = 必须出现、**允许被跳过**。
#
# **每一项都要写理由**:名字出现在这里,就是「这条防线在 CI 上允许不生效」的
# 书面授权——没有理由就不许出现在名单里。
ALLOW_SKIPPED_JUSTIFICATION: dict[str, dict[str, str]] = {
    "tests/test_approval_ledger.py": {
        "test_reading_is_consistent_with_the_research_side":
            "没有本地账本时这条用例跳过(CI 上永远没有本地账本)",
    },
    "tests/test_research_profile_check.py": {
        "test_the_repo_patches_compose_into_a_passing_profile":
            "要真跑一遍 dsh CLI 才能验(CI 上没有 DSH;设了 INVEST_DSH_CLI 才跑)",
        "test_the_source_scan_matches_the_recorded_registrants":
            "要扫本机 DSH 的包源码才能验(CI 上没有 DSH;设了 DSH_TOOL_SCAN_ROOT 或 INVEST_DSH_CLI 才跑)",
    },
}

# 允许出现在 `conftest.py` 里的「改收集」钩子(**现在是空的**):
# 要加必须先在这里登记(路径::钩子名 → 理由),评审时看得见。
COLLECTION_HOOK_ALLOWLIST: dict[str, str] = {}


def _conftest_files() -> list[Path]:
    """仓库里所有的 `conftest.py`(跳过虚拟环境与隐藏目录)。"""
    found: list[Path] = []
    for path in ROOT.rglob("conftest.py"):
        parts = path.relative_to(ROOT).parts
        if any(part.startswith(".") for part in parts):
            continue                                  # .git / .venv / .pytest_cache
        if any(part in ("venv", "node_modules", "reviewtmp", "site-packages")
               for part in parts):
            continue
        found.append(path)
    return sorted(found)


def test_no_conftest_may_silently_drop_or_deselect_tests():
    """`conftest.py` 的收集钩子能**静默地**把守卫用例摘掉。

    第 48 轮(codex 第 2 轮复核的 H1/MC1)补的洞:守卫盯着两份套件「有没有被跳过」,
    但**谁来决定收集到哪几条**是 conftest 的钩子——在 `tests/conftest.py` 里加一个
    `pytest_collection_modifyitems`,把守卫用例 `deselect` 掉,
    守卫自己当然看不见(CI 报告里它压根没出现)。
    ⇒ 这里把「收集钩子」也纳入守卫:要加必须先登记(现在是空的)。
    """
    hooks = ("pytest_collection_modifyitems", "pytest_ignore_collect")
    for path in _conftest_files():
        tree = ast.parse(_read_source(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in hooks:
                key = f"{_rel(path)}::{node.name}"
                assert key in COLLECTION_HOOK_ALLOWLIST, (
                    f"{key}:conftest 里不许出现改收集的钩子"
                    "(它能静默摘掉守卫用例,而守卫自己看不见);"
                    "要加必须先登记在 COLLECTION_HOOK_ALLOWLIST 并写明理由")
        # conftest 里的模块级 `pytestmark` 同样能把整目录的用例跳过
        namespaces, factories = _pytest_mark_aliases(tree)
        for value in _module_level_pytestmark(tree):
            skipped = _mark_names(value, namespaces, factories) & {"skip", "skipif"}
            assert not skipped, (
                f"{_rel(path)}: conftest 里的模块级 pytestmark 带跳过类标记 {sorted(skipped)}"
                "——它能把整个目录的用例变成 skipped")


# 模块级的**复合块**:里面定义的用例,pytest 也会按条件收集/不收集
# (第 49 轮 codex M2)。名单的采集必须**看得见**它们。
_COMPOUND_BLOCKS = (
    ast.If, ast.Try, ast.With, ast.AsyncWith,
    ast.For, ast.AsyncFor, ast.While,
) + tuple(block for block in (getattr(ast, "TryStar", None),) if block is not None)


def _suite_test_names(path: Path) -> set[str]:
    """一份套件里的测试函数名集合。

    第 49 轮(codex M2):**条件块里的用例定义也要算进去**。
    此前只扫模块**顶层**的函数定义 ⇒ 一条写在 `if False:`(或 `try:`)里的用例
    **既不会被 pytest 收集,也不会进名单** ⇒ 没人发现它被藏起来了。
    现在递归进模块级的 if/try/with/for 块——藏起来的用例会进名单,
    CI 侧那道「点名核对」发现它没被收集 ⇒ 红。

    **不递归进函数体**:测试函数里定义的辅助函数不算用例。
    """
    tree = ast.parse(_read_source(path))
    names: set[str] = set()

    def collect(body) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_"):
                    names.add(node.name)
            elif isinstance(node, _COMPOUND_BLOCKS):
                collect(node.body)
                for extra in (getattr(node, "orelse", None), getattr(node, "finalbody", None)):
                    if extra:
                        collect(extra)
                for handler in (getattr(node, "handlers", None) or []):
                    collect(handler.body)

    collect(tree.body)
    return names


def _expected_must_run(suite: str) -> set[str]:
    """「CI 上必须真跑」的用例集合(按套件各自的规则算)。"""
    if suite == "tests/test_fs_capability_closure.py":
        names = _suite_test_names(CLOSURE_SUITE)
        # 被标成"需要交易栈"的 ⇒ CI 上跳过;平台跳过 ⇒ CI 上也跳过。
        return names - set(TRADING_STACK_MARKED) - set(PLATFORM_SKIPS)
    if suite == "tests/test_research_boundary.py":
        # 纪律守卫自己:**一条都不许在 CI 上跳过**——跳过它就等于没有守卫。
        return _suite_test_names(BOUNDARY_SUITE)
    if suite in GUARD_SUITE_ROSTER:
        # **守卫工具自己的用例**(第 49 轮):它们各自守着一个判定点
        # (分档器 / 「真的跑了吗」闸门 / 变异脚手架)。
        # 这三份此前**不在名单里** ⇒ 整份被跳过、或某条守卫用例被删掉,
        # CI 都看不出来——**守卫的守卫自己没有兜底**。
        #
        # 第 54 轮:真钱那两份也进了名单(逐条点名核对);
        # 其中「允许跳过」的那几条**不算**在必须真跑里(见 ALLOW_SKIPPED_JUSTIFICATION)。
        return _suite_test_names(ROOT / suite) - _allowed_to_skip(suite)
    raise AssertionError(f"名单里出现了未知套件:{suite}")


def _allowed_to_skip(suite: str) -> set[str]:
    """这一份套件里**允许被跳过**的用例(必须逐条登记理由)。"""
    return set(ALLOW_SKIPPED_JUSTIFICATION.get(suite, {}))


# 采集器自己的探针(第 49 轮 codex M2:采集器漏了条件块 ⇒ 用例可以藏起来)
_CONDITIONAL_TEST_SNIPPET = """
import pytest

if False:
    def test_hidden_behind_a_condition():
        pass

try:
    def test_defined_inside_a_try():
        pass
except Exception:
    pass
"""


def test_the_name_collector_sees_a_conditionally_defined_test(tmp_path):
    """名单的采集必须看得见**条件块里**定义的用例(第 49 轮 codex M2)。

    这条是采集器**自己**的证据:采集器漏了条件块 ⇒ 一条用例可以写在
    `if False:` 里不被收集、也不进名单 ⇒ **没有任何东西会发现它被藏起来了**。
    """
    path = tmp_path / "suite.py"
    path.write_text(_CONDITIONAL_TEST_SNIPPET, encoding="utf-8")
    names = _suite_test_names(path)
    assert "test_hidden_behind_a_condition" in names, (
        "if 块里的用例没被采集 => 它可以藏起来,没人会发现")
    assert "test_defined_inside_a_try" in names, (
        "try 块里的用例没被采集 => 它可以藏起来,没人会发现")


def test_the_guard_looks_at_every_suite_the_roster_names():
    """纪律守卫的**视野**必须覆盖名单里的**每一份**套件(第 49 轮)。

    这条是**刻意写成字面量**的:如果它写成「等于 `GUARD_SUITE_ROSTER` 那个常量」,
    那么把 `_guarded_suites()` 改窄(退回只剩那两份核心套件)时,
    守卫会**自己把自己的视野改小**而这条用例仍然绿——那正是它要防的事。

    背景:三份「守卫工具自己」的用例(`test_check_domain.py` /
    `test_check_ci_ran.py` / `test_check_guard_mutations.py`)各自守着一个判定点。
    它们不进守卫的视野 ⇒ 整份被跳过、或某条守卫用例被删掉,
    **在 pytest 这一层没人发现**。第 49 轮把它们同时纳入:
      - 守卫的视野(本条用例盯着);
      - 机器可读名单 `tests/guard_roster.json` ⇒ CI 侧的点名核对(CI 上红)。
    """
    expected = {
        "tests/test_fs_capability_closure.py",
        "tests/test_research_boundary.py",
        "tests/test_check_domain.py",
        "tests/test_check_ci_ran.py",
        "tests/test_check_guard_mutations.py",
        "tests/test_constitution_string_programs.py",
        "tests/test_ci_wiring.py",
        "tests/test_approval_ledger.py",
        "tests/test_research_collect.py",
        "tests/test_research_profile_check.py",
    }
    seen = {path.relative_to(ROOT).as_posix() for path in _guarded_suites()}
    assert seen == expected, (
        f"纪律守卫的视野是 {sorted(seen)},应当是 {sorted(expected)}"
        "(视野窄了 ⇒ 那些套件里的守卫用例被跳过/删掉时,守卫自己看不见)")
    # 名单里的每一份也必须在视野里(两道兜底指的是同一批套件)
    import json
    roster = json.loads(GUARD_ROSTER.read_text(encoding="utf-8"))["must_run"]
    assert set(roster) == expected, (
        f"名单里的套件与守卫的视野必须一致:名单 {sorted(roster)},视野 {sorted(expected)}")


def test_the_guard_roster_matches_the_suites_it_names():
    """机器可读的「CI 上必须真跑的用例」名单必须与套件**完全一致**。

    第 48 轮(codex 第 2 轮复核的 MC4,即 M5/M6):CI 侧那道兜底(`tools/check_ci_ran.py`)
    按**名单**核对报告——名单陈旧(用例改名/删除)就会让那道兜底**名不副实**
    (它查的是一条已经不存在的用例,永远查不到 ⇒ 静默失效)。
    ⇒ 这里让名单与套件**互相**校验,三个方向都要挡:
      - 名单里的名字**不存在**于套件 ⇒ 陈旧(兜底查了个空);
      - 套件里"CI 上应该真跑"的用例**不在**名单里 ⇒ 漏登记(兜底看不到它);
      - 名单里的用例**会**在 CI 上被跳过(在 TRADING_STACK_MARKED / PLATFORM_SKIPS 里)
        ⇒ "必须真跑"是句空话(它一开跑就被跳过,兜底会一直红)。
    """
    import json

    assert GUARD_ROSTER.exists(), (
        f"{GUARD_ROSTER.name} 不存在——CI 侧的「真的跑了吗」闸门没有名单,"
        "等于永远查不到任何一条 ⇒ 静默失效")
    data = json.loads(GUARD_ROSTER.read_text(encoding="utf-8"))
    must_run = data.get("must_run")
    allow_skipped = data.get("allow_skipped", {})
    assert isinstance(must_run, dict), "guard_roster.json 里的 must_run 必须是 {套件路径: [用例名]}"
    assert isinstance(allow_skipped, dict), (
        "guard_roster.json 里的 allow_skipped 必须是 {套件路径: [用例名]}")
    assert must_run, "must_run 是空的 ⇒ CI 侧那道闸门什么也不查"
    # 名单里必须**恰好**是这两份套件:上面那些检查是**按名单里的条目**逐个做的,
    # 所以「把整份套件的条目从名单里删掉」那种改法,逐条检查根本不会碰到它
    # (⇒ 名单少一整份 ⇒ 那一整份的防线**没人点名核对**)。
    # CI 侧那道闸门现在也会因为「点名的那份不在名单里」而红(第 48 轮补的洞),
    # 两边都挡:守卫侧挡「名单被改坏」,CI 侧挡「点名核对静默降级」。
    assert set(must_run) == set(GUARD_SUITE_ROSTER), (
        f"名单里必须是且只是这{len(GUARD_SUITE_ROSTER)}份套件:{sorted(must_run)}"
        "(少一份 ⇒ 那一整份的用例没人点名核对)")

    for suite, names in must_run.items():
        expected = _expected_must_run(suite)
        listed = set(names)
        stale = listed - expected
        assert not stale, (
            f"{GUARD_ROSTER.name}:{suite} 里的名单有**陈旧或不成立**的条目: {sorted(stale)}"
            "(用例改名了、或者它其实在 CI 上会被跳过 ⇒ '必须真跑'是句空话)")
        missing = expected - listed
        assert not missing, (
            f"{GUARD_ROSTER.name}:{suite} **漏登记**了这些用例: {sorted(missing)}"
            "(CI 侧的兜底按名单核对;漏登记 ⇒ 那条防线没跑也没人知道)")

    # 第 54 轮(codex H4):「允许跳过」那一段必须与**登记的理由**完全一致。
    #
    # 为什么要钉:允许跳过 = 「这条防线在 CI 上允许不生效」的书面授权。
    # 如果只改名单文件就能多允许一条,那这份授权就**不需要评审** ⇒ 防线被悄悄松开。
    # 所以:多一条少一条都得改**这份守卫文件**(代码改动 ⇒ 走评审与变异验证)。
    expected_allow = {suite: set(names)
                      for suite, names in ALLOW_SKIPPED_JUSTIFICATION.items() if names}
    assert set(allow_skipped) == set(expected_allow), (
        f"{GUARD_ROSTER.name} 里的 allow_skipped {sorted(allow_skipped)} "
        f"与守卫里登记的理由 {sorted(expected_allow)} 对不上"
        "(多出来的 ⇒ 有人绕过评审就放开了某条防线)")
    for suite, names in allow_skipped.items():
        assert set(names) == expected_allow.get(suite, set()), (
            f"{GUARD_ROSTER.name}:{suite} 的 allow_skipped {sorted(names)} "
            f"与登记的理由 {sorted(expected_allow.get(suite, set()))} 对不上")
        assert suite in must_run, (
            f"{GUARD_ROSTER.name}:{suite} 只在 allow_skipped 里、却不在 must_run 里"
            "⇒ 它压根没被逐条核对,「允许跳过」是空转")
        overlap = set(names) & set(must_run[suite])
        assert not overlap, (
            f"{GUARD_ROSTER.name}:{suite} 的 {sorted(overlap)} 同时在两段里"
            "⇒ 既要求真跑又允许跳过,判据自相矛盾")

    # 名单里不许出现会跳过的东西
    for suite, names in must_run.items():
        if suite != "tests/test_fs_capability_closure.py":
            continue
        for name in names:
            assert name not in TRADING_STACK_MARKED, (
                f"{name} 在 TRADING_STACK_MARKED 里 ⇒ CI 上会被跳过,不能进 must_run")
            assert name not in PLATFORM_SKIPS, (
                f"{name} 在 PLATFORM_SKIPS 里 ⇒ CI 上会被跳过,不能进 must_run")


def _swallowing_of(source: str) -> bool:
    """把一段源码解析成函数,返回「其中那次生产入口调用是不是被包住」。"""
    fn = ast.parse(source).body[0]
    assert isinstance(fn, ast.FunctionDef), fn
    parents = _call_parents(fn)
    calls = _production_entry_calls(fn)
    assert calls, "这段源码里没有 run_candidate 调用"
    return all(_is_failure_swallowing(call, parents) for call in calls)


def test_the_failure_swallowing_detector_covers_every_wrapper_form():
    """「被包住」的判定**自己**也要有探针(第 47 轮之三,实测发现的洞)。

    漏判的方向很关键:漏判成「没被包住」⇒ 那次调用被当成"离不开交易栈"
    ⇒ 一条**其实不需要**交易栈的用例也能挂上标记 ⇒ 纪律守卫被绕过。
    所以「被包住」必须**认全**包裹形态:

    - `try:` 的体(有 handler);
    - `except*`(3.11+)——`ast.TryStar` **不是** `ast.Try` 的子类,漏认就等于漏判;
    - `with pytest.raises(...):` 的块体;
    - `with raises(...):` 的块体(`from pytest import raises` 之后的写法)——
      只认 `pytest.raises` 会让这条形态**漏判**⇒ 守卫被绕过。
    """
    # 基准:没被包住 ⇒ 调用"离不开交易栈"(标记才算有理由)
    assert not _swallowing_of(
        """
def t():
    run_candidate(x)
"""), "裸调用不该被判成'被包住'"

    # try/except 的体
    assert _swallowing_of(
        """
def t():
    try:
        run_candidate(x)
    except Exception:
        pass
"""), "try/except 的体算被包住"

    # except*(3.11+):ast.TryStar 不是 ast.Try 的子类,必须单独认
    assert _swallowing_of(
        """
def t():
    try:
        run_candidate(x)
    except* Exception:
        pass
"""), "except* 的 try 体也算被包住"

    # pytest.raises 的块体
    assert _swallowing_of(
        """
def t():
    with pytest.raises(ValueError):
        run_candidate(x)
"""), "with pytest.raises 的块体算被包住"

    # 不带 pytest. 前缀的 raises(第 47 轮之三补上的形态)
    assert _swallowing_of(
        """
def t():
    with raises(ValueError):
        run_candidate(x)
"""), "with raises 的块体也算被包住(漏判它 = 守卫被绕过)"

    # 反向:handler **里面**的调用是"重试",不是"被包住"——
    # 判成"被包住"会把一条真需要交易栈的用例判成没理由(过度收紧)。
    assert not _swallowing_of(
        """
def t():
    try:
        pass
    except Exception:
        run_candidate(x)
"""), "except 处理器里的调用是重试,不算被包住"

    # ---- 第 48 轮(codex 第 2 轮复核的 M2)补的形态 ----

    # contextlib.suppress:显式吞异常
    assert _swallowing_of(
        """
def t():
    import contextlib
    with contextlib.suppress(Exception):
        run_candidate(x)
"""), "contextlib.suppress 的块体算被包住"

    # ExitStack:里面可以注册任何回调,静态判不准 ⇒ 保守当吞
    assert _swallowing_of(
        """
def t():
    import contextlib
    with contextlib.ExitStack() as stack:
        run_candidate(x)
"""), "ExitStack 判不准 ⇒ 保守当被包住"

    # try 只有 finally、没有 handler ⇒ **不吞**(异常照样往上抛)
    assert not _swallowing_of(
        """
def t():
    try:
        run_candidate(x)
    finally:
        pass
"""), "只有 finally 的 try 不吞异常"

    # handler 原样重抛 ⇒ **不吞**
    assert not _swallowing_of(
        """
def t():
    try:
        run_candidate(x)
    except Exception:
        raise
"""), "handler 原样重抛 ⇒ 不吞"

    # 调用在**嵌套辅助函数**里 ⇒ 静态判不准 ⇒ 保守当吞
    assert _swallowing_of(
        """
def t():
    def helper():
        run_candidate(x)
    helper()
"""), "嵌套辅助函数里的调用判不准 ⇒ 保守当被包住"

    # 上下文表达式本身(不是 with 体)不算被包住
    assert not _swallowing_of(
        """
def t():
    with pytest.raises(ValueError) as info:
        pass
    run_candidate(x)
"""), "with 的上下文表达式本身不算被包住"
