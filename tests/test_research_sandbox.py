"""研究沙盒底座:路径限制、静态筛查、环境白名单、窗口限制、往返盈亏、结果存取。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

import research_sandbox as rs


# ---------- 路径:只认 pending_strategies/ ----------

def test_resolve_pending_accepts_file_inside_pending_dir(tmp_path):
    pending = tmp_path / "pending_strategies"
    pending.mkdir()
    (pending / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert rs.resolve_pending("pending_strategies/a.py", tmp_path) == (pending / "a.py").resolve()
    assert rs.resolve_pending("a.py", tmp_path) == (pending / "a.py").resolve()      # 裸文件名也按 pending 解析


@pytest.mark.parametrize("arg", ["../outside.py", "pending_strategies/../x.py", "/etc/passwd.py", "pending_strategies/a.txt", "approved/a.py"])
def test_resolve_pending_rejects_escapes_and_wrong_suffix(tmp_path, arg):
    (tmp_path / "pending_strategies").mkdir()
    (tmp_path / "outside.py").write_text("x", encoding="utf-8")
    (tmp_path / "x.py").write_text("x", encoding="utf-8")

    with pytest.raises(rs.SandboxError):
        rs.resolve_pending(arg, tmp_path)


def test_resolve_pending_rejects_missing_file(tmp_path):
    (tmp_path / "pending_strategies").mkdir()

    with pytest.raises(rs.SandboxError, match="不存在"):
        rs.resolve_pending("nope.py", tmp_path)


# ---------- 静态筛查(纵深防御,不是沙盒) ----------

CLEAN = "from vnpy_ctastrategy import CtaTemplate\nimport math\nclass S(CtaTemplate):\n    pass\n"


def test_screen_accepts_plain_strategy():
    assert rs.screen_source(CLEAN) == []


@pytest.mark.parametrize("src, needle", [
    ("import os\n", "os"),
    ("import subprocess\n", "subprocess"),
    ("from pathlib import Path\n", "pathlib"),
    ("import socket, math\n", "socket"),
    ("from urllib.request import urlopen\n", "urllib"),
    ("import importlib\n", "importlib"),
    ("exec('1')\n", "exec"),
    ("eval('1')\n", "eval"),
    ("open('.env').read()\n", "open"),
    ("__import__('os')\n", "__import__"),
    ("getattr(x, 'y')\n", "getattr"),
    ("x.__class__.__bases__\n", "__bases__"),
])
def test_screen_flags_dangerous_constructs(src, needle):
    findings = rs.screen_source(src)

    assert findings and any(needle in f for f in findings)


def test_screen_reports_syntax_error_instead_of_crashing():
    assert any("语法" in f for f in rs.screen_source("def (:\n"))


@pytest.mark.parametrize("src, needle", [
    ("operator.attrgetter('__dict__')(self)\n", "__dict__"),
    ("x.__class__.__bases__\n", "__class__"),
])
def test_screen_flags_forbidden_attr_names_used_as_strings(src, needle):
    """属性名当字符串用,能绕开"禁止访问 __dict__"的属性写法。

    operator.attrgetter('__dict__')(self) 的调用点看不出访问的是什么,只有字符串本身露馅。"""
    findings = rs.screen_source(src)
    assert findings and any(needle in f for f in findings), findings


# ---------- 子进程环境白名单 ----------

def test_sandbox_env_drops_trading_credentials_and_unknown_vars():
    base = {"PATH": "p", "SYSTEMROOT": "r", "CTP_PASSWORD": "x", "CTP_AUTH_CODE": "y", "WEBHOOK_URL": "u",
            "LIVE_VT_SYMBOL": "c", "ANTHROPIC_API_KEY": "k", "TEMP": "t"}

    env = rs.sandbox_env(base)

    assert env["PATH"] == "p" and env["TEMP"] == "t"
    assert not any(k.startswith(("CTP_", "WEBHOOK_", "LIVE_", "ANTHROPIC_")) for k in env)


# ---------- 窗口:测试段不可达 ----------

def test_segment_window_allows_train_and_valid_only():
    start, end = rs.segment_window("CADJ", "train")
    assert start < end
    assert rs.segment_window("CADJ", "valid")[0] > end - (end - start)       # 取得到即可

    with pytest.raises(rs.SandboxError, match="测试段"):
        rs.segment_window("CADJ", "test")
    with pytest.raises(rs.SandboxError):
        rs.segment_window("NOPE", "train")


def test_segment_window_never_reaches_test_start():
    import optimize
    for key in ("CADJ", "MAADJ"):
        test_start = optimize.windows_for(key)["test"][0]
        for seg in ("train", "valid"):
            assert rs.segment_window(key, seg)[1] < test_start


# ---------- 往返盈亏 ----------

def T(direction, offset, price, vol=1):
    from vnpy.trader.constant import Direction, Offset      # 真实枚举:value 的文案是英文,不能按中文匹配
    return SimpleNamespace(direction=Direction[direction], offset=Offset[offset], price=price, volume=vol)


def test_round_trip_pnls_long_and_short():
    trades = [T("LONG", "OPEN", 100), T("SHORT", "CLOSE", 110),        # +10 点
              T("SHORT", "OPEN", 120), T("LONG", "CLOSE", 125)]        # -5 点
    assert rs.round_trip_pnls(trades, size=10) == [100.0, -50.0]


def test_round_trip_pnls_ignores_unclosed_position_and_handles_empty():
    assert rs.round_trip_pnls([T("LONG", "OPEN", 100)], size=10) == []
    assert rs.round_trip_pnls([], size=10) == []


# ---------- 结果存取 ----------

def test_save_and_load_result_roundtrip_and_list(tmp_path):
    rid = rs.save_result({"a": 1}, tmp_path, now=datetime(2026, 11, 12, 21, 0, 0))

    assert rs.load_result(rid, tmp_path) == {"a": 1, "run_id": rid}
    assert rs.list_results(tmp_path) == [rid]


@pytest.mark.parametrize("rid", ["../x", "a/b", "", "x.json"])
def test_load_result_rejects_unsafe_ids(tmp_path, rid):
    with pytest.raises(rs.SandboxError):
        rs.load_result(rid, tmp_path)


# ---------- 批准:绑定到代码哈希 ----------

def test_approval_is_bound_to_content_hash(tmp_path):
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"

    assert rs.is_approved(f, approvals) is False
    sha = rs.record_approval(f, approvals, now=datetime(2026, 11, 12, 21, 0))
    assert sha == rs.file_sha256(f) and rs.is_approved(f, approvals) is True

    f.write_text("a = 2\n", encoding="utf-8")                  # 代码一改,批准自动失效
    assert rs.is_approved(f, approvals) is False


def test_approval_survives_rename_but_not_edit(tmp_path):
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"
    rs.record_approval(f, approvals)
    g = tmp_path / "renamed.py"
    g.write_text("a = 1\n", encoding="utf-8")

    assert rs.is_approved(g, approvals) is True                # 批准的是内容,不是文件名


def test_corrupt_approvals_file_fails_closed(tmp_path):
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"
    approvals.write_text("{broken", encoding="utf-8")

    with pytest.raises(rs.SandboxError):
        rs.is_approved(f, approvals)
    with pytest.raises(rs.SandboxError):
        rs.record_approval(f, approvals)


def test_recording_twice_keeps_one_entry(tmp_path):
    import json
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"
    rs.record_approval(f, approvals)
    rs.record_approval(f, approvals)

    assert len(json.loads(approvals.read_text(encoding="utf-8"))) == 1


# ---------- 配置驱动的筛查与目录 ----------

def test_screen_extra_lists_extend_the_builtin_ones():
    assert rs.screen_source("import numpy\n") == []
    assert any("numpy" in x for x in rs.screen_source("import numpy\n", extra_modules=("numpy",)))
    assert any("print" in x for x in rs.screen_source("print(1)\n", extra_calls=("print",)))
    assert any("os" in x for x in rs.screen_source("import os\n", extra_modules=("numpy",)))   # 内置清单不会被覆盖


def test_resolve_pending_honours_configured_directory(tmp_path):
    d = tmp_path / "inbox"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")

    assert rs.resolve_pending("a.py", tmp_path, pending_dir="inbox") == (d / "a.py").resolve()
    with pytest.raises(rs.SandboxError):
        rs.resolve_pending("pending_strategies/a.py", tmp_path, pending_dir="inbox")


def test_sandbox_env_passes_data_and_config_paths_but_still_nothing_secret():
    env = rs.sandbox_env({"INVEST_C15_DATA": "/data/derived/x.csv", "RESEARCH_SANDBOX_CONFIG": "/tmp/c.json",
                          "CTP_PASSWORD": "x", "WEBHOOK_URL": "u", "DEEPSEEK_API_KEY": "k"})

    assert env == {"INVEST_C15_DATA": "/data/derived/x.csv", "RESEARCH_SANDBOX_CONFIG": "/tmp/c.json"}


# ---------- 不可变宪法接入静态筛查 ----------

def test_screen_source_reports_constitution_violations():
    findings = rs.screen_source("class C:\n    risk_budget = 99999.0\n")
    assert any("risk_budget" in f for f in findings)


def test_screen_source_still_reports_forbidden_imports():
    findings = rs.screen_source("import os\n")
    assert any("禁止导入" in f for f in findings)


def test_screen_source_accepts_a_well_behaved_candidate():
    src = "class C:\n    fast_window = 8\n    def entry_signal(self):\n        return 0\n"
    assert rs.screen_source(src) == []


def test_screen_source_does_not_report_new_members():
    src = "class C:\n    adx_window = 14\n    def helper(self):\n        return 1\n"
    assert rs.screen_source(src) == []


# ---------- 别名绕过:拦引用而不是拦调用 ----------

@pytest.mark.parametrize("src, needle", [
    ("s = setattr\ns(self, 'risk_budget', 10**12)\n", "setattr"),
    ("v = vars\nv(self)['risk_budget'] = 10**12\n", "vars"),
    ("g = getattr\ng(object, '__setattr__')\n", "getattr"),
    ("d = delattr\nd(self, 'risk_budget')\n", "delattr"),
    ("t = type\nt(self)\n", "type"),
])
def test_screen_flags_an_alias_of_a_forbidden_call(src, needle):
    """先取别名再调用,调用点已经看不出被调的是谁——所以要拦**引用**。

    `s = setattr` 之后 `s(self, "risk_budget", 10**12)` 的调用点是裸名字,
    只拦"调用"等于给别名留了门。"""
    findings = rs.screen_source(src)
    assert findings and any(needle in f for f in findings), findings


def test_screen_rejects_the_operator_module():
    """operator.attrgetter('__dict__') 是反射入口:属性名可以是字符串,绕开属性名检查。"""
    findings = rs.screen_source("import operator\noperator.attrgetter('__dict__')(self)\n")
    assert findings and any("operator" in f for f in findings), findings


# ---------- 第 6 轮 codex 的 M1:受禁名字的**裸名**与**字符串**形式 ----------

def test_screen_flags_a_bare_builtins_reference():
    """裸名 `__builtins__` 也必须拦。

    `exec(code, module.__dict__)` 会自动往候选的 globals 里塞一个 `__builtins__`
    (里面就有 `__import__`),而受禁属性名此前只拦 `x.__builtins__` 这种属性写法——
    裸名写法完全看不见:静态层 0 findings,候选照样能拿到执行入口。"""
    findings = rs.screen_source("f = __builtins__['__import__']" + chr(10) + "f('os')" + chr(10))
    assert findings and any("__builtins__" in f for f in findings), findings


def test_screen_flags_a_string_form_of_the_import_entry():
    """入口藏在字符串里也要拦:`'__import__'` 这种字符串不是属性名,属性检查看不见。"""
    findings = rs.screen_source("name = '__import__'" + chr(10))
    assert findings and any("__import__" in f for f in findings), findings


def test_screen_does_not_reject_ordinary_words_that_merely_look_like_builtins():
    """普通词不按字符串拒:`type`/`open` 这类词在候选代码里太常见(字典键、注释都会命中),
    一律拒绝会把正常候选整片拒掉。真正危险的是 dunder 形状的那些。"""
    findings = rs.screen_source("KIND = {'type': 'ema', 'open': 3}" + chr(10))
    assert not findings, findings


# ---------- 执行时的能力隔离:不靠枚举名字,靠"不给" ----------

def test_safe_builtins_do_not_expose_the_dangerous_ones():
    """候选执行时拿到的内建集合里,不该有属性改写与反射的入口。"""
    safe = rs.safe_builtins()
    for name in ("setattr", "delattr", "getattr", "eval", "exec", "compile", "open",
                 "globals", "locals", "vars", "type", "object"):
        assert name not in safe, f"{name} 仍在候选可见的内建里"


def test_safe_builtins_keep_the_ordinary_ones():
    """常用内建必须留着,否则正常候选全被 NameError 打断。"""
    safe = rs.safe_builtins()
    for name in ("len", "range", "min", "max", "sum", "abs", "sorted", "round",
                 "int", "float", "str", "bool", "list", "dict", "set", "tuple",
                 "enumerate", "zip", "isinstance", "hasattr", "print", "Exception",
                 "ValueError", "TypeError", "__build_class__"):
        assert name in safe, f"{name} 不该被拿掉"


def test_safe_builtins_block_the_modules_that_can_patch_attributes():
    """`unittest.mock.patch.object` 能改写任意对象的属性,必须导不进来。"""
    safe = rs.safe_builtins()
    try:
        safe["__import__"]("unittest.mock")
    except ImportError as exc:
        assert "unittest" in str(exc)
    else:
        pytest.fail("unittest.mock 竟然能导入")


def test_safe_builtins_still_allow_ordinary_imports():
    """候选必须能 import(模板就是这么来的)。"""
    safe = rs.safe_builtins()
    module = safe["__import__"]("json")
    assert module.dumps({"a": 1}) == '{"a": 1}'


def test_safe_builtins_block_the_risk_package():
    """风控包必须导不进来:拿到 `risk.risk_guard` 就等于拿到风控函数的定义。

    候选改写 `rg.risk_per_trade` 就能架空 300 元单笔预算,而静态层只看得到
    "属性名"、看不到它是谁(第 9 轮 codex 实测:三行 monkey-patch,四层防线全放行)。
    候选不需要 import 风控模块——风控由基类固定,候选只写信号逻辑。"""
    safe = rs.safe_builtins()
    try:
        safe["__import__"]("risk.risk_guard")
    except ImportError as exc:
        assert "risk" in str(exc)
    else:
        pytest.fail("risk 包竟然能导入")


def test_screen_rejects_importing_the_risk_package():
    """静态层同样要拦:候选源码里出现 `import risk...` 一律拒绝。"""
    findings = rs.screen_source("import risk.risk_guard as rg\n")
    assert any("risk" in f for f in findings), findings


def test_safe_builtins_honour_the_configured_extra_blocklist():
    """配置里追加的禁用模块,在受限导入器里同样生效。"""
    safe = rs.safe_builtins(forbidden_modules=("json",))
    try:
        safe["__import__"]("json")
    except ImportError:
        pass
    else:
        pytest.fail("配置禁用模块竟然能导入")


def test_the_reserved_word_table_matches_the_code():
    """ADR 里的保留字清单必须与代码常量一致——清单是候选作者唯一的查处,漂了就等于没有。

    这条用例把 ADR `docs/adr/20261113-evolution-engine.md` 的"保留标识符与误报"一节
    与代码里的常量逐个核对:表里出现的名字必须真的在代码里,代码里受禁的名字也必须在表里。"""
    import pathlib
    import re

    adr = (pathlib.Path(__file__).resolve().parents[1] / "docs" / "adr" / "20261113-evolution-engine.md")
    text = adr.read_text(encoding="utf-8")
    section = text.split("### 保留标识符与误报", 1)[1].split("### ", 1)[0]

    def listed(*names):
        for name in names:
            assert f"`{name}`" in section, f"ADR 的保留字表里没有 {name}"

    listed(*rs.FORBIDDEN_MODULES)
    listed(*rs.FORBIDDEN_CALLS)
    listed(*rs.FORBIDDEN_ATTRS)
    listed(*rs.FORBIDDEN_STRINGS)
    listed(*rs.UNSAFE_BUILTINS)
    listed(*rs.EXTRA_FORBIDDEN_MODULES)
    # 宪法层的名单
    from risk import constitution as const
    listed(*const.BUILTIN_PROTECTED_PARAMETERS)
    listed(*const.BUILTIN_PROTECTED_NAMES)
    listed(*const.BUILTIN_FORBIDDEN_DUNDERS)
    listed(*const.BUILTIN_FORBIDDEN_BY_NAME_SETTERS)
    listed(*const.BUILTIN_FORBIDDEN_ORDER_CALLS)
    listed(*const.BUILTIN_PROTECTED_ENTRY_POINTS)
    listed(*const.BUILTIN_PROTECTED_STATE_OBJECTS)
    listed(*const.BUILTIN_PROTECTED_PATHS)
    listed(*const.BUILTIN_PROTECTED_CONTAINERS)
    listed(*const.BUILTIN_PROTECTED_CONTAINER_SCOPE)
    listed(*const.BUILTIN_PROTECTED_CONTAINER_MUTATORS)
    listed(*const.BUILTIN_PROTECTED_MODULE_ATTRS)
    listed(*const.BUILTIN_PROTECTED_READ_ATTRS)
    listed(*const.PROTECTED_RUNTIME_PACKAGES)
