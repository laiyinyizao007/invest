#!/usr/bin/env python
"""不可变宪法的"只能追加"守卫:清单文件与代码内置保护项,相对历史提交都只增不减。

**为什么要有这个独立脚本**:这条守卫若寄生在 pytest 里,CI 就得把整个交易环境装上
(`tests/conftest.py` 会 import segment_ledger → bootstrap → vnpy),守卫在 CI 上根本跑不起来——
而"跑不起来"等于"没人执行",这份宪法的保护承诺就是空的(第 6 轮 codex 实测:
那个工作流在自己声明的环境里必然失败,退出码 4)。
所以守卫只用标准库 + git,CI 直接 `python tools/check_constitution_history.py`。

退出码:
- 0 通过;
- 1 发现问题(清单被删了条目、内置保护被删了、注册表与源码不一致);
- 2 环境不可用(浅克隆 / git 不可用)——**同样是失败**:查不了等于没查。
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONSTITUTION_REL = "config/immutable_constitution.json"
CONSTITUTION_SOURCE_REL = "risk/constitution.py"

# 清单文件里"只能增不减"的字段。
CONSTITUTION_FIELDS = (
    "allowed_overrides", "protected_names", "protected_parameters",
    "forbidden_order_calls", "protected_module_attrs",
)

# 代码里的内置保护项(字符串集合)。这些是清单文件之外的保护,删掉一个等于开一个洞,
# 而清单文件的历史比对看不见它们——它们在代码常量里,只能靠比对历史版本来看住。
BUILTIN_SETS = (
    "BUILTIN_ALLOWED_OVERRIDES",
    "BUILTIN_PROTECTED_NAMES",
    "BUILTIN_PROTECTED_PARAMETERS",
    "BUILTIN_FORBIDDEN_ORDER_CALLS",
    "BUILTIN_PROTECTED_MODULE_ATTRS",
    "BUILTIN_FORBIDDEN_DUNDERS",
    "BUILTIN_FORBIDDEN_BY_NAME_SETTERS",
    "BUILTIN_ALLOWED_TEMPLATES",
)

# 沙盒层的内置 deny set 同样是"代码内置的保护",一并纳入比对。
SANDBOX_SETS = ("FORBIDDEN_MODULES", "FORBIDDEN_CALLS", "FORBIDDEN_ATTRS", "FORBIDDEN_STRINGS")

# 注册表:(文件相对路径, 该文件里受保护的常量名)
BUILTIN_REGISTRY = (
    (CONSTITUTION_SOURCE_REL, BUILTIN_SETS),
    ("scripts/research_sandbox.py", SANDBOX_SETS),
)

# 单值常量:不是集合,没有"只增不减"可言。它只能通过"必须落在白名单里"来约束。
SINGLE_CONSTANTS = (("risk/constitution.py", "BUILTIN_TEMPLATE", "BUILTIN_ALLOWED_TEMPLATES"),)

# 会被完整性检查扫到的常量名前缀。
GUARDED_PREFIXES = ("BUILTIN_", "FORBIDDEN_")


class HistoryUnavailable(Exception):
    """环境不可用(浅克隆 / git 不可用):查不了等于没查,调用方必须当失败处理。"""


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    """跑 git。显式按 utf-8 解码:系统默认编码在中文 Windows 上是 cp1252,
    解不出仓库里的中文注释,守卫会以 UnicodeDecodeError 崩掉(而不是干净地失败)。"""
    return subprocess.run(["git", *args], capture_output=True, cwd=str(root),
                          encoding="utf-8", errors="replace")


def require_full_history(root: Path) -> None:
    """浅克隆下 `git log` 只能看到一个提交,比对无从谈起——那是"查不了",必须当失败。"""
    shallow = git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        raise HistoryUnavailable(f"git 不可用: {shallow.stderr.strip()}")
    if shallow.stdout.strip() == "true":
        raise HistoryUnavailable("仓库是浅克隆,无法比对历史(需要完整历史,CI 请用 fetch-depth: 0)")


def const_string_set(source: str, names) -> dict[str, set[str]]:
    """从源码里取出若干常量的字符串集合。

    求值范围(受控,不是完整的 Python 求值器):
    - tuple / list / set 字面量,元素是字符串常量;
    - 常量之间的 `+` 或 `|`(如 `BUILTIN_PROTECTED_NAMES = (...) + BUILTIN_PROTECTED_PARAMETERS`);
    - 引用同文件里已解析的常量名。

    **不支持的形式一律不返回值**——调用方据此断言"每个应检查的常量都被解析到了",
    查不了就报错,不能让它冒充通过。早先的提取器只认 tuple/list,
    `BUILTIN_PROTECTED_NAMES` 是 BinOp,于是这组受保护成员从来没被比对过:
    删掉 `on_disconnect` 之后守卫照样全绿(第 5 轮 codex 实测)。"""
    def evaluate(node, seen):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out = set()
            for elt in node.elts:
                if not isinstance(elt, ast.Constant) or not isinstance(elt.value, str):
                    return None
                out.add(elt.value)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
            left = evaluate(node.left, seen)
            right = evaluate(node.right, seen)
            if left is None or right is None:
                return None
            return left | right
        if isinstance(node, ast.Name):
            return seen.get(node.id)
        return None

    found: dict[str, set[str]] = {}
    assignments = {}
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments[node.targets[0].id] = node.value

    # 按依赖顺序反复求解:常量之间可能互相引用(如 PROTECTED_NAMES 引用 PARAMETERS)
    pending = {name: assignments[name] for name in names if name in assignments}
    for _ in range(len(pending) + 1):
        progressed = False
        for name in list(pending):
            values = evaluate(pending[name], found)
            if values is not None:
                found[name] = values
                del pending[name]
                progressed = True
        if not progressed:
            break
    return found


def removed_protections(current_source: str, older_source: str, names) -> dict[str, set[str]]:
    """从 older 到 current 被删掉的保护项:{常量名: 被删掉的条目}。

    这条比较是"内置保护只增不减"的全部逻辑,单独抽出来是为了能对它自己写测试——
    历史上这条检查对 `BUILTIN_PROTECTED_NAMES` 完全失效,而失效时没有任何测试会报警。"""
    current = const_string_set(current_source, names)
    older = const_string_set(older_source, names)
    removed = {}
    for name, values in older.items():
        diff = values - current.get(name, set())
        if diff:
            removed[name] = diff
    return removed


def _read_at(root: Path, rev: str, rel: str) -> str | None:
    out = git(root, "show", f"{rev}:{rel}")
    if out.returncode != 0:
        return None            # 该版本里文件还不存在:没有内容可比
    return out.stdout


def _commits_touching(root: Path, rel: str) -> list[str]:
    # --full-history:默认的历史简化会把"与某个父提交内容相同"的合并提交剪掉。
    # 那种提交的内容与被剪掉的父提交完全一样,不比也不会漏掉内容;
    # 但带上 --full-history 后列表更全,读起来也更直白。
    out = git(root, "log", "--full-history", "--format=%H", "--", rel)
    if out.returncode != 0:
        raise HistoryUnavailable(f"git 不可用,无法检查 {rel}: {out.stderr.strip()}")
    return [h.strip() for h in out.stdout.split() if h.strip()]


def check_constitution_file(root: Path) -> list[str]:
    """清单文件相对历史提交只增不减(含 template 的配套约束)。"""
    commits = _commits_touching(root, CONSTITUTION_REL)
    if len(commits) < 2:
        # 仓库历史是完整的(浅克隆已在 require_full_history 里排除),清单确实只有一个版本:
        # 没有"上一次"可比,不存在被删的可能,平凡通过。
        return []

    def read(rev: str) -> dict:
        raw = _read_at(root, rev, CONSTITUTION_REL)
        if raw is None:
            raise HistoryUnavailable(f"无法读取历史版本 {rev}")
        return json.loads(raw)

    # "当前"取**工作区**的状态(而不是最近一次改动清单的那个提交):
    # 白名单可能是在另一个提交里改的(那个提交没碰清单),拿工作区的白名单比才说得通。
    current = json.loads((root / CONSTITUTION_REL).read_text(encoding="utf-8"))
    current_source = (root / CONSTITUTION_SOURCE_REL).read_text(encoding="utf-8")
    current_templates = const_string_set(current_source, ("BUILTIN_ALLOWED_TEMPLATES",)).get(
        "BUILTIN_ALLOWED_TEMPLATES", set())

    problems: list[str] = []
    for rev in commits[1:]:
        older = read(rev)
        for field in CONSTITUTION_FIELDS:
            removed = set(older.get(field, [])) - set(current.get(field, []))
            if removed:
                problems.append(f"{CONSTITUTION_REL} 的 {field} 里的 {sorted(removed)} 在后续提交中被删除了(清单只能追加)")
        # template 不在"只能增不减"之列(换模板是合法需求),但换模板必须**同时**改内置白名单:
        # 只改清单里的 template 就等于绕开了白名单——白名单才是真正限制"能换成哪个模板"的东西。
        if older.get("template") != current.get("template"):
            older_source = _read_at(root, rev, CONSTITUTION_SOURCE_REL) or ""
            older_templates = const_string_set(older_source, ("BUILTIN_ALLOWED_TEMPLATES",)).get(
                "BUILTIN_ALLOWED_TEMPLATES", set())
            if older_templates == current_templates:
                problems.append(
                    f"{CONSTITUTION_REL} 的 template 从 {older.get('template')!r} 改成了 {current.get('template')!r},"
                    "但内置白名单 BUILTIN_ALLOWED_TEMPLATES 没有同时变化(换模板必须同时改白名单)")
    return problems


def check_builtin_protections(root: Path) -> list[str]:
    """代码里的内置保护项相对历史提交只增不减。"""
    problems: list[str] = []
    for rel, names in BUILTIN_REGISTRY:
        current_source = (root / rel).read_text(encoding="utf-8")
        current = const_string_set(current_source, names)
        missing = [name for name in names if name not in current]
        if missing:
            # 提取失败就是这条保护没被守着:查不了等于没查,不能让它冒充通过。
            problems.append(f"{rel} 里的内置常量 {missing} 无法解析成字符串集合,历史比对对它们无效")
            continue

        commits = _commits_touching(root, rel)
        if len(commits) < 2:
            continue          # 该文件确实只有一个版本:没有"上一次"可比
        for rev in commits:
            older_source = _read_at(root, rev, rel)
            if older_source is None:
                continue      # 该版本里文件还不存在
            for name, values in removed_protections(current_source, older_source, names).items():
                problems.append(
                    f"{rel} 的 {name} 里的 {sorted(values)} 在后续提交中被删除了(内置保护只能增不能减)")
    return problems


def check_registry_completeness(root: Path) -> list[str]:
    """注册表必须覆盖源码里**全部**字符串集合型的内置保护常量。

    漏登记一个常量 = 那个常量的历史守卫是空的,而它自己不会报警:
    当年 `BUILTIN_PROTECTED_NAMES` 因为提取器不认 BinOp 从未被比对过,
    正是不该漏的那一组(第 5 轮 codex 实测:删掉 on_disconnect 守卫照样全绿)。"""
    problems: list[str] = []
    for rel, names in BUILTIN_REGISTRY:
        source = (root / rel).read_text(encoding="utf-8")
        registered = set(names)
        for node in ast.parse(source).body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            if not name.startswith(GUARDED_PREFIXES) or name in registered:
                continue
            if const_string_set(source, (name,)).get(name) is not None:
                problems.append(f"{rel} 的 {name} 是字符串集合型的内置保护常量,但没登记进历史守卫的注册表")
    return problems


def check_single_constants(root: Path) -> list[str]:
    """单值内置常量必须落在对应的白名单里(它没有"只增不减"可言,只能靠白名单约束)。"""
    problems: list[str] = []
    for rel, name, whitelist in SINGLE_CONSTANTS:
        source = (root / rel).read_text(encoding="utf-8")
        value = _single_string(source, name)
        if value is None:
            problems.append(f"{rel} 的 {name} 无法解析出字符串值")
            continue
        allowed = const_string_set(source, (whitelist,)).get(whitelist, set())
        if value not in allowed:
            problems.append(f"{rel} 的 {name}={value!r} 不在 {whitelist} 里")
    return problems


def _single_string(source: str, name: str) -> str | None:
    """取一个顶层单值字符串常量(如 `BUILTIN_TEMPLATE = "模块:类"`)。"""
    for node in ast.parse(source).body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == name
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)):
            return node.value.value
    return None


def check_all(root: Path) -> list[str]:
    """跑完全部检查,返回问题清单(空列表表示通过)。"""
    require_full_history(root)
    problems: list[str] = []
    problems += check_constitution_file(root)
    problems += check_builtin_protections(root)
    problems += check_registry_completeness(root)
    problems += check_single_constants(root)
    seen: set[str] = set()
    unique = []
    for problem in problems:            # 同一个问题可能对多个历史版本重复出现,报一次就够
        if problem not in seen:
            seen.add(problem)
            unique.append(problem)
    return unique


def main(argv: list[str] | None = None) -> int:
    argv = list(argv or [])
    root = Path(argv[0]).resolve() if argv else ROOT
    # Windows 控制台默认是 cp1252,直接打印中文/符号会抛 UnicodeEncodeError——
    # 那会让**成功路径**也以异常退出(退出码 1),看起来像"检查不通过"。先改成 utf-8。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # 老解释器或已被替换的流:不影响结论
            pass
    try:
        problems = check_all(root)
    except HistoryUnavailable as exc:
        print(f"✗ 无法检查宪法历史(fail-closed): {exc}", file=sys.stderr)
        return 2
    if problems:
        for problem in problems:
            print("✗ " + problem, file=sys.stderr)
        print(f"发现 {len(problems)} 处问题", file=sys.stderr)
        return 1
    print("✓ 宪法清单与代码内置保护项相对历史提交均只增不减")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
