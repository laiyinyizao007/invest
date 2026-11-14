"""候选逃逸的**生产路径**探针(Issue #26 第 21 轮实测的续)。

前面 `tests/test_helper_module_escape.py` 是在**测试自己的执行环境**里复现逃逸;
这一份走**生产入口** `scripts/research_api.py::import_candidate`——也就是子进程真正用的那条路,
断言候选在那里也拿不到真实内建。

背景(第 21 轮动态实测,不是静态推理出来的):
- 逃逸一(辅助模块):候选写一个 .py 再 import 它 → import 机制用**真实内建**执行它;
- 逃逸二(模块 loader):候选模块的 `__loader__.exec_module(m)` 能用真实内建把源码再跑一遍;
- 逃逸三(异常链):`e.__traceback__.tb_frame.f_builtins` 能拿到**我们自己的帧**的真实内建;
- 逃逸四(from-import):`from <包> import <子模块>` 的子模块 import 在**导入机制内部**完成,
  不经过守卫;命名空间包(`scripts/` 这种没有 `__init__.py` 的目录)origin 为 `None`,按"看 origin"的规则会被放行。

四条路都已封堵。本文件用生产入口复验,并把"真的拿到真实内建"这件事显式测出来,
而不是靠"没报错"推断——静态推理一定漏,见 [[static-ban-rules-need-dynamic-probes]]。

**断言"被拒绝了"是不够的**:还必须确认是**哪一条规则**拒绝的——
本文件的一条用例曾经在守卫失灵时依然全绿(它压根没走到自己要走的那条路上),详见 ADR 第 22 轮补记。

**已知仍然敞着的一条**(不在本文件的断言范围内,别把它当成已修):
任何第三方模块的模块字典里就是真实内建。
进程内执行不受信代码无法做到安全;真正的边界是**人工审批 + 容器隔离**(见 ADR 残余风险)。
"""
import sys
import textwrap

import pytest

import research_api as api


def build(*parts):
    """把几段代码拼成一份候选源码(每段各自去掉缩进,免得拼出来的缩进不一致)。"""
    return "\n".join(textwrap.dedent(part) for part in parts)


def purge_modules(monkeypatch, *prefixes):
    """把同名顶层模块从 `sys.modules` 里清掉(monkeypatch 会在用例结束后还原)。

    不清就会踩一个很难查的坑:假仓库里的 `strategies` 会和**真仓库**的 `strategies`
    重名——真包一旦已经在 `sys.modules` 里,`from strategies import evil` 就直接命中它,
    根本不去假仓库找。用例会因"别的测试先跑过"而莫名其妙地绿(测的不是我们以为的那条路)。
    """
    for name in [n for n in list(sys.modules)
                 if any(n == prefix or n.startswith(prefix + ".") for prefix in prefixes)]:
        monkeypatch.delitem(sys.modules, name, raising=False)


def git_commit_all(root):
    """把假仓库变成一个**真的 git 仓库**(并提交一次)。

    第 25 轮 codex 的 Critical 1:受信目录的完整性基准是**仓库提交里的那份**
    (快照是每次导入重拍的,挡不住"上一轮写、这一轮导")。生产入口按这条核对,
    所以假仓库也必须像个真仓库,否则这条规则在测试里永远是"取不到基准"的另一条路径。
    """
    import subprocess

    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "sandbox-test@example.invalid")
    run("config", "user.name", "sandbox-test")
    run("add", "-A")
    run("commit", "-q", "-m", "init")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个假仓库:候选跑在它里面,`bootstrap.ROOT` 指向它。"""
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    git_commit_all(root)
    monkeypatch.setattr(api.bootstrap, "ROOT", root)
    monkeypatch.chdir(root)
    return root


def run_candidate(tmp_path, monkeypatch, source, cls_name="S"):
    """把一段候选源码走生产入口 `import_candidate` 跑一遍,返回它的类。"""
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    path = tmp_path / "candidate.py"
    path.write_text(source, encoding="utf-8")
    return api.import_candidate(path, cls_name, api.rs.file_sha256(path))


def run_probe_at_runtime_layer(source):
    """只在**运行期**那一层跑探针(绕开静态筛查),返回探针的执行命名空间。

    为什么需要这个入口(第 24 轮):
    静态层现在会**直接拒掉**"顺着异常链走栈"这类源码(走栈属性一律不得引用),
    所以再用生产入口跑这类探针,测到的其实是**静态层**,运行期坏了也看不出来——
    这正是第 22 轮踩过的坑(规则失灵时用例照样绿)。
    于是分成两处测:
    - 这里:运行期那一层到底怎么样(允许源码长什么样,只测行为);
    - 生产入口那条用例:静态层把这类源码**整个拒掉**。
    """
    safe = api.rs.safe_builtins(())
    namespace = {"__name__": "probe", "__builtins__": safe}
    exec(compile(source, "probe.py", "exec"), namespace)      # noqa: S102 —— 这就是要测的那条路
    return namespace


# 候选自己执行的探针代码。三段都写成"尝试 → 记录成功与否",这样源码本身不会抛异常,
# 生产入口能正常跑完,断言才落在"到底漏没漏"这件事上。
#
# 注意写法上的几处约束(都是被测对象本身的要求,不是绕过):
# - 静态筛查按**字符串字面量**拦 "setattr"/"__builtins__"/"import os" 之类的钥匙,
#   所以这里用拼接写出来——候选当然也能这么写,静态层拦不住"拼出来的名字";
# - 属性名 "__dict__" 被静态层禁止,所以读模块字典改走 `__getattribute__`。
BASE = """
    LEAK = {}
