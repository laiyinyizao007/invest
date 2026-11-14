"""第 26 轮(codex 复核,33/50)新规则的回归用例。

第 26 轮 codex 报出的问题里三条 Critical 都落在**沙盒边界**上,这一份逐条钉住:

- Critical 1:提交基准**不许用 `git status`**——`git status` 会应用 filter,
  候选在 `.git/config` 里写一条 filter 命令,守卫就替候选执行了它;
  改成"取提交里那份 blob + 只比换行",并对 `.git/config`/`.git/HEAD`/`.git/index` 做指纹。
- Critical 2:通配符路径(`from 包 import *`)上的每个 `.py` 也要过**提交基准**,
  否则"上一轮改写、这一轮通配符导入"这条跨轮次逃逸原样可用。
- Critical 3:视图的"拒绝"被 CPython 的 `import_from` 回退绕过时,
  回退查表必须用**绝对名**(返回模块的 `__name__`),相对导入时原始名会落空。

其余:危险名单漏项(_winapi/_ctypes/_posixsubprocess、codeop/types)、
静态层不认危险名单、静态层不筛相对导入、路径基准不一致、fail-open、
能力泄漏缓存的失效判据、git 调用没有缓存。

**每条规则都有双向探针**(放行 + 拦截),而且断言"**是哪一条规则**拒的",
不只是"被拒了"——规则失灵时用例照样全绿是第 22 轮踩过的坑。
本文件每条规则都做过变异验证:把对应代码去掉,用例必须失败。
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

# 合并 main #22 之后:`research_api` 不再在模块顶部 import bootstrap(为了不在导入时 chdir),
# 而这些用例会导入 `strategies` 模板 -> vnpy.trader.database,后面别的用例首次 import bootstrap 就会炸
# ("bootstrap 必须先于 vnpy.trader.database 导入")。所以这里先 import,与生产脚本的约定一致。
import bootstrap  # noqa: E402,F401
import research_api as api
import research_sandbox as rs


# --------------------------------------------------------------------------- 夹具

def git_commit_all(root: Path) -> None:
    """把目录变成一个**真的 git 仓库**并提交一次。"""

    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "sandbox-test@example.invalid")
    run("config", "user.name", "sandbox-test")
    run("add", "-A")
    run("commit", "-q", "-m", "init")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个**真的 git 仓库**:受信目录 `strategies/` 里有一个已提交的模块。"""
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (root / "strategies" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    # `marked.py`:给"filter 会被执行"那条对照探针当靶子(见下面的 filter 用例)。
    (root / "strategies" / "marked.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commit_all(root)
    for name in [n for n in list(sys.modules)
                 if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(api.rc, "CODE_ROOT", root)
    monkeypatch.chdir(root)
    return root


def guard_for(root: Path, **kwargs):
    """按生产入口的同一套参数造一个守卫(快照 = 此刻受信目录的内容)。"""
    kwargs.setdefault("allowed_dirs", rs.ALLOWED_REPO_IMPORT_DIRS)
    kwargs.setdefault("snapshot", rs.snapshot_allowed_files(root))
    return rs.RepoImportGuard(root, **kwargs)


# ------------------------------------------- Critical 1:git 不许替候选执行命令

def test_a_filter_command_written_by_the_candidate_is_never_executed(repo, tmp_path):
    """拦截:候选在 `.git/config` 里写 `[filter "pwn"] clean=<命令>`,守卫**不许**执行它。

    第 26 轮 codex 实测的原始路径:`git status` 会应用 filter,
    于是守卫自己跑的那次 `git status` 就替候选执行了候选写的命令(标记文件被写出,
    而静态筛查 0 findings)。

    这里做**双向对照**:
    - 先证明"老路(`git status`)"确实会执行它(否则这条用例的前提不成立);
    - 再证明"新路(守卫的提交基准核对)"**不会**执行它,且**拒绝**这次导入。
    """
    marker = repo / "PWNED_BY_FILTER.txt"
    config = repo / ".git" / "config"
    guard = guard_for(repo)          # 守卫先构造(指纹记的是**干净**的元数据)

    # 模拟"候选在运行期间改写 .git/config 与 .gitattributes"。
    # 命令写成"脚本文件 + sh <脚本>":git 会把配置值里的引号**脱掉**再交给 sh 执行,
    # 于是带空格的路径(本机 python 在 "C:/Program Files/..." 下)会散成好几个词;
    # 脚本里的引号自己管,配置值里的引号用 \" 转义。
    py = sys.executable.replace(os.sep, "/")
    target = str(marker).replace(os.sep, "/")
    script = repo / "probe_filter.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'"{py}" -c "import pathlib;pathlib.Path(r\'{target}\').write_text(\'x\')"\n',
        encoding="utf-8")
    script_path = str(script).replace(os.sep, "/")
    config.write_text(
        config.read_text(encoding="utf-8")
        + textwrap.dedent(f"""
            [filter "pwn"]
            \tclean = sh \\"{script_path}\\"
            \trequired = true
            """),
        encoding="utf-8")
    (repo / ".gitattributes").write_text("strategies/marked.py filter=pwn\n", encoding="utf-8")
    # 靶子文件的内容**必须真的改动**:git 只在"工作区内容与 index 不一致"时才会
    # 比对内容、才会把 clean filter 用上去。内容没变 → git 走 stat 快路径 →
    # filter 根本不跑 → 对照组会**假阴性**(第一版就是这么翻车的:
    # 单跑时靠 git 的 racy-clean 恰好触发了,连跑整份套件时就复现不出来了)。
    marked = repo / "strategies" / "marked.py"
    marked.write_text("VALUE = 2\n", encoding="utf-8")

    # 对照组:**老路**确实会执行候选写的命令(证明这条用例的前提成立)。
    proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True)
    assert marker.exists(), (
        "对照组失效:这一轮 `git status` 没有执行 filter 命令"
        f"(git 退出码 {proc.returncode}),"
        "说明本机 git 的行为与 codex 实测不同,后面的断言说明不了任何东西")
    marker.unlink()

    reason = guard.check("strategies.base")

    assert not marker.exists(), (
        "守卫自己执行了候选写在 .git/config 里的 filter 命令——"
        "提交基准核对不许用 `git status` 这条路")
    assert reason, "仓库元数据被改写竟然还能导入"
    assert "仓库元数据" in reason, f"拒绝理由不是元数据指纹那条:{reason}"


def test_the_guard_never_runs_a_git_command_that_applies_filters(repo):
    """拦截:**即使**元数据指纹没报警,守卫也不能跑会应用 filter 的 git 命令。

    这是上一条用例的"另一半",也是更硬的那一半:
    上一条里指纹先报警 → 守卫直接拒 → 根本没跑 git,所以它证明的是
    "**指纹**这条规则在";这一条把 filter **写在守卫构造之前**
    (于是指纹拍到的就是**已经被改过**的配置,不会报警),
    守卫于是真的会去跑 git——这时 marker 还不出现,才能证明
    "**命令选择**这条规则在"(守卫跑的是 `ls-tree`/`cat-file`,它们不读工作区)。

    少了这一条,把提交基准改回 `git status` 的变异就抓不住(守卫在报警处就停了)。
    """
    marker = repo / "PWNED_BY_FILTER.txt"
    config = repo / ".git" / "config"
    py = sys.executable.replace(os.sep, "/")
    target = str(marker).replace(os.sep, "/")
    script = repo / "probe_filter.sh"
    script.write_text(
        "#!/bin/sh\n"
        f'"{py}" -c "import pathlib;pathlib.Path(r\'{target}\').write_text(\'x\')"\n',
        encoding="utf-8")
    script_path = str(script).replace(os.sep, "/")
    config.write_text(
        config.read_text(encoding="utf-8")
        + textwrap.dedent(f"""
            [filter "pwn"]
            \tclean = sh \\"{script_path}\\"
            \trequired = true
            """),
        encoding="utf-8")
    (repo / ".gitattributes").write_text("strategies/marked.py filter=pwn\n", encoding="utf-8")
    marked = repo / "strategies" / "marked.py"
    marked.write_text("VALUE = 2\n", encoding="utf-8")

    # 守卫在"配置已被改过"之后构造 → 指纹拍到的就是改过的配置 → 指纹那条不会报警。
    guard = guard_for(repo)

    reason = guard.check("strategies.base")

    assert not marker.exists(), (
        "守卫跑了会应用 filter 的 git 命令(命令选择这条规则失灵了)")
    assert reason is None or "仓库元数据" in reason, f"拒绝理由不对:{reason}"


def test_an_untouched_repository_metadata_still_allows_the_trusted_import(repo):
    """放行:仓库元数据没被动过时,未改动的受信模块照常能导入。

    这是上一条的另一半:指纹核对不能变成"一律不许导"。
    """
    guard = guard_for(repo)

    assert guard.check("strategies.base") is None


def test_the_guard_refuses_when_the_git_head_is_rewritten(repo):
    """拦截:候选改写 `.git/HEAD` → fail-closed。

    `.git/HEAD` 指向哪个提交,决定了提交基准(`ls-tree HEAD`)看到的是哪棵树;
    候选改它就能让基准看到一份**它挑过**的树。
    """
    guard = guard_for(repo)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/evil\n", encoding="utf-8")

    reason = guard.check("strategies.base")

    assert reason, "仓库元数据被改写竟然还能导入"
    assert "仓库元数据" in reason, f"拒绝理由不是元数据指纹那条:{reason}"


def test_the_guard_detects_a_config_change_in_a_linked_worktree(tmp_path, monkeypatch):
    """拦截:**linked worktree** 形态下,共用 `.git/config` 被改写 → fail-closed。

    第 26 轮 codex 第二轮实测的 High 1:
    `git rev-parse --absolute-git-dir` 在 linked worktree 里指的是
    **per-worktree** 的 gitdir(`.git/worktrees/<名字>`),而 `.git/config`
    在 **common dir** 里。只按 per-worktree gitdir 取指纹 → config 根本没被记下来,
    候选改写共用 config 完全不被发现。
    **本仓库自己就是这个形态**(`invest-26` 是 `invest` 的 linked worktree),
    所以这条必须用真的 linked worktree 来测,不能只用 `git init` 的普通仓库。
    """
    main = tmp_path / "main"
    (main / "strategies").mkdir(parents=True)
    (main / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (main / "strategies" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commit_all(main)

    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "wt"],
                   check=True, capture_output=True)
    try:
        for name in [n for n in list(sys.modules)
                     if n == "strategies" or n.startswith("strategies.")]:
            monkeypatch.delitem(sys.modules, name, raising=False)
        monkeypatch.syspath_prepend(str(wt))
        guard = guard_for(wt)
        # 共用的 config 在主仓库那边(worktree 自己的 gitdir 里没有 config)。
        config = main / ".git" / "config"
        config.write_text(config.read_text(encoding="utf-8")
                          + "\n[filter \"pwn\"]\n\tclean = echo pwned\n",
                          encoding="utf-8")

        reason = guard.check("strategies.base")

        assert reason, "linked worktree 下共用 .git/config 被改写竟然还能导入"
        assert "仓库元数据" in reason, f"拒绝理由不是元数据指纹那条:{reason}"
    finally:
        subprocess.run(["git", "-C", str(main), "worktree", "remove", "--force", str(wt)],
                       capture_output=True)


def test_the_git_layer_refuses_a_subcommand_that_reads_the_worktree():
    """拦截:git 调用层**只允许**读对象库的子命令(机制,不是约定)。

    第 26 轮 codex 第二轮的意见:光靠"这次没写 git status"是约定不是机制——
    以后有人加回 `git status`(它会应用 filter)就复发。
    """
    with pytest.raises(ValueError):
        rs._git_run(["status", "--porcelain"])


def test_the_bytecode_cache_cannot_shadow_a_trusted_module(repo, tmp_path, monkeypatch):
    """拦截:候选伪造的 `.pyc` 不许盖过**已核对过**的源码。

    第 26 轮 codex 第二轮实测的 High 2:
    提交基准核对的是**源码**,而 CPython 导入时**优先用 `.pyc`**——
    `.pyc` 的有效性只校验头部里的 mtime+size(候选能伪造)。
    实测(改写前):候选伪造 `strategies/__pycache__/base.cpython-*.pyc` 之后,
    守卫放行,导入机制执行的是**伪造的字节码**(VALUE=666 而不是 1)。
    处置:核对完源码就删掉可能伪造的 `.pyc`,让导入机制从源码重新编译。
    """
    import importlib.util

    source = repo / "strategies" / "base.py"
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(parents=True, exist_ok=True)
    # 伪造一个字节码:内容是 "VALUE = 666",头部对齐源文件的 mtime+size。
    stat = source.stat()
    import importlib.util as _u
    import marshal
    import struct
    import sys as _sys
    code = compile("VALUE = 666\n", str(source), "exec")
    header = _u.MAGIC_NUMBER + struct.pack("<III", 0, stat.st_mtime_ns & 0xFFFFFFFF,
                                           stat.st_size)
    cache.write_bytes(header + marshal.dumps(code))
    for name in [n for n in list(sys.modules) if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(repo))

    guard = guard_for(repo)
    reason = guard.check("strategies.base")

    assert reason is None, f"未改动的受信模块被误拒:{reason}"
    assert not cache.exists(), (
        "可能伪造的 .pyc 没有被清掉——导入机制仍可能执行候选写的字节码")
    module = __import__("strategies.base", fromlist=["VALUE"])
    assert module.VALUE == 1, (
        "导入到的不是源码里的值(.pyc 盖过了已核对过的源码)")


def test_a_trusted_module_modified_after_the_commit_is_refused(repo):
    """拦截:受信模块与提交不一致 → 拒绝(第 25 轮的规则在新实现下依然成立)。"""
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check("strategies.base")

    assert reason, "被改写过的受信模块竟然能导入"
    assert "与仓库提交的内容不一致" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_a_file_written_into_a_trusted_dir_is_refused(repo):
    """拦截:受信目录里**不在提交里**的文件(候选自己写的)一律不得导入。"""
    (repo / "strategies" / "evil.py").write_text("X = 1\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check("strategies.evil")

    assert reason, "候选写进受信目录的文件竟然能导入"
    assert "不在仓库提交里" in reason, f"拒绝理由不是提交基准那条:{reason}"


# ------------------------------------------- Critical 2:通配符路也要有提交基准

def test_a_wildcard_import_cannot_pick_up_a_module_rewritten_in_a_previous_round(repo):
    """拦截:包里的 `.py` 被改写后,`from 包 import *` 必须被拒。

    第 26 轮 codex 的 Critical 2:通配符这一路原来**只有**"快照"这一关,
    没有提交基准——于是"上一轮候选改写了 strategies/base.py、
    这一轮 `from strategies import *`"原样可用。
    """
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check_package_wildcard("strategies")

    assert reason, "被改写过的包内模块竟然能通过通配符裁决"
    assert "与仓库提交的内容不一致" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_a_wildcard_import_cannot_pick_up_a_file_written_in_a_previous_round(repo):
    """拦截:包里的 `.py` 不在提交里(候选写的)→ 通配符裁决必须拒。"""
    (repo / "strategies" / "evil.py").write_text("X = 1\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check_package_wildcard("strategies")

    assert reason, "候选写进受信目录的文件竟然能通过通配符裁决"
    assert "不在仓库提交里" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_an_untouched_package_still_passes_the_wildcard_check(repo):
    """放行:没被动过的包,通配符裁决必须放行(这条规则不能变成"一律不许导")。"""
    guard = guard_for(repo)

    assert guard.check_package_wildcard("strategies") is None


# ------------------------------------------- Critical 3:相对导入不许把真模块交出去

def test_a_relative_import_cannot_fall_back_to_the_real_module(tmp_path, monkeypatch):
    """拦截:相对导入的"视图拒绝"不许被 CPython 的回退绕过。

    CPython 的 `IMPORT_FROM` 在 `getattr` 失败时会回退到
    `sys.modules["<返回模块的 __name__>.<名字>"]`——**绝对名**。
    原来的查表用的是**原始名**:相对导入时原始名可能是 ""(from . import x),
    于是查表永远落空 → 视图拒绝之后回退把**真模块**交给候选。
    """
    pkg = tmp_path / "pkgrel"
    pkg.mkdir()
    # 包初始化时把属性删掉,于是视图取 'sub' 会失败 -> 触发 CPython 的回退。
    (pkg / "__init__.py").write_text(
        textwrap.dedent("""
            from . import sub
            del sub
            """), encoding="utf-8")
    (pkg / "sub.py").write_text("import ctypes\nVALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in list(sys.modules) if n == "pkgrel" or n.startswith("pkgrel.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    # 合并 main #22:`pkgrel` 走配置追加的白名单,被检验的才是"回退键"这条规则本身。
    safe = rs.safe_builtins((), ["pkgrel"])
    namespace = {"__name__": "pkgrel.probe", "__package__": "pkgrel", "__builtins__": safe}
    exec(compile(textwrap.dedent("""
        try:
            from . import sub
            got = sub
        except Exception as exc:
            got = repr(exc)
        """), "probe.py", "exec"), namespace)
    got = namespace["got"]

    assert not isinstance(got, ModuleType), (
        f"候选拿到了**真模块**(相对导入的回退键查错了,拒绝被绕过):{got}")
    assert isinstance(got, str), f"期望是导入失败,实际拿到:{got!r}"
    assert "能力泄漏" in got or "模块视图" in got, f"拒绝理由不对:{got}"


def test_an_ordinary_relative_import_of_a_clean_module_still_works(tmp_path, monkeypatch):
    """放行:相对导入一个**干净**的模块必须照常工作(这条规则不能误伤)。"""
    pkg = tmp_path / "pkgok"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub.py").write_text("VALUE = 7\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in list(sys.modules) if n == "pkgok" or n.startswith("pkgok.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    # 合并 main #22:`pkgok` 走配置追加的白名单(它不在内置白名单里)。
    safe = rs.safe_builtins((), ["pkgok"])
    namespace = {"__name__": "pkgok.probe", "__package__": "pkgok", "__builtins__": safe}
    exec(compile(textwrap.dedent("""
        try:
            from . import sub
            got = sub.VALUE
        except Exception as exc:
            got = repr(exc)
        """), "probe.py", "exec"), namespace)

    assert namespace["got"] == 7, f"干净的相对导入被误伤:{namespace['got']!r}"


# ------------------------------------------- High 1/2:危险名单漏项

@pytest.mark.parametrize("name", ["_winapi", "_ctypes", "_posixsubprocess",
                                  "codeop", "types", "posixpath"])
def test_the_runtime_import_gate_refuses_the_native_and_execution_primitives(name):
    """拦截:执行原语与原生调用后门必须在**导入门**就被拒。

    第 26 轮 codex 的 High 1(实测:`_winapi` 的 `CreateProcess` 可达;
    `_posixsubprocess` 是 Linux 对偶,本机无法实测,按推理登记)
    与 High 2(实测:`codeop.compile_command` + `types.FunctionType` + 真内建 = 任意代码执行)。
    """
    safe = rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert name in str(exc.value)


@pytest.mark.parametrize("source", ["import ssl\n", "import codecs\n", "import mmap\n",
                                    "import codeop\n", "from ssl import SSLContext\n"])
def test_the_static_screen_refuses_the_dangerous_modules(source):
    """静态层必须与运行期**同一份**危险名单(第 26 轮 codex 的 Medium 3)。

    原来静态层只认 `FORBIDDEN_MODULES`,于是 `import ssl/codecs/mmap` 静态 0 findings,
    只在运行期才被拒——而运行期的拒绝发生在 import **之后**。
    """
    findings = rs.screen_source(source)

    assert findings, f"静态层没拦住:{source!r}"


@pytest.mark.parametrize("source", ["import numpy\n", "from numpy import random\n",
                                    "import json\n", "import math\n"])
def test_the_static_screen_still_allows_the_ordinary_stack(source):
    """放行:候选正当需要的科学栈不能被这条规则误伤。"""
    findings = rs.screen_source(source)

    assert not findings, f"误伤了普通写法:{source!r} -> {findings}"


# ------------------------------------------- Medium 1:路径基准必须一致

def test_a_trusted_module_is_not_rejected_when_the_code_lives_in_a_subdirectory(tmp_path, monkeypatch):
    """放行:代码目录是仓库的**子目录**时,受信模块不能被整片误杀。

    第 26 轮 codex 的 Medium 1:提交基准原来是 repo_root 口径、调用方却按 toplevel 口径比,
    repo_root != toplevel 时**全部**受信导入被拒(埋着的误杀)。
    """
    top = tmp_path / "outer"
    root = top / "inner"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (root / "strategies" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    git_commit_all(top)
    for name in [n for n in list(sys.modules)
                 if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(root))

    guard = guard_for(root)

    assert guard.check("strategies.base") is None, (
        "代码在仓库子目录里时,受信模块被整片误杀(路径基准不一致)")


# ------------------------------------------- Medium 2:通配符扫描不许 fail-open

def test_the_wildcard_scan_fails_closed_when_the_package_cannot_be_resolved(repo, monkeypatch):
    """拦截:包位置查不出来 → 拒(fail-closed),不许"跳过"。

    第 26 轮 codex 的 Medium 2:这里原来是 `except OSError: continue`,
    与同文件 `check_package` 的 fail-closed 口径相反。
    """

    def boom(self, *args, **kwargs):
        raise OSError("模拟:路径解析失败")

    guard = guard_for(repo)
    monkeypatch.setattr(Path, "resolve", boom)

    reason = guard.check_package_wildcard("strategies")

    assert reason, "查不到包位置竟然放行了(fail-open)"
    assert "fail-closed" in reason, f"拒绝理由不是 fail-closed 那条:{reason}"


# ------------------------------------------- Medium 3 / Low 3:静态层

def test_the_static_screen_refuses_a_relative_import_it_cannot_resolve():
    """拦截:相对导入在静态层必须被处理(不能整个跳过)。

    第 26 轮 codex 的 Low 3:原来 `from . import x` 的 `node.module` 是 None,
    静态层**连名字都没看见**;而运行期的相对导入发生在候选模块里,
    静态层看不见就等于没有这一关。
    """
    findings = rs.screen_source("from . import os\n")

    assert findings, "静态层把相对导入整个跳过了"


def test_the_static_screen_resolves_a_relative_import_against_its_package():
    """拦截:知道包名时,相对导入要还原成**绝对名**再筛。

    还原不出来就整条跳过(`node.module` 是 None),于是 `from . import x`
    连"名字"都没进筛查——第 26 轮 codex 的 Low 3。
    """
    findings = rs.screen_source("from . import x\n", package="codecs")

    assert any("codecs" in f for f in findings), (
        f"相对导入没被还原成绝对名:{findings}")


def test_the_static_screen_still_allows_an_ordinary_from_import():
    """放行:普通的 from-import 不能被误伤。"""
    findings = rs.screen_source("from numpy import random\n")

    assert not findings, f"误伤了普通写法:{findings}"


# ------------------------------------------- Low 1:git 调用要缓存

def test_the_committed_blob_is_fetched_once_and_reused(repo, monkeypatch):
    """性能/行为:同一份提交内容不该反复起 git 进程。

    第 26 轮 codex 的 Low 1:`git status` 每次受信导入都跑一遍(实测 ≈39ms)。
    现在改成"取提交里那份 blob",内容按 sha 寻址、**不可变**,所以缓存永远有效。

    这里数的是 **cat-file**(取内容)那几条:仓库**元数据**的指纹必须每次重新读
    (候选在运行期间改了 `.git/config`/HEAD 就是要当场发现的),
    那几次 `rev-parse` 是设计如此,不是浪费。
    """
    calls: list = []
    original = rs._git_run

    def counting(args, cwd=None, timeout=30):
        calls.append(args)
        return original(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(rs, "_git_run", counting)
    guard = guard_for(repo)

    guard.check("strategies.base")
    first = len([c for c in calls if c and c[0] == "cat-file"])
    guard.check("strategies.base")
    guard.check("strategies.base")
    again = len([c for c in calls if c and c[0] == "cat-file"])

    assert again == first, f"重复核对又起了 git 进程取内容:{calls}"


# ------------------------------------------- Low 2:能力泄漏缓存的失效判据

def test_the_leak_cache_notices_a_same_size_replacement():
    """拦截:成员**等量替换**(数量不变)时,能力泄漏判定必须更新。

    第 26 轮 codex 的 Low 2:缓存原来用 `len(vars(module))` 判失效,
    于是"换掉一个成员、成员数量不变"会用到旧判定(实测:应为 ctypes,实得 None)。
    """
    import ctypes

    module = ModuleType("fake_probe_module")
    module.alpha = 1
    module.beta = 2

    assert rs._capability_leak(module) is None, "前置条件不成立:一开始就判成有泄漏"

    module.alpha = ctypes.CDLL                       # 等量替换:成员数量不变

    assert rs._capability_leak(module) == "ctypes", (
        "等量替换成员后,能力泄漏判定还在用旧结果")


def test_the_leak_cache_still_works_for_a_clean_module():
    """放行:干净的模块判成"没有泄漏"(这条规则不能变成"一律判成有泄漏")。"""
    module = ModuleType("fake_clean_module")
    module.alpha = 1
    module.beta = 2

    assert rs._capability_leak(module) is None


# 这些用例会走生产入口、把**临时仓库里的**包真的导入进 sys.modules。monkeypatch.delitem 只还原
# "删除时存在的"几项,测试期间新导入的不会被清掉,于是后面的测试(例如 test_research_api 里的
# `import strategies.instruments`)拿到指向临时仓库的残缺包 —— 表现为"按文件顺序才出现"的
# ModuleNotFoundError(合并 main #22 之后暴露)。每个用例前后自己保存/还原这几个前缀。
_SANDBOX_TEST_PREFIXES = ("strategies", "pkgx", "pkgrel", "pkgok", "leaky_union_pkg")


@pytest.fixture(autouse=True)
def _restore_modules_touched_by_sandbox_tests():
    import sys as _sys

    def touched(name):
        return name.split(".")[0] in _SANDBOX_TEST_PREFIXES

    saved = {n: m for n, m in _sys.modules.items() if touched(n)}
    yield
    for name in [n for n in list(_sys.modules) if touched(n)]:
        del _sys.modules[name]
    _sys.modules.update(saved)
