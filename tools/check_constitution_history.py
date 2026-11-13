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
    "protected_entry_points", "protected_state_objects", "protected_paths",
)

# **白名单**类字段:语义相反,必须"只减不增"。
# allowed_overrides 是"候选**可以**重写什么"——多一项就是多放开一点底线,
# 与保护清单"只能追加"的规则正好相反,不能同规则比(第 7 轮 codex 实测:
# `BUILTIN_ALLOWED_OVERRIDES` 加一项 "get_size",守卫退出码 0,而候选重写
# `get_size()` 就能让 300 元单笔预算失效)。
CONSTITUTION_SHRINK_ONLY_FIELDS = ("allowed_overrides",)

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
    "BUILTIN_PROTECTED_ENTRY_POINTS",
    "BUILTIN_PROTECTED_STATE_OBJECTS",
    "BUILTIN_PROTECTED_PATHS",
    "BUILTIN_PROTECTED_CONTAINERS",
    # 运行期第四层(身份指纹)的覆盖范围:这三个包里的模块与类成员不许被候选改写。
    # 少一个包 = 那一个包里的对象不再受指纹保护,所以它同样"只能增不能减"。
    "PROTECTED_RUNTIME_PACKAGES",
    "_PROTECTED_PRELOAD_MODULES",
)

# 同为"白名单"语义的内置常量:只减不增。
BUILTIN_SHRINK_ONLY_SETS = ("BUILTIN_ALLOWED_OVERRIDES",)

# 沙盒层的内置 deny set 同样是"代码内置的保护",一并纳入比对。
SANDBOX_SETS = ("FORBIDDEN_MODULES", "FORBIDDEN_CALLS", "FORBIDDEN_ATTRS", "FORBIDDEN_STRINGS")

# 注册表:(文件相对路径, 该文件里受保护的常量名)
BUILTIN_REGISTRY = (
    (CONSTITUTION_SOURCE_REL, BUILTIN_SETS),
    ("scripts/research_sandbox.py", SANDBOX_SETS),
)

# 清单**字段**的执行点也要守:删掉 `if node.attr in c.forbidden_order_calls:` 这个分支,
# 常量集合没变、清单文件也没变,但下单黑名单不再被执行——只有"字段名被引用的次数"会变。
# 字段名与常量名分开登记,免得两套语义混在一张表里。
FIELD_REGISTRY = ((CONSTITUTION_SOURCE_REL, CONSTITUTION_FIELDS),)

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
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assignments[node.target.id] = node.value      # 带注解的常量定义(`X: frozenset[str] = {...}`)

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


def constant_usage_counts(source: str, names) -> dict[str, int]:
    """统计每个受保护常量在源码里被**引用**的次数(AST 层面,只数 Load)。

    历史守卫原来只比对常量**集合**:把执行点删掉——例如把
    `if name in BUILTIN_FORBIDDEN_DUNDERS:` 整块删掉、或把它窄化成硬编码元组、
    或让 `_is_protected` 恒返回 False——常量集合本身没变,守卫照样全绿:
    常量还在,底线的**执行**没了(第 7 轮 codex 实测:五种改法守卫退出码全是 0)。

    所以这里把"引用次数"也纳入比对:只许增,不许减。少一次引用,就可能少一道闸。"""
    counts = {name: 0 for name in names}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return counts
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in counts:
            counts[node.id] += 1
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load) and node.attr in counts:
            counts[node.attr] += 1
    return counts


def usage_regressions_from_counts(current_counts: dict[str, int], older_source: str, names) -> dict[str, tuple[int, int]]:
    """拿**已经数好的**当前引用次数去比:只解析历史源码那一份。

    第 9 轮 codex 指出这里把同一份当前源码重复解析了很多遍(每个历史版本解析一次)。
    引用次数的语义没变,只是把重复的解析去掉。"""
    tracked = list(names)
    older_counts = constant_usage_counts(older_source, tracked)
    regressions = {}
    for name in tracked:
        if older_counts.get(name, 0) > current_counts.get(name, 0):
            regressions[name] = (older_counts[name], current_counts.get(name, 0))
    return regressions


def usage_regressions(current_source: str, older_source: str, names) -> dict[str, tuple[int, int]]:
    """引用次数减少的名字:{名字: (历史次数, 当前次数)}。

    单独抽出来是为了能对它自己写测试——历史上这条检查对"常量在、但执行点被删"
    完全失效,而失效时没有任何测试会报警。"""
    tracked = list(names)
    current_counts = constant_usage_counts(current_source, tracked)
    return usage_regressions_from_counts(current_counts, older_source, tracked)


def shrink_only_additions(current: set[str], older: set[str]) -> set[str]:
    """白名单里"多出来"的项(当前有、历史没有)——白名单只减不增,多一项就是多放开一点底线。"""
    return set(current) - set(older)