"""

PROBE_LOADER = """
    try:
        import types
        m = types.ModuleType("pending_strategy")
        __loader__.exec_module(m)
        LEAK["loader"] = hasattr(m, "S")
    except Exception:
        LEAK["loader"] = False
"""

# 顺着异常链(含 __context__)找真实内建:
# 我们的受限导入器如果在被拒绝时**在 except 块里**抛新异常,原始的 ImportError
# 会挂在 __context__ 上,它的帧里有 importlib 的真实内建。
# 所以这里连 __context__ 一起走一遍。
PROBE_TRACEBACK = """
    try:
        import strategies.no_such_module
    except Exception as exc:
        LEAK["traceback"] = False
        cur = exc
        while cur is not None:
            tb = cur.__traceback__
            while tb is not None:
                if ("set" + "attr") in tb.tb_frame.f_builtins:
                    LEAK["traceback"] = True
                tb = tb.tb_next
            cur = cur.__context__
"""

PROBE_HELPER = """
    try:
        import numpy as np
        np.savetxt("evil_helper.py", np.array(["import" + " os"]), fmt="%s")
        import evil_helper
        LEAK["helper"] = True
        LEAK["helper_reason"] = "imported"
    except Exception as exc:
        LEAK["helper"] = False
        # 把拒绝理由也带出来:断言"被拒绝了"不够,还得确认**是哪一条规则**拒的
        # (第 22 轮踩过的坑:规则失灵时用例照样绿)。
        LEAK["helper_reason"] = str(exc)
"""

# 候选必须继承 CtaTemplate(生产入口的第三层校验),所以每段探针后面都接这一段。
TAIL = """
    from vnpy_ctastrategy import CtaTemplate
    class S(CtaTemplate):
        PASS = LEAK
"""


def test_the_module_loader_is_not_exposed_to_the_candidate(tmp_path, monkeypatch, repo):
    """逃逸二:候选模块不再带真正的 loader,`__loader__.exec_module` 这条路走不通。"""
    source = build(BASE, PROBE_LOADER, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["loader"] is False, "候选拿到了能执行代码的 loader"


def test_the_candidate_cannot_reach_a_framework_module_through_a_from_import(tmp_path, monkeypatch):
    """逃逸四(第 22 轮外部复核时才发现):`from <包> import <子模块>` 绕过路径规则。

    `scripts/` 这种目录没有 `__init__.py`,是**命名空间包**:它的 origin 是 None,
    按"看 origin"的规则会被放行;而子模块的 import 是在**导入机制内部**完成的,
    根本不经过我们的守卫。结果:候选 `from scripts import research_api` 就拿到了
    一个带 `subprocess` 的框架模块——四层防线全绿,等于任意命令执行。

    这条用例用**真实仓库根**(`bootstrap.ROOT` 不改),因为要绕过的正是真实仓库里的
    `scripts/` 包;用假仓库的话,"scripts" 会解析到真实仓库,与守卫的仓库根对不上。
    """
    purge_modules(monkeypatch, "scripts")
    source = build(
        BASE,
        """
        try:
            from scripts import research_api as fw
            LEAK["got_framework"] = hasattr(fw, "sub" + "process")
        except Exception:
            LEAK["got_framework"] = False
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["got_framework"] is False, "候选通过 from-import 拿到了框架模块"


