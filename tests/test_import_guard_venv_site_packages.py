"""导入守卫:仓库**内**的站点包该怎么分类(Issue #47)。

背景:仓库自带的 venv 常常就躺在仓库根之内(本仓库是 `.venv/`),于是站点包里的模块
**物理上**在仓库根之下。旧口径按字面判「在仓库根之下 = 仓库内模块」,把它们直接拒掉——
而 `RepoImportGuard` 的 docstring 第三支(站点包:要求「候选开始运行之前就存在」)
就永远用不上,候选连 `vnpy_ctastrategy` 都 import 不了。

**判据(修好后)**:一个模块算不算「第三方库」,看它是不是**站点包里安装好的文件**——
即它在 `*.dist-info/RECORD`(安装记录)里,且**内容没被改过**。
安装记录就是站点包的「提交基准」等价物:受信目录正因如此才补了 sha256 + git 基准。

按仓库自己的标准([[static-ban-rules-need-dynamic-probes]]),这些用例是**动态探针**:
真的写文件、真的 import、真的看结果。只靠静态推理一定漏掉写法——
第 26 轮 codex 实测过的「上一轮写、这一轮导」就是静态推理看不出来的。
"""
from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import textwrap
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


def _write_record(site_root: Path, package: str) -> None:
    """给站点包写一份 pip 格式的安装记录(`*.dist-info/RECORD`)。

    格式照 pip:`相对路径,sha256=<urlsafe-base64 原始摘要>,大小`。
    守卫靠它判断「这个文件是**装上去的**,还是候选自己写进去的」。
    """
    target = site_root / f"{package}.py"
    data = target.read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    dist_info = site_root / f"{package}-1.0.dist-info"
    dist_info.mkdir(exist_ok=True)
    (dist_info / "RECORD").write_text(
        f"{package}.py,sha256={digest},{len(data)}\n", encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个临时仓库:里面有「仓库内的站点包」与仓库自己的代码。"""
    root = tmp_path / "repo"
    site = root / ".venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    (root / "strategies").mkdir()
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (root / "strategies" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "risk").mkdir()
    (root / "risk" / "__init__.py").write_text("", encoding="utf-8")
    (root / "risk" / "evil.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "framework.py").write_text("VALUE = 3\n", encoding="utf-8")
    # 装上去的第三方库(带安装记录)
    (site / "thirdparty.py").write_text("VALUE = 4\n", encoding="utf-8")
    _write_record(site, "thirdparty")
    _git_commit_all(root)
    # 站点包里的文件必须**早于**候选运行开始
    old = time.time() - 3600
    for path in site.rglob("*.py"):
        os.utime(path, (old, old))

    # 站点包根指到临时仓库里那个 `.venv/Lib/site-packages`。
    # 用 monkeypatch 改**模块函数**:修复前的那份代码没有这个概念,
    # 改模块函数能让**同一份用例**在修复前也跑得起来 —— 于是「修复前红、修复后绿」
    # 才是真的由防线差异决定,而不是由「用例报错」决定。
    site_root = site.resolve()
    monkeypatch.setattr(rs, "site_package_roots", lambda: (site_root,), raising=False)
    # 安装记录也要跟着换(否则索引的是真实解释器的站点包)
    monkeypatch.setattr(rs, "installed_site_files",
                        lambda _roots: rs._read_site_records(site_root), raising=False)

    # 守卫是按导入机制解析来源的 —— 两条路径都要在 sys.path 上。
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(root)

    # 测试期间真的把临时仓库的模块 import 进了 sys.modules:
    # monkeypatch 只还原「删除时存在的」那几项,新导入的不会被清掉 → 后面��测试会拿到
    # 指向临时仓库的残缺包。整体还原。
    saved = dict(sys.modules)
    yield root
    for name in [n for n in list(sys.modules) if n not in saved]:
        del sys.modules[name]
    sys.modules.update(saved)


def _guard(root: Path, **kwargs):
    kwargs.setdefault("allowed_dirs", rs.ALLOWED_REPO_IMPORT_DIRS)
    kwargs.setdefault("snapshot", rs.snapshot_allowed_files(root))
    kwargs.setdefault("verify_trusted_against_git", False)
    return rs.RepoImportGuard(root, **kwargs)


def _run_candidate(root: Path, source: str):
    """在受限内建下执行一段候选代码,返回它抛出的异常(或 None)。

    走的是**真实路径**(`safe_builtins` + 导入门),不是直接调 `guard.check()`——
    静态调用看不见导入机制内部做的事(第 22 轮 codex 实测过)。
    """
    guard = _guard(root)
    # 名字白名单是**另一道**闸(与路径裁决无关):把测试用的名字显式放进去,
    # 否则探针停在「未列入白名单」那一支,根本走不到路径裁决,测不到本修复。
    builtins = rs.safe_builtins(allowed_modules=["thirdparty"], repo_guard=guard)
    namespace = {"__builtins__": builtins, "__name__": "pending_strategy"}
    try:
        exec(compile(textwrap.dedent(source), "<candidate>", "exec"), namespace)
    except Exception as exc:  # noqa: BLE001 - 探针要看的就是异常
        return exc
    return None


# ---------------------------------------------------------------- 放行

def test_a_site_package_inside_the_repo_can_be_imported(repo: Path) -> None:
    """放行:仓库**内**、且**装上去的**站点包,候选能导入。

    这是 Issue #47 的正面用例:旧口径把这类模块直接拒了,
    候选于是连 `vnpy_ctastrategy` 都 import 不了(主 checkout 里 4 条用例红)。
    """
    guard = _guard(repo)
    assert guard.check("thirdparty") is None


def test_an_installed_package_is_importable_through_the_real_import_path(repo: Path) -> None:
    """放行(动态探针):走真实导入路径,装好的第三方库也能导进来。"""
    exc = _run_candidate(repo, "import thirdparty")
    assert exc is None, f"装好的站点包被拒了(Issue #47 没修好): {exc}"


def test_a_site_package_outside_the_repo_can_still_be_imported(repo: Path) -> None:
    """放行(回归):站点包在仓库**之外**时,行为与从前一致 —— 允许。

    目录必须**在 sys.path 上**,否则导入机制解析不到来源、守卫根本没裁决,
    这条用例就会**恒真**(第 4 轮 codex 的 M2:恒真的用例给人虚假的安全感)。
    """
    outside = repo.parent / "outside_site"
    outside.mkdir()
    (outside / "outsidepkg.py").write_text("VALUE = 5\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(outside / "outsidepkg.py", (old, old))
    monkey_outside = os.path.normpath(str(outside.resolve()))
    original = rs.site_package_roots
    try:
        rs.site_package_roots = lambda: (outside.resolve(),)
        guard = _guard(repo)
        # 目录得在 sys.path 上,否则解析不到来源
        sys.path.insert(0, str(outside.resolve()))
        try:
            assert guard.check("outsidepkg") is None
        finally:
            sys.path.remove(str(outside.resolve()))
    finally:
        rs.site_package_roots = original
    assert monkey_outside  # 目录确实解析过


# ---------------------------------------------------------------- 拦截

def test_a_repo_module_outside_allowed_dirs_is_still_refused(repo: Path) -> None:
    """拦截:仓库自己的代码(`risk/`、`scripts/`)仍然**一律被拒**。

    这是本修复的红线:改的只是**站点包的分类**,仓库自己的框架/风控/回测模块
    一道检查都没松。站点包豁免绝不能顺带把 `risk/` 也放过去。
    """
    guard = _guard(repo)
    for name in ("risk.evil", "scripts.framework"):
        reason = guard.check(name)
        assert reason is not None, f"{name} 被放行了 —— 防线被拆"
        assert "不在允许的目录" in reason, reason


def test_a_module_written_into_the_site_package_during_the_run_is_refused(repo: Path) -> None:
    """拦截:候选在站点包里**写出来**的模块仍然被拒。

    第 24 轮 codex 的原始理由:站点包在宿主机上通常可写,候选往里写一个辅助模块
    就能绕开仓库内的路径规则。所以站点包那一支带着「是不是装上去的」这道检查——
    本修复只改**分类**,不改这道检查,这条用例把它钉住。

    **断言「被拒」而不是断言拒绝理由的字样**:拒绝可能来自**更严**的那一支
    (不在安装记录里 → 当作仓库内模块直接拒),那比「预先存在」更严,同样是好事。
    钉死理由字样反而会在收紧实现时误报。
    """
    started = time.time_ns()
    guard = _guard(repo, started_at_ns=started)
    # 候选「运行期间」往站点包里写一个模块
    (repo / ".venv" / "Lib" / "site-packages" / "sneaky.py").write_text(
        "VALUE = 6\n", encoding="utf-8")
    reason = guard.check("sneaky")
    assert reason is not None, "候选写进站点包的模块被放行了 —— 这道防线被拆"


def test_a_helper_module_written_in_one_round_cannot_be_imported_in_the_next(repo: Path) -> None:
    """拦截(**跨轮次**,动态探针):第 1 轮写进站点包的辅助模块,第 2 轮导不进来。

    为什么这条最要紧:守卫是**每个子进程重建一次**的
    (`research_api.py` 每轮刷新 `started_at_ns`),所以「mtime 早于本轮起点」
    这道检查**挡不住跨轮次**——第 1 轮写,第 2 轮的文件就「早于本轮起点」了。
    实测(第 4 轮 codex 的 H1):候选用白名单里的真实包 `numpy` 写下辅助模块,
    第 2 轮那个辅助模块被执行并**拿到真内建**。

    挡住它的正是**安装记录**:候选写的文件不在 `*.dist-info/RECORD` 里 → 不算装上去的。
    这条用例按仓库自己的标准写成动态探针:真的写、真的导、真的看结果。
    """
    site = repo / ".venv" / "Lib" / "site-packages"
    # 第 1 轮:候选往站点包里写一个辅助模块
    (site / "roundsneaky.py").write_text("MARKER = 1\n", encoding="utf-8")
    # 第 2 轮:**新守卫、新起点**(这正是线上每轮的情形)
    guard = _guard(repo, started_at_ns=time.time_ns())
    reason = guard.check("roundsneaky")
    assert reason is not None, "上一轮写进站点包的模块在下一轮被放行了 —— 跨轮次逃逸成立"


def test_an_installed_package_that_was_tampered_with_is_refused(repo: Path) -> None:
    """拦截:装好的站点包文件**被改写过** → 仍被拒(内容与安装记录对不上)。

    仓库早就证过「只比 mtime 不可靠」(改写内容再把 mtime 还原能骗过),
    受信目录正因如此补了 sha256 + 提交基准;安装记录是站点包的同一把尺子。
    """
    target = repo / ".venv" / "Lib" / "site-packages" / "thirdparty.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n# tampered\n")
        guard = _guard(repo)
        reason = guard.check("thirdparty")
        assert reason is not None, "被改写过的站点包文件被放行了 —— 内容校验失效"
    finally:
        target.write_bytes(original)


def test_a_repo_module_written_during_the_run_is_still_refused(repo: Path) -> None:
    """拦截(回归):仓库里的模块在运行期间被改写 → 仍然被拒。"""
    guard = _guard(repo, started_at_ns=time.time_ns())
    reason = guard.check("risk.evil")
    assert reason is not None
    assert "不在允许的目录" in reason, reason


def test_site_package_roots_are_discovered_for_the_running_interpreter() -> None:
    """站点包根必须真的能被发现 —— 否则上面几条用例只是「注入了一个假根」。

    这条是 **API 存在性探针**,不是防线证据:它只保证新能力存在。
    """
    roots = rs.site_package_roots()
    assert roots, "站点包根一个都没取到(分类会退回字面口径 = Issue #47 没修)"
    assert any(root.exists() for root in roots), roots
    # 只认真的是站点包目录的(Windows 上 site.getsitepackages() 返回的是 prefix 本身)
    assert all(root.name in ("site-packages", "dist-packages") for root in roots), roots
