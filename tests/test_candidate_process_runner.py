"""框架侧候选进程启动器(`scripts/research_candidate_process.py`)的行为探针(Issue #29 S1 · P2 T2.1)。

放行:正常候选照常拿到等长信号。拦截:出错 / 超时 / 输出洪水 / 污染协议通道 / 畸形响应,
一律整格判失败(抛 `CandidateProcessError`),不截断、不修补,并且不留临时目录。
"""
from __future__ import annotations

import glob
import importlib.util
import tempfile
import time
from pathlib import Path

import pytest

import research_candidate_process as rcp
from research.candidate import protocol as proto
from test_candidate_process_isolation import PASS_SOURCE, synth_bars


def _require_talib() -> None:
    """候选进程里的指标用 ta-lib;研究侧 CI 不装它 ⇒ 干净地跳过,而不是红。"""
    if importlib.util.find_spec("talib") is None:
        pytest.skip("需要 ta-lib(候选侧指标依赖);研究侧 CI 不装")


def _leftovers() -> set[str]:
    base = Path(tempfile.gettempdir())
    return {p for p in glob.glob(str(base / "cand-rt-*")) + glob.glob(str(base / "cand-cwd-*"))}


def _run(source: str, *, bars=None, timeout: float = 60.0, params: dict | None = None, class_name: str = "Candidate"):
    _require_talib()
    return rcp.run_candidate_signals(source, class_name, params or {}, bars if bars is not None else synth_bars(120),
                                     timeout=timeout)


# ---------------------------------------------------------------- 放行


def test_good_candidate_gets_equal_length_signals_and_leaves_no_temp_dirs():
    before = _leftovers()
    bars = synth_bars(200)
    signals = _run(PASS_SOURCE, bars=bars)
    assert len(signals) == len(bars) and set(signals) <= {-1, 0, 1}
    assert any(signals)
    assert _leftovers() == before, "临时目录没有清干净"


def test_params_reach_the_candidate():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def entry_signal(self):
        return 1 if self.fast_window == 3 else 0
'''
    bars = synth_bars(120)
    assert any(_run(src, bars=bars, params={"fast_window": 3, "slow_window": 10, "atr_window": 5}))
    assert not any(_run(src, bars=bars, params={"fast_window": 4, "slow_window": 10, "atr_window": 5}))


# ---------------------------------------------------------------- 拦截


def test_candidate_exception_is_a_candidate_error_with_untrusted_detail():
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        raise ValueError("boom")
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "candidate_error" and "ValueError" in err.value.detail
    assert "boom" not in err.value.message, "候选写的文字只能在 detail 里,message 是框架自己的固定说明"


def test_timeout_kills_the_process_and_cleans_up():
    before = _leftovers()
    src = '''
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        while True:
            pass
'''
    started = time.monotonic()
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src, timeout=3.0)
    assert err.value.kind == "timeout"
    assert time.monotonic() - started < 30
    assert _leftovers() == before


def test_stdout_flood_is_cut_off():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        chunk = b"x" * (1 << 20)
        for _ in range(64):
            sys.__stdout__.buffer.write(chunk)
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "output_limit"


def test_stderr_flood_is_cut_off():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        chunk = "y" * (1 << 20)
        for _ in range(64):
            sys.stderr.write(chunk)
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src, timeout=60)
    assert err.value.kind == "output_limit"


def test_junk_written_to_the_real_stdout_corrupts_the_frames_and_fails_closed():
    src = '''
