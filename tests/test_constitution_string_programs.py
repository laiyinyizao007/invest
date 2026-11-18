"""第 55 轮之二十(codex 第 2 轮 M2):字符串里藏着的执行原语必须**显式登记**。

**问题**:宪法层按 **AST** 找执行入口(`exec` / `eval` / `compile` 的裸名与属性形式)
⇒ 藏在**字符串**里的原语它**永远看不见**。典型规避形态:

    subprocess.run([sys.executable, "-c", "exec(compile(src, src, 'exec'))", ...])

codex 第 2 轮实测:任何模块都能用同样的写法在字符串里塞 `exec(compile(...))`,
而**两层守卫全绿**(AST 判据 `False`、完备性检查 `[]`)⇒ 那条通道没有守卫看守。

**修法不是「一刀切禁掉 `-c`」**:本仓库自己的变异脚手架**必须**用 `python -c`
把「编译」交给一次性子解释器(只 compile + marshal,**不执行**)
⇒ 一刀切会把修法逼回死路。⇒ 改成**显式登记**:
- `exec` / `eval` 出现在字符串程序里 ⇒ **一律报出来**;
- `compile` 出现在字符串程序里 ⇒ 必须**点名文件 + 写明理由**地登记。

**本文件的两侧探针**:
- 拦截侧:藏起来的原语必须被报出来(而且要**指名文件与行号**);
- 放行侧:登记过的良性样板、仓库自身、`git -c key=value` 都**不许**误报。
"""
import ast
import re
from pathlib import Path

from tools.check_constitution_history import (
    _string_programs,
    check_string_program_allowlist_registry,
    check_string_program_primitives,
)
from tools import check_constitution_history as constitution

ROOT = Path(__file__).resolve().parent.parent

# 藏在 `python -c` 的程序体里的执行原语(codex 的复现脚本就是这个形态)。
EVIL = '''import subprocess
import sys


def run_arbitrary_source(src, path):
    program = "import sys; exec(compile(open(sys.argv[1]).read(), sys.argv[1], 'exec'))"
    return subprocess.run([sys.executable, "-c", program, path], capture_output=True)
'''

# 良性样板:只 compile + marshal,不执行(与本仓库 `_CHILD_COMPILE_PROGRAM` 同形)。
BENIGN = '''import subprocess
import sys

PROGRAM = "import marshal,sys; code=compile(open(sys.argv[1],encoding='utf-8').read(),sys.argv[1],'exec'); sys.stdout.buffer.write(marshal.dumps(code))"


def compile_only(path):
    return subprocess.run([sys.executable, "-c", PROGRAM, path], capture_output=True)
'''

# `git -c core.pager=cat` 的 `-c` 是**配置项**,不是程序体 ⇒ 不该被当成程序体扫。
GIT_CONFIG = '''import subprocess


def show(repo):
    return subprocess.run(["git", "-c", "core.pager=cat", "status"], capture_output=True)
'''


def _root_with(tmp_path: Path, files: dict[str, str]) -> Path:
    """造一个最小仓库骨架(不是 git 仓库 ⇒ 守卫退回按目录遍历)。"""
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_guard_reports_a_primitive_hidden_in_a_string_program(tmp_path):
    """**拦截侧**:藏在 `python -c` 程序体里的 `exec` 必须被报出来。

    这条形态在修之前**两层守卫全绿**(AST 看不见字符串)⇒ 必须报出**文件与行号**,
    否则排障时连「在哪儿藏的」都没有。
    """
    root = _root_with(tmp_path, {"scripts/evil_runner.py": EVIL})
    problems = check_string_program_primitives(root)
    assert any("evil_runner.py" in p and "exec" in p for p in problems), problems
    assert any("evil_runner.py" in p and "第 7 行" in p for p in problems), (
        "必须指名**行号**:%s" % problems)


def test_the_guard_reports_a_compile_that_is_not_registered(tmp_path):
    """**拦截侧**:`compile` 藏在字符串程序里、却**没有登记** ⇒ 必须报出来。

    「只许编译」也是一条要登记的口径 —— 登记是**点名 + 理由**,不是「大家都行」。
    """
    root = _root_with(tmp_path, {"scripts/benign.py": BENIGN})
    problems = check_string_program_primitives(root)
    assert any("benign.py" in p and "compile" in p for p in problems), problems
    assert any("_STRING_COMPILE_ALLOWLIST" in p for p in problems), (
        "报错必须说清「要去哪里登记」:%s" % problems)


def test_the_guard_accepts_a_registered_compile_only_program(tmp_path):
    """**放行侧**:登记过的良性样板**不许**报出来。

    登记的口径是「点名文件 + 写明理由」⇒ 登记过的文件必须能过,
    否则这条规则会把本仓库自己的修法逼回死路。

    **两条断言缺一不可**(第 55 轮之二十五,codex 第 2 轮 L2):
    只断「没报」⇒ **判据全盲**时这条用例照样绿 ⇒ 是个**空测试**
    ⇒ 同一形态**换成真原语**时必须看得见。
    """
    root = _root_with(tmp_path, {"tools/check_guard_mutations.py": BENIGN})
    assert check_string_program_primitives(root) == []
    genuine = BENIGN.replace(
        "import marshal,sys; code=compile(open(sys.argv[1],encoding='utf-8').read(),"
        "sys.argv[1],'exec'); sys.stdout.buffer.write(marshal.dumps(code))",
        "exec(compile(open(sys.argv[1],encoding='utf-8').read(),"
        "sys.argv[1],'exec'))")
    genuine_root = _root_with(tmp_path / "genuine3",
                              {"tools/check_guard_mutations.py": genuine})
    problems = check_string_program_primitives(genuine_root)
    assert any("exec" in p for p in problems), (
        "换成真原语后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s" % problems)


def test_the_guard_refuses_to_let_a_registered_file_smuggle_exec(tmp_path):
    """**拦截侧**:豁免的口径是「只许**编译**、不许**执行**」。

    一个被登记过的文件若在字符串程序里**同时**藏了 `exec` ⇒ 豁免不得罩住它
    (否则「登记」就变成了「万能钥匙」)。

    **注意**(第 55 轮之二十四,claude 第 1 轮复核):此前这里写的是
    `EVIL.replace(X, X)`——**两边的字符串一模一样** ⇒ 那次 replace
    **什么也没改** ⇒ 读代码时以为「拼出了一段新程序体」,其实用的还是
    `EVIL` 本身 ⇒ 一句**空操作**掩盖了「这条用例到底测的是哪一段」的意图。
    ⇒ 现在直接用 `EVIL`(它本来就同时含 `exec` 与 `compile`)。
    """
    evil = EVIL
    root = _root_with(tmp_path, {"tools/check_guard_mutations.py": evil})
    problems = check_string_program_primitives(root)
    assert any("只许**编译**" in p for p in problems), problems


def test_the_guard_does_not_treat_a_git_dash_c_as_a_program(tmp_path):
    """**放行侧**:`git -c key=value` 的 `-c` 是配置项,不是程序体。

    把它当程序体去扫,就会把「仓库里到处是 `-c`」这件事变成噪音 ⇒ 守卫会被
    调宽(有人去加豁免)⇒ 那正是这条规则要防的下场。

    **两条断言缺一不可**(第 55 轮之二十四,claude 第 1 轮复核 LOW-1):
    只断言「没报」⇒ **判据全盲**时这条用例照样绿 ⇒ 是个空测试。
    ⇒ 同一形态**换成真解释器 + 真原语**时必须看得见。
    """
    root = _root_with(tmp_path, {"scripts/git_c.py": GIT_CONFIG})
    assert check_string_program_primitives(root) == []
    genuine = GIT_CONFIG.replace('"git"', '"python"').replace(
        '"core.pager=cat"', '"exec(compile(src, src, \'exec\'))"')
    genuine_root = _root_with(tmp_path / "genuine",
                              {"scripts/git_c.py": genuine})
    problems = check_string_program_primitives(genuine_root)
    assert any("exec" in problem for problem in problems), (
        "换成真解释器 + 真原语后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s"
        % problems)


def test_the_shipped_repository_has_no_unregistered_string_program():
    """**放行侧**:仓库自身必须干净(否则 CI 上宪法层直接红)。

    这条也钉住「本仓库的例外只有一个,且是**点名**的」:
    `tools/check_guard_mutations.py` 的编译样板在豁免名单里,理由写在那里。
    """
    assert check_string_program_primitives(ROOT) == []


def test_the_constitution_check_all_calls_the_string_program_check(monkeypatch):
    """**接线侧**:总检查(`check_all`)必须**真的**调它。

    「探针测得到这个函数」不等于「这个函数被挂在总检查上」——
    挂漏了,`check_all` 照样绿 ⇒ 那条规则就是**写下了但没人跑**
    (仓库教训:「写下了但没人跑」等于没有防线)。
    """
    sentinel = "哨兵:字符串原语检查被调用了"
    monkeypatch.setattr(constitution, "check_string_program_primitives",
                        lambda root: [sentinel])
    monkeypatch.setattr(constitution, "require_full_history", lambda root: None)
    problems = constitution.check_all(ROOT)
    assert any(sentinel in p for p in problems), (
        "总检查没有调用字符串原语检查 ⇒ 那条规则**没被挂上** ⇒ 写下了但没人跑")