def check_protected_usage(root: Path) -> list[str]:
    """受保护常量与清单字段的**引用次数**相对历史提交不许减少(守的是执行点,不是常量本身)。"""
    problems: list[str] = []
    # (文件, 名字列表, 这些名字是不是"源码里的常量")
    registry = [(rel, names, True) for rel, names in BUILTIN_REGISTRY]
    registry += [(rel, names, False) for rel, names in FIELD_REGISTRY]
    for rel, names, is_constant in registry:
        current_source = (root / rel).read_text(encoding="utf-8")
        if is_constant:
            current = const_string_set(current_source, names)
            # 已经被删掉的常量由集合比对报告(报一次就够);字段名没有集合可比,一律跟踪
            tracked = [name for name in names if name in current]
        else:
            tracked = list(names)
        if not tracked:
            continue
        # 当前源码的引用次数只数一遍(第 9 轮 codex:此前每个历史版本都重新解析一次)
        current_counts = constant_usage_counts(current_source, tracked)
        commits = _commits_touching(root, rel)
        for rev in commits:
            older_source = _read_at(root, rev, rel)
            if older_source is None:
                continue
            for name, (older_count, current_count) in usage_regressions_from_counts(
                    current_counts, older_source, tracked).items():
                problems.append(
                    f"{rel} 里的 {name} 的引用次数从 {older_count} 降到 {current_count}"
                    f"(提交 {rev[:8]}):常量还在但底线的执行点被删了(只能增不能减)")
    return problems


def check_shrink_only(root: Path) -> list[str]:
    """白名单类清单(`allowed_overrides`)相对历史提交**只减不增**。

    保护清单是"只能追加"(多一项=多一道保护),白名单正好相反:
    多一项=多放开一点底线。两种语义用同一条规则比,等于给白名单开了后门。"""
    problems: list[str] = []

    # 清单文件里的白名单字段
    commits = _commits_touching(root, CONSTITUTION_REL)
    if len(commits) >= 2:
        current = json.loads((root / CONSTITUTION_REL).read_text(encoding="utf-8"))
        for rev in commits[1:]:
            raw = _read_at(root, rev, CONSTITUTION_REL)
            if raw is None:
                continue
            older = json.loads(raw)
            for field in CONSTITUTION_SHRINK_ONLY_FIELDS:
                added = set(current.get(field, [])) - set(older.get(field, []))
                if added:
                    problems.append(
                        f"{CONSTITUTION_REL} 的 {field} 新增了 {sorted(added)}(提交 {rev[:8]} 之后)"
                        "(白名单只减不增:多一项就多放开一点底线)")

    # 代码里的白名单常量
    for rel, names in BUILTIN_REGISTRY:
        current_source = (root / rel).read_text(encoding="utf-8")
        current = const_string_set(current_source, names)
        for name in names:
            if name not in BUILTIN_SHRINK_ONLY_SETS or name not in current:
                continue
            for rev in _commits_touching(root, rel):
                older_source = _read_at(root, rev, rel)
                if older_source is None:
                    continue
                older = const_string_set(older_source, (name,)).get(name)
                if older is None:
                    continue
                added = shrink_only_additions(current[name], older)
                if added:
                    problems.append(
                        f"{rel} 里的 {name} 新增了 {sorted(added)}(提交 {rev[:8]} 之后)"
                        "(白名单只减不增:多一项就多放开一点底线)")
    return problems


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
    # template 不在"只能增不减"之列(换模板是合法需求),但**每个版本的清单**都必须指向
    # "那个版本的内置白名单"允许的模板:只改清单里的 template 就等于绕开了白名单。
    # 早先的写法是"当前白名单 vs 旧白名单不相等才报",那只在白名单从未增长过时才成立——
    # 白名单一旦在历史上涨过,这条检查就永远不报(第 7 轮独立复核实测:隔离克隆里
    # 先给白名单加一项、再改清单的 template,守卫退出码 0)。
    # 现在按"逐个版本"检查:该版本清单里的 template 必须在**该版本**的白名单里。
    for rev in commits:
        manifest = read(rev)
        template = manifest.get("template")
        if not template:
            continue
        source = _read_at(root, rev, CONSTITUTION_SOURCE_REL)
        if source is None:
            continue                      # 那个版本里还没有宪法模块:无从判断,跳过
        templates = const_string_set(source, ("BUILTIN_ALLOWED_TEMPLATES",)).get(
            "BUILTIN_ALLOWED_TEMPLATES", set())
        if templates and template not in templates:
            problems.append(
                f"{CONSTITUTION_REL} 在提交 {rev[:8]} 里的 template={template!r} "
                f"不在那一版内置白名单 BUILTIN_ALLOWED_TEMPLATES 里(清单不得指向白名单之外的模板)")
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
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                # 带注解的写法(`FORBIDDEN_X: frozenset[str] = {...}`)同样是常量定义
                name = node.target.id
            else:
                continue
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
    problems += check_protected_usage(root)
    problems += check_shrink_only(root)
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
