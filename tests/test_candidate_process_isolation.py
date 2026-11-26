"""候选进程化 S1 · P1:候选侧运行时与宿主进程的动态探针(Issue #29,ADR 20261115 §7 S1)。

每条边界都有「放行 + 拦截」双向探针:光证明"拦住了坏的"不够,还得证明"好的照常通过"。
探针经**真实子进程**(`python -E -P -B`)运行,不是在测试进程里模拟——静态推理必漏,要实测。

候选的观测手段:候选只能回信号,探针候选把观测结果塞进它抛出的异常文本里,宿主会把它变成
结构化错误帧(`status=error`)送回来。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from research.candidate import protocol as proto
from research.candidate.bars import Bar

ROOT = Path(__file__).resolve().parents[1]
HOST = ROOT / "scripts" / "research_candidate_host.py"
RUNTIME_SRC = ROOT / "research" / "candidate"


# ---------------------------------------------------------------- 辅助


def synth_bars(n: int, seed: int = 1, start: datetime = datetime(2021, 1, 4, 9, 0)) -> list[Bar]:
    """确定性随机游走 15 分钟线(不依赖本机行情文件,CI 可跑)。"""
    rng = random.Random(seed)
    price = 2500.0
    out: list[Bar] = []
    for i in range(n):
        o = price
        c = o + rng.uniform(-8, 8)
        h = max(o, c) + rng.uniform(0, 4)
        lo = min(o, c) - rng.uniform(0, 4)
        out.append(Bar(open_price=o, high_price=h, low_price=lo, close_price=c,
                       volume=float(rng.randint(100, 5000)), turnover=0.0,
                       open_interest=float(rng.randint(10000, 20000)),
                       datetime=start + timedelta(minutes=15 * i), interval="15m"))
        price = c
    return out


def _sandbox_env() -> dict[str, str]:
    import research_sandbox as rs
    return rs.sandbox_env(os.environ)


def run_host(source: str, *, class_name: str = "Candidate", params: dict | None = None,
             bars: list[Bar] | None = None, env_extra: dict | None = None,
             stdin_override: bytes | None = None, timeout: float = 120.0):
    """按设计文档 4.3 节的形态真起一个候选宿主进程,返回 (控制帧, 信号|None, cwd, 原始 stdout, stderr)。"""
    bars = bars if bars is not None else synth_bars(80)
    runtime = Path(tempfile.mkdtemp(prefix="cand-rt-"))
    workdir = Path(tempfile.mkdtemp(prefix="cand-cwd-"))
    try:
        shutil.copytree(RUNTIME_SRC, runtime / "candidate", ignore=shutil.ignore_patterns("__pycache__"))
        (runtime / "request.json").write_text(json.dumps({
            "class": class_name, "params": params or {}, "interval": "15m",
            "n_bars": len(bars), "source": source}), encoding="utf-8")
        env = _sandbox_env()
        env["CANDIDATE_RUNTIME_DIR"] = str(runtime)
        env.update(env_extra or {})
        payload = stdin_override if stdin_override is not None else proto.encode_frame(proto.pack_bars(bars))
        # 宿主**按源码文本**起(`-c`),不按路径起:按路径起时 `sys.orig_argv` 与调用栈帧的
        # `co_filename` 都会把仓库路径交给候选(A2 探针实测抓到的泄露)。
        done = subprocess.run([sys.executable, "-E", "-P", "-B", "-c", HOST.read_text(encoding="utf-8")], input=payload,
                              capture_output=True, cwd=workdir, env=env, timeout=timeout)
        head, signals = parse_output(done.stdout)
        return head, signals, workdir, done.stdout, done.stderr.decode("utf-8", "replace")
    finally:
        shutil.rmtree(runtime, ignore_errors=True)
        # workdir 留给调用方检查后由 cleanup 删除:A3 探针要看里面有没有候选写的文件


def parse_output(raw: bytes):
    stream = io.BytesIO(raw)
    head = proto.decode_json(proto.read_frame(stream))
    signals = None
    if head.get("status") == "ok":
        signals = proto.unpack_signals(proto.read_frame(stream), int(head["n_bars"]))
    return head, signals


def observed(head: dict) -> dict:
    """探针候选把观测结果 JSON 塞在异常文本里:`RuntimeError: {...}`。"""
    assert head["status"] == "error", head
    text = head["error"]
    return json.loads(text[text.index("{"):])


PASS_SOURCE = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    pass
'''


# ---------------------------------------------------------------- 放行:好候选照常通过


def test_good_candidate_returns_signal_sequence_of_same_length():
    """放行探针:正常候选能起来、回来的是等长且取值合法的信号序列。"""
    bars = synth_bars(200)
    head, signals, workdir, _, stderr = run_host(PASS_SOURCE, bars=bars)
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "ok", (head, stderr)
    assert head["n_bars"] == len(bars)
    assert signals is not None and len(signals) == len(bars)
    assert set(signals) <= {-1, 0, 1}
    assert any(s != 0 for s in signals), "200 根随机游走上默认双均线应当至少发出一次信号"


def test_candidate_extra_metric_fields_are_not_in_protocol():
    """A5 的协议侧:响应帧里只有 magic/status/error/n_bars + 信号帧;候选想塞 sharpe 无处可放。"""
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    sharpe = 42
    def run(self, bars):
        out = super().run(bars)
        self.sharpe = 42
        return out
'''
    head, signals, workdir, _, _ = run_host(src)
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "ok"
    assert set(head) == {"magic", "status", "n_bars", "error"}
    assert "sharpe" not in json.dumps(head)
    assert set(signals) <= {-1, 0, 1}


# ---------------------------------------------------------------- A1:候选拿不到框架模块


def test_a1_candidate_cannot_import_framework_modules_but_can_import_its_runtime():
    src = '''
import importlib, json
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        out = {}
        for name in ("risk", "research_grid", "scripts.backtest_lib", "backtest_lib", "strategies",
                     "research_sandbox", "walk_forward_optimizer", "research_gate", "bootstrap"):
            try:
                importlib.import_module(name)
                out[name] = "IMPORTED"
            except ImportError:
                out[name] = "blocked"
        try:
            importlib.import_module("candidate.protocol")
            out["candidate.protocol"] = "ok"
        except ImportError:
            out["candidate.protocol"] = "MISSING"
        raise RuntimeError(json.dumps(out))
'''
    head, _, workdir, _, _ = run_host(src)
    shutil.rmtree(workdir, ignore_errors=True)
    seen = observed(head)
    assert seen["candidate.protocol"] == "ok"
    leaked = {k: v for k, v in seen.items() if k != "candidate.protocol" and v == "IMPORTED"}
    assert not leaked, f"候选进程里不该能 import 的框架模块被导入了: {leaked}"


# ---------------------------------------------------------------- A2:不知道仓库在哪


def _probe(src_body: str, *, env_extra: dict | None = None):
    """把一小段探针体包成候选并跑一次,返回观测 dict。"""
    src = ("import json, os, sys\nfrom candidate.signal_template import SignalTemplate\n\n"
           "class Candidate(SignalTemplate):\n    def run(self, bars):\n        out = {}\n"
           + "".join("        " + line + "\n" for line in src_body.strip().splitlines())
           + "        raise RuntimeError(json.dumps(out))\n")
    head, _, workdir, _, _ = run_host(src, env_extra=env_extra)
    try:
        return observed(head), workdir
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_a2_cwd_is_not_repo_root():
    seen, workdir = _probe('out["cwd"] = os.getcwd()')
    assert Path(seen["cwd"]).resolve() != ROOT
    assert not str(Path(seen["cwd"]).resolve()).startswith(str(ROOT))


def test_a2_sys_path_has_no_repo_path():
    seen, _ = _probe('out["bad"] = [p for p in sys.path if %r in os.path.abspath(p or ".")]' % str(ROOT))
    assert seen["bad"] == []


def test_a2_argv_and_main_file_are_scrubbed():
    seen, _ = _probe('m = sys.modules.get("__main__")\nout["argv"] = list(sys.argv)\nout["main"] = getattr(m, "__file__", None)')
    assert all(str(ROOT) not in a for a in seen["argv"])
    assert seen["main"] is None or str(ROOT) not in str(seen["main"])


def test_a2_orig_argv_does_not_leak_repo_path():
    # 布尔值在候选端算:orig_argv 里现在装着整份宿主源码,整个回传会被 400 字符的错误截断弄坏 JSON
    seen, _ = _probe('out["leak"] = any(%r in a for a in getattr(sys, "orig_argv", []))\n'
                     'out["has_orig_argv"] = hasattr(sys, "orig_argv")' % str(ROOT))
    assert seen["has_orig_argv"] is True, "探针前提:这个解释器应当有 sys.orig_argv(没有就说明探针空转)"
    assert seen["leak"] is False


def test_a2_call_stack_filenames_do_not_leak_repo_path():
    seen, _ = _probe(
        'files = []\nf = sys._getframe()\nwhile f is not None:\n    files.append(f.f_code.co_filename)\n    f = f.f_back\n'
        'out["frames"] = files')
    leaked = [x for x in seen["frames"] if str(ROOT) in x]
    assert leaked == [], leaked


def test_a2_credentials_in_parent_env_do_not_reach_candidate():
    seen, _ = _probe('out["keys"] = [k for k in os.environ if k.startswith(("CTP_", "WEBHOOK_", "LIVE_", "BISHENG"))]',
                     env_extra=None)
    assert seen["keys"] == []


# ---------------------------------------------------------------- A3:候选的写不外溢


def _tree_digest(*dirs: str) -> str:
    h = hashlib.sha256()
    for d in dirs:
        for p in sorted((ROOT / d).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            h.update(str(p.relative_to(ROOT)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_a3_candidate_can_write_in_its_cwd_but_framework_files_are_untouched():
    before = _tree_digest("scripts", "risk", "strategies", "research")
    src = '''
import json, os
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        with open(os.path.join(os.getcwd(), "scratch.txt"), "w", encoding="utf-8") as fh:
            fh.write("candidate was here")
        raise RuntimeError(json.dumps({"wrote": True}))
'''
    head, _, workdir, _, _ = run_host(src)
    try:
        assert observed(head) == {"wrote": True}
        assert (workdir / "scratch.txt").read_text(encoding="utf-8") == "candidate was here"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    assert _tree_digest("scripts", "risk", "strategies", "research") == before


# ---------------------------------------------------------------- 宿主 fail-closed


@pytest.mark.parametrize("body, label", [
    ("raise SystemExit(0)", "候选自己 exit()"),
    ("raise ValueError('boom')", "候选抛异常"),
])
def test_candidate_failure_becomes_structured_error_not_crash(body, label):
    src = f'''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        {body}
'''
    head, signals, workdir, _, _ = run_host(src)
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "error", label
    assert signals is None


def test_candidate_returning_illegal_signal_value_is_an_error():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        return [2] * len(bars)
'''
    head, signals, workdir, _, _ = run_host(src)
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "error" and signals is None


def test_candidate_print_does_not_corrupt_protocol_channel():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        print("hello from candidate")
        return super().run(bars)
'''
    head, signals, workdir, raw, _ = run_host(src)
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "ok" and b"hello from candidate" not in raw
    assert len(signals) == head["n_bars"]


def test_truncated_bar_frame_is_an_error_not_a_hang():
    head, signals, workdir, _, _ = run_host(PASS_SOURCE, stdin_override=b"\x00\x00")
    shutil.rmtree(workdir, ignore_errors=True)
    assert head["status"] == "error" and signals is None


# ---------------------------------------------------------------- 协议:畸形输入 fail-closed(T1.3)


def test_protocol_roundtrip_bars_and_signals():
    bars = synth_bars(50)
    back = proto.unpack_bars(proto.pack_bars(bars), len(bars), "15m")
    assert [b.close_price for b in back] == [b.close_price for b in bars]
    assert [b.datetime for b in back] == [b.datetime for b in bars]
    sig = [1, 0, -1, 0, 1]
    assert proto.unpack_signals(proto.pack_signals(sig), len(sig)) == sig


@pytest.mark.parametrize("payload, count", [
    (b"\x00" * 10, 1),            # 字节数不符
    (b"", 1),                      # 空
])
def test_unpack_bars_rejects_wrong_size(payload, count):
    with pytest.raises(proto.ProtocolError):
        proto.unpack_bars(payload, count)


def test_unpack_signals_rejects_wrong_length_and_value():
    with pytest.raises(proto.ProtocolError):
        proto.unpack_signals(b"\x01\x00", 3)
    with pytest.raises(proto.ProtocolError):
        proto.unpack_signals(bytes([2]), 1)
    with pytest.raises(proto.ProtocolError):
        proto.pack_signals([5])


def test_frame_rejects_truncation_and_oversize():
    with pytest.raises(proto.ProtocolError):
        proto.read_frame(io.BytesIO(b"\x00\x00"))                       # 帧头不完整
    with pytest.raises(proto.ProtocolError):
        proto.read_frame(io.BytesIO((10).to_bytes(4, "big") + b"abc"))   # 载荷不完整
    with pytest.raises(proto.ProtocolError):
        proto.read_frame(io.BytesIO((proto.MAX_FRAME_BYTES + 1).to_bytes(4, "big")))  # 超限
    with pytest.raises(proto.ProtocolError):
        proto.encode_frame(b"x" * (proto.MAX_FRAME_BYTES + 1))


def test_decode_json_rejects_non_object_and_garbage():
    with pytest.raises(proto.ProtocolError):
        proto.decode_json(b"[1,2]")
    with pytest.raises(proto.ProtocolError):
        proto.decode_json(b"\xff\xfe")


# ---------------------------------------------------------------- T1.1 / T1.2:与 vn.py 的数值与字段等价(需要交易栈)


def _vnpy_pair(n: int):
    vn_utility = pytest.importorskip("vnpy.trader.utility")
    vn_object = pytest.importorskip("vnpy.trader.object")
    from vnpy.trader.constant import Exchange, Interval
    bars = synth_bars(n)
    vbars = [vn_object.BarData(gateway_name="t", symbol="X", exchange=Exchange.DCE, datetime=b.datetime,
                               interval=Interval.MINUTE, volume=b.volume, turnover=b.turnover,
                               open_interest=b.open_interest, open_price=b.open_price,
                               high_price=b.high_price, low_price=b.low_price, close_price=b.close_price)
             for b in bars]
    return bars, vbars, vn_utility


def test_indicators_match_vnpy_array_manager_bar_by_bar():
    from research.candidate.indicators import ArrayManager as Mine
    import numpy as np
    bars, vbars, vn_utility = _vnpy_pair(160)
    mine, theirs = Mine(60), vn_utility.ArrayManager(60)
    for b, vb in zip(bars, vbars):
        mine.update_bar(b)
        theirs.update_bar(vb)
        assert mine.inited == theirs.inited and mine.count == theirs.count
        if not mine.inited:
            continue
        for name, args in (("sma", (10,)), ("sma", (40,)), ("ema", (10,)), ("std", (20,)), ("rsi", (14,)), ("atr", (14,))):
            assert getattr(mine, name)(*args) == getattr(theirs, name)(*args), (name, args)
        up_m, dn_m = mine.donchian(20, array=True)
        up_t, dn_t = theirs.donchian(20, array=True)
        assert np.array_equal(up_m, up_t, equal_nan=True) and np.array_equal(dn_m, dn_t, equal_nan=True)


def test_bar_fields_align_with_vnpy_bar_data():
    from dataclasses import fields
    _, vbars, _ = _vnpy_pair(2)
    mine = {f.name for f in fields(Bar)} - {"extra", "interval"}
    theirs = set(vbars[0].__dict__)
    assert mine <= theirs, f"候选侧 Bar 的字段在 vnpy BarData 里找不到: {mine - theirs}"