def test_the_constitution_check_all_calls_the_registry_check(monkeypatch):
    """**接线侧**:总检查必须**真的**调豁免表的核对。

    豁免表与 ADR 的核对若挂漏了 ⇒ 加一行豁免就永久放行,而没人看见
    ⇒ 同样是「写下了但没人跑」。
    """
    sentinel = "哨兵:豁免表核对被调用了"
    monkeypatch.setattr(constitution, "check_string_program_allowlist_registry",
                        lambda root: [sentinel])
    monkeypatch.setattr(constitution, "require_full_history", lambda root: None)
    problems = constitution.check_all(ROOT)
    assert any(sentinel in p for p in problems), (
        "总检查没有调用豁免表核对 ⇒ 那条规则**没被挂上** ⇒ 写下了但没人跑")


def test_the_mutation_harnesses_hold_no_execution_primitive_in_a_string():
    """**反向探针(第 55 轮之二十的扩展)**:两个脚手架的**字符串程序**里
    不许有 `exec` / `eval`。

    此前那条反向探针按 **AST** 判据 ⇒ 看不见字符串 ⇒ 宪法层与反向探针
    **同时失明**(codex M2 的原话)。现在改用**字符串判据**再钉一遍:
    两个脚手架里只许有「只编译不执行」的样板。
    """
    for rel in ("tools/check_guard_mutations.py", "tools/check_mutations.py"):
        source = (ROOT / rel).read_text(encoding="utf-8")
        programs = _string_programs(ast.parse(source))
        assert programs, "%s 里没有字符串程序 ⇒ 本条探针的前提不成立" % rel
        for lineno, program in programs:
            assert not re.search(r"\b(exec|eval)\s*\(", program), (
                "%s 第 %d 行的**字符串程序**里藏着执行原语:AST 判据看不见它 ⇒ "
                "宪法层对它失明 ⇒ 必须显式登记,而脚手架不该有这一层" % (rel, lineno))


# ---------------------------------------------------------------------------
# 第 55 轮之二十之补:G8 第 1 轮**自己实测**出来的三条绕过路径
# ---------------------------------------------------------------------------
#
# 上一版的判据有三条**可复现的绕过**(探针见 %TEMP%/r190b_probe_holes.py):
#   B:`python -I -c "exec(...)"` —— 解释器与 `-c` 之间隔了 flag ⇒ 漏判;
#   C:`subprocess.run("python -c …", shell=True)` —— 整条命令是**一个字符串** ⇒ 漏判;
#   E:同一个名字在两个函数里被赋成**不同**的字符串 ⇒ 后一个覆盖前一个 ⇒ 漏判。
# ⇒ 每条都要有**自己的**探针,否则下一轮还会漏。

# 解释器与 `-c` 之间隔了一个 flag(本仓库自己也有这种写法:`python -X utf8 -c …`)。
BEHIND_FLAG = '''import subprocess
import sys

PROGRAM = "exec(compile(src, src, 'exec'))"


def run(src):
    return subprocess.run([sys.executable, "-I", "-c", PROGRAM, src])
'''

# 整条命令是**一个字符串**(`shell=True` 形态)。
SHELL_STRING = '''import subprocess

COMMAND = "python -c \\"exec(compile(src, src, 'exec'))\\""


def run(src):
    return subprocess.run(COMMAND, shell=True)
'''

# 同一个名字在两个函数里被赋成**不同**的字符串。
CONFLICTING_BINDINGS = '''import subprocess
import sys


def a(src):
    payload = "exec(compile(src, src, 'exec'))"
    return subprocess.run([sys.executable, "-c", payload, src])


def b():
    payload = "print(1)"
    return payload
'''


