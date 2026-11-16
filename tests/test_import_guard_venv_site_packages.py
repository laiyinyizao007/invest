"""导入守卫:仓库**内**的站点包该怎么分类(Issue #47)。

背景:仓库自带的 venv 常常就躺在仓库根之内(本仓库是 `.venv/`),于是站点包里的模块
**物理上**在仓库根之下。旧口径按字面判「在仓库根之下 = 仓库内模块」,把它们直接拒掉——
而 `RepoImportGuard` 的 docstring 第三支(站点包:要求「候选开始运行之前就存在」)
就永远用不上,候选连 `vnpy_ctastrategy` 都 import 不了。

**判据(修好后)**:一个模块算不算「候选开跑之前就装好的第三方库」,看三件事:
①它在某个站点包根之下;②它在**父进程拍的快照**里(父进程不是候选执行的,
所以快照不在候选的控制之下);③它此后没被改过(mtime 与大小都对得上)。

按仓库自己的标准([[static-ban-rules-need-dynamic-probes]]),这些用例是**动态探针**:
真的写文件、真的 import、真的看结果。只靠静态推理一定漏掉写法——
第 26 轮 codex 实测过的「上一轮写、这一轮导」就是静态推理看不出来的。
"""
from __future__ import annotations

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


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个临时仓库:里面有「仓库内的站点包」与仓库自己的代码。

    快照由**父进程**拍(这里就是测试替父进程拍),写到临时文件,
    再经环境变量交给守卫——走的是**生产同一条路**。
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
    (root / "scripts").mkdir()
    (root / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts" / "framework.py").write_text("VALUE = 3\n", encoding="utf-8")
    # 装上去的第三方库
    (site / "thirdparty.py").write_text("VALUE = 4\n", encoding="utf-8")
    _git_commit_all(root)
    # 站点包里的文件必须**早于**快照(否则会被「预先存在」那条拦掉)
    old = time.time() - 3600
    for path in site.rglob("*.py"):
        os.utime(path, (old, old))

    # 站点包根指到临时仓库里那个 `.venv/Lib/site-packages`
    site_root = site.resolve()
    monkeypatch.setattr(rs, "site_package_roots", lambda: (site_root,), raising=False)
    # 快照由 `snapshot_env` 这个夹具拍(父进程的角色)并经环境变量交给守卫。

    # 守卫是按导入机制解析来源的 —— 两条路径都要在 sys.path 上。
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.chdir(root)

    # 测试期间真的把临时仓库的模块 import 进了 sys.modules:
    # monkeypatch 只还原「删除时存在的」那几项,新导入的不会被清掉 → 后面的测试会拿到
    # 指向临时仓库的残缺包。整体还原。
    saved = dict(sys.modules)
    yield root
    for name in [n for n in list(sys.modules) if n not in saved]:
        del sys.modules[name]
    sys.modules.update(saved)


# 快照文件的环境变量名:**写成字面量**,不引用实现里的常量——
# 这样这份用例在**修复前**也能跑起来,红的原因才会是「防线行为」
# (误分类),而不是「实现里还没有这个函数」(用例报错 = 假信号)。
SNAPSHOT_ENV = "RESEARCH_SITE_SNAPSHOT"


def _write_snapshot(site_root: Path) -> Path:
    """父进程拍快照并写到文件。

    **自己写**,不用实现里的函数:理由同上(让用例在修复前也能跑)。
    """
    import json
    import tempfile

    snapshot = {}
    for path in site_root.rglob("*.py"):
        stat = path.stat()
        snapshot[os.path.normpath(str(path.resolve()))] = [stat.st_mtime_ns, stat.st_size]
    handle, name = tempfile.mkstemp(prefix="site_snapshot_", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        json.dump(snapshot, stream)
    return Path(name)


@pytest.fixture
def snapshot_env(repo: Path, monkeypatch: pytest.MonkeyPatch):
    """把父进程拍的快照经环境变量交给守卫(生产同一条路)。"""
    site_root = (repo / ".venv" / "Lib" / "site-packages").resolve()
    path = _write_snapshot(site_root)
    monkeypatch.setenv(SNAPSHOT_ENV, str(path))
    return path


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

def test_a_site_package_inside_the_repo_can_be_imported(repo: Path, snapshot_env) -> None:
    """放行:仓库**内**、且**候选开跑之前就装好**的站点包,候选能导入。

    这是 Issue #47 的正面用例:旧口径把这类模块直接拒了,
    候选于是连 `vnpy_ctastrategy` 都 import 不了(主 checkout 里 4 条用例红)。
    """
    guard = _guard(repo)
    assert guard.check("thirdparty") is None


def test_an_installed_package_is_importable_through_the_real_import_path(
    repo: Path, snapshot_env,
) -> None:
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
    original = rs.site_package_roots
    try:
        rs.site_package_roots = lambda: (outside.resolve(),)
        guard = _guard(repo)
        sys.path.insert(0, str(outside.resolve()))
        try:
            assert guard.check("outsidepkg") is None
        finally:
            sys.path.remove(str(outside.resolve()))
    finally:
        rs.site_package_roots = original


# ---------------------------------------------------------------- 拦截

def test_a_repo_module_outside_allowed_dirs_is_still_refused(repo: Path, snapshot_env) -> None:
    """拦截:仓库自己的代码(`risk/`、`scripts/`)仍然**一律被拒**。

    这是本修复的红线:改的只是**站点包的分类**,仓库自己的框架/风控/回测模块
    一道检查都没松。站点包豁免绝不能顺带把 `risk/` 也放过去。
    """
    guard = _guard(repo)
    for name in ("risk.evil", "scripts.framework"):
        reason = guard.check(name)
        assert reason is not None, f"{name} 被放行了 —— 防线被拆"
        assert "不在允许的目录" in reason, reason


def test_a_module_written_into_the_site_package_during_the_run_is_refused(
    repo: Path, snapshot_env,
) -> None:
    """拦截:候选在站点包里**写出来**的模块仍然被拒。

    断言「被拒」而不是断言拒绝理由的字样:拒绝可能来自**更严**的那一支,
    钉死理由字样反而会在收紧实现时误报。
    """
    guard = _guard(repo)
    (repo / ".venv" / "Lib" / "site-packages" / "sneaky.py").write_text(
        "VALUE = 6\n", encoding="utf-8")
    reason = guard.check("sneaky")
    assert reason is not None, "候选写进站点包的模块被放行了 —— 这道防线被拆"


def test_a_helper_module_written_in_one_round_cannot_be_imported_in_the_next(
    repo: Path, snapshot_env,
) -> None:
    """拦截(**跨轮次**,动态探针):第 1 轮写进站点包的辅助模块,第 2 轮导不进来。

    为什么这条最要紧:守卫是**每个子进程重建一次**的,子进程自己拍的快照会
    **包含上一轮候选写进去的文件** —— 那就是候选自己的「自证」。
    实测(第 4 轮 codex 的 H1/C1):候选用白名单里的真实包 `numpy` 写下辅助模块,
    第 2 轮那个辅助模块被执行并**拿到真内建**。

    挡住它的正是**父进程的快照**:候选写的文件不在里面 → 不算装好的。
    这条用例按仓库自己的标准写成动态探针:真的写、真的导、真的看结果。
    """
    site = repo / ".venv" / "Lib" / "site-packages"
    # 第 1 轮:候选往站点包里写一个辅助模块
    (site / "roundsneaky.py").write_text("MARKER = 1\n", encoding="utf-8")
    # 第 2 轮:**新守卫**(线上每轮都是新子进程、新守卫)
    guard = _guard(repo)
    reason = guard.check("roundsneaky")
    assert reason is not None, "上一轮写进站点包的模块在下一轮被放行了 —— 跨轮次逃逸成立"


def test_an_installed_package_that_was_tampered_with_is_refused(
    repo: Path, snapshot_env,
) -> None:
    """拦截:装好的站点包文件**被改写过** → 仍被拒(与快照对不上)。

    仓库早就证过「只比 mtime 不可靠」,受信目录正因如此补了 sha256 + 提交基准;
    站点包这一支用父进程快照的 (mtime, size)——见 ADR 的「已知局限」记的残余。
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


def test_when_the_baseline_cannot_be_computed_the_site_package_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拦截(fail-closed):**基准算不出来** → 仓库内的站点包一律被拒。

    基准有两条来源:父进程经环境变量传下来的快照,或(父进程没给时)本进程的第一份。
    两条都拿不到时(站点包根一个都发现不了),分类必须退回「仓库内模块」那一支 ——
    宁可多拒,不能因为「基准读不到」就放行。
    """
    monkeypatch.delenv(SNAPSHOT_ENV, raising=False)
    # 站点包根一个都发现不了 → 基准里必然没有这个文件
    monkeypatch.setattr(rs, "site_package_roots", lambda: (), raising=False)
    guard = _guard(repo)
    reason = guard.check("thirdparty")
    assert reason is not None, "没有基准却放行了 —— 不是 fail-closed"


def test_a_tampered_snapshot_file_is_refused(
    repo: Path, snapshot_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拦截:候选**伪造快照文件** → 守卫不采信(fail-closed)。

    快照文件落在系统临时目录,那是**候选可写**的地方:候选在上一轮把文件
    改掉(把自己的模块加进快照),下一轮就会读到一份「说自己装好了」的清单。
    哈希由**父进程**算并经环境变量传下来,候选改不了父进程的环境,
    所以对不上哈希就当「没有基准」→ 仓库内的站点包一律拒。
    """
    import json

    # 候选把自己写的模块加进快照,再把文件替换掉
    forged = json.loads(snapshot_env.read_text(encoding="utf-8"))
    planted = repo / ".venv" / "Lib" / "site-packages" / "forged.py"
    marker = "MARKER = 1\n"
    planted.write_text(marker, encoding="utf-8")
    forged[os.path.normpath(str(planted.resolve()))] = [0, len(marker)]
    snapshot_env.write_text(json.dumps(forged), encoding="utf-8")
    # 环境变量里的哈希仍是父进程算的那份 → 对不上
    monkeypatch.setenv(SNAPSHOT_ENV, str(snapshot_env))
    guard = _guard(repo)
    reason = guard.check("forged")
    assert reason is not None, "伪造的快照文件被采信了 —— 候选可以给自己写后门"


def test_a_repo_module_written_during_the_run_is_still_refused(
    repo: Path, snapshot_env,
) -> None:
    """拦截(回归):仓库里的模块在运行期间被改写 → 仍然被拒。"""
    guard = _guard(repo)
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
