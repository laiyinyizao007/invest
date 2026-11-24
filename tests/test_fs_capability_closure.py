"""候选文件读写能力收口(Issue #31):审计钩子 / numpy·pandas 移出默认白名单 / 账本防篡改 / 行情先取定。

动态探针,双向:放行正常写法 + 拦截攻击写法,并断言**是哪条规则**拒的(第 22 轮教训:规则失灵时用例照样绿)。
攻击用真 numpy/pandas 复现(`np.memmap` 写 pickle + `pd.read_pickle`、`np.savetxt`、`DataFrame.to_csv`……)。

诚实边界(别把这些用例读成"安全边界已建成"):审计钩子是纵深防御;同进程里候选能影响框架的问题由 Issue #29 闭合。
"""
import contextlib
import copy
import importlib.util
import json
import os
import pickle
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

# `bootstrap` 的唯一作用是"在 vnpy.trader.database 之前把时区设成 CST-8"。
# 研究侧 CI 不装 vnpy(见 .github/workflows/research-guard.yml),那里根本没有
# vnpy.trader.database 可管;按仓库既有惯例(与 tests/test_constitution_round11.py 同款)
# 只在交易栈在位时才导入它。
#
# 注意**不是**"import 失败就整份文件跳过":那会让这份核心防线在 CI 上一次都不跑
# (第 7 轮 codex 的 High 同族——闸门看上去配好了,实际没跑)。
# 真正需要交易栈的用例由 `@requires_trading_stack` 逐条显式标出。
if importlib.util.find_spec("vnpy") is not None:
    import bootstrap  # noqa: F401  与既有用例一致:先于 vnpy.trader.database
import numpy as np   # 必须在进入窗口前导入:pandas 首次导入会 ctypes.dlopen,窗口里会被拒
import pandas as pd
import research_api as api
import research_sandbox as rs
from risk import ledger_admin as admin
from risk import ledger_chain as chain
from risk import trial_ledger as tl

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# 需要交易栈的用例:逐条**显式**标记,而不是"import 失败就整份文件跳过"
# ---------------------------------------------------------------------------
# 这份文件是 Issue #31 的核心防线(候选能力收口)。它此前**在 CI 上一次都没跑过**:
# 文件头的 `import bootstrap` 会拖进 vnpy,而研究侧 CI 只装 numpy/pandas/mcp/pyyaml,
# 于是整份文件在收集阶段就 ModuleNotFoundError——**闸门看上去配好了,实际没跑**
# (第 7 轮 codex 的 High 同族)。现在把"需要交易栈的用例"逐条标记出来,
# 让**其余与交易栈无关的防线用例真的在 CI 上跑**。
#
# 这个标记是**环境驱动**的,不是"为了让 CI 绿而放宽":
#   - 本机(装了 vnpy)这些用例**照常跑**,一条不少;
#   - 跳过时 pytest 会打印原因,CI 日志里看得见;
#   - 用例本身断言"**是哪条规则**拒的",规则失灵时照样红(第 22 轮教训)。
# 新增用例若要用 vnpy,**必须**加这个标记——否则它在 CI 上会直接失败
# (而不是被静默跳过),这正是我们要的行为:漏标记会立刻暴露,不会悄悄退化。


def _has_trading_stack() -> bool:
    """交易栈(vnpy_ctastrategy)在不在?——决定 `@requires_trading_stack` 是否跳过。"""
    try:
        return importlib.util.find_spec("vnpy_ctastrategy") is not None
    except (ImportError, ValueError):   # pragma: no cover - 找不到就是没有
        return False


requires_trading_stack = pytest.mark.skipif(
    not _has_trading_stack(),
    reason="需要交易栈(vnpy_ctastrategy):研究侧 CI 不装交易栈,这类用例在本地全量环境跑",
)


@pytest.fixture(autouse=True)
def isolate_global_state():
    """保存/还原 sys.modules 与沙盒全局状态:本文件会造假仓库(`strategies` 重名)、动审计状态,
    不能靠文件顺序污染别的测试(上次整合出过 14 条顺序依赖失败)。"""
    modules_before = dict(sys.modules)
    path_before = list(sys.path)
    allowed_before = rs._ACTIVE_ALLOWED_ROOTS[0]
    yield
    rs._AUDIT.active = False
    rs._AUDIT.allowed = None
    rs._AUDIT.depth = 0
    rs._ACTIVE_ALLOWED_ROOTS[0] = allowed_before
    for name in list(sys.modules):
        if name not in modules_before:
            root = name.split(".")[0]
            origin = str(getattr(sys.modules[name], "__file__", "") or "")
            if root in ("pending_strategy", "strategies", "evil_helper") or "pytest-of" in origin:
                del sys.modules[name]
    for name, module in modules_before.items():
        if sys.modules.get(name) is not module:
            sys.modules[name] = module
    sys.path[:] = path_before


# ======================================================================
# A. 审计钩子:窗口内
# ======================================================================

@pytest.fixture()
def victim(tmp_path):
    """一个"不该被改"的文件(代表行情 CSV / 账本 / 批准记录)。"""
    path = tmp_path / "victim.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


def denied(rule):
    return pytest.raises(rs.AuditDenied, match=None) if rule is None else _expect_rule(rule)


@contextlib.contextmanager
def _expect_rule(rule):
    with pytest.raises(rs.AuditDenied) as info:
        yield
    assert info.value.rule == rule, f"被拒了,但不是预期的规则: {info.value.rule} != {rule}"


class Evil:
    """`__reduce__` 返回 (os.mkdir, (标记目录,)):反序列化即执行;标记目录出现 = 命令真的执行了。"""

    def __init__(self, marker):
        self.marker = marker

    def __reduce__(self):
        return (os.mkdir, (self.marker,))


def test_allowed_normal_candidate_behaviour_inside_the_window(victim):
    """放行方向:只读 numpy 计算、读文件、读 CSV、写临时目录、写空设备、exec/compile 都照常。"""
    with rs.candidate_window() as scratch:
        assert float(np.mean(np.array([1.0, 2.0, 3.0]))) == 2.0
        assert victim.read_text(encoding="utf-8").startswith("a,b")
        assert list(pd.read_csv(victim)["a"]) == [1]
        assert np.loadtxt(victim, delimiter=",", skiprows=1).tolist() == [1.0, 2.0]
        np.savetxt(scratch / "ok.txt", [1, 2])                    # 允许目录里可写
        (scratch / "ok2.txt").write_text("x", encoding="utf-8")
        open(os.devnull, "w").close()
        exec(compile("x = 1", "<s>", "exec"))                     # 不拦 exec/compile(pandas 惰性导入会用到)
        assert rs.window_denials() == ()
    assert not scratch.exists(), "窗口结束必须清掉临时目录"


@pytest.mark.parametrize("name,rule,attack", [
    ("np.memmap(w+)", "audit:open-write",
     lambda p, d: np.memmap(str(d / "m.bin"), dtype="u1", mode="w+", shape=(4,))),
    ("np.memmap(r+) 改既有文件", "audit:open-write",
     lambda p, d: np.memmap(str(p), dtype="u1", mode="r+")),
    ("np.savetxt", "audit:open-write", lambda p, d: np.savetxt(p, [1, 2])),
    ("DataFrame.to_csv", "audit:open-write", lambda p, d: pd.DataFrame({"a": [9]}).to_csv(p)),
    ("ndarray.tofile", "audit:open-write", lambda p, d: np.array([1, 2]).tofile(str(p))),
    ("open(w)", "audit:open-write", lambda p, d: open(p, "w")),
    ("open(a)", "audit:open-write", lambda p, d: open(p, "a")),
    ("open(x)", "audit:open-write", lambda p, d: open(d / "new.txt", "x")),
    ("open(r+)", "audit:open-write", lambda p, d: open(p, "r+")),
    ("os.open(O_WRONLY|O_CREAT)", "audit:open-write",
     lambda p, d: os.open(str(d / "o.txt"), os.O_WRONLY | os.O_CREAT)),
    ("os.open(O_RDWR)", "audit:open-write", lambda p, d: os.open(str(p), os.O_RDWR)),
    ("os.open(O_TRUNC)", "audit:open-write", lambda p, d: os.open(str(p), os.O_WRONLY | os.O_TRUNC)),
    ("os.remove", "audit:path-mutation", lambda p, d: os.remove(p)),
    ("os.rename", "audit:path-mutation", lambda p, d: os.rename(p, d / "moved.csv")),
    ("os.mkdir", "audit:path-mutation", lambda p, d: os.mkdir(d / "newdir")),
    ("shutil.copyfile 到外部", "audit:path-mutation",
     lambda p, d: __import__("shutil").copyfile(p, d / "copy.csv")),
    ("os.system", "audit:os.system", lambda p, d: os.system("echo hi")),
    ("subprocess.run", "audit:subprocess.Popen", lambda p, d: subprocess.run([sys.executable, "-c", "pass"])),
    ("socket", "audit:socket", lambda p, d: __import__("socket").socket()),
    ("ctypes", "audit:ctypes", lambda p, d: __import__("ctypes").CDLL("kernel32" if os.name == "nt" else "libc.so.6")),
])
def test_attack_is_denied_by_the_named_rule_and_leaves_the_file_untouched(name, rule, attack, victim, tmp_path):
    before = victim.read_bytes()
    out_dir = tmp_path / "outside"
    out_dir.mkdir()
    with rs.candidate_window():
        with _expect_rule(rule):
            attack(victim, out_dir)
    assert victim.read_bytes() == before, name
    assert list(out_dir.iterdir()) == [], f"{name}: 目标目录不该出现新文件"


def test_the_pickle_to_command_execution_chain_is_cut_at_find_class(tmp_path):
    """第 35 轮 codex 的攻击链:写 pickle 字节 -> pd.read_pickle -> __reduce__ -> 命令执行。

    对照(窗口外):同一份 pickle 被 read_pickle 读到,标记目录真的出现 = 攻击本身有效(探针不是空转);
    窗口内:同一调用在 `pickle.find_class` 这条规则上被拒,标记不出现。"""
    marker_control = tmp_path / "marker_control"
    marker_attack = tmp_path / "marker_attack"
    payload_control = tmp_path / "control.pkl"
    payload_attack = tmp_path / "attack.pkl"
    payload_control.write_bytes(pickle.dumps(Evil(str(marker_control))))
    payload_attack.write_bytes(pickle.dumps(Evil(str(marker_attack))))

    pd.read_pickle(payload_control)
    assert marker_control.is_dir(), "对照失败:窗口外攻击应当有效,否则本探针什么都没证明"

    with rs.candidate_window():
        with _expect_rule("audit:pickle.find_class"):
            pd.read_pickle(payload_attack)
        # numpy 把底层异常包成 UnpicklingError:要在异常链上找到审计钩子的拒绝(确认是这条规则拒的)
        with pytest.raises(pickle.UnpicklingError) as info:
            np.load(payload_attack, allow_pickle=True)
        chain_rules, exc = [], info.value
        while exc is not None:
            if isinstance(exc, rs.AuditDenied):
                chain_rules.append(exc.rule)
            exc = exc.__cause__ or exc.__context__
        assert chain_rules == ["audit:pickle.find_class"], chain_rules
    assert not marker_attack.exists()


def test_the_memmap_then_read_pickle_chain_cannot_even_write_the_payload(tmp_path):
    """上一条的前半段:候选连 pickle 字节都写不出去(open-write),链在第一环就断。"""
    payload = tmp_path / "payload.pkl"
    marker = tmp_path / "marker"
    data = pickle.dumps(Evil(str(marker)))
    with rs.candidate_window():
        with _expect_rule("audit:open-write"):
            mm = np.memmap(str(payload), dtype="u1", mode="w+", shape=(len(data),))
            mm[:] = np.frombuffer(data, dtype="u1")
    assert not payload.exists()


def test_the_hook_is_inert_outside_the_window(victim):
    """窗口外(框架自己写结果/日志/账本)不判定;窗口结束后状态复位。"""
    with rs.candidate_window():
        assert rs._AUDIT.active
    assert not rs._AUDIT.active
    victim.write_text("framework wrote this", encoding="utf-8")
    subprocess.run([sys.executable, "-c", "pass"], check=True)
    assert victim.read_text(encoding="utf-8") == "framework wrote this"


def test_the_window_closes_even_when_the_body_raises(victim):
    with pytest.raises(RuntimeError):
        with rs.candidate_window():
            raise RuntimeError("boom")
    assert not rs._AUDIT.active and rs._AUDIT.allowed is None


def test_denials_are_recorded_but_a_swallowed_denial_still_did_not_happen(victim):
    """候选可以 try/except 吞掉异常继续跑(残余,如实记录),但被拒的动作确实没发生,拒绝记在 window_denials()。"""
    before = victim.read_bytes()
    with rs.candidate_window():
        try:
            np.savetxt(victim, [1])
        except Exception:      # noqa: BLE001 —— 模拟候选吞异常
            pass
        assert ("audit:open-write", "open") in rs.window_denials()
    assert victim.read_bytes() == before


def test_nested_window_keeps_the_outer_allowed_directory(victim):
    with rs.candidate_window() as outer:
        with rs.candidate_window() as inner:
            assert Path(inner) == Path(outer)
            (Path(inner) / "x").write_text("ok", encoding="utf-8")
        with _expect_rule("audit:open-write"):
            open(victim, "w")           # 内层退出后外层窗口仍然有效
    assert not rs._AUDIT.active


def test_framework_exemption_is_path_level_not_a_global_switch(victim, tmp_path):
    """框架的显式豁免是**路径级/命令级**的:豁免期间窗口其余部分仍然受裁决(第 32 号 PR 复审 Medium:
    原先的 `framework_privilege` 关全局开关,候选 import 仓库模块触发守卫清 .pyc 时,窗口整个暂停)。"""
    pyc = tmp_path / "fake.pyc"
    pyc.write_bytes(b"x")
    other = tmp_path / "other.bin"
    other.write_bytes(b"y")
    with rs.candidate_window():
        with rs.framework_exempt(paths=(pyc,)):
            os.remove(pyc)                                   # 豁免的那一个:放行
            with _expect_rule("audit:path-mutation"):        # 同一豁免期间,别的路径照拒
                os.remove(other)
            with _expect_rule("audit:open-write"):
                open(victim, "w")
            with _expect_rule("audit:subprocess.Popen"):     # 也没有放开进程创建
                subprocess.run([sys.executable, "-c", "pass"])
        assert rs._AUDIT.active and rs._AUDIT.exempt_paths == ()      # 退出后复原,窗口始终是开的
        with _expect_rule("audit:path-mutation"):
            os.remove(other)
    assert other.exists()


def test_framework_exemption_for_a_command_matches_exactly_one_git_argv(tmp_path):
    git = [rs._git_binary(), "--version"]
    with rs.candidate_window():
        with rs.framework_exempt(argv=git):
            subprocess.run(git, check=True, capture_output=True)              # 恰好这一条 git:放行
            with _expect_rule("audit:subprocess.Popen"):
                subprocess.run([rs._git_binary(), "--help"], capture_output=True)   # 同一豁免期间,别的 git 命令照拒
        with _expect_rule("audit:subprocess.Popen"):
            subprocess.run(git, capture_output=True)                           # 豁免结束后同一条也拒
        # 豁免的 argv 不是 git(例如被误用来放行 python)也不放行:与 docstring 一致,只放行 git
        py = [sys.executable, "-c", "pass"]
        with rs.framework_exempt(argv=py):
            with _expect_rule("audit:subprocess.Popen"):
                subprocess.run(py)


@pytest.mark.skipif(os.name != "nt", reason="直接调用 _winapi.CreateProcess 只在 Windows 上存在")
def test_the_spawn_exemption_is_one_shot_not_sticky():
    """放行一次底层创建进程之后立即清零:同一个豁免块里再直接起进程照拒(原先 armed 黏住,块内 spawn 全放行)。"""
    import _winapi
    git = [rs._git_binary(), "--version"]
    with rs.candidate_window():
        with rs.framework_exempt(argv=git):
            subprocess.run(git, check=True, capture_output=True)
            assert rs._AUDIT.armed is False
            with _expect_rule("audit:_winapi.CreateProcess"):
                _winapi.CreateProcess(None, "cmd /c exit 0", None, None, False, 0, None, None, subprocess.STARTUPINFO())


def test_the_guard_still_runs_git_and_purges_pyc_inside_the_window(tmp_path):
    """守卫真实路径(窗口内):只读 git 与清 .pyc 都能工作——框架没有因窗口变瞎。"""
    pyc = tmp_path / "x.cpython-313.pyc"
    pyc.write_bytes(b"x")
    with rs.candidate_window():
        assert rs._git_run(["rev-parse", "HEAD"], cwd=ROOT) is not None
        rs._purge_bytecode_cache(tmp_path / "x.py", cached=pyc)
        assert not pyc.exists()
        assert rs.window_denials() == ()


def test_exemption_is_wired_only_at_the_three_framework_call_sites():
    """"只写在文档里的防线等于没有":豁免调用点有且只有这三处(grep 接线),且 research_api 里一处都没有。"""
    source = (ROOT / "scripts" / "research_sandbox.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in source.splitlines()
             if "with framework_exempt(" in line and not line.strip().startswith("#")]
    assert len(calls) == 3, calls
    # 研究侧模块里的豁免只有那三处(路径级 .pyc、命令级 git、家目录)
    assert "framework_privilege" not in source
    api_source = (ROOT / "scripts" / "research_api.py").read_text(encoding="utf-8")
    # research_api 里允许**一处**豁免:框架自己在窗口里读 vnpy 库(sqlite 连接惰性打开,落在窗口内)。
    # 它必须是**窄**豁免(只豁免 sqlite3.connect 到那个库),不带 paths=/argv=。
    calls = [line.strip() for line in api_source.splitlines() if "rs.framework_exempt(" in line]
    assert len(calls) == 1 and "sqlite_paths=" in calls[0], calls
    assert "argv=" not in calls[0] and "framework_exempt(paths=" not in calls[0], calls


