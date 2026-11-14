"""候选"写辅助模块再 import"的逃逸,以及它的封堵(Issue #26)。

**为什么按名字堵不收敛**(第 12-14 轮已验证):禁用名单是黑名单,而这条逃逸**根本不经过名单**——
候选用不受限的第三方库(如 `numpy.savetxt`)把 `.py` 写进可写目录,再 import 它。
辅助模块是在 import 机制里执行的,拿到的是**真实内建**(`os`/`sys`/`subprocess` 全有),
静态筛查(只筛候选那份源码)与受限内建(只对候选那份源码生效)对它都不起作用。

因此规则改成**按路径**:
- 仓库内的模块,只有落在允许目录(`strategies/`)里、**且在候选开始运行之前就已存在**才可导入;
- 仓库内的其它模块(`scripts/`、`tools/`、`config/`、候选自己写出来的任何文件)一律拒绝;
- 仓库之外的模块(标准库、站点包)照旧放行——候选需要 `numpy`、`pandas`。

这些用例全部是**动态探针**:真的写文件、真的 import、真的看结果。
只靠静态推理一定漏掉写法(见 [[static-ban-rules-need-dynamic-probes]])。
"""
import json
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import research_sandbox as rs


# ---------- 一个假的"仓库":允许目录 + 别的目录 + 可写目录 ----------

