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


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """一个假仓库:候选跑在它里面,`bootstrap.ROOT` 指向它。"""
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(api.bootstrap, "ROOT", root)
    monkeypatch.chdir(root)
    return root


def run_candidate(tmp_path, monkeypatch, source, cls_name="S"):
    """把一段候选源码走生产入口 `import_candidate` 跑一遍,返回它的类。"""
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    path = tmp_path / "candidate.py"
    path.write_text(source, encoding="utf-8")
    return api.import_candidate(path, cls_name, api.rs.file_sha256(path))


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
    except Exception:
        LEAK["helper"] = False
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
            LEAK["got_framework"] = hasattr(fw, "subprocess")
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
    """
    source = build(BASE, PROBE_TRACEBACK, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["traceback"] is False, "候选从异常链上拿到了真实内建"


def test_a_helper_module_written_inside_the_repository_cannot_be_imported(tmp_path, monkeypatch, repo):
    """逃逸一(仓库内):候选写出的辅助模块在生产入口上同样导不进来。

    文件写在仓库根目录下——它在仓库内、但不在允许目录 `strategies/` 里,
    按路径规则直接拒绝(这条与时间无关,判定是确定的)。
    """
    source = build(BASE, PROBE_HELPER, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["helper"] is False, "候选自己写的辅助模块被导入成功了"


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
    """**已知敞口**:第三方模块的模块字典里就是真实内建。

    这条用例**断言敞口确实存在**,不是断言它被封住了——
    把它写成"应当被封住"就会变成一句空话,让人以为这条路也堵上了。
    真正的边界是人工审批 + 容器隔离,见 ADR 的残余风险一节。
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
        TAIL,
    )

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS["numpy"] is True, (
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


@pytest.mark.parametrize("name", ["os", "sys", "subprocess", "importlib", "unittest"])
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
    """
    source = build(BASE, PROBE_LOADER, PROBE_TRACEBACK, PROBE_HELPER, TAIL)

    cls = run_candidate(tmp_path, monkeypatch, source)

    assert cls.PASS == {"loader": False, "traceback": False, "helper": False}, (
        f"有逃逸漏了:{cls.PASS}"
    )
