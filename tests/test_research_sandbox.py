"""研究沙盒底座:路径限制、静态筛查、环境白名单、窗口限制、往返盈亏、结果存取。"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from types import ModuleType, SimpleNamespace

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


# ---------- 白名单模块自己会漏:静态层 + 运行期都要挡住(第 10 轮 codex 的 Critical) ----------

@pytest.mark.parametrize("src, needle", [
    # 危险段在属性链的**中间**:`enum.sys.modules["os"]` —— 旧规则只看「X.os」这一层,漏。
    ("import enum\nenum.sys.modules['os'].system('calc')\n", "sys"),
    ("import typing\ntyping.sys.modules['os'].system('calc')\n", "sys"),
    ("import statistics\nstatistics.sys.modules['os'].system('calc')\n", "sys"),
    ("import fractions\nfractions.sys.modules['os'].system('calc')\n", "sys"),
    ("import dataclasses\ndataclasses.sys.modules['os'].system('calc')\n", "sys"),
    ("import collections\ncollections._sys.modules['os'].system('calc')\n", "_sys"),
    # 危险段是模块自己挂的私有别名。
    ("import random\nrandom._os.system('calc')\n", "_os"),
    # 直接把能力入口导成裸名。
    ("from enum import sys\nsys.modules['os'].system('calc')\n", "sys"),
    ("from zipfile import os\nos.system('calc')\n", "os"),
    # 子模块也漏。
    ("import pandas.io.common\npandas.io.common.os.system('calc')\n", "os"),
])
def test_screen_flags_modules_that_leak_os_or_sys(src, needle):
    """白名单里的模块**自己**就把 os/sys 挂在属性上:静态层必须报。

    `enum.sys` / `random._os` 都是真的 os/sys 模块,旧规则(只认「X.os」这一层)零告警,
    实测能把任意命令执行出去(第 10 轮 codex 的 Critical)。"""
    findings = rs.screen_source(src)

    assert findings and any(needle in f for f in findings), findings


def test_screen_refuses_a_config_added_module_that_leaks():
    """配置追加的白名单模块也要过准入审计:不能靠配置把防线拆掉。

    静态层按**属性链**报违规(候选用了 `zipfile.os`);
    运行期的准入审计(导进来就审计)则整个模块拒收——
    两者合起来,配置放行一个泄漏模块也拿不到 os。
    """
    findings = rs.screen_source("from zipfile import os\nos.system('x')\n",
                                extra_allowed=["zipfile"])

    assert any("zipfile" in f for f in findings), findings
    assert any("os" in f for f in findings), findings


def test_runtime_refuses_a_config_added_module_that_leaks():
    """运行期:配置放行的模块自己泄漏 os 时,**整个模块拒收**(不是只挡属性)。

    审计用的是刚导进来的模块对象,不需要额外动态导入——
    动态导入会让研究沙盒模块被宪法守卫判成「执行候选的入口」。
    """
    safe = rs.safe_builtins(allowed_modules=["zipfile"])

    with pytest.raises(ImportError) as excinfo:
        safe["__import__"]("zipfile")

    assert "zipfile" in str(excinfo.value)


def test_runtime_still_allows_a_config_added_module_that_is_clean():
    """干净的配置放行模块照常可用(否则这条规则会被加白名单绕过)。"""
    safe = rs.safe_builtins(allowed_modules=["textwrap"])

    assert safe["__import__"]("textwrap").fill is not None


@pytest.mark.parametrize("src", [
    "import numpy as np\nimport pandas as pd\nx = np.zeros(3)\n",
    "import enum\nclass E(enum.Enum):\n    A = 1\n",
    "import statistics\nmu = statistics.mean([1.0, 2.0])\n",
    "import random\nx = random.random()\n",
])
def test_screen_does_not_flag_ordinary_module_use(src):
    """白名单模块的**正常**用法不能被误报(否则这条规则会被加白名单绕过)。"""
    assert rs.screen_source(src) == []


def test_runtime_blocks_going_through_a_leaking_module(tmp_path):
    """运行期也要挡:静态层封不住"经模块属性间接拿到 os"的全部写法。"""
    marker = tmp_path / "pwned.txt"
    src = f"import enum\nenum.sys.modules['os'].system('echo pwned > {marker.as_posix()}')\n"

    with rs.guarded_scope():
        with pytest.raises(ImportError):
            exec(compile(src, "cand.py", "exec"), {"__builtins__": rs.safe_builtins()})

    assert not marker.exists()


def test_runtime_restores_the_real_module_afterwards():
    """代理只在该候选执行期间生效:宿主机自己的代码不该看到代理。"""
    with rs.guarded_scope():
        exec(compile("import enum\nx = enum\n", "cand.py", "exec"),
             {"__builtins__": rs.safe_builtins()})

    module = sys.modules["enum"]
    assert isinstance(module, ModuleType)
    assert not isinstance(module, rs._GuardedModule)
    assert rs.audit_module(module) == ["sys"]      # 审计照样如实报告它会漏


@pytest.mark.parametrize("src, needle", [
    ("operator.attrgetter('__dict__')(self)\n", "__dict__"),
    ("x.__class__.__bases__\n", "__class__"),
])
def test_screen_flags_forbidden_attr_names_used_as_strings(src, needle):
    """属性名当字符串用,能绕开"禁止访问 __dict__"的属性写法。

    operator.attrgetter('__dict__')(self) 的调用点看不出访问的是什么,只有字符串本身露馅。"""
    findings = rs.screen_source(src)
    assert findings and any(needle in f for f in findings), findings


# ---------- 第 10 轮:模块代理的 dunder / 私有名闸门 ----------

@pytest.mark.parametrize("src", [
    "import numpy as np\nx = np.__spec__\n",
    "import numpy as np\nx = np.__loader__\n",
    "import numpy as np\nx = np.__file__\n",
    "import statistics\nx = statistics.__dict__\n",
    "import numpy as np\nx = np.__getattribute__\n",
    "import numpy as np\nx = np._module\n",
])
def test_the_runtime_proxy_blocks_dunder_and_underscore_attributes(src):
    """代理不给 dunder 与私有名:`__spec__`/`__loader__` 能读写任意文件。

    `mod.__spec__.loader.set_data(path, b)` 能直接改 `approvals.json`,
    于是「人读过源码才批准」这道仪式被候选单方面废掉(源码里看不见的写文件路径,
    静态审查抓不到)——第 10 轮 codex 实测的 Critical。
    `__dict__`/`__getattribute__` 是同一类钥匙:拿到真模块或真字典就等于拿到全部。
    """
    with rs.guarded_scope():
        with pytest.raises(ImportError):
            exec(compile(src, "cand.py", "exec"), {"__builtins__": rs.safe_builtins()})


@pytest.mark.parametrize("src, needle", [
    ("import numpy as np\nx = np.__spec__\n", "__spec__"),
    ("import numpy as np\nx = np.__loader__\n", "__loader__"),
    ("import numpy as np\nx = np.__file__\n", "__file__"),
    ("import numpy as np\nx = getattr(np, '__spec__')\n", "__spec__"),
    ("import numpy as np\nx = np.__spec__.loader.get_data('x')\n", "__spec__"),
])
def test_screen_flags_dunder_module_attribute_access(src, needle):
    """静态层也要看得见 dunder 取用(字符串形态同样算)。"""
    findings = rs.screen_source(src)

    assert findings and any(needle in f for f in findings), findings


def test_numpy_and_pandas_stay_usable_through_the_proxy():
    """**对照用例**:代理不能把正常用法也堵死。

    堵死了(normal 用法报 ImportError),这条规则就会被人加白名单绕过——
    第 9 轮曾经因为「按模块整体拒收」把 numpy/pandas 一起弄坏过一轮。"""
    src = (
        "import numpy as np\n"
        "import pandas as pd\n"
        "a = np.array([1.0, 2.0, 3.0])\n"
        "m = float(a.mean())\n"
        "s = float(a.sum())\n"
        "df = pd.DataFrame({'x': [1.0, 2.0]})\n"
        "dm = float(df['x'].mean())\n"
        "name = np.__name__\n"
    )
    out: dict = {}
    with rs.guarded_scope():
        exec(compile(src, "cand.py", "exec"), {"__builtins__": rs.safe_builtins()}, out)

    assert out["m"] == 2.0
    assert out["s"] == 6.0
    assert out["dm"] == 1.5
    assert out["name"] == "numpy"


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
    import segment_windows
    for key in ("CADJ", "MAADJ"):
        test_start = segment_windows.windows_for(key)["test"][0]
        for seg in ("train", "valid"):
            assert rs.segment_window(key, seg)[1] < test_start


# ---------- 往返盈亏 ----------

class _Enum:
    """只带 `name` 的枚举替身。

    生产代码只读 `t.direction.name` / `t.offset.name`(文案随 vnpy 版本与语言变化,
    所以按 name 匹配)。这里不 import vnpy:研究侧测试不该为了构造一条测试数据
    就把交易栈拖进来——CI 的研究侧闸门不装交易环境(第 7 轮 codex 的 H1)。"""

    def __init__(self, name):
        self.name = name


def T(direction, offset, price, vol=1):
    return SimpleNamespace(direction=_Enum(direction), offset=_Enum(offset), price=price, volume=vol)


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


def test_a_stale_lock_does_not_block_approvals_forever(tmp_path):
    """进程崩了会留下锁文件:不认残留锁的话,一次崩溃就让**之后所有**批准永久失败。"""
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"
    lock = tmp_path / "approvals.json.lock"
    lock.write_text("999999 0\n", encoding="ascii")          # 一个早就不存在的 PID

    sha = rs.record_approval(f, approvals)

    assert len(sha) == 64 and rs.is_approved(f, approvals) is True
    assert not lock.exists()


def test_a_lock_held_by_a_live_process_is_not_stolen(tmp_path, monkeypatch):
    """锁还**活着**的时候不能抢:抢了就变成两个进程同时写批准记录。"""
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"
    lock = tmp_path / "approvals.json.lock"
    lock.write_text(f"{os.getpid()} {time.time()}\n", encoding="ascii")

    with pytest.raises(rs.SandboxError):
        rs.record_approval(f, approvals)

    assert lock.exists()          # 锁还在,没被抢


def test_a_write_failure_is_reported_as_a_sandbox_error(tmp_path, monkeypatch):
    """写不进去必须变成 SandboxError:裸 OSError 会一路冒到 HTTP handler,
    浏览器只看到连接被重置(第 8 轮 codex 的 M2)。"""
    f = tmp_path / "s.py"
    f.write_text("a = 1\n", encoding="utf-8")
    approvals = tmp_path / "approvals.json"

    def boom(*args, **kwargs):
        raise OSError("磁盘满了")

    monkeypatch.setattr(rs.os, "replace", boom)

    with pytest.raises(rs.SandboxError):
        rs.record_approval(f, approvals)


def test_concurrent_approvals_do_not_lose_entries(tmp_path):
    """同时记多条批准:「先读后写」会丢更新,独占锁必须挡住
    (第 9 轮 codex 的 M3:并发场景原来一条测试都没有)。

    丢更新在这里是**静默**的:批准记录少一条,那一条候选就永远批不了,
    而没人会知道是并发吃掉了它。
    """
    import json
    import threading

    approvals = tmp_path / "approvals.json"
    files = []
    for i in range(6):
        f = tmp_path / f"s{i}.py"
        f.write_text(f"a = {i}\n", encoding="utf-8")
        files.append(f)

    barrier = threading.Barrier(len(files))
    errors: list[Exception] = []

    def worker(path):
        try:
            barrier.wait(timeout=10)          # 尽量让它们挤在一起
            rs.record_approval(path, approvals)
        except Exception as exc:             # noqa: BLE001 - 汇总后一次性断言
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert errors == [], errors
    data = json.loads(approvals.read_text(encoding="utf-8"))
    assert len(data) == len(files), data
    assert all(rs.is_approved(f, approvals) for f in files)


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


# ---------- 本机路径脱敏(第 5 轮 codex 的 Low) ----------

def test_redact_paths_hides_local_absolute_paths():
    """错误消息里的本机绝对路径要盖掉:它会经网关响应回到模型。"""
    out = rs.redact_paths(
        "批准记录被其他进程占用,拒绝写入: "
        r"C:\Example\research\data\research-control\approvals.json")

    assert "<路径>" in out
    assert "Dev" not in out
    assert "research-control" not in out


def test_redact_paths_hides_posix_absolute_paths():
    out = rs.redact_paths("错误 /usr/local/lib/python3.13/site-packages/x.py 找不到")

    assert "site-packages" not in out
    assert "<路径>" in out


def test_redact_paths_keeps_urls_and_bare_filenames():
    """URL 是诊断信息,不是本机结构;候选文件名是人要看的信息,都不能盖掉。"""
    assert rs.redact_paths("请求 https://example.com/a/b 失败") == "请求 https://example.com/a/b 失败"
    assert "atr_breakout_15m_2atr.py" in rs.redact_paths("候选 atr_breakout_15m_2atr.py 尚未批准")
    assert "pending_strategies/" in rs.redact_paths("策略文件必须位于 pending_strategies/ 内")


def test_redact_paths_leaves_ordinary_text_alone():
    text = "拒绝: 未知数据段 'test',可用: ('train', 'valid')"

    assert rs.redact_paths(text) == text


# ---------- 第 11 轮 codex 的发现:回溯帧、路径形态、私有名、值的清洗 ----------

def test_the_screening_catches_the_exception_traceback_route():
    """候选顺着**异常回溯帧**能拿回真 os(第 11 轮 codex 实测的 Critical)。

    `e.__traceback__.tb_frame.f_globals` 里是 numpy 内部帧的全局命名空间,
    里面有真的 os——于是任意写文件(可伪造 approvals.json)、读文件(.env、令牌)、
    执行命令,而筛查原本**零告警**:这条路上一个禁用名都不出现。
    现在静态层按名字把它抓住(人看到告警就不批准)。"""
    source = (
        "import numpy as np" + chr(10) +
        "try:" + chr(10) +
        "    np.load('/no/such/file.npy')" + chr(10) +
        "except BaseException as e:" + chr(10) +
        "    tb = e.__traceback__" + chr(10) +
        "    g = tb.tb_frame.f_globals" + chr(10)
    )
    findings = rs.screen_source(source)
    assert findings, "回溯帧那一路必须被静态层抓住"
    assert any("__traceback__" in f for f in findings), findings
    assert any("f_globals" in f for f in findings), findings
    # 字符串形态与模板形态同样要抓住
    assert rs.screen_source('x = getattr(e, "__traceback__")')
    assert rs.screen_source('x = "{0.__globals__}".format(np.mean)')
    # 普通代码不该被误报
    assert not rs.screen_source("import numpy as np" + chr(10) + "np.array([1, 2, 3]).mean()" + chr(10))


def test_the_screening_flags_private_capability_attributes():
    """私有名里藏能力入口(`numpy._module`):运行期取不到,静态层也要报
    (第 11 轮 codex 的 F9)——否则模型看到的是「筛查说没问题、运行时才炸」。"""
    assert rs.screen_source("import numpy" + chr(10) + "numpy._module" + chr(10))
    assert not rs.screen_source("import numpy as np" + chr(10) + "np.array([1, 2]).mean()" + chr(10))


def test_redact_paths_handles_the_json_shaped_windows_path():
    """JSON/repr 形态的 Windows 路径(两个反斜杠)原先只盖掉 `Users` 一段,
    尾部段(含用户名与文件名)留在外面(第 11 轮 codex 的 F4)。"""
    import json
    bs = chr(92)
    text = json.dumps({"p": "C:" + bs * 2 + "Users" + bs * 2 + "someone" + bs * 2 + "x.py"})
    cleaned = rs.redact_paths(text)
    assert "someone" not in cleaned, cleaned
    assert "x.py" not in cleaned, cleaned


def test_redact_paths_does_not_mangle_regex_literals():
    """正则字面量不是本机路径,不该被改坏(第 11 轮 codex 的 F5)。"""
    bs = chr(92)
    literal = 're.compile(r"' + bs * 2 + 'd+")'
    assert rs.redact_paths(literal) == literal


def test_redact_paths_still_covers_unc_and_extended_length_paths():
    """UNC 与扩展长度路径仍然要盖掉(改 UNC 规则时别把这两条弄丢)。"""
    bs = chr(92)
    unc = bs * 2 + "server" + bs + "share" + bs + "dir" + bs + "file"
    extended = bs * 2 + "?" + bs + "C:" + bs + "dir" + bs + "file"
    assert rs.redact_paths(unc) == "<路径>"
    assert rs.redact_paths(extended) == "<路径>"


def test_short_bearer_tokens_are_scrubbed():
    """Bearer 令牌原先要求 ≥8 字符,短令牌原样保留(第 11 轮 codex 的 F10)。"""
    assert "abcd" not in rs.redact_secrets("Authorization: Bearer abcd")


def test_scrub_text_can_scrub_known_secret_values_by_value():
    """**按值**清洗:名字那一层认不出来时(候选把裸值写进异常文本),
    按值替换是最后一道(第 11 轮 codex 的 F6)。"""
    assert "s3cr3tvalue" not in rs.scrub_text("令牌是 s3cr3tvalue", ["s3cr3tvalue"])
    # 太短的值不按值替换(否则「a」这种会把正文全糊掉)
    assert rs.scrub_text("a", ["a"]) == "a"


def test_the_audit_does_not_treat_submodules_as_leaked_capabilities():
    """`pandas.io` 是 pandas 的**子模块**,不是它泄漏的能力入口;
    按名字一律拒会把整个 `pandas.io` 命名空间打死(第 11 轮 codex 的 F8)。"""
    import pandas
    import pandas.io

    assert "io" not in rs.audit_module(pandas)
    assert rs.audit_module(pandas.io) == []


# ---------- 第 12 轮 codex 的回归用例 ----------

def test_redact_paths_covers_a_drive_letter_glued_to_a_letter():
    """盘符前紧贴一个 ASCII 字母时,整条绝对路径原先原样出去
    (第 12 轮 codex 的 M1 ①:那条否定前瞻是为躲 URL 加的,连带把真路径也躲了)。"""
    bs = chr(92)
    text = "openC:" + bs + "Users" + bs + "alice" + bs + ".env failed"
    cleaned = rs.redact_paths(text)
    assert "alice" not in cleaned, cleaned
    assert ".env" not in cleaned, cleaned
    # 尾部的散文是诊断信息,不该跟着没掉
    assert "failed" in cleaned, cleaned


def test_redact_paths_covers_a_user_name_containing_a_space():
    """用户目录里带空格的显示名原先只盖掉前半段,**OS 用户名**留在文本里
    (第 12 轮 codex 的 M1 ②)。"""
    bs = chr(92)
    text = "C:" + bs + "Users" + bs + "John Doe" + bs + ".env"
    cleaned = rs.redact_paths(text)
    assert "John" not in cleaned, cleaned
    assert "Doe" not in cleaned, cleaned


def test_redact_paths_covers_a_unc_host_with_a_space():
    """UNC 主机名原先不允许空格,带空格的主机名整条原样出去
    (第 12 轮 codex 的 M1 ③)。"""
    bs = chr(92)
    text = bs * 2 + "My Server" + bs + "share" + bs + "x.txt"
    assert rs.redact_paths(text) == "<路径>"


def test_redact_paths_keeps_the_sentence_around_a_path():
    """路径旁边的正文不该被一起吃掉(过度遮蔽会把诊断信息也弄没)。"""
    bs = chr(92)
    text = ("报错了:C:" + bs + "Users" + bs + "someone" + bs + "data" + bs + "approvals.json "
            "不可写。参考 https://example.com/docs/a/b 与 pending_strategies/cand_1.py")
    cleaned = rs.redact_paths(text)
    assert "approvals.json" not in cleaned, cleaned
    assert "不可写" in cleaned, cleaned
    assert "https://example.com/docs/a/b" in cleaned, cleaned
    assert "pending_strategies/cand_1.py" in cleaned, cleaned


def test_redact_paths_does_not_treat_a_repr_newline_as_a_path():
    """repr 里的换行转义(两个反斜杠)不是 UNC 起点——UNC 规则要是把它当成路径,
    后面的正文会被整段盖掉(过度遮蔽)。"""
    bs = chr(92)
    text = "line1" + bs + "nline2"
    assert rs.redact_paths(text) == text


def test_redact_digests_covers_a_digest_inside_a_longer_hex_string():
    """64 位摘要**藏在更长的十六进制串里**时原先原样出去(第 12 轮 codex 的 L2)——
    而「前面拼一段前缀、后面拼一段后缀」正是最常见的拼接形态。"""
    digest = "a" * 64
    text = "x=" + "0" * 32 + digest
    assert digest not in rs.redact_digests(text)


def test_redact_approval_urls_covers_the_form_without_a_scheme():
    """批准页地址不带 scheme 时同样是模型能直接用的地址(第 12 轮 codex 的 L4)。"""
    assert rs.redact_approval_urls("127.0.0.1:8765/a/TOKEN") != "127.0.0.1:8765/a/TOKEN"


def test_redact_secrets_covers_the_whole_cookie_header():
    """Cookie 头原先只隐去第一个值,后面的键值对原样出去(第 12 轮 codex 的 L5)。"""
    cleaned = rs.redact_secrets("Cookie: session=abc123def; other=1")
    assert "abc123def" not in cleaned, cleaned
    assert "other=1" not in cleaned, cleaned


def test_the_screening_sees_a_forbidden_name_hidden_in_a_string_concatenation():
    """字符串常量**拼接**起来藏禁用名,原先逐常量判定看不见
    (第 12 轮 codex 的 M4:这条规则的全部价值就是「人批准前能看见告警」)。"""
    src = "f = '{0.__gl' + 'obals__}'" + chr(10) + "w = f.format(np.load)" + chr(10)
    assert rs.screen_source(src), "拼接出来的禁用名没有被静态层看见"


def test_the_screening_allows_a_candidate_own_private_attributes():
    """`self._state` / `self._file` 是 LLM 写策略时最常见的私有字段名,
    静态层原先一刀切把它们**直接拒掉**(第 12 轮 codex 的 L1)——
    运行期的模块代理只拦模块属性,口径要一致。"""
    src = ("class C:" + chr(10) + "    def setup(self):" + chr(10)
           + "        self._state = {}" + chr(10)
           + "        self._file = None" + chr(10))
    assert not rs.screen_source(src)


def test_the_screening_still_flags_a_private_capability_attribute_on_a_module():
    """模块上的私有名藏能力入口(np._module)仍然要报(第 12 轮 codex 的 L1 的另一半)。"""
    src = "import numpy as np" + chr(10) + "np._module" + chr(10)
    assert rs.screen_source(src)


# ---------- 第 13 轮 codex 的回归用例 ----------

def test_redact_paths_covers_a_profile_directory_whose_name_has_a_space():
    """profile 目录本身就带空格(后面没有别的段)时原先只盖掉名字的前半段,
    **姓氏原样出去**——而 OS 用户名正是本机路径里最该盖掉的那一段
    (第 13 轮 codex 的 M1:那条规则的搜索起点是盘符之后,要求「Users 前面有分隔符」的
    老写法在起点处永远匹配不上)。"""
    bs = chr(92)
    text = "C:" + bs + "Users" + bs + "John Doe"
    cleaned = rs.redact_paths(text)
    assert "John" not in cleaned, cleaned
    assert "Doe" not in cleaned, cleaned


def test_redact_paths_covers_a_profile_directory_with_a_space_before_prose():
    """同一条形态后面还跟着散文时,姓氏同样不能漏。"""
    bs = chr(92)
    text = "打不开 C:" + bs + "Users" + bs + "John Doe 这个目录"
    cleaned = rs.redact_paths(text)
    assert "John" not in cleaned, cleaned
    assert "Doe" not in cleaned, cleaned


def test_redact_paths_still_keeps_the_prose_after_a_path_with_a_file_segment():
    """对照:路径后面跟的是散文时,散文要留着(过度遮蔽同样是信息损失)。"""
    bs = chr(92)
    text = "读不到 C:" + bs + "Users" + bs + "alice" + bs + "data" + bs + "x.json 里的内容"
    cleaned = rs.redact_paths(text)
    assert "x.json" not in cleaned, cleaned
    assert "里的内容" in cleaned, cleaned


# ---------- 第 14 轮 codex 的回归用例 ----------

def test_redact_paths_covers_a_forward_slash_path_with_a_multi_word_segment():
    """正斜杠路径的多词段原先整段漏掉(第 14 轮 codex 的 M1)。

    `Path.as_posix()` 是常见写法,候选 raise 的 `FileNotFoundError` 很容易带这种形态 ——
    目录名与文件名一起进模型通道,本机目录结构就出去了。
    根因:多词段靠「向前看分隔符」判断,而当时那个前瞻**只认反斜杠**
    (第 13 轮为了不吃掉散文里的相对路径而收窄的),于是正斜杠形态整条漏。
    现在按**这条路径自己的风格**认分隔符:反斜杠路径只认 `\\`,正斜杠路径只认 `/`。"""
    text = "error at C:/Documents and Settings/x.txt later"
    cleaned = rs.redact_paths(text)
    assert "Documents" not in cleaned, cleaned
    assert "Settings" not in cleaned, cleaned
    assert "x.txt" not in cleaned, cleaned
    assert "later" in cleaned, cleaned


def test_redact_paths_covers_a_forward_slash_program_files_path():
    """正斜杠形态的 `Program Files (x86)` 同样要盖住。"""
    text = "C:/Program Files (x86)/app.exe"
    cleaned = rs.redact_paths(text)
    assert "Program" not in cleaned, cleaned
    assert "app.exe" not in cleaned, cleaned


def test_redact_paths_still_keeps_a_relative_path_in_the_prose():
    """对照:散文里的相对路径**不能**被当成前一段路径的一部分
    (第 13 轮 codex 实测出来的回归——收窄前瞻就是为这个)。"""
    bs = chr(92)
    text = "报错了:C:" + bs + "x" + bs + "y 不可写。参考 pending_strategies/cand_1.py"
    cleaned = rs.redact_paths(text)
    assert "pending_strategies/cand_1.py" in cleaned, cleaned
    assert "不可写" in cleaned, cleaned


def test_redact_paths_covers_a_long_multi_word_segment():
    """超过 4 个词的路径段原先会漏掉尾部(第 14 轮 codex 的 L6)。"""
    bs = chr(92)
    text = "error at C:" + bs + "Users" + bs + "alice" + bs + "a b c d e f" + bs + "g.txt later"
    cleaned = rs.redact_paths(text)
    assert "g.txt" not in cleaned, cleaned
    assert "later" in cleaned, cleaned


def test_redact_paths_covers_a_posix_path_with_a_space():
    """POSIX 路径带空格的目录名原先同样漏掉尾部(第 14 轮 codex 的 L7)。"""
    text = "path is /home/john doe/secret.txt"
    cleaned = rs.redact_paths(text)
    assert "john" not in cleaned, cleaned
    assert "secret.txt" not in cleaned, cleaned


def test_redact_paths_keeps_the_prose_after_a_chinese_full_stop():
    """中文句号后面的正文原先被整段吃掉(第 14 轮 codex 的 L4)——
    过度遮蔽同样是信息损失。"""
    bs = chr(92)
    text = "C:" + bs + "Users" + bs + "alice" + bs + ".env。后面还有内容"
    cleaned = rs.redact_paths(text)
    assert "后面还有内容" in cleaned, cleaned
    assert ".env" not in cleaned, cleaned


def test_redact_paths_does_not_eat_a_prose_word_after_a_users_directory():
    """`C:\\Users\\alice failed to load` 里那个 `failed` 是散文,不是路径的一部分
    (第 14 轮 codex 的 L5):profile 目录那条「额外多吃一个词」的规则
    只在那个词**像显示名**(`John Doe`)时才生效。"""
    bs = chr(92)
    text = "C:" + bs + "Users" + bs + "alice failed to load"
    cleaned = rs.redact_paths(text)
    assert "failed" in cleaned, cleaned
    assert "alice" not in cleaned, cleaned
