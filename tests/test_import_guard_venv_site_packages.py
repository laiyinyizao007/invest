"""导入守卫:仓库**内**的站点包该怎么分类(Issue #47)。

背景:仓库自带的 venv 常常就躺在仓库根之内(本仓库是 `.venv/`),于是站点包里的模块
**物理上**在仓库根之下。旧口径按字面判「在仓库根之下 = 仓库内模块」,把它们直接拒掉——
而 `RepoImportGuard` 的 docstring 第三支(站点包:要求「候选开始运行之前就存在」)
就永远用不上,候选连 `vnpy_ctastrategy` 都 import 不了。

本文件把**两个方向**都钉住:
- 放行:仓库内的站点包**预先存在** → 允许导入;
- 拦截:仓库自己的代码(`risk/` 等)→ 仍然被拒;
- 拦截:候选在站点包里**运行期间新写**的模块 → 仍然被拒(「预先存在」这条防线没被拆)。

每条都配**放行 + 拦截**两侧,否则探针可能恒真。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import research_sandbox as rs  # noqa: E402


def _git_commit_all(root: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": "/usr/bin:/bin:/c/Windows/System32",
        "HOME": str(root),
    }
    for cmd in (["git", "init", "-q", "-b", "main"],
                ["git", "add", "-A"],
                ["git", "commit", "-q", "-m", "base"]):
        subprocess.run(cmd, cwd=str(root), check=True, env=env, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个临时仓库:里面有一个「站点包」根(`.venv/Lib/site-packages`)与仓库自己的代码。

    两条路径都要进 `sys.path` —— 守卫是**按导入机制解析出来的**来源路径裁决的,
    路径不在 sys.path 上就解析不到来源,那条分支根本走不到(探针会空转)。
    """
    root = tmp_path / "repo"
    site = root / ".venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (root / "strategies").mkdir()
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (root / "strategies" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "risk").mkdir()
    (root / "risk" / "__init__.py").write_text("", encoding="utf-8")
    (root / "risk" / "evil.py").write_text("VALUE = 2\n", encoding="utf-8")
    (site / "thirdparty.py").write_text("VALUE = 3\n", encoding="utf-8")
    _git_commit_all(root)
    # 站点包里的文件必须**早于**候选运行开始(否则会被「预先存在」那条拦掉)
    old = time.time() - 3600
    for path in site.rglob("*.py"):
        os.utime(path, (old, old))

    # 守卫是按导入机制解析来源的 —— 两条路径都要在 sys.path 上。
    # 顺带把仓库根排在站点包**后面**,免得 `risk` 之类的仓库包被站点包里的同名包遮住。
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(root)

    # 测试期间真的把临时仓库的模块 import 进了 sys.modules:
    # monkeypatch 只还原「删除时存在的」那几项,新导入的不会被清掉 →
    # 后面的测试会拿到指向临时仓库的残缺包。整体还原。
    # 站点包根指到临时仓库里那个 `.venv/Lib/site-packages`。
    # 用 monkeypatch 改**模块函数**(而不是构造参数):修复前的那份代码没有这个概念,
    # 改模块函数能让**同一份用例**在修复前也跑得起来 —— 于是「修复前红、修复后绿」
    # 才是真的由防线差异决定,而不是由「用例报错」决定。
    site_root = site.resolve()
    monkeypatch.setattr(rs, "site_package_roots", lambda: (site_root,), raising=False)

    saved = dict(sys.modules)
    yield root
    for name in [n for n in list(sys.modules) if n not in saved]:
        del sys.modules[name]
    sys.modules.update(saved)


def _guard(root: Path, **kwargs):
    kwargs.setdefault("allowed_dirs", rs.ALLOWED_REPO_IMPORT_DIRS)
    kwargs.setdefault("snapshot", rs.snapshot_allowed_files(root))
    return rs.RepoImportGuard(root, **kwargs)


def test_a_site_package_inside_the_repo_can_be_imported(repo: Path) -> None:
    """放行:仓库**内**的站点包,只要它预先存在就该能导入。

    这是 Issue #47 的正面用例:旧口径把这类模块直接拒了,
    候选于是连 `vnpy_ctastrategy` 都 import 不了(主 checkout 里 4 条用例红)。
    """
    guard = _guard(repo)
    assert guard.check("thirdparty") is None


def test_a_site_package_outside_the_repo_can_still_be_imported(repo: Path) -> None:
    """放行(回归):站点包在仓库**之外**时,行为与从前一致 —— 允许。"""
    outside = repo.parent / "outside_site"
    outside.mkdir()
    (outside / "outsidepkg.py").write_text("VALUE = 4\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(outside / "outsidepkg.py", (old, old))
    import research_sandbox as _rs
    old = _rs.site_package_roots
    try:
        _rs.site_package_roots = lambda: (outside.resolve(),)
        guard = _guard(repo)
    finally:
        _rs.site_package_roots = old
    assert guard.check("outsidepkg") is None


def test_a_repo_module_outside_allowed_dirs_is_still_refused(repo: Path) -> None:
    """拦截:仓库自己的代码(`risk/`)仍然**一律被拒**。

    这是本修复的红线:改的只是**站点包的分类**,仓库自己的框架/风控/回测模块
    一道检查都没松。站点包豁免绝不能顺带把 `risk/` 也放过去。
    """
    guard = _guard(repo)
    reason = guard.check("risk.evil")
    assert reason is not None, "仓库自己的模块被放行了 —— 防线被拆"
    assert "不在允许的目录" in reason, reason


def test_a_module_written_into_the_site_package_during_the_run_is_refused(repo: Path) -> None:
    """拦截:候选在站点包里**运行期间新写**的模块仍然被拒。

    第 24 轮 codex 的原始理由:站点包在宿主机上通常可写,候选往里写一个辅助模块
    就能绕开仓库内的路径规则。所以站点包那一支带着「预先存在」的检查——
    本修复只改**分类**,不改这道检查,这条用例把它钉住。
    """
    started = time.time_ns()
    guard = _guard(repo, started_at_ns=started)
    # 候选「运行期间」往站点包里写一个模块
    (repo / ".venv" / "Lib" / "site-packages" / "sneaky.py").write_text(
        "VALUE = 5\n", encoding="utf-8")
    reason = guard.check("sneaky")
    assert reason is not None, "候选运行期间写出的模块被放行了 —— 「预先存在」这条防线被拆"
    assert "之后才出现" in reason, reason


def test_site_package_roots_are_discovered_for_the_running_interpreter() -> None:
    """站点包根必须真的能被发现 —— 否则上面几条用例只是「注入了一个假根」。"""
    roots = rs.site_package_roots()
    assert roots, "站点包根一个都没取到(分类会退回字面口径 = Issue #47 没修)"
    assert any(root.exists() for root in roots), roots