def test_the_hook_denial_sets_cover_the_required_capabilities():
    assert {"pickle.find_class", "os.system", "subprocess.Popen", "os.exec", "os.spawn", "os.posix_spawn"} \
        <= rs.AUDIT_DENIED_EVENTS
    assert {"ctypes.", "socket."} <= rs.AUDIT_DENIED_EVENT_PREFIXES
    # 不拦 exec/compile/marshal.loads/import:pandas 惰性子模块导入会触发它们
    for event in ("exec", "compile", "marshal.loads", "import"):
        assert event not in rs.AUDIT_DENIED_EVENTS
        assert not any(event.startswith(p) for p in rs.AUDIT_DENIED_EVENT_PREFIXES)


# ======================================================================
# A'. 窗口接线:生产入口 import_candidate 与三个子进程入口
# ======================================================================

def _git_commit_all(root):
    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    run("add", "-A")
    run("commit", "-q", "-m", "init")


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "strategies").mkdir(parents=True)
    (root / "strategies" / "__init__.py").write_text("", encoding="utf-8")
    _git_commit_all(root)
    monkeypatch.setattr(api.rc, "CODE_ROOT", root)
    monkeypatch.chdir(root)
    for name in [n for n in list(sys.modules) if n == "strategies" or n.startswith("strategies.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)
    return root


def run_candidate(tmp_path, monkeypatch, source, allowed=("numpy", "pandas"), static=False):
    """走生产入口 `import_candidate`(窗口接线在里面),numpy/pandas 经**配置追加**放行(默认已不在白名单)。
    只绕开静态筛查与运行时类校验:这里测的是运行期的窗口。"""
    cfg = copy.deepcopy(api.rc.load())
    cfg["screening"]["allowed_modules"] = list(allowed)
    monkeypatch.setattr(api.rc, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    if not static:
        monkeypatch.setattr(api.rs, "screen_source", lambda *a, **k: [])
    path = tmp_path / "candidate.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return api.import_candidate(path, "S", api.rs.file_sha256(path))


@requires_trading_stack
def test_import_time_write_is_denied_through_the_production_entry(repo, tmp_path, monkeypatch, victim):
    """导入期窗口:候选在模块顶层 np.savetxt 改"行情 CSV"——被拒,文件不变,窗口复位。"""
    before = victim.read_bytes()
    source = f"""
        import numpy as np
        RESULT = {{}}
        try:
            np.savetxt({str(victim)!r}, np.array([1.0]))
            RESULT["wrote"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "wrote" not in cls.RESULT, cls.RESULT
    assert "审计钩子拒绝" in cls.RESULT["err"] and "audit:open-write" in cls.RESULT["err"], cls.RESULT
    assert victim.read_bytes() == before
    assert not rs._AUDIT.active


@requires_trading_stack
def test_import_time_pickle_attack_is_denied_through_the_production_entry(repo, tmp_path, monkeypatch):
    marker = tmp_path / "import_marker"
    payload = tmp_path / "p.pkl"
    payload.write_bytes(pickle.dumps(Evil(str(marker))))
    source = f"""
        import pandas as pd
        RESULT = {{}}
        try:
            pd.read_pickle({str(payload)!r})
            RESULT["ran"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "ran" not in cls.RESULT and "audit:pickle.find_class" in cls.RESULT["err"], cls.RESULT
    assert not marker.exists()


@requires_trading_stack
def test_normal_candidate_still_imports_through_the_production_entry(repo, tmp_path, monkeypatch):
    source = """
        import numpy as np
        import math
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            value = float(np.mean(np.array([1.0, 3.0]))) + math.floor(0.5)
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert cls.value == 2.0


@pytest.mark.parametrize("source,needle", [
    ("import research_sandbox\n", "白名单"),
    ("import scripts.research_sandbox\n", "白名单"),
    ("from scripts import research_sandbox\n", "白名单"),
    ("import risk.ledger_chain\n", "禁止导入"),
    ("import sys\n", "禁止导入"),
    ("import gc\n", "禁止导入"),
    ("import importlib\n", "禁止导入"),
])
def test_the_candidate_cannot_reach_the_framework_module_that_holds_the_switch(
        repo, tmp_path, monkeypatch, source, needle):
    """开关(`_AUDIT`)在框架模块里:候选 import 不到它,也就关不掉判定。断言是哪条规则拒的。"""
    with pytest.raises(api.CandidateError) as info:
        run_candidate(tmp_path, monkeypatch, source)
    assert needle in info.value.detail["message"], info.value.detail
    assert not rs._AUDIT.active and rs._AUDIT.installed


@pytest.mark.parametrize("expr", ["np.os", "np.sys", "np.__dict__", "np._core", "np.lib._npyio_impl",
                                  "np.testing.os", "np.ctypeslib"])
def test_the_candidate_cannot_reach_the_switch_through_numpy_attributes(repo, tmp_path, monkeypatch, expr):
    source = f"""
        import numpy as np
        from vnpy_ctastrategy import CtaTemplate
        try:
            leaked = {expr}
            LEAK = "got"
        except BaseException as exc:
            LEAK = None
            ERR = str(exc)
        class S(CtaTemplate):
            pass
        S.LEAK = LEAK
    """
    try:
        cls = run_candidate(tmp_path, monkeypatch, source)
    except api.CandidateError:
        return                      # 在导入阶段就被拒也是"拿不到"
    assert cls.LEAK is None, f"{expr} 居然拿到了 {cls.LEAK}"


@requires_trading_stack
def test_run_window_wraps_the_gate_evaluate_and_backtest_children(monkeypatch, tmp_path):
    """三个子进程入口的**运行期**窗口接线:调用候选相关的运行函数时窗口是开的,返回后复位。"""
    import research_gate
    import backtest_lib

    seen = {}
    monkeypatch.setattr(api, "child_file", lambda p: p)
    monkeypatch.setattr(api, "child_approved", lambda sha: None)
    monkeypatch.setattr(api, "child_params", lambda a: {})
    monkeypatch.setattr(api, "child_int_args", lambda a: (1, 1))
    monkeypatch.setattr(api, "import_candidate", lambda *a, **k: type("S", (), {}))
    monkeypatch.setattr(backtest_lib, "load_bars_csv", lambda *a, **k: [])
    monkeypatch.setattr(research_gate, "run_gate",
                        lambda **k: seen.setdefault("gate", rs._AUDIT.active) and {"ok": 1})
    monkeypatch.setattr(research_gate, "run_evaluation",
                        lambda **k: seen.setdefault("evaluate", rs._AUDIT.active) and {"ok": 1})
    monkeypatch.setattr(backtest_lib, "run_backtest",
                        lambda *a, **k: (seen.setdefault("backtest", rs._AUDIT.active), None, []))
    monkeypatch.setattr(rs, "round_trip_pnls", lambda *a, **k: [])
    monkeypatch.setattr(api, "backtest_stats_row", lambda s: {})

    parser = api.build_parser()
    gate_args = parser.parse_args(["_gate_child", "--file", "x.py", "--cls", "S", "--params", "{}",
                                   "--on-plateau", "0", "--sha256", "0" * 64, "--random-runs", "1"])
    eval_args = parser.parse_args(["_evaluate_child", "--file", "x.py", "--cls", "S",
                                   "--random-runs", "1", "--sha256", "0" * 64])
    bt_args = parser.parse_args(["_child", "--file", "x.py", "--cls", "S", "--key", "CADJ",
                                 "--segment", "train", "--params", "{}", "--sha256", "0" * 64])
    assert api.run_gate_child(gate_args) == api.EXIT_OK
    assert api.run_evaluate_child(eval_args) == api.EXIT_OK
    assert api.run_child(bt_args) == api.EXIT_OK
    assert seen == {"gate": True, "evaluate": True, "backtest": True}
    assert not rs._AUDIT.active


# ======================================================================
# B. numpy/pandas 移出候选默认白名单
# ======================================================================

def test_numpy_and_pandas_are_not_in_the_default_candidate_allowlist():
    assert not ({"numpy", "pandas"} & rs.ALLOWED_MODULE_ROOTS)
    assert rs.CAPABILITY_LIB_ROOTS == frozenset({"numpy", "pandas"})
    # 对照:纯计算类模块照旧在
    assert {"math", "statistics", "random", "json", "strategies", "vnpy"} <= rs.ALLOWED_MODULE_ROOTS


@pytest.mark.parametrize("source", ["import numpy as np\n", "import pandas as pd\n", "from numpy import mean\n",
                                    "import numpy.random\n"])
def test_static_screen_rejects_numpy_and_pandas_by_default_and_accepts_them_when_configured(source):
    default = rs.screen_source(source)
    assert any("白名单" in f for f in default), default            # 默认:被"未列入白名单"这条规则拒
    configured = rs.screen_source(source, (), (), ("numpy", "pandas"))
    assert configured == [], configured                            # 显式配置追加:放行


def test_runtime_import_gate_rejects_numpy_by_default_and_admits_it_when_configured():
    safe = rs.safe_builtins(())
    with pytest.raises(ImportError, match="未列入白名单"):
        safe["__import__"]("numpy")
    safe = rs.safe_builtins((), ("numpy",))
    module = safe["__import__"]("numpy")
    assert float(module.mean(module.array([1.0, 3.0]))) == 2.0      # 配置追加的路径照常可用(且过了准入审计)
    with pytest.raises(ImportError, match="未列入白名单"):
        safe["__import__"]("pandas")                                 # 只追加了 numpy


def test_numpy_cannot_be_smuggled_in_through_another_allowed_modules_attribute():
    """对象层规则:没有追加 numpy 时,别的白名单模块(如 `strategies.x`、`vnpy.trader.utility`)
    属性里挂着的 numpy 也不递给候选。"""
    import types
    carrier = types.ModuleType("carrier_mod")
    carrier.np = np
    carrier.pd = pd
    carrier.harmless = 1
    rs.safe_builtins(())                                              # 没有追加
    proxy = rs._proxy_for(carrier)
    assert proxy.harmless == 1
    for attr in ("np", "pd"):
        with pytest.raises(AttributeError, match="函数级能力库未授权"):
            getattr(proxy, attr)
    view = rs._view_for(carrier)
    with pytest.raises(AttributeError, match="函数级能力库未授权"):
        view.np
    rs._PROXY_CACHE.clear()
    rs.safe_builtins((), ("numpy",))                                  # 追加 numpy 之后:numpy 给,pandas 仍不给
    proxy = rs._proxy_for(carrier)
    assert proxy.np is not None
    with pytest.raises(AttributeError, match="函数级能力库未授权"):
        proxy.pd
    rs._PROXY_CACHE.clear()


def test_prompts_and_templates_do_not_depend_on_numpy_in_candidates():
    """盘点:LLM 提示词/工作流/模板里没有要求候选 import numpy/pandas(否则默认白名单变更会让它们直接被拒)。"""
    hits = []
    for rel in ("tools/bisheng", "tools/research_mcp", "strategies", "config"):
        base = ROOT / rel
        for path in base.rglob("*"):
            if path.suffix in (".py", ".md", ".json", ".yaml", ".yml", ".js", ".txt") and path.is_file():
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "import numpy" in text or "import pandas" in text or "np.mean" in text:
                    hits.append(str(path.relative_to(ROOT)))
    assert hits == [], f"这些文件引导/包含了 numpy 用法,需要随默认白名单调整: {hits}"


# ======================================================================
# C. 账本防篡改
# ======================================================================

def make_trials(tmp_path, n=3):
    path = tmp_path / "data" / "research" / "trials.jsonl"
    for index in range(n):
        tl.record_trials(path, "submission", index + 1, at=float(index))
    return path


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_trial_ledger_records_form_a_hash_chain_and_anchor_is_elsewhere(tmp_path):
    path = make_trials(tmp_path)
    rows = [json.loads(line) for line in lines(path)]
    assert [r["seq"] for r in rows] == [1, 2, 3]
    assert rows[0]["prev"] == chain.GENESIS and rows[1]["prev"] == rows[0]["hash"]
    anchor = chain.anchor_path_for(path)
    # 第 43 轮:锚点是三段 —— seq、头哈希、**那一刻的累计次数**(有了它才查得出"删行")。
    assert anchor.read_text(encoding="ascii").split() == ["3", rows[-1]["hash"], "6"]
    assert anchor.parent != path.parent and anchor.parent.name == "ledger_anchor"
    assert tl.total_trials(path) == 6


def _tamper_edit_count(path):
    rows = [json.loads(x) for x in lines(path)]
    rows[1]["count"] = 1
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


TRIAL_TAMPERS = [
    ("改小某条 count", _tamper_edit_count, "哈希对不上"),
    ("截断到前两条", lambda p: p.write_text("\n".join(lines(p)[:2]) + "\n", encoding="utf-8"), "与锚点对不上"),
    ("删除账本文件", lambda p: p.unlink(), "与锚点对不上"),
    ("清空账本", lambda p: p.write_text("", encoding="utf-8"), "与锚点对不上"),
    ("追加一条无链记录", lambda p: p.write_text(
        p.read_text(encoding="utf-8") + json.dumps({"kind": "submission", "count": 1, "at": 0}) + "\n",
        encoding="utf-8"), "没有哈希链字段"),
    ("删除锚点", lambda p: chain.anchor_path_for(p).unlink(), "没有锚点"),
    ("整本换成无链旧账本", lambda p: p.write_text(
        json.dumps({"kind": "submission", "count": 1, "at": 0}) + "\n", encoding="utf-8"), "没有哈希链字段"),
    ("删掉中间一条", lambda p: p.write_text("\n".join(lines(p)[:1] + lines(p)[2:]) + "\n", encoding="utf-8"),
     "哈希链在第 2 条断开"),
]


@pytest.mark.parametrize("name,tamper,needle", TRIAL_TAMPERS, ids=[t[0] for t in TRIAL_TAMPERS])
def test_reader_fails_closed_on_every_kind_of_trial_ledger_tampering(tmp_path, name, tamper, needle):
    """读侧这一层单独成立:这里是"绕过了钩子的外部写"(直接用 pathlib 写),不经过任何审计窗口。"""
    path = make_trials(tmp_path)
    tamper(path)
    with pytest.raises(tl.TrialLedgerError, match=needle):
        tl.total_trials(path)
    before = path.read_bytes() if path.exists() else None
    with pytest.raises(tl.TrialLedgerError):                         # 写侧也不在被篡改的账本上继续追加
        tl.record_trials(path, "gate_run", 1, at=9.0)
    assert (path.read_bytes() if path.exists() else None) == before


def test_a_forged_consistent_chain_without_touching_the_anchor_is_detected(tmp_path):
    path = make_trials(tmp_path)
    forged = [chain.seal({"kind": "submission", "count": 1, "at": 0.0}, chain.GENESIS, 1)]
    path.write_text(json.dumps(forged[0]) + "\n", encoding="utf-8")   # 链自洽、更短,但锚点没改
    with pytest.raises(tl.TrialLedgerError, match="与锚点对不上"):
        tl.total_trials(path)


def test_known_residual_forging_ledger_and_anchor_together_is_not_detected(tmp_path):
    """如实钉住的**残余**(文件头/ADR 都写了):哈希链没有密钥,同时改账本与锚点的人骗得过读侧。
    这一条不是"期望如此",是防止有人把本层写成"防篡改"的过强声明;真正的外部锚点要 #29 的另一个信任域。"""
    path = make_trials(tmp_path)
    forged = chain.seal({"kind": "submission", "count": 1, "at": 0.0}, chain.GENESIS, 1)
    path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
    chain.write_anchor(chain.anchor_path_for(path), forged["seq"], forged["hash"])
    assert tl.total_trials(path) == 1


def test_a_crash_between_ledger_append_and_anchor_write_is_benign_for_trials(tmp_path):
    """崩在写锚点之前:账本比锚点多一条自洽的记录——试验账本按良性处理(只会让 N 变大),下次写入修复锚点。"""
    path = make_trials(tmp_path)
    anchor = chain.anchor_path_for(path)
    anchor_before = anchor.read_text(encoding="ascii")
    tl.record_trials(path, "grid_cell", 5, at=7.0)
    anchor.write_text(anchor_before, encoding="ascii")                # 回退锚点,模拟崩溃
    assert tl.total_trials(path) == 11
    tl.record_trials(path, "grid_cell", 1, at=8.0)
    assert anchor.read_text(encoding="ascii").split()[0] == "5"
    assert tl.total_trials(path) == 12


def test_candidate_cannot_shrink_the_trial_ledger_through_the_hook_layer(tmp_path):
    """写侧这一层单独成立:窗口内 savetxt/to_csv/open/memmap 改账本都被审计钩子拒,账本字节不变。"""
    path = make_trials(tmp_path)
    before = path.read_bytes()
    anchor = chain.anchor_path_for(path)
    anchor_before = anchor.read_bytes()
    with rs.candidate_window():
        for attack in (lambda: np.savetxt(path, [1]),
                       lambda: pd.DataFrame({"a": [1]}).to_csv(path),
                       lambda: open(path, "w"),
                       lambda: np.memmap(str(path), dtype="u1", mode="r+"),
                       lambda: np.savetxt(anchor, [1]),
                       lambda: os.remove(path)):
            with _expect_rule("audit:open-write" if attack is not None and "remove" not in
                              attack.__code__.co_names else "audit:path-mutation"):
                attack()
    assert path.read_bytes() == before and anchor.read_bytes() == anchor_before
    assert tl.total_trials(path) == 6


def test_trial_ledger_two_layers_each_discriminate(tmp_path):
    """两层各自有判别力:钩子层(窗口内写被拒)与读侧(窗口外外部写被发现)互不依赖。"""
    path = make_trials(tmp_path)
    with rs.candidate_window():
        with pytest.raises(rs.AuditDenied):
            np.savetxt(path, [1])
    assert tl.total_trials(path) == 6                                  # 钩子层:没被改
    path.write_text("", encoding="utf-8")                              # 绕过钩子的外部写
    with pytest.raises(tl.TrialLedgerError):                           # 读侧:被发现
        tl.total_trials(path)


def test_concurrent_writers_in_separate_processes_keep_the_chain_intact(tmp_path):
    path = tmp_path / "data" / "research" / "trials.jsonl"
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        from risk.trial_ledger import record_trials
        for i in range(8):
            record_trials(Path({str(path)!r}), "submission", 1, at=float(i))
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script], stderr=subprocess.PIPE) for _ in range(4)]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode("utf-8", "replace")
    assert tl.total_trials(path) == 32
    assert [json.loads(x)["seq"] for x in lines(path)] == list(range(1, 33))


# ---- 迁移与人工重置 ----

def test_legacy_trial_ledger_is_not_silently_trusted_but_migrates_once(tmp_path):
    path = tmp_path / "data" / "research" / "trials.jsonl"
    path.parent.mkdir(parents=True)
    legacy = [{"kind": "submission", "count": 2, "at": 1.0}, {"kind": "grid_cell", "count": 343, "at": 2.0}]
    path.write_text("\n".join(json.dumps(r) for r in legacy) + "\n", encoding="utf-8")
    with pytest.raises(tl.TrialLedgerError, match="migrate"):          # 不静默采信,且给出路
        tl.total_trials(path)
    result = admin.migrate_trials(path)
    assert result["migrated"] == 2 and result["total"] == 345 and result["backup"]
    assert tl.total_trials(path) == 345                                # 迁移后总数不变,流水线可继续
    assert tl.record_trials(path, "gate_run", 1, at=3.0) == 346
    with pytest.raises(admin.AdminError, match="已有锚点"):            # 已迁移的账本不能再被"迁移"洗白
        admin.migrate_trials(path)


def test_migrate_refuses_a_ledger_that_already_carries_chain_fields(tmp_path):
    path = make_trials(tmp_path)
    chain.anchor_path_for(path).unlink()
    with pytest.raises(admin.AdminError, match="哈希链字段"):
        admin.migrate_trials(path)


def test_trial_reset_keeps_the_valid_prefix_and_supports_manual_top_up(tmp_path):
    path = make_trials(tmp_path)                                       # 1+2+3
    rows = lines(path)
    path.write_text(rows[0] + "\n" + rows[1] + "\n" + '{"kind":"submission","count":1}\n', encoding="utf-8")
    with pytest.raises(tl.TrialLedgerError):
        tl.total_trials(path)
    out = admin.reset_trials(path, add_trials=100, at=5.0)
    assert out["kept"] == 3 and out["total"] == 1 + 2 + 100 and out["backup"]
    assert tl.total_trials(path) == 103


def test_reset_trials_refuses_to_shrink_the_trial_count_without_an_explicit_flag(tmp_path):
    """第 40 轮(Medium 1):重置**不许悄悄把 N 变小**——N 变小 = DSR 的分母变小 = 去通胀失效
    (年化 Sharpe 3.5:N=1620 时 DSR 0.6397 不过,N=1 时 0.9999 过;见 risk/trial_ledger.py 的说明)。

    基准取"账本**声称**的总数"(不看链):链在中间断掉时,有效前缀比真实历史少一大截,
    拿有效前缀当基准就等于"变小不算变小"。"""
    path = make_trials(tmp_path)                                       # 1+2+3 = 6
    rows = lines(path)
    path.write_text(rows[0] + "\n" + rows[1] + "\n" + '{"kind":"submission","count":3}\n', encoding="utf-8")

    with pytest.raises(admin.AdminError, match="降到"):
        admin.reset_trials(path, reason="链断")

    out = admin.reset_trials(path, allow_reduce_n=True, reason="链断,丢失的试验次数无法恢复")
    assert out["before"] == 6 and out["total"] == 3                 # 6 → 3:变小这件事被显式确认过
    recorded = [json.loads(line) for line in lines(path)]
    resets = [r for r in recorded if r["kind"] == tl.RESET_KIND]
    assert len(resets) == 1 and resets[0]["before"] == 6 and resets[0]["after"] == 3
    assert resets[0]["count"] == 0 and "reason" in resets[0]        # 重置本身不是一次试验
    assert tl.total_trials(path) == 3                                # 链仍然自洽(N 确实变成了 3)


def test_the_reset_cli_refuses_to_shrink_the_trial_count_without_the_flag(tmp_path, monkeypatch, capsys):
    """命令行入口同样必须拦住(守卫不能只做在库里)。"""
    import research_ledger as cli
    trials = tmp_path / "data" / "research" / "trials.jsonl"
    approvals = tmp_path / "data" / "research-control" / "approvals.json"
    monkeypatch.setattr(cli, "_paths", lambda: (trials, approvals))
    for index in range(3):
        tl.record_trials(trials, "submission", index + 1, at=float(index))
    rows = trials.read_text(encoding="utf-8").splitlines()
    trials.write_text(rows[0] + "\n" + rows[1] + "\n" + '{"kind":"submission","count":3}\n', encoding="utf-8")

    assert cli.main(["reset", "trials", "--reason", "链断"]) == 1
    assert "降到" in capsys.readouterr().err

    assert cli.main(["reset", "trials", "--reason", "链断", "--allow-reduce-n"]) == 0
    capsys.readouterr()
    assert tl.total_trials(trials) == 3


# ---- 批准账本 ----

@pytest.fixture()
def approvals(tmp_path):
    cand = tmp_path / "cand.py"
    cand.write_text("class C: pass\n", encoding="utf-8")
    cand2 = tmp_path / "cand2.py"
    cand2.write_text("class D: pass\n", encoding="utf-8")
    file = tmp_path / "data" / "research-control" / "approvals.json"
    return cand, cand2, file


def test_approvals_form_a_chain_and_legit_flow_still_works(approvals):
    cand, cand2, file = approvals
    sha = rs.record_approval(cand, file)
    sha2 = rs.record_approval(cand2, file)
    assert rs.record_approval(cand, file) == sha                       # 幂等
    data = json.loads(file.read_text(encoding="utf-8"))
    assert set(data) == {sha, sha2} and data[sha2]["prev"] == data[sha]["hash"]
    assert rs.is_approved_sha(sha, file) and rs.is_approved(cand2, file)
    assert not rs.is_approved_sha("0" * 64, file)
    assert chain.anchor_path_for(file).parent.name == "ledger_anchor"


def _forge_plain(cand_sha):
    def run(file):
        data = json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
        data[cand_sha] = {"name": "evil.py", "at": "now"}
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(data), encoding="utf-8")
    return run


