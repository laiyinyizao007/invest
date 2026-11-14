"""候选文件读写能力收口(Issue #31):审计钩子 / numpy·pandas 移出默认白名单 / 账本防篡改 / 行情先取定。

动态探针,双向:放行正常写法 + 拦截攻击写法,并断言**是哪条规则**拒的(第 22 轮教训:规则失灵时用例照样绿)。
攻击用真 numpy/pandas 复现(`np.memmap` 写 pickle + `pd.read_pickle`、`np.savetxt`、`DataFrame.to_csv`……)。

诚实边界(别把这些用例读成"安全边界已建成"):审计钩子是纵深防御;同进程里候选能影响框架的问题由 Issue #29 闭合。
"""
import contextlib
import copy
import json
import os
import pickle
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import bootstrap  # noqa: F401  与既有用例一致:先于 vnpy.trader.database
import numpy as np   # 必须在进入窗口前导入:pandas 首次导入会 ctypes.dlopen,窗口里会被拒
import pandas as pd
import research_api as api
import research_sandbox as rs
from risk import ledger_admin as admin
from risk import ledger_chain as chain
from risk import trial_ledger as tl

ROOT = Path(__file__).resolve().parents[1]


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


def test_framework_privilege_is_the_only_explicit_bypass(victim):
    """框架自己的动作(守卫跑 git、清 .pyc)走显式放行路径;离开后窗口恢复。"""
    with rs.candidate_window():
        with rs.framework_privilege():
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        with _expect_rule("audit:subprocess.Popen"):
            subprocess.run([sys.executable, "-c", "pass"])


def test_privilege_is_wired_only_at_the_two_framework_call_sites():
    """"只写在文档里的防线等于没有":放行路径必须有且只有这两处调用点(grep 接线)。"""
    source = (ROOT / "scripts" / "research_sandbox.py").read_text(encoding="utf-8")
    calls = [line.strip() for line in source.splitlines() if "framework_privilege()" in line and "def " not in line
             and not line.strip().startswith(("#", '"""', "用法", "`"))]
    assert sorted(calls) == ["with framework_privilege():", "with framework_privilege():"], calls
    api_source = (ROOT / "scripts" / "research_api.py").read_text(encoding="utf-8")
    assert "framework_privilege" not in api_source


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


def run_candidate(tmp_path, monkeypatch, source, allowed=("numpy", "pandas")):
    """走生产入口 `import_candidate`(窗口接线在里面),numpy/pandas 经**配置追加**放行(默认已不在白名单)。
    只绕开静态筛查与运行时类校验:这里测的是运行期的窗口。"""
    cfg = copy.deepcopy(api.rc.load())
    cfg["screening"]["allowed_modules"] = list(allowed)
    monkeypatch.setattr(api.rc, "load", lambda *a, **k: cfg)
    monkeypatch.setattr(api, "check_candidate_class", lambda *a, **k: None)
    monkeypatch.setattr(api.rs, "screen_source", lambda *a, **k: [])
    path = tmp_path / "candidate.py"
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    return api.import_candidate(path, "S", api.rs.file_sha256(path))


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
    assert anchor.read_text(encoding="ascii").split() == ["3", rows[-1]["hash"]]
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