def test_the_candidate_cannot_run_its_own_helper_module_through_a_from_import(tmp_path, monkeypatch, repo):
    """逃逸四的另一半:`from <命名空间包> import <自己写的辅助模块>` 整条路径规则作废。

    这里用一个**自造的命名空间目录**(`helperpkg/`,没有 `__init__.py`),
    候选把辅助模块写进去再 `from helperpkg import evil`:
    包位置落在仓库内、但不在允许目录 `strategies/` 里 -> 必须被拒。
    """
    pkg = repo / "helperpkg"
    pkg.mkdir()
    monkeypatch.syspath_prepend(str(repo))
    purge_modules(monkeypatch, "helperpkg", "strategies")
    target = str(pkg / "evil.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array(["MARKER = 'EXECUTED'"]), fmt="%s")
            from helperpkg import evil
            LEAK["helper_executed"] = evil.MARKER
        except Exception:
            LEAK["helper_executed"] = "denied"
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper_executed"] == "denied", "候选通过 from-import 跑起了自己的辅助模块"


def test_the_candidate_cannot_reach_a_newly_written_module_in_the_allowed_directory(tmp_path, monkeypatch, repo):
    """允许目录里的 from-import 同样要过"运行之前就存在"这一关。

    `from strategies import evil`(evil.py 是候选刚写的)——包位置是合法的 `strategies/`,
    但子模块本身不在快照里 -> 必须被拒。

    注意这里**必须把假仓库插到 sys.path 最前面**:否则 `strategies` 解析到的是**真仓库**
    那个包(真仓库根在 pytest 的 pythonpath 里),候选写出来的 evil.py 在真仓库里根本不存在,
    于是 `from strategies import evil` 因为"找不到模块"而失败——用例照样绿,但绿得毫无意义,
    去掉逐项裁决也照样绿(守不住)。
    """
    monkeypatch.syspath_prepend(str(repo))
    purge_modules(monkeypatch, "strategies")
    target = str(repo / "strategies" / "evil.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array(["MARKER = 'EXECUTED'"]), fmt="%s")
            from strategies import evil
            LEAK["helper_executed"] = evil.MARKER
        except Exception:
            LEAK["helper_executed"] = "denied"
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper_executed"] == "denied", "候选导入了自己刚写出来的模块"


def test_the_candidate_cannot_use_a_wildcard_from_import_to_run_its_own_helper(tmp_path, monkeypatch, repo):
    """通配符形式 `from <包> import *`:逐个子模块的裁决管不到它,只能靠"包位置"那条。

    `import *` 会把包位置下的**所有**子模块都导入一遍——候选写在命名空间目录里的
    辅助模块照样会被执行。所以"包位置不在允许目录里 -> 拒绝"这条必须独立存在,
    否则逐项裁决会被通配符绕过。
    """
    pkg = repo / "helperpkg"
    pkg.mkdir()
    monkeypatch.syspath_prepend(str(repo))
    purge_modules(monkeypatch, "helperpkg")
    target = str(pkg / "evil.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array(["MARKER = 'EXECUTED'"]), fmt="%s")
            from helperpkg import *
            LEAK["helper_executed"] = "evil" in dir()
        except Exception:
            LEAK["helper_executed"] = "denied"
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper_executed"] == "denied", "候选用通配符 from-import 跑起了自己的辅助模块"


def test_the_candidate_cannot_run_a_helper_module_through_a_wildcard_import(tmp_path, monkeypatch, repo):
    """通配符 `from <包> import *`:逐个子模块的裁决对它**无能为力**。

    导入机制按包的 `__all__`(有的话)自己 import 那些子模块,整个过程**不经过守卫**。
    于是只要包里有 `__all__`,候选把一个同名模块写进这个包,通配符导入就会把它
    **在导入机制里执行**——拿到真实内建,而逐项裁决对 `"*"` 一无所知。

    仓库里的 `strategies/__init__.py` 目前是空的(没有 `__all__`),所以这条路是**潜伏**的:
    等哪天有人给包加了 `__all__`,这条洞就会自己打开。这里用一个**自造的包**把它逼出来,
    免得"等出事再补"。

    探针里辅助模块只有一行 `open(...).write("x")`:`open` 在候选自己的帧里是被收走的,
    只有**在导入机制里执行**才拿得到真内建——所以标记文件被写出来就等于"确实被执行了"。
    """
    monkeypatch.syspath_prepend(str(repo))
    purge_modules(monkeypatch, "strategies")
    # 给包装上 `__all__`:导入机制据此自己去 import 子模块。
    (repo / "strategies" / "__init__.py").write_text('__all__ = ["evil"]\n', encoding="utf-8")
    # 受信目录必须与**仓库提交**一致(第 25 轮 codex 的 Critical 1)——
    # 改完模板要提交,否则那条规则先拒(候选改写了受信模块时走的也是同一条路)。
    git_commit_all(repo)
    marker = tmp_path / "executed.txt"
    helper = 'open(r"%s","w").write("x")' % str(marker).replace("\\", "/")
    target = str(repo / "strategies" / "evil.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array([{helper!r}]), fmt="%s")
            from strategies import *
            LEAK["helper_executed"] = "evil" in dir()
        except Exception as exc:
            LEAK["helper_executed"] = "denied"
            LEAK["reason"] = repr(exc)
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper_executed"] == "denied", "候选用通配符 from-import 跑起了自己的辅助模块"
    assert not marker.exists(), "辅助模块被执行了(它用真实内建写出了标记文件)"
    # 还必须确认是**哪一条规则**拒的:只断言"被拒了"的话,换个理由拒也照样绿。
    assert "包内" in cls.PASS.get("reason", ""), (
        f"拒绝理由不对,看不出是'通配符扫目录'那条规则拒的:{cls.PASS.get('reason')}"
    )


def test_the_candidate_cannot_run_a_helper_module_through_a_wildcard_import_outside_the_repository(
        tmp_path, monkeypatch, repo):
    """仓库**之外**也一样:站点包在宿主机上通常可写,通配符导入会由导入机制去 import 它。

    同一个洞的另一个方向:包是**早就存在**的(所以"运行之前就存在"这条对它无效),
    但候选往包里写了一个**新模块**,而包自己的 `__all__` 正好列着那个名字——
    `from 包 import *` 由导入机制去 import 它,整条路不经过守卫。

    这里用一个自造的"站点包"(在仓库之外)把它逼出来。
    """
    lib = tmp_path / "fakelib"
    lib.mkdir()
    (lib / "__init__.py").write_text('__all__ = ["evil"]\n', encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    purge_modules(monkeypatch, "fakelib")
    marker = tmp_path / "executed.txt"
    helper = 'open(r"%s","w").write("x")' % str(marker).replace("\\", "/")
    target = str(lib / "evil.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array([{helper!r}]), fmt="%s")
            from fakelib import *
            LEAK["helper_executed"] = "evil" in dir()
        except Exception as exc:
            LEAK["helper_executed"] = "denied"
            LEAK["reason"] = repr(exc)
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper_executed"] == "denied", "候选用通配符 from-import 跑起了自己在仓库外写的辅助模块"
    assert not marker.exists(), "辅助模块被执行了(它用真实内建写出了标记文件)"


def test_the_package_position_rule_denies_a_name_that_is_not_a_module(tmp_path, monkeypatch, repo):
    """包位置裁决这条规则的**专属**探针(逐项裁决看不见它)。

    逐项裁决只管**模块**:候选 `from <包> import <某个不是模块的名字>` 时,
    那一项在文件系统上查不到来源,逐项裁决无从拒绝——只有"包位置"那条规则
    (包落在仓库内、但不在允许目录里)能把整个包挡在门外。

    所以这条用例断言的不只是"被拒了",而是**拒绝理由里含"包位置"**:
    否则把它换成别的理由拒,用例照样绿(守住的东西就变了)。
    """
    pkg = repo / "framepkg"          # 命名空间包(没有 __init__.py`)
    pkg.mkdir()
    monkeypatch.syspath_prepend(str(repo))
    purge_modules(monkeypatch, "framepkg")
    source = build(
        BASE,
        """
        try:
            from framepkg import NOT_A_MODULE_NAME
            LEAK["outcome"] = "allowed"
        except Exception as exc:
            LEAK["outcome"] = "denied"
            LEAK["reason"] = repr(exc)
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["outcome"] == "denied", "候选从仓库内非允许目录的包里导入出了东西"
    assert "包位置" in cls.PASS.get("reason", ""), (
        f"拒绝理由不对,不是'包位置'那条规则拒的:{cls.PASS.get('reason')}"
    )


def test_the_traceback_of_our_own_frames_does_not_expose_the_real_builtins(tmp_path, monkeypatch, repo):
    """逃逸三:候选触发一次运行期被拒绝的 import,再顺着异常链找真实内建——找不到。

    这条特意挑"我们自己抛异常"的场景:如果受限导入器是用模块级函数实现的,
    它的帧的 f_builtins 就是**真实内建**;必须给它单独造一个受限的帧环境。

    第 24 轮补一条更狠的:受限导入器那一帧的 **f_globals** 里原先放着 `real_import`
    (第 21 轮只换了 f_builtins,没换 f_globals)。候选一句
    `tb_frame.f_globals["real_import"]` 就把真导入器拿走,接着 `real_import("os")`
    就是任意命令执行(实测标记文件被写出来)。
    现在这一帧的 f_globals 是**干净的新字典**(只有受限内建),真导入器在**闭包单元**里,
    闭包单元不在 f_locals 里,只有 `__closure__` 能摸到——而那个名字静态层已禁。
    """
    source = build(BASE, PROBE_TRACEBACK)

    namespace = run_probe_at_runtime_layer(source)

    assert namespace["LEAK"]["traceback"] is False, "候选从异常链上拿到了真实内建"


def test_the_guard_frame_does_not_hand_out_the_real_importer():
    """第 24 轮的 Critical:候选不能从守卫自己那一帧的 f_globals 里摸到真导入器。

    探针做的事就是当年击穿沙盒的那件事:顺着 ImportError 的异常链走遍每一帧,
    看有没有哪一帧的 f_globals 里放着 `real_import`。

    第 21 轮只把那一帧的 **f_builtins** 换成受限字典,f_globals 里还放着真导入器,
    候选一句 `tb_frame.f_globals["real_import"]` 就拿到任意命令执行(实测标记文件被写出)。
    现在那一帧的 f_globals 是**干净的新字典**(只有受限内建),真导入器在**闭包单元**里。
    """
    source = textwrap.dedent("""
        LEAK = {"found_globals": False}
        try:
            import os
        except Exception as exc:
            tb = exc.__traceback__
            while tb is not None:
                if "real_import" in tb.tb_frame.f_globals:
                    LEAK["found_globals"] = True
                tb = tb.tb_next
        else:
            LEAK["found_globals"] = "os-imported"
    """)

    namespace = run_probe_at_runtime_layer(source)

    assert namespace["LEAK"]["found_globals"] is False, (
        "守卫那一帧的 f_globals 里还放着真导入器——候选顺着异常链就能拿到任意命令执行")


def test_the_runtime_layer_still_exposes_the_closure_cells_through_f_locals():
    """**如实记账的残余**:运行期那一层挡不住"能读 f_locals 的候选"。

    第 24 轮实测:真导入器搬进了**闭包单元**,f_globals 里没有了——
    但闭包单元在 Python 3.13 的 `f_locals` 里**看得见**(实测:`real_import` 出现在
    守卫那一帧的 f_locals 键里)。

    为什么这条**改不了**:凡是这一帧能拿到的对象,候选拿到这一帧就能拿到它。
    Python 层面没有"藏起来但仍能使用"的存放处(闭包单元、默认参数、代码常量、
    放进容器——全都从这一帧可达)。所以运行期这一层**只能**保证 f_globals 干净。

    挡住它的是另外两层,各有各的用例钉着:
    - 静态层:走栈属性(`__traceback__`/`tb_frame`/`f_globals`/`f_locals`/`__closure__`)
      一个都不许引用(见 `test_the_production_path_refuses_to_walk_the_stack`);
    - 人工审批 + 容器隔离(见 ADR 残余风险一节)。
    """
    source = textwrap.dedent("""
        LEAK = {"found_locals": False}
        try:
            import os
        except Exception as exc:
            tb = exc.__traceback__
            while tb is not None:
                if "real_import" in tb.tb_frame.f_locals:
                    LEAK["found_locals"] = True
                tb = tb.tb_next
    """)

    namespace = run_probe_at_runtime_layer(source)

    assert namespace["LEAK"]["found_locals"] is True, (
        "守卫那一帧的 f_locals 里已经看不到真导入器了——那说明实现变了,"
        "本用例与 ADR 的残余风险登记都要一并更新(别只是把断言删掉)")


def test_the_production_path_refuses_to_walk_the_stack(tmp_path, monkeypatch, repo):
    """走栈这条路在**静态层**就被拒:属性名一个都不许出现。

    这一条与上面两条是分工:上面两条测"运行期那一层我们自己那一帧干不干净",
    这一条测"候选根本写不出这种源码"。
    断言的是**哪一条规则**拒的(走栈属性名),不是笼统的"被拒了"。
    """
    source = build(BASE, PROBE_TRACEBACK, TAIL)

    with pytest.raises(api.JobError) as exc:
        run_candidate(tmp_path, monkeypatch, source)

    message = str(exc.value)
    assert "静态筛查未通过" in message, message
    assert "禁止访问 __traceback__" in message or "禁止引用 __traceback__" in message, (
        f"拒绝理由里没提走栈属性,规则可能不是那一条:{message}")


def test_a_helper_module_written_inside_the_repository_cannot_be_imported(tmp_path, monkeypatch, repo):
    """逃逸一(仓库内):候选写出的辅助模块在生产入口上同样导不进来。

    文件写在仓库根目录下——它在仓库内、但不在允许目录 `strategies/` 里,
    按路径规则直接拒绝(这条与时间无关,判定是确定的)。

    **必须把假仓库插到 sys.path 最前面**(第 24 轮 codex 指出的假绿):
    否则 `import evil_helper` 根本找不到模块,失败原因是 ModuleNotFoundError,
    用例照样绿——但绿得毫无意义:把逐项裁决整个去掉,它照样绿。
    插进去之后,模块**找得到**,被拒的必须是守卫规则本身。
    """
    monkeypatch.syspath_prepend(str(repo))
    source = build(BASE, PROBE_HELPER, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper"] is False, "候选自己写的辅助模块被导入成功了"
    assert "禁止导入" in cls.PASS["helper_reason"], (
        f"辅助模块不是被守卫规则拒的(可能是根本没找到模块):{cls.PASS['helper_reason']}")


def test_a_helper_module_written_outside_the_repository_cannot_be_imported(tmp_path, monkeypatch, repo):
    """逃逸一(仓库外):候选往站点包那样可写的地方写模块,也必须导不进来。

    仓库内的路径规则管不到仓库外的文件(比如站点包在宿主机上通常可写),
    只能靠"运行之前就存在"这条时间规则拦住。
    """
    monkeypatch.syspath_prepend(str(tmp_path))
    # 写到**仓库之外**的绝对路径(正斜杠:Windows 上 Python 一样认)。
    # 写到仓库里的那种由另一个用例负责(走的是路径规则,不是时间规则)。
    target = str(tmp_path / "outside_helper.py").replace("\\", "/")
    source = build(
        BASE,
        f"""
        try:
            import numpy as np
            np.savetxt("{target}", np.array(["import" + " os"]), fmt="%s")
            import outside_helper
            LEAK["helper"] = True
        except Exception:
            LEAK["helper"] = False
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper"] is False, "候选在仓库外写的辅助模块被导入成功了"


def test_the_real_builtins_are_reachable_from_a_third_party_module(tmp_path, monkeypatch, repo):
    """**已知敞口**:第三方模块的帧里就是真实内建。

    这条用例**断言敞口确实存在**,不是断言它被封住了——
    把它写成"应当被封住"就会变成一句空话,让人以为这条路也堵上了。
    真正的边界是人工审批 + 容器隔离,见 ADR 的残余风险一节。

    第 24 轮实测补了一脚:这个敞口**不是假设**——numpy 自己的 traceback 里就印着
    `numpy/lib/_npyio_impl.py` 第 454 行的 `open(os.fspath(file), "rb")`,
    也就是说 numpy 的帧的 f_globals 里就有 `os`。

    但这条敞口**在生产入口上够不着**:静态层把走栈属性(`__traceback__`/`tb_frame`/
    `f_globals`/`f_builtins`)一律拒掉(见 `test_the_production_path_refuses_to_walk_the_stack`),
    候选连这种源码都写不出来。写这条用例是为了**如实记账**:
    运行期这一层挡不住,挡它的是静态层 + 人工审批 + 容器隔离。
    """
    source = build(
        BASE,
        """
        try:
            import numpy as np
            np.genfromtxt("definitely_missing_file.csv")
        except Exception as exc:
            LEAK["numpy"] = False
            tb = exc.__traceback__
            while tb is not None:
                if ("set" + "attr") in tb.tb_frame.f_builtins:
                    LEAK["numpy"] = True
                tb = tb.tb_next
        """,
    )

    namespace = run_probe_at_runtime_layer(source)

    assert namespace["LEAK"]["numpy"] is True, (
        "第三方模块里已经拿不到真实内建了——那说明能力防火墙有进展,"
        "ADR 的残余风险一节需要同步更新(别只是把断言删掉)"
    )


def test_the_candidate_still_gets_the_legitimate_imports(tmp_path, monkeypatch, repo):
    """放行方向:合法的导入不能被这几道防线误伤。

    `numpy`/`json` 这类站点包必须照旧能 import——候选的信号逻辑要用它们。
    """
    source = build(
        BASE,
        """
        import json
        import numpy
        LEAK["ok"] = True
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["ok"] is True


@pytest.mark.parametrize("name", ["os", "sys", "subprocess", "importlib", "unittest",
                                   "runpy", "code", "pkgutil", "zipimport", "pty", "platform",
                                   "webbrowser"])
def test_the_name_blocklist_still_holds_on_the_production_path(tmp_path, monkeypatch, repo, name):
    """回归:名字那一层在生产入口上依然有效(路径规则没把它顶掉)。

    注意这里是**两道**独立的闸:静态层按源码里的 import 语句拦(大部分名字在这一层就没了),
    运行期那层按 import 机制实际要加载的模块名拦(静态层放过的名字在这一层被拒)。
    两道都拦下来才算数——只测其中一道,另一道被顶掉也看不出来。
    """
    source = build(
        f"""
        LEAK = {{"ok": True}}
        try:
            import {name}
        except Exception:
            LEAK = {{"ok": False}}
        """,
        TAIL,
    )

    try:
        cls = run_candidate(tmp_path, monkeypatch, source)
    except api.JobError as exc:
        assert "静态筛查未通过" in str(exc), f"{name} 被拒的理由不对:{exc}"
        return
    assert cls.PASS["ok"] is False, f"{name} 竟然能 import"


def test_the_candidate_cannot_reach_the_real_builtins_through_any_of_the_three_routes(tmp_path, monkeypatch, repo):
    """把三条路合在一起跑一遍:哪一条都不许漏。

    合在一处是有意的——分开写三个用例时,某个用例的失败可能只是"探针自己写错了",
    合起来断言"一个都没漏",失败时更容易看出是哪一条。

    这一条只测**运行期**那一层,所以把静态筛查**故意**放行(第 23 轮起就这么干):
    静态层本来就该拦下"顺着异常链走栈"这种源码,留着它就永远测不到运行期了
    (第 22 轮踩过的坑:规则失灵时用例照样绿)。
    """
    monkeypatch.setattr(api.rs, "screen_source", lambda *a, **k: [])
    monkeypatch.syspath_prepend(str(repo))
    source = build(BASE, PROBE_LOADER, PROBE_TRACEBACK, PROBE_HELPER, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["loader"] is False, "候选拿到了能执行代码的 loader"
    assert cls.PASS["traceback"] is False, "候选从异常链上拿到了真实内建"
    assert cls.PASS["helper"] is False, "候选自己写的辅助模块被导入成功了"
    assert "禁止导入" in cls.PASS["helper_reason"], (
        f"辅助模块不是被守卫规则拒的:{cls.PASS['helper_reason']}")


# ---------------------------------------------------------------------------
# 第 6 类逃逸(第 23 轮外部复核时才发现):**执行原语**。
#
# 前面五条逃逸全都在"候选调用 import"这条路上,而按路径的导入裁决(RepoImportGuard)
# 也只在这条路上生效。但 `runpy.run_path(文件)` / `code.InteractiveInterpreter.runsource(...)`
# / `zipimport.zipimporter(...).exec_module(...)` 这些**执行原语**根本不经过 `__import__`,
# 于是路径规则对它们**完全无效**。对照实验(同一个"运行期间写出来的模块"):
#
#   `import 它`          -> 路径规则拒绝:"该文件在候选开始运行之后才出现";
#   `runpy.run_path(它)` -> 直接执行,辅助模块在**真实内建**下跑起来(标记文件被写出)。
#
# 所以这一类不是"再加一条路径规则"能解决的,只能靠**名字**堵:把执行原语列进禁用名单。
EXECUTION_PRIMITIVES = ("runpy", "code", "pkgutil", "zipimport", "pty", "platform", "webbrowser")


@pytest.mark.parametrize("name", EXECUTION_PRIMITIVES)
def test_the_runtime_layer_refuses_the_execution_primitives(name):
    """运行期那一层必须拒掉执行原语。

    这一层要**单独**测:静态层会先按 `import` 语句把候选拦下来,那样这条用例
    永远走不到运行期,就成了"绿得毫无意义"(第 22 轮踩过的坑:规则失灵时用例照样绿)。
    """
    safe = api.rs.safe_builtins(())

    with pytest.raises(ImportError) as exc:
        safe["__import__"](name)

    assert name in str(exc.value)


def test_the_candidate_cannot_run_its_own_module_through_an_execution_primitive(
        tmp_path, monkeypatch, repo):
    """不变式:候选**不得**让"运行期间写出来的模块"被执行,走哪条路都不行。

    这一条把静态层**故意**放行(只留下运行期那一层):静态层本来就该拦下这种源码,
    留着它就永远测不到运行期了。

    探针里的辅助模块只做一件事——写一个标记文件。它**必须**在真实内建下才写得出来
    (候选自己那些帧里 `open` 是被收走的),所以标记文件一旦出现,就说明
    "辅助模块在真实内建下执行了"。
    """
    monkeypatch.setattr(api.rs, "screen_source", lambda *a, **k: [])
    source = build(
        BASE,
        """
        try:
            import numpy as np
            np.savetxt("evil_helper.py",
                       np.array(["open('leaked_marker.txt','w').write('x')"]), fmt="%s")
            import runpy
            runpy.run_path("evil_helper.py")
            LEAK["primitive"] = True
        except Exception:
            LEAK["primitive"] = False
        """,
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["primitive"] is False, "候选用执行原语跑起了自己写的模块"
    assert not (repo / "leaked_marker.txt").exists(), (
        "辅助模块在真实内建下执行了(标记文件被写出来)")


def test_the_runtime_layer_refuses_the_dangerous_submodule_paths():
    """第 24 轮:`numpy.ctypeslib` 这类"把执行能力递出来"的角落按**全名**禁掉。

    `numpy` 本身是候选正当需要的包,所以不能整包禁;
    但 `numpy.ctypeslib`(递出 `ctypes`)、`numpy.f2py`(**import 它就会跑 f2py 的主程序**)
    这几个角落必须单独禁。这一层要**单独**测:静态层会先按 import 语句拦掉候选源码,
    那样这条用例永远走不到运行期(第 22 轮踩过的坑)。
    """
    safe = api.rs.safe_builtins(())

    for name in ("numpy.ctypeslib", "numpy.f2py", "numpy.distutils"):
        with pytest.raises(ImportError) as exc:
            safe["__import__"](name)
        assert "按全名禁用" in str(exc.value), f"{name} 的拒绝理由不对:{exc.value}"


def test_the_module_view_refuses_a_module_that_hands_out_a_capability_object():
    """判据是"对象**从哪儿定义**",不是"名字像不像"——所以这一条是收敛的。

    第 23 轮的结论"名单式封堵不收敛"(`numpy.ctypeslib` 换个名字也能递出 ctypes),
    第 24 轮换了判据:凡是**定义在** ctypes/subprocess 里的对象(类、函数),
    一律不给。这里用一个自造的模块递出 `ctypes.CDLL`(**类**,不是模块)来验证。
    """
    import ctypes
    import types as pytypes

    fake = pytypes.ModuleType("fake_pkg")
    fake.CDLL = ctypes.CDLL                       # 只递出类,不递出 ctypes 模块
    view = api.rs._view_for(fake)

    with pytest.raises(AttributeError) as exc:
        view.CDLL

    assert "能力泄漏" in str(exc.value), f"拒绝理由不对:{exc.value}"


def test_the_candidate_cannot_reach_a_native_library_through_an_allowed_package(tmp_path, monkeypatch, repo):
    """第 24 轮:`numpy.ctypeslib` -> `ctypes` -> `CDLL` 这条路已经堵上了。

    第 23 轮实测它**还漏着**(所以当时如实登记成残余风险)。
    第 24 轮从两头堵:
    - 静态层:`numpy.ctypeslib` 按全名拒;
    - 运行期:按全名拒(路径规则)+ 模块视图按"对象来源"拒。

    真正剩下的边界不是这一条,而是"进程内执行不受信代码"本身:
    候选自己写的类、第三方模块的帧里仍然是真实内建(另有用例钉着),
    真正的边界是**人工审批 + 容器隔离**(见 ADR 残余风险)。
    """
    # 1) 静态层:整条 import 语句被拒。
    source = build(
        BASE,
        """
        try:
            import numpy.ctypeslib as C
            lib = C.ctypes.CDLL("kernel32")
            LEAK["native"] = lib.GetCurrentProcessId() > 0
        except Exception:
            LEAK["native"] = False
        """,
        TAIL,
    )

    with pytest.raises(api.JobError) as exc:
        run_candidate(tmp_path, monkeypatch, source)

    assert "静态筛查未通过" in str(exc.value), str(exc.value)

    # 2) 运行期:即使静态层被放行(只留运行期那一层),按全名的规则也把它拒了。
    monkeypatch.setattr(api.rs, "screen_source", lambda *a, **k: [])
    safe = api.rs.safe_builtins(())
    with pytest.raises(ImportError) as exc:
        safe["__import__"]("numpy.ctypeslib")
    assert "按全名禁用" in str(exc.value), str(exc.value)

    # 3) 模块视图:即便候选 import 的是 numpy(正当需求),numpy 的角落里那个
    #    递出 ctypes 的子模块也不给。
    import numpy

    view = api.rs._view_for(numpy)
    with pytest.raises(AttributeError) as exc:
        view.ctypeslib
    assert "规则:模块视图" in str(exc.value) or "能力泄漏" in str(exc.value), str(exc.value)