def _forge_sealed_ahead(cand_sha):
    """攻击者会算哈希:追加一条链上自洽的记录,但不改锚点。"""
    def run(file):
        data = json.loads(file.read_text(encoding="utf-8"))
        rows = sorted(data.values(), key=lambda r: r["seq"])
        row = chain.seal({"sha": cand_sha, "name": "evil.py", "at": "now"}, rows[-1]["hash"], len(rows) + 1)
        data[cand_sha] = row
        file.write_text(json.dumps(data), encoding="utf-8")
    return run


EVIL_SHA = "e" * 64
APPROVAL_TAMPERS = [
    ("写入无链记录", _forge_plain(EVIL_SHA), "没有哈希链字段"),
    ("追加自洽的链上记录(锚点没改)", _forge_sealed_ahead(EVIL_SHA), "多出 1 条"),
    ("回滚到只剩第一条", lambda f: f.write_text(json.dumps(
        {k: v for k, v in json.loads(f.read_text(encoding="utf-8")).items() if v["seq"] == 1}), encoding="utf-8"),
     "与锚点对不上"),
    ("删除锚点", lambda f: chain.anchor_path_for(f).unlink(), "没有锚点"),
    ("改某条记录的 name", lambda f: f.write_text(json.dumps(
        {k: {**v, "name": "x"} if v["seq"] == 1 else v for k, v in json.loads(f.read_text(encoding="utf-8")).items()}),
        encoding="utf-8"), "哈希对不上"),
    ("整个文件清空成 {}", lambda f: f.write_text("{}", encoding="utf-8"), "与锚点对不上"),
]


@pytest.mark.parametrize("name,tamper,needle", APPROVAL_TAMPERS, ids=[t[0] for t in APPROVAL_TAMPERS])
def test_forged_or_damaged_approvals_are_never_trusted(approvals, name, tamper, needle):
    """候选(或任何外部写入者)写进去的记录不会被采信:无链/链断/多出/回滚一律 fail-closed。"""
    cand, cand2, file = approvals
    sha = rs.record_approval(cand, file)
    rs.record_approval(cand2, file)
    tamper(file)
    for probe in (lambda: rs.is_approved_sha(sha, file), lambda: rs.is_approved_sha(EVIL_SHA, file),
                  lambda: rs._load_approvals(file)):
        with pytest.raises(rs.SandboxError, match=needle):
            probe()
    with pytest.raises(rs.SandboxError):                               # 也不在坏账本上继续批准
        rs.record_approval(cand, file)


def test_child_entry_refuses_to_run_with_a_forged_approval(approvals, monkeypatch):
    """子进程入口 `child_approved` 的实际调用点:伪造的批准 -> JobError(拒绝),不是"已批准"。"""
    cand, cand2, file = approvals
    rs.record_approval(cand, file)
    _forge_plain(EVIL_SHA)(file)
    monkeypatch.setattr(api, "settings", lambda: ({}, {"approvals_file": file}))
    with pytest.raises(api.JobError) as info:
        api.child_approved(EVIL_SHA)
    assert info.value.code == api.EXIT_REFUSED and "没有哈希链字段" in info.value.message


def test_candidate_cannot_write_its_own_sha_into_approvals_through_the_hook_layer(approvals):
    cand, cand2, file = approvals
    rs.record_approval(cand, file)
    before = file.read_bytes()
    with rs.candidate_window():
        with _expect_rule("audit:open-write"):
            open(file, "w").write(json.dumps({EVIL_SHA: {"name": "x", "at": "now"}}))
        with _expect_rule("audit:open-write"):
            pd.DataFrame({"a": [1]}).to_json(file)
    assert file.read_bytes() == before


def test_approval_migration_and_reset(approvals):
    cand, cand2, file = approvals
    file.parent.mkdir(parents=True)
    sha1, sha2 = rs.file_sha256(cand), rs.file_sha256(cand2)
    file.write_text(json.dumps({sha1: {"name": "cand.py", "at": "1"}, sha2: {"name": "cand2.py", "at": "2"}}),
                    encoding="utf-8")
    with pytest.raises(rs.SandboxError, match="migrate"):              # 旧账本不静默采信,给出路
        rs.is_approved_sha(sha1, file)
    assert admin.migrate_approvals(file)["migrated"] == 2
    assert rs.is_approved_sha(sha1, file) and rs.is_approved_sha(sha2, file)
    with pytest.raises(admin.AdminError, match="已有锚点"):
        admin.migrate_approvals(file)
    _forge_plain(EVIL_SHA)(file)
    with pytest.raises(rs.SandboxError):
        rs.is_approved_sha(sha1, file)
    out = admin.reset_approvals(file)                                  # 人工重置:只留链上有效前缀,伪造的被丢弃
    assert out["kept"] == 2 and len(out["dropped"]) == 1
    assert rs.is_approved_sha(sha1, file) and not rs.is_approved_sha(EVIL_SHA, file)


def test_research_ledger_cli_status_migrate_reset(tmp_path, monkeypatch, capsys):
    import research_ledger as cli
    trials = tmp_path / "data" / "research" / "trials.jsonl"
    approvals_file = tmp_path / "data" / "research-control" / "approvals.json"
    monkeypatch.setattr(cli, "_paths", lambda: (trials, approvals_file))
    trials.parent.mkdir(parents=True)
    trials.write_text(json.dumps({"kind": "submission", "count": 4, "at": 1.0}) + "\n", encoding="utf-8")
    assert cli.main(["status"]) == 0
    assert "migrate" in capsys.readouterr().out
    assert cli.main(["migrate", "trials"]) == 0
    assert tl.total_trials(trials) == 4
    assert cli.main(["reset", "trials", "--reason", "x"]) == 1           # 健康账本默认拒绝重置
    assert cli.main(["migrate", "approvals"]) == 1                      # 缺 --confirm-all-approved / 无文件
    log = chain.anchor_path_for(trials).parent / "ledger.admin.log"
    assert log.exists() and "migrate" in log.read_text(encoding="utf-8")


def _container_tag(posix_path: str) -> str:
    import hashlib
    return hashlib.sha256(posix_path.lower().encode("utf-8")).hexdigest()[:10]


def test_research_ledger_cli_container_view_anchors_for_the_gateway_path(tmp_path, monkeypatch, capsys):
    """Issue #73:网关容器里账本在 /app/data/research/,锚点文件名按那个路径取哈希。

    宿主机人工入口加 --as-container-root /app 后,必须把锚点写到容器会去读的文件名上
    (宿主机真实目录 data/ledger_anchor/,容器里挂在 /app/data/ledger_anchor/)。
    不加该参数时行为不变;读侧(chain.verify)不动。"""
    import research_ledger as cli
    trials = tmp_path / "data" / "research" / "trials.jsonl"
    approvals_file = tmp_path / "data" / "research-control" / "approvals.json"
    monkeypatch.setattr(cli, "_paths", lambda: (trials, approvals_file))
    monkeypatch.setattr(cli, "_ROOT", tmp_path)
    trials.parent.mkdir(parents=True)
    trials.write_text(json.dumps({"kind": "submission", "count": 3, "at": 1.0}) + "\n", encoding="utf-8")
    assert cli.main(["migrate", "trials", "--as-container-root", "/app"]) == 0
    anchors = tmp_path / "data" / "ledger_anchor"
    want = anchors / f"trials-{_container_tag('/app/data/research/trials.jsonl')}.anchor"
    assert want.exists()
    seq, head, _total = chain.read_anchor(want)
    row = json.loads(trials.read_text(encoding="utf-8").splitlines()[-1])
    assert (seq, head) == (row["seq"], row["hash"])
    # 没加参数的宿主机视角没有被顺手写出来:两边是各自独立的锚点
    assert not chain.anchor_path_for(trials).exists()
    # 容器视角下 status 读到的是健康的
    capsys.readouterr()
    assert cli.main(["status", "--as-container-root", "/app"]) == 0
    assert '"trials": "ok"' in capsys.readouterr().out


def test_research_ledger_cli_container_view_still_fails_closed_without_anchor(tmp_path, monkeypatch, capsys):
    import research_ledger as cli
    trials = tmp_path / "data" / "research" / "trials.jsonl"
    approvals_file = tmp_path / "data" / "research-control" / "approvals.json"
    monkeypatch.setattr(cli, "_paths", lambda: (trials, approvals_file))
    monkeypatch.setattr(cli, "_ROOT", tmp_path)
    trials.parent.mkdir(parents=True)
    trials.write_text(json.dumps({"kind": "submission", "count": 3, "at": 1.0}) + "\n", encoding="utf-8")
    assert cli.main(["migrate", "trials"]) == 0                          # 只给宿主机视角定了锚
    capsys.readouterr()
    assert cli.main(["status", "--as-container-root", "/app"]) == 0
    assert "没有锚点" in capsys.readouterr().out                           # 容器视角没锚点 ⇒ 仍然拒绝


def test_research_ledger_cli_rejects_a_relative_container_root(tmp_path, monkeypatch):
    import research_ledger as cli
    monkeypatch.setattr(cli, "_paths", lambda: (tmp_path / "t.jsonl", tmp_path / "a.json"))
    assert cli.main(["status", "--as-container-root", "app"]) == 1


# ======================================================================
# D. 行情在候选导入前取定
# ======================================================================

def _write_bars_csv(path, close):
    path.write_text("datetime,open,high,low,close,volume,open_interest\n"
                    f"2023-01-03 09:00:00,10,11,9,{close},100,1000\n"
                    f"2023-01-03 09:15:00,10,11,9,{close},100,1000\n", encoding="utf-8")


@pytest.fixture()
def child_env(monkeypatch, tmp_path):
    import research_gate
    import walk_forward_optimizer as wf
    csv = tmp_path / "bars.csv"
    _write_bars_csv(csv, 10.5)
    monkeypatch.setattr(wf, "DATA_FILE", csv)
    # #70 之后评估按产品从 wf.PRODUCTS 取数据文件;只补 DATA_FILE 会读到真实行情(夹具要同步换掉 C15 那一项)
    monkeypatch.setitem(wf.PRODUCTS, "C15", (csv, wf.PRODUCTS["C15"][1]))
    monkeypatch.setattr(api, "child_file", lambda p: p)
    monkeypatch.setattr(api, "child_approved", lambda sha: None)
    monkeypatch.setattr(api, "child_params", lambda a: {})
    monkeypatch.setattr(api, "child_int_args", lambda a: (1, 1))
    return csv, research_gate