import sys
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        sys.__stdout__.buffer.write(b"not a frame at all")
        sys.__stdout__.buffer.flush()
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_candidate_that_exits_silently_is_a_protocol_failure_not_a_hang():
    src = '''
import os
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        os._exit(0)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_no_bars_is_refused_before_launching():
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp.run_candidate_signals(PASS_SOURCE, "Candidate", {}, [])
    assert err.value.kind == "protocol"


# ---------------------------------------------------------------- 响应解析(纯函数,不起进程)


def _frames(*payloads: bytes) -> bytes:
    return b"".join(proto.encode_frame(p) for p in payloads)


def _head(**over) -> bytes:
    body = {"magic": proto.MAGIC_RESPONSE, "status": "ok", "n_bars": 3, "error": ""}
    body.update(over)
    return proto.encode_json(body)


def test_parse_response_accepts_a_well_formed_response():
    raw = _frames(_head(), proto.pack_signals([1, 0, -1]))
    assert rcp._parse_response(raw, 3) == [1, 0, -1]


@pytest.mark.parametrize("raw, why", [
    (_frames(_head(magic="WRONG"), proto.pack_signals([0, 0, 0])), "magic 不符"),
    (_frames(_head(status="weird"), proto.pack_signals([0, 0, 0])), "status 非法"),
    (_frames(_head(), proto.pack_signals([0, 0])), "信号序列比框架发出去的根数短"),
    (_frames(_head(), proto.pack_signals([0, 0, 0, 0])), "信号序列比框架发出去的根数长"),
    (_frames(_head(), bytes([2, 0, 0])), "取值域越界"),
    (_frames(_head(), proto.pack_signals([0, 0, 0])) + b"\x00", "响应后面多余字节"),
    (_frames(_head()), "缺信号帧"),
    (b"\x00\x00", "帧头不完整"),
])
def test_parse_response_rejects_anything_malformed(raw, why):
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp._parse_response(raw, 3)
    assert err.value.kind == "protocol", why


def test_parse_response_trusts_the_framework_bar_count_not_the_self_reported_one():
    """响应头里自报 n_bars=999 也没用:长度只认框架发出去的根数。"""
    raw = _frames(_head(n_bars=999), proto.pack_signals([1, 0, -1]))
    assert rcp._parse_response(raw, 3) == [1, 0, -1]


def test_candidate_error_head_carries_the_text_only_in_detail():
    raw = _frames(_head(status="error", error="ValueError: secret-looking text"))
    with pytest.raises(rcp.CandidateProcessError) as err:
        rcp._parse_response(raw, 3)
    assert err.value.kind == "candidate_error" and "secret-looking" in err.value.detail
    assert "secret-looking" not in err.value.message


def test_host_source_fits_the_command_line_budget():
    assert len(rcp.host_source()) < rcp.MAX_HOST_SOURCE_CHARS * 0.6, "宿主源码接近命令行上限,先瘦身再加功能"


# ---------------------------------------------------------------- A5:框架不采信候选的任何自报


def test_a5_candidate_forging_its_own_response_frames_is_rejected():
    """候选自己往真实 stdout 写一份「合法」响应,宿主随后又写自己的:多出来的字节一律拒绝(不取其一)。"""
    src = '''
import sys
from candidate import protocol as p
from candidate.signal_template import SignalTemplate

class Candidate(SignalTemplate):
    def run(self, bars):
        out = sys.__stdout__.buffer
        out.write(p.encode_frame(p.encode_json({"magic": p.MAGIC_RESPONSE, "status": "ok",
                                                "n_bars": len(bars), "sharpe": 42, "error": ""})))
        out.write(p.encode_frame(p.pack_signals([1] * len(bars))))
        out.flush()
        return super().run(bars)
'''
    with pytest.raises(rcp.CandidateProcessError) as err:
        _run(src)
    assert err.value.kind == "protocol"


def test_a5_framework_reads_only_magic_status_and_error_from_the_response_head():
    """AST 不变量:`_parse_response` 里对响应头只取 magic / status / error 三个键,没有任何指标类的键。

    候选无论往头里塞什么(sharpe / metrics / trials / pnls ...),框架代码里就没有读它的地方。"""
    import ast
    tree = ast.parse(Path(rcp.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_parse_response")
    keys = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "head" \
                and node.args and isinstance(node.args[0], ast.Constant):
            keys.add(node.args[0].value)
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "head" \
                and isinstance(node.slice, ast.Constant):
            keys.add(node.slice.value)
    assert keys == {"magic", "status", "error"}, keys


def test_a5_no_metric_like_key_is_read_from_candidate_output_anywhere_in_the_new_path():
    """新路径的三个框架侧文件里,不出现从候选输出里取指标类键的写法(sharpe / metrics / stats / pnls / trials)。"""
    import ast
    banned = {"sharpe", "metrics", "stats", "pnls", "trials", "dsr", "maxdd"}
    for module in (rcp, __import__("research_signal_replay")):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        hits = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                    and isinstance(node.slice.value, str) and node.slice.value in banned:
                hits.add(node.slice.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                    and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value in banned:
                hits.add(node.args[0].value)
        assert not hits, f"{module.__name__} 里出现了读指标类键的写法: {hits}"


def test_ported_example_candidate_parses_and_passes_screening_only_on_the_new_path():
    """示例候选(由 atr_breakout_15m_2atr 等价移植)在新路径上可解析、可过筛查;旧路径白名单照旧不放行 `candidate`。"""
    import research_candidate_spec as spec_mod
    import research_sandbox as rs
    example = Path(__file__).parent / "fixtures" / "candidate_process" / "atr_breakout_15m_2atr_signal.py"
    source = example.read_text(encoding="utf-8")
    spec = spec_mod.parse_spec(source)
    assert spec.class_name == "Candidate" and len(spec.param_grid) == 3
    assert all(isinstance(v, int) for v in spec.param_grid["breakout_n"]), "整数取值必须保持整数"
    assert rs.screen_source(source, extra_allowed=["candidate"]) == []
    assert any("candidate" in finding for finding in rs.screen_source(source)), "关着开关时白名单不得放行 candidate"
