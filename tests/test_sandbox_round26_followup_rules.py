"""第 26 轮**补修之三**(codex 对最终状态复核,35/50)新规则的回归用例。

codex 在最终状态上又找出 5 条,这一份逐条钉住:

- **Critical 1**:守卫只裁决"被解析出的那一个文件",而导入机制导入子模块**之前
  必定先执行父包的 `__init__.py`** → 候选上一轮改写的 `strategies/__init__.py`
  在这一轮被导入机制执行(实测:标记文件被写出来,而守卫全程放行)。
  现在祖先包也要过同一把尺子;**框架自己预加载**的那条路(守卫还没构造、
  真内建、沙盒之外)由"启动期受信目录核对"兜住。
- **High 1**:提交基准是**懒取**的(第一次 check() 时才算,而那发生在候选运行期间),
  候选在运行期间提交一次 → 基准按候选挑过的那棵树算 → 篡改被放行。
  现在基准在**构造守卫时**取,并对 **HEAD 解析到的 sha** 做指纹(ref 一动就响)。
- **Medium 2**:字节码清理只清"被核对的那一个源文件",父包的 `.pyc` 从不清;
  而且清缓存用的路径与导入机制定位缓存的路径可能不一致(符号链接)。
- **Medium 3**:提交基准放在**进程级**缓存里,不随 HEAD 变 → 开发者正常提交一次
  之后,同一个进程里新建的守卫拿旧基准,把没被篡改的受信模块误杀。
- **Medium 1**:通配符扫描只 glob `.py` → 候选写进站点包的**无源码 `.pyc`**
  辅助模块根本不在清单里(实测:被导入机制执行)。

**每条规则都有双向探针**(放行 + 拦截),断言"**是哪一条规则**拒的"。
本文件每条规则都做过变异验证:把对应代码去掉,用例必须失败。
"""
from __future__ import annotations

import importlib.util
import marshal
import struct
import subprocess
import sys
from pathlib import Path

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


# ------------------------------------------ Critical 1:祖先包的 __init__.py 也要核

def test_the_guard_refuses_when_an_ancestor_package_init_was_tampered(repo):
    """拦截:候选在"上一轮"改了 `strategies/__init__.py`,这一轮导入子模块要被拒。

    导入机制导入 `strategies.base` **之前必定先执行** `strategies/__init__.py`——
    守卫只核对 `strategies.base` 是不够的(实测:标记文件被写出来,而守卫放行)。
    """
    (repo / "strategies" / "__init__.py").write_text(
        "from pathlib import Path\nPath('MARKER').write_text('x')\n", encoding="utf-8")
    guard = guard_for(repo)
    reason = guard.check("strategies.base")
    assert reason, "祖先包的 __init__.py 被改写过,竟然还能导入子模块"
    assert "祖先包" in reason, f"拒绝理由不是祖先包那条:{reason}"
    assert "与仓库提交的内容不一致" in reason, f"拒绝理由不是提交基准那条:{reason}"


def test_the_guard_still_allows_an_untouched_ancestor_package(repo):
    """放行:没被动过的祖先包不能被误伤(否则这条规则会把一切拦掉)。"""
    guard = guard_for(repo)
    assert guard.check("strategies.base") is None


def test_the_wildcard_import_also_checks_the_ancestor_package(repo):
    """拦截:`from strategies.base import *` 这一路同样要核祖先包的 `__init__.py`。"""
    (repo / "strategies" / "__init__.py").write_text(
        "from pathlib import Path\nPath('MARKER').write_text('x')\n", encoding="utf-8")
    guard = guard_for(repo)
    reason = guard.check_package_wildcard("strategies.base")
    assert reason, "祖先包被改写过,通配符裁决竟然放行"
    assert "祖先包" in reason, f"拒绝理由不是祖先包那条:{reason}"


def test_the_startup_check_refuses_a_tampered_trusted_tree(repo):
    """拦截:**框架预加载**那条路(守卫还没构造、真内建、沙盒之外)由启动期核对兜住。

    `risk/constitution.py::_preload_protected_modules` 在跑候选之前就
    `import strategies.…` → 导入机制先执行父包的 `__init__.py`。
    """
    (repo / "strategies" / "__init__.py").write_text(
        "from pathlib import Path\nPath('MARKER').write_text('x')\n", encoding="utf-8")
    problems = rs.verify_trusted_tree(repo, rs.ALLOWED_REPO_IMPORT_DIRS)
    assert problems, "受信目录被改写过,启动期核对竟然说没问题"
    assert any("与仓库提交的内容不一致" in p for p in problems), \
        f"问题清单说的不是提交基准那条:{problems}"


def test_the_startup_check_allows_an_untouched_trusted_tree(repo):
    """放行:干净的受信目录不能被启动期核对拦掉(否则框架跑不起来)。"""
    assert rs.verify_trusted_tree(repo, rs.ALLOWED_REPO_IMPORT_DIRS) == []


