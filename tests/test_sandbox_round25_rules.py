"""第 25 轮(codex 第二轮,31/50)新规则的回归用例。

第 25 轮 codex 给这个沙盒打了 31/50(低于 45 的可发布线),指出的问题里
Critical 1 与 High 2/3/4 是**沙盒边界**上的,这一份逐条钉住:

- Critical 1:受信目录的完整性基准必须是**仓库提交里的那份**——
  快照是每次导入重拍的,挡不住"上一轮写、这一轮导"。
- High 2:视图用"抛 AttributeError"表示不给,而 CPython 的 `import_from`
  在 getattr 失败时会**回退到 sys.modules**——于是"拒绝"恰好触发了"交出真模块"。
- High 3:导入门不再整块拒绝"会把能力递出来"的模块(否则 matplotlib 直接用不了)。
- High 4:危险名单原先只在属性层生效,导入门放行了一大片(httpx/codecs/ssl)。
- Medium 6:静态层看不见 `from numpy import ctypeslib` 的 fromlist。
- Low 11:视图不透传 `__version__`,候选连"记录库版本"都做不到。

**每条规则都有双向探针**(放行 + 拦截),而且断言"**是哪一条规则**拒的",
不只是"被拒了"——第 22 轮踩过的坑:规则失灵时用例照样全绿。
本文件每条规则都做过变异验证:把对应代码去掉,用例必须失败。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest

import research_api as api
import research_sandbox as rs


# --------------------------------------------------------------------------- 夹具

def git_commit_all(root: Path) -> None:
    """把目录变成一个**真的 git 仓库**并提交一次。

    第 25 轮的提交基准规则要拿 HEAD 当准绳,所以夹具必须是真仓库。
    """
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
    git_commit_all(root)
    for name in [n for n in list(sys.modules)
                 if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(api.bootstrap, "ROOT", root)
    monkeypatch.chdir(root)
    return root


def guard_for(root: Path, **kwargs):
    """按生产入口的同一套参数造一个守卫(快照 = 此刻受信目录的内容)。"""
    kwargs.setdefault("allowed_dirs", rs.ALLOWED_REPO_IMPORT_DIRS)
    kwargs.setdefault("snapshot", rs.snapshot_allowed_files(root))
    return rs.RepoImportGuard(root, **kwargs)


def run_candidate(tmp_path, monkeypatch, source, cls_name="S"):
    """把候选源码走**生产入口** `import_candidate` 跑一遍,返回它的类。"""
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    path = tmp_path / "candidate.py"
    path.write_text(source, encoding="utf-8")
    return api.import_candidate(path, cls_name, api.rs.file_sha256(path))


BASE = """
    LEAK = {}
"""

TAIL = """
    from vnpy_ctastrategy import CtaTemplate
    class S(CtaTemplate):
        PASS = LEAK
