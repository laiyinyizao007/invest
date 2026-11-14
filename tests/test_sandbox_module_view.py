"""第 24 轮的沙盒层用例(Issue #26):模块视图、能力判定、快照哈希、fail-closed。

这一份与 `tests/test_candidate_escape_production_path.py` 分开:
那一份走**生产入口**(`import_candidate`),测的是"整条路"；
这一份直接测**沙盒层的各条规则**,因为第 24 轮实测出来的洞大多在规则之间的缝隙里
(规则本身没坏,是"这条规则管得到的那条路"之外还有别的路)。

每条用例都断言**是哪一条规则**拒绝的——"被拒绝了"不够,第 22 轮踩过的坑。
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import research_sandbox as rs      # noqa: E402


# ---------------------------------------------------------------------------
# 一、模块视图:候选拿到的不是模块,是视图
# ---------------------------------------------------------------------------

DANGEROUS_ATTRIBUTES = ("sys", "os")


def test_the_candidate_gets_a_view_instead_of_the_real_module():
    """`import logging` 给候选的必须是**视图**,不是模块本身。

    否则候选一句 `logging.__dict__` 就摸回真模块(视图这一层就白做了)。
    """
    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    assert not isinstance(view, type(rs)), "给候选的是模块本身,不是视图"
    assert isinstance(view, rs.ModuleView)
    with pytest.raises(AttributeError):
        view.__dict__


@pytest.mark.parametrize("name", DANGEROUS_ATTRIBUTES)
def test_the_view_does_not_hand_out_the_dangerous_modules(name):
    """视图把危险模块的属性挡下来,并在理由里写明是哪一条规则。"""
    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    with pytest.raises(AttributeError) as exc:
        getattr(view, name)

    message = str(exc.value)
    assert "模块视图" in message or "能力泄漏" in message, message


def test_the_view_closes_the_logging_sys_route():
    """第 24 轮 codex 实测的那条路:`logging.sys.modules["os"].system(...)`。

    走完之后必须**摸不到** `os`——不是"摸到了但用不了"。
    """
    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    with pytest.raises(AttributeError) as exc:
        view.sys
    assert "禁止访问 logging.sys" in str(exc.value), str(exc.value)


def test_the_view_still_gives_the_ordinary_attributes():
    """放行方向:视图不能把正常的东西也一起收走。"""
    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    assert view.getLogger is not None
    assert "getLogger" in dir(view)


def test_the_view_refuses_a_module_that_hands_out_a_capability_class():
    """判据是"对象**从哪儿定义**",不是"名字像不像"。

    所以只递出**类**(不递出 ctypes 模块)也一样堵住——
    `subprocess.Popen`、`ctypes.CDLL` 都是"递出去就能执行命令"的东西。
    """
    import ctypes
    import types as pytypes

    fake = pytypes.ModuleType("fake_pkg")
    fake.Popen = ctypes.CDLL

    with pytest.raises(AttributeError) as exc:
        rs._view_for(fake).Popen

    assert "能力泄漏" in str(exc.value), str(exc.value)


# ---------------------------------------------------------------------------
# 二、按全名禁用的子模块 + 第 24 轮补进名单的名字
# ---------------------------------------------------------------------------

FORBIDDEN_SUBMODULE_PATHS = ("numpy.ctypeslib", "numpy.f2py", "numpy.distutils")

NEW_BLOCKED_MODULES = (
    "timeit", "doctest", "pdb", "bdb", "trace", "profile", "cProfile", "shelve",
    "distutils", "setuptools", "pip", "venv", "ensurepip", "concurrent",
    "socketserver", "telnetlib", "xmlrpc",
)


@pytest.mark.parametrize("name", FORBIDDEN_SUBMODULE_PATHS)
def test_the_runtime_layer_refuses_the_dangerous_submodule_paths(name):
    """按**全名**禁:根名字(numpy)照常放行,这几个角落禁掉。"""
    safe = rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert "按全名禁用" in str(exc.value), str(exc.value)


@pytest.mark.parametrize("name", FORBIDDEN_SUBMODULE_PATHS)
def test_the_static_layer_refuses_the_dangerous_submodule_paths(name):
    """静态层同样按全名拒(两道独立的闸)。"""
    findings = rs.screen_source(f"import {name}\n")

    assert findings, f"静态层没拦下 {name}"
    assert "numpy" in findings[0], findings


@pytest.mark.parametrize("name", NEW_BLOCKED_MODULES)
def test_the_runtime_layer_refuses_the_newly_blocked_names(name):
    """第 24 轮补进禁用名单的名字:运行期那一层必须拒。"""
    safe = rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert name in str(exc.value), str(exc.value)


@pytest.mark.parametrize("name", NEW_BLOCKED_MODULES)
def test_the_static_layer_refuses_the_newly_blocked_names(name):
    """静态层同样拒(两道独立的闸)。"""
    findings = rs.screen_source(f"import {name}\n")

    assert findings, f"静态层没拦下 {name}"


# ---------------------------------------------------------------------------
# 三、快照:三项一起比(mtime + size + sha256)
# ---------------------------------------------------------------------------

def _make_importable_repo(tmp_path, monkeypatch):
    """造一个"模块真的能被找到"的仓库:路径规则按 origin 裁决,
    找不到模块的话规则根本不启动(用例会绿得毫无意义)。"""
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    target = root / "strategies" / "mod.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in [n for n in list(sys.modules) if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return root, target


def test_the_snapshot_notices_a_content_change_even_when_the_mtime_is_restored(tmp_path, monkeypatch):
    """只比 mtime 会被骗:改写内容再把 mtime 还原,粗粒度文件系统上 mtime 根本不变。

    所以快照记三项(mtime、大小、内容哈希),比对也三项。
    """
    root, target = _make_importable_repo(tmp_path, monkeypatch)

    snapshot = rs.snapshot_allowed_files(root)
    guard = rs.RepoImportGuard(root, snapshot=snapshot, started_at_ns=time.time_ns(),
                              verify_trusted_against_git=False)
    assert guard.check("strategies.mod") is None, "前提不成立:守卫一开始就没放行这个模块"

    # 改写内容,并把 mtime 还原成原值
    original_mtime = target.stat().st_mtime_ns
    target.write_text("VALUE = 2\n", encoding="utf-8")       # 长度一样 -> size 也没变
    import os
    os.utime(target, ns=(original_mtime, original_mtime))

    assert original_mtime == target.stat().st_mtime_ns, "本用例的前提没成立(mtime 没被还原)"
    reason = guard.check("strategies.mod")

    assert reason is not None, "内容被改写却没被发现"
    assert "被改动过" in reason, reason


def test_the_guard_refuses_when_the_state_of_the_file_cannot_be_checked(tmp_path, monkeypatch):
    """**查不了等于没查(fail-closed)**:stat 失败一律拒,不许"拿不准就放行"。"""
    root, _target = _make_importable_repo(tmp_path, monkeypatch)

    snapshot = rs.snapshot_allowed_files(root)
    guard = rs.RepoImportGuard(root, snapshot=snapshot, started_at_ns=time.time_ns(),
                              verify_trusted_against_git=False)
    assert guard.check("strategies.mod") is None, "前提不成立:守卫一开始就没放行这个模块"

    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self.name == "mod.py":
            raise OSError("模拟 stat 失败")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    reason = guard.check("strategies.mod")

    assert reason is not None, "查不到文件状态却放行了"
    assert "fail-closed" in reason, reason


# ---------------------------------------------------------------------------
# 四、调用点:白名单必须**显式**传
# ---------------------------------------------------------------------------

def test_the_call_site_passes_the_allow_list_explicitly():
    """靠默认参数的话,将来有人把默认值改成 ("strategies","scripts")
    就能放开整个 scripts/,而登记在宪法历史守卫里的那个常量根本不会被改动。

    所以这一条用"读源码"的方式钉住:调用点必须写着 `allowed_dirs=`。
    """
    source = (Path(__file__).resolve().parents[1] / "scripts" / "research_api.py").read_text(encoding="utf-8")

    assert "allowed_dirs=rs.ALLOWED_REPO_IMPORT_DIRS" in source, (
        "调用点没有显式传允许目录白名单")
    assert "rs.RepoImportGuard(" in source


# ---------------------------------------------------------------------------
# 五、G8 第一轮自查带出的两处小修
# ---------------------------------------------------------------------------

def test_the_view_cache_does_not_keep_the_module_alive():
    """视图用**弱引用**持有模块——否则"弱引用缓存"是假的,条目永不回收。

    反例的形态:value(view)强引用 key(module) -> 弱字典的条目永远不会被回收,
    每个被导入过的模块都永久驻留(无界增长)。
    """
    import weakref

    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    ref = object.__getattribute__(view, "_view_module_ref")
    assert isinstance(ref, weakref.ref), "视图强引用了模块,弱引用缓存形同虚设"
    assert ref() is not None, "模块还在时视图应当可用"


def test_the_view_still_works_after_the_fix():
    """改弱引用不能把视图改坏:普通属性照常给,危险属性照常拒。"""
    safe = rs.safe_builtins(())
    view = safe["__import__"]("logging")

    assert view.getLogger is not None
    with pytest.raises(AttributeError):
        view.sys


@pytest.mark.parametrize("source, denied", [
    ('d["os"]\n', True),
    ('d.get("os")\n', True),
    ('d.pop("sys")\n', True),
    ('dict(os=1)\n', True),
    ('print("os")\n', False),
    ('label = "os"\n', False),
    ('d["close"]\n', False),
])
def test_the_dangerous_short_names_are_only_refused_when_used_as_keys(source, denied):
    """短名只在"按键取"的语境里拒,当普通词时放行(第 24 轮自查修)。

    要封的是"用这些名字当钥匙去取东西"这个动作(`tb_frame.f_globals["os"]`),
    不是这些字符本身——全局禁字符串会误伤策略里的状态名/列名/打印内容。
    """
    findings = rs.screen_source(source)

    key_findings = [f for f in findings if "按键取危险模块" in f]
    if denied:
        assert key_findings, f"按键取危险模块没被拦下:{source!r} -> {findings}"
    else:
        assert not key_findings, f"普通用法被误伤了:{source!r} -> {findings}"