@pytest.fixture()
def fake_repo(tmp_path, monkeypatch):
    """搭一个假仓库:模板包在 `strategies/`,框架代码在 `scripts/`,候选目录可写。"""
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "strategies" / "template.py").write_text(
        "class Base:\n    pass\n", encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "backtest_lib.py").write_text(
        "import sys\nVALUE = 1\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "helper.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "pending_strategies").mkdir()
    # 假仓库里的 `strategies` 会和**真仓库**的 `strategies` 包重名:
    # 真包一旦已经在 sys.modules 里,`import strategies.template` 就直接命中它,
    # 根本不会去假仓库里找——用例会因"别的测试先跑过"而莫名其妙地红。
    # 这里把同名的顶层模块清掉(monkeypatch 会在用例结束后还原)。
    for name in [n for n in list(sys.modules) if n == "strategies" or n.startswith("strategies.")
                 or n == "scripts" or n.startswith("scripts.") or n == "tools" or n.startswith("tools.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)          # 候选运行时的工作目录就是仓库根(bootstrap 会 chdir)
    return tmp_path


def guard_for(root, **kwargs):
    # 假仓库是临时目录,没有 git 提交基准 -> 显式关掉"与提交核对"这条
    # (那条规则由 tests/test_trusted_dir_git_baseline.py 用真 git 夹具专门盯)。
    kwargs.setdefault("verify_trusted_against_git", False)
    return rs.RepoImportGuard(root, snapshot=rs.snapshot_allowed_files(root), **kwargs)


def run_candidate(root, source, allowed_dirs=rs.ALLOWED_REPO_IMPORT_DIRS):
    """在受限内建下执行一段候选代码,返回它抛出的异常(或 None)。"""
    guard = guard_for(root, allowed_dirs=allowed_dirs)
    builtins = rs.safe_builtins(repo_guard=guard)
    namespace = {"__builtins__": builtins, "__name__": "pending_strategy"}
    try:
        exec(compile(textwrap.dedent(source), "<candidate>", "exec"), namespace)
    except Exception as exc:                                    # noqa: BLE001 - 探针要看的就是异常
        return exc
    return None


# ---------- 拦截:候选写出来的辅助模块 ----------

def test_a_helper_module_the_candidate_wrote_itself_cannot_be_imported(fake_repo):
    """核心逃逸:候选写出辅助模块再 import 它。"""
    source = """
        import numpy as np
        np.savetxt("pending_strategies/evil.py", np.array(["import os", "print(os.getcwd())"]), fmt="%s")
        import pending_strategies.evil
    """
    exc = run_candidate(fake_repo, source)

    assert exc is not None, "辅助模块被导入成功 = 逃逸成立"
    assert isinstance(exc, ImportError)
    assert "禁止导入" in str(exc)


def test_a_helper_module_written_into_the_allowed_directory_cannot_be_imported(fake_repo):
    """宿主机上整个仓库都可写,所以"允许目录"里新写出来的文件同样必须被拒。"""
    source = """
        import numpy as np
        np.savetxt("strategies/evil.py", np.array(["import os"]), fmt="%s")
        from strategies.evil import nothing
    """
    exc = run_candidate(fake_repo, source)

    assert exc is not None, "允许目录里新写出来的辅助模块被导入成功 = 逃逸成立"
    assert isinstance(exc, ImportError)
    assert "运行之前并不存在" in str(exc)


def test_a_repository_module_outside_the_allowed_directory_cannot_be_imported(fake_repo):
    """仓库里与模板无关的模块(框架、回测、工具)一律不许被候选 import。"""
    for name in ("scripts.backtest_lib", "tools.helper"):
        exc = run_candidate(fake_repo, f"import {name}")
        assert exc is not None, f"{name} 被导入成功 = 逃逸成立"
        assert isinstance(exc, ImportError)
        assert "禁止导入" in str(exc)


def test_a_helper_module_written_at_the_repository_root_cannot_be_imported(fake_repo):
    """写到仓库根目录下的辅助模块同样要被拒。"""
    source = """
        import numpy as np
        np.savetxt("evil.py", np.array(["import os"]), fmt="%s")
        import evil
    """
    exc = run_candidate(fake_repo, source)

    assert exc is not None
    assert isinstance(exc, ImportError)


def test_a_module_outside_the_repository_is_still_importable(fake_repo):
    """放行方向:仓库之外的模块(标准库、站点包)必须照旧能 import。"""
    exc = run_candidate(fake_repo, "import json\nimport numpy\n")
    assert exc is None, f"标准库/站点包被误伤: {exc}"


def test_the_legitimate_template_import_still_works(fake_repo):
    """放行方向:候选本来就要 `from strategies.<模板> import <基类>`——不能被误伤。"""
    exc = run_candidate(fake_repo, "from strategies.template import Base\n")
    assert exc is None, f"合法的模板导入被误伤: {exc}"


def test_the_template_package_itself_can_still_be_imported(fake_repo):
    """放行方向:导入包本身(`import strategies`)也要能用。"""
    exc = run_candidate(fake_repo, "import strategies\n")
    assert exc is None, f"合法的模板包导入被误伤: {exc}"


# ---------- "运行之前就存在"这条判定 ----------

def test_a_file_that_existed_before_the_candidate_started_is_importable(fake_repo):
    """放行方向:允许目录里**早就存在**的文件(模板)照旧可导入。"""
    exc = run_candidate(fake_repo, "from strategies.template import Base\n")
    assert exc is None


def test_a_file_modified_during_the_run_cannot_be_imported(fake_repo):
    """候选把既有模板文件改写了再导入:名字还在清单里,只有 mtime 变了才认得出来。"""
    source = """
        import numpy as np
        np.savetxt("strategies/template.py", np.array(["x = 1"]), fmt="%s")
        import strategies.template
    """
    exc = run_candidate(fake_repo, source)

    assert exc is not None, "被改写过的文件仍被导入 = 逃逸成立"
    assert isinstance(exc, ImportError)
    assert "被改动过" in str(exc)


def test_the_snapshot_is_taken_before_the_candidate_runs(fake_repo):
    """快照必须在候选开始运行**之前**拍:运行期间写出的文件一律不在快照里。"""
    snapshot = rs.snapshot_allowed_files(fake_repo)
    assert str((fake_repo / "strategies" / "template.py").resolve()) in snapshot
    (fake_repo / "strategies" / "later.py").write_text("x = 1\n", encoding="utf-8")
    assert str((fake_repo / "strategies" / "later.py").resolve()) not in snapshot


# ---------- 名字名单那一层仍然有效 ----------

def test_the_name_blocklist_still_blocks_the_usual_suspects(fake_repo):
    """回归:按名字那一层没有被路径规则顶掉。"""
    for name in ("os", "sys", "subprocess", "importlib"):
        exc = run_candidate(fake_repo, f"import {name}")
        assert exc is not None, f"{name} 竟然能 import"
        assert isinstance(exc, ImportError)
        assert "禁止导入" in str(exc)


# ---------- 守卫自身的行为 ----------

def test_the_guard_does_not_execute_the_module_it_inspects(fake_repo):
    """探测动作本身不能变成执行入口:解析模块位置**不得**执行它或它的父包。"""
    (fake_repo / "strategies" / "sideeffect.py").write_text(
        "raise RuntimeError('这一行不该被执行')\n", encoding="utf-8")
    guard = guard_for(fake_repo)

    reason = guard.check("strategies.sideeffect")

    assert reason is not None or True        # 不抛异常即达标
    assert "RuntimeError" not in str(reason or "")


def test_the_guard_reports_a_clear_reason_in_chinese(fake_repo):
    """拒绝理由要能给人看懂:运维看到的是"为什么不让导入"。"""
    guard = guard_for(fake_repo)

    reason = guard.check("scripts.backtest_lib")

    assert reason is not None
    assert "禁止导入" in reason
    assert "strategies" in reason            # 顺带告诉人允许的是哪个目录