"""


# ------------------------------------------- Critical 1:受信目录要以**提交**为准

def test_a_trusted_module_that_matches_the_commit_can_be_imported(repo):
    """放行:与提交一致的受信模块照常能导入(这条规则不能变成"一律不许导")。"""
    guard = guard_for(repo)

    assert guard.check("strategies.base") is None


def test_a_trusted_module_modified_after_the_commit_cannot_be_imported(repo):
    """拦截:受信模块与提交不一致(候选上一轮改写过)→ 拒绝导入。

    快照是**每次导入重拍**的,所以"运行期间被改动过"那条时间规则在这里看不见
    (快照就是在改写之后拍的)——只有拿提交当基准才拦得住。
    """
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check("strategies.base")

    assert reason, "被改写过的受信模块竟然能导入"
    assert "与仓库提交的内容不一致" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_a_file_written_into_a_trusted_dir_cannot_be_imported(repo):
    """拦截:受信目录里**不在提交里**的文件(候选自己写的)一律不得导入。"""
    (repo / "strategies" / "evil.py").write_text("X = 1\n", encoding="utf-8")
    guard = guard_for(repo)

    reason = guard.check("strategies.evil")

    assert reason, "候选写进受信目录的文件竟然能导入"
    assert "不在仓库提交里" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_it_is_the_commit_rule_that_refuses_them(repo):
    """双向对照:关掉提交基准这条规则,上面两桩就都放行了。

    断言"是哪一条规则拒的"的另一面:被拒的原因必须**就是**这条规则。
    """
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    (repo / "strategies" / "evil.py").write_text("X = 1\n", encoding="utf-8")
    guard = guard_for(repo, verify_trusted_against_git=False)

    assert guard.check("strategies.base") is None, "关掉提交基准后还被拒,说明拒的是别的规则"
    assert guard.check("strategies.evil") is None, "关掉提交基准后还被拒,说明拒的是别的规则"


def test_the_candidate_cannot_tamper_with_a_trusted_module_in_one_round_and_import_it_in_the_next(
        tmp_path, monkeypatch, repo):
    """**跨轮次逃逸**的端到端复现:第 1 轮改写受信模块,第 2 轮导入它。

    这是第 25 轮 codex 的 Critical 1 的原始复现路径:
    候选用 `np.savetxt` 改写 `strategies/base.py`(它拿到的是**真实** numpy,
    写文件的能力是 numpy 自己的,不是被收走的内建),
    第 2 轮再 `import strategies.base` —— 那个模块会在**导入机制**里执行,
    拿到真实内建,等于任意命令执行。
    """
    # 第 1 轮:改写受信模块。
    # 被改写的内容里带一句"写标记文件"——一旦它被导入机制执行,
    # 标记文件就会出现,于是"没被执行"这件事是可断言的,不是推理出来的。
    round1 = "\n".join(textwrap.dedent(part) for part in (
        BASE,
        """
        try:
            import numpy as np
            np.savetxt("strategies/base.py",
                      np.array(["import os",
                                "open('MARKER', 'w').write('x')"]),
                      fmt="%s")
            LEAK["wrote"] = True
        except Exception:
            LEAK["wrote"] = False
        """,
        TAIL,
    ))
    cls1 = run_candidate(tmp_path, monkeypatch, round1)
    assert cls1.PASS["wrote"] is True, "候选没能改写受信模块(这条用例的前提没成立)"
    assert (repo / "strategies" / "base.py").read_text(encoding="utf-8") != "VALUE = 1\n", (
        "受信模块的内容没变,后面那句拒绝就说明不了任何东西")
    assert not (repo / "MARKER").exists(), "改写本身不该触发执行(还没到导入那一轮)"

    # 第 2 轮:导入那个被改写过的模块 —— 必须被拒。
    round2 = "\n".join(textwrap.dedent(part) for part in (
        BASE,
        """
        try:
            import strategies.base
            LEAK["imported"] = True
        except Exception as exc:
            LEAK["imported"] = False
            LEAK["reason"] = repr(exc)
        """,
        TAIL,
    ))
    # 第 26 轮补修之后,这条拒绝**提前到入口**:受信目录与仓库提交不一致时,
    # 整轮根本不跑(候选连运行的机会都没有)——比"候选跑起来再被拒"更强。
    # 两种形态都算通过,但必须说清是哪一条规则拒的。
    try:
        cls2 = run_candidate(tmp_path, monkeypatch, round2)
    except api.JobError as exc:
        assert "受信目录与仓库提交不一致" in str(exc), (
            f"入口拒绝的理由不是受信目录核对那条:{exc}")
        assert "与仓库提交的内容不一致" in str(exc), (
            f"入口拒绝没有点名提交基准那条:{exc}")
    else:
        assert cls2.PASS["imported"] is False, "候选导入了自己上一轮改写过的受信模块"
        assert "与仓库提交的内容不一致" in cls2.PASS.get("reason", ""), (
            f"拒绝理由不是提交基准那条:{cls2.PASS.get('reason')}")

    # 关键断言:那个被改写过的模块**没有被导入机制执行过**。
    assert not (repo / "MARKER").exists(), (
        "被改写过的受信模块被执行了(标记文件被写出来)——这是真正的逃逸")


def test_an_untouched_trusted_module_is_still_importable_through_the_production_path(
        tmp_path, monkeypatch, repo):
    """放行(生产入口):没被改过的受信模块必须照常能导入——否则这条规则是"全拒"。"""
    source = "\n".join(textwrap.dedent(part) for part in (
        BASE,
        """
        try:
            import strategies.base
            LEAK["value"] = strategies.base.VALUE
        except Exception as exc:
            LEAK["value"] = repr(exc)
        """,
        TAIL,
    ))

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS.get("value") == 1, f"没被改过的受信模块导入不了:{cls.PASS.get('value')}"


# ------------------------------------------- High 2:视图的"拒绝"不许被回退绕过

def test_from_pkgx_import_sub_does_not_hand_out_the_real_module(tmp_path, monkeypatch):
    """`from <包> import <子模块>` 不得把**真模块**递到候选手里。

    CPython 的 `IMPORT_FROM` 在 `getattr` 失败时会清掉异常、回退到
    `sys.modules["pkgx.sub"]`——视图层用"抛 AttributeError"表示不给,
    于是"拒绝"恰好触发了"交出真模块"(第 25 轮 codex 实测)。
    这里断言的是**候选拿到的东西是不是真模块**,不是"有没有报错"。
    """
    pkg = tmp_path / "pkgx"
    pkg.mkdir()
    # 子模块把执行能力递出来(ctypes);包初始化时把属性删掉,
    # 于是视图取 'sub' 会失败 -> 触发 CPython 的回退。
    (pkg / "__init__.py").write_text(
        textwrap.dedent("""
            import sys
            from . import sub
            del sub
        """), encoding="utf-8")
    (pkg / "sub.py").write_text("import ctypes\nVALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [n for n in list(sys.modules) if n == "pkgx" or n.startswith("pkgx.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    safe = rs.safe_builtins(())
    namespace = {"__name__": "probe", "__builtins__": safe}
    exec(compile(textwrap.dedent("""
        try:
            from pkgx import sub
            got = sub
        except Exception as exc:
            got = repr(exc)
        """), "probe.py", "exec"), namespace)
    got = namespace["got"]

    assert not isinstance(got, ModuleType), (
        f"候选拿到了**真模块**(视图的拒绝被导入机制的回退绕过了):{got}")
    assert isinstance(got, str), f"期望是导入失败,实际拿到:{got!r}"
    assert "能力泄漏" in got or "模块视图" in got, f"拒绝理由不对:{got}"


# ------------------------------------------- High 3/4:危险名单两层口径一致

@pytest.mark.parametrize("name", ["httpx", "codecs", "ssl", "urllib3"])
def test_the_runtime_import_gate_refuses_the_network_and_write_modules(name):
    """导入门必须与属性层**同一份**危险名单(第 25 轮 codex 的 High 4)。

    原来危险名单只在属性层生效,导入门放行了一大片:
    `httpx.get(...)` 能外传、`codecs.open(...)` 能写文件。
    """
    safe = rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert name in str(exc.value)


@pytest.mark.parametrize("name", ["numpy", "matplotlib", "psutil"])
def test_the_runtime_import_gate_still_allows_the_ordinary_stack(name):
    """放行:候选正当需要的科学栈照常能导(第 25 轮 codex 的 High 3)。

    `matplotlib` 与 `psutil` 的命名空间里都有 `subprocess`(matplotlib 建字体缓存要用),
    曾经因为"整块拒绝会递出能力的模块"而被整个拒掉——误伤比漏检更糟。
    """
    safe = rs.safe_builtins(())

    module = safe["__import__"](name)

    assert module is not None


# ------------------------------------------- Medium 6:静态层要看得见 fromlist

def test_the_static_screen_refuses_from_numpy_import_ctypeslib():
    """`from numpy import ctypeslib` 与 `import numpy.ctypeslib` 是同一件事。

    原来静态层只看 `node.module`(numpy),看不见 names 里的 ctypeslib,
    于是这条写法**放行**——而运行期的拒绝发生在 import **之后**,
    模块已经在导入机制里以真实内建执行过,副作用已经发生。
    """
    findings = rs.screen_source("from numpy import ctypeslib\n")

    assert any("numpy.ctypeslib" in f for f in findings), f"静态层没拦住:{findings}"


def test_the_static_screen_still_allows_the_ordinary_from_import():
    """反向对照:普通的 from-import 不能被这条规则误伤。"""
    findings = rs.screen_source("from numpy import random\n")

    assert not any("numpy.random" in f for f in findings), f"误伤了普通写法:{findings}"


# ------------------------------------------- Low 11:视图透传 __version__

def test_the_candidate_can_read_the_library_version():
    """候选得能读到库版本(记一次评估用了哪个版本,是正常需求)。

    视图原先只透传 `__all__`/`__name__`/`__doc__`/`__package__`,
    `np.__version__` 直接 AttributeError。
    """
    safe = rs.safe_builtins(())
    namespace = {"__name__": "probe", "__builtins__": safe}
    exec(compile("import numpy as np\nV = np.__version__\n", "probe.py", "exec"), namespace)

    assert isinstance(namespace.get("V"), str) and namespace["V"], (
        f"候选读不到库版本:{namespace.get('V')!r}")