def test_the_production_entry_refuses_to_run_against_a_tampered_trusted_tree(
        tmp_path, monkeypatch, repo):
    """拦截(**生产入口**):祖先包被改写过 → 这一轮根本不跑。

    这一条盯的是"启动期核对真的接在生产路径上",不是"函数本身对不对"——
    只测函数的话,把调用点删掉用例照样全绿。
    """
    (repo / "strategies" / "__init__.py").write_text(
        "from pathlib import Path\nPath('MARKER').write_text('x')\n", encoding="utf-8")
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    path = tmp_path / "candidate.py"
    path.write_text("class S:\n    pass\n", encoding="utf-8")
    with pytest.raises(api.JobError) as excinfo:
        api.import_candidate(path, "S", api.rs.file_sha256(path))
    assert "受信目录与仓库提交不一致" in str(excinfo.value), (
        f"生产入口没有因为受信目录被篡改而拒绝:{excinfo.value}")
    assert not (repo / "MARKER").exists(), "被改写的祖先包被执行了(标记文件被写出来)"


def test_the_bytecode_cache_path_used_is_the_one_the_import_machinery_uses(tmp_path):
    """拦截:清字节码要用**导入机制自己算出来的那个路径**(`spec.cached`)。

    自己按 resolve 之后的路径算,遇到符号链接就会打空——清不到要清的那一份,
    伪造的字节码照样生效。这条用例直接盯"清掉的是不是传进来的那个路径"。
    """
    source = tmp_path / "mod.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    cached = tmp_path / "elsewhere" / "mod.cpython-313.pyc"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"fake")
    computed = Path(importlib.util.cache_from_source(str(source)))
    computed.parent.mkdir(parents=True, exist_ok=True)
    computed.write_bytes(b"fake")
    rs._purge_bytecode_cache(source, cached=cached)
    assert not cached.exists(), "导入机制实际会用的那份字节码没有被清掉"
    assert computed.exists(), "清错了对象:动到了不是导入机制会用的那一份"


# --------------------------------------------- High 1:提交基准必须是"运行前"的

def test_the_baseline_is_taken_before_the_candidate_runs(repo):
    """拦截:候选在运行期间提交一次 → 基准不能跟着变成"候选挑过的那棵树"。

    原来基准是**懒取**的(第一次 check() 才算),而第一次 check() 发生在候选运行期间;
    候选提交一次(`.git/HEAD` 的内容一个字没变,只是 ref 指到了别处)就绕过去了:
    懒取到的基准正是候选自己提交的那棵树 → 篡改过的模块被放行。
    场景:上一轮候选改了 `strategies/base.py`(本轮快照已经收了它),
    这一轮候选运行期间把它提交上去 → 基准跟着变 → 放行。
    """
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    guard = guard_for(repo)                       # 守卫在这里构造(候选还没跑)
    # 候选在运行期间把改写提交上去:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "candidate"],
                   check=True, capture_output=True)
    reason = guard.check("strategies.base")
    assert reason, "候选在运行期间提交过,篡改竟然被放行"
    # 要么是"元数据变了"(ref 动了),要么是"与构造时的基准不一致"——
    # 两条都是 fail-closed,断言其中之一即可,但要说清是哪一条。
    assert ("仓库元数据" in reason) or ("与仓库提交的内容不一致" in reason), \
        f"拒绝理由不是那两条之一:{reason}"


def test_the_commit_baseline_alone_refuses_a_tamper_committed_during_the_run(
        repo, monkeypatch):
    """拦截(**单层**):把元数据指纹这一层关掉,提交基准这一层**自己**也要挡得住。

    为什么单独拆一条:HEAD 指纹与提交基准是**两道**独立的防线,同一个攻击
    ("候选在运行期间把自己的篡改提交上去")会被两道都拦。这条用例把指纹层
    摘掉,专门验证"基准是在**构造守卫时**取的"这件事本身有判别力——
    否则基准改成懒取,只要指纹还在,用例照样全绿(测不出基准这一层)。
    """
    monkeypatch.setattr(rs, "git_metadata_fingerprints", lambda root: {})
    (repo / "strategies" / "base.py").write_text("VALUE = 666\n", encoding="utf-8")
    guard = guard_for(repo)                       # 构造时取基准(篡改还没提交)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "candidate"],
                   check=True, capture_output=True)
    reason = guard.check("strategies.base")
    assert reason, "候选把自己的篡改提交上去之后竟然被放行"
    assert "与仓库提交的内容不一致" in reason, (
        f"拒绝理由不是提交基准那条:{reason}")