def _args(kind):
    parser = api.build_parser()
    if kind == "gate":
        return parser.parse_args(["_gate_child", "--file", "x.py", "--cls", "S", "--params", "{}",
                                  "--on-plateau", "0", "--sha256", "0" * 64, "--random-runs", "1"])
    return parser.parse_args(["_evaluate_child", "--file", "x.py", "--cls", "S",
                              "--random-runs", "1", "--sha256", "0" * 64])


@pytest.mark.parametrize("kind", ["gate", "evaluate"])
@requires_trading_stack
def test_bars_are_fixed_before_the_candidate_is_imported(kind, child_env, monkeypatch):
    """导入期(假设绕过了钩子的)外部写改了 CSV,框架拿到的仍是导入前读到的那份数据。"""
    csv, research_gate = child_env
    order, captured = [], {}

    def tampering_import(*a, **k):
        order.append("import")
        _write_bars_csv(csv, 99999.0)                 # 导入期改 CSV(模拟钩子被绕过的外部写)
        return type("S", (), {})

    monkeypatch.setattr(api, "import_candidate", tampering_import)

    def fake_run(**kwargs):
        captured["bars"] = kwargs["bars"]
        return {"ok": 1}

    monkeypatch.setattr(research_gate, "run_gate" if kind == "gate" else "run_evaluation", fake_run)
    code = api.run_gate_child(_args("gate")) if kind == "gate" else api.run_evaluate_child(_args("evaluate"))
    assert code == api.EXIT_OK and order == ["import"]
    assert [bar.close_price for bar in captured["bars"]] == [10.5, 10.5], "框架读到了导入期被改过的价格"


@requires_trading_stack
def test_the_production_import_cannot_rewrite_the_csv_in_the_first_place(repo, tmp_path, monkeypatch):
    """上一条的另一半:生产入口里,候选导入期 np.savetxt 改 CSV 被钩子拒(两层各自成立)。"""
    csv = tmp_path / "bars.csv"
    _write_bars_csv(csv, 10.5)
    before = csv.read_bytes()
    source = f"""
        import numpy as np
        from vnpy_ctastrategy import CtaTemplate
        try:
            np.savetxt({str(csv)!r}, np.array([1.0]))
        except Exception:
            pass
        class S(CtaTemplate):
            pass
    """
    run_candidate(tmp_path, monkeypatch, source)
    assert csv.read_bytes() == before
    assert ("audit:open-write", "open") in rs.window_denials()


# ======================================================================
# 第 32 号 PR 复审的补充项
# ======================================================================

def test_audit_denied_is_an_oserror_but_not_a_permission_error():
    """继承 OSError(导入机制吞它,写 .pyc 缓存被拒时静默放弃),但**不是** PermissionError
    (tempfile 在 Windows 上对 PermissionError 会重试到 TMP_MAX,窗口内实测挂住数分钟)。"""
    assert issubclass(rs.AuditDenied, OSError) and not issubclass(rs.AuditDenied, PermissionError)


def test_mkstemp_into_a_denied_directory_fails_fast_instead_of_hanging(tmp_path):
    denied_dir = tmp_path / "denied"
    denied_dir.mkdir()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT / "scripts")!r})
        import tempfile
        import research_sandbox as rs
        started = time.monotonic()
        with rs.candidate_window():
            try:
                tempfile.mkstemp(dir={str(denied_dir)!r})
            except rs.AuditDenied as exc:
                print("DENIED", exc.rule, round(time.monotonic() - started, 2))
            else:
                print("ALLOWED")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert proc.stdout.split()[:2] == ["DENIED", "audit:open-write"], (proc.stdout, proc.stderr)
    assert float(proc.stdout.split()[2]) < 10
    assert list(denied_dir.iterdir()) == []