def test_the_guard_reports_a_primitive_hidden_behind_an_interpreter_flag(tmp_path):
    """**拦截侧**:解释器与 `-c` 之间**隔了 flag** 的形态必须被报出来。

    上一版只看 `-c` 的**前一个元素** ⇒ `python -I -c …` 整条绕过(实测漏判)。
    本仓库自己就有 `python -X utf8 -c …` 的写法 ⇒ 这个洞是**现实存在**的。
    """
    root = _root_with(tmp_path, {"scripts/behind_flag.py": BEHIND_FLAG})
    problems = check_string_program_primitives(root)
    assert any("behind_flag.py" in p and "exec" in p for p in problems), (
        "解释器与 `-c` 之间隔了 flag ⇒ 上一版会漏判:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_in_a_shell_command(tmp_path):
    """**拦截侧**:`shell=True` 且**整条命令是一个字符串**的形态必须被报出来。

    上一版只处理**列表**形态的命令 ⇒ `subprocess.run("python -c …", shell=True)`
    整条绕过(实测漏判)。
    """
    root = _root_with(tmp_path, {"scripts/shell_string.py": SHELL_STRING})
    problems = check_string_program_primitives(root)
    assert any("shell_string.py" in p and "exec" in p for p in problems), (
        "`shell=True` 的整条命令字符串上一版会漏判:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_in_a_name_bound_in_two_places(tmp_path):
    """**拦截侧**:同一个名字在两处被赋成**不同**字符串时,两个取值都要扫。

    上一版只留**最后一个**取值 ⇒ 前一个里的原语被漏判(实测漏判)。
    机器判不了「这次调用用的是哪一个」⇒ **全部**扫(宁可多报,不可漏报)。
    """
    root = _root_with(tmp_path, {"scripts/conflicting.py": CONFLICTING_BINDINGS})
    problems = check_string_program_primitives(root)
    assert any("conflicting.py" in p and "exec" in p for p in problems), (
        "同名变量的多个取值上一版只会扫最后一个 ⇒ 会漏判:%s" % problems)


# ---------------------------------------------------------------------------
# 豁免表不许只活在代码里(否则「登记」就是万能钥匙)
# ---------------------------------------------------------------------------

def test_the_allowlist_matches_the_registry_in_the_adr():
    """**放行侧**:代码里的豁免表必须与 **ADR 里那张登记表**一致。

    豁免表本来是**代码里的手写常量** ⇒ 加一行就永久放行那个文件在字符串里
    `exec`,**没有任何人看见** ⇒「显式登记」实际是「万能钥匙」
    ⇒ 把它与 ADR 挂钩:代码加了条目而 ADR 没登记 ⇒ 报出来。
    """
    assert check_string_program_allowlist_registry(ROOT) == []


def test_the_registry_check_catches_an_unregistered_allowlist_entry(monkeypatch):
    """**拦截侧**:代码里多出一条豁免而 ADR 没登记 ⇒ 必须报出来。

    这条就是「万能钥匙」的**反证**:登记不是「写进代码就算」,
    而是**代码与 ADR 两边都要有**,少一边都红。

    **第 55 轮之二十一(codex 第 2 轮 F3)**:此前这条用例往 **exec 侧**的登记通道里
    塞一条 ⇒ 而那条通道**已被删掉**(结构上不存在「登记一个 exec 就能永久放行」这条路)
    ⇒ 用例改用**还存在的** `compile` 通道来证明同一件事:
    **规则没变**,只是那条通道换了个名字。
    """
    original = dict(constitution._STRING_COMPILE_ALLOWLIST)
    original["scripts/sneaky.py"] = "临时登记一个理由"
    monkeypatch.setattr(constitution, "_STRING_COMPILE_ALLOWLIST", original)
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("sneaky.py" in p and "没有登记在 ADR" in p for p in problems), (
        "代码多出的豁免条目没有被报出来 ⇒ 加一行即永久放行:%s" % problems)


def test_the_registry_check_catches_an_adr_entry_the_code_does_not_have(tmp_path, monkeypatch):
    """**拦截侧**:ADR 登记了而**代码里没有** ⇒ 必须报出来(文档比代码宽)。

    否则 ADR 会变成「看起来登记过、实际没生效」的假承诺。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(
        "<!-- string-program-allowlist:begin -->\n"
        "| 文件 | 允许的原语 | 理由 |\n"
        "|---|---|---|\n"
        "| tools/check_guard_mutations.py | compile | 只编译不执行 |\n"
        "| scripts/ghost.py | compile | 只在文档里登记 |\n"
        "<!-- string-program-allowlist:end -->\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("ghost.py" in p and "代码里没有" in p for p in problems), (
        "ADR 多写的登记没有被报出来 ⇒ 文档比代码宽:%s" % problems)


def test_the_registry_check_requires_a_reason(tmp_path, monkeypatch):
    """**拦截侧**:登记**必须写明理由** ⇒ 空理由必须被报出来。

    没有理由的登记等于「随手加一行」⇒ 正是要防的那件事。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(
        "<!-- string-program-allowlist:begin -->\n"
        "| 文件 | 允许的原语 | 理由 |\n"
        "|---|---|---|\n"
        "| tools/check_guard_mutations.py | compile |  |\n"
        "<!-- string-program-allowlist:end -->\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("没有写理由" in p for p in problems), (
        "空理由的登记没有被报出来:%s" % problems)


def test_the_registry_check_reports_a_missing_registry(tmp_path, monkeypatch):
    """**拦截侧**:ADR 里**根本没有那张登记表** ⇒ 必须报出来。

    「登记表缺席」= 所有豁免都成了隐形放行 ⇒ 查不了 ≠ 干净。
    """
    adr = tmp_path / "adr.md"
    adr.write_text("# 没有登记表\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("找不到豁免登记表" in p for p in problems), (
        "登记表缺席没有被报出来:%s" % problems)


# ---------------------------------------------------------------------------
# 第 55 轮之二十一(codex 第 2 轮 F1/F2/F3/F4):上一版的判据比声明的**窄**
# ---------------------------------------------------------------------------
#
# codex 第 2 轮用探针实测出**六条可复现的绕过** + **一条制度性的洞**:
# 上一版的 ADR 说「只认**静态可确定**的程序体」,可实现连多类**静态可确定**
# 的程序体都不扫 ⇒ 文档比代码**宽** ⇒ 读者以为覆盖了,其实没有。
# 下面每条都有**专属的拦截侧用例** + **专属的变异**。

# 海象赋值:`if program := "exec(...)"`(codex F1-1)
WALRUS = '''import subprocess
import sys

if program := "exec(compile(src, src, \'exec\'))":
    subprocess.run([sys.executable, "-c", program])
'''

# `-c` 被绑定成静态字符串(codex F1-2)
BOUND_FLAG = '''import subprocess

PY = "python"
FLAG = "-c"
PROGRAM = "exec(compile(src, src, \'exec\'))"
subprocess.run([PY, FLAG, PROGRAM])
'''

# 解释器被绑定成静态字符串(codex F1-2)
BOUND_INTERPRETER = '''import subprocess

PY = "python"
PROGRAM = "exec(compile(src, src, \'exec\'))"
subprocess.run([PY, "-c", PROGRAM])
'''

# bytes 程序体(codex F1-3)
BYTES_BODY = '''import subprocess
import sys

subprocess.run([sys.executable, "-c", b"exec(compile(src, src, \'exec\'))"])
'''

# 原语名被拆开(codex F1-4)
GETATTR_BODY = '''import subprocess
import sys

subprocess.run([sys.executable, "-c", "import builtins; getattr(builtins, \'exec\')(src)"])
'''

# `subprocess.getoutput` / `getstatusoutput`:整条命令是一个字符串(codex F2-1)
GETOUTPUT_BODY = '''import subprocess

subprocess.getoutput("python -c \\"exec(compile(src, src, \'exec\'))\\"")
'''

# `run = subprocess.run` 之后用别名调用(codex F2-2)
ALIAS_BODY = '''import subprocess
import sys

run = subprocess.run
run([sys.executable, "-c", "exec(compile(src, src, \'exec\'))"])
'''

# 放行侧:程序体里没有原语 ⇒ 不许报
CLEAN_BODY = '''import subprocess
import sys

subprocess.run([sys.executable, "-c", "print(\'hello\')"])
'''

# 放行侧:绑定出来的 flag **不是** `-c` ⇒ 不许当成程序体
BOUND_OTHER_FLAG = '''import subprocess

FLAG = "--version"
subprocess.run(["git", FLAG, "status"])
'''

# 命令列表是**关键字参数**(第 55 轮之二十二,自审 M1)
KEYWORD_ARGS_BODY = '''import subprocess
import sys

subprocess.run(args=[sys.executable, "-c", "exec(compile(src, src, \'exec\'))"])
'''

# 解释器绑定到**属性**(第 55 轮之二十二,自审 M2)
BOUND_ATTR_INTERPRETER = '''import subprocess
import sys

EXE = sys.executable

subprocess.run([EXE, "-c", "exec(compile(src, src, \'exec\'))"])
'''

# **实参位置上的海象**(第 55 轮之二十二,自审 M3)
INLINE_WALRUS_BODY = '''import subprocess
import sys

subprocess.run([sys.executable, "-c", (b := "exec(compile(src, src, \'exec\'))")])
'''

# **点号导入**之后的名字(第 55 轮之二十二,自审 M4)
DOTTED_IMPORT_BODY = '''import os.path

os.system("python -c \\"exec(compile(src, src, \'exec\'))\\"")
'''

# 放行侧:关键字参数形式、但程序体里**没有原语**
KEYWORD_ARGS_CLEAN = '''import subprocess
import sys

subprocess.run(args=[sys.executable, "-c", "print(\'hello\')"])
'''

# 放行侧:绑定出来的属性**不是解释器**
BOUND_NON_INTERPRETER = '''import os
import subprocess

TARGET = os.getcwd

subprocess.run([TARGET, "-c", "exec(compile(src, src, \'exec\'))"])
'''


def test_the_guard_reports_a_primitive_bound_by_a_walrus(tmp_path):
    """**拦截侧**:海象赋值(`if program := "exec(...)"`)里的原语必须被报出来。

    codex 第 2 轮 F1-1 实测:上一版只认 `ast.Assign` / `ast.AnnAssign`
    ⇒ 海象绑定**根本没进绑定表** ⇒ 程序体被判成「不可静态确定」⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/walrus.py": WALRUS})
    problems = check_string_program_primitives(root)
    assert any("walrus.py" in p and "exec" in p for p in problems), (
        "海象赋值里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_with_a_bound_dash_c(tmp_path):
    """**拦截侧**:`-c` 被绑定成静态字符串(`FLAG = "-c"`)⇒ 必须被报出来。

    codex 第 2 轮 F1-2 实测:上一版只认**字面量** `-c` ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/bound_flag.py": BOUND_FLAG})
    problems = check_string_program_primitives(root)
    assert any("bound_flag.py" in p and "exec" in p for p in problems), (
        "绑定出来的 `-c` 里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_with_a_bound_interpreter(tmp_path):
    """**拦截侧**:解释器被绑定成静态字符串(`PY = "python"`)⇒ 必须被报出来。

    codex 第 2 轮 F1-2 实测:上一版的解释器只认**字面量与属性**
    ⇒ `PY = "python"` 认不出是解释器 ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/bound_interp.py": BOUND_INTERPRETER})
    problems = check_string_program_primitives(root)
    assert any("bound_interp.py" in p and "exec" in p for p in problems), (
        "绑定出来的解释器里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_in_a_bytes_program(tmp_path):
    """**拦截侧**:bytes 程序体(`b"exec(...)"`)必须被报出来。

    codex 第 2 轮 F1-3 实测:`subprocess.run([sys.executable, "-c", b"..."])`
    是合法的 ⇒ 上一版只认 `str` ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/bytes_body.py": BYTES_BODY})
    problems = check_string_program_primitives(root)
    assert any("bytes_body.py" in p and "exec" in p for p in problems), (
        "bytes 程序体里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_behind_getattr(tmp_path):
    """**拦截侧**:原语名被拆开(`getattr(builtins, 'exec')`)必须被报出来。

    codex 第 2 轮 F1-4 实测:上一版只找字面 `exec(` ⇒ 原语名一拆开就看不见。
    """
    root = _root_with(tmp_path, {"scripts/getattr_body.py": GETATTR_BODY})
    problems = check_string_program_primitives(root)
    assert any("getattr_body.py" in p and "exec" in p for p in problems), (
        "getattr 混淆里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_passed_to_getoutput(tmp_path):
    """**拦截侧**:`subprocess.getoutput` / `getstatusoutput` 里的原语必须被报出来。

    codex 第 2 轮 F2-1 实测:这两个入口把**整条命令**当字符串交给 shell
    ⇒ 程序体就藏在里面 ⇒ 上一版的入口表里没有它们 ⇒ 漏判。
    """
    for name in ("getoutput", "getstatusoutput"):
        source = GETOUTPUT_BODY.replace("getoutput", name)
        root = _root_with(tmp_path, {"scripts/%s.py" % name: source})
        problems = check_string_program_primitives(root)
        assert any("%s.py" % name in p and "exec" in p for p in problems), (
            "`subprocess.%s` 里的原语被漏判:%s" % (name, problems))


def test_the_guard_reports_a_primitive_passed_through_an_alias(tmp_path):
    """**拦截侧**:`run = subprocess.run` 之后用别名调用 ⇒ 必须被报出来。

    codex 第 2 轮 F2-2 实测:上一版只解析**导入**别名,不解析**赋值**别名。
    """
    root = _root_with(tmp_path, {"scripts/alias_body.py": ALIAS_BODY})
    problems = check_string_program_primitives(root)
    assert any("alias_body.py" in p and "exec" in p for p in problems), (
        "别名调用里的原语被漏判:%s" % problems)


def test_the_guard_says_when_a_python_file_cannot_be_parsed(tmp_path):
    """**拦截侧**:读不了/语法错的文件必须**出声** ⇒ 「查不了」≠「干净」。

    codex 第 2 轮 F4 实测:上一版直接 `continue` ⇒ 一个语法错误的文件能让
    整条判据对它「报干净」⇒ 那不是「没问题」,是「没看」。
    """
    broken = EVIL + "\nif:\n"
    root = _root_with(tmp_path, {"scripts/broken.py": broken})
    problems = check_string_program_primitives(root)
    assert any("查不了" in p for p in problems), (
        "读不了的文件没有被说出来:%s" % problems)


def test_the_guard_reports_exec_even_for_a_registered_file(tmp_path):
    """**拦截侧**:被登记(compile)的文件里出现 `exec` ⇒ **exec 本身也要报**。

    「只许编译、不许执行」= 豁免**罩不住** exec
    ⇒ 不能只提醒一句「只许编译」就放过。
    """
    root = _root_with(tmp_path, {"tools/check_guard_mutations.py": EVIL})
    problems = check_string_program_primitives(root)
    # **断言必须指名是哪一条消息**(第 55 轮之二十三,实测):
    # 上一版只断言「含 `exec` 且含 `字符串程序`」
    # ⇒ 而「豁免只许编译、不许执行」那条消息**也**同时含这两个词
    # ⇒ 把「exec 那一支」掐掉之后,这条断言**照样绿**
    # ⇒ 纪律脚手架的变异「不再报出已登记文件里的 exec」因此**没被见证抓住**
    # (实测:189 条变异里有 1 条「见证没红」)。⇒ 现在断言**exec 那一支的原话**。
    assert any("把 `exec`/`eval` 藏在" in p for p in problems), (
        "已登记文件里的 exec 没有被报出来(豁免罩住了它):%s" % problems)
    assert any("只许**编译**" in p for p in problems), problems


def test_the_registry_check_refuses_an_exec_row(tmp_path, monkeypatch):
    """**拦截侧**:ADR 登记表里写 `exec` ⇒ 必须报出来(本仓库**没有**这条通道)。

    codex 第 2 轮 F3 实测:此前留着 exec 侧的登记列 ⇒ 「代码与 ADR 两边
    同时加一行」就能**永久放行** ⇒ 与「只许编译、不许执行」直接矛盾。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(
        "<!-- string-program-allowlist:begin -->\n"
        "| 文件 | 允许的原语 | 理由 |\n"
        "|---|---|---|\n"
        "| scripts/x.py | exec | 想放行 |\n"
        "<!-- string-program-allowlist:end -->\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("只认 `compile`" in p for p in problems), (
        "ADR 里写 `exec` 没有被报出来:%s" % problems)


def test_the_guard_has_no_registration_channel_for_exec():
    """**结构侧**:代码里**没有** `exec` 的登记通道。

    「显式登记」若还能登记 exec ⇒ 加一行即永久放行
    ⇒ 与「豁免只许编译、不许执行」矛盾(codex 第 2 轮 F3)。
    """
    assert not hasattr(constitution, "_STRING_EXEC_ALLOWLIST"), (
        "又出现了 exec 的登记通道 ⇒ 加一行即永久放行")


def test_the_guard_does_not_report_a_program_without_primitives(tmp_path):
    """**放行侧**:程序体里没有原语 ⇒ **不许**报出来。

    判据被调宽 ⇒ 守卫被调宽 ⇒ 防线变松 ⇒ 放行侧必须一起钉住。

    **两条断言缺一不可**(第 55 轮之二十四,claude 第 1 轮复核 LOW-1):
    只断言「没报」⇒ **判据全盲**时这条用例照样绿 ⇒ 是个空测试。
    ⇒ 同一形态的程序体**换上原语**时必须看得见。
    """
    root = _root_with(tmp_path, {"scripts/clean.py": CLEAN_BODY})
    assert check_string_program_primitives(root) == []
    genuine = CLEAN_BODY.replace(
        "print(\'hello\')", "exec(compile(src, src, \'exec\'))")
    genuine_root = _root_with(tmp_path / "genuine",
                              {"scripts/clean.py": genuine})
    problems = check_string_program_primitives(genuine_root)
    assert any("exec" in problem for problem in problems), (
        "换上原语后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s" % problems)


def test_the_guard_does_not_treat_a_bound_flag_that_is_not_dash_c(tmp_path):
    """**放行侧**:绑定出来的 flag **不是** `-c` ⇒ 不许当成程序体去扫。

    `git -c key=value` 的 `-c` 是**配置项**;同理 `FLAG = "--version"`
    ⇒ 报错成「藏了原语」就是误报 ⇒ 守卫被调宽 ⇒ 防线变松。

    **两条断言缺一不可**(第 55 轮之二十四,claude 第 1 轮复核 LOW-1):
    只断言「没报」⇒ **判据全盲**时这条用例照样绿 ⇒ 是个空测试。
    ⇒ 同一个 flag **换成 `-c`** 时必须看得见。
    """
    root = _root_with(tmp_path, {"scripts/other_flag.py": BOUND_OTHER_FLAG})
    assert check_string_program_primitives(root) == []
    genuine = BOUND_OTHER_FLAG.replace(
        'FLAG = "--version"', 'FLAG = "-c"').replace(
        '"git"', '"python"').replace(
        '"status"', '"exec(compile(src, src, \'exec\'))"')
    genuine_root = _root_with(tmp_path / "genuine",
                              {"scripts/other_flag.py": genuine})
    problems = check_string_program_primitives(genuine_root)
    assert any("exec" in problem for problem in problems), (
        "flag 换成 `-c` 后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s"
        % problems)


def test_the_guard_reports_a_primitive_when_the_command_list_is_a_keyword_argument(tmp_path):
    """**拦截侧**:命令列表是**关键字参数**(`args=[...]`)⇒ 必须被报出来。

    第 55 轮之二十二(自审 M1,实测):`subprocess.run(args=[...])` 是**合法**的
    ⇒ 上一版只看**位置参数** ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/kw_args.py": KEYWORD_ARGS_BODY})
    problems = check_string_program_primitives(root)
    assert any("kw_args.py" in p and "exec" in p for p in problems), (
        "关键字参数形式里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_interpreter_is_bound_to_an_attribute(tmp_path):
    """**拦截侧**:解释器绑定到**属性**(`EXE = sys.executable`)⇒ 必须被报出来。

    第 55 轮之二十二(自审 M2,实测):绑定表只收**字符串**
    ⇒ `EXE = sys.executable` 认不出是解释器 ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/attr_interp.py": BOUND_ATTR_INTERPRETER})
    problems = check_string_program_primitives(root)
    assert any("attr_interp.py" in p and "exec" in p for p in problems), (
        "绑定成属性的解释器里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_program_is_an_inline_walrus(tmp_path):
    """**拦截侧**:程序体是**实参位置上的海象** ⇒ 必须被报出来。

    第 55 轮之二十二(自审 M3,实测):`_strings_of` 只认字面量与 `ast.Name`
    ⇒ 海象两样都不是 ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/inline_walrus.py": INLINE_WALRUS_BODY})
    problems = check_string_program_primitives(root)
    assert any("inline_walrus.py" in p and "exec" in p for p in problems), (
        "实参位置上的海象里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_after_importing_a_dotted_module(tmp_path):
    """**拦截侧**:`import os.path` 之后的 `os.system` ⇒ 必须被报出来。

    第 55 轮之二十二(自审 M4,实测):导入表把 `os` 记成 `os.path`
    ⇒ `os.system` 被算成 `os.path.system` ⇒ 不在入口表里 ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/dotted_import.py": DOTTED_IMPORT_BODY})
    problems = check_string_program_primitives(root)
    assert any("dotted_import.py" in p and "exec" in p for p in problems), (
        "点号导入之后的入口里的原语被漏判:%s" % problems)


def test_the_guard_sees_a_keyword_argument_program_without_reporting_it(tmp_path):
    """**放行侧**:关键字参数形式**要看得见**,但没有原语时**不许**报。

    两条断言缺一不可:
      - `programs` 非空 ⇒ 判据**确实看见了**那个程序体
        (否则这条用例在「判据退化成什么都不看」时也会绿 ⇒ 空测试);
      - `problems` 为空 ⇒ 判据被调宽不许造成误报。
    """
    body = KEYWORD_ARGS_CLEAN
    root = _root_with(tmp_path, {"scripts/kw_clean.py": body})
    programs = _string_programs(ast.parse(body))
    assert programs, (
        "关键字参数形式根本没被当成程序体 ⇒ 拦截侧那条用例是空的")
    assert check_string_program_primitives(root) == []


def test_the_guard_does_not_treat_a_bound_attribute_as_an_interpreter(tmp_path):
    """**放行侧**:绑定出来的属性**不是解释器** ⇒ 不许当成 `python -c`。

    `TARGET = os.getcwd` 之后 `subprocess.run([TARGET, "-c", body])`
    ⇒ 那不是解释器 ⇒ 程序体不该被扫 ⇒ 报了就是误报 ⇒ 守卫被调宽。

    **两条断言缺一不可**(第 55 轮之二十四,claude 第 1 轮复核 LOW-1):
    只断言「没报」⇒ **判据全盲**时这条用例照样绿 ⇒ 是个空测试。
    ⇒ 同一个属性**换成真解释器**时必须看得见。
    """
    body = BOUND_NON_INTERPRETER
    root = _root_with(tmp_path, {"scripts/non_interp.py": body})
    programs = _string_programs(ast.parse(body))
    assert not programs, (
        "绑定出来的非解释器属性被当成了解释器 ⇒ 判据被调宽:%s" % programs)
    assert check_string_program_primitives(root) == []
    genuine = body.replace("import os\n", "import os\nimport sys\n").replace(
        "TARGET = os.getcwd", "TARGET = sys.executable")
    genuine_root = _root_with(tmp_path / "genuine",
                              {"scripts/non_interp.py": genuine})
    genuine_problems = check_string_program_primitives(genuine_root)
    assert any("exec" in problem for problem in genuine_problems), (
        "换成真解释器后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s"
        % genuine_problems)


# ---------------------------------------------------------------------------
# 第 55 轮之二十三(codex 第 2 轮 39/50 的逐条处置):
# G8 第 2 轮(codex CLI)**实测复现**了 10 条漏判 + 3 条误报面。
# 每条漏判都配**拦截侧**用例;每条误报面都配**放行侧**用例
# (放行侧同时断言「同一份文件换成真解释器时**看得见**」⇒ 不会变成空测试)。
# ---------------------------------------------------------------------------

# 命令列表**绑定到变量**(codex 第 2 轮 High-1:最常见的写法)
BOUND_COMMAND_LIST = '''import subprocess
import sys

CMD = [sys.executable, "-c", "exec(compile(src, src, 'exec'))"]

subprocess.run(CMD)
'''

# **星号导入**之后用裸名调用(codex 第 2 轮 M1)
STAR_IMPORTED_ENTRY = '''from subprocess import *
import sys

run([sys.executable, "-c", "exec(compile(src, src, 'exec'))"])
'''

# `os.execv` 族(codex 第 2 轮 M2:整族执行入口)
OS_EXECV_FAMILY = '''import os
import sys

os.execv(sys.executable, [sys.executable, "-c", "exec(compile(src, src, 'exec'))"])
'''

# **变参形态**的入口(codex 第 2 轮 M3)
ASYNCIO_SUBPROCESS = '''import asyncio
import sys

asyncio.create_subprocess_exec(sys.executable, "-c", "exec(compile(src, src, 'exec'))")
'''

# **入口用 getattr 取**(codex 第 2 轮 M4:入口侧此前没有 getattr 混淆)
GETATTR_ENTRY = '''import subprocess
import sys

run = getattr(subprocess, "run")

run([sys.executable, "-c", "exec(compile(src, src, 'exec'))"])
'''

# 解释器**绑定链**(codex 第 2 轮 M1:`A = sys.executable; B = A`)
CHAINED_INTERPRETER = '''import subprocess
import sys

A = sys.executable
B = A

subprocess.run([B, "-c", "exec(compile(src, src, 'exec'))"])
'''

# **bytes 解释器**(codex 第 2 轮 M1:POSIX 上合法)
BYTES_INTERPRETER = '''import subprocess

subprocess.run([b"python3", b"-c", b"exec(compile(src, src, 'exec'))"])
'''

# 解释器在 **`executable=` 关键字参数**里(codex 第 2 轮 M1)
EXECUTABLE_KEYWORD = '''import subprocess
import sys

subprocess.Popen(["-c", "exec(compile(src, src, 'exec'))"], executable=sys.executable)
'''

# 命令列表是**星号实参**(codex 第 2 轮 M1)
STARRED_COMMAND_LIST = '''import subprocess
import sys

subprocess.run(*[sys.executable, "-c", "exec(compile(src, src, 'exec'))"])
'''

# 命令列表是**字典展开**(codex 第 2 轮 M1)
DICT_UNPACKED_COMMAND = '''import subprocess
import sys

subprocess.run(**{"args": [sys.executable, "-c", "exec(compile(src, src, 'exec'))"]})
'''

# 解释器**经字符串绑定链**传递(`PY = "python"; Q = PY`)
CHAINED_STRING_INTERPRETER = '''import subprocess

PY = "python"
Q = PY

subprocess.run([Q, "-c", "exec(compile(src, src, 'exec'))"])
'''

# 放行侧:任意对象的 `.executable` **不是**解释器(codex 第 2 轮 Low-1)
ARBITRARY_EXECUTABLE_ATTRIBUTE = '''import subprocess


class Cfg:
    executable = "mytool"


config = Cfg()
EXE = config.executable

subprocess.run([EXE, "-c", "compile(src, src, 'exec')"])
'''

# 放行侧:名字**只是以 python 开头**的假解释器(codex 第 2 轮 Low-1)
PYTHONISH_NAME = '''import subprocess

subprocess.run(["pythonista-tool", "-c", "compile(src, src, 'exec')"])
'''

# 放行侧:shell 命令里**只是提到了** eval(codex 第 2 轮 Low-1)
SHELL_MENTIONING_EVAL = '''import os

os.system("echo 'eval(' >> notes.txt")
'''


def test_the_guard_reports_a_primitive_when_the_command_list_is_bound_to_a_variable(tmp_path):
    """**拦截侧**:命令列表**绑定到变量** ⇒ 必须被报出来。

    codex 第 2 轮(High-1)实测:上一版只展开**内联**列表
    ⇒ `CMD = [...]` 之后 `subprocess.run(CMD)` 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/bound_cmd.py": BOUND_COMMAND_LIST})
    problems = check_string_program_primitives(root)
    assert any("bound_cmd.py" in p and "exec" in p for p in problems), (
        "绑定到变量的命令列表里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_after_a_star_import(tmp_path):
    """**拦截侧**:`from subprocess import *` 之后的裸名调用 ⇒ 必须被报出来。

    codex 第 2 轮(M1)实测:星号导入之后名字**没有任何来源可查**
    ⇒ 上一版直接跳过 ⇒ 整条漏判。
    """
    root = _root_with(tmp_path, {"scripts/star_import.py": STAR_IMPORTED_ENTRY})
    problems = check_string_program_primitives(root)
    assert any("star_import.py" in p and "exec" in p for p in problems), (
        "星号导入之后的入口里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_in_an_os_execv_call(tmp_path):
    """**拦截侧**:`os.execv` 族 ⇒ 必须被报出来。

    codex 第 2 轮(M2)实测:这一族**整族**不在入口表里 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/execv.py": OS_EXECV_FAMILY})
    problems = check_string_program_primitives(root)
    assert any("execv.py" in p and "exec" in p for p in problems), (
        "os.execv 族里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_in_an_asyncio_subprocess(tmp_path):
    """**拦截侧**:`asyncio.create_subprocess_exec`(变参形态)⇒ 必须被报出来。

    codex 第 2 轮(M3)实测:命令是**逐个位置参数**,不是一个列表
    ⇒ 按「列表」去找元素 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/async_sub.py": ASYNCIO_SUBPROCESS})
    problems = check_string_program_primitives(root)
    assert any("async_sub.py" in p and "exec" in p for p in problems), (
        "asyncio 变参入口里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_entry_is_taken_by_getattr(tmp_path):
    """**拦截侧**:入口用 `getattr` 取 ⇒ 必须被报出来。

    codex 第 2 轮(M4)实测:**原语侧**有 getattr 的混淆正则,**入口侧**此前没有。
    """
    root = _root_with(tmp_path, {"scripts/getattr_entry.py": GETATTR_ENTRY})
    problems = check_string_program_primitives(root)
    assert any("getattr_entry.py" in p and "exec" in p for p in problems), (
        "getattr 取到的入口里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_interpreter_is_bound_through_another_name(tmp_path):
    """**拦截侧**:解释器**经另一个名字**绑定(`A = sys.executable; B = A`)⇒ 必须报出来。

    codex 第 2 轮(M1)实测:绑定表只解一层 ⇒ `B` 这层断了 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/chained.py": CHAINED_INTERPRETER})
    problems = check_string_program_primitives(root)
    assert any("chained.py" in p and "exec" in p for p in problems), (
        "绑定链上的解释器里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_in_a_bytes_program_with_a_bytes_interpreter(tmp_path):
    """**拦截侧**:**bytes 解释器**(`b"python3"`)⇒ 必须被报出来。

    codex 第 2 轮(M1)实测:上一版只认 `str` 字面量 ⇒ bytes 解释器漏判。
    """
    root = _root_with(tmp_path, {"scripts/bytes_interp.py": BYTES_INTERPRETER})
    problems = check_string_program_primitives(root)
    assert any("bytes_interp.py" in p and "exec" in p for p in problems), (
        "bytes 解释器里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_interpreter_is_the_executable_keyword(tmp_path):
    """**拦截侧**:解释器在 **`executable=`** 关键字参数里 ⇒ 必须被报出来。

    codex 第 2 轮(M1)实测:`executable=` 不在视野 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/executable_kw.py": EXECUTABLE_KEYWORD})
    problems = check_string_program_primitives(root)
    assert any("executable_kw.py" in p and "exec" in p for p in problems), (
        "executable= 关键字里的解释器那条被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_command_list_is_starred(tmp_path):
    """**拦截侧**:命令列表是**星号实参** ⇒ 必须被报出来。

    codex 第 2 轮(M1)实测:位置参数是 `ast.Starred` ⇒ 上一版取不到 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/starred.py": STARRED_COMMAND_LIST})
    problems = check_string_program_primitives(root)
    assert any("starred.py" in p and "exec" in p for p in problems), (
        "星号实参里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_command_list_is_unpacked_from_a_dict(tmp_path):
    """**拦截侧**:命令列表是**字典展开**(`**{"args": [...]}`)⇒ 必须被报出来。

    codex 第 2 轮(M1)实测:关键字参数的 `arg` 是 `None`
    ⇒ 上一版两个分支都取不到 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/dict_unpack.py": DICT_UNPACKED_COMMAND})
    problems = check_string_program_primitives(root)
    assert any("dict_unpack.py" in p and "exec" in p for p in problems), (
        "字典展开的命令列表里的原语被漏判:%s" % problems)


def test_the_guard_reports_a_primitive_when_the_interpreter_is_bound_through_a_string(tmp_path):
    """**拦截侧**:解释器**经字符串绑定链**传递(`PY = "python"; Q = PY`)⇒ 必须报出来。

    codex 第 2 轮(M1)实测:绑定表只认**直接**字面量 ⇒ `Q` 这一层断了 ⇒ 漏判。
    """
    root = _root_with(tmp_path, {"scripts/chained_str.py": CHAINED_STRING_INTERPRETER})
    problems = check_string_program_primitives(root)
    assert any("chained_str.py" in p and "exec" in p for p in problems), (
        "字符串绑定链上的解释器里的原语被漏判:%s" % problems)


def test_the_guard_does_not_treat_an_arbitrary_executable_attribute_as_an_interpreter(tmp_path):
    """**放行侧**:任意对象的 `.executable` **不是**解释器 ⇒ 不许当成 `python -c`。

    codex 第 2 轮(Low-1)实测:上一版「属性名在 {executable,…} 里就算」
    ⇒ `config.executable`(那个可执行文件根本不是 Python)也当解释器 ⇒ 误报。

    **两条断言缺一不可**:误报不许有;而同一形态**换成真解释器时必须看得见**
    ⇒ 否则这条用例在「判据退化成什么都不看」时也会绿 ⇒ 空测试。
    """
    body = ARBITRARY_EXECUTABLE_ATTRIBUTE
    root = _root_with(tmp_path, {"scripts/arb_exec.py": body})
    assert check_string_program_primitives(root) == [], (
        "任意对象的 `.executable` 被当成了解释器 ⇒ 误报")
    genuine = body.replace("config.executable", "sys.executable").replace(
        "import subprocess\n", "import subprocess\nimport sys\n")
    assert _string_programs(ast.parse(genuine)), (
        "换成真解释器后判据却什么都看不见 ⇒ 这条放行侧用例是空的")


def test_the_guard_does_not_treat_a_name_that_only_starts_with_python_as_an_interpreter(tmp_path):
    """**放行侧**:`pythonista-tool` 这种**名字像**解释器的 ⇒ 不许当解释器。

    codex 第 2 轮(Low-1)实测:上一版是 `startswith("python")` ⇒ 误报。
    """
    body = PYTHONISH_NAME
    root = _root_with(tmp_path, {"scripts/pythonish.py": body})
    assert check_string_program_primitives(root) == [], (
        "只是以 python 开头的名字被当成了解释器 ⇒ 误报")
    genuine = body.replace('"pythonista-tool"', '"python3"')
    assert _string_programs(ast.parse(genuine)), (
        "真解释器却看不见 ⇒ 这条放行侧用例是空的")


def test_the_guard_does_not_treat_a_shell_command_that_merely_mentions_eval_as_a_program(tmp_path):
    """**放行侧**:shell 命令里**只是提到了** `eval(` ⇒ 不许当字符串程序。

    codex 第 2 轮(Low-1)实测:上一版把**任何**含 `eval(` 的 shell 字符串
    都当程序体 ⇒ `os.system("echo 'eval(' >> notes.txt")` 也被报出来 ⇒ 误报。
    """
    body = SHELL_MENTIONING_EVAL
    root = _root_with(tmp_path, {"scripts/shell_eval.py": body})
    assert check_string_program_primitives(root) == [], (
        "只是提到 eval 的 shell 命令被当成了字符串程序 ⇒ 误报")
    genuine = body.replace(
        '"echo \'eval(\' >> notes.txt"',
        '"python -c \'exec(compile(src, src, \\\'exec\\\'))\'"')
    assert _string_programs(ast.parse(genuine)), (
        "真的 python -c 却看不见 ⇒ 这条放行侧用例是空的")


# --------------------------------------------------------------------------
# 第 55 轮之二十四:claude CLI 第 1 轮复核(G8 第 1 轮)的发现 —— 逐条复现后逐条修
#
# **这一节的每一条都是「先复现、再修、再钉住」**:
# claude 第 1 轮报出的形态 ⇒ 写一份最小复现(本地**实测**)⇒ 确认漏判/误报
# ⇒ 修 ⇒ 两侧探针(拦截侧 + 放行侧)⇒ 一条变异 + 一个见证。
#
# **复现脚本在 %TEMP%**(不进仓库):`r277_replicate_round24.py`
# --------------------------------------------------------------------------

# 拦截侧:解释器是 `python.exe`(Windows 上**最常见**的写法)
PYTHON_EXE_INTERPRETER = '''import subprocess

subprocess.run(["python.exe", "-c", "exec(compile(src, src, 'exec'))"])
'''

# 拦截侧:解释器是 `pythonw`
PYTHONW_INTERPRETER = '''import subprocess

subprocess.run(["pythonw", "-c", "exec(compile(src, src, 'exec'))"])
'''

# 拦截侧:**模块别名**(`mod = subprocess` ⇒ `mod.run([...])`)
MODULE_ALIAS = '''import subprocess

mod = subprocess
mod.run(["python", "-c", "exec(compile(src, src, 'exec'))"])
'''

# 拦截侧:asyncio 的 shell 入口,命令是**关键字实参** `cmd=`
ASYNCIO_SHELL_KEYWORD = '''import asyncio

asyncio.create_subprocess_shell(cmd="python -c \\"exec(compile(src, src, 'exec'))\\"")
'''

# 拦截侧:程序体走**标准输入**(`input=`),命令表里没有 `-c`
STDIN_PROGRAM = '''import subprocess
import sys

subprocess.run([sys.executable], input="exec(compile(src, src, 'exec'))")
'''

# 拦截侧:flag 是**拼接**出来的(`"-c" + BODY`)
CONCATENATED_FLAG = '''import subprocess
import sys

BODY = "exec(compile(src, src, 'exec'))"
subprocess.run([sys.executable, "-c" + BODY])
'''

# 拦截侧:flag 是**f-string**(`f"-c{BODY}"`)
FSTRING_FLAG = '''import subprocess
import sys

BODY = "exec(compile(src, src, 'exec'))"
subprocess.run([sys.executable, f"-c{BODY}"])
'''

# 拦截侧:程序体在 **heredoc** 里(喂给解释器的标准输入)
HEREDOC_PROGRAM = '''import os

os.system("python - <<'EOF'\\nexec(compile(src, src, 'exec'))\\nEOF")
'''

# 放行侧:`re.compile(...)` / `py_compile.compile(...)` 是**模块自己的函数**,
# 不是内建的 `compile` ⇒ 不许当成原语
DOTTED_COMPILE = '''import subprocess
import sys

subprocess.run([sys.executable, "-c", "import re; re.compile(src)"])
subprocess.run([sys.executable, "-c", "import py_compile; py_compile.compile(src)"])
'''

# 拦截侧:flag 与程序体**粘在同一个元素里**(`-cVALUE` 形态)
# python 的短选项解析允许 `-cVALUE` ⇒ 这个元素**同时**是 flag 和程序体。
ATTACHED_FLAG = '''import subprocess
import sys

subprocess.run([sys.executable, "-c exec(compile(src, src, 'exec'))"])
'''


def _assert_reported(root, rel, primitive="exec"):
    """这个文件里的原语必须被报出来(而且要**指名文件与原语**)。"""
    problems = check_string_program_primitives(root)
    assert any(rel in problem and primitive in problem for problem in problems), (
        "没有报出来(%s / %s):%s" % (rel, primitive, problems))
    return problems


def test_the_guard_reports_a_primitive_behind_a_python_dot_exe_interpreter(tmp_path):
    """**拦截侧**:解释器是 `python.exe` ⇒ 原语必须被报出来。

    claude 第 1 轮实测:第 55 轮之二十三那次「收紧解释器名」把 `.exe` 收没了
    ⇒ `python.exe`(Windows 上**最常见**的写法)反而漏判 ⇒ 那是**回归**。
    """
    rel = "scripts/py_exe.py"
    root = _root_with(tmp_path, {rel: PYTHON_EXE_INTERPRETER})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_behind_a_pythonw_interpreter(tmp_path):
    """**拦截侧**:解释器是 `pythonw` ⇒ 原语必须被报出来。

    claude 第 1 轮实测:上一版只认 `python` / `python3` ⇒ `pythonw` 漏判。
    """
    rel = "scripts/pyw.py"
    root = _root_with(tmp_path, {rel: PYTHONW_INTERPRETER})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_behind_a_module_alias(tmp_path):
    """**拦截侧**:`mod = subprocess` 之后 `mod.run([...])` ⇒ 必须被报出来。

    claude 第 1 轮实测:上一版把 `mod` 当成普通名字 ⇒ 入口名算成 `mod.run`
    ⇒ 不在入口表里 ⇒ 整条漏判。
    """
    rel = "scripts/module_alias.py"
    root = _root_with(tmp_path, {rel: MODULE_ALIAS})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_behind_the_asyncio_shell_keyword(tmp_path):
    """**拦截侧**:`asyncio.create_subprocess_shell(cmd=…)` ⇒ 必须被报出来。

    claude 第 1 轮实测:命令是**关键字实参** `cmd` ⇒ 上一版只按位置参数取 ⇒ 漏判。
    """
    rel = "scripts/asyncio_shell.py"
    root = _root_with(tmp_path, {rel: ASYNCIO_SHELL_KEYWORD})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_passed_through_stdin(tmp_path):
    """**拦截侧**:程序体走 `input=`(标准输入)⇒ 必须被报出来。

    claude 第 1 轮实测:命令表里**没有** `-c` ⇒ 程序体是喂进去的那一段
    ⇒ 上一版只看 `-c` ⇒ 整条漏判。
    """
    rel = "scripts/stdin_program.py"
    root = _root_with(tmp_path, {rel: STDIN_PROGRAM})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_when_the_flag_is_built_by_concatenation(tmp_path):
    """**拦截侧**:flag 是 `"-" + "c"` 之类**拼出来**的 ⇒ 必须被报出来。

    claude 第 1 轮实测:上一版只认**字面量**与**绑定名** ⇒ 这个实参两样都不是
    ⇒ 整条漏判。
    """
    rel = "scripts/concat_flag.py"
    root = _root_with(tmp_path, {rel: CONCATENATED_FLAG})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_when_the_flag_is_built_by_an_f_string(tmp_path):
    """**拦截侧**:flag 是 f-string(`f"-c{BODY}"`)⇒ 必须被报出来。

    claude 第 1 轮实测:上一版把 f-string 的动态段换成**占位符** ⇒ 元素变成
    `"-c\\x00"` ⇒ 认不出它是 `-c` ⇒ 整条漏判。
    """
    rel = "scripts/fstring_flag.py"
    root = _root_with(tmp_path, {rel: FSTRING_FLAG})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_hidden_in_a_heredoc(tmp_path):
    """**拦截侧**:程序体在 **heredoc** 里 ⇒ 必须被报出来。

    claude 第 1 轮实测:`os.system("python - <<'EOF'\\n…\\nEOF")` 的程序体是
    **喂给解释器的标准输入** ⇒ 命令里**没有** `-c` ⇒ 上一版整条漏判。
    """
    rel = "scripts/heredoc.py"
    root = _root_with(tmp_path, {rel: HEREDOC_PROGRAM})
    _assert_reported(root, rel)


def test_the_guard_reports_a_primitive_when_the_flag_is_attached_to_the_program(tmp_path):
    """**拦截侧**:flag 与程序体**粘在同一个元素里** ⇒ 必须被报出来。

    claude 第 1 轮实测:python 的短选项解析允许 `-cVALUE` ⇒
    `["python", "-c exec(...)"]` 里那个元素**同时**是 flag 和程序体
    ⇒ 上一版只认「元素正好等于 `-c`,程序体在**下一个**元素」⇒ 整条漏判。

    **这条为什么单列**(而不是靠拼接/f-string 那两条):
    拼接与 f-string 都是**先拼出** `-cVALUE` 这个**字符串**再交给这一段判据;
    ⇒ 这三条规则**共用**同一段识别代码 ⇒ 若只留拼接/f-string 的见证,
    这条规则失灵时**没有专属见证**会红(被它们的红顶替)⇒ **见证必须指向
    唯一那一条规则**(仓库教训)。
    """
    rel = "scripts/attached_flag.py"
    root = _root_with(tmp_path, {rel: ATTACHED_FLAG})
    _assert_reported(root, rel)


def test_the_guard_does_not_treat_a_dotted_compile_as_the_builtin_primitive(tmp_path):
    """**放行侧**:`re.compile(...)` / `py_compile.compile(...)` **不是**内建原语。

    claude 第 1 轮实测:上一版用 `\\bcompile\\s*\\(` ⇒ `.` 是非词字符
    ⇒ **词边界成立** ⇒ 被误报成「把 `compile` 藏在字符串程序里」
    ⇒ 逼人去登记豁免 ⇒ 那正是这条规则要防的「守卫被调宽」。

    **两条断言缺一不可**:误报不许有;而**真**的 `compile(` 必须看得见
    ⇒ 否则判据全盲时这条用例照样绿。
    """
    rel = "scripts/dotted_compile.py"
    root = _root_with(tmp_path, {rel: DOTTED_COMPILE})
    problems = check_string_program_primitives(root)
    assert not [p for p in problems if rel in p], (
        "带点号的 `compile` 被当成内建原语 ⇒ 误报:%s" % problems)
    genuine = DOTTED_COMPILE.replace(
        "import re; re.compile(src)", "compile(src, src, 'exec')")
    genuine_root = _root_with(tmp_path / "genuine",
                              {rel: genuine})
    genuine_problems = check_string_program_primitives(genuine_root)
    assert any(rel in problem for problem in genuine_problems), (
        "真的 `compile(` 却看不见 ⇒ 这条放行侧用例是空的:%s" % genuine_problems)


def test_the_guard_says_when_the_shared_interpreter_name_implementation_is_missing(
        tmp_path, monkeypatch):
    """**拦截侧**:「什么算 python 解释器」那份**共享实现**拿不到 ⇒ 必须出声。

    第 55 轮之二十四:那份实现现在**只有一份**
    (`tools/check_ci_wiring.py::is_python_interpreter_name`)⇒ 拿不到它
    ⇒ 这条判据**判不了** ⇒ 「查不了」不等于「干净」⇒ 必须报出来。
    """
    monkeypatch.setattr(constitution, "_shared_python_names", lambda: None)
    root = _root_with(tmp_path, {"scripts/whatever.py": CLEAN_BODY})
    problems = check_string_program_primitives(root)
    assert any("查不了" in problem for problem in problems), (
        "共享实现拿不到时静悄悄放行了 ⇒ 判不了却当成干净:%s" % problems)


def test_the_registry_check_reports_a_row_with_the_wrong_number_of_columns(
        tmp_path, monkeypatch):
    """**拦截侧**:ADR 登记表里有一行**不是三列** ⇒ 必须报出来。

    第 55 轮之二十四:此前那条「理由里不许有竖线」的检查是**死代码**
    (竖线会把行切开 ⇒ 那个条件永远不成立)⇒ 它看起来在守,其实一行也拦不住。
    ⇒ 现在按**列数**判 ⇒ 规则真的被执行。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(
        "<!-- string-program-allowlist:begin -->\n"
        "| 文件 | 原语 | 理由 |\n"
        "|---|---|---|\n"
        "| scripts/x.py | compile | 只编译 | 多写了一列 |\n"
        "<!-- string-program-allowlist:end -->\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert any("列" in problem for problem in problems), (
        "列数不对的行没有被报出来 ⇒ 那条规则仍然是死的:%s" % problems)


def test_the_registry_check_accepts_a_row_with_three_columns(tmp_path, monkeypatch):
    """**放行侧**:三列的行 ⇒ **不许**被报成「列数不对」。

    没有这条 ⇒ 那条规则可以一直乱报(把登记表判成坏的)⇒ 守卫被调宽。
    """
    adr = tmp_path / "adr.md"
    adr.write_text(
        "<!-- string-program-allowlist:begin -->\n"
        "| 文件 | 原语 | 理由 |\n"
        "|---|---|---|\n"
        "| scripts/x.py | compile | 只编译,不执行 |\n"
        "<!-- string-program-allowlist:end -->\n", encoding="utf-8")
    monkeypatch.setattr(constitution, "_STRING_ALLOWLIST_ADR", str(adr))
    problems = check_string_program_allowlist_registry(ROOT)
    assert not [p for p in problems if "列" in p], (
        "三列的行被报成列数不对 ⇒ 误报:%s" % problems)


# ---------------------------------------------------------------------------
# 第 55 轮之二十五(codex 第 2 轮 38/50):六条族 —— 逐条本地复现后逐条修
# ---------------------------------------------------------------------------
#
# 这一节里的每一条都对应 codex 第 2 轮报告里的一条发现,**复现脚本放在 %TEMP%**
# (不进仓库)⇒ 仓库里只留**两侧探针**。
# 纪律不变:**先复现 → 确认是漏判还是误报 → 才动手改 → 两侧探针 → 一条变异 + 一个专属见证**。

# H1:元组/列表解包把解释器与程序体**一起**绑走。
# 上一版只认 `ast.Name` 的绑定目标 ⇒ 解释器与程序体**双双漏判**(实测:
# `_string_bindings` 返回 `{}`、`_string_programs` 返回 `[]`)。
TUPLE_UNPACK = '''import subprocess
import sys

INTERPRETER, PROGRAM = "python", "exec(compile(src, src, 'exec'))"
subprocess.run([INTERPRETER, "-c", PROGRAM])
'''

# H1 的放行侧:同样的解包写法,但程序体里**没有**原语。
TUPLE_UNPACK_BENIGN = '''import subprocess
import sys

INTERPRETER, PROGRAM = "python", "print(1)"
subprocess.run([INTERPRETER, "-c", PROGRAM])
'''

# H2:四种「原语名被藏起来」的形态 —— 上一版那条**单一正则**全部认不出(实测)。
OBFUSCATED_BY_GETATTR_OF_AN_IMPORT = '''import subprocess
import sys

PROGRAM = "getattr(__import__('builtins'), 'exec')(1)"
subprocess.run([sys.executable, "-c", PROGRAM])
'''

OBFUSCATED_BY_CONCATENATED_NAME = '''import subprocess
import sys

PROGRAM = "getattr(builtins, 'ex' + 'ec')(1)"
subprocess.run([sys.executable, "-c", PROGRAM])
'''

OBFUSCATED_BY_CONCATENATED_SUBSCRIPT = '''import subprocess
import sys

PROGRAM = "d['ex' + 'ec'](1)"
subprocess.run([sys.executable, "-c", PROGRAM])
'''

OBFUSCATED_BY_ONE_ELEMENT_TUPLE = '''import subprocess
import sys

PROGRAM = "getattr(builtins, ('exec'))(1)"
subprocess.run([sys.executable, "-c", PROGRAM])
'''

# H2 的误报面:模块**自己**的 `compile` 不是内建原语 ⇒ 不许报
# (报它就是逼人去登记豁免 ⇒ 把守卫调宽 —— 那正是这条规则要防的下场)。
MODULE_COMPILE = '''import subprocess
import sys

PROGRAM = "import re; re.compile(src)"
subprocess.run([sys.executable, "-c", PROGRAM])
'''

# M1:未闭合的 heredoc。POSIX shell 会给个 warning 但**照样执行**
# ⇒ 上一版「找不到终止行 ⇒ 返回空列表」= 静默 fail-open(实测:程序体列表为空)。
UNCLOSED_HEREDOC = '''import os

os.system("python - <<'EOF'\\nexec(open('p').read())\\n")
'''


def test_the_guard_reports_a_primitive_hidden_behind_a_tuple_unpacking(tmp_path):
    """**拦截侧**:元组/列表解包把解释器与程序体绑走时,必须被报出来。

    上一版只认 `ast.Name` 的绑定目标 ⇒ 这一整条**漏判**(实测:
    `_string_bindings` 返回 `{}`)。⇒ 「判据比它自己声明的窄」。
    """
    root = _root_with(tmp_path, {"scripts/tuple_unpack.py": TUPLE_UNPACK})
    problems = check_string_program_primitives(root)
    assert any("tuple_unpack.py" in p and "exec" in p for p in problems), (
        "元组解包藏起来的原语没有被报出来:%s" % problems)


def test_the_guard_accepts_a_tuple_unpacking_without_any_primitive(tmp_path):
    """**放行侧**:解包写法本身**不许**被当成「藏了原语」。

    **两条断言缺一不可**(仓库纪律):只断「没报」⇒ 判据全盲时照样绿
    ⇒ 是个空测试。⇒ 同一形态**换上真原语**时必须看得见。
    """
    root = _root_with(tmp_path, {"scripts/tuple_benign.py": TUPLE_UNPACK_BENIGN})
    assert check_string_program_primitives(root) == []
    genuine = TUPLE_UNPACK_BENIGN.replace("print(1)", "exec(compile(src, src, 'exec'))")
    genuine_root = _root_with(tmp_path / "genuine",
                              {"scripts/tuple_benign.py": genuine})
    problems = check_string_program_primitives(genuine_root)
    assert any("exec" in p for p in problems), (
        "换上真原语后判据却什么都看不见 ⇒ 这条放行侧用例是空的:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_behind_getattr_of_an_import(tmp_path):
    """**拦截侧**:`getattr(__import__('builtins'), 'exec')(...)` 必须被报出来。

    上一版那条正则要求 `getattr(` 后面**不能有括号** ⇒ 这一形态**漏判**(实测)。
    """
    root = _root_with(tmp_path,
                      {"scripts/obf_import.py": OBFUSCATED_BY_GETATTR_OF_AN_IMPORT})
    problems = check_string_program_primitives(root)
    assert any("obf_import.py" in p and "exec" in p for p in problems), (
        "getattr(导入结果, 'exec') 藏起来的原语没有被报出来:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_behind_a_concatenated_name(tmp_path):
    """**拦截侧**:`getattr(builtins, 'ex' + 'ec')(...)` 必须被报出来。

    原语名被**拼**出来 ⇒ 上一版那条正则(只认字面量)漏判(实测)。
    """
    root = _root_with(tmp_path,
                      {"scripts/obf_concat.py": OBFUSCATED_BY_CONCATENATED_NAME})
    problems = check_string_program_primitives(root)
    assert any("obf_concat.py" in p and "exec" in p for p in problems), (
        "拼接出来的原语名没有被报出来:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_behind_a_concatenated_subscript(tmp_path):
    """**拦截侧**:`d['ex' + 'ec'](...)` 必须被报出来(实测上一版漏判)。"""
    root = _root_with(tmp_path,
                      {"scripts/obf_sub.py": OBFUSCATED_BY_CONCATENATED_SUBSCRIPT})
    problems = check_string_program_primitives(root)
    assert any("obf_sub.py" in p and "exec" in p for p in problems), (
        "下标里拼出来的原语名没有被报出来:%s" % problems)


def test_the_guard_reports_a_primitive_hidden_behind_a_one_element_tuple(tmp_path):
    """**拦截侧**:`getattr(builtins, ('exec'))(...)` 必须被报出来(实测上一版漏判)。"""
    root = _root_with(tmp_path,
                      {"scripts/obf_tuple.py": OBFUSCATED_BY_ONE_ELEMENT_TUPLE})
    problems = check_string_program_primitives(root)
    assert any("obf_tuple.py" in p and "exec" in p for p in problems), (
        "单元素元组里藏起来的原语名没有被报出来:%s" % problems)


def test_the_guard_does_not_treat_a_module_compile_as_the_builtin_one(tmp_path):
    """**放行侧**:模块**自己**的 `compile`(`re.compile`)**不许**被报出来。

    报它就是逼人去登记豁免 ⇒ 那正是这条规则要防的「守卫被调宽」(第 55 轮之二十四)。
    ⇒ 新的 AST 判据必须与那条收紧**同一口径**。

    **两条断言缺一不可**:同一形态换成**内建**的 `compile(` 时必须看得见。
    """
    root = _root_with(tmp_path, {"scripts/mod_compile.py": MODULE_COMPILE})
    problems = check_string_program_primitives(root)
    assert not [p for p in problems if "compile" in p], (
        "模块自己的 compile 被报成内建原语 ⇒ 误报(逼人去登记豁免):%s" % problems)
    genuine = MODULE_COMPILE.replace("import re; re.compile(src)",
                                     "compile(src, src, 'exec')")
    genuine_root = _root_with(tmp_path / "genuine2",
                              {"scripts/mod_compile.py": genuine})
    genuine_problems = check_string_program_primitives(genuine_root)
    assert any("compile" in p for p in genuine_problems), (
        "真正的内建 compile 却看不见 ⇒ 这条放行侧用例是空的:%s" % genuine_problems)


def test_the_guard_reports_a_primitive_hidden_in_an_unclosed_heredoc(tmp_path):
    """**拦截侧**:**未闭合**的 heredoc 里的原语必须被报出来。

    POSIX shell 对「到 EOF 才结束」的 heredoc 会给个 warning 但**照样执行**
    ⇒ 上一版「找不到终止行 ⇒ 返回空列表」= **静默 fail-open**(实测:程序体列表为空)
    ⇒ 守卫对它失明。⇒ 现在把剩下的全部当程序体扫。
    """
    root = _root_with(tmp_path, {"scripts/heredoc.py": UNCLOSED_HEREDOC})
    problems = check_string_program_primitives(root)
    assert any("heredoc.py" in p and "exec" in p for p in problems), (
        "未闭合 heredoc 里的原语没有被报出来 ⇒ 静默 fail-open:%s" % problems)


def test_the_executor_check_says_when_a_file_cannot_be_read(tmp_path):
    """**拦截侧**:**读不了**的文件必须出声 —— 「查不了」≠「干净」。

    第 55 轮之二十五(codex 第 2 轮 M2):上一版 `except (UnicodeDecodeError,
    SyntaxError): continue` ⇒ 一个**坏编码**的文件能让「执行入口完备性」
    对它**报干净** ⇒ 那不是「没问题」,是「没看」。
    """
    bad = tmp_path / "bad_encoding.py"
    bad.write_bytes(b"# -*- coding: cp1252 -*-\n# caf\xe9\nx = 1\n")
    problems = constitution.check_executor_completeness(tmp_path)
    assert any("bad_encoding.py" in p and "查不了" in p for p in problems), (
        "读不了的文件被静悄悄跳过了 ⇒ 判不了却当成干净:%s" % problems)


def test_the_shell_interpreter_check_follows_the_shared_implementation():
    """**放行侧 + 拦截侧**:shell 里的解释器名**只认一份**。

    第 55 轮之二十五(codex 第 2 轮 L1):上一版在本地又写了一份正则
    ⇒ `python3.` 这种**根本不是解释器**的名字也会被认(实测)
    ⇒ CHANGELOG 里「只留一份实现」那句话与代码对不上。

    ⇒ 现在两条断言:真解释器认得;不是解释器的名字**不认**。
    """
    assert constitution._shell_has_python_interpreter("python3 -c x")
    assert constitution._shell_has_python_interpreter("/usr/bin/python3.11 -c x")
    assert not constitution._shell_has_python_interpreter("python3. -c x")


# 第 55 轮之二十五之补(codex 第 2 轮 H2 的**兜底那一层**):
# 程序体**解析不了**时,只能靠**正则**那一层。
UNPARSEABLE_BODY = '''import subprocess
import sys


def go(path):
    return subprocess.run(
        [sys.executable, "-c", "python -c \\"getattr(builtins, 'exec')\\"", path],
        capture_output=True)
'''


def test_the_guard_reports_a_primitive_in_a_program_body_it_cannot_parse(tmp_path):
    """**拦截侧**:程序体**解析不了**时,正则那一层**仍要**认出混淆原语。

    第 55 轮之二十五(codex 第 2 轮 H2):原语识别改成了「**当 Python 解析**
    + 对取值表达式**折叠**」⇒ 那一层比正则**准**;但**程序体未必是合法 Python**
    (例如整条 shell 命令)⇒ 解析不了时**只能退回正则那一层**。

    ⇒ 那一层此前**没有任何探针** ⇒ ADR 里把「解析不了的程序体」登记成了盲区
    ⇒ 「写下了但没人跑」= 没有防线 ⇒ 现在补一条**专属**探针,
    并给「把正则那一层关掉」那条变异配**专属**见证(它此前是**空转**的:
    H2 修完之后,AST 那层把可解析的形态都接走了 ⇒ 关掉正则**没人红**)。
    """
    root = _root_with(tmp_path, {
        "scripts/unparseable.py": UNPARSEABLE_BODY,
        "scripts/clean.py": CLEAN_BODY,
    })
    problems = check_string_program_primitives(root)
    assert any("unparseable.py" in p and "exec" in p for p in problems), (
        "解析不了的程序体里的原语被漏判了 ⇒ 兜底那一层失效:%s" % problems)
    # **第二条断言**(放行侧):同一个仓库里**没有**原语的文件**不许**被报出来
    # (否则这条判据可能变成「一律报红」⇒ 没人再看它)。
    assert not any("clean.py" in p for p in problems), (
        "没有原语的文件被误报了 ⇒ 这条判据变成了「一律报红」:%s" % problems)