def test_a_commit_during_the_run_is_seen_as_a_metadata_change(repo):
    """拦截:HEAD 解析到的 sha 变了 → 指纹必须响(`.git/HEAD` 的内容一个字没变)。"""
    guard = guard_for(repo)
    (repo / "strategies" / "other.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "someone"],
                   check=True, capture_output=True)
    reason = guard.check("strategies.base")
    assert reason, "候选运行期间仓库被提交过,竟然毫无察觉"
    assert "仓库元数据" in reason, f"拒绝理由不是元数据指纹那条:{reason}"


def test_a_normal_commit_does_not_false_kill_a_later_guard(repo):
    """放行:开发者正常提交一次之后,**新**建的守卫不该误杀没被篡改的模块。

    原来基准放在进程级缓存里(键只有仓库路径),不随 HEAD 变 → 新守卫拿旧基准 → 误杀。
    """
    guard_before = guard_for(repo)
    assert guard_before.check("strategies.base") is None
    (repo / "strategies" / "base.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "dev"],
                   check=True, capture_output=True)
    guard_after = guard_for(repo)
    assert guard_after.check("strategies.base") is None, \
        "正常提交之后,新守卫把没被篡改的受信模块误杀了"


# -------------------------------------- Medium 2:父包的字节码缓存也要清

def _write_pyc(target: Path, code: str) -> Path:
    """写一个**无时间戳校验**的字节码文件(源文件为空、mtime=0 → 一直"有效")。"""
    data = (importlib.util.MAGIC_NUMBER + struct.pack("<I", 0)
            + struct.pack("<I", 0) + struct.pack("<I", 0)
            + marshal.dumps(compile(code, str(target), "exec")))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def test_the_bytecode_cache_of_an_ancestor_package_is_purged(repo):
    """拦截:父包的 `.pyc` 也要清——否则伪造的字节码能盖过已核对过的源码。

    `.pyc` 头里的 mtime 是**秒**(源码 mtime 取整到秒),伪造时要按秒写,
    否则那个 `.pyc` 本来就无效(导入机制会重编译),测不到要测的东西
    (第 26 轮 codex 第二轮复核的 Low 2:上一版的用例用的是纳秒,没复现它声称的攻击)。
    """
    cache_dir = repo / "strategies" / "__pycache__"
    cache = cache_dir / "__init__.cpython-313.pyc"
    _write_pyc(cache, "from pathlib import Path\nPath('MARKER').write_text('x')\n")
    guard = guard_for(repo)
    assert guard.check("strategies.base") is None, \
        f"干净的仓库不该被拒:{guard.check('strategies.base')}"
    assert not cache.exists(), "祖先包的字节码缓存没有被清掉(伪造的字节码还能生效)"


def test_the_bytecode_cache_of_the_module_itself_is_purged(repo):
    """放行对照:被导入模块自己的 `.pyc` 同样要清(这条上一轮就有了,别退回去)。"""
    cache = repo / "strategies" / "__pycache__" / "base.cpython-313.pyc"
    _write_pyc(cache, "VALUE = 666\n")
    guard = guard_for(repo)
    assert guard.check("strategies.base") is None
    assert not cache.exists(), "模块自己的字节码缓存没有被清掉"


# -------------------------------- Medium 1:通配符扫描要看得见无源码的 .pyc

def _site_package(tmp_path, monkeypatch):
    """仓库**之外**的一个站点包(还没有辅助模块);返回 (仓库根, 包目录)。"""
    outside = tmp_path / "outside"
    pkg = outside / "extpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("__all__ = ['helper']\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(outside))
    for name in [n for n in list(sys.modules)
                 if n == "extpkg" or n.startswith("extpkg.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return repo_root, pkg


def test_the_wildcard_scan_sees_a_sourceless_pyc_in_a_site_package(tmp_path, monkeypatch):
    """拦截:站点包里的**无源码 `.pyc`** 辅助模块也要进清单。

    原来扫描只 glob `.py` → 候选在运行期间写进站点包的 `helper.pyc`
    根本不在清单里,而导入机制会直接执行它(无源码模块)。
    """
    repo_root, pkg = _site_package(tmp_path, monkeypatch)
    guard = guard_for(repo_root, allowed_dirs=("strategies",),
                      verify_trusted_against_git=False)
    # 候选在运行期间往站点包里写一个无源码模块:
    _write_pyc(pkg / "helper.pyc", "VALUE = 666\n")
    reason = guard.check_package_wildcard("extpkg")
    assert reason, "站点包里的无源码 .pyc 竟然不在通配符清单里"
    assert "helper.pyc" in reason, f"拒绝理由没有点名那个 .pyc:{reason}"


def test_the_wildcard_scan_still_allows_an_ordinary_site_package(tmp_path, monkeypatch):
    """放行:干净的站点包不能被误伤。"""
    repo_root, pkg = _site_package(tmp_path, monkeypatch)
    (pkg / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    guard = guard_for(repo_root, allowed_dirs=("strategies",),
                      verify_trusted_against_git=False)
    assert guard.check_package_wildcard("extpkg") is None