def test_an_import_inside_the_window_silently_gives_up_writing_its_pyc_cache(tmp_path, monkeypatch):
    """放行方向:导入机制写 `__pycache__` 被拒时静默放弃缓存,导入本身照常成功,也不算"越权企图"。"""
    pkg = tmp_path / "pycdemo"
    pkg.mkdir()
    (pkg / "mod_pycdemo.py").write_text("VALUE = 41\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(pkg))
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    import importlib
    with rs.candidate_window():
        module = importlib.import_module("mod_pycdemo")
        assert module.VALUE == 41
        assert rs.window_denials() == ()
    assert not (pkg / "__pycache__").exists()


def test_hardlink_needs_both_ends_inside_the_allowed_directory(victim):
    """os.link:源端也要判——否则允许目录里的硬链接与外部文件是同一个 inode,经它写就等于改外部文件。"""
    with rs.candidate_window() as scratch:
        inner = scratch / "a.txt"
        inner.write_text("x", encoding="utf-8")
        os.link(inner, scratch / "b.txt")                         # 两端都在允许目录:放行
        with _expect_rule("audit:path-mutation"):
            os.link(victim, scratch / "evil_link.csv")            # 源端在外面:拒
        with _expect_rule("audit:path-mutation"):
            os.link(inner, victim.parent / "evil2.csv")           # 目的端在外面:拒
    assert not (victim.parent / "evil2.csv").exists()


def test_os_write_has_no_audit_event_and_stdout_logging_still_works(victim, capsys):
    """如实钉住一个**限制**:CPython 没有 `os.write`/`os.pwrite` 的审计事件,钩子无法裁决对已打开 fd 的写。
    fd 只能来自此前已裁决过的 `open`(写模式 open 在窗口内被拒;只读 fd 上的 write 在本机 EBADF);
    本用例确认:这条限制没被说成"已拦",并且 print/日志在窗口内照常。"""
    seen = []
    with rs.candidate_window():
        print("hello from window")
        import logging
        logging.getLogger("fs-capability-test").warning("window log")
        fd = os.open(str(victim), os.O_RDONLY)
        try:
            with pytest.raises(OSError) as info:
                os.write(fd, b"x")
            assert not isinstance(info.value, rs.AuditDenied)    # 失败原因是 fd 只读(EBADF),不是钩子
        finally:
            os.close(fd)
        seen.append(rs.window_denials())
    assert victim.read_text(encoding="utf-8").startswith("a,b")
    assert "hello from window" in capsys.readouterr().out
    assert seen == [()]


# ---- vnpy.trader.utility 的任意路径写原语 ----

@requires_trading_stack
def test_save_json_style_primitives_are_not_reachable_by_the_candidate(tmp_path):
    """第 32 号 PR 复审 Medium:`vnpy.trader.utility.save_json(filename)` 用 `get_file_path(filename)`(joinpath),
    绝对路径胜出 = 任意路径写。对象层规则把这些函数从候选可达的属性里拿掉;模板需要的成员仍可取。"""
    import types
    import vnpy.trader.utility as util
    rs.safe_builtins(())
    proxy = rs._proxy_for(util)
    members = sorted(m.rsplit(".", 1)[1] for m in rs.UNREACHABLE_MEMBERS
                     if m.startswith("vnpy.trader.utility.") and not m.rsplit(".", 1)[1].startswith("_"))
    assert {"save_json", "load_json", "get_file_path", "get_folder_path", "TEMP_DIR"} <= set(members)
    for name in members:
        with pytest.raises(AttributeError, match="任意路径读写"):
            getattr(proxy, name)
    # 放行方向:模板正常要的成员照旧
    for name in ("ArrayManager", "BarGenerator", "virtual", "round_to", "floor_to", "ceil_to"):
        assert getattr(proxy, name) is getattr(util, name)
    view = rs._view_for(util)
    with pytest.raises(AttributeError, match="任意路径读写"):
        view.save_json
    assert view.ArrayManager is util.ArrayManager
    assert isinstance(types, object)


@requires_trading_stack
def test_the_production_import_path_cannot_call_save_json(repo, tmp_path, monkeypatch):
    target = tmp_path / "save_json_victim.json"
    source = f"""
        from vnpy.trader.utility import ArrayManager, BarGenerator
        RESULT = {{}}
        import vnpy.trader.utility as util
        try:
            util.save_json({str(target)!r}, {{"x": 1}})
            RESULT["wrote"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        try:
            from vnpy.trader.utility import save_json
            save_json({str(target)!r}, {{"x": 1}})
            RESULT["wrote2"] = True
        except Exception as exc:
            RESULT["err2"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
        S.AM = ArrayManager
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "wrote" not in cls.RESULT and "wrote2" not in cls.RESULT, cls.RESULT
    assert "任意路径读写" in cls.RESULT["err"], cls.RESULT        # 属性访问形态:报的是这条规则
    assert "err2" in cls.RESULT                                    # from-import 形态:导入机制把 AttributeError 转成 ImportError
    assert not target.exists()
    assert cls.AM.__name__ == "ArrayManager"


# ---- reset approvals 的容错(第 32 号 PR 复审 High)与账本锁 ----

@pytest.mark.parametrize("damage", ["corrupted-string", "not json {{{", "", "[1, 2, 3]"],
                         ids=["JSON 字符串", "非 JSON", "空文件", "数组"])
def test_reset_approvals_recovers_a_file_damaged_beyond_parsing(approvals, damage):
    """批准记录被写坏成非对象/非 JSON/空文件:读侧 fail-closed 且消息里有可执行的出路;
    `reset approvals` 不再被严格读取卡死,备份后按创世重新定锚,之后能继续批准。"""
    cand, cand2, file = approvals
    rs.record_approval(cand, file)
    file.write_text(json.dumps(damage) if damage == "corrupted-string" else damage, encoding="utf-8")
    with pytest.raises(rs.SandboxError) as info:
        rs.is_approved_sha("0" * 64, file)
    assert "research_ledger.py reset approvals" in str(info.value) and "--force" in str(info.value)
    out = admin.reset_approvals(file)
    assert out["kept"] == 0 and out["backup"]
    assert not rs.is_approved_sha(rs.file_sha256(cand), file)          # 没有任何有效前缀:要重新批准
    sha = rs.record_approval(cand, file)
    assert rs.is_approved_sha(sha, file)
    rs.record_approval(cand2, file)
    assert rs.is_approved_sha(rs.file_sha256(cand2), file)


def test_reset_approvals_cli_works_on_a_corrupted_file(tmp_path, monkeypatch, capsys):
    import research_ledger as cli
    cand = tmp_path / "c.py"
    cand.write_text("class C: pass\n", encoding="utf-8")
    trials = tmp_path / "data" / "research" / "trials.jsonl"
    file = tmp_path / "data" / "research-control" / "approvals.json"
    monkeypatch.setattr(cli, "_paths", lambda: (trials, file))
    rs.record_approval(cand, file)
    file.write_text('"corrupted"', encoding="utf-8")
    assert cli.main(["reset", "approvals", "--reason", "文件被写坏"]) == 0       # 状态异常:不需要 --force
    capsys.readouterr()
    assert rs.record_approval(cand, file) == rs.file_sha256(cand)             # 重置后能继续批准
    log = chain.anchor_path_for(trials).parent / "ledger.admin.log"
    assert "文件被写坏" in log.read_text(encoding="utf-8")                      # 留审计痕迹


def test_trial_ledger_error_messages_carry_runnable_steps(tmp_path):
    path = make_trials(tmp_path)
    path.write_text("", encoding="utf-8")
    with pytest.raises(tl.TrialLedgerError) as info:
        tl.total_trials(path)
    text = str(info.value)
    assert "research_ledger.py reset trials" in text and "--add-trials" in text and "--force" in text


def test_file_lock_retries_on_windows_delete_pending_permission_error(tmp_path, monkeypatch):
    """"全量里偶发失败"的真根因(压测复现:`[Errno 13] Permission denied: ...chainlock`):Windows 上锁文件刚被
    持有者 unlink、处于删除待决时,别人 O_EXCL 创建抛 PermissionError 而不是 FileExistsError。必须按"被占用"重试。"""
    real_open = os.open
    calls = {"n": 0}

    def flaky_open(path, flags, *a, **k):
        if str(path).endswith(".chainlock") and calls["n"] < 3:
            calls["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(chain.os, "open", flaky_open)
    with chain.file_lock(tmp_path / "x.jsonl", timeout=10):
        pass
    assert calls["n"] == 3


def test_known_residual_a_callback_planted_in_the_window_fires_outside_it(tmp_path):
    """如实钉住的**残余**(第 32 号 PR 复审实测,本机复现):钩子只罩"候选正在跑"这一段。
    候选若在窗口内埋一个回调(`np.seterrcall` + `np.seterr(all="call")`),它在窗口**外**触发时写文件不受限。
    今天端到端打不通,靠的是宪法的 `__del__` 禁令与 atexit/threading/weakref/gc 在候选侧取不到——这是显式依赖,
    不是钩子的能力。本用例不是"期望如此",是防止有人把钩子说成"覆盖了候选的所有执行"。
    (彻底做法:钩子 active 覆盖子进程整个生命周期,框架自己的写走显式放行;本 PR 未做,见 ADR 残余。)"""
    out = tmp_path / "planted.txt"

    def callback(kind, flag):
        open(out, "w", encoding="utf-8").write("written outside the window")

    old = np.seterr(all="ignore")
    try:
        with rs.candidate_window():
            np.seterr(all="call")
            previous = np.seterrcall(callback)           # 窗口内埋回调:这一步本身不被拒
        np.float64(1.0) / np.float64(0.0)                 # 窗口外触发
        assert out.exists(), "若这条变红:说明残余被收掉了,请同步更新 ADR 残余清单"
    finally:
        np.seterrcall(previous)
        np.seterr(**old)


# ======================================================================
# 第 37 轮复审:导入原语再导出 / sqlite3 无 open 事件 / 账本锁分类
# ======================================================================

@requires_trading_stack
def test_importlib_import_module_reexport_is_refused_by_the_object_layer():
    """Critical:`vnpy.trader.database`/`datafeed` 再导出了 `importlib.import_module`(不受限的导入原语)。
    断言是**对象层的"能力泄漏"规则**拒的(按 `__module__` 的根判来源,不是名字名单)。"""
    import vnpy.trader.database as database
    import vnpy.trader.datafeed as datafeed
    rs.safe_builtins(())
    for module in (database, datafeed):
        assert module.import_module.__module__ == "importlib"          # 对照:确实是 importlib 的函数
        proxy = rs._proxy_for(module)
        with pytest.raises(AttributeError, match="规则:能力泄漏"):
            proxy.import_module
        with pytest.raises(AttributeError, match="规则:能力泄漏"):
            rs._view_for(module).import_module
    rs._PROXY_CACHE.clear()


def test_builtins_import_primitive_reexported_by_a_module_is_refused():
    import builtins
    import types
    carrier = types.ModuleType("carrier_builtins")
    carrier.importer = builtins.__import__
    carrier.opener = builtins.open
    carrier.harmless = len                                            # 对照:别的 builtins 函数照常
    rs.safe_builtins(())
    proxy = rs._proxy_for(carrier)
    assert proxy.harmless is len
    for name in ("importer", "opener"):
        with pytest.raises(AttributeError, match="规则:导入原语"):
            getattr(proxy, name)
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_threads_processes_paths_reexported_by_whitelisted_modules_are_refused():
    import vnpy.trader.engine as engine
    import vnpy.trader.utility as util
    rs.safe_builtins(())
    proxy = rs._proxy_for(engine)
    for name in ("Thread", "current_thread"):
        with pytest.raises(AttributeError, match="规则:按来源不可达"):
            getattr(proxy, name)
    with pytest.raises(AttributeError, match="任意路径读写原语"):           # `vnpy.trader.utility.Path`:成员级规则先拒(与来源规则重复登记)
        rs._proxy_for(util).Path
    import types
    carrier = types.ModuleType("carrier_path")
    carrier.P = util.Path                                              # 别的模块再导出 Path:按来源(pathlib)统一处置
    with pytest.raises(AttributeError, match="规则:按来源不可达"):
        rs._check_object_rules(carrier, "P", util.Path)
    assert rs._proxy_for(util).ArrayManager is util.ArrayManager       # 放行方向:模板要的成员照旧
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_systematic_scan_no_whitelisted_module_hands_out_a_dangerous_origin():
    """系统性扫描的回归版(完整扫描脚本与结果见 ADR):逐个公开成员按 `__module__`/模块名判来源,
    凡是危险来源,对象层规则必须拒;不按成员名猜。覆盖脚本扫描里出过命中的那几个模块。"""
    import importlib
    import types
    # **危险来源集合从 `rs.DANGEROUS_MODULE_NAMES` 派生**,不再手写名单:
    # 第 40 轮实测证明"手写名单"这一族已经漏了三次(第 37 轮 `importlib` → 第 37 轮 `multiprocessing`
    # → 第 40 轮 `concurrent`),每漏一次就要补一次名字。派生之后,危险模块登记一次,
    # 本用例自动跟着变严——`concurrent` 就是这么被补上的(实测 `vnpy.trader.optimize.ProcessPoolExecutor`、
    # `vnpy_ctastrategy.engine.ThreadPoolExecutor`/`Future` 三个都可达)。
    # `builtins` 排除:内建原语另有一条更精确的规则(`BUILTIN_PRIMITIVE_NAMES`),一刀切会误伤放行方向。
    dangerous = (set(rs.DANGEROUS_MODULE_NAMES) - {"builtins"}) | set(rs.UNREACHABLE_ORIGINS)
    assert "concurrent" in dangerous, "concurrent 必须在危险来源集合里(第 40 轮实测漏过)"
    rs.safe_builtins(())
    leaked = []
    for name in ("vnpy.trader.database", "vnpy.trader.datafeed", "vnpy.trader.engine", "vnpy.trader.utility",
                 "vnpy.trader.logger", "vnpy.trader.app", "vnpy.event.engine", "vnpy.trader.optimize",
                 "vnpy_ctastrategy", "vnpy_ctastrategy.engine", "vnpy_ctastrategy.locale"):
        module = importlib.import_module(name)
        for member, value in vars(module).items():
            if member.startswith("_") and member != "__import__":
                continue
            origin = value.__name__ if isinstance(value, types.ModuleType) else (getattr(value, "__module__", "") or "")
            if isinstance(origin, str) and (origin.split(".")[0] in dangerous
                                           or (origin == "builtins" and member in rs.BUILTIN_PRIMITIVE_NAMES)):
                try:
                    rs._check_object_rules(module, member, value)
                    leaked.append(f"{name}.{member} <- {origin}")
                except AttributeError:
                    pass
    assert leaked == [], leaked


@requires_trading_stack
def test_thread_and_process_executors_reexported_by_whitelisted_modules_are_refused():
    """第 40 轮(实测):候选 `from vnpy.trader.optimize import ProcessPoolExecutor` **实测拿到**——
    对象层的"来源"判定此前只查 `UNREACHABLE_ORIGINS` 一个集合,而"能起线程/进程"的来源在
    `DANGEROUS_MODULE_NAMES` 里,`concurrent` 就这么漏了。同一族第三次漏(importlib → multiprocessing → concurrent),
    所以判据改成派生而不是补名字;这里钉住**规则名**("按来源不可达"),并覆盖另两个同源成员
    (`vnpy_ctastrategy.engine` 还再导出了 `ThreadPoolExecutor` 与 `Future`,codex 只报了第一个)。"""
    import importlib
    rs.safe_builtins(())
    cases = [
        ("vnpy.trader.optimize", "ProcessPoolExecutor"),
        ("vnpy_ctastrategy.engine", "ThreadPoolExecutor"),
        ("vnpy_ctastrategy.engine", "Future"),
    ]
    for module_name, member in cases:
        module = importlib.import_module(module_name)
        value = getattr(module, member)
        assert value is not None, f"{module_name}.{member} 不存在,本用例的前提变了"
        with pytest.raises(AttributeError, match="规则:按来源不可达"):
            rs._check_object_rules(module, member, value)
    # 放行方向:同一个白名单模块里正常模板要用的东西照旧可达
    assert rs._check_object_rules is not None
    assert rs._proxy_for(importlib.import_module("vnpy.trader.utility")).ArrayManager is not None
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_capability_library_classes_reexported_by_whitelisted_modules_are_refused():
    """第 40 轮(实测):"函数级能力库未授权"此前**只查模块**,类不在射程内——
    `from vnpy_ctastrategy.backtesting import DataFrame` 照样拿到 `pandas.core.frame.DataFrame`
    (pandas 是大攻击面:`to_csv`/`to_pickle` 是任意写,`DataFrame.eval` 能用 pandas 的真实 globals 求表达式)。
    这里钉住规则名("函数级能力库未授权"),并覆盖同模块的另外两个类。"""
    import importlib
    rs.safe_builtins(())
    for member in ("DataFrame", "Series", "ExponentialMovingWindow"):
        module = importlib.import_module("vnpy_ctastrategy.backtesting")
        value = getattr(module, member)
        assert value is not None, f"{member} 不存在,本用例的前提变了"
        with pytest.raises(AttributeError, match="规则:函数级能力库未授权"):
            rs._check_object_rules(module, member, value)
    # 放行方向:模板真正要用的类不受影响
    from vnpy.trader.object import BarData
    assert rs._check_object_rules(importlib.import_module("vnpy.trader.object"), "BarData", BarData) is None
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_production_path_refuses_import_module_and_leaves_framework_modules_untouched(repo, tmp_path, monkeypatch):
    """端到端(生产 import_candidate,静态筛查开):候选经 `import_module` 复活 research_grid 并改 `validate_grid`
    ——对象层规则拒,框架模块对象未变(第 33 轮"格子上限失效 → 试验少记"不能原样复活)。"""
    import research_grid
    original = research_grid.validate_grid
    source = """
        RESULT = {}
        try:
            import vnpy.trader.database as db
            grid = db.import_module("research_grid")
            grid.validate_grid = lambda *a, **k: None
            RESULT["patched"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        try:
            from vnpy.trader.datafeed import import_module as im2
            RESULT["from_import"] = True
        except Exception as exc:
            RESULT["err2"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source, static=True)
    assert "patched" not in cls.RESULT and "from_import" not in cls.RESULT, cls.RESULT
    assert "规则:能力泄漏" in cls.RESULT["err"], cls.RESULT            # 对象层的"能力泄漏"规则拒的(不是静态规则)
    assert "err2" in cls.RESULT
    assert research_grid.validate_grid is original


# ---- 没有 open 事件的写原语 ----

def test_sqlite3_connects_are_denied_outside_the_allowed_directory(tmp_path):
    """sqlite3 走的是 **C 层**的写,不发 `open` 事件 ⇒ 靠**自己的**审计事件判(平台无关)。

    第 48 轮拆出来的**平台无关**那一半:`sqlite3.connect` 的审计事件在所有平台上都会发,
    所以这条规则在 Linux CI 上**真跑**(第 47 轮之后 CI 第一次跑这份套件时,同族的 dbm 用例红了)。
    """
    import sqlite3
    with rs.candidate_window():
        with pytest.raises(OSError) as info:
            sqlite3.connect(str(tmp_path / "x.db"))
        chain_rules, exc = [], info.value
        while exc is not None:
            if isinstance(exc, rs.AuditDenied):
                chain_rules.append(exc.rule)
            exc = exc.__cause__ or exc.__context__
        assert chain_rules and set(chain_rules) <= {"audit:sqlite3-connect"}, chain_rules
    assert [p.name for p in tmp_path.iterdir()] == []


@pytest.mark.skipif(os.name != "nt", reason="POSIX 上 dbm 的默认后端是 C 实现(ndbm/gdbm),"
                                            "写入在 C 层发生、不发 Python 审计事件 ⇒ 这条规则在 POSIX 上"
                                            "不生效(真实缺口已登记为 Issue #51);Windows 上默认后端是纯 Python "
                                            "的 dbm.dumb ⇒ 审计事件会触发")
def test_sqlite3_backed_dbm_and_shelve_are_denied_outside_the_allowed_directory(tmp_path):
    """dbm/shelve 的写最终落到 `open`(纯 Python 后端)⇒ 被 `audit:open-write` 拒。

    **平台边界(第 48 轮实测,如实登记)**:Windows 上 dbm 的默认后端是 `dbm.dumb`(**纯 Python**)⇒
    写文件会发 `open` 审计事件 ⇒ 这条规则**生效**;POSIX 上默认后端是 C 实现(`dbm.ndbm`/`dbm.gnu`)⇒
    写入在 C 层发生,**不发** Python 审计事件 ⇒ 规则**不生效**(候选能在窗口外留下 dbm 文件)。
    这是沙盒的**真实缺口**,不是用例写错——已登记为 Issue #51,本条在 POSIX 上按平台跳过。
    """
    import dbm
    import shelve
    with rs.candidate_window():
        for attack in (lambda: shelve.open(str(tmp_path / "s")), lambda: dbm.open(str(tmp_path / "d"), "c")):
            with pytest.raises(OSError) as info:
                attack()
            chain_rules, exc = [], info.value
            while exc is not None:
                if isinstance(exc, rs.AuditDenied):
                    chain_rules.append(exc.rule)
                exc = exc.__cause__ or exc.__context__
            assert chain_rules and set(chain_rules) <= {"audit:open-write", "audit:sqlite3-connect",
                                                         "audit:path-mutation"}, chain_rules
    assert [p.name for p in tmp_path.iterdir()] == []


@pytest.mark.skipif(os.name != "nt", reason="_winapi 只在 Windows 上")
def test_winapi_createfile_with_write_access_is_judged_by_path(tmp_path):
    import _winapi
    existing = tmp_path / "r.bin"
    existing.write_bytes(b"x")
    with rs.candidate_window() as scratch:
        GENERIC_READ, GENERIC_WRITE, CREATE_ALWAYS, OPEN_EXISTING = 0x80000000, 0x40000000, 2, 3
        handle = _winapi.CreateFile(str(existing), GENERIC_READ, 0, 0, OPEN_EXISTING, 0x80, 0)     # 只读:放行
        _winapi.CloseHandle(handle)
        handle = _winapi.CreateFile(str(scratch / "w.bin"), GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0x80, 0)
        _winapi.CloseHandle(handle)                                                                  # 允许目录内写:放行
        with _expect_rule("audit:open-write"):
            _winapi.CreateFile(str(tmp_path / "w2.bin"), GENERIC_WRITE, 0, 0, CREATE_ALWAYS, 0x80, 0)
    assert not (tmp_path / "w2.bin").exists()


@pytest.mark.skipif(os.name != "nt", reason="winreg 只在 Windows 上")
def test_registry_writes_are_denied_inside_the_window():
    import winreg
    with contextlib.suppress(OSError):
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\zz_research_probe_should_not_exist")   # 清掉变异运行可能留下的
    with rs.candidate_window():
        with _expect_rule("audit:winreg.CreateKey"):
            winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\zz_research_probe_should_not_exist")
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\zz_research_probe_should_not_exist")


# ---- 账本锁:被占用 vs 真权限错误 / 未来 mtime ----

@pytest.mark.skipif(os.name != "nt", reason="Windows 上对目录做 O_EXCL 创建抛 PermissionError")
def test_file_lock_reports_a_real_permission_error_instead_of_waiting_the_full_timeout(tmp_path):
    import time
    target = tmp_path / "x.jsonl"
    (tmp_path / "x.jsonl.chainlock").mkdir()                               # 锁路径是目录:真权限错误,不是"被占用"
    started = time.monotonic()
    with pytest.raises(chain.LedgerChainError, match="权限错误") as info:
        with chain.file_lock(target, timeout=30):
            pass
    assert time.monotonic() - started < 5 and "权限错误,不是被其他进程占用" in str(info.value)


def test_file_lock_gives_up_on_a_persistent_permission_error_with_a_distinct_message(tmp_path, monkeypatch):
    import time

    def always_denied(path, flags, *a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(chain.os, "open", always_denied)
    started = time.monotonic()
    with pytest.raises(chain.LedgerChainError, match="权限错误"):
        with chain.file_lock(tmp_path / "y.jsonl", timeout=30, permission_grace=0.3):
            pass
    assert time.monotonic() - started < 5


def _write_a_foreign_holder_lock(target: Path, token: str = "foreign-token") -> subprocess.Popen:
    """假装**另一个活着的进程**持有着这把锁(真起一个子进程,取它的 PID)。

    第 42 轮:原先那条用例是**同一个进程**嵌套拿两次锁,却断言"被其他进程占用"——
    占用者其实就是本进程,那句话是错的。现在分成两种情形分别测。"""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lock = target.with_name(target.name + ".chainlock")
    lock.write_text(f"{proc.pid} {time.time():.3f} {token}\n", encoding="ascii")
    return proc


def test_a_lock_held_by_another_process_is_reported_as_busy(tmp_path):
    target = tmp_path / "z.jsonl"
    proc = _write_a_foreign_holder_lock(target)
    try:
        started = time.monotonic()
        with pytest.raises(chain.LedgerChainError, match="被其他进程占用"):
            with chain.file_lock(target, timeout=0.3):
                pass
        assert time.monotonic() - started < 5
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_same_thread_cannot_take_the_same_lock_twice(tmp_path):
    """**重入**(第 42 轮):**同一个线程**再拿同一把锁 ⇒ 立刻失败,而不是空转到超时。

    原先的行为:O_EXCL 见到自己刚建的锁文件 ⇒ 等满 timeout(批准路径 5 秒、默认 30 秒),
    最后报"被其他进程占用"——**占用者就是本进程**,那句话是错的。

    注意:判定是**线程级**的。同一进程的**另一个**线程拿这把锁是**真并发**(该等),
    见 `test_another_thread_of_the_same_process_waits_instead_of_being_called_reentrant`。"""
    target = tmp_path / "r.jsonl"
    with chain.file_lock(target):
        started = time.monotonic()
        with pytest.raises(chain.LedgerChainError, match="本进程同一线程"):
            with chain.file_lock(target, timeout=5):
                pass
        assert time.monotonic() - started < 2, "重入没有立刻失败,是在等满超时"


def test_another_thread_of_the_same_process_waits_instead_of_being_called_reentrant(tmp_path):
    """同进程的**另一个线程**拿同一把锁 ⇒ **等**,不是"重入"(第 42 轮第三版)。

    这条是上一条的**反向探针**:第一版把 token 定成每进程一个,于是别的线程
    在等锁时被误判成"重入"直接拒绝,把 6 线程并发批准打挂了
    (`tests/test_research_sandbox.py::test_concurrent_approvals_do_not_lose_entries`)。
    真正的重入只发生在**同一个线程**嵌套拿同一把锁。"""
    target = tmp_path / "t.jsonl"
    outcome: list[str] = []

    def worker() -> None:
        try:
            with chain.file_lock(target, timeout=10):
                outcome.append("拿到锁")
        except chain.LedgerChainError as exc:
            outcome.append(f"失败: {exc}")

    with chain.file_lock(target):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        # 别的线程此刻必须还在**等**(不能已经失败),也不能已经拿到锁
        assert outcome == [], f"另一个线程不该在主线程持锁期间就结束:{outcome}"
        assert thread.is_alive(), "另一个线程应该还在等锁"

    thread.join(timeout=15)
    assert outcome == ["拿到锁"], f"另一个线程应该等主线程放锁后拿到:{outcome}"
    assert not thread.is_alive()


def test_an_error_inside_the_approvals_lock_is_not_reported_as_occupancy(tmp_path):
    """临界区**内部**失败 ≠ "被其他进程占用"(第 42 轮)。

    原先 `except chain.LedgerChainError` 圈住整个临界区,于是里面抛的
    LedgerChainError 被翻译成"批准记录被其他进程占用,拒绝写入"——**那句话是错的**。
    现在只翻译"拿不到锁"这一种。"""
    with pytest.raises(chain.LedgerChainError, match="链断了"):
        with rs._approvals_lock(tmp_path / "approvals.json"):
            raise chain.LedgerChainError("链断了")


def test_a_reused_pid_is_not_mistaken_for_a_reentrant_lock(tmp_path):
    """PID 被复用时(锁文件里的 PID 恰好是我们、但 token 不是)⇒ **不算**重入。

    没有 token 时,系统把别人的残留锁认成自己的 ⇒ 报"本进程重入",把真故障说错。"""
    target = tmp_path / "p.jsonl"
    lock = target.with_name(target.name + ".chainlock")
    lock.write_text(f"{os.getpid()} {time.time():.3f} somebody-elses-token\n", encoding="ascii")
    started = time.monotonic()
    with pytest.raises(chain.LedgerChainError, match="被其他进程占用"):
        with chain.file_lock(target, timeout=0.3):
            pass
    assert time.monotonic() - started < 5


def test_a_lockfile_with_a_future_mtime_is_treated_as_stale(tmp_path):
    import time
    target = tmp_path / "f.jsonl"
    lock = tmp_path / "f.jsonl.chainlock"
    lock.write_bytes(b"")
    future = time.time() + 3600
    os.utime(lock, (future, future))
    started = time.monotonic()
    with chain.file_lock(target, timeout=3):                              # 否则会阻塞到超时
        pass
    assert time.monotonic() - started < 2


# ======================================================================
# 第 38 轮复审:活动数据库句柄不可达 / 回测窗口不登记 sqlite
# ======================================================================

def test_sqlite3_connect_is_judged_by_path_inside_the_window(tmp_path):
    """sqlite3 只发 `sqlite3.connect`,没有 `open` 事件。**新连接**按路径判:允许目录内放行、`:memory:` 放行、之外拒(文件没建出来)。
    窗口**不再**登记任何框架 sqlite 路径(第 38 轮:登记既不必要也不解决问题)。"""
    import inspect
    import sqlite3
    assert "sqlite_paths" not in inspect.signature(rs.candidate_window).parameters
    registered = tmp_path / "vnpy_database.db"
    registered.write_bytes(b"")
    outside = tmp_path / "outside.db"
    with rs.candidate_window() as scratch:
        con = sqlite3.connect(scratch / "inside.db")
        con.execute("create table t(x)")
        con.commit()
        con.close()
        sqlite3.connect(":memory:").close()
        with _expect_rule("audit:sqlite3-connect"):
            sqlite3.connect(str(outside))
        with _expect_rule("audit:sqlite3-connect"):
            sqlite3.connect(str(registered))                 # 即使是"框架的库"也没有特例
    assert not outside.exists()


def test_event_layer_cannot_judge_vacuum_into_on_an_open_connection(tmp_path):
    """如实钉住一个**限制**(第 38 轮,ADR 也写了):已打开连接上的 `ATTACH DATABASE` / `VACUUM INTO` 不发任何带路径的
    审计事件,钩子判不了——不要再往事件名单里补名字。修法只能在**能力层**(不把句柄给候选)或 sqlite 层(只读打开)。
    本用例在窗口内对一个**框架持有**的连接做 VACUUM INTO,证明钩子确实看不见;所以"句柄不可达"才是防线。"""
    import sqlite3
    con = sqlite3.connect(tmp_path / "framework.db")           # 窗口外打开(框架)
    con.execute("create table t(x)")
    con.commit()
    out = tmp_path / "vacuumed.db"
    with rs.candidate_window():
        con.execute("VACUUM INTO ?", (str(out),))
        assert rs.window_denials() == ()
    assert out.exists(), "若这条变红:说明钩子能看见 VACUUM INTO 了,请同步更新 ADR"
    con.close()


@requires_trading_stack
def test_run_child_opens_the_vnpy_database_before_the_window_and_registers_nothing():
    """行为测试(**新进程里**跑——peewee 的库对象是进程级单例,同进程里别的用例建过连接会让"先建连接"无从区分):
    回测入口在窗口之前建好 vnpy 数据库连接;窗口里 peewee 读库复用它(无新 connect 事件、无拒绝)。
    拿掉"先建连接"→ 窗口里 get_database() 首次连接被拒 → 本用例变红。"""
    script = textwrap.dedent(f"""
        import sys, json
        sys.path.insert(0, {str(ROOT / "scripts")!r}); sys.path.insert(0, {str(ROOT)!r})
        import bootstrap
        import research_api as api, research_sandbox as rs, backtest_lib
        import vnpy.trader.database as vdb
        from datetime import datetime
        from vnpy.trader.constant import Exchange, Interval
        seen = {{}}
        def fake_run_backtest(*a, **k):
            seen["active"] = rs._AUDIT.active
            db = vdb.get_database()
            seen["bars"] = len(db.load_bar_data("ZZNODATA", Exchange.DCE, Interval.DAILY, datetime(2020, 1, 1), datetime(2020, 2, 1)))
            seen["denials"] = [list(d) for d in rs.window_denials()]
            return {{}}, None, []
        api.child_file = lambda p: p
        api.child_approved = lambda sha: None
        api.child_params = lambda a: {{}}
        api.import_candidate = lambda *a, **k: type("S", (), {{}})
        backtest_lib.run_backtest = fake_run_backtest
        rs.round_trip_pnls = lambda *a, **k: []
        api.backtest_stats_row = lambda s: {{}}
        args = api.build_parser().parse_args(["_child", "--file", "x.py", "--cls", "S", "--key", "CADJ",
                                              "--segment", "train", "--params", "{{}}", "--sha256", "0" * 64])
        code = api.run_child(args)
        print("SEEN" + json.dumps({{"code": code, **seen}}))
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, cwd=str(ROOT))
    line = [x for x in proc.stdout.splitlines() if x.startswith("SEEN")]
    assert line, (proc.stdout[-500:], proc.stderr[-500:])
    seen = json.loads(line[-1][4:])
    assert seen.get("active") is True and seen.get("bars") == 0 and seen.get("denials") == [], (seen, proc.stdout[-600:])


@requires_trading_stack
def test_the_candidate_cannot_obtain_the_database_handle_through_any_reexport():
    """对象层只判**模块成员**,不判函数返回值/实例属性:所以活动句柄必须在"拿到函数/实例"这一步拒。"""
    import types
    import vnpy.trader.database as vdb
    import vnpy_ctastrategy.backtesting as bt
    import vnpy_ctastrategy.engine as eng
    vdb.get_database()                                             # 让模块级 `database` 变成带 `.db` 句柄的实例
    rs.safe_builtins(())
    for module, name, rule in ((vdb, "get_database", "任意路径读写原语"),
                               (vdb, "database", "任意路径读写原语"),
                               (bt, "get_database", "规则:返回活动句柄的函数"),
                               (eng, "get_database", "规则:返回活动句柄的函数")):
        with pytest.raises(AttributeError, match=rule):
            rs._check_object_rules(module, name, getattr(module, name))
    handle = vdb.database.db
    assert handle.__class__.__module__.split(".")[0] in ("peewee", "playhouse")
    carrier = types.ModuleType("carrier_db")
    carrier.handle = handle
    with pytest.raises(AttributeError, match="规则:按来源不可达"):
        rs._check_object_rules(carrier, "handle", handle)
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_production_path_candidate_cannot_vacuum_into_through_the_framework_database(repo, tmp_path, monkeypatch):
    """端到端(生产 import_candidate,静态筛查开):候选经 `get_database().db.execute_sql("VACUUM INTO …")`
    想在允许目录之外落盘——拿不到句柄,文件没出现。放行方向:同一候选里模板正常成员照常可用。"""
    from vnpy.trader.database import get_database
    get_database()                                                  # 框架先把连接建好(与回测入口同)
    out = tmp_path / "attached_out.db"
    attack = f"""
        try:
            {{imp}}
            handle = g() if callable(g) else g
            handle.db.execute_sql("VACUUM INTO ?", ({str(out)!r},))
            RESULT[{{label!r}}] = "ATTACKED"
        except Exception as exc:
            RESULT[{{label!r}}] = "refused:" + str(exc)
    """
    imports = {
        "backtesting": "from vnpy_ctastrategy.backtesting import get_database as g",
        "engine": "from vnpy_ctastrategy.engine import get_database as g",
        "database.get_database": "from vnpy.trader.database import get_database as g",
        "database.database": "from vnpy.trader.database import database as g",
    }
    blocks = "\n".join(textwrap.dedent(attack.format(imp=imp, label=label)) for label, imp in imports.items())
    source = ("RESULT = {}\n" + blocks + "\n"
              "from vnpy.trader.utility import ArrayManager, BarGenerator\n"
              "from vnpy_ctastrategy import CtaTemplate\n"
              "class S(CtaTemplate):\n    pass\n"
              "S.RESULT = RESULT\nS.OK = (ArrayManager, BarGenerator)\n")
    cls = run_candidate(tmp_path, monkeypatch, source, static=True)
    assert set(cls.RESULT) == set(imports)
    for label, outcome in cls.RESULT.items():
        # 断言**是哪条规则**拒的,不是"红了就行"(第 39 轮 High:对象层的 AttributeError 被 IMPORT_FROM 回退掩盖,
        # 候选只看到无关的提示——所以受限导入器把对象层的拒绝转成带规则名的 ImportError)。
        assert outcome.startswith("refused:"), (label, outcome)
        assert "规则:" in outcome, (label, outcome)
    assert not out.exists()
    assert [x.__name__ for x in cls.OK] == ["ArrayManager", "BarGenerator"]       # 放行方向


@requires_trading_stack
def test_systematic_handle_scan_every_unblocked_handle_returning_member_is_reviewed():
    """第 38 轮的系统性扫描(可重复):白名单模块里"调用后返回活动句柄/引擎/连接"的公开函数、以及带 db/connection/cursor/execute*
    的模块级实例。判据与 `%TEMP%\\\\scan_handles.py` 同。**每个未被对象层挡住的命中必须出现在下面的已审查清单里**——
    vnpy 升级带来新的再导出时,本用例变红,逼着人去审(这是"库自带重导出随升级变化"的重跑机制)。"""
    import importlib
    import inspect
    import pkgutil
    import re
    import types
    from vnpy.trader.database import get_database
    get_database()
    name_re = re.compile(r"^(get|create|open|connect|make|new|init)_|^(connect|database|engine)$", re.I)
    ann_re = re.compile(r"Database|Engine|Connection|Cursor|Session|Handle|Socket|IO\b|File|Thread|Process|Manager|Client",
                        re.I)
    handle_attrs = ("db", "connection", "cursor", "execute", "execute_sql", "conn")
    reviewed_harmless = {   # 成员 -> 为什么无害(纯计算/字符串/绘图布局,不持有句柄)
        "get_digits": "纯计算", "get_localzone_name": "返回时区名字符串", "get_target_value": "回测里取指标数值",
        "make_subplots": "plotly 布局对象(plotly 不可 import)",
    }
    modules = {}
    for root in ("vnpy.trader", "vnpy.event", "vnpy_ctastrategy"):
        pkg = importlib.import_module(root)
        modules[root] = pkg
        for info in pkgutil.walk_packages(getattr(pkg, "__path__", []), root + "."):
            if any(x in info.name for x in (".gui", ".ui", "__main__", ".test", "ctp", "wechat")):
                continue
            try:
                modules[info.name] = importlib.import_module(info.name)
            except BaseException:       # noqa: BLE001 —— 导入不了的可选子模块(缺依赖)不在候选可达范围
                pass
    rs.safe_builtins(())
    unreviewed = []
    for mname, mod in modules.items():
        for name, val in vars(mod).items():
            if name.startswith("_"):
                continue
            flagged = False
            if inspect.isfunction(val) or inspect.isbuiltin(val):
                try:
                    annotation = str(inspect.signature(val).return_annotation)
                except (TypeError, ValueError):
                    annotation = ""
                flagged = bool(name_re.search(name)) or bool(annotation and ann_re.search(annotation))
            elif not isinstance(val, (type, types.ModuleType, str, int, float, bool, bytes, tuple, list, dict, set,
                                      frozenset, type(None))):
                origin = (getattr(type(val), "__module__", "") or "").split(".")[0]
                if origin not in ("builtins", "datetime", "enum", "typing", "pathlib", "zoneinfo", "collections", "re"):
                    flagged = any(a in dir(val) for a in handle_attrs) or bool(name_re.search(name))
            if not flagged:
                continue
            try:
                rs._check_object_rules(mod, name, val)
            except AttributeError:
                continue
            if name not in reviewed_harmless:
                unreviewed.append(f"{mname}.{name}")
    assert unreviewed == [], f"新的、未审查的句柄型成员(请审查后拦截或登记为无害): {unreviewed}"
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_backtest_reads_market_data_before_the_candidate_is_constructed(monkeypatch):
    """③ 回测入口:`engine.load_data()` 在 `add_strategy`(候选 `__init__` 在此跑)**之前**——
    与 gate/evaluate 入口"先取定行情、再让候选运行"同一模式。改回旧顺序 → 本用例变红。"""
    import backtest_lib
    from vnpy_ctastrategy.backtesting import BacktestingEngine
    order = []

    def fake_load(self):
        order.append("load_data")
        self.history_data = [object()]

    class Stop(Exception):
        pass

    def fake_add(self, strategy_class, setting):
        order.append("add_strategy")
        raise Stop()

    monkeypatch.setattr(BacktestingEngine, "load_data", fake_load)
    monkeypatch.setattr(BacktestingEngine, "add_strategy", fake_add)
    start, end = rs.segment_window("CADJ", "train")
    with pytest.raises(Stop):
        backtest_lib.run_backtest(type("S", (), {}), {}, "CADJ", start, end)
    assert order == ["load_data", "add_strategy"]


# ======================================================================
# 第 39 轮复审:库读原语按模式收口 / 对象层拒绝要带规则名
# ======================================================================

def test_the_data_primitive_pattern_covers_read_and_write_primitives():
    """收口按**模式**,不是列两个名字。覆盖读与写两侧的数据语义。"""
    pattern = rs.DATA_PRIMITIVE_PATTERN
    for name in ("load_bar_data", "load_tick_data", "save_bar_data", "delete_bar_data",
                 "get_order_data", "set_account_data", "load_data", "get_position"):
        assert pattern.match(name), name
    for name in ("load_bars_csv", "get_datafeed", "get_target_value", "get_digits",
                 "get_pricetick", "ArrayManager", "BarGenerator", "round_to"):
        assert not pattern.match(name), name
    # `load_bar` 命中模式(它就是"读 bar 数据"),但它只是**类成员**(模板自己的方法):
    # 规则只作用于**模块级**成员,模板方法不受影响(另有用例钉住这一点)。


@requires_trading_stack
def test_the_data_primitive_rule_applies_to_module_members_only_not_to_template_methods():
    """`CtaTemplate.load_bar` 是**类成员**(模板自己的方法),不受这条规则约束——规则只管模块级成员。
    模板方法被拦会误伤正常候选(它们本来就在继承 CtaTemplate)。"""
    import vnpy_ctastrategy.template as tpl
    rs.safe_builtins(())
    with pytest.raises(AttributeError, match="规则:数据读写原语"):
        rs._check_object_rules(tpl, "load_bar_data", getattr(tpl, "CtaTemplate"))
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_library_read_primitives_are_refused_by_name_and_the_rule_is_named():
    """`vnpy_ctastrategy.backtesting.load_bar_data` 能读任意区间(含测试段);按模式收口,断言规则名。"""
    import vnpy_ctastrategy.backtesting as bt
    rs.safe_builtins(())
    for name in ("load_bar_data", "load_tick_data"):
        with pytest.raises(AttributeError, match="规则:数据读写原语"):
            rs._check_object_rules(bt, name, getattr(bt, name))
    rs._PROXY_CACHE.clear()


@requires_trading_stack
def test_the_candidate_cannot_read_the_database_through_any_reexport(repo, tmp_path, monkeypatch):
    """生产入口:候选拿不到任何库读原语(`load_bar_data` 的再导出 + `get_database`)。断言规则名。"""
    source = """
        RESULT = {}
        try:
            from vnpy_ctastrategy.backtesting import load_bar_data as g
            RESULT["bt.load_bar_data"] = "REACHABLE"
        except Exception as exc:
            RESULT["bt.load_bar_data"] = "refused:" + str(exc)
        try:
            from vnpy_ctastrategy.backtesting import BaseDatabase as g
            RESULT["bt.BaseDatabase"] = "REACHABLE"
        except Exception as exc:
            RESULT["bt.BaseDatabase"] = "refused:" + str(exc)
        try:
            from vnpy_ctastrategy.engine import get_database as g
            RESULT["engine.get_database"] = "REACHABLE"
        except Exception as exc:
            RESULT["engine.get_database"] = "refused:" + str(exc)
        try:
            from vnpy.trader.database import get_database as g
            RESULT["db.get_database"] = "REACHABLE"
        except Exception as exc:
            RESULT["db.get_database"] = "refused:" + str(exc)
        from vnpy.trader.utility import ArrayManager, BarGenerator, round_to
        from vnpy.trader.object import BarData
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
        S.OK = (ArrayManager, BarGenerator, round_to, BarData)
    """
    cls = run_candidate(tmp_path, monkeypatch, source, static=True)
    for label, outcome in cls.RESULT.items():
        assert outcome.startswith("refused:"), (label, outcome)
        assert "规则:" in outcome, (label, outcome)
    assert [x.__name__ for x in cls.OK] == ["ArrayManager", "BarGenerator", "round_to", "BarData"]


@requires_trading_stack
def test_the_candidate_cannot_read_test_segment_bars_through_the_database(tmp_path):
    """回测路径:候选在窗口内拿不到库读原语(对象层按模式收口),也建不了允许目录外的库。"""
    import sqlite3
    import vnpy_ctastrategy.backtesting as bt
    with rs.candidate_window():
        with pytest.raises(AttributeError, match="规则:数据读写原语"):
            rs._check_object_rules(bt, "load_bar_data", bt.load_bar_data)
        with _expect_rule("audit:sqlite3-connect"):
            sqlite3.connect(str(tmp_path / "evil.db"))
    assert not (tmp_path / "evil.db").exists()


def test_the_framework_sqlite_exemption_is_narrow_and_does_not_allow_writes(tmp_path):
    """框架读库的豁免**只**豁免 `sqlite3.connect` 到那个库文件;写那个文件仍然被拒。"""
    import sqlite3
    db_file = tmp_path / "vnpy.db"
    db_file.write_bytes(b"")
    with rs.candidate_window():
        with rs.framework_exempt(sqlite_paths=[db_file]):
            sqlite3.connect(str(db_file)).close()                  # connect 到框架的库:放行
            with _expect_rule("audit:open-write"):
                open(db_file, "w")
            with _expect_rule("audit:sqlite3-connect"):
                sqlite3.connect(str(tmp_path / "other.db"))         # 别的库:照拒
    assert not (tmp_path / "other.db").exists()


def test_the_object_layer_refusal_carries_its_rule_name():
    """对象层的拒绝必须**带规则名**:CPython 的 IMPORT_FROM 在 AttributeError 时回退到 sys.modules,
    候选于是只看到无关提示(第 39 轮 High)。所以对象层抛的异常里必须有规则名,受限导入器据此转 ImportError。"""
    import types
    import ctypes
    carrier = types.ModuleType("carrier_rule")
    carrier.leaky = ctypes.CDLL                      # 能力泄漏(ctypes/subprocess 来源)
    monkey = types.ModuleType("carrier_data")
    monkey.load_bar_data = lambda *a, **k: None     # 数据读写原语(名字匹配)
    rs.safe_builtins(())
    proxy = rs._proxy_for(carrier)
    with pytest.raises(AttributeError, match="规则:能力泄漏"):
        proxy.leaky
    with pytest.raises(AttributeError, match="规则:数据读写原语"):
        rs._check_object_rules(monkey, "load_bar_data", monkey.load_bar_data)
    rs._PROXY_CACHE.clear()


def test_the_systematic_scan_also_covers_data_read_primitives():
    """扫描判据扩到"读/写数据原语":白名单模块里名字匹配数据原语模式的**模块级**成员必须都被收口。
    升级 vnpy 后若出现新的读原语,本用例变红(判据与 `%TEMP%\\\\scan_dataprims.py` 同)。"""
    import importlib
    import inspect
    import pkgutil
    modules = {}
    for root in ("vnpy", "vnpy_ctastrategy", "strategies"):
        try:
            pkg = importlib.import_module(root)
        except BaseException:       # noqa: BLE001
            continue
        modules[root] = pkg
        for info in pkgutil.walk_packages(getattr(pkg, "__path__", []), root + "."):
            if any(x in info.name for x in (".gui", ".ui", "__main__", ".test", "ctp", "wechat")):
                continue
            try:
                modules[info.name] = importlib.import_module(info.name)
            except BaseException:   # noqa: BLE001 —— 导入不了的可选子模块不在候选可达范围
                pass
    rs.safe_builtins(())
    leaked = []
    for mname, mod in modules.items():
        for name, value in list(vars(mod).items()):
            if name.startswith("_") or not rs.DATA_PRIMITIVE_PATTERN.match(name):
                continue
            if not (inspect.isfunction(value) or inspect.isbuiltin(value) or inspect.ismethod(value)):
                continue
            try:
                rs._check_object_rules(mod, name, value)
                leaked.append(f"{mname}.{name}")
            except AttributeError:
                pass
    assert leaked == [], f"白名单模块里还有未收口的数据读写原语: {leaked}"
    rs._PROXY_CACHE.clear()
# ======================================================================
# 第 40 轮:前向引用求值一族(把真内建递给候选)
#
# 根因(生产入口 `import_candidate` 实测,不是推理):
#   `typing.ForwardRef` 存着候选给的**字符串注解**。一旦有人调
#   `fr._evaluate({}, None, recursive_guard=set())`,`eval` 就在**空的 globalns** 里跑——
#   CPython 会把**真内建**注入那个空字典,于是真 `__import__` → 真 `os` → 真环境变量。
#   实测把真 `PATH` 的值读了出来,并经异常消息带了出来。
#
# 这一族**静态层看不见载荷**:它藏在字符串字面量里(`"__import__('os').environ['PATH']"`)。
# 静态层能做的只有"候选必须写出 `_evaluate`/`_eval_type` 才能调它"
# (getattr / type / __dict__ / __getattribute__ 一律已被禁),所以按名字拒。
# 事件层是第二道:按"谁触发的这次 exec"判(帧表实测,见 `FORBIDDEN_EVAL_FRAMES`)。
# 对象层是第三道:`typing.get_type_hints` 这类"按名求值的执行器"早已拒。
#
# 双向:每条用例都断言**是哪条规则**拒的(第 22 轮教训:规则失灵时用例照样绿),
# 并且都有对应的放行方向(正常 typing 用法、库自己 eval 自己生成的代码必须照旧跑通)。
# ======================================================================

# 载荷刻意放进字符串字面量:静态层看不见它,只有运行期才暴露。
# 读的是**本次用例自己设的环境变量**:断言"这个标记一个字都没漏出去"是确定性的,
# 不用去比对 PATH(太长,截断后就看不出来,断言会失真——第 40 轮自查时踩过)。
_MARKER_ENV = "BISHENG_ROUND40_MARKER"
_PAYLOAD = f"__import__('os').environ[{_MARKER_ENV!r}]"
_MARKER_VALUE = "BISHENG-ROUND40-MARKER-8f3a1c"


@pytest.fixture()
def marker_env(monkeypatch):
    """本节用例的载荷读的是这个环境变量:统一设好——
    免得某个用例"恰好"因为变量没设而看起来没泄漏(假绿)。"""
    monkeypatch.setenv(_MARKER_ENV, _MARKER_VALUE)
    return _MARKER_VALUE


@requires_trading_stack
def test_forward_ref_evaluate_is_denied_by_the_audit_hook(repo, tmp_path, monkeypatch, marker_env):
    """拦截方向:候选调 `ForwardRef._evaluate` 求值自己给的字符串 → 事件层拒,规则名 `audit:forward-ref-eval`。"""
    source = f"""
        import typing
        RESULT = {{}}
        try:
            fr = typing.ForwardRef({_PAYLOAD!r})
            fr._evaluate({{}}, None, recursive_guard=set())
            RESULT["leaked"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "leaked" not in cls.RESULT, "候选求值成功了——真内建已经递到它手上"
    assert "audit:forward-ref-eval" in cls.RESULT["err"], cls.RESULT
    assert ("audit:forward-ref-eval", "exec") in rs.window_denials(), rs.window_denials()


@requires_trading_stack
def test_the_optional_get_args_route_cannot_reach_the_evaluator(repo, tmp_path, monkeypatch, marker_env):
    """同一条路换个入口:不必出现 `ForwardRef` 这个名字(`Optional["…"]` → `get_args` 也能拿到实例)照样拒。"""
    source = f"""
        import typing
        RESULT = {{}}
        try:
            fr = typing.get_args(typing.Optional[{_PAYLOAD!r}])[0]
            fr._evaluate({{}}, None, recursive_guard=set())
            RESULT["leaked"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "leaked" not in cls.RESULT, cls.RESULT
    assert "audit:forward-ref-eval" in cls.RESULT["err"], cls.RESULT


@requires_trading_stack
def test_the_candidate_cannot_read_the_process_environment(repo, tmp_path, monkeypatch, marker_env):
    """端到端:这条向量修好之后,候选读不到**真环境变量**——标记值一个字都不许漏出去。"""
    marker = marker_env
    source = f"""
        import typing
        RESULT = {{}}
        try:
            fr = typing.ForwardRef({_PAYLOAD!r})
            fr._evaluate({{}}, None, recursive_guard=set())
            RESULT["leaked"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)[:200]
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    blob = str(cls.RESULT)
    assert "leaked" not in cls.RESULT, blob
    assert marker not in blob, f"真环境变量的值漏进了候选的输出: {blob[:200]}"


def test_forward_ref_evaluate_is_refused_by_the_static_screen(repo, tmp_path, monkeypatch):
    """静态层:候选必须写出这两个名字才能调它,所以在筛查阶段就拒(口径与运行期一致)。"""
    cases = (
        (f'import typing\nfr = typing.ForwardRef({_PAYLOAD!r})\nfr._evaluate({{}}, None, recursive_guard=set())\n',
         "禁止访问 _evaluate"),
        ('import typing\nfr = typing.ForwardRef("int")\ntyping._eval_type(fr, {}, None)\n',
         "禁止访问 _eval_type"),
    )
    for snippet, needle in cases:
        source = snippet + "from vnpy_ctastrategy import CtaTemplate\nclass S(CtaTemplate):\n    pass\n"
        with pytest.raises(api.JobError) as info:
            run_candidate(tmp_path, monkeypatch, source, static=True)
        assert needle in info.value.message, info.value.message


@requires_trading_stack
def test_typing_eval_type_is_refused_by_the_module_proxy_at_runtime(repo, tmp_path, monkeypatch, marker_env):
    """运行期的名字层:`typing._eval_type` 是下划线私有名,模块代理取不到(与静态层口径一致)。"""
    source = f"""
        import typing
        RESULT = {{}}
        try:
            typing._eval_type(typing.ForwardRef({_PAYLOAD!r}), {{}}, None)
            RESULT["leaked"] = True
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "leaked" not in cls.RESULT, cls.RESULT
    assert "_eval_type" in cls.RESULT["err"], cls.RESULT


@requires_trading_stack
def test_legitimate_typing_usage_still_works(repo, tmp_path, monkeypatch):
    """放行方向:正常 typing 用法不受影响(事件层只拦"替候选求值候选给的字符串"的那种 exec)。"""
    source = """
        import typing
        import collections
        import enum
        RESULT = {}
        RESULT["optional"] = str(typing.Optional["int"])
        RESULT["get_args"] = str(typing.get_args(typing.Optional[int]))
        RESULT["forward_ref"] = str(typing.ForwardRef("int"))
        RESULT["namedtuple_typing"] = typing.NamedTuple("P", [("a", int)])(1).a
        RESULT["namedtuple_std"] = collections.namedtuple("Q", ["b"])(2).b
        RESULT["enum"] = enum.Enum("E", ["A", "B"]).A.value
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "Optional[ForwardRef('int')]" in cls.RESULT["optional"], cls.RESULT
    assert cls.RESULT["get_args"] == "(<class 'int'>, <class 'NoneType'>)", cls.RESULT
    assert cls.RESULT["forward_ref"] == "ForwardRef('int')", cls.RESULT
    assert cls.RESULT["namedtuple_typing"] == 1 and cls.RESULT["namedtuple_std"] == 2, cls.RESULT
    assert cls.RESULT["enum"] == 1, cls.RESULT
    assert rs.window_denials() == (), rs.window_denials()


@requires_trading_stack
def test_the_library_still_evaluates_its_own_generated_code(repo, tmp_path, monkeypatch):
    """反向对照:库自己 eval **自己生成**的代码(namedtuple/dataclass)必须照旧跑通——
    事件层的判据若太宽,会把正常候选整片拒掉。"""
    source = """
        import collections
        import dataclasses
        RESULT = {}
        RESULT["nt"] = collections.namedtuple("P", ["a"])(1).a
        RESULT["dc"] = dataclasses.make_dataclass("C", [("a", int)])(1).a
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert cls.RESULT["nt"] == 1 and cls.RESULT["dc"] == 1, cls.RESULT
    assert rs.window_denials() == (), rs.window_denials()


def test_the_string_module_is_refused_even_when_the_config_adds_it_to_the_allowlist(repo, tmp_path, monkeypatch):
    """`string` 的 `Formatter.get_field` 能顺着"属性名"摸到真内建 → 真框架模块 → 关掉审计钩子。
    配置把它追加进白名单也不能放行(黑名单先判)。"""
    safe = rs.safe_builtins((), ("string",))
    with pytest.raises(ImportError):
        safe["__import__"]("string")
    source = """
        import string
        RESULT = {"r": "放行了"}
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    with pytest.raises(api.CandidateError) as info:
        run_candidate(tmp_path, monkeypatch, source, allowed=("numpy", "pandas", "string"))
    assert "string" in info.value.detail["message"], info.value.detail


def test_vnpy_alpha_is_refused_by_the_static_screen(repo, tmp_path, monkeypatch):
    """`vnpy.alpha` 里有一处拿调用方给的表达式去 eval 的代码(潜伏风险):静态层按**全名**禁。"""
    findings = rs.screen_source("import vnpy.alpha\n")
    assert any("禁止导入 vnpy.alpha" in f for f in findings), findings
    source = """
        import vnpy.alpha
        RESULT = {"r": "放行了"}
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    with pytest.raises(api.JobError) as info:
        run_candidate(tmp_path, monkeypatch, source, static=True)
    assert "禁止导入 vnpy.alpha" in info.value.message, info.value.message


@requires_trading_stack
def test_get_type_hints_is_refused_with_its_rule_name(repo, tmp_path, monkeypatch):
    """对象层:`typing.get_type_hints` 按名求值候选给的表达式 → 规则名 `按名求值的执行器`。"""
    source = """
        import typing
        RESULT = {}
        try:
            RESULT["r"] = str(typing.get_type_hints(dict))
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "按名求值的执行器" in cls.RESULT["err"], cls.RESULT
    assert "get_type_hints" in cls.RESULT["err"], cls.RESULT


def test_the_forbidden_eval_frames_are_registered_in_the_constitution_guard():
    """新增的求值帧名单必须登记进历史守卫:没登记 = 没人守(第 25 轮 codex 实测过)。"""
    guard = (ROOT / "tools" / "check_constitution_history.py").read_text(encoding="utf-8")
    assert "FORBIDDEN_EVAL_FRAMES" in guard, "求值帧名单没有登记进宪法历史守卫"
    assert "typing.ForwardRef._evaluate" in rs.FORBIDDEN_EVAL_FRAMES, rs.FORBIDDEN_EVAL_FRAMES


# ======================================================================
# 第 43 轮(codex 第二轮的 4 条 must-close + 几条便宜的补强)
# ======================================================================

# ---------- H2:账本被删了行,重置不许悄悄把 N 变小 ----------

def _trials_ledger(tmp_path, counts=(1, 2, 3)):
    path = tmp_path / "data" / "research" / "trials.jsonl"
    for index, count in enumerate(counts):
        tl.record_trials(path, "submission", count, at=float(index))
    return path


def _truncate_to_first_row(path):
    """把账本截到只剩第一行(链仍然自洽——这是最阴的一种删法)。"""
    rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text(rows[0] + "\n", encoding="utf-8")


def test_a_truncated_ledger_cannot_be_reset_into_a_silently_smaller_n(tmp_path):
    """删行后重置必须**先说清楚丢了多少次**,不许悄悄把 N 变小(第 43 轮,codex 的 H2)。

    此前(第 40 轮)的"防变小"基准是"文件当前自称的累计次数"——行被删了,基准也跟着变小,
    于是 60 → 10 这种缩小**完全检不出来**,而且重置记录还会把 before/after 都写成 10,
    把丢失**洗白**。现在基准含**锚点记着的累计次数**,丢行 ⇒ 基准不变 ⇒ 必须显式确认。"""
    path = _trials_ledger(tmp_path, counts=(10, 20, 30))
    assert tl.total_trials(path) == 60
    _truncate_to_first_row(path)

    with pytest.raises(admin.AdminError) as info:
        admin.reset_trials(path, reason="测试")
    message = str(info.value)
    assert "被删掉" in message, message
    assert "50" in message, message              # 丢的是 20+30 = 50 次(锚点可证,精确)
    # 账本没被动过(拒绝发生在写之前)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_the_lost_trials_can_be_restored_by_adding_them_back(tmp_path):
    """丢失的次数**精确**可算(锚点带累计次数),所以补记也是精确的:补齐后 N 不变小。"""
    path = _trials_ledger(tmp_path, counts=(10, 20, 30))
    _truncate_to_first_row(path)

    result = admin.reset_trials(path, add_trials=50, reason="测试:补记被删的行")

    assert result["total"] == 60, result
    assert result["before"] == 60, result
    assert result["lost"] == 50, result
    # 重置记录里也记着丢了多少(可审计)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    reset_rows = [r for r in rows if r.get("kind") == tl.RESET_KIND]
    assert reset_rows and reset_rows[-1]["lost"] == 50, rows
    assert reset_rows[-1]["before"] == 60 and reset_rows[-1]["after"] == 60, rows


def test_an_explicitly_allowed_reduction_is_still_recorded_honestly(tmp_path):
    """即便人工显式允许变小,重置记录里的 before 也必须是**锚点可证的 60**,不是"当前文件的 10"。

    否则 before==after==10,审计轨迹上"什么都没丢"——把丢失洗白(第 43 轮,codex 的 H2)。"""
    path = _trials_ledger(tmp_path, counts=(10, 20, 30))
    _truncate_to_first_row(path)

    result = admin.reset_trials(path, allow_reduce_n=True, reason="测试:接受变小")

    assert result["before"] == 60, result
    assert result["total"] == 10, result
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    reset_rows = [r for r in rows if r.get("kind") == tl.RESET_KIND]
    assert reset_rows[-1]["before"] == 60 and reset_rows[-1]["after"] == 10, rows
    assert reset_rows[-1]["lost"] == 50, rows


def test_a_ledger_whose_anchor_total_does_not_match_is_refused(tmp_path):
    """锚点记的累计次数与账本对不上 ⇒ fail-closed(第 43 轮)。

    锚点是**账本之外的**信任根:它记的累计次数必须与账本前 N 条的合计一致,
    否则说明账本被换过/删过行。"""
    path = _trials_ledger(tmp_path, counts=(1, 2, 3))
    anchor = chain.anchor_path_for(path)
    seq, head, _total = chain.read_anchor(anchor)
    anchor.write_text(f"{seq} {head} 99\n", encoding="ascii")

    with pytest.raises(tl.TrialLedgerError) as info:
        tl.total_trials(path)
    assert "累计次数与锚点对不上" in str(info.value), str(info.value)


# ---------- H3:"按名求值的执行器"按**值的来源**判,再导出也逃不掉 ----------

def test_the_evaluator_rule_catches_a_reexported_get_type_hints():
    """再导出的「按名求值的执行器」也拒——但**平台无关**。

    按**访问路径**判会漏再导出("typing_extensions.get_type_hints" 不在名单里);
    此前它只是因为"typing_extensions 本身被导入门挡住"才没出事——那是**别的规则**兜着。
    现在按**值的来源**判(`typing.get_type_hints`),再导出也拒(第 43 轮,codex 的 H3)。

    **为什么不用 `typing_extensions` 当探针**(第 48 轮修正):`typing_extensions.get_type_hints is
    typing.get_type_hints` 这条**前提本身是平台相关的**——实测 Windows/3.13 上成立,
    Linux/3.12 上**不成立**(那边 `typing_extensions` 不是 `typing` 的再导出)。
    把它当断言 ⇒ 那条用例在 Linux CI 上必然红(第 47 轮之后 CI 第一次跑这份套件就红了),
    而红的原因是**探针的前提不成立**,不是规则坏了。

    所以探针改成**合成一个再导出模块**:把 `typing.get_type_hints` 原样挂到一个自造模块上,
    访问路径是"fake_reexport_module.get_type_hints"(不在名单里),拿到的**值**的来源却是
    `typing.get_type_hints` ⇒ 按来源判必须拒。这样**任何平台上的前提都成立**。
    """
    import types
    import typing

    fake = types.ModuleType("fake_reexport_module")
    fake.get_type_hints = typing.get_type_hints
    view = rs._view_for(fake)
    with pytest.raises(AttributeError) as info:
        rs._check_object_rules(view, "get_type_hints", typing.get_type_hints)
    assert "按名求值的执行器" in str(info.value), str(info.value)

    # 真·再导出的那一条(平台**成立**时才断言):`typing_extensions` 在某些平台上
    # 确实是 `typing` 的再导出,那就把它也钉住;不在(前提不成立)就不拿它当断言。
    try:
        import typing_extensions
    except ImportError:                                  # pragma: no cover - 环境没装
        return
    if typing_extensions.get_type_hints is typing.get_type_hints:
        view2 = rs._view_for(typing_extensions)
        with pytest.raises(AttributeError) as info2:
            rs._check_object_rules(view2, "get_type_hints", typing_extensions.get_type_hints)
        assert "按名求值的执行器" in str(info2.value), str(info2.value)


def test_a_harmless_typing_member_is_still_reachable():
    """**放行侧**:这条规则不该把无害成员一起打死(`typing.get_args` 只读类型参数)。"""
    import typing

    view = rs._view_for(typing)
    rs._check_object_rules(view, "get_args", typing.get_args)     # 不抛 = 放行


# ---------- M2:持有者还活着就**不抢**它的锁 ----------

def test_a_live_holders_lock_is_not_stolen_even_when_it_is_old(tmp_path):
    """持有者进程**还活着** ⇒ 锁不是陈旧的,哪怕它的年龄已经超过 `stale_seconds`。

    此前是"年龄到了就抢":持有者只是写得慢一点也会被抢走锁,于是"批准与重置并发"
    照样能静默丢记录(第 43 轮,codex 的 M2)。"""
    target = tmp_path / "old.jsonl"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lock = target.with_name(target.name + ".chainlock")
    lock.write_text(f"{proc.pid} {time.time():.3f} foreign-token\n", encoding="ascii")
    old = time.time() - 3600                       # 一小时前"创建"的锁
    os.utime(lock, (old, old))
    try:
        started = time.monotonic()
        with pytest.raises(chain.LedgerChainError) as info:
            with chain.file_lock(target, timeout=0.5):
                pass
        assert "被其他进程占用" in str(info.value), str(info.value)
        assert time.monotonic() - started < 5
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_both_callers_of_the_approvals_lock_use_the_same_staleness_limit():
    """同一把 `approvals.json.chainlock`,两个调用方必须用**同一个**陈旧口径。

    此前批准路径 60 秒、账本维护入口 30 秒 ⇒ 维护入口能按自己的口径抢走批准路径的活锁
    (第 43 轮,codex 的 M2)。"""
    assert rs.LOCK_STALE_SECONDS == chain.DEFAULT_STALE_SECONDS, (
        rs.LOCK_STALE_SECONDS, chain.DEFAULT_STALE_SECONDS)


# ---------- M3:锁失败的对外说法按情形分 ----------

def test_the_reentrancy_message_does_not_say_another_process_holds_the_lock(tmp_path):
    """重入 ≠ "被其他进程占用"(第 43 轮,codex 的 M3)。"""
    target = tmp_path / "approvals.json"
    with rs._approvals_lock(target):
        with pytest.raises(rs.SandboxError) as info:
            with rs._approvals_lock(target):
                pass
    message = str(info.value)
    assert "本进程同一线程" in message, message
    assert "被其他进程占用" not in message, message


def test_the_occupancy_message_says_it_is_transient(tmp_path):
    """暂态占用要说清"该等/该重试",并给出持有者与已等待时长(第 43 轮,codex 的 M3)。"""
    target = tmp_path / "approvals.json"
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    lock = target.with_name(target.name + ".chainlock")
    lock.write_text(f"{proc.pid} {time.time():.3f} foreign-token\n", encoding="ascii")
    try:
        with pytest.raises(rs.SandboxError) as info:
            with rs._approvals_lock(target, timeout=0.5):
                pass
        message = str(info.value)
        assert "被其他进程占用" in message, message
        assert "请稍后重试" in message, message
        assert str(proc.pid) in message, message
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---------- M1:取不到触发帧 ⇒ fail-closed ----------

def test_an_exec_whose_trigger_frame_cannot_be_read_is_refused(monkeypatch):
    """**查不了 = 没查** ⇒ 拒(第 43 轮,codex 的 M1)。

    此前是 fail-open:只有 `co_filename == "<string>"` 才拒,于是"来自真实文件"的代码
    (取不到帧时最可能的情形)被放行。"""
    assert rs._audit_frame_is_forbidden_eval(None, None) is True

    def _no_frame(depth=0):
        raise ValueError("no frame")

    monkeypatch.setattr(rs.sys, "_getframe", _no_frame)
    with rs.candidate_window():
        with pytest.raises(rs.AuditDenied) as info:
            rs._audit_hook("exec", (compile("1+1", "<string>", "eval"),))
    assert "audit:eval-origin-unknown" in str(info.value), str(info.value)


# ---------- 第 46 轮(codex 第二轮 MC3):求值帧这族规则在 CI 上真跑 ----------

def _forward_ref_evaluate(fr):
    """跨版本地调 `ForwardRef._evaluate`。

    3.12 是 `(globalns, localns)`;3.13 多了**必填**的关键字参数 `recursive_guard`
    (同时会打 `type_params` 的弃用警告)。这里按签名决定怎么调——
    测试不能因为 Python 版本变了就**悄悄不测那条规则**。
    """
    import inspect
    params = inspect.signature(type(fr)._evaluate).parameters
    if "recursive_guard" in params:
        return fr._evaluate({}, None, recursive_guard=set())
    return fr._evaluate({}, None)


def test_the_audit_hook_denies_forward_ref_eval_without_the_trading_stack():
    """**不经生产入口**也能验到 `audit:forward-ref-eval`——这条用例在 CI 上真跑。

    第 46 轮(codex 第二轮 M3,Medium):承载第 40 轮新规则的那几条用例全部经
    `run_candidate`(生产入口),而生产入口硬依赖交易栈 ⇒ 在研究侧 CI 上**一条都不跑**
    ⇒ 新接的核心防线 CI 步骤对这族规则的覆盖率是 **0**。
    这里补一条**沙盒层**用例:直接开 `candidate_window()`,不走生产入口 ⇒ 不需要 vnpy。
    """
    with rs.candidate_window():
        with pytest.raises(rs.AuditDenied) as info:
            import typing
            fr = typing.ForwardRef("__import__('os').environ")
            _forward_ref_evaluate(fr)
    assert info.value.rule == "audit:forward-ref-eval", info.value.rule
    assert ("audit:forward-ref-eval", "exec") in rs.window_denials(), rs.window_denials()


def test_the_audit_hook_still_allows_the_libraries_own_exec():
    """放行方向:库**自己** eval 自己生成的代码必须照旧跑通(否则规则就成了"一律拒")。

    `collections.namedtuple` 会 exec 一段框架生成的代码,触发帧是它自己
    ⇒ 不在 `FORBIDDEN_EVAL_FRAMES` 里 ⇒ 不该被拒。
    """
    with rs.candidate_window():
        import collections
        point = collections.namedtuple("Point", "x y")
        assert point(1, 2).x == 1
    assert rs.window_denials() == (), rs.window_denials()


# ---------- L2:`vnpy.alpha` 的**运行期**导入门也要有用例 ----------

@requires_trading_stack
def test_vnpy_alpha_is_refused_by_the_runtime_import_gate(repo, tmp_path, monkeypatch):
    """静态层拦 `vnpy.alpha` 之外,**运行期导入门**也拦(第 43 轮,codex 的 L2)。

    静态层被绕过时(比如源码是拼出来的),这一层才是关键;此前只有静态层有用例。"""
    source = """
        RESULT = {}
        try:
            import vnpy.alpha
            RESULT["r"] = "放行了"
        except Exception as exc:
            RESULT["err"] = str(exc)
        from vnpy_ctastrategy import CtaTemplate
        class S(CtaTemplate):
            pass
        S.RESULT = RESULT
    """
    cls = run_candidate(tmp_path, monkeypatch, source)
    assert "禁止导入 vnpy.alpha" in cls.RESULT.get("err", ""), cls.RESULT
    assert "按全名禁用" in cls.RESULT.get("err", ""), cls.RESULT


# ---------- L5:白名单模块的公开成员不该被"按来源不可达"整片打死 ----------

def test_the_whitelisted_libraries_keep_their_ordinary_members_reachable(monkeypatch):
    """白名单模块(numpy/pandas)的**公开成员**基本都该拿得到(第 43 轮,codex 的 L5)。

    `_OBJECT_ORIGIN_ROOTS` 是**派生**出来的来源根集合(危险模块名 ∪ 不可达来源),
    口径一改就可能把白名单模块的正常成员**整片**打死——而那不会报错,只表现为
    "候选突然用不了 numpy.array"。所以这里做一次**扫描式回归**:
    实测(2026-11-14,numpy/pandas 放行时)被拒的公开成员只有 3 个,
    全是本来就该拒的(numpy.ctypeslib / numpy.f2py / pandas.errors —— 它们把
    ctypes/subprocess 递出来)。这里把"被拒的公开成员数量"钉住,整片打死会立刻红。"""
    import numpy
    import pandas

    monkeypatch.setattr(rs, "_ACTIVE_ALLOWED_ROOTS",
                        [tuple(set(rs.ALLOWED_MODULE_ROOTS) | {"numpy", "pandas"})])
    blocked: dict[str, list[str]] = {}
    for module in (numpy, pandas):
        view = rs._view_for(module)
        for name in dir(module):
            if name.startswith("_"):
                continue
            try:
                value = getattr(module, name)
            except Exception:                       # noqa: BLE001 - 拿不到就跳过
                continue
            try:
                rs._check_object_rules(view, name, value)
            except AttributeError as exc:
                rule = str(exc).split("(规则:")[-1].rstrip(")")
                blocked.setdefault(f"{module.__name__}:{rule}", []).append(name)

    total_blocked = sum(len(names) for names in blocked.values())
    assert total_blocked <= 10, f"白名单模块的公开成员被拒得太多了(可能整片打死):{blocked}"
    # 常见的核心成员必须拿得到
    for module, name in ((numpy, "array"), (numpy, "mean"), (pandas, "DataFrame"),
                         (pandas, "Series"), (pandas, "concat")):
        rs._check_object_rules(rs._view_for(module), name, getattr(module, name))


# ---------- 第 45 轮:维护入口的锁错误不许把"链坏了"说成"被其他进程占用" ----------

def test_an_error_inside_the_admin_lock_is_not_reported_as_occupancy(tmp_path):
    """临界区**内部**失败 ≠ "被其他进程占用"(第 45 轮)。

    `risk/ledger_admin._ledger_lock` 此前把**整个临界区**圈在 `try` 里,
    于是临界区里抛的 `LedgerChainError`(reason="chain",如锚点写失败)被翻译成
    "账本被其他进程占用"——**那句话是错的**:
    被占用 ⇒ 该稍后重试;链坏了 ⇒ 重试没用,得人工处理。
    这与第 42 轮在 `scripts/research_sandbox._approvals_lock` 修掉的是同一个问题,
    那一边改了,这一边漏了。

    钉住两件事:①异常**类型**不变(仍原样抛 `LedgerChainError`,不吞);
    ②消息里**不许**出现"被其他进程占用"。"""
    path = _trials_ledger(tmp_path, counts=(1,))
    original = chain.write_anchor

    def broken_write_anchor(*args, **kwargs):
        raise chain.LedgerChainError("锚点写失败(fail-closed): 磁盘满了", reason="chain")

    chain.write_anchor = broken_write_anchor
    try:
        with pytest.raises(chain.LedgerChainError) as info:
            admin.reset_trials(path, reason="第 45 轮用例")
    finally:
        chain.write_anchor = original
    message = str(info.value)
    assert "被其他进程占用" not in message, message
    assert "磁盘满了" in message, message


def test_the_admin_lock_occupancy_message_says_it_is_transient(tmp_path):
    """暂态占用要说清"该重试",并把占用与其它失败**分开说**(第 45 轮)。

    此前 `occupancy` 与 `chain`/`generic` 共用兜底分支,兜底说的是"被其他进程占用",
    于是 reason="chain" 也被说成"被其他进程占用"。现在 `occupancy` 单独说。"""
    message = admin._ledger_lock_message(
        "trials.jsonl", chain.LedgerChainError("账本被其他进程占用", reason="occupancy"))
    assert "被其他进程占用" in message, message
    assert "请稍后重试" in message, message

    other = admin._ledger_lock_message(
        "trials.jsonl", chain.LedgerChainError("链坏了", reason="chain"))
    assert "被其他进程占用" not in other, other
    assert "reason=chain" in other, other


def test_the_sandbox_lock_occupancy_message_says_it_is_transient(tmp_path):
    """沙盒侧同一件事:占用单独说,其它 reason 不许被说成占用(第 45 轮)。"""
    message = rs._approvals_lock_message(
        "approvals.json", chain.LedgerChainError("账本被其他进程占用", reason="occupancy"))
    assert "被其他进程占用" in message, message
    assert "请稍后重试" in message, message

    other = rs._approvals_lock_message(
        "approvals.json", chain.LedgerChainError("链坏了", reason="chain"))
    assert "被其他进程占用" not in other, other
    assert "reason=chain" in other, other


def test_every_lock_caller_passes_the_staleness_limit_explicitly():
    """**安全口径**(`stale_seconds`)必须三个调用方完全一致(第 45 轮)。

    `stale_seconds` 决定"多久算残留锁、可以抢走"——它是**安全**参数:
    同一把锁文件的不同调用方用不同口径,快的那个能按自己的口径把慢的那个的**活锁**
    当成陈旧锁抢走(第 43 轮 codex 的 M2 就是这个)。所以它**不许**有策略差异。

    与之相对,`timeout`(等不到就放弃) 是**调用方策略**:
    人工维护入口失败即重试(5 秒),写入路径宁可多等(30 秒)。两者**故意不同**,
    所以这条用例只钉住:①三个调用方用的 `stale_seconds` 完全一致;
    ②每个调用方都**显式**传参,不靠 `file_lock` 的默认值——
    默认值是"只有不传参的人才用"的暗口径(此前 `risk/trial_ledger` 就是不传的)。"""
    assert admin.ADMIN_LOCK_TIMEOUT == 5.0, admin.ADMIN_LOCK_TIMEOUT
    assert rs.APPROVALS_LOCK_TIMEOUT == 5.0, rs.APPROVALS_LOCK_TIMEOUT
    assert tl.LOCK_TIMEOUT == chain.DEFAULT_LOCK_TIMEOUT, tl.LOCK_TIMEOUT
    # 三个调用方用的**安全**口径必须是同一个值:
    assert rs.LOCK_STALE_SECONDS == chain.DEFAULT_STALE_SECONDS
    # 每个调用方都显式传了 stale_seconds(源码层面钉住,不留暗口径):
    for path, needle in (
            (ROOT / "risk" / "ledger_admin.py", "stale_seconds=chain.DEFAULT_STALE_SECONDS"),
            (ROOT / "risk" / "trial_ledger.py", "stale_seconds=chain.DEFAULT_STALE_SECONDS"),
            (ROOT / "scripts" / "research_sandbox.py", "stale_seconds=LOCK_STALE_SECONDS")):
        source = path.read_text(encoding="utf-8")
        assert needle in source, f"{path.name} 没有显式传 stale_seconds:{needle}"


# ---------------------------------------------------------------------------
# 第 55 轮之二十五(codex 第 2 轮 L3/L4):账本维护入口的持久性与 fail-closed
# ---------------------------------------------------------------------------


def test_the_approval_ledger_write_flushes_and_fsyncs(tmp_path, monkeypatch):
    """**拦截侧**:批准账本落盘**必须** flush + fsync。

    第 55 轮之二十五(codex 第 2 轮 L3):试验账本那侧 `_write_lines` 有
    `flush + os.fsync`,而批准账本这侧 `_write_dict` 只有 `write_text + os.replace`
    ⇒ 同一套维护动作的持久性口径**不一致** ⇒ 断电/崩溃时可能落下**内容不完整**
    的批准账本。

    ⇒ 现在与试验账本**同一套**写法。
    """
    calls = []
    real_fsync = os.fsync

    def _spy(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(admin.os, "fsync", _spy)
    target = tmp_path / "approvals.json"
    admin._write_dict(target, [{"sha": "a" * 64, "name": "x.py"}])

    assert calls, "批准账本落盘没有 fsync ⇒ 与试验账本那侧口径不一致"
    # **第二条断言**(放行侧):内容**照样**是合法的 JSON(加了 fsync 不许写坏)
    assert json.loads(target.read_text(encoding="utf-8"))["a" * 64]["name"] == "x.py"


def test_the_claimed_total_refuses_to_call_an_unreadable_ledger_empty(tmp_path):
    """**拦截侧**:账本**读不了**时不许假装它是「0 次试验」。

    第 55 轮之二十五(codex 第 2 轮 L4):上一版 `except OSError: return 0`
    ⇒ 读失败被当成「账本声称 0 次」⇒ 「防变小」的基准**归零**
    ⇒ 一次悄悄变小反而**看不出来** ⇒ 与这段函数**自己写的原则**相反
    (「宁可高估也不低估,放过一次悄悄变小」)。

    ⇒ 现在**报出去**(fail-closed):读不了就不许据此判断。
    """
    as_dir = tmp_path / "ledger_is_a_dir"
    as_dir.mkdir()
    with pytest.raises(admin.AdminError):
        admin._claimed_total(as_dir)


def test_the_claimed_total_still_sums_a_readable_ledger(tmp_path):
    """**放行侧**:账本读得了时,累计次数**照样**算得对。

    没有这条 ⇒ 上一条可能把「读得了」也判成读不了 ⇒ 维护入口整个不可用
    ⇒ 守卫被调宽。
    """
    ledger = tmp_path / "trials.jsonl"
    ledger.write_text(
        '{"count": 3}\n{"count": 4}\n', encoding="utf-8")
    assert admin._claimed_total(ledger) == 7
